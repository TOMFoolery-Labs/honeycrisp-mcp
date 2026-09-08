"""Live write round trip for calendar and contacts against the real iCloud account.

Creates one tagged event on a non-shared, writable calendar and one tagged
contact, runs update and delete on each, verifies every step by reading
back, and checks the backups. Only the two items it creates are touched;
on failure it still tries to remove them. Needs .env.

    .venv/bin/python scripts/roundtrip_pim.py              # both halves
    .venv/bin/python scripts/roundtrip_pim.py --contacts   # or --calendar
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import server  # noqa: E402

TAG = f"honeycrisp-roundtrip-{uuid.uuid4().hex[:10]}"
created = {"event": None, "calendar": None, "contact": None}
failed = False


def say(msg):
    print(f"   {msg}")


def check(cond, msg):
    global failed
    if not cond:
        failed = True
        print(f"\nFAILED: {msg}")
        raise SystemExit(1)
    say(f"ok: {msg}")


def find_event(event_id, start, end):
    events = server.get_calendar_events(start_date=start.isoformat(), end_date=end.isoformat(), limit=500)
    return [e for e in events if e["id"] == event_id]


def find_contacts():
    return [c for c in server.search_contacts(query="Honeycrisp Roundtrip", limit=50) if TAG in c["name"]]


def calendar_round_trip():
    cals = server.list_calendars()
    candidates = [c for c in cals if c["kind"] == "events" and c["writable"] and c["sharing"] == ""]
    check(candidates, f"a writable, non-shared events calendar exists (have: {[c['name'] for c in cals]})")
    target = next((c for c in candidates if c["name"] == "Home"), candidates[0])["name"]
    created["calendar"] = target
    say(f"using calendar {target!r}")

    start = (datetime.now(timezone.utc) + timedelta(days=30)).replace(hour=15, minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=45)
    window = (start - timedelta(days=2), start + timedelta(days=5))

    print("\n== 1. create_event")
    r = server.create_event(summary=f"Honeycrisp round trip {TAG}", start=start.isoformat(), end=end.isoformat(),
                            calendar=target, location="Nowhere", description="Created by scripts/roundtrip_pim.py", dry_run=False)
    check(r["created"], "create reported")
    created["event"] = r["id"]
    found = find_event(r["id"], *window)
    check(len(found) == 1, f"event visible via get_calendar_events (id={r['id']})")
    check(found[0]["summary"] == f"Honeycrisp round trip {TAG}", "summary round-tripped")
    check(found[0]["calendar"] == target, f"landed in {target!r}")

    print("\n== 2. update_event (move one day later, change location)")
    new_start = start + timedelta(days=1)
    r = server.update_event(created["event"], start=new_start.isoformat(), location="Somewhere", dry_run=False)
    check(r["changed"], f"update reported: {r['changes']}")
    check(r["backup"] and os.path.isfile(r["backup"]), f"backup written: {r['backup']}")
    found = find_event(created["event"], *window)
    check(len(found) == 1, "event still exactly once after update")
    got = datetime.fromisoformat(found[0]["start"]).astimezone(timezone.utc)
    check(got == new_start, f"start moved: {found[0]['start']}")
    check(r["after"]["location"] == "Somewhere", "location changed")
    check(r["after"]["end"] and datetime.fromisoformat(r["after"]["end"]).astimezone(timezone.utc) == new_start + timedelta(minutes=45),
          "duration preserved")

    print("\n== 3. delete_event")
    r = server.delete_event(created["event"], dry_run=False)
    check(r["deleted"], "delete reported")
    check(r["backup"] and os.path.isfile(r["backup"]), f"backup written: {r['backup']}")
    check(not find_event(created["event"], *window), "event gone")
    created["event"] = None


def contact_round_trip():
    print("\n== 4. create_contact")
    r = server.create_contact(name=f"Honeycrisp Roundtrip {TAG}", emails=[f"{TAG}@example.com"],
                              phones=["+1 202 555 0199"], organization="Honeycrisp Tests", dry_run=False)
    check(r["created"], "create reported")
    created["contact"] = r["contact"]["id"]
    found = find_contacts()
    check(len(found) == 1 and found[0]["id"] == created["contact"], f"contact visible via search_contacts (id={created['contact']})")
    check(found[0]["emails"] == [f"{TAG}@example.com"] and found[0]["phones"] == ["+1 202 555 0199"], "fields round-tripped")
    check(found[0]["organization"] == "Honeycrisp Tests", "organization round-tripped")

    print("\n== 5. update_contact (replace phone)")
    r = server.update_contact(created["contact"], phones=["+1 202 555 0100"], dry_run=False)
    check(r["changed"], f"update reported: {r['changes']}")
    found = find_contacts()
    check(len(found) == 1 and found[0]["phones"] == ["+1 202 555 0100"], "phone replaced on the server")
    check(found[0]["emails"] == [f"{TAG}@example.com"], "email untouched")

    print("\n== 6. delete_contact")
    r = server.delete_contact(created["contact"], dry_run=False)
    check(r["deleted"], "delete reported")
    check(r["backup"] and os.path.isfile(r["backup"]), f"backup written: {r['backup']}")
    check(not find_contacts(), "contact gone")
    created["contact"] = None


def cleanup():
    if created["event"]:
        try:
            server.delete_event(created["event"], dry_run=False)
            print(f"cleanup: removed event {created['event']}")
        except Exception as e:
            print(f"cleanup: could not remove event {created['event']}: {e}")
    if created["contact"]:
        try:
            server.delete_contact(created["contact"], dry_run=False)
            print(f"cleanup: removed contact {created['contact']}")
        except Exception as e:
            print(f"cleanup: could not remove contact {created['contact']}: {e}")


def main():
    which = sys.argv[1:] or ["--calendar", "--contacts"]
    try:
        if "--calendar" in which:
            calendar_round_trip()
        if "--contacts" in which:
            contact_round_trip()
    finally:
        cleanup()
    print("\nRound trip complete. Nothing created by this run remains on the account.")


if __name__ == "__main__":
    main()
