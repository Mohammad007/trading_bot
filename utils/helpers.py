"""
General-purpose helpers: retry, rate-limit, crypto, math, time.

Kept dependency-light so any module can pull from here.
"""
from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, TypeVar

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from utils.logger import get_logger

log = get_logger(__name__)
T = TypeVar("T")

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def now_ms() -> int:
    return int(time.time() * 1000)


def utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Retry decorator (async + sync)
# ---------------------------------------------------------------------------

def async_retry(
    attempts: int = 3,
    delay: float = 0.5,
    backoff: float = 2.0,
    exceptions: tuple = (Exception,),
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Retry an async callable with exponential backoff."""

    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapped(*args: Any, **kwargs: Any) -> T:
            current_delay = delay
            last_exc: Optional[BaseException] = None
            for attempt in range(1, attempts + 1):
                try:
                    return await fn(*args, **kwargs)
                except exceptions as exc:  # noqa: PERF203
                    last_exc = exc
                    if attempt == attempts:
                        break
                    log.debug(
                        "retry %s attempt %d/%d in %.2fs: %s",
                        fn.__name__, attempt, attempts, current_delay, exc,
                    )
                    await asyncio.sleep(current_delay)
                    current_delay *= backoff
            assert last_exc is not None
            raise last_exc

        return wrapped

    return decorator


# ---------------------------------------------------------------------------
# Token-bucket rate limiter
# ---------------------------------------------------------------------------

@dataclass
class RateLimiter:
    """Simple async token bucket. Safe under asyncio (no threads)."""

    rate: float            # tokens per second
    capacity: float        # max tokens
    _tokens: float = 0.0
    _last: float = 0.0

    def __post_init__(self) -> None:
        self._tokens = self.capacity
        self._last = time.monotonic()

    async def acquire(self, cost: float = 1.0) -> None:
        while True:
            now = time.monotonic()
            elapsed = now - self._last
            self._last = now
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            if self._tokens >= cost:
                self._tokens -= cost
                return
            need = cost - self._tokens
            await asyncio.sleep(need / self.rate)


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------

def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b else default


def pct_change(old: float, new: float) -> float:
    if old == 0:
        return 0.0
    return (new - old) / old


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ---------------------------------------------------------------------------
# Symmetric encryption for private keys
# ---------------------------------------------------------------------------

def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=200_000,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def encrypt_secret(plaintext: str, passphrase: str) -> str:
    """
    Encrypt a secret with a passphrase. Returns base64 blob:
        b64(salt(16) || fernet_token)
    """
    if not passphrase:
        raise ValueError("passphrase required for encryption")
    salt = os.urandom(16)
    key = _derive_key(passphrase, salt)
    token = Fernet(key).encrypt(plaintext.encode("utf-8"))
    return base64.urlsafe_b64encode(salt + token).decode("ascii")


def decrypt_secret(blob: str, passphrase: str) -> str:
    """Inverse of `encrypt_secret`. Raises ValueError on failure."""
    if not blob or not passphrase:
        raise ValueError("blob and passphrase required for decryption")
    try:
        raw = base64.urlsafe_b64decode(blob.encode("ascii"))
        salt, token = raw[:16], raw[16:]
        key = _derive_key(passphrase, salt)
        return Fernet(key).decrypt(token).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise ValueError("decryption failed (wrong passphrase or corrupt blob)") from exc


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def short_addr(addr: str, prefix: int = 4, suffix: int = 4) -> str:
    if not addr or len(addr) <= prefix + suffix + 1:
        return addr
    return f"{addr[:prefix]}…{addr[-suffix:]}"


def stable_hash(*parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()


def truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(v)
