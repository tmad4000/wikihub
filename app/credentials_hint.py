"""
client credentials hint.

agent-first convention: after signup (or after fetching a key via
password), the agent should write its credentials to a well-known
local file so other agents on the same machine can read them without
the human having to copy-paste the key.

path:   ~/.wikihub/credentials.json
mode:   0600
format:
    {
      "default": {
        "server":   "https://wikihub.md",
        "username": "jacob",
        "api_key":  "wh_..."
      }
    }

responses from account creation and token endpoints include a
`client_config` object with the exact content to save and instructions
for merging into an existing file.
"""

CREDENTIALS_PATH = "~/.wikihub/credentials.json"
FILE_MODE = "0600"
DEFAULT_PROFILE = "default"


def build_client_config(username, api_key, server_url, profile=DEFAULT_PROFILE):
    """return the `client_config` blob to embed in auth responses.

    the blob tells an agent exactly where to save the credentials file
    and what to put in it. includes pure-shell and pure-python read
    snippets so future agents can fetch the key without any wikihub
    tooling installed.
    """
    profile_block = {
        "server": server_url,
        "username": username,
        "api_key": api_key,
    }
    return {
        "path": CREDENTIALS_PATH,
        "mode": FILE_MODE,
        "profile": profile,
        "content": {profile: profile_block},
        "save_instruction": (
            f"Save as JSON at {CREDENTIALS_PATH} (mode {FILE_MODE}). "
            f"If the file exists, merge `content` into the existing JSON "
            f"(add/update the '{profile}' profile, keep other profiles)."
        ),
        "read_snippets": {
            "shell": f"jq -r .{profile}.api_key {CREDENTIALS_PATH}",
            "python": (
                "import json, os; "
                f"api_key = json.load(open(os.path.expanduser("
                f"'{CREDENTIALS_PATH}')))['{profile}']['api_key']"
            ),
            "curl": (
                f"curl -H \"Authorization: Bearer $(jq -r .{profile}.api_key "
                f"{CREDENTIALS_PATH})\" {server_url}/api/v1/accounts/me"
            ),
        },
        "env_alternative": {
            "WIKIHUB_SERVER": server_url,
            "WIKIHUB_USERNAME": username,
            "WIKIHUB_API_KEY": api_key,
        },
    }


def resolve_server_url(app, request):
    """server URL to embed in credentials file.

    prefer configured BASE_URL, fall back to the request's url_root.
    """
    configured = (app.config.get("BASE_URL") or "").rstrip("/")
    if configured and not configured.startswith("http://localhost"):
        return configured
    fallback = (request.url_root or "").rstrip("/")
    return fallback or configured or ""
