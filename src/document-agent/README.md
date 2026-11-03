# Document Agent Agent (`src/document-agent`)

Microsoft Foundry **hosted agent** that produces a German credit **"Firmenanalyse"**
from financial documents that the user attaches in the chat dialog.

## Workflow (Foundry multi-agent)

The assessment is built as an Agent Framework `Workflow`. Each step is a Foundry-backed `Agent`:

```
analysis ─▶ revision ─▶ (validation) ─▶ (correction) ─▶ finalize
```

| Step                        | Origin in the AWS pilot                           | Prompts                                           |
|-----------------------------|---------------------------------------------------|---------------------------------------------------|
| **analysis**                | `_run_analysis_and_revision` (step 1)             | `system_firmenanalyse_assessment.md`, `user_all.md`     |
| **revision**                | `_run_analysis_and_revision` (step 2)             | `system_revision_assessment.md`, `revision_assessment.md`     |
| **validation** *(optional)* | `_run_optional_validation`                        | `system_validation_assessment.md`, `validation_assessment.md` |
| **correction** *(optional)* | `_run_optional_correction`                        | `system_correction_assessment.md`, `correction_assessment.md` |
| **finalize**                | returns `analysis_corrected` / `analysis_revised` | —                                                 |

`validation` and `correction` are wired through **conditional edges**: validation
only runs when enabled *and* a `FINANCIAL_SUMMARY` document is attached; correction only runs
when enabled *and* validation flags a deviation (`⚠️`). This reproduces the
`run_validation` / `run_correction` switches and the `⚠️` gate of the pilot. A
shared `AssessmentState` flows through the graph so every step can see the FINANCIAL_SUMMARY
source document and all intermediate results.

## Attachments and conversation context

The request path is `ResponsesHostServer` -> `DocumentAgent` -> `AttachmentResolver`
-> Content Understanding -> assessment workflow.

Supported Responses API inputs:

- `input_file.file_data`: a `data:<mime-type>;base64,...` URI.
- `input_file.file_id`: a file uploaded to the configured Foundry project's
  OpenAI Files API. The agent downloads metadata and streams the bytes using
  `AIProjectClient.get_openai_client()`. The agent identity needs permission to
  read those files. File IDs from other projects or session-filesystem paths
  are not interchangeable with these IDs.
- Framework `Content.from_data(...)` uses the same inline path.

Ordinary `file_url` / HTTPS attachments are rejected, not downloaded. External
downloads require a separately reviewed source allowlist and redirect policy;
arbitrary user URLs must not be fetched by the agent.

Malformed Base64, empty input, unsupported types, download failures and empty OCR
results fail the request explicitly. They never become "no documents attached".
The supported types are PDF, PNG, JPEG, TIFF, BMP and HEIF.

Limits are defined in `app/attachments.py` and `app/agent.py`:

- 20 MiB per file, enforced before decoding/download and while streaming.
- 20 documents per conversation, including guidelines.
- 200,000 characters of extracted document text per conversation. This is a
  safety limit, not a token count or a guarantee that every model context fits.
  Choose a model with sufficient room for templates and output as well.
- File API calls have a 60 second timeout. OCR uses a 10 second connection
  timeout, a 60 second read timeout and a 180 second polling wait. A polling
  timeout fails the request; it does not cancel an already submitted Azure job.

`DocumentAgent.run(..., session=...)` stores only successful extracted text,
filenames and SHA-256 hashes in `AgentSession.state["document.documents.v1"]`.
The host saves/restores that state through its session store (user-isolated in
the Foundry-backed store). There is no process-global document cache.
Identical file bytes in that conversation reuse OCR output; changed bytes with
the same filename replace the old document. Distinct explicit filenames retain
their roles (for example FINANCIAL_SUMMARY versus ASSESSMENT_GUIDELINE), even for identical bytes.
No binary copy is added to this
session state. Each request gets a fresh workflow instance.

