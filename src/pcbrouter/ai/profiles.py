"""Provider profiles: the *non-secret* configuration of one AI endpoint.

A profile is safe to persist in ``settings.json``. It holds a ``credential_ref`` —
the name of an OS-keyring entry — never the API key itself.
"""

from __future__ import annotations

import re
import uuid
from enum import StrEnum
from typing import Self
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pcbrouter import APP_SLUG


class ProviderKind(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    #: Any server speaking the OpenAI Chat Completions protocol: LM Studio, Ollama,
    #: vLLM, llama.cpp server, hosted gateways…
    OPENAI_COMPATIBLE = "openai_compatible"

    @property
    def display_name(self) -> str:
        return {
            ProviderKind.OPENAI: "OpenAI",
            ProviderKind.ANTHROPIC: "Anthropic",
            ProviderKind.OPENAI_COMPATIBLE: "OpenAI-compatible / local",
        }[self]


#: Official endpoints, passed explicitly so SDK environment variables such as
#: ``OPENAI_BASE_URL`` can never silently redirect requests.
DEFAULT_BASE_URLS: dict[ProviderKind, str] = {
    ProviderKind.OPENAI: "https://api.openai.com/v1",
    ProviderKind.ANTHROPIC: "https://api.anthropic.com",
}

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def new_profile_id(kind: ProviderKind) -> str:
    return f"{kind.value.replace('_', '-')}-{uuid.uuid4().hex[:8]}"


def credential_ref_for(profile_id: str) -> str:
    return f"{APP_SLUG}/{profile_id}"


class ProviderProfile(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    profile_id: str
    name: str = Field(min_length=1, max_length=60)
    kind: ProviderKind
    #: Model IDs live in configuration, never in code. Empty until chosen.
    model_id: str = Field(default="", max_length=200)
    base_url: str | None = Field(default=None, max_length=300)
    organization: str | None = Field(default=None, max_length=100)  # OpenAI only
    project: str | None = Field(default=None, max_length=100)  # OpenAI only
    timeout_s: float = Field(default=90.0, ge=5.0, le=600.0)
    requires_api_key: bool = True
    #: Keyring entry name (a reference, not a secret). Derived from ``profile_id``.
    credential_ref: str = ""

    @field_validator("profile_id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not _ID_RE.match(value):
            raise ValueError("profile_id must be lowercase letters, digits and dashes")
        return value

    @field_validator("model_id", "name")
    @classmethod
    def _strip(cls, value: str) -> str:
        if any(ord(ch) < 32 for ch in value):
            raise ValueError("control characters are not allowed")
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip().rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("base URL must start with http:// or https:// and include a host")
        return value

    @model_validator(mode="after")
    def _derive(self) -> Self:
        expected = credential_ref_for(self.profile_id)
        if self.credential_ref != expected:
            object.__setattr__(self, "credential_ref", expected)
        if self.kind is not ProviderKind.OPENAI_COMPATIBLE and not self.requires_api_key:
            object.__setattr__(self, "requires_api_key", True)
        if self.kind is ProviderKind.OPENAI_COMPATIBLE and self.base_url is None:
            raise ValueError("an OpenAI-compatible profile needs a base URL")
        return self

    @property
    def effective_base_url(self) -> str:
        return self.base_url or DEFAULT_BASE_URLS.get(self.kind, "")

    @property
    def is_insecure_remote_http(self) -> bool:
        """True for plain-http endpoints that are not on this machine (keys sent in clear)."""
        parsed = urlparse(self.effective_base_url)
        return parsed.scheme == "http" and (parsed.hostname or "") not in _LOCAL_HOSTS

    @property
    def label(self) -> str:
        return f"{self.name} ({self.kind.display_name})"
