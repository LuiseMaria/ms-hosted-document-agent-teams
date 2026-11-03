"""Integration test for Azure AI Document Intelligence (OCR).

This test performs a real OCR run against your deployed
Document Intelligence endpoint and serves as a quick smoke test
to verify endpoint connectivity, authentication, and ``prebuilt-read``.

Prerequisites
-------------
1. Configure the endpoint (one of the following options):

   - Set an environment variable::

         export AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT="https://example-ocr.cognitiveservices.azure.com/"

   - or use a local ``.env`` file (see ``.env.example``).

2. Configure authentication (one of the following options):

   - Managed Identity / local Azure CLI login::

         az login

     Your user account or managed identity must have the
     "Cognitive Services User" role assigned on the resource.

   - or set an API key::

         export AZURE_DOCUMENT_INTELLIGENCE_KEY="<key>"

Run
---
From the ``src/document-agent`` directory (so that ``app`` is importable)::

    python -m unittest tests.test_docintelligence_ocr -v

If no endpoint is configured, the test is skipped rather than failed,
so it stays green in CI environments without Azure access.
"""

from __future__ import annotations

import logging
import os
import sys
import unittest
from pathlib import Path

# Ensure the ``app`` package is importable even when unittest is started
# from a different working directory.
_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from app.ocr_document_intelligence import DocumentIntelligenceOcrProvider

# Optional: load .env if python-dotenv is installed (useful for local runs).
try:  # pragma: no cover - convenience only for local runs
    from dotenv import load_dotenv

    load_dotenv(_AGENT_ROOT / ".env")
except ImportError:  # pragma: no cover
    load_dotenv = None


_DATA_DIR = Path(__file__).resolve().parent / "data"
_DEFAULT_ENDPOINT = "https://example-ocr.cognitiveservices.azure.com/"


def _resolve_endpoint() -> str | None:
    """Read the endpoint from the environment, with a sensible default."""
    return (
        os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
        or os.getenv("DOCUMENTINTELLIGENCE_ENDPOINT")
        or _DEFAULT_ENDPOINT
    )


def _first_test_pdf() -> Path | None:
    """Pick a PDF from the ``tests/data`` directory as sample input."""
    if not _DATA_DIR.is_dir():
        return None
    pdfs = sorted(_DATA_DIR.glob("*.pdf"))
    return pdfs[0] if pdfs else None


class TestDocumentIntelligenceOcr(unittest.TestCase):
    """Live integration test for the OCR provider using Azure DI."""

    def test_document_intelligence_ocr(self) -> None:
        """Run a real OCR request against the DI endpoint.

        Verifies that text including page labels (``--- Page N ---``) is returned.
        """
        endpoint = _resolve_endpoint()
        if not endpoint:
            self.skipTest(
                "No Document Intelligence endpoint configured "
                "(AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT)."
            )

        pdf_path = _first_test_pdf()
        if pdf_path is None:
            self.skipTest(f"No sample PDF found in {_DATA_DIR}.")

        logging.basicConfig(level=logging.INFO)

        provider = DocumentIntelligenceOcrProvider(
            endpoint=endpoint,
            # api_key is read automatically from AZURE_DOCUMENT_INTELLIGENCE_KEY;
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
    # Allows a quick manual run: ``python tests/test_docintelligence_ocr.py``
    unittest.main(verbosity=2)
