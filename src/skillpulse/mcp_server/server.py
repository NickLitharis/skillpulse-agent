"""MCP server (M5): the M4 read-only tools over stdio and streamable HTTP.

The server is a thin protocol shell around :class:`skillpulse.tools.ToolRuntime`, so
every MCP tool call goes through the exact same registry, fixed parameterized SQL,
read-only session, bounds and content-addressed ``result_id`` the LangGraph agent uses.
There is deliberately no MCP-only query path and no SQL/filesystem/shell tool.

Safety rules enforced here:

* a tool error is reduced to a short safe message before it reaches the client; no
  database URL, SQL text or credential is ever put on the wire;
* the streamable HTTP transport binds loopback by default and keeps DNS-rebinding
  protection on (Host and Origin allow-lists) so it is not exposed by accident;
* on stdio, stdout carries protocol frames only; logs go to stderr.

``mcp`` 2.x renamed ``FastMCP`` to ``MCPServer``; this module uses the current API.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from time import perf_counter
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field, ValidationError
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request
from starlette.responses import Response

from skillpulse.config import Settings, get_settings
from skillpulse.db import make_engine, make_session_factory
from skillpulse.eval.fixture import build_fixture_engine
from skillpulse.extraction.taxonomy import Taxonomy, load_taxonomy
from skillpulse.logging_config import configure_logging
from skillpulse.observability import metrics_content, observe_mcp_tool
from skillpulse.tools import ToolRuntime
from skillpulse.tools.registry import ToolInputError

logger = logging.getLogger(__name__)

SERVER_NAME = "skillpulse"
SERVER_VERSION = "0.1.0"
TAXONOMY_URI = "skillpulse://taxonomy"
_SAFE_TOOL_ERROR = "the tool could not complete the request"
_SAFE_DB_ERROR = "database unavailable"

# Mirrors the Pydantic bounds in skillpulse.tools.models; tests assert the parity.
Extractor = Literal["baseline", "llm"]


@dataclass
class AppContext:
    """Everything a tool handler needs for the life of one server process."""

    runtime: ToolRuntime
    taxonomy: Taxonomy


def _run(ctx: Context[AppContext], tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run one shared tool and translate every failure to a safe tool error.

    ``ToolInputError`` messages (unknown tool, unknown skill, unknown role family) are
    safe to pass through because they only echo caller-supplied identifiers. Anything
    else is logged server-side by type and reduced to a generic message so a driver
    error can never leak the DSN or SQL.
    """
    started = perf_counter()
    outcome = "error"
    try:
        result = ctx.request_context.lifespan_context.runtime.call(tool, args)
        response = result.model_dump(mode="json")
        outcome = "ok"
        return response
    except ToolInputError as exc:
        raise ToolError(str(exc)) from None
    except ValidationError as exc:
        fields = sorted({".".join(str(part) for part in err["loc"]) for err in exc.errors()})
        raise ToolError(f"invalid arguments for {tool}: {', '.join(fields) or 'unknown'}") from None
    except (OperationalError, DBAPIError) as exc:
        logger.warning("mcp tool %s database error: %s", tool, type(exc).__name__)
        raise ToolError(_SAFE_DB_ERROR) from None
    except Exception as exc:
        logger.warning("mcp tool %s failed: %s", tool, type(exc).__name__)
        raise ToolError(_SAFE_TOOL_ERROR) from None
    finally:
        observe_mcp_tool(tool, outcome, perf_counter() - started)


