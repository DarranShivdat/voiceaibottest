# Clinic New-Patient Intake Voice Agent (Pipecat demo)

A **standalone, laptop-only** inbound-call voice agent for a general clinic. It
answers like a front desk, figures out whether you're a **new or existing**
patient, walks a new patient through **referral → intake → a mock cost estimate →
booking an Initial Exam (IE)**, and books an existing patient straight into a
slot. You talk to it from a browser tab — no phone number, no real EMR, no real
insurance, no real SMS/email. All data is mocked in local JSON.

Built on **Pipecat v1.4** + **Pipecat Flows** (Deepgram STT · Claude LLM
`claude-haiku-4-5` · Cartesia TTS with Deepgram-TTS failover · SmallWebRTC ·
Silero VAD + Smart Turn v3).

```
Browser mic  ──▶  SmallWebRTC  ──▶  Deepgram STT  ──▶  ┌── Pipecat Flows ──┐
                                                       │  (FlowManager)    │
Browser speaker ◀── SmallWebRTC ◀── Cartesia TTS ◀──── │  Claude (haiku)   │
                       (Deepgram TTS failover)         └───────────────────┘
        Silero VAD (conf 0.7 / stop 0.6s) + Smart Turn v3 detect end-of-turn
```

## Conversation flow

| Stage | Node (`flow.py`) | Tools |
|------|------------------|-------|
| 1. Greeting | `greeting` | — |
| 2. New vs. existing | `greeting` | `verify_existing_patient` / `begin_new_intake` |
| 3. Referral? (new only) | `referral` | `set_referral` |
| 4. Collect intake (conversational) | `intake` | `submit_intake` → `create_patient` |
| 5. Mock out-of-pocket estimate | `estimate` | `get_cost_estimate` |
| 6. Offer + book an IE slot | `scheduling` | `get_available_slots`, `book_appointment` |
| 7. Confirm + close | `confirm` | `send_confirmation` (log only) |

**Existing patients** verify (name + DOB) and jump straight to scheduling — no
intake re-collection.

**Edge paths, all handled gracefully:**
- Existing patient not found → `existing_not_found` (offer to register as new).
- No open slots → `no_slots` (take a callback number → `callback`).
- Caller declines to give info → `caller_declines` → human handoff.
- **Human handoff** (`handoff`) — reachable from *every* node for any
  out-of-scope request ("let me get someone from our team…").

### Conversation quality

- Short, natural sentences; brief backchannels ("Got it", "Thanks").
- **Barge-in** is automatic (Pipecat interrupts TTS on caller speech). Because
  the flow only advances on explicit tool calls, an interruption never restarts
  the script — the agent handles what you said and resumes the current step.
- Intake is a single node with append-context, so the agent doesn't re-ask
  what you already answered; it reads DOB and the appointment time back to you.

## Setup

Requires **Python 3.11+** (tested on 3.13 with Pipecat 1.4.0).

```bash
cd voiceaidemo
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # or:  uv pip install -r requirements.txt

cp .env.example .env
# edit .env: set DEEPGRAM_API_KEY, CARTESIA_API_KEY, ANTHROPIC_API_KEY
```

Only those three keys are required. First run downloads the local Silero VAD and
Smart Turn v3 models (a few seconds, once).

## Run

```bash
python bot.py            # or: uv run bot.py
```

On boot the process **pre-warms once** (before accepting connections): it loads
the Silero VAD and Smart Turn v3 models into memory and sends a tiny request to
each service (1-token Anthropic call, Deepgram init, short Cartesia synth) to
establish DNS/TLS. You'll see per-step logs ending in `Warmup complete` — this
takes a few seconds so the **first** call (including the opening greeting) is
already fast instead of paying cold-start cost. Warmup is best-effort: if a step
fails it logs a warning and startup continues.

Two front-ends are served by the same bot on `:7860`:

- **http://localhost:7860/phone** — a polished "phone call" UI (recommended for
  demos): clinic call screen, Call/End buttons, live call timer, a speaking
  indicator, mute, and a streaming chat-bubble transcript of both sides. Each
  call creates a brand-new WebRTC client/transport and fully tears it down on
  hang-up, so **Call → Hang up → Call works repeatedly without refreshing**.
  Requires internet (loads the Pipecat JS client SDK from a CDN).
- **http://localhost:7860/client** — the prebuilt Pipecat Playground (unchanged),
  a fallback dev UI.

