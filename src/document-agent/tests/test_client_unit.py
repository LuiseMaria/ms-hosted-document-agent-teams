"""Offline tests for conversation continuity in the sample client."""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from document_client import DocumentAgentClient


class TestDocumentClient(unittest.TestCase):
    def setUp(self) -> None:
        self.project = patch("document_client.AIProjectClient").start()
        self.addCleanup(patch.stopall)
        self.client = DocumentAgentClient(
            endpoint="https://example.services.ai.azure.com/api/projects/demo",
            credential=Mock(),
        )
        self.openai = self.project.return_value.get_openai_client.return_value

    def test_followup_reuses_previous_response_id(self) -> None:
        self.openai.responses.create.side_effect = [
            Mock(status="completed", id="resp-1", output_text="First"),
            Mock(status="completed", id="resp-2", output_text="Second"),
        ]
        self.assertEqual(self.client.analyze("First question"), "First")
        self.assertEqual(self.client.analyze("Followup"), "Second")
        calls = self.openai.responses.create.call_args_list
        self.assertIsNone(calls[0].kwargs["previous_response_id"])
        self.assertEqual(calls[1].kwargs["previous_response_id"], "resp-1")

    def test_failed_or_incomplete_response_does_not_advance_conversation(self) -> None:
        self.client._previous_response_id = "resp-ok"
        for status in ("failed", "incomplete", "in_progress"):
            self.openai.responses.create.return_value = Mock(status=status, id="resp-bad")
            with self.subTest(status=status), self.assertRaises(RuntimeError):
                self.client.analyze("Question")
            self.assertEqual(self.client._previous_response_id, "resp-ok")

    def test_close_clears_conversation_reference(self) -> None:
        self.client._previous_response_id = "resp-1"
        self.client.close()
        self.assertIsNone(self.client._previous_response_id)


if __name__ == "__main__":
    unittest.main()
