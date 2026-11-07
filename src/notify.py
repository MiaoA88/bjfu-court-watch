"""推送通道。

默认实现 WxPusher 极简推送（SPT）：只需一个 SPT_ 令牌，不必建应用、不必拿 UID。
接口：POST https://wxpusher.zjiecode.com/api/send/message/simple-push
body：{spt, content, summary, contentType}，contentType=3 为 Markdown，
业务成功码 1000。正文上限 40000 字符。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import requests

log = logging.getLogger(__name__)

SPT_ENDPOINT = "https://wxpusher.zjiecode.com/api/send/message/simple-push"
SUCCESS_CODE = 1000
MAX_CONTENT_CHARS = 39000  # 官方 40000，留点余量


class NotifyError(Exception):
    """推送失败。"""


class Notifier(Protocol):
    def send(self, title: str, markdown: str, url: str | None = None) -> None: ...


class WxPusherSpt:
    #: 是否真的把消息投递出去了。去重记账只认 True 的通道，
    #: 否则"没配 SPT 只记日志"的那一轮会把空场标记成已推送，真正配好后反而漏推。
    delivers = True

    def __init__(
        self,
        spt: str,
        *,
        timeout: float = 10.0,
        session: Any = None,
    ) -> None:
        if not spt or not spt.strip():
            raise NotifyError("SPT 为空：请配置 WXPUSHER_SPT")
        self._spt = spt.strip()
        self._timeout = timeout
        self._session = session or requests.Session()

    def send(self, title: str, markdown: str, url: str | None = None) -> None:
        content = markdown
        if len(content) > MAX_CONTENT_CHARS:
            content = content[:MAX_CONTENT_CHARS] + "\n\n…（内容过长已截断）"
        payload: dict[str, Any] = {
            "spt": self._spt,
            "content": content,
            "summary": (title or "场地提醒")[:100],
            "contentType": 3,
        }
        if url:
            payload["url"] = url

        try:
            response = self._session.post(
                SPT_ENDPOINT,
                json=payload,
                timeout=self._timeout,
                headers={"Content-Type": "application/json"},
            )
        except requests.RequestException as exc:
            raise NotifyError(f"推送请求失败：{exc}") from exc

        status = getattr(response, "status_code", 200)
        if not 200 <= status < 300:
            raise NotifyError(f"推送返回 HTTP {status}")
        body = getattr(response, "text", "") or ""
        try:
            result = json.loads(body)
        except json.JSONDecodeError as exc:
            raise NotifyError(f"推送返回非 JSON：{body[:200]!r}") from exc

        if not isinstance(result, dict):
            raise NotifyError("推送响应必须是 JSON 对象")
        code = result.get("code")
        if code != SUCCESS_CODE:
            raise NotifyError(f"推送被拒绝：code={code} msg={result.get('msg')!r}")
        log.info("已推送：%s（WxPusher code=%s）", title, code)


class LoggingNotifier:
    """没配 SPT 时的降级通道：只写日志。

    单独存在是为了避免"忘了配 Secret"导致每天 132 次任务失败、132 封失败邮件。
    日志里仍然能看到本轮命中了什么，配好 SPT 后自动转为真实推送。
    """

    delivers = False

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []

    def send(self, title: str, markdown: str, url: str | None = None) -> None:
        self.sent.append((title, markdown, url))
        log.warning("[未推送·缺 WXPUSHER_SPT] %s\n%s", title, markdown)


class ConsoleNotifier:
    """本地调试 / --dry-run 用：只打印，不发请求。"""

    delivers = False

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []

    def send(self, title: str, markdown: str, url: str | None = None) -> None:
        self.sent.append((title, markdown, url))
        log.info("[dry-run] 本应推送：%s", title)
        print("-" * 60)
        print(f"[标题] {title}")
        print(markdown)
        if url:
            print(f"[链接] {url}")
        print("-" * 60)


def build_notifier(provider: str, spt: str | None = None, *, dry_run: bool = False) -> Notifier:
    if dry_run:
        return ConsoleNotifier()
    name = (provider or "wxpusher_spt").strip().lower()
    if name not in {"wxpusher_spt", "wxpusher", "spt"}:
        raise NotifyError(f"未知的推送通道：{provider!r}（当前仅支持 wxpusher_spt）")
    if not spt:
        return LoggingNotifier()
    return WxPusherSpt(spt)
