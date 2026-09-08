"""Honeycrisp: an MCP server exposing iCloud Mail, Calendar, Contacts and Notes.

Authentication uses an Apple app-specific password over the standard open
protocols Apple supports: IMAP for mail, CalDAV for calendar, CardDAV for
contacts.

This runs as a stdio server, so stdout carries the JSON-RPC stream. Nothing
human-readable may be written there -- all diagnostics go to stderr.
"""

import atexit
import email
import email.policy
import logging
import os
import re
import smtplib
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid, parsedate_to_datetime
from html import unescape as html_unescape
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Tuple
from urllib.parse import urljoin
from xml.sax.saxutils import escape as xml_escape

import caldav
from caldav.lib.error import NotFoundError
import imapclient
from imapclient.imapclient import SENT as SENT_FLAG, TRASH as TRASH_FLAG
import requests
import vobject
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from requests.auth import HTTPBasicAuth

# MCP clients launch the server from an arbitrary working directory, so the
# .env location is resolved relative to this file rather than the cwd.
load_dotenv(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

logging.basicConfig(
    stream=sys.stderr,
    level=os.getenv("HONEYCRISP_LOG_LEVEL", "INFO").upper(),
    format="%(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("honeycrisp")

ICLOUD_EMAIL = os.getenv("ICLOUD_EMAIL")
ICLOUD_APP_PASSWORD = os.getenv("ICLOUD_APP_PASSWORD")

IMAP_HOST = "imap.mail.me.com"
SMTP_HOST = "smtp.mail.me.com"
SMTP_PORT = 587  # STARTTLS
CALDAV_URL = "https://caldav.icloud.com/"
CARDDAV_URL = "https://contacts.icloud.com"

# Network timeout (seconds) so a hung connection can't wedge the server.
HTTP_TIMEOUT = 30

# Bytes fetched per message to build a preview. Enough to cover headers and the
# start of the first body part without pulling down attachments.
PREVIEW_FETCH_BYTES = 16384
PREVIEW_CHARS = 500

# Default cap on the body text returned by get_email.
DEFAULT_BODY_CHARS = 20000

# Default window for calendar queries when no end date is given. CalDAV
# recurrence expansion requires a closed interval, and an unbounded query
# against iCloud pulls down every event in every calendar.
DEFAULT_CALENDAR_WINDOW = timedelta(days=90)

# Pre-change copies of any card or event the server writes, and full copies
# of any message it deletes permanently, are saved here.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_backup_dir(source_root: str) -> str:
    """backups/ beside the source checkout, or ~/.honeycrisp/backups when installed.

    When the module is installed as a package, source_root is a site-packages
    directory that must never receive user data.
    """
    if os.path.isdir(os.path.join(source_root, "src")) and os.path.isfile(os.path.join(source_root, "src", "server.py")):
        return os.path.join(source_root, "backups")
    return os.path.join(os.path.expanduser("~"), ".honeycrisp", "backups")


BACKUP_DIR = os.getenv("HONEYCRISP_BACKUP_DIR") or _default_backup_dir(PROJECT_ROOT)

if not ICLOUD_EMAIL or not ICLOUD_APP_PASSWORD:
    log.warning("ICLOUD_EMAIL and ICLOUD_APP_PASSWORD must be set in the environment.")

mcp = FastMCP("Honeycrisp")


def _require_credentials() -> Tuple[str, str]:
    if not ICLOUD_EMAIL or not ICLOUD_APP_PASSWORD:
        raise ToolError(
            "Missing iCloud credentials: set ICLOUD_EMAIL and ICLOUD_APP_PASSWORD."
        )
    return ICLOUD_EMAIL, ICLOUD_APP_PASSWORD


# --------------------------------------------------------------------------
# IMAP connection handling
# --------------------------------------------------------------------------
# iCloud caps concurrent IMAP connections and throttles repeated logins, so the
# connection is cached across tool calls rather than re-established each time.
# IMAPClient is not thread-safe, so a lock serialises access to it.

_imap_lock = threading.Lock()
_imap_client: Optional[imapclient.IMAPClient] = None


def _connect_imap() -> imapclient.IMAPClient:
    address, password = _require_credentials()
    client = imapclient.IMAPClient(IMAP_HOST, ssl=True)
    try:
        client.login(address, password)
    except Exception as e:
        _quiet_close(client)
        raise ToolError(
            f"iCloud IMAP login failed: {e}. Confirm ICLOUD_EMAIL and that "
            "ICLOUD_APP_PASSWORD is a valid app-specific password."
        ) from e
    return client


def _quiet_close(client: Optional[imapclient.IMAPClient]) -> None:
    if client is None:
        return
    try:
        client.logout()
    except Exception:
        try:
            client.shutdown()
        except Exception as e:
            log.debug("Could not close IMAP connection cleanly: %s", e)


@contextmanager
def imap_session() -> Iterator[imapclient.IMAPClient]:
    """Yield a live, exclusively-held IMAP connection."""
    global _imap_client
    with _imap_lock:
        client = _imap_client
        if client is not None:
            # Cheap liveness probe; iCloud drops idle connections.
            try:
                client.noop()
            except Exception:
                log.debug("Cached IMAP connection went stale; reconnecting.")
                _quiet_close(client)
                client = None
        if client is None:
            client = _connect_imap()

        try:
            yield client
        except ToolError:
            # Our own validation errors leave the connection usable.
            _imap_client = client
            raise
        except BaseException:
            # Anything else may have desynchronised the protocol stream.
            _quiet_close(client)
            _imap_client = None
            raise
        else:
            _imap_client = client


@atexit.register
def _close_imap_at_exit() -> None:
    global _imap_client
    _quiet_close(_imap_client)
    _imap_client = None


def _connect_smtp() -> smtplib.SMTP:
    """Open an authenticated SMTP session. Not cached: SMTP sessions are short."""
    address, password = _require_credentials()
    try:
        client = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=HTTP_TIMEOUT)
    except OSError as e:
        raise ToolError(f"Could not reach {SMTP_HOST}:{SMTP_PORT}: {e}") from e
    try:
        client.starttls()
        client.login(address, password)
    except Exception as e:
        _quiet_quit(client)
        raise ToolError(
            f"iCloud SMTP login failed: {e}. Confirm ICLOUD_EMAIL and that "
            "ICLOUD_APP_PASSWORD is a valid app-specific password."
        ) from e
    return client


def _quiet_quit(client: smtplib.SMTP) -> None:
    try:
        client.quit()
    except Exception as e:
        log.debug("Could not close SMTP connection cleanly: %s", e)


def get_caldav_client() -> caldav.DAVClient:
    address, password = _require_credentials()
    return caldav.DAVClient(
        url=CALDAV_URL,
        username=address,
        password=password,
        timeout=HTTP_TIMEOUT,
    )


# --------------------------------------------------------------------------
# Message parsing helpers
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
# Block-level HTML boundaries that should become line breaks in text output.
_PARAGRAPH_END_RE = re.compile(r"(?i)<\s*/(?:p|h[1-6]|blockquote|pre|table)\s*>")
_LINE_BREAK_RE = re.compile(r"(?i)<\s*(?:br\s*/?|/div|/tr|/li)\s*>")
_HIDDEN_HTML_RE = re.compile(r"(?is)<\s*(style|script|head)\b.*?<\s*/\s*\1\s*>")
_BLANK_LINES_RE = re.compile(r"\n\s*\n(?:\s*\n)+")
_WS_RE = re.compile(r"\s+")


def _decode_header_value(raw: Any) -> str:
    """Decode an RFC 2047 encoded header (e.g. '=?UTF-8?B?...?=') to text."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raw = raw.decode("latin-1", errors="replace")
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:
        log.debug("Falling back to raw header for %r", raw)
        return str(raw).strip()


def _format_address(addresses: Any) -> str:
    """Render an IMAP ENVELOPE address list as 'Name <user@host>' entries."""
    if not addresses:
        return ""
    rendered = []
    for addr in addresses:
        mailbox = (addr.mailbox or b"").decode("utf-8", errors="replace")
        host = (addr.host or b"").decode("utf-8", errors="replace")
        name = _decode_header_value(addr.name)
        email_addr = f"{mailbox}@{host}" if mailbox and host else mailbox or host
        rendered.append(f"{name} <{email_addr}>" if name else email_addr)
    return ", ".join(r for r in rendered if r)


def _truncate(text: str, limit: int = PREVIEW_CHARS) -> str:
    text = _WS_RE.sub(" ", text).strip()
    return text[:limit] + "..." if len(text) > limit else text


def _html_to_text(content: str) -> str:
    content = _HIDDEN_HTML_RE.sub(" ", content)
    content = _PARAGRAPH_END_RE.sub("\n\n", content)
    content = _LINE_BREAK_RE.sub("\n", content)
    content = _TAG_RE.sub(" ", content)
    content = html_unescape(content)
    lines = [_WS_RE.sub(" ", line).strip() for line in content.split("\n")]
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()


def _message_text(message: Any, raw: bytes) -> str:
    """Return the readable body of a parsed message, preferring text/plain.

    Bodies are commonly base64 or quoted-printable encoded, so the raw bytes
    are not human-readable on their own; they must be decoded per-part.
    """
    part = None
    for preference in (("plain",), ("html",)):
        try:
            part = message.get_body(preferencelist=preference)
        except Exception:
            part = None
        if part is not None:
            break
    if part is None:
        part = message

    try:
        content = part.get_content()
    except Exception:
        # Truncated base64/quoted-printable payloads can fail to decode.
        payload = part.get_payload(decode=False)
        content = payload if isinstance(payload, str) else raw.decode("utf-8", "replace")

    if not isinstance(content, str):
        return ""
    if (part.get_content_subtype() or "").lower() == "html":
        return _html_to_text(content)
    return content.replace("\r\n", "\n").strip("\n")


def _extract_preview(raw: bytes) -> str:
    """Extract a short readable preview from a (possibly truncated) raw message."""
    if not raw:
        return ""
    try:
        message = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception:
        return _truncate(raw.decode("utf-8", errors="replace"))
    return _truncate(_message_text(message, raw))


def _header_datetime(message: Any) -> str:
    try:
        parsed = parsedate_to_datetime(str(message["Date"])) if message["Date"] else None
    except Exception:
        parsed = None
    return parsed.isoformat() if parsed else str(message["Date"] or "")


def _attachment_summary(message: Any) -> List[Dict[str, Any]]:
    attachments = []
    try:
        parts = list(message.iter_attachments())
    except Exception as e:
        log.debug("Could not enumerate attachments: %s", e)
        return attachments
    for part in parts:
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:
            payload = b""
        attachments.append({
            "filename": part.get_filename() or "",
            "content_type": part.get_content_type(),
            "size": len(payload),
        })
    return attachments


def _fetch_raw_message(client: imapclient.IMAPClient, uid: int, folder: str, parts: List[str]) -> bytes:
    fetched = client.fetch([uid], parts)
    raw = _fetch_body_bytes(fetched.get(uid, {}))
    if not raw:
        raise ToolError(
            f"No message with id {uid} in folder {folder!r}. Ids are specific to a "
            "folder; re-run search_emails with the same folder."
        )
    return raw


_REPLY_HEADERS = "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES SUBJECT FROM REPLY-TO)]"
_RE_PREFIX_RE = re.compile(r"(?i)^\s*(re|aw|sv|fwd?)\s*:\s*")


def _reply_context(uid: int, folder: str) -> Dict[str, Any]:
    """Read the headers needed to thread a reply onto an existing message."""
    with imap_session() as client:
        try:
            client.select_folder(folder, readonly=True)
        except Exception as e:
            raise ToolError(f"Could not open folder {folder!r}: {e}") from e
        raw = _fetch_raw_message(client, uid, folder, [_REPLY_HEADERS])

    original = email.message_from_bytes(raw, policy=email.policy.default)
    message_id = str(original["Message-ID"] or "").strip()
    references = str(original["References"] or "").split()
    if message_id and message_id not in references:
        references.append(message_id)
    reply_to = str(original["Reply-To"] or original["From"] or "").strip()
    subject = str(original["Subject"] or "").strip()
    if not _RE_PREFIX_RE.match(subject):
        subject = f"Re: {subject}" if subject else "Re:"
    return {
        "message_id": message_id,
        "references": references,
        "reply_to": [reply_to] if reply_to else [],
        "subject": subject,
    }


def _fetch_body_bytes(data: Dict[bytes, Any]) -> bytes:
    """Pull the raw message bytes out of an imapclient FETCH response.

    Partial fetches come back under a range-suffixed key such as b'BODY[]<0>'.
    """
    for key, value in data.items():
        if key.startswith(b"BODY[") and isinstance(value, bytes):
            return value
    return b""


def _parse_message_ids(message_ids: List[str]) -> List[int]:
    """Validate the ids handed back by search_emails (IMAP UIDs as strings)."""
    if not message_ids:
        # An empty list must never widen to "every message in the folder".
        raise ToolError("message_ids is empty; pass the 'id' values from search_emails.")
    uids: List[int] = []
    for raw in message_ids:
        text = str(raw).strip()
        if not text.isdigit():
            raise ToolError(f"Invalid message id {raw!r}; use the 'id' field from search_emails.")
        uids.append(int(text))
    return sorted(set(uids))


def _describe_messages(client: imapclient.IMAPClient, uids: List[int], folder: str) -> List[Dict[str, str]]:
    """Fetch envelopes for the given UIDs, refusing to proceed if any is missing.

    UIDs are per-folder, so a stale or mis-foldered id must fail loudly rather
    than have the remaining ids acted on as if the request were complete.
    """
    fetched = client.fetch(uids, ["ENVELOPE"])
    missing = [u for u in uids if not fetched.get(u, {}).get(b"ENVELOPE")]
    if missing:
        raise ToolError(
            f"No message with id {', '.join(map(str, missing))} in folder {folder!r}. "
            "Ids are specific to a folder; re-run search_emails with the same folder."
        )
    described = []
    for uid in uids:
        envelope = fetched[uid][b"ENVELOPE"]
        described.append({
            "id": str(uid),
            "from": _format_address(envelope.from_),
            "subject": _decode_header_value(envelope.subject),
            "date": envelope.date.isoformat() if envelope.date else "",
        })
    return described


def _special_folder(client: imapclient.IMAPClient, flag: bytes) -> Optional[str]:
    try:
        return client.find_special_folder(flag)
    except Exception as e:
        log.debug("Special folder lookup for %r failed: %s", flag, e)
        return None


def _backup_messages(folder: str, raw_by_uid: Dict[int, bytes]) -> str:
    """Save full copies of messages about to be permanently deleted."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_folder = re.sub(r"[^A-Za-z0-9._-]+", "_", folder) or "folder"
    directory = os.path.join(BACKUP_DIR, f"mail-{stamp}")
    os.makedirs(directory, exist_ok=True)
    for uid, raw in raw_by_uid.items():
        with open(os.path.join(directory, f"{safe_folder}-{uid}.eml"), "wb") as handle:
            handle.write(raw)
    return directory


def _parse_recipients(values: Optional[List[str]], field: str) -> List[str]:
    """Normalise a list of recipients, rejecting anything without a usable address."""
    if not values:
        return []
    recipients = []
    for raw in values:
        text = str(raw).strip()
        if not text:
            continue
        parsed = getaddresses([text])
        name, addr = parsed[0] if len(parsed) == 1 else ("", "")
        local, _, domain = addr.rpartition("@")
        if not local or "." not in domain or any(c.isspace() for c in addr):
            raise ToolError(f"Invalid {field} address {raw!r}.")
        recipients.append(f"{name} <{addr}>" if name else addr)
    return recipients


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
def search_emails(query: str = "ALL", folder: str = "INBOX", limit: int = 10) -> List[Dict[str, str]]:
    """
    Search for emails in a specific folder.

    Args:
        query: IMAP search query (e.g., 'UNSEEN', 'FROM "apple"', 'SUBJECT "hello"'). Default is 'ALL'.
        folder: The mail folder to search (default 'INBOX').
        limit: Maximum number of emails to return (most recent first).
    """
    if limit < 1:
        raise ToolError("limit must be at least 1.")

    with imap_session() as client:
        try:
            client.select_folder(folder, readonly=True)
        except Exception as e:
            raise ToolError(f"Could not open folder {folder!r}: {e}") from e

        # A malformed query must surface as an error. Silently falling back to
        # 'ALL' would return unfiltered mail that looks like a valid result set.
        try:
            messages = client.search(query)
        except Exception as e:
            raise ToolError(
                f"Invalid IMAP search query {query!r}: {e}. "
                'Use IMAP syntax, e.g. \'UNSEEN\', \'FROM "apple"\', \'SUBJECT "hello"\'.'
            ) from e

        if not messages:
            return []

        # Highest UIDs are the most recent.
        messages = sorted(messages)[-limit:]

        # PEEK avoids setting \Seen, and the byte range keeps large messages
        # and attachments off the wire -- only a preview is ever returned.
        body_part = f"BODY.PEEK[]<0.{PREVIEW_FETCH_BYTES}>"
        fetch_data = client.fetch(messages, ["ENVELOPE", body_part])

        results = []
        for msg_id in reversed(messages):
            data = fetch_data.get(msg_id)
            if not data:
                continue
            envelope = data.get(b"ENVELOPE")
            if not envelope:
                log.debug("Message %s returned no ENVELOPE; skipping.", msg_id)
                continue

            results.append({
                "id": str(msg_id),
                "from": _format_address(envelope.from_),
                "subject": _decode_header_value(envelope.subject),
                "date": envelope.date.isoformat() if envelope.date else "",
                "body_preview": _extract_preview(_fetch_body_bytes(data)),
            })

        return results


@mcp.tool(annotations={"readOnlyHint": True})
def get_email(message_id: str, folder: str = "INBOX", max_chars: int = DEFAULT_BODY_CHARS) -> Dict[str, Any]:
    """
    Fetch one email in full: headers, the decoded body, and attachment names.

    The body is the text/plain part when present, otherwise the HTML part
    converted to text. Attachments are listed but never downloaded. The
    message is not marked as read.

    Args:
        message_id: The 'id' from search_emails.
        folder: The folder the message lives in (default 'INBOX').
        max_chars: Cap on the returned body length; 'truncated' says whether it hit.
    """
    if max_chars < 1:
        raise ToolError("max_chars must be at least 1.")
    uid = _parse_message_ids([message_id])[0]

    with imap_session() as client:
        try:
            client.select_folder(folder, readonly=True)
        except Exception as e:
            raise ToolError(f"Could not open folder {folder!r}: {e}") from e
        raw = _fetch_raw_message(client, uid, folder, ["BODY.PEEK[]"])

    message = email.message_from_bytes(raw, policy=email.policy.default)
    text = _message_text(message, raw)
    truncated = len(text) > max_chars
    return {
        "id": str(uid),
        "folder": folder,
        "from": str(message["From"] or ""),
        "to": str(message["To"] or ""),
        "cc": str(message["Cc"] or ""),
        "reply_to": str(message["Reply-To"] or ""),
        "subject": str(message["Subject"] or ""),
        "date": _header_datetime(message),
        "message_id": str(message["Message-ID"] or "").strip(),
        "in_reply_to": str(message["In-Reply-To"] or "").strip(),
        "body": text[:max_chars] + ("..." if truncated else ""),
        "truncated": truncated,
        "attachments": _attachment_summary(message),
    }


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
def delete_emails(
    message_ids: List[str],
    folder: str = "INBOX",
    permanent: bool = False,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """
    Delete emails from a folder. Modifies the live mailbox -- preview first.

    By default messages are moved to the account's Trash folder, from which
    they can be recovered. With permanent=True they are flagged \\Deleted and
    expunged instead; a full copy of each is saved under backups/ first.

    Args:
        message_ids: The 'id' values from search_emails. Ids are specific to
            the folder they were found in.
        folder: The folder the messages live in (default 'INBOX').
        permanent: Expunge outright instead of moving to Trash. Required to
            delete messages that are already in Trash.
        dry_run: When True (the default) report what would be deleted without
            touching anything.
    """
    uids = _parse_message_ids(message_ids)

    with imap_session() as client:
        try:
            client.select_folder(folder, readonly=dry_run)
        except Exception as e:
            raise ToolError(f"Could not open folder {folder!r}: {e}") from e

        messages = _describe_messages(client, uids, folder)

        trash = _special_folder(client, TRASH_FLAG)
        if permanent:
            action, destination = "delete_permanently", None
        else:
            if trash is None:
                raise ToolError(
                    "Could not locate the Trash folder on this account. Pass "
                    "permanent=True to delete outright."
                )
            if trash == folder:
                raise ToolError(
                    f"Messages in {folder!r} are already in Trash; pass permanent=True "
                    "to delete them outright."
                )
            action, destination = "move_to_trash", trash

        backup_path = None
        if not dry_run:
            if permanent:
                fetched = client.fetch(uids, ["BODY.PEEK[]"])
                backup_path = _backup_messages(
                    folder, {uid: _fetch_body_bytes(fetched.get(uid, {})) for uid in uids})
                client.delete_messages(uids)
                _expunge(client, uids)
            else:
                _move_uids(client, uids, destination)

    return {
        "dry_run": dry_run,
        "deleted": not dry_run,
        "action": action,
        "folder": folder,
        "destination": destination,
        "backup": backup_path,
        "messages": messages,
    }


def _move_uids(client: imapclient.IMAPClient, uids: List[int], destination: str) -> None:
    """Move UIDs out of the selected folder, atomically where the server allows."""
    if client.has_capability("MOVE"):
        client.move(uids, destination)
    else:
        client.copy(uids, destination)
        client.delete_messages(uids)
        _expunge(client, uids)


def _expunge(client: imapclient.IMAPClient, uids: List[int]) -> None:
    """Expunge only the given UIDs where the server allows it.

    A plain EXPUNGE removes every message in the folder carrying \\Deleted,
    including ones flagged by other clients, so it is used only as a fallback.
    """
    if client.has_capability("UIDPLUS"):
        client.uid_expunge(uids)
    else:
        log.warning("Server lacks UIDPLUS; expunging all \\Deleted messages in the folder.")
        client.expunge()


_FOLDER_ROLES = {
    b"\\Inbox": "inbox", b"\\Sent": "sent", b"\\Drafts": "drafts", b"\\Trash": "trash",
    b"\\Junk": "junk", b"\\Archive": "archive", b"\\All": "all", b"\\Flagged": "flagged",
}
# iCloud flags only Sent and Trash in LIST (and does not advertise SPECIAL-USE),
# so the rest are recognised by the names Apple and other providers use.
_FOLDER_ROLE_NAMES = {
    "inbox": "inbox",
    "sent": "sent", "sent items": "sent", "sent messages": "sent",
    "drafts": "drafts",
    "trash": "trash", "deleted items": "trash", "deleted messages": "trash", "deleted": "trash",
    "junk": "junk", "spam": "junk", "junk e-mail": "junk",
    "archive": "archive",
}


def _folder_role(name: str, flags: Tuple[bytes, ...]) -> str:
    role = next((r for f, r in _FOLDER_ROLES.items() if f in flags), "")
    return role or _FOLDER_ROLE_NAMES.get(name.strip().lower(), "")


@mcp.tool(annotations={"readOnlyHint": True})
def list_folders() -> List[Dict[str, Any]]:
    """
    List every mail folder with its message counts and special role.

    Use the 'name' values as the 'folder' argument to the other mail tools.
    'role' is one of inbox, sent, drafts, trash, junk, archive or '' for an
    ordinary folder; 'selectable' is False for containers that hold no mail.
    """
    with imap_session() as client:
        try:
            listing = client.list_folders()
        except Exception as e:
            raise ToolError(f"Could not list folders: {e}") from e

        results = []
        for flags, _delimiter, name in listing:
            flags = tuple(flags or ())
            role = _folder_role(name, flags)
            selectable = b"\\Noselect" not in flags
            entry: Dict[str, Any] = {"name": name, "role": role, "selectable": selectable,
                                     "total": None, "unseen": None}
            if selectable:
                try:
                    status = client.folder_status(name, ["MESSAGES", "UNSEEN"])
                    entry["total"] = status.get(b"MESSAGES")
                    entry["unseen"] = status.get(b"UNSEEN")
                except Exception as e:
                    log.debug("STATUS failed for %r: %s", name, e)
            results.append(entry)
        return results


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
def move_emails(
    message_ids: List[str],
    to_folder: str,
    folder: str = "INBOX",
    dry_run: bool = True,
) -> Dict[str, Any]:
    """
    Move emails from one folder to another (archive, file, or restore from Trash).

    Args:
        message_ids: The 'id' values from search_emails, specific to 'folder'.
        to_folder: Destination folder name; see list_folders.
        folder: The folder the messages currently live in (default 'INBOX').
        dry_run: When True (the default) report what would move without touching anything.
    """
    uids = _parse_message_ids(message_ids)
    to_folder = to_folder.strip()
    if not to_folder:
        raise ToolError("to_folder is required; see list_folders for names.")
    if to_folder == folder:
        raise ToolError(f"Messages are already in {folder!r}.")

    with imap_session() as client:
        try:
            if not client.folder_exists(to_folder):
                raise ToolError(f"Destination folder {to_folder!r} does not exist; see list_folders.")
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"Could not check folder {to_folder!r}: {e}") from e
        try:
            client.select_folder(folder, readonly=dry_run)
        except Exception as e:
            raise ToolError(f"Could not open folder {folder!r}: {e}") from e

        messages = _describe_messages(client, uids, folder)
        if not dry_run:
            _move_uids(client, uids, to_folder)

    return {
        "dry_run": dry_run,
        "moved": not dry_run,
        "folder": folder,
        "destination": to_folder,
        "messages": messages,
    }


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
def mark_emails(
    message_ids: List[str],
    folder: str = "INBOX",
    read: Optional[bool] = None,
    flagged: Optional[bool] = None,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """
    Mark emails as read/unread or flagged/unflagged.

    Args:
        message_ids: The 'id' values from search_emails, specific to 'folder'.
        folder: The folder the messages live in (default 'INBOX').
        read: True marks as read, False marks as unread, None leaves it alone.
        flagged: True flags, False unflags, None leaves it alone.
        dry_run: When True (the default) report what would change without touching anything.
    """
    if read is None and flagged is None:
        raise ToolError("Nothing to do: pass read and/or flagged.")
    uids = _parse_message_ids(message_ids)
    add = [f for f, on in ((imapclient.SEEN, read), (imapclient.FLAGGED, flagged)) if on is True]
    remove = [f for f, on in ((imapclient.SEEN, read), (imapclient.FLAGGED, flagged)) if on is False]

    with imap_session() as client:
        try:
            client.select_folder(folder, readonly=dry_run)
        except Exception as e:
            raise ToolError(f"Could not open folder {folder!r}: {e}") from e

        messages = _describe_messages(client, uids, folder)
        if not dry_run:
            if add:
                client.add_flags(uids, add, silent=True)
            if remove:
                client.remove_flags(uids, remove, silent=True)

    return {
        "dry_run": dry_run,
        "changed": not dry_run,
        "folder": folder,
        "add_flags": [f.decode() for f in add],
        "remove_flags": [f.decode() for f in remove],
        "messages": messages,
    }


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
def send_email(
    body: str,
    to: Optional[List[str]] = None,
    subject: Optional[str] = None,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    reply_to_id: Optional[str] = None,
    reply_folder: str = "INBOX",
    dry_run: bool = True,
) -> Dict[str, Any]:
    """
    Send a plain-text email from the iCloud account. Preview first.

    The message goes out over SMTP and a copy is then filed in the account's
    Sent folder so it appears in Mail on every device. Sending cannot be
    undone, so the default is a dry run that renders the message without
    sending it.

    To reply to a message, pass its 'id' from search_emails as reply_to_id.
    The reply is threaded onto the original (In-Reply-To and References), and
    'to' and 'subject' default to the original sender and 'Re: <subject>'
    unless you pass them explicitly. The original text is not quoted; include
    any quote you want in the body.

    Args:
        body: Plain-text body.
        to: Recipient addresses, e.g. ['ann@example.com', 'Bob <bob@example.com>'].
            Required unless replying.
        subject: Subject line. Required unless replying.
        cc: Optional Cc recipients.
        bcc: Optional Bcc recipients (not included in the headers).
        reply_to_id: Id of the message being replied to.
        reply_folder: Folder that message lives in (default 'INBOX').
        dry_run: When True (the default) return the rendered message without sending.
    """
    address, _ = _require_credentials()
    if not body.strip():
        raise ToolError("body is required.")
    to_list = _parse_recipients(to, "to")
    cc_list = _parse_recipients(cc, "cc")
    bcc_list = _parse_recipients(bcc, "bcc")

    context: Optional[Dict[str, Any]] = None
    if reply_to_id is not None:
        uid = _parse_message_ids([reply_to_id])[0]
        context = _reply_context(uid, reply_folder)
        if not to_list:
            to_list = _parse_recipients(context["reply_to"], "to")
        if subject is None or not subject.strip():
            subject = context["subject"]

    if not to_list:
        raise ToolError("At least one 'to' recipient is required.")
    if subject is None or not subject.strip():
        raise ToolError("subject is required.")

    message = EmailMessage()
    message["From"] = address
    message["To"] = ", ".join(to_list)
    if cc_list:
        message["Cc"] = ", ".join(cc_list)
    message["Subject"] = subject.strip()
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=address.rsplit("@", 1)[-1])
    if context and context["message_id"]:
        message["In-Reply-To"] = context["message_id"]
        message["References"] = " ".join(context["references"])
    message.set_content(body)

    result: Dict[str, Any] = {
        "dry_run": dry_run,
        "sent": False,
        "from": address,
        "to": to_list,
        "cc": cc_list,
        "bcc": bcc_list,
        "subject": message["Subject"],
        "body": body,
        "in_reply_to": str(message["In-Reply-To"] or ""),
        "references": str(message["References"] or ""),
    }
    if dry_run:
        return result

    recipients = to_list + cc_list + bcc_list
    smtp = _connect_smtp()
    try:
        smtp.send_message(message, from_addr=address, to_addrs=recipients)
    except smtplib.SMTPException as e:
        raise ToolError(f"SMTP rejected the message: {e}") from e
    finally:
        _quiet_quit(smtp)
    result["sent"] = True

    # iCloud's SMTP does not file outgoing mail; clients APPEND it themselves.
    # The message has already left, so a failure here is reported, not raised.
    result["saved_to_sent"] = False
    try:
        with imap_session() as client:
            sent_folder = _special_folder(client, SENT_FLAG)
            if sent_folder is None:
                log.warning("Could not locate the Sent folder; message not filed.")
            else:
                wire = message.as_bytes(policy=message.policy.clone(linesep="\r\n"))
                client.append(sent_folder, wire, flags=[imapclient.SEEN])
                result["saved_to_sent"] = True
    except Exception as e:
        log.warning("Message sent but could not be filed in Sent: %s", e)
    return result


def _parse_iso(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise ToolError(
            f"{field} must be an ISO 8601 datetime (e.g. '2026-01-01T00:00:00Z'), got {value!r}."
        ) from e
    # Mixing naive and aware datetimes raises on comparison, so normalise.
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _as_datetime(value: Any) -> Optional[datetime]:
    """Normalise an icalendar date/datetime to an aware datetime for sorting."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    if isinstance(value, date):
        # All-day events carry a plain date.
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return None


@mcp.tool(annotations={"readOnlyHint": True})
def get_calendar_events(start_date: Optional[str] = None, end_date: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Get calendar events from iCloud within a date range, across all calendars.

    Recurring events are expanded, so each occurrence in the range is returned
    separately. Results are sorted by start time. Each result carries an 'id'
    (the iCalendar UID) for use with delete_event.

    Args:
        start_date: ISO 8601 date string (e.g. '2026-01-01T00:00:00Z'). Defaults to now.
        end_date: ISO 8601 date string (e.g. '2026-12-31T23:59:59Z'). Defaults to 90 days after start_date.
        limit: Maximum number of events to return.
    """
    if limit < 1:
        raise ToolError("limit must be at least 1.")

    start = _parse_iso(start_date, "start_date") if start_date else datetime.now(timezone.utc)
    end = _parse_iso(end_date, "end_date") if end_date else start + DEFAULT_CALENDAR_WINDOW
    if end <= start:
        raise ToolError(f"end_date ({end.isoformat()}) must be after start_date ({start.isoformat()}).")

    try:
        principal = get_caldav_client().principal()
        calendars = principal.calendars()
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"Failed to connect to iCloud calendar: {e}") from e

    dated: List[Tuple[datetime, Dict[str, Any]]] = []
    undated: List[Dict[str, Any]] = []

    for calendar in calendars:
        try:
            # A closed range is required for expansion, and keeps iCloud from
            # returning the entire calendar history.
            events = calendar.search(start=start, end=end, event=True, expand=True)
        except Exception as e:
            log.warning("Search failed for calendar %r: %s", calendar.name, e)
            continue

        for event in events:
            try:
                vevent = event.vobject_instance.vevent
                summary = vevent.summary.value if hasattr(vevent, "summary") else "No Title"
                raw_start = vevent.dtstart.value if hasattr(vevent, "dtstart") else None
                raw_end = vevent.dtend.value if hasattr(vevent, "dtend") else None
                uid = str(vevent.uid.value) if hasattr(vevent, "uid") else ""
            except Exception as e:
                log.warning("Skipping unparseable event in %r: %s", calendar.name, e)
                continue

            entry = {
                "id": uid,
                "calendar": calendar.name,
                "summary": summary,
                "start": raw_start.isoformat() if raw_start is not None else "",
                "end": raw_end.isoformat() if raw_end is not None else "",
                "all_day": isinstance(raw_start, date) and not isinstance(raw_start, datetime),
            }

            # Sort on real datetimes: lexicographic ISO sorting is wrong across
            # mixed all-day dates, offsets and time zones.
            sort_key = _as_datetime(raw_start)
            if sort_key is None:
                undated.append(entry)
            else:
                dated.append((sort_key, entry))

    dated.sort(key=lambda pair: pair[0])
    return [entry for _, entry in dated][:limit] + undated[: max(0, limit - len(dated))]


def _calendars() -> List[Any]:
    try:
        return list(get_caldav_client().principal().calendars())
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"Failed to connect to iCloud calendar: {e}") from e


