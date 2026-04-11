"""
Curator Agent: inline AI sidebar for wiki curation and navigation.

SSE streaming endpoint that runs a Claude-powered coding agent with
filesystem access scoped to a temp working directory containing cloned
wiki repos.
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

import anthropic
from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context

from app.auth_utils import get_current_user_from_request
from app.git_sync import list_files_in_repo, read_file_from_repo

agent_chat_bp = Blueprint("agent_chat", __name__)

# In-memory session store: conversation_id -> session dict
_sessions = {}
_sessions_lock = threading.Lock()

# Session TTL in seconds (30 minutes)
SESSION_TTL = 30 * 60

# Max conversation turns to keep
MAX_HISTORY = 40


def _cleanup_expired():
    """Remove sessions older than SESSION_TTL."""
    now = time.time()
    with _sessions_lock:
        expired = [
            cid for cid, s in _sessions.items()
            if now - s["last_used"] > SESSION_TTL
        ]
        for cid in expired:
            work_dir = _sessions[cid].get("work_dir")
            if work_dir and os.path.isdir(work_dir):
                shutil.rmtree(work_dir, ignore_errors=True)
            del _sessions[cid]


def _repo_path(repos_dir, username, slug):
    """Return filesystem path for a wiki's bare repo."""
    safe_user = "".join(c for c in username if c.isalnum() or c in "-_")
    safe_slug = "".join(c for c in slug if c.isalnum() or c in "-_")
    return os.path.join(repos_dir, safe_user, f"{safe_slug}.git")


def _clone_wiki(repos_dir, owner, slug, work_dir):
    """Clone a wiki bare repo into the working directory. Returns clone path."""
    bare_repo = _repo_path(repos_dir, owner, slug)
    if not os.path.isdir(bare_repo):
        return None
    clone_dest = os.path.join(work_dir, owner, slug)
    os.makedirs(os.path.dirname(clone_dest), exist_ok=True)
    try:
        subprocess.run(
            ["git", "clone", bare_repo, clone_dest],
            check=True, capture_output=True, timeout=30,
        )
        # Configure git user for commits
        subprocess.run(
            ["git", "config", "user.name", "Curator"],
            cwd=clone_dest, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "curator@wikihub"],
            cwd=clone_dest, check=True, capture_output=True,
        )
        return clone_dest
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def _commit_and_push(wiki_dir, message="Curator edit"):
    """Stage all changes, commit, and push back to the bare repo."""
    try:
        # Check if there are changes
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=wiki_dir, capture_output=True, text=True, timeout=10,
        )
        if not status.stdout.strip():
            return False, "No changes to commit"

        subprocess.run(
            ["git", "add", "-A"],
            cwd=wiki_dir, check=True, capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=wiki_dir, check=True, capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "push"],
            cwd=wiki_dir, check=True, capture_output=True, timeout=30,
        )
        return True, "Changes committed and pushed"
    except subprocess.CalledProcessError as e:
        return False, f"Git error: {e.stderr.decode() if e.stderr else str(e)}"
    except subprocess.TimeoutExpired:
        return False, "Git operation timed out"


# --- Tool implementations ---

def _tool_read_file(work_dir, path):
    """Read a file from the working directory."""
    full = os.path.normpath(os.path.join(work_dir, path))
    if not full.startswith(work_dir):
        return "Error: path escapes working directory"
    if not os.path.isfile(full):
        return f"Error: file not found: {path}"
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        # Truncate very large files
        if len(content) > 50000:
            content = content[:50000] + "\n\n[... truncated, file is very large ...]"
        return content
    except Exception as e:
        return f"Error reading file: {e}"


def _tool_write_file(work_dir, path, content):
    """Write a file in the working directory."""
    full = os.path.normpath(os.path.join(work_dir, path))
    if not full.startswith(work_dir):
        return "Error: path escapes working directory"
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Written {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error writing file: {e}"


def _tool_list_files(work_dir, directory=""):
    """List files in a directory within the working directory."""
    full = os.path.normpath(os.path.join(work_dir, directory))
    if not full.startswith(work_dir):
        return "Error: path escapes working directory"
    if not os.path.isdir(full):
        return f"Error: directory not found: {directory}"
    try:
        entries = []
        for entry in sorted(os.listdir(full)):
            if entry.startswith(".git") and entry != ".gitkeep":
                continue
            entry_path = os.path.join(full, entry)
            kind = "dir" if os.path.isdir(entry_path) else "file"
            rel = os.path.relpath(entry_path, work_dir)
            entries.append(f"{kind}\t{rel}")
        return "\n".join(entries) if entries else "(empty directory)"
    except Exception as e:
        return f"Error listing directory: {e}"


def _tool_search_content(work_dir, query):
    """Search for content across files in the working directory."""
    try:
        result = subprocess.run(
            ["grep", "-r", "-n", "-i", "--include=*.md", "--include=*.txt",
             "--include=*.yaml", "--include=*.yml", "--include=*.json",
             query, "."],
            cwd=work_dir, capture_output=True, text=True, timeout=10,
        )
        output = result.stdout.strip()
        if not output:
            return f"No matches found for: {query}"
        # Truncate long results
        lines = output.split("\n")
        if len(lines) > 50:
            output = "\n".join(lines[:50]) + f"\n\n[... {len(lines) - 50} more matches ...]"
        return output
    except subprocess.TimeoutExpired:
        return "Search timed out"
    except Exception as e:
        return f"Search error: {e}"


def _tool_wikihub_api(base_url, auth_token, method, endpoint, body=None):
    """Proxy a call to WikiHub's own API."""
    import requests
    url = f"{base_url}/api/v1{endpoint}"
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    try:
        resp = requests.request(
            method.upper(), url, headers=headers,
            json=body if body else None, timeout=15,
        )
        try:
            return json.dumps(resp.json(), indent=2)
        except Exception:
            return resp.text[:2000]
    except Exception as e:
        return f"API call failed: {e}"


TOOLS = [
    {
        "name": "read_file",
        "description": "Read a file from the working directory. Path is relative to the working directory root (e.g. 'owner/wiki/page.md').",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file in the working directory. Creates parent directories as needed. After writing, changes are auto-committed and pushed to WikiHub.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path"},
                "content": {"type": "string", "description": "Full file content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_files",
        "description": "List files and directories in the working directory. Returns entries with 'file' or 'dir' prefix.",
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Directory path relative to working dir root (empty string for root)",
                    "default": "",
                }
            },
            "required": [],
        },
    },
    {
        "name": "search_content",
        "description": "Search for text across all markdown, text, and config files in the working directory. Case-insensitive grep.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (plain text or regex)"}
            },
            "required": ["query"],
        },
    },
    {
        "name": "wikihub_api",
        "description": "Call WikiHub's REST API. Use for operations like starring, forking, or reading wiki metadata. Endpoint should start with / (e.g. '/wikis/owner/slug').",
        "input_schema": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                "endpoint": {"type": "string", "description": "API endpoint path (e.g. '/wikis/owner/slug/pages')"},
                "body": {
                    "type": "object",
                    "description": "Request body for POST/PUT/PATCH",
                    "default": None,
                },
            },
            "required": ["method", "endpoint"],
        },
    },
]


def _build_system_prompt(username, owner, wiki_slug, page_path, page_content, page_list):
    """Build the system prompt with session context."""
    files_section = "\n".join(f"  - {f}" for f in page_list[:100]) if page_list else "  (no pages)"
    if len(page_list) > 100:
        files_section += f"\n  ... and {len(page_list) - 100} more"

    return f"""You are the Curator, WikiHub's AI librarian. You help users navigate, organize, and curate wiki content.

## Current session
- User: @{username}
- Viewing: /@{owner}/{wiki_slug}/{page_path}
- Working directory contains cloned wiki repos

## Working directory layout
- {owner}/{wiki_slug}/  — the wiki being viewed

## Wiki pages
{files_section}

## Current page content ({page_path})
```markdown
{page_content[:8000] if page_content else '(empty or not found)'}
```

## How you work
- Use read_file and list_files to explore the wiki content
- Use write_file to create or edit pages (changes are auto-committed and pushed)
- Use search_content to find information across the wiki
- Use wikihub_api for metadata operations (starring, forking, reading wiki info)
- File paths are relative to the working directory root: {owner}/{wiki_slug}/filename.md

## Guidelines
- Be helpful, concise, and take direct action when asked
- When editing pages, follow the wiki's schema.md if one exists
- Explain what you did after making changes
- Use wikilinks ([[page-name]]) when referencing other wiki pages
- Respect the wiki's existing structure and conventions"""


