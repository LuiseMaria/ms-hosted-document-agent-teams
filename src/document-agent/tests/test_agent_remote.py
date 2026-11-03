"""Live integration test for the Document Agent agent deployed to Microsoft Foundry.

Unlike ``test_agent_call.py`` (which calls a locally running server via plain
HTTP), this test calls the **remote/deployed** agent through
:class:`document_client.DocumentAgentClient`, which authenticates with Azure AD
(``DefaultAzureCredential``) instead of a raw HTTP request. A plain
``httpx.post`` against the Foundry agent URL returns ``401`` because the
endpoint requires a bearer token, not an API-Management subscription key.

Configure via environment variables (e.g. in ``.env``):

    FOUNDRY_PROJECT_ENDPOINT=https://<resource>.services.ai.azure.com/api/projects/<project>
    DOCUMENT_AGENT_AGENT_NAME=document-agent        # optional, defaults to "document-agent"
    DOCUMENT_AGENT_AGENT_VERSION=7                # optional, defaults to latest

Authenticate locally first with ``az login`` (DefaultAzureCredential picks up
the Azure CLI session).
"""

from __future__ import annotations

import logging
import os
import sys
import unittest
from pathlib import Path

# Show INFO-level logs (e.g. the agent_session_id / response_id logged by
# document_client.py) on stdout when running this test directly or via
# unittest/pytest with -s / --log-cli-level.
logging.basicConfig(level=logging.INFO)

# Ensure both ``app`` and the sibling ``document_client`` module are importable
# regardless of the working directory unittest is started from.
_TESTS_DIR = Path(__file__).resolve().parent
_AGENT_ROOT = _TESTS_DIR.parent
for path in (str(_AGENT_ROOT), str(_TESTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

try:  # pragma: no cover - convenience only for local runs
    from dotenv import load_dotenv

    load_dotenv(_AGENT_ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

from azure.core.exceptions import ClientAuthenticationError  # noqa: E402

from document_client import DocumentAgentClient  # noqa: E402

_DATA_DIR = _TESTS_DIR / "data"

# Attachments to send with every request, mirroring a real chat upload:
# a FINANCIAL_SUMMARY excerpt (leading data source) and the annual financial statement.
_ATTACHMENT_NAMES = (
    "FINANCIAL_SUMMARY - Beispielholding AG-31.12.2024.pdf",
    "Konzernabschluss Beispielholding AG.pdf",
)


class TestDocumentAgentRemote(unittest.TestCase):
    """Live integration test calling the deployed Foundry Document Agent agent."""

    def test_analyze_documents_via_foundry_agent(self) -> None:
        """Send the sample PDFs to the deployed agent and check the reply.

        Requires ``FOUNDRY_PROJECT_ENDPOINT`` to be set and a valid Azure
        credential (e.g. ``az login``) with access to the Foundry project.
        """
        endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT") or os.getenv("AZURE_AI_PROJECT_ENDPOINT")
        if not endpoint:
            self.skipTest(
                "No Foundry project endpoint configured "
                "(FOUNDRY_PROJECT_ENDPOINT or AZURE_AI_PROJECT_ENDPOINT)."
            )

        missing = [name for name in _ATTACHMENT_NAMES if not (_DATA_DIR / name).is_file()]
        if missing:
            self.skipTest(f"Missing sample attachment(s) in {_DATA_DIR}: {missing}")

        attachments = [_DATA_DIR / name for name in _ATTACHMENT_NAMES]

        try:
            with DocumentAgentClient(endpoint=endpoint) as client:
                output_text = client.analyze(
                    "Bitte erstelle eine Firmenanalyse auf Basis der angehängten Dokumente.",
                    attachments=attachments,
                )
        except ClientAuthenticationError as exc:
            self.skipTest(f"Azure authentication failed - run 'az login' first. Details: {exc}")

        print(f"\n--- Firmenanalyse (agent={os.getenv('DOCUMENT_AGENT_AGENT_NAME', 'document-agent')}) ---")
        print(output_text[:2000])
        print(f"\n[Total length: {len(output_text)} characters]")

        self.assertTrue(output_text.strip(), "Empty output_text in response.")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
