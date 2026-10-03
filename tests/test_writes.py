"""Write-path tests. Nothing here touches a real account."""
import os

import pytest
from fastmcp.exceptions import ToolError

import server
from fakes import HOME_XML, PRINCIPAL_XML, FakeHTTP, FakeResponse, collections_xml, report_xml, vcard

# The real corrupt shape observed on a live account.
CORRUPT = (
    "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:\r\nN:Whitfield;Dana;;;\r\n"
    "UID:11111111-2222-3333-4444-555555555555\r\n"
    "PRODID:-//Apple Inc.//macOS 26.5//EN\r\nREV:2026-05-20T15:13:17Z\r\nORG:;\r\n"
    # Deliberately truncated base64: vobject cannot decode this card, and the
    # repair must still work. Real cards do contain photos.
    "PHOTO;ENCODING=b;TYPE=JPEG:/9j/4AAQSkZJRgABAQAAAQABAAD\r\n"
    " AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\r\n"
    "TEL:+12025550143X-SHARED-PHOTO-DISPLAY-PREF:ALWAYS_ASK\r\nEND:VCARD\r\n"
)


def install(*extra, **kwargs):
    http = FakeHTTP([FakeResponse(PRINCIPAL_XML), FakeResponse(HOME_XML), *extra], **kwargs)
    server.requests.request = http
    return http


@pytest.fixture(autouse=True)
def isolate_backups(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "BACKUP_DIR", str(tmp_path / "backups"))


# --------------------------------------------------------------------------
# Backup location
# --------------------------------------------------------------------------

def test_backups_live_beside_a_source_checkout(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "server.py").write_text("")
    assert server._default_backup_dir(str(tmp_path)) == str(tmp_path / "backups")


def test_backups_never_land_inside_an_installed_package(tmp_path):
    # An installed module sits in site-packages; PROJECT_ROOT is then its parent.
    site = tmp_path / "lib" / "python3.14" / "site-packages"
    site.mkdir(parents=True)
    result = server._default_backup_dir(str(site.parent))
    assert result == os.path.join(os.path.expanduser("~"), ".honeycrisp", "backups")
    assert not result.startswith(str(tmp_path))


# --------------------------------------------------------------------------
# repair_contacts
# --------------------------------------------------------------------------

def test_dry_run_is_the_default_and_writes_nothing():
    http = install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(CORRUPT)))
    result = server.repair_contacts()
    assert result["dry_run"] is True
    assert result["needing_repair"] == 1
    assert result["written"] == 0
    assert http.writes == [], "dry run must not issue a PUT"


def test_repair_fixes_both_defects_in_one_card():
    install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(CORRUPT)))
    result = server.repair_contacts()
    changes = " ".join(result["contacts"][0]["changes"])
    assert "FN" in changes and "Dana Whitfield" in changes
    assert "TEL" in changes
    assert result["contacts"][0]["name"] == "Dana Whitfield"


def test_applied_repair_sends_the_corrected_card():
    http = install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(CORRUPT)))
    result = server.repair_contacts(dry_run=False)
    assert result["written"] == 1
    body = http.writes[0]["body"]
    assert "FN:Dana Whitfield\r\n" in body
    assert "TEL:+12025550143\r\n" in body
    # The stripped metadata is preserved as its own property, not discarded.
    assert "X-SHARED-PHOTO-DISPLAY-PREF:ALWAYS_ASK\r\n" in body


def test_untouched_lines_survive_byte_identical():
    http = install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(CORRUPT)))
    server.repair_contacts(dry_run=False)
    body = http.writes[0]["body"]
    for line in ("UID:11111111-2222-3333-4444-555555555555",
                 "PRODID:-//Apple Inc.//macOS 26.5//EN",
                 "REV:2026-05-20T15:13:17Z",
                 "PHOTO;ENCODING=b;TYPE=JPEG:/9j/4AAQSkZJRgABAQAAAQABAAD",
                 " AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"):
        assert line + "\r\n" in body, f"lost: {line!r}"


