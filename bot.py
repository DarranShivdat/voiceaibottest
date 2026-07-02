"""Clinic scheduling voice agent — entrypoint.

Wires the Pipecat v1.x pipeline (SmallWebRTC transport + Deepgram STT + swappable
LLM + Cartesia TTS with failover + Silero VAD + Smart Turn v3), drives the
conversation with Pipecat Flows, and logs the transcript + per-turn latency.

Run locally and talk from a browser:

    uv run bot.py        # or: python bot.py
    # then open http://localhost:7860/client and click Connect
"""

import json
import re

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.observers.loggers.metrics_log_observer import MetricsLogObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

import config
import flow
from observers import TranscriptObserver
from services import build_llm, build_stt, build_tts

# Flows package (`pipecat_flows`) or vendored (`pipecat.flows`).
try:
    from pipecat_flows import FlowManager
except ImportError:  # pragma: no cover
    from pipecat.flows import FlowManager  # type: ignore

load_dotenv(override=True)


def build_transport_params() -> dict:
    """Transport factory map for the Pipecat runner.

    The browser (SmallWebRTC) demo is always available. Telephony (Twilio /
    Telnyx websocket) is only registered when ENABLE_TELEPHONY is set, so it
    never affects the local browser demo.
    """
    params = {
        "webrtc": lambda: TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    }
    if config.ENABLE_TELEPHONY:
        from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

        def _telephony_params():
            return FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True)

        params["twilio"] = _telephony_params
        params["telnyx"] = _telephony_params
        logger.info("Telephony transports enabled (twilio, telnyx).")
    return params


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    # Snapshot the active flow ONCE per session: a runtime swap (POST
    # /api/active-flow) only affects the NEXT session, never this one, and voice
    # + greeting are guaranteed to come from the same flow.
    active_flow = flow.get_active_engine()

    logger.info(
        f"Starting clinic scheduler | flow={flow.get_active_flow_name()} "
        f"STT={config.STT_PROVIDER} "
        f"LLM={config.LLM_PROVIDER}:{config.ANTHROPIC_MODEL if config.LLM_PROVIDER == 'anthropic' else ''} "
        f"TTS={config.TTS_PROVIDER}"
    )

    stt = build_stt()
    llm = build_llm()
    tts = build_tts(voice_id=active_flow.voice_id)  # may be a ServiceSwitcher (auto-failover)

    # Universal, provider-agnostic context (v1.x) — lets us swap LLMs freely.
    context = LLMContext()
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(params=VADParams(confidence=0.7, start_secs=0.2, stop_secs=0.6)),
            # Silero VAD + Pipecat Smart Turn v3 for end-of-turn detection.
            user_turn_strategies=UserTurnStrategies(
                stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())],
            ),
        ),
    )

    transcript = TranscriptObserver()

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        observers=[transcript, MetricsLogObserver()],
        idle_timeout_secs=getattr(runner_args, "pipeline_idle_timeout_secs", None),
    )

    flow_manager = FlowManager(
        worker=worker,
        llm=llm,
        context_aggregator=context_aggregator,
        transport=transport,
    )
    # Let the transcript observer read flow state for the end-of-call summary.
    transcript.attach_flow(flow_manager)

    # Idempotent per-call teardown. Without this, on disconnect the worker is
    # never cancelled, so `runner.run()` never returns, the bot task lingers, and
    # the WebRTC peer stays open — a second connect (reusing the client / pc_id)
    # then hangs on "Connecting...". Cancelling the worker lets the runner return
    # and the `finally` below closes the transport + peer so the next session is
    # clean, without restarting the process.
    _torn_down = False

    async def teardown(reason: str) -> None:
        nonlocal _torn_down
        if _torn_down:
            return
        _torn_down = True
        logger.info(f"Tearing down session ({reason}).")
        transcript.close()
        try:
            await worker.cancel(reason=reason)
        except Exception as e:  # noqa: BLE001 - best-effort cleanup
            logger.warning(f"worker.cancel during teardown failed: {e}")

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):  # noqa: ANN001
        logger.info("Client connected — starting scheduling flow.")
        transcript.start_session()
        await flow_manager.initialize(active_flow.build_initial_node())

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):  # noqa: ANN001
        # Cancel the worker so runner.run() returns and resources free.
        await teardown("client disconnected")

    runner = WorkerRunner(handle_sigint=getattr(runner_args, "handle_sigint", True))
    try:
        await runner.add_workers(worker)
        await runner.run()
    finally:
        # Covers both endings: an explicit disconnect and a flow-driven
        # end_conversation (EndFrame) where no disconnect event fires.
        await teardown("run complete")
        try:
            await transport.cleanup()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"transport.cleanup failed: {e}")
        # Close the WebRTC peer so its pc_id is dropped from the runner's session
        # map; otherwise a same-pc_id reconnect renegotiates a dead session.
        connection = getattr(runner_args, "webrtc_connection", None)
        for method_name in ("disconnect", "cleanup"):
            closer = getattr(connection, method_name, None)
            if closer is None:
                continue
            try:
                await closer()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"connection.{method_name} during teardown failed: {e}")
        logger.info("Session torn down; ready for a new connection.")


