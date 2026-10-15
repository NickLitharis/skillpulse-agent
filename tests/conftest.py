"""Shared test fixtures."""

from __future__ import annotations

import pytest

from skillpulse.hn_client import HNItem


def make_item(
    *,
    item_id: int,
    title: str | None = None,
    text: str | None = None,
    kids: tuple[int, ...] = (),
    time: int = 1_700_000_000,
    deleted: bool = False,
    dead: bool = False,
    descendants: int | None = None,
) -> HNItem:
    """Build an ``HNItem`` for tests without touching the network."""
    return HNItem(
        id=item_id,
        type="comment" if title is None else "story",
        by="whoishiring" if title is not None else "someone",
        title=title,
        text=text,
        time=time,
        kids=kids,
        deleted=deleted,
        dead=dead,
        descendants=descendants,
    )


@pytest.fixture
def sample_thread_item() -> HNItem:
    """A realistic monthly hiring-thread item."""
    return make_item(
        item_id=1000,
        title="Ask HN: Who is hiring? (October 2026)",
        kids=(2001, 2002),
        descendants=2,
    )
