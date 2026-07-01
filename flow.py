"""Pipecat Flows conversation graph — new-patient intake for a clinic.

A front-desk style inbound-call agent that schedules an Initial Exam (IE) for a
NEW or EXISTING patient. Verified against Pipecat 1.4.0 / pipecat_flows.

Node map (each function returns a NodeConfig):

    greeting ─┬─ (existing) verify_existing_patient ─┬─ scheduling ─ confirm
              │                                       └─ existing_not_found ─(register)─┐
              └─ (new) begin_new_intake ─ referral ─ intake ─ estimate ─ scheduling ─ confirm
                                                       │
    no_slots ─ callback     handoff  (reachable from ANY node)   <───────────────────┘

Edge paths handled: existing not found (offer to register), no slots (take a
callback number), caller declines (offer human handoff), and a human-handoff
node reachable from anywhere.

Flows v1.x: a function handler returns `(result, next_node)`:
    (result, NodeConfig) -> return result to the LLM AND transition
    (result, None)       -> return result, stay on the current node
"""

from loguru import logger

import config
import tools

try:
    from pipecat_flows import (
        ContextStrategy,
        ContextStrategyConfig,
        FlowManager,
        FlowsFunctionSchema,
        NodeConfig,
    )
except ImportError:  # pragma: no cover - vendored location in some builds
    from pipecat.flows import (  # type: ignore
        ContextStrategy,
        ContextStrategyConfig,
        FlowManager,
        FlowsFunctionSchema,
        NodeConfig,
    )


PERSONA = (
    f"You are {config.ASSISTANT_NAME}, a warm, efficient front-desk scheduling "
    f"assistant for {config.CLINIC_NAME}. You are on a phone call. Speak naturally "
    "and keep every reply "
    "to one or two short sentences. Use brief backchannels like 'Got it' or "
    "'Thanks'. Collect information one or two items at a time — never read a long "
    "list or interrogate. Confirm important details (date of birth, appointment "
    "time) by reading them back. Never re-ask something the caller already told "
    "you. Never invent patient records, prices, appointment times, or confirmation "
    "numbers — only use what your tools return. If the caller asks for something "
    "outside scheduling an initial exam, hand off to a human."
)


def _dev(content: str) -> dict:
    """A task/developer instruction message (the role Pipecat's context uses)."""
    return {"role": "developer", "content": content}


# ===========================================================================
# Function handlers
# ===========================================================================

async def handle_verify_existing(args: dict, flow_manager: FlowManager):
    name = (args.get("name") or "").strip()
    dob = (args.get("dob") or "").strip()
    patient = tools.look_up_patient(name, dob)

    if patient is None:
        attempts = flow_manager.state.get("verify_attempts", 0) + 1
        flow_manager.state["verify_attempts"] = attempts
        if attempts >= 2:
            return {"verified": False}, create_existing_not_found_node()
        return {"verified": False, "attempts": attempts}, None  # stay, re-ask

    flow_manager.state["patient"] = patient
    flow_manager.state["patient_name"] = patient["name"]
    flow_manager.state["is_new"] = False
    flow_manager.state.pop("verify_attempts", None)

    slots = tools.get_available_slots()
    if not slots:
        return {"verified": True, "name": patient["name"]}, create_no_slots_node()
    return (
        {"verified": True, "name": patient["name"]},
        create_scheduling_node(patient, slots),
    )


async def handle_begin_new_intake(args: dict, flow_manager: FlowManager):
    flow_manager.state["is_new"] = True
    return {"ok": True}, create_referral_node()


async def handle_set_referral(args: dict, flow_manager: FlowManager):
    has_referral = bool(args.get("has_referral"))
    referring = (args.get("referring_provider") or "").strip() or None
    flow_manager.state["referral"] = {
        "has_referral": has_referral,
        "referring_provider": referring,
    }
    return (
        {"has_referral": has_referral, "referring_provider": referring},
        create_intake_node(),
    )


