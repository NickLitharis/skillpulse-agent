"""``python -m skillpulse.mcp_server`` entry point."""

from __future__ import annotations

import sys

from skillpulse.mcp_server.server import main

if __name__ == "__main__":
    sys.exit(main())
