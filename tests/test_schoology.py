from datetime import datetime

import pytest
import requests

from schoology import (
    Post,
    SchoologyClient,
    _feed_markup,
    _filename_from_disposition,
    _safe_filename,
    _unescape,
    parse_post_datetime,
)

NOW = datetime(2026, 8, 23, 12, 0)

FEED_HTML = """
<ul class="feed">
  <li class="first" id="edge-assoc-40000000002">
    <span class="small gray">Today at 4:12 pm</span>
    <a title="View user profile." href="/user/1">Sam Rivers</a>
    <img class="imagecache imagecache-profile_sm" src="/pic.jpg"/>
    <span class="update-body s-rte"><p>Good morning 世界</p><img src="/img/a.png"/></span>
    <div class="attachments clearfix">
      <a href="/attachment/9/download"><span aria-label="Keeping_a_Diary_at_Home.pdf"></span></a>
    </div>
  </li>
  <li id="edge-assoc-40000000001">
    <span class="small gray">Fri Nov 21, 2025 at 8:11 am</span>
    <a title="View user profile." href="/user/1">Sam Rivers</a>
    <span class="update-body s-rte"><p>Older post</p></span>
    <a class="show-more-link" href="/update/40000000001/full">Show more</a>
  </li>
</ul>
"""


