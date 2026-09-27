"""Phase 1 capability awareness: Ollama model facts, badges, and hints."""

from __future__ import annotations

from pcbrouter.ai import ollama
from pcbrouter.ai.models import AIModelInfo, model_hint

TAGS = {
    "models": [
        {
            "name": "qwen3:0.6b",
            "details": {"parameter_size": "751.63M", "context_length": 40960},
            "capabilities": ["completion", "tools", "thinking"],
        },
        {
            "name": "llama3.2:3b",
            "details": {"parameter_size": "3.2B", "context_length": 131072},
            "capabilities": ["completion", "tools"],
        },
        {"name": "bare", "details": {}, "capabilities": []},
    ]
}


def _fake_get(monkeypatch: object, payload: object) -> None:
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.setattr(ollama, "_get", lambda *a, **k: payload)


def test_model_facts_parse_details_and_capabilities(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _fake_get(monkeypatch, TAGS)
    facts = ollama.model_facts()
    assert facts["qwen3:0.6b"].thinking is True
    assert facts["qwen3:0.6b"].context_length == 40960
    assert facts["qwen3:0.6b"].parameter_size == "751.63M"
    assert facts["llama3.2:3b"].thinking is False
    assert facts["bare"].context_length is None
    assert facts["bare"].capabilities == ()


def test_model_facts_empty_when_unreachable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import urllib.error

    monkeypatch.setattr(
        ollama, "_get", lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("x"))
    )
    assert ollama.model_facts() == {}


def test_detailed_label_and_hint() -> None:
    thinker = AIModelInfo(
        provider_kind="openai_compatible",
        model_id="qwen3:0.6b",
        context_window=40960,
        supports_tools=True,
        metadata={"parameter_size": "751.63M", "thinking": True},
    )
    assert thinker.detailed_label == "qwen3:0.6b — 751.63M · ctx 40k · thinking · tools"
    hint = model_hint(thinker)
    assert "Reasoning model" in hint
    big = AIModelInfo(
        provider_kind="openai_compatible", model_id="llama3.2:3b", context_window=131072
    )
    assert "ctx 128k" in big.detailed_label
    assert "KV cache" in model_hint(big)
    plain = AIModelInfo(provider_kind="openai_compatible", model_id="qwen2.5:7b")
    assert plain.detailed_label == "qwen2.5:7b"
    assert model_hint(plain) == ""
