"""Clinic scheduling voice agent — entrypoint.

Wires the Pipecat v1.x pipeline (SmallWebRTC transport + Deepgram STT + swappable
LLM + Cartesia TTS with failover + Silero VAD + Smart Turn v3), drives the
conversation with Pipecat Flows, and logs the transcript + per-turn latency.

Run locally and talk from a browser:

    uv run bot.py        # or: python bot.py
    # then open http://localhost:7860/client and click Connect
"""

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
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
from flow import create_greeting_node
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
    logger.info(
        f"Starting clinic scheduler | STT={config.STT_PROVIDER} "
        f"LLM={config.LLM_PROVIDER}:{config.ANTHROPIC_MODEL if config.LLM_PROVIDER == 'anthropic' else ''} "
        f"TTS={config.TTS_PROVIDER}"
    )

    stt = build_stt()
    llm = build_llm()
    tts = build_tts()  # may be a ServiceSwitcher (auto-failover)

    # Universal, provider-agnostic context (v1.x) — lets us swap LLMs freely.
    context = LLMContext()
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
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

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):  # noqa: ANN001
        logger.info("Client connected — starting scheduling flow.")
        transcript.start_session()
        await flow_manager.initialize(create_greeting_node())

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):  # noqa: ANN001
        logger.info("Client disconnected.")
        transcript.close()

    runner = WorkerRunner(handle_sigint=getattr(runner_args, "handle_sigint", True))
    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments) -> None:
    """Entry point discovered by the Pipecat runner."""
    transport = await create_transport(runner_args, build_transport_params())
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
