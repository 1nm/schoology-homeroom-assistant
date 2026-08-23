import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """Keep the developer's own .env and API keys out of the tests."""
    import config

    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(config, "find_dotenv", lambda *args, **kwargs: "")
    for var in (
        "SCHOOLOGY_EMAIL",
        "SCHOOLOGY_PASSWORD",
        "SCHOOLOGY_SUBDOMAIN",
        "SCHOOLOGY_COURSE_ID",
        "HOMEROOM_CLASS",
        "HOMEROOM_COURSE_URL",
        "HOMEROOM_COURSE_NAME",
        "SUMMARY_SENDER_EMAIL",
        "SUMMARY_RECEIVER_EMAIL",
        "BCC_EMAILS",
        "GOOGLE_APP_PASSWORD",
        "TRANSLATION_LANGUAGES",
        "DATA_DIR",
        "HEADLESS",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    import config

    monkeypatch.setenv("SCHOOLOGY_EMAIL", "parent@example.com")
    monkeypatch.setenv("SCHOOLOGY_PASSWORD", "hunter2")
    monkeypatch.setenv("SCHOOLOGY_SUBDOMAIN", "example")
    monkeypatch.setenv("SUMMARY_SENDER_EMAIL", "sender@example.com")
    monkeypatch.setenv("GOOGLE_APP_PASSWORD", "app-password")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return config.Settings.load()
