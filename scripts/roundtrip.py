"""Live write round trip against the real iCloud account.

Sends one uniquely tagged message to the account's own address, then runs
every mail write tool on it with dry_run=False, verifying each step over
IMAP, and finally removes every copy (INBOX and Sent) permanently. Only the
message this script creates is ever touched. Needs .env.

    .venv/bin/python scripts/roundtrip.py
    .venv/bin/python scripts/roundtrip.py --resume <tag>   # finish cleanup after a failure
"""

import os
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import imapclient  # noqa: E402

import server  # noqa: E402

TAG = sys.argv[2] if len(sys.argv) == 3 and sys.argv[1] == "--resume" else f"honeycrisp-roundtrip-{uuid.uuid4().hex[:10]}"
SUBJECT = f"Honeycrisp round trip {TAG}"
RESUME = "--resume" in sys.argv

# Self-addressed delivery on iCloud has been observed to take over 90 s.
DELIVERY_WAIT = 240


def say(msg):
    print(f"   {msg}")


def check(cond, msg):
    if not cond:
        print(f"\nFAILED: {msg}")
        print(f"Re-run with --resume {TAG} once the mail has settled to finish the cleanup,")
        print(f"or look for subject {SUBJECT!r} to clean up by hand.")
        sys.exit(1)
    say(f"ok: {msg}")


def find(folder, timeout=0):
    """UIDs carrying the tag in a folder, waiting up to timeout seconds for one."""
    deadline = time.time() + timeout
    while True:
        with server.imap_session() as client:
            client.select_folder(folder, readonly=True)
            uids = client.search(["SUBJECT", TAG])
        if uids or time.time() >= deadline:
            return sorted(uids)
        time.sleep(3)


def flags_of(folder, uid):
    with server.imap_session() as client:
        client.select_folder(folder, readonly=True)
        return set(client.fetch([uid], ["FLAGS"]).get(uid, {}).get(b"FLAGS", ()))


def main():
    address, _ = server._require_credentials()
    roles = {f["role"]: f["name"] for f in server.list_folders() if f["role"]}
    trash, sent, archive = roles["trash"], roles["sent"], roles.get("archive")
    check(archive, f"archive folder present (roles: {roles})")

    if RESUME:
        print(f"\n== resuming cleanup for {TAG}")
        in_inbox = find("INBOX")
        check(in_inbox, "tagged messages present in INBOX")
        ids = [str(u) for u in in_inbox]
    else:
        ids = create_and_exercise(address, archive)

    print(f"\n== 7. delete_emails INBOX -> {trash}")
    r = server.delete_emails(ids, dry_run=False)
    check(r["deleted"] and r["action"] == "move_to_trash", "delete reported")
    check(not find("INBOX"), "gone from INBOX")
    in_trash = find(trash, timeout=15)
    check(len(in_trash) == 2, f"both copies in {trash!r} (uids={in_trash})")

    print(f"\n== 8. delete_emails from {sent} (the two Sent copies)")
    in_sent = find(sent, timeout=15)
    check(len(in_sent) == 2, f"two copies in {sent!r} (uids={in_sent})")
    server.delete_emails([str(u) for u in in_sent], folder=sent, dry_run=False)
    check(not find(sent), f"gone from {sent!r}")
    in_trash = find(trash, timeout=15)
    check(len(in_trash) == 4, f"all four copies in {trash!r} (uids={in_trash})")

    print(f"\n== 9. delete_emails permanent from {trash}")
    r = server.delete_emails([str(u) for u in in_trash], folder=trash, permanent=True, dry_run=False)
    check(r["action"] == "delete_permanently" and r["backup"], "permanent delete reported")
    saved = sorted(os.listdir(r["backup"]))
    check(len(saved) == 4, f"four .eml backups in {r['backup']}: {saved}")
    check(TAG.encode() in open(os.path.join(r["backup"], saved[0]), "rb").read(), "backup holds the message")
    check(not find(trash), f"gone from {trash!r}")

    for folder in ("INBOX", archive, sent, trash):
        check(not find(folder), f"no trace left in {folder!r}")
    print(f"\nRound trip complete. Backups kept at {r['backup']}")


def create_and_exercise(address, archive):
    """Steps 1-6: create the message and run the non-destructive tools on it."""
    print("\n== 1. send_email to self")
    r = server.send_email(to=[address], subject=SUBJECT, body=f"Round trip body {TAG}\nLine two.", dry_run=False)
    check(r["sent"], "SMTP accepted the message")
    check(r["saved_to_sent"], f"copy filed in {sent!r}")

    print("\n== 2. wait for delivery to INBOX")
    uids = find("INBOX", timeout=DELIVERY_WAIT)
    check(len(uids) == 1, f"exactly one copy landed in INBOX (uids={uids})")
    uid = str(uids[0])

    print("\n== 3. get_email")
    m = server.get_email(uid)
    check(m["subject"] == SUBJECT, "subject round-tripped")
    check(f"Round trip body {TAG}\nLine two." == m["body"], f"body round-tripped: {m['body']!r}")
    check(m["message_id"].startswith("<") and m["message_id"].endswith(">"), f"message_id {m['message_id']}")

    print("\n== 4. mark_emails read, then unread")
    server.mark_emails([uid], read=True, dry_run=False)
    check(imapclient.SEEN in flags_of("INBOX", int(uid)), "\\Seen set")
    server.mark_emails([uid], read=False, flagged=True, dry_run=False)
    f = flags_of("INBOX", int(uid))
    check(imapclient.SEEN not in f and imapclient.FLAGGED in f, f"\\Seen cleared and \\Flagged set ({f})")
    server.mark_emails([uid], flagged=False, dry_run=False)
    check(imapclient.FLAGGED not in flags_of("INBOX", int(uid)), "\\Flagged cleared")

    print(f"\n== 5. move_emails INBOX -> {archive} -> INBOX")
    r = server.move_emails([uid], to_folder=archive, dry_run=False)
    check(r["moved"], "move reported")
    check(not find("INBOX"), "gone from INBOX")
    in_archive = find(archive, timeout=15)
    check(len(in_archive) == 1, f"present once in {archive!r} (uids={in_archive})")
    server.move_emails([str(in_archive[0])], folder=archive, to_folder="INBOX", dry_run=False)
    check(not find(archive), f"gone from {archive!r}")
    back = find("INBOX", timeout=15)
    check(len(back) == 1, f"back in INBOX once (uids={back})")
    uid = str(back[0])

    print(f"\n== 6. send_email reply (threading), to self")
    r = server.send_email(body=f"Reply body {TAG}", reply_to_id=uid, dry_run=False)
    check(r["sent"] and r["in_reply_to"] == m["message_id"], f"reply threaded on {m['message_id']}")
    replies = [u for u in find("INBOX", timeout=DELIVERY_WAIT) if str(u) != uid]
    check(len(replies) == 1, f"reply landed in INBOX (uids={replies})")
    reply = server.get_email(str(replies[0]))
    check(reply["in_reply_to"] == m["message_id"], "In-Reply-To survived delivery")
    check(reply["subject"] == f"Re: {SUBJECT}", f"reply subject {reply['subject']!r}")

    return [uid, str(replies[0])]


if __name__ == "__main__":
    main()