class FakeResponse:
    def __init__(self, *, headers=None, body=b"", status=200, json_data=None, url=""):
        self.headers = headers or {}
        self._body = body
        self.status_code = status
        self._json = json_data
        self.url = url
        self.ok = status < 400

    @property
    def text(self):
        return self._body.decode() if isinstance(self._body, bytes) else self._body

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code}")

    def iter_content(self, chunk_size=1):
        body = self._body if isinstance(self._body, bytes) else self._body.encode()
        for i in range(0, len(body), chunk_size):
            yield body[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.response

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.response


def client_for(response):
    return SchoologyClient(FakeSession(response), "https://example.schoology.com", timeout=5)


class TestParsePostDatetime:
    def test_today(self):
        assert parse_post_datetime("Today at 7:04 am", NOW) == datetime(2026, 8, 23, 7, 4)

    def test_yesterday(self):
        # The old implementation raised here, which aborted the whole run.
        assert parse_post_datetime("Yesterday at 4:12 pm", NOW) == datetime(2026, 8, 22, 16, 12)

    def test_absolute_with_weekday(self):
        assert parse_post_datetime("Fri Nov 21, 2025 at 8:11 am", NOW) == datetime(2025, 11, 21, 8, 11)

    def test_absolute_without_weekday(self):
        assert parse_post_datetime("Nov 21, 2025 at 8:11 am", NOW) == datetime(2025, 11, 21, 8, 11)

    def test_uppercase_meridiem(self):
        assert parse_post_datetime("Today at 7:04 AM", NOW) == datetime(2026, 8, 23, 7, 4)

    @pytest.mark.parametrize("value", ["", "   ", "sometime last week", "2026-08-23"])
    def test_unrecognised_returns_none(self, value):
        assert parse_post_datetime(value, NOW) is None

    def test_post_falls_back_to_now_instead_of_raising(self):
        assert isinstance(Post(post_id="1", datetime_text="whenever").posted_at, datetime)


class TestUnescape:
    def test_restores_slashes(self):
        assert _unescape("https:\\/\\/x.com\\/a") == "https://x.com/a"

    def test_keeps_japanese_intact(self):
        # `bytes.decode("unicode-escape")` used to turn this into mojibake.
        assert _unescape("\\u4eca\\u65e5\\u306f") == "今日は"

    def test_joins_surrogate_pairs(self):
        assert _unescape("\\ud83c\\udf38") == "🌸"


class TestFeedMarkup:
    def test_reads_the_json_envelope(self):
        response = FakeResponse(json_data={"css": "", "content": "<li>post</li>"})
        assert _feed_markup(response) == "<li>post</li>"

    def test_falls_back_to_the_longest_string_in_the_envelope(self):
        response = FakeResponse(json_data={"a": "x", "b": "<li>much longer body</li>"})
        assert _feed_markup(response) == "<li>much longer body</li>"

    def test_unescapes_a_non_json_body(self):
        response = FakeResponse(body=b"<a href=\\/course\\/1>x</a>")
        assert _feed_markup(response) == "<a href=/course/1>x</a>"


class TestParsePosts:
    @pytest.fixture
    def posts(self):
        return client_for(FakeResponse()).parse_posts(FEED_HTML)

    def test_finds_every_post_not_only_the_first(self, posts):
        # The old class="first" selector only matched the top post of the feed.
        assert [p.post_id for p in posts] == ["40000000002", "40000000001"]

    def test_extracts_the_fields(self, posts):
        post = posts[0]
        assert post.author == "Sam Rivers"
        assert post.datetime_text == "Today at 4:12 pm"
        assert "Good morning 世界" in post.content
        assert post.images == ["/img/a.png"]
        assert post.show_more_href == ""

    def test_attachment_urls_are_absolute(self, posts):
        attachment = posts[0].attachments[0]
        assert attachment.url == "https://example.schoology.com/attachment/9/download"
        assert attachment.filename == "Keeping_a_Diary_at_Home.pdf"

    def test_parsing_does_no_network_io(self, posts):
        client = client_for(FakeResponse())
        client.parse_posts(FEED_HTML)
        assert client._session.calls == []

    def test_show_more_link_is_recorded_but_not_followed(self, posts):
        assert posts[1].show_more_href == "/update/40000000001/full"


class TestParentRealm:
    PARENT_HOME = (
        '<a href="/course/3333333333/preview/4444444444/parent">Music</a>'
        '<a href="/course/2222222222/preview/4444444444/parent">Homeroom: Section B</a>'
    )

    def test_finds_the_child_uid_for_the_right_course(self):
        client = client_for(FakeResponse(body=self.PARENT_HOME.encode()))
        assert client.find_child_uid("2222222222") == "4444444444"

    def test_no_preview_link_means_no_uid(self):
        client = client_for(FakeResponse(body=b"<a href=/home>home</a>"))
        assert client.find_child_uid("2222222222") == ""

    def test_enters_the_course_through_the_preview_url(self):
        client = client_for(FakeResponse(body=self.PARENT_HOME.encode()))
        assert client.enter_course_as_parent("2222222222") == "4444444444"
        assert client._session.calls[-1][1] == (
            "https://example.schoology.com/course/2222222222/preview/4444444444/parent"
        )

    def test_a_known_uid_skips_the_lookup(self):
        client = client_for(FakeResponse(body=b""))
        client.enter_course_as_parent("2222222222", child_uid="999")
        assert [url for _, url, _ in client._session.calls] == [
            "https://example.schoology.com/course/2222222222/preview/999/parent"
        ]

    def test_a_direct_account_without_a_preview_link_is_left_alone(self):
        client = client_for(FakeResponse(body=b"<html>no preview links</html>"))
        assert client.enter_course_as_parent("2222222222") == ""


class TestExpand:
    def test_replaces_the_body(self):
        client = client_for(FakeResponse(json_data={"update": "<p>The full text</p>"}))
        post = Post(post_id="1", content="Truncated", show_more_href="/update/1/full")

        client.expand(post)

        assert post.content == "The full text"
        assert client._session.calls[0][1] == "https://example.schoology.com/update/1/full"

    def test_no_link_means_no_request(self):
        client = client_for(FakeResponse())
        client.expand(Post(post_id="1", content="Short"))
        assert client._session.calls == []

    def test_keeps_the_truncated_body_when_the_request_fails(self):
        client = client_for(FakeResponse(status=500))
        post = Post(post_id="1", content="Truncated", show_more_href="/update/1/full")
        client.expand(post)
        assert post.content == "Truncated"


class TestDownload:
    def test_saves_a_named_attachment(self, tmp_path):
        client = client_for(
            FakeResponse(
                headers={
                    "Content-Disposition": 'attachment; filename="Diary.pdf"',
                    "Content-Type": "application/pdf",
                },
                body=b"%PDF-1.4 fake",
            )
        )
        path = client._download("https://example.schoology.com/a/1", tmp_path, 50)
        assert path == tmp_path / "Diary.pdf"
        assert path.read_bytes() == b"%PDF-1.4 fake"

    def test_refuses_an_unnamed_html_body(self, tmp_path):
        # This is what filled the attachments directory with 16k copies of the
        # Schoology home page: a redirect to HTML, saved under a random name.
        client = client_for(
            FakeResponse(headers={"Content-Type": "text/html; charset=utf-8"}, body=b"<html>Home</html>")
        )
        assert client._download("https://example.schoology.com/a/1", tmp_path, 50) is None
        assert list(tmp_path.iterdir()) == []

    def test_unnamed_binary_gets_a_stable_name(self, tmp_path):
        def fresh():
            return client_for(FakeResponse(headers={"Content-Type": "image/png"}, body=b"\x89PNG"))

        first = fresh()._download("https://example.schoology.com/a/1", tmp_path, 50)
        second = fresh()._download("https://example.schoology.com/a/1", tmp_path, 50)

        assert first == second
        assert first.suffix == ".png"
        assert len(list(tmp_path.iterdir())) == 1

    def test_existing_file_is_not_refetched(self, tmp_path):
        (tmp_path / "Diary.pdf").write_bytes(b"already here")
        client = client_for(
            FakeResponse(
                headers={"Content-Disposition": 'attachment; filename="Diary.pdf"'},
                body=b"new bytes",
            )
        )
        path = client._download("https://example.schoology.com/a/1", tmp_path, 50)
        assert path.read_bytes() == b"already here"

    def test_declared_size_over_the_cap_is_skipped(self, tmp_path):
        client = client_for(
            FakeResponse(
                headers={
                    "Content-Disposition": 'attachment; filename="Huge.pdf"',
                    "Content-Length": str(200 * 1024 * 1024),
                },
                body=b"x",
            )
        )
        assert client._download("https://example.schoology.com/a/1", tmp_path, 50) is None
        assert list(tmp_path.iterdir()) == []

    def test_undeclared_oversize_body_is_aborted_mid_stream(self, tmp_path):
        client = client_for(
            FakeResponse(
                headers={"Content-Disposition": 'attachment; filename="Huge.pdf"'},
                body=b"x" * 4096,
            )
        )
        assert client._download("https://example.schoology.com/a/1", tmp_path, 0.001) is None
        assert list(tmp_path.iterdir()) == []

    def test_a_failing_download_leaves_the_post_usable(self, tmp_path):
        class ExplodingSession(FakeSession):
            def get(self, url, **kwargs):
                raise requests.ConnectionError("boom")

        client = SchoologyClient(ExplodingSession(None), "https://example.schoology.com")
        post = client.parse_posts(FEED_HTML)[0]

        client.download_attachments(post, tmp_path)  # must not raise

        assert post.attachments[0].path is None
        assert post.attachments[0].text == ""


class TestFilenames:
    @pytest.mark.parametrize(
        "header,expected",
        [
            ('attachment; filename="Diary.pdf"', "Diary.pdf"),
            ("attachment; filename=Diary.pdf", "Diary.pdf"),
            ("attachment; filename*=UTF-8''%E6%97%A5%E6%9C%AC.pdf", "日本.pdf"),
            ("", ""),
            ("inline", ""),
        ],
    )
    def test_from_disposition(self, header, expected):
        assert _filename_from_disposition(header) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("a/b.pdf", "a.b.pdf"),
            ("../../etc/passwd", "etc.passwd"),
            ("weird*name?.pdf", "weird_name_.pdf"),
            ("", "attachment"),
        ],
    )
    def test_safe_filename(self, raw, expected):
        assert _safe_filename(raw) == expected

    def test_length_is_capped(self):
        assert len(_safe_filename("x" * 500)) == 120
