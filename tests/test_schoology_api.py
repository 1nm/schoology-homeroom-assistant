import re

import pytest

from schoology_api import FolderItem, MaterialFile, SchoologyAPI, SchoologyAPIError


@pytest.fixture
def api():
    return SchoologyAPI("KEY123", "SECRET456", timeout=5)


class TestAuthHeader:
    """The two details that make Schoology answer with a misleading
    "Duplicate timestamp/nonce combination" error when they are wrong."""

    def test_realm_space_is_url_encoded(self, api):
        header = api._auth_header()
        assert 'realm="Schoology%20API"' in header
        assert 'realm="Schoology API"' not in header

    def test_signature_ends_in_a_literal_ampersand(self, api):
        assert 'oauth_signature="SECRET456&"' in api._auth_header()
        assert "%26" not in api._auth_header()

    def test_carries_no_oauth_token(self, api):
        assert "oauth_token" not in api._auth_header()

    def test_plaintext_signature_method(self, api):
        assert 'oauth_signature_method="PLAINTEXT"' in api._auth_header()

    def test_nonce_differs_between_calls(self, api):
        nonces = {re.search(r'oauth_nonce="([^"]+)"', api._auth_header()).group(1)
                  for _ in range(20)}
        assert len(nonces) == 20


class FakeAPI(SchoologyAPI):
    """Serves canned payloads by path instead of calling Schoology."""

    def __init__(self, payloads):
        super().__init__("k", "s")
        self.payloads = payloads
        self.requested = []

    def get(self, path, **params):
        self.requested.append(path)
        # Suffix match, so "/folder/1" does not also answer for "/folder/11".
        for match in (str.endswith, str.__contains__):
            for pattern, payload in self.payloads.items():
                if match(path, pattern):
                    if isinstance(payload, SchoologyAPIError):
                        raise payload
                    return payload
        return {}


class TestIdentity:
    def test_children_are_split_from_the_csv_field(self):
        api = FakeAPI({"/users/1": {"child_uids": "10,11,12"}})
        assert api.child_ids("1") == ["10", "11", "12"]

    def test_no_children_is_an_empty_list(self):
        assert FakeAPI({"/users/1": {"child_uids": ""}}).child_ids("1") == []
        assert FakeAPI({"/users/1": {}}).child_ids("1") == []

    def test_sections_are_named(self):
        api = FakeAPI({"/sections": {"section": [
            {"id": 99, "course_title": "Homeroom", "section_title": "Section B"}]}})
        assert api.sections("10")[0].name == "Homeroom: Section B"


class TestWalk:
    PAYLOADS = {
        "/folder/0": {"folder-item": [
            {"id": 1, "type": "folder", "title": "Class Information"},
            {"id": 2, "type": "folder", "title": "Photos"},
        ]},
        "/folder/1": {"folder-item": [
            {"id": 10, "type": "document", "title": "Welcome Packet"},
            {"id": 11, "type": "folder", "title": "Unit 1"},
        ]},
        "/folder/11": {"folder-item": [{"id": 12, "type": "page", "title": "Overview"}]},
        "/folder/2": {"folder-item": [{"id": 20, "type": "document", "title": "Photo"}]},
    }

    def test_recurses_into_subfolders_and_records_the_path(self):
        items = FakeAPI(self.PAYLOADS).walk("SEC")
        by_title = {i.title: i for i in items}
        assert by_title["Welcome Packet"].path == ("Class Information",)
        assert by_title["Overview"].path == ("Class Information", "Unit 1")

    def test_skipped_folders_are_not_entered(self):
        api = FakeAPI(self.PAYLOADS)
        titles = {i.title for i in api.walk("SEC", skip=frozenset({"Photos"}))}
        assert "Photo" not in titles
        assert "/folder/2" not in api.requested

    def test_depth_is_bounded(self):
        # A folder that contains itself must not recurse forever.
        class Looping(FakeAPI):
            def get(self, path, **params):
                return {"folder-item": [{"id": 1, "type": "folder", "title": "Loop"}]}

        assert len(Looping({}).walk("SEC")) < 20


class TestAttachments:
    DOC = {"attachments": {"files": {"file": [{
        "filename": "Diary.pdf", "filesize": "1024", "md5_checksum": "abc123",
        "timestamp": "1786088477", "filemime": "application/pdf",
        "download_path": "https://api.schoology.com/v1/attachment/1/source/x",
        "extension": "pdf"}]}}}

    def test_files_are_extracted_with_their_checksum(self):
        item = FolderItem(id="10", type="document", title="Diary", path=("Class Information",))
        f = SchoologyAPI.files_of(self.DOC, item)[0]
        assert (f.filename, f.filesize, f.md5) == ("Diary.pdf", 1024, "abc123")
        assert f.folder_path == ("Class Information",)

    def test_a_file_without_a_download_path_is_dropped(self):
        doc = {"attachments": {"files": {"file": [{"filename": "x.pdf"}]}}}
        assert SchoologyAPI.files_of(doc, FolderItem("1", "document", "x")) == []

    def test_document_with_no_attachments(self):
        assert SchoologyAPI.files_of({}, FolderItem("1", "document", "x")) == []

    def test_videos_and_links_are_reported_separately(self):
        doc = {"attachments": {
            "videos": {"video": [{"url": "https://youtu.be/1"}]},
            "links": {"link": [{"url": "https://example.com"}]}}}
        assert set(SchoologyAPI.links_of(doc)) == {
            ("video", "https://youtu.be/1"), ("link", "https://example.com")}

    def test_fingerprint_prefers_the_checksum(self):
        item = FolderItem("10", "document", "Diary")
        assert SchoologyAPI.files_of(self.DOC, item)[0].fingerprint == "abc123"

    def test_fingerprint_falls_back_to_size_and_timestamp(self):
        f = MaterialFile("1", "t", "f.pdf", 99, "", 12345, "", "u")
        assert f.fingerprint == "99:12345"