For a follow-up, send the last successful response ID as `previous_response_id`,
or use the same Responses conversation ID. The sample
`tests/document_client.py` automatically chains successful calls within one client
instance using `previous_response_id`. Reusing only a platform execution session
ID is not a substitute for Responses conversation continuity. Submit turns of
one conversation sequentially, not concurrently.

A new conversation has no access to the previous conversation's document context.
This is document memory, not a full user/assistant transcript: follow-up questions
are analyzed against the retained documents, not the previous answer.
Extracted text is sensitive financial data and is persisted by the hosting
session store. Configure its access, retention and deletion policies before
production use; starting a new conversation does not delete old stored data or
the uploaded source files. Downstream model calls explicitly use `store=False`;
this is not a blanket guarantee about platform logging or data retention.

The default analyzer remains `prebuilt-read` (basic OCR). It is not a custom
financial field extractor, and it does not offer `prebuilt-layout`'s table layout
analysis. Evaluate extraction quality on representative financial statements
before changing analyzers. OCR is billed separately; all assessment steps still
incur model input/output token charges.

## Configuration

| Variable                                                 | Purpose                                                     |
|----------------------------------------------------------|-------------------------------------------------------------|
| `FOUNDRY_PROJECT_ENDPOINT` / `AZURE_AI_PROJECT_ENDPOINT` | Foundry project endpoint                                    |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME`                         | Model deployment name (e.g. `gpt-4.1`)                      |
| `AZURE_CONTENT_UNDERSTANDING_ENDPOINT`                   | Content Understanding endpoint (Foundry account)             |
| `AZURE_CONTENT_UNDERSTANDING_ANALYZER`                    | Analyzer ID (default `prebuilt-read`)                       |
| `AZURE_CONTENT_UNDERSTANDING_API_VERSION`                 | Optional SDK API-version override                          |
| `RUN_VALIDATION`                                         | Enable the optional FINANCIAL_SUMMARY validation step (default `false`) |
| `RUN_CORRECTION`                                         | Enable the optional correction step (default `false`)       |

Copy `.env.example` to `.env` for local runs. In Foundry, these are injected from
the azd environment / Bicep outputs and **Managed Identity** is used for auth.

## Run locally

```bash
azd ai agent run                                   # http://localhost:8088
azd ai agent invoke --local "Erstelle die Firmenanalyse für die angehängten Unterlagen."
```

Attach the financial PDFs (e.g. `FINANCIAL_SUMMARY ...pdf`, `Konzernabschluss ...pdf`) in the
chat. A document whose name contains `ASSESSMENT_GUIDELINE` is treated as the guideline.

## Trace correlation

The Entra-authenticated Application Insights exporter records metadata, not
document text, prompts, responses or exception messages. The assessment is a
server span carrying the platform-provided `FOUNDRY_AGENT_NAME` and
`FOUNDRY_AGENT_VERSION`. A span processor copies AgentServer's request-scoped
conversation and response IDs from OpenTelemetry baggage onto assessment,
workflow, OCR and model spans. No conversation ID is invented when the host
does not provide one; such calls cannot appear in Foundry's Conversation view.
Model inference through the central APIM gateway does not prevent correlation.

## Offline regression tests

Run from this directory with the project's Python environment:

```powershell
python -m unittest discover -s tests -p "*_unit.py" -v
```

These tests use synthetic bytes and mocked Azure/model responses. They cover
inline/file-ID ingestion, explicit URL rejection, byte and context limits,
session serialization/isolation, duplicate OCR reuse, follow-ups, streaming and
OCR/workflow failures without uploading financial documents.

The existing live tests are separate. They require Azure credentials and send
the sample PDFs to the configured services. An HTTP 200 or a nonempty answer
alone does not prove that an attachment reached OCR.

## Deploy

```bash
azd deploy
azd ai agent invoke "Bitte erstelle die Firmenanalyse."
```
