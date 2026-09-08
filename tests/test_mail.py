import base64
import subprocess
import sys
from datetime import datetime, timezone

import pytest
from fastmcp.exceptions import ToolError

import server
from fakes import Address, Envelope, FakeIMAP


@pytest.fixture(autouse=True)
def reset_imap_cache():
    server._imap_client = None
    yield
    server._imap_client = None


def use(client):
    server._connect_imap = lambda: client
    return client


def message(envelope, raw=b""):
    return {b"ENVELOPE": envelope, b"BODY[]<0>": raw}


# --------------------------------------------------------------------------
# stdout must stay clean: it carries the JSON-RPC stream
# --------------------------------------------------------------------------

def _import_server(env):
    return subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); import server"],
        capture_output=True,
        env={"PATH": "/usr/bin:/bin", **env},
        cwd=__file__.rsplit("/tests/", 1)[0],
    )


def test_import_writes_nothing_to_stdout_without_credentials():
    # Blank values stay in os.environ, so load_dotenv() will not fill them in
    # from a real .env -- this isolates the missing-credentials path.
    result = _import_server({"ICLOUD_EMAIL": "", "ICLOUD_APP_PASSWORD": ""})
    assert result.stdout == b"", f"stdout polluted: {result.stdout!r}"
    assert b"ICLOUD_EMAIL" in result.stderr, "warning must reach stderr"


def test_import_writes_nothing_to_stdout_with_credentials():
    result = _import_server({"ICLOUD_EMAIL": "a@b.com", "ICLOUD_APP_PASSWORD": "x"})
    assert result.stdout == b"", f"stdout polluted: {result.stdout!r}"


# --------------------------------------------------------------------------
# Header decoding
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (b"=?UTF-8?B?SGVsbG8gV29ybGQ=?=", "Hello World"),
    (b"=?UTF-8?Q?Caf=C3=A9_r=C3=A9union?=", "Café réunion"),
    (b"=?iso-8859-1?q?Se=F1or?=", "Señor"),
    (b"Plain ASCII subject", "Plain ASCII subject"),
    (None, ""),
])
def test_mime_encoded_subjects_are_decoded(raw, expected):
    assert server._decode_header_value(raw) == expected


def test_undecodable_subject_bytes_do_not_raise():
    assert server._decode_header_value(b"\xff\xfe broken") != ""


def test_from_address_is_rendered():
    envelope = Envelope(from_=[Address(b"=?UTF-8?B?Sm9zw6k=?=", b"jose", b"example.com")])
    assert server._format_address(envelope.from_) == "José <jose@example.com>"


# --------------------------------------------------------------------------
# Body previews: real messages are transfer-encoded
# --------------------------------------------------------------------------

def test_base64_multipart_body_is_decoded():
    body = base64.b64encode("Meeting moved to Tuesday.".encode()).decode()
    raw = (
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/alternative; boundary="b1"\r\n\r\n'
        "--b1\r\nContent-Type: text/plain; charset=utf-8\r\n"
        f"Content-Transfer-Encoding: base64\r\n\r\n{body}\r\n"
        "--b1--\r\n"
    ).encode()
    assert server._extract_preview(raw) == "Meeting moved to Tuesday."


def test_quoted_printable_body_is_decoded():
    raw = (
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        "Caf=C3=A9 at 3pm\r\n"
    ).encode()
    assert "Café at 3pm" in server._extract_preview(raw)


def test_html_only_body_is_stripped_of_tags():
    raw = (
        "Content-Type: text/html; charset=utf-8\r\n\r\n"
        "<html><body><p>Hello <b>there</b></p></body></html>"
    ).encode()
    preview = server._extract_preview(raw)
    assert "<" not in preview and "Hello" in preview and "there" in preview


def test_preview_is_truncated():
    raw = ("Content-Type: text/plain\r\n\r\n" + "x" * 5000).encode()
    preview = server._extract_preview(raw)
    assert len(preview) == server.PREVIEW_CHARS + 3 and preview.endswith("...")


def test_truncated_base64_payload_does_not_raise():
    body = base64.b64encode(b"y" * 4000).decode()[:1001]  # deliberately mid-stream
    raw = (
        "Content-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n" + body
    ).encode()
    server._extract_preview(raw)  # must not raise


