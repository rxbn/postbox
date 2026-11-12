"""Shared concurrency, mailbox operations and mail submission orchestration."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import partial

import anyio

from . import mime
from .config import Settings
from .errors import MailError
from .imap import Client, Mailbox
from .models import Compose, Draft, MessageRef, Submission
from .smtp import Submitter

log = logging.getLogger(__name__)


class MailService:
    def __init__(
        self, cfg: Settings, mailbox: Mailbox | None = None, smtp: Submitter | None = None
    ):
        self.cfg = cfg
        self.mailbox = mailbox or Mailbox(cfg)
        self.smtp = smtp or Submitter(cfg)
        self.slots = anyio.Semaphore(cfg.mail_concurrency)
        self.writes = anyio.Lock()

    @asynccontextmanager
    async def operation(self, *, write: bool) -> AsyncIterator[None]:
        if write and self.cfg.mcp_read_only:
            raise MailError("This Postbox instance is read-only.")
        try:
            self.slots.acquire_nowait()
        except anyio.WouldBlock:
            raise MailError("Postbox is busy. Wait for an active operation to finish.") from None
        try:
            if write:
                async with self.writes:
                    yield
            else:
                yield
        finally:
            self.slots.release()

    def _connected[T](self, fn: Callable[[Client], T], *, mutating: bool = False) -> T:
        with self.mailbox.connect(mutating=mutating) as client:
            return fn(client)

    async def imap[T](self, fn: Callable[[Client], T], *, write: bool = False) -> T:
        async with self.operation(write=write):
            return await anyio.to_thread.run_sync(partial(self._connected, fn, mutating=write))

    async def draft(self, data: Compose, old: MessageRef | None = None) -> Draft:
        def save(client: Client) -> Draft:
            message, _ = mime.build_message(data, self.cfg, draft=True)
            raw = mime.wire_bytes(message, self.cfg)
            message_id = str(message["Message-ID"])
            if old is None:
                return self.mailbox.create_draft(client, raw, message_id)
            return self.mailbox.replace_draft(client, old, raw, message_id)

        return await self.imap(save, write=True)

    async def send(
        self, data: Compose, *, reply_to: MessageRef | None = None, reply_all: bool = False
    ) -> Submission:
        cfg = self.cfg

        def prepare() -> tuple[bytes, bytes | None, list[str], str, str | None]:
            parent = None
            composed = data
            sent = None
            if reply_to or cfg.append_sent:
                with self.mailbox.connect() as client:
                    if reply_to:
                        # The same RFC822 size guard applies to replies as to message reads.
                        parent, _ = self.mailbox.message(client, reply_to)
                        to, cc = mime.reply_recipients(
                            parent, cfg.mail_address, reply_all=reply_all
                        )
                        subject = str(parent.get("Subject", ""))[:512]
                        if not subject.lower().startswith("re:"):
                            subject = f"Re: {subject}"
                        composed = Compose(
                            to=to,
                            cc=cc,
                            subject=subject,
                            body_text=data.body_text,
                            attachments=data.attachments,
                        )
                    if cfg.append_sent:
                        sent = self.mailbox.special(client, "sent")

            message, recipients = mime.build_message(composed, cfg, parent=parent)
            sent_copy = mime.wire_bytes(message, cfg) if sent else None
            del message["Bcc"]
            return (
                mime.wire_bytes(message, cfg),
                sent_copy,
                recipients,
                str(message["Message-ID"]),
                sent,
            )

        async with self.operation(write=True):
            wire, copy, recipients, message_id, sent = await anyio.to_thread.run_sync(prepare)
            result = await self.smtp.submit(wire, recipients, message_id)
            if result.status == "delivery_unknown":
                return result
            if not sent or copy is None:
                result.sent_copy = "disabled"
                return result

            def save(client: Client) -> None:
                client.append(sent, copy, flags=(r"\Seen",))

            try:
                await anyio.to_thread.run_sync(partial(self._connected, save, mutating=True))
            except Exception:
                log.warning("SMTP accepted a message, but its Sent copy could not be confirmed.")
                result.sent_copy = "failed"
                note = (
                    "SMTP accepted the message, but the Sent copy was not confirmed. Do not resend."
                )
                result.note = f"{result.note} {note}" if result.note else note
            else:
                result.sent_copy = "saved"
            return result
