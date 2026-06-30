"""Pipecat Flows conversation graph for the clinic scheduling agent.

Adapted from the official Pipecat Flows "patient intake" example into a
scheduling flow. Nodes (each a function returning a NodeConfig):

    1. greeting        - introduce + ask name & DOB         -> look_up_patient
    2. (verify)        - handled inside look_up_patient handler
    3. offer_slots     - read back 2-3 open times           -> book_appointment
    4. (book)          - handled inside book_appointment handler
    5. confirm         - repeat booked time, then end

Failure paths: patient not found, no slots available, caller declines.

In Flows v1.x a function handler returns `(result, next_node)`:
  (result, NodeConfig) -> return result to the LLM AND transition
  (result, None)       -> return result, stay on the current node
"""

from loguru import logger

import tools

# The flows package installs as `pipecat_flows`. Newer Pipecat builds vendor it
# as `pipecat.flows`; support both.
try:
    from pipecat_flows import (
        ContextStrategy,
        ContextStrategyConfig,
        FlowManager,
        FlowsFunctionSchema,
        NodeConfig,
    )
except ImportError:  # pragma: no cover - depends on installed version
    from pipecat.flows import (  # type: ignore
        ContextStrategy,
        ContextStrategyConfig,
        FlowManager,
        FlowsFunctionSchema,
        NodeConfig,
    )


PERSONA = (
    "You are Riley, a warm and efficient scheduling assistant for Lakeside Family "
    "Clinic. You help callers verify their identity and book an appointment. "
    "Speak naturally and keep every reply to one or two short sentences, as if on "
    "the phone. Never invent patient records, appointment times, or confirmation "
    "numbers — only use what the tools return to you. Spell nothing out unless "
    "asked; just talk like a helpful person."
)


def _dev(content: str) -> dict:
    """Build a task/developer message (the role Pipecat's universal context uses)."""
    return {"role": "developer", "content": content}


# ---------------------------------------------------------------------------
# Function handlers
# ---------------------------------------------------------------------------

async def handle_look_up_patient(args: dict, flow_manager: FlowManager):
    name = (args.get("name") or "").strip()
    dob = (args.get("dob") or "").strip()
    patient = tools.look_up_patient(name, dob)

    if patient is None:
        attempts = flow_manager.state.get("verify_attempts", 0) + 1
        flow_manager.state["verify_attempts"] = attempts
        if attempts >= 2:
            return {"verified": False, "attempts": attempts}, create_not_found_node()
        # Stay on the greeting node so the LLM can re-ask for name/DOB.
        return {"verified": False, "attempts": attempts}, None

    flow_manager.state["patient"] = patient
    flow_manager.state.pop("verify_attempts", None)
    slots = tools.get_available_slots()
    if not slots:
        return (
            {"verified": True, "name": patient["name"], "slots_available": False},
            create_no_slots_node(patient),
        )
    return (
        {"verified": True, "name": patient["name"]},
        create_offer_slots_node(patient, slots),
    )


async def handle_get_available_slots(args: dict, flow_manager: FlowManager):
    slots = tools.get_available_slots()
    payload = [
        {"slot_id": s["slot_id"], "when": tools.format_when(s["datetime"]), "provider": s["provider"]}
        for s in slots[:5]
    ]
    # Stay on the current node; this just feeds fresh options to the LLM.
    return {"slots": payload}, None


async def handle_book_appointment(args: dict, flow_manager: FlowManager):
    slot_id = (args.get("slot_id") or "").strip()
    patient = flow_manager.state.get("patient")
    if not patient:
        # Defensive: identity somehow lost — restart.
        return {"success": False, "reason": "not_verified"}, create_greeting_node()

    result = tools.book_appointment(patient["patient_id"], slot_id)
    if not result.get("success"):
        slots = tools.get_available_slots()
        if not slots:
            return result, create_no_slots_node(patient)
        # Re-offer remaining slots if the chosen one was taken/invalid.
        return result, create_offer_slots_node(patient, slots)

    flow_manager.state["booking"] = result
    sms_text = (
        f"Lakeside Family Clinic: your appointment is confirmed for {result['when']} "
        f"with {result['provider']}. Confirmation {result['confirmation_number']}."
    )
    tools.send_sms_confirmation(patient["phone"], sms_text)
    return result, create_confirm_node(patient, result)


async def handle_decline(args: dict, flow_manager: FlowManager):
    return {"acknowledged": True}, create_declined_node()


# ---------------------------------------------------------------------------
# Function schemas (the LLM-callable tools)
# ---------------------------------------------------------------------------

