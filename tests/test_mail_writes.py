"""delete_emails and send_email: the mail write paths."""

import os

import pytest
from fastmcp.exceptions import ToolError
from imapclient.imapclient import SENT, TRASH

import server
from fakes import Address, Envelope, FakeIMAP, FakeSMTP


@pytest.fixture(autouse=True)
def reset_imap_cache():
    server._imap_client = None
    yield
    server._imap_client = None


@pytest.fixture(autouse=True)
def isolated_backups(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "BACKUP_DIR", str(tmp_path / "backups"))
    return tmp_path / "backups"


def use(client):
    server._connect_imap = lambda: client
    return client


def message(subject, raw=b"", sender="ann"):
    envelope = Envelope(subject=subject.encode(), from_=[Address(None, sender.encode(), b"example.com")])
    return {b"ENVELOPE": envelope, b"BODY[]": raw}


def mailbox(**kwargs):
    kwargs.setdefault("uids", [1, 2, 3])
    kwargs.setdefault("messages", {u: message(f"S{u}", raw=f"Body {u}".encode()) for u in kwargs["uids"]})
    kwargs.setdefault("folders", ("INBOX", "Deleted Messages", "Sent Messages"))
    kwargs.setdefault("special", {TRASH: "Deleted Messages", SENT: "Sent Messages"})
    return use(FakeIMAP(**kwargs))


def tuples(client, name):
    return [c for c in client.calls if isinstance(c, tuple) and c[0] == name]


# --------------------------------------------------------------------------
# delete_emails: validation
# --------------------------------------------------------------------------

def test_empty_id_list_raises_rather_than_matching_everything():
    client = mailbox()
    with pytest.raises(ToolError, match="message_ids is empty"):
        server.delete_emails(message_ids=[])
    assert not client.moved and not client.expunged
    assert not tuples(client, "select_folder"), "must fail before touching the mailbox"


@pytest.mark.parametrize("bad", ["", "abc", "1 OR 2", "*"])
def test_non_numeric_ids_are_rejected(bad):
    client = mailbox()
    with pytest.raises(ToolError, match="Invalid message id"):
        server.delete_emails(message_ids=[bad], dry_run=False)
    assert not client.moved and not client.expunged


def test_unknown_id_aborts_the_whole_request():
    client = mailbox()
    with pytest.raises(ToolError) as excinfo:
        server.delete_emails(message_ids=["1", "99"], dry_run=False)
    assert "99" in str(excinfo.value) and "INBOX" in str(excinfo.value)
    # The valid id must not be acted on while the request is partially wrong.
    assert not client.moved and not client.expunged and client.uids == [1, 2, 3]


def test_missing_folder_reports_the_folder_name():
    mailbox()
    with pytest.raises(ToolError, match="Nope"):
        server.delete_emails(message_ids=["1"], folder="Nope")


# --------------------------------------------------------------------------
# delete_emails: dry run
# --------------------------------------------------------------------------

def test_dry_run_is_the_default_and_touches_nothing():
    client = mailbox()
    result = server.delete_emails(message_ids=["2", "1"])
    assert result["dry_run"] is True and result["deleted"] is False
    assert result["action"] == "move_to_trash"
    assert result["destination"] == "Deleted Messages"
    assert [m["id"] for m in result["messages"]] == ["1", "2"]
    assert result["messages"][0] == {
        "id": "1", "from": "ann@example.com", "subject": "S1", "date": "2026-08-17T10:00:00+00:00",
    }
    assert ("select_folder", "INBOX", True) in client.calls, "dry run opens the folder read-only"
    assert not client.moved and not client.expunged and not client.flagged_deleted
    assert client.uids == [1, 2, 3]


def test_dry_run_still_validates_ids():
    mailbox()
    with pytest.raises(ToolError, match="99"):
        server.delete_emails(message_ids=["99"])


# --------------------------------------------------------------------------
# delete_emails: move to Trash
# --------------------------------------------------------------------------

