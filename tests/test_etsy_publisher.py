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
                return {"listing_id": 77} if method == "POST" and path == "/shops/5/listings" else {}

            with patch.object(publisher, "_api", side_effect=fake_api):
                draft = publisher.create_draft_and_upload(shop_id="5", taxonomy_id="123", price="4.00",
                    title="A Halloween coloring book", description="A digital book", tags=["halloween"],
                    upload_files=files, images=images)
                self.assertTrue(draft["complete"])
                self.assertEqual(len(draft["uploaded_files"]), 2)
                self.assertEqual(len(draft["uploaded_images"]), 1)
                self.assertEqual(publisher._plain_status()["published"], False)
                publisher.publish("5", "77")

            self.assertEqual(calls[0][0:2], ("POST", "/shops/5/listings"))
            self.assertEqual(calls[-1][0:2], ("PATCH", "/shops/5/listings/77"))
            self.assertEqual(calls[-1][2]["data"], {"state": "active"})
            saved = publisher._plain_status()
            self.assertTrue(saved["published"])
            self.assertTrue(json.loads(publisher.status_file.read_text(encoding="utf-8"))["published"])


if __name__ == "__main__":
    unittest.main()
