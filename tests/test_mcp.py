import asyncio
import re
import ssl
import sys

import pytest
import trustme
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from pydantic import SecretStr, ValidationError
from starlette.testclient import TestClient

from postbox.config import Settings
from postbox.errors import MailError
from postbox.imap import HEADER_FIELD, Mailbox
from postbox.models import Compose
from postbox.server import create_server, http_app
from postbox.service import MailService

from .conftest import email_bytes

TOKEN = "synthetic-mcp-test-token-only-for-ci-00"
READ = {
    "list_folders",
    "list_mailbox_status",
    "list_emails",
    "search_emails",
    "get_email",
    "get_thread",
    "get_attachment",
}
WRITE = {
    "mark_email",
    "move_email",
    "delete_email",
    "send_email",
    "reply_email",
    "create_draft",
    "update_draft",
    "delete_draft",
}


async def test_read_only_tools_and_execution_guard(cfg):
    service = MailService(cfg)
    async with Client(create_server(cfg, service)) as client:
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == READ
        assert all(t.annotations.read_only_hint and t.output_schema for t in tools)
        assert (await client.call_tool("send_email", {})).is_error
    with pytest.raises(MailError, match="read-only"):
        await service.send(Compose(to=[], subject="", body_text=""))


async def test_tool_schemas_annotations_and_malicious_inputs(settings_factory, fake_imap, ref):
    cfg = settings_factory(mcp_read_only=False)
    service = MailService(cfg, Mailbox(cfg, factory=lambda *a, **kw: fake_imap))
    async with Client(create_server(cfg, service)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == READ | WRITE
        send = tools["send_email"]
        assert {"to", "subject", "body_text"} == set(send.input_schema["required"])
        assert "from" not in send.input_schema["properties"]
        assert send.annotations.open_world_hint and not send.annotations.read_only_hint
        assert tools["update_draft"].annotations.destructive_hint
        assert tools["mark_email"].annotations.idempotent_hint
        assert not tools["delete_email"].annotations.destructive_hint
        for name, arguments in [
            ("list_emails", {"limit": 101}),
            ("list_emails", {"folder": "INBOX\r\nUID EXPUNGE"}),
            ("get_email", {"message": ref.model_dump() | {"uid": 0}}),
            ("search_emails", {"text": "a\r\nb"}),
            (
                "send_email",
                {"to": ["x@example.test"], "subject": "a\nBcc: x", "body_text": "hello"},
            ),
            (
                "send_email",
                {
                    "to": ["x@example.test"],
                    "subject": "a",
                    "body_text": "hello",
                    "from": "other@example.test",
                },
            ),
        ]:
            assert (await client.call_tool(name, arguments)).is_error, name
        assert not fake_imap.calls

    enabled = settings_factory(mcp_read_only=False, allow_permanent_delete=True)
    async with Client(create_server(enabled)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert tools["permanently_delete_email"].annotations.destructive_hint


def test_http_auth_and_legacy_mcp_session(settings_factory, fake_imap):
    cfg = settings_factory(mcp_transport="http", mcp_auth_token=SecretStr(TOKEN))
    service = MailService(cfg, Mailbox(cfg, factory=lambda *a, **kw: fake_imap))
    headers = {"Accept": "application/json, text/event-stream"}
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        },
    }
    with TestClient(
        http_app(cfg, create_server(cfg, service)), base_url="http://localhost"
    ) as http:
        assert http.get("/healthz").json() == {"status": "ok"}
        assert http.post("/mcp", json=initialize, headers=headers).status_code == 401
        wrong = headers | {"Authorization": "Bearer wrong"}
        assert http.post("/mcp", json=initialize, headers=wrong).status_code == 401
        assert not fake_imap.calls
        auth = headers | {"Authorization": f"Bearer {TOKEN}"}
        assert (
            http.post("/mcp", json=initialize, headers=auth | {"Host": "other.test"}).status_code
            == 421
        )
        assert (
            http.post(
                "/mcp", json=initialize, headers=auth | {"Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        response = http.post("/mcp", json=initialize, headers=auth)
        assert response.json()["result"]["serverInfo"]["name"] == "Postbox"
        assert "mcp-session-id" not in response.headers
        auth["MCP-Protocol-Version"] = response.json()["result"]["protocolVersion"]
        result = http.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_mailbox_status", "arguments": {}},
            },
            headers=auth,
        )
        assert result.json()["result"]["structuredContent"]["uid_validity"] == 55


def test_http_requires_token_and_secret_files(settings_factory, tmp_path, monkeypatch):
    with pytest.raises(ValidationError, match="MCP_AUTH_TOKEN"):
        settings_factory(mcp_transport="http")
    (tmp_path / "IMAP_PASSWORD").write_text("secret-file-pass\n")
    (tmp_path / "MCP_AUTH_TOKEN").write_text(f"{TOKEN}\n")
    monkeypatch.setenv("IMAP_HOST", "localhost")
    monkeypatch.setenv("IMAP_USERNAME", "mailbox@example.test")
    monkeypatch.setenv("MAIL_ADDRESS", "mailbox@example.test")
    settings = Settings(_secrets_dir=tmp_path)
    assert settings.imap_password.get_secret_value() == "secret-file-pass"
    assert settings.mcp_auth_token.get_secret_value() == TOKEN
    assert "secret-file-pass" not in repr(settings)


@pytest.mark.parametrize("enabled", [True, False])
def test_request_audit_metadata_only(settings_factory, fake_imap, caplog, enabled):
    cfg = settings_factory(
        mcp_transport="http", mcp_auth_token=SecretStr(TOKEN), mcp_audit_log=enabled
    )
    service = MailService(cfg, Mailbox(cfg, factory=lambda *a, **kw: fake_imap))
    headers = {"Accept": "application/json, text/event-stream"}
    auth = headers | {"Authorization": f"Bearer {TOKEN}"}
    private = "private-input\nforged-audit-event"
    caplog.set_level("INFO", logger="postbox.audit")
    with TestClient(
        http_app(cfg, create_server(cfg, service)), base_url="http://localhost"
    ) as http:
        assert http.get("/healthz").status_code == 200
        assert http.get("/private-path").status_code == 404
        assert http.post("/mcp", headers=headers).status_code == 401
        initialized = http.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": private, "version": "1"},
                },
            },
            headers=auth,
        )
        auth["MCP-Protocol-Version"] = initialized.json()["result"]["protocolVersion"]
        for method, params in [
            ("tools/call", {"name": "list_emails", "arguments": {}}),
            ("tools/call", {"name": "search_emails", "arguments": {"text": private}}),
            ("tools/call", {"name": private}),
            (private, {}),
        ]:
            http.post(
                "/mcp",
                params={"q": private},
                json={"jsonrpc": "2.0", "id": private, "method": method, "params": params},
                headers=auth,
            )

    messages = [record.getMessage() for record in caplog.records if record.name == "postbox.audit"]
    if not enabled:
        assert not messages
        return
    assert len(messages) == 13
    logged = "\n".join(messages)
    for expected in (
        "method=GET route=/healthz status=200",
        "method=GET route=other status=404",
        "method=POST route=/mcp status=401",
        "method=tools/call tool=list_emails outcome=ok",
        "method=tools/call tool=search_emails outcome=error",
        "method=tools/call tool=unknown outcome=error",
        "method=other tool=- outcome=error",
    ):
        assert expected in logged
    match = re.search(r"mcp request_id=(\w+) method=tools/call tool=list_emails", logged)
    assert match and f"http request_id={match[1]} method=POST route=/mcp status=200" in logged
    for sensitive in (TOKEN, "private-input", "private-path", "forged-audit-event", "Café meeting"):
        assert sensitive not in logged