async def handle_submit_intake(args: dict, flow_manager: FlowManager):
    fields = ["full_name", "dob", "phone", "email", "chief_complaint",
              "insurance_carrier", "member_id"]
    intake = {k: (args.get(k) or "").strip() for k in fields}
    flow_manager.state["intake"] = intake

    referral = flow_manager.state.get("referral", {}) or {}
    patient = tools.create_patient(
        full_name=intake["full_name"],
        dob=intake["dob"],
        phone=intake["phone"],
        email=intake["email"],
        insurance_carrier=intake["insurance_carrier"],
        member_id=intake["member_id"],
        chief_complaint=intake["chief_complaint"],
    )
    flow_manager.state["patient"] = patient
    flow_manager.state["patient_name"] = patient["name"]
    flow_manager.state["registered"] = True

    estimate = tools.get_cost_estimate(
        intake["insurance_carrier"], referral.get("has_referral", False)
    )
    flow_manager.state["estimate"] = estimate

    return (
        {"registered": True, "estimate_usd": estimate["estimate_usd"]},
        create_estimate_node(patient, estimate, referral),
    )


async def handle_ready_to_schedule(args: dict, flow_manager: FlowManager):
    patient = flow_manager.state.get("patient")
    slots = tools.get_available_slots()
    if not slots:
        return {"ready": True}, create_no_slots_node()
    return {"ready": True}, create_scheduling_node(patient, slots)


async def handle_get_available_slots(args: dict, flow_manager: FlowManager):
    slots = tools.get_available_slots()
    payload = [
        {"slot_id": s["slot_id"], "when": tools.format_when(s["datetime"]), "provider": s["provider"]}
        for s in slots[:5]
    ]
    return {"slots": payload}, None  # stay, just feed fresh options to the LLM


async def handle_book_appointment(args: dict, flow_manager: FlowManager):
    slot_id = (args.get("slot_id") or "").strip()
    patient = flow_manager.state.get("patient")
    if not patient:
        return {"success": False, "reason": "not_verified"}, create_greeting_node()

    result = tools.book_appointment(patient["patient_id"], slot_id)
    if not result.get("success"):
        slots = tools.get_available_slots()
        if not slots:
            return result, create_no_slots_node()
        return result, create_scheduling_node(patient, slots)  # re-offer

    flow_manager.state["booking"] = result
    flow_manager.state["appointment"] = {"when": result["when"], "provider": result["provider"]}
    flow_manager.state["outcome"] = "booked"
    tools.send_confirmation(patient, result)
    return result, create_confirm_node(patient, result)


async def handle_take_callback(args: dict, flow_manager: FlowManager):
    phone = (args.get("phone") or "").strip()
    flow_manager.state["callback_number"] = phone
    flow_manager.state["outcome"] = "callback"
    logger.info(f"[CALLBACK] scheduling team will call back {phone}")
    return {"callback": phone}, create_callback_node(phone)


async def handle_caller_declines(args: dict, flow_manager: FlowManager):
    flow_manager.state["outcome"] = "handoff"
    return {"declined": True}, create_handoff_node()


async def handle_human_handoff(args: dict, flow_manager: FlowManager):
    flow_manager.state["outcome"] = "handoff"
    return {"handed_off": True}, create_handoff_node()


# ===========================================================================
# Function schemas (the LLM-callable tools)
# ===========================================================================

verify_existing_patient_fn = FlowsFunctionSchema(
    name="verify_existing_patient",
    description=(
        "Verify an EXISTING patient by matching their full name and date of birth. "
        "Call only after the caller has confirmed they've been seen here before and "
        "given both name and date of birth."
    ),
    properties={
        "name": {"type": "string", "description": "Caller's full name."},
        "dob": {"type": "string", "description": "Date of birth in YYYY-MM-DD format."},
    },
    required=["name", "dob"],
    handler=handle_verify_existing,
)

begin_new_intake_fn = FlowsFunctionSchema(
    name="begin_new_intake",
    description="Start new-patient intake (caller is new, or an existing lookup failed and they agreed to register).",
    properties={},
    required=[],
    handler=handle_begin_new_intake,
)

set_referral_fn = FlowsFunctionSchema(
    name="set_referral",
    description="Record whether the new patient was referred by another provider.",
    properties={
        "has_referral": {"type": "boolean", "description": "True if referred by another provider."},
        "referring_provider": {"type": "string", "description": "Name of the referring provider, if any."},
    },
    required=["has_referral"],
    handler=handle_set_referral,
)

