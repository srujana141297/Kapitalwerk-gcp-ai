"""Pluggable email notifier -- same swap pattern as storage.py / llm_client.py.
Callers only ever talk to the EmailNotifier interface.
"""


from __future__ import annotations

import os
import time
from dataclasses import dataclass, field


@dataclass
class InlineAttachment:
    """Image embedded via `cid:<content_id>`, not a regular attachment."""

    content_id: str
    filename: str
    mime_type: str
    data: bytes


@dataclass
class SentEmail:
    to: str
    subject: str
    body: str  # plain-text part
    html: str | None = None
    sent_at: float = field(default_factory=time.time)
    provider_response: dict | None = None


class EmailNotifier:
    def send(
        self,
        to: str,
        subject: str,
        body: str,
        *,
        html: str | None = None,
        from_address: str | None = None,
        inline_attachments: list[InlineAttachment] | None = None,
    ) -> SentEmail:
        raise NotImplementedError


class ConsoleEmailNotifier(EmailNotifier):
    """Dev/demo notifier -- prints instead of sending."""

    def send(self, to, subject, body, *, html=None, from_address=None, inline_attachments=None) -> SentEmail:
        print(f"\n---- EMAIL ----\nFrom: {from_address}\nTo: {to}\nSubject: {subject}\n\n{body}\n---------------\n")
        return SentEmail(to=to, subject=subject, body=body, html=html)


class InMemoryEmailNotifier(EmailNotifier):
    """Test notifier -- captures sent emails so tests can assert on them."""

    def __init__(self):
        self.sent: list[SentEmail] = []

    def send(self, to, subject, body, *, html=None, from_address=None, inline_attachments=None) -> SentEmail:
        email = SentEmail(to=to, subject=subject, body=body, html=html)
        self.sent.append(email)
        return email


class SMTPEmailNotifier(EmailNotifier):
    """Unwired manual fallback -- not used by CLI/orchestrator by default."""

    def __init__(self):
        self.host = os.environ.get("SMTP_HOST")
        self.port = int(os.environ.get("SMTP_PORT", "587"))
        self.user = os.environ.get("SMTP_USER")
        self.password = os.environ.get("SMTP_PASSWORD")

    def send(self, to, subject, body, *, html=None, from_address=None, inline_attachments=None) -> SentEmail:
        import smtplib
        from email.mime.image import MIMEImage
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        sender = from_address or self.user
        msg = MIMEMultipart("related")
        msg["From"] = sender
        msg["To"] = to
        msg["Subject"] = subject

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body, "plain"))
        if html:
            alt.attach(MIMEText(html, "html"))
        msg.attach(alt)

        for att in inline_attachments or []:
            img = MIMEImage(att.data, _subtype=att.mime_type.split("/")[-1])
            img.add_header("Content-ID", f"<{att.content_id}>")
            img.add_header("Content-Disposition", "inline", filename=att.filename)
            msg.attach(img)

        with smtplib.SMTP(self.host, self.port) as server:
            server.starttls()
            server.login(self.user, self.password)
            server.send_message(msg)

        return SentEmail(to=to, subject=subject, body=body, html=html)


class ResendEmailNotifier(EmailNotifier):
    """Production notifier -- the only real provider wired into cloud/local runs."""

    def __init__(self):
        self.api_key = os.environ.get("RESEND_API_KEY")
        self.from_email = os.environ.get("RESEND_FROM_EMAIL")
        self.from_name = os.environ.get("RESEND_FROM_NAME", "KapitalWerkNxt")
        if not self.api_key:
            raise RuntimeError(
                "RESEND_API_KEY is not set. Export it (or put it in your Cloud Run "
                "service's env vars / a Secret Manager binding) before using "
                "ResendEmailNotifier -- see reporting_agent/DEPLOY.md."
            )
        if not self.from_email:
            raise RuntimeError(
                "RESEND_FROM_EMAIL is not set -- Resend rejects sends from a "
                "domain/address that isn't verified. Verify a domain in Resend "
                "first, then set this to an address on that domain."
            )

        import resend

        self._resend = resend
        self._resend.api_key = self.api_key

    def send(self, to, subject, body, *, html=None, from_address=None, inline_attachments=None) -> SentEmail:
        sender = from_address or self.from_email
        params: dict = {
            "from": f"{self.from_name} <{sender}>" if self.from_name else sender,
            "to": [to],
            "subject": subject,
            "text": body,
        }
        if html:
            params["html"] = html
        if inline_attachments:
            params["attachments"] = [
                {
                    "content": list(att.data),
                    "filename": att.filename,
                    "content_type": att.mime_type,
                    "content_id": att.content_id,
                }
                for att in inline_attachments
            ]

        try:
            response = self._resend.Emails.send(params)
        except self._resend.exceptions.ResendError as e:
            raise RuntimeError(
                f"Resend send failed for to={to!r} subject={subject!r}: {e}"
            ) from e

        return SentEmail(
            to=to,
            subject=subject,
            body=body,
            html=html,
            provider_response={"id": response.get("id")},
        )
