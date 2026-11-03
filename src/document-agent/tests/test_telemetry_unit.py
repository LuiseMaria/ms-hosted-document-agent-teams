"""Verify trace configuration and content-safe operation spans offline."""

import unittest
from unittest.mock import Mock, patch

from opentelemetry import baggage, context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from app.telemetry import FoundryCorrelationProcessor, configure_telemetry, operation_span


class TestTelemetry(unittest.TestCase):
    def test_host_conversation_and_response_ids_reach_request_and_child_spans(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(FoundryCorrelationProcessor())
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        request_context = baggage.set_baggage(
            "azure.ai.agentserver.conversation_id", "synthetic-conversation"
        )
        request_context = baggage.set_baggage(
            "azure.ai.agentserver.response_id", "synthetic-response", context=request_context
        )
        token = context.attach(request_context)
        try:
            with (
                patch.dict("os.environ", {
                    "FOUNDRY_AGENT_NAME": "synthetic-agent",
                    "FOUNDRY_AGENT_VERSION": "2",
                }),
                patch("app.telemetry.trace.get_tracer", return_value=provider.get_tracer("test")),
            ):
                with operation_span("document.assessment", kind=SpanKind.SERVER):
                    with operation_span("document.ocr"):
                        pass
            child, request = exporter.get_finished_spans()
            self.assertEqual(request.kind, SpanKind.SERVER)
            self.assertEqual(request.attributes["gen_ai.agent.name"], "synthetic-agent")
            self.assertEqual(request.attributes["gen_ai.agent.id"], "synthetic-agent:2")
            self.assertEqual(request.attributes["gen_ai.operation.name"], "invoke_agent")
            self.assertEqual(child.parent.span_id, request.context.span_id)
            self.assertNotIn("gen_ai.agent.name", child.attributes)
            for span in (request, child):
                self.assertEqual(span.attributes["gen_ai.conversation.id"], "synthetic-conversation")
                self.assertEqual(span.attributes["gen_ai.response.id"], "synthetic-response")
                self.assertFalse(span.events)
        finally:
            context.detach(token)
            provider.shutdown()

    def test_missing_host_ids_are_not_fabricated(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(FoundryCorrelationProcessor())
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        token = context.attach(context.Context())
        try:
            with patch.dict("os.environ", {}, clear=True):
                with provider.get_tracer("test").start_as_current_span("request", kind=SpanKind.SERVER):
                    pass
            attributes = exporter.get_finished_spans()[0].attributes
            self.assertNotIn("gen_ai.conversation.id", attributes)
            self.assertNotIn("gen_ai.response.id", attributes)
            self.assertNotIn("gen_ai.agent.id", attributes)
        finally:
            context.detach(token)
            provider.shutdown()

    def test_missing_configuration_is_explicit_and_does_not_create_exporter(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("app.telemetry.AzureMonitorTraceExporter") as exporter,
            self.assertLogs("app.telemetry", level="WARNING"),
        ):
            self.assertIsNone(configure_telemetry(Mock()))
        exporter.assert_not_called()

    def test_exporter_uses_credential_and_disables_sensitive_capture(self):
        credential = Mock()
        with (
            patch.dict("os.environ", {"APPLICATIONINSIGHTS_CONNECTION_STRING": "synthetic"}),
            patch("app.telemetry.AzureMonitorTraceExporter") as exporter,
            patch("app.telemetry.TracerProvider") as provider,
            patch("app.telemetry.BatchSpanProcessor"),
            patch("app.telemetry.trace.set_tracer_provider") as register,
            patch("app.telemetry.enable_instrumentation") as instrumentation,
        ):
            self.assertIs(configure_telemetry(credential), provider.return_value)
        exporter.assert_called_once_with(
            connection_string="synthetic", credential=credential, disable_offline_storage=True
        )
        register.assert_called_once_with(provider.return_value)
        instrumentation.assert_called_once_with(
            enable_sensitive_data=False, enable_message_events=False
        )

    def test_operation_span_preserves_error_without_recording_its_content(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        try:
            with patch("app.telemetry.trace.get_tracer", return_value=provider.get_tracer("test")):
                with operation_span("document.assessment"):
                    with self.assertRaisesRegex(ValueError, "private-document"):
                        with operation_span("document.ocr"):
                            raise ValueError("private-document")
            spans = exporter.get_finished_spans()
            self.assertEqual(len(spans), 2)
            self.assertEqual(spans[0].status.status_code, StatusCode.ERROR)
            self.assertEqual(spans[0].parent.span_id, spans[1].context.span_id)
            for span in spans:
                self.assertFalse(span.events)
                self.assertFalse(span.attributes)
                self.assertIsNone(span.status.description)
        finally:
            provider.shutdown()