def test_write_is_guarded_by_an_if_match_etag():
    http = install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(CORRUPT)))
    server.repair_contacts(dry_run=False)
    assert http.writes[0]["if_match"] == '"etag-0"'


def test_concurrent_modification_is_reported_not_clobbered():
    install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(CORRUPT)), put_status=412)
    result = server.repair_contacts(dry_run=False)
    assert result["written"] == 0
    assert "changed on the server" in result["failures"][0]["error"]


def test_backup_is_written_before_changes_are_applied():
    install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(CORRUPT)))
    result = server.repair_contacts(dry_run=False)
    saved = open(result["backup"], encoding="utf-8", newline="").read()
    # The backup holds the ORIGINAL, so the change is reversible.
    assert "TEL:+12025550143X-SHARED-PHOTO-DISPLAY-PREF:ALWAYS_ASK" in saved
    assert "FN:\r\n" in saved


def test_healthy_cards_are_left_alone():
    http = install(FakeResponse(collections_xml("card")),
                   FakeResponse(report_xml(vcard("Ann", ["a@b.com"], ["555-1234"]))))
    result = server.repair_contacts(dry_run=False)
    assert result["needing_repair"] == 0 and http.writes == []


def test_repair_is_idempotent():
    once = server._plan_card_repairs(CORRUPT)[0]
    twice, changes = server._plan_card_repairs(once)
    assert changes == [] and twice == once


def test_repair_limit_is_respected():
    install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(CORRUPT, CORRUPT, CORRUPT)))
    assert server.repair_contacts(limit=2)["needing_repair"] == 2


def test_update_of_a_card_vobject_cannot_parse_succeeds_and_reports_it():
    # Previously the PUT went through and the result-building _summarise then
    # raised, so a successful edit was reported as a failure (and a retry 412d).
    http = install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(CORRUPT)))
    with pytest.raises(Exception):
        server._summarise(CORRUPT)
    result = server.update_contact("11111111-2222-3333-4444-555555555555", name="Dana Whitfield", dry_run=False)
    assert result["changed"] is True
    assert result["changes"] == ["FN '' -> 'Dana Whitfield'"]
    assert result["before"]["name"] == "Dana Whitfield", "fell back to N on the lines"
    assert result["after"]["name"] == "Dana Whitfield"
    assert result["after"]["phones"] == ["+12025550143"]
    [write] = http.writes
    assert "FN:Dana Whitfield\r\n" in write["body"]


# --------------------------------------------------------------------------
# update_contact
# --------------------------------------------------------------------------

CARD = vcard("Ann Smith", ["a@work.com"], ["555-1111"], uid="U1")


def test_update_requires_at_least_one_field():
    with pytest.raises(ToolError, match="Nothing to update"):
        server.update_contact("U1")


def test_unknown_contact_id_raises():
    install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(CARD)))
    with pytest.raises(ToolError, match="No contact found"):
        server.update_contact("nope", name="X")


def test_update_dry_run_writes_nothing_but_shows_the_diff():
    http = install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(CARD)))
    result = server.update_contact("U1", name="Ann Smith-Jones")
    assert http.writes == []
    assert result["before"]["name"] == "Ann Smith"
    assert result["after"]["name"] == "Ann Smith-Jones"


def test_update_name_is_applied():
    http = install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(CARD)))
    server.update_contact("U1", name="Ann Smith-Jones", dry_run=False)
    assert "FN:Ann Smith-Jones\r\n" in http.writes[0]["body"]


def test_replacing_phones_keeps_type_labels_on_retained_numbers():
    card = ("BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Ann\r\nUID:U2\r\n"
            "TEL;type=CELL;type=pref:555-1111\r\nTEL;type=WORK:555-9999\r\nEND:VCARD\r\n")
    http = install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(card)))
    server.update_contact("U2", phones=["555-1111", "555-3333"], dry_run=False)
    body = http.writes[0]["body"]
    assert "TEL;type=CELL;type=pref:555-1111\r\n" in body   # kept, params intact
    assert "555-9999" not in body                            # dropped
    assert "TEL:555-3333\r\n" in body                        # added


