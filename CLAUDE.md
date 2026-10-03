# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

The venv is uv-managed. Recreate it with `uv venv && uv pip install -r requirements-dev.txt`
(the venv bakes in absolute paths, so rebuild it rather than move it if the project relocates).

```bash
.venv/bin/python src/server.py                      # run the MCP server (stdio)
.venv/bin/python -m pytest tests/ -q                # full suite
.venv/bin/python -m pytest tests/test_writes.py -q  # one file
.venv/bin/python -m pytest tests/test_mail.py::test_result_shape -q   # one test
HONEYCRISP_LOG_LEVEL=DEBUG .venv/bin/python src/server.py             # verbose stderr
uvx --from git+https://github.com/TOMFoolery-Labs/honeycrisp-mcp honeycrisp  # packaged run
```

Tests run fully offline against fakes in `tests/fakes.py`. No credentials or network needed.
Live checks against the real account require `.env` (`ICLOUD_EMAIL`, `ICLOUD_APP_PASSWORD`):

```bash
.venv/bin/python scripts/smoke.py                    # read-only walk of every tool + dry runs
.venv/bin/python scripts/roundtrip.py                # mail writes, on a self-sent message
.venv/bin/python scripts/roundtrip_pim.py            # calendar + contact writes, on tagged items
```

CI (`.github/workflows/ci.yml`) runs the offline suite on Python 3.11–3.14 and then builds
the wheel, installs it and completes an MCP handshake, so packaging breaks fail there too.
It never has credentials; anything needing the live account stays in `scripts/`.

The round trips send real mail and write real data (on items they create and remove). Run
them deliberately, and not back to back: CardDAV throttles the account after bursts (see
below). All tools passed these live on 2026-09-08.

## Architecture

One module, `src/server.py`, exposing twenty-one FastMCP tools over four protocols: IMAP for mail
and legacy notes, SMTP (stdlib `smtplib`, STARTTLS on 587) for sending, CalDAV (via the
`caldav` lib) for calendar, and raw CardDAV over `requests` for contacts. `tests/fakes.py`
provides stand-ins for all four; tests swap `_connect_imap` and `_connect_smtp` for fakes.

**CardDAV is hand-rolled** and has the most structure. `_addressbooks` (cached) →
`_fetch_cards` → `_put_card` / `_create_card` / `_delete_card` is the shared transport;
every contact tool goes through it. PROPFIND and REPORT both go through `_carddav_request`,
which raises on anything but 2xx: a failed address book must never contribute an empty
result, because that reads as "no contacts" or "no contact found" to the model.
`update_contact` and `delete_contact` locate their card with `_find_card`: a direct GET of
`<addressbook>/<UID>.vcf` per book (the URL `create_contact` writes to), falling back to
the full REPORT only when no book serves the card there. The direct path was added
2026-10-03 and has not yet been run live; `scripts/roundtrip_pim.py --contacts` exercises
it, and the fallback keeps behaviour correct if iCloud ever lays a card out differently. `_fetch_cards` returns `Card(url, etag, raw)` — the
ETag is what makes safe writes possible. Read and write paths share this layer, so changes
to discovery affect both. Tests replace `requests.request` with `tests/fakes.py:FakeHTTP`.

**CalDAV goes through the `caldav` lib** but with two of its paths avoided: `_event_by_uid`
fetches `<calendar>/<UID>.ics` directly, and `create_event` checks for an existing object
the same way, because iCloud 412s the library's by-UID REPORT. `list_calendars` issues a
raw PROPFIND per calendar. Tests replace `get_caldav_client` with fakes.

**Mail sending** is `_deliver`: SMTP send, then an IMAP APPEND to Sent. `send_email` and
`forward_email` both use it. `smtplib` raises only when every recipient is refused; a
partial refusal comes back as a dict, which `_deliver` reports as `refused` next to
`sent: True`.

**IMAP connections are cached** module-globally (`_imap_client`) behind `_imap_lock`, because
iCloud caps concurrent connections and throttles repeated logins. `imap_session()` probes
liveness with `noop()` and reconnects on failure. It deliberately keeps the connection on
`ToolError` (our own validation, stream still fine) and drops it on anything else (may have
desynchronised the protocol).

## Constraints that will bite you

**`SERVER_INSTRUCTIONS` is what the model reads about this server.** It goes out in the
initialize handshake and clients feed it to the model as usage guidance, so when a tool is
added, renamed or changes its contract, update that string too. Keep `SERVER_VERSION` in
step with `pyproject.toml`.

**stdout is the JSON-RPC stream.** This is a stdio server. Never `print()`; all diagnostics
go through `log` to stderr. `tests/test_mail.py` asserts stdout stays byte-empty on import,
both with and without credentials.

