from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from scripts.etsy_publisher import EtsyPublisher, _b64url


@unittest.skipUnless(os.name == "nt", "Etsy secret storage uses Windows DPAPI")
class EtsyPublisherTests(unittest.TestCase):
    def test_oauth_rejects_non_https_callback_and_public_verifier_rejects_lookalike_host(self):
        with tempfile.TemporaryDirectory() as temp:
            publisher = EtsyPublisher(Path(temp))
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                publisher.begin_auth("test-key", "test-secret", "http://localhost/callback")
            self.assertFalse(publisher.secret_file.exists())
        self.assertFalse(EtsyPublisher._verify_public_etsy_url("https://notetsy.com/listing/77/item", "77"))
        self.assertFalse(EtsyPublisher._verify_public_etsy_url("https://etsy.com/listing/77fake/item", "77"))

    def test_pkce_credentials_are_encrypted_and_authorization_code_is_exchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            publisher = EtsyPublisher(Path(temp))
            result = publisher.begin_auth("test-key", "test-secret", "https://example.test/etsy/callback")
            query = urllib.parse.parse_qs(urllib.parse.urlparse(result["authorize_url"]).query)
            pending = publisher._read_secret(publisher.pending_file)

            self.assertEqual(query["code_challenge_method"], ["S256"])
            self.assertEqual(query["code_challenge"], [_b64url(hashlib.sha256(pending["verifier"].encode()).digest())])
            self.assertEqual(query["state"], [result["state"]])
            self.assertNotIn(b"test-secret", publisher.secret_file.read_bytes())

            tokens = {"access_token": "123.test-access", "refresh_token": "123.test-refresh",
                "expires_in": 3600, "scope": "listings_w listings_r shops_r"}
            with patch.object(publisher, "_token_request", return_value=tokens) as exchange:
                status = publisher.finish_auth("authorization-code", result["state"])

            self.assertTrue(status["authorized"])
            exchange.assert_called_once()
            self.assertEqual(exchange.call_args.args[0]["code_verifier"], pending["verifier"])
            self.assertFalse(publisher.pending_file.exists())

    def test_digital_draft_resumes_uploads_and_publish_is_a_separate_api_action(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            publisher = EtsyPublisher(root / "run")
            files = [root / "book.pdf", root / "book-pngs.zip"]
            images = [root / "listing.jpg"]
            for path in files + images:
                path.write_bytes(b"smoke-test-content")
            calls: list[tuple[str, str, dict]] = []

            def fake_api(method, path, **kwargs):
                calls.append((method, path, kwargs))
                if method == "POST" and path == "/shops/5/listings":
                    self.assertEqual(kwargs["data"]["is_supply"], "false")
                    return {"listing_id": 77}
                if method == "GET" and path == "/listings/77":
                    return {"listing_id": 77, "state": "active", "url": "https://www.etsy.com/listing/77/little-haunts"}
                return {}

            with patch.object(publisher, "_api", side_effect=fake_api), patch.object(publisher, "_verify_public_etsy_url", return_value=True) as verify:
                draft = publisher.create_draft_and_upload(shop_id="5", taxonomy_id="123", price="4.00",
                    title="A Halloween coloring book", description="A digital book", tags=["halloween"],
                    upload_files=files, images=images)
                self.assertTrue(draft["complete"])
                self.assertEqual(len(draft["uploaded_files"]), 2)
                self.assertEqual(len(draft["uploaded_images"]), 1)
                self.assertEqual(publisher._plain_status()["published"], False)
                publisher.publish("5", "77")

            self.assertEqual(calls[0][0:2], ("POST", "/shops/5/listings"))
            self.assertEqual(calls[-2][0:2], ("PATCH", "/shops/5/listings/77"))
            self.assertEqual(calls[-2][2]["data"], {"state": "active"})
            self.assertEqual(calls[-1][0:2], ("GET", "/listings/77"))
            verify.assert_called_once_with("https://www.etsy.com/listing/77/little-haunts", "77")
            saved = publisher._plain_status()
            self.assertTrue(saved["published"])
            self.assertTrue(saved["public_url_verified"])
            self.assertTrue(json.loads(publisher.status_file.read_text(encoding="utf-8"))["published"])

    def test_uncertain_create_is_not_repeated_and_can_be_reconciled(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            publisher = EtsyPublisher(root / "run")
            files = [root / "book.pdf"]
            images = [root / "listing.jpg"]
            for path in files + images:
                path.write_bytes(b"smoke-test-content")
            with patch.object(publisher, "_api", side_effect=TimeoutError("connection dropped after request")) as api:
                with self.assertRaises(RuntimeError):
                    publisher.create_draft_and_upload(shop_id="5", taxonomy_id="123", price="4.99",
                        title="A Halloween coloring book", description="A digital book", tags=["halloween"],
                        upload_files=files, images=images)
                self.assertTrue(publisher.status()["creation_uncertain"])
                with self.assertRaises(ValueError):
                    publisher.create_draft_and_upload(shop_id="5", taxonomy_id="123", price="4.99",
                        title="A Halloween coloring book", description="A digital book", tags=["halloween"],
                        upload_files=files, images=images)
                self.assertEqual(api.call_count, 1)
            with patch.object(publisher, "_api", return_value={"results": [{"listing_id": 89,
                    "title": "A Halloween coloring book", "price": 4.99, "creation_timestamp": 123}]}):
                reconciled = publisher.reconcile_creation_intent()
            self.assertTrue(reconciled["matched"])
            self.assertEqual(publisher.status()["draft"]["listing_id"], "89")
            self.assertFalse(publisher.status()["creation_uncertain"])


if __name__ == "__main__":
    unittest.main()
