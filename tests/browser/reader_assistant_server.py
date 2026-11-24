"""Dev server for tests/browser/reader_assistant.mjs (wikihub-1bfx).

Runs the normal app with the reader assistant enabled and the scripted
FakeReaderModel in place of the Anthropic client, so the browser check is
deterministic and needs no key. Never used in production.

    DATABASE_URL=... REPOS_DIR=... SECRET_KEY=dev \\
      python3 tests/browser/reader_assistant_server.py --port 7xxx
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.dirname(HERE))

from app import create_app, db  # noqa: E402
from reader_assistant_fake import FakeReaderModel  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    os.environ.setdefault("SESSION_COOKIE_SECURE", "0")
    app = create_app()
    app.config["READER_ASSISTANT_ENABLED"] = True
    app.config["READER_ASSISTANT_CLIENT_FACTORY"] = lambda: FakeReaderModel(
        probe_paths=["private-notes.md", ".wikihub/acl"],
        extra_quotes=["BANDSECRET owner only reminder"],
    )
    app.config["TRUST_GATE_ENABLED"] = False
    with app.app_context():
        db.create_all()
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
