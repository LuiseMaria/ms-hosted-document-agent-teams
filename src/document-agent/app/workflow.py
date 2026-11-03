"""Multi-agent credit-assessment workflow (Foundry Agent Framework).

This module rebuilds the original AWS pipeline from
``generate_leasing_credit_assessment.py`` as an Agent Framework
:class:`~agent_framework.Workflow`, following the multi-agent pattern shown in
``src/agent-framework-workflows-responses/main.py`` (chained executors built with
:class:`~agent_framework.WorkflowBuilder`).

Pipeline (each step is a Foundry-backed :class:`~agent_framework.Agent`):

    analysis ─▶ revision ─▶ (validation) ─▶ (correction) ─▶ finalize

* **analysis**   – primary "Firmenanalyse" (``_run_analysis_and_revision``, step 1).
* **revision**   – shortens/refines the analysis (``_run_analysis_and_revision``, step 2).
* **validation** – optional FINANCIAL_SUMMARY cross-check (``_run_optional_validation``).
* **correction** – optional fix of figures when validation flags ``⚠️``
  (``_run_optional_correction``).
* **finalize**   – yields the final analysis (corrected if available, else revised).

Validation and correction are wired through **conditional edges** so they only run
when enabled and needed – exactly like the ``run_validation`` / ``run_correction``
switches and the ``⚠️`` gate in the AWS implementation. A shared
:class:`AssessmentState` object flows through the graph so downstream steps can see
the FINANCIAL_SUMMARY source document and every intermediate result.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Never

from agent_framework import (
    Agent,
    Executor,
    Workflow,
    WorkflowBuilder,
    WorkflowContext,
    handler,
)

from app.analysis import AnalysisPrompts

logger = logging.getLogger(__name__)

# Marker the validation step emits for detected deviations; gates the correction.
WARNING_MARKER = "⚠️"

_FINANCIAL_SUMMARY_HEADING = "## FINANCIAL_SUMMARY-DOKUMENT (führende Datenquelle)"


@dataclass
class AssessmentState:
    """Mutable state passed between the workflow steps.

    Attributes:
        assembled_input: Full analysis prompt (guideline + documents + task
            template + user message) as produced by ``build_analysis_input``.
        financial_summary_context: OCR text of the leading FINANCIAL_SUMMARY document, if attached.
        run_validation: Whether the optional validation step should run.
        run_correction: Whether the optional correction step should run.
        analysis: Raw primary analysis text.
        analysis_revised: Revised (shortened/refined) analysis text.
        validation_report: Validation report produced against FINANCIAL_SUMMARY.
        analysis_corrected: Corrected analysis text (when correction ran).
        correction_applied: Whether a correction was applied.
    """

    assembled_input: str
    financial_summary_context: str | None = None
    run_validation: bool = False
    run_correction: bool = False
    analysis: str = ""
    analysis_revised: str = ""
    validation_report: str = ""
    analysis_corrected: str = ""
    correction_applied: bool = False

    @property
    def final_text(self) -> str:
        """Return the best available analysis text for the user."""
        return self.analysis_corrected or self.analysis_revised or self.analysis


def _financial_summary_block(financial_summary_context: str | None) -> str:
    """Render the FINANCIAL_SUMMARY source block for validation/correction prompts."""
    if not financial_summary_context:
        return ""
    return f"{_FINANCIAL_SUMMARY_HEADING}\n\n{financial_summary_context}\n\n"


async def _run_agent(agent: Agent, prompt: str) -> str:
    """Run a Foundry agent on a prompt and return its text response.

    Args:
        agent: The Foundry-backed agent to invoke.
        prompt: The user prompt for this step.

    Returns:
        The agent's text response.
    """
    response = await agent.run(prompt)
    return response.text


class AnalysisExecutor(Executor):
    """Step 1: produce the primary "Firmenanalyse"."""

    def __init__(self, agent: Agent, *, id: str = "analysis") -> None:
        super().__init__(id=id)
        self._agent = agent

    @handler
    async def run(self, state: AssessmentState, ctx: WorkflowContext[AssessmentState]) -> None:
        """Generate the primary analysis and forward the state."""
        logger.info("Running analysis step", extra={"input_length": len(state.assembled_input)})
        state.analysis = await _run_agent(self._agent, state.assembled_input)
        logger.info("Analysis completed", extra={"analysis_length": len(state.analysis)})
        await ctx.send_message(state)


class RevisionExecutor(Executor):
    """Step 2: shorten and refine the analysis (editor persona)."""

    def __init__(self, agent: Agent, revision_task: str, *, id: str = "revision") -> None:
        super().__init__(id=id)
        self._agent = agent
        self._revision_task = revision_task

    @handler
    async def run(self, state: AssessmentState, ctx: WorkflowContext[AssessmentState]) -> None:
        """Revise the analysis and forward the state."""
        prompt = (
            f"{self._revision_task}\n\n"
            f"## ZU ÜBERARBEITENDE FIRMENANALYSE\n\n{state.analysis}"
        )
        logger.info("Running revision step")
        state.analysis_revised = await _run_agent(self._agent, prompt)
        logger.info("Revision completed", extra={"revised_length": len(state.analysis_revised)})
        await ctx.send_message(state)


class ValidationExecutor(Executor):
    """Optional step 3: validate figures against the FINANCIAL_SUMMARY source document."""

    def __init__(self, agent: Agent, validation_task: str, *, id: str = "validation") -> None:
        super().__init__(id=id)
        self._agent = agent
        self._validation_task = validation_task

    @handler
    async def run(self, state: AssessmentState, ctx: WorkflowContext[AssessmentState]) -> None:
        """Produce the validation report and forward the state."""
        prompt = (
            f"{_financial_summary_block(state.financial_summary_context)}"
            f"## ZU PRÜFENDE FIRMENANALYSE\n\n{state.analysis_revised}\n\n"
            f"{self._validation_task}"
        )
        logger.info("Running validation step")
        state.validation_report = await _run_agent(self._agent, prompt)
        logger.info(
            "Validation completed",
            extra={
                "report_length": len(state.validation_report),
                "contains_warning": WARNING_MARKER in state.validation_report,
            },
        )
        await ctx.send_message(state)


class CorrectionExecutor(Executor):
    """Optional step 4: correct flagged figures against the FINANCIAL_SUMMARY source."""

    def __init__(self, agent: Agent, correction_task: str, *, id: str = "correction") -> None:
        super().__init__(id=id)
        self._agent = agent
        self._correction_task = correction_task

    @handler
    async def run(self, state: AssessmentState, ctx: WorkflowContext[AssessmentState]) -> None:
        """Apply corrections and forward the state."""
        prompt = (
            f"{_financial_summary_block(state.financial_summary_context)}"
            f"{self._correction_task}\n\n"
            f"## VALIDIERUNGSBERICHT\n{state.validation_report}\n\n"
            f"## ZU KORRIGIERENDE FIRMENANALYSE\n\n{state.analysis_revised}"
        )
        logger.info("Running correction step")
        state.analysis_corrected = await _run_agent(self._agent, prompt)
        state.correction_applied = True
        logger.info("Correction completed", extra={"corrected_length": len(state.analysis_corrected)})
        await ctx.send_message(state)


class FinalizeExecutor(Executor):
    """Terminal step: yield the final analysis text as the workflow output."""

    def __init__(self, *, id: str = "finalize") -> None:
        super().__init__(id=id)

    @handler
    async def run(self, state: AssessmentState, ctx: WorkflowContext[Never, str]) -> None:
        """Emit the best available analysis text."""
        logger.info(
            "Finalizing assessment",
            extra={"correction_applied": state.correction_applied, "length": len(state.final_text)},
        )
        await ctx.yield_output(state.final_text)


def _should_validate(state: AssessmentState) -> bool:
    """Route to validation only when enabled and a FINANCIAL_SUMMARY document is present."""
    return bool(state.run_validation and state.financial_summary_context)


def _skip_validation(state: AssessmentState) -> bool:
    """Route straight to finalize when validation is not applicable."""
    return not _should_validate(state)


def _should_correct(state: AssessmentState) -> bool:
    """Route to correction only when enabled and validation flagged a warning."""
    return bool(state.run_correction and WARNING_MARKER in state.validation_report)


def _skip_correction(state: AssessmentState) -> bool:
    """Route to finalize when no correction is required."""
    return not _should_correct(state)


def build_assessment_workflow(chat_client: Any, prompts: AnalysisPrompts) -> Workflow:
    """Build the multi-agent credit-assessment workflow.

    Args:
        chat_client: A Foundry chat client shared by all step agents.
        prompts: The loaded prompt assets for every step.

    Returns:
        A built :class:`~agent_framework.Workflow` accepting an
        :class:`AssessmentState` and yielding the final analysis as ``str``.
    """
    analysis_executor = AnalysisExecutor(
        Agent(
            client=chat_client, instructions=prompts.system_prompt, name="analysis",
            default_options={"store": False},
        )
    )
    revision_executor = RevisionExecutor(
        Agent(
            client=chat_client, instructions=prompts.revision_system, name="revision",
            default_options={"store": False},
        ),
        prompts.revision_task,
    )
    validation_executor = ValidationExecutor(
        Agent(
            client=chat_client, instructions=prompts.validation_system, name="validation",
            default_options={"store": False},
        ),
        prompts.validation_task,
    )
    correction_executor = CorrectionExecutor(
        Agent(
            client=chat_client, instructions=prompts.correction_system, name="correction",
            default_options={"store": False},
        ),
        prompts.correction_task,
    )
    finalize_executor = FinalizeExecutor()

    return (
        WorkflowBuilder(start_executor=analysis_executor, output_from=[finalize_executor])
        .add_edge(analysis_executor, revision_executor)
        .add_edge(revision_executor, validation_executor, condition=_should_validate)
        .add_edge(revision_executor, finalize_executor, condition=_skip_validation)
        .add_edge(validation_executor, correction_executor, condition=_should_correct)
        .add_edge(validation_executor, finalize_executor, condition=_skip_correction)
        .add_edge(correction_executor, finalize_executor)
        .build()
    )
