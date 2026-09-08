"""list_calendars, list_addressbooks and update_event. Nothing here touches a real account."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from caldav.lib.error import ETagMismatchError
from fastmcp.exceptions import ToolError

import server
from fakes import HOME_XML, PRINCIPAL_XML, FakeCalendar, FakeDAVClient, FakeHTTP, FakeResponse, collections_xml, make_event

UTC = timezone.utc


@pytest.fixture(autouse=True)
def isolate_backups(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "BACKUP_DIR", str(tmp_path / "backups"))
    return tmp_path / "backups"


def use_calendars(*calendars):
    server.get_caldav_client = lambda: FakeDAVClient(list(calendars))
    return calendars


# --------------------------------------------------------------------------
# list_calendars
# --------------------------------------------------------------------------

WRITE = ("read", "write", "write-content", "bind", "unbind")


def test_list_calendars_reports_kind_writability_sharing_and_colour():
    # Shapes observed live: Reminders lists are calendars that only support VTODO.
    use_calendars(
        FakeCalendar("Family", cal_id="9241", info={"display": "Family", "comps": ("VEVENT",), "privs": WRITE,
                                                    "rtypes": ("collection", "calendar", "shared-owner"), "color": "#1BADF8FF"}),
        FakeCalendar("Reminders", info={"comps": ("VTODO",), "privs": WRITE}),
        FakeCalendar("Holidays", info={"comps": ("VEVENT",), "privs": ("read",), "rtypes": ("collection", "calendar", "shared")}),
    )
    result = server.list_calendars()
    assert result == [
        {"name": "Family", "id": "9241", "kind": "events", "writable": True, "sharing": "owner", "color": "#1BADF8FF"},
        {"name": "Reminders", "id": "reminders", "kind": "reminders", "writable": True, "sharing": "", "color": ""},
        {"name": "Holidays", "id": "holidays", "kind": "events", "writable": False, "sharing": "shared-with-me", "color": ""},
    ]


def test_list_calendars_survives_a_failing_propfind():
    use_calendars(FakeCalendar("Odd", info=None), FakeCalendar("Home", info={"comps": ("VEVENT",)}))
    result = {c["name"]: c for c in server.list_calendars()}
    assert result["Odd"] == {"name": "Odd", "id": "odd", "kind": "unknown", "writable": None, "sharing": "", "color": ""}
    assert result["Home"]["kind"] == "events" and result["Home"]["writable"] is None


def test_list_calendars_issues_one_depth_zero_propfind_per_calendar():
    cals = use_calendars(FakeCalendar("A", info={"comps": ("VEVENT",)}), FakeCalendar("B", info={"comps": ("VEVENT",)}))
    server.list_calendars()
    for cal in cals:
        assert cal.client.propfinds == [(cal.url, 0)]


# --------------------------------------------------------------------------
# list_addressbooks
# --------------------------------------------------------------------------

def install(*extra):
    http = FakeHTTP([FakeResponse(PRINCIPAL_XML), FakeResponse(HOME_XML), *extra])
    server.requests.request = http
    return http


def test_list_addressbooks_returns_names_and_display_names():
    install(FakeResponse(collections_xml("card", "work", display_names={"card": "Cody's Contacts"})))
    assert server.list_addressbooks() == [
        {"name": "card", "display_name": "Cody's Contacts"},
        {"name": "work", "display_name": ""},
    ]


def test_list_addressbooks_asks_for_displayname():
    http = install(FakeResponse(collections_xml("card")))
    server.list_addressbooks()
    body = http.calls[-1]["data"].decode()
    assert "<displayname/>" in body and "<resourcetype/>" in body


def test_list_addressbooks_ignores_non_addressbook_collections():
    install(FakeResponse(collections_xml("card")))
    assert [b["name"] for b in server.list_addressbooks()] == ["card"]


# --------------------------------------------------------------------------
# update_event
# --------------------------------------------------------------------------

ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Apple Inc.//iOS 26.0//EN\r\n"
    "BEGIN:VTIMEZONE\r\nTZID:America/Chicago\r\nBEGIN:STANDARD\r\nDTSTART:19701101T020000\r\n"
    "TZOFFSETFROM:-0500\r\nTZOFFSETTO:-0600\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n"
    "BEGIN:VEVENT\r\nUID:ABC-123\r\nSUMMARY:Dentist\r\n"
    "DTSTART;TZID=America/Chicago:20260910T100000\r\nDTEND;TZID=America/Chicago:20260910T110000\r\n"
    "LOCATION:12 Main St\r\nSEQUENCE:2\r\nX-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC\r\n"
    "BEGIN:VALARM\r\nTRIGGER:-PT15M\r\nACTION:DISPLAY\r\nDESCRIPTION:Reminder\r\nEND:VALARM\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


def target(data=ICS, **kwargs):
    return make_event("Dentist", datetime(2026, 9, 10, 15, tzinfo=UTC), uid="ABC-123", data=data, **kwargs)


def test_update_requires_a_field():
    use_calendars(FakeCalendar("Home", events=[target()]))
    with pytest.raises(ToolError, match="Nothing to update"):
        server.update_event("ABC-123")


def test_update_dry_run_is_the_default_and_saves_nothing(isolate_backups):
    event = target()
    use_calendars(FakeCalendar("Home", events=[event]))
    result = server.update_event("ABC-123", summary="Dentist (moved)")
    assert result["dry_run"] is True and result["changed"] is False and result["backup"] is None
    assert result["changes"] == ["summary: 'Dentist' -> 'Dentist (moved)'"]
    assert result["before"]["summary"] == "Dentist" and result["after"]["summary"] == "Dentist (moved)"
    assert result["before"]["start"] == "2026-09-10T10:00:00-05:00"
    assert event.saved == [] and not isolate_backups.exists()


def test_moving_start_keeps_the_duration_and_preserves_everything_else(isolate_backups):
    event = target()
    use_calendars(FakeCalendar("Home", events=[event]))
    result = server.update_event("ABC-123", start="2026-09-11T14:00:00-05:00", dry_run=False)
    assert result["changed"] is True
    assert result["changes"] == [
        "start: 2026-09-10T10:00:00-05:00 -> 2026-09-11T14:00:00-05:00",
        "end: 2026-09-10T11:00:00-05:00 -> 2026-09-11T15:00:00-05:00",
    ]
    [save] = event.saved
    assert save["only_this_recurrence"] is False, "we hold the whole .ics; the library must not merge"
    data = save["data"]
    assert "DTSTART:20260911T190000Z" in data and "DTEND:20260911T200000Z" in data
    assert "SUMMARY:Dentist" in data and "LOCATION:12 Main St" in data
    assert "BEGIN:VALARM" in data and "TRIGGER:-PT15M" in data, "alarm preserved"
    assert "X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC" in data, "custom property preserved"
    assert "BEGIN:VTIMEZONE" in data
    # The original was backed up byte for byte before the save.
    assert Path(result["backup"]).read_bytes().decode() == ICS


def test_end_only_and_explicit_both():
    event = target()
    use_calendars(FakeCalendar("Home", events=[event]))
    result = server.update_event("ABC-123", end="2026-09-10T12:00:00-05:00", dry_run=False)
    assert result["changes"] == ["end: 2026-09-10T11:00:00-05:00 -> 2026-09-10T12:00:00-05:00"]
    assert "DTSTART;TZID=America/Chicago:20260910T100000" in event.data, "untouched start keeps its TZID form"
    assert "DTEND:20260910T170000Z" in event.data


def test_inverted_times_are_rejected_before_anything_is_touched():
    event = target()
    use_calendars(FakeCalendar("Home", events=[event]))
    with pytest.raises(ToolError, match="must be after"):
        server.update_event("ABC-123", end="2026-09-10T09:00:00-05:00", dry_run=False)
    assert event.saved == []


def test_location_and_description_can_be_set_and_removed():
    event = target()
    use_calendars(FakeCalendar("Home", events=[event]))
    result = server.update_event("ABC-123", location="", description="Bring insurance card", dry_run=False)
    assert result["changes"] == ["location: '12 Main St' -> ''", "description: '' -> 'Bring insurance card'"]
    assert "LOCATION" not in event.data and "DESCRIPTION:Bring insurance card" in event.data
    assert result["after"]["location"] == "" and result["after"]["description"] == "Bring insurance card"


def test_unchanged_values_are_a_no_op():
    event = target()
    use_calendars(FakeCalendar("Home", events=[event]))
    result = server.update_event("ABC-123", summary="Dentist", location="12 Main St", dry_run=False)
    assert result["changed"] is False and result["changes"] == [] and event.saved == []


def test_all_day_events_take_dates():
    ics = ICS.replace("DTSTART;TZID=America/Chicago:20260910T100000", "DTSTART;VALUE=DATE:20260910").replace(
        "DTEND;TZID=America/Chicago:20260910T110000", "DTEND;VALUE=DATE:20260911")
    event = target(ics)
    use_calendars(FakeCalendar("Home", events=[event]))
    result = server.update_event("ABC-123", start="2026-09-12", dry_run=False)
    assert result["before"]["all_day"] is True
    assert result["changes"] == ["start: 2026-09-10 -> 2026-09-12", "end: 2026-09-11 -> 2026-09-13"]
    assert "DTSTART;VALUE=DATE:20260912" in event.data and "DTEND;VALUE=DATE:20260913" in event.data
    with pytest.raises(ToolError, match="must be a date"):
        server.update_event("ABC-123", start="2026-09-12T10:00:00Z")


def test_duration_based_events_are_converted_to_dtend():
    ics = ICS.replace("DTEND;TZID=America/Chicago:20260910T110000", "DURATION:PT30M")
    event = target(ics)
    use_calendars(FakeCalendar("Home", events=[event]))
    result = server.update_event("ABC-123", start="2026-09-10T16:00:00Z", dry_run=False)
    assert result["before"]["end"] == "2026-09-10T10:30:00-05:00"
    assert "DURATION" not in event.data and "DTEND:20260910T163000Z" in event.data


def test_edits_go_to_the_series_master_not_an_override():
    override = (
        "BEGIN:VEVENT\r\nUID:ABC-123\r\nRECURRENCE-ID;TZID=America/Chicago:20260917T100000\r\nSUMMARY:Dentist (one-off)\r\n"
        "DTSTART;TZID=America/Chicago:20260917T130000\r\nDTEND;TZID=America/Chicago:20260917T140000\r\nEND:VEVENT\r\n"
    )
    ics = ICS.replace("SEQUENCE:2\r\n", "SEQUENCE:2\r\nRRULE:FREQ=WEEKLY\r\n").replace("END:VCALENDAR", override + "END:VCALENDAR")
    ics = ics.replace("BEGIN:VEVENT\r\nUID:ABC-123\r\nSUMMARY:Dentist", override.rstrip("\r\n")[:0] + "BEGIN:VEVENT\r\nUID:ABC-123\r\nSUMMARY:Dentist", 1)
    # Put the override FIRST so a naive "first VEVENT" pick would edit the wrong one.
    head, master = ics.split("BEGIN:VEVENT\r\nUID:ABC-123\r\nSUMMARY:Dentist", 1)
    ics = head + override + "BEGIN:VEVENT\r\nUID:ABC-123\r\nSUMMARY:Dentist" + master.replace(override, "")
    event = target(ics)
    use_calendars(FakeCalendar("Home", events=[event]))
    result = server.update_event("ABC-123", summary="Dentist (series)", dry_run=False)
    assert result["before"]["summary"] == "Dentist"
    assert "SUMMARY:Dentist (series)" in event.data and "SUMMARY:Dentist (one-off)" in event.data
    assert "RRULE:FREQ=WEEKLY" in event.data


def test_concurrent_edit_is_refused(isolate_backups):
    event = target(save_error=ETagMismatchError("412"))
    use_calendars(FakeCalendar("Home", events=[event]))
    with pytest.raises(ToolError, match="changed on the server"):
        server.update_event("ABC-123", summary="x", dry_run=False)


def test_other_save_failures_are_tool_errors():
    event = target(save_error=Exception("403 Forbidden"))
    use_calendars(FakeCalendar("Home", events=[event]))
    with pytest.raises(ToolError, match="Saving the event to 'Home' failed"):
        server.update_event("ABC-123", summary="x", dry_run=False)


def test_unknown_event_and_blank_summary_are_rejected():
    use_calendars(FakeCalendar("Home", events=[target()]))
    with pytest.raises(ToolError, match="No event found"):
        server.update_event("nope", summary="x")
    with pytest.raises(ToolError, match="summary cannot be empty"):
        server.update_event("ABC-123", summary="  ")


def test_calendar_argument_scopes_the_lookup():
    event = target()
    home, work = use_calendars(FakeCalendar("Home", events=[event]), FakeCalendar("Work"))
    with pytest.raises(ToolError, match="No event found"):
        server.update_event("ABC-123", calendar="Work", summary="x")
    assert server.update_event("ABC-123", calendar="Home", summary="x")["calendar"] == "Home"
