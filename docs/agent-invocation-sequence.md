# Agent Invocation Sequence Diagram

Das folgende Mermaid-Sequenzdiagramm zeigt den tatsächlichen Workflow eines Aufrufs für den Hosted Agent in `src/document-agent`.

Der Agent nutzt seit dem Umbau einen **mehrstufigen Foundry-Multi-Agent-Workflow**
(`app/workflow.py`) statt eines einzelnen Inner-Agents. Er bildet die originale
AWS-Pipeline (`generate_leasing_credit_assessment.py`) nach:
`analysis → revision → (validation) → (correction) → finalize`.

```mermaid
sequenceDiagram
    autonumber
    actor User as User / Client
    participant Host as ResponsesHostServer
    participant Main as main.py
    participant DocumentAgent as DocumentAgent
    participant Analysis as app.analysis
    participant OCR as app.ocr
    participant DI as Azure AI Document Intelligence
    participant WF as app.workflow (Workflow)
    participant Steps as Step-Agents (analysis/revision/validation/correction)
    participant Foundry as Azure AI Foundry Chat Model

    Note over Main,Host: Prozessstart beim lokalen Run oder im Hosted-Agent-Container
    Main->>Main: load_dotenv()
    Main->>Main: _project_endpoint() aus Env auflösen
    Main->>DocumentAgent: FoundryChatClient(project_endpoint, model, credential)
    Main->>DocumentAgent: DocumentAgent(chat_client, ocr_language, run_validation?, run_correction?)
    DocumentAgent->>WF: build_assessment_workflow(chat_client, prompts)
    Note over WF,Steps: Erzeugt je einen Foundry-Agent pro Stufe + konditionale Kanten
    Main->>Host: ResponsesHostServer(agent).run()

    User->>Host: Nachricht + Attachments senden
    alt non-streaming
        Host->>DocumentAgent: run(messages, thread, ...)
    else streaming
        Host->>DocumentAgent: run_stream(messages, thread, ...)
    end

    DocumentAgent->>DocumentAgent: _prepare_state(messages)
    DocumentAgent->>DocumentAgent: _normalize(messages)
    DocumentAgent->>DocumentAgent: _collect(messages)

    loop pro User-Message
        alt Textinhalt
            DocumentAgent->>DocumentAgent: User-Text sammeln
        else PDF/Bild-Anhang
            DocumentAgent->>OCR: extract_text(raw, language, content_type)
            OCR->>DI: begin_analyze_document('prebuilt-read', body, ...)
            DI-->>OCR: erkannter Seitentext
            OCR-->>DocumentAgent: extrahierter Text
            alt Dateiname enthält ASSESSMENT_GUIDELINE
                DocumentAgent->>DocumentAgent: guideline_text setzen
            else reguläres Finanzdokument
                DocumentAgent->>DocumentAgent: ExtractedDocument anhängen
            end
        else nicht unterstützt
            DocumentAgent->>DocumentAgent: Attachment überspringen
        end
    end

    DocumentAgent->>Analysis: select_financial_summary_context(documents)
    Analysis-->>DocumentAgent: FINANCIAL_SUMMARY-Text (führende Quelle) oder None
    DocumentAgent->>Analysis: build_analysis_input(AnalysisRequest, user_template)
    Analysis-->>DocumentAgent: zusammengesetzter Markdown-Prompt
    DocumentAgent->>DocumentAgent: AssessmentState(assembled_input, financial_summary_context, run_validation, run_correction)

    DocumentAgent->>WF: workflow.run(AssessmentState)

    WF->>Steps: analysis: run(assembled_input)
    Steps->>Foundry: Chat-Completion (System = Firmenanalyse-Prompt)
    Foundry-->>Steps: Roh-Analyse
    Steps-->>WF: state.analysis

    WF->>Steps: revision: run(revision_task + analysis)
    Steps->>Foundry: Chat-Completion (System = Lektor-Prompt)
    Foundry-->>Steps: revidierte Analyse
    Steps-->>WF: state.analysis_revised

    alt run_validation UND FINANCIAL_SUMMARY vorhanden
        WF->>Steps: validation: run(FINANCIAL_SUMMARY + revidierte Analyse + validation_task)
        Steps->>Foundry: Chat-Completion (System = QA-Prompt)
        Foundry-->>Steps: Validierungsbericht (ggf. mit ⚠️)
        Steps-->>WF: state.validation_report
        alt run_correction UND ⚠️ im Bericht
            WF->>Steps: correction: run(FINANCIAL_SUMMARY + Bericht + revidierte Analyse)
            Steps->>Foundry: Chat-Completion (System = Korrektur-Prompt)
            Foundry-->>Steps: korrigierte Analyse
            Steps-->>WF: state.analysis_corrected
        end
    end

    WF->>WF: finalize: yield_output(analysis_corrected | analysis_revised)
    WF-->>DocumentAgent: finaler Markdown-Text

    alt non-streaming
        DocumentAgent-->>Host: AgentResponse (finaler Text)
    else streaming
        DocumentAgent-->>Host: AgentResponseUpdate je Absatz (nach Abschluss der Pipeline)
    end
    Host-->>User: Firmenanalyse als Markdown
```

## Kurzfassung des Flows

1. `main.py` initialisiert Foundry-Client, `DocumentAgent` und den `ResponsesHostServer`. `DocumentAgent` baut dabei einmalig den mehrstufigen Workflow (`build_assessment_workflow`) mit je einem Foundry-Agent pro Stufe.
2. Der Host empfängt eine User-Nachricht samt Anhängen und ruft `DocumentAgent.run(...)` oder `run_stream(...)` auf.
3. `DocumentAgent._prepare_state(...)` normalisiert die Nachrichten, trennt Text von binären Attachments und wählt das FINANCIAL_SUMMARY-Dokument als führende Datenquelle aus.
4. PDFs und Bilder werden über `app.ocr` an Azure AI Document Intelligence (`prebuilt-read`) geschickt.
5. Die OCR-Ergebnisse werden mit User-Text und Prompt-Templates in `app.analysis` zu einem `AssessmentState` (inkl. `assembled_input`, `financial_summary_context`, `run_validation`, `run_correction`) zusammengesetzt.
6. Der Workflow durchläuft die Stufen **analysis → revision → (validation) → (correction) → finalize**. Jede Stufe ist ein eigener Foundry-Agent mit eigener Persona/Prompt. Validierung und Korrektur laufen nur über **konditionale Kanten**: Validierung nur bei aktiviertem `run_validation` und vorhandenem FINANCIAL_SUMMARY-Dokument, Korrektur nur bei aktiviertem `run_correction` und einer `⚠️`-Markierung im Validierungsbericht.
7. `finalize` gibt die beste verfügbare Analyse zurück (korrigiert, sonst revidiert). Das Ergebnis wird als `AgentResponse` (non-streaming) bzw. absatzweise als `AgentResponseUpdate` (streaming, nach Abschluss der Pipeline) an den Client zurückgegeben.
