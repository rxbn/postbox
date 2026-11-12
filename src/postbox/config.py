"""Process configuration. Secret files use the same names as environment variables."""

import re
import ssl
from pathlib import Path
from typing import Annotated, Literal, Self

from email_validator import EmailNotValidError, validate_email
from pydantic import Field, SecretStr, StringConstraints, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Port = Annotated[int, Field(ge=1, le=65535)]
NonEmpty = Annotated[str, StringConstraints(min_length=1, max_length=512)]


def mailbox_address(value: str) -> str:
    """Validate a single envelope address without performing DNS lookups."""
    if value.strip() != value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Enter one email address without display names or control characters.")
    try:
        return (
            validate_email(
                value, check_deliverability=False, allow_smtputf8=False, test_environment=True
            ).ascii_email
            or value
        )
    except EmailNotValidError:
        raise ValueError("Enter one valid email address without a display name.") from None


def tls_context(verify: bool, ca_file: Path | None = None) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if ca_file:
        context.load_verify_locations(cafile=str(ca_file))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True, frozen=True)

    imap_host: NonEmpty
    imap_port: Port = 993
    imap_username: NonEmpty
    imap_password: SecretStr
    imap_tls: Literal["implicit", "starttls"] = "implicit"
    smtp_host: NonEmpty | None = None
    smtp_port: Port = 465
    smtp_username: NonEmpty | None = None
    smtp_password: SecretStr | None = None
    smtp_security: Literal["implicit", "starttls"] = "implicit"
    mail_address: NonEmpty
    mail_display_name: Annotated[str, StringConstraints(max_length=200)] = ""
    mail_verify_tls: bool = True
    mail_ca_file: Path | None = None
    mail_timeout: Annotated[float, Field(ge=1, le=30)] = 10
    mail_concurrency: Annotated[int, Field(ge=1, le=16)] = 4

    sent_folder: NonEmpty | None = None
    drafts_folder: NonEmpty | None = None
    trash_folder: NonEmpty | None = None
    archive_folder: NonEmpty | None = None
    append_sent: bool = True
    allow_permanent_delete: bool = False

    max_message_bytes: Annotated[int, Field(ge=1024, le=25 * 1024 * 1024)] = 10 * 1024 * 1024
    max_outgoing_bytes: Annotated[int, Field(ge=1024, le=2 * 1024 * 1024)] = 2 * 1024 * 1024
    max_attachment_bytes: Annotated[int, Field(ge=1, le=512 * 1024)] = 128 * 1024
    max_body_chars: Annotated[int, Field(ge=100, le=100_000)] = 32_000
    thread_scan_per_folder: Annotated[int, Field(ge=10, le=250)] = 100
    thread_max_folders: Annotated[int, Field(ge=1, le=12)] = 6

    mcp_transport: Literal["http", "stdio"] = "http"
    mcp_host: NonEmpty = "127.0.0.1"
    mcp_port: Port = 8000
    mcp_allowed_hosts: NonEmpty | None = None
    mcp_auth_token: SecretStr | None = None
    mcp_read_only: bool = True
    mcp_audit_log: bool = False

    @field_validator("mail_address")
    @classmethod
    def address(cls, value: str) -> str:
        return mailbox_address(value)

    @model_validator(mode="after")
    def check(self) -> Self:
        if not self.imap_password.get_secret_value():
            raise ValueError("IMAP_PASSWORD cannot be empty.")
        if any(
            not 32 <= ord(char) < 127
            for value in (self.imap_username, self.imap_password.get_secret_value())
            for char in value
        ):
            raise ValueError("IMAP LOGIN credentials must be printable ASCII.")
        if not self.mcp_read_only and (
            not self.smtp_host
            or not self.smtp_username
            or not self.smtp_password
            or not self.smtp_password.get_secret_value()
        ):
            raise ValueError("Set SMTP_HOST, SMTP_USERNAME and SMTP_PASSWORD to enable writes.")
        if self.mcp_transport == "http":
            token = self.mcp_auth_token.get_secret_value() if self.mcp_auth_token else ""
            if not re.fullmatch(r"[A-Za-z0-9._~+/-]{32,256}=*", token) or len(token) > 256:
                raise ValueError(
                    "Set MCP_AUTH_TOKEN to a random bearer token of 32–256 characters."
                )
        if any(ord(char) < 32 or ord(char) == 127 for char in self.mail_display_name):
            raise ValueError("MAIL_DISPLAY_NAME cannot contain control characters.")
        return self


def load_settings() -> Settings:
    directory = Path("/run/secrets")
    return Settings(_secrets_dir=directory if directory.is_dir() else None)  # type: ignore[call-arg]
