"""
Encrypted model fields.

Camera RTSP URLs embed working credentials for devices on your network. They
are encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256) under
``settings.FIELD_ENCRYPTION_KEY`` so a database dump does not hand over the
camera estate.

Implemented here rather than pulling in a third-party package: the surface is
small, and the commonly suggested options are unmaintained.

Trade-off worth knowing: encrypted columns cannot be indexed, filtered with
``__contains``, or compared in SQL. That is fine for credentials, which are
only ever read whole by the application.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.utils.functional import cached_property


class _FernetMixin:
    """Shared encrypt/decrypt plumbing."""

    @cached_property
    def _fernet(self):
        from cryptography.fernet import Fernet, MultiFernet

        key = getattr(settings, "FIELD_ENCRYPTION_KEY", None)
        if not key:
            raise ImproperlyConfigured(
                "FIELD_ENCRYPTION_KEY is required to read or write encrypted "
                "fields. Generate one with: "
                "python -c \"from cryptography.fernet import Fernet; "
                'print(Fernet.generate_key().decode())"'
            )
        # A list supports key rotation: put the new key first, keep the old
        # one to decrypt existing rows, then re-save and drop the old key.
        keys = key if isinstance(key, list | tuple) else [key]
        fernets = [Fernet(k.encode() if isinstance(k, str) else k) for k in keys]
        return MultiFernet(fernets) if len(fernets) > 1 else fernets[0]

    def get_internal_type(self) -> str:
        return "TextField"

    def db_type(self, connection) -> str:
        return "text"

    def get_prep_value(self, value):
        if value is None or value == "":
            return value
        if isinstance(value, bytes):
            value = value.decode()
        return self._fernet.encrypt(str(value).encode()).decode()

    def from_db_value(self, value, expression, connection):
        if value in (None, ""):
            return value
        from cryptography.fernet import InvalidToken

        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken:
            # Wrong or rotated-away key. Surface it rather than returning
            # ciphertext that would be silently used as a password.
            raise ImproperlyConfigured(
                "Could not decrypt an encrypted field. FIELD_ENCRYPTION_KEY "
                "does not match the key the value was written with."
            ) from None


class EncryptedTextField(_FernetMixin, models.TextField):
    """Free-text secret, e.g. a full RTSP URL with credentials."""

    description = "Fernet-encrypted text"


class EncryptedCharField(_FernetMixin, models.CharField):
    """Short secret, e.g. a camera service-account password.

    ``max_length`` applies to the plaintext; the stored ciphertext is longer,
    so the column is TEXT regardless.
    """

    description = "Fernet-encrypted string"

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_length", 255)
        super().__init__(*args, **kwargs)
