"""
Decrypts camera credentials written by the web tier.

Mirrors :mod:`apps.common.fields` so the vision workers can read what Django
wrote, without importing Django. Both sides share
``FIELD_ENCRYPTION_KEY``; if they disagree, camera startup fails loudly
rather than silently trying to open a URL that is still ciphertext.
"""

from __future__ import annotations

from functools import lru_cache

from vision.config import settings


@lru_cache(maxsize=1)
def _fernet():
    from cryptography.fernet import Fernet, MultiFernet

    key = settings.field_encryption_key
    if not key:
        raise RuntimeError(
            "FIELD_ENCRYPTION_KEY is not set for the vision worker; it cannot "
            "decrypt camera URLs."
        )
    keys = [k.strip() for k in key.split(",") if k.strip()]
    fernets = [Fernet(k.encode()) for k in keys]
    return MultiFernet(fernets) if len(fernets) > 1 else fernets[0]


def decrypt(value: str | None) -> str:
    if not value:
        return ""
    return _fernet().decrypt(value.encode()).decode()
