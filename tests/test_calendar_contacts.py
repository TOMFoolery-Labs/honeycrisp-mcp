import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

import pytest
from fastmcp.exceptions import ToolError

import server
from fakes import (
    HOME_XML,
    PRINCIPAL_XML,
    FakeDAVClient,
    FakeHTTP,
    FakeResponse,
    FakeCalendar,
    collections_xml,
    make_event,
    report_xml,
    vcard,
)

UTC = timezone.utc


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------

def use_calendars(*calendars):
    server.get_caldav_client = lambda: FakeDAVClient(list(calendars))
    return calendars


def test_start_date_is_honoured_without_an_end_date():
    cal = FakeCalendar("Home")
    use_calendars(cal)
    server.get_calendar_events(start_date="2026-03-01T00:00:00Z")
    kwargs = cal.search_kwargs
    assert kwargs["start"] == datetime(2026, 3, 1, tzinfo=UTC)
    # A bounded window, not an unbounded dump of every event ever.
    assert kwargs["end"] == datetime(2026, 3, 1, tzinfo=UTC) + server.DEFAULT_CALENDAR_WINDOW


def test_search_is_scoped_to_events_and_expands_recurrences():
    cal = FakeCalendar("Home")
    use_calendars(cal)
    server.get_calendar_events()
    assert cal.search_kwargs["event"] is True
    assert cal.search_kwargs["expand"] is True


def test_naive_start_is_treated_as_utc():
    cal = FakeCalendar("Home")
    use_calendars(cal)
    server.get_calendar_events(start_date="2026-03-01T09:00:00", end_date="2026-03-02T09:00:00")
    assert cal.search_kwargs["start"].tzinfo is not None


def test_default_start_is_now():
    cal = FakeCalendar("Home")
    use_calendars(cal)
    server.get_calendar_events()
    assert abs((cal.search_kwargs["start"] - datetime.now(UTC)).total_seconds()) < 60


@pytest.mark.parametrize("field,value", [
    ("start_date", "not-a-date"),
    ("end_date", "2026-13-45"),
])
def test_malformed_dates_are_rejected(field, value):
    use_calendars(FakeCalendar("Home"))
    with pytest.raises(ToolError, match="ISO 8601"):
        server.get_calendar_events(**{field: value})


def test_inverted_range_is_rejected():
    use_calendars(FakeCalendar("Home"))
    with pytest.raises(ToolError, match="must be after"):
        server.get_calendar_events(start_date="2026-05-01T00:00:00Z", end_date="2026-04-01T00:00:00Z")


def test_events_sort_chronologically_across_timezones_and_all_day():
    # Lexicographic ISO sorting gets this wrong: the all-day date has no offset,
    # and 09:00-07:00 is later in absolute time than 15:00+00:00.
    cal = FakeCalendar("Home", events=[
        make_event("Afternoon UTC", datetime(2026, 6, 1, 15, 0, tzinfo=UTC)),
        make_event("All day", date(2026, 6, 1)),
        make_event("Morning Pacific", datetime(2026, 6, 1, 9, 0, tzinfo=timezone(timedelta(hours=-7)))),
    ])
    use_calendars(cal)
    events = server.get_calendar_events(start_date="2026-06-01T00:00:00Z", end_date="2026-06-02T00:00:00Z")
    assert [e["summary"] for e in events] == ["All day", "Afternoon UTC", "Morning Pacific"]


def test_all_day_events_are_flagged():
    cal = FakeCalendar("Home", events=[make_event("Holiday", date(2026, 6, 1))])
    use_calendars(cal)
    events = server.get_calendar_events(start_date="2026-06-01T00:00:00Z", end_date="2026-06-02T00:00:00Z")
    assert events[0]["all_day"] is True


def test_results_merge_across_calendars_and_respect_limit():
    a = FakeCalendar("Work", events=[make_event(f"W{i}", datetime(2026, 6, 1, i, tzinfo=UTC)) for i in range(5)])
    b = FakeCalendar("Home", events=[make_event("H", datetime(2026, 6, 1, 1, 30, tzinfo=UTC))])
    use_calendars(a, b)
    events = server.get_calendar_events(start_date="2026-06-01T00:00:00Z", end_date="2026-06-02T00:00:00Z", limit=3)
    assert [e["summary"] for e in events] == ["W0", "W1", "H"]
    assert events[0]["calendar"] == "Work"


