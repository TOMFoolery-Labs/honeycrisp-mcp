"""Read-only smoke test against the live iCloud account.

Prints the server facts the mail tools depend on (capabilities, special
folders, fetch key shapes) and walks every read tool plus every write tool
in dry-run mode. Nothing is modified. Needs .env with ICLOUD_EMAIL and
ICLOUD_APP_PASSWORD.

    .venv/bin/python scripts/smoke.py
"""

import os
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import server  # noqa: E402
from imapclient.imapclient import DRAFTS, JUNK, SENT, TRASH  # noqa: E402

failures = []


def step(name, fn):
    print(f"\n== {name}")
    try:
        result = fn()
        return result
    except Exception as e:
        failures.append(name)
        print(f"   FAILED: {type(e).__name__}: {e}")
        traceback.print_exc(limit=2)
        return None


def show(label, value):
    print(f"   {label}: {value}")


def capabilities():
    with server.imap_session() as client:
        caps = sorted(c.decode() for c in client.capabilities())
        show("capabilities", " ".join(caps))
        for needed in ("MOVE", "UIDPLUS", "SPECIAL-USE"):
            show(f"has {needed}", client.has_capability(needed))
        for flag, label in ((TRASH, "trash"), (SENT, "sent"), (DRAFTS, "drafts"), (JUNK, "junk")):
            show(f"special folder {label}", client.find_special_folder(flag))


def folders():
    result = server.list_folders()
    for f in result:
        show(f["name"], f"role={f['role']!r} selectable={f['selectable']} total={f['total']} unseen={f['unseen']}")
    return result


def recent():
    result = server.search_emails(limit=3)
    for m in result:
        show(m["id"], f"{m['date']}  {m['from']!r}  {m['subject']!r}")
    return result


def header_fetch_shape(uid):
    # The reply path relies on the key the server uses for a HEADER.FIELDS fetch.
    with server.imap_session() as client:
        client.select_folder("INBOX", readonly=True)
        data = client.fetch([uid], [server._REPLY_HEADERS])
        keys = list(data.get(uid, {}).keys())
        show("fetch keys", keys)
        raw = server._fetch_body_bytes(data.get(uid, {}))
        show("header bytes", len(raw))
        assert raw, "no BODY[...] key matched"


def full_message(uid):
    result = server.get_email(str(uid))
    show("subject", result["subject"])
    show("message_id", result["message_id"])
    show("body chars", f"{len(result['body'])} truncated={result['truncated']}")
    show("attachments", result["attachments"])
    show("body head", result["body"][:200].replace("\n", " | "))


def dry_runs(uid):
    r = server.delete_emails([str(uid)])
    show("delete_emails", f"action={r['action']} destination={r['destination']!r} deleted={r['deleted']}")
    r = server.move_emails([str(uid)], to_folder=trash_name or "INBOX") if trash_name else None
    if r:
        show("move_emails", f"destination={r['destination']!r} moved={r['moved']}")
    r = server.mark_emails([str(uid)], read=True)
    show("mark_emails", f"add={r['add_flags']} changed={r['changed']}")
    r = server.send_email(body="(smoke test, not sent)", reply_to_id=str(uid))
    show("send_email reply", f"to={r['to']} subject={r['subject']!r} sent={r['sent']}")
    show("  in_reply_to", r["in_reply_to"])
    show("  references", r["references"])


def calendar():
    result = server.get_calendar_events(limit=3)
    for e in result:
        show(e.get("start", ""), e.get("summary", ""))


def contacts():
    result = server.search_contacts(limit=3)
    for c in result:
        show(c.get("id", ""), c.get("name", ""))


trash_name = None


def main():
    global trash_name
    step("IMAP capabilities and special folders", capabilities)
    listing = step("list_folders", folders) or []
    trash_name = next((f["name"] for f in listing if f["role"] == "trash"), None)
    messages = step("search_emails (INBOX, newest 3)", recent) or []
    if messages:
        uid = int(messages[0]["id"])
        step("HEADER.FIELDS fetch key shape", lambda: header_fetch_shape(uid))
        step("get_email", lambda: full_message(uid))
        step("write tools, dry run", lambda: dry_runs(uid))
    else:
        print("   INBOX is empty; skipping message-level checks")
    step("get_calendar_events", calendar)
    step("search_contacts", contacts)

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        sys.exit(1)
    print("All smoke checks passed. Nothing was modified.")


if __name__ == "__main__":
    main()
