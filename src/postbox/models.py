"""MCP input constraints and structured response schemas."""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

U32 = Annotated[int, Field(strict=True, ge=1, le=2**32 - 1)]
FolderName = Annotated[
    str, StringConstraints(min_length=1, max_length=512, pattern=r"^[^\x00-\x1f\x7f]+$")
]
Query = Annotated[
    str, StringConstraints(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f\x7f]+$")
]
PageLimit = Annotated[int, Field(strict=True, ge=1, le=100)]
PageOffset = Annotated[int, Field(strict=True, ge=0, le=100_000)]
AttachmentId = Annotated[int, Field(strict=True, ge=1, le=100)]
Address = Annotated[str, StringConstraints(min_length=3, max_length=254)]
Recipients = Annotated[list[Address], Field(max_length=50)]
Subject = Annotated[str, StringConstraints(max_length=998, pattern=r"^[^\x00-\x1f\x7f]*$")]
Body = Annotated[str, StringConstraints(max_length=262_144)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class MessageRef(Model):
    """Folder-scoped UID; UIDVALIDITY prevents using stale identifiers."""

    folder: FolderName
    uid_validity: U32
    uid: U32


class Folder(Model):
    name: str
    delimiter: str | None
    attributes: list[str]


class Folders(Model):
    folders: list[Folder]
    special_use: dict[str, str | None]
    truncated: bool


class FolderStatus(Model):
    folder: str
    messages: int
    unread: int
    uid_next: int
    uid_validity: int


class Contact(Model):
    name: str
    address: str


class Summary(Model):
    message: MessageRef
    subject: str
    sender: list[Contact]
    recipients: list[Contact]
    date: str | None
    unread: bool
    has_attachments: bool
    message_id: str | None


class Page(Model):
    emails: list[Summary]
    next_offset: int | None


class Attachment(Model):
    attachment_id: int
    filename: str
    content_type: str
    size_bytes: int
    inline_available: bool


class AttachmentContent(Attachment):
    content_base64: str | None


class EmailDetail(Summary):
    headers: dict[str, list[str]]
    headers_truncated: bool
    body_text: str | None
    body_html: str | None
    body_truncated: bool
    attachments: list[Attachment]
    attachments_truncated: bool
    in_reply_to: list[str]
    references: list[str]
    flags: list[str]


class Thread(Model):
    emails: list[Summary]
    folders_scanned: list[str]
    truncated: bool


class Change(Model):
    status: Literal["updated", "moved", "deleted"]
    message: MessageRef
    destination: str | None = None


class OutgoingAttachment(Model):
    filename: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    content_base64: Annotated[str, StringConstraints(max_length=2_097_152)]
    content_type: Annotated[str, StringConstraints(max_length=128)] = "application/octet-stream"


OutgoingAttachments = Annotated[list[OutgoingAttachment], Field(max_length=10)]


class Compose(Model):
    to: Recipients
    cc: Recipients = Field(default_factory=list)
    bcc: Recipients = Field(default_factory=list)
    subject: Subject
    body_text: Body
    body_html: Body | None = None
    attachments: OutgoingAttachments = Field(default_factory=list)


class Search(Model):
    sender: Query | None = None
    recipient: Query | None = None
    subject: Query | None = None
    text: Query | None = None
    date_from: date | None = None
    date_to: date | None = None
    unread: bool | None = None


class Draft(Model):
    message: MessageRef | None
    message_id: str
    status: Literal["created", "replaced", "replacement_saved"]
    note: str | None = None


class Submission(Model):
    message_id: str
    status: Literal["accepted", "partially_accepted", "delivery_unknown"]
    refused_recipients: list[str] = Field(default_factory=list)
    sent_copy: Literal["saved", "failed", "disabled", "not_attempted"]
    note: str | None = None