For either: allow the microphone, click **Call** / **Connect**, and start talking.

**Try the new-patient path:** say you're a new patient → answer the referral
question → give your details when asked (interrupt once mid-way to see it
resume) → hear the estimate → pick a time → get confirmation.

**Try the existing-patient path:** say you've been seen before and give a known
name + DOB:

| Name | DOB | Insurance |
|------|-----|-----------|
| Maria Garcia | 1985-03-12 | Blue Shield |
| James Chen | 1990-11-23 | Aetna |
| Aisha Mohammed | 1978-07-04 | UnitedHealthcare |
| Robert Johnson | 1965-01-30 | Medicare |
| Emily Nguyen | 2001-09-15 | Cigna |
| David Okafor | 1972-05-08 | Kaiser |

*(Maria Garcia's 9:00 AM slot is pre-booked in the mock data — pick another.)*

### What to verify

1. **Booking persists** — `data/slots.json`: the chosen slot flips to
   `"status": "booked"` with a `patient_id` + `booked_at`.
2. **New patient persists** — `data/patients.json` gains the registered patient.
3. **Transcript saved** — `transcripts/transcript-<timestamp>.txt`.
4. **Per-turn latency logged** — e.g.
   `⏱  turn latency: STT=120ms LLM=430ms TTS=90ms | TTFB sum=640ms (target <800ms: OK)`.
5. **End-of-call summary** prints on hang-up:

   ```
   ================= CALL SUMMARY =================
   Patient:      Jordan Lee (new)
   Action:       booked
   Appointment:  Wednesday, July 1 at 2:00 PM with Dr. Alan Smith
   Est. cost:    ~$120 out of pocket (mock estimate)
   Turns:        16 (8 caller / 8 agent)
   Avg latency:  640ms TTFB/turn (target <800ms)
   Transcript:   transcripts/transcript-....txt
   ================================================
   ```

## How to swap a provider

Config-driven — edit `.env` and restart. No code changes.

| What | Env var | Options |
|------|---------|---------|
| Speech-to-text | `STT_PROVIDER` | `deepgram` |
| LLM (A/B this) | `LLM_PROVIDER` | `anthropic`, `openai`, `google` |
| LLM model | `ANTHROPIC_MODEL` / `OPENAI_MODEL` / `GOOGLE_MODEL` | e.g. `claude-sonnet-4-6` |
| Text-to-speech | `TTS_PROVIDER` | `cartesia`, `deepgram`, `openai` |
| TTS failover target | `TTS_FALLBACK_PROVIDER` | any TTS provider, or empty to disable |

The default LLM is **`claude-haiku-4-5`** — chosen for low latency in real-time
voice. Bump to `claude-sonnet-4-6` for more headroom; the per-turn latency log
shows the trade-off.

**TTS failover:** the TTS runs through Pipecat's `ServiceSwitcher` with a
failover strategy. If Cartesia errors/times out, it auto-switches to Deepgram
TTS (reusing the Deepgram key — no extra credentials) and logs
`[TTS FAILOVER] now using ...`.

## Tools (`tools.py`)

`look_up_patient`, `get_available_slots`, `book_appointment`,
`get_cost_estimate(carrier, has_referral)`, `create_patient(...)`,
`send_confirmation(...)` (log-only). Cost estimates come from
`data/insurance_estimates.json` — clearly mock ballparks, never verified amounts.

## Project layout

```
bot.py            Entrypoint: pipeline + Flows + runner + observers
flow.py           Intake conversation graph (branches + edge/handoff paths)
services.py       Swappable STT / LLM / TTS factories (+ TTS failover)
tools.py          Mock data store + the 6 callable tools
observers.py      Transcript logging + per-turn latency + end-of-call summary
config.py         Env-driven provider/model configuration
data/
  patients.json             6 existing patients (name, DOB, phone, email, insurance)
  slots.json                10 IE slots (mutated on booking)
  insurance_estimates.json  mock out-of-pocket table (carrier x referral)
transcripts/      Timestamped conversation transcripts (audit record)
```

## Not in this demo (by design)

No real EMR, no live telephony, no real insurance APIs, no real SMS/email, no
Penciled data. One agent, one flow with branches — no multi-agent, no config UI.
Telephony scaffolding exists but is gated behind `ENABLE_TELEPHONY=false` and
never affects the browser demo.
