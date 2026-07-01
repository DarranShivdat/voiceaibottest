"""Observability: full transcript, per-turn latency, and an end-of-call summary.

`TranscriptObserver` taps the pipeline's frame stream to:
  * log every user and agent turn to the console AND a timestamped file in
    `transcripts/` (the compliance/audit record),
  * log per-turn STT / LLM / TTS time-to-first-byte (TTFB) so we can see whether
    each turn is under the ~800ms target, and
  * print a clean session summary (patient, action taken, appointment, turns,
    average latency) when the call ends.

Pipecat's built-in `MetricsLogObserver` runs alongside this (see bot.py) for
detailed per-service metric logging.
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
        self._flow = None
        # summary stats
        self._user_turns = 0
        self._bot_turns = 0
        self._turn_totals: list[float] = []  # per-turn TTFB sums (seconds)
        self._started = False
        self._closed = False

    def attach_flow(self, flow_manager) -> None:
        """Give the observer access to flow state for the end-of-call summary."""
        self._flow = flow_manager

    # -- session lifecycle --------------------------------------------------
    def start_session(self) -> None:
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._path = TRANSCRIPT_DIR / f"transcript-{ts}.txt"
        self._file = open(self._path, "a", encoding="utf-8")
        self._started = True
        self._closed = False
        self._write(f"=== Conversation transcript {datetime.now().isoformat(timespec='seconds')} ===")
        logger.info(f"📝 Saving transcript to {self._path}")

    def close(self) -> None:
        if self._closed or not self._started:
            # Never started (e.g. connect failed before greeting) — nothing to
            # summarize; keep teardown quiet.
            return
        self._closed = True
        self._flush_bot_turn()
        self._print_summary()
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
            self._bot_turns += 1
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
            self._turn_totals.append(total)
            status = "OK" if total < 0.8 else "OVER"
            self._write(
                f"⏱  turn latency: {' '.join(parts)} | TTFB sum={total * 1000:.0f}ms "
                f"(target <800ms: {status})"
            )
        self._ttfb = {}

    def _print_summary(self) -> None:
        state = getattr(self._flow, "state", {}) if self._flow else {}
        patient_name = state.get("patient_name") or "(unknown)"
        is_new = state.get("is_new")
        who = "new" if is_new else "existing" if is_new is not None else "?"
        outcome = state.get("outcome") or ("registered" if state.get("registered") else "no action")
        appt = state.get("appointment")
        estimate = state.get("estimate")
        avg_ms = (sum(self._turn_totals) / len(self._turn_totals) * 1000) if self._turn_totals else 0.0

        lines = [
            "",
            "================= CALL SUMMARY =================",
            f"Patient:      {patient_name} ({who})",
            f"Action:       {outcome}",
        ]
        if appt:
            lines.append(f"Appointment:  {appt.get('when')} with {appt.get('provider')}")
        if estimate:
            lines.append(f"Est. cost:    ~${estimate.get('estimate_usd')} out of pocket (mock estimate)")
        if state.get("callback_number"):
            lines.append(f"Callback:     {state['callback_number']}")
        lines += [
            f"Turns:        {self._user_turns + self._bot_turns} "
            f"({self._user_turns} caller / {self._bot_turns} agent)",
            f"Avg latency:  {avg_ms:.0f}ms TTFB/turn (target <800ms)",
        ]
        if self._path:
            lines.append(f"Transcript:   {self._path}")
        lines.append("================================================")
        for ln in lines:
            self._write(ln)

    # -- frame tap ----------------------------------------------------------
    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        src = data.source

        # Final user speech (only at the STT service, to avoid duplicates).
        if isinstance(frame, TranscriptionFrame) and isinstance(src, STTService):
            text = (getattr(frame, "text", "") or "").strip()
            if text:
                self._flush_bot_turn()  # a new user turn closes any pending bot turn
                self._write(f"USER: {text}")
                self._user_turns += 1

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
