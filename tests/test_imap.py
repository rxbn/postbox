import ssl
from datetime import date

import pytest
from imapclient.exceptions import LoginError

from postbox.errors import MailError
from postbox.imap import Mailbox, search_ids, search_queries
from postbox.models import Compose, MessageRef, Search
from postbox.service import MailService

from .conftest import email_bytes


def test_search_filters_and_quoting(fake_imap):
    filters = Search(
        sender="alice@example.test",
        recipient="family@example.test",
        subject='") OR ALL',
        text="Grüße",
        date_from=date(2026, 11, 1),
        date_to=date(2026, 11, 9),
        unread=False,
    )
    ascii_query, utf8_query = search_queries(filters)
    assert ascii_query == (
        r'FROM "alice@example.test" OR TO "family@example.test" OR CC "family@example.test" '
        r'BCC "family@example.test" SUBJECT "\") OR ALL" SINCE 01-Nov-2026 BEFORE 10-Nov-2026 SEEN'
    )
    assert utf8_query == ["TEXT", "Grüße"]
    fake_imap.selected = "INBOX"
    assert search_ids(fake_imap, filters) == [42]
    assert ("search", utf8_query, "UTF-8") in fake_imap.calls
    assert ("search", ascii_query, None) in fake_imap.calls
    with pytest.raises(MailError, match="date_from"):
        search_queries(Search(date_from=date(2026, 11, 10), date_to=date(2026, 11, 9)))


def test_uid_pagination_and_peek(cfg, fake_imap, ref):
    fake_imap.mail["INBOX"] |= {10: email_bytes(), 100: email_bytes()}
    fake_imap.flags[("INBOX", 42)] = [rb"\Seen"]
    box = Mailbox(cfg)
    page = box.page(fake_imap, "INBOX", Search(unread=True), 2, 0)
    assert [email.message.uid for email in page.emails] == [100, 42]
    assert page.emails[1].message == ref
    assert page.emails[0].has_attachments
    assert page.next_offset == 2
    assert ("search", "UNSEEN", None) in fake_imap.calls
    last = box.page(fake_imap, "INBOX", Search(unread=True), 2, page.next_offset)
    assert [email.message.uid for email in last.emails] == [10]
    assert last.next_offset is None
    assert ("select", "INBOX", True) in fake_imap.calls
    assert all(
        "BODY.PEEK" in field
        for call in fake_imap.calls
        if call[0] == "fetch"
        for field in call[2]
        if field.startswith("BODY[") or field.startswith("BODY.PEEK")
    )


def test_stale_uids_never_fetch_or_mutate(cfg, fake_imap, ref):
    fake_imap.validity = 56
    box = Mailbox(cfg)
    with pytest.raises(MailError, match="UIDVALIDITY"):
        box.mark(fake_imap, ref, unread=True, flagged=None)
    assert fake_imap.calls == [("select", "INBOX", False)]


def test_message_size_checked_before_body(settings_factory, fake_imap, ref):
    fake_imap.mail["INBOX"][42] += b"x" * 1024
    with pytest.raises(MailError, match="MAX_MESSAGE_BYTES"):
        Mailbox(settings_factory(max_message_bytes=1024)).message(fake_imap, ref)
    assert fake_imap.calls[-1] == ("fetch", [42], ["FLAGS", "RFC822.SIZE"])


def test_thread_across_folders_transitively(cfg, fake_imap, ref):
    fake_imap.mail["Outbox Copies"][100] = email_bytes(
        message_id="<reply@example.test>", reply_to="<root@example.test>"
    )
    fake_imap.mail["INBOX"][101] = email_bytes(
        message_id="<later@example.test>", references="<reply@example.test>"
    )
    fake_imap.mail["INBOX"][102] = email_bytes(message_id="<unrelated@example.test>")
    thread = Mailbox(cfg).thread(fake_imap, ref)
    assert {message.message_id for message in thread.emails} == {
        "<root@example.test>",
        "<reply@example.test>",
        "<later@example.test>",
    }
    assert "Outbox Copies" in thread.folders_scanned
    assert "Hierarchy" not in thread.folders_scanned


def test_move_falls_back_only_to_uid_expunge(cfg, fake_imap, ref):
    fake_imap.capabilities.clear()
    box = Mailbox(cfg)
    with pytest.raises(MailError, match="targeted removal"):
        box.move(fake_imap, ref, "Trash")
    assert not any(call[0] == "copy" for call in fake_imap.calls)
    fake_imap.capabilities = {"UIDPLUS"}
    box.move(fake_imap, ref, "Trash")
    assert fake_imap.calls[-3:] == [
        ("copy", [42], "Trash"),
        ("add_flags", [42], [r"\Deleted"]),
        ("expunge", [42]),
    ]


async def test_draft_create_update_and_ambiguous_removal(settings_factory, fake_imap):
    cfg = settings_factory(mcp_read_only=False)
    service = MailService(cfg, Mailbox(cfg, factory=lambda *a, **kw: fake_imap))
    data = Compose(to=[], subject="Notes", body_text="A private draft")
    result = await service.draft(data)
    assert result.status == "created" and result.message.uid == 66
    append_call = next(call for call in fake_imap.calls if call[0] == "append")
    assert append_call[1] == "Drafts"
    assert b"A private draft" in append_call[2]
    assert append_call[3] == (r"\Draft", r"\Seen")

    old = MessageRef(folder="Drafts", uid=42, uid_validity=55)
    fake_imap.mail["Drafts"][42] = email_bytes()
    with pytest.raises(MailError, match="not marked as a draft"):
        await service.draft(data, old)
    fake_imap.flags[("Drafts", 42)] = [rb"\Draft"]
    fake_imap.append_result = b"Append completed"
    saved = await service.draft(data, old)
    assert saved.status == "replacement_saved"
    assert not any(call[0] == "expunge" for call in fake_imap.calls)
    fake_imap.append_result = b"[APPENDUID 55 66] Append completed"
    replaced = await service.draft(data, old)
    assert replaced.status == "replaced" and replaced.message.uid == 66
    assert ("expunge", [42]) in fake_imap.calls


def test_imap_failed_login_is_sanitized_and_closes(settings_factory, fake_imap, monkeypatch):
    def fail(*_args):
        raise LoginError("server echoed secret")

    monkeypatch.setattr(fake_imap, "login", fail)
    cfg = settings_factory(imap_tls="starttls")
    box = Mailbox(cfg, factory=lambda *a, **kw: fake_imap)
    with pytest.raises(MailError, match="^IMAP authentication failed") as error, box.connect():
        pytest.fail("IMAP must not yield a failed connection")
    assert "secret" not in str(error.value)
    assert fake_imap.calls[0][0] == "starttls"
    assert fake_imap.calls[0][1].verify_mode == ssl.CERT_REQUIRED
    assert fake_imap.calls[-1] == ("shutdown",)
