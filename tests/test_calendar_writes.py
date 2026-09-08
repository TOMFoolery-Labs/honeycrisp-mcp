"""create_event and delete_event. Nothing here touches a real account."""

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

import server
from fakes import FakeCalendar, FakeDAVClient, make_event

UTC = timezone.utc


@pytest.fixture(autouse=True)
def isolate_backups(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "BACKUP_DIR", str(tmp_path / "backups"))
    return tmp_path / "backups"


def use_calendars(*calendars):
    server.get_caldav_client = lambda: FakeDAVClient(list(calendars))
    return calendars


class DirectEvent:
    """Stand-in for caldav.Event constructed by URL: found in `store` or 404."""

    store = {}

    def __init__(self, client, url=None, parent=None):
        self.url = url
        self.parent = parent
        self.data = ""
        self.deleted = False

    def load(self):
        from caldav.lib.error import NotFoundError
        if self.url not in self.store:
            raise NotFoundError(self.url)
        self.data = self.store[self.url]["data"]
        self.vobject_instance = self.store[self.url]["event"].vobject_instance
        return self

    def delete(self):
        self.deleted = True


@pytest.fixture(autouse=True)
def direct_lookup(monkeypatch):
    DirectEvent.store = {}
    monkeypatch.setattr(server.caldav, "Event", DirectEvent)


def vevent_lines(ical):
    return ical.split("\r\n")


# --------------------------------------------------------------------------
# get_calendar_events now exposes ids
# --------------------------------------------------------------------------

def test_events_carry_their_uid_as_id():
    use_calendars(FakeCalendar("Home", events=[make_event("Standup", datetime(2026, 6, 1, 9, tzinfo=UTC), uid="ABC-123")]))
    [event] = server.get_calendar_events(start_date="2026-06-01T00:00:00Z")
    assert event["id"] == "ABC-123"


def test_events_without_uid_get_an_empty_id():
    use_calendars(FakeCalendar("Home", events=[make_event("Standup", datetime(2026, 6, 1, 9, tzinfo=UTC))]))
    assert server.get_calendar_events(start_date="2026-06-01T00:00:00Z")[0]["id"] == ""


# --------------------------------------------------------------------------
# create_event
# --------------------------------------------------------------------------

def test_create_dry_run_is_the_default_and_writes_nothing():
    [cal] = use_calendars(FakeCalendar("Home"))
    result = server.create_event(summary="Dentist", start="2026-09-10T15:00:00-05:00")
    assert result["dry_run"] is True and result["created"] is False
    assert result["calendar"] == "Home"
    assert result["start"] == "2026-09-10T15:00:00-05:00"
    assert result["end"] == "2026-09-10T16:00:00-05:00", "default duration is one hour"
    assert result["all_day"] is False
    assert len(result["id"]) == 36
    assert cal.added == []


def test_create_writes_a_well_formed_vevent_in_utc():
    [cal] = use_calendars(FakeCalendar("Home"))
    result = server.create_event(
        summary="Dentist; bring card, insurance",
        start="2026-09-10T15:00:00-05:00", end="2026-09-10T15:30:00-05:00",
        location="12 Main St", description="Line one\nLine two", dry_run=False,
    )
    assert result["created"] is True
    [added] = cal.added
    assert added["no_overwrite"] is True, "must never replace an existing object"
    lines = vevent_lines(added["ical"])
    assert lines[0] == "BEGIN:VCALENDAR" and "BEGIN:VEVENT" in lines and lines[-2:] == ["END:VCALENDAR", ""]
    assert f"UID:{result['id']}" in lines
    assert "DTSTART:20260910T200000Z" in lines
    assert "DTEND:20260910T203000Z" in lines
    assert r"SUMMARY:Dentist\; bring card\, insurance" in lines
    assert "LOCATION:12 Main St" in lines
    assert "DESCRIPTION:Line one\\nLine two" in lines
    assert any(line.startswith("DTSTAMP:") and line.endswith("Z") for line in lines)


def test_all_day_event_uses_date_values_and_next_day_end():
    [cal] = use_calendars(FakeCalendar("Home"))
    result = server.create_event(summary="Holiday", start="2026-12-25", all_day=True, dry_run=False)
    assert result["start"] == "2026-12-25" and result["end"] == "2026-12-26" and result["all_day"] is True
    lines = vevent_lines(cal.added[0]["ical"])
    assert "DTSTART;VALUE=DATE:20261225" in lines and "DTEND;VALUE=DATE:20261226" in lines


def test_long_lines_are_folded():
    [cal] = use_calendars(FakeCalendar("Home"))
    server.create_event(summary="x" * 200, start="2026-09-10T15:00:00Z", dry_run=False)
    raw = cal.added[0]["ical"]
    assert all(len(line.encode()) <= 75 for line in raw.split("\r\n"))
    assert "\r\n " in raw, "continuation lines start with a space"
    assert raw.replace("\r\n ", "").count("x" * 200) == 1


def test_naive_times_are_treated_as_utc():
    [cal] = use_calendars(FakeCalendar("Home"))
    server.create_event(summary="Call", start="2026-09-10T15:00:00", dry_run=False)
    assert "DTSTART:20260910T150000Z" in vevent_lines(cal.added[0]["ical"])


def test_multiple_calendars_require_an_explicit_choice():
    use_calendars(FakeCalendar("Home"), FakeCalendar("Work"))
    with pytest.raises(ToolError, match="several calendars.*'Home'.*'Work'"):
        server.create_event(summary="Call", start="2026-09-10T15:00:00Z")


