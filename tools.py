"""Mock clinic data store + the callable tools the agent uses.

This is a DEMO. There is no real EMR — all patient and appointment data lives in
local JSON files under `data/`. The four functions below are the tools exposed
to the LLM (wired into the conversation flow in `flow.py`):

    look_up_patient(name, dob)        -> patient dict or None
    get_available_slots()             -> list of open slots
    book_appointment(patient_id, id)  -> confirmation dict
    send_sms_confirmation(phone, txt) -> sends via Twilio if configured, else logs
"""

import json
import os
import threading
from datetime import datetime
from pathlib import Path

from loguru import logger

DATA_DIR = Path(__file__).parent / "data"
PATIENTS_FILE = DATA_DIR / "patients.json"
SLOTS_FILE = DATA_DIR / "slots.json"

# Guards read-modify-write on slots.json so two concurrent bookings can't race.
_slots_lock = threading.Lock()


def _load(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(path: Path, data) -> None:
    # Atomic write: dump to a temp file then replace, so a crash mid-write can't
    # corrupt the store.
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def format_when(iso: str) -> str:
    """Render an ISO datetime as a spoken-friendly string."""
    dt = datetime.fromisoformat(iso)
    # %-d / %-I are non-zero-padded (Linux/macOS). Demo target is macOS.
    return dt.strftime("%A, %B %-d at %-I:%M %p")


# --- Tools ------------------------------------------------------------------

def look_up_patient(name: str, dob: str):
    """Return the patient matching `name` + `dob` (YYYY-MM-DD), else None."""
    patients = _load(PATIENTS_FILE)
    norm_name = _normalize(name)
    dob = dob.strip()

    # Exact full-name + DOB match first.
    for p in patients:
        if _normalize(p["name"]) == norm_name and p["dob"] == dob:
            return p

    # Be forgiving for voice: match on DOB + last name appearing in what they said.
    spoken_tokens = set(norm_name.split())
    for p in patients:
        last_name = _normalize(p["name"]).split()[-1]
        if p["dob"] == dob and last_name in spoken_tokens:
            return p

    logger.info(f"No patient match for name={name!r} dob={dob!r}")
    return None


def get_available_slots():
    """Return all appointment slots whose status is still 'open'."""
    slots = _load(SLOTS_FILE)
    return [s for s in slots if s.get("status") == "open"]


def book_appointment(patient_id: str, slot_id: str) -> dict:
    """Mark a slot as booked for the given patient and persist it to slots.json."""
    with _slots_lock:
        slots = _load(SLOTS_FILE)
        slot = next((s for s in slots if s["slot_id"] == slot_id), None)

        if slot is None:
            return {"success": False, "reason": "slot_not_found", "slot_id": slot_id}
        if slot.get("status") != "open":
            return {"success": False, "reason": "slot_taken", "slot_id": slot_id}

        slot["status"] = "booked"
        slot["patient_id"] = patient_id
        slot["booked_at"] = datetime.now().isoformat(timespec="seconds")
        _save(SLOTS_FILE, slots)

    when = format_when(slot["datetime"])
    confirmation = f"LFC-{slot_id.split('-')[-1]}-{patient_id.split('-')[-1]}"
    logger.info(f"Booked {slot_id} ({when}) for {patient_id} [conf {confirmation}]")
    return {
        "success": True,
        "confirmation_number": confirmation,
        "slot_id": slot_id,
        "when": when,
        "provider": slot["provider"],
        "patient_id": patient_id,
    }


def send_sms_confirmation(phone: str, text: str) -> dict:
    """Send an SMS via Twilio if TWILIO_* keys are set; otherwise just log it.

    SMS is optional/secondary — the demo never blocks on it.
    """
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_FROM_NUMBER")

    if sid and token and from_number:
        try:
            from twilio.rest import Client

            Client(sid, token).messages.create(to=phone, from_=from_number, body=text)
            logger.info(f"Sent SMS confirmation to {phone}")
            return {"sent": True, "channel": "twilio", "to": phone}
        except Exception as e:  # noqa: BLE001 - never let SMS break the call
            logger.warning(f"Twilio SMS failed ({e}); falling back to log.")

    logger.info(f"[SMS-MOCK] to {phone}: {text}")
    return {"sent": False, "channel": "log", "to": phone, "text": text}
