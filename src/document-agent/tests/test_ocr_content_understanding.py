
from __future__ import annotations
from __future__ import annotations

import logging
import os
import sys
import unittest
from pathlib import Path

from app.ocr_content_understanding import ContentUnderstandingOcrProvider

# Ensure the ``app`` package is importable even when unittest is started
# from a different working directory.
_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))


# Optional: load .env if python-dotenv is installed (useful for local runs).
try:  # pragma: no cover - convenience only for local runs
    from dotenv import load_dotenv

    load_dotenv(_AGENT_ROOT / ".env")
except ImportError:  # pragma: no cover
    load_dotenv = None


_DATA_DIR = Path(__file__).resolve().parent / "data"


def _resolve_endpoint() -> str | None:
    """Read the endpoint from the environment"""
    return os.getenv("AZURE_CONTENT_UNDERSTANDING_ENDPOINT")


def _first_test_pdf() -> Path | None:
    """Pick a PDF from the ``tests/data`` directory as sample input."""
    if not _DATA_DIR.is_dir():
        return None
    pdfs = sorted(_DATA_DIR.glob("*.pdf"))
    return pdfs[0] if pdfs else None


class TestContentUnderstandingOcr(unittest.TestCase):
    """Live integration test for the OCR provider using Azure Content Understanding service."""

    def test_content_understanding_ocr(self) -> None:
        """Run a real OCR request against the Content Understanding endpoint.

        Verifies that text including page labels (``--- Page N ---``) is returned.
        """
        endpoint = _resolve_endpoint()
        if not endpoint:
            self.skipTest(
                "No Content Understanding endpoint configured "
                "(AZURE_CONTENT_UNDERSTANDING_ENDPOINT)."
            )

        pdf_path = _first_test_pdf()
        if pdf_path is None:
            self.skipTest(f"No sample PDF found in {_DATA_DIR}.")

        logging.basicConfig(level=logging.INFO)

        provider = ContentUnderstandingOcrProvider(
            endpoint=endpoint,
            # api_key is read automatically from environment variables (AZURE_DOCUMENT_INTELLIGENCE_KEY or DOCUMENTINTELLIGENCE_KEY);
            # without a key, DefaultAzureCredential (az login / Managed Identity) is used.
        )

        document_bytes = pdf_path.read_bytes()
        text = provider.extract_text(
            document_bytes,
            language="deu",
            content_type="application/pdf",
        )

        # Visible during a direct test run; useful for quick manual verification.
        print(f"\n--- OCR result for '{pdf_path.name}' ---")
        print(text[:2000])
        print(f"\n[Total length: {len(text)} characters]")

        self.assertTrue(text.strip(), "OCR returned an empty text result.")
        self.assertIn(
            "--- Page 1 ---",
            text,
            "Expected page label is missing from the OCR output.",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
