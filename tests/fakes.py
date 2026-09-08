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

    def __init__(self, uids=None, messages=None, folders=("INBOX",),
                 special=None, capabilities=("MOVE", "UIDPLUS")):
        self.uids = list(uids or [])
        self.messages = messages or {}
        self.folders = set(folders)
        # Maps imapclient special-folder flags (b"\\Trash", b"\\Sent") to names,
        # the way find_special_folder resolves them on a real server.
        self.special = dict(special or {})
        self.capabilities = set(capabilities)
        self.calls = []
        self.selected = None
        self.readonly = None
        self.logged_out = False
        self.noop_fails = False
        self.search_error = None
        self.fetch_error = None
        # Optional per-folder (flags, total, unseen) for list_folders.
        self.folder_info = {}
        self.moved = []
        self.copied = []
        self.flags_added = []
        self.flags_removed = []
        self.flagged_deleted = []
        self.expunged = []
        self.appended = []

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

    def folder_exists(self, folder):
        self.calls.append(("folder_exists", folder))
        return folder in self.folders

    def list_folders(self, directory="", pattern="*"):
        self.calls.append(("list_folders",))
        return [
            (tuple(self.folder_info.get(name, ((), 0, 0))[0]), "/", name)
            for name in sorted(self.folders)
        ]

    def folder_status(self, folder, what=None):
        self.calls.append(("folder_status", folder))
        if folder not in self.folders:
            raise Exception(f"STATUS failed: [{folder}] Mailbox doesn't exist")
        _, total, unseen = self.folder_info.get(folder, ((), 0, 0))
        return {b"MESSAGES": total, b"UNSEEN": unseen}

    def add_flags(self, uids, flags, silent=False):
        self.calls.append(("add_flags", tuple(uids), tuple(flags)))
        self.flags_added.append((tuple(uids), tuple(flags)))

    def remove_flags(self, uids, flags, silent=False):
        self.calls.append(("remove_flags", tuple(uids), tuple(flags)))
        self.flags_removed.append((tuple(uids), tuple(flags)))

    def has_capability(self, capability):
        return capability in self.capabilities

    def find_special_folder(self, flag):
        self.calls.append(("find_special_folder", flag))
        return self.special.get(flag)

    def move(self, uids, folder):
        assert "MOVE" in self.capabilities, "MOVE issued without the capability"
        self.calls.append(("move", tuple(uids), folder))
        self.moved.append((tuple(uids), folder))
        self._remove(uids)

    def copy(self, uids, folder):
        self.calls.append(("copy", tuple(uids), folder))
        self.copied.append((tuple(uids), folder))

    def delete_messages(self, uids, silent=False):
        self.calls.append(("delete_messages", tuple(uids)))
        self.flagged_deleted.extend(uids)

    def uid_expunge(self, uids):
        assert "UIDPLUS" in self.capabilities, "UID EXPUNGE issued without UIDPLUS"
        self.calls.append(("uid_expunge", tuple(uids)))
        self.expunged.extend(uids)
        self._remove(uids)

    def expunge(self, uids=None):
        self.calls.append(("expunge", None))
        self.expunged.extend(self.flagged_deleted)
        self._remove(self.flagged_deleted)

    def append(self, folder, msg, flags=(), msg_time=None):
        self.calls.append(("append", folder, tuple(flags)))
        self.appended.append((folder, msg, tuple(flags)))

    def _remove(self, uids):
        for uid in uids:
            if uid in self.uids:
                self.uids.remove(uid)
            self.messages.pop(uid, None)

    def logout(self):
        self.calls.append("logout")
        self.logged_out = True

    def shutdown(self):
        self.calls.append("shutdown")
        self.logged_out = True


class FakeSMTP:
    """Records what would have gone out over SMTP."""

    def __init__(self, error=None):
        self.sent = []
        self.error = error
        self.quit_called = False

    def send_message(self, msg, from_addr=None, to_addrs=None):
        if self.error:
            raise self.error
        self.sent.append({"message": msg, "from": from_addr, "to": list(to_addrs or [])})
        return {}

    def quit(self):
        self.quit_called = True


class FakeEvent:
    """An event as caldav hands it back: vobject view, raw data, and -- when
    built from iCalendar text -- a live icalendar instance that save() serialises."""

    def __init__(self, vobject_instance, data="", delete_error=None, save_error=None):
        self.vobject_instance = vobject_instance
        self.data = data
        self.delete_error = delete_error
        self.save_error = save_error
        self.deleted = False
        self.saved = []
        self.icalendar_instance = None
        if data:
            try:
                import icalendar
                self.icalendar_instance = icalendar.Calendar.from_ical(data)
            except Exception:
                self.icalendar_instance = None

    def delete(self):
        if self.delete_error:
            raise self.delete_error
        self.deleted = True

    def save(self, **kwargs):
        if self.save_error:
            raise self.save_error
        self.data = self.icalendar_instance.to_ical().decode()
        self.saved.append({"data": self.data, **kwargs})
        return self


