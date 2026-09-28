"""Slack is a mirror of the mail: off by default, Markdown when on, never fatal."""

from dataclasses import replace

import pytest

import main
import slack_notify
from materials import Change
from schoology import Post
from state import State


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.text = str(payload)

    def json(self):
        return self.payload


@pytest.fixture
def slack(settings):
    return replace(settings, slack_bot_token="xoxb-test", slack_channel="C123",
                   slack_mentions=["U1", "U2"])


@pytest.fixture
def posted(monkeypatch):
    calls = []
    monkeypatch.setattr(slack_notify.requests, "post",
                        lambda url, **k: calls.append(k) or FakeResponse({"ok": True}))
    return calls


def test_off_without_a_token(settings, posted):
    slack_notify.notify_post(settings, "On Monday, Sam posted:", "# Summary", {"Chinese": "摘要"})
    assert posted == []


def test_post_mentions_parents_and_prefers_chinese(slack, posted):
    slack_notify.notify_post(
        slack, "On Monday, Sam posted:", "# Summary", {"Japanese": "要約", "Chinese": "摘要"}
    )

    assert len(posted) == 1
    body = posted[0]["json"]
    assert body["channel"] == "C123"
    assert body["blocks"][0]["type"] == "markdown"
    text = body["blocks"][0]["text"]
    assert text.startswith("<@U1> <@U2>")
    assert "摘要" in text and "要約" not in text and "# Summary" not in text
    assert posted[0]["headers"]["Authorization"] == "Bearer xoxb-test"


def test_materials_lists_titles_only(slack, posted):
    slack_notify.notify_materials(slack, [Change("new", "Homework", "Week 8", "w8.html")], [])

    text = posted[0]["json"]["blocks"][0]["text"]
    assert "Homework — Week 8" in text and "Updated" not in text


def test_a_slack_failure_is_logged_not_raised(slack, monkeypatch, caplog):
    monkeypatch.setattr(slack_notify.requests, "post",
                        lambda url, **k: FakeResponse({"ok": False, "error": "channel_not_found"}))

    slack_notify.notify_post(slack, "header", "summary", {})

    assert "channel_not_found" in caplog.text


class FakeClient:
    def expand(self, post):
        pass

    def download_attachments(self, post, dest_dir, *args):
        pass


def test_handle_post_notifies_slack_after_recording(settings, monkeypatch):
    monkeypatch.setattr(main, "summarize", lambda text, model, posted_on="": "# Summary")
    monkeypatch.setattr(main, "translate", lambda content, language, model: f"[{language}] {content}")
    monkeypatch.setattr(main, "send_email", lambda *a, **k: None)
    seen = []
    monkeypatch.setattr(
        main.slack_notify, "notify_post",
        lambda s, header, summary, translations: seen.append((s, header, translations)),
    )
    state = State(path=settings.state_file)
    post = Post(post_id="222", datetime_text="Today at 7:04 am", author="Sam Rivers", content="Body")

    main.handle_post(settings, FakeClient(), post, state)

    assert state.is_sent("222")
    assert len(seen) == 1
    assert "Sam Rivers posted" in seen[0][1]
    assert set(seen[0][2]) == set(settings.translation_languages)
