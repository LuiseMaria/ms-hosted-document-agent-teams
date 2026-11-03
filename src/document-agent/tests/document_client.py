"""Real client for a Foundry-hosted Document Agent agent (OpenAI Responses API).

Wraps :class:`azure.ai.projects.AIProjectClient` to obtain an authenticated
OpenAI client bound directly to the deployed agent's Responses endpoint.

Why this is needed instead of a plain ``httpx.post(<remote-url>/responses)``:
the Foundry agent endpoint requires an **Azure AD bearer token**, not an
API-Management subscription key. A raw HTTP call without an ``Authorization``
header fails with::

    401 - Access denied due to invalid subscription key or wrong API endpoint.

``AIProjectClient.get_openai_client(agent_name=...)`` handles this
automatically via ``DefaultAzureCredential`` (``az login`` locally, Managed
Identity when deployed), returning a standard ``openai.OpenAI`` client whose
``base_url`` already points at
``{endpoint}/agents/{agent_name}/endpoint/protocols/openai``.

Usage::

    from document_client import DocumentAgentClient

    with DocumentAgentClient(endpoint=..., agent_name="document-agent") as client:
        text = client.analyze("Bitte erstelle eine Firmenanalyse.", attachments=[pdf_path])
        print(text)
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Any, Sequence

from azure.ai.projects import AIProjectClient
from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential

logger = logging.getLogger(__name__)


def _encode_attachment(path: Path) -> dict[str, Any]:
    """Build a Responses-API ``input_file`` content item from a local file.

    Args:
        path: Path to the file to attach (e.g. a PDF).

    Returns:
        A dict matching the ``input_file`` content schema (base64 data URI).
    """
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "input_file",
        "filename": path.name,
        "file_data": f"data:{mime_type};base64,{encoded}",
    }


class DocumentAgentClient:
    """Authenticated client for a Foundry-hosted Document Agent agent.

    Attributes:
        endpoint: Foundry project endpoint, e.g.
            ``https://<resource>.services.ai.azure.com/api/projects/<project>``.
        agent_name: Name of the deployed agent (e.g. ``"document-agent"``).
        agent_version: Optional pinned agent version. When omitted, the
            agent's default/latest version is used.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        agent_name: str | None = None,
        agent_version: str | None = None,
        isolation_key: str | None = None,
        credential: TokenCredential | None = None,
        use_session: bool = False,
    ) -> None:
        """Initialize the client.

        Args:
            endpoint: Foundry project endpoint. Defaults to the
                ``FOUNDRY_PROJECT_ENDPOINT`` environment variable.
            agent_name: Deployed agent name. Defaults to the
                ``DOCUMENT_AGENT_AGENT_NAME`` environment variable (or ``"document-agent"``).
            agent_version: Optional agent version to pin. Defaults to the
                ``DOCUMENT_AGENT_AGENT_VERSION`` environment variable, if set.
            isolation_key: Session isolation key (only used when
                ``use_session=True``). Defaults to a random UUID.
            credential: Azure credential. Defaults to ``DefaultAzureCredential()``
                (``az login`` locally, Managed Identity when deployed).
            use_session: Whether to create a Foundry Agents execution session.
                Responses continuity uses previous_response_id independently.
                Requires a preview ``azure-ai-projects`` version that exposes
                ``beta.agents.create_session`` / ``delete_session``. Off by
                default; document memory uses the Responses conversation.

        Raises:
            KeyError: If ``endpoint`` is not provided and
                ``FOUNDRY_PROJECT_ENDPOINT`` is not set.
        """
        self.endpoint = endpoint or os.environ["FOUNDRY_PROJECT_ENDPOINT"]
        self.agent_name = agent_name or os.getenv("DOCUMENT_AGENT_AGENT_NAME", "document-agent")
        self.agent_version = agent_version or os.getenv("DOCUMENT_AGENT_AGENT_VERSION", "latest")
        self._isolation_key = isolation_key or os.getenv("DOCUMENT_AGENT_ISOLATION_KEY") or str(uuid.uuid4())
        self._use_session = use_session

        self._credential: TokenCredential = credential or DefaultAzureCredential()
        self._project_client = AIProjectClient(
            endpoint=self.endpoint,
            credential=self._credential,
            allow_preview=True,
        )
        self._openai_client: Any = None
        self._session: Any = None
        self._previous_response_id: str | None = None

    def __enter__(self) -> "DocumentAgentClient":
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def open(self) -> None:
        """Lazily create the OpenAI client (and optional session).

        Called automatically by :meth:`analyze`; only call this directly if
        you need the underlying clients before the first request.

        Raises:
            RuntimeError: If ``use_session=True`` but the installed
                ``azure-ai-projects`` version does not support Agent sessions.
        """
        if self._openai_client is not None:
            return
        self._openai_client = self._project_client.get_openai_client(agent_name=self.agent_name)
        if self._use_session and self._session is None:
            self._session = self._create_session()

    def _create_session(self) -> Any:
        """Create an optional Foundry Agents execution session."""
        try:
            from azure.ai.projects.models import VersionRefIndicator
        except ImportError as exc:  # pragma: no cover - depends on installed SDK version
            raise RuntimeError(
                "Execution sessions require an 'azure-ai-projects' version that exposes "
                "'beta.agents.create_session'. The installed SDK does not expose this optional API. "
                "Construct DocumentAgentClient with use_session=False (default) instead."
            ) from exc

        version_indicator = VersionRefIndicator(agent_version=self.agent_version) if self.agent_version else None
        return self._project_client.beta.agents.create_session(
            agent_name=self.agent_name,
            isolation_key=self._isolation_key,
            version_indicator=version_indicator,
        )

    def analyze(
        self,
        text: str,
        attachments: Sequence[Path] = (),
        *,
        timeout: float | None = 600.0,
    ) -> str:
        """Send a text prompt and optional file attachments to the agent.

        Successful calls within this client instance are chained using the
        previous response ID, retaining the agent's document session state.

        Args:
            text: Free-text instruction/question for the agent.
            attachments: Local file paths (e.g. PDFs) to attach, exactly like
                a chat file upload.
            timeout: Request timeout in seconds (the workflow runs OCR plus a
                multi-step LLM pipeline, so the default is generous).

        Returns:
            The agent's aggregated reply text (``response.output_text``).

        Raises:
            RuntimeError: If the agent-side response status is ``"failed"``.
        """
        self.open()

        content: list[dict[str, Any]] = [{"type": "input_text", "text": text}]
        content.extend(_encode_attachment(path) for path in attachments)

        extra_body: dict[str, Any] = {}
        if self._session is not None:
            extra_body["agent_session_id"] = self._session.agent_session_id
            logger.info(
                "Sending request with agent_session_id=%s",
                self._session.agent_session_id,
                extra={"agent_session_id": self._session.agent_session_id},
            )

        response = self._openai_client.responses.create(
            input=[{"role": "user", "content": content}],
            previous_response_id=self._previous_response_id,
            extra_body=extra_body or None,
            timeout=timeout,
        )

        status = getattr(response, "status", None)
        response_id = getattr(response, "id", None)
        agent_session_id = getattr(response, "agent_session_id", None)
        logger.info(
            "Received agent response response_id=%s agent_session_id=%s status=%s",
            response_id,
            agent_session_id,
            status,
            extra={
                "response_id": response_id,
                "agent_session_id": agent_session_id,
                "status": status,
            },
        )
        if status != "completed":
            error = getattr(response, "error", None)
            raise RuntimeError(f"Agent response did not complete (status={status}): {error}")

        self._previous_response_id = response_id
        return response.output_text

    def close(self) -> None:
        """Release the session (if any) and close the underlying clients."""
        self._previous_response_id = None
        if self._session is not None:
            self._project_client.beta.agents.delete_session(
                agent_name=self.agent_name,
                session_id=self._session.agent_session_id,
                isolation_key=self._isolation_key,
            )
            self._session = None
        if self._openai_client is not None:
            self._openai_client.close()
            self._openai_client = None
        self._project_client.close()
