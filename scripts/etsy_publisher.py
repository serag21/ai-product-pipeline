"""Local Etsy Open API v3 publishing support. Secrets are DPAPI protected on Windows."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

OAUTH_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
API_ROOT = "https://api.etsy.com/v3/application"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Etsy credentials can only be stored on Windows with DPAPI protection")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    source = ctypes.create_string_buffer(data)
    blob = _DataBlob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    output = _DataBlob()
    if protect:
        fn = crypt32.CryptProtectData
        fn.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_DataBlob)]
        fn.restype = ctypes.c_int
        ok = fn(ctypes.byref(blob), "Little Haunts Etsy credentials", None, None, None, 1, ctypes.byref(output))
    else:
        fn = crypt32.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(_DataBlob), ctypes.POINTER(ctypes.c_wchar_p), ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_DataBlob)]
        fn.restype = ctypes.c_int
        ok = fn(ctypes.byref(blob), None, None, None, None, 1, ctypes.byref(output))
    if not ok:
        raise OSError("Windows DPAPI could not protect or unlock the Etsy credentials")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        local_free = kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        local_free(ctypes.cast(output.pbData, ctypes.c_void_p))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


class EtsyPublisher:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir.resolve()
        self.secret_file = self.run_dir / "etsy_credentials.dpapi"
        self.pending_file = self.run_dir / "etsy_pending.dpapi"
        self.status_file = self.run_dir / "etsy_listing_state.json"

    def _read_secret(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        return json.loads(_dpapi(path.read_bytes(), False))

    def _write_secret(self, path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_bytes(_dpapi(json.dumps(value).encode("utf-8"), True))
        os.replace(temp, path)

    def _plain_status(self) -> dict[str, Any]:
        if not self.status_file.is_file():
            return {}
        return json.loads(self.status_file.read_text(encoding="utf-8"))

    def _save_status(self, data: dict[str, Any]) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        temp = self.status_file.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temp, self.status_file)

    def status(self) -> dict[str, Any]:
        secrets_data = self._read_secret(self.secret_file)
        status = self._plain_status()
        return {"configured": bool(secrets_data.get("api_key") and secrets_data.get("shared_secret")),
            "authorized": bool(secrets_data.get("access_token") and float(secrets_data.get("expires_at", 0)) > time.time()),
            "scope": secrets_data.get("scope", ""), "draft": status.get("draft"),
            "published": bool(status.get("published")), "last_error": status.get("last_error")}

    def begin_auth(self, api_key: str, shared_secret: str, redirect_uri: str) -> dict[str, str]:
        api_key, shared_secret, redirect_uri = api_key.strip(), shared_secret.strip(), redirect_uri.strip()
        parsed = urllib.parse.urlparse(redirect_uri)
        if not api_key or not shared_secret:
            raise ValueError("Enter the Etsy API keystring and shared secret from Your Apps")
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("Etsy requires the exact registered HTTPS redirect URI (without a query string)")
        existing = self._read_secret(self.secret_file)
        existing.update({"api_key": api_key, "shared_secret": shared_secret, "redirect_uri": redirect_uri})
        self._write_secret(self.secret_file, existing)
        verifier = _b64url(secrets.token_bytes(48))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state = secrets.token_urlsafe(32)
        self._write_secret(self.pending_file, {"verifier": verifier, "state": state, "redirect_uri": redirect_uri, "created_at": time.time()})
        params = {"response_type": "code", "client_id": api_key, "redirect_uri": redirect_uri,
            "scope": "listings_w listings_r shops_r", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"}
        return {"authorize_url": OAUTH_URL + "?" + urllib.parse.urlencode(params), "state": state}

    def finish_auth(self, code: str, state: str) -> dict[str, Any]:
        pending = self._read_secret(self.pending_file)
        secrets_data = self._read_secret(self.secret_file)
        if not pending or time.time() - float(pending.get("created_at", 0)) > 900:
            raise ValueError("The Etsy authorization request expired; start it again")
        if not secrets.compare_digest(str(pending.get("state", "")), state.strip()):
            raise ValueError("The returned Etsy state did not match the saved request")
        response = self._token_request({"grant_type": "authorization_code", "client_id": secrets_data["api_key"],
            "redirect_uri": pending["redirect_uri"], "code": code.strip(), "code_verifier": pending["verifier"]})
        self._accept_tokens(secrets_data, response)
        self._write_secret(self.secret_file, secrets_data)
        self.pending_file.unlink(missing_ok=True)
        return self.status()

    def _token_request(self, params: dict[str, str]) -> dict[str, Any]:
        data = urllib.parse.urlencode(params).encode()
        request = urllib.request.Request(TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Etsy authorization failed ({exc.code}): {exc.read().decode(errors='replace')[:1200]}") from exc

    def _accept_tokens(self, data: dict[str, Any], token: dict[str, Any]) -> None:
        data.update({"access_token": token["access_token"], "refresh_token": token["refresh_token"],
            "scope": token.get("scope", ""), "expires_at": time.time() + int(token.get("expires_in", 3600)) - 60})

    def _ensure_token(self, data: dict[str, Any]) -> str:
        if float(data.get("expires_at", 0)) <= time.time() + 15:
            if not data.get("refresh_token"):
                raise ValueError("Etsy is not authorized; connect the shop first")
            token = self._token_request({"grant_type": "refresh_token", "client_id": data["api_key"], "refresh_token": data["refresh_token"]})
            self._accept_tokens(data, token)
            self._write_secret(self.secret_file, data)
        if "listings_w" not in str(data.get("scope", "")).split():
            raise ValueError("The Etsy authorization needs listings_w; reconnect and grant listing access")
        return str(data["access_token"])

    def _api(self, method: str, path: str, *, data: dict[str, Any] | None = None, file: Path | None = None,
             file_field: str = "file", filename: str | None = None) -> dict[str, Any]:
        secrets_data = self._read_secret(self.secret_file)
        token = self._ensure_token(secrets_data)
        self._write_secret(self.secret_file, secrets_data)
        headers = {"x-api-key": f"{secrets_data['api_key']}:{secrets_data['shared_secret']}", "Authorization": f"Bearer {token}"}
        if file:
            boundary = "----LittleHaunts" + uuid.uuid4().hex
            chunks = []
            for key, value in (data or {}).items():
                chunks.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n").encode())
            fname = filename or file.name
            chunks.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{fname}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode())
            chunks.append(file.read_bytes()); chunks.append(f"\r\n--{boundary}--\r\n".encode())
            body = b"".join(chunks); headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif data is not None:
            body = urllib.parse.urlencode(data, doseq=True).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = None
        request = urllib.request.Request(API_ROOT + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            error = exc.read().decode(errors="replace")[:1500]
            raise RuntimeError(f"Etsy API {method} {path} failed ({exc.code}): {error}") from exc

    def create_draft_and_upload(self, *, shop_id: str, taxonomy_id: str, price: str, title: str,
                                description: str, tags: list[str], upload_files: list[Path], images: list[Path]) -> dict[str, Any]:
        if not shop_id.isdigit() or not taxonomy_id.isdigit():
            raise ValueError("Shop ID and Etsy taxonomy ID must be numeric")
        if not price.replace(".", "", 1).isdigit() or float(price) <= 0:
            raise ValueError("Enter a valid positive listing price in your Etsy shop currency")
        if not upload_files or len(upload_files) > 5 or not images:
            raise ValueError("A digital draft requires product files (up to five) and listing images")
        if any(not x.is_file() for x in [*upload_files, *images]):
            raise ValueError("A package upload file is missing; rebuild the current product package")
        body = {"quantity": "999", "title": title[:140], "description": description, "price": f"{float(price):.2f}",
            "who_made": "i_did", "when_made": "2020_2026", "taxonomy_id": taxonomy_id, "type": "download",
            "tags": ",".join(tags[:13])}
        status = self._plain_status()
        draft = status.get("draft") or {}
        if draft and draft.get("complete"):
            raise ValueError("A complete Etsy draft already exists. Review it in Etsy; remove it there before creating another draft.")
        if draft and (str(draft.get("shop_id")) != shop_id or draft.get("title") != title):
            raise ValueError("An incomplete Etsy draft is saved. Resume that draft with its original shop and title before starting another.")
        if not draft:
            created = self._api("POST", f"/shops/{shop_id}/listings", data=body)
            listing_id = str(created.get("listing_id", ""))
            if not listing_id:
                raise RuntimeError(f"Etsy created a response without a listing ID: {created}")
            draft = {"shop_id": shop_id, "listing_id": listing_id, "title": title, "price": f"{float(price):.2f}",
                "created_at": time.time(), "uploaded_files": [], "uploaded_images": [], "complete": False}
            status["draft"] = draft; status["published"] = False; self._save_status(status)
        listing_id = str(draft["listing_id"])
        try:
            for image in images:
                if image.name not in draft["uploaded_images"]:
                    self._api("POST", f"/shops/{shop_id}/listings/{listing_id}/images", file=image, file_field="image")
                    draft["uploaded_images"].append(image.name); status["draft"] = draft; self._save_status(status)
            for item in upload_files:
                if item.name not in draft["uploaded_files"]:
                    self._api("POST", f"/shops/{shop_id}/listings/{listing_id}/files", file=item, file_field="file",
                        data={"name": item.name})
                    draft["uploaded_files"].append(item.name); status["draft"] = draft; self._save_status(status)
            self._api("PATCH", f"/shops/{shop_id}/listings/{listing_id}", data={"type": "download"})
            draft["complete"] = True; status["draft"] = draft; status.pop("last_error", None); self._save_status(status)
        except Exception as exc:
            status["last_error"] = str(exc); status["draft"] = draft; self._save_status(status)
            raise
        return draft

    def publish(self, shop_id: str, listing_id: str) -> dict[str, Any]:
        status = self._plain_status(); draft = status.get("draft") or {}
        if str(draft.get("shop_id")) != str(shop_id) or str(draft.get("listing_id")) != str(listing_id):
            raise ValueError("This listing ID does not match the saved Etsy draft")
        result = self._api("PATCH", f"/shops/{shop_id}/listings/{listing_id}", data={"state": "active"})
        status["published"] = True; status["published_at"] = time.time(); status["published_response"] = result
        self._save_status(status)
        return result

    def shops(self) -> list[dict[str, Any]]:
        data = self._read_secret(self.secret_file)
        access = self._ensure_token(data)
        user_id = access.split(".", 1)[0]
        self._write_secret(self.secret_file, data)
        return self._api("GET", f"/users/{user_id}/shops").get("results", [])

    def taxonomies(self) -> list[dict[str, Any]]:
        data = self._read_secret(self.secret_file)
        if not data.get("api_key") or not data.get("shared_secret"):
            raise ValueError("Save your Etsy app credentials first")
        req = urllib.request.Request("https://api.etsy.com/v3/application/seller-taxonomy/nodes",
            headers={"x-api-key": f"{data['api_key']}:{data['shared_secret']}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.loads(response.read()).get("results", [])
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Etsy taxonomy lookup failed ({exc.code}): {exc.read().decode(errors='replace')[:1000]}") from exc