def test_live_delete_moves_to_trash_with_a_single_move():
    client = mailbox()
    result = server.delete_emails(message_ids=["3", "1"], dry_run=False)
    assert result["deleted"] is True and result["dry_run"] is False
    assert result["backup"] is None, "Trash is the backup; nothing is written locally"
    assert ("select_folder", "INBOX", False) in client.calls
    assert client.moved == [((1, 3), "Deleted Messages")]
    assert client.uids == [2]
    assert not client.expunged, "a MOVE must not be followed by an expunge"


def test_without_move_capability_falls_back_to_copy_flag_and_uid_expunge():
    client = mailbox(capabilities=("UIDPLUS",))
    server.delete_emails(message_ids=["2"], dry_run=False)
    assert client.copied == [((2,), "Deleted Messages")]
    assert client.flagged_deleted == [2]
    assert client.expunged == [2]
    # The copy must land before the message is flagged for removal.
    names = [c[0] for c in client.calls if isinstance(c, tuple)]
    assert names.index("copy") < names.index("delete_messages") < names.index("uid_expunge")


def test_deleting_from_trash_requires_permanent():
    client = mailbox()
    with pytest.raises(ToolError, match="permanent=True"):
        server.delete_emails(message_ids=["1"], folder="Deleted Messages", dry_run=False)
    assert not client.moved and not client.expunged


def test_unlocatable_trash_folder_is_an_error_not_a_permanent_delete():
    client = mailbox(special={})
    with pytest.raises(ToolError, match="Trash"):
        server.delete_emails(message_ids=["1"], dry_run=False)
    assert not client.moved and not client.expunged and not client.flagged_deleted


# --------------------------------------------------------------------------
# delete_emails: permanent
# --------------------------------------------------------------------------

def test_permanent_delete_backs_up_then_expunges_only_those_uids(isolated_backups):
    client = mailbox()
    result = server.delete_emails(message_ids=["1", "3"], permanent=True, dry_run=False)
    assert result["action"] == "delete_permanently" and result["destination"] is None
    assert result["backup"] and result["backup"].startswith(str(isolated_backups))
    saved = sorted(os.listdir(result["backup"]))
    assert saved == ["INBOX-1.eml", "INBOX-3.eml"]
    assert open(os.path.join(result["backup"], "INBOX-3.eml"), "rb").read() == b"Body 3"

    assert client.flagged_deleted == [1, 3]
    assert client.expunged == [1, 3] and client.uids == [2]
    assert not client.moved and not client.copied
    names = [c[0] for c in client.calls if isinstance(c, tuple)]
    assert names.index("fetch") < names.index("delete_messages"), "backup happens before the flag"


def test_permanent_backup_fetch_uses_peek():
    client = mailbox()
    server.delete_emails(message_ids=["1"], permanent=True, dry_run=False)
    parts = [c[2] for c in tuples(client, "fetch")]
    assert ("BODY.PEEK[]",) in parts


def test_permanent_delete_works_from_trash():
    client = mailbox()
    server.delete_emails(message_ids=["2"], folder="Deleted Messages", permanent=True, dry_run=False)
    assert client.expunged == [2]


def test_permanent_dry_run_writes_no_backup(isolated_backups):
    client = mailbox()
    result = server.delete_emails(message_ids=["1"], permanent=True)
    assert result["backup"] is None and not isolated_backups.exists()
    assert not client.flagged_deleted


def test_without_uidplus_falls_back_to_plain_expunge():
    client = mailbox(capabilities=("MOVE",))
    server.delete_emails(message_ids=["1"], permanent=True, dry_run=False)
    assert ("expunge", None) in client.calls
    assert client.expunged == [1]


# --------------------------------------------------------------------------
# send_email: validation and dry run
# --------------------------------------------------------------------------

def no_smtp():
    def fail():
        raise AssertionError("SMTP must not be contacted")
    server._connect_smtp = fail


def test_send_dry_run_is_the_default_and_renders_without_connecting():
    no_smtp()
    result = server.send_email(to=["Ann <ann@example.com>"], subject="  Hi  ", body="Hello Ann")
    assert result == {
        "dry_run": True,
        "sent": False,
        "from": "test@icloud.com",
        "to": ["Ann <ann@example.com>"],
        "cc": [],
        "bcc": [],
        "subject": "Hi",
        "body": "Hello Ann",
        "in_reply_to": "",
        "references": "",
    }


