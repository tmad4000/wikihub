import os

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager

db = SQLAlchemy()
login_manager = LoginManager()


def create_app(config_class="config.Config"):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    from app.routes import main_bp, auth_bp, api_bp, wiki_bp
    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp, url_prefix="/api/v1")
    app.register_blueprint(wiki_bp)

    from app.git_backend import git_bp
    app.register_blueprint(git_bp)

    from app.routes.auth import init_oauth
    init_oauth(app)

    os.makedirs(app.config["REPOS_DIR"], exist_ok=True)

    return app
