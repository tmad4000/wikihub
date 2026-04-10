"""
git Smart HTTP backend for wikihub.

provides HTTPS clone/pull/push for per-wiki bare repos.
each wiki has two repos:
  - repos/<user>/<slug>.git        (authoritative, owner only)
  - repos/<user>/<slug>-public.git (derived public mirror, non-owners)

ported from listhub's git_backend.py with multi-wiki path generalization.

auth dispatch:
  owner?  -> authoritative repo
  else?   -> public mirror (read-only)
"""

import hashlib
import os
import shutil
import stat
import subprocess

import bcrypt
from flask import Blueprint, request, Response, abort, current_app

from app import db
from app.models import User, ApiKey

git_bp = Blueprint("git", __name__, url_prefix="/git")

HOOK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks")


def _repo_path(username, slug, public=False):
    """return filesystem path for a wiki's bare repo."""
    repos_dir = current_app.config["REPOS_DIR"]
    safe_user = "".join(c for c in username if c.isalnum() or c in "-_")
    safe_slug = "".join(c for c in slug if c.isalnum() or c in "-_")
    suffix = "-public" if public else ""
    return os.path.join(repos_dir, safe_user, f"{safe_slug}{suffix}.git")


def _hash_api_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


def _check_basic_auth():
    """validate HTTP Basic Auth. accepts password or API key."""
    auth = request.authorization
    if not auth or not auth.username or not auth.password:
        return None

    user = User.query.filter_by(username=auth.username).first()
    if not user:
        return None

    # try bcrypt password
    if user.password_hash:
        try:
            if bcrypt.checkpw(auth.password.encode(), user.password_hash.encode()):
                return user
        except Exception:
            pass

    # try API key
    key_hash = _hash_api_key(auth.password)
    api_key = ApiKey.query.filter_by(key_hash=key_hash, user_id=user.id).first()
    if api_key:
        return user

    return None


def _require_auth_401():
    return Response(
        "Authentication required\n",
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="wikihub Git"'},
    )


def _git_command_path(cmd):
    """find full path to a git sub-command binary."""
    result = subprocess.run(["git", "--exec-path"], capture_output=True, text=True)
    exec_path = result.stdout.strip()
    candidate = os.path.join(exec_path, cmd)
    if os.path.isfile(candidate):
        return candidate
    return cmd


def _run_git_service(cmd, repo, content_type):
    """run a git service command in stateless-rpc mode."""
    input_data = request.get_data()
    proc = subprocess.Popen(
        [cmd, "--stateless-rpc", repo],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = proc.communicate(input=input_data)

    if proc.returncode != 0 and not stdout:
        return Response(f"Git error: {stderr.decode()}\n", status=500)

    return Response(
        stdout,
        status=200,
        content_type=content_type,
        headers={"Cache-Control": "no-cache"},
    )


def init_wiki_repo(username, slug):
    """create authoritative + public mirror bare repos for a wiki.
    installs post-receive hook on the authoritative repo."""
    auth_repo = _repo_path(username, slug)
    pub_repo = _repo_path(username, slug, public=True)

    for repo in (auth_repo, pub_repo):
        if not os.path.isdir(repo):
            os.makedirs(repo, exist_ok=True)
            subprocess.run(["git", "init", "--bare", repo], check=True, capture_output=True)
            subprocess.run(
                ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                cwd=repo, check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "config", "http.receivepack", "true"],
                cwd=repo, check=True, capture_output=True,
            )

    _install_hook(auth_repo, username, slug)
    return auth_repo


def _install_hook(repo_path, username, slug):
    """install post-receive hook into the authoritative bare repo."""
    hooks_dir = os.path.join(repo_path, "hooks")
    os.makedirs(hooks_dir, exist_ok=True)

    hook_src = os.path.join(HOOK_DIR, "post-receive")
    hook_dst = os.path.join(hooks_dir, "post-receive")

    if os.path.isfile(hook_src):
        shutil.copy2(hook_src, hook_dst)
        st = os.stat(hook_dst)
        os.chmod(hook_dst, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    config_path = os.path.join(hooks_dir, "wikihub.conf")
    base_url = current_app.config.get("BASE_URL", os.environ.get("BASE_URL", "http://localhost:5000"))
    admin_token = current_app.config.get("ADMIN_TOKEN", os.environ.get("ADMIN_TOKEN", ""))
    with open(config_path, "w") as f:
        f.write(f"WIKIHUB_USERNAME={username}\n")
        f.write(f"WIKIHUB_SLUG={slug}\n")
        f.write(f"WIKIHUB_BASE_URL={base_url}\n")
        f.write(f"WIKIHUB_ADMIN_TOKEN={admin_token}\n")


# --- Smart HTTP routes ---
# pattern: /@<user>/<slug>.git/...


@git_bp.route("/@<username>/<slug>.git/info/refs")
def info_refs(username, slug):
    """smart HTTP discovery. dispatches to authoritative or public mirror based on auth."""
    service = request.args.get("service", "")
    if service not in ("git-upload-pack", "git-receive-pack"):
        abort(400)

    user = _check_basic_auth()
    is_owner = user and user.username == username

    # receive-pack (push) requires owner auth
    if service == "git-receive-pack":
        if not is_owner:
            return _require_auth_401()

    # dispatch: owner -> authoritative, others -> public mirror
    if is_owner:
        repo = _repo_path(username, slug)
    else:
        repo = _repo_path(username, slug, public=True)

    if not os.path.isdir(repo):
        if is_owner:
            init_wiki_repo(username, slug)
            repo = _repo_path(username, slug)
        else:
            abort(404)

    cmd = _git_command_path(service)
    proc = subprocess.Popen(
        [cmd, "--stateless-rpc", "--advertise-refs", repo],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = proc.communicate()

    if proc.returncode != 0:
        return Response(f"Git error: {stderr.decode()}\n", status=500)

    service_line = f"# service={service}\n"
    pkt_len = len(service_line) + 4
    pkt = f"{pkt_len:04x}{service_line}"
    body = pkt.encode() + b"0000" + stdout

    return Response(
        body,
        status=200,
        content_type=f"application/x-{service}-advertisement",
        headers={"Cache-Control": "no-cache"},
    )


@git_bp.route("/@<username>/<slug>.git/git-upload-pack", methods=["POST"])
def upload_pack(username, slug):
    """handle git clone / fetch / pull."""
    user = _check_basic_auth()
    is_owner = user and user.username == username

    repo = _repo_path(username, slug) if is_owner else _repo_path(username, slug, public=True)
    if not os.path.isdir(repo):
        abort(404)

    cmd = _git_command_path("git-upload-pack")
    return _run_git_service(cmd, repo, "application/x-git-upload-pack-result")


@git_bp.route("/@<username>/<slug>.git/git-receive-pack", methods=["POST"])
def receive_pack(username, slug):
    """handle git push. owner only."""
    user = _check_basic_auth()
    if not user or user.username != username:
        return _require_auth_401()

    repo = _repo_path(username, slug)
    if not os.path.isdir(repo):
        init_wiki_repo(username, slug)

    cmd = _git_command_path("git-receive-pack")
    return _run_git_service(cmd, repo, "application/x-git-receive-pack-result")