**Search criteria are a list, never a formatted string.** `_compile_search` returns a list
so imapclient does the quoting and IMAP date formatting; the only string path is the raw
`query` escape hatch, which cannot be mixed with the filters because a raw string inside a
list gets quoted as one literal. Non-ASCII values set `charset="UTF-8"`, which iCloud
accepts (verified 2026-09-08).

**Contact summaries must not raise after a write.** `_summarise` goes through vobject,
which rejects some real cards (undecodable inline photo). `search_contacts` and
`update_contact` use `_summarise_safely`, which falls back to `_summarise_from_lines`, and
`update_contact` builds both summaries before the PUT so nothing can fail afterwards.

**Tools raise `ToolError`; they never return error-shaped data.** Returning `[{"error": ...}]`
gives a model something it will mistake for a result. Relatedly, never silently fall back to
a broader query on failure — an invalid IMAP search must raise, not quietly return all mail.

**CardDAV hrefs need `urljoin`, never concatenation.** Apple redirects to partition hosts
(`pNN-contacts.icloud.com`) and returns absolute hrefs; string concatenation produces
`https://contacts.icloud.comhttps://p132-...`.

**vCard edits are surgical.** `_plan_card_repairs` and `_apply_contact_updates` rewrite only
the lines that change and preserve every other line verbatim, so photos, custom properties
and folded continuations survive byte-identical. Do not switch to parse-and-re-serialise;
it would drop data never modelled. `_split_vcard` detects the terminator from the input so
cards round-trip with whatever line endings they arrived with.

**Repair matching uses an allowlist, not a pattern.** `GLUED_METADATA_PROPERTIES` lists the
property names seen concatenated into other values. `REV`, `URL`, `NOTE`, `PHOTO` and
`X-SOCIALPROFILE` all legitimately contain colons, so a loose `WORD:` regex corrupts real
data. Tests pin this. `TEL`/`EMAIL` get the broader `_EMBEDDED_PROPERTY_RE` only because
they never legitimately contain a colon.

**Write tools default to `dry_run=True`** and destructive ones are annotated
`destructiveHint`. Contact writes and deletes back up originals to `backups/` and send
`If-Match` so a concurrent edit from another device is reported rather than clobbered;
`create_contact` sends `If-None-Match: *` instead. `update_event` and `delete_event` back
up the `.ics`, and `update_event`'s save carries the ETag recorded on load.
`_pick_calendar` and `_pick_addressbook` refuse to guess when there are several targets.
`contact_ids=[]` and `message_ids=[]` raise rather than matching everything. Get explicit
user confirmation before any `dry_run=False` run against the live account.

**Mail write tools fail whole, not partial.** Message ids are IMAP UIDs and are per-folder;
`_describe_messages` raises if any requested id is missing, before anything is acted on.
`delete_emails` and `move_emails` share `_move_uids`: `MOVE` where the server has it, else
copy + flag + `UID EXPUNGE`; iCloud lacks `MOVE`, so the fallback is the live path. The
default delete action is that move to Trash (found via `find_special_folder`, which is
`Deleted Messages` on iCloud). `permanent=True` writes each message to
`backups/mail-<stamp>/*.eml` first, then flags `\Deleted` and issues `UID EXPUNGE` for
just those UIDs; a plain `EXPUNGE` would also purge messages other clients flagged.

**iCloud SMTP does not file sent mail.** `send_email` APPENDs a copy to the Sent folder over
IMAP after delivery. That step runs after the message has left, so it logs and reports
`saved_to_sent: False` rather than raising. Bcc recipients go only in the SMTP envelope.
Replies (`reply_to_id`) fetch only the threading headers of the original via
`HEADER.FIELDS`, read-only and with PEEK, so replying never marks the original as read.

**`save_attachments` is the only tool that writes outside `backups/`, and only under
`DOWNLOAD_DIR`** (`~/Downloads/Honeycrisp`, or `HONEYCRISP_DOWNLOAD_DIR`). `directory` is
resolved by `_resolve_download_dir` (relative to that folder, `realpath` for symlinks, then
a `commonpath` check) and anything outside raises. Mail is hostile input: without this a
model talked into "saving" an attachment over `~/.ssh/authorized_keys` would have a
write-anywhere primitive. Filenames go through `_safe_filename` (basename only, separators
and control characters stripped), and `_unique_path` suffixes rather than overwrites by
default.

**Body text goes through `_message_text`.** Both `search_emails` previews and `get_email`
use it: text/plain preferred, otherwise HTML converted with `_html_to_text`, which drops
`<style>`/`<script>`/`<head>`, turns block boundaries into line breaks and unescapes
entities. Previews then collapse whitespace via `_truncate`; full bodies keep line structure.

**`load_dotenv` resolves `.env` relative to the source file**, not the cwd — MCP clients
launch servers from arbitrary directories. `pyproject.toml` installs `src/server.py` as the
top-level module `server` with a `honeycrisp` console script (`main()`); installed that way
there is no `.env` or `backups/` beside the module, so credentials come from the client's
`env` block and `_default_backup_dir` falls back to `~/.honeycrisp/backups`. Never write
user data relative to `__file__` without going through that helper.

