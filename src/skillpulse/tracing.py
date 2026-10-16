"""Opt-in, metadata-only Langfuse traces using its OTLP/HTTP endpoint.

No model hooks are installed: prompts, answers, CVs and tool arguments never enter
this exporter. It uses the existing HTTP client dependency, not a second SDK.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import TYPE_CHECKING, Any

import httpx

from skillpulse.config import Settings, get_settings

if TYPE_CHECKING:
    from skillpulse.agent.graph import AgentRun

logger = logging.getLogger(__name__)


def record_agent_trace(run: AgentRun, started_ns: int, *, settings: Settings | None = None) -> None:
    """Export one bounded summary; telemetry failures never change the answer."""
    try:
        configuration = settings if settings is not None else get_settings()
        if not configuration.langfuse_enabled:
            return
        if not (
            configuration.langfuse_base_url
            and configuration.langfuse_public_key
            and configuration.langfuse_secret_key
        ):
            logger.warning("Langfuse export skipped: configuration is incomplete")
            return
        trace_id = run.trace_id if re.fullmatch(r"[0-9a-f]{32}", run.trace_id) else uuid.uuid4().hex
        attributes: list[dict[str, Any]] = [
            {"key": "langfuse.trace.name", "value": {"stringValue": "skillpulse-agent"}},
            {"key": "langfuse.trace.public", "value": {"boolValue": False}},
            {"key": "langfuse.trace.metadata.grounded", "value": {"boolValue": run.grounded}},
            {"key": "langfuse.trace.metadata.partial", "value": {"boolValue": run.partial}},
            {
                "key": "langfuse.trace.metadata.failed",
                "value": {"boolValue": run.error is not None},
            },
            {
                "key": "langfuse.trace.metadata.tool_calls",
                "value": {"intValue": str(run.tool_calls_used)},
            },
        ]
        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "skillpulse"}}
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "skillpulse.metadata"},
                            "spans": [
                                {
                                    "traceId": trace_id,
                                    "spanId": uuid.uuid4().hex[:16],
                                    "name": "skillpulse-agent",
                                    "kind": 1,
                                    "startTimeUnixNano": str(started_ns),
                                    "endTimeUnixNano": str(time.time_ns()),
                                    "attributes": attributes,
                                    "status": {"code": 2 if run.error else 1},
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        with httpx.Client(timeout=1.0, follow_redirects=False) as client:
            response = client.post(
                configuration.langfuse_base_url.rstrip("/") + "/api/public/otel/v1/traces",
                auth=(configuration.langfuse_public_key, configuration.langfuse_secret_key),
                headers={"x-langfuse-ingestion-version": "4"},
                json=payload,
            )
            response.raise_for_status()
    except Exception:
        # Do not log the exception: HTTP errors can contain credentials or payloads.
        logger.warning("Langfuse export failed")
