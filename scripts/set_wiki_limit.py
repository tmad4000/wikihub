#!/usr/bin/env python3
"""
Set a per-user wiki cap override (wikihub-20ct).

Usage (from the app root, with the same env the app uses):

    source .venv/bin/activate
    DATABASE_URL=postgresql://localhost/wikihub \
        python3 scripts/set_wiki_limit.py <username> <limit>

    # clear the override (revert to the config default):
    DATABASE_URL=... python3 scripts/set_wiki_limit.py <username> none

Examples:
    # make the owner effectively unlimited on prod:
    python3 scripts/set_wiki_limit.py jacobcole 100000

NULL/none = "use Config.MAX_WIKIS_PER_USER". Any integer wins over the default.
Equivalent raw SQL:
    UPDATE users SET wiki_limit = 100000 WHERE username = 'jacobcole';
"""
import sys

from app import create_app, db
from app.models import User


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)

    username = sys.argv[1].strip().lower()
    raw = sys.argv[2].strip().lower()
    new_limit = None if raw in ("none", "null", "") else int(raw)

    app = create_app()
    with app.app_context():
        user = User.query.filter_by(username=username).first()
        if not user:
            print(f"No user named '{username}'")
            sys.exit(1)
        old = user.wiki_limit
        user.wiki_limit = new_limit
        db.session.commit()
        print(f"@{username}: wiki_limit {old!r} -> {new_limit!r} "
              f"(effective limit now {user.effective_wiki_limit()})")


if __name__ == "__main__":
    main()