class FakeCalClient:
    """Answers the single PROPFIND list_calendars issues, from the calendar's `info`."""

    def __init__(self, calendar):
        self.calendar = calendar
        self.propfinds = []

    def propfind(self, url, props="", depth=0):
        self.propfinds.append((url, depth))
        info = self.calendar.info
        if info is None:
            raise Exception("PROPFIND failed")
        comps = "".join(f'<C:comp name="{c}"/>' for c in info.get("comps", ()))
        privs = "".join(f"<privilege><{p}/></privilege>" for p in info.get("privs", ()))
        types = "".join(f"<{t}/>" for t in info.get("rtypes", ("collection", "calendar")))
        color = f"<A:calendar-color>{info['color']}</A:calendar-color>" if info.get("color") else ""
        name = f"<displayname>{xml_escape(info['display'])}</displayname>" if info.get("display") else ""
        xml = (
            '<multistatus xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav" xmlns:A="http://apple.com/ns/ical/">'
            f"<response><href>{url}</href><propstat><prop>{name}<resourcetype>{types}</resourcetype>"
            f"<current-user-privilege-set>{privs}</current-user-privilege-set>"
            f"<C:supported-calendar-component-set>{comps}</C:supported-calendar-component-set>{color}"
            "</prop></propstat></response></multistatus>"
        )
        return type("Resp", (), {"raw": xml})()


class _Attr:
    def __init__(self, value):
        self.value = value


class FakeVEvent:
    def __init__(self, summary=None, dtstart=None, dtend=None, uid=None):
        if summary is not None:
            self.summary = _Attr(summary)
        if uid is not None:
            self.uid = _Attr(uid)
        if dtstart is not None:
            self.dtstart = _Attr(dtstart)
        if dtend is not None:
            self.dtend = _Attr(dtend)


class FakeVObject:
    def __init__(self, vevent):
        self.vevent = vevent


class FakeCalendar:
    def __init__(self, name, events=(), error=None, add_error=None, info=None, cal_id=None):
        self.name = name
        self.id = cal_id or name.lower()
        self.url = f"https://caldav.example.com/cal/{name}/"
        self.info = info
        self.client = FakeCalClient(self)
        self._events = list(events)
        self.error = error
        self.add_error = add_error
        self.search_kwargs = None
        self.added = []

    def search(self, **kwargs):
        self.search_kwargs = kwargs
        if self.error:
            raise self.error
        return self._events

    def add_event(self, ical=None, no_overwrite=False, no_create=False, **kwargs):
        if self.add_error:
            raise self.add_error
        self.added.append({"ical": ical, "no_overwrite": no_overwrite})
        return FakeEvent(None, data=ical)

    def get_event_by_uid(self, uid):
        from caldav.lib.error import NotFoundError
        if self.error:
            raise self.error
        for event in self._events:
            vevent = event.vobject_instance.vevent
            if getattr(vevent, "uid", None) is not None and vevent.uid.value == uid:
                return event
        raise NotFoundError(f"no event with uid {uid}")


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


def make_event(summary, dtstart, dtend=None, uid=None, data="", delete_error=None, save_error=None):
    return FakeEvent(FakeVObject(FakeVEvent(summary, dtstart, dtend, uid)), data=data,
                     delete_error=delete_error, save_error=save_error)


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


def collections_xml(*names, display_names=None):
    display_names = display_names or {}
    entries = "".join(
        "<response><href>https://p61-contacts.icloud.com:443/123456/carddavhome/%s/</href>"
        "<propstat><prop><resourcetype><collection/><card:addressbook/></resourcetype>%s</prop></propstat>"
        "</response>" % (name, f"<displayname>{xml_escape(display_names[name])}</displayname>" if name in display_names else "")
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

    def __init__(self, responses, put_status=204, delete_status=204):
        self.responses = list(responses)
        self.calls = []
        self.writes = []
        self.deletes = []
        self.put_status = put_status
        self.delete_status = delete_status

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
                "if_none_match": (kwargs.get("headers") or {}).get("If-None-Match"),
            })
            return FakeResponse("", status_code=self.put_status)
        if method == "DELETE":
            self.deletes.append({
                "url": url,
                "if_match": (kwargs.get("headers") or {}).get("If-Match"),
            })
            return FakeResponse("", status_code=self.delete_status)
        if not self.responses:
            raise AssertionError(f"unexpected extra request: {method} {url}")
        return self.responses.pop(0)
