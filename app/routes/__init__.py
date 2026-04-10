from flask import Blueprint

main_bp = Blueprint("main", __name__)
auth_bp = Blueprint("auth", __name__, url_prefix="/auth")
api_bp = Blueprint("api", __name__)
wiki_bp = Blueprint("wiki", __name__)

from app.routes import main, auth, api, api_wikis, wiki  # noqa: E402, F401