def _execute_tool(tool_name, tool_input, session):
    """Execute a tool call and return the result string."""
    work_dir = session["work_dir"]

    if tool_name == "read_file":
        return _tool_read_file(work_dir, tool_input["path"])
    elif tool_name == "write_file":
        result = _tool_write_file(work_dir, tool_input["path"], tool_input["content"])
        # Auto-commit and push after writes
        wiki_dir = None
        path_parts = tool_input["path"].split("/")
        if len(path_parts) >= 2:
            wiki_dir = os.path.join(work_dir, path_parts[0], path_parts[1])
        if wiki_dir and os.path.isdir(os.path.join(wiki_dir, ".git")):
            summary = tool_input["path"].split("/")[-1]
            ok, msg = _commit_and_push(wiki_dir, f"Curator: update {summary}")
            result += f"\n{msg}"
        return result
    elif tool_name == "list_files":
        return _tool_list_files(work_dir, tool_input.get("directory", ""))
    elif tool_name == "search_content":
        return _tool_search_content(work_dir, tool_input["query"])
    elif tool_name == "wikihub_api":
        return _tool_wikihub_api(
            session["base_url"],
            session.get("auth_token"),
            tool_input["method"],
            tool_input["endpoint"],
            tool_input.get("body"),
        )
    else:
        return f"Unknown tool: {tool_name}"


def _sse_event(data):
    """Format a dict as an SSE data line."""
    return f"data: {json.dumps(data)}\n\n"