@pytest.fixture
async def imap_wire(tmp_path):
    ca = trustme.CA()
    cert = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(cert)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ca.issue_cert("127.0.0.1").configure_cert(ctx)
    raw = email_bytes()
    headers = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
    commands = []

    async def serve(reader, writer):
        writer.write(b"* OK Postbox test IMAP\r\n")
        await writer.drain()
        try:
            while line := await reader.readline():
                tag, command, *rest = line.strip().split(maxsplit=2)
                args = rest[0] if rest else b""
                commands.append((command, args))
                if command == b"CAPABILITY":
                    writer.write(b"* CAPABILITY IMAP4rev1\r\n")
                elif command == b"LOGIN":
                    assert args == b'"mailbox@example.test" "synthetic-imap-password"'
                elif command == b"EXAMINE":
                    writer.write(b"* 1 EXISTS\r\n* OK [UIDVALIDITY 55] valid\r\n")
                elif command == b"UID" and args.startswith(b"SEARCH "):
                    writer.write(b"* SEARCH 42\r\n")
                elif command == b"UID" and b"BODY.PEEK[HEADER.FIELDS" in args:
                    body = (
                        b'(("text" "plain" NIL NIL NIL "7bit" 12 1)'
                        b'("application" "pdf" NIL NIL NIL "base64" 12 NIL '
                        b'("attachment" ("filename*" "utf-8\'\'R%C3%A9sum%C3%A9.pdf"))) "mixed")'
                    )
                    fields = HEADER_FIELD.encode() + b"<0> {" + str(len(headers)).encode() + b"}"
                    writer.write(
                        b"* 1 FETCH (UID 42 FLAGS () BODYSTRUCTURE "
                        + body
                        + b" "
                        + fields
                        + b"\r\n"
                        + headers
                        + b")\r\n"
                    )
                elif command == b"UID" and b"RFC822.SIZE" in args:
                    writer.write(
                        b"* 1 FETCH (UID 42 FLAGS () RFC822.SIZE "
                        + str(len(raw)).encode()
                        + b")\r\n"
                    )
                elif command == b"UID" and b"BODY.PEEK[]" in args:
                    writer.write(
                        b"* 1 FETCH (UID 42 BODY[]<0> {"
                        + str(len(raw)).encode()
                        + b"}\r\n"
                        + raw
                        + b")\r\n"
                    )
                else:
                    writer.write(tag + b" BAD unsupported\r\n")
                    await writer.drain()
                    continue
                writer.write(tag + b" OK command complete\r\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=ctx)
    async with server:
        yield server.sockets[0].getsockname()[1], cert, commands


async def test_stdio_mcp_smoke_against_tls_imap(imap_wire):
    port, cert, commands = imap_wire
    environment = {
        "IMAP_HOST": "127.0.0.1",
        "IMAP_PORT": str(port),
        "IMAP_USERNAME": "mailbox@example.test",
        "IMAP_PASSWORD": "synthetic-imap-password",
        "MAIL_ADDRESS": "mailbox@example.test",
        "MAIL_CA_FILE": str(cert),
        "MCP_TRANSPORT": "stdio",
        "MCP_READ_ONLY": "true",
        "MCP_AUDIT_LOG": "true",
    }
    server = StdioServerParameters(command=sys.executable, args=["-m", "postbox"], env=environment)
    async with Client(server, read_timeout_seconds=5) as client:
        assert {t.name for t in (await client.list_tools()).tools} == READ
        page = await client.call_tool("list_emails", {"limit": 1})
        assert not page.is_error, page.content
        item = page.structured_content["emails"][0]
        assert item["message"] == {"folder": "INBOX", "uid": 42, "uid_validity": 55}
        assert item["subject"] == "Café meeting" and item["has_attachments"]
        result = await client.call_tool("get_email", {"message": item["message"]})
        assert not result.is_error, result.content
        assert result.structured_content["body_text"].strip() == "Plain café text."
    assert any(command == b"EXAMINE" for command, _ in commands)
    assert not any(command == b"SELECT" for command, _ in commands)
