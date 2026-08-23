import json

import state
from state import State


class TestLoad:
    def test_missing_file_starts_empty(self, tmp_path):
        loaded = State.load(tmp_path / ".sadc.conf")
        assert loaded.sent == {}
        assert loaded.course_id == ""

    def test_migrates_the_legacy_format(self, tmp_path):
        path = tmp_path / ".sadc.conf"
        path.write_text(
            json.dumps(
                {
                    "course_id": "1111111111",
                    "downloaded": {},
                    "updates": {
                        "451103": {"datetime": "Today at 7:04 am", "html_content": "<p>x</p>"},
                        "451910": {"datetime": "Fri Nov 21, 2025 at 8:11 am"},
                    },
                }
            )
        )

        loaded = State.load(path)

        assert loaded.course_id == "1111111111"
        assert loaded.is_sent("451103")
        assert loaded.is_sent("451910")
        assert loaded.sent["451103"]["posted_at"] == "Today at 7:04 am"

    def test_migration_drops_the_stored_post_bodies(self, tmp_path):
        path = tmp_path / ".sadc.conf"
        big = {"datetime": "Today at 7:04 am", "html_content": "x" * 10000}
        path.write_text(json.dumps({"course_id": "1", "updates": {"1": big}}))

        State.load(path).save()

        assert path.stat().st_size < 500


class TestSave:
    def test_roundtrip(self, tmp_path):
        path = tmp_path / ".sadc.conf"
        original = State(path=path, course_id="42")
        original.mark_sent("abc", "K3B Homeroom Updates 20260101", "2026-01-01T07:04")
        original.save()

        reloaded = State.load(path)
        assert reloaded.course_id == "42"
        assert reloaded.is_sent("abc")
        assert reloaded.sent["abc"]["subject"] == "K3B Homeroom Updates 20260101"
        assert reloaded.sent["abc"]["sent_at"]

    def test_leaves_no_temporary_file_behind(self, tmp_path):
        path = tmp_path / ".sadc.conf"
        State(path=path).save()
        assert [p.name for p in tmp_path.iterdir()] == [".sadc.conf"]

    def test_prunes_to_the_cap_keeping_the_newest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(state, "MAX_ENTRIES", 3)
        s = State(path=tmp_path / ".sadc.conf")
        for i in range(5):
            s.mark_sent(f"post{i}", "subject", "2026-01-01T07:04")
        s.save()

        assert list(State.load(s.path).sent) == ["post2", "post3", "post4"]