def test_empty_body_yields_empty_preview():
    assert server._extract_preview(b"") == ""


# --------------------------------------------------------------------------
# search_emails
# --------------------------------------------------------------------------

def test_invalid_query_raises_instead_of_returning_all_mail():
    client = use(FakeIMAP(uids=[1, 2, 3]))
    client.search_error = ValueError("SEARCH command error: bad criteria")
    with pytest.raises(ToolError) as excinfo:
        server.search_emails(query='FROM "apple"')
    assert "Invalid IMAP search query" in str(excinfo.value)
    # It must not have silently retried with ALL.
    assert [c for c in client.calls if c[0] == "search"] == [("search", 'FROM "apple"')]
    assert not any(c[0] == "fetch" for c in client.calls if isinstance(c, tuple))


def test_missing_folder_reports_the_folder_name():
    use(FakeIMAP(folders=("INBOX",)))
    with pytest.raises(ToolError, match="Nope"):
        server.search_emails(folder="Nope")


@pytest.mark.parametrize("limit", [0, -1])
def test_non_positive_limit_is_rejected(limit):
    use(FakeIMAP(uids=list(range(1, 30))))
    with pytest.raises(ToolError, match="limit must be at least 1"):
        server.search_emails(limit=limit)


def test_returns_newest_first_and_respects_limit():
    uids = [3, 1, 20, 7, 15]
    msgs = {u: message(Envelope(subject=f"S{u}".encode())) for u in uids}
    use(FakeIMAP(uids=uids, messages=msgs))
    results = server.search_emails(limit=3)
    assert [r["id"] for r in results] == ["20", "15", "7"]


def test_fetch_uses_peek_and_a_bounded_byte_range():
    use(FakeIMAP(uids=[1], messages={1: message(Envelope(subject=b"Hi"))}))
    server.search_emails()
    fetch = next(c for c in server._imap_client.calls if isinstance(c, tuple) and c[0] == "fetch")
    parts = fetch[2]
    assert "ENVELOPE" in parts
    body_part = next(p for p in parts if p.startswith("BODY"))
    # PEEK avoids marking mail as read; the range avoids downloading attachments.
    assert body_part == f"BODY.PEEK[]<0.{server.PREVIEW_FETCH_BYTES}>"


def test_folder_is_opened_read_only():
    use(FakeIMAP(uids=[], messages={}))
    server.search_emails()
    assert ("select_folder", "INBOX", True) in server._imap_client.calls


def test_empty_search_returns_empty_list():
    use(FakeIMAP(uids=[]))
    assert server.search_emails() == []


def test_result_shape():
    envelope = Envelope(
        subject=b"=?UTF-8?B?SGVsbG8=?=",
        date=datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc),
        from_=[Address(b"Ann", b"ann", b"example.com")],
    )
    raw = b"Content-Type: text/plain\r\n\r\nBody text here"
    use(FakeIMAP(uids=[9], messages={9: message(envelope, raw)}))
    assert server.search_emails() == [{
        "id": "9",
        "from": "Ann <ann@example.com>",
        "subject": "Hello",
        "date": "2026-08-17T10:00:00+00:00",
        "unread": True,
        "flagged": False,
        "body_preview": "Body text here",
    }]


def test_flags_are_reported():
    envelope = Envelope(subject=b"Hi")
    msg = {**message(envelope), b"FLAGS": (b"\\Seen", b"\\Flagged")}
    use(FakeIMAP(uids=[1], messages={1: msg}))
    [result] = server.search_emails()
    assert result["unread"] is False and result["flagged"] is True
    fetch = next(c for c in server._imap_client.calls if isinstance(c, tuple) and c[0] == "fetch")
    assert "FLAGS" in fetch[2]


# --------------------------------------------------------------------------
# Connection lifecycle
# --------------------------------------------------------------------------

def test_connection_is_reused_across_calls():
    client = use(FakeIMAP(uids=[]))
    connects = []
    server._connect_imap = lambda: (connects.append(1), client)[1]
    server.search_emails()
    server.search_emails()
    assert len(connects) == 1, "re-logging in on every call invites iCloud throttling"
    assert client.calls.count("noop") == 1  # liveness probe on the second call