def test_one_failing_calendar_does_not_sink_the_others():
    good = FakeCalendar("Home", events=[make_event("Standup", datetime(2026, 6, 1, 9, tzinfo=UTC))])
    bad = FakeCalendar("Broken", error=Exception("500 Server Error"))
    use_calendars(bad, good)
    events = server.get_calendar_events(start_date="2026-06-01T00:00:00Z", end_date="2026-06-02T00:00:00Z")
    assert [e["summary"] for e in events] == ["Standup"]


def test_connection_failure_raises_rather_than_returning_error_shaped_data():
    def boom():
        raise Exception("network down")
    server.get_caldav_client = boom
    with pytest.raises(ToolError, match="Failed to connect"):
        server.get_calendar_events()


# --------------------------------------------------------------------------
# Contacts
# --------------------------------------------------------------------------

def install_http(*extra):
    http = FakeHTTP([
        FakeResponse(PRINCIPAL_XML),
        FakeResponse(HOME_XML),
        *extra,
    ])
    server.requests.request = http
    return http


def test_partitioned_absolute_hrefs_are_resolved_not_concatenated():
    http = install_http(
        FakeResponse(collections_xml("card")),
        FakeResponse(report_xml(vcard("Ann", ["a@b.com"]))),
    )
    server.search_contacts()
    urls = [c["url"] for c in http.calls]
    assert not any("icloud.comhttps" in u for u in urls), urls
    assert urls[1] == "https://p61-contacts.icloud.com:443/123456/principal/"
    assert urls[2] == "https://p61-contacts.icloud.com:443/123456/carddavhome/"
    assert urls[3] == "https://p61-contacts.icloud.com:443/123456/carddavhome/card/"


def test_every_request_carries_a_timeout():
    http = install_http(
        FakeResponse(collections_xml("card")),
        FakeResponse(report_xml(vcard("Ann"))),
    )
    server.search_contacts()
    assert all(c["timeout"] == server.HTTP_TIMEOUT for c in http.calls)


@pytest.mark.parametrize("query", ["Smith & Sons", "O<Brien", 'He said "hi"', "a > b", "José & Co"])
def test_special_characters_in_query_produce_well_formed_xml(query):
    http = install_http(
        FakeResponse(collections_xml("card")),
        FakeResponse(report_xml(vcard(query))),
    )
    results = server.search_contacts(query=query)
    body = http.calls[-1]["data"]
    ET.fromstring(body)  # raises if the query broke the document
    assert query in ET.fromstring(body).findtext(".//{urn:ietf:params:xml:ns:carddav}text-match")
    assert results[0]["name"] == query


def test_report_body_is_utf8_encoded():
    http = install_http(
        FakeResponse(collections_xml("card")),
        FakeResponse(report_xml(vcard("José"))),
    )
    server.search_contacts(query="José")
    body = http.calls[-1]["data"]
    assert isinstance(body, bytes)
    assert "José" in body.decode("utf-8")


def test_empty_query_omits_the_text_match_filter():
    http = install_http(
        FakeResponse(collections_xml("card")),
        FakeResponse(report_xml(vcard("Ann"))),
    )
    server.search_contacts()
    assert b"text-match" not in http.calls[-1]["data"]


def test_all_emails_and_phones_are_returned():
    http = install_http(
        FakeResponse(collections_xml("card")),
        FakeResponse(report_xml(vcard("Ann", ["a@work.com", "a@home.com"], ["555-1111", "555-2222"]))),
    )
    results = server.search_contacts()
    assert results == [{
        "id": "uid-Ann",
        "name": "Ann",
        "organization": "",
        "emails": ["a@work.com", "a@home.com"],
        "phones": ["555-1111", "555-2222"],
    }]


def test_contacts_without_email_or_phone_are_still_returned():
    http = install_http(
        FakeResponse(collections_xml("card")),
        FakeResponse(report_xml(vcard("No Details"))),
    )
    assert server.search_contacts() == [
        {"id": "uid-No Details", "name": "No Details", "organization": "",
         "emails": [], "phones": []}
    ]


