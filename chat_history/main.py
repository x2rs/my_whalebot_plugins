"""聊天记录查询工具：让 AI 主动翻阅当前会话的历史消息。

调用 OneBot 的 get_group_msg_history / get_friend_msg_history（SnowLuma 已实现），
不受 AstrBot 上下文压缩影响，可以逐页回溯到 QQ 里保留的最早记录。

翻页约定（SnowLuma）：省略锚点时返回最新一页；翻旧页时把上一页最早的
message_id 作为 message_id 传入，并设 reverse_order=true。

工具参数：
- hours   回溯多少小时（默认 24，最多 720）
- keyword 关键词过滤（匹配正文或发送者昵称）
- limit   最多返回多少条（默认 100，最多 300）
"""

import asyncio
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

TZ = ZoneInfo("Asia/Shanghai")

_SEGMENT_LABELS = {
    "image": "[图片]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "face": "[表情]",
    "mface": "[动画表情]",
    "reply": "[回复]",
    "forward": "[转发]",
    "json": "[卡片]",
    "xml": "[卡片]",
    "location": "[位置]",
    "music": "[音乐]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
    "poke": "[戳一戳]",
}
_CQ_RE = re.compile(r"\[CQ:([a-zA-Z0-9_]+)(?:,[^\]]*)?\]")
_MAX_OUTPUT_CHARS = 9000
_PAGE_SIZE = 100
_MAX_PAGES = 15


def _clean(value: Any, limit: int = 300) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:limit]


def _segments_to_text(message: Any, raw_message: Any = "") -> str:
    parts: list[str] = []
    if isinstance(message, list):
        for seg in message:
            if not isinstance(seg, dict):
                continue
            kind = str(seg.get("type") or "").lower()
            data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
            if kind == "text":
                parts.append(str(data.get("text") or ""))
            elif kind == "at":
                target = str(data.get("qq") or data.get("user_id") or "")
                parts.append("@全体成员" if target == "all" else (f"@{target}" if target else "[@]"))
            else:
                parts.append(_SEGMENT_LABELS.get(kind, f"[{kind or '消息'}]"))
    elif isinstance(message, str):
        parts.append(message)
    text = "".join(parts)
    if not text.strip():
        text = str(raw_message or "")

    def _cq(match: re.Match[str]) -> str:
        return _SEGMENT_LABELS.get(match.group(1).lower(), f"[{match.group(1)}]")

    text = _CQ_RE.sub(_cq, text).strip()
    return _clean(text, 1200)


def _sender_name(raw: dict[str, Any]) -> str:
    sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
    for source in (sender, raw):
        for key in ("card", "nickname", "name", "user_id"):
            value = _clean(source.get(key), 40)
            if value:
                return value
    return "未知"


def _timestamp(raw: dict[str, Any]) -> float:
    value = raw.get("time") or raw.get("timestamp") or raw.get("send_time")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number > 10_000_000_000:
        number /= 1000.0
    return number if number > 0 else 0.0


def _message_key(raw: dict[str, Any]) -> str:
    for key in ("message_id", "real_id", "msg_id"):
        value = _clean(raw.get(key), 60)
        if value:
            return value
    return ""


def _parse_when(value: Any, *, default: float = 0.0) -> float:
    """把 "10-28 06:00" / "2026-10-28 06:00" 这类时间解析成时间戳。"""
    text = _clean(value, 40)
    if not text:
        return default
    for old, new in (("号", "-"), ("月", "-"), ("日", " "), ("/", "-"), ("T", " ")):
        text = text.replace(old, new)
    text = " ".join(text.split())
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%m-%d %H:%M",
        "%Y-%m-%d",
        "%m-%d",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            parsed = parsed.replace(year=datetime.now(TZ).year)
        return parsed.replace(tzinfo=TZ).timestamp()
    return default


