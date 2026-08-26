"""The orchestration guarantees: never re-send, never silently drop, never pay
for a post twice."""

from datetime import datetime

import pytest

import main
from schoology import Attachment, Post
from state import State


class FakeClient:
    def __init__(self, posts):
        self.posts = posts
        self.expanded = []
        self.downloaded = []
        self.entered = []
        self.fetched = []

    def enter_course_as_parent(self, course_id, child_uid=""):
        self.entered.append(course_id)
        return "4444444444"

    def fetch_feed(self, course_id):
        self.fetched.append(course_id)
        return "<feed/>"

    def parse_posts(self, html):
        return self.posts

    def expand(self, post):
        self.expanded.append(post.post_id)

    def download_attachments(self, post, dest_dir, *args):
        self.downloaded.append(post.post_id)


@pytest.fixture
def fake_pipeline(monkeypatch):
    """Stub out the model calls and SMTP; record what each post produced."""
    sent = []
    monkeypatch.setattr(main, "summarize", lambda text, model, posted_on="": f"# AI Summary\n{text[:20]}")
    monkeypatch.setattr(main, "translate", lambda content, language, model: f"[{language}] {content}")
    monkeypatch.setattr(main, "markdown_to_html", lambda md: f"<div>{md}</div>")
    def record(settings, subject, html, attachments=(), bcc=None):
        sent.append((subject, html, list(attachments), bcc))

    monkeypatch.setattr(main, "send_email", record)
    return sent


class FakeAuth:
    """Stands in for the browser/cookie dance: hands back a session and, if the
    course id was not already known, reports the one it discovered."""

    def __init__(self, discovered="1111111111"):
        self.course_id = ""
        self.screenshot = None
        self._discovered = discovered

    def session(self):
        self.course_id = self.course_id or self._discovered
        return object()


def make_post(post_id, text="Today at 7:04 am", content="Body"):
    return Post(post_id=post_id, datetime_text=text, author="Sam Rivers", content=content)


class TestHandlePost:
    def test_records_the_post_only_after_the_mail_is_accepted(self, settings, fake_pipeline, tmp_path):
        state = State(path=settings.state_file)
        client = FakeClient([])

        main.handle_post(settings, client, make_post("111"), state)

        assert state.is_sent("111")
        assert State.load(settings.state_file).is_sent("111"), "state must be flushed immediately"

    def test_a_failed_send_leaves_the_post_unrecorded(self, settings, fake_pipeline, monkeypatch):
        def explode(*args, **kwargs):
            raise RuntimeError("smtp down")

        monkeypatch.setattr(main, "send_email", explode)
        state = State(path=settings.state_file)

        with pytest.raises(RuntimeError):
            main.handle_post(settings, FakeClient([]), make_post("111"), state)

        assert not state.is_sent("111")

    def test_email_carries_the_summary_and_every_translation(self, settings, fake_pipeline):
        main.handle_post(settings, FakeClient([]), make_post("111"), State(path=settings.state_file))

        subject, html, _, _ = fake_pipeline[0]
        assert subject == f"Homeroom Updates {datetime.now():%Y%m%d}"
        assert "[Japanese]" in html and "[Chinese]" in html
        assert html.count("<hr/>") == 3  # summary + two translations


class TestCollectAttachments:
    def test_gathers_text_and_paths(self, tmp_path):
        pdf = tmp_path / "a.pdf"
        pdf.write_bytes(b"x" * 100)
        post = Post(post_id="1", attachments=[Attachment(url="u", filename="a.pdf", path=pdf, text="hello")])

        paths, text = main.collect_attachments(post, budget_mb=10)

        assert paths == [pdf]
        assert text == "hello"

    def test_stops_attaching_once_the_budget_is_gone(self, tmp_path):
        small = tmp_path / "small.pdf"
        small.write_bytes(b"x" * 10)
        big = tmp_path / "big.pdf"
        big.write_bytes(b"x" * 4096)
        post = Post(
            post_id="1",
            attachments=[
                Attachment(url="u", filename="small.pdf", path=small),
                Attachment(url="u", filename="big.pdf", path=big),
            ],
        )

        paths, _ = main.collect_attachments(post, budget_mb=0.001)

        assert paths == [small]

    def test_text_is_kept_even_for_files_that_are_too_big_to_attach(self, tmp_path):
        big = tmp_path / "big.pdf"
        big.write_bytes(b"x" * 4096)
        post = Post(post_id="1", attachments=[Attachment(url="u", filename="b.pdf", path=big, text="summary me")])

        paths, text = main.collect_attachments(post, budget_mb=0.001)

        assert paths == []
        assert text == "summary me"


