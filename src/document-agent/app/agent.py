"""The Document Agent "Firmenanalyse" hosted agent.

:class:`DocumentAgent` is an Agent Framework ``BaseAgent`` that:

1. reads the user's chat message and any attached documents (PDF/images),
2. extracts text from attachments via the configured OCR provider,
3. assembles the credit-assessment prompt, and
4. delegates generation to a **multi-agent Foundry workflow** that mirrors the
   original AWS pipeline (analysis → revision → optional validation → optional
   correction, see :mod:`app.workflow`), streaming the resulting German
   "Firmenanalyse" (Markdown) back into the chat.

Extracted documents are retained in the hosting-managed AgentSession, not in a
process-global cache. Binary documents are not copied into session state.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import AsyncIterable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from opentelemetry.trace import SpanKind

from agent_framework import (
    AgentResponse,
    AgentResponseUpdate,
    AgentSession,
    BaseAgent,
    Content,
    Message,
    ResponseStream,
)

from app.analysis import (
    AnalysisRequest,
    ExtractedDocument,
    build_analysis_input,
    load_prompts,
    select_financial_summary_context,
)
from app.ocr import OcrProvider
from app.attachments import AttachmentResolver
from app.workflow import AssessmentState, build_assessment_workflow
from app.telemetry import operation_span

logger = logging.getLogger(__name__)

# Chat roles are string literals in the Agent Framework ("user"/"assistant"/...).
_ROLE_USER = "user"
_ROLE_ASSISTANT = "assistant"

# Documents whose name contains this token are treated as the ASSESSMENT_GUIDELINE
# guideline instead of financial data.
_GUIDELINE_TOKEN = "ASSESSMENT_GUIDELINE"
_DOCUMENT_STATE_KEY = "document.documents.v1"
MAX_DOCUMENTS = 20
MAX_CONTEXT_CHARACTERS = 200_000


@dataclass(frozen=True)
class CachedDocument:
    name: str
    text: str
    digest: str


def _env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean flag from the environment.

    Args:
        name: Environment variable name.
        default: Value used when the variable is unset.

    Returns:
        The parsed boolean value.
    """
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}



