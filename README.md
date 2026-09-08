# Honeycrisp

A Model Context Protocol (MCP) server for iCloud — Mail, Calendar, Contacts and Notes —
using an Apple app-specific password.

It talks to the open protocols Apple supports (IMAP and SMTP for mail, CalDAV for calendar,
CardDAV for contacts), so no private API and no Apple ID password are involved.

## Setup

1. Generate an app-specific password at [appleid.apple.com](https://appleid.apple.com).
2. Create a `.env` in the project root:
   ```env
   ICLOUD_EMAIL=your.email@icloud.com
   ICLOUD_APP_PASSWORD=your-app-specific-password
   ```
   `.env` is gitignored. Never commit it.
3. Install and run:
   ```bash
   uv venv && uv pip install -r requirements.txt   # or: pip install -r requirements.txt
   .venv/bin/python src/server.py
   ```

`.env` is resolved relative to the source file, so the server works no matter which
directory a client launches it from.

### Registering with an MCP client

```json
{
  "mcpServers": {
    "honeycrisp": {
      "command": "/absolute/path/to/honeycrisp-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/honeycrisp-mcp/src/server.py"]
    }
  }
}
```

## Tools

### Read

| Tool | Description |
| --- | --- |
| `list_folders()` | Every mail folder with its role (inbox, sent, trash, ...) and message and unseen counts. |
| `search_emails(query, folder, limit)` | Search a mail folder with an IMAP query. Returns sender, subject, date and a decoded body preview, newest first. |
| `get_email(message_id, folder, max_chars)` | One message in full: headers, decoded body (HTML converted to text), attachment names and sizes. |
| `get_calendar_events(start_date, end_date, limit)` | Events across all calendars in a date range, recurrences expanded, sorted by start time. Each carries an `id` for `delete_event`. |
| `search_contacts(query, limit)` | Search all address books. Returns id, name, organization, and every email and phone per contact. |
| `search_notes(query, limit)` | Legacy IMAP notes only — see the caveat below. |

### Write

| Tool | Description |
| --- | --- |
| `move_emails(message_ids, to_folder, folder, dry_run)` | Move messages between folders: archive, file, or restore from Trash. |
| `mark_emails(message_ids, folder, read, flagged, dry_run)` | Mark messages read/unread or flagged/unflagged. |
| `delete_emails(message_ids, folder, permanent, dry_run)` | Move messages to Trash, or expunge them outright with `permanent=True`. |
| `send_email(body, to, subject, cc, bcc, reply_to_id, reply_folder, dry_run)` | Send a plain-text email over SMTP and file a copy in Sent. Pass `reply_to_id` to reply in-thread. |
| `create_event(summary, start, end, calendar, all_day, location, description, dry_run)` | Create a timed or all-day event. |
| `delete_event(event_id, calendar, dry_run)` | Delete an event (every occurrence of a recurring one), backing up its iCalendar text first. |
| `create_contact(name, phones, emails, organization, addressbook, dry_run)` | Create a contact. |
| `update_contact(contact_id, name, phones, emails, dry_run)` | Edit one contact. |
| `delete_contact(contact_id, dry_run)` | Delete a contact, backing up the card first. |
| `repair_contacts(dry_run, limit, contact_ids)` | Fix systematic contact data defects in bulk or for a chosen subset. |

Every write tool defaults to `dry_run=True`: it reports exactly what would change and sends
nothing. Pass `dry_run=False` to apply.

Mail:

- **Trash first.** `delete_emails` moves messages to the account's Trash folder (`Deleted
  Messages` on iCloud) with a single IMAP `MOVE`, so they stay recoverable. Deleting from
  Trash itself requires `permanent=True`.
- **Permanent deletes are backed up.** With `permanent=True` each message is saved in full to
  `backups/mail-<timestamp>/` as an `.eml` file before it is expunged. Only the requested UIDs
  are expunged (`UID EXPUNGE`), never every `\Deleted` message in the folder.
- **All-or-nothing ids.** Ids are per-folder. If any id is not found, nothing is deleted,
  moved or marked. `move_emails` also checks that the destination exists before opening
  the source folder.
- **Replies thread properly.** With `reply_to_id`, `send_email` reads the original's headers
  and sets `In-Reply-To` and `References`, so the reply lands in the same conversation in
  every mail client. `to` defaults to the original's `Reply-To` or `From`, and `subject` to
  `Re: <original>` (no stacked prefixes). The original text is not quoted automatically.
- **Sent mail is filed.** iCloud's SMTP server does not save outgoing mail, so `send_email`
  appends a copy to the Sent folder after delivery, the same way Mail.app does. If that step
  fails the message has still gone out; the result says `saved_to_sent: false`.

Calendar:

- **Explicit targets.** `create_event` needs `calendar=` when the account has more than one,
  rather than guessing. Naive times are treated as UTC, so pass an offset for local times.
  All-day events take dates, with `end` being the day after the last day.
- **Backups.** `delete_event` saves the event's iCalendar text to `backups/` before removing
  it. Deleting a recurring event removes every occurrence.

Contacts:

- **Creation is guarded.** `create_contact` PUTs with `If-None-Match: *`, so it can never
  overwrite an existing card, and needs `addressbook=` only when there are several.
- **Surgical edits.** Only changed lines are rewritten; every other line — photos, custom
  properties, folded continuations — is preserved byte for byte.
- **Backups.** Originals are saved to `backups/` as a timestamped `.vcf` before any write,
  including deletes. `delete_contact` sends `If-Match` too.
- **Optimistic concurrency.** Every `PUT` carries an `If-Match` ETag, so an edit made on
  another device since the read is reported rather than clobbered.
- **Idempotent.** Re-running a repair on an already-repaired card is a no-op.
- **Targeted runs.** `repair_contacts(contact_ids=[...])` repairs a chosen subset in one
  pass, so a single card can be verified on-device before a bulk run.

## Caveats worth knowing

**Contacts with no `FN`.** iCloud stores many contacts with an empty `FN`, keeping the name
only in the structured `N` property. `search_contacts` falls back `FN` → `N` → `ORG`, so
they still come back named. But the CardDAV filter runs **server-side against `FN` only**,
so such a contact cannot be found by name no matter what you pass — call with an empty
`query` and filter client-side. `repair_contacts` fixes this at the source by populating
`FN`.

**Corrupt stored values.** Some cards arrive with a metadata property concatenated onto a
value, e.g. `+12025550143X-SHARED-PHOTO-DISPLAY-PREF:ALWAYS_ASK`. The line arrives from
Apple already joined, so this is bad data at rest rather than a parsing artifact. Reads
strip the suffix and log a warning; `repair_contacts` splits it back onto its own line.

**Notes.** Notes created in the modern Notes app sync over CloudKit, not IMAP, and are not
reachable here. `search_notes` sees only notes stored on the IMAP account, which for most
accounts is empty. An empty result does not mean the user has no notes.

## Behaviour

- **Errors are raised, not returned.** Tools raise `ToolError` so the client sees a real
  protocol error, never an error-shaped value a model could mistake for data. An invalid
  IMAP query fails loudly rather than silently falling back to returning all mail.
- **Calendar queries are bounded.** Without `end_date` the range defaults to 90 days after
  `start_date` (`DEFAULT_CALENDAR_WINDOW`). A closed interval is also what makes recurrence
  expansion possible.
- **Message previews are bounded.** Only the first 16 KB of each message is fetched
  (`PREVIEW_FETCH_BYTES`), using `BODY.PEEK` so mail is never marked read, and the text is
  MIME-decoded before truncation to 500 characters. `get_email` fetches the whole message,
  still with `BODY.PEEK`, and caps the returned body at `max_chars` (20,000 by default).
  Attachments are listed but never downloaded.
- **The IMAP connection is cached** across tool calls and re-established when stale, because
  iCloud caps concurrent connections and throttles repeated logins. Access is serialised
  with a lock, as `IMAPClient` is not thread-safe.
- **Diagnostics go to stderr.** This is a stdio server, so stdout carries the JSON-RPC
  stream exclusively. Set `HONEYCRISP_LOG_LEVEL=DEBUG` for more detail.

## Development

```bash
uv pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q
```

Tests run fully offline against fakes in `tests/fakes.py` — no credentials, no network.
`scripts/smoke.py` is the complement: a read-only walk of every tool against the live
account (needs `.env`) that also prints the server facts the mail tools rely on. On
iCloud it shows no `MOVE` capability, so moves and deletes run the copy + flag +
`UID EXPUNGE` fallback, and only Sent and Trash carry special-use flags.
`scripts/roundtrip.py` is the live write check: it sends one tagged message to the account's
own address, runs every mail write tool on it (mark, move, reply, delete, permanent delete)
and removes every copy. It sends real mail, so run it deliberately. If a slow delivery trips
it, `--resume <tag>` finishes the cleanup.
The fixtures deliberately mirror bytes observed on a real account (CRLF line endings, empty
`FN` with a populated `N`, an undecodable inline photo), because earlier fixtures that
didn't hid real bugs. See `CLAUDE.md` for the architecture and the constraints behind it.