## Test fixtures must mirror the real wire

Bugs hid here twice. `tests/fakes.py:report_xml` escapes CR as `&#13;` because Apple does;
without it an XML parser normalises CRLF to LF and the fixtures stop matching reality.
`vcard()` emits CRLF and supports an empty `FN` with the name in `N`. `test_writes.py`
fixtures include a card whose inline photo is deliberately undecodable, because a card that
`vobject` cannot fully parse must still be repairable — that is why `_name_from_lines` reads
`N`/`ORG` straight off the lines instead of going through `vobject`.

## iCloud data facts

- Many contacts have an **empty `FN`** with the real name only in the structured `N`.
  `_contact_name` falls back `FN` → `N` → `ORG`.
- The CardDAV server-side filter matches **`FN` only**, so empty-`FN` contacts cannot be
  found by name at all — pass an empty query and filter client-side.
- Some stored values arrive with a metadata property already concatenated on
  (`+12025550143X-SHARED-PHOTO-DISPLAY-PREF:ALWAYS_ASK`). The line arrives joined from
  Apple, so this is corrupt data at rest, not a parsing artifact.
- **iCloud IMAP has no `MOVE` and no `SPECIAL-USE`** (verified 2026-09-08 with
  `scripts/smoke.py`). `_move_uids` therefore always takes the copy + `\Deleted` +
  `UID EXPUNGE` path live, and `list_folders` recognises Drafts/Junk/Archive by name.
  `UIDPLUS` is present. Trash is `Deleted Messages`, Sent is `Sent Messages`. Every mail
  write tool was verified live the same day with `scripts/roundtrip.py`; self-addressed
  delivery took over 90 s on one run, so waits in live scripts must be generous.
- **iCloud CalDAV rejects the by-UID `calendar-query` REPORT with 412** but serves every
  event at `<calendar>/<UID>.ics`. `_event_by_uid` does the direct GET first and keeps the
  REPORT only as a fallback. Verified 2026-09-08, when calendar and contact create, update
  and delete all passed live via `scripts/roundtrip_pim.py`.
- **Reminders lists are CalDAV calendars** whose `supported-calendar-component-set` is
  `VTODO` only. On this account they are the ones with ⚠️ in the name. `list_calendars`
  reads that property (plus privileges, resourcetype and Apple's `calendar-color`) with one
  raw PROPFIND per calendar, because the `caldav` lib has no element for the privilege set.
- **Upgraded Reminders are not reachable, so do not build reminders tools.** Probed
  2026-09-08: both lists hold exactly two VTODOs created 2020-08-22, "The creator of this
  list has upgraded these reminders" (DESCRIPTION links HT210220) and "Where are my
  reminders?". Real reminders sync over CloudKit since the 2019 upgrade; a VTODO written
  here never appears in the app. Also, `calendar.todos()` (which filters on completion) gets
  a 500 from iCloud, while `search(todo=True, include_completed=True)` works.
- **`update_event` writes a new time in the event's own zone.** `_set_time` converts the
  value into the zone of the time it replaces (`DTSTART;TZID=America/Chicago:...`) and
  only falls back to UTC for events stored in UTC or as floating times. Writing UTC for a
  series anchored in a named zone pins it to one offset and shifts every occurrence by an
  hour across DST; a test pins the TZID form.
- **`update_event` edits through `event.icalendar_instance`**, never by re-serialising
  from vobject. Reading `event.data` first captures the raw bytes for the backup; touching
  the icalendar instance afterwards clears the cached raw data so `save()` serialises the
  edit. `save(only_this_recurrence=False)` is deliberate: we hold the whole `.ics`, and the
  default would try to merge into a master. `load()` records the ETag, so the PUT carries
  `If-Match` and a 412 surfaces as `ETagMismatchError`.
- **iCloud CardDAV throttles the account's partition with empty 401s.** The root PROPFIND
  (served by another partition) keeps answering, but everything under `/<dsid>/` returns
  401 with no body, on every host, for minutes at a time (roughly fifteen minutes on
  2026-09-08 after a day of probing; an earlier blip cleared in seconds). Each request is a fresh Basic-auth login to Apple, so
  volume is the likely trigger. Mitigations: discovery is cached per process
  (`_addressbook_cache`, one hour), a 401 past the root is reported as throttling rather
  than bad credentials, and short retries are kept only for the seconds-long blips seen
  earlier. Apple also varies between absolute partition-host hrefs and relative ones.
- **Notes sync over CloudKit, not IMAP.** `search_notes` reaches only legacy IMAP notes and
  is empty for most accounts; an empty result is not evidence the user has no notes.
