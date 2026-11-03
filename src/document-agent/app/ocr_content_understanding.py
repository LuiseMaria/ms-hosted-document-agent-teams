"""OCR provider backed by Azure AI Content Understanding.

This module mirrors ``app.ocr_document_intelligence`` but uses Azure AI Content
Understanding for OCR-style text extraction. The provider accepts document
bytes, sends them to a prebuilt analyzer, and returns page-labeled text so the
downstream analysis pipeline can remain provider-agnostic.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from azure.core.credentials import TokenCredential
from azure.core.exceptions import AzureError

from app.ocr import _PAGE_LINE_SEPARATOR, _PAGE_SEPARATOR, _page_label

logger = logging.getLogger(__name__)

class ContentUnderstandingOcrProvider:
    """OCR provider backed by Azure AI Content Understanding.

    Authentication resolution order:

    1. An injected ``client`` (useful for tests or custom configuration).
    2. An API key (``api_key`` argument or environment variables).
    3. ``DefaultAzureCredential`` (Managed Identity / az login).
    """

    def __init__(
        self,
        client: Any | None = None,
        endpoint: str | None = None,
        api_key: str | None = None,
        analyzer_id: str | None = None,
        api_version: str | None = None,
        credential: TokenCredential | None = None,
    ) -> None:
        self._client = client
        self._endpoint = (
            endpoint
            or os.getenv("AZURE_CONTENT_UNDERSTANDING_ENDPOINT")
            or os.getenv("CONTENTUNDERSTANDING_ENDPOINT")
        )
        self._api_key = (
            api_key
            or os.getenv("AZURE_CONTENT_UNDERSTANDING_KEY")
            or os.getenv("CONTENTUNDERSTANDING_KEY")
            or os.getenv("CONTENT_UNDERSTANDING_KEY")
        )
        self._analyzer_id = analyzer_id or os.getenv("AZURE_CONTENT_UNDERSTANDING_ANALYZER", "prebuilt-read")
        self._api_version = api_version or os.getenv("AZURE_CONTENT_UNDERSTANDING_API_VERSION")
        self._credential = credential
        self._owns_credential = False
        self._owns_client = client is None
        if client is None and not self._endpoint:
            logger.error("Content Understanding endpoint is not configured.")
            raise ValueError(
                "Missing Content Understanding endpoint. Set AZURE_CONTENT_UNDERSTANDING_ENDPOINT."
            )

    def _get_client(self) -> Any:
        """Create and cache the Content Understanding client."""
        if self._client is not None:
            return self._client

        if not self._endpoint:
            raise ValueError(
                "Missing Content Understanding endpoint. Set AZURE_CONTENT_UNDERSTANDING_ENDPOINT."
            )

        from azure.ai.contentunderstanding import ContentUnderstandingClient

        if self._api_key:
            from azure.core.credentials import AzureKeyCredential

            credential: Any = AzureKeyCredential(self._api_key)
            logger.info(
                "Creating Content Understanding client",
                extra={"auth": "api_key", "endpoint": self._endpoint},
            )
        elif self._credential is not None:
            credential = self._credential
        else:
            from azure.identity import DefaultAzureCredential

            self._credential = DefaultAzureCredential()
            self._owns_credential = True
            credential = self._credential
            logger.info(
                "Creating Content Understanding client",
                extra={"auth": "managed_identity", "endpoint": self._endpoint},
            )

        client_kwargs: dict[str, Any] = {}
        if self._api_version:
            client_kwargs["api_version"] = self._api_version

        self._client = ContentUnderstandingClient(
            endpoint=self._endpoint,
            credential=credential,
            **client_kwargs,
        )
        return self._client

    def close(self) -> None:
        """Close only resources owned by this provider."""
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None
        if self._owns_credential and self._credential is not None:
            self._credential.close()
            self._credential = None
            self._owns_credential = False

    @staticmethod
    def _page_number_from_content(content: Any, fallback_page_number: int) -> int:
        """Derive a stable page number for output formatting."""
        path = getattr(content, "path", None) or ""
        if isinstance(path, str):
            match = re.search(r"(\d+)(?!.*\d)", path)
            if match:
                return int(match.group(1))
        return fallback_page_number

    @staticmethod
    def _content_to_text(content: Any) -> str:
        """Extract a readable text block from a Content Understanding result."""
        markdown = getattr(content, "markdown", None)
        if markdown:
            return markdown.strip()

        fields = getattr(content, "fields", None) or {}
        if fields:
            extracted: list[str] = []
            for field_name, field_value in fields.items():
                value = getattr(field_value, "value", None)
                if value is None:
                    value = getattr(field_value, "value_string", None)
                if value is None:
                    value = getattr(field_value, "valueString", None)
                if value is None:
                    value = str(field_value)
                extracted.append(f"{field_name}: {value}")
            return _PAGE_LINE_SEPARATOR.join(extracted).strip()

        return ""

    def extract_text(self, document_bytes: bytes, language: str = "deu", content_type: str = "application/pdf") -> str:
        """Extract page-labeled OCR text from document bytes.

        The ``language`` hint is accepted for protocol compatibility but is not
        currently used by Content Understanding.
        """
        del language
        if not document_bytes:
            logger.error("Cannot run OCR on empty input.")
            raise ValueError("Cannot run OCR on an empty document.")

        logger.info(
            "Starting OCR extraction",
            extra={
                "provider": "azure-content-understanding",
                "analyzer_id": self._analyzer_id,
                "content_type": content_type,
                "document_size_bytes": len(document_bytes),
            },
        )

        from azure.ai.contentunderstanding.models import AnalysisInput

        client = self._get_client()
        try:
            poller = client.begin_analyze(
                analyzer_id=self._analyzer_id,
                inputs=[AnalysisInput(data=document_bytes, mime_type=content_type)],
                connection_timeout=10,
                read_timeout=60,
            )
            result = poller.result(timeout=180)
            if not poller.done():
                logger.error("Content Understanding exceeded the 180 second polling wait.")
                raise TimeoutError("Content Understanding did not finish within 180 seconds.")
        except AzureError:
            logger.error("Content Understanding analysis failed or timed out.")
            raise

        pages: list[str] = []
        for index, content in enumerate(getattr(result, "contents", None) or [], start=1):
            page_number = self._page_number_from_content(content, index)
            page_text = self._content_to_text(content)
            if not page_text:
                logger.warning("Content Understanding returned a content block without readable text.")
                continue
            logger.info(
                "OCR content extracted",
                extra={
                    "provider": "azure-content-understanding",
                    "page": page_number,
                    "text_length": len(page_text),
                    "content_kind": getattr(content, "kind", None),
                },
            )
            pages.append(_page_label(page_number, page_text))

        if not pages:
            logger.error("Content Understanding returned no readable text.")
            raise ValueError("Content Understanding returned no readable text for the document.")

        logger.info(
            "OCR extraction completed",
            extra={
                "provider": "azure-content-understanding",
                "analyzer_id": self._analyzer_id,
                "page_count": len(pages),
                "result_length": sum(len(page) for page in pages),
            },
        )
        return _PAGE_SEPARATOR.join(pages)


CONTENT_UNDERSTANDING_AZURE_ALIASES = {
    "azure-content-understanding",
    "azure",
    "content-understanding",
    "contentunderstanding",
}
