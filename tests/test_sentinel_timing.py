from __future__ import annotations

from regix.openai import quickjs


def test_behavior_duration_jitters_around_base(monkeypatch):
    monkeypatch.setenv("OPENAI_SENTINEL_BEHAVIOR_MS", "3000")
    monkeypatch.setenv("OPENAI_SENTINEL_BEHAVIOR_JITTER_MS", "120")

    values = {quickjs._behavior_duration_ms() for _ in range(30)}

    assert all(2880 <= value <= 3120 for value in values)
    assert len(values) > 1


def test_behavior_duration_can_be_disabled_for_tests(monkeypatch):
    monkeypatch.setenv("OPENAI_SENTINEL_BEHAVIOR_MS", "0")
    monkeypatch.setenv("OPENAI_SENTINEL_BEHAVIOR_JITTER_MS", "350")

    assert quickjs._behavior_duration_ms() == 0


def test_default_behavior_duration_keeps_human_like_jitter(monkeypatch):
    monkeypatch.delenv("OPENAI_SENTINEL_BEHAVIOR_MS", raising=False)
    monkeypatch.delenv("OPENAI_SENTINEL_BEHAVIOR_JITTER_MS", raising=False)

    values = {quickjs._behavior_duration_ms() for _ in range(30)}

    assert all(3150 <= value <= 3650 for value in values)
    assert len(values) > 1


def test_behavior_is_skipped_when_so_token_is_not_required(monkeypatch):
    monkeypatch.delenv("OPENAI_SENTINEL_BEHAVIOR_MS", raising=False)

    assert quickjs._behavior_duration_ms({"so": {"required": False}}) == 0
    assert quickjs._behavior_duration_ms({}) == 0
