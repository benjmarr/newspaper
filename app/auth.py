"""Single-password login: signed 30-day session cookie and a per-client lockout after failed attempts."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from pathlib import Path

from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

COOKIE = "paper_session"
MAX_AGE = 30 * 24 * 3600
MAX_FAILURES = 5
LOCKOUT_SECONDS = 5 * 60


def load_secret(data_dir: Path) -> str:
    """A random signing key kept in /data so sessions survive restarts."""
    path = data_dir / "secret_key"
    if path.exists():
        value = path.read_text().strip()
        if len(value) >= 32:
            return value
    value = secrets.token_urlsafe(48)
    path.write_text(value)
    path.chmod(0o600)
    return value


class Auth:
    def __init__(self, password: str, secret: str):
        self.password = password
        # Changing APP_PASSWORD changes the signing key, which logs out every existing session.
        key = hmac.new(secret.encode(), password.encode(), hashlib.sha256).hexdigest()
        self.signer = TimestampSigner(key, salt="paper-session")
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def check_password(self, attempt: str) -> bool:
        return hmac.compare_digest(attempt.encode(), self.password.encode())

    def make_cookie(self) -> str:
        return self.signer.sign(secrets.token_urlsafe(16)).decode()

    def valid_cookie(self, value: str | None) -> bool:
        if not value:
            return False
        try:
            self.signer.unsign(value, max_age=MAX_AGE)
            return True
        except (BadSignature, SignatureExpired):
            return False

    # --- rate limiting --------------------------------------------------
    def locked_for(self, client: str) -> int:
        """Seconds of lockout remaining for this client (0 if not locked)."""
        with self._lock:
            until = self._locked_until.get(client, 0)
            remaining = int(until - time.monotonic() + 0.999)
            if remaining <= 0 and client in self._locked_until:
                del self._locked_until[client]
            return max(0, remaining)

    def record_failure(self, client: str) -> int:
        """Record a failed attempt. Returns attempts left before lockout (0 = now locked)."""
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._failures.get(client, []) if now - t < LOCKOUT_SECONDS] + [now]
            if len(recent) >= MAX_FAILURES:
                self._locked_until[client] = now + LOCKOUT_SECONDS
                self._failures.pop(client, None)
                return 0
            self._failures[client] = recent
            return MAX_FAILURES - len(recent)

    def record_success(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)