@agent_chat_bp.route("/agent/chat", methods=["POST"])
def agent_chat():
    """SSE streaming chat endpoint for the Curator agent."""
    # Auth: accept Bearer token or Flask-Login session
    user = get_current_user_from_request()
    if not user:
        return {"error": "unauthorized", "message": "Authentication required"}, 401

    data = request.get_json(silent=True)
    if not data or "message" not in data:
        return {"error": "bad_request", "message": "Missing 'message' field"}, 400

    message = data["message"].strip()
    if not message:
        return {"error": "bad_request", "message": "Empty message"}, 400

    conversation_id = data.get("conversation_id")
    context = data.get("context", {})
    owner = context.get("owner", "")
    wiki_slug = context.get("wiki", "")
    page_path = context.get("page_path", "")

    # Get API key from the auth header (for wikihub_api tool proxying)
    auth_header = request.headers.get("Authorization", "")
    auth_token = auth_header[7:] if auth_header.startswith("Bearer ") else None

    # Clean up expired sessions periodically
    _cleanup_expired()

    repos_dir = current_app.config["REPOS_DIR"]
    base_url = request.host_url.rstrip("/")

    # Get or create session
    session = None
    if conversation_id:
        with _sessions_lock:
            session = _sessions.get(conversation_id)

    if session is None:
        # New session: create work dir, clone repo
        conversation_id = str(uuid.uuid4())
        work_dir = tempfile.mkdtemp(prefix="curator-")

        clone_path = None
        if owner and wiki_slug:
            clone_path = _clone_wiki(repos_dir, owner, wiki_slug, work_dir)

        # Read current page content and file list for system prompt
        page_content = ""
        page_list = []
        if owner and wiki_slug:
            page_content = read_file_from_repo(owner, wiki_slug, page_path) or ""
            page_list = list_files_in_repo(owner, wiki_slug)

        system_prompt = _build_system_prompt(
            user.username, owner, wiki_slug, page_path, page_content, page_list,
        )

        session = {
            "conversation_id": conversation_id,
            "work_dir": work_dir,
            "clone_path": clone_path,
            "messages": [],
            "system_prompt": system_prompt,
            "last_used": time.time(),
            "base_url": base_url,
            "auth_token": auth_token,
            "owner": owner,
            "wiki_slug": wiki_slug,
        }
        with _sessions_lock:
            _sessions[conversation_id] = session
    else:
        session["last_used"] = time.time()
        if auth_token:
            session["auth_token"] = auth_token

    # Add user message to history
    session["messages"].append({"role": "user", "content": message})

    # Trim history if too long
    if len(session["messages"]) > MAX_HISTORY:
        session["messages"] = session["messages"][-MAX_HISTORY:]

    # Get model from env
    model = os.environ.get("CURATOR_MODEL", "claude-sonnet-4-20250514")

    # Check for auth: per-user API key, Claude subscription, or server env var
    from app.routes.main import get_user_llm_key
    api_key = get_user_llm_key(user) or os.environ.get("ANTHROPIC_API_KEY")

    # Check for Claude subscription credentials if no API key
    # 1. Per-user Claude config
    # 2. Server-wide Claude config (shared login for all users)
    for config_dir in [_claude_config_dir(user.id), SERVER_CONFIG_DIR]:
        if api_key:
            break
        creds_file = os.path.join(config_dir, ".credentials.json")
        if os.path.exists(creds_file):
            try:
                with open(creds_file) as f:
                    creds = json.load(f)
                oauth = creds.get("claudeAiOauth", {})
                api_key = oauth.get("accessToken")
            except Exception:
                pass

    if not api_key:
        return {"error": "config_error", "message": "Login with your Claude subscription or add an API key in Settings"}, 400

    def generate():
        client = anthropic.Anthropic(api_key=api_key)

        # Send conversation_id first
        yield _sse_event({"conversation_id": conversation_id})

        messages = list(session["messages"])

        # Agentic loop: keep going while the model wants to use tools
        max_iterations = 10
        for _iteration in range(max_iterations):
            try:
                # Stream the response
                full_text = ""
                tool_uses = []
                current_tool = None

                with client.messages.stream(
                    model=model,
                    max_tokens=4096,
                    system=session["system_prompt"],
                    messages=messages,
                    tools=TOOLS,
                ) as stream:
                    for event in stream:
                        if event.type == "content_block_start":
                            if event.content_block.type == "text":
                                pass  # text streaming handled by deltas
                            elif event.content_block.type == "tool_use":
                                current_tool = {
                                    "id": event.content_block.id,
                                    "name": event.content_block.name,
                                    "input_json": "",
                                }
                                yield _sse_event({
                                    "type": "tool_use",
                                    "name": event.content_block.name,
                                    "input": {},
                                })

                        elif event.type == "content_block_delta":
                            if event.delta.type == "text_delta":
                                full_text += event.delta.text
                                yield _sse_event({
                                    "type": "text",
                                    "content": event.delta.text,
                                })
                            elif event.delta.type == "input_json_delta":
                                if current_tool:
                                    current_tool["input_json"] += event.delta.partial_json

                        elif event.type == "content_block_stop":
                            if current_tool:
                                try:
                                    parsed_input = json.loads(current_tool["input_json"]) if current_tool["input_json"] else {}
                                except json.JSONDecodeError:
                                    parsed_input = {}
                                current_tool["parsed_input"] = parsed_input
                                tool_uses.append(current_tool)
                                # Send updated tool_use with parsed input
                                yield _sse_event({
                                    "type": "tool_use",
                                    "name": current_tool["name"],
                                    "input": parsed_input,
                                })
                                current_tool = None

                    # Get the final message for stop reason
                    final_message = stream.get_final_message()
                    stop_reason = final_message.stop_reason

            except anthropic.APIError as e:
                yield _sse_event({"type": "error", "content": f"API error: {e.message}"})
                break

            # Build the assistant message content for history
            assistant_content = []
            if full_text:
                assistant_content.append({"type": "text", "text": full_text})
            for tu in tool_uses:
                assistant_content.append({
                    "type": "tool_use",
                    "id": tu["id"],
                    "name": tu["name"],
                    "input": tu["parsed_input"],
                })

            if assistant_content:
                messages.append({"role": "assistant", "content": assistant_content})

            # If there are tool uses, execute them and continue
            if tool_uses and stop_reason == "tool_use":
                tool_results = []
                for tu in tool_uses:
                    result = _execute_tool(tu["name"], tu["parsed_input"], session)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": result,
                    })
                    yield _sse_event({
                        "type": "tool_result",
                        "name": tu["name"],
                        "content": result[:1000],
                    })

                messages.append({"role": "user", "content": tool_results})
            else:
                # No more tool calls, we're done
                break

        # Save messages back to session
        session["messages"] = messages
        yield _sse_event({"type": "done"})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# --- Claude subscription auth ---

