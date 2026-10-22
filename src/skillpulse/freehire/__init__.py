"""Optional freehire.me integration: live vacancies next to the HN corpus.

Import from here: :class:`FreehireClient` for a plain search, and
:class:`FreehireToolRuntime` to give the agent the ``freehire_search_jobs`` tool.
"""

from __future__ import annotations

from skillpulse.freehire.client import (
    DEFAULT_BASE_URL,
    FreehireClient,
    FreehireError,
    SearchPage,
)
from skillpulse.freehire.tool import (
    FREEHIRE_TOOL_NAME,
    FREEHIRE_TOOL_SPEC,
    FreehireSearchArgs,
    FreehireToolRuntime,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "FREEHIRE_TOOL_NAME",
    "FREEHIRE_TOOL_SPEC",
    "FreehireClient",
    "FreehireError",
    "FreehireSearchArgs",
    "FreehireToolRuntime",
    "SearchPage",
]
