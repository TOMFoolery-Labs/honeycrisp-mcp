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
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Tuple
from urllib.parse import urljoin
from xml.sax.saxutils import escape as xml_escape

import caldav
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

# Default window for calendar queries when no end date is given. CalDAV
# recurrence expansion requires a closed interval, and an unbounded query
# against iCloud pulls down every event in every calendar.
DEFAULT_CALENDAR_WINDOW = timedelta(days=90)

# Pre-change copies of any card the server writes, and full copies of any
# message it deletes permanently, are saved here.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP_DIR = os.path.join(PROJECT_ROOT, "backups")

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


def _extract_preview(raw: bytes) -> str:
    """Extract readable text from a (possibly truncated) raw MIME message.

    Bodies are commonly base64 or quoted-printable encoded, so the raw bytes
    are not human-readable on their own; they must be decoded per-part.
    """
    if not raw:
        return ""
    try:
        message = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception:
        return _truncate(raw.decode("utf-8", errors="replace"))

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
        content = _TAG_RE.sub(" ", content)
    return _truncate(content)


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
            elif client.has_capability("MOVE"):
                client.move(uids, destination)
            else:
                client.copy(uids, destination)
                client.delete_messages(uids)
                _expunge(client, uids)

    return {
        "dry_run": dry_run,
        "deleted": not dry_run,
        "action": action,
        "folder": folder,
        "destination": destination,
        "backup": backup_path,
        "messages": messages,
    }


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


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
def send_email(
    to: List[str],
    subject: str,
    body: str,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """
    Send a plain-text email from the iCloud account. Preview first.

    The message goes out over SMTP and a copy is then filed in the account's
    Sent folder so it appears in Mail on every device. Sending cannot be
    undone, so the default is a dry run that renders the message without
    sending it.

    Args:
        to: Recipient addresses, e.g. ['ann@example.com', 'Bob <bob@example.com>'].
        subject: Subject line.
        body: Plain-text body.
        cc: Optional Cc recipients.
        bcc: Optional Bcc recipients (not included in the headers).
        dry_run: When True (the default) return the rendered message without sending.
    """
    address, _ = _require_credentials()
    to_list = _parse_recipients(to, "to")
    cc_list = _parse_recipients(cc, "cc")
    bcc_list = _parse_recipients(bcc, "bcc")
    if not to_list:
        raise ToolError("At least one 'to' recipient is required.")
    if not subject.strip():
        raise ToolError("subject is required.")
    if not body.strip():
        raise ToolError("body is required.")

    message = EmailMessage()
    message["From"] = address
    message["To"] = ", ".join(to_list)
    if cc_list:
        message["Cc"] = ", ".join(cc_list)
    message["Subject"] = subject.strip()
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=address.rsplit("@", 1)[-1])
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
    separately. Results are sorted by start time.

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
            except Exception as e:
                log.warning("Skipping unparseable event in %r: %s", calendar.name, e)
                continue

            entry = {
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


def _carddav_propfind(url: str, depth: int, body: str, auth: HTTPBasicAuth) -> ET.Element:
    response = requests.request(
        "PROPFIND", url,
        auth=auth,
        headers={"Depth": str(depth), "Content-Type": "application/xml; charset=utf-8"},
        data=body.encode("utf-8"),
        timeout=HTTP_TIMEOUT,
    )
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


if __name__ == "__main__":
    # Run the server using stdin/stdout streams
    mcp.run()
