"""In-memory keyring backends so tests never touch the real OS credential store."""

from __future__ import annotations

from collections.abc import Iterator

import keyring
import keyring.backend
import keyring.errors
import pytest


class MemoryKeyring(keyring.backend.KeyringBackend):
    """A secure-looking in-memory backend standing in for Windows/Secret Service."""

    priority = 10

    def __init__(self) -> None:
        super().__init__()
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.data.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.data[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.data:
            raise keyring.errors.PasswordDeleteError("missing")
        del self.data[(service, username)]


class FailKeyring(keyring.backend.KeyringBackend):
    priority = 1

    def get_password(self, service: str, username: str) -> str | None:
        return None

    def set_password(self, service: str, username: str, password: str) -> None:
        raise AssertionError("must never be called")

    def delete_password(self, service: str, username: str) -> None:
        raise AssertionError("must never be called")


FailKeyring.__module__ = "keyring.backends.fail"  # looks exactly like keyring's null backend


@pytest.fixture
def memory_keyring() -> Iterator[MemoryKeyring]:
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture
def no_secure_keyring() -> Iterator[None]:
    previous = keyring.get_keyring()
    keyring.set_keyring(FailKeyring())
    yield
    keyring.set_keyring(previous)
