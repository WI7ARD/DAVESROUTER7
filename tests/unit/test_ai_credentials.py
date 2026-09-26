"""Credential service (spec item 52): keys live in the OS keyring, never in settings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import keyring
import keyring.backend
import pytest

from pcbrouter.ai.credentials import (
    SERVICE_NAME,
    CredentialLocation,
    CredentialService,
    KeyringCredentialStore,
    SecureStorageUnavailable,
    SessionCredentialStore,
    mask_secret,
    validate_key,
)
from pcbrouter.ai.profiles import ProviderKind, ProviderProfile
from pcbrouter.settings import AppSettings, SettingsStore
from tests.support.keyrings import MemoryKeyring
from tests.support.mock_provider import FAKE_KEY

REF = "ai-pcb-router/openai-test"


def test_store_retrieve_replace_delete_missing(memory_keyring: MemoryKeyring) -> None:
    svc = CredentialService()
    assert svc.secure_available
    assert svc.get(REF) is None and svc.location(REF) is None  # missing key
    assert svc.save(REF, f"  {FAKE_KEY}\n") is CredentialLocation.SECURE_STORE
    assert memory_keyring.data[(SERVICE_NAME, REF)] == FAKE_KEY  # stripped, in the OS store
    secret = svc.get(REF)
    assert secret is not None and secret.get_secret_value() == FAKE_KEY
    assert FAKE_KEY not in repr(secret) and FAKE_KEY not in str(secret)
    svc.save(REF, "sk-test-REPLACEMENT-111111111111")
    assert svc.get(REF).get_secret_value() == "sk-test-REPLACEMENT-111111111111"  # type: ignore[union-attr]
    assert svc.masked(REF) == "••••••••1111"
    assert svc.delete(REF) is True
    assert svc.get(REF) is None and svc.delete(REF) is False


def test_no_plaintext_fallback(no_secure_keyring: None) -> None:
    svc = CredentialService()
    assert not svc.secure_available
    assert "unavailable" in svc.describe_secure_backend().lower()
    with pytest.raises(SecureStorageUnavailable):
        svc.save(REF, FAKE_KEY)
    assert svc.get(REF) is None  # nothing was written anywhere
    assert svc.save(REF, FAKE_KEY, session_only=True) is CredentialLocation.SESSION_ONLY
    assert svc.get(REF).get_secret_value() == FAKE_KEY  # type: ignore[union-attr]
    assert svc.location(REF) is CredentialLocation.SESSION_ONLY


def test_plaintext_file_backends_are_refused() -> None:
    class PlaintextKeyring(MemoryKeyring):
        pass

    PlaintextKeyring.__module__ = "keyrings.alt.file"
    previous = keyring.get_keyring()
    keyring.set_keyring(PlaintextKeyring())
    try:
        assert not KeyringCredentialStore().available()
    finally:
        keyring.set_keyring(previous)


def test_session_store_is_memory_only(tmp_path: Path) -> None:
    store = SessionCredentialStore()
    store.set(REF, FAKE_KEY)
    assert store.get(REF) == FAKE_KEY and store.delete(REF) and store.get(REF) is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("bad", ["", "   ", "sk live", "a\nb", "x" * 1001])
def test_key_validation(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_key(bad)


def test_masking_never_reveals_more_than_four_chars() -> None:
    assert mask_secret(FAKE_KEY).endswith(FAKE_KEY[-4:])
    assert FAKE_KEY[:-4] not in mask_secret(FAKE_KEY)
    assert mask_secret("short") == "••••••••"


def test_settings_file_never_contains_the_key(
    tmp_path: Path, memory_keyring: MemoryKeyring
) -> None:
    svc = CredentialService()
    settings = AppSettings()
    profile = ProviderProfile(
        profile_id="openai-main",
        name="OpenAI - Main",
        kind=ProviderKind.OPENAI,
        model_id="some-model",
    )
    settings.ai.profiles = [profile]
    svc.save(profile.credential_ref, FAKE_KEY)
    store = SettingsStore(tmp_path / "settings.json")
    store.save(settings)
    raw = store.path.read_text(encoding="utf-8")
    assert FAKE_KEY not in raw and "sk-test" not in raw
    assert profile.credential_ref in raw  # only the reference is persisted
    reloaded = store.load()
    assert reloaded.ai.profiles[0].credential_ref == "ai-pcb-router/openai-main"
    assert svc.get(reloaded.ai.profiles[0].credential_ref).get_secret_value() == FAKE_KEY  # type: ignore[union-attr]


def test_profile_repr_and_dump_have_no_secret_fields() -> None:
    p = ProviderProfile(profile_id="p1", name="n", kind=ProviderKind.ANTHROPIC)
    dumped: dict[str, Any] = p.model_dump()
    assert set(dumped) == {
        "profile_id",
        "name",
        "kind",
        "model_id",
        "base_url",
        "organization",
        "project",
        "timeout_s",
        "requires_api_key",
        "credential_ref",
    }
    assert p.credential_ref == "ai-pcb-router/p1"
    # credential_ref cannot be pointed at another entry by editing settings.
    assert (
        ProviderProfile(
            profile_id="p1", name="n", kind=ProviderKind.OPENAI, credential_ref="other/thing"
        ).credential_ref
        == "ai-pcb-router/p1"
    )


def test_a_non_secure_store_is_never_reported_as_secure() -> None:
    svc = CredentialService(secure=SessionCredentialStore())  # memory store injected
    assert not svc.secure_available
    with pytest.raises(SecureStorageUnavailable):
        svc.save(REF, FAKE_KEY)
