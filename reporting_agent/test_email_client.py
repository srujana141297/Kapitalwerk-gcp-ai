"""Unit tests for email_client.py. Run with: pytest test_email_client.py -v

SendGrid tests mock urllib.request.urlopen -- no real network call, no real
"""


import io
import json
from unittest.mock import MagicMock, patch

import pytest
from email_client import InlineAttachment, SendGridEmailNotifier


def test_missing_api_key_raises():
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(RuntimeError, match="SENDGRID_API_KEY"):
            SendGridEmailNotifier()


def test_missing_from_email_raises():
    with patch.dict("os.environ", {"SENDGRID_API_KEY": "SG.fake"}, clear=True):
        with pytest.raises(RuntimeError, match="SENDGRID_FROM_EMAIL"):
            SendGridEmailNotifier()


def _fake_response(status=202, message_id="abc123"):
    resp = MagicMock()
    resp.status = status
    resp.headers = {"X-Message-Id": message_id}
    resp.__enter__ = lambda self: resp
    resp.__exit__ = lambda self, *a: False
    return resp


def test_sandbox_mode_flag_is_sent_in_payload():
    env = {
        "SENDGRID_API_KEY": "SG.fake",
        "SENDGRID_FROM_EMAIL": "reports@kapitalwerknxt.bank",
        "SENDGRID_SANDBOX_MODE": "true",
    }
    with patch.dict("os.environ", env, clear=True):
        notifier = SendGridEmailNotifier()
        assert notifier.sandbox_mode is True

        with patch("email_client.urllib.request.urlopen", return_value=_fake_response()) as mock_open:
            sent = notifier.send(
                to="finance@example.com",
                subject="Test subject",
                body="plain text body",
                html="<p>html body</p>",
                inline_attachments=[InlineAttachment("logo", "logo.png", "image/png", b"\x89PNG\r\n")],
            )

        request = mock_open.call_args[0][0]
        payload = json.loads(request.data.decode("utf-8"))
        assert payload["mail_settings"]["sandbox_mode"]["enable"] is True
        assert payload["personalizations"][0]["to"][0]["email"] == "finance@example.com"
        assert {"type": "text/plain", "value": "plain text body"} in payload["content"]
        assert {"type": "text/html", "value": "<p>html body</p>"} in payload["content"]
        assert payload["attachments"][0]["content_id"] == "logo"
        assert request.headers["Authorization"] == "Bearer SG.fake"
        assert sent.provider_response["sandbox_mode"] is True
        assert sent.provider_response["message_id"] == "abc123"


def test_http_error_raises_with_response_body():
    env = {"SENDGRID_API_KEY": "SG.fake", "SENDGRID_FROM_EMAIL": "reports@kapitalwerknxt.bank"}
    with patch.dict("os.environ", env, clear=True):
        notifier = SendGridEmailNotifier()
        import urllib.error

        err = urllib.error.HTTPError(
            url="https://api.sendgrid.com/v3/mail/send",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b'{"errors":[{"message":"bad key"}]}'),
        )
        with patch("email_client.urllib.request.urlopen", side_effect=err):
            with pytest.raises(RuntimeError, match="bad key"):
                notifier.send(to="a@example.com", subject="s", body="b")