def _claude_config_dir(user_id):
    """Per-user Claude config directory."""
    base = os.environ.get("CLAUDE_USER_CONFIG_DIR", "/tmp/claude-configs")
    d = os.path.join(base, str(user_id))
    os.makedirs(d, exist_ok=True)
    return d


@agent_chat_bp.route("/agent/claude-auth/status", methods=["GET"])
def claude_auth_status():
    """Check if user has Claude subscription credentials."""
    from flask_login import current_user
    user = getattr(request, "current_user", None)
    if not user:
        if current_user and current_user.is_authenticated:
            user = current_user
    if not user:
        return {"error": "unauthorized"}, 401

    config_dir = _claude_config_dir(user.id)
    try:
        result = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "CLAUDE_CONFIG_DIR": config_dir},
        )
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        return jsonify(data)
    except Exception as e:
        return jsonify({"loggedIn": False, "error": str(e)})


@agent_chat_bp.route("/agent/claude-auth/login", methods=["POST"])
def claude_auth_login():
    """Start Claude login flow. Returns SSE stream with auth URL."""
    from flask_login import current_user
    user = getattr(request, "current_user", None)
    if not user:
        if current_user and current_user.is_authenticated:
            user = current_user
    if not user:
        return {"error": "unauthorized"}, 401

    config_dir = _claude_config_dir(user.id)

    def generate():
        try:
            proc = subprocess.Popen(
                ["claude", "auth", "login"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                env={**os.environ, "CLAUDE_CONFIG_DIR": config_dir},
            )
            for line in iter(proc.stdout.readline, ""):
                yield _sse_event({"type": "output", "text": line.rstrip("\n")})
                # Check if auth completed (credentials file appeared)
                creds_file = os.path.join(config_dir, ".credentials.json")
                if os.path.exists(creds_file):
                    yield _sse_event({"type": "output", "text": "Authenticated successfully!"})
                    yield _sse_event({"type": "done", "success": True})
                    proc.terminate()
                    return

            proc.wait(timeout=5)
            # Check one more time
            creds_file = os.path.join(config_dir, ".credentials.json")
            if os.path.exists(creds_file):
                yield _sse_event({"type": "done", "success": True})
            else:
                yield _sse_event({"type": "done", "success": False})
        except Exception as e:
            yield _sse_event({"type": "error", "message": str(e)})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@agent_chat_bp.route("/agent/claude-auth/logout", methods=["POST"])
def claude_auth_logout():
    """Remove Claude subscription credentials."""
    from flask_login import current_user
    user = getattr(request, "current_user", None)
    if not user:
        if current_user and current_user.is_authenticated:
            user = current_user
    if not user:
        return {"error": "unauthorized"}, 401

    config_dir = _claude_config_dir(user.id)
    creds_file = os.path.join(config_dir, ".credentials.json")
    if os.path.exists(creds_file):
        os.unlink(creds_file)
    return jsonify({"loggedIn": False})


# --- Admin Claude auth (server-wide, token-protected) ---

# Background login process state
_admin_login = {"proc": None, "output": [], "status": "idle", "lock": threading.Lock()}

SERVER_CONFIG_DIR = "/opt/wikihub-app/claude-config"


def _check_admin_token(req):
    token = req.args.get("token") or req.headers.get("X-Admin-Token") or ""
    expected = current_app.config.get("ADMIN_TOKEN", "")
    return bool(expected and token == expected)


@agent_chat_bp.route("/admin/claude-auth", methods=["GET"])
def admin_claude_auth_page():
    """Admin page for server-wide Claude auth. Token required."""
    from flask import render_template_string
    return render_template_string(ADMIN_AUTH_PAGE)


@agent_chat_bp.route("/admin/claude-auth/status", methods=["GET"])
def admin_claude_auth_status():
    if not _check_admin_token(request):
        return {"error": "unauthorized"}, 401
    os.makedirs(SERVER_CONFIG_DIR, exist_ok=True)
    try:
        result = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "CLAUDE_CONFIG_DIR": SERVER_CONFIG_DIR},
        )
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        return jsonify(data)
    except Exception as e:
        return jsonify({"loggedIn": False, "error": str(e)})