def build_server(
    session_factory: sessionmaker[Session],
    taxonomy: Taxonomy | None = None,
    *,
    settings: Settings | None = None,
) -> MCPServer[AppContext]:
    """Build the MCP server bound to one session factory and taxonomy."""
    tax = taxonomy or load_taxonomy()
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_server: MCPServer[AppContext]) -> AsyncIterator[AppContext]:
        runtime = ToolRuntime(
            session_factory,
            tax,
            statement_timeout_ms=cfg.tool_statement_timeout_ms,
            max_rows=cfg.tool_max_rows,
        )
        yield AppContext(runtime=runtime, taxonomy=tax)

    server: MCPServer[AppContext] = MCPServer(
        SERVER_NAME,
        version=SERVER_VERSION,
        instructions=(
            "Read-only job-market tools over Hacker News 'Who is hiring?' posts. "
            "Counts cover eligible stored posts only; shares are withheld below 30 "
            "posts. No tool returns post text."
        ),
        lifespan=lifespan,
    )

    async def metrics(_request: Request) -> Response:
        content, content_type = metrics_content()
        return Response(content=content, headers={"Content-Type": content_type})

    server.custom_route("/metrics", methods=["GET"])(metrics)

    @server.tool(description="Structured post rows for filters; max 20; never post text.")
    def search_jobs(
        ctx: Context[AppContext],
        extractor: Extractor = "baseline",
        role_family: Annotated[str | None, Field()] = None,
        remote: Annotated[bool | None, Field()] = None,
        months: Annotated[int | None, Field(ge=1, le=240)] = None,
        month_from: Annotated[date | None, Field()] = None,
        month_to: Annotated[date | None, Field()] = None,
        skill_id: Annotated[str | None, Field()] = None,
        country: Annotated[str | None, Field(min_length=2, max_length=64)] = None,
        limit: Annotated[int, Field(ge=1, le=20)] = 20,
    ) -> dict[str, Any]:
        return _run(
            ctx,
            "search_jobs",
            {
                "extractor": extractor,
                "role_family": role_family,
                "remote": remote,
                "months": months,
                "month_from": month_from,
                "month_to": month_to,
                "skill_id": skill_id,
                "country": country,
                "limit": limit,
            },
        )

    @server.tool(description="Most requested skills for a filter with eligible denominator.")
    def skill_counts(
        ctx: Context[AppContext],
        extractor: Extractor = "baseline",
        role_family: Annotated[str | None, Field()] = None,
        remote: Annotated[bool | None, Field()] = None,
        months: Annotated[int | None, Field(ge=1, le=240)] = None,
        month_from: Annotated[date | None, Field()] = None,
        month_to: Annotated[date | None, Field()] = None,
        top_n: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        return _run(
            ctx,
            "skill_counts",
            {
                "extractor": extractor,
                "role_family": role_family,
                "remote": remote,
                "months": months,
                "month_from": month_from,
                "month_to": month_to,
                "top_n": top_n,
            },
        )

    @server.tool(description="Monthly share of one skill with numerator and denominator.")
    def skill_trend(
        ctx: Context[AppContext],
        skill_id: Annotated[str, Field()],
        extractor: Extractor = "baseline",
        role_family: Annotated[str | None, Field()] = None,
        remote: Annotated[bool | None, Field()] = None,
        months: Annotated[int | None, Field(ge=1, le=240)] = None,
        month_from: Annotated[date | None, Field()] = None,
        month_to: Annotated[date | None, Field()] = None,
    ) -> dict[str, Any]:
        return _run(
            ctx,
            "skill_trend",
            {
                "skill_id": skill_id,
                "extractor": extractor,
                "role_family": role_family,
                "remote": remote,
                "months": months,
                "month_from": month_from,
                "month_to": month_to,
            },
        )

    @server.tool(description="Side-by-side counts and trends for 1 to 5 skills.")
    def compare_skills(
        ctx: Context[AppContext],
        skill_ids: Annotated[list[str], Field(min_length=1, max_length=5)],
        extractor: Extractor = "baseline",
        role_family: Annotated[str | None, Field()] = None,
        remote: Annotated[bool | None, Field()] = None,
        months: Annotated[int | None, Field(ge=1, le=240)] = None,
        month_from: Annotated[date | None, Field()] = None,
        month_to: Annotated[date | None, Field()] = None,
    ) -> dict[str, Any]:
        return _run(
            ctx,
            "compare_skills",
            {
                "skill_ids": skill_ids,
                "extractor": extractor,
                "role_family": role_family,
                "remote": remote,
                "months": months,
                "month_from": month_from,
                "month_to": month_to,
            },
        )

    @server.tool(description="CV skills versus most demanded skills; CV text is never stored.")
    def match_cv(
        ctx: Context[AppContext],
        cv_text: Annotated[str, Field(min_length=1, max_length=20_000)],
        extractor: Extractor = "baseline",
        role_family: Annotated[str | None, Field()] = None,
        remote: Annotated[bool | None, Field()] = None,
        months: Annotated[int | None, Field(ge=1, le=240)] = None,
        month_from: Annotated[date | None, Field()] = None,
        month_to: Annotated[date | None, Field()] = None,
        top_n: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        return _run(
            ctx,
            "match_cv",
            {
                "cv_text": cv_text,
                "extractor": extractor,
                "role_family": role_family,
                "remote": remote,
                "months": months,
                "month_from": month_from,
                "month_to": month_to,
                "top_n": top_n,
            },
        )

    @server.resource(
        TAXONOMY_URI,
        name="taxonomy",
        description="Taxonomy version, skills and role families the tools validate against.",
        mime_type="application/json",
    )
    def taxonomy_resource() -> str:
        payload = {
            "version": tax.version,
            "skills": [
                {"id": skill.id, "name": skill.name, "category": skill.category}
                for skill in tax.skills
            ],
            "role_families": [{"id": role.id, "name": role.name} for role in tax.role_families],
        }
        return json.dumps(payload, separators=(",", ":"))

    return server


def _is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"} or host.startswith("127.")


def build_transport_security(host: str, port: int, settings: Settings) -> TransportSecuritySettings:
    """Allow only the configured Host/Origin values plus the exact bind address."""
    hosts = list(dict.fromkeys([*settings.mcp_allowed_hosts, f"{host}:{port}"]))
    origins = list(dict.fromkeys([*settings.mcp_allowed_origins, f"http://{host}:{port}"]))
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: ``skillpulse-mcp --transport stdio|streamable-http``."""
    import argparse

    parser = argparse.ArgumentParser(prog="skillpulse-mcp", description="SkillPulse MCP server")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default=None, help="HTTP bind host (loopback by default)")
    parser.add_argument("--port", type=int, default=None, help="HTTP bind port")
    parser.add_argument("--json-response", action="store_true", help="HTTP: JSON instead of SSE")
    parser.add_argument("--stateless", action="store_true", help="HTTP: no session state")
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="Use the deterministic no-text SQLite fixture DB instead of Postgres.",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)

    engine = build_fixture_engine() if args.fixture else make_engine(settings.database_url)
    factory = make_session_factory(engine)
    server = build_server(factory, load_taxonomy(), settings=settings)
    try:
        if args.transport == "stdio":
            server.run(transport="stdio")
        else:
            host = args.host or settings.mcp_host
            port = args.port or settings.mcp_port
            if not _is_loopback(host) and not settings.mcp_allow_non_loopback:
                parser.error(
                    f"refusing to bind non-loopback host {host!r}; set "
                    "SKILLPULSE_MCP_ALLOW_NON_LOOPBACK=true only behind a controlled network"
                )
            server.run(
                transport="streamable-http",
                host=host,
                port=port,
                streamable_http_path=settings.mcp_path,
                json_response=args.json_response,
                stateless_http=args.stateless,
                transport_security=build_transport_security(host, port, settings),
            )
    except KeyboardInterrupt:  # pragma: no cover - operator interrupted
        return 130
    finally:
        engine.dispose()
    return 0


__all__ = [
    "SERVER_NAME",
    "SERVER_VERSION",
    "TAXONOMY_URI",
    "AppContext",
    "build_server",
    "build_transport_security",
    "main",
]