def test_update_finds_the_card_by_direct_get_and_skips_the_report():
    # iCloud serves every card at <book>/<UID>.vcf, so one small GET replaces
    # a REPORT that would pull the whole address book (photos included) down.
    http = install(FakeResponse(collections_xml("card")),
                   cards={"U1.vcf": FakeResponse(CARD, status_code=200, headers={"ETag": '"etag-1"'})})
    server.update_contact("U1", name="Ann Smith-Jones", dry_run=False)
    assert [c["method"] for c in http.calls].count("REPORT") == 0
    [get] = [c for c in http.calls if c["method"] == "GET"]
    assert get["url"].endswith("/card/U1.vcf")
    [write] = http.writes
    assert write["url"].endswith("/card/U1.vcf") and write["if_match"] == '"etag-1"'
    assert "FN:Ann Smith-Jones\r\n" in write["body"]


def test_update_falls_back_to_the_report_when_the_url_does_not_match():
    # A card stored under some other file name (another client's choice) is
    # still found, just the slow way.
    http = install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(CARD)))
    server.update_contact("U1", name="Ann Smith-Jones", dry_run=False)
    methods = [c["method"] for c in http.calls]
    assert methods.count("GET") == 1 and methods.count("REPORT") == 1
    assert http.writes[0]["url"].endswith("/card/0.vcf")


def test_direct_get_that_returns_the_wrong_card_is_not_trusted():
    other = vcard("Someone Else", uid="U9")
    http = install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(CARD)),
                   cards={"U1.vcf": FakeResponse(other, status_code=200)})
    server.update_contact("U1", name="Ann Smith-Jones", dry_run=False)
    assert "UID:U1" in http.writes[0]["body"]


def test_uid_is_quoted_into_the_card_url():
    http = install(FakeResponse(collections_xml("card")), FakeResponse(report_xml()))
    with pytest.raises(ToolError, match="No contact found"):
        server.update_contact("../../etc/passwd", name="X")
    [get] = [c for c in http.calls if c["method"] == "GET"]
    assert get["url"].endswith("/card/..%2F..%2Fetc%2Fpasswd.vcf")


def test_update_only_touches_requested_fields():
    http = install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(CARD)))
    server.update_contact("U1", name="New Name", dry_run=False)
    body = http.writes[0]["body"]
    assert "EMAIL;TYPE=INTERNET:a@work.com\r\n" in body
    assert "TEL:555-1111\r\n" in body


def test_no_op_update_reports_no_change_and_writes_nothing():
    http = install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(CARD)))
    result = server.update_contact("U1", name="Ann Smith", dry_run=False)
    assert result["changed"] is False and result["changes"] == [] and http.writes == []


@pytest.mark.parametrize("name,expected", [
    ("Smith; Jones", r"FN:Smith\; Jones"),
    ("A, B", r"FN:A\, B"),
    ("back\\slash", r"FN:back\\slash"),
])
def test_special_characters_in_names_are_vcard_escaped(name, expected):
    http = install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(CARD)))
    server.update_contact("U1", name=name, dry_run=False)
    assert expected in http.writes[0]["body"]


def test_repair_can_target_a_single_contact():
    other = vcard("Ann", phones=["555-1111"], uid="OTHER")
    http = install(FakeResponse(collections_xml("card")),
                   FakeResponse(report_xml(CORRUPT, other)))
    result = server.repair_contacts(
        dry_run=False, contact_ids=["11111111-2222-3333-4444-555555555555"])
    assert result["scanned"] == 1
    assert result["written"] == 1
    assert len(http.writes) == 1
    assert "FN:Dana Whitfield" in http.writes[0]["body"]


def test_repair_with_unknown_contact_id_writes_nothing():
    http = install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(CORRUPT)))
    result = server.repair_contacts(dry_run=False, contact_ids=["does-not-exist"])
    assert result["scanned"] == 0 and result["written"] == 0 and http.writes == []


