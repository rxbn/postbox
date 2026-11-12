import base64

import pytest
from pydantic import ValidationError

from postbox import mime
from postbox.errors import MailError
from postbox.models import Compose, OutgoingAttachment

from .conftest import email_bytes


def test_multipart_unicode_and_attachment_limits(ref, settings_factory):
    cfg = settings_factory(max_attachment_bytes=8)
    parsed = mime.parse(
        email_bytes(references="<first@example.test>", reply_to="<parent@example.test>")
    )
    detail = mime.detail(parsed, ref, [], cfg, include_html=False)
    assert detail.subject == "Café meeting"
    assert detail.sender[0].name == "Miyuki 山田"
    assert detail.date == "2026-11-10T09:00:00+00:00"
    assert detail.unread and detail.has_attachments
    assert detail.body_text.strip() == "Plain café text."
    assert detail.body_html is None
    assert detail.in_reply_to == ["<parent@example.test>"]
    assert detail.references == ["<first@example.test>"]
    assert detail.attachments[0].filename == "Résumé.pdf"
    blocked = mime.get_attachment(parsed, 1, cfg)
    assert (
        blocked.size_bytes == 9 and not blocked.inline_available and blocked.content_base64 is None
    )
    allowed = mime.get_attachment(parsed, 1, settings_factory(max_attachment_bytes=9))
    assert base64.b64decode(allowed.content_base64) == b"123456789"
    assert "HTML café" in mime.detail(parsed, ref, [], cfg, include_html=True).body_html
    with pytest.raises(MailError, match="Attachment not found"):
        mime.get_attachment(parsed, 2, cfg)


def test_html_only_and_truncated_body(cfg, ref):
    raw = b"Content-Type: text/html; charset=us-ascii\r\n\r\n<p>HTML only</p>"
    parsed = mime.parse(raw)
    assert mime.detail(parsed, ref, [], cfg, include_html=False).body_text is None
    assert mime.extract_body(parsed, "html", 5) == ("<p>HT", True)


def test_reply_all_deduplicates_and_preserves_thread(cfg):
    parent = mime.parse(email_bytes(reply_to="<older@example.test>"))
    to, cc = mime.reply_recipients(parent, cfg.mail_address, reply_all=True)
    assert to == ["reply@example.test", "teammate@example.test"]
    assert cc == ["extra@example.test"]
    assert mime.reply_recipients(parent, cfg.mail_address, reply_all=False) == (
        ["reply@example.test"],
        [],
    )
    message, envelope = mime.build_message(
        Compose(to=to, cc=cc, subject="Re: Café", body_text="Merci"), cfg, parent=parent
    )
    assert str(message["In-Reply-To"]) == "<root@example.test>"
    assert str(message["References"]) == "<older@example.test> <root@example.test>"
    assert str(message["From"]) == "Zoë Mailbox <mailbox@example.test>"
    assert message["Date"] and message["Message-ID"]
    assert envelope == to + cc


def test_reply_to_sent_message_skips_self(cfg):
    original = mime.parse(
        b"From: mailbox@example.test\r\nTo: correspondent@example.test\r\n\r\nHello"
    )
    assert mime.reply_recipients(original, cfg.mail_address, reply_all=False) == (
        ["correspondent@example.test"],
        [],
    )


@pytest.mark.parametrize(
    "to", [["x@example.test\r\nBcc: y@example.test"], ["X <x@example.test>"], []]
)
def test_invalid_recipient(cfg, to):
    with pytest.raises(MailError):
        mime.build_message(Compose(to=to, subject="subject", body_text="text"), cfg)


def test_malicious_subject_attachment_and_size(cfg, settings_factory):
    with pytest.raises(ValidationError):
        Compose(to=[], subject="test\nBcc: someone@example.test", body_text="")
    assert mime.safe_filename("..\\somewhere/..\\..\x00.pdf") == "pdf"
    data = Compose(
        to=["x@example.test"],
        subject="Test",
        body_text="",
        attachments=[OutgoingAttachment(filename="../../local", content_base64="not+base64!")],
    )
    with pytest.raises(MailError, match="base64"):
        mime.build_message(data, cfg)
    data.attachments[0].content_base64 = "YWJj"
    data.attachments[0].content_type = "text/plain; charset=weird"
    with pytest.raises(MailError, match="content_type"):
        mime.build_message(data, cfg)
    tiny = settings_factory(max_outgoing_bytes=1024)
    data.attachments = []
    data.body_text = "x" * 3000
    with pytest.raises(MailError, match="MAX_OUTGOING_BYTES"):
        mime.wire_bytes(mime.build_message(data, tiny)[0], tiny)
