"""Authenticated SMTP submission. Never retry a submission automatically."""

import asyncio
from collections.abc import Callable

from aiosmtplib import SMTP
from aiosmtplib.errors import (
    SMTPAuthenticationError,
    SMTPException,
    SMTPRecipientsRefused,
    SMTPResponseException,
)

from .config import Settings, tls_context
from .errors import MailError
from .models import Submission


class Submitter:
    def __init__(self, cfg: Settings, *, factory: Callable[..., SMTP] = SMTP):
        self.cfg = cfg
        self.factory = factory
        self.context = tls_context(cfg.mail_verify_tls, cfg.mail_ca_file)

    async def submit(self, raw: bytes, recipients: list[str], message_id: str) -> Submission:
        cfg = self.cfg
        if not cfg.smtp_host or not cfg.smtp_username or not cfg.smtp_password:
            raise MailError(
                "SMTP is not configured. Check SMTP_HOST, SMTP_USERNAME and SMTP_PASSWORD."
            )
        smtp = self.factory(
            hostname=cfg.smtp_host,
            port=cfg.smtp_port,
            use_tls=cfg.smtp_security == "implicit",
            start_tls=cfg.smtp_security == "starttls",
            tls_context=self.context,
            timeout=cfg.mail_timeout,
        )
        try:
            try:
                async with asyncio.timeout(cfg.mail_timeout):
                    await smtp.connect()
                    await smtp.login(cfg.smtp_username, cfg.smtp_password.get_secret_value())
            except SMTPAuthenticationError:
                raise MailError(
                    "SMTP authentication failed. Check SMTP_USERNAME and SMTP_PASSWORD."
                ) from None
            except (SMTPException, OSError):
                raise MailError(
                    "SMTP connection or login failed. Check host, port, TLS mode and certificate."
                ) from None

            try:
                async with asyncio.timeout(cfg.mail_timeout):
                    refused, _response = await smtp.sendmail(cfg.mail_address, recipients, raw)
            except SMTPResponseException as exc:
                raise MailError(
                    f"SMTP rejected the submission ({exc.code}). "
                    "Check the envelope or provider policy."
                ) from None
            except SMTPRecipientsRefused:
                raise MailError(
                    "SMTP refused all recipients. Verify the recipient addresses."
                ) from None
            except (SMTPException, OSError):
                return Submission(
                    message_id=message_id,
                    status="delivery_unknown",
                    sent_copy="not_attempted",
                    note="SMTP did not confirm acceptance. Check provider logs or the recipient "
                    "before resending; the message may already have been accepted.",
                )
            return Submission(
                message_id=message_id,
                status="partially_accepted" if refused else "accepted",
                refused_recipients=[address for address in recipients if address in refused],
                sent_copy="not_attempted",
                note="Some recipients were refused. Do not resend to recipients already accepted."
                if refused
                else None,
            )
        finally:
            smtp.close()
