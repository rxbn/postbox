"""Log request metadata without recording request or response data."""

import logging
from contextvars import ContextVar
from time import monotonic
from typing import Any
from uuid import uuid4

from mcp.server import MCPServer, ServerRequestContext
from mcp.server.context import CallNext, HandlerResult
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)
request_id: ContextVar[str] = ContextVar("audit_request_id", default="-")
HTTP_METHODS = {"GET", "HEAD", "POST", "DELETE", "OPTIONS", "PUT", "PATCH", "CONNECT", "TRACE"}
MCP_METHODS = {
    "initialize",
    "server/discover",
    "ping",
    "tools/list",
    "tools/call",
    "resources/list",
    "resources/templates/list",
    "resources/read",
    "prompts/list",
    "prompts/get",
    "completion/complete",
    "logging/setLevel",
    "notifications/initialized",
    "notifications/cancelled",
    "notifications/progress",
}


class HttpAudit:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = monotonic()
        identifier = uuid4().hex
        token = request_id.set(identifier)
        method = scope["method"] if scope["method"] in HTTP_METHODS else "other"
        path = scope["path"] if scope["path"] in {"/mcp", "/healthz"} else "other"
        status: int | str = "-"

        async def track_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, track_status)
        finally:
            request_id.reset(token)
            log.info(
                "http request_id=%s method=%s route=%s status=%s duration_ms=%.1f",
                identifier,
                method,
                path,
                status,
                (monotonic() - started) * 1000,
            )


class McpAudit:
    def __init__(self, server: MCPServer[None]) -> None:
        self.server = server

    async def __call__(
        self, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        started = monotonic()
        method = ctx.method if ctx.method in MCP_METHODS else "other"
        tool = "-"
        outcome = "interrupted"
        try:
            if method == "tools/call":
                name = (ctx.params or {}).get("name")
                tool = next(
                    (tool.name for tool in await self.server.list_tools() if tool.name == name),
                    "unknown",
                )
            result = await call_next(ctx)
            # SDK middleware receives the serialized MCP result.
            outcome = "error" if isinstance(result, dict) and result.get("isError") else "ok"
            return result
        except Exception:
            outcome = "error"
            raise
        finally:
            log.info(
                "mcp request_id=%s method=%s tool=%s outcome=%s duration_ms=%.1f",
                request_id.get(),
                method,
                tool,
                outcome,
                (monotonic() - started) * 1000,
            )
