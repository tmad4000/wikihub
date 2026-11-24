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
        self.bodies = []
        self._body = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.scripts.append(dict(attrs))
            self._body = []

    def handle_data(self, data):
        if self._body is not None:
            self._body.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._body is not None:
            self.bodies.append("".join(self._body))
            self._body = None


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

    def test_base_and_landing_keep_one_strict_tag_without_legacy_bootstrap(self):
        """Exercise complete browser-facing pages with real nested includes."""
        root = Path(__file__).resolve().parents[1]
        env = Environment(
            loader=FileSystemLoader(root / "app" / "templates"),
            undefined=StrictUndefined,
            autoescape=True,
        )
        for page in ("base.html", "landing.html"):
            for signed_in in (False, True):
                with self.subTest(page=page, signed_in=signed_in):
                    rendered = env.get_template(page).render(
                        config={
                            "POSTHOG_KEY": "synthetic-configured-key",
                            "POSTHOG_HOST": "https://posthog.example.invalid",
                        },
                        current_user=SimpleNamespace(
                            is_authenticated=signed_in,
                            id="synthetic-private-id",
                            username="synthetic-private-name",
                            email="synthetic@example.invalid",
                            display_name="Synthetic Private Name",
                            email_verified_at=True,
                        ),
                        request=SimpleNamespace(
                            environ={}, host="wikihub.md",
                            host_url="https://wikihub.md/", path="/", args={},
                        ),
                        url_for=lambda endpoint, **kwargs: "/synthetic/" + endpoint,
                        csrf_token=lambda: "synthetic-csrf",
                        og_url="https://wikihub.md/", og_image="",
                        featured_wikis=[], site_host=None,
                        ideaflow_login_enabled=False, app_version="synthetic",
                    )
                    parser = Scripts()
                    parser.feed(rendered)
                    executable = "\n".join(parser.bodies).lower()
                    self.assertNotIn("posthog", executable)
                    self.assertNotIn("synthetic-configured-key", executable)
                    self.assertNotIn("synthetic-private-id", executable)
                    self.assertNotIn("synthetic@example.invalid", executable)
                    self.assertFalse(any("posthog" in s.get("src", "").lower()
                                         for s in parser.scripts))
                    telemetry = [s for s in parser.scripts if s.get("src") ==
                                 "https://analytics.ideaflow.app/ideaflow.js"]
                    self.assertEqual(len(telemetry), 1)
                    self.assertEqual(telemetry[0].get("data-product"), "wikihub")
                    self.assertEqual(telemetry[0].get("data-privacy"), "strict")
                    self.assertEqual(telemetry[0].get("data-respect-dnt"), "true")
                    self.assertIn("async", telemetry[0])
                    self.assertNotIn("defer", telemetry[0])


if __name__ == "__main__":
    unittest.main()
