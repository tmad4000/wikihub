"""Configured public-page analytics includes must not revive legacy collection.

Render the real Jinja include for both visitor states. This exercises the
production template boundary without a database, credentials or network.
Run: python3 tests/test_analytics_privacy.py
"""
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
import unittest

from jinja2 import Environment, FileSystemLoader, StrictUndefined


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.scripts.append(dict(attrs))


class AnalyticsPrivacyFlow(unittest.TestCase):
    def test_configured_public_include_never_starts_an_identified_provider(self):
        root = Path(__file__).resolve().parents[1]
        env = Environment(
            loader=FileSystemLoader(root / "app" / "templates"),
            undefined=StrictUndefined,
            autoescape=True,
        )
        for signed_in in (False, True):
            with self.subTest(signed_in=signed_in):
                rendered = env.get_template("_analytics.html").render(
                    config={
                        "POSTHOG_KEY": "synthetic-configured-key",
                        "POSTHOG_HOST": "https://analytics.example.invalid",
                    },
                    current_user=SimpleNamespace(
                        is_authenticated=signed_in,
                        id="synthetic-private-id",
                        username="synthetic-private-name",
                        email="synthetic@example.invalid",
                        display_name="Synthetic Private Name",
                    ),
                )
                parser = Scripts()
                parser.feed(rendered)
                self.assertEqual(parser.scripts, [], "Legacy provider bootstrap returned")
                self.assertNotIn("synthetic-private", rendered)
                self.assertNotIn("synthetic@example.invalid", rendered)


if __name__ == "__main__":
    unittest.main()
