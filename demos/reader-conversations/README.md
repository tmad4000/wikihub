# Running the conversation comparison

Run from the WikiHub checkout with its usual dependencies and PostgreSQL. Create
the dedicated local database with `createdb wikihub_reader_preview_3add`. Set
`DATABASE_URL=postgresql:///wikihub_reader_preview_3add`,
`BASE_URL=http://127.0.0.1:8794`, `SESSION_COOKIE_SECURE=0`, a persistent
random `SECRET_KEY`, and a dedicated, empty local `REPOS_DIR`. Supply the normal
server-side `READER_ASSISTANT_API_KEY`. Run
`python demos/reader-conversations/server.py` and open `/?variant=A` and
`/?variant=B` in separate tabs. Use the normal WikiHub magic-link flow for
the seeded owner `fieldnotes` to exercise writes. Both tabs use the same database
and Git repositories. Restarting preserves approved changes.

The two seeded wikis contain synthetic field notes. The server uses the real
reader and model, WikiHub page ACLs, PostgreSQL and Git. Cross-wiki search in
this preview covers Field Library and Watershed Notes. A keeps one conversation
when changing scope. B keeps the page chat and library chat separate and lets a
reader attach the selected passage explicitly. Select a passage, ask for a
comment, review the target and text, then approve. Reload either tab to find the
comment. Inspect a source link and check private pages in a signed-out window.
