from __future__ import annotations

import logging
import os
from typing import Any

from app.ocr import _PAGE_LINE_SEPARATOR, _page_label, _PAGE_SEPARATOR

logger = logging.getLogger(__name__)

class DocumentIntelligenceOcrProvider:
    """OCR provider backed by Azure AI Document Intelligence (``prebuilt-read``).

    Authentication resolution order:

    1. An injected ``client`` (useful for testing / custom configuration).
    2. An API key (``api_key`` argument or ``AZURE_DOCUMENT_INTELLIGENCE_KEY`` /
       ``DOCUMENTINTELLIGENCE_API_KEY``).
    3. ``DefaultAzureCredential`` (Managed Identity), used when no key is set.
    """

    _LANGUAGE_TO_LOCALE = {
        "deu": "de",
        "de": "de",
        "eng": "en",
        "en": "en",
        "fra": "fr",
        "fr": "fr",
    }

    def __init__(
        self,
        client: Any | None = None,
        endpoint: str | None = None,
        api_key: str | None = None,
        model_id: str | None = None,
    ) -> None:
        """Initialize the provider.

        Args:
            logger: Logger instance used for structured runtime logs.
            client: Optional pre-built ``DocumentIntelligenceClient``. When
                omitted, a client is created lazily on first use.
            endpoint: Document Intelligence endpoint. Falls back to
                ``AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT`` /
                ``DOCUMENTINTELLIGENCE_ENDPOINT``.
            api_key: Optional API key. Falls back to
                ``AZURE_DOCUMENT_INTELLIGENCE_KEY`` /
                ``DOCUMENTINTELLIGENCE_API_KEY``.
            model_id: Model to use. Falls back to
                ``AZURE_DOCUMENT_INTELLIGENCE_MODEL`` or ``prebuilt-read``.
        """
        self._client = client
        self._endpoint = (
            endpoint
            or os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
            or os.getenv("DOCUMENTINTELLIGENCE_ENDPOINT")
        )
        self._api_key = (
            api_key
            or os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY")
            or os.getenv("DOCUMENTINTELLIGENCE_API_KEY")
        )
        self._model_id = model_id or os.getenv("AZURE_DOCUMENT_INTELLIGENCE_MODEL", "prebuilt-read")

    @classmethod
    def _language_to_locale(cls, language: str) -> str | None:
        """Map a language hint to a Document Intelligence BCP-47 locale.

        Args:
            language: Tesseract code or locale string (e.g. ``deu``, ``de``).

        Returns:
            A locale hint (e.g. ``de``) or None when it cannot be mapped.
        """
        return cls._LANGUAGE_TO_LOCALE.get((language or "").strip().lower())

    def _get_client(self) -> Any:
        """Create (and cache) the Document Intelligence client.

        Returns:
            A configured ``DocumentIntelligenceClient`` instance.

        Raises:
            ValueError: If no endpoint is configured.
        """
        if self._client is not None:
            return self._client

        if not self._endpoint:
            raise ValueError(
                "Missing Document Intelligence endpoint. Set AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT."
            )

        from azure.ai.documentintelligence import DocumentIntelligenceClient

        if self._api_key:
            from azure.core.credentials import AzureKeyCredential

            credential: Any = AzureKeyCredential(self._api_key)
            logger.info(
                "Creating Document Intelligence client",
                extra={"auth": "api_key", "endpoint": self._endpoint},
            )
        else:
            from azure.identity import DefaultAzureCredential

            credential = DefaultAzureCredential()
            logger.info(
                "Creating Document Intelligence client",
                extra={"auth": "managed_identity", "endpoint": self._endpoint},
            )

        self._client = DocumentIntelligenceClient(endpoint=self._endpoint, credential=credential)
        return self._client

    def extract_text(self, document_bytes: bytes, language: str = "deu", content_type: str = "application/pdf") -> str:
        """Extract text from a document using Azure AI Document Intelligence.

        Args:
            document_bytes: Document (PDF/image) as bytes.
            language: Language hint mapped to the Document Intelligence
                ``locale`` when possible.
            content_type: MIME type of the document (e.g. ``application/pdf``).

        Returns:
            Combined page-labeled OCR text string.
        """
        locale = self._language_to_locale(language)
        logger.info(
            "Starting OCR extraction",
            extra={
                "provider": "azure-document-intelligence",
                "model_id": self._model_id,
                "locale": locale,
                "content_type": content_type,
                "document_size_bytes": len(document_bytes),
            },
        )

        client = self._get_client()
        analyze_kwargs: dict[str, Any] = {"body": document_bytes, "content_type": content_type}
        if locale:
            analyze_kwargs["locale"] = locale

        poller = client.begin_analyze_document(self._model_id, **analyze_kwargs)
        result = poller.result()

        pages: list[str] = []
        for page in getattr(result, "pages", None) or []:
            page_number = getattr(page, "page_number", len(pages) + 1)
            lines = getattr(page, "lines", None) or []
            page_text = _PAGE_LINE_SEPARATOR.join(line.content for line in lines)
            logger.info(
                "OCR page extracted",
                extra={
                    "provider": "azure-document-intelligence",
                    "page": page_number,
                    "line_count": len(lines),
                    "text_length": len(page_text),
                },
            )
            pages.append(_page_label(page_number, page_text))

        if not pages and getattr(result, "content", None):
            pages.append(_page_label(1, result.content))

        logger.info(
            "OCR extraction completed",
            extra={
                "provider": "azure-document-intelligence",
                "model_id": self._model_id,
                "page_count": len(pages),
                "result_length": sum(len(page) for page in pages),
            },
        )
        return _PAGE_SEPARATOR.join(pages)


DOCUMENT_INTELLIGENCE_AZURE_ALIASES = {
    "azure-document-intelligence",
    "azure",
    "document-intelligence",
    "documentintelligence",
}
