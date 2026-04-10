import os


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-me")
    SQLALCHEMY_DATABASE_URI = os.environ["DATABASE_URL"].replace("postgres://", "postgresql://", 1) if "DATABASE_URL" in os.environ else "postgresql://localhost/wikihub"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    TEMPLATES_AUTO_RELOAD = True
    REPOS_DIR = os.environ.get("REPOS_DIR", os.path.join(os.path.dirname(__file__), "repos"))
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
    SERVER_NAME = os.environ.get("SERVER_NAME")  # e.g. wikihub.md
    BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")
    ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB max request size
    MAX_PAGE_SIZE = 2 * 1024 * 1024  # 2MB per page
    MAX_UPLOAD_FILES = 500  # max files in a single upload/zip
    MAX_WIKIS_PER_USER = 50
    TESTING_LOGIN = os.environ.get("TESTING_LOGIN", "").lower() in ("1", "true", "yes")
