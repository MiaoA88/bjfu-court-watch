import json

import pytest

from src.notify import (
    MAX_CONTENT_CHARS,
    SPT_ENDPOINT,
    ConsoleNotifier,
    LoggingNotifier,
    NotifyError,
    WxPusherSpt,
    build_notifier,
)


class FakeResponse:
    def __init__(self, payload):
        self.text = json.dumps(payload, ensure_ascii=False)


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return FakeResponse(self.payload)


def test_sends_structured_markdown_payload():
    session = FakeSession({"code": 1000, "msg": "ok"})
    sender = WxPusherSpt("SPT_abc", session=session)
    sender.send("标题", "**正文**", "https://example.test/x")

    call = session.calls[0]
    assert call["url"] == SPT_ENDPOINT
    body = call["json"]
    assert body["spt"] == "SPT_abc"
    assert body["content"] == "**正文**"
    assert body["summary"] == "标题"
    assert body["contentType"] == 3
    assert body["url"] == "https://example.test/x"


def test_non_1000_code_raises():
    session = FakeSession({"code": 1001, "msg": "content too long"})
    with pytest.raises(NotifyError) as info:
        WxPusherSpt("SPT_abc", session=session).send("t", "c")
    assert "1001" in str(info.value)


def test_non_json_response_raises():
    class Broken:
        text = "<html>502</html>"

    class Session:
        def post(self, url, **kwargs):
            return Broken()

    with pytest.raises(NotifyError):
        WxPusherSpt("SPT_abc", session=Session()).send("t", "c")


def test_empty_spt_is_rejected():
    with pytest.raises(NotifyError):
        WxPusherSpt("  ")


def test_oversized_content_is_truncated():
    session = FakeSession({"code": 1000})
    WxPusherSpt("SPT_abc", session=session).send("t", "x" * (MAX_CONTENT_CHARS + 500))
    assert len(session.calls[0]["json"]["content"]) <= MAX_CONTENT_CHARS + 20


def test_build_notifier_chooses_channels():
    assert isinstance(build_notifier("wxpusher_spt", "SPT_x", dry_run=True), ConsoleNotifier)
    assert isinstance(build_notifier("wxpusher_spt", "SPT_x"), WxPusherSpt)
    with pytest.raises(NotifyError):
        build_notifier("telegram", "x")


def test_missing_spt_degrades_to_logging_instead_of_failing():
    """没配 SPT 时不应该让任务每 5 分钟失败一次。"""
    notifier = build_notifier("wxpusher_spt", None)
    assert isinstance(notifier, LoggingNotifier)
    notifier.send("标题", "正文")
    assert len(notifier.sent) == 1


def test_logging_notifier_records_without_network():
    sink = LoggingNotifier()
    sink.send("t", "body", "https://x")
    assert sink.sent == [("t", "body", "https://x")]


def test_delivers_flag_guards_dedup_bookkeeping():
    """只有真正投递的通道才能让调用方记账，否则配好 SPT 后会漏推。"""
    assert WxPusherSpt("SPT_x").delivers is True
    assert LoggingNotifier().delivers is False
    assert ConsoleNotifier().delivers is False


def test_console_notifier_records_messages():
    sink = ConsoleNotifier()
    sink.send("t", "body", None)
    assert sink.sent == [("t", "body", None)]


@pytest.mark.parametrize("payload", [[], None, 42, "ok"])
def test_non_object_json_is_reported_as_notify_error(payload):
    with pytest.raises(NotifyError):
        WxPusherSpt("SPT_test", session=FakeSession(payload)).send("t", "body")


def test_http_failure_is_not_acknowledged_even_with_success_body():
    class Session:
        def post(self, *a, **kw):
            response = FakeResponse({"code": 1000})
            response.status_code = 503
            return response
    with pytest.raises(NotifyError, match="HTTP 503"):
        WxPusherSpt("SPT_test", session=Session()).send("t", "body")
