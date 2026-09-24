"""API-key storage.

Policy:

* Persistent keys go to the **OS credential store** through ``keyring``
  (Windows Credential Manager, macOS Keychain, Linux Secret Service/KWallet).
* If no secure backend exists, the user may keep a key **for this session only**,
  in memory. There is no plaintext fallback, ever — file-based ``keyrings.alt``
  backends are treated as insecure and refused.
* Keys are handled as :class:`pydantic.SecretStr`, so ``repr()``/``str()`` never show
  them, and callers only reveal the value at the moment an SDK client is built.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from pydantic import SecretStr

from pcbrouter import APP_SLUG

log = logging.getLogger(__name__)

SERVICE_NAME = APP_SLUG
MAX_KEY_LENGTH = 1000
_INSECURE_BACKEND_MARKERS = ("fail", "null", "plaintext", "keyrings.alt", "file")


class CredentialStoreError(Exception):
    """A credential backend failed (locked keychain, D-Bus error…)."""


class SecureStorageUnavailable(CredentialStoreError):  # noqa: N818
    """No secure OS credential store is available on this machine."""


class CredentialLocation(Enum):
    SECURE_STORE = "secure_store"
    SESSION_ONLY = "session_only"


def validate_key(raw: str) -> str:
    key = raw.strip()
    if not key:
        raise ValueError("the API key is empty")
    if len(key) > MAX_KEY_LENGTH or any(ch.isspace() for ch in key):
        raise ValueError("the API key contains spaces/newlines or is too long")
    return key


def mask_secret(secret: str) -> str:
    """``"••••••••1234"`` — never more than the last four characters."""
    tail = secret[-4:] if len(secret) >= 12 else ""
    return "••••••••" + tail


class CredentialStore(ABC):
    name: str = "store"
    secure: bool = False

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def get(self, ref: str) -> str | None: ...

    @abstractmethod
    def set(self, ref: str, secret: str) -> None: ...

    @abstractmethod
    def delete(self, ref: str) -> bool: ...


class KeyringCredentialStore(CredentialStore):
    """OS keychain via the ``keyring`` package (imported lazily; optional)."""

    secure = True

    def __init__(self) -> None:
        self._keyring: Any = None
        self._reason: str | None = None
        try:
            import keyring

            self._keyring = keyring
        except ImportError:
            self._reason = "the 'keyring' package is not installed"

    @property
    def backend_name(self) -> str:
        if self._keyring is None:
            return "none"
        try:
            backend = self._keyring.get_keyring()
            return f"{type(backend).__module__}.{type(backend).__name__}"
        except Exception:  # pragma: no cover - defensive
            return "unknown"

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"OS keyring ({self.backend_name})"

    @property
    def unavailable_reason(self) -> str | None:
        self.available()
        return self._reason

    def available(self) -> bool:
        if self._keyring is None:
            return False
        try:
            backend = self._keyring.get_keyring()
        except Exception as exc:
            self._reason = f"keyring backend error: {exc}"
            return False
        backends = getattr(backend, "backends", None) or [backend]
        for candidate in backends:
            ident = f"{type(candidate).__module__}.{type(candidate).__name__}".lower()
            if not any(marker in ident for marker in _INSECURE_BACKEND_MARKERS):
                self._reason = None
                return True
        self._reason = f"no secure OS credential store found (backend: {self.backend_name})"
        return False

    def _call(self, fn_name: str, *args: str) -> Any:
        if not self.available():
            raise SecureStorageUnavailable(self._reason or "secure storage unavailable")
        try:
            return getattr(self._keyring, fn_name)(SERVICE_NAME, *args)
        except Exception as exc:  # keyring.errors.* and backend-specific errors
            if type(exc).__name__ == "PasswordDeleteError":
                return None
            raise CredentialStoreError(f"{type(exc).__name__}: {exc}") from exc

    def get(self, ref: str) -> str | None:
        value = self._call("get_password", ref)
        return str(value) if value else None

    def set(self, ref: str, secret: str) -> None:
        self._call("set_password", ref, secret)

    def delete(self, ref: str) -> bool:
        if self.get(ref) is None:
            return False
        self._call("delete_password", ref)
        return True


class SessionCredentialStore(CredentialStore):
    """In-memory only; forgotten when the application exits."""

    name = "this session only (memory)"
    secure = False

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def available(self) -> bool:
        return True

    def get(self, ref: str) -> str | None:
        return self._data.get(ref)

    def set(self, ref: str, secret: str) -> None:
        self._data[ref] = secret

    def delete(self, ref: str) -> bool:
        return self._data.pop(ref, None) is not None


class CredentialService:
    """Facade used by the UI and provider factory."""

    def __init__(
        self,
        secure: CredentialStore | None = None,
        session: SessionCredentialStore | None = None,
    ) -> None:
        self.secure_store = secure if secure is not None else KeyringCredentialStore()
        self.session_store = session or SessionCredentialStore()

    @property
    def secure_available(self) -> bool:
        # Both conditions: a store must *declare* itself secure (a memory store never
        # does) and actually be usable on this machine.
        return self.secure_store.secure and self.secure_store.available()

    def describe_secure_backend(self) -> str:
        if self.secure_available:
            return self.secure_store.name
        reason = getattr(self.secure_store, "unavailable_reason", None)
        if reason is None and not self.secure_store.secure:
            reason = "the configured store is not an OS credential store"
        return f"Secure storage unavailable ({reason or 'unknown reason'})"

    def save(self, ref: str, raw_key: str, *, session_only: bool = False) -> CredentialLocation:
        key = validate_key(raw_key)
        if session_only:
            self.session_store.set(ref, key)
            log.info("credentials.saved ref=%s location=session", ref)
            return CredentialLocation.SESSION_ONLY
        if not self.secure_available:
            raise SecureStorageUnavailable(self.describe_secure_backend())
        self.secure_store.set(ref, key)
        self.session_store.delete(ref)  # a persisted key supersedes a session one
        log.info("credentials.saved ref=%s location=secure", ref)
        return CredentialLocation.SECURE_STORE

    def location(self, ref: str) -> CredentialLocation | None:
        if self.session_store.get(ref):
            return CredentialLocation.SESSION_ONLY
        if self.secure_available:
            try:
                if self.secure_store.get(ref):
                    return CredentialLocation.SECURE_STORE
            except CredentialStoreError as exc:
                log.warning("credentials.lookup_failed ref=%s error=%s", ref, exc)
        return None

    def get(self, ref: str) -> SecretStr | None:
        value = self.session_store.get(ref)
        if value is None and self.secure_available:
            try:
                value = self.secure_store.get(ref)
            except CredentialStoreError as exc:
                log.warning("credentials.lookup_failed ref=%s error=%s", ref, exc)
                value = None
        return SecretStr(value) if value else None

    def masked(self, ref: str) -> str | None:
        secret = self.get(ref)
        return mask_secret(secret.get_secret_value()) if secret else None

    def delete(self, ref: str) -> bool:
        removed = self.session_store.delete(ref)
        if self.secure_available:
            try:
                removed = self.secure_store.delete(ref) or removed
            except CredentialStoreError as exc:
                log.warning("credentials.delete_failed ref=%s error=%s", ref, exc)
        log.info("credentials.deleted ref=%s removed=%s", ref, removed)
        return removed
