"""UID-based IMAP operations. Each worker owns one short-lived IMAP connection."""

import re
import ssl
from collections import defaultdict, deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import date, timedelta
from email.message import EmailMessage
from time import monotonic
from typing import Literal, Protocol, cast

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientAbortError, IMAPClientError, LoginError

from . import mime
from .config import Settings, tls_context
from .errors import MailError
from .models import Change, Draft, Folder, Folders, FolderStatus, MessageRef, Page, Search, Summary
from .models import Thread as EmailThread

HEADERS = "DATE FROM TO CC SUBJECT MESSAGE-ID IN-REPLY-TO REFERENCES"
HEADER_FIELD = f"BODY[HEADER.FIELDS ({HEADERS})]"
HEADER_PEEK = f"BODY.PEEK[HEADER.FIELDS ({HEADERS})]<0.8192>"
SPECIAL_USE = {
    "sent": (r"\sent", ("Sent", "Sent Items", "Sent Mail")),
    "drafts": (r"\drafts", ("Drafts",)),
    "trash": (r"\trash", ("Trash", "Deleted Items", "Deleted Messages")),
    "archive": (r"\archive", ("Archive",)),
}
APPEND_UID = re.compile(rb"\[APPENDUID ([0-9]{1,10}) ([0-9]{1,10})\]", re.I)
MAX_FOLDERS = 250
MAX_THREAD_RESULTS = 100
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
type FetchRow = dict[bytes, object]
type Special = Literal["sent", "drafts", "trash", "archive"]


class Client(Protocol):
    def starttls(self, ssl_context: ssl.SSLContext | None = None) -> object: ...
    def login(self, username: str, password: str) -> object: ...
    def shutdown(self) -> None: ...
    def list_folders(self) -> list[tuple[tuple[bytes, ...], bytes | None, str]]: ...
    def select_folder(self, folder: str, readonly: bool = False) -> FetchRow: ...
    def folder_status(self, folder: str, what: Sequence[str]) -> FetchRow: ...
    def search(self, criteria: str | list[str], charset: str | None = None) -> list[int]: ...
    def fetch(self, messages: list[int], data: list[str]) -> dict[int, FetchRow]: ...
    def has_capability(self, capability: str) -> bool: ...
    def add_flags(self, messages: list[int], flags: list[str], silent: bool = False) -> object: ...
    def remove_flags(
        self, messages: list[int], flags: list[str], silent: bool = False
    ) -> object: ...
    def move(self, messages: list[int], folder: str) -> bytes: ...
    def copy(self, messages: list[int], folder: str) -> bytes: ...
    def expunge(self, messages: list[int]) -> object: ...
    def append(self, folder: str, msg: bytes, flags: Sequence[str]) -> bytes: ...


