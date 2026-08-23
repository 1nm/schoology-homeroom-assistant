import email
import smtplib
from types import SimpleNamespace

import pytest

import mailer
from mailer import EmailError, notify_error, send_email


class FakeSMTP:
    """Records what would have gone out; raises instead if told to."""

    sent = []

    def __init__(self, host, port, timeout=None, fail=False):
        self.fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        if self.fail:
            raise smtplib.SMTPAuthenticationError(535, b"nope")

    def sendmail(self, sender, recipients, message):
        FakeSMTP.sent.append((sender, recipients, message))


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


class TestSendEmail:
    def test_sends_to_receiver_and_bcc(self, settings, smtp):
        send_email(settings, "Subject", "<p>hi</p>", bcc=["a@example.com"])

        sender, recipients, raw = smtp.sent[0]
        assert sender == "sender@example.com"
        assert recipients == ["sender@example.com", "a@example.com"]
        assert email.message_from_string(raw)["Subject"] == "Subject"

    def test_attaches_files(self, settings, smtp, tmp_path):
        pdf = tmp_path / "Diary.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")

        send_email(settings, "Subject", "<p>hi</p>", [pdf])

        raw = smtp.sent[0][2]
        assert "Diary.pdf" in raw

    def test_failure_raises_instead_of_being_swallowed(self, settings, monkeypatch):
        # The old send_email logged and returned, so the caller recorded a post as
        # delivered that had never left the machine.
        monkeypatch.setattr(
            mailer.smtplib, "SMTP_SSL", lambda *a, **kw: FakeSMTP(*a[:2], fail=True)
        )
        with pytest.raises(EmailError):
            send_email(settings, "Subject", "<p>hi</p>")


class TestNotifyError:
    def test_sends_the_first_failure(self, settings, smtp):
        notify_error(settings, "Traceback...\nRuntimeError: boom")
        assert len(smtp.sent) == 1

    def test_repeat_of_the_same_failure_is_suppressed(self, settings, smtp):
        for _ in range(5):
            notify_error(settings, "Traceback...\nRuntimeError: boom")
        assert len(smtp.sent) == 1

    def test_a_different_failure_still_gets_through(self, settings, smtp):
        notify_error(settings, "Traceback...\nRuntimeError: boom")
        notify_error(settings, "Traceback...\nValueError: other")
        assert len(smtp.sent) == 2

    def test_reports_how_many_were_suppressed_once_the_window_passes(self, settings, smtp, monkeypatch):
        notify_error(settings, "Traceback...\nRuntimeError: boom")
        notify_error(settings, "Traceback...\nRuntimeError: boom")
        notify_error(settings, "Traceback...\nRuntimeError: boom")

        later = mailer.time.time() + 24 * 3600
        monkeypatch.setattr(mailer, "time", SimpleNamespace(time=lambda: later))
        notify_error(settings, "Traceback...\nRuntimeError: boom")

        assert len(smtp.sent) == 2
        assert "2 identical failures were suppressed" in smtp.sent[1][2]

    def test_an_unsendable_notification_does_not_raise(self, settings, monkeypatch):
        monkeypatch.setattr(
            mailer.smtplib, "SMTP_SSL", lambda *a, **kw: FakeSMTP(*a[:2], fail=True)
        )
        notify_error(settings, "Traceback...\nRuntimeError: boom")  # must not raise
