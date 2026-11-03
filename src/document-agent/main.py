# Copyright (c) Microsoft. All rights reserved.
"""Entry point for the Document Agent hosted agent.

Builds a :class:`~app.agent.DocumentAgent` backed by a Foundry chat model and serves
it via the Agent Framework ``ResponsesHostServer`` (OpenAI Responses protocol) so
it can run as a Microsoft Foundry hosted agent. Users interact with the agent
through the chat dialog and attach the financial documents (PDF/images) to analyze.
"""

import logging
import os
from contextlib import ExitStack

from agent_framework.openai import OpenAIChatCompletionClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from azure.identity.aio import DefaultAzureCredential as AsyncDefaultAzureCredential
from azure.identity.aio import get_bearer_token_provider
from azure.ai.projects import AIProjectClient
from dotenv import load_dotenv
from openai import AsyncAzureOpenAI

from app.agent import DocumentAgent
from app.attachments import AttachmentResolver
from app.ocr_content_understanding import ContentUnderstandingOcrProvider
from app.telemetry import configure_telemetry

# Load environment variables from a local .env file when present (local dev).
load_dotenv()

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


def _project_endpoint() -> str:
    """Resolve the Foundry project endpoint from the environment.

    Returns:
        The Foundry project endpoint URL.

    Raises:
        KeyError: If no endpoint environment variable is set.
    """
    for key in ("FOUNDRY_PROJECT_ENDPOINT", "AZURE_AI_PROJECT_ENDPOINT", "AZURE_AIPROJECT_ENDPOINT"):
        value = os.getenv(key)
        if value:
            return value
    raise KeyError(
        "Missing Foundry project endpoint. Set FOUNDRY_PROJECT_ENDPOINT (or AZURE_AI_PROJECT_ENDPOINT)."
    )


def _gateway_chat_client() -> OpenAIChatCompletionClient:
    """Build the chat client for model inference via the central AI gateway.

    Document Agent is a Foundry **hosted** agent. Per
    01_PLATFORM_MODEL_FEDERATED_ARCH_v2.md ("How each runtime reaches a
    shared model" / "The architectural impact"), the admin-connected-model
    mechanism (a Foundry project "connection") is documented as
    **prompt-agent only** -- a hosted agent cannot select a gateway-connected
    model in its agent definition. The supported alternative is to treat
    model inference as ordinary application code: an OpenAI-compatible
    client calling API Management directly, authenticated with the
    workload's own managed identity token (not the project's Foundry
    connection, not FoundryChatClient against this account -- no model is
    deployed on the spoke's own Foundry account).

    Required environment variables:
        AI_GATEWAY_BASE_URL: central APIM gateway base URL for the OpenAI
            API, e.g. "https://example-gateway.azure-api.net/openai".
        AI_GATEWAY_AUDIENCE: Entra ID audience APIM's validate-azure-ad-token
            policy expects (must match apimConfig.apiAudience in
            central-ai-platform-infrastructure). Defaults to
            "api://central-ai-gateway".
        AI_GATEWAY_API_VERSION: API version to send to the gateway. Defaults
            to AZURE_AI_MODEL_API_VERSION or "2024-10-21".
        AZURE_AI_MODEL_DEPLOYMENT_NAME: the model deployment name as
            published on the central Foundry account.
    """
    base_url = os.environ["AI_GATEWAY_BASE_URL"]
    audience = os.getenv("AI_GATEWAY_AUDIENCE", "api://central-ai-gateway")
    api_version = os.getenv("AI_GATEWAY_API_VERSION", os.getenv("AZURE_AI_MODEL_API_VERSION", "2024-10-21"))
    model = os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"]

    # The workload's own managed identity is the caller identity APIM
    # validates -- it is never a Foundry project identity or a Foundry
    # connection for a hosted agent. See "Two distinct authentication hops"
    # and "Bypass prevention": this identity holds no role on any Foundry
    # account, only an allow-listed client-application-id on the gateway.
    token_provider = get_bearer_token_provider(AsyncDefaultAzureCredential(), f"{audience}/.default")

    async_client = AsyncAzureOpenAI(
        base_url=f"{base_url.rstrip('/')}/",
        azure_ad_token_provider=token_provider,
        api_version=api_version,
    )
    return OpenAIChatCompletionClient(model=model, async_client=async_client)


def main() -> None:
    """Build the Document Agent agent and start the Responses host server."""
    endpoint = _project_endpoint()
    with ExitStack() as resources:
        credential = resources.enter_context(DefaultAzureCredential())
        telemetry = configure_telemetry(credential)
        if telemetry is not None:
            resources.callback(telemetry.shutdown)
        project = resources.enter_context(
            AIProjectClient(endpoint=endpoint, credential=credential)
        )
        files_client = resources.enter_context(project.get_openai_client())
        ocr_provider = ContentUnderstandingOcrProvider(credential=credential)
        resources.callback(ocr_provider.close)
        client = _gateway_chat_client()
        agent = DocumentAgent(
            chat_client=client,
            ocr_provider=ocr_provider,
            attachment_resolver=AttachmentResolver(files_client),
        )

        logger.info("Starting Document Agent hosted agent on port 8088")
        # The host persists AgentSession.state; DocumentAgent owns document history.
        server = ResponsesHostServer(
            agent, history_source="agent", configure_observability=None
        )
        server.run()


if __name__ == "__main__":
    main()
