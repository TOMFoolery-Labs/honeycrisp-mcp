"""create_contact and delete_contact. Nothing here touches a real account."""

import os

import pytest
from fastmcp.exceptions import ToolError

import server
from fakes import HOME_XML, PRINCIPAL_XML, FakeHTTP, FakeResponse, collections_xml, report_xml, vcard


def install(*extra, **kwargs):
    http = FakeHTTP([FakeResponse(PRINCIPAL_XML), FakeResponse(HOME_XML), *extra], **kwargs)
    server.requests.request = http
    return http


@pytest.fixture(autouse=True)
def isolate_backups(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "BACKUP_DIR", str(tmp_path / "backups"))
    return tmp_path / "backups"


# --------------------------------------------------------------------------
# create_contact
# --------------------------------------------------------------------------

def test_create_dry_run_is_the_default_and_writes_nothing():
    http = install(FakeResponse(collections_xml("card")))
    result = server.create_contact(name="Dana Whitfield", emails=["dana@example.com"])
    assert result["dry_run"] is True and result["created"] is False
    assert result["addressbook"] == "card"
    assert result["contact"]["name"] == "Dana Whitfield"
    assert result["contact"]["emails"] == ["dana@example.com"]
    assert len(result["contact"]["id"]) == 36
    assert http.writes == []


def test_create_puts_an_apple_shaped_vcard_guarded_by_if_none_match():
    http = install(FakeResponse(collections_xml("card")))
    result = server.create_contact(
        name="Dana Marie Whitfield", phones=["+1 202 555 0143"], emails=["dana@example.com"],
        organization="Smith & Sons, Inc.", dry_run=False,
    )
    assert result["created"] is True
    [write] = http.writes
    uid = result["contact"]["id"]
    assert write["url"] == f"https://p61-contacts.icloud.com:443/123456/carddavhome/card/{uid}.vcf"
    assert write["if_none_match"] == "*" and write["if_match"] is None
    lines = write["body"].split("\r\n")
    assert lines[:3] == ["BEGIN:VCARD", "VERSION:3.0", "PRODID:-//Honeycrisp//EN"]
    assert "N:Whitfield;Dana Marie;;;" in lines
    assert "FN:Dana Marie Whitfield" in lines
    assert "ORG:Smith & Sons\\, Inc.;" in lines
    assert "EMAIL;TYPE=INTERNET:dana@example.com" in lines
    assert "TEL;TYPE=CELL:+1 202 555 0143" in lines
    assert f"UID:{uid}" in lines
    assert any(line.startswith("REV:") for line in lines)
    assert lines[-2:] == ["END:VCARD", ""], "CRLF terminated, like the cards Apple stores"


def test_organization_only_contact_uses_it_as_the_display_name():
    http = install(FakeResponse(collections_xml("card")))
    result = server.create_contact(organization="Acme", phones=["555"], dry_run=False)
    lines = http.writes[0]["body"].split("\r\n")
    assert "FN:Acme" in lines and "N:;;;;" in lines
    assert result["contact"]["name"] == "Acme"


def test_single_word_name_becomes_the_family_name():
    http = install(FakeResponse(collections_xml("card")))
    server.create_contact(name="Cher", dry_run=False)
    assert "N:Cher;;;;" in http.writes[0]["body"].split("\r\n")


def test_blank_entries_are_dropped_and_bad_emails_rejected():
    http = install(FakeResponse(collections_xml("card")))
    result = server.create_contact(name="A B", phones=["", "  ", "555"], emails=[" a@b.co "])
    assert result["contact"]["phones"] == ["555"] and result["contact"]["emails"] == ["a@b.co"]
    install(FakeResponse(collections_xml("card")))
    with pytest.raises(ToolError, match="Invalid email address 'nope'"):
        server.create_contact(name="A B", emails=["nope"])
    assert http.writes == []


def test_name_or_organization_is_required():
    install(FakeResponse(collections_xml("card")))
    with pytest.raises(ToolError, match="name or an organization"):
        server.create_contact(phones=["555"])


def test_multiple_address_books_require_an_explicit_choice():
    install(FakeResponse(collections_xml("card", "work")))
    with pytest.raises(ToolError, match="several address books.*'card'.*'work'"):
        server.create_contact(name="A B")


def test_named_address_book_is_used():
    http = install(FakeResponse(collections_xml("card", "work")))
    server.create_contact(name="A B", addressbook="work", dry_run=False)
    assert "/carddavhome/work/" in http.writes[0]["url"]
    install(FakeResponse(collections_xml("card", "work")))
    with pytest.raises(ToolError, match="No address book named 'nope'"):
        server.create_contact(name="A B", addressbook="nope")


def test_existing_card_at_the_url_is_not_overwritten():
    install(FakeResponse(collections_xml("card")), put_status=412)
    with pytest.raises(ToolError, match="already exists"):
        server.create_contact(name="A B", dry_run=False)


def test_server_rejection_is_a_tool_error():
    install(FakeResponse(collections_xml("card")), put_status=403)
    with pytest.raises(ToolError, match="failed \\(403\\)"):
        server.create_contact(name="A B", dry_run=False)


# --------------------------------------------------------------------------
# delete_contact
# --------------------------------------------------------------------------

CARD = vcard("Dana Whitfield", emails=("dana@example.com",), uid="UID-DANA")
OTHER = vcard("Someone Else", uid="UID-OTHER")


def test_delete_dry_run_is_the_default_and_touches_nothing(isolate_backups):
    http = install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(OTHER, CARD)))
    result = server.delete_contact("UID-DANA")
    assert result == {
        "dry_run": True, "deleted": False, "backup": None,
        "contact": {"id": "UID-DANA", "name": "Dana Whitfield", "organization": "",
                    "emails": ["dana@example.com"], "phones": []},
    }
    assert http.deletes == [] and not isolate_backups.exists()


def test_delete_backs_up_then_sends_delete_with_if_match(isolate_backups):
    http = install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(OTHER, CARD)))
    result = server.delete_contact("UID-DANA", dry_run=False)
    assert result["deleted"] is True
    [delete] = http.deletes
    assert delete["url"].endswith("/carddavhome/card/1.vcf")
    assert delete["if_match"] == '"etag-1"'
    [backup] = os.listdir(isolate_backups)
    saved = open(os.path.join(isolate_backups, backup), newline="").read()
    assert "UID:UID-DANA" in saved and "UID:UID-OTHER" not in saved


def test_delete_of_a_card_changed_elsewhere_is_refused():
    install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(CARD)), delete_status=412)
    with pytest.raises(ToolError, match="changed on the server"):
        server.delete_contact("UID-DANA", dry_run=False)


def test_delete_unknown_id_raises():
    http = install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(CARD)))
    with pytest.raises(ToolError, match="No contact found with id 'nope'"):
        server.delete_contact("nope", dry_run=False)
    assert http.deletes == []


def test_blank_contact_id_is_rejected():
    install()
    with pytest.raises(ToolError, match="contact_id is required"):
        server.delete_contact(" ")


def test_delete_failure_is_a_tool_error():
    install(FakeResponse(collections_xml("card")), FakeResponse(report_xml(CARD)), delete_status=403)
    with pytest.raises(ToolError, match="failed \\(403\\)"):
        server.delete_contact("UID-DANA", dry_run=False)
