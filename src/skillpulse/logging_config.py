"""Logging setup shared by the CLI and (later) the workers."""

from __future__ import annotations

import logging

_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    """Configure root logging once, in a container-friendly single-line format."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # httpx logs every request at INFO, which drowns out our own progress lines.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _CONFIGURED = True
