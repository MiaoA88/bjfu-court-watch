import pytest

from src.ehall import (
    ApiError,
    AuthExpired,
    EhallClient,
    SchemaChanged,
    TransportError,
    parse_envelope,
)


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.content = text.encode("utf-8")


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(session, **kwargs):
    kwargs.setdefault("sleep", lambda _s: None)
    return EhallClient("sessionId=abc", session=session, **kwargs)


def test_parse_envelope_success():
    env = parse_envelope('{"e":"0","m":"ok","d":[1,2]}', "/x")
    assert env.success is True
    assert env.data == [1, 2]


def test_parse_envelope_rejects_html():
    with pytest.raises(SchemaChanged):
        parse_envelope("<html>blocked</html>", "/x")


def test_parse_envelope_rejects_missing_e():
    with pytest.raises(SchemaChanged):
        parse_envelope('{"data": []}', "/x")


def test_parse_envelope_rejects_empty():
    with pytest.raises(SchemaChanged):
        parse_envelope("   ", "/x")


def test_data_raises_auth_expired_on_10042():
    body = '{"e":"10042","m":"该操作需要登录，请登录后重试","d":[]}'
    client = make_client(FakeSession(FakeResponse(text=body)))
    with pytest.raises(AuthExpired):
        client.data("GET", "/site/reservation/hall")


def test_data_raises_api_error_on_other_codes():
    client = make_client(FakeSession(FakeResponse(text='{"e":"50001","m":"参数错误","d":null}')))
    with pytest.raises(ApiError) as info:
        client.data("GET", "/site/reservation/hall")
    assert info.value.code == "50001"


def test_redirect_is_treated_as_auth_expired():
    resp = FakeResponse(status_code=302, text="", headers={"Location": "https://cas/login"})
    client = make_client(FakeSession(resp))
    with pytest.raises(AuthExpired):
        client.request("GET", "/site/reservation/hall")


def test_retries_then_succeeds_on_5xx():
    session = FakeSession(
        FakeResponse(status_code=502, text="bad gateway"),
        FakeResponse(text='{"e":"0","m":"","d":{"ok":true}}'),
    )
    client = make_client(session, retries=2)
    assert client.data("GET", "/site/reservation/hall") == {"ok": True}
    assert len(session.calls) == 2


def test_gives_up_after_retries():
    session = FakeSession(*[FakeResponse(status_code=500, text="err")] * 3)
    client = make_client(session, retries=2)
    with pytest.raises(TransportError):
        client.data("GET", "/site/reservation/hall")


def test_network_exception_is_wrapped():
    import requests

    session = FakeSession(requests.ConnectionError("boom"))
    client = make_client(session, retries=0)
    with pytest.raises(TransportError):
        client.data("GET", "/site/reservation/hall")


def test_empty_cookie_is_rejected():
    with pytest.raises(ValueError):
        EhallClient("   ")


def test_cookie_and_ua_headers_are_sent():
    session = FakeSession(FakeResponse(text='{"e":"0","m":"","d":null}'))
    client = EhallClient("sessionId=abc; route=x", user_agent="UA-TEST", session=session)
    client.request("GET", "/site/reservation/hall", params={"hallId": 54})
    call = session.calls[0]
    assert call["url"] == "https://ehall.bjfu.edu.cn/site/reservation/hall"
    assert call["headers"]["Cookie"] == "sessionId=abc; route=x"
    assert call["headers"]["User-Agent"] == "UA-TEST"
    assert call["params"] == {"hallId": 54}
    assert call["allow_redirects"] is False
