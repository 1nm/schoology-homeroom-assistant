import hashlib
import json

import pytest

import materials
from schoology_api import FolderItem, SchoologyAPI


class FakeAPI:
    """Walks a fixed tree and writes canned bytes instead of downloading."""

    def __init__(self, items, documents, pages, payloads=None):
        self._items = items
        self._documents = documents
        self._pages = pages
        self._payloads = payloads or {}
        self.downloaded = []

    def walk(self, section_id, skip=frozenset()):
        return [i for i in self._items if not (i.path and i.path[0] in skip)]

    def documents(self, section_id):
        return self._documents

    def pages(self, section_id):
        return self._pages

    def download(self, url, destination, expected_size=0):
        self.downloaded.append(destination.name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self._payloads.get(url, b"x" * max(expected_size, 1)))
        return destination

    files_of = staticmethod(SchoologyAPI.files_of)
    links_of = staticmethod(SchoologyAPI.links_of)


def document(md5="abc123", size=4, filename="Diary.pdf"):
    return {"attachments": {"files": {"file": [{
        "filename": filename, "filesize": str(size), "md5_checksum": md5,
        "timestamp": "1786088477", "filemime": "application/pdf",
        "download_path": f"https://api.schoology.com/v1/attachment/{md5}/source/x",
        "extension": "pdf"}]}}}


@pytest.fixture
def api():
    items = [
        FolderItem("1", "folder", "Class Information"),
        FolderItem("10", "document", "Diary", ("Class Information",)),
        FolderItem("20", "page", "Class Schedule", ("Class Information",)),
    ]
    return FakeAPI(items, {"10": document()}, {"20": {"body": "<p>8:10 start</p>"}})


class TestBaseline:
    def test_first_run_is_flagged_as_a_baseline(self, api, tmp_path):
        report = materials.sync(api, "SEC", tmp_path, {})
        assert report.baseline is True
        assert len(report.new) == 2  # one file, one page

    def test_a_later_run_is_not_a_baseline(self, api, tmp_path):
        known = {}
        materials.sync(api, "SEC", tmp_path, known)
        assert materials.sync(api, "SEC", tmp_path, known).baseline is False


class TestChangeDetection:
    def test_unchanged_material_produces_no_change(self, api, tmp_path):
        known = {}
        materials.sync(api, "SEC", tmp_path, known)
        api.downloaded.clear()

        report = materials.sync(api, "SEC", tmp_path, known)

        assert report.changes == []
        assert api.downloaded == []

    def test_a_new_checksum_is_an_update_not_a_new_item(self, api, tmp_path):
        known = {}
        materials.sync(api, "SEC", tmp_path, known)
        api._documents = {"10": document(md5="def456")}

        report = materials.sync(api, "SEC", tmp_path, known)

        assert [c.kind for c in report.changes] == ["updated"]
        assert report.updated[0].title == "Diary"

    def test_an_edited_page_is_an_update(self, api, tmp_path):
        known = {}
        materials.sync(api, "SEC", tmp_path, known)
        api._pages = {"20": {"body": "<p>8:30 start</p>"}}

        report = materials.sync(api, "SEC", tmp_path, known)

        assert [c.title for c in report.updated] == ["Class Schedule"]

    def test_a_file_already_on_disk_is_reported_but_not_refetched(self, api, tmp_path):
        # State lost, disk intact: what happens when a run mails badly and never saves.
        payload = b"real bytes"
        doc = document(md5=hashlib.md5(payload).hexdigest(), size=len(payload))
        url = doc["attachments"]["files"]["file"][0]["download_path"]
        api._documents, api._payloads = {"10": doc}, {url: payload}
        materials.sync(api, "SEC", tmp_path, {})
        api.downloaded.clear()

        report = materials.sync(api, "SEC", tmp_path, {})

        assert "Diary" in [c.title for c in report.new]
        assert api.downloaded == [], "the bytes were already correct on disk"


class TestLayout:
    def test_files_land_under_their_folder_path(self, api, tmp_path):
        materials.sync(api, "SEC", tmp_path, {})
        assert (tmp_path / "Class Information" / "Diary.pdf").exists()
        assert (tmp_path / "Class Information" / "Class Schedule.html").exists()

    def test_index_lists_every_item(self, api, tmp_path):
        materials.sync(api, "SEC", tmp_path, {})
        index = json.loads((tmp_path / "index.json").read_text())
        assert {e["type"] for e in index.values()} == {"file", "page"}
        assert all(e["folder"] == "Class Information" for e in index.values())

    def test_links_are_indexed_but_not_downloaded(self, tmp_path):
        doc = {"attachments": {"videos": {"video": [{"url": "https://youtu.be/abc"}]}}}
        api = FakeAPI([FolderItem("10", "document", "Read aloud", ("Literacy",))],
                      {"10": doc}, {})
        materials.sync(api, "SEC", tmp_path, {})
        index = json.loads((tmp_path / "index.json").read_text())
        assert [e["type"] for e in index.values()] == ["video"]
        assert api.downloaded == []


class TestLimits:
    def test_a_file_over_the_cap_is_skipped(self, api, tmp_path):
        api._documents = {"10": document(size=80 * 1024 * 1024)}
        report = materials.sync(api, "SEC", tmp_path, {}, max_file_mb=50)
        assert [c.title for c in report.new] == ["Class Schedule"]
        assert api.downloaded == []

    def test_a_failing_item_is_recorded_and_the_rest_continue(self, api, tmp_path):
        def explode(url, destination, expected_size=0):
            raise OSError("disk full")

        api.download = explode
        report = materials.sync(api, "SEC", tmp_path, {})

        assert report.errors and "Diary" in report.errors[0]
        assert [c.title for c in report.new] == ["Class Schedule"]


class TestDryRun:
    def test_reports_without_downloading_or_recording(self, api, tmp_path):
        known = {}
        report = materials.sync(api, "SEC", tmp_path, known, dry_run=True)

        assert len(report.new) == 2
        assert api.downloaded == []
        assert known == {}
        assert not (tmp_path / "index.json").exists()
        assert not (tmp_path / "Class Information").exists()
