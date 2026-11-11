"""Logins for the GUI: users file on the RevPi, PBKDF2 passwords, sessions.

The users file (default secrets/users.json, not in git, owner-only) holds
name -> role, salt, PBKDF2-SHA256 hash and iteration count; manage it with
tools/manage_users.py. Passwords are never stored or logged.

Sessions live in GUI-server memory only (a GUI restart logs everyone out):
cookie HttpOnly + SameSite=Strict, 30 min idle, 8 h at most. Five wrong
passwords for one name lock that name for 60 s.
"""

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time

ROLES = ("OPERATOR", "COMMISSIONING")
ITERATIONS = 200_000
MIN_PASSWORD = 10
IDLE_S = 30 * 60
MAX_AGE_S = 8 * 3600
FAILURES_BEFORE_LOCK = 5
LOCK_S = 60


def hash_password(password: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


class UserStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("users", {})
        except FileNotFoundError:
            return {}

    def save(self, users: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump({"users": users}, file, indent=2)
        os.replace(temp, self.path)

    def set_user(self, name: str, role: str, password: str):
        if role not in ROLES:
            raise ValueError(f"Role must be one of {ROLES}")
        if not name or not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError("Name: letters, digits, - and _ only")
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"Password needs at least {MIN_PASSWORD} characters")
        salt = secrets.token_bytes(16)
        users = self.load()
        users[name] = {"role": role, "salt": salt.hex(), "iterations": ITERATIONS,
                       "hash": hash_password(password, salt).hex()}
        self.save(users)

    def remove_user(self, name: str) -> bool:
        users = self.load()
        if users.pop(name, None) is None:
            return False
        self.save(users)
        return True

    def verify(self, name: str, password: str) -> str | None:
        """The user's role, or None. Always does the hashing work."""
        entry = self.load().get(name)
        salt = bytes.fromhex(entry["salt"]) if entry else b"\0" * 16
        iterations = int(entry["iterations"]) if entry else ITERATIONS
        digest = hash_password(password, salt, iterations)
        if entry and hmac.compare_digest(digest.hex(), entry["hash"]) and entry["role"] in ROLES:
            return entry["role"]
        return None


@dataclass
class Session:
    user: str
    role: str
    created: float
    seen: float


class SessionStore:
    def __init__(self, clock=time.monotonic, on_end=None):
        """on_end(session, cause): called (outside the lock) whenever a session
        ends: LOGOUT, IDLE_EXPIRED, MAX_AGE_EXPIRED or GUI_STOPPED."""
        self.clock = clock
        self.on_end = on_end
        self._sessions: dict[str, Session] = {}
        self._failures: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def locked_out(self, name) -> float:
        """Seconds the name stays locked after repeated failures (0: not locked)."""
        with self._lock:
            count, since = self._failures.get(name, (0, 0.0))
            if count >= FAILURES_BEFORE_LOCK:
                remaining = LOCK_S - (self.clock() - since)
                if remaining > 0:
                    return remaining
                self._failures.pop(name, None)
            return 0.0

    def failed(self, name):
        with self._lock:
            count, _ = self._failures.get(name, (0, 0.0))
            self._failures[name] = (count + 1, self.clock())

    def create(self, name, role) -> str:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            self._failures.pop(name, None)
            self._sessions[token] = Session(name, role, now, now)
        return token

    @staticmethod
    def _expiry(session, now) -> str | None:
        if now - session.created > MAX_AGE_S:
            return "MAX_AGE_EXPIRED"
        if now - session.seen > IDLE_S:
            return "IDLE_EXPIRED"
        return None

    def _ended(self, ended):
        for session, cause in ended:
            if self.on_end is not None:
                try:
                    self.on_end(session, cause)
                except Exception:
                    pass            # the controller expires the requests anyway

    def get(self, token) -> Session | None:
        if not token:
            return None
        now = self.clock()
        ended = []
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            cause = self._expiry(session, now)
            if cause:
                del self._sessions[token]
                ended.append((session, cause))
            else:
                session.seen = now
        self._ended(ended)
        return None if ended else session

    def end(self, token, cause="LOGOUT"):
        with self._lock:
            session = self._sessions.pop(token, None) if token else None
        if session is not None:
            self._ended([(session, cause)])

    def sweep(self) -> int:
        """End expired sessions (called periodically); returns how many ended."""
        now = self.clock()
        with self._lock:
            ended = [(token, session, self._expiry(session, now))
                     for token, session in self._sessions.items()]
            ended = [(token, session, cause) for token, session, cause in ended if cause]
            for token, _, _ in ended:
                del self._sessions[token]
        self._ended([(session, cause) for _, session, cause in ended])
        return len(ended)

    def end_all(self, cause="GUI_STOPPED"):
        with self._lock:
            ended = list(self._sessions.values())
            self._sessions.clear()
        self._ended([(session, cause) for session in ended])
