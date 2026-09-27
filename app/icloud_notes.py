from __future__ import annotations

import asyncio
import email
import imaplib
import logging
import re
from email.message import Message
from html.parser import HTMLParser
from typing import Any, Optional

from .db import Database

logger = logging.getLogger(__name__)


class NotesError(RuntimeError):
    pass


class _HTMLToText(HTMLParser):
    BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.BLOCK_TAGS and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")
        if tag in {"li"}:
            self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.BLOCK_TAGS and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(value: Optional[str]) -> str:
    if not value:
        return ""
    parser = _HTMLToText()
    parser.feed(value)
    lines = [line.strip() for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _decode(payload: Any, charset: Optional[str]) -> str:
    if isinstance(payload, bytes):
        text = payload
    else:
        text = str(payload).encode("utf-8", errors="replace")
    for encoding in (charset, "utf-8"):
        if not encoding:
            continue
        try:
            return text.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return text.decode("utf-8", errors="replace")


def extract_note(message: Message) -> tuple[Optional[str], Optional[str]]:
    """Return (html, plain) note content from an IMAP note message."""
    html_candidates: list[str] = []
    plain_candidates: list[str] = []
    for part in message.walk():
        content_type = part.get_content_type() or ""
        filename = part.get_filename() or ""
        if part.is_multipart():
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:  # noqa: BLE001
            continue
        if payload is None:
            continue
        charset = part.get_content_charset()
        if content_type == "text/html" or (filename and filename.lower().endswith(".html")):
            html_candidates.append(_decode(payload, charset))
        elif content_type == "text/plain":
            plain_candidates.append(_decode(payload, charset))
    html = max(html_candidates, key=len) if html_candidates else None
    plain = max(plain_candidates, key=len) if plain_candidates else None
    return html, plain


def _note_to_html(content: str) -> str:
    paragraphs = [paragraph.strip() for paragraph in content.splitlines() if paragraph.strip()]
    return "".join(f"<p>{_html_escape(paragraph)}</p>" for paragraph in paragraphs)


def _html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class ICloudNotes:
    """Experimental IMAP bridge to a single iCloud Note (unofficial, may break)."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def configured(self) -> bool:
        settings = self.database.get_settings()
        return bool(
            settings.get("notes_imap_username")
            and settings.get("notes_imap_app_password")
            and settings.get("notes_imap_note_title")
        )

    def enabled(self) -> bool:
        settings = self.database.get_settings()
        return settings.get("notes_imap_enabled", "false").lower() == "true" and self.configured()

    def _connect(self) -> imaplib.IMAP4_SSL:
        settings = self.database.get_settings()
        host = settings.get("notes_imap_host") or "imap.mail.me.com"
        try:
            connection = imaplib.IMAP4_SSL(host, 993)
        except OSError as exc:
            raise NotesError(f"Kunne ikke forbinde til {host}: {exc}") from exc
        try:
            connection.login(
                settings.get("notes_imap_username") or "",
                settings.get("notes_imap_app_password") or "",
            )
        except imaplib.IMAP4.error as exc:
            connection.logout()
            raise NotesError("Login mislykkedes – kontrollér Apple ID og app-specifikt password") from exc
        return connection

    def _notes_folders(self, connection: imaplib.IMAP4_SSL) -> list[str]:
        typ, data = connection.list()
        if typ != "OK":
            raise NotesError("Kunne ikke liste IMAP-mapper")
        candidates: list[str] = []
        for raw in data:
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            for mailbox in _parse_list_mailboxes(line):
                candidates.append(mailbox)
        notes = [
            mailbox
            for mailbox in candidates
            if "notes" in _decode_folder(mailbox).lower().replace(" ", "")
        ]
        if not notes:
            raise NotesError("IMAP Notes-mappen blev ikke fundet i din iCloud-konto")
        return notes

    def _find_note_in_folder(
        self, connection: imaplib.IMAP4_SSL, folder: str, title: str
    ) -> Optional[str]:
        typ, _ = connection.select(folder)
        if typ != "OK":
            raise NotesError("Kunne ikke åbne Notes-mappen")
        typ, data = connection.uid("search", None, "ALL")
        if typ != "OK" or not data or not data[0]:
            return None
        uids = data[0].split()
        if not uids:
            return None
        comma_uids = ",".join(
            uid.decode() if isinstance(uid, bytes) else str(uid) for uid in uids
        )
        typ, messages = connection.uid(
            "fetch", comma_uids, "(UID BODY.PEEK[HEADER.FIELDS (SUBJECT)])"
        )
        if typ != "OK":
            return None
        for item in messages:
            if not isinstance(item, tuple) or not item:
                continue
            head = item[0] if isinstance(item[0], bytes) else b""
            body = item[1] if len(item) > 1 and isinstance(item[1], bytes) else b""
            match = re.search(rb"UID\s+(\d+)", head)
            if not match:
                continue
            message = email.message_from_bytes(body)
            subject = str(message.get("subject") or "").strip()
            if subject == title or subject.casefold() == title.casefold():
                return match.group(1).decode()
        return None

    def _find_note(
        self, connection: imaplib.IMAP4_SSL, folders: list[str], title: str
    ) -> Optional[tuple[str, str]]:
        for folder in folders:
            uid = self._find_note_in_folder(connection, folder, title)
            if uid is not None:
                return folder, uid
        return None

    async def fetch(self) -> dict[str, Any]:
        if not self.enabled():
            return {
                "enabled": False,
                "title": "",
                "content": "",
                "error": "disabled",
                "configured": self.configured(),
                "last_error": self.database.get_setting("notes_imap_last_error") or "",
            }
        try:
            result = await asyncio.to_thread(self._fetch_sync)
            self.database.update_settings(
                {"notes_imap_last_sync": result.get("fetched_at", ""), "notes_imap_last_error": ""}
            )
            return result
        except NotesError as exc:
            logger.warning("iCloud note fetch failed: %s", exc)
            self.database.update_settings(
                {"notes_imap_last_error": str(exc)[:500]}
            )
            return {
                "enabled": True,
                "configured": True,
                "title": self.database.get_setting("notes_imap_note_title") or "",
                "content": "",
                "error": str(exc),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("iCloud note fetch failed: %s", exc)
            self.database.update_settings({"notes_imap_last_error": str(exc)[:500]})
            return {
                "enabled": True,
                "configured": True,
                "title": self.database.get_setting("notes_imap_note_title") or "",
                "content": "",
                "error": str(exc),
            }

    def _fetch_sync(self) -> dict[str, Any]:
        title = self.database.get_setting("notes_imap_note_title") or ""
        connection = self._connect()
        try:
            folders = self._notes_folders(connection)
            found = self._find_note(connection, folders, title)
            if found is None:
                return {
                    "enabled": True,
                    "configured": True,
                    "found": False,
                    "title": title,
                    "content": "",
                    "error": "",
                    "fetched_at": _utc_now(),
                }
            _, found_uid = found
            typ, data = connection.uid("fetch", found_uid, "(UID BODY.PEEK[])")
            if typ != "OK" or not data:
                raise NotesError("Kunne ikke læse note-indholdet")
            message = email.message_from_bytes(_joined_payload(data))
            html, plain = extract_note(message)
            content = (html_to_text(html) if html else plain) or ""
            return {
                "enabled": True,
                "configured": True,
                "found": True,
                "title": title,
                "content": content,
                "error": "",
                "fetched_at": _utc_now(),
            }
        finally:
            try:
                connection.logout()
            except Exception:  # noqa: BLE001
                pass

    async def save(self, content: str) -> dict[str, Any]:
        if not self.enabled():
            return {"saved": False, "error": "disabled"}
        try:
            message = await asyncio.to_thread(self._save_sync, content)
            self.database.update_settings(
                {"notes_imap_last_sync": _utc_now(), "notes_imap_last_error": ""}
            )
            return message
        except NotesError as exc:
            self.database.update_settings({"notes_imap_last_error": str(exc)[:500]})
            return {"saved": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            self.database.update_settings({"notes_imap_last_error": str(exc)[:500]})
            return {"saved": False, "error": str(exc)}

    def _save_sync(self, content: str) -> dict[str, Any]:
        settings = self.database.get_settings()
        title = settings.get("notes_imap_note_title") or ""
        username = settings.get("notes_imap_username") or ""
        connection = self._connect()
        try:
            folders = self._notes_folders(connection)
            found = self._find_note(connection, folders, title)
            if found is None:
                raise NotesError(
                    "Noten blev ikke fundet i iCloud. Kun opdatering af en eksisterende note understøttes."
                )
            folder, uid = found
            typ, _ = connection.select(folder)
            if typ != "OK":
                raise NotesError("Kunne ikke åbne Notes-mappen")
            connection.uid("store", uid, "+FLAGS", r"(\Deleted)")
            connection.expunge()
            message = email.message.EmailMessage()
            message["From"] = username
            message["Subject"] = title
            message["MIME-Version"] = "1.0"
            message.set_content(content)
            message.add_alternative(_note_to_html(content), subtype="html")
            typ, _ = connection.append(folder, None, None, message.as_bytes())
            if typ != "OK":
                raise NotesError("Kunne ikke gemme noten i iCloud")
            return {"saved": True, "title": title, "content": content}
        finally:
            try:
                connection.logout()
            except Exception:  # noqa: BLE001
                pass


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _decode_folder(value: str) -> str:
    # Mailbox names are IMAP UTF-7 encoded; decode best-effort.
    try:
        return imaplib.imap4_utf7.decode(value)
    except Exception:  # noqa: BLE001
        return value


def _parse_list_mailboxes(line: str) -> list[str]:
    rest = line.strip()
    if rest.startswith("("):
        close = rest.find(")")
        if close == -1:
            return []
        rest = rest[close + 1 :].strip()
    if not rest:
        return []

    def quoted(prefix: str) -> tuple[Optional[str], str]:
        match = re.match(r'"((?:[^"\\]|\\.)*)"', prefix)
        if match:
            return match.group(1), prefix[match.end() :].strip()
        return None, prefix

    _, rest = quoted(rest)
    if not rest:
        return []
    name, _ = quoted(rest)
    if name is not None:
        return [name]
    return [rest.split()[0]] if rest.split() else []


def _joined_payload(data: list[Any]) -> bytes:
    combined: list[bytes] = []
    for item in data:
        if isinstance(item, tuple):
            for segment in item:
                if isinstance(segment, bytes):
                    combined.append(segment)
        elif isinstance(item, bytes):
            combined.append(item)
    return b"".join(combined)