@agent_chat_bp.route("/admin/claude-auth/start", methods=["POST"])
def admin_claude_auth_start():
    """Start the claude auth login process in background."""
    if not _check_admin_token(request):
        return {"error": "unauthorized"}, 401

    with _admin_login["lock"]:
        # Kill any existing process
        if _admin_login["proc"] and _admin_login["proc"].poll() is None:
            _admin_login["proc"].terminate()

        _admin_login["output"] = []
        _admin_login["status"] = "waiting"
        os.makedirs(SERVER_CONFIG_DIR, exist_ok=True)

        proc = subprocess.Popen(
            ["claude", "auth", "login"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            env={**os.environ, "CLAUDE_CONFIG_DIR": SERVER_CONFIG_DIR},
        )
        _admin_login["proc"] = proc

        # Reader thread — collects output without blocking
        def reader():
            for line in iter(proc.stdout.readline, ""):
                _admin_login["output"].append(line.rstrip("\n"))
                # Check if credentials appeared
                if os.path.exists(os.path.join(SERVER_CONFIG_DIR, ".credentials.json")):
                    _admin_login["status"] = "success"
                    proc.terminate()
                    return
            proc.wait()
            if os.path.exists(os.path.join(SERVER_CONFIG_DIR, ".credentials.json")):
                _admin_login["status"] = "success"
            else:
                _admin_login["status"] = "failed"

        t = threading.Thread(target=reader, daemon=True)
        t.start()

    return jsonify({"started": True})


@agent_chat_bp.route("/admin/claude-auth/poll", methods=["GET"])
def admin_claude_auth_poll():
    """Poll for login output. Returns accumulated lines and status."""
    if not _check_admin_token(request):
        return {"error": "unauthorized"}, 401
    return jsonify({
        "output": _admin_login["output"],
        "status": _admin_login["status"],
    })


@agent_chat_bp.route("/admin/claude-auth/logout", methods=["POST"])
def admin_claude_auth_revoke():
    if not _check_admin_token(request):
        return {"error": "unauthorized"}, 401
    creds = os.path.join(SERVER_CONFIG_DIR, ".credentials.json")
    if os.path.exists(creds):
        os.unlink(creds)
    return jsonify({"loggedIn": False})


ADMIN_AUTH_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WikiHub Admin — Claude Auth</title>
<style>
  :root { --bg: #0f0e0c; --surface: #191714; --border: rgba(200,180,150,0.12); --text: #e8e0d4; --muted: #887d6e; --accent: #d4a04a; --success: #7a9e6b; --error: #c45c3c; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'IBM Plex Mono', monospace; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 32px; max-width: 560px; width: 100%; }
  h1 { font-size: 1.25rem; margin-bottom: 8px; }
  .sub { color: var(--muted); font-size: 0.8125rem; margin-bottom: 24px; }
  label { display: block; font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 6px; }
  input { width: 100%; padding: 10px 12px; background: rgba(0,0,0,0.3); border: 1px solid var(--border); border-radius: 4px; color: var(--text); font-family: inherit; font-size: 0.875rem; margin-bottom: 16px; }
  input:focus { outline: none; border-color: var(--accent); }
  button { padding: 10px 20px; background: var(--accent); color: #0f0e0c; border: none; border-radius: 4px; font-family: inherit; font-weight: 600; cursor: pointer; font-size: 0.875rem; }
  button:hover { opacity: 0.9; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  button.secondary { background: transparent; color: var(--error); border: 1px solid rgba(196,92,60,0.3); }
  .status { margin: 16px 0 8px; font-size: 0.8125rem; }
  .status.success { color: var(--success); }
  .status.error { color: var(--error); }
  .terminal { background: #0a0a08; border: 1px solid var(--border); border-radius: 4px; padding: 12px; font-size: 0.75rem; color: var(--success); max-height: 200px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; margin: 12px 0; display: none; }
  .terminal a { color: var(--accent); }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; }
  #auth-section { display: none; }
</style>
</head>
<body>
<div class="card">
  <h1>[[wikihub]] Claude Auth</h1>
  <p class="sub">Server-wide Claude subscription for the Curator agent.</p>

  <div id="token-section">
    <label>Admin Token</label>
    <input type="password" id="token" placeholder="Enter admin token..." autocomplete="off">
    <button onclick="authenticate()">Authenticate</button>
    <div id="token-error" class="status error" style="display:none;"></div>
  </div>

  <div id="auth-section">
    <div id="current-status" class="status">Checking...</div>
    <div class="terminal" id="terminal"></div>
    <div class="actions">
      <button id="login-btn" onclick="startLogin()">Login with Claude</button>
      <button id="logout-btn" class="secondary" onclick="logout()" style="display:none;">Disconnect</button>
    </div>
  </div>
</div>

<script>
let token = '';
let pollInterval = null;

async function authenticate() {
  token = document.getElementById('token').value.trim();
  if (!token) return;
  const resp = await fetch('/api/v1/admin/claude-auth/status?token=' + encodeURIComponent(token));
  if (resp.status === 401) {
    document.getElementById('token-error').style.display = 'block';
    document.getElementById('token-error').textContent = 'Invalid token';
    return;
  }
  document.getElementById('token-section').style.display = 'none';
  document.getElementById('auth-section').style.display = 'block';
  const data = await resp.json();
  updateStatus(data);
}

function updateStatus(data) {
  const el = document.getElementById('current-status');
  const loginBtn = document.getElementById('login-btn');
  const logoutBtn = document.getElementById('logout-btn');
  if (data.loggedIn) {
    el.textContent = 'Connected — Claude subscription active';
    el.className = 'status success';
    loginBtn.style.display = 'none';
    logoutBtn.style.display = 'inline-block';
  } else {
    el.textContent = 'Not connected';
    el.className = 'status';
    loginBtn.style.display = 'inline-block';
    logoutBtn.style.display = 'none';
  }
}

async function startLogin() {
  const term = document.getElementById('terminal');
  const btn = document.getElementById('login-btn');
  term.style.display = 'block';
  term.innerHTML = 'Starting login...\\n';
  btn.disabled = true;
  btn.textContent = 'Waiting for auth...';

  await fetch('/api/v1/admin/claude-auth/start?token=' + encodeURIComponent(token), {method: 'POST'});

  // Poll for output
  let lastLen = 0;
  pollInterval = setInterval(async () => {
    const resp = await fetch('/api/v1/admin/claude-auth/poll?token=' + encodeURIComponent(token));
    if (!resp.ok) { term.innerHTML += 'Poll error: ' + resp.status + '\\n'; return; }
    const data = await resp.json();

    if (data.output && data.output.length > lastLen) {
      for (let i = lastLen; i < data.output.length; i++) {
        const line = data.output[i].replace(/(https:\\/\\/[^\\s]+)/g, '<a href="$1" target="_blank">$1</a>');
        term.innerHTML += line + '\\n';
      }
      lastLen = data.output.length;
      term.scrollTop = term.scrollHeight;
    }

    if (data.status === 'success') {
      clearInterval(pollInterval);
      term.innerHTML += '\\n<span style="color:var(--success);">Login successful!</span>\\n';
      btn.disabled = false;
      btn.textContent = 'Login with Claude';
      // Refresh status
      const sr = await fetch('/api/v1/admin/claude-auth/status?token=' + encodeURIComponent(token));
      updateStatus(await sr.json());
    } else if (data.status === 'failed') {
      clearInterval(pollInterval);
      term.innerHTML += '\\n<span style="color:var(--error);">Login failed or timed out.</span>\\n';
      btn.disabled = false;
      btn.textContent = 'Login with Claude';
    }
  }, 1500);
}

async function logout() {
  await fetch('/api/v1/admin/claude-auth/logout?token=' + encodeURIComponent(token), {method: 'POST'});
  const resp = await fetch('/api/v1/admin/claude-auth/status?token=' + encodeURIComponent(token));
  updateStatus(await resp.json());
  document.getElementById('terminal').style.display = 'none';
}

// Allow Enter to submit token
document.getElementById('token').addEventListener('keydown', e => {
  if (e.key === 'Enter') authenticate();
});
</script>
</body>
</html>"""
