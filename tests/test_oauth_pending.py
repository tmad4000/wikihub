"""Browser-cookie/route regression using real Authlib state, PKCE and ID tokens.

Only the provider transport is replaced. Uses the task/CI disposable PostgreSQL
specified by DATABASE_URL; never configure it against production.
"""
import os
import secrets
import sys
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlsplit

sys.path.insert(0, os.path.dirname(__file__))
import test_e2e as core
import app.routes.auth as auth
from app import db
from app.models import User
from authlib.jose import JsonWebKey, jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization


class PendingOAuth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = core.setup()
        cls.app.config.update(IDEAFLOW_OIDC_ENABLED=True,
                              IDEAFLOW_OIDC_CLIENT_ID="pending-test",
                              IDEAFLOW_OIDC_CLIENT_SECRET="synthetic-only",
                              IDEAFLOW_OIDC_ISSUER="https://id.test")
        pem = Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption())
        cls.key = JsonWebKey.import_key(pem, {"kty": "OKP", "crv": "Ed25519", "kid": "pending-test"})

    def setUp(self):
        self.context = self.app.app_context()
        self.context.push()
        core.reset_database()
        auth.oauth._clients.pop("ideaflow", None)
        auth.oauth._registry.pop("ideaflow", None)
        auth.init_oauth(self.app)
        self.provider = auth.oauth.ideaflow
        self.provider.server_metadata.update({
            "_loaded_at": time.time(), "issuer": "https://id.test",
            "authorization_endpoint": "https://id.test/authorize",
            "token_endpoint": "https://id.test/token",
            "jwks": {"keys": [self.key.as_dict(is_private=False)]},
            "id_token_signing_alg_values_supported": ["EdDSA"],
        })
        self.browser = self.app.test_client()
        self.no_network = patch("requests.sessions.Session.request",
                                side_effect=AssertionError("No provider network allowed"))
        self.no_network.start()

    def tearDown(self):
        self.no_network.stop()
        db.session.remove()
        self.context.pop()

    def start(self, browser=None, target="/"):
        response = (browser or self.browser).get("/auth/ideaflow?next=" + quote(target, safe=""))
        self.assertEqual(response.status_code, 302)
        location = response.headers["Location"]
        if not location.startswith("https://id.test/"):
            return None, response
        params = parse_qs(urlsplit(location).query)
        self.assertEqual(params["code_challenge_method"], ["S256"])
        return params["state"][0], response

    def pending(self):
        with self.browser.session_transaction() as sess:
            return {k: v for k, v in sess.items() if k.startswith("_state_ideaflow_")}, dict(sess.get("ideaflow_oauth_contexts", {}))

    def token_for(self, state, wrong_nonce=False):
        with self.browser.session_transaction() as sess:
            data = sess["_state_ideaflow_" + state]["data"]
        nonce = "incorrect-nonce" if wrong_nonce else data["nonce"]
        claims = {"iss": "https://id.test", "sub": "pending-user", "aud": "pending-test",
                  "iat": int(time.time()), "exp": int(time.time()) + 300,
                  "nonce": nonce, "email": "pending@example.test", "email_verified": True}
        signed = jwt.encode({"alg": "EdDSA", "kid": "pending-test"}, claims, self.key).decode()
        return {"access_token": "synthetic-access-token", "id_token": signed}, data["code_verifier"]

    def test_repeated_real_authorize_attempts_bound_cookie_and_keep_latest_three(self):
        starts = []
        largest = 0
        snapshots = []
        for _ in range(40):
            state, response = self.start()
            self.assertIsNotNone(state)
            starts.append(state)
            largest = max(largest, *(len(x.encode()) for x in response.headers.getlist("Set-Cookie")))
            records, contexts = self.pending()
            snapshots.append((dict(records), dict(contexts), list(starts[-3:])))
        self.assertLess(largest, 3200, "OAuth cookies must not approach the nginx/browser header boundary")
        for records, contexts, expected in snapshots:
            self.assertLessEqual(len(records), 3)
            self.assertLessEqual(len(contexts), 3)
            self.assertEqual(set(contexts), set(expected))
            self.assertEqual(set(records), {"_state_ideaflow_" + s for s in expected})

    def test_equal_clock_starts_keep_latest_three_and_google_shares_the_budget(self):
        clock = time.time()
        with patch("app.oauth_pending.time", return_value=clock):
            first, _ = self.start()
            second, _ = self.start()
            auth.oauth.register(name="google", client_id="google-pending-test",
                                client_secret="synthetic-google", client_kwargs={"scope": "openid email profile"})
            google = auth.oauth.google
            google.server_metadata.update({"_loaded_at": time.time(),
                                           "authorization_endpoint": "https://google.test/authorize"})
            response = self.browser.get("/auth/google?next=/settings")
            google_state = parse_qs(urlsplit(response.headers["Location"]).query)["state"][0]
            newest, _ = self.start()
        with self.browser.session_transaction() as sess:
            self.assertNotIn(first, sess["ideaflow_oauth_contexts"])
            self.assertEqual(set(sess["ideaflow_oauth_contexts"]), {second, newest})
            self.assertEqual(set(sess["google_oauth_contexts"]), {google_state})
            self.assertNotIn("_state_ideaflow_" + first, sess)
        self.browser.get(f"/auth/google/callback?state={google_state}&error=access_denied")
        with self.browser.session_transaction() as sess:
            self.assertNotIn("google_oauth_contexts", sess)
            self.assertNotIn("_state_google_" + google_state, sess)
            self.assertEqual(set(sess["ideaflow_oauth_contexts"]), {second, newest})

    def test_callback_success_consumes_only_target_and_preserves_other_tab(self):
        first, _ = self.start(target="/explore")
        second, _ = self.start(target="/settings")
        token, verifier = self.token_for(first)
        with patch.object(self.provider, "fetch_access_token", return_value=token) as transport:
            response = self.browser.get(f"/auth/ideaflow/callback?state={first}&code=synthetic")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/explore"))
        self.assertEqual(transport.call_args.kwargs["code_verifier"], verifier)
        records, contexts = self.pending()
        self.assertNotIn(first, contexts)
        self.assertNotIn("_state_ideaflow_" + first, records)
        self.assertIn(second, contexts)
        with self.browser.session_transaction() as sess:
            self.assertIn("_user_id", sess)
        with patch.object(self.provider, "fetch_access_token") as transport:
            self.browser.get(f"/auth/ideaflow/callback?state={first}&code=replayed")
        transport.assert_not_called()

    def test_provider_denial_token_failure_and_wrong_nonce_clean_the_target(self):
        for mode in ("denied", "transport", "nonce"):
            self.browser = self.app.test_client()
            first, _ = self.start()
            other, _ = self.start()
            token, _ = self.token_for(first, wrong_nonce=mode == "nonce")
            kwargs = {"side_effect": RuntimeError("synthetic provider failure")} if mode == "transport" else {"return_value": token}
            query = "error=access_denied" if mode == "denied" else "code=synthetic"
            with patch.object(self.provider, "fetch_access_token", **kwargs) as transport:
                response = self.browser.get(f"/auth/ideaflow/callback?state={first}&{query}")
            self.assertEqual(response.status_code, 302)
            if mode == "denied": transport.assert_not_called()
            records, contexts = self.pending()
            self.assertNotIn(first, contexts)
            self.assertNotIn("_state_ideaflow_" + first, records)
            self.assertIn(other, contexts)
            with self.browser.session_transaction() as sess:
                self.assertNotIn("_user_id", sess)

    def test_ten_minute_expiry_rejects_before_exchange_and_keeps_younger_flow(self):
        clock = time.time()
        with patch("app.oauth_pending.time", return_value=clock): first, _ = self.start()
        with patch("app.oauth_pending.time", return_value=clock + 100): younger, _ = self.start()
        with patch("app.oauth_pending.time", return_value=clock + 600), patch.object(self.provider, "fetch_access_token") as transport:
            self.browser.get(f"/auth/ideaflow/callback?state={first}&code=expired")
        transport.assert_not_called()
        records, contexts = self.pending()
        self.assertNotIn(first, contexts)
        self.assertIn(younger, contexts)

    def test_explicit_provider_error_and_unknown_state_never_exchange_or_leave_orphans(self):
        state, _ = self.start()
        with patch.object(self.provider, "fetch_access_token") as transport:
            self.browser.get("/auth/ideaflow/callback?state=unknown&code=synthetic")
        transport.assert_not_called()
        self.assertIn(state, self.pending()[1])
        self.browser.get(f"/auth/ideaflow/callback?state={state}&error=access_denied")
        self.assertEqual(self.pending(), ({}, {}))

    def test_legacy_undated_records_are_pruned_without_losing_existing_identity(self):
        with self.browser.session_transaction() as sess:
            sess["unrelated"] = "preserved"
            sess["ideaflow_oauth_contexts"] = {"old": {"next": "/settings", "mode": "link", "user_id": 123}}
            sess["_state_ideaflow_old"] = {"data": {"nonce": "old", "code_verifier": "old"}, "exp": time.time() + 3600}
        self.start()
        records, contexts = self.pending()
        self.assertNotIn("old", contexts)
        self.assertNotIn("_state_ideaflow_old", records)
        with self.browser.session_transaction() as sess: self.assertEqual(sess["unrelated"], "preserved")

    def test_budget_refusal_does_not_grow_existing_identity_csrf_or_unrelated_cookie(self):
        user = User(username="budget-user", email="budget@example.test")
        db.session.add(user)
        db.session.commit()
        auth.oauth.register(name="google", client_id="google-budget-test",
                            client_secret="synthetic-google", client_kwargs={"scope": "openid email profile"})
        auth.oauth.google.server_metadata.update({"_loaded_at": time.time(),
            "authorization_endpoint": "https://google.test/authorize"})
        serializer = self.app.session_interface.get_signing_serializer(self.app)
        for route in ("/auth/ideaflow", "/auth/google", "/auth/ideaflow/link"):
            with self.subTest(route=route):
                browser = self.app.test_client()
                original = {"_user_id": str(user.id), "_fresh": False,
                            "csrf_token": "preserve", "unrelated": secrets.token_urlsafe(2100)}
                while len(serializer.dumps(original).encode()) < 3050:
                    original["unrelated"] += secrets.token_urlsafe(3)
                baseline = len(serializer.dumps(original).encode())
                self.assertLessEqual(baseline, 3072)
                with browser.session_transaction() as sess:
                    sess.update(original)
                response = browser.get(route)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/auth/login", response.headers["Location"])
                with browser.session_transaction() as sess:
                    final = dict(sess)
                self.assertLessEqual(len(serializer.dumps(final).encode()), 3072)
                self.assertEqual(final, original, "Refusal must preserve existing session without adding a flash")
                self.assertTrue(all(len(h.encode()) <= 3200 for h in response.headers.getlist("Set-Cookie")))

    def test_oversized_new_context_does_not_redirect_to_provider_without_matching_state(self):
        state, response = self.start(target="/" + secrets.token_urlsafe(5000))
        self.assertIsNone(state)
        self.assertIn("/auth/login", response.headers["Location"])
        self.assertEqual(self.pending(), ({}, {}))
        self.assertTrue(all(len(h.encode()) < 3200 for h in response.headers.getlist("Set-Cookie")))


if __name__ == "__main__":
    unittest.main()
