import asyncio
import ssl

import pytest
import trustme

from postbox import mime
from postbox.config import tls_context
from postbox.errors import MailError
from postbox.imap import Mailbox
from postbox.models import Compose
from postbox.service import MailService


@pytest.fixture
def outgoing():
    return Compose(
        to=["first@example.test"],
        cc=["copy@example.test"],
        bcc=["blind@example.test"],
        subject="A synthetic email",
        body_text="No external service received this message.",
    )


@pytest.fixture
async def smtp_sink(tmp_path, request):
    """Minimal local SMTP sink that authenticates over real TLS and never forwards mail."""
    ca = trustme.CA()
    ca_file = tmp_path / "root.pem"
    ca.cert_pem.write_to_path(ca_file)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ca.issue_cert("127.0.0.1").configure_cert(context)
    use_starttls = getattr(request, "param", None) == "starttls"
    state = {"messages": [], "envelope": [], "mode": "accept", "auth_attempts": 0}

    async def handle(reader, writer):
        async def say(response):
            writer.write(response + b"\r\n")
            await writer.drain()

        try:
            await say(b"220 localhost test SMTP")
            while line := await reader.readline():
                command = line.split(maxsplit=1)[0].upper()
                if command == b"EHLO":
                    if writer.get_extra_info("ssl_object") is None:
                        await say(b"250-localhost\r\n250 STARTTLS")
                    else:
                        await say(b"250-localhost\r\n250-AUTH PLAIN\r\n250 SIZE 999999")
                elif command == b"STARTTLS" and use_starttls:
                    await say(b"220 begin TLS")
                    await writer.start_tls(context)
                elif command == b"AUTH":
                    assert writer.get_extra_info("ssl_object") is not None
                    state["auth_attempts"] += 1
                    if state["mode"] == "bad_auth":
                        await say(b"535 synthetic-smtp-password")
                    else:
                        await say(b"235 authenticated")
                elif command in {b"MAIL", b"RCPT"}:
                    state["envelope"].append(line)
                    if state["mode"] == "partial" and b"copy@example.test" in line:
                        await say(b"550 no such user")
                    else:
                        await say(b"250 ok")
                elif command == b"DATA":
                    await say(b"354 send message")
                    raw = bytearray()
                    while (line := await reader.readline()) not in {b".\r\n", b""}:
                        raw.extend(line)
                    state["messages"].append(bytes(raw))
                    if state["mode"] == "ambiguous":
                        return
                    if state["mode"] == "reject":
                        await say(b"554 delivery rejected")
                    else:
                        await say(b"250 queued")
                elif command == b"RSET":
                    await say(b"250 reset")
                else:
                    await say(b"500 unsupported")
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(
        handle, "127.0.0.1", 0, ssl=None if use_starttls else context
    )
    async with server:
        yield server.sockets[0].getsockname()[1], ca_file, state


def test_tls_verification_defaults():
    context = tls_context(True)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    insecure = tls_context(False)
    assert insecure.verify_mode == ssl.CERT_NONE and not insecure.check_hostname


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("accept", "accepted"), ("partial", "partially_accepted"), ("ambiguous", "delivery_unknown")],
)
async def test_smtp_acceptance_partial_and_no_retry(
    settings_factory, fake_imap, outgoing, smtp_sink, mode, expected
):
    port, ca_file, state = smtp_sink
    state["mode"] = mode
    cfg = settings_factory(
        smtp_port=port,
        mcp_read_only=False,
        mail_ca_file=ca_file,
    )
    service = MailService(cfg, Mailbox(cfg, factory=lambda *a, **kw: fake_imap))
    result = await service.send(outgoing)
    assert result.status == expected
    assert len(state["messages"]) == 1
    wire = mime.parse(state["messages"][0])
    assert wire["Bcc"] is None
    assert wire["Message-ID"] == result.message_id
    assert any(b"blind@example.test" in line for line in state["envelope"])
    appends = [call for call in fake_imap.calls if call[0] == "append"]
    if mode == "ambiguous":
        assert result.sent_copy == "not_attempted" and not appends
        assert "before resending" in result.note
    else:
        assert result.sent_copy == "saved"
        assert len(appends) == 1 and appends[0][1] == "Outbox Copies"
        copy = mime.parse(appends[0][2])
        assert copy["Message-ID"] == wire["Message-ID"]
        assert str(copy["Bcc"]) == "blind@example.test"
    if mode == "partial":
        assert result.refused_recipients == ["copy@example.test"]


async def test_smtp_definite_rejection_and_auth_failure(settings_factory, outgoing, smtp_sink):
    port, ca_file, state = smtp_sink
    cfg = settings_factory(
        smtp_port=port, mcp_read_only=False, mail_ca_file=ca_file, append_sent=False
    )
    service = MailService(cfg)
    state["mode"] = "bad_auth"
    with pytest.raises(MailError, match="SMTP authentication failed") as error:
        await service.send(outgoing)
    assert "synthetic-smtp-password" not in str(error.value)
    assert not state["messages"]
    state["mode"] = "reject"
    with pytest.raises(MailError, match="SMTP rejected the submission"):
        await service.send(outgoing)
    assert len(state["messages"]) == 1


async def test_smtp_refuses_untrusted_cert_before_auth(settings_factory, outgoing, smtp_sink):
    port, _ca, state = smtp_sink
    cfg = settings_factory(smtp_port=port, mcp_read_only=False, append_sent=False)
    with pytest.raises(MailError, match="SMTP connection or login failed"):
        await MailService(cfg).send(outgoing)
    assert state["auth_attempts"] == 0


async def test_append_failure_does_not_reclassify_accepted_send(
    settings_factory, fake_imap, monkeypatch, outgoing, smtp_sink
):
    port, ca_file, state = smtp_sink
    cfg = settings_factory(smtp_port=port, mcp_read_only=False, mail_ca_file=ca_file)

    def fail(*_args, **_kwargs):
        raise OSError("untrusted server message")

    monkeypatch.setattr(fake_imap, "append", fail)
    service = MailService(cfg, Mailbox(cfg, factory=lambda *a, **kw: fake_imap))
    result = await service.send(outgoing)
    assert result.status == "accepted" and result.sent_copy == "failed"
    assert "Do not resend" in result.note and "untrusted" not in result.note
    assert len(state["messages"]) == 1


@pytest.mark.parametrize("smtp_sink", ["starttls"], indirect=True)
async def test_smtp_starttls_upgrades_before_auth(settings_factory, outgoing, smtp_sink):
    port, ca_file, state = smtp_sink
    cfg = settings_factory(
        smtp_port=port,
        smtp_security="starttls",
        mcp_read_only=False,
        mail_ca_file=ca_file,
        append_sent=False,
    )
    result = await MailService(cfg).send(outgoing)
    assert result.status == "accepted"
    assert state["auth_attempts"] == len(state["messages"]) == 1
