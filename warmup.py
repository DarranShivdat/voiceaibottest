"""Server-startup pre-warming so the FIRST real call is already fast.

Runs ONCE at process boot (before the runner accepts any connection), not
per-session. It:

  1. Pre-loads the Silero VAD and Smart Turn v3 ONNX models into memory (and runs
     a dummy inference) so they aren't lazy-loaded on the first utterance.
  2. Sends one tiny request to each configured service — a 1-token Anthropic LLM
     call, a minimal Deepgram STT init, and a short Cartesia TTS synth — to
     establish DNS/TLS/connections so the first real turn doesn't pay that cost.

Every step is best-effort and time-boxed: a warmup failure logs a warning and
never blocks the process from starting. This does not change any flow behavior,
providers, or the per-session pipeline — it only pre-warms shared/process-level
state.
"""

import asyncio
import os

import numpy as np
from loguru import logger

import config

# Hold the pre-loaded model instances for the life of the process so their
# loaded ONNX sessions and the native onnxruntime stay warm.
_WARM_MODELS: list = []

# Per-step ceiling so a bad key / network hang can't stall boot.
_STEP_TIMEOUT_S = 12.0


async def _warm_models() -> None:
    """Load the local VAD + Smart Turn ONNX models (bundled — no network)."""
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams

    # Match the VAD params used per-session in bot.py (params don't affect the
    # load, but keeps the warmed instance representative).
    vad = SileroVADAnalyzer(params=VADParams(confidence=0.7, start_secs=0.2, stop_secs=0.6))
    try:
        vad.set_sample_rate(16000)
        silent = b"\x00\x00" * vad.num_frames_required()  # int16 silence
        vad.voice_confidence(silent)  # warm the inference path
    except Exception as e:  # noqa: BLE001 - construction already did the heavy load
        logger.debug(f"Warmup: VAD dummy inference skipped ({e}).")
    _WARM_MODELS.append(vad)
    logger.info("Warmup: Silero VAD model loaded.")

    turn = LocalSmartTurnAnalyzerV3()
    try:
        turn._predict_endpoint(np.zeros(16000, dtype=np.float32))  # 1s silence @16k
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Warmup: Smart Turn dummy inference skipped ({e}).")
    _WARM_MODELS.append(turn)
    logger.info("Warmup: Smart Turn v3 model loaded.")


async def _warm_llm() -> None:
    """1-token Anthropic completion to establish the LLM connection."""
    if config.LLM_PROVIDER != "anthropic":
        logger.info(f"Warmup: LLM warmup skipped (provider={config.LLM_PROVIDER}).")
        return
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        logger.warning("Warmup: ANTHROPIC_API_KEY not set; skipping LLM warmup.")
        return

    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=key)
    try:
        await client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=1,
            messages=[{"role": "user", "content": "warmup"}],
        )
        logger.info(f"Warmup: Anthropic LLM ({config.ANTHROPIC_MODEL}) connection established.")
    finally:
        await client.close()


async def _warm_stt() -> None:
    """Minimal Deepgram STT init + DNS/TLS warm to the Deepgram API host."""
    if config.STT_PROVIDER != "deepgram":
        logger.info(f"Warmup: STT warmup skipped (provider={config.STT_PROVIDER}).")
        return
    key = os.getenv("DEEPGRAM_API_KEY")
    if not key:
        logger.warning("Warmup: DEEPGRAM_API_KEY not set; skipping STT warmup.")
        return

    # Construct the same SDK client the STT service uses (the "init").
    try:
        from deepgram import AsyncDeepgramClient

        AsyncDeepgramClient(api_key=key)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Warmup: Deepgram client init note ({e}).")

    # Warm DNS/TLS to api.deepgram.com (the same host the STT websocket uses).
    import httpx

    async with httpx.AsyncClient(timeout=8.0) as http:
        resp = await http.get(
            "https://api.deepgram.com/v1/projects",
            headers={"Authorization": f"Token {key}"},
        )
    logger.info(f"Warmup: Deepgram STT connection established (HTTP {resp.status_code}).")


async def _warm_tts() -> None:
    """Short Cartesia TTS synth (HTTP /tts/bytes) to establish the connection."""
    if config.TTS_PROVIDER != "cartesia":
        logger.info(f"Warmup: TTS warmup skipped (provider={config.TTS_PROVIDER}).")
        return
    key = os.getenv("CARTESIA_API_KEY")
    if not key:
        logger.warning("Warmup: CARTESIA_API_KEY not set; skipping TTS warmup.")
        return

    import httpx

    payload = {
        "model_id": config.CARTESIA_MODEL,
        "transcript": "Hello.",
        "voice": {"mode": "id", "id": config.CARTESIA_VOICE},
        "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 16000},
    }
    headers = {"Cartesia-Version": "2026-03-01", "X-API-Key": key}
    async with httpx.AsyncClient(timeout=8.0) as http:
        resp = await http.post(
            "https://api.cartesia.ai/tts/bytes", json=payload, headers=headers
        )
    logger.info(f"Warmup: Cartesia TTS synth complete (HTTP {resp.status_code}).")


async def run_warmup() -> None:
    """Run all warmup steps once at process startup. Never raises."""
    logger.info("Warming up: pre-loading models and establishing service connections...")
    steps = [
        ("models", _warm_models),
        ("LLM", _warm_llm),
        ("STT", _warm_stt),
        ("TTS", _warm_tts),
    ]
    for name, fn in steps:
        try:
            await asyncio.wait_for(fn(), timeout=_STEP_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning(f"Warmup step '{name}' timed out after {_STEP_TIMEOUT_S}s; continuing.")
        except Exception as e:  # noqa: BLE001 - warmup is best-effort
            logger.warning(f"Warmup step '{name}' failed ({e}); continuing.")
    logger.info("Warmup complete")
