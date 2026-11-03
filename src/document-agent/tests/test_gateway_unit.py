"""Verify the real SDK's model requests against the central gateway contract."""

import json
import unittest
from unittest.mock import Mock, patch

import httpx2 as httpx
from openai import AsyncAzureOpenAI

from main import _gateway_chat_client


class TestGatewayClient(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_uses_chat_completions_without_duplicate_openai_path(self) -> None:
        for suffix in ("", "/"):
            with self.subTest(suffix=suffix):
                requests = []

                def respond(request: httpx.Request) -> httpx.Response:
                    requests.append(request)
                    return httpx.Response(200, json={
                        "id": "chatcmpl-example",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "REPLACE_WITH_MODEL_DEPLOYMENT_NAME",
                        "choices": [{
                            "index": 0,
                            "message": {"role": "assistant", "content": "Example OK"},
                            "finish_reason": "stop",
                        }],
                        "usage": {
                            "prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4,
                        },
                    })

                async def token() -> str:
                    return "synthetic-token"

                def sdk(**kwargs) -> AsyncAzureOpenAI:
                    return AsyncAzureOpenAI(
                        **kwargs,
                        http_client=httpx.AsyncClient(
                            transport=httpx.MockTransport(respond)
                        ),
                    )

                with (
                    patch.dict("os.environ", {
                        "AI_GATEWAY_BASE_URL": "https://gateway.invalid/openai" + suffix,
                        "AI_GATEWAY_AUDIENCE": "api://example",
                        "AI_GATEWAY_API_VERSION": "2024-10-21",
                        "AZURE_AI_MODEL_DEPLOYMENT_NAME": "REPLACE_WITH_MODEL_DEPLOYMENT_NAME",
                    }),
                    patch("main.AsyncDefaultAzureCredential", return_value=Mock()),
                    patch("main.get_bearer_token_provider", return_value=token) as provider,
                    patch("main.AsyncAzureOpenAI", side_effect=sdk),
                ):
                    client = _gateway_chat_client()
                    try:
                        result = await client.get_response(
                            "Synthetic example request", options={"store": False}
                        )
                    finally:
                        await client.client.close()

                provider.assert_called_once()
                self.assertEqual(provider.call_args.args[1], "api://example/.default")
                self.assertEqual(result.text, "Example OK")
                self.assertEqual(len(requests), 1)
                self.assertEqual(
                    requests[0].url.path,
                    "/openai/deployments/REPLACE_WITH_MODEL_DEPLOYMENT_NAME/chat/completions",
                )
                self.assertEqual(
                    requests[0].url.params["api-version"], "2024-10-21"
                )
                self.assertEqual(
                    requests[0].headers["Authorization"], "Bearer synthetic-token"
                )
                self.assertFalse(json.loads(requests[0].content)["store"])
