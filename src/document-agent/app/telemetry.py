"""Entra-authenticated Azure Monitor traces without document or prompt capture."""

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager

from agent_framework.observability import enable_instrumentation
from azure.core.credentials import TokenCredential
from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
from opentelemetry import baggage, context, trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Span as SdkSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, SpanKind, StatusCode

logger = logging.getLogger(__name__)


class FoundryCorrelationProcessor(SpanProcessor):
    """Copy hosting correlation metadata, never request bodies, onto spans."""

    def on_start(self, span: SdkSpan, parent_context: Context | None = None) -> None:
        current = parent_context if parent_context is not None else context.get_current()
        project_id = os.getenv("FOUNDRY_PROJECT_ARM_ID", "").strip()
        if project_id:
            span.set_attribute("microsoft.foundry.project.id", project_id)
        for source, target in (
            ("azure.ai.agentserver.conversation_id", "gen_ai.conversation.id"),
            ("azure.ai.agentserver.response_id", "gen_ai.response.id"),
        ):
            value = baggage.get_baggage(source, context=current)
            if isinstance(value, str) and value:
                span.set_attribute(target, value)
        if span.kind == SpanKind.SERVER:
            name = os.getenv("FOUNDRY_AGENT_NAME", "").strip()
            version = os.getenv("FOUNDRY_AGENT_VERSION", "").strip()
            if name:
                span.set_attribute("gen_ai.agent.name", name)
                if version:
                    span.set_attribute("gen_ai.agent.version", version)
                    span.set_attribute("gen_ai.agent.id", f"{name}:{version}")
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.provider.name", "AzureAI Hosted Agents")


def configure_telemetry(credential: TokenCredential) -> TracerProvider | None:
    connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if not connection_string:
        logger.warning("Agent trace export disabled: Application Insights is not configured.")
        return None

    exporter = AzureMonitorTraceExporter(
        connection_string=connection_string,
        credential=credential,
        disable_offline_storage=True,
    )
    provider = TracerProvider(resource=Resource.create({"service.name": "document-agent"}))
    provider.add_span_processor(FoundryCorrelationProcessor())
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    enable_instrumentation(enable_sensitive_data=False, enable_message_events=False)
    logger.info("Agent trace export configured with Entra authentication; content capture disabled.")
    return provider


@contextmanager
def operation_span(name: str, *, kind: SpanKind = SpanKind.INTERNAL) -> Iterator[Span]:
    # Exception messages can contain document text or model responses.
    with trace.get_tracer(__name__).start_as_current_span(
        name, kind=kind, record_exception=False, set_status_on_exception=False
    ) as span:
        try:
            yield span
        except Exception:
            span.set_status(StatusCode.ERROR)
            raise
