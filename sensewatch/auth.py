"""Keychain credential loading and HMAC request signing for SenseCore APIs.

Vendored from sensecore_hmac_request.py — the core signing logic is ~30 lines.
Each macOS user has their own Keychain, so multi-user sharing works automatically.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import subprocess
import urllib.error
import urllib.request
from email.utils import formatdate
from typing import Any

from . import config


def read_keychain_password(service: str, account: str) -> str:
    """Read a password from macOS Keychain."""
    return subprocess.check_output(
        ["security", "find-generic-password", "-w", "-a", account, "-s", service],
        text=True,
    ).strip()


def build_auth_header(
    access_key_id: str, secret: str, method: str, path: str, x_date: str
) -> str:
    """Build the HMAC Authorization header value."""
    request_line = f"{method} {path} HTTP/1.1"
    string_to_sign = f"x-date: {x_date}\n{request_line}"
    signature = base64.b64encode(
        hmac.new(
            secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("utf-8")
    return (
        f'hmac accesskey="{access_key_id}", algorithm="hmac-sha256", '
        f'headers="x-date request-line", signature="{signature}"'
    )


class SenseCoreAuth:
    """Loads AK/SK from macOS Keychain once, caches in memory."""

    def __init__(self, account: str | None = None):
        self.account = account or getpass.getuser()
        self._akid: str | None = None
        self._secret: str | None = None

    def load_credentials(self) -> bool:
        """Attempt to read AK/SK from Keychain. Returns True if both found."""
        try:
            self._akid = read_keychain_password(
                config.KEYCHAIN_AKID_SERVICE, self.account
            )
            self._secret = read_keychain_password(
                config.KEYCHAIN_SECRET_SERVICE, self.account
            )
            return True
        except subprocess.CalledProcessError:
            self._akid = None
            self._secret = None
            return False

    @property
    def is_authenticated(self) -> bool:
        return self._akid is not None and self._secret is not None

    def build_headers(self, method: str, path: str) -> dict[str, str]:
        """Return signed headers for a SenseCore API request."""
        if not self.is_authenticated:
            raise RuntimeError("Credentials not loaded — call load_credentials() first")
        x_date = formatdate(usegmt=True)
        auth = build_auth_header(self._akid, self._secret, method, path, x_date)  # type: ignore[arg-type]
        return {
            "X-Date": x_date,
            "Authorization": auth,
            "Accept": "application/json",
        }

    def clear_cache(self) -> None:
        """Force re-read from Keychain on next load_credentials() call."""
        self._akid = None
        self._secret = None

    def request_json(
        self,
        method: str,
        service_base: str,
        path: str,
        body: dict | None = None,
    ) -> dict[str, Any]:
        """Send a signed HTTP request and return parsed JSON response."""
        headers = self.build_headers(method, path)
        body_bytes = None
        if body is not None:
            body_bytes = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        url = service_base.rstrip("/") + path
        req = urllib.request.Request(url, data=body_bytes, method=method, headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            # On 401, clear cache so next attempt re-reads Keychain
            if exc.code == 401:
                self.clear_cache()
            raise
