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
```

Tests run fully offline against fakes in `tests/fakes.py`. No credentials or network needed.
Live checks against the real account require `.env` (`ICLOUD_EMAIL`, `ICLOUD_APP_PASSWORD`).

## Architecture

One module, `src/server.py`, exposing nine FastMCP tools over four protocols: IMAP for mail
and legacy notes, SMTP (stdlib `smtplib`, STARTTLS on 587) for sending, CalDAV (via the
`caldav` lib) for calendar, and raw CardDAV over `requests` for contacts. `tests/fakes.py`
provides stand-ins for all four; tests swap `_connect_imap` and `_connect_smtp` for fakes.

**CardDAV is hand-rolled** and has the most structure. `_addressbook_urls` → `_fetch_cards`
→ `_put_card` is the shared transport; every contact tool goes through it. `_fetch_cards`
returns `Card(url, etag, raw)` — the ETag is what makes safe writes possible. Read and write
paths share this layer, so changes to discovery affect both.

**IMAP connections are cached** module-globally (`_imap_client`) behind `_imap_lock`, because
iCloud caps concurrent connections and throttles repeated logins. `imap_session()` probes
liveness with `noop()` and reconnects on failure. It deliberately keeps the connection on
`ToolError` (our own validation, stream still fine) and drops it on anything else (may have
desynchronised the protocol).

## Constraints that will bite you

**stdout is the JSON-RPC stream.** This is a stdio server. Never `print()`; all diagnostics
go through `log` to stderr. `tests/test_mail.py` asserts stdout stays byte-empty on import,
both with and without credentials.

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

**Write tools default to `dry_run=True`** and are annotated `destructiveHint`. Contact
writes back up originals to `backups/` and send `If-Match` so a concurrent edit from another
device is reported rather than clobbered. `contact_ids=[]` and `message_ids=[]` raise rather
than matching everything. Get explicit user confirmation before any `dry_run=False` run
against the live account.

**`delete_emails` fails whole, not partial.** Message ids are IMAP UIDs and are per-folder;
if any requested id is missing from the folder the tool raises before acting on the rest.
The default action is a single `MOVE` to Trash (found via `find_special_folder`, which is
`Deleted Messages` on iCloud). `permanent=True` writes each message to
`backups/mail-<stamp>/*.eml` first, then flags `\Deleted` and issues `UID EXPUNGE` for
just those UIDs; a plain `EXPUNGE` would also purge messages other clients flagged.

**iCloud SMTP does not file sent mail.** `send_email` APPENDs a copy to the Sent folder over
IMAP after delivery. That step runs after the message has left, so it logs and reports
`saved_to_sent: False` rather than raising. Bcc recipients go only in the SMTP envelope.
Replies (`reply_to_id`) fetch only the threading headers of the original via
`HEADER.FIELDS`, read-only and with PEEK, so replying never marks the original as read.

**Body text goes through `_message_text`.** Both `search_emails` previews and `get_email`
use it: text/plain preferred, otherwise HTML converted with `_html_to_text`, which drops
`<style>`/`<script>`/`<head>`, turns block boundaries into line breaks and unescapes
entities. Previews then collapse whitespace via `_truncate`; full bodies keep line structure.

**`load_dotenv` resolves `.env` relative to the source file**, not the cwd — MCP clients
launch servers from arbitrary directories.

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
- **Notes sync over CloudKit, not IMAP.** `search_notes` reaches only legacy IMAP notes and
  is empty for most accounts; an empty result is not evidence the user has no notes.