class DocumentAgent(BaseAgent):
    """Hosted agent that produces a German credit "Firmenanalyse" from chat input."""

    def __init__(
        self,
        chat_client: Any,
        *,
        ocr_provider: OcrProvider,
        attachment_resolver: AttachmentResolver | None = None,
        run_validation: bool | None = None,
        run_correction: bool | None = None,
        name: str = "document",
        description: str = (
            "Document Agent erstellt eine Firmenanalyse (Kreditwürdigkeit) aus den im Chat "
            "angehängten Finanzdokumenten."
        ),
    ) -> None:
        """Initialize the agent.

        Args:
            chat_client: A Foundry chat client (``FoundryChatClient``) shared by
                every step agent of the assessment workflow.
            ocr_provider: OCR provider.
            attachment_resolver: Inline and authenticated Foundry file resolver.
            run_validation: Enable the optional FINANCIAL_SUMMARY validation step. Defaults to
                the ``RUN_VALIDATION`` environment flag (off by default).
            run_correction: Enable the optional correction step. Defaults to the
                ``RUN_CORRECTION`` environment flag (off by default).
            name: Agent name.
            description: Human-readable agent description.
        """
        super().__init__(name=name, description=description)
        self._prompts = load_prompts()
        self._ocr_provider = ocr_provider
        self._attachment_resolver = attachment_resolver or AttachmentResolver()
        self._run_validation = _env_flag("RUN_VALIDATION") if run_validation is None else run_validation
        self._run_correction = _env_flag("RUN_CORRECTION") if run_correction is None else run_correction
        self._chat_client = chat_client

    def _normalize(self, messages: Any) -> list[Message]:
        """Normalize arbitrary run input into a list of ``Message``.

        Args:
            messages: ``None``, a string, a ``Message`` or a list thereof.

        Returns:
            A list of ``Message`` instances.
        """
        if messages is None:
            return []
        if isinstance(messages, str):
            return [Message(role=_ROLE_USER, contents=[messages])]
        if isinstance(messages, Message):
            return [messages]
        if not isinstance(messages, Sequence):
            logger.error("Unsupported run input.")
            raise TypeError("Expected a string, Message or sequence of messages.")
        normalized: list[Message] = []
        for item in messages:
            if isinstance(item, str):
                normalized.append(Message(role=_ROLE_USER, contents=[item]))
            elif isinstance(item, Message):
                normalized.append(item)
            else:
                logger.error("Unsupported message input.")
                raise TypeError("Expected a string or an Agent Framework Message.")
        return normalized

    @staticmethod
    def _session_documents(session: AgentSession | None) -> list[CachedDocument]:
        if session is None:
            return []
        stored = session.state.get(_DOCUMENT_STATE_KEY, [])
        if not isinstance(stored, list):
            logger.error("Invalid document session state.")
            raise ValueError("Invalid document session state. Start a new conversation.")
        documents: list[CachedDocument] = []
        for item in stored:
            if not isinstance(item, dict):
                logger.error("Invalid document session entry.")
                raise ValueError("Invalid document session state. Start a new conversation.")
            name, text, digest = item.get("name"), item.get("text"), item.get("digest")
            if not (
                isinstance(name, str) and name
                and isinstance(text, str) and text
                and isinstance(digest, str) and digest
            ):
                logger.error("Invalid document session fields.")
                raise ValueError("Invalid document session state. Start a new conversation.")
            documents.append(CachedDocument(name=name, text=text, digest=digest))
        DocumentAgent._check_context_limits(documents)
        return documents

    @staticmethod
    def _check_context_limits(documents: Sequence[CachedDocument]) -> None:
        if len(documents) > MAX_DOCUMENTS:
            logger.error("Document count limit exceeded.")
            raise ValueError("A conversation supports at most 20 documents. Start a new conversation.")
        if sum(len(doc.text) for doc in documents) > MAX_CONTEXT_CHARACTERS:
            logger.error("Document context limit exceeded.")
            raise ValueError(
                "Document text exceeds the 200,000 character context limit. Split the documents."
            )

    def _collect(
        self, messages: Sequence[Message], previous: Sequence[CachedDocument] = ()
    ) -> tuple[str, list[CachedDocument]]:
        """Resolve attachments and reuse successful OCR results within this session."""
        user_text_parts: list[str] = []
        documents = list(previous)

        for message in messages:
            role = getattr(message, "role", None)
            if role is not None and str(role) != _ROLE_USER:
                continue
            for content in getattr(message, "contents", None) or []:
                if isinstance(content, str):
                    if content.strip():
                        user_text_parts.append(content.strip())
                    continue

                if content.type == "text":
                    if content.text and content.text.strip():
                        user_text_parts.append(content.text.strip())
                    continue
                fallback_name = f"dokument_{len(documents) + 1}"
                attachment = self._attachment_resolver.resolve(content, fallback_name)
                digest = hashlib.sha256(attachment.data).hexdigest()
                cached = next((doc for doc in documents if doc.digest == digest), None)
                name = (
                    cached.name
                    if cached is not None and attachment.name == fallback_name
                    else attachment.name
                )
                if any(doc.digest == digest and doc.name == name for doc in documents):
                    logger.info("Reusing session OCR result.")
                    continue
                retained = [doc for doc in documents if doc.name != name]
                if len(retained) >= MAX_DOCUMENTS:
                    logger.error("Document count limit exceeded before OCR.")
                    raise ValueError("A conversation supports at most 20 documents.")
                if cached is not None:
                    text = cached.text
                    logger.info("Reusing session OCR result for another document name.")
                else:
                    logger.info(
                        "Running OCR on attachment | media_type=%s bytes=%d",
                        attachment.media_type, len(attachment.data),
                    )
                    with operation_span("document.ocr"):
                        text = self._ocr_provider.extract_text(
                            attachment.data, content_type=attachment.media_type
                        )
                if not text.strip():
                    logger.error("OCR returned empty document text.")
                    raise ValueError("No readable text was found in an attachment.")
                documents = [
                    *retained, CachedDocument(name=name, text=text, digest=digest)
                ]
                self._check_context_limits(documents)
        return "\n\n".join(user_text_parts), documents

    async def _prepare_state(
        self, messages: Any, session: AgentSession | None = None
    ) -> tuple[AssessmentState, list[CachedDocument]]:
        """Build workflow state and the document snapshot to commit on success.

        Args:
            messages: Raw run input.

        Returns:
            Assessment state and extracted documents, without mutating the session.
        """
        normalized = self._normalize(messages)
        previous = self._session_documents(session)
        user_text, cached_documents = await asyncio.to_thread(self._collect, normalized, previous)
        documents = [
            ExtractedDocument(name=doc.name, text=doc.text)
            for doc in cached_documents if _GUIDELINE_TOKEN not in doc.name.upper()
        ]
        guideline_text = "\n\n".join(
            doc.text for doc in cached_documents if _GUIDELINE_TOKEN in doc.name.upper()
        ) or None
        financial_summary_context = select_financial_summary_context(documents)
        logger.info(
            "Prepared analysis input",
            extra={
                "document_count": len(documents),
                "has_guideline": guideline_text is not None,
                "has_financial_summary": financial_summary_context is not None,
                "run_validation": self._run_validation,
                "run_correction": self._run_correction,
            },
        )
        assembled = build_analysis_input(
            AnalysisRequest(
                user_message=user_text,
                documents=documents,
                guideline_text=guideline_text,
            ),
            self._prompts.user_template,
        )
        return AssessmentState(
            assembled_input=assembled,
            financial_summary_context=financial_summary_context,
            run_validation=self._run_validation,
            run_correction=self._run_correction,
        ), cached_documents

    async def _run_workflow(self, messages: Any, session: AgentSession | None = None) -> str:
        """Execute the assessment workflow and return the final analysis text.

        Args:
            messages: Chat input (text and/or document attachments).

        Returns:
            The final Markdown analysis (corrected if available, else revised).
        """
        with operation_span("document.assessment", kind=SpanKind.SERVER):
            return await self._assess(messages, session)

    async def _assess(self, messages: Any, session: AgentSession | None) -> str:
        state, documents = await self._prepare_state(messages, session)
        # Workflow executors have mutable run state; never share them across requests.
        workflow = build_assessment_workflow(self._chat_client, self._prompts)
        result = await workflow.run(state)
        outputs = result.get_outputs()
        if not outputs or not isinstance(outputs[-1], str) or not outputs[-1].strip():
            logger.error("Assessment workflow returned no final text.")
            raise RuntimeError("Assessment workflow returned no final text.")
        if session is not None:
            session.state[_DOCUMENT_STATE_KEY] = [asdict(doc) for doc in documents]
        return outputs[-1]

    def run(
        self,
        messages: Any = None,
        *,
        stream: bool = False,
        session: AgentSession | None = None,
        thread: AgentSession | None = None,
        **kwargs: Any,
    ) -> Any:
        """Dispatch to the streaming or non-streaming implementation.

        The hosting runtime consumes updates and then calls
        ``get_final_response()`` on the returned ``ResponseStream``. A bare
        async generator would emit text but fail when completing the response.

        Args:
            messages: Chat input (text and/or document attachments).
            stream: Whether to return a streaming (async-iterator) response.
            session: Hosting-managed conversation and document state.
            thread: Legacy alias for session.
            **kwargs: Additional options (unused; kept for interface parity).

        Returns:
            A ``ResponseStream`` when ``stream`` is ``True``,
            otherwise an ``Awaitable[AgentResponse]``.
        """
        if session is not None and thread is not None and session is not thread:
            logger.error("Conflicting session and thread arguments.")
            raise ValueError("Pass session or thread, not two different sessions.")
        session = session if session is not None else thread
        if stream:
            return ResponseStream(
                self._run_stream(messages, session=session, **kwargs),
                finalizer=AgentResponse.from_updates,
            )
        return self._run(messages, session=session, **kwargs)

    async def _run(
        self,
        messages: Any = None,
        *,
        session: AgentSession | None = None,
        **kwargs: Any,
    ) -> AgentResponse:
        """Run the assessment workflow and return the analysis (non-streaming).

        Args:
            messages: Chat input (text and/or document attachments).
            session: Hosting-managed conversation and document state.
            **kwargs: Additional options (unused; kept for interface parity).

        Returns:
            The complete agent response containing the Markdown analysis.
        """
        text = await self._run_workflow(messages, session)
        return AgentResponse(
            messages=Message(role=_ROLE_ASSISTANT, contents=[Content.from_text(text)])
        )

    async def _run_stream(
        self,
        messages: Any = None,
        *,
        session: AgentSession | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[AgentResponseUpdate]:
        """Run the assessment workflow and stream the resulting analysis.

        The multi-step workflow (analysis → revision → optional validation →
        optional correction) must complete before the final text is known, so the
        result is emitted paragraph-by-paragraph once the pipeline has finished.

        Args:
            messages: Chat input (text and/or document attachments).
            session: Hosting-managed conversation and document state.
            **kwargs: Additional options (unused; kept for interface parity).

        Yields:
            Incremental response updates with the Markdown analysis.
        """
        text = await self._run_workflow(messages, session)
        chunks = text.split("\n\n")
        last = len(chunks) - 1
        for index, chunk in enumerate(chunks):
            payload = chunk if index == last else f"{chunk}\n\n"
            yield AgentResponseUpdate(
                contents=[Content.from_text(payload)],
                role=_ROLE_ASSISTANT,
            )