def test_named_calendar_is_used_and_unknown_name_is_rejected():
    home, work = use_calendars(FakeCalendar("Home"), FakeCalendar("Work"))
    server.create_event(summary="Call", start="2026-09-10T15:00:00Z", calendar="Work", dry_run=False)
    assert len(work.added) == 1 and home.added == []
    with pytest.raises(ToolError, match="No calendar named 'Nope'"):
        server.create_event(summary="Call", start="2026-09-10T15:00:00Z", calendar="Nope")


@pytest.mark.parametrize("kwargs,match", [
    ({"summary": "  ", "start": "2026-09-10T15:00:00Z"}, "summary is required"),
    ({"summary": "x", "start": "not a date"}, "start must be"),
    ({"summary": "x", "start": "2026-09-10T15:00:00Z", "end": "2026-09-10T14:00:00Z"}, "must be after"),
    ({"summary": "x", "start": "2026-09-10T15:00:00Z", "end": "2026-09-10T15:00:00Z"}, "must be after"),
    ({"summary": "x", "start": "2026-13-45", "all_day": True}, "must be a date"),
])
def test_create_validation(kwargs, match):
    [cal] = use_calendars(FakeCalendar("Home"))
    with pytest.raises(ToolError, match=match):
        server.create_event(dry_run=False, **kwargs)
    assert cal.added == []


def test_server_rejection_is_a_tool_error():
    use_calendars(FakeCalendar("Home", add_error=Exception("403 Forbidden")))
    with pytest.raises(ToolError, match="Creating the event in 'Home' failed"):
        server.create_event(summary="x", start="2026-09-10T15:00:00Z", dry_run=False)


# --------------------------------------------------------------------------
# delete_event
# --------------------------------------------------------------------------

def target():
    return make_event("Standup", datetime(2026, 6, 1, 9, tzinfo=UTC), datetime(2026, 6, 1, 9, 30, tzinfo=UTC),
                      uid="ABC-123", data="BEGIN:VCALENDAR\r\nUID:ABC-123\r\nEND:VCALENDAR\r\n")


def test_delete_dry_run_is_the_default_and_touches_nothing(isolate_backups):
    event = target()
    use_calendars(FakeCalendar("Home", events=[event]))
    result = server.delete_event("ABC-123")
    assert result == {
        "dry_run": True, "deleted": False, "id": "ABC-123", "calendar": "Home", "backup": None,
        "summary": "Standup", "start": "2026-06-01T09:00:00+00:00", "end": "2026-06-01T09:30:00+00:00",
    }
    assert not event.deleted and not isolate_backups.exists()


def test_delete_backs_up_then_deletes(isolate_backups):
    event = target()
    use_calendars(FakeCalendar("Home", events=[event]))
    result = server.delete_event("ABC-123", dry_run=False)
    assert result["deleted"] is True and event.deleted
    assert result["backup"].startswith(str(isolate_backups)) and result["backup"].endswith(".ics")
    assert Path(result["backup"]).read_bytes().decode() == event.data


def test_delete_searches_every_calendar_and_skips_failing_ones():
    event = target()
    use_calendars(FakeCalendar("Broken", error=Exception("500")), FakeCalendar("Work"), FakeCalendar("Home", events=[event]))
    result = server.delete_event("ABC-123", dry_run=False)
    assert result["calendar"] == "Home" and event.deleted


def test_delete_scoped_to_a_calendar():
    event = target()
    use_calendars(FakeCalendar("Home", events=[event]), FakeCalendar("Work"))
    with pytest.raises(ToolError, match="No event found"):
        server.delete_event("ABC-123", calendar="Work", dry_run=False)
    assert not event.deleted


def test_missing_event_mentions_calendars_that_could_not_be_searched():
    use_calendars(FakeCalendar("Broken", error=Exception("500 boom")), FakeCalendar("Home"))
    with pytest.raises(ToolError, match="No event found.*Lookup failed in 'Broken'"):
        server.delete_event("ABC-123")


def test_delete_failure_is_a_tool_error_and_backup_survives(isolate_backups):
    event = target()
    event.delete_error = Exception("403")
    use_calendars(FakeCalendar("Home", events=[event]))
    with pytest.raises(ToolError, match="Deleting the event from 'Home' failed"):
        server.delete_event("ABC-123", dry_run=False)
    assert len(os.listdir(isolate_backups)) == 1


def test_lookup_tries_the_direct_url_before_the_report(isolate_backups):
    # Observed live: iCloud answers the by-UID REPORT with 412 but serves <cal>/<UID>.ics.
    class NoReport(FakeCalendar):
        def get_event_by_uid(self, uid):
            raise Exception("412 Precondition Failed")

    cal = NoReport("Home")
    use_calendars(cal)
    DirectEvent.store[f"{cal.url}ABC-123.ics"] = {"data": "BEGIN:VCALENDAR\r\nUID:ABC-123\r\nEND:VCALENDAR\r\n", "event": target()}
    result = server.delete_event("ABC-123", dry_run=False)
    assert result["calendar"] == "Home" and result["summary"] == "Standup"
    assert Path(result["backup"]).read_bytes().startswith(b"BEGIN:VCALENDAR")


def test_lookup_falls_back_to_the_report_when_the_url_is_absent():
    event = target()
    use_calendars(FakeCalendar("Home", events=[event]))
    result = server.delete_event("ABC-123", dry_run=False)
    assert result["calendar"] == "Home" and event.deleted


def test_blank_event_id_is_rejected():
    use_calendars(FakeCalendar("Home"))
    with pytest.raises(ToolError, match="event_id is required"):
        server.delete_event("  ")
