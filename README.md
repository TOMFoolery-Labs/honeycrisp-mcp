# Honeycrisp

A Model Context Protocol (MCP) server for iCloud — Mail, Calendar, Contacts and Notes —
using an Apple app-specific password.

It talks to the open protocols Apple supports (IMAP for mail, CalDAV for calendar, CardDAV
for contacts), so no private API and no Apple ID password are involved.

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
| `search_emails(query, folder, limit)` | Search a mail folder with an IMAP query. Returns sender, subject, date and a decoded body preview, newest first. |
| `get_calendar_events(start_date, end_date, limit)` | Events across all calendars in a date range, recurrences expanded, sorted by start time. |
| `search_contacts(query, limit)` | Search all address books. Returns id, name, organization, and every email and phone per contact. |
| `search_notes(query, limit)` | Legacy IMAP notes only — see the caveat below. |

### Write

| Tool | Description |
| --- | --- |
| `update_contact(contact_id, name, phones, emails, dry_run)` | Edit one contact. |
| `repair_contacts(dry_run, limit, contact_ids)` | Fix systematic contact data defects in bulk or for a chosen subset. |

Both write tools default to `dry_run=True`: they report exactly what would change and send
nothing. Pass `dry_run=False` to apply.

- **Surgical edits.** Only changed lines are rewritten; every other line — photos, custom
  properties, folded continuations — is preserved byte for byte.
- **Backups.** Originals are saved to `backups/` as a timestamped `.vcf` before any write.
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
  MIME-decoded before truncation to 500 characters.
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
The fixtures deliberately mirror bytes observed on a real account (CRLF line endings, empty
`FN` with a populated `N`, an undecodable inline photo), because earlier fixtures that
didn't hid real bugs. See `CLAUDE.md` for the architecture and the constraints behind it.