def quote(value: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise MailError("IMAP values cannot contain control characters.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def search_queries(filters: Search) -> tuple[str, list[str]]:
    """Return quoted ASCII criteria and optional UTF-8 terms sent as library-managed literals."""
    if filters.date_from and filters.date_to and filters.date_from > filters.date_to:
        raise MailError("date_from must be on or before date_to.")
    ascii_terms: list[str] = []
    utf8_terms: list[str] = []
    for keyword, value in (
        ("FROM", filters.sender),
        ("TO", filters.recipient),
        ("SUBJECT", filters.subject),
        ("TEXT", filters.text),
    ):
        if value is None:
            continue
        destination = ascii_terms if value.isascii() else utf8_terms
        argument = quote(value) if value.isascii() else value
        if keyword == "TO":
            destination.extend(("OR", "TO", argument, "OR", "CC", argument, "BCC", argument))
        else:
            destination.extend((keyword, argument))

    def imap_date(value: date) -> str:
        return f"{value.day:02d}-{MONTHS[value.month - 1]}-{value.year:04d}"

    if filters.date_from:
        ascii_terms.extend(("SINCE", imap_date(filters.date_from)))
    if filters.date_to and filters.date_to != date.max:
        ascii_terms.extend(("BEFORE", imap_date(filters.date_to + timedelta(days=1))))
    if filters.unread is not None:
        ascii_terms.append("UNSEEN" if filters.unread else "SEEN")
    return " ".join(ascii_terms), utf8_terms


def search_ids(client: Client, filters: Search) -> list[int]:
    ascii_query, utf8_query = search_queries(filters)
    if utf8_query:
        results = client.search(utf8_query, charset="UTF-8")
        if ascii_query and results:
            results = list(set(results).intersection(client.search(ascii_query)))
    else:
        results = client.search(ascii_query or "ALL")
    return sorted(set(results), reverse=True)


def flags_of(row: FetchRow) -> list[str]:
    raw = cast("tuple[bytes, ...]", row.get(b"FLAGS", ()))
    return [flag.decode("ascii", "replace")[:128] for flag in raw[:100]]


def wire_section(row: FetchRow, section: str) -> bytes:
    prefix = section.encode("ascii")
    for key, value in row.items():
        # BODY.PEEK responses are named BODY and partial fetches carry the origin.
        if key in (prefix, prefix + b"<0>") and isinstance(value, bytes):
            return value
    raise MailError("The IMAP server did not return the requested message data.")


def _has_parameter(params: object, name: bytes) -> bool:
    return isinstance(params, tuple) and any(
        isinstance(key, bytes) and key.upper().split(b"*", 1)[0] == name for key in params[::2]
    )


def _attachment_disposition(value: object) -> bool:
    if not isinstance(value, tuple) or not value:
        return False
    return (
        isinstance(value[0], bytes)
        and value[0].upper() == b"ATTACHMENT"
        or len(value) > 1
        and _has_parameter(value[1], b"FILENAME")
    )


def _body_has_attachments(body: object) -> bool:
    if not isinstance(body, tuple) or len(body) < 2:
        return False
    if isinstance(body[0], list):  # IMAPClient's normalized multipart BODYSTRUCTURE
        return (
            len(body) > 3
            and _attachment_disposition(body[3])
            or any(_body_has_attachments(child) for child in body[0])
        )
    major, minor = (item.upper() if isinstance(item, bytes) else item for item in body[:2])
    if major == b"MESSAGE" and minor in (b"RFC822", b"GLOBAL"):
        return True
    if len(body) > 2 and _has_parameter(body[2], b"NAME"):
        return True
    # The body extension starts with MD5 before disposition. Text has an extra
    # line-count field before MD5.
    index = 9 if major == b"TEXT" else 8
    return len(body) > index and _attachment_disposition(body[index])


@dataclass(frozen=True)
class ThreadEntry:
    summary: Summary
    ids: tuple[str, ...]


def thread_entry(message: EmailMessage, summary: Summary) -> ThreadEntry:
    ids = tuple(
        dict.fromkeys(
            token
            for header in ("Message-ID", "In-Reply-To", "References")
            for token in mime.message_ids(message, header)
        )
    )
    return ThreadEntry(summary, ids)


def connected_thread(seed: ThreadEntry, others: Sequence[ThreadEntry]) -> list[Summary]:
    entries = [seed, *others]
    by_id: dict[str, list[int]] = defaultdict(list)
    for index, entry in enumerate(entries):
        for identifier in entry.ids:
            by_id[identifier].append(index)
    reached = {0}
    queue = deque([0])
    while queue:
        for identifier in entries[queue.popleft()].ids:
            for index in by_id.pop(identifier, []):
                if index not in reached:
                    reached.add(index)
                    queue.append(index)
    return sorted(
        (entries[index].summary for index in sorted(reached)),
        key=lambda item: (item.date or "", item.message.folder, item.message.uid),
    )


class Mailbox:
    def __init__(self, cfg: Settings, *, factory: Callable[..., Client] = IMAPClient):
        self.cfg = cfg
        self.factory = factory
        self.context = tls_context(cfg.mail_verify_tls, cfg.mail_ca_file)

    @contextmanager
    def connect(self, *, mutating: bool = False) -> Iterator[Client]:
        client: Client | None = None
        try:
            client = self.factory(
                self.cfg.imap_host,
                port=self.cfg.imap_port,
                use_uid=True,
                ssl=self.cfg.imap_tls == "implicit",
                ssl_context=self.context,
                timeout=self.cfg.mail_timeout,
            )
            if self.cfg.imap_tls == "starttls":
                client.starttls(self.context)
            # imaplib quotes the password internally, but not the username.
            client.login(quote(self.cfg.imap_username), self.cfg.imap_password.get_secret_value())
            yield client
        except LoginError:
            raise MailError(
                "IMAP authentication failed. Check IMAP_USERNAME and IMAP_PASSWORD."
            ) from None
        except ssl.SSLError:
            raise MailError(
                "IMAP TLS failed. Check the hostname, certificate and IMAP_TLS."
            ) from None
        except (IMAPClientAbortError, OSError):
            detail = (
                " The change may have applied; verify mailbox state before trying again."
                if mutating
                else " Check IMAP host, port and reachability."
            )
            raise MailError("The IMAP connection failed or timed out." + detail) from None
        except IMAPClientError:
            detail = (
                " Verify mailbox state before trying again."
                if mutating
                else " Check folder access and provider support for the search or command."
            )
            raise MailError("The IMAP server rejected the operation." + detail) from None
        finally:
            if client is not None:
                # All commands are complete; close the socket without issuing CLOSE/EXPUNGE.
                with suppress(OSError, IMAPClientError):
                    client.shutdown()

    def folders(self, client: Client) -> Folders:
        items = [
            Folder(
                name=name,
                delimiter=sep.decode("utf-8", "replace") if sep else None,
                attributes=[a.decode("ascii", "replace") for a in attrs],
            )
            for attrs, sep, name in client.list_folders()
        ]
        selectable = {
            folder.name: folder
            for folder in items
            if r"\noselect" not in {a.casefold() for a in folder.attributes}
            and r"\nonexistent" not in {a.casefold() for a in folder.attributes}
        }
        folded = {name.casefold(): name for name in selectable}
        special: dict[str, str | None] = {}
        for role, (flag, defaults) in SPECIAL_USE.items():
            override = getattr(self.cfg, f"{role}_folder")
            if override is not None:
                special[role] = (
                    override
                    if override in selectable
                    else (folded.get("inbox") if override.casefold() == "inbox" else None)
                )
                continue
            special[role] = next(
                (
                    f.name
                    for f in selectable.values()
                    if flag in (a.casefold() for a in f.attributes)
                ),
                next(
                    (folded[name.casefold()] for name in defaults if name.casefold() in folded),
                    None,
                ),
            )
        return Folders(
            folders=items[:MAX_FOLDERS], special_use=special, truncated=len(items) > MAX_FOLDERS
        )

    def special(self, client: Client, role: Special) -> str:
        folder = self.folders(client).special_use[role]
        if folder is None:
            raise MailError(f"No selectable {role} folder was found. Set {role.upper()}_FOLDER.")
        return folder

    def select(self, client: Client, folder: str, *, write: bool = False) -> int:
        info = client.select_folder(folder, readonly=not write)
        uid_validity = info.get(b"UIDVALIDITY")
        if not isinstance(uid_validity, int) or not 1 <= uid_validity < 2**32:
            raise MailError("The IMAP server did not provide a valid UIDVALIDITY for this folder.")
        return uid_validity

    def select_ref(self, client: Client, ref: MessageRef, *, write: bool = False) -> None:
        if self.select(client, ref.folder, write=write) != ref.uid_validity:
            raise MailError(
                "The message identifier is stale (UIDVALIDITY changed). List the folder again."
            )

    def status(self, client: Client, folder: str) -> FolderStatus:
        data = client.folder_status(folder, ["MESSAGES", "UNSEEN", "UIDNEXT", "UIDVALIDITY"])
        try:
            return FolderStatus(
                folder=folder,
                messages=cast("int", data[b"MESSAGES"]),
                unread=cast("int", data[b"UNSEEN"]),
                uid_next=cast("int", data[b"UIDNEXT"]),
                uid_validity=cast("int", data[b"UIDVALIDITY"]),
            )
        except (KeyError, ValueError):
            raise MailError("The IMAP server did not return all requested folder counts.") from None

    def metadata(
        self, client: Client, folder: str, validity: int, ids: list[int], deadline: float
    ) -> Iterator[ThreadEntry]:
        for index in range(0, len(ids), 10):
            if monotonic() >= deadline:
                raise MailError("The IMAP operation took too long. Try a smaller result limit.")
            group = ids[index : index + 10]
            rows = client.fetch(
                group,
                [
                    "FLAGS",
                    "BODYSTRUCTURE",
                    HEADER_PEEK,
                ],
            )
            for uid in group:
                if uid not in rows:  # message moved concurrently
                    continue
                row = rows[uid]
                header = mime.parse(wire_section(row, HEADER_FIELD), headers_only=True)
                ref = MessageRef(folder=folder, uid_validity=validity, uid=uid)
                summary = mime.summary(
                    header,
                    ref,
                    flags_of(row),
                    has_attachments=_body_has_attachments(row.get(b"BODYSTRUCTURE")),
                )
                yield thread_entry(header, summary)

    def page(self, client: Client, folder: str, filters: Search, limit: int, offset: int) -> Page:
        deadline = monotonic() + self.cfg.mail_timeout * 2
        validity = self.select(client, folder)
        ids = search_ids(client, filters)
        end = offset + limit
        emails = [
            e.summary for e in self.metadata(client, folder, validity, ids[offset:end], deadline)
        ]
        return Page(emails=emails, next_offset=end if end < len(ids) and end <= 100_000 else None)

    def row(self, client: Client, ref: MessageRef) -> FetchRow:
        row = client.fetch([ref.uid], ["FLAGS", "RFC822.SIZE"]).get(ref.uid)
        if row is None:
            raise MailError("Message not found. List the folder again; the message may have moved.")
        return row

    def message(self, client: Client, ref: MessageRef) -> tuple[EmailMessage, list[str]]:
        self.select_ref(client, ref)
        row = self.row(client, ref)
        size = row.get(b"RFC822.SIZE")
        if not isinstance(size, int) or size < 0:
            raise MailError("The IMAP server did not return a valid message size.")
        if size > self.cfg.max_message_bytes:
            raise MailError(
                "Message exceeds MAX_MESSAGE_BYTES. Use another mail client to read it."
            )
        body = client.fetch([ref.uid], [f"BODY.PEEK[]<0.{size + 1}>"]).get(ref.uid)
        if body is None:
            raise MailError("Message moved while reading. List the folder again.")
        raw = wire_section(body, "BODY[]")
        if len(raw) != size:
            raise MailError("IMAP returned an incomplete message; Postbox did not parse it.")
        return mime.parse(raw), flags_of(row)

    def thread(self, client: Client, ref: MessageRef) -> EmailThread:
        deadline = monotonic() + self.cfg.mail_timeout * 2
        self.select_ref(client, ref)
        seed = next(self.metadata(client, ref.folder, ref.uid_validity, [ref.uid], deadline), None)
        if seed is None:
            raise MailError("Message not found. List the folder again.")
        if not seed.ids:
            return EmailThread(emails=[seed.summary], folders_scanned=[ref.folder], truncated=False)
        listing = self.folders(client)
        names = [ref.folder, listing.special_use["sent"], "INBOX", listing.special_use["archive"]]
        names.extend(
            folder.name
            for folder in listing.folders
            if not ({a.casefold() for a in folder.attributes} & {r"\noselect", r"\nonexistent"})
        )
        folders = list(dict.fromkeys(name for name in names if name is not None))
        clipped = listing.truncated or len(folders) > self.cfg.thread_max_folders
        entries: list[ThreadEntry] = []
        scanned: list[str] = []
        for folder in folders[: self.cfg.thread_max_folders]:
            if monotonic() >= deadline:
                clipped = True
                break
            validity = self.select(client, folder)
            if folder == ref.folder and validity != ref.uid_validity:
                raise MailError("The folder changed while scanning. List the folder again.")
            ids = search_ids(client, Search())
            if folder == ref.folder:
                ids = [uid for uid in ids if uid != ref.uid]
            clipped |= len(ids) > self.cfg.thread_scan_per_folder
            try:
                entries.extend(
                    self.metadata(
                        client, folder, validity, ids[: self.cfg.thread_scan_per_folder], deadline
                    )
                )
            except MailError:
                if monotonic() < deadline:
                    raise
                clipped = True
                break
            scanned.append(folder)
        related = connected_thread(seed, entries)
        if len(related) > MAX_THREAD_RESULTS:
            clipped = True
            others = [item for item in related if item.message != ref][: MAX_THREAD_RESULTS - 1]
            related = [item for item in related if item.message == ref or item in others]
        return EmailThread(emails=related, folders_scanned=scanned, truncated=clipped)

    def mark(
        self, client: Client, ref: MessageRef, *, unread: bool | None, flagged: bool | None
    ) -> Change:
        if unread is None and flagged is None:
            raise MailError("Specify unread, flagged, or both.")
        self.select_ref(client, ref, write=True)
        self.row(client, ref)
        changes = [(r"\Seen", None if unread is None else not unread), (r"\Flagged", flagged)]
        for desired, fn in ((True, client.add_flags), (False, client.remove_flags)):
            flags = [flag for flag, state in changes if state is desired]
            if flags:
                fn([ref.uid], flags, silent=True)
        return Change(status="updated", message=ref)

    @staticmethod
    def same_folder(left: str, right: str) -> bool:
        return left == right or left.upper() == right.upper() == "INBOX"

    def move(
        self, client: Client, ref: MessageRef, destination: str, *, draft_only: bool = False
    ) -> Change:
        if self.same_folder(ref.folder, destination):
            raise MailError("The message is already in the destination folder.")
        if draft_only:
            self.require_draft(client, ref)
        else:
            self.select_ref(client, ref, write=True)
            self.row(client, ref)
        if client.has_capability("MOVE"):
            client.move([ref.uid], destination)
        else:
            self.require_scoped_expunge(client)
            client.copy([ref.uid], destination)
            try:
                self.expunge_one(client, ref)
            except (IMAPClientError, OSError):
                raise MailError(
                    "A copy was created, but source removal was not confirmed. "
                    "Check both folders before retrying."
                ) from None
        return Change(status="moved", message=ref, destination=destination)

    def require_draft(self, client: Client, ref: MessageRef) -> None:
        if not self.same_folder(ref.folder, self.special(client, "drafts")):
            raise MailError("Draft operations are restricted to the configured Drafts folder.")
        self.select_ref(client, ref, write=True)
        if r"\draft" not in {flag.casefold() for flag in flags_of(self.row(client, ref))}:
            raise MailError("This message is not marked as a draft.")

    @staticmethod
    def require_scoped_expunge(client: Client) -> None:
        if not (client.has_capability("UIDPLUS") or client.has_capability("IMAP4REV2")):
            raise MailError("This operation needs UIDPLUS or IMAP4rev2 for targeted removal.")

    @staticmethod
    def expunge_one(client: Client, ref: MessageRef) -> None:
        client.add_flags([ref.uid], [r"\Deleted"], silent=True)
        client.expunge([ref.uid])  # UID EXPUNGE; never expunge a whole folder.

    def permanent_delete(self, client: Client, ref: MessageRef) -> Change:
        if not self.cfg.allow_permanent_delete:
            raise MailError("Permanent deletion is disabled.")
        self.require_scoped_expunge(client)
        self.select_ref(client, ref, write=True)
        self.row(client, ref)
        self.expunge_one(client, ref)
        return Change(status="deleted", message=ref)

    @staticmethod
    def append_draft(client: Client, folder: str, raw: bytes) -> MessageRef | None:
        response = client.append(folder, raw, flags=(r"\Draft", r"\Seen"))
        match = APPEND_UID.search(response)
        if match and all(0 < int(item) < 2**32 for item in match.groups()):
            return MessageRef(folder=folder, uid_validity=int(match[1]), uid=int(match[2]))
        return None

    def create_draft(self, client: Client, raw: bytes, message_id: str) -> Draft:
        ref = self.append_draft(client, self.special(client, "drafts"), raw)
        return Draft(
            message=ref,
            message_id=message_id,
            status="created",
            note=None
            if ref
            else "The provider omitted APPENDUID. List Drafts to find the new draft.",
        )

    def replace_draft(self, client: Client, old: MessageRef, raw: bytes, message_id: str) -> Draft:
        self.require_scoped_expunge(client)
        self.require_draft(client, old)
        new = self.append_draft(client, old.folder, raw)
        if new is None or new.uid_validity != old.uid_validity or new.uid == old.uid:
            return Draft(
                message=new,
                message_id=message_id,
                status="replacement_saved",
                note="Replacement saved without a verifiable new UID. "
                "The original was left in place.",
            )
        try:
            self.expunge_one(client, old)
        except (OSError, IMAPClientError):
            return Draft(
                message=new,
                message_id=message_id,
                status="replacement_saved",
                note="Replacement saved, but original removal was not confirmed. "
                "Check Drafts before retrying.",
            )
        return Draft(message=new, message_id=message_id, status="replaced")