def test_all_address_books_are_searched():
    http = install_http(
        FakeResponse(collections_xml("card", "work")),
        FakeResponse(report_xml(vcard("Ann"))),
        FakeResponse(report_xml(vcard("Bob"))),
    )
    results = server.search_contacts()
    assert [r["name"] for r in results] == ["Ann", "Bob"]
    assert http.calls[-1]["url"].endswith("/work/")


def test_non_addressbook_collections_are_ignored():
    http = install_http(
        FakeResponse(collections_xml("card")),
        FakeResponse(report_xml(vcard("Ann"))),
    )
    server.search_contacts()
    assert len([c for c in http.calls if c["method"] == "REPORT"]) == 1


def test_unparseable_vcards_are_skipped_not_fatal():
    http = install_http(
        FakeResponse(collections_xml("card")),
        FakeResponse(report_xml("NOT A VCARD", vcard("Ann"))),
    )
    assert [r["name"] for r in server.search_contacts()] == ["Ann"]


def test_limit_is_respected():
    http = install_http(
        FakeResponse(collections_xml("card")),
        FakeResponse(report_xml(*[vcard(f"P{i}") for i in range(10)])),
    )
    assert len(server.search_contacts(limit=3)) == 3


def test_no_address_books_returns_empty():
    install_http(FakeResponse(collections_xml()))
    assert server.search_contacts() == []


def test_spurious_401_during_discovery_is_retried(monkeypatch):
    # Observed live: iCloud intermittently 401s a correctly authenticated PROPFIND.
    monkeypatch.setattr(server, "CARDDAV_RETRY_DELAY", 0)
    http = FakeHTTP([FakeResponse(PRINCIPAL_XML), FakeResponse("", status_code=401),
                     FakeResponse(HOME_XML), FakeResponse(collections_xml("card")), FakeResponse(report_xml())])
    server.requests.request = http
    assert server.search_contacts() == []
    assert [c["method"] for c in http.calls].count("PROPFIND") == 4


def test_persistent_401_is_reported_after_retries(monkeypatch):
    monkeypatch.setattr(server, "CARDDAV_RETRY_DELAY", 0)
    http = FakeHTTP([FakeResponse("", status_code=401)] * (server.CARDDAV_401_RETRIES + 1))
    server.requests.request = http
    with pytest.raises(ToolError, match="401"):
        server.search_contacts()
    assert len(http.calls) == server.CARDDAV_401_RETRIES + 1


def test_failed_discovery_raises():
    server.requests.request = FakeHTTP([FakeResponse("nope", status_code=500)])
    with pytest.raises(ToolError, match="PROPFIND failed"):
        server.search_contacts()


@pytest.mark.parametrize("limit", [0, -5])
def test_contacts_limit_guard(limit):
    with pytest.raises(ToolError, match="limit must be at least 1"):
        server.search_contacts(limit=limit)


# --------------------------------------------------------------------------
# Notes
# --------------------------------------------------------------------------

def test_missing_notes_folder_explains_cloudkit():
    from fakes import FakeIMAP
    server._imap_client = None
    server._connect_imap = lambda: FakeIMAP(folders=("INBOX",))
    with pytest.raises(ToolError, match="CloudKit"):
        server.search_notes()
    server._imap_client = None


def test_notes_reads_the_notes_folder():
    from fakes import Envelope, FakeIMAP
    server._imap_client = None
    client = FakeIMAP(
        uids=[1],
        messages={1: {b"ENVELOPE": Envelope(subject=b"Grocery list"), b"BODY[]<0>": b""}},
        folders=("INBOX", "Notes"),
    )
    server._connect_imap = lambda: client
    results = server.search_notes()
    assert results[0]["subject"] == "Grocery list"
    assert ("select_folder", "Notes", True) in client.calls
    server._imap_client = None


# --------------------------------------------------------------------------
# Real-world iCloud vCard shapes
#
# Fixtures below mirror bytes observed on a live iCloud account: CRLF endings,
# an empty FN with the name in the structured N property, and TEL values with a
# metadata property concatenated on by whatever wrote the card.
# --------------------------------------------------------------------------

