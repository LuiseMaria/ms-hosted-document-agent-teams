"""Live HTTP integration test for the locally running Document Agent hosted agent.

This test does **not** start the server itself. Start it first in a separate
terminal:

    azd ai agent run
    # or directly:
    python main.py

It then sends a real HTTP request to the OpenAI-Responses-compatible endpoint
(``POST /responses``) exposed by :class:`agent_framework_foundry_hosting.ResponsesHostServer`,
attaching the two sample PDFs from ``tests/data`` exactly like a user would
attach files in the Foundry chat UI.

If no server is reachable on ``AGENT_BASE_URL`` (default
``http://localhost:8088``), the test is skipped with a hint on how to start
one, instead of failing.
"""

from __future__ import annotations

import base64
import os
import sys
import unittest
from pathlib import Path
from typing import Any

# Ensure the ``app`` package is importable even when unittest is started
# from a different working directory.
_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

try:  # pragma: no cover - convenience only for local runs
    from dotenv import load_dotenv

    load_dotenv(_AGENT_ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

_DATA_DIR = Path(__file__).resolve().parent / "data"

# Attachments to send with every request, mirroring a real chat upload:
# a FINANCIAL_SUMMARY excerpt (leading data source) and the annual financial statement.
_ATTACHMENT_NAMES = (
    "FINANCIAL_SUMMARY - Beispielholding AG-31.12.2024.pdf",
    "Konzernabschluss Beispielholding AG.pdf",
)

# Generous timeout: the request triggers OCR + a multi-step LLM workflow
# (analysis -> revision -> optional validation/correction).
_REQUEST_TIMEOUT_SECONDS = 600.0


def _base_url() -> str:
    """Resolve the base URL of the locally running hosted agent."""
    return os.getenv("AGENT_BASE_URL", "http://localhost:8088").rstrip("/")


def _server_is_reachable(base_url: str) -> bool:
    """Best-effort check whether a server is listening on ``base_url``."""
    if httpx is None:
        return False
    try:
        # Any response (even 404/405) proves a server is listening; only a
        # connection error means "nothing running on that port".
        httpx.get(f"{base_url}/", timeout=3.0)
        return True
    except httpx.HTTPError:
        return False


def _encode_attachment(path: Path) -> dict[str, Any]:
    """Build a Responses-API ``input_file`` content item from a local PDF.

    Args:
        path: Path to the PDF file to attach.

    Returns:
        A dict matching the ``input_file`` content schema (base64 data URI).
    """
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "input_file",
        "filename": path.name,
        "file_data": f"data:application/pdf;base64,{encoded}",
    }


class TestDocumentAgentHttpServer(unittest.TestCase):
    """Live integration test calling the hosted agent over HTTP (Responses API)."""

    def test_analyze_documents_via_responses_endpoint(self) -> None:
        """Send the sample PDFs to ``POST /responses`` and check the reply.

        Requires the agent to be running locally, e.g. via ``azd ai agent run``
        (or ``python main.py``) in another terminal.
        """
        base_url = _base_url()

        if httpx is None:
            self.skipTest("httpx is not installed; run 'pip install httpx' to use this test.")

        if not _server_is_reachable(base_url):
            self.skipTest(
                f"No agent server reachable at {base_url}. Start it first, e.g. with "
                "'azd ai agent run' (or 'python main.py') in another terminal."
            )

        missing = [name for name in _ATTACHMENT_NAMES if not (_DATA_DIR / name).is_file()]
        if missing:
            self.skipTest(f"Missing sample attachment(s) in {_DATA_DIR}: {missing}")

        content: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": "Bitte erstelle eine Firmenanalyse auf Basis der angehängten Dokumente.",
            }
        ]
        content.extend(_encode_attachment(_DATA_DIR / name) for name in _ATTACHMENT_NAMES)

        payload: dict[str, Any] = {
            "input": [{"role": "user", "content": content}],
            "stream": False,
        }
        model = os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME")
        if model:
            payload["model"] = model

        response = httpx.post(
            f"{base_url}/responses",
            json=payload,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )

        #print(response.text)

        self.assertEqual(
            response.status_code,
            200,
            f"Unexpected status {response.status_code}: {response.text[:2000]}",
        )

        output_list = response.json().get("output", [])
        for output in output_list:
            status = output.get("status")
            self.assertEqual(status, "completed", f"Unexpected status: {status}")
            content = output.get("content", [])
            for content_item in content:
                if content_item.get("type") == "output_text":
                    output_text = content_item.get("text", "")
                    print(output_text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
