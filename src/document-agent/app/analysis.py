"""Prompt assembly for the Document Agent "Firmenanalyse" credit assessment.

This module builds the model input for the credit assessment and loads the prompt
assets for every step of the multi-step workflow that mirrors the original AWS
implementation (``generate_leasing_credit_assessment.py``):

* **analysis**   – ``system_firmenanalyse_assessment.md`` + ``user_all.md``
* **revision**   – ``system_revision_assessment.md`` + ``revision_assessment.md``
* **validation** – ``system_validation_assessment.md`` + ``validation_assessment.md``
* **correction** – ``system_correction_assessment.md`` + ``correction_assessment.md``

Keeping this logic separate from transport (chat/attachments), OCR and workflow
orchestration follows the Single Responsibility Principle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

DEFAULT_SYSTEM_PROMPT_FILE = "system_firmenanalyse_assessment.md"
DEFAULT_USER_PROMPT_FILE = "user_all.md"
DEFAULT_REVISION_SYSTEM_FILE = "system_revision_assessment.md"
DEFAULT_REVISION_TASK_FILE = "revision_assessment.md"
DEFAULT_VALIDATION_SYSTEM_FILE = "system_validation_assessment.md"
DEFAULT_VALIDATION_TASK_FILE = "validation_assessment.md"
DEFAULT_CORRECTION_SYSTEM_FILE = "system_correction_assessment.md"
DEFAULT_CORRECTION_TASK_FILE = "correction_assessment.md"

# Documents whose name contains this token are the leading FINANCIAL_SUMMARY data source.
FINANCIAL_SUMMARY_TOKEN = "FINANCIAL_SUMMARY"


@dataclass
class ExtractedDocument:
    """A single uploaded document after OCR.

    Attributes:
        name: Human-readable document name (e.g. file name or a fallback label).
        text: OCR-extracted, page-labeled text of the document.
    """

    name: str
    text: str


@dataclass
class AnalysisPrompts:
    """Loaded prompt assets for every step of the credit-assessment workflow.

    Attributes:
        system_prompt: System instruction for the primary analyst persona.
        user_template: Task template describing the required analysis.
        revision_system: System persona for the revision (editor) step.
        revision_task: Instructions for shortening/refining the analysis.
        validation_system: System persona for the validation (QA) step.
        validation_task: Instructions for the FINANCIAL_SUMMARY-based validation.
        correction_system: System persona for the correction step.
        correction_task: Instructions for correcting figures against FINANCIAL_SUMMARY.
    """

    system_prompt: str
    user_template: str
    revision_system: str
    revision_task: str
    validation_system: str
    validation_task: str
    correction_system: str
    correction_task: str


def _read_prompt(filename: str) -> str:
    """Read a prompt file from the bundled ``prompts`` directory.

    Args:
        filename: File name inside the ``prompts`` directory.

    Returns:
        Stripped UTF-8 text content of the prompt file.
    """
    return (_PROMPTS_DIR / filename).read_text(encoding="utf-8").strip()


def load_prompts() -> AnalysisPrompts:
    """Load the system and task prompt templates for all workflow steps.

    Returns:
        The loaded :class:`AnalysisPrompts`.
    """
    return AnalysisPrompts(
        system_prompt=_read_prompt(DEFAULT_SYSTEM_PROMPT_FILE),
        user_template=_read_prompt(DEFAULT_USER_PROMPT_FILE),
        revision_system=_read_prompt(DEFAULT_REVISION_SYSTEM_FILE),
        revision_task=_read_prompt(DEFAULT_REVISION_TASK_FILE),
        validation_system=_read_prompt(DEFAULT_VALIDATION_SYSTEM_FILE),
        validation_task=_read_prompt(DEFAULT_VALIDATION_TASK_FILE),
        correction_system=_read_prompt(DEFAULT_CORRECTION_SYSTEM_FILE),
        correction_task=_read_prompt(DEFAULT_CORRECTION_TASK_FILE),
    )


@dataclass
class AnalysisRequest:
    """The assembled context for one credit-assessment run.

    Attributes:
        user_message: Free-text request the user typed into the chat.
        documents: OCR-extracted uploaded documents (financial data).
        guideline_text: Optional OCR/plain text of the ASSESSMENT_GUIDELINE guideline.
    """

    user_message: str
    documents: list[ExtractedDocument] = field(default_factory=list)
    guideline_text: str | None = None


def select_financial_summary_context(documents: list[ExtractedDocument]) -> str | None:
    """Return the OCR text of the leading FINANCIAL_SUMMARY document, if present.

    Mirrors ``_build_financial_summary_content`` from the AWS pilot: the FINANCIAL_SUMMARY document is the
    binding data source used by the validation and correction steps.

    Args:
        documents: OCR-extracted uploaded documents.

    Returns:
        The FINANCIAL_SUMMARY document text, or ``None`` when no FINANCIAL_SUMMARY document was attached.
    """
    for document in documents:
        if FINANCIAL_SUMMARY_TOKEN in document.name.upper():
            return document.text
    return None


def build_analysis_input(request: AnalysisRequest, user_template: str) -> str:
    """Build the assembled user message passed to the model.

    Args:
        request: The assembled analysis request (message, documents, guideline).
        user_template: The task template loaded from ``user_all.md``.

    Returns:
        A single Markdown string combining guideline, financial documents,
        the task template and the user's free-text request.
    """
    parts: list[str] = []

    if request.guideline_text:
        parts.append(
            "## RICHTLINIE: ASSESSMENT GUIDELINE\n"
            "Die folgende Richtlinie ist für die Analyse maßgeblich:\n\n"
            f"{request.guideline_text}"
        )

    if request.documents:
        parts.append("## FINANZDATEN\nDie folgenden Dokumente enthalten die Finanzdaten des Kunden:")
        for document in request.documents:
            parts.append(f"### {document.name.upper()}\n\n{document.text}")
    else:
        parts.append(
            "## FINANZDATEN\nEs wurden keine Dokumente angehängt. Fordere die "
            "benötigten Unterlagen (z. B. FINANCIAL_SUMMARY, Jahresabschluss) klar und knapp an."
        )

    parts.append(user_template)

    user_message = request.user_message.strip()
    if user_message:
        parts.append(f"## ZUSÄTZLICHE ANWEISUNGEN DES NUTZERS\n{user_message}")

    return "\n\n".join(parts)
