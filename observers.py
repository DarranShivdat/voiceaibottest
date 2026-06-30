"""Observability: full transcript + per-turn latency.

`TranscriptObserver` taps the pipeline's frame stream to:
  * log every user and agent turn to the console AND a timestamped file in
    `transcripts/` (the compliance/audit record), and
  * log per-turn STT / LLM / TTS time-to-first-byte (TTFB) so we can see whether
    each turn is under the ~800ms target.

Pipecat's built-in `MetricsLogObserver` is used alongside this (in bot.py) for
detailed per-service metric logging to the console.
"""

from datetime import datetime
from pathlib import Path

from loguru import logger

from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    MetricsFrame,
    TranscriptionFrame,
    TTSTextFrame,
)
from pipecat.metrics.metrics import TTFBMetricsData
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService

TRANSCRIPT_DIR = Path(__file__).parent / "transcripts"


class TranscriptObserver(BaseObserver):
    def __init__(self):
        super().__init__()
        self._file = None
        self._path = None
        self._bot_buffer: list[str] = []
        self._ttfb: dict[str, float] = {}  # 'STT' | 'LLM' | 'TTS' -> seconds

    # -- session lifecycle --------------------------------------------------
    def start_session(self) -> None:
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._path = TRANSCRIPT_DIR / f"transcript-{ts}.txt"
        self._file = open(self._path, "a", encoding="utf-8")
        self._write(f"=== Conversation transcript {datetime.now().isoformat(timespec='seconds')} ===")
        logger.info(f"📝 Saving transcript to {self._path}")

    def close(self) -> None:
        self._flush_bot_turn()
        if self._file:
            self._write("=== End of transcript ===")
            self._file.close()
            self._file = None

    # -- writing ------------------------------------------------------------
    def _write(self, line: str) -> None:
        logger.info(line)
        if self._file:
            self._file.write(line + "\n")
            self._file.flush()

    def _flush_bot_turn(self) -> None:
        if self._bot_buffer:
            self._write("BOT:  " + " ".join(self._bot_buffer).strip())
            self._bot_buffer = []
            self._emit_latency()

    def _emit_latency(self) -> None:
        if not self._ttfb:
            return
        parts = []
        total = 0.0
        for kind in ("STT", "LLM", "TTS"):
            val = self._ttfb.get(kind)
            if val is not None:
                parts.append(f"{kind}={val * 1000:.0f}ms")
                total += val
        if parts:
            status = "OK" if total < 0.8 else "OVER"
            self._write(
                f"⏱  turn latency: {' '.join(parts)} | TTFB sum={total * 1000:.0f}ms "
                f"(target <800ms: {status})"
            )
        self._ttfb = {}

    # -- frame tap ----------------------------------------------------------
    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        src = data.source

        # Final user speech (only at the STT service, to avoid duplicates).
        if isinstance(frame, TranscriptionFrame) and isinstance(src, STTService):
            text = (getattr(frame, "text", "") or "").strip()
            if text:
                # A new user turn implicitly closes any pending bot turn.
                self._flush_bot_turn()
                self._write(f"USER: {text}")

        # Agent speech: accumulate the spoken text chunks for this turn.
        elif isinstance(frame, TTSTextFrame) and isinstance(src, TTSService):
            text = getattr(frame, "text", "") or ""
            if text:
                self._bot_buffer.append(text)

        # Agent finished speaking -> commit the turn + latency line.
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._flush_bot_turn()

        # Per-service TTFB metrics (recorded at the originating service only).
        elif isinstance(frame, MetricsFrame):
            self._record_metrics(frame, src)

    def _record_metrics(self, frame, src) -> None:
        src_name = getattr(src, "name", "") or ""
        for md in getattr(frame, "data", None) or []:
            if not isinstance(md, TTFBMetricsData):
                continue
            processor = getattr(md, "processor", "") or src_name
            value = getattr(md, "value", None)
            if value is None:
                continue
            # Only record at the originating service to avoid double counting.
            if src_name and processor and src_name != processor:
                continue
            if "STT" in processor:
                self._ttfb["STT"] = value
            elif "LLM" in processor:
                self._ttfb["LLM"] = value
            elif "TTS" in processor:
                self._ttfb["TTS"] = value
