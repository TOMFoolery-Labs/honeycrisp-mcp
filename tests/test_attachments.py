"""save_attachments: writing attachments to disk. Nothing here touches a real account."""

import base64
import os

import pytest
from fastmcp.exceptions import ToolError

import server
from fakes import Envelope, FakeIMAP


@pytest.fixture(autouse=True)
def reset_imap_cache():
    server._imap_client = None
    yield
    server._imap_client = None


@pytest.fixture(autouse=True)
def default_download_dir(tmp_path, monkeypatch):
    # The download folder is the sandbox: every test directory below sits inside it.
    monkeypatch.setattr(server, "DOWNLOAD_DIR", str(tmp_path))
    return tmp_path


PDF = b"%PDF-1.4 fake"
PNG = b"\x89PNG fake"


def raw_message(attachments):
    body = (
        "From: Ann <ann@example.com>\r\nSubject: Files\r\nMIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="b1"\r\n\r\n'
        "--b1\r\nContent-Type: text/plain\r\n\r\nSee attached.\r\n"
    )
    for name, ctype, payload in attachments:
        body += (
            f"--b1\r\nContent-Type: {ctype}\r\nContent-Disposition: attachment; filename=\"{name}\"\r\n"
            f"Content-Transfer-Encoding: base64\r\n\r\n{base64.b64encode(payload).decode()}\r\n"
        )
    return (body + "--b1--\r\n").encode()


def use(raw, uid=5):
    client = FakeIMAP(uids=[uid], messages={uid: {b"ENVELOPE": Envelope(), b"BODY[]": raw}})
    server._connect_imap = lambda: client
    return client


TWO = raw_message([("report.pdf", "application/pdf", PDF), ("chart.png", "image/png", PNG)])


def test_saves_every_attachment_to_the_default_directory(default_download_dir):
    client = use(TWO)
    result = server.save_attachments("5")
    assert result["id"] == "5" and result["directory"] == str(default_download_dir)
    assert [s["filename"] for s in result["saved"]] == ["report.pdf", "chart.png"]
    assert open(result["saved"][0]["path"], "rb").read() == PDF
    assert open(result["saved"][1]["path"], "rb").read() == PNG
    assert result["saved"][0] == {"filename": "report.pdf", "path": str(default_download_dir / "report.pdf"),
                                  "content_type": "application/pdf", "size": len(PDF)}
    assert ("select_folder", "INBOX", True) in client.calls
    [fetch] = [c for c in client.calls if isinstance(c, tuple) and c[0] == "fetch"]
    assert fetch[2] == ("BODY.PEEK[]",), "must not mark the message read"


def test_filenames_filter_and_explicit_directory(tmp_path):
    use(TWO)
    result = server.save_attachments("5", filenames=["chart.png"], directory=str(tmp_path / "out"))
    assert [s["filename"] for s in result["saved"]] == ["chart.png"]
    assert os.listdir(tmp_path / "out") == ["chart.png"]


def test_relative_directory_is_a_subfolder_of_the_download_folder(tmp_path):
    use(TWO)
    result = server.save_attachments("5", filenames=["chart.png"], directory="invoices/2026")
    assert result["directory"] == str(tmp_path / "invoices" / "2026")
    assert (tmp_path / "invoices" / "2026" / "chart.png").exists()


@pytest.mark.parametrize("escape", ["..", "../elsewhere", "{abs}/../outside", "/etc", "~/.ssh"])
def test_directories_outside_the_download_folder_are_refused(tmp_path, monkeypatch, escape):
    # Mail is hostile input; a model can be talked into "saving" an attachment
    # over a dotfile. Writes are confined to the download folder.
    monkeypatch.setenv("HOME", str(tmp_path.parent))
    use(TWO)
    with pytest.raises(ToolError, match="outside the download folder"):
        server.save_attachments("5", directory=escape.format(abs=tmp_path))
    assert list(tmp_path.iterdir()) == [], "nothing written"


def test_symlink_inside_the_folder_cannot_point_out_of_it(tmp_path):
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    (tmp_path / "link").symlink_to(outside)
    use(TWO)
    with pytest.raises(ToolError, match="outside the download folder"):
        server.save_attachments("5", directory="link")
    assert not list(outside.iterdir())


def test_unknown_filename_lists_what_is_available(tmp_path):
    use(TWO)
    with pytest.raises(ToolError, match="No attachment named 'nope'.*Available: 'report.pdf', 'chart.png'"):
        server.save_attachments("5", filenames=["nope"], directory=str(tmp_path))
    assert not (tmp_path / "report.pdf").exists(), "nothing is written when the request is partly wrong"


def test_existing_files_get_a_suffix_unless_overwrite(tmp_path):
    (tmp_path / "report.pdf").write_bytes(b"old")
    use(TWO)
    result = server.save_attachments("5", filenames=["report.pdf"], directory=str(tmp_path))
    assert result["saved"][0]["path"] == str(tmp_path / "report (2).pdf")
    assert (tmp_path / "report.pdf").read_bytes() == b"old"
    use(TWO)
    result = server.save_attachments("5", filenames=["report.pdf"], directory=str(tmp_path), overwrite=True)
    assert result["saved"][0]["path"] == str(tmp_path / "report.pdf")
    assert (tmp_path / "report.pdf").read_bytes() == PDF


def test_hostile_filenames_cannot_escape_the_directory(tmp_path):
    use(raw_message([("../../evil.sh", "text/plain", b"x"), ("C:\\\\Windows\\\\bad.exe", "text/plain", b"y"), ("", "text/plain", b"z")]))
    result = server.save_attachments("5", directory=str(tmp_path))
    paths = [s["path"] for s in result["saved"]]
    assert all(os.path.dirname(p) == str(tmp_path) for p in paths)
    assert sorted(os.path.basename(p) for p in paths) == ["attachment-3", "bad.exe", "evil.sh"]


def test_message_without_attachments_saves_nothing(tmp_path):
    use(b"From: a@example.com\r\nContent-Type: text/plain\r\n\r\nplain")
    result = server.save_attachments("5", directory=str(tmp_path))
    assert result["saved"] == []


def test_validation(tmp_path):
    use(TWO)
    with pytest.raises(ToolError, match="Invalid message id"):
        server.save_attachments("abc")
    with pytest.raises(ToolError, match="no usable names"):
        server.save_attachments("5", filenames=["", " "])
    with pytest.raises(ToolError, match="99"):
        server.save_attachments("99", directory=str(tmp_path))
    with pytest.raises(ToolError, match="Nope"):
        server.save_attachments("5", folder="Nope")


def test_directory_is_expanded_and_created(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    use(TWO)
    result = server.save_attachments("5", directory="~/deep/er")
    assert result["directory"] == str(tmp_path / "deep" / "er")
    assert (tmp_path / "deep" / "er" / "report.pdf").exists()
