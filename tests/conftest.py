from email import policy
from email.message import EmailMessage

import pytest
from pydantic import SecretStr

from postbox.config import Settings
from postbox.imap import HEADER_FIELD
from postbox.models import MessageRef


@pytest.fixture
def settings_factory():
    def build(**updates):
        values = {
            "imap_host": "127.0.0.1",
            "imap_username": "mailbox@example.test",
            "imap_password": SecretStr("synthetic-imap-password"),
            "smtp_host": "127.0.0.1",
            "smtp_username": "mailbox@example.test",
            "smtp_password": SecretStr("synthetic-smtp-password"),
            "mail_address": "mailbox@example.test",
            "mail_display_name": "Zoë Mailbox",
            "mcp_transport": "stdio",
            "mail_timeout": 2,
        }
        return Settings(**(values | updates))

    return build


@pytest.fixture
def cfg(settings_factory):
    return settings_factory()


@pytest.fixture
def ref():
    return MessageRef(folder="INBOX", uid_validity=55, uid=42)


def email_bytes(
    subject="Café meeting", message_id="<root@example.test>", *, reply_to=None, references=None
):
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = "Miyuki 山田 <miyuki@example.test>"
    msg["To"] = "mailbox@example.test, teammate@example.test"
    msg["Cc"] = "TEAMMATE@example.test, extra@example.test"
    msg["Reply-To"] = "reply@example.test"
    msg["Subject"] = subject
    msg["Date"] = "Tue, 10 Nov 2026 10:00:00 +0100"
    msg["Message-ID"] = message_id
    if reply_to:
        msg["In-Reply-To"] = reply_to
    if references:
        msg["References"] = references
    msg.set_content("Plain café text.")
    msg.add_alternative("<p>HTML café text.</p>", subtype="html")
    msg.add_attachment(
        b"123456789", maintype="application", subtype="pdf", filename="../../Résumé.pdf"
    )
    return msg.as_bytes()


class FakeIMAP:
    def __init__(self):
        self.selected = None
        self.mail = {"INBOX": {42: email_bytes()}, "Outbox Copies": {}, "Drafts": {}, "Trash": {}}
        self.flags = {}
        self.capabilities = {"UIDPLUS", "MOVE"}
        self.calls = []
        self.validity = 55
        self.append_result = b"[APPENDUID 55 66] Append completed"

    def starttls(self, context):
        self.calls.append(("starttls", context))

    def login(self, username, password):
        self.calls.append(("login", username, password))

    def shutdown(self):
        self.calls.append(("shutdown",))

    def list_folders(self):
        return [
            ((), b"/", "INBOX"),
            ((rb"\Sent",), b"/", "Outbox Copies"),
            ((rb"\Drafts",), b"/", "Drafts"),
            ((rb"\Trash",), b"/", "Trash"),
            ((rb"\Noselect",), b"/", "Hierarchy"),
        ]

    def select_folder(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        self.selected = folder
        return {b"UIDVALIDITY": self.validity}

    def folder_status(self, folder, what):
        self.calls.append(("status", folder, what))
        return {b"MESSAGES": 1, b"UNSEEN": 1, b"UIDNEXT": 43, b"UIDVALIDITY": self.validity}

    def search(self, criteria, charset=None):
        self.calls.append(("search", criteria, charset))
        return list(self.mail[self.selected])

    def fetch(self, messages, data):
        self.calls.append(("fetch", messages, data))
        return {
            uid: {
                b"FLAGS": tuple(self.flags.get((self.selected, uid), [])),
                b"RFC822.SIZE": len(raw),
                b"BODY[]<0>": raw,
                HEADER_FIELD.encode() + b"<0>": raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n",
                b"BODYSTRUCTURE": (
                    [(b"APPLICATION", b"PDF", (b"NAME", b"test.pdf"), None, None, b"BASE64", 10)],
                    b"MIXED",
                ),
            }
            for uid in messages
            if (raw := self.mail[self.selected].get(uid)) is not None
        }

    def has_capability(self, name):
        return name in self.capabilities

    def add_flags(self, messages, flags, silent=False):
        self.calls.append(("add_flags", messages, flags))

    def remove_flags(self, messages, flags, silent=False):
        self.calls.append(("remove_flags", messages, flags))

    def move(self, messages, folder):
        self.calls.append(("move", messages, folder))

    def copy(self, messages, folder):
        self.calls.append(("copy", messages, folder))

    def expunge(self, messages):
        self.calls.append(("expunge", messages))

    def append(self, folder, raw, flags=()):
        self.calls.append(("append", folder, raw, flags))
        return self.append_result


@pytest.fixture
def fake_imap():
    return FakeIMAP()