submit_intake_fn = FlowsFunctionSchema(
    name="submit_intake",
    description=(
        "Submit the collected new-patient details. Call ONLY once you have gathered "
        "all of: full name, date of birth, phone, email, reason for visit, insurance "
        "carrier, and insurance member ID."
    ),
    properties={
        "full_name": {"type": "string"},
        "dob": {"type": "string", "description": "Date of birth in YYYY-MM-DD format."},
        "phone": {"type": "string"},
        "email": {"type": "string"},
        "chief_complaint": {"type": "string", "description": "Reason for the visit."},
        "insurance_carrier": {"type": "string"},
        "member_id": {"type": "string", "description": "Insurance member ID."},
    },
    required=["full_name", "dob", "phone", "email", "chief_complaint", "insurance_carrier", "member_id"],
    handler=handle_submit_intake,
)

ready_to_schedule_fn = FlowsFunctionSchema(
    name="ready_to_schedule",
    description="The caller is ready to pick an appointment time after hearing the cost estimate.",
    properties={},
    required=[],
    handler=handle_ready_to_schedule,
)

get_available_slots_fn = FlowsFunctionSchema(
    name="get_available_slots",
    description="List currently available initial-exam appointment times if the caller wants more options.",
    properties={},
    required=[],
    handler=handle_get_available_slots,
)

book_appointment_fn = FlowsFunctionSchema(
    name="book_appointment",
    description="Book the initial-exam slot the caller chose, by its slot_id.",
    properties={
        "slot_id": {"type": "string", "description": "The slot_id of the chosen time, e.g. 'slot-3'."},
    },
    required=["slot_id"],
    handler=handle_book_appointment,
)

take_callback_fn = FlowsFunctionSchema(
    name="take_callback_number",
    description="Record a callback number when no appointment slots are available.",
    properties={
        "phone": {"type": "string", "description": "Best callback phone number."},
    },
    required=["phone"],
    handler=handle_take_callback,
)

caller_declines_fn = FlowsFunctionSchema(
    name="caller_declines",
    description="The caller does not want to provide required information or continue with intake.",
    properties={},
    required=[],
    handler=handle_caller_declines,
)

human_handoff_fn = FlowsFunctionSchema(
    name="human_handoff",
    description=(
        "Hand off to a human teammate. Use for any request outside scheduling an "
        "initial exam, or whenever the caller asks for a person."
    ),
    properties={},
    required=[],
    handler=handle_human_handoff,
)

# human_handoff is reachable from every node.
COMMON = [human_handoff_fn]


# ===========================================================================
# Nodes
# ===========================================================================

def create_greeting_node() -> NodeConfig:
    """Node 1+2: greet, then ask new vs. existing."""
    return NodeConfig(
        name="greeting",
        role_message=PERSONA,
        task_messages=[
            _dev(
                "Warmly greet the caller and introduce yourself as the scheduling "
                f"assistant at {config.CLINIC_NAME}. In the same breath, ask if "
                "they've been seen here before. "
                "If they HAVE been seen before, collect their full name and date of "
                "birth, then call verify_existing_patient. "
                "If they are NEW, call begin_new_intake."
            )
        ],
        functions=[verify_existing_patient_fn, begin_new_intake_fn, *COMMON],
        context_strategy=ContextStrategyConfig(strategy=ContextStrategy.RESET),
        respond_immediately=True,
    )


def create_existing_not_found_node() -> NodeConfig:
    return NodeConfig(
        name="existing_not_found",
        role_message=PERSONA,
        task_messages=[
            _dev(
                "You couldn't find the caller in the records after checking. Reassure "
                "them, and offer to get them set up as a new patient. If they agree, "
                "call begin_new_intake. If they'd rather talk to someone, hand off."
            )
        ],
        functions=[begin_new_intake_fn, *COMMON],
        respond_immediately=True,
    )


def create_referral_node() -> NodeConfig:
    """Node 3: referral vs. no referral."""
    return NodeConfig(
        name="referral",
        role_message=PERSONA,
        task_messages=[
            _dev(
                "Ask whether they were referred by another provider. If yes, get the "
                "referring provider's name. Then call set_referral with has_referral "
                "and the provider name (if any). Keep it to one short question."
            )
        ],
        functions=[set_referral_fn, *COMMON],
        respond_immediately=True,
    )


