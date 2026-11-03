"""Offline tests for Content Understanding result and failure handling."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from azure.core.exceptions import HttpResponseError

from app.ocr_content_understanding import ContentUnderstandingOcrProvider


class TestContentUnderstandingProvider(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Mock()
        self.poller = self.client.begin_analyze.return_value
        self.poller.done.return_value = True
        self.provider = ContentUnderstandingOcrProvider(client=self.client)

    def test_bytes_are_sent_to_prebuilt_read(self) -> None:
        self.poller.result.return_value = SimpleNamespace(contents=[
            SimpleNamespace(markdown="  Umsatz: 100 EUR  ", path=None),
        ])
        text = self.provider.extract_text(b"%PDF-demo")
        self.assertIn("--- Page 1 ---", text)
        self.assertIn("Umsatz: 100 EUR", text)
        kwargs = self.client.begin_analyze.call_args.kwargs
        self.assertEqual(kwargs["analyzer_id"], "prebuilt-read")
        self.assertEqual(kwargs["inputs"][0].data, b"%PDF-demo")
        self.assertEqual(kwargs["inputs"][0].mime_type, "application/pdf")
        self.poller.result.assert_called_once_with(timeout=180)

    def test_empty_results_and_empty_markdown_are_errors(self) -> None:
        for contents in ([], [SimpleNamespace(markdown="  ", path=None, fields={})]):
            self.poller.result.return_value = SimpleNamespace(contents=contents)
            with self.subTest(contents=contents), self.assertRaisesRegex(ValueError, "no readable text"):
                self.provider.extract_text(b"%PDF-demo")

    def test_empty_input_is_rejected_before_service_call(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty"):
            self.provider.extract_text(b"")
        self.client.begin_analyze.assert_not_called()

    def test_service_errors_propagate(self) -> None:
        self.client.begin_analyze.side_effect = HttpResponseError("Access denied")
        with self.assertRaises(HttpResponseError):
            self.provider.extract_text(b"%PDF-demo")

    def test_polling_timeout_is_an_explicit_error(self) -> None:
        self.poller.done.return_value = False
        with self.assertRaisesRegex(TimeoutError, "180 seconds"):
            self.provider.extract_text(b"%PDF-demo")

    def test_injected_client_is_not_closed_by_provider(self) -> None:
        self.provider.close()
        self.client.close.assert_not_called()

    def test_owned_client_is_closed_but_shared_credential_is_not(self) -> None:
        credential = Mock()
        with patch("azure.ai.contentunderstanding.ContentUnderstandingClient") as client_type:
            provider = ContentUnderstandingOcrProvider(
                endpoint="https://example.invalid", credential=credential
            )
            provider._get_client()
            provider.close()
        client_type.return_value.close.assert_called_once()
        credential.close.assert_not_called()

    def test_owned_default_credential_is_closed(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with patch("azure.identity.DefaultAzureCredential") as credential_type:
                with patch("azure.ai.contentunderstanding.ContentUnderstandingClient"):
                    provider = ContentUnderstandingOcrProvider(endpoint="https://example.invalid")
                    provider._get_client()
                    provider.close()
        credential_type.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
