"""BP30 (BP28 #20) — the bge-m3 token counter loads the kit-seeded LOCAL
file when present (zero egress on a deployed box); the HF hub download is
the dev-bench fallback only. Pure fakes: the real tokenizer never loads
here, so the suite itself stays offline."""
from __future__ import annotations

import pytest

import knowledge_hub.chunking as chunking
from knowledge_hub.config import settings


class _FakeTokenizer:
    calls: list[tuple[str, str]] = []

    @classmethod
    def from_file(cls, path):
        cls.calls.append(("from_file", path))
        return cls()

    @classmethod
    def from_pretrained(cls, name):
        cls.calls.append(("from_pretrained", name))
        return cls()

    def encode(self, text, add_special_tokens=False):
        class _Enc:
            ids = list(range(len(text.split())))
        return _Enc()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Reset the module-global tokenizer cache and swap in the fake class
    at the import boundary chunking uses (`from tokenizers import
    Tokenizer` inside the function)."""
    import tokenizers
    _FakeTokenizer.calls = []
    monkeypatch.setattr(chunking, "_tokenizer", None)
    monkeypatch.setattr(tokenizers, "Tokenizer", _FakeTokenizer)
    yield
    chunking._tokenizer = None


def test_counter_prefers_the_local_tokenizer_file(tmp_path, monkeypatch):
    local = tmp_path / "tokenizer.json"
    local.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(settings, "bge_m3_tokenizer_json", str(local))

    counter = chunking._bge_m3_token_counter()

    assert _FakeTokenizer.calls == [("from_file", str(local))]
    assert counter("three word text") == 3


def test_counter_falls_back_to_the_hub(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "bge_m3_tokenizer_json",
                        str(tmp_path / "absent" / "tokenizer.json"))

    chunking._bge_m3_token_counter()

    assert _FakeTokenizer.calls == [
        ("from_pretrained", chunking.BGE_M3_TOKENIZER)]
