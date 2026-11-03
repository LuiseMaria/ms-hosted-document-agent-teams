"""Offline tests for OCR, document sessions and streaming parity."""

import asyncio
import json
import unittest
from contextlib import nullcontext
from unittest.mock import AsyncMock, Mock, patch

from agent_framework import AgentSession, Content, Message
from agent_framework_foundry_hosting._responses import _convert_message_content

from app.agent import DocumentAgent
from app.attachments import AttachmentResolver
from app.workflow import AssessmentState, build_assessment_workflow
from app.analysis import load_prompts


def _message(data: bytes = b"%PDF-demo", name: str = "FINANCIAL_SUMMARY.pdf") -> Message:
    return Message(role="user", contents=[
        Content.from_text("Bitte analysieren."),
        Content.from_data(data, "application/pdf", additional_properties={"filename": name}),
    ])


class TestDocumentAgent(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.ocr = Mock()
        self.ocr.extract_text.return_value = "Umsatz: 100 EUR"
        self.workflow = Mock()
        self.workflow.run = AsyncMock(return_value=Mock(get_outputs=lambda: ["Firmenanalyse"]))
        self.builder = patch("app.agent.build_assessment_workflow", return_value=self.workflow).start()
        self.addCleanup(patch.stopall)
        self.agent = DocumentAgent(chat_client=Mock(), ocr_provider=self.ocr, run_validation=True)

    async def test_inline_file_reaches_ocr_and_model_prompt(self) -> None:
        attachment = _convert_message_content({
            "type": "input_file", "filename": "FINANCIAL_SUMMARY.pdf",
            "file_data": "data:application/pdf;base64,JVBERi1kZW1v",
        })
        response = await self.agent.run(Message(role="user", contents=[attachment]))
        self.assertEqual(response.text, "Firmenanalyse")
        self.ocr.extract_text.assert_called_once_with(b"%PDF-demo", content_type="application/pdf")
        state = self.workflow.run.call_args.args[0]
        self.assertIn("Umsatz: 100 EUR", state.assembled_input)
        self.assertEqual(state.financial_summary_context, "Umsatz: 100 EUR")
        self.assertNotIn("Es wurden keine Dokumente angehängt", state.assembled_input)

    async def test_document_survives_session_serialization_and_followup(self) -> None:
        session = AgentSession()
        await self.agent.run(_message(), session=session)
        restored = AgentSession.from_dict(json.loads(json.dumps(session.to_dict())))
        await self.agent.run("Wie hoch ist der Umsatz?", session=restored)
        state = self.workflow.run.call_args.args[0]
        self.assertIn("Umsatz: 100 EUR", state.assembled_input)
        self.assertIn("Wie hoch ist der Umsatz?", state.assembled_input)
        self.assertEqual(self.ocr.extract_text.call_count, 1)
        self.assertNotIn("%PDF-demo", json.dumps(restored.to_dict()))

    async def test_file_id_reaches_ocr_and_model_instead_of_missing_file_prompt(self) -> None:
        client = Mock()
        client.files.retrieve.return_value = Mock(bytes=9, filename="FINANCIAL_SUMMARY.pdf")
        response = Mock()
        response.iter_bytes.return_value = iter([b"%PDF-demo"])
        client.files.with_streaming_response.content.return_value = nullcontext(response)
        self.agent._attachment_resolver = AttachmentResolver(client)
        content = _convert_message_content({"type": "input_file", "file_id": "file-demo"})
        await self.agent.run(Message(role="user", contents=[content]))
        self.ocr.extract_text.assert_called_once_with(b"%PDF-demo", content_type="application/pdf")
        state = self.workflow.run.call_args.args[0]
        self.assertIn("Umsatz: 100 EUR", state.assembled_input)
        self.assertNotIn("Es wurden keine Dokumente angehängt", state.assembled_input)

    async def test_other_sessions_and_stateless_calls_do_not_see_documents(self) -> None:
        await self.agent.run(_message(), session=AgentSession())
        for session in (AgentSession(), None):
            await self.agent.run("Bitte analysieren.", session=session)
            state = self.workflow.run.call_args.args[0]
            self.assertNotIn("Umsatz: 100 EUR", state.assembled_input)
            self.assertIn("Es wurden keine Dokumente angehängt", state.assembled_input)

    async def test_duplicate_binary_data_does_not_repeat_ocr(self) -> None:
        session = AgentSession()
        await self.agent.run(_message(), session=session)
        await self.agent.run(_message(), session=session)
        self.assertEqual(self.ocr.extract_text.call_count, 1)
        self.assertEqual(len(self.agent._session_documents(session)), 1)

    async def test_same_filename_with_changed_bytes_replaces_document(self) -> None:
        session = AgentSession()
        await self.agent.run(_message(), session=session)
        self.ocr.extract_text.return_value = "Umsatz: 200 EUR"
        await self.agent.run(_message(b"%PDF-changed"), session=session)
        state = self.workflow.run.call_args.args[0]
        self.assertIn("Umsatz: 200 EUR", state.assembled_input)
        self.assertNotIn("Umsatz: 100 EUR", state.assembled_input)
        self.assertEqual(len(self.agent._session_documents(session)), 1)
        self.assertEqual(self.ocr.extract_text.call_count, 2)

    async def test_same_bytes_with_guideline_name_keep_both_document_roles(self) -> None:
        session = AgentSession()
        await self.agent.run(_message(), session=session)
        await self.agent.run(_message(name="ASSESSMENT_GUIDELINE.pdf"), session=session)
        state = self.workflow.run.call_args.args[0]
        self.assertEqual(state.financial_summary_context, "Umsatz: 100 EUR")
        self.assertIn("RICHTLINIE: ASSESSMENT GUIDELINE", state.assembled_input)
        self.assertEqual(len(self.agent._session_documents(session)), 2)
        self.assertEqual(self.ocr.extract_text.call_count, 1)

    async def test_unnamed_duplicate_does_not_create_another_document(self) -> None:
        session = AgentSession()
        message = Message(role="user", contents=[Content.from_data(b"%PDF-demo", "application/pdf")])
        await self.agent.run(message, session=session)
        await self.agent.run(message, session=session)
        self.assertEqual(len(self.agent._session_documents(session)), 1)
        self.assertEqual(self.ocr.extract_text.call_count, 1)

    async def test_replacement_reuses_other_cached_bytes_without_retaining_old_version(self) -> None:
        session = AgentSession()
        await self.agent.run(_message(b"%PDF-first", "first.pdf"), session=session)
        self.ocr.extract_text.return_value = "old second document"
        await self.agent.run(_message(b"%PDF-second", "second.pdf"), session=session)
        await self.agent.run(_message(b"%PDF-first", "second.pdf"), session=session)
        self.assertEqual(self.ocr.extract_text.call_count, 2)
        self.assertEqual(len(self.agent._session_documents(session)), 2)
        self.assertNotIn("old second document", self.workflow.run.call_args.args[0].assembled_input)

    async def test_guideline_is_retained_separately_from_financial_documents(self) -> None:
        session = AgentSession()
        await self.agent.run(_message(name="ASSESSMENT_GUIDELINE.pdf"), session=session)
        await self.agent.run("Bitte prüfen.", session=session)
        state = self.workflow.run.call_args.args[0]
        self.assertIn("RICHTLINIE: ASSESSMENT GUIDELINE", state.assembled_input)
        self.assertIsNone(state.financial_summary_context)

    async def test_bad_attachment_stops_before_workflow(self) -> None:
        content = _convert_message_content({
            "type": "input_file", "file_url": "https://example.invalid/file.pdf",
        })
        with self.assertRaisesRegex(ValueError, "not enabled"):
            await self.agent.run(Message(role="user", contents=[content]))
        self.builder.assert_not_called()
        self.ocr.extract_text.assert_not_called()

    async def test_ocr_failure_does_not_change_session(self) -> None:
        session = AgentSession()
        await self.agent.run(_message(), session=session)
        original = session.to_dict()
        self.ocr.extract_text.side_effect = ValueError("OCR failed")
        with self.assertRaisesRegex(ValueError, "OCR failed"):
            await self.agent.run(_message(b"%PDF-new", "new.pdf"), session=session)
        self.assertEqual(session.to_dict(), original)

    async def test_empty_ocr_is_an_error(self) -> None:
        self.ocr.extract_text.return_value = "  "
        with self.assertRaisesRegex(ValueError, "No readable text"):
            await self.agent.run(_message())
        self.builder.assert_not_called()

    async def test_context_limit_is_enforced_before_model_call(self) -> None:
        with patch("app.agent.MAX_CONTEXT_CHARACTERS", 3):
            with self.assertRaisesRegex(ValueError, "context limit"):
                await self.agent.run(_message())
        self.builder.assert_not_called()

    async def test_document_count_limit_is_enforced_before_extra_ocr(self) -> None:
        session = AgentSession()
        await self.agent.run(_message(), session=session)
        with patch("app.agent.MAX_DOCUMENTS", 1):
            with self.assertRaisesRegex(ValueError, "at most"):
                await self.agent.run(_message(b"%PDF-new", "new.pdf"), session=session)
        self.assertEqual(self.ocr.extract_text.call_count, 1)

    async def test_workflow_failure_does_not_persist_partial_documents(self) -> None:
        session = AgentSession()
        self.workflow.run.side_effect = RuntimeError("Model failed")
        with self.assertRaisesRegex(RuntimeError, "Model failed"):
            await self.agent.run(_message(), session=session)
        self.assertEqual(session.state, {})

    async def test_missing_workflow_output_is_an_error_not_success_fallback(self) -> None:
        self.workflow.run.return_value = Mock(get_outputs=lambda: [])
        with self.assertRaisesRegex(RuntimeError, "no final text"):
            await self.agent.run(_message())

    async def test_streaming_updates_document_session_and_matches_response(self) -> None:
        session = AgentSession()
        stream = self.agent.run(_message(), session=session, stream=True)
        self.assertTrue(hasattr(stream, "__aiter__"))
        output = "".join([update.text async for update in stream])
        self.assertEqual(output, "Firmenanalyse")
        final = await stream.get_final_response()
        self.assertEqual(final.text, output)
        await self.agent.run("Folgefrage", session=session)
        self.assertEqual(self.ocr.extract_text.call_count, 1)
        self.assertEqual(self.builder.call_count, 2)

    async def test_host_can_finalize_stream_without_duplicate_workflow_execution(self) -> None:
        stream = self.agent.run("Synthetic request", stream=True)
        final = await stream.get_final_response()
        self.assertEqual(final.text, "Firmenanalyse")
        self.assertIs(await stream.get_final_response(), final)
        self.workflow.run.assert_awaited_once()

    async def test_stream_finalization_propagates_workflow_failure(self) -> None:
        self.workflow.run.side_effect = RuntimeError("Model failed")
        stream = self.agent.run("Synthetic request", stream=True)
        with self.assertRaisesRegex(RuntimeError, "Model failed"):
            await stream.get_final_response()

    async def test_invalid_input_is_not_silently_dropped(self) -> None:
        for messages in ({"role": "user"}, [object()]):
            with self.subTest(messages=messages), self.assertRaises(TypeError):
                await self.agent.run(messages)

    async def test_corrupt_document_state_is_an_explicit_error(self) -> None:
        session = AgentSession()
        session.state["document.documents.v1"] = [{"name": "a.pdf", "text": "data"}]
        with self.assertRaisesRegex(ValueError, "Invalid document session"):
            await self.agent.run("Folgefrage", session=session)

    async def test_legacy_thread_alias_and_conflicting_sessions(self) -> None:
        session = AgentSession()
        await self.agent.run(_message(), thread=session)
        self.assertEqual(len(self.agent._session_documents(session)), 1)
        with self.assertRaisesRegex(ValueError, "different sessions"):
            self.agent.run("test", session=session, thread=AgentSession())


class TestAssessmentWorkflow(unittest.IsolatedAsyncioTestCase):
    async def test_actual_graph_preserves_optional_step_routing(self) -> None:
        cases = (
            (False, False, "FINANCIAL_SUMMARY", ["analysis", "revision"], "revision"),
            (True, True, None, ["analysis", "revision"], "revision"),
            (True, False, "FINANCIAL_SUMMARY", ["analysis", "revision", "OK"], "revision"),
            (True, True, "FINANCIAL_SUMMARY", ["analysis", "revision", "OK"], "revision"),
            (True, True, "FINANCIAL_SUMMARY", ["analysis", "revision", "\u26a0\ufe0f", "corrected"], "corrected"),
        )
        for validate, correct, financial_summary, responses, expected in cases:
            with self.subTest(validate=validate, correct=correct, responses=responses):
                with patch("app.workflow._run_agent", new=AsyncMock(side_effect=responses)) as run:
                    workflow = build_assessment_workflow(Mock(), load_prompts())
                    result = await workflow.run(AssessmentState(
                        assembled_input="documents", financial_summary_context=financial_summary,
                        run_validation=validate, run_correction=correct,
                    ))
                    self.assertEqual(result.get_outputs(), [expected])
                    self.assertEqual(run.await_count, len(responses))

    async def test_concurrent_requests_use_separate_workflows_and_document_state(self) -> None:
        ocr = Mock()
        ocr.extract_text.side_effect = lambda data, **kwargs: data.decode("ascii")
        agent = DocumentAgent(chat_client=Mock(), ocr_provider=ocr)
        first_session, second_session = AgentSession(), AgentSession()

        async def respond(step, prompt: str) -> str:
            await asyncio.sleep(0)
            return f"{step.name}: {prompt}"

        with patch("app.workflow._run_agent", side_effect=respond):
            first, second = await asyncio.gather(
                agent.run(_message(b"%PDF-first", "first.pdf"), session=first_session),
                agent.run(_message(b"%PDF-second", "second.pdf"), session=second_session),
            )
        self.assertIn("%PDF-first", first.text)
        self.assertNotIn("%PDF-second", first.text)
        self.assertIn("%PDF-second", second.text)
        self.assertNotIn("%PDF-first", second.text)
        self.assertEqual(agent._session_documents(first_session)[0].name, "first.pdf")
        self.assertEqual(agent._session_documents(second_session)[0].name, "second.pdf")


if __name__ == "__main__":
    unittest.main()