def _messages_of(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("messages", "message_list", "list"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    nested = payload.get("data")
    if nested is not payload:
        return _messages_of(nested)
    return []


def _result_data(result: Any) -> Any:
    """剥掉 OneBot 的 {status, retcode, data} 外壳，失败时抛错。"""
    if not isinstance(result, dict):
        return result
    retcode = result.get("retcode", result.get("code"))
    status = str(result.get("status") or "").lower()
    failed = status in {"failed", "fail", "error", "nok"}
    if retcode not in (None, ""):
        try:
            failed = failed or int(retcode) != 0
        except (TypeError, ValueError):
            pass
    if failed:
        message = _clean(result.get("message") or result.get("msg") or result.get("wording"), 200)
        raise RuntimeError(message or "OneBot 接口返回失败")
    return result.get("data", result)


async def _maybe_await(value: Any, timeout: float = 20.0) -> Any:
    if hasattr(value, "__await__"):
        return await asyncio.wait_for(value, timeout=timeout)
    return value


@register("chat_history", "whalebot2", "聊天记录查询", "1.0.0")
class ChatHistory(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    def _call_action(self, event: AstrMessageEvent):
        for candidate in (
            getattr(event, "bot", None),
            getattr(getattr(event, "bot", None), "api", None),
        ):
            action = getattr(candidate, "call_action", None)
            if callable(action):
                return action
        context = getattr(self, "context", None)
        manager = getattr(context, "platform_manager", None)
        for platform in list(getattr(manager, "platform_insts", []) or []):
            bot = getattr(platform, "bot", None)
            action = getattr(bot, "call_action", None)
            if callable(action):
                return action
        return None

    def _scope(self, event: AstrMessageEvent) -> tuple[str, str]:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        parts = umo.split(":")
        kind = parts[1] if len(parts) >= 3 else ""
        target = re.sub(r"\D", "", parts[2]) if len(parts) >= 3 else ""
        if kind == "GroupMessage":
            return "group", target
        if kind == "FriendMessage":
            sender = ""
            getter = getattr(event, "get_sender_id", None)
            if callable(getter):
                try:
                    sender = re.sub(r"\D", "", str(getter() or ""))
                except Exception:
                    sender = ""
            return "private", target or sender
        return "", ""

    async def _fetch_page(
        self, caller, kind: str, target: str, anchor: str, count: int
    ) -> list[dict[str, Any]]:
        action = "get_group_msg_history" if kind == "group" else "get_friend_msg_history"
        key = "group_id" if kind == "group" else "user_id"
        params: dict[str, Any] = {key: int(target), "count": count}
        if anchor:
            # SnowLuma 用 message_id 作锚点，并返回该锚点及更旧的消息
            params["message_id"] = int(anchor)
            params["reverse_order"] = True
        result = await _maybe_await(caller(action, **params))
        return _messages_of(_result_data(result))

    @filter.llm_tool(name="read_chat_history")
    async def read_chat_history(
        self,
        event: AstrMessageEvent,
        hours: float = 24,
        start_time: str = "",
        end_time: str = "",
        keyword: str = "",
        limit: int = 100,
    ) -> str:
        """翻阅当前群聊或私聊的历史消息，用来回答“今天聊了什么”“28号早上呢”“找一下提到 X 的消息”。

        问某个具体时间段时，用 start_time / end_time 传绝对时间，例如“28号早上”传 start_time="10-28 06:00"、end_time="10-28 12:00"；
        只说“今天/最近 N 小时”时留空，用 hours。

        Args:
            hours(number): 回溯小时数，默认 24，最大 720；提供了 start_time 时忽略
            start_time(string): 起始时间，格式“MM-DD HH:MM”或“YYYY-MM-DD HH:MM”
            end_time(string): 结束时间，格式同上；留空表示到现在
            keyword(string): 关键词过滤，匹配消息正文或发送者昵称；留空表示不筛选
            limit(number): 最多返回多少条，默认 100，最大 300
        """
        caller = self._call_action(event)
        if caller is None:
            return "当前平台不支持读取历史消息（需要 aiocqhttp/OneBot 连接）。"

        kind, target = self._scope(event)
        if not kind or not target:
            return "无法识别当前会话，读取历史失败。"

        try:
            hour_span = float(hours)
        except (TypeError, ValueError):
            hour_span = 24.0
        hour_span = max(0.5, min(hour_span, 720.0))

        try:
            want = max(10, min(int(limit), 300))
        except (TypeError, ValueError):
            want = 100

        now = time.time()
        end_ts = _parse_when(end_time, default=now) or now
        start_ts = _parse_when(start_time, default=0.0)
        if not start_ts:
            start_ts = end_ts - hour_span * 3600
        if start_ts > end_ts:
            start_ts, end_ts = end_ts, start_ts
        cutoff = start_ts
        key = _clean(keyword, 60).strip()
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        anchor = ""

        for page_index in range(_MAX_PAGES):
            try:
                page = await self._fetch_page(caller, kind, target, anchor, _PAGE_SIZE)
            except Exception as exc:
                logger.warning(f"[chat_history] 拉取历史失败(page {page_index + 1}): {exc}")
                if collected:
                    break
                return f"读取聊天记录失败：{exc}"

            if not page:
                break

            fresh: list[dict[str, Any]] = []
            for raw in page:
                token = _message_key(raw)
                if token and token in seen:
                    continue
                if token:
                    seen.add(token)
                fresh.append(raw)
            if not fresh:
                break
            collected.extend(fresh)

            oldest = min(
                (raw for raw in page if _timestamp(raw)),
                key=_timestamp,
                default=None,
            )
            if oldest is None:
                break
            if _timestamp(oldest) < cutoff:
                break
            next_anchor = _message_key(oldest)
            if not next_anchor or next_anchor == anchor:
                break
            anchor = next_anchor

        lines: list[str] = []
        for raw in sorted(collected, key=_timestamp):
            ts = _timestamp(raw)
            if ts and (ts < start_ts or ts > end_ts):
                continue
            text = _segments_to_text(raw.get("message"), raw.get("raw_message"))
            if not text:
                continue
            who = _sender_name(raw)
            if key and key not in text and key not in who:
                continue
            stamp = datetime.fromtimestamp(ts, TZ).strftime("%m-%d %H:%M") if ts else "--:--"
            lines.append(f"[{stamp}] {who}: {text}")

        if not lines:
            scope = "群聊" if kind == "group" else "私聊"
            extra = f"，关键词“{key}”" if key else ""
            window = (
                f"{datetime.fromtimestamp(start_ts, TZ).strftime('%m-%d %H:%M')} ~ "
                f"{datetime.fromtimestamp(end_ts, TZ).strftime('%m-%d %H:%M')}"
            )
            return f"{scope}里 {window} 这段时间没有找到可读消息{extra}。"

        lines = lines[-want:]
        first_stamp = lines[0][1:15] if lines else "--"
        last_stamp = lines[-1][1:15] if lines else "--"
        header = (
            f"共取到 {len(lines)} 条消息，覆盖 {first_stamp} ~ {last_stamp}"
            f"（按时间正序，第一行最早）。这是我实际拉到的全部范围，"
            f"如果这比要求的 {hour_span:g} 小时短，说明 QQ 端只保留了这么多。"
        )
        body = "\n".join(lines)
        if len(body) > _MAX_OUTPUT_CHARS:
            body = body[-_MAX_OUTPUT_CHARS:]
            header += "（内容过长，只保留了最近的部分）"
        return f"{header}\n{body}"

    async def terminate(self):
        return None
