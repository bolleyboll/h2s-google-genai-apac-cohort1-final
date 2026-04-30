"""Symmetric encryption helpers for chat history at rest.

Uses Fernet (AES-128-CBC + HMAC-SHA256) from the ``cryptography`` package. The
master key is read from ``SIDEKICK_ENC_MASTER_KEY`` (a urlsafe-base64 encoded
32-byte key, exactly what ``Fernet.generate_key()`` returns).

Generate one with:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

If the env var is missing or malformed we fail fast — chat history is not
written or read in plaintext.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


_ENV_KEY = "SIDEKICK_ENC_MASTER_KEY"


class EncryptionConfigError(RuntimeError):
    """Raised when ``SIDEKICK_ENC_MASTER_KEY`` is missing or invalid."""


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    """Return the process-wide Fernet instance, validating the master key once.

    Raises:
        EncryptionConfigError: When the env var is missing or not a valid Fernet key.

    Returns:
        Fernet: Initialized Fernet primitive.
    """
    raw = os.environ.get(_ENV_KEY, "").strip()
    if not raw:
        raise EncryptionConfigError(
            f"{_ENV_KEY} must be set (urlsafe-base64 32 bytes; "
            "generate with `python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"`)."
        )
    try:
        return Fernet(raw.encode("utf-8"))
    except (ValueError, TypeError) as e:
        raise EncryptionConfigError(
            f"{_ENV_KEY} is not a valid Fernet key: {e}"
        ) from e


def encryption_configured() -> bool:
    """Return whether encryption is configured (does not raise).

    Returns:
        bool: True if a usable master key is present.
    """
    try:
        _fernet()
        return True
    except EncryptionConfigError:
        return False


def assert_encryption_ready() -> None:
    """Validate encryption setup at startup; raise immediately if not configured.

    Raises:
        EncryptionConfigError: If the master key is missing/invalid.
    """
    _fernet()


def encrypt_text(plaintext: str) -> bytes:
    """Encrypt a plaintext string for at-rest storage.

    Args:
        plaintext (str): Message text.

    Returns:
        bytes: Fernet token (urlsafe-base64) suitable for ``BYTEA`` storage.
    """
    return _fernet().encrypt((plaintext or "").encode("utf-8"))


def decrypt_text(token: bytes | memoryview | str) -> Optional[str]:
    """Decrypt a token produced by :func:`encrypt_text`.

    Args:
        token (bytes | memoryview | str): Stored ciphertext (BYTEA from Postgres
            arrives as ``bytes`` or ``memoryview`` via SQLAlchemy).

    Returns:
        Optional[str]: Decoded UTF-8 plaintext, or ``None`` if decryption fails
        (key rotation, corruption, wrong key — the row is unrecoverable).
    """
    if token is None:
        return None
    if isinstance(token, memoryview):
        raw = bytes(token)
    elif isinstance(token, str):
        raw = token.encode("utf-8")
    else:
        raw = bytes(token)
    try:
        return _fernet().decrypt(raw).decode("utf-8")
    except (InvalidToken, ValueError):
        return None
