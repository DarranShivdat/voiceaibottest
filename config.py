"""Central, env-driven configuration.

Every provider choice (STT / LLM / TTS) and model is read from environment
variables so that swapping a provider is a one-line `.env` change rather than a
code edit. See `.env.example` for the full list.
"""

import os

from dotenv import load_dotenv

# `override=True` so values in .env win over anything already exported.
load_dotenv(override=True)


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# --- Branding (clinic + assistant names) ------------------------------------
CLINIC_NAME = os.getenv("CLINIC_NAME", "Penciled Test Clinic")
ASSISTANT_NAME = os.getenv("ASSISTANT_NAME", "Riley")

# --- Flow config -------------------------------------------------------------
# The conversation flow is data-driven: this JSON file defines every node,
# question, transition, and tool call. Point at a different file to load a
# different clinic's flow. Path is relative to the project root.
FLOW_CONFIG = os.getenv("FLOW_CONFIG", "flows/penciled_test_clinic.json")


# --- Provider selection (swap these in .env) --------------------------------
STT_PROVIDER = os.getenv("STT_PROVIDER", "deepgram").lower()
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").lower()
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "cartesia").lower()

# Automatic TTS failover: if the primary TTS errors/times out, the pipeline
# switches to this provider. Defaults to Deepgram because its API key is
# already required for STT, so the fallback works out-of-the-box with no extra
# credentials. Set empty to disable failover.
TTS_FALLBACK_PROVIDER = os.getenv("TTS_FALLBACK_PROVIDER", "deepgram").lower()

# --- Per-provider models ----------------------------------------------------
DEEPGRAM_MODEL = os.getenv("DEEPGRAM_MODEL", "nova-3-general")

# Default is Haiku: for a real-time voice demo, low latency (the ~800ms per-turn
# target) matters more than peak reasoning. Swap via ANTHROPIC_MODEL in .env
# (e.g. claude-sonnet-4-6 for more headroom). Per-turn latency is logged so the
# trade-off is always visible.
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1")
GOOGLE_MODEL = os.getenv("GOOGLE_MODEL", "gemini-2.5-flash")

CARTESIA_MODEL = os.getenv("CARTESIA_MODEL", "sonic-3.5")
# "British Reading Lady" — a known-valid Cartesia voice id.
CARTESIA_VOICE = os.getenv("CARTESIA_VOICE", "71a7ad14-091c-4e8e-a314-022ece01c121")

# --- Stretch: telephony (Twilio / Telnyx websocket transport) ---------------
# Off by default so it never affects the browser (SmallWebRTC) demo. When on,
# the same bot can also answer an inbound phone websocket via the Pipecat runner.
ENABLE_TELEPHONY = _flag("ENABLE_TELEPHONY")
