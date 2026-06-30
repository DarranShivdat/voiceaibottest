# Clinic Scheduling Voice Agent (Pipecat demo)

A **standalone, laptop-only** voice AI agent that talks to a caller, verifies who
they are, offers open appointment slots, and books one — built on **Pipecat
v1.x** + **Pipecat Flows**. You talk to it from a browser tab; no phone number
and no real EMR required. All patient and appointment data is mocked in local
JSON files.

```
Browser mic  ──▶  SmallWebRTC  ──▶  Deepgram STT  ──▶  ┌── Pipecat Flows ──┐
                                                       │  (FlowManager)    │
Browser speaker ◀── SmallWebRTC ◀── Cartesia TTS ◀──── │  Claude (LLM)     │
                       (Deepgram TTS failover)         └───────────────────┘
        Silero VAD + Smart Turn v3 decide when the caller has finished talking
```

## Conversation flow

| Stage | Node (`flow.py`) | Tool(s) called |
|------|------------------|----------------|
| 1. Greeting | `greeting` | — |
| 2. Verify identity (name + DOB) | `greeting` → | `look_up_patient(name, dob)` |
| 3. Offer 2–3 slots | `offer_slots` | `get_available_slots()` |
| 4. Book the chosen slot | `offer_slots` → | `book_appointment(patient_id, slot_id)` + `send_sms_confirmation(...)` |
| 5. Confirm + close | `confirm` | — (ends the call) |

Failure paths are handled: **patient not found** (re-ask, then a graceful
`not_found` exit), **no slots available** (`no_slots` exit), and **caller
declines** (`decline_booking` → `declined` exit).

## Setup

Requires **Python 3.11+**.

```bash
cd voiceaidemo
python3 -m venv .venv && source .venv/bin/activate

pip install -r requirements.txt          # or:  uv pip install -r requirements.txt

cp .env.example .env
# edit .env and set DEEPGRAM_API_KEY, CARTESIA_API_KEY, ANTHROPIC_API_KEY
```

The three required keys are `DEEPGRAM_API_KEY`, `CARTESIA_API_KEY`,
`ANTHROPIC_API_KEY`. Everything else is optional (see `.env.example`).

> First run downloads the local Silero VAD and Smart Turn v3 models (a few
> seconds, once).

## Run

```bash
python bot.py            # or: uv run bot.py
```

Then open **http://localhost:7860/client** in your browser, allow microphone
access, and click **Connect**. Start talking — the agent greets you and asks for
your name and date of birth.

Use one of the **mock patients** to get verified:

| Name | Date of birth |
|------|---------------|
| Maria Garcia | 1985-03-12 |
| James Chen | 1990-11-23 |
| Aisha Mohammed | 1978-07-04 |
| Robert Johnson | 1965-01-30 |
| Emily Nguyen | 2001-09-15 |

After booking you can verify the success criteria:

1. **Slot updated** — open `data/slots.json`; the chosen slot now has
   `"status": "booked"`, a `patient_id`, and a `booked_at` timestamp.
2. **Transcript saved** — see `transcripts/transcript-<timestamp>.txt` (also
   streamed to the console).
3. **Latency logged** — each agent turn logs a line like
   `⏱  turn latency: STT=120ms LLM=480ms TTS=90ms | TTFB sum=690ms (target <800ms: OK)`.

## How to swap a provider

Everything is config-driven — change `.env` and restart `bot.py`. No code edits.

| What | Env var | Options |
|------|---------|---------|
| Speech-to-text | `STT_PROVIDER` | `deepgram` |
| **LLM (A/B this)** | `LLM_PROVIDER` | `anthropic`, `openai`, `google` |
| Text-to-speech | `TTS_PROVIDER` | `cartesia`, `deepgram`, `openai` |
| TTS failover target | `TTS_FALLBACK_PROVIDER` | any TTS provider, or empty to disable |
| LLM model | `ANTHROPIC_MODEL` / `OPENAI_MODEL` / `GOOGLE_MODEL` | e.g. `claude-haiku-4-5` |

Examples:

```bash
# A/B the LLM against OpenAI
LLM_PROVIDER=openai      # also set OPENAI_API_KEY

# ...or Gemini
LLM_PROVIDER=google      # also set GOOGLE_API_KEY

# Hit the ~800ms latency target with a faster Claude model
ANTHROPIC_MODEL=claude-haiku-4-5
```

### TTS failover

The TTS service is wired through Pipecat's `ServiceSwitcher` with a **failover**
strategy: if the primary TTS (`TTS_PROVIDER`) errors or times out, the pipeline
automatically switches to `TTS_FALLBACK_PROVIDER` and logs
`[TTS FAILOVER] now using ...`. The default fallback is **Deepgram TTS**, which
reuses the Deepgram key you already need for STT — so failover works with no
extra credentials.

### A note on the LLM model & latency

`ANTHROPIC_MODEL` defaults to `claude-opus-4-8` (highest quality). For a
real-time voice demo aiming for **~800ms per turn**, set
`ANTHROPIC_MODEL=claude-haiku-4-5` (or `claude-sonnet-4-6`) — the per-turn
latency log makes the trade-off visible either way.

## SMS confirmations (optional)

`send_sms_confirmation()` sends a real text via Twilio **only if**
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_FROM_NUMBER` are set.
Otherwise it logs `[SMS-MOCK] to <phone>: <text>`. SMS is secondary — the demo
never blocks on it.

## Stretch: phone (Twilio / Telnyx)

Set `ENABLE_TELEPHONY=true` to also register Twilio/Telnyx websocket transports.
This is off by default and has **no effect on the browser demo**. The same
`bot()` entrypoint answers an inbound phone websocket via the Pipecat runner;
point your Twilio/Telnyx number's media stream at this server (see the
[Pipecat telephony docs](https://docs.pipecat.ai)).

## Project layout

```
bot.py            Entrypoint: pipeline assembly + Flows + runner
flow.py           Pipecat Flows nodes (greeting → offer → book → confirm + failure paths)
services.py       Swappable STT / LLM / TTS factories (+ TTS failover)
tools.py          Mock data store + the 4 callable tools
observers.py      Transcript logging + per-turn STT/LLM/TTS latency
config.py         Env-driven provider/model configuration
data/
  patients.json   ~5 mock patients
  slots.json      ~10 appointment slots (mutated on booking)
transcripts/      Timestamped conversation transcripts (audit record)
.env.example      Every key/setting the app reads
```

Each piece is swappable in isolation: providers live behind `services.py`, the
conversation graph behind `flow.py`, and the data behind `tools.py`.
