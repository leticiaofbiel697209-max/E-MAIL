from __future__ import annotations

import imaplib
import mimetypes
import smtplib
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import Any

from utils import clean_text, decode_mime_words, env, extract_sender, html_to_text, safe_decode


def _imap_config() -> tuple[str, int, str, str]:
    host = env("EMAIL_IMAP_HOST")
    port = int(env("EMAIL_IMAP_PORT", "993") or 993)
    user = env("EMAIL_USER")
    password = env("EMAIL_PASSWORD")
    if not all([host, user, password]):
        raise RuntimeError("Configure EMAIL_IMAP_HOST, EMAIL_USER e EMAIL_PASSWORD no arquivo .env")
    return host, port, user, password


def _smtp_config() -> tuple[str, int, str, str, bool]:
    host = env("EMAIL_SMTP_HOST")
    port = int(env("EMAIL_SMTP_PORT", "587") or 587)
    user = env("EMAIL_USER")
    password = env("EMAIL_PASSWORD")
    if not all([host, user, password]):
        raise RuntimeError("Configure EMAIL_SMTP_HOST, EMAIL_USER e EMAIL_PASSWORD no arquivo .env")
    use_ssl = (env("EMAIL_SMTP_USE_SSL", "") or "").strip().lower() in ("1", "true", "sim", "yes")
    return host, port, user, password, use_ssl or port == 465


class EmailClient:
    def __init__(self) -> None:
        self.imap_host, self.imap_port, self.user, self.password = _imap_config()

    def connect(self) -> imaplib.IMAP4_SSL:
        mail = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        mail.login(self.user, self.password)
        return mail

    def fetch_recent_and_unread(self, days: int = 7, mailbox: str = "INBOX", limit: int = 80, include_old_unread: bool = False) -> list[dict[str, Any]]:
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        criteria_list = [f'SINCE "{since}"']
        if include_old_unread:
            criteria_list.append("UNSEEN")

        with self.connect() as mail:
            mail.select(mailbox)
            uids: set[bytes] = set()
            for criteria in criteria_list:
                status, data = mail.uid("search", None, criteria)
                if status != "OK":
                    raise RuntimeError(f"Falha ao buscar e-mails no IMAP com critério: {criteria}")
                if data and data[0]:
                    uids.update(data[0].split())

            ordered_uids = sorted(uids, key=lambda item: int(item))[-limit:]
            emails = []
            for uid in reversed(ordered_uids):
                status, msg_data = mail.uid("fetch", uid, "(RFC822 FLAGS)")
                if status != "OK" or not msg_data:
                    continue
                raw = None
                flags_raw = b""
                for part in msg_data:
                    if isinstance(part, tuple):
                        flags_raw += part[0]
                        raw = part[1]
                    elif isinstance(part, bytes):
                        flags_raw += part
                if not raw:
                    continue
                msg = message_from_bytes(raw)
                is_unread = b"\\Seen" not in flags_raw
                parsed = parse_email_message(msg, uid.decode(errors="ignore"), is_unread)
                if include_old_unread or _is_recent(parsed.get("date"), days):
                    emails.append(parsed)
            return emails


def parse_email_message(msg, imap_uid: str, is_unread: bool) -> dict[str, Any]:
    sender_raw = decode_mime_words(msg.get("From"))
    sender_name, sender_email = extract_sender(sender_raw)
    subject = decode_mime_words(msg.get("Subject")) or "(sem assunto)"
    message_id = (msg.get("Message-ID") or f"imap-{imap_uid}").strip().strip("<>").lower()
    date_raw = msg.get("Date") or ""
    try:
        date = parsedate_to_datetime(date_raw).isoformat()
    except Exception:
        date = date_raw

    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict[str, Any]] = []

    if msg.is_multipart():
        for part in msg.walk():
            content_disposition = (part.get("Content-Disposition") or "").lower()
            content_type = part.get_content_type()
            filename = decode_mime_words(part.get_filename())
            if filename or "attachment" in content_disposition:
                attachments.append(
                    {
                        "filename": filename or "anexo_sem_nome",
                        "content_type": content_type,
                        "size_bytes": len(part.get_payload(decode=True) or b""),
                    }
                )
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            decoded = safe_decode(payload, part.get_content_charset())
            if content_type == "text/plain":
                text_parts.append(decoded)
            elif content_type == "text/html":
                html_parts.append(decoded)
    else:
        payload = msg.get_payload(decode=True) or b""
        decoded = safe_decode(payload, msg.get_content_charset())
        if msg.get_content_type() == "text/html":
            html_parts.append(decoded)
        else:
            text_parts.append(decoded)

    body = clean_text("\n\n".join(text_parts))
    html_body = html_to_text("\n".join(html_parts)) if html_parts else ""
    if not body:
        body = html_body
    elif html_body:
        body = clean_text(body + "\n\n" + html_body)

    return {
        "message_id": message_id,
        "imap_uid": imap_uid,
        "sender_name": sender_name,
        "sender_email": sender_email,
        "subject": subject,
        "date": date,
        "body": body,
        "attachments": attachments,
        "is_unread": is_unread,
    }


def _is_recent(date_value: str | None, days: int) -> bool:
    if not date_value:
        return True
    try:
        dt = datetime.fromisoformat(date_value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        cutoff = datetime.now(tz=dt.tzinfo) - timedelta(days=days)
        return dt >= cutoff
    except Exception:
        return True


def send_email_smtp(
    to_email: str,
    subject: str,
    body: str,
    reply_to_message_id: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> None:
    host, port, user, password, use_ssl = _smtp_config()
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to_email
    msg["Subject"] = subject
    if reply_to_message_id:
        msg["In-Reply-To"] = reply_to_message_id
        msg["References"] = reply_to_message_id
    msg.set_content(body)
    for attachment in attachments or []:
        filename = str(attachment.get("filename") or "anexo")
        content = attachment.get("content") or b""
        if isinstance(content, str):
            content = content.encode("utf-8")
        mime_type = attachment.get("mime_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        maintype, subtype = mime_type.split("/", 1)
        msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)

    smtp_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_cls(host, port, timeout=30) as smtp:
        if not use_ssl:
            smtp.starttls()
        smtp.login(user, password)
        refused = smtp.send_message(msg)
        if refused:
            raise RuntimeError(f"SMTP recusou destinatário(s): {refused}")
