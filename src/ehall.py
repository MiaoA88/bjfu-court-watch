"""ehall（北林）HTTP 客户端。

实测事实（2026-11 探测）：
  * 接口前缀是 https://ehall.bjfu.edu.cn/site/...，不是 /v2/。
  * 统一信封 {"e": 码, "m": 文案, "d": 数据}。
  * 未登录时 e = "10042"，m = "该操作需要登录，请登录后重试"。
  * 鉴权只靠 Cookie，不需要额外请求头。

因此本模块只做四件事：拼 URL、带 Cookie、解析信封、把失败分类成
AuthExpired / ApiError / SchemaChanged / TransportError，让上层决定要不要告警。
"""

from __future__ import annotations

import json
import logging
import time as _time
from dataclasses import dataclass
from typing import Any, Protocol

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://ehall.bjfu.edu.cn"
REFERER = f"{BASE_URL}/v2/reserve/hallView"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

AUTH_EXPIRED_CODE = "10042"
# 金智系产品成功码通常是 "0"；留几个常见变体以免误判。
SUCCESS_CODES = {"0", "1000", "200", "success"}

REDIRECT_STATUS = {301, 302, 303, 307, 308}


class EhallError(Exception):
    """ehall 相关错误的基类。"""


class AuthExpired(EhallError):
    """登录态失效：e=10042，或被重定向到统一身份认证页。"""


class ApiError(EhallError):
    """接口明确返回了业务错误码。"""

    def __init__(self, code: str, message: str, path: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message} ({path})")
        self.code = code
        self.message = message
        self.path = path
        self.data = data


class SchemaChanged(EhallError):
    """响应不是预期的 JSON 信封，多半是接口改版或返回了 HTML。"""


class TransportError(EhallError):
    """网络层 / HTTP 状态码层面的失败，重试后仍不成功。"""


@dataclass(frozen=True)
class Envelope:
    code: str
    message: str
    data: Any

    @property
    def success(self) -> bool:
        return self.code.lower() in {c.lower() for c in SUCCESS_CODES}


def parse_envelope(body: str, path: str) -> Envelope:
    """把响应体解析成信封；结构不符直接抛 SchemaChanged（绝不静默当作空数据）。"""
    text = (body or "").strip()
    if not text:
        raise SchemaChanged(f"{path} 返回空响应体")
    if text[0] not in "{[":
        # 典型情况：被 WAF 拦了 / 被重定向到登录页 / 返回 HTML
        snippet = text[:200].replace("\n", " ")
        raise SchemaChanged(f"{path} 返回的不是 JSON，开头为：{snippet!r}")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SchemaChanged(f"{path} 的 JSON 无法解析：{exc}") from exc

    if not isinstance(payload, dict) or "e" not in payload:
        keys = list(payload)[:10] if isinstance(payload, dict) else type(payload).__name__
        raise SchemaChanged(f"{path} 缺少信封字段 e，实际结构：{keys}")

    code = str(payload.get("e"))
    message = str(payload.get("m") or "")
    data = payload.get("d", payload.get("data"))
    return Envelope(code=code, message=message, data=data)


class HttpSession(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...


class EhallClient:
    """最小可用客户端：一次调用一次请求，失败分类明确。"""

    def __init__(
        self,
        cookie: str,
        user_agent: str | None = None,
        *,
        base_url: str = BASE_URL,
        timeout: float = 15.0,
        retries: int = 2,
        backoff: float = 1.5,
        session: HttpSession | None = None,
        sleep=_time.sleep,
    ) -> None:
        if not cookie or not cookie.strip():
            raise ValueError("Cookie 为空：请配置 EHALL_COOKIE")
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._retries = max(0, int(retries))
        self._backoff = backoff
        self._sleep = sleep
        self._session: HttpSession = session or requests.Session()
        self._headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Cookie": cookie.strip(),
            "Referer": REFERER,
            "User-Agent": (user_agent or DEFAULT_UA).strip(),
        }

    # ------------------------------------------------------------------ 底层
    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        form: dict[str, Any] | None = None,
    ) -> Envelope:
        """发一次请求并返回信封（不判断业务码是否成功）。"""
        url = self._base + path
        last_error: Exception | None = None

        for attempt in range(self._retries + 1):
            if attempt:
                delay = self._backoff * attempt
                log.debug("第 %d 次重试 %s，等待 %.1fs", attempt, path, delay)
                self._sleep(delay)
            try:
                response = self._session.request(
                    method.upper(),
                    url,
                    params=params,
                    json=json_body,
                    data=form,
                    headers=self._headers,
                    timeout=self._timeout,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                last_error = TransportError(f"{path} 请求失败：{exc}")
                continue

            status = getattr(response, "status_code", 0)
            if status in REDIRECT_STATUS:
                location = _header(response, "Location") or ""
                raise AuthExpired(f"{path} 被重定向（{status} → {location[:120]}），登录态可能已失效")
            if status == 429 or status >= 500:
                last_error = TransportError(f"{path} 返回 HTTP {status}")
                continue
            if status != 200:
                raise TransportError(f"{path} 返回 HTTP {status}")

            body = getattr(response, "text", "")
            if not body:
                body = _decode(getattr(response, "content", b""))
            return parse_envelope(body, path)

        raise last_error or TransportError(f"{path} 重试后仍失败")

    def data(self, method: str, path: str, **kwargs: Any) -> Any:
        """发请求并只返回 d；失败抛具体异常。"""
        envelope = self.request(method, path, **kwargs)
        return self._unwrap(envelope, path)

    @staticmethod
    def _unwrap(envelope: Envelope, path: str) -> Any:
        if envelope.code == AUTH_EXPIRED_CODE:
            raise AuthExpired(f"{path}：{envelope.message or '需要登录'}")
        if not envelope.success:
            raise ApiError(envelope.code, envelope.message, path, envelope.data)
        return envelope.data


def _header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        value = headers.get(name)
    except AttributeError:
        return None
    return None if value is None else str(value)


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace") if raw else ""
