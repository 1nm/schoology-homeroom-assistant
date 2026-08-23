import pytest

from config import ConfigError, Settings, course_id_from_url


class TestCourseIdFromUrl:
    def test_extracts_id(self):
        assert course_id_from_url("https://x.schoology.com/course/1111111111") == "1111111111"

    def test_extracts_id_with_trailing_path(self):
        assert course_id_from_url("https://x.schoology.com/course/1111111111/feed?filter=1") == "1111111111"

    def test_no_match(self):
        assert course_id_from_url("https://x.schoology.com/home") == ""
        assert course_id_from_url("") == ""


class TestSettings:
    def test_missing_required_variable_is_reported_by_name(self, monkeypatch):
        monkeypatch.setenv("SCHOOLOGY_EMAIL", "parent@example.com")
        with pytest.raises(ConfigError, match="SCHOOLOGY_PASSWORD"):
            Settings.load()

    def test_course_id_falls_back_to_the_course_url(self, settings, monkeypatch):
        monkeypatch.setenv("HOMEROOM_COURSE_URL", "https://example.schoology.com/course/123456")
        assert Settings.load().course_id == "123456"

    def test_explicit_course_id_wins(self, settings, monkeypatch):
        monkeypatch.setenv("HOMEROOM_COURSE_URL", "https://example.schoology.com/course/123456")
        monkeypatch.setenv("SCHOOLOGY_COURSE_ID", "999")
        assert Settings.load().course_id == "999"

    def test_receiver_defaults_to_sender(self, settings):
        assert settings.receiver_email == "sender@example.com"

    def test_bcc_is_split_and_stripped(self, settings, monkeypatch):
        monkeypatch.setenv("BCC_EMAILS", " a@example.com , b@example.com ,")
        assert Settings.load().bcc_emails == ["a@example.com", "b@example.com"]

    def test_paths_hang_off_the_data_dir(self, settings, tmp_path):
        assert settings.state_file == tmp_path / ".sadc.conf"
        assert settings.cookie_file == tmp_path / ".schoology_cookies.json"
        assert settings.attachments_dir == tmp_path / "attachments"

    def test_base_url(self, settings):
        assert settings.base_url == "https://example.schoology.com"
