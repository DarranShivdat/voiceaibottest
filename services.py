"""Swappable STT / LLM / TTS service factories.

Provider choices come from `config` (env vars), so swapping a provider is a
one-line change in `.env`. The TTS factory wires an automatic failover: if the
primary TTS errors or times out, Pipecat's ServiceSwitcher transparently routes
to the fallback provider.
"""

import os

from loguru import logger

from pipecat.pipeline.service_switcher import (
    ServiceSwitcher,
    ServiceSwitcherStrategyFailover,
)
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService

import config


# --- STT --------------------------------------------------------------------

def build_stt():
    if config.STT_PROVIDER == "deepgram":
        return DeepgramSTTService(
            api_key=os.environ["DEEPGRAM_API_KEY"],
            settings=DeepgramSTTService.Settings(model=config.DEEPGRAM_MODEL),
        )
    raise ValueError(f"Unsupported STT_PROVIDER: {config.STT_PROVIDER!r} (supported: deepgram)")


# --- LLM --------------------------------------------------------------------

def build_llm():
    provider = config.LLM_PROVIDER
    if provider == "anthropic":
        return AnthropicLLMService(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            settings=AnthropicLLMService.Settings(
                model=config.ANTHROPIC_MODEL,
                max_tokens=1024,
            ),
        )
    if provider == "openai":
        from pipecat.services.openai.llm import OpenAILLMService

        return OpenAILLMService(
            api_key=os.environ["OPENAI_API_KEY"],
            settings=OpenAILLMService.Settings(model=config.OPENAI_MODEL),
        )
    if provider in ("google", "gemini"):
        from pipecat.services.google.llm import GoogleLLMService

        return GoogleLLMService(
            api_key=os.environ["GOOGLE_API_KEY"],
            settings=GoogleLLMService.Settings(model=config.GOOGLE_MODEL),
        )
    raise ValueError(
        f"Unsupported LLM_PROVIDER: {provider!r} (supported: anthropic, openai, google)"
    )


# --- TTS --------------------------------------------------------------------

def _build_single_tts(provider: str):
    if provider == "cartesia":
        return CartesiaTTSService(
            api_key=os.environ["CARTESIA_API_KEY"],
            settings=CartesiaTTSService.Settings(
                model=config.CARTESIA_MODEL,
                voice=config.CARTESIA_VOICE,
            ),
        )
    if provider == "deepgram":
        from pipecat.services.deepgram.tts import DeepgramTTSService

        # Reuses the Deepgram key already required for STT — a zero-config fallback.
        return DeepgramTTSService(api_key=os.environ["DEEPGRAM_API_KEY"])
    if provider == "openai":
        from pipecat.services.openai.tts import OpenAITTSService

        return OpenAITTSService(api_key=os.environ["OPENAI_API_KEY"])
    raise ValueError(
        f"Unsupported TTS provider: {provider!r} (supported: cartesia, deepgram, openai)"
    )


def build_tts():
    """Return the TTS processor for the pipeline.

    If a (different) fallback provider is configured, returns a ServiceSwitcher
    that auto-fails-over from primary -> fallback on a non-fatal error.
    """
    primary = _build_single_tts(config.TTS_PROVIDER)

    fallback = config.TTS_FALLBACK_PROVIDER
    if not fallback or fallback == config.TTS_PROVIDER:
        logger.info(f"TTS: {config.TTS_PROVIDER} (no fallback configured)")
        return primary

    backup = _build_single_tts(fallback)
    switcher = ServiceSwitcher(
        services=[primary, backup],
        strategy_type=ServiceSwitcherStrategyFailover,
    )

    @switcher.strategy.event_handler("on_service_switched")
    async def _on_service_switched(strategy, service):  # noqa: ANN001
        logger.warning(f"[TTS FAILOVER] now using {service}")

    logger.info(f"TTS: {config.TTS_PROVIDER} (auto-failover -> {fallback})")
    return switcher
