"""Mock clinic data store + the callable tools the intake agent uses.

This is a DEMO on fake data only. There is no real EMR, no real insurance
verification, and no real SMS/email — all data lives in local JSON files under
`data/` and all "sends" are logged.

Tools exposed to the flow (wired in `flow.py`):

    look_up_patient(name, dob)                 -> patient dict or None
    get_available_slots()                      -> list of open IE slots
    book_appointment(patient_id, slot_id)      -> confirmation dict
    get_cost_estimate(carrier, has_referral)   -> mock out-of-pocket ballpark
    create_patient(...)                        -> new patient dict (added to store)
    send_confirmation(patient, appointment)    -> log-only confirmation
"""

import json
import threading
from datetime import datetime
from pathlib import Path

from loguru import logger

DATA_DIR = Path(__file__).parent / "data"
PATIENTS_FILE = DATA_DIR / "patients.json"
SLOTS_FILE = DATA_DIR / "slots.json"
ESTIMATES_FILE = DATA_DIR / "insurance_estimates.json"

# Guard read-modify-write on the JSON stores against concurrent access.
_slots_lock = threading.Lock()
_patients_lock = threading.Lock()


def _load(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(path: Path, data) -> None:
    # Atomic write: dump to temp then replace, so a crash can't corrupt the store.
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def _normalize(text: str) -> str:
    return " ".join((text or "").lower().split())


def format_when(iso: str) -> str:
    """Render an ISO datetime as a spoken-friendly string."""
    dt = datetime.fromisoformat(iso)
    # %-d / %-I are non-zero-padded (Linux/macOS). Demo target is macOS.
    return dt.strftime("%A, %B %-d at %-I:%M %p")


# --- Tools ------------------------------------------------------------------

def look_up_patient(name: str, dob: str):
    """Return the existing patient matching `name` + `dob` (YYYY-MM-DD), else None."""
    patients = _load(PATIENTS_FILE)
    norm_name = _normalize(name)
    dob = (dob or "").strip()

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
    """Return all open Initial Exam (IE) slots."""
    slots = _load(SLOTS_FILE)
    return [
        s for s in slots
        if s.get("status") == "open" and s.get("visit_type", "IE") == "IE"
    ]


def book_appointment(patient_id: str, slot_id: str) -> dict:
    """Mark an IE slot booked for the patient and persist it to slots.json."""
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
        "visit_type": slot.get("visit_type", "IE"),
        "patient_id": patient_id,
    }


def get_cost_estimate(carrier: str, has_referral: bool) -> dict:
    """Return a MOCK out-of-pocket ballpark for the initial exam.

    Looks up `carrier` (+ referral status) in the mock estimates table. This is
    a rough estimate only — never a verified benefits amount.
    """
    data = _load(ESTIMATES_FILE)
    carriers = data.get("carriers", {})
    norm = _normalize(carrier)

    match = None
    if norm:
        for key, vals in carriers.items():
            if key in norm or norm in key:
                match = vals
                break
    if match is None:
        match = data.get("default", {"with_referral": 60, "without_referral": 175})

    amount = match["with_referral"] if has_referral else match["without_referral"]
    logger.info(
        f"Cost estimate: carrier={carrier!r} referral={has_referral} -> ~${amount}"
    )
    return {
        "carrier": carrier,
        "has_referral": bool(has_referral),
        "estimate_usd": amount,
        "is_estimate": True,
    }


def create_patient(
    full_name: str,
    dob: str,
    phone: str | None = None,
    email: str | None = None,
    insurance_carrier: str | None = None,
    member_id: str | None = None,
    chief_complaint: str | None = None,
) -> dict:
    """Register a new patient in the mock store and return the record."""
    with _patients_lock:
        patients = _load(PATIENTS_FILE)
        existing_ids = {p["patient_id"] for p in patients}
        n = len(patients) + 1
        while f"p-{n:03d}" in existing_ids:
            n += 1
        new_id = f"p-{n:03d}"

        patient = {
            "patient_id": new_id,
            "name": full_name,
            "dob": dob,
            "phone": phone,
            "email": email,
            "insurance": {"carrier": insurance_carrier, "member_id": member_id},
            "chief_complaint": chief_complaint,
            "new_patient": True,
            "registered_at": datetime.now().isoformat(timespec="seconds"),
        }
        patients.append(patient)
        _save(PATIENTS_FILE, patients)

    logger.info(f"Registered new patient {new_id} ({full_name})")
    return patient


def send_confirmation(patient: dict, appointment: dict) -> dict:
    """Mock-send an appointment confirmation (log only — no real SMS/email)."""
    phone = (patient or {}).get("phone")
    email = (patient or {}).get("email")
    when = appointment.get("when")
    provider = appointment.get("provider")
    message = (
        f"Lakeside Family Clinic: your initial exam is confirmed for {when} "
        f"with {provider}."
    )
    logger.info(f"[CONFIRMATION-MOCK] phone={phone} email={email} :: {message}")
    return {"sent": False, "channel": "log", "phone": phone, "email": email, "message": message}