def create_intake_node() -> NodeConfig:
    """Node 4: collect new-patient info conversationally."""
    return NodeConfig(
        name="intake",
        role_message=PERSONA,
        task_messages=[
            _dev(
                "Collect the new patient's details, ONE or TWO items at a time, with "
                "brief acknowledgements — never list everything at once. You need: "
                "full name, date of birth, phone number, email, the reason for their "
                "visit, and their insurance carrier plus member ID. Read the date of "
                "birth back to confirm it. Do not re-ask anything already provided "
                "earlier in the call. When you have ALL of these, call submit_intake. "
                "If the caller refuses to provide needed info, call caller_declines."
            )
        ],
        functions=[submit_intake_fn, caller_declines_fn, *COMMON],
        respond_immediately=True,
    )


def create_estimate_node(patient: dict, estimate: dict, referral: dict) -> NodeConfig:
    """Node 5: mock out-of-pocket estimate."""
    amount = estimate["estimate_usd"]
    carrier = estimate.get("carrier") or "your plan"
    ref_clause = (
        "since you have a referral on file"
        if referral.get("has_referral")
        else "since there's no referral on file"
    )
    return NodeConfig(
        name="estimate",
        role_message=PERSONA,
        task_messages=[
            _dev(
                f"Let the caller know that with {carrier}, {ref_clause}, the initial "
                f"exam is typically around ${amount} out of pocket. Make it clear this "
                "is just an estimate, not a final or verified amount. Then ask if "
                "they'd like to go ahead and schedule. When they say yes, call "
                "ready_to_schedule."
            )
        ],
        functions=[ready_to_schedule_fn, *COMMON],
        respond_immediately=True,
    )


def create_scheduling_node(patient: dict, slots: list) -> NodeConfig:
    """Node 6: offer 2-3 open IE slots and book the chosen one."""
    top = slots[:3]
    options = "\n".join(
        f"- slot_id {s['slot_id']}: {tools.format_when(s['datetime'])} with {s['provider']}"
        for s in top
    )
    name = patient.get("name", "the caller") if patient else "the caller"
    return NodeConfig(
        name="scheduling",
        role_message=PERSONA,
        task_messages=[
            _dev(
                f"Offer {name} these open initial-exam times and ask which works "
                f"best:\n{options}\n\n"
                "Read at most three, saying the day and time naturally (never the "
                "slot_id). When they choose, call book_appointment with that option's "
                "slot_id. If they want other times, call get_available_slots."
            )
        ],
        functions=[book_appointment_fn, get_available_slots_fn, *COMMON],
        respond_immediately=True,
    )


def create_confirm_node(patient: dict, booking: dict) -> NodeConfig:
    """Node 7: recap + close."""
    name = patient.get("name", "")
    return NodeConfig(
        name="confirm",
        role_message=PERSONA,
        task_messages=[
            _dev(
                f"The initial exam is booked for {name}: {booking['when']} with "
                f"{booking['provider']}. Recap their name and that appointment time "
                "back to them, and let them know a confirmation text and email will "
                "follow. Thank them warmly and say goodbye. Ask no further questions."
            )
        ],
        functions=[],
        post_actions=[{"type": "end_conversation"}],
        respond_immediately=True,
    )


def create_no_slots_node() -> NodeConfig:
    return NodeConfig(
        name="no_slots",
        role_message=PERSONA,
        task_messages=[
            _dev(
                "There are no open initial-exam slots right now. Apologize briefly and "
                "offer to take a callback number so the scheduling team can reach out. "
                "If they share a number, call take_callback_number."
            )
        ],
        functions=[take_callback_fn, *COMMON],
        respond_immediately=True,
    )


def create_callback_node(phone: str) -> NodeConfig:
    return NodeConfig(
        name="callback",
        role_message=PERSONA,
        task_messages=[
            _dev(
                f"Confirm you've noted their callback number ({phone}) and that the "
                "scheduling team will reach out soon. Thank them and say goodbye."
            )
        ],
        functions=[],
        post_actions=[{"type": "end_conversation"}],
        respond_immediately=True,
    )


def create_handoff_node() -> NodeConfig:
    return NodeConfig(
        name="handoff",
        role_message=PERSONA,
        task_messages=[
            _dev(
                "Let the caller know you'll get someone from the team who can help with "
                "that, in a warm and reassuring way. Keep it to one sentence, then say "
                "goodbye."
            )
        ],
        functions=[],
        post_actions=[{"type": "end_conversation"}],
        respond_immediately=True,
    )


logger.debug(
    "Intake flow loaded: greeting -> (existing verify | new: referral->intake->estimate) "
    "-> scheduling -> confirm (+ not_found/no_slots/callback/handoff)"
)
