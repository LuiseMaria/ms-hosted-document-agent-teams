import copy
import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location(
    "enable_teams_endpoint", Path(__file__).parents[1] / "enable_teams_endpoint.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.url = "https://example.services.ai.azure.com/api/projects/test"
        self.agent = {
            "agent_endpoint": {
                "protocol_configuration": {"responses": {}, "a2a": {"enabled": True}},
                "authorization_schemes": [
                    {"type": "Entra", "isolation_key_source": {"kind": "Entra"}},
                    {"type": "Custom", "setting": "preserve"},
                ],
                "version_selector": {"unchanged": True},
            }
        }

    def test_merge_preserves_configuration_and_does_not_mutate_input(self):
        original = copy.deepcopy(self.agent)
        self.agent["agent_endpoint"]["protocol_configuration"]["activity"] = {"other": 1}
        desired = module.desired_configuration(self.agent)
        self.assertEqual(desired["protocol_configuration"]["activity"]["other"], 1)
        self.assertTrue(desired["protocol_configuration"]["activity"]["enable_m365_public_endpoint"])
        self.assertEqual(desired["protocol_configuration"]["a2a"], {"enabled": True})
        self.assertEqual(desired["authorization_schemes"][:-1],
                         original["agent_endpoint"]["authorization_schemes"])
        self.assertEqual(desired["authorization_schemes"][-1], {"type": "BotServiceTenant"})
        self.assertNotIn("enable_m365_public_endpoint",
                         self.agent["agent_endpoint"]["protocol_configuration"]["activity"])

    def test_patch_then_verify_and_second_run_is_noop(self):
        state = copy.deepcopy(self.agent)
        calls = []

        def rest(method, url, body=None):
            calls.append(method)
            self.assertTrue(url.endswith("/agents/document-agent?api-version=v1"))
            if method == "patch":
                self.assertEqual(set(body), {"agent_endpoint"})
                state["agent_endpoint"].update(copy.deepcopy(body["agent_endpoint"]))
            return copy.deepcopy(state)

        module.configure(self.url, "document-agent", rest)
        module.configure(self.url, "document-agent", rest)
        self.assertEqual(calls, ["get", "patch", "get", "get"])
        self.assertEqual(state["agent_endpoint"]["version_selector"], {"unchanged": True})

    def test_already_enabled_does_not_patch(self):
        self.agent["agent_endpoint"].update(module.desired_configuration(self.agent))
        rest = Mock(return_value=self.agent)
        module.configure(self.url, "document-agent", rest)
        rest.assert_called_once()

    def test_existing_bot_scope_is_preserved(self):
        for scope in ("BotServiceTenant",):
            with self.subTest(scope=scope):
                agent = copy.deepcopy(self.agent)
                agent["agent_endpoint"]["authorization_schemes"].append(
                    {"type": scope, "setting": "preserve"}
                )
                original = copy.deepcopy(agent)
                desired = module.desired_configuration(agent)
                self.assertEqual(
                    desired["authorization_schemes"],
                    agent["agent_endpoint"]["authorization_schemes"],
                )
                self.assertEqual(agent, original)
                agent["agent_endpoint"].update(desired)
                rest = Mock(return_value=agent)
                module.configure(self.url, "document-agent", rest)
                rest.assert_called_once()

    def test_rbac_is_replaced_with_tenant_access(self):
        state = copy.deepcopy(self.agent)
        state["agent_endpoint"]["authorization_schemes"].append({"type": "BotServiceRbac"})
        original = copy.deepcopy(state)
        calls = []

        def rest(method, url, body=None):
            calls.append(method)
            if method == "patch":
                state["agent_endpoint"].update(copy.deepcopy(body["agent_endpoint"]))
            return copy.deepcopy(state)

        module.configure(self.url, "document-agent", rest)
        module.configure(self.url, "document-agent", rest)
        self.assertEqual(calls, ["get", "patch", "get", "get"])
        self.assertEqual(
            state["agent_endpoint"]["authorization_schemes"],
            original["agent_endpoint"]["authorization_schemes"][:-1]
            + [{"type": "BotServiceTenant"}],
        )
        self.assertEqual(state["agent_endpoint"]["version_selector"], {"unchanged": True})

    def test_tenant_scope_can_enable_activity_and_verify(self):
        state = copy.deepcopy(self.agent)
        state["agent_endpoint"]["authorization_schemes"].append({"type": "BotServiceTenant"})
        calls = []

        def rest(method, url, body=None):
            calls.append(method)
            if method == "patch":
                state["agent_endpoint"].update(copy.deepcopy(body["agent_endpoint"]))
            return copy.deepcopy(state)

        module.configure(self.url, "document-agent", rest)
        self.assertEqual(calls, ["get", "patch", "get"])
        self.assertTrue(
            state["agent_endpoint"]["protocol_configuration"]["activity"]["enable_m365_public_endpoint"]
        )
        self.assertNotIn(
            {"type": "BotServiceRbac"}, state["agent_endpoint"]["authorization_schemes"]
        )

    def test_conflicting_bot_scopes_do_not_patch(self):
        self.agent["agent_endpoint"]["authorization_schemes"].extend([
            {"type": "BotServiceTenant"}, {"type": "BotServiceRbac"},
        ])
        rest = Mock(return_value=self.agent)
        with self.assertRaisesRegex(ValueError, "publishing scope"):
            module.configure(self.url, "document-agent", rest)
        rest.assert_called_once()

    def test_malformed_or_missing_access_fails_closed(self):
        for protocols, schemes in [
            (None, [{"type": "Entra"}]), ({}, [{"type": "Entra"}]),
            ({"responses": {}}, None), ({"responses": {}}, []),
            ({"responses": {}}, [{"type": "Unknown"}]),
            ({"responses": {}, "activity": None}, [{"type": "Entra"}]),
        ]:
            with self.subTest(protocols=protocols, schemes=schemes):
                self.agent["agent_endpoint"].update(
                    protocol_configuration=protocols, authorization_schemes=schemes
                )
                with self.assertRaises(ValueError):
                    module.desired_configuration(self.agent)

    def test_verification_failure_is_not_reported_as_success(self):
        rest = Mock(side_effect=[self.agent, None, self.agent])
        with self.assertRaisesRegex(RuntimeError, "verification failed"):
            module.configure(self.url, "document-agent", rest)

    def test_cli_failure_stops_without_patch(self):
        rest = Mock(side_effect=subprocess.CalledProcessError(1, "az", stderr="denied"))
        with self.assertRaises(subprocess.CalledProcessError):
            module.configure(self.url, "document-agent", rest)
        rest.assert_called_once()

    def test_invalid_url_rejected_before_cli(self):
        rest = Mock()
        for url in ("http://example/api/projects/test", self.url + "?other=1"):
            with self.assertRaises(ValueError):
                module.configure(url, "document-agent", rest)
        rest.assert_not_called()

    def test_azure_cli_body_file_and_cleanup(self):
        paths = []

        def run(command, **kwargs):
            self.assertTrue(kwargs["check"])
            body_path = Path(command[command.index("--body") + 1][1:])
            paths.append(body_path)
            self.assertEqual(json.loads(body_path.read_text()), {"agent_endpoint": {}})
            self.assertIn("Content-Type=application/merge-patch+json", command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with patch.object(module.subprocess, "run", side_effect=run):
            module.az_rest("patch", self.url, {"agent_endpoint": {}})
        self.assertFalse(paths[0].exists())


if __name__ == "__main__":
    unittest.main()
