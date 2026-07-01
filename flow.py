"""Conversation flow — now config-driven.

The flow (greeting → new/existing → verify → referral → intake → estimate →
scheduling → confirm, plus not_found/no_slots/callback/handoff) is defined by a
JSON file (see `config.FLOW_CONFIG`, default `flows/penciled_test_clinic.json`)
and constructed at runtime by `flow_engine.FlowEngine`. Behavior is identical to
the previous hand-written flow — this is a representation change only.

`bot.py` imports `create_greeting_node` from here; that contract is unchanged.
"""

from loguru import logger

import config
from flow_engine import FlowEngine

# Load and validate the flow config once at import.
_engine = FlowEngine(config.FLOW_CONFIG)


def create_greeting_node():
    """Return the initial NodeConfig — the entry point of the flow."""
    return _engine.build_initial_node()


logger.debug(f"Flow loaded from config: {config.FLOW_CONFIG}")
