"""Shared OCR provider protocol and output formatting.

This module isolates OCR concerns behind a small :class:`OcrProvider` protocol so
callers depend on an abstraction rather than a concrete OCR engine. The entry
point uses Content Understanding; Document Intelligence is an alternative.

The output format keeps content labeled ``--- Page {n} ---`` so downstream text
handling stays provider-agnostic (identical to the original AWS implementation).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

_PAGE_SEPARATOR = "\n\n"
_PAGE_LINE_SEPARATOR = "\n"

def _page_label(page_number: int, page_text: str) -> str:
    """Build a page-labeled text block.

    Args:
        page_number: 1-based page number.
        page_text: Extracted text for the page.

    Returns:
        A labeled block, e.g. ``--- Page 1 ---<sep>text``.
    """
    return f"--- Page {page_number} ---{_PAGE_LINE_SEPARATOR}{page_text}"


@runtime_checkable
class OcrProvider(Protocol):
    """Protocol for OCR providers extracting text from document bytes."""

    def extract_text(self, document_bytes: bytes, language: str = "deu", content_type: str = "application/pdf") -> str:
        """Extract page-labeled text from a document.

        Args:
            document_bytes: Document (PDF/image) as bytes.
            language: Language hint (implementation-specific interpretation).
            content_type: MIME type of the document.

        Returns:
            Combined page-labeled OCR text string.
        """
        ...
