"""The live-fetch authorization gate (docs/terms.md).

Live Hacker News access is gated until an explicit authorization decision is recorded,
so ``runtime.build_client`` — the one place every live path builds its client — refuses
while the setting is false. These tests run in a temp working directory so the repo's
``.env`` file cannot leak into the result.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skillpulse.config import Settings
from skillpulse.runtime import LiveFetchDisabledError, build_client


def test_setting_defaults_to_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SKILLPULSE_HN_LIVE_FETCH_ENABLED", raising=False)
    assert Settings().hn_live_fetch_enabled is False


def test_build_client_refuses_while_gated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SKILLPULSE_HN_LIVE_FETCH_ENABLED", raising=False)
    with pytest.raises(LiveFetchDisabledError):
        build_client()


def test_build_client_allows_only_when_explicitly_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKILLPULSE_HN_LIVE_FETCH_ENABLED", "true")
    with build_client() as client:
        assert client.base_url.startswith("http")