async def bot(runner_args: RunnerArguments) -> None:
    """Entry point discovered by the Pipecat runner."""
    transport = await create_transport(runner_args, build_transport_params())
    await run_bot(transport, runner_args)


def _register_phone_route() -> None:
    """Additively serve the custom /phone call UI on the runner's app.

    This uses the runner's documented extension point (import the module-level
    `app` and add routes before `main()`). It does NOT touch the bot pipeline,
    flow.py, the /start → offer flow, or the prebuilt /client route — the
    Playground remains fully functional as a fallback.
    """
    import html
    from pathlib import Path

    from fastapi.responses import HTMLResponse

    from pipecat.runner.run import app

    phone_html = Path(__file__).parent / "static" / "phone.html"

    @app.get("/phone")
    async def phone_page():  # noqa: ANN202
        # Read fresh + inject the configurable clinic/assistant names.
        page = (
            phone_html.read_text(encoding="utf-8")
            .replace("__CLINIC_NAME__", html.escape(config.CLINIC_NAME))
            .replace("__ASSISTANT_NAME__", html.escape(config.ASSISTANT_NAME))
        )
        return HTMLResponse(page)

    logger.info("Custom phone UI available at /phone")


def _render_data_page() -> str:
    """Server-render the mock data (read fresh) as a read-only HTML reference."""
    import html
    import json
    from pathlib import Path

    data_dir = Path(__file__).parent / "data"

    def _load(name, default):
        try:
            with open(data_dir / name, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:  # noqa: BLE001 - reference page must never 500
            logger.warning(f"/data: could not read {name}: {exc}")
            return default

    patients = _load("patients.json", [])
    slots = _load("slots.json", [])
    estimates = _load("insurance_estimates.json", {})

    e = html.escape
    clinic = e(config.CLINIC_NAME)

    def cell(v):
        return e("" if v is None else str(v))

    # Patients
    patient_rows = []
    for p in patients:
        ins = p.get("insurance") or {}
        ins_txt = f"{ins.get('carrier') or '—'} / {ins.get('member_id') or '—'}"
        patient_rows.append(
            "<tr>"
            f"<td>{cell(p.get('name'))}</td>"
            f"<td class='mono'>{cell(p.get('dob'))}</td>"
            f"<td class='mono'>{cell(p.get('phone'))}</td>"
            f"<td>{cell(p.get('email'))}</td>"
            f"<td>{cell(ins_txt)}</td>"
            f"<td class='mono'>{cell(p.get('patient_id'))}</td>"
            "</tr>"
        )

    # Slots (import tools for a readable datetime; fall back to raw)
    try:
        import tools

        def when(dt):
            try:
                return tools.format_when(dt)
            except Exception:  # noqa: BLE001
                return dt
    except Exception:  # noqa: BLE001
        def when(dt):
            return dt

    slot_rows = []
    for s in slots:
        status = s.get("status", "")
        badge = "open" if status == "open" else "booked"
        slot_rows.append(
            "<tr>"
            f"<td>{cell(when(s.get('datetime')))}</td>"
            f"<td>{cell(s.get('provider'))}</td>"
            f"<td class='mono'>{cell(s.get('visit_type'))}</td>"
            f"<td><span class='pill {badge}'>{cell(status)}</span></td>"
            "</tr>"
        )

    # Insurance estimates
    est_rows = []
    carriers = estimates.get("carriers", {}) if isinstance(estimates, dict) else {}
    for carrier, vals in carriers.items():
        if str(carrier).startswith("_") or not isinstance(vals, dict):
            continue
        est_rows.append(
            f"<tr><td>{cell(carrier)}</td><td>Yes</td>"
            f"<td class='mono'>${cell(vals.get('with_referral'))}</td></tr>"
        )
        est_rows.append(
            f"<tr><td>{cell(carrier)}</td><td>No</td>"
            f"<td class='mono'>${cell(vals.get('without_referral'))}</td></tr>"
        )
    default = estimates.get("default") if isinstance(estimates, dict) else None
    if isinstance(default, dict):
        est_rows.append(
            f"<tr><td><em>Any other carrier (default)</em></td><td>Yes</td>"
            f"<td class='mono'>${cell(default.get('with_referral'))}</td></tr>"
        )
        est_rows.append(
            f"<tr><td><em>Any other carrier (default)</em></td><td>No</td>"
            f"<td class='mono'>${cell(default.get('without_referral'))}</td></tr>"
        )

    def table(headers, rows, empty):
        head = "".join(f"<th>{e(h)}</th>" for h in headers)
        body = "".join(rows) if rows else f"<tr><td colspan='{len(headers)}' class='empty'>{e(empty)}</td></tr>"
        return f"<div class='table-wrap'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"

    patients_tbl = table(
        ["Name", "DOB", "Phone", "Email", "Insurance (carrier / member ID)", "Patient ID"],
        patient_rows, "No patients.")
    slots_tbl = table(
        ["Date & time", "Provider", "Visit type", "Status"], slot_rows, "No slots.")
    est_tbl = table(
        ["Carrier", "Referral", "Est. out-of-pocket"], est_rows, "No estimates.")

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>TEST INFO — {clinic} (mock data)</title>
<style>
  :root {{ --ink:#0f172a; --muted:#64748b; --line:#e2e8f0; --head:#0f766e; --accent:#f59e0b; --band:#f8fafc; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
         color:var(--ink); margin:0; background:#f1f5f9; padding:20px; line-height:1.4; }}
  .wrap {{ max-width:980px; margin:0 auto; }}
  .banner {{ background:#fffbeb; border:1px solid #fde68a; border-left:6px solid var(--accent);
            border-radius:12px; padding:16px 18px; margin-bottom:22px; }}
  h1 {{ margin:0; font-size:30px; letter-spacing:1px; color:#b45309; }}
  .note {{ margin:4px 0 0; color:var(--muted); font-size:14px; }}
  h2 {{ font-size:16px; text-transform:uppercase; letter-spacing:.6px; color:var(--head);
       margin:26px 0 8px; }}
  .table-wrap {{ overflow-x:auto; border:1px solid var(--line); border-radius:12px; background:#fff; }}
  table {{ width:100%; border-collapse:collapse; font-size:14px; }}
  th, td {{ text-align:left; padding:9px 12px; border-bottom:1px solid var(--line); white-space:nowrap; }}
  th {{ background:var(--band); font-weight:650; color:#334155; position:sticky; top:0; }}
  tbody tr:nth-child(even) {{ background:#fafcff; }}
  tbody tr:last-child td {{ border-bottom:none; }}
  td.mono {{ font-variant-numeric:tabular-nums; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
  td.empty {{ color:var(--muted); text-align:center; font-style:italic; }}
  .pill {{ padding:2px 9px; border-radius:999px; font-size:12px; font-weight:600; }}
  .pill.open {{ background:#dcfce7; color:#166534; }}
  .pill.booked {{ background:#fee2e2; color:#991b1b; }}
  .foot {{ color:var(--muted); font-size:12px; margin-top:26px; }}
</style></head>
<body><div class="wrap">
  <div class="banner">
    <h1>TEST INFO</h1>
    <p class="note">Mock demo data — not real patients.</p>
  </div>

  <h2>Patients (valid existing-patient logins)</h2>
  {patients_tbl}

  <h2>Available slots</h2>
  {slots_tbl}

  <h2>Insurance estimates</h2>
  {est_tbl}

  <p class="foot">Read-only. Reflects the current contents of data/patients.json,
  data/slots.json, and data/insurance_estimates.json at page load.</p>
</div></body></html>"""


def _register_data_route() -> None:
    """Additively serve a read-only /data reference page (mock DB at request time)."""
    from fastapi.responses import HTMLResponse

    from pipecat.runner.run import app

    @app.get("/data")
    async def data_page():  # noqa: ANN202
        return HTMLResponse(_render_data_page())

    logger.info("Test-data reference available at /data")


# --- Visual flow builder (additive; separate front-end, same JSON format) ----
# Serves /builder plus a tiny read/write API over flows/*.json. This does NOT
# touch the bot pipeline, flow_engine.py, tools, existing flows, or the /phone,
# /client, /data routes. It reads and writes the SAME flow config JSON the
# engine already loads, so anything created here runs unchanged via FLOW_CONFIG.
_FLOW_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _flows_dir():
    from pathlib import Path

    return Path(__file__).parent / "flows"


def _safe_flow_path(name: str):
    """Resolve `name` to flows/<name>.json, rejecting path traversal / bad names."""
    from pathlib import Path

    base = name[:-5] if name.endswith(".json") else name
    if not _FLOW_NAME_RE.match(base):
        return None
    flows = _flows_dir().resolve()
    path = (flows / f"{base}.json").resolve()
    # Belt-and-suspenders: the resolved path must stay inside flows/.
    if path.parent != flows:
        return None
    return path


def _register_builder_route() -> None:
    """Additively serve the visual flow builder UI + a small flows/ read/write API."""
    import html
    from pathlib import Path

    from fastapi import Request
    from fastapi.responses import HTMLResponse, JSONResponse, Response

    from pipecat.runner.run import app

    builder_html = Path(__file__).parent / "static" / "builder.html"

    @app.get("/builder")
    async def builder_page():  # noqa: ANN202
        page = (
            builder_html.read_text(encoding="utf-8")
            .replace("__CLINIC_NAME__", html.escape(config.CLINIC_NAME))
            .replace("__ASSISTANT_NAME__", html.escape(config.ASSISTANT_NAME))
        )
        return HTMLResponse(page)

    @app.get("/api/flows")
    async def list_flows():  # noqa: ANN202
        flows = _flows_dir()
        items = []
        if flows.is_dir():
            for p in sorted(flows.glob("*.json")):
                name = p.stem
                display = name
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        display = json.load(f).get("name", name)
                except Exception as exc:  # noqa: BLE001 - listing must never 500
                    logger.warning(f"/api/flows: could not read {p.name}: {exc}")
                items.append({"file": p.name, "name": name, "title": display})
        return JSONResponse({"flows": items})

    @app.get("/api/flows/{name}")
    async def get_flow(name: str):  # noqa: ANN202
        path = _safe_flow_path(name)
        if path is None:
            return JSONResponse({"error": "invalid flow name"}, status_code=400)
        if not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        # Return the raw text so the editor sees the file byte-for-byte.
        return Response(path.read_text(encoding="utf-8"), media_type="application/json")

    @app.post("/api/flows/{name}")
    async def save_flow(name: str, request: Request):  # noqa: ANN202
        path = _safe_flow_path(name)
        if path is None:
            return JSONResponse({"error": "invalid flow name"}, status_code=400)
        raw = (await request.body()).decode("utf-8")
        # Validate that it parses AND has the top-level fields the engine reads,
        # so we never write a file that would crash FlowEngine on load.
        try:
            spec = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": f"invalid JSON: {exc}"}, status_code=400)
        missing = [k for k in ("persona", "initial_node", "functions", "nodes") if k not in spec]
        if missing:
            return JSONResponse(
                {"error": f"missing required field(s): {', '.join(missing)}"}, status_code=400
            )
        if spec["initial_node"] not in spec.get("nodes", {}):
            return JSONResponse(
                {"error": f"initial_node '{spec['initial_node']}' is not a defined node"},
                status_code=400,
            )
        _flows_dir().mkdir(parents=True, exist_ok=True)
        # Write the editor's bytes verbatim (round-trip-safe: what you export is
        # exactly what lands on disk).
        path.write_text(raw, encoding="utf-8")
        logger.info(f"Flow builder saved {path.name} ({len(raw)} bytes)")
        return JSONResponse({"ok": True, "file": path.name})

    logger.info("Visual flow builder available at /builder")


# --- Builder component library (additive; same pattern as the flows API) -----
# Serves a tiny read/write API over components/*.json — reusable pieces the
# builder's left rail offers for drag-in. Same name validation and traversal
# guard as /api/flows. Components are a builder-only concept: each one is just
# a saved set of questions the builder instantiates as a normal collect node,
# so the engine and flow format are untouched.
def _components_dir():
    from pathlib import Path

    return Path(__file__).parent / "components"


def _safe_component_path(name: str):
    """Resolve `name` to components/<name>.json, rejecting path traversal / bad names."""
    base = name[:-5] if name.endswith(".json") else name
    if not _FLOW_NAME_RE.match(base):
        return None
    comps = _components_dir().resolve()
    path = (comps / f"{base}.json").resolve()
    # Belt-and-suspenders: the resolved path must stay inside components/.
    if path.parent != comps:
        return None
    return path


def _register_components_route() -> None:
    """Additively serve the builder's reusable-component library (components/*.json)."""
    from fastapi import Request
    from fastapi.responses import JSONResponse, Response

    from pipecat.runner.run import app

    @app.get("/api/components")
    async def list_components():  # noqa: ANN202
        comps = _components_dir()
        items = []
        if comps.is_dir():
            for p in sorted(comps.glob("*.json")):
                name = p.stem
                title = name
                fields = []
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    title = data.get("name", name)
                    fields = data.get("fields", [])
                except Exception as exc:  # noqa: BLE001 - listing must never 500
                    logger.warning(f"/api/components: could not read {p.name}: {exc}")
                items.append({"file": p.name, "name": name, "title": title, "fields": fields})
        return JSONResponse({"components": items})

    @app.get("/api/components/{name}")
    async def get_component(name: str):  # noqa: ANN202
        path = _safe_component_path(name)
        if path is None:
            return JSONResponse({"error": "invalid component name"}, status_code=400)
        if not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return Response(path.read_text(encoding="utf-8"), media_type="application/json")

    @app.post("/api/components/{name}")
    async def save_component(name: str, request: Request):  # noqa: ANN202
        path = _safe_component_path(name)
        if path is None:
            return JSONResponse({"error": "invalid component name"}, status_code=400)
        raw = (await request.body()).decode("utf-8")
        try:
            spec = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": f"invalid JSON: {exc}"}, status_code=400)
        if not isinstance(spec.get("name"), str) or not spec["name"].strip():
            return JSONResponse({"error": "missing required field: name"}, status_code=400)
        fields = spec.get("fields")
        if not isinstance(fields, list) or not fields:
            return JSONResponse({"error": "fields must be a non-empty list"}, status_code=400)
        for f in fields:
            if not isinstance(f, dict) or not f.get("question") or not f.get("field"):
                return JSONResponse(
                    {"error": "each field needs a question and a field key"}, status_code=400
                )
        _components_dir().mkdir(parents=True, exist_ok=True)
        path.write_text(raw, encoding="utf-8")
        logger.info(f"Flow builder saved component {path.name} ({len(raw)} bytes)")
        return JSONResponse({"ok": True, "file": path.name})

    logger.info("Component library available at /api/components")


# --- Runtime controls: active-flow hot-swap + voice picker/preview -----------
# Additive demo-view plumbing. These read/mutate ONLY the module-level active
# flow (via flow.py) and the curated voice list (config.VOICES); they do not
# touch the pipeline, engine parsing, or any existing route. Swapping the active
# flow affects only the NEXT session — in-progress calls are unaffected.
def _register_runtime_route() -> None:
    from fastapi import Request
    from fastapi.responses import JSONResponse, Response

    from pipecat.runner.run import app

    @app.get("/api/active-flow")
    async def get_active_flow():  # noqa: ANN202
        return JSONResponse({"name": flow.get_active_flow_name()})

    @app.post("/api/active-flow")
    async def set_active_flow(request: Request):  # noqa: ANN202
        try:
            body = json.loads((await request.body()).decode("utf-8") or "{}")
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": f"invalid JSON: {exc}"}, status_code=400)
        name = (body.get("flow") or "").strip()
        if not name:
            return JSONResponse({"error": "missing 'flow'"}, status_code=400)
        # Same name/traversal validation as /api/flows.
        path = _safe_flow_path(name)
        if path is None:
            return JSONResponse({"error": "invalid flow name"}, status_code=400)
        if not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            # Loads through FlowEngine; on any parse/build error the previous
            # flow stays active (swap_active_flow raises before swapping).
            active = flow.swap_active_flow(str(path), path.stem)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"/api/active-flow: refused swap to '{name}': {exc}")
            return JSONResponse(
                {"error": f"flow failed to load: {exc}", "active": flow.get_active_flow_name()},
                status_code=400,
            )
        return JSONResponse({"ok": True, "active": active})

    @app.get("/api/voices")
    async def list_voices():  # noqa: ANN202
        return JSONResponse({"voices": config.VOICES, "default": config.DEFAULT_VOICE_ID})

    @app.post("/api/voice-preview")
    async def voice_preview(request: Request):  # noqa: ANN202
        import os

        try:
            body = json.loads((await request.body()).decode("utf-8") or "{}")
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": f"invalid JSON: {exc}"}, status_code=400)
        voice_id = (body.get("voice_id") or "").strip()
        if not any(v["id"] == voice_id for v in config.VOICES):
            return JSONResponse({"error": "unknown voice_id"}, status_code=400)
        text = (body.get("text") or "").strip() or "Hi, thanks for calling. How can I help you today?"
        text = text[:200]  # cap preview length

        key = os.getenv("CARTESIA_API_KEY")
        if not key:
            return JSONResponse({"error": "CARTESIA_API_KEY not configured"}, status_code=503)

        import httpx

        payload = {
            "model_id": config.CARTESIA_MODEL,
            "transcript": text,
            "voice": {"mode": "id", "id": voice_id},
            "output_format": {"container": "wav", "encoding": "pcm_s16le", "sample_rate": 44100},
        }
        headers = {"Cartesia-Version": "2026-03-01", "X-API-Key": key}
        try:
            async with httpx.AsyncClient(timeout=15.0) as http:
                resp = await http.post(
                    "https://api.cartesia.ai/tts/bytes", json=payload, headers=headers
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"/api/voice-preview: synth request failed: {exc}")
            return JSONResponse({"error": f"synth failed: {exc}"}, status_code=502)
        if resp.status_code != 200:
            logger.warning(f"/api/voice-preview: Cartesia HTTP {resp.status_code}: {resp.text[:200]}")
            return JSONResponse(
                {"error": f"Cartesia returned HTTP {resp.status_code}"}, status_code=502
            )
        return Response(content=resp.content, media_type="audio/wav")

    logger.info("Runtime controls available at /api/active-flow, /api/voices, /api/voice-preview")


if __name__ == "__main__":
    import asyncio

    from warmup import run_warmup

    # Pre-warm ONCE at process boot (before the server accepts connections) so the
    # first real call — including the user-facing opening greeting — is already
    # fast. Blocking here is intentional: the process comes up warm.
    asyncio.run(run_warmup())

    # Additive routes — register before main() configures/serves the app.
    _register_phone_route()
    _register_data_route()
    _register_builder_route()
    _register_components_route()
    _register_runtime_route()

    from pipecat.runner.run import main

    main()
