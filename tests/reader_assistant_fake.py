"""Scripted stand-in for the Anthropic client used by the reader assistant
(wikihub-1bfx). Shared by tests/test_e2e.py and the browser check
(tests/browser/reader_assistant_server.py) so neither needs a real key.

It plays an *adversarial* reader: it lists and searches, then tries to read
every listed page plus any `probe_paths` (private pages, plumbing), and
finally cites every source id it saw -- plus an id it never got and quotes
it hopes are hidden. The server must keep all of that inside what the
visitor can read. Every request it receives is recorded in `.calls`.
"""

import copy
import re


class _Msg:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


class FakeReaderModel:
    def __init__(self, probe_paths=(), extra_quotes=()):
        self.probe_paths = list(probe_paths)
        self.extra_quotes = list(extra_quotes)
        self.calls = []
        self.messages = self

    # client.messages.create(...)
    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))  # the server keeps appending
        msgs = kwargs["messages"]
        tool_results = [
            block
            for m in msgs if m["role"] == "user" and isinstance(m["content"], list)
            for block in m["content"] if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        rounds = sum(1 for m in msgs if m["role"] == "assistant" and isinstance(m["content"], list)
                     and any(b.get("type") == "tool_use" for b in m["content"]))
        question = ""
        for m in reversed(msgs):
            if m["role"] == "user" and isinstance(m["content"], str):
                question = m["content"].rsplit("Question:", 1)[-1].strip()
                break
        if kwargs.get("tool_choice", {}).get("type") != "none":
            if rounds == 0:
                calls = [("list_pages", {}), ("search_pages", {"query": question.split()[0] if question else "the"})]
                calls += [("read_page", {"path": p}) for p in self.probe_paths]
                return self._tools(calls, len(self.calls))
            if rounds == 1:
                listed = []
                for r in tool_results:
                    for line in str(r["content"]).splitlines():
                        if "\t" in line and not line.startswith("["):
                            listed.append(line.split("\t", 1)[0])
                calls = [("read_page", {"path": p}) for p in listed[:6]]
                if calls:
                    return self._tools(calls, len(self.calls))
        return _Msg([{"type": "text", "text": self._answer(tool_results)}], "end_turn")

    @staticmethod
    def _tools(calls, salt):
        return _Msg([
            {"type": "tool_use", "id": f"tu_{salt}_{i}", "name": name, "input": inp}
            for i, (name, inp) in enumerate(calls)
        ], "tool_use")

    def _answer(self, tool_results):
        lines = []
        seen = set()
        for r in tool_results:
            text = str(r["content"])
            m = re.match(r"\[(S\d+)\] (\S[^\n]*?) — ([^\n]*)\n\n(.*)", text, re.S)
            if not m or m.group(1) in seen:
                continue
            sid, path, title, body = m.groups()
            seen.add(sid)
            quote = _first_sentence_words(body)
            if quote:
                lines.append(f"- {title or path} says this [{sid}: \"{quote}\"].")
            else:
                lines.append(f"- See {title or path} [{sid}].")
        for q in self.extra_quotes:
            lines.append(f"- Hidden claim [S1: \"{q}\"].")
        lines.append("- Invented source [S99].")
        return "Here is what I found:\n" + "\n".join(lines)


def _first_sentence_words(body, n=6):
    in_fm = False
    for i, line in enumerate(body.splitlines()):
        s = line.strip()
        if i == 0 and s == "---":
            in_fm = True
            continue
        if in_fm:
            if s == "---":
                in_fm = False
            continue
        if not s or s.startswith(("#", "<!--", "|", "[")):
            continue
        return " ".join(s.split()[:n])
    return ""