class TestRun:
    @pytest.fixture
    def auth(self):
        return FakeAuth()

    def _run(self, settings, auth, posts, monkeypatch):
        client = FakeClient(posts)
        monkeypatch.setattr(main, "SchoologyClient", lambda *a, **kw: client)
        return client, main.run(settings, auth)

    def test_skips_posts_that_were_already_sent(self, settings, auth, fake_pipeline, monkeypatch):
        state = State(path=settings.state_file)
        state.mark_sent("111", "old subject", "2026-08-23T07:04")
        state.save()

        client, code = self._run(settings, auth, [make_post("222"), make_post("111")], monkeypatch)

        assert code == 0
        # The expensive work only happens for the new post -- the old script
        # downloaded every attachment of every post on every single run.
        assert client.downloaded == ["222"]
        assert len(fake_pipeline) == 1
        # Both posts get an archive entry, though: the sent one is backfilled.
        assert sorted(p.name.split("-")[-1] for p in settings.posts_dir.glob("*.md")) == [
            "111.md", "222.md"
        ]

    def test_processes_oldest_first(self, settings, auth, fake_pipeline, monkeypatch):
        posts = [make_post("333", content="newest"), make_post("222"), make_post("111", content="oldest")]
        client, code = self._run(settings, auth, posts, monkeypatch)

        assert code == 0
        assert client.downloaded == ["111", "222", "333"]

    def test_one_bad_post_does_not_block_the_rest(self, settings, auth, fake_pipeline, monkeypatch):
        notified = []
        monkeypatch.setattr(main, "notify_error", lambda settings, msg, *a: notified.append(msg))

        def flaky(text, model, posted_on=""):
            if "explode" in text:
                raise RuntimeError("model refused")
            return "# AI Summary"

        monkeypatch.setattr(main, "summarize", flaky)
        posts = [make_post("222"), make_post("111", content="explode")]

        client, code = self._run(settings, auth, posts, monkeypatch)

        assert code == 1
        assert len(fake_pipeline) == 1, "the healthy post still went out"
        assert "Post 111" in notified[0]
        # The failed post stays unrecorded, so the next run retries it.
        assert not State.load(settings.state_file).is_sent("111")
        assert State.load(settings.state_file).is_sent("222")

    def test_missing_course_id_is_an_error(self, settings, monkeypatch):
        with pytest.raises(RuntimeError, match="No course id"):
            self._run(settings, FakeAuth(discovered=""), [], monkeypatch)

    def test_no_bcc_forces_an_empty_bcc_list(self, settings, auth, fake_pipeline, monkeypatch):
        client = FakeClient([make_post("111")])
        monkeypatch.setattr(main, "SchoologyClient", lambda *a, **kw: client)

        main.run(settings, auth, no_bcc=True)

        assert fake_pipeline[0][3] == []

    def test_dry_run_sends_nothing_and_records_nothing(self, settings, auth, fake_pipeline, monkeypatch):
        client = FakeClient([make_post("111")])
        monkeypatch.setattr(main, "SchoologyClient", lambda *a, **kw: client)

        assert main.run(settings, auth, dry_run=True) == 0
        assert fake_pipeline == []
        assert not State.load(settings.state_file).is_sent("111")

    def test_enters_the_parent_realm_before_reading_the_feed(self, settings, auth, monkeypatch):
        # Without this the feed answers "There are no posts" for a parent account.
        client, code = self._run(settings, auth, [], monkeypatch)
        assert client.entered == ["1111111111"]
        assert client.fetched == ["1111111111"]

    def test_course_id_is_remembered_for_the_next_run(self, settings, auth, monkeypatch):
        self._run(settings, auth, [], monkeypatch)
        assert State.load(settings.state_file).course_id == "1111111111"