def test_repair_can_target_a_subset_in_one_pass():
    a = vcard("", phones=["555-1X-SHARED-PHOTO-DISPLAY-PREF:ALWAYS_ASK"], n="One;Ann;;;", uid="A")
    b = vcard("", phones=["555-2X-SHARED-PHOTO-DISPLAY-PREF:ALWAYS_ASK"], n="Two;Bob;;;", uid="B")
    c = vcard("", n="Three;Cat;;;", uid="C")
    http = install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(a, b, c)))
    result = server.repair_contacts(dry_run=False, contact_ids=["A", "B"])
    assert result["scanned"] == 2 and result["written"] == 2
    # Exactly one REPORT, not one per targeted card.
    assert len([x for x in http.calls if x["method"] == "REPORT"]) == 1
    assert {w["url"].rsplit("/", 1)[1] for w in http.writes} == {"0.vcf", "1.vcf"}


def test_empty_contact_ids_is_rejected_rather_than_repairing_everything():
    install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(CORRUPT)))
    with pytest.raises(ToolError, match="no usable ids"):
        server.repair_contacts(dry_run=False, contact_ids=[])


# --------------------------------------------------------------------------
# The same glue defect outside TEL/EMAIL (observed in ADR and X-ABLabel)
# --------------------------------------------------------------------------

def _repair(line):
    card = f"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Ann\r\nUID:U\r\n{line}\r\nEND:VCARD\r\n"
    return server._plan_card_repairs(card)


def test_glue_in_adr_is_split_out():
    out, changes = _repair(
        "ADR:;;1 Example Way;Springfield;IL;62701;X-SHARED-PHOTO-DISPLAY-PREF:ALWAYS_ASK")
    assert "ADR:;;1 Example Way;Springfield;IL;62701;\r\n" in out
    assert "X-SHARED-PHOTO-DISPLAY-PREF:ALWAYS_ASK\r\n" in out
    assert len(changes) == 1


def test_adr_keeps_its_trailing_empty_component():
    # ADR has 7 components; the trailing ';' is the empty country field and
    # dropping it would shift the address structure.
    out, _ = _repair("ADR:;;1 Main St;Springfield;MN;55974;X-SHARED-PHOTO-DISPLAY-PREF:ALWAYS_ASK")
    adr = next(l for l in out.split("\r\n") if l.startswith("ADR:"))
    assert adr.count(";") == 6, adr
    assert adr.endswith(";")


def test_glue_in_ab_label_is_split_out_preserving_case():
    out, changes = _repair("item4.X-ABLabel:_$!<Anniversary>!$_X-SHARED-PHOTO-DISPLAY-PREF:ALWAYS_ASK")
    # Apple's mixed-case group prefix must survive verbatim.
    assert "item4.X-ABLabel:_$!<Anniversary>!$_\r\n" in out
    assert "X-SHARED-PHOTO-DISPLAY-PREF:ALWAYS_ASK\r\n" in out
    assert len(changes) == 1


@pytest.mark.parametrize("line", [
    "REV:2026-05-20T15:13:17Z",
    "URL:https://example.com/a:b",
    "NOTE:Reminder: call him back at 3pm",
    "PHOTO;ENCODING=b;TYPE=JPEG:/9j/4AAQSkZJRgABAQ",
    "item1.URL:https://www.linkedin.com/in/someone",
    "X-SOCIALPROFILE;type=twitter:https://twitter.com/someone",
    "X-SHARED-PHOTO-DISPLAY-PREF:ALWAYS_ASK",
])
def test_values_that_legitimately_contain_colons_are_untouched(line):
    out, changes = _repair(line)
    assert changes == [], f"would have corrupted: {line}"
    assert line + "\r\n" in out


def test_only_allowlisted_property_names_are_treated_as_glue():
    # An unknown token inside a non-TEL value must not trigger a split.
    out, changes = _repair("ADR:;;1 Main St;Town;MN;55974;SOMETHING-ELSE:VALUE")
    assert changes == []
