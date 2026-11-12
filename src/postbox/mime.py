"""Parse untrusted MIME and build messages without filesystem access."""

import base64
import binascii
import re
import unicodedata
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage, MIMEPart
from email.parser import BytesParser
from email.utils import format_datetime, getaddresses, make_msgid, parsedate_to_datetime

from .config import Settings, mailbox_address
from .errors import MailError
from .models import (
    Attachment,
    AttachmentContent,
    Compose,
    Contact,
    EmailDetail,
    MessageRef,
    Summary,
)

MAX_PARTS = 100
MSG_ID = re.compile(r"<[A-Za-z0-9.!#$%&'*+/=?^_\x60{|}~-]+@[A-Za-z0-9._-]+>")
MEDIA_TYPE = re.compile(r"(application|audio|font|image|text|video)/([a-z0-9][a-z0-9.+_-]*)")


def parse(raw: bytes, *, headers_only: bool = False) -> EmailMessage:
    try:
        return BytesParser(policy=policy.default).parsebytes(raw, headersonly=headers_only)
    except (ValueError, RecursionError):
        raise MailError("This message contains malformed or deeply nested MIME.") from None


def contacts(message: EmailMessage, *fields: str) -> list[Contact]:
    values = [str(value)[:8192] for field in fields for value in message.get_all(field, [])[:20]]
    return [
        Contact(name=name[:200], address=address[:254])
        for name, address in getaddresses(values, strict=False)[:50]
        if address
    ]


def message_ids(message: EmailMessage, field: str) -> list[str]:
    ids = (
        match
        for value in message.get_all(field, [])[:10]
        for match in MSG_ID.findall(str(value)[:8192])
        if len(match) <= 254
    )
    return list(dict.fromkeys(ids))[:40]


def message_date(message: EmailMessage) -> str | None:
    try:
        value = parsedate_to_datetime(str(message.get("Date", "")))
        return (value.replace(tzinfo=value.tzinfo or UTC)).astimezone(UTC).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def summary(
    message: EmailMessage, ref: MessageRef, flags: Sequence[str], *, has_attachments: bool
) -> Summary:
    return Summary(
        message=ref,
        subject=str(message.get("Subject", ""))[:512],
        sender=contacts(message, "From")[:8],
        recipients=contacts(message, "To", "Cc")[:8],
        date=message_date(message),
        unread=r"\seen" not in {flag.casefold() for flag in flags},
        has_attachments=has_attachments,
        message_id=next(iter(message_ids(message, "Message-ID")), None),
    )


def safe_filename(value: str) -> str:
    name = unicodedata.normalize("NFKC", value).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(
        char for char in name if not unicodedata.category(char).startswith("C") and char != ":"
    )
    return name.strip(" .")[:180] or "attachment"


def attachment_parts(part: MIMEPart) -> Iterator[MIMEPart]:
    if (
        part.get_content_disposition() == "attachment"
        or part.get_filename()
        or part.get_content_type() in {"message/rfc822", "message/global"}
    ):
        yield part
    elif part.is_multipart():
        for child in part.iter_parts():
            yield from attachment_parts(child)


def attachment_data(part: MIMEPart) -> bytes:
    if part.is_multipart():
        if part.get_content_type() in {"message/rfc822", "message/global"}:
            embedded = next(part.iter_parts(), None)
            return embedded.as_bytes(policy=policy.SMTP) if embedded else b""
        return part.as_bytes(policy=policy.SMTP)
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else b""


def attachment_meta(part: MIMEPart, number: int, data: bytes, cfg: Settings) -> Attachment:
    return Attachment(
        attachment_id=number,
        filename=safe_filename(part.get_filename() or "attachment"),
        content_type=part.get_content_type(),
        size_bytes=len(data),
        inline_available=len(data) <= cfg.max_attachment_bytes,
    )


def get_attachment(message: EmailMessage, number: int, cfg: Settings) -> AttachmentContent:
    for index, part in enumerate(attachment_parts(message), 1):
        if index == number:
            data = attachment_data(part)
            meta = attachment_meta(part, index, data, cfg)
            return AttachmentContent(
                **meta.model_dump(),
                content_base64=base64.b64encode(data).decode("ascii")
                if meta.inline_available
                else None,
            )
    raise MailError("Attachment not found. Use get_email to list valid attachment IDs.")


def extract_body(message: EmailMessage, kind: str, limit: int) -> tuple[str | None, bool]:
    part = message.get_body(("plain",) if kind == "plain" else ("html",))
    if part is None or part.get_content_disposition() == "attachment" or part.get_filename():
        return None, False
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return None, False
    try:
        value = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    except LookupError:
        value = payload.decode("utf-8", errors="replace")
    return value[:limit], len(value) > limit


