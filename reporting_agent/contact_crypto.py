"""Reversible encryption-at-rest for customer contact emails.

WHY THIS EXISTS
"""


from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet

# Demo-only seed, stable across restarts. Set CONTACT_ENCRYPTION_KEY in prod.
_DEMO_KEY_SEED = "contact-encryption-demo-seed-change-me"


def _resolve_key() -> bytes:
    """Resolve the Fernet key, env-var first (production, from Secret Manager),
falling back to a fixed demo key derived deterministically from
"""

    env_key = os.environ.get("CONTACT_ENCRYPTION_KEY")
    if env_key:
        return env_key.encode("utf-8") if isinstance(env_key, str) else env_key
    digest = hashlib.sha256(_DEMO_KEY_SEED.encode("utf-8")).digest()  # 32 bytes
    return base64.urlsafe_b64encode(digest)


def _fernet() -> Fernet:
    return Fernet(_resolve_key())


def encrypt_email(plaintext: str) -> str:
    """Encrypt a plaintext email address to a Fernet ciphertext (str). This is
what gets persisted to disk instead of the real address.
"""

    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_email(ciphertext: str) -> str:
    """Reverse encrypt_email(): recover the plaintext address. Only
contact_store.resolve_send_email() should call this, and only at the
"""

    return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