@pytest.mark.parametrize("bad", ["not-an-address", "two words@example.com", "", "@example.com"])
def test_invalid_recipient_is_rejected(bad):
    no_smtp()
    with pytest.raises(ToolError, match="Invalid to address|At least one"):
        server.send_email(to=[bad], subject="s", body="b", dry_run=False)


def test_invalid_cc_and_bcc_are_rejected():
    no_smtp()
    with pytest.raises(ToolError, match="Invalid cc address"):
        server.send_email(to=["a@example.com"], cc=["nope"], subject="s", body="b")
    with pytest.raises(ToolError, match="Invalid bcc address"):
        server.send_email(to=["a@example.com"], bcc=["nope"], subject="s", body="b")


def test_empty_to_subject_and_body_are_rejected():
    no_smtp()
    with pytest.raises(ToolError, match="At least one 'to'"):
        server.send_email(to=[], subject="s", body="b")
    with pytest.raises(ToolError, match="subject is required"):
        server.send_email(to=["a@example.com"], subject="  ", body="b")
    with pytest.raises(ToolError, match="body is required"):
        server.send_email(to=["a@example.com"], subject="s", body="")


# --------------------------------------------------------------------------
# send_email: live
# --------------------------------------------------------------------------

def test_live_send_delivers_to_every_recipient_and_files_a_copy_in_sent():
    smtp = FakeSMTP()
    server._connect_smtp = lambda: smtp
    imap = mailbox()

    result = server.send_email(
        to=["ann@example.com"], cc=["Bob <bob@example.com>"], bcc=["carol@example.com"],
        subject="Plans", body="See you Tuesday.\nCody", dry_run=False,
    )
    assert result["sent"] is True and result["dry_run"] is False
    assert result["saved_to_sent"] is True
    assert smtp.quit_called

    [delivery] = smtp.sent
    assert delivery["from"] == "test@icloud.com"
    assert delivery["to"] == ["ann@example.com", "Bob <bob@example.com>", "carol@example.com"]
    msg = delivery["message"]
    assert msg["From"] == "test@icloud.com"
    assert msg["To"] == "ann@example.com"
    assert msg["Cc"] == "Bob <bob@example.com>"
    assert msg["Bcc"] is None, "Bcc recipients must never appear in the headers"
    assert msg["Subject"] == "Plans"
    assert msg["Date"] and msg["Message-ID"].endswith("@icloud.com>")
    assert msg.get_content() == "See you Tuesday.\nCody\n"

    [(folder, raw, flags)] = imap.appended
    assert folder == "Sent Messages"
    assert flags == (imapclient_seen(),)
    assert b"Subject: Plans\r\n" in raw, "IMAP APPEND needs CRLF line endings"
    assert b"carol@example.com" not in raw


def imapclient_seen():
    return server.imapclient.SEEN


def test_smtp_rejection_raises_and_files_nothing():
    smtp = FakeSMTP(error=server.smtplib.SMTPRecipientsRefused({"ann@example.com": (550, b"no")}))
    server._connect_smtp = lambda: smtp
    imap = mailbox()
    with pytest.raises(ToolError, match="SMTP rejected"):
        server.send_email(to=["ann@example.com"], subject="s", body="b", dry_run=False)
    assert smtp.quit_called, "connection leaked on the error path"
    assert not imap.appended


def test_sent_folder_failure_is_reported_not_raised():
    smtp = FakeSMTP()
    server._connect_smtp = lambda: smtp
    imap = mailbox()
    imap.append = lambda *a, **k: (_ for _ in ()).throw(OSError("append failed"))
    result = server.send_email(to=["ann@example.com"], subject="s", body="b", dry_run=False)
    assert result["sent"] is True and result["saved_to_sent"] is False
    assert len(smtp.sent) == 1


def test_missing_sent_folder_is_reported_not_raised():
    smtp = FakeSMTP()
    server._connect_smtp = lambda: smtp
    imap = mailbox(special={TRASH: "Deleted Messages"})
    result = server.send_email(to=["ann@example.com"], subject="s", body="b", dry_run=False)
    assert result["sent"] is True and result["saved_to_sent"] is False
    assert not imap.appended


