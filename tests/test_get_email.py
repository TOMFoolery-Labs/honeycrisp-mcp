"""get_email: full message retrieval."""

import base64

import pytest
from fastmcp.exceptions import ToolError

import server
from fakes import Envelope, FakeIMAP


@pytest.fixture(autouse=True)
def reset_imap_cache():
    server._imap_client = None
    yield
    server._imap_client = None


def use(raw, uid=5, folders=("INBOX",)):
    client = FakeIMAP(uids=[uid], messages={uid: {b"ENVELOPE": Envelope(), b"BODY[]": raw}}, folders=folders)
    server._connect_imap = lambda: client
    return client


def test_full_message_shape_with_attachment():
    pdf = base64.b64encode(b"%PDF-1.4 fake").decode()
    raw = (
        "From: Ann <ann@example.com>\r\n"
        "To: test@icloud.com\r\n"
        "Cc: bob@example.com\r\n"
        "Subject: =?UTF-8?B?UmVwb3J0?=\r\n"
        "Date: Mon, 07 Sep 2026 10:00:00 +0000\r\n"
        "Message-ID: <m1@example.com>\r\n"
        "In-Reply-To: <m0@example.com>\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="b1"\r\n\r\n'
        "--b1\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        "Line one.\r\nLine two.\r\n"
        "--b1\r\nContent-Type: application/pdf; name=report.pdf\r\n"
        "Content-Disposition: attachment; filename=report.pdf\r\n"
        f"Content-Transfer-Encoding: base64\r\n\r\n{pdf}\r\n"
        "--b1--\r\n"
    ).encode()
    client = use(raw)
    result = server.get_email("5")
    assert result == {
        "id": "5",
        "folder": "INBOX",
        "from": "Ann <ann@example.com>",
        "to": "test@icloud.com",
        "cc": "bob@example.com",
        "reply_to": "",
        "subject": "Report",
        "date": "2026-09-07T10:00:00+00:00",
        "message_id": "<m1@example.com>",
        "in_reply_to": "<m0@example.com>",
        "body": "Line one.\nLine two.",
        "truncated": False,
        "attachments": [{"filename": "report.pdf", "content_type": "application/pdf", "size": 13}],
    }
    assert ("select_folder", "INBOX", True) in client.calls
    [fetch] = [c for c in client.calls if isinstance(c, tuple) and c[0] == "fetch"]
    assert fetch == ("fetch", (5,), ("BODY.PEEK[]",)), "full fetch, but never marking as read"


def test_html_only_body_keeps_paragraph_breaks_and_drops_styles():
    raw = (
        "Content-Type: text/html; charset=utf-8\r\n\r\n"
        "<html><head><style>p{color:red}</style></head><body>"
        "<p>Hello &amp; welcome</p><p>Second<br>line</p></body></html>"
    ).encode()
    use(raw)
    assert server.get_email("5")["body"] == "Hello & welcome\n\nSecond\nline"


def test_plain_part_is_preferred_over_html():
    raw = (
        'Content-Type: multipart/alternative; boundary="b"\r\n\r\n'
        "--b\r\nContent-Type: text/plain\r\n\r\nplain text\r\n"
        "--b\r\nContent-Type: text/html\r\n\r\n<p>html text</p>\r\n--b--\r\n"
    ).encode()
    use(raw)
    assert server.get_email("5")["body"].strip() == "plain text"


def test_body_is_capped_by_max_chars():
    use(("Content-Type: text/plain\r\n\r\n" + "x" * 100).encode())
    result = server.get_email("5", max_chars=10)
    assert result["body"] == "x" * 10 + "..." and result["truncated"] is True


def test_missing_id_raises_with_folder_name():
    use(b"Content-Type: text/plain\r\n\r\nhi")
    with pytest.raises(ToolError, match="99.*INBOX"):
        server.get_email("99")


def test_missing_folder_reports_the_folder_name():
    use(b"", folders=("INBOX",))
    with pytest.raises(ToolError, match="Nope"):
        server.get_email("5", folder="Nope")


@pytest.mark.parametrize("bad", ["", "abc", "1,2"])
def test_non_numeric_id_is_rejected(bad):
    use(b"")
    with pytest.raises(ToolError, match="Invalid message id"):
        server.get_email(bad)


def test_non_positive_max_chars_is_rejected():
    use(b"")
    with pytest.raises(ToolError, match="max_chars"):
        server.get_email("5", max_chars=0)


def test_unparseable_date_falls_back_to_raw_header():
    raw = b"Date: not a date\r\nContent-Type: text/plain\r\n\r\nhi"
    use(raw)
    assert server.get_email("5")["date"] == "not a date"
