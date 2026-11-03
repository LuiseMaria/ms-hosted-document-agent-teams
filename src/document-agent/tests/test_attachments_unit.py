"""Offline regression tests for Responses API attachment resolution."""

import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from agent_framework import Content
from agent_framework.exceptions import ContentError
from agent_framework_foundry_hosting._responses import _convert_message_content
from openai import APIConnectionError, OpenAI
import httpx

from app.attachments import AttachmentResolver, MAX_ATTACHMENT_BYTES


class TestAttachmentResolver(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Mock()
        self.client.files.retrieve.return_value = Mock(bytes=9, filename="FINANCIAL_SUMMARY.pdf")
        self.response = Mock()
        self.response.iter_bytes.return_value = iter([b"%PDF-", b"demo"])
        self.client.files.with_streaming_response.content.return_value = nullcontext(self.response)
        self.resolver = AttachmentResolver(self.client)

    def test_inline_responses_file_preserves_bytes_type_and_filename(self) -> None:
        content = _convert_message_content({
            "type": "input_file", "filename": "FINANCIAL_SUMMARY.pdf",
            "file_data": "data:application/pdf;base64,JVBERi1kZW1v",
        })
        attachment = self.resolver.resolve(content, "fallback")
        self.assertEqual((attachment.name, attachment.data, attachment.media_type),
                         ("FINANCIAL_SUMMARY.pdf", b"%PDF-demo", "application/pdf"))
        self.client.files.retrieve.assert_not_called()

    def test_framework_raw_bytes_use_the_same_path(self) -> None:
        attachment = self.resolver.resolve(
            Content.from_data(b"%PDF-demo", "application/pdf"), "fallback"
        )
        self.assertEqual(attachment.data, b"%PDF-demo")

    def test_file_id_downloads_from_project_and_uses_metadata_name(self) -> None:
        content = _convert_message_content({"type": "input_file", "file_id": "file-demo"})
        attachment = self.resolver.resolve(content, "fallback")
        self.assertEqual(attachment.name, "FINANCIAL_SUMMARY.pdf")
        self.assertEqual(attachment.data, b"%PDF-demo")
        self.assertEqual(attachment.media_type, "application/pdf")
        self.client.files.retrieve.assert_called_once_with("file-demo", timeout=60)
        self.response.iter_bytes.assert_called_once_with(chunk_size=64 * 1024)

    def test_file_id_preserves_request_filename(self) -> None:
        content = Content.from_hosted_file("file-demo", name="ASSESSMENT_GUIDELINE.pdf")
        self.assertEqual(self.resolver.resolve(content, "fallback").name, "ASSESSMENT_GUIDELINE.pdf")

    def test_missing_file_client_is_an_explicit_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "not configured"):
            AttachmentResolver().resolve(Content.from_hosted_file("file-demo"), "fallback")

    def test_external_url_is_rejected_without_a_network_request(self) -> None:
        content = _convert_message_content({
            "type": "input_file", "file_url": "https://example.invalid/private.pdf",
        })
        with self.assertRaisesRegex(ValueError, "not enabled"):
            self.resolver.resolve(content, "fallback")
        self.client.files.retrieve.assert_not_called()
        self.client.files.with_streaming_response.content.assert_not_called()

    def test_invalid_inline_payloads_are_not_silently_ignored(self) -> None:
        for uri in (
            "data:application/pdf;base64,!!!",
            "data:application/pdf,not-base64",
            "data:application/pdf;base64,",
        ):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                self.resolver.resolve(Content.from_uri(uri), "demo.pdf")

    def test_data_uri_without_separator_is_rejected_by_hosting_sdk(self) -> None:
        with self.assertRaises(ContentError):
            _convert_message_content({
                "type": "input_file", "file_data": "data:application/pdf;base64",
            })

    def test_unsupported_type_is_an_explicit_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            self.resolver.resolve(Content.from_data(b"zip", "application/zip"), "demo.zip")

    def test_inline_size_limit_includes_exact_boundary(self) -> None:
        with patch("app.attachments.MAX_ATTACHMENT_BYTES", 3):
            self.assertEqual(
                self.resolver.resolve(Content.from_data(b"123", "application/pdf"), "a.pdf").data,
                b"123",
            )
            with self.assertRaisesRegex(ValueError, "limit"):
                self.resolver.resolve(Content.from_data(b"1234", "application/pdf"), "a.pdf")

    def test_metadata_size_rejected_before_downloading(self) -> None:
        self.client.files.retrieve.return_value.bytes = MAX_ATTACHMENT_BYTES + 1
        with self.assertRaisesRegex(ValueError, "limit"):
            self.resolver.resolve(Content.from_hosted_file("file-demo"), "fallback")
        self.client.files.with_streaming_response.content.assert_not_called()

    def test_stream_size_is_checked_even_when_metadata_is_wrong(self) -> None:
        self.response.iter_bytes.return_value = iter([b"123", b"4"])
        self.client.files.retrieve.return_value.bytes = 1
        with patch("app.attachments.MAX_ATTACHMENT_BYTES", 3):
            with self.assertRaisesRegex(ValueError, "limit"):
                self.resolver.resolve(Content.from_hosted_file("file-demo"), "fallback")

    def test_download_failure_is_not_reported_as_missing_attachment(self) -> None:
        self.client.files.retrieve.side_effect = APIConnectionError(
            request=httpx.Request("GET", "https://example.invalid/files/file-demo")
        )
        with self.assertRaisesRegex(RuntimeError, "Could not load"):
            self.resolver.resolve(Content.from_hosted_file("file-demo"), "fallback")

    def test_real_files_sdk_downloads_metadata_and_binary_content(self) -> None:
        paths: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path.endswith("/content"):
                return httpx.Response(200, content=b"%PDF-demo")
            return httpx.Response(200, json={
                "id": "file-demo", "object": "file", "bytes": 9, "created_at": 1,
                "filename": "FINANCIAL_SUMMARY.pdf", "purpose": "assistants", "status": "processed",
            })

        with OpenAI(
            api_key="offline-test", base_url="https://example.invalid/v1",
            http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        ) as client:
            attachment = AttachmentResolver(client).resolve(
                Content.from_hosted_file("file-demo"), "fallback"
            )
        self.assertEqual(attachment.data, b"%PDF-demo")
        self.assertEqual(attachment.name, "FINANCIAL_SUMMARY.pdf")
        self.assertEqual(paths, ["/v1/files/file-demo", "/v1/files/file-demo/content"])


if __name__ == "__main__":
    unittest.main()