def _pick_calendar(calendars: List[Any], name: Optional[str]) -> Any:
    """Choose the target calendar, refusing to guess between several."""
    names = [c.name for c in calendars]
    if not calendars:
        raise ToolError("No calendars found on this account.")
    if name:
        match = next((c for c in calendars if c.name == name), None)
        if match is None:
            raise ToolError(f"No calendar named {name!r}. Available: {', '.join(map(repr, names))}.")
        return match
    if len(calendars) == 1:
        return calendars[0]
    raise ToolError(
        f"This account has several calendars; pass calendar=... "
        f"Available: {', '.join(map(repr, names))}."
    )


def _ical_escape(value: str) -> str:
    return (value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _ical_fold(line: str) -> str:
    """Fold a content line at 75 octets as RFC 5545 requires."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    pieces, current = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        limit = 75 if not pieces else 74
        if len(current) + len(b) > limit:
            pieces.append(current)
            current = b""
        current += b
    pieces.append(current)
    return "\r\n ".join(p.decode("utf-8") for p in pieces)


def _parse_event_time(value: str, field: str, all_day: bool) -> Any:
    if all_day:
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError as e:
            raise ToolError(f"{field} must be a date (YYYY-MM-DD) for an all-day event, got {value!r}.") from e
    return _parse_iso(value, field)


def _ical_time(value: Any, prop: str) -> str:
    if isinstance(value, datetime):
        return f"{prop}:{value.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    return f"{prop};VALUE=DATE:{value.strftime('%Y%m%d')}"


def _build_event_ical(uid: str, summary: str, start: Any, end: Any,
                      location: Optional[str], description: Optional[str]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Honeycrisp//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        _ical_time(start, "DTSTART"),
        _ical_time(end, "DTEND"),
        f"SUMMARY:{_ical_escape(summary)}",
    ]
    if location:
        lines.append(f"LOCATION:{_ical_escape(location)}")
    if description:
        lines.append(f"DESCRIPTION:{_ical_escape(description)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_ical_fold(line) for line in lines) + "\r\n"


def _backup_text(prefix: str, ext: str, text: str) -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(BACKUP_DIR, f"{prefix}-{stamp}.{ext}")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return path


def _event_by_uid(calendar: Any, uid: str) -> Optional[Any]:
    """Locate an event in one calendar by UID, or return None.

    iCloud stores each event at <calendar>/<UID>.ics and rejects the
    calendar-query-by-UID REPORT with 412, so a direct GET is tried first and
    the REPORT is kept only as a fallback for servers that lay objects out
    differently.
    """
    url = str(calendar.url).rstrip("/") + "/" + uid + ".ics"
    try:
        event = caldav.Event(calendar.client, url=url, parent=calendar)
        event.load()
        return event
    except NotFoundError:
        pass
    except Exception as e:
        log.debug("Direct GET of %s failed: %s", url, e)
    try:
        return calendar.get_event_by_uid(uid)
    except NotFoundError:
        return None


def _event_summary(event: Any) -> Dict[str, Any]:
    try:
        vevent = event.vobject_instance.vevent
        raw_start = vevent.dtstart.value if hasattr(vevent, "dtstart") else None
        raw_end = vevent.dtend.value if hasattr(vevent, "dtend") else None
        return {
            "summary": vevent.summary.value if hasattr(vevent, "summary") else "No Title",
            "start": raw_start.isoformat() if raw_start is not None else "",
            "end": raw_end.isoformat() if raw_end is not None else "",
        }
    except Exception as e:
        log.debug("Could not summarise event: %s", e)
        return {"summary": "", "start": "", "end": ""}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
def create_event(
    summary: str,
    start: str,
    end: Optional[str] = None,
    calendar: Optional[str] = None,
    all_day: bool = False,
    location: Optional[str] = None,
    description: Optional[str] = None,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """
    Create a calendar event. Preview first.

    Times are ISO 8601; a value without an offset is treated as UTC, so pass
    an offset (e.g. '2026-09-10T15:00:00-05:00') for local times. All-day
    events take dates (YYYY-MM-DD) and 'end' is the day after the last day.

    Args:
        summary: Event title.
        start: Start datetime, or start date when all_day is True.
        end: End datetime or date. Defaults to one hour after start, or the
            next day for all-day events.
        calendar: Calendar name (see get_calendar_events results). Required
            when the account has more than one calendar.
        all_day: Create an all-day event.
        location: Optional location text.
        description: Optional notes.
        dry_run: When True (the default) return the event without creating it.
    """
    if not summary.strip():
        raise ToolError("summary is required.")
    start_value = _parse_event_time(start, "start", all_day)
    if end:
        end_value = _parse_event_time(end, "end", all_day)
    else:
        end_value = start_value + (timedelta(days=1) if all_day else timedelta(hours=1))
    if end_value <= start_value:
        raise ToolError(f"end ({end_value.isoformat()}) must be after start ({start_value.isoformat()}).")

    target = _pick_calendar(_calendars(), calendar)
    uid = str(uuid.uuid4()).upper()
    ical = _build_event_ical(uid, summary.strip(), start_value, end_value, location, description)

    if not dry_run:
        try:
            target.add_event(ical=ical, no_overwrite=True)
        except Exception as e:
            raise ToolError(f"Creating the event in {target.name!r} failed: {e}") from e

    return {
        "dry_run": dry_run,
        "created": not dry_run,
        "id": uid,
        "calendar": target.name,
        "summary": summary.strip(),
        "start": start_value.isoformat(),
        "end": end_value.isoformat(),
        "all_day": all_day,
        "location": location or "",
        "description": description or "",
    }


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
def delete_event(event_id: str, calendar: Optional[str] = None, dry_run: bool = True) -> Dict[str, Any]:
    """
    Delete a calendar event. Modifies live data -- preview first.

    Deleting a recurring event removes every occurrence. The event's
    iCalendar text is saved under backups/ before it is removed.

    Args:
        event_id: The 'id' from get_calendar_events.
        calendar: Calendar name, to skip searching the others.
        dry_run: When True (the default) report the event without deleting it.
    """
    uid = event_id.strip()
    if not uid:
        raise ToolError("event_id is required; use the 'id' field from get_calendar_events.")

    calendars = _calendars()
    if calendar:
        calendars = [_pick_calendar(calendars, calendar)]

    found, failures = None, []
    for cal in calendars:
        try:
            event = _event_by_uid(cal, uid)
        except Exception as e:
            failures.append(f"{cal.name!r}: {e}")
            log.warning("Lookup failed in calendar %r: %s", cal.name, e)
            continue
        if event is not None:
            found = (cal, event)
            break
    if found is None:
        hint = f" Lookup failed in {', '.join(failures)}." if failures else ""
        raise ToolError(f"No event found with id {uid!r}.{hint}")
    cal, event = found

    backup_path = None
    if not dry_run:
        backup_path = _backup_text("event", "ics", str(event.data or ""))
        try:
            event.delete()
        except Exception as e:
            raise ToolError(f"Deleting the event from {cal.name!r} failed: {e}") from e

    return {
        "dry_run": dry_run,
        "deleted": not dry_run,
        "id": uid,
        "calendar": cal.name,
        "backup": backup_path,
        **_event_summary(event),
    }


@mcp.tool(annotations={"readOnlyHint": True})
def search_notes(query: str = "ALL", limit: int = 10) -> List[Dict[str, str]]:
    """
    Search legacy iCloud notes stored in the IMAP 'Notes' folder.

    IMPORTANT: notes created in the modern Notes app sync over CloudKit, not
    IMAP, and are NOT visible here. This only reaches notes stored on the IMAP
    account, which for most accounts is empty. An empty result does not mean
    the user has no notes.

    Args:
        query: IMAP search query. Default is 'ALL'.
        limit: Maximum number of notes to return.
    """
    try:
        return search_emails(query=query, folder="Notes", limit=limit)
    except ToolError as e:
        if "Could not open folder" in str(e):
            raise ToolError(
                "No IMAP 'Notes' folder exists on this account. Notes created in "
                "the modern Notes app sync over CloudKit and are not reachable "
                "over IMAP."
            ) from e
        raise


# iCloud intermittently answers a correctly authenticated discovery PROPFIND
# with an empty 401 (observed 2026-09-08 on the principal URL, clearing within
# seconds), so a 401 is retried briefly before being reported as an auth error.
CARDDAV_401_RETRIES = 3
CARDDAV_RETRY_DELAY = 1.5


def _carddav_propfind(url: str, depth: int, body: str, auth: HTTPBasicAuth) -> ET.Element:
    for attempt in range(CARDDAV_401_RETRIES + 1):
        response = requests.request(
            "PROPFIND", url,
            auth=auth,
            headers={"Depth": str(depth), "Content-Type": "application/xml; charset=utf-8"},
            data=body.encode("utf-8"),
            timeout=HTTP_TIMEOUT,
        )
        if response.status_code != 401 or attempt == CARDDAV_401_RETRIES:
            break
        log.warning("CardDAV PROPFIND %s returned 401; retrying (%d/%d).", url, attempt + 1, CARDDAV_401_RETRIES)
        time.sleep(CARDDAV_RETRY_DELAY)
    if response.status_code not in (200, 207):
        raise ToolError(f"CardDAV PROPFIND failed ({response.status_code}): {response.text[:200]}")
    return ET.fromstring(response.text)


# Some cards in the wild have a metadata property concatenated onto the end of
# a TEL/EMAIL value (e.g. '+12025550143X-SHARED-PHOTO-DISPLAY-PREF:ALWAYS_ASK').
# The line arrives from the server already joined, so this is corrupt stored
# data rather than a parsing artifact -- but such a suffix is never part of a
# real value, so it is stripped and reported.
_EMBEDDED_PROPERTY_RE = re.compile(r"(?:X-[A-Z0-9-]+|[A-Z][A-Z0-9-]{2,}):.*$")

# The same defect turns up outside TEL/EMAIL -- observed in ADR and X-ABLabel.
# Those fields CAN legitimately contain a colon, as can REV, URL, NOTE and
# PHOTO, so the sweep across arbitrary properties matches only property names
# actually seen glued into values. A loose pattern would corrupt real data.
GLUED_METADATA_PROPERTIES = ("X-SHARED-PHOTO-DISPLAY-PREF",)
_GLUED_METADATA_RE = re.compile(
    r"(?:" + "|".join(re.escape(p) for p in GLUED_METADATA_PROPERTIES) + r"):[^\r\n]*$"
)


def _clean_value(value: str) -> Tuple[str, bool]:
    cleaned = _EMBEDDED_PROPERTY_RE.sub("", value).strip()
    return (cleaned, True) if cleaned != value.strip() else (value.strip(), False)


def _vcard_values(card: Any, field: str) -> List[str]:
    """Return every value for a vCard field (contacts often have several)."""
    entries = getattr(card, f"{field}_list", None)
    if not entries:
        return []
    values = []
    for entry in entries:
        value = getattr(entry, "value", None)
        if not isinstance(value, str) or not value.strip():
            continue
        cleaned, repaired = _clean_value(value)
        if repaired:
            log.warning(
                "Stripped an embedded vCard property from a %s value; the stored "
                "contact data is corrupt: %r -> %r", field.upper(), value, cleaned
            )
        if cleaned:
            values.append(cleaned)
    return values


def _structured_name(card: Any) -> str:
    """Assemble a display name from the structured N property."""
    n = getattr(card, "n", None)
    value = getattr(n, "value", None)
    if value is None:
        return ""
    parts = [
        getattr(value, "prefix", "") or "",
        getattr(value, "given", "") or "",
        getattr(value, "additional", "") or "",
        getattr(value, "family", "") or "",
        getattr(value, "suffix", "") or "",
    ]
    return _WS_RE.sub(" ", " ".join(parts)).strip()


def _organization(card: Any) -> str:
    value = getattr(getattr(card, "org", None), "value", None)
    if isinstance(value, list):
        return _WS_RE.sub(" ", " ".join(p for p in value if p)).strip()
    return (value or "").strip() if isinstance(value, str) else ""


def _contact_name(card: Any) -> str:
    """Best available display name.

    iCloud stores plenty of contacts with an empty FN but a populated N, so
    falling back to the structured name is what makes them identifiable at all.
    """
    fn = getattr(getattr(card, "fn", None), "value", "")
    if isinstance(fn, str) and fn.strip():
        return fn.strip()
    return _structured_name(card) or _organization(card)


# --------------------------------------------------------------------------
# CardDAV transport
# --------------------------------------------------------------------------


class Card(NamedTuple):
    """One address-book entry, with what is needed to safely write it back."""
    url: str
    etag: str
    raw: str


def _addressbook_urls(auth: HTTPBasicAuth) -> List[str]:
    """Discover every address book collection.

    Apple redirects to partition hosts (e.g. p61-contacts.icloud.com) and
    returns absolute hrefs, so every href must be resolved with urljoin --
    naive concatenation corrupts them.
    """
    principal_xml = _carddav_propfind(
        CARDDAV_URL, 0,
        '<?xml version="1.0" encoding="utf-8" ?><propfind xmlns="DAV:"><prop><current-user-principal/></prop></propfind>',
        auth,
    )
    principal_elem = principal_xml.find('.//{DAV:}current-user-principal/{DAV:}href')
    if principal_elem is None or not principal_elem.text:
        raise ToolError("CardDAV discovery failed: no principal URL in the server response.")
    principal_url = urljoin(CARDDAV_URL, principal_elem.text)

    home_xml = _carddav_propfind(
        principal_url, 0,
        '<?xml version="1.0" encoding="utf-8" ?><propfind xmlns="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav"><prop><card:addressbook-home-set/></prop></propfind>',
        auth,
    )
    home_elem = home_xml.find('.//{urn:ietf:params:xml:ns:carddav}addressbook-home-set/{DAV:}href')
    if home_elem is None or not home_elem.text:
        raise ToolError("CardDAV discovery failed: no addressbook-home-set in the server response.")
    home_url = urljoin(principal_url, home_elem.text)

    collections = _carddav_propfind(
        home_url, 1,
        '<?xml version="1.0" encoding="utf-8" ?><propfind xmlns="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav"><prop><resourcetype/></prop></propfind>',
        auth,
    )
    urls = []
    for response in collections.findall('{DAV:}response'):
        resourcetype = response.find('.//{DAV:}resourcetype')
        if resourcetype is None:
            continue
        if resourcetype.find('{urn:ietf:params:xml:ns:carddav}addressbook') is None:
            continue
        href = response.find('{DAV:}href')
        if href is not None and href.text:
            urls.append(urljoin(home_url, href.text))
    return urls


def _fetch_cards(auth: HTTPBasicAuth, query: str = "") -> List[Card]:
    """REPORT every address book, returning each card with its URL and ETag.

    The ETag is what makes safe writes possible: it is sent back as If-Match so
    a concurrent edit from another device is rejected rather than overwritten.
    """
    # The query is interpolated into an XML document, so it must be escaped: an
    # unescaped '&' or '<' (e.g. "Smith & Sons") produces malformed XML.
    if query:
        prop_filter = (
            '<card:prop-filter name="FN">'
            '<card:text-match collation="i;unicode-casemap" match-type="contains">'
            f'{xml_escape(query)}</card:text-match></card:prop-filter>'
        )
    else:
        prop_filter = '<card:prop-filter name="FN" />'

    report_body = f"""<?xml version="1.0" encoding="utf-8" ?>
<card:addressbook-query xmlns:DAV="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
    <DAV:prop>
        <DAV:getetag />
        <card:address-data />
    </DAV:prop>
    <card:filter>
        {prop_filter}
    </card:filter>
</card:addressbook-query>"""

    cards: List[Card] = []
    for addressbook_url in _addressbook_urls(auth):
        response = requests.request(
            "REPORT", addressbook_url,
            auth=auth,
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            data=report_body.encode("utf-8"),
            timeout=HTTP_TIMEOUT,
        )
        if response.status_code not in (200, 207):
            log.warning("CardDAV REPORT failed for %s (%s)", addressbook_url, response.status_code)
            continue

        for entry in ET.fromstring(response.text).findall('{DAV:}response'):
            data = entry.find('.//{urn:ietf:params:xml:ns:carddav}address-data')
            href = entry.find('{DAV:}href')
            etag = entry.find('.//{DAV:}getetag')
            if data is None or not data.text or href is None or not href.text:
                continue
            cards.append(Card(
                url=urljoin(addressbook_url, href.text),
                etag=(etag.text or "") if etag is not None else "",
                raw=data.text,
            ))
    return cards


def _put_card(auth: HTTPBasicAuth, card: Card, new_text: str) -> None:
    """Write a card back, refusing to clobber a concurrent change."""
    headers = {"Content-Type": "text/vcard; charset=utf-8"}
    if card.etag:
        headers["If-Match"] = card.etag
    response = requests.request(
        "PUT", card.url, auth=auth, headers=headers,
        data=new_text.encode("utf-8"), timeout=HTTP_TIMEOUT,
    )
    if response.status_code == 412:
        raise ToolError(
            f"{card.url} changed on the server since it was read; nothing was written. "
            "Re-run to pick up the current version."
        )
    if response.status_code not in (200, 201, 204):
        raise ToolError(f"Writing {card.url} failed ({response.status_code}): {response.text[:200]}")


# --------------------------------------------------------------------------
# vCard editing
#
# Edits are made line by line and every untouched line is preserved verbatim,
# so photos, custom properties and folding elsewhere in the card survive
# byte-identical. Rewriting whole cards would risk losing data we never parsed.
# --------------------------------------------------------------------------


def _vcard_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")


def _split_vcard(raw: str) -> Tuple[List[str], str]:
    terminator = "\r\n" if "\r\n" in raw else "\n"
    return raw.split(terminator), terminator


def _property_name(line: str) -> str:
    """Property name of a physical line, or '' for continuations and junk."""
    if not line or line[:1] in (" ", "\t"):
        return ""
    prop, sep, _ = line.partition(":")
    return prop.split(";", 1)[0].upper() if sep else ""


def _vcard_unescape(value: str) -> str:
    return value.replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _split_components(value: str) -> List[str]:
    """Split a structured value on unescaped semicolons."""
    return [_vcard_unescape(part) for part in re.split(r"(?<!\\);", value)]


def _name_from_lines(lines: List[str]) -> str:
    """Derive a display name straight from the N (then ORG) line.

    Deliberately independent of vobject: a card with, say, an undecodable
    inline photo must still get its name repaired.
    """
    for line in lines:
        if _property_name(line) == "N":
            parts = (_split_components(line.partition(":")[2]) + [""] * 5)[:5]
            family, given, additional, prefix, suffix = parts
            name = _WS_RE.sub(" ", " ".join([prefix, given, additional, family, suffix])).strip()
            if name:
                return name
    for line in lines:
        if _property_name(line) == "ORG":
            parts = _split_components(line.partition(":")[2])
            org = _WS_RE.sub(" ", " ".join(p for p in parts if p)).strip()
            if org:
                return org
    return ""


def _plan_card_repairs(raw: str) -> Tuple[str, List[str]]:
    """Repair the two defect classes seen in real iCloud data.

    1. A metadata property concatenated onto a TEL/EMAIL value. The suffix is
       split back out onto its own line rather than discarded.
    2. An empty FN, where the name is present in the structured N property.
    """
    lines, terminator = _split_vcard(raw)
    fallback_name = _name_from_lines(lines)

    out: List[str] = []
    changes: List[str] = []
    for line in lines:
        name = _property_name(line)
        prop, _, value = line.partition(":")

        if name:
            # TEL/EMAIL never legitimately contain a colon, so they take the
            # broader pattern. Every other property takes the strict allowlist.
            if name in ("TEL", "EMAIL"):
                match = _EMBEDDED_PROPERTY_RE.search(value)
                cleaned = value[:match.start()].strip() if match else value
            else:
                match = _GLUED_METADATA_RE.search(value)
                # No strip here: a structured value such as ADR ends in a
                # meaningful empty component (';'), which strip would keep but
                # which must not be confused with padding.
                cleaned = value[:match.start()] if match else value
            if match:
                recovered = match.group(0).strip()
                out.append(f"{prop}:{cleaned}")
                out.append(recovered)
                changes.append(
                    f"{name} {value!r} -> {cleaned!r}; split off {recovered.split(':', 1)[0]}"
                )
                continue

        if name == "FN" and not value.strip() and fallback_name:
            out.append(f"FN:{_vcard_escape(fallback_name)}")
            changes.append(f"FN '' -> {fallback_name!r}")
            continue

        out.append(line)

    return terminator.join(out), changes


def _apply_contact_updates(
    raw: str,
    name: Optional[str],
    phones: Optional[List[str]],
    emails: Optional[List[str]],
) -> Tuple[str, List[str]]:
    """Apply replace-semantics updates, preserving type params where possible.

    An existing TEL/EMAIL line whose value is being kept is left untouched, so
    its type labels (CELL, WORK, ...) survive. Only genuinely new values are
    added, as bare properties.
    """
    lines, terminator = _split_vcard(raw)
    keep_phones = None if phones is None else {p.strip() for p in phones if p.strip()}
    keep_emails = None if emails is None else {e.strip() for e in emails if e.strip()}
    seen_phones, seen_emails = set(), set()

    out: List[str] = []
    changes: List[str] = []
    fn_set = False

    for line in lines:
        prop_name = _property_name(line)
        prop, _, value = line.partition(":")
        current = _clean_value(value)[0]

        if name is not None and prop_name == "FN":
            if current != name:
                changes.append(f"FN {current!r} -> {name!r}")
            out.append(f"FN:{_vcard_escape(name)}")
            fn_set = True
            continue

        if keep_phones is not None and prop_name == "TEL":
            if current in keep_phones:
                seen_phones.add(current)
                out.append(line)
            else:
                changes.append(f"removed TEL {current!r}")
            continue

        if keep_emails is not None and prop_name == "EMAIL":
            if current in keep_emails:
                seen_emails.add(current)
                out.append(line)
            else:
                changes.append(f"removed EMAIL {current!r}")
            continue

        if prop_name == "END":
            for phone in sorted(keep_phones - seen_phones) if keep_phones else []:
                out.append(f"TEL:{_vcard_escape(phone)}")
                changes.append(f"added TEL {phone!r}")
            for address in sorted(keep_emails - seen_emails) if keep_emails else []:
                out.append(f"EMAIL:{_vcard_escape(address)}")
                changes.append(f"added EMAIL {address!r}")
            if name is not None and not fn_set:
                out.append(f"FN:{_vcard_escape(name)}")
                changes.append(f"added FN {name!r}")
            out.append(line)
            continue

        out.append(line)

    return terminator.join(out), changes


def _card_uid(raw: str) -> str:
    for line in _split_vcard(raw)[0]:
        if _property_name(line) == "UID":
            return line.partition(":")[2].strip()
    return ""


def _summarise(raw: str) -> Dict[str, Any]:
    card = vobject.readOne(raw)
    return {
        "id": _card_uid(raw),
        "name": _contact_name(card),
        "organization": _organization(card),
        "emails": _vcard_values(card, "email"),
        "phones": _vcard_values(card, "tel"),
    }


def _summarise_safely(raw: str) -> Dict[str, Any]:
    """Summarise a card that may not fully parse.

    A single card with, say, an undecodable inline photo must not abort a whole
    repair run, so fall back to what can be read straight off the lines.
    """
    try:
        return _summarise(raw)
    except Exception as e:
        log.debug("Falling back to line-level summary: %s", e)
        name = ""
        for line in _split_vcard(raw)[0]:
            if _property_name(line) == "FN" and line.partition(":")[2].strip():
                name = line.partition(":")[2].strip()
                break
        return {"id": _card_uid(raw), "name": name, "organization": "",
                "emails": [], "phones": []}


def _backup(cards: List[Tuple[Card, str]]) -> str:
    """Save the pre-change version of every card about to be written."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(BACKUP_DIR, f"contacts-{stamp}.vcf")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        for card, _ in cards:
            handle.write(f"X-BACKUP-SOURCE-URL:{card.url}\r\n")
            handle.write(card.raw if card.raw.endswith("\n") else card.raw + "\r\n")
    return path


# --------------------------------------------------------------------------
# Contact tools
# --------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
def search_contacts(query: str = "", limit: int = 10) -> List[Dict[str, Any]]:
    """
    Search for contacts in iCloud using CardDAV, across all address books.

    The server-side filter matches on FN only. Many iCloud contacts have an
    empty FN and store the name in the structured N property, so those cards
    are NOT findable by name -- use an empty query and filter client-side to
    reach them. Returned names fall back to N, then organization.

    Each result carries an 'id' (the vCard UID) for use with update_contact.

    Args:
        query: Name to search for. Empty string returns all contacts.
        limit: Maximum number of contacts to return.
    """
    if limit < 1:
        raise ToolError("limit must be at least 1.")

    address, password = _require_credentials()
    auth = HTTPBasicAuth(address, password)

    try:
        results: List[Dict[str, Any]] = []
        for card in _fetch_cards(auth, query):
            try:
                results.append(_summarise(card.raw))
            except Exception as e:
                log.warning("Skipping unparseable vCard at %s: %s", card.url, e)
                continue
            if len(results) >= limit:
                break
        return results
    except ToolError:
        raise
    except requests.RequestException as e:
        raise ToolError(f"CardDAV request failed: {e}") from e
    except ET.ParseError as e:
        raise ToolError(f"CardDAV returned malformed XML: {e}") from e
    except Exception as e:
        # Surface as a protocol-level error rather than an error-shaped result
        # the caller would mistake for data.
        raise ToolError(f"Failed to search contacts: {e}") from e


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
def update_contact(
    contact_id: str,
    name: Optional[str] = None,
    phones: Optional[List[str]] = None,
    emails: Optional[List[str]] = None,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """
    Update one contact in iCloud. Modifies live data -- preview first.

    Only the fields you pass are touched; anything omitted is left alone, and
    every other property in the card (photo, notes, custom fields) is preserved
    byte for byte. Passing phones or emails REPLACES that whole list; existing
    entries you keep retain their type labels (CELL, WORK, ...).

    Args:
        contact_id: The 'id' (vCard UID) from search_contacts.
        name: New display name (sets FN).
        phones: Full replacement list of phone numbers.
        emails: Full replacement list of email addresses.
        dry_run: When True (the default) report the change without writing.
    """
    if name is None and phones is None and emails is None:
        raise ToolError("Nothing to update: pass at least one of name, phones or emails.")
    if not contact_id.strip():
        raise ToolError("contact_id is required; use the 'id' field from search_contacts.")

    address, password = _require_credentials()
    auth = HTTPBasicAuth(address, password)

    match = next((c for c in _fetch_cards(auth) if _card_uid(c.raw) == contact_id.strip()), None)
    if match is None:
        raise ToolError(f"No contact found with id {contact_id!r}.")

    updated, changes = _apply_contact_updates(match.raw, name, phones, emails)
    if not changes:
        return {"contact_id": contact_id, "changed": False, "changes": [], "dry_run": dry_run}

    if not dry_run:
        _backup([(match, updated)])
        _put_card(auth, match, updated)

    return {
        "contact_id": contact_id,
        "changed": not dry_run,
        "dry_run": dry_run,
        "changes": changes,
        "before": _summarise(match.raw),
        "after": _summarise(updated),
    }


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
def repair_contacts(
    dry_run: bool = True,
    limit: int = 500,
    contact_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Find and fix the two systematic defects in iCloud contact data.

    1. A metadata property concatenated onto a phone number, e.g.
       '+12025550143X-SHARED-PHOTO-DISPLAY-PREF:ALWAYS_ASK'. The suffix is
       split back onto its own line rather than thrown away.
    2. An empty FN where the name exists in the structured N property, which
       is what makes a contact show up as nameless and unsearchable.

    Every other line of each card is preserved verbatim. When writing, the
    original cards are saved to backups/ first, and each write is guarded by an
    If-Match ETag so a concurrent edit from another device is never clobbered.

    Args:
        dry_run: When True (the default) report what would change, writing nothing.
        limit: Maximum number of contacts to repair in one run.
        contact_ids: Repair only these contacts (the 'id' from search_contacts).
            Use a single id to verify one card before a bulk run, or a list to
            repair a chosen subset in one pass.
    """
    if limit < 1:
        raise ToolError("limit must be at least 1.")
    wanted = {i.strip() for i in contact_ids if i and i.strip()} if contact_ids else None
    if contact_ids is not None and not wanted:
        raise ToolError("contact_ids was given but contained no usable ids.")

    address, password = _require_credentials()
    auth = HTTPBasicAuth(address, password)

    planned: List[Tuple[Card, str]] = []
    report: List[Dict[str, Any]] = []
    scanned = 0

    for card in _fetch_cards(auth):
        if wanted is not None and _card_uid(card.raw) not in wanted:
            continue
        scanned += 1
        try:
            updated, changes = _plan_card_repairs(card.raw)
        except Exception as e:
            log.warning("Could not plan repairs for %s: %s", card.url, e)
            continue
        if not changes:
            continue
        planned.append((card, updated))
        report.append({
            "id": _card_uid(card.raw),
            "name": _summarise_safely(updated).get("name", ""),
            "changes": changes,
        })
        if len(planned) >= limit:
            break

    backup_path = None
    written, failures = 0, []
    if not dry_run and planned:
        backup_path = _backup(planned)
        for card, updated in planned:
            try:
                _put_card(auth, card, updated)
                written += 1
            except ToolError as e:
                failures.append({"id": _card_uid(card.raw), "error": str(e)})
                log.warning("Repair failed for %s: %s", card.url, e)

    return {
        "dry_run": dry_run,
        "scanned": scanned,
        "needing_repair": len(planned),
        "written": written,
        "backup": backup_path,
        "failures": failures,
        "contacts": report,
    }


def _build_vcard(name: str, phones: List[str], emails: List[str], organization: str) -> Tuple[str, str]:
    """Assemble a new vCard 3.0 in the shape Apple stores. Returns (uid, text)."""
    uid = str(uuid.uuid4()).upper()
    parts = name.split()
    structured = f"{_vcard_escape(parts[-1])};{_vcard_escape(' '.join(parts[:-1]))};;;" if parts else ";;;;"
    lines = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        "PRODID:-//Honeycrisp//EN",
        f"N:{structured}",
        f"FN:{_vcard_escape(name or organization)}",
    ]
    if organization:
        lines.append(f"ORG:{_vcard_escape(organization)};")
    lines += [f"EMAIL;TYPE=INTERNET:{_vcard_escape(e)}" for e in emails]
    lines += [f"TEL;TYPE=CELL:{_vcard_escape(p)}" for p in phones]
    lines += [
        f"UID:{uid}",
        f"REV:{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "END:VCARD",
    ]
    return uid, "\r\n".join(lines) + "\r\n"


def _create_card(auth: HTTPBasicAuth, url: str, text: str) -> None:
    """PUT a brand-new card; If-None-Match guarantees nothing is overwritten."""
    response = requests.request(
        "PUT", url, auth=auth,
        headers={"Content-Type": "text/vcard; charset=utf-8", "If-None-Match": "*"},
        data=text.encode("utf-8"), timeout=HTTP_TIMEOUT,
    )
    if response.status_code == 412:
        raise ToolError(f"A card already exists at {url}; nothing was written.")
    if response.status_code not in (200, 201, 204):
        raise ToolError(f"Creating {url} failed ({response.status_code}): {response.text[:200]}")


def _delete_card(auth: HTTPBasicAuth, card: Card) -> None:
    headers = {"If-Match": card.etag} if card.etag else {}
    response = requests.request("DELETE", card.url, auth=auth, headers=headers, timeout=HTTP_TIMEOUT)
    if response.status_code == 412:
        raise ToolError(
            f"{card.url} changed on the server since it was read; nothing was deleted. "
            "Re-run to pick up the current version."
        )
    if response.status_code not in (200, 202, 204):
        raise ToolError(f"Deleting {card.url} failed ({response.status_code}): {response.text[:200]}")


def _pick_addressbook(urls: List[str], name: Optional[str]) -> str:
    """Choose the target address book by its last path segment, never by guessing."""
    if not urls:
        raise ToolError("No address books found on this account.")
    labels = {u.rstrip("/").rsplit("/", 1)[-1]: u for u in urls}
    if name:
        if name not in labels:
            raise ToolError(f"No address book named {name!r}. Available: {', '.join(map(repr, labels))}.")
        return labels[name]
    if len(urls) == 1:
        return urls[0]
    raise ToolError(
        f"This account has several address books; pass addressbook=... "
        f"Available: {', '.join(map(repr, labels))}."
    )


def _clean_list(values: Optional[List[str]]) -> List[str]:
    return [v.strip() for v in values or [] if v and v.strip()]


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
def create_contact(
    name: Optional[str] = None,
    phones: Optional[List[str]] = None,
    emails: Optional[List[str]] = None,
    organization: Optional[str] = None,
    addressbook: Optional[str] = None,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """
    Create a contact in iCloud. Preview first.

    Args:
        name: Full display name, e.g. 'Dana Whitfield'. The last word becomes
            the family name. Required unless organization is given.
        phones: Phone numbers.
        emails: Email addresses.
        organization: Company or organisation.
        addressbook: Address book to create in; required only when the
            account has more than one.
        dry_run: When True (the default) return the card without creating it.
    """
    name = (name or "").strip()
    organization = (organization or "").strip()
    if not name and not organization:
        raise ToolError("Pass a name or an organization.")
    phones, emails = _clean_list(phones), _clean_list(emails)
    for e in emails:
        if "@" not in e or any(c.isspace() for c in e):
            raise ToolError(f"Invalid email address {e!r}.")

    address, password = _require_credentials()
    auth = HTTPBasicAuth(address, password)
    book = _pick_addressbook(_addressbook_urls(auth), addressbook)
    uid, text = _build_vcard(name, phones, emails, organization)
    url = urljoin(book, f"{uid}.vcf")

    if not dry_run:
        _create_card(auth, url, text)

    return {
        "dry_run": dry_run,
        "created": not dry_run,
        "addressbook": book.rstrip("/").rsplit("/", 1)[-1],
        "contact": _summarise(text),
    }


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
def delete_contact(contact_id: str, dry_run: bool = True) -> Dict[str, Any]:
    """
    Delete one contact from iCloud. Modifies live data -- preview first.

    The card is saved to backups/ before deletion, and the DELETE carries an
    If-Match ETag so a card edited on another device since it was read is
    left alone.

    Args:
        contact_id: The 'id' (vCard UID) from search_contacts.
        dry_run: When True (the default) report the contact without deleting it.
    """
    uid = contact_id.strip()
    if not uid:
        raise ToolError("contact_id is required; use the 'id' field from search_contacts.")

    address, password = _require_credentials()
    auth = HTTPBasicAuth(address, password)
    match = next((c for c in _fetch_cards(auth) if _card_uid(c.raw) == uid), None)
    if match is None:
        raise ToolError(f"No contact found with id {uid!r}.")

    backup_path = None
    if not dry_run:
        backup_path = _backup([(match, match.raw)])
        _delete_card(auth, match)

    return {
        "dry_run": dry_run,
        "deleted": not dry_run,
        "backup": backup_path,
        "contact": _summarise_safely(match.raw),
    }


def main() -> None:
    """Console entry point: serve over stdin/stdout."""
    mcp.run()


if __name__ == "__main__":
    main()
