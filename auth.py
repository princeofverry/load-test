import base64
import hashlib
import hmac
import json
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import config


WIB_TZ = timezone(timedelta(hours=7))


def iso_timestamp() -> str:
    """Generate ISO-8601 timestamp dengan offset +07:00."""
    return datetime.now(WIB_TZ).isoformat(timespec="seconds")


class BiSnapAuthManager:
    """Mengelola B2B token (SHA256withRSA) dan X-SIGNATURE (HMAC-SHA512) untuk tiap request."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._token_fetched_at: float = 0.0
        self._private_key = None

    def _load_private_key(self):
        if self._private_key is None:
            with open(config.PRIVATE_KEY_PATH, "rb") as f:
                self._private_key = serialization.load_pem_private_key(f.read(), password=None)
        return self._private_key

    def _sign_rsa(self, string_to_sign: str) -> str:
        private_key = self._load_private_key()
        signature = private_key.sign(
            string_to_sign.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _fetch_new_token(self) -> str:
        timestamp = iso_timestamp()
        string_to_sign = f"{config.CLIENT_ID}|{timestamp}"
        signature = self._sign_rsa(string_to_sign)

        headers = {
            "Content-Type": "application/json",
            "X-TIMESTAMP": timestamp,
            "X-CLIENT-KEY": config.CLIENT_ID,
            "X-SIGNATURE": signature,
        }
        payload = {"grantType": "client_credentials"}
        url = f"{config.BASE_URL}{config.TOKEN_PATH}"

        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        token = data.get("accessToken") or data.get("access_token")
        if not token:
            raise RuntimeError(f"Token tidak ditemukan di response: {data}")
        return token

    def get_token(self) -> str:
        now = time.monotonic()
        if self._access_token and (now - self._token_fetched_at) < config.TOKEN_REFRESH_SECONDS:
            return self._access_token

        with self._lock:
            now = time.monotonic()
            if self._access_token and (now - self._token_fetched_at) < config.TOKEN_REFRESH_SECONDS:
                return self._access_token
            self._access_token = self._fetch_new_token()
            self._token_fetched_at = time.monotonic()
            return self._access_token

    @staticmethod
    def _minify_body_sha256(body: dict[str, Any] | list[Any] | None) -> str:
        if body is None:
            raw = ""
        else:
            raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest().lower()

    def build_headers(
        self,
        method: str,
        relative_url: str,
        body: dict[str, Any] | list[Any] | None = None,
    ) -> dict[str, str]:
        """Bangun header standar request AVA (otomatis bypass signature jika ENABLE_AUTH=false)."""
        headers = {
            "Content-Type": "application/json",
            "login-id": config.LOGIN_ID,
            "user-id": config.USER_ID,
            "branch-id": config.BRANCH_ID,
        }
        if not config.ENABLE_AUTH:
            return headers

        token = self.get_token()
        timestamp = iso_timestamp()
        body_hash = self._minify_body_sha256(body)
        string_to_sign = f"{method.upper()}:{relative_url}:{token}:{body_hash}:{timestamp}"

        mac = hmac.new(
            config.CLIENT_SECRET.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha512,
        )
        signature = base64.b64encode(mac.digest()).decode("utf-8")

        headers.update(
            {
                "Authorization": f"Bearer {token}",
                "X-TIMESTAMP": timestamp,
                "X-PARTNER-ID": config.PARTNER_ID,
                "X-SIGNATURE": signature,
            }
        )
        return headers


auth_manager = BiSnapAuthManager()