look_up_patient_fn = FlowsFunctionSchema(
    name="look_up_patient",
    description=(
        "Verify the caller's identity by matching their full name and date of "
        "birth against patient records. Call only after the caller has given both."
    ),
    properties={
        "name": {"type": "string", "description": "Caller's full name, e.g. 'Maria Garcia'."},
        "dob": {"type": "string", "description": "Date of birth in YYYY-MM-DD format."},
    },
    required=["name", "dob"],
    handler=handle_look_up_patient,
)

get_available_slots_fn = FlowsFunctionSchema(
    name="get_available_slots",
    description="List the currently available appointment slots if the caller wants to hear more options.",
    properties={},
    required=[],
    handler=handle_get_available_slots,
)

book_appointment_fn = FlowsFunctionSchema(
    name="book_appointment",
    description="Book the appointment slot the caller selected, by its slot_id.",
    properties={
        "slot_id": {"type": "string", "description": "The slot_id of the chosen appointment, e.g. 'slot-3'."},
    },
    required=["slot_id"],
    handler=handle_book_appointment,
)

decline_fn = FlowsFunctionSchema(
    name="decline_booking",
    description="Call this when the caller decides they do not want to book any appointment.",
    properties={},
    required=[],
    handler=handle_decline,
)


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def create_greeting_node() -> NodeConfig:
    """Node 1+2: greet, introduce, and verify identity."""
    return NodeConfig(
        name="greeting",
        role_message=PERSONA,
        task_messages=[
            _dev(
                "Greet the caller, introduce yourself as the scheduling assistant for "
                "Lakeside Family Clinic, and say you can help book an appointment. To "
                "verify their identity, ask for their full name and date of birth. "
                "Once you have BOTH, call look_up_patient. If verification fails, "
                "apologize and ask them to repeat their name and date of birth."
            )
        ],
        functions=[look_up_patient_fn],
        context_strategy=ContextStrategyConfig(strategy=ContextStrategy.RESET),
        respond_immediately=True,
    )


def create_offer_slots_node(patient: dict, slots: list) -> NodeConfig:
    """Node 3: offer 2-3 open times and let the caller choose."""
    top = slots[:3]
    options_text = "\n".join(
        f"- slot_id {s['slot_id']}: {tools.format_when(s['datetime'])} with {s['provider']}"
        for s in top
    )
    return NodeConfig(
        name="offer_slots",
        role_message=PERSONA,
        task_messages=[
            _dev(
                f"The caller {patient['name']} is verified. Offer these available "
                f"appointment times and ask which one they'd like:\n{options_text}\n\n"
                "Read at most three options aloud in a natural, conversational way "
                "(say the day and time, not the slot_id). When the caller picks one, "
                "call book_appointment with that option's slot_id. If they want other "
                "times, call get_available_slots. If they don't want to book at all, "
                "call decline_booking."
            )
        ],
        functions=[book_appointment_fn, get_available_slots_fn, decline_fn],
        respond_immediately=True,
    )


def create_confirm_node(patient: dict, booking: dict) -> NodeConfig:
    """Node 5: repeat the booked time back, then end the call."""
    return NodeConfig(
        name="confirm",
        role_message=PERSONA,
        task_messages=[
            _dev(
                f"The appointment is booked: {booking['when']} with {booking['provider']} "
                f"(confirmation {booking['confirmation_number']}). Confirm the day and "
                "time back to the caller, mention a text confirmation has been sent, "
                "thank them, and say goodbye. Do not ask any further questions."
            )
        ],
        functions=[],
        post_actions=[{"type": "end_conversation"}],
        respond_immediately=True,
    )


def create_no_slots_node(patient: dict) -> NodeConfig:
    return NodeConfig(
        name="no_slots",
        role_message=PERSONA,
        task_messages=[
            _dev(
                "There are no open appointment slots right now. Apologize, suggest the "
                "caller try again later or call the front desk, then say goodbye."
            )
        ],
        functions=[],
        post_actions=[{"type": "end_conversation"}],
        respond_immediately=True,
    )


def create_not_found_node() -> NodeConfig:
    return NodeConfig(
        name="not_found",
        role_message=PERSONA,
        task_messages=[
            _dev(
                "Identity could not be verified after a couple of tries. Apologize, "
                "explain you can't access their record right now, suggest they call the "
                "front desk to confirm their details, then say goodbye."
            )
        ],
        functions=[],
        post_actions=[{"type": "end_conversation"}],
        respond_immediately=True,
    )


def create_declined_node() -> NodeConfig:
    return NodeConfig(
        name="declined",
        role_message=PERSONA,
        task_messages=[
            _dev(
                "The caller doesn't want to book. Let them know that's no problem, invite "
                "them to call back anytime, and say goodbye."
            )
        ],
        functions=[],
        post_actions=[{"type": "end_conversation"}],
        respond_immediately=True,
    )


logger.debug("Flow nodes loaded: greeting -> offer_slots -> confirm (+ no_slots/not_found/declined)")