def detail(
    message: EmailMessage, ref: MessageRef, flags: list[str], cfg: Settings, *, include_html: bool
) -> EmailDetail:
    headers: dict[str, list[str]] = {}
    headers_truncated = False
    budget = 8192
    for key, value in message.items():
        key = key.lower()[:128]
        decoded = str(value)
        if budget <= len(key):
            headers_truncated = True
            break
        clipped = decoded[: min(2048, budget - len(key))]
        headers.setdefault(key, []).append(clipped)
        budget -= len(key) + len(clipped)
        headers_truncated |= clipped != decoded

    attachments: list[Attachment] = []
    truncated = False
    for number, part in enumerate(attachment_parts(message), 1):
        if number > MAX_PARTS:
            truncated = True
            break
        attachments.append(attachment_meta(part, number, attachment_data(part), cfg))
    body_text, text_truncated = extract_body(message, "plain", cfg.max_body_chars)
    body_html, html_truncated = (
        extract_body(message, "html", cfg.max_body_chars) if include_html else (None, False)
    )
    return EmailDetail(
        **summary(message, ref, flags, has_attachments=bool(attachments)).model_dump(),
        headers=headers,
        headers_truncated=headers_truncated,
        body_text=body_text,
        body_html=body_html,
        body_truncated=text_truncated or html_truncated,
        attachments=attachments,
        attachments_truncated=truncated,
        in_reply_to=message_ids(message, "In-Reply-To"),
        references=message_ids(message, "References"),
        flags=flags,
    )


def unique_addresses(addresses: Iterable[str], seen: set[str]) -> list[str]:
    out = []
    for address in addresses:
        normalized = mailbox_address(address)
        key = normalized.casefold()
        if key not in seen:
            seen.add(key)
            out.append(normalized)
    return out


def reply_recipients(
    original: EmailMessage, identity: str, *, reply_all: bool
) -> tuple[list[str], list[str]]:
    def valid(fields: tuple[str, ...]) -> Iterator[str]:
        for contact in contacts(original, *fields):
            try:
                yield mailbox_address(contact.address)
            except ValueError:
                continue

    seen = {identity.casefold()}
    author = list(valid(("Reply-To",))) or list(valid(("From",)))
    to = unique_addresses(author, seen)
    if reply_all or not to:
        to.extend(unique_addresses(valid(("To",)), seen))
    cc = unique_addresses(valid(("Cc",)), seen) if reply_all else []
    if not to and cc:
        to, cc = cc, []
    if not to:
        raise MailError("No valid reply recipient was found. Check From, Reply-To and To.")
    return to[:50], cc[:50]


def build_message(
    data: Compose, cfg: Settings, *, draft: bool = False, parent: EmailMessage | None = None
) -> tuple[EmailMessage, list[str]]:
    seen: set[str] = set()
    try:
        to = unique_addresses(data.to, seen)
        cc = unique_addresses(data.cc, seen)
        bcc = unique_addresses(data.bcc, seen)
    except ValueError as exc:
        raise MailError("Use individual email addresses in to, cc and bcc.") from exc
    recipients = to + cc + bcc
    if not draft and not recipients:
        raise MailError("Specify at least one recipient before sending.")
    if len(recipients) > 50:
        raise MailError("Use at most 50 distinct recipients.")

    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = Address(display_name=cfg.mail_display_name, addr_spec=cfg.mail_address)
    for field, values in (("To", to), ("Cc", cc), ("Bcc", bcc)):
        if values:
            msg[field] = ", ".join(values)
    msg["Subject"] = data.subject
    msg["Date"] = format_datetime(datetime.now(UTC))
    msg["Message-ID"] = make_msgid(domain=cfg.mail_address.rsplit("@", 1)[1])
    if parent:
        parent_ids = message_ids(parent, "Message-ID")
        if parent_ids:
            history = message_ids(parent, "References")
            if not history:
                in_reply_to = message_ids(parent, "In-Reply-To")
                if len(in_reply_to) == 1:
                    history = in_reply_to
            ref_chain = [*history, parent_ids[0]]
            msg["In-Reply-To"] = parent_ids[0]
            msg["References"] = " ".join(dict.fromkeys(ref_chain))

    msg.set_content(data.body_text, cte="quoted-printable")
    if data.body_html is not None:
        msg.add_alternative(data.body_html, subtype="html", cte="quoted-printable")
    # Preflight the aggregate before decoding any attachment. The final wire-size check
    # also covers MIME overhead, headers, and quoted-printable body expansion.
    if sum(len(a.content_base64) for a in data.attachments) > cfg.max_outgoing_bytes:
        raise MailError("Attachments exceed MAX_OUTGOING_BYTES when base64 encoded.")
    for attachment in data.attachments:
        match = MEDIA_TYPE.fullmatch(attachment.content_type.lower())
        if not match:
            raise MailError(
                "Attachment content_type must be a supported type/subtype without parameters."
            )
        try:
            blob = base64.b64decode(attachment.content_base64, validate=True)
        except (ValueError, binascii.Error):
            raise MailError("Attachment content_base64 is not valid standard base64.") from None
        msg.add_attachment(
            blob, maintype=match[1], subtype=match[2], filename=safe_filename(attachment.filename)
        )
    return msg, recipients


def wire_bytes(msg: EmailMessage, cfg: Settings) -> bytes:
    result = msg.as_bytes(policy=policy.SMTP)
    if len(result) > cfg.max_outgoing_bytes:
        raise MailError("The MIME message exceeds MAX_OUTGOING_BYTES. Reduce its size.")
    return result