# --------------------------------------------------------------------------
# send_email: replies
# --------------------------------------------------------------------------

ORIGINAL = (
    b"From: Ann Example <ann@example.com>\r\n"
    b"To: test@icloud.com\r\n"
    b"Subject: Lunch?\r\n"
    b"Date: Mon, 07 Sep 2026 10:00:00 +0000\r\n"
    b"Message-ID: <orig-1@example.com>\r\n"
    b"References: <root-0@example.com>\r\n"
    b"Content-Type: text/plain\r\n\r\n"
    b"Are you free Tuesday?\r\n"
)


def original_mailbox(raw=ORIGINAL, **kwargs):
    return mailbox(uids=[7], messages={7: {b"ENVELOPE": Envelope(subject=b"Lunch?"), b"BODY[]": raw}}, **kwargs)


def test_reply_threads_onto_the_original_and_defaults_to_and_subject():
    no_smtp()
    client = original_mailbox()
    result = server.send_email(body="Yes, noon works.", reply_to_id="7")
    assert result["to"] == ["Ann Example <ann@example.com>"]
    assert result["subject"] == "Re: Lunch?"
    assert result["in_reply_to"] == "<orig-1@example.com>"
    assert result["references"] == "<root-0@example.com> <orig-1@example.com>"
    # Headers only, opened read-only, never marking the original as read.
    assert ("select_folder", "INBOX", True) in client.calls
    [fetch] = tuples(client, "fetch")
    assert fetch[1] == (7,) and fetch[2][0].startswith("BODY.PEEK[HEADER.FIELDS")


def test_reply_honours_reply_to_header_and_existing_re_prefix():
    no_smtp()
    raw = ORIGINAL.replace(b"Subject: Lunch?", b"Subject: RE: Lunch?").replace(
        b"To: test@icloud.com", b"To: test@icloud.com\r\nReply-To: list@example.com")
    original_mailbox(raw)
    result = server.send_email(body="ok", reply_to_id="7")
    assert result["to"] == ["list@example.com"]
    assert result["subject"] == "RE: Lunch?", "must not stack Re: prefixes"


def test_explicit_to_and_subject_override_reply_defaults():
    no_smtp()
    original_mailbox()
    result = server.send_email(body="ok", reply_to_id="7", to=["bob@example.com"], subject="Moved")
    assert result["to"] == ["bob@example.com"] and result["subject"] == "Moved"
    assert result["in_reply_to"] == "<orig-1@example.com>"


def test_reply_without_original_message_id_sends_unthreaded():
    no_smtp()
    original_mailbox(ORIGINAL.replace(b"Message-ID: <orig-1@example.com>\r\n", b""))
    result = server.send_email(body="ok", reply_to_id="7")
    assert result["in_reply_to"] == "" and result["references"] == ""


def test_reply_to_missing_id_raises():
    no_smtp()
    original_mailbox()
    with pytest.raises(ToolError, match="99"):
        server.send_email(body="ok", reply_to_id="99")


def test_reply_folder_is_respected():
    no_smtp()
    client = original_mailbox(folders=("INBOX", "Archive"))
    server.send_email(body="ok", reply_to_id="7", reply_folder="Archive")
    assert ("select_folder", "Archive", True) in client.calls


def test_live_reply_carries_threading_headers_on_the_wire():
    smtp = FakeSMTP()
    server._connect_smtp = lambda: smtp
    original_mailbox()
    server.send_email(body="ok", reply_to_id="7", dry_run=False)
    msg = smtp.sent[0]["message"]
    assert msg["In-Reply-To"] == "<orig-1@example.com>"
    assert msg["References"] == "<root-0@example.com> <orig-1@example.com>"


def test_non_reply_still_requires_to_and_subject():
    no_smtp()
    with pytest.raises(ToolError, match="At least one 'to'"):
        server.send_email(body="b", subject="s")
    with pytest.raises(ToolError, match="subject is required"):
        server.send_email(body="b", to=["a@example.com"])