def test_stale_connection_is_replaced():
    dead = FakeIMAP(uids=[])
    dead.noop_fails = True
    fresh = FakeIMAP(uids=[])
    clients = [dead, fresh]
    server._connect_imap = lambda: clients.pop(0)
    server.search_emails()
    server.search_emails()
    assert dead.logged_out
    assert server._imap_client is fresh


def test_protocol_error_drops_the_cached_connection():
    client = use(FakeIMAP(uids=[1], messages={}))
    client.fetch_error = OSError("broken pipe")
    with pytest.raises(OSError):
        server.search_emails()
    assert client.logged_out, "connection leaked on the error path"
    assert server._imap_client is None


def test_validation_error_keeps_the_connection():
    client = use(FakeIMAP(uids=[1]))
    client.search_error = ValueError("bad criteria")
    with pytest.raises(ToolError):
        server.search_emails(query="???")
    # A rejected SEARCH does not desynchronise the stream; no need to reconnect.
    assert not client.logged_out
    assert server._imap_client is client


# --------------------------------------------------------------------------
# Structured search filters compile to IMAP criteria
# --------------------------------------------------------------------------

def searched(client):
    return next(c[1] for c in client.calls if isinstance(c, tuple) and c[0] == "search")


def test_no_filters_means_all():
    client = use(FakeIMAP(uids=[]))
    server.search_emails()
    assert searched(client) == ["ALL"]


def test_filters_compile_to_a_criteria_list_that_imapclient_quotes():
    from datetime import date
    client = use(FakeIMAP(uids=[]))
    server.search_emails(sender="Ann Example", to="me", subject='say "hi"', text="invoice",
                         since="2026-09-01", before="2026-09-08", unread=True, flagged=False)
    # A list, not a hand-built string: imapclient quotes values and formats dates.
    assert searched(client) == [
        "FROM", "Ann Example", "TO", "me", "SUBJECT", 'say "hi"', "TEXT", "invoice",
        "SINCE", date(2026, 9, 1), "BEFORE", date(2026, 9, 8), "UNSEEN", "UNFLAGGED",
    ]


def test_read_and_flagged_variants():
    client = use(FakeIMAP(uids=[]))
    server.search_emails(unread=False, flagged=True)
    assert searched(client) == ["SEEN", "FLAGGED"]


def test_blank_filters_are_ignored():
    client = use(FakeIMAP(uids=[]))
    server.search_emails(sender="  ", subject="", since=" ")
    assert searched(client) == ["ALL"]


def test_dates_accept_iso_datetimes_and_reject_garbage():
    from datetime import date
    client = use(FakeIMAP(uids=[]))
    server.search_emails(since="2026-09-01T10:00:00Z")
    assert searched(client) == ["SINCE", date(2026, 9, 1)]
    with pytest.raises(ToolError, match="since must be a date"):
        server.search_emails(since="last tuesday")
    with pytest.raises(ToolError, match="before .* must be after since"):
        server.search_emails(since="2026-09-08", before="2026-09-01")


def test_raw_query_is_passed_through_untouched():
    client = use(FakeIMAP(uids=[]))
    server.search_emails(query='OR FROM "ann" FROM "bob"')
    assert searched(client) == 'OR FROM "ann" FROM "bob"'


def test_raw_query_cannot_be_mixed_with_filters():
    client = use(FakeIMAP(uids=[]))
    with pytest.raises(ToolError, match="either 'query' or the structured filters"):
        server.search_emails(query="UNSEEN", sender="ann")
    assert not any(isinstance(c, tuple) and c[0] == "search" for c in client.calls)


def test_non_ascii_filter_uses_utf8_charset():
    class CharsetIMAP(FakeIMAP):
        def search(self, criteria, charset=None):
            self.calls.append(("search", criteria, charset))
            return []
    client = use(CharsetIMAP(uids=[]))
    server.search_emails(text="café")
    assert next(c for c in client.calls if c[0] == "search") == ("search", ["TEXT", "café"], "UTF-8")
