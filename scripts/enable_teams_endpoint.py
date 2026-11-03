"""Enable the protected M365 Activity route with tenant-wide Bot Service access."""

import argparse
import copy
import json
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit


def az_rest(method, url, body=None):
    command = [
        "az", "rest", "--method", method, "--url", url,
        "--resource", "https://ai.azure.com", "--output", "json",
    ]
    with tempfile.TemporaryDirectory(prefix="document-endpoint-") as directory:
        if body is not None:
            path = Path(directory) / "body.json"
            path.write_text(json.dumps(body), encoding="utf-8")
            command.extend([
                "--headers", "Content-Type=application/merge-patch+json",
                "--body", f"@{path}",
            ])
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    if method == "get":
        return json.loads(result.stdout)
    return None


def desired_configuration(agent):
    endpoint = agent["agent_endpoint"]
    protocols = endpoint["protocol_configuration"]
    schemes = endpoint["authorization_schemes"]
    if not isinstance(protocols, dict) or not isinstance(protocols.get("responses"), dict):
        raise ValueError("Expected an existing Responses endpoint; refusing to replace its configuration.")
    if not isinstance(schemes, list) or not schemes or not all(
        isinstance(scheme, dict) and isinstance(scheme.get("type"), str)
        for scheme in schemes
    ):
        raise ValueError("Expected explicit authorization schemes; refusing to change access.")
    if not any(scheme["type"] in ("Entra", "AzureRbac") for scheme in schemes):
        raise ValueError("Expected Entra/AzureRbac authorization for direct Responses access.")
    bot_schemes = [
        scheme for scheme in schemes
        if scheme["type"] in ("BotServiceRbac", "BotServiceTenant")
    ]
    if len(bot_schemes) > 1:
        raise ValueError(
            "Multiple Bot Service authorization schemes are configured. "
            "Resolve the publishing scope before enabling the endpoint."
        )
    merged = copy.deepcopy(protocols)
    activity = merged.setdefault("activity", {})
    if not isinstance(activity, dict):
        raise ValueError("Expected an Activity configuration object.")
    activity["enable_m365_public_endpoint"] = True
    authorization = copy.deepcopy(schemes)
    if bot_schemes and bot_schemes[0]["type"] == "BotServiceRbac":
        print("Changing Teams authorization from BotServiceRbac to BotServiceTenant.")
        authorization = [
            {"type": "BotServiceTenant"} if scheme["type"] == "BotServiceRbac" else scheme
            for scheme in authorization
        ]
    if not bot_schemes:
        authorization.append({"type": "BotServiceTenant"})
    return {
        "protocol_configuration": merged,
        "authorization_schemes": authorization,
    }


def configure(project_endpoint, agent_name, rest=az_rest):
    parsed = urlsplit(project_endpoint)
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.query
        or parsed.fragment or parsed.username or parsed.password
        or not parsed.path.rstrip("/").startswith("/api/projects/")
        or not agent_name.strip()
    ):
        raise ValueError("An HTTPS Foundry project endpoint and agent name are required.")
    url = f"{project_endpoint.rstrip('/')}/agents/{quote(agent_name, safe='')}?api-version=v1"
    current = rest("get", url)
    desired = desired_configuration(current)
    endpoint = current["agent_endpoint"]
    if all(endpoint[key] == value for key, value in desired.items()):
        print("M365 Activity endpoint is already configured; no PATCH required.")
        return
    rest("patch", url, {"agent_endpoint": desired})
    actual = rest("get", url)["agent_endpoint"]
    if any(actual.get(key) != value for key, value in desired.items()):
        raise RuntimeError("Endpoint verification failed: requested configuration was not preserved.")
    bot_scheme = next(
        scheme["type"] for scheme in desired["authorization_schemes"]
        if scheme["type"] in ("BotServiceRbac", "BotServiceTenant")
    )
    print(f"M365 Activity endpoint enabled and verified with {bot_scheme}.")
    print("Bot creation, Teams publishing and tenant approval remain separate steps.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-endpoint", required=True)
    parser.add_argument("--agent-name", required=True)
    args = parser.parse_args()
    try:
        configure(args.project_endpoint, args.agent_name)
    except subprocess.CalledProcessError as error:
        parser.exit(1, f"Azure CLI failed ({error.returncode}): {error.stderr}\n")
    except (ValueError, KeyError, TypeError, RuntimeError) as error:
        parser.exit(1, f"Endpoint configuration failed: {error}\n")


if __name__ == "__main__":
    main()
