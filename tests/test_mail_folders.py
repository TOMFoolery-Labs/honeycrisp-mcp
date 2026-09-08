"""list_folders, move_emails and mark_emails."""

import pytest
from fastmcp.exceptions import ToolError
from imapclient.imapclient import SENT, TRASH

import server
from fakes import Address, Envelope, FakeIMAP


@pytest.fixture(autouse=True)
def reset_imap_cache():
    server._imap_client = None
    yield
    server._imap_client = None


def message(subject):
    return {b"ENVELOPE": Envelope(subject=subject.encode(), from_=[Address(None, b"ann", b"example.com")])}


def mailbox(**kwargs):
    kwargs.setdefault("uids", [1, 2, 3])
    kwargs.setdefault("messages", {u: message(f"S{u}") for u in kwargs["uids"]})
    kwargs.setdefault("folders", ("INBOX", "Archive", "Deleted Messages", "Sent Messages"))
    kwargs.setdefault("special", {TRASH: "Deleted Messages", SENT: "Sent Messages"})
    client = FakeIMAP(**kwargs)
    server._connect_imap = lambda: client
    return client


def tuples(client, name):
    return [c for c in client.calls if isinstance(c, tuple) and c[0] == name]


# --------------------------------------------------------------------------
# list_folders
# --------------------------------------------------------------------------

def test_list_folders_reports_roles_and_counts():
    client = mailbox(folders=("INBOX", "Deleted Messages", "Sent Messages", "Projects", "Projects/Old"))
    client.folder_info = {
        "INBOX": ((b"\\HasNoChildren",), 120, 4),
        "Deleted Messages": ((b"\\Trash", b"\\HasNoChildren"), 9, 0),
        "Sent Messages": ((b"\\Sent",), 50, 0),
        "Projects": ((b"\\Noselect", b"\\HasChildren"), 0, 0),
        "Projects/Old": ((), 3, 1),
    }
    result = server.list_folders()
    by_name = {f["name"]: f for f in result}
    assert by_name["INBOX"] == {"name": "INBOX", "role": "inbox", "selectable": True, "total": 120, "unseen": 4}
    assert by_name["Deleted Messages"]["role"] == "trash"
    assert by_name["Sent Messages"]["role"] == "sent"
    assert by_name["Projects/Old"] == {"name": "Projects/Old", "role": "", "selectable": True, "total": 3, "unseen": 1}
    assert by_name["Projects"] == {"name": "Projects", "role": "", "selectable": False, "total": None, "unseen": None}
    # No STATUS is issued for a container that cannot be selected.
    assert "Projects" not in [c[1] for c in tuples(client, "folder_status")]


def test_list_folders_survives_a_failed_status():
    client = mailbox(folders=("INBOX", "Odd"))
    real_status = client.folder_status

    def status(folder, what=None):
        if folder == "Odd":
            raise Exception("STATUS failed")
        return real_status(folder, what)

    client.folder_status = status
    result = {f["name"]: f for f in server.list_folders()}
    assert result["Odd"]["total"] is None and result["INBOX"]["total"] == 0


def test_list_folders_error_is_a_tool_error():
    client = mailbox()
    client.list_folders = lambda *a, **k: (_ for _ in ()).throw(Exception("LIST failed"))
    with pytest.raises(ToolError, match="Could not list folders"):
        server.list_folders()


# --------------------------------------------------------------------------
# move_emails
# --------------------------------------------------------------------------

def test_move_dry_run_is_the_default_and_touches_nothing():
    client = mailbox()
    result = server.move_emails(message_ids=["2"], to_folder="Archive")
    assert result == {
        "dry_run": True, "moved": False, "folder": "INBOX", "destination": "Archive",
        "messages": [{"id": "2", "from": "ann@example.com", "subject": "S2", "date": "2026-08-17T10:00:00+00:00"}],
    }
    assert ("select_folder", "INBOX", True) in client.calls
    assert not client.moved and client.uids == [1, 2, 3]


def test_live_move_uses_a_single_move():
    client = mailbox()
    result = server.move_emails(message_ids=["3", "1"], to_folder="Archive", dry_run=False)
    assert result["moved"] is True
    assert client.moved == [((1, 3), "Archive")] and client.uids == [2]
    assert ("select_folder", "INBOX", False) in client.calls


def test_move_without_move_capability_copies_flags_and_expunges():
    client = mailbox(capabilities=("UIDPLUS",))
    server.move_emails(message_ids=["2"], to_folder="Archive", dry_run=False)
    assert client.copied == [((2,), "Archive")]
    assert client.flagged_deleted == [2] and client.expunged == [2]


def test_move_restores_from_trash():
    client = mailbox()
    server.move_emails(message_ids=["1"], folder="Deleted Messages", to_folder="INBOX", dry_run=False)
    assert client.moved == [((1,), "INBOX")]


def test_move_to_missing_destination_is_rejected_before_opening_the_source():
    client = mailbox()
    with pytest.raises(ToolError, match="Nope.*does not exist"):
        server.move_emails(message_ids=["1"], to_folder="Nope", dry_run=False)
    assert not tuples(client, "select_folder") and not client.moved


def test_move_to_same_folder_is_rejected():
    mailbox()
    with pytest.raises(ToolError, match="already in"):
        server.move_emails(message_ids=["1"], to_folder="INBOX")


def test_move_with_empty_or_unknown_ids_is_rejected():
    client = mailbox()
    with pytest.raises(ToolError, match="message_ids is empty"):
        server.move_emails(message_ids=[], to_folder="Archive", dry_run=False)
    with pytest.raises(ToolError, match="99"):
        server.move_emails(message_ids=["1", "99"], to_folder="Archive", dry_run=False)
    assert not client.moved


# --------------------------------------------------------------------------
# mark_emails
# --------------------------------------------------------------------------

def test_mark_requires_something_to_change():
    client = mailbox()
    with pytest.raises(ToolError, match="Nothing to do"):
        server.mark_emails(message_ids=["1"])
    assert not tuples(client, "select_folder")


def test_mark_dry_run_reports_the_flag_changes_without_applying():
    client = mailbox()
    result = server.mark_emails(message_ids=["1", "2"], read=True, flagged=False)
    assert result["dry_run"] is True and result["changed"] is False
    assert result["add_flags"] == ["\\Seen"] and result["remove_flags"] == ["\\Flagged"]
    assert [m["id"] for m in result["messages"]] == ["1", "2"]
    assert not client.flags_added and not client.flags_removed
    assert ("select_folder", "INBOX", True) in client.calls


def test_mark_read_and_unread_live():
    client = mailbox()
    server.mark_emails(message_ids=["1"], read=True, dry_run=False)
    server.mark_emails(message_ids=["2"], read=False, dry_run=False)
    assert client.flags_added == [((1,), (b"\\Seen",))]
    assert client.flags_removed == [((2,), (b"\\Seen",))]


def test_mark_flagged_only_leaves_seen_alone():
    client = mailbox()
    result = server.mark_emails(message_ids=["3"], flagged=True, dry_run=False)
    assert client.flags_added == [((3,), (b"\\Flagged",))] and not client.flags_removed
    assert result["add_flags"] == ["\\Flagged"] and result["remove_flags"] == []


def test_mark_unknown_id_changes_nothing():
    client = mailbox()
    with pytest.raises(ToolError, match="99"):
        server.mark_emails(message_ids=["1", "99"], read=True, dry_run=False)
    assert not client.flags_added