REAL_EMPTY_FN = (
    "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:\r\nN:Whitfield;Dana;;;\r\n"
    "UID:11111111-2222-3333-4444-555555555555\r\n"
    "PRODID:-//Apple Inc.//macOS 26.5//EN\r\nREV:2026-05-20T15:13:17Z\r\nORG:;\r\n"
    "TEL:+12025550143X-SHARED-PHOTO-DISPLAY-PREF:ALWAYS_ASK\r\nEND:VCARD\r\n"
)


def test_empty_fn_falls_back_to_structured_name():
    install_http(
        FakeResponse(collections_xml("card")),
        FakeResponse(report_xml(REAL_EMPTY_FN)),
    )
    assert server.search_contacts()[0]["name"] == "Dana Whitfield"


def test_embedded_property_is_stripped_from_phone_number():
    install_http(
        FakeResponse(collections_xml("card")),
        FakeResponse(report_xml(REAL_EMPTY_FN)),
    )
    assert server.search_contacts()[0]["phones"] == ["+12025550143"]


def test_fn_still_wins_when_present():
    install_http(
        FakeResponse(collections_xml("card")),
        FakeResponse(report_xml(vcard("Mira Patel", n="Patel;Mira;;;"))),
    )
    assert server.search_contacts()[0]["name"] == "Mira Patel"


def test_structured_name_collapses_empty_components():
    card = "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:\r\nN:Okafor;Alex;;;\r\nEND:VCARD\r\n"
    install_http(FakeResponse(collections_xml("card")), FakeResponse(report_xml(card)))
    # Not "Alex  Okafor" -- the blank middle/prefix/suffix slots must collapse.
    assert server.search_contacts()[0]["name"] == "Alex Okafor"


def test_full_structured_name_is_ordered_correctly():
    card = "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:\r\nN:Whitfield;Dana;Q;Dr.;Jr.\r\nEND:VCARD\r\n"
    install_http(FakeResponse(collections_xml("card")), FakeResponse(report_xml(card)))
    assert server.search_contacts()[0]["name"] == "Dr. Dana Q Whitfield Jr."


def test_organization_is_returned():
    install_http(
        FakeResponse(collections_xml("card")),
        FakeResponse(report_xml(vcard("Jordan Lee", org="Initech;Sales"))),
    )
    assert server.search_contacts()[0]["organization"] == "Initech Sales"


def test_nameless_card_falls_back_to_organization():
    card = "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:\r\nN:;;;;\r\nORG:The Pet Lodge;\r\nEND:VCARD\r\n"
    install_http(FakeResponse(collections_xml("card")), FakeResponse(report_xml(card)))
    assert server.search_contacts()[0]["name"] == "The Pet Lodge"


def test_genuinely_nameless_card_yields_empty_name_not_a_crash():
    card = "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:\r\nTEL:555-0100\r\nEND:VCARD\r\n"
    install_http(FakeResponse(collections_xml("card")), FakeResponse(report_xml(card)))
    result = server.search_contacts()[0]
    assert result["name"] == "" and result["phones"] == ["555-0100"]


@pytest.mark.parametrize("raw,expected", [
    ("+12025550143X-SHARED-PHOTO-DISPLAY-PREF:ALWAYS_ASK", "+12025550143"),
    ("202.555.0151X-SHARED-PHOTO-DISPLAY-PREF:ALWAYS_ASK", "202.555.0151"),
    ("+1 202.555.0164X-SHARED-PHOTO-DISPLAY-PREF:ALWAYS_ASK", "+1 202.555.0164"),
])
def test_known_corrupt_phone_values_are_repaired(raw, expected):
    assert server._clean_value(raw) == (expected, True)


@pytest.mark.parametrize("value", [
    "+1 202-555-0147",
    "(202) 555-0152;526",   # a real extension, not corruption
    "1-800-MY-APPLE",       # vanity number, must survive intact
    "2025550168",
])
def test_legitimate_phone_values_are_left_alone(value):
    assert server._clean_value(value) == (value, False)


def test_crlf_vcards_parse():
    install_http(
        FakeResponse(collections_xml("card")),
        FakeResponse(report_xml(vcard("Ann", ["a@b.com"], ["555-1234"]))),
    )
    assert server.search_contacts() == [
        {"id": "uid-Ann", "name": "Ann", "organization": "",
         "emails": ["a@b.com"], "phones": ["555-1234"]}
    ]
