"""Minimal stand-ins for the iCloud services, so tests run offline."""

from datetime import datetime, timezone
from xml.sax.saxutils import escape as xml_escape


class Address:
    def __init__(self, name=None, mailbox=b"", host=b""):
        self.name = name
        self.route = None
        self.mailbox = mailbox
        self.host = host


class Envelope:
    def __init__(self, subject=b"", date=None, from_=None):
        self.subject = subject
        self.date = date or datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc)
        self.from_ = from_


class FakeIMAP:
    """Records the commands issued so tests can assert on protocol usage."""

    def __init__(self, uids=None, messages=None, folders=("INBOX",)):
        self.uids = list(uids or [])
        self.messages = messages or {}
        self.folders = set(folders)
        self.calls = []
        self.selected = None
        self.readonly = None
        self.logged_out = False
        self.noop_fails = False
        self.search_error = None
        self.fetch_error = None

    def noop(self):
        self.calls.append("noop")
        if self.noop_fails:
            raise OSError("connection reset")

    def select_folder(self, folder, readonly=True):
        self.calls.append(("select_folder", folder, readonly))
        if folder not in self.folders:
            raise Exception(f"NONEXISTENT [{folder}] Mailbox doesn't exist")
        self.selected, self.readonly = folder, readonly

    def search(self, criteria):
        self.calls.append(("search", criteria))
        if self.search_error:
            raise self.search_error
        return list(self.uids)

    def fetch(self, uids, parts):
        self.calls.append(("fetch", tuple(uids), tuple(parts)))
        if self.fetch_error:
            raise self.fetch_error
        return {uid: self.messages[uid] for uid in uids if uid in self.messages}

    def logout(self):
        self.calls.append("logout")
        self.logged_out = True

    def shutdown(self):
        self.calls.append("shutdown")
        self.logged_out = True


class FakeEvent:
    def __init__(self, vobject_instance):
        self.vobject_instance = vobject_instance


class _Attr:
    def __init__(self, value):
        self.value = value


class FakeVEvent:
    def __init__(self, summary=None, dtstart=None, dtend=None):
        if summary is not None:
            self.summary = _Attr(summary)
        if dtstart is not None:
            self.dtstart = _Attr(dtstart)
        if dtend is not None:
            self.dtend = _Attr(dtend)


class FakeVObject:
    def __init__(self, vevent):
        self.vevent = vevent


class FakeCalendar:
    def __init__(self, name, events=(), error=None):
        self.name = name
        self._events = list(events)
        self.error = error
        self.search_kwargs = None

    def search(self, **kwargs):
        self.search_kwargs = kwargs
        if self.error:
            raise self.error
        return self._events


class FakePrincipal:
    def __init__(self, calendars):
        self._calendars = calendars

    def calendars(self):
        return self._calendars


class FakeDAVClient:
    def __init__(self, calendars):
        self._calendars = calendars

    def principal(self):
        return FakePrincipal(self._calendars)


def make_event(summary, dtstart, dtend=None):
    return FakeEvent(FakeVObject(FakeVEvent(summary, dtstart, dtend)))


# --- CardDAV canned responses (Apple returns ABSOLUTE hrefs on partition hosts)

PRINCIPAL_XML = (
    '<multistatus xmlns="DAV:"><response><propstat><prop>'
    "<current-user-principal><href>https://p61-contacts.icloud.com:443/123456/principal/</href></current-user-principal>"
    "</prop></propstat></response></multistatus>"
)

HOME_XML = (
    '<multistatus xmlns="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav"><response><propstat><prop>'
    "<card:addressbook-home-set><href>https://p61-contacts.icloud.com:443/123456/carddavhome/</href></card:addressbook-home-set>"
    "</prop></propstat></response></multistatus>"
)


def collections_xml(*names):
    entries = "".join(
        "<response><href>https://p61-contacts.icloud.com:443/123456/carddavhome/%s/</href>"
        "<propstat><prop><resourcetype><collection/><card:addressbook/></resourcetype></prop></propstat>"
        "</response>" % name
        for name in names
    )
    # A non-addressbook collection that must be ignored.
    noise = (
        "<response><href>https://p61-contacts.icloud.com:443/123456/carddavhome/notes/</href>"
        "<propstat><prop><resourcetype><collection/></resourcetype></prop></propstat></response>"
    )
    return (
        '<multistatus xmlns="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
        + noise + entries + "</multistatus>"
    )


def report_xml(*vcards):
    # vCard text is character data inside the response document, so it has to
    # be escaped here the same way a real server would escape it. Each entry
    # also carries the href and ETag a real server returns, which is what the
    # write path needs to target and guard the card.
    entries = "".join(
        "<response><href>https://p61-contacts.icloud.com:443/123456/carddavhome/card/%d.vcf</href>"
        '<propstat><prop><getetag>"etag-%d"</getetag>'
        "<card:address-data>%s</card:address-data></prop></propstat></response>"
        # Apple escapes CR as a character reference; without this an XML parser
        # normalises CRLF to LF and the fixture stops matching the real wire.
        % (i, i, xml_escape(v).replace("\r", "&#13;"))
        for i, v in enumerate(vcards)
    )
    return (
        '<multistatus xmlns="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
        + entries + "</multistatus>"
    )


def vcard(fn, emails=(), phones=(), n=None, org=None, uid=None):
    """Build a vCard shaped like the ones iCloud actually returns.

    Real cards use CRLF line endings and often carry an empty FN with the name
    living in the structured N property.
    """
    lines = ["BEGIN:VCARD", "VERSION:3.0", f"FN:{fn}"]
    lines.append(f"UID:{uid if uid is not None else 'uid-' + fn}")
    if n is not None:
        lines.append(f"N:{n}")
    if org is not None:
        lines.append(f"ORG:{org}")
    lines += [f"EMAIL;TYPE=INTERNET:{e}" for e in emails]
    lines += [f"TEL:{p}" for p in phones]
    lines.append("END:VCARD")
    return "\r\n".join(lines) + "\r\n"


class FakeResponse:
    def __init__(self, text, status_code=207):
        self.text = text
        self.status_code = status_code


class FakeHTTP:
    """Replays canned DAV responses and records every request made.

    PUTs are answered with 204 by default and recorded in `writes`, so tests
    can assert on exactly what would hit the server.
    """

    def __init__(self, responses, put_status=204):
        self.responses = list(responses)
        self.calls = []
        self.writes = []
        self.put_status = put_status

    def __call__(self, method, url, **kwargs):
        self.calls.append({
            "method": method,
            "url": url,
            "data": kwargs.get("data"),
            "timeout": kwargs.get("timeout"),
            "headers": kwargs.get("headers") or {},
        })
        if method == "PUT":
            self.writes.append({
                "url": url,
                "body": (kwargs.get("data") or b"").decode("utf-8"),
                "if_match": (kwargs.get("headers") or {}).get("If-Match"),
            })
            return FakeResponse("", status_code=self.put_status)
        if not self.responses:
            raise AssertionError(f"unexpected extra request: {method} {url}")
        return self.responses.pop(0)
