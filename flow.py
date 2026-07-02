"""Conversation flow — now config-driven.

The flow (greeting → new/existing → verify → referral → intake → estimate →
scheduling → confirm, plus not_found/no_slots/callback/handoff) is defined by a
JSON file (see `config.FLOW_CONFIG`, default `flows/penciled_test_clinic.json`)
and constructed at runtime by `flow_engine.FlowEngine`. Behavior is identical to
the previous hand-written flow — this is a representation change only.

`bot.py` imports `create_greeting_node` from here; that contract is unchanged.
"""

import threading
from pathlib import Path

from loguru import logger

import config
from flow_engine import FlowEngine

# The active flow can be hot-swapped at runtime (see swap_active_flow) without a
# process restart. New sessions read whichever engine is active when they start;
# in-progress calls keep the engine they were built with. A lock makes the swap
# atomic so a session never reads a half-updated pair of (engine, name).
_lock = threading.Lock()

# Load and validate the initial flow config once at import (FLOW_CONFIG env).
_engine = FlowEngine(config.FLOW_CONFIG)
_active_flow_name = Path(config.FLOW_CONFIG).stem


def get_active_engine() -> FlowEngine:
    """Return the currently active FlowEngine.

    Callers should snapshot this ONCE per session so voice + greeting come from
    the same flow even if a swap lands mid-session.
    """
    return _engine


def get_active_flow_name() -> str:
    """Return the name (file stem) of the currently active flow."""
    return _active_flow_name


def active_voice_id():
    """The active flow's pinned TTS voice id, or None to use the default."""
    return _engine.voice_id


def swap_active_flow(config_path: str, name: str) -> str:
    """Build a FlowEngine from `config_path` and, only if it loads cleanly, make
    it the active flow atomically. Raises on parse/build failure — in which case
    the previous flow stays active (the bot is never left flowless).
    """
    new_engine = FlowEngine(config_path)  # may raise; caller keeps old flow
    global _engine, _active_flow_name
    with _lock:
        _engine = new_engine
        _active_flow_name = name
    logger.info(
        f"Active flow swapped to '{name}' ({config_path}); the NEXT session will use it."
    )
    return name


def create_greeting_node():
    """Return the initial NodeConfig — the entry point of the active flow."""
    return _engine.build_initial_node()


logger.debug(f"Flow loaded from config: {config.FLOW_CONFIG} (active: {_active_flow_name})")
