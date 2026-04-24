"""
/api root discovery endpoint.

Lives outside the /api/v1 prefix so agents hitting /api (naturally, to
discover versioning) get a tiny JSON map pointing at the current version
and its capability surfaces. No auth required; safe to cache.
"""

from flask import Blueprint, jsonify

api_root_bp = Blueprint("api_root", __name__)


def _discovery_payload():
    return {
        "name": "wikihub",
        "current_version": "v1",
        "versions": {
            "v1": {
                "base": "/api/v1",
                "openapi": "/api/v1/openapi.json",
                "capabilities": "/api/v1/me/capabilities",
                "docs": "/docs/api",
            },
        },
        "deprecated_versions": [],
        "feedback": "/api/v1/feedback",
        "request_id_header": "X-Request-ID",
    }


@api_root_bp.route("/api", methods=["GET", "HEAD"])
@api_root_bp.route("/api/", methods=["GET", "HEAD"])
def api_discovery():
    resp = jsonify(_discovery_payload())
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp
