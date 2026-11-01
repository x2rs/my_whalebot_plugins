"""消息归档插件：把收到的消息落库，之后按时间/关键词秒查。

- 写入：每条消息（text/image/voice/...）在 message 阶段写进 SQLite。
- 图片描述：在 on_llm_request 阶段捡 private_companion 已经生成的
  `private_companion_image_caption_route_text` 回填，零额外视觉调用。
- 语音转写：若 AstrBot 已把语音转成文字，会体现在 text 字段里。
- 查询：search_archive 走 SQL；archive_backfill 用 OneBot 历史接口把
  已有记录一次性回填进库（分页逻辑与 read_chat_history 一致）。

工具：
- search_archive(hours/start_time/end_time/keyword/limit/scope)
- archive_backfill(hours/start_time/end_time/scope)
"""

import hashlib
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:  # AstrBot 消息组件
    from astrbot.core.message.components import (
        Face,
        File,
        Image,
        Plain,
        Record,
        Video,
    )
except ImportError:  # pragma: no cover
    Face = File = Image = Plain = Record = Video = None

TZ = ZoneInfo("Asia/Shanghai")
_PAGE_SIZE = 100
_MAX_PAGES = 15
_MAX_OUTPUT_CHARS = 9000

_RAW_SEGMENT_LABELS = {
    "text": None,
    "at": "[@]",
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
_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    platform TEXT,
    session_id TEXT,
    scope TEXT,
    sender_id TEXT,
    sender_name TEXT,
    kind TEXT,
    text TEXT,
    caption TEXT,
    message_id TEXT,
    dedupe_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_messages_session_ts ON messages(session_id, ts);
"""


def _clean(value: Any, limit: int = 300) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()[:limit]


def _parse_when(value: Any, *, default: float = 0.0) -> float:
    text = _clean(value, 40)
    if not text:
        return default
    for old, new in (("号", "-"), ("月", "-"), ("日", " "), ("/", "-"), ("T", " ")):
        text = text.replace(old, new)
    text = " ".join(text.split())
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m-%d %H:%M", "%Y-%m-%d", "%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            parsed = parsed.replace(year=datetime.now(TZ).year)
        return parsed.replace(tzinfo=TZ).timestamp()
    return default


@register("msg_archive", "whalebot2", "消息归档", "1.0.0")
class MsgArchive(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        data_dir = Path(StarTools.get_data_dir("msg_archive"))
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = data_dir / "archive.db"
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10)
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---------- DB 基础 ----------
    def _execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchall()

    def _commit(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    @staticmethod
    def _scope_of(umo: str) -> str:
        if "GroupMessage" in umo:
            return "group"
        if "FriendMessage" in umo or "PrivateMessage" in umo:
            return "private"
        return "other"

    # ---------- 事件写入 ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _on_message(self, event: AstrMessageEvent):
        try:
            self._store_event(event)
        except Exception as exc:  # pragma: no cover - 归档失败绝不打断对话
            logger.warning(f"[msg_archive] 写入失败: {exc}")

    def _store_event(self, event: AstrMessageEvent) -> None:
        umo = _clean(getattr(event, "unified_msg_origin", None), 200)
        if not umo:
            return
        chain = list(event.get_messages() or [])
        plain_parts: list[str] = []
        has_image = has_record = has_video = has_file = has_face = False
        for seg in chain:
            if Plain is not None and isinstance(seg, Plain):
                plain_parts.append(getattr(seg, "text", "") or "")
            elif Image is not None and isinstance(seg, Image):
                has_image = True
            elif Record is not None and isinstance(seg, Record):
                has_record = True
            elif Video is not None and isinstance(seg, Video):
                has_video = True
            elif File is not None and isinstance(seg, File):
                has_file = True
            elif Face is not None and isinstance(seg, Face):
                has_face = True

        text = "".join(plain_parts).strip()
        if not text:
            text = _clean(getattr(event, "message_str", None), 4000)

        if has_record:
            kind, text = "voice", text
        elif has_image:
            kind, text = "image", text
        elif has_video:
            kind, text = "video", text
        elif has_file:
            kind, text = "file", text
        elif has_face:
            kind, text = "face", text
        else:
            kind = "text"

        sender_id = ""
        getter = getattr(event, "get_sender_id", None)
        if callable(getter):
            try:
                sender_id = _clean(getter(), 60)
            except Exception:
                sender_id = ""
        sender_name = ""
        getter = getattr(event, "get_sender_name", None)
        if callable(getter):
            try:
                sender_name = _clean(getter(), 60)
            except Exception:
                sender_name = ""

        message_id = _clean(getattr(event, "message_id", None), 80)
        ts = time.time()
        dedupe = message_id or hashlib.sha1(
            f"{umo}|{sender_id}|{ts:.3f}|{kind}|{text}".encode("utf-8")
        ).hexdigest()[:32]

        self._commit(
            "INSERT OR IGNORE INTO messages(ts, platform, session_id, scope, sender_id, sender_name, kind, text, caption, message_id, dedupe_key) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ts, "whalebot2", umo, self._scope_of(umo), sender_id, sender_name, kind, text, "", message_id, dedupe),
        )

    # ---------- 图片描述回填 ----------
    @filter.on_llm_request(priority=-50000)
    async def _on_llm_request(self, event: AstrMessageEvent, req: Any):
        try:
            caption = _clean(getattr(event, "private_companion_image_caption_route_text", None), 2000)
            if not caption:
                return
            umo = _clean(getattr(event, "unified_msg_origin", None), 200)
            if not umo:
                return
            self._commit(
                "UPDATE messages SET caption=? WHERE id=(SELECT id FROM messages WHERE session_id=? AND kind='image' AND caption='' ORDER BY ts DESC LIMIT 1)",
                (caption, umo),
            )
        except Exception as exc:  # pragma: no cover
            logger.debug(f"[msg_archive] caption 回填失败: {exc}")

    # ---------- 查询工具 ----------
    @filter.llm_tool(name="search_archive")
    async def search_archive(
        self,
        event: AstrMessageEvent,
        hours: float = 24,
        start_time: str = "",
        end_time: str = "",
        keyword: str = "",
        limit: int = 100,
        scope: str = "current",
    ) -> str:
        """查询消息归档库。归档里存了收到的消息、图片描述和语音转写，速度比现场翻历史快得多。

        Args:
            hours(number): 回溯小时数，默认 24，最大 720；提供 start_time 时忽略
            start_time(string): 起始时间 "MM-DD HH:MM" 或 "YYYY-MM-DD HH:MM"
            end_time(string): 结束时间，同上，留空表示现在
            keyword(string): 关键词，匹配正文/图片描述/语音转写/发送者昵称
            limit(number): 最多返回条数，默认 100，最大 300
            scope(string): current=当前会话（默认）/ private / group / all
        """
        umo = _clean(getattr(event, "unified_msg_origin", None), 200)
        scope = _clean(scope, 20).lower()
        if scope in ("current", ""):
            session_filter, params_base = "session_id = ?", [umo]
        elif scope == "private":
            session_filter, params_base = "scope = 'private'", []
        elif scope == "group":
            session_filter, params_base = "scope = 'group'", []
        else:
            session_filter, params_base = "1=1", []

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

        key = _clean(keyword, 60)
        sql = f"SELECT ts, sender_name, kind, text, caption FROM messages WHERE {session_filter} AND ts >= ? AND ts <= ?"
        params = params_base + [start_ts, end_ts]
        if key:
            sql += " AND (text LIKE ? OR caption LIKE ? OR sender_name LIKE ?)"
            like = f"%{key}%"
            params += [like, like, like]
        sql += " ORDER BY ts ASC"

        rows = self._execute(sql, tuple(params))
        if not rows:
            window = (
                f"{datetime.fromtimestamp(start_ts, TZ).strftime('%m-%d %H:%M')} ~ "
                f"{datetime.fromtimestamp(end_ts, TZ).strftime('%m-%d %H:%M')}"
            )
            return f"归档里 {window} 这段时间没有匹配的消息。可先用 archive_backfill 把历史回填进库。"

        lines: list[str] = []
        for ts, sender_name, kind, text, caption in rows:
            stamp = datetime.fromtimestamp(ts, TZ).strftime("%m-%d %H:%M")
            who = sender_name or "未知"
            content = text or ""
            if kind == "image":
                content = caption or "[图片]"
            elif kind == "voice":
                content = caption or text or "[语音]"
            elif kind == "video":
                content = caption or "[视频]"
            elif kind == "file":
                content = caption or "[文件]"
            elif kind == "face":
                content = text or "[表情]"
            lines.append(f"[{stamp}] {who}: {content}")

        lines = lines[-want:]
        header = f"归档命中 {len(lines)} 条（{scope}）"
        body = "\n".join(lines)
        if len(body) > _MAX_OUTPUT_CHARS:
            body = body[-_MAX_OUTPUT_CHARS:]
            header += "（内容过长，已截断到最近部分）"
        return f"{header}\n{body}"

    # ---------- 历史回填工具 ----------
    @filter.llm_tool(name="archive_backfill")
    async def archive_backfill(
        self,
        event: AstrMessageEvent,
        hours: float = 24,
        start_time: str = "",
        scope: str = "current",
    ) -> str:
        """把 QQ 端保留的历史消息回填进归档库（翻页拉取，之后查询就秒回了）。

        Args:
            hours(number): 回填多少小时，默认 24，最大 720
            start_time(string): 起始时间 "MM-DD HH:MM"，给了就忽略 hours
            scope(string): current=当前会话 / private / group
        """
        caller = self._call_action(event)
        if caller is None:
            return "当前平台不支持读取历史（需要 aiocqhttp/OneBot）。"

        umo = _clean(getattr(event, "unified_msg_origin", None), 200)
        scope = _clean(scope, 20).lower()
        if scope in ("private", "group"):
            kind, target = scope, ""
            if scope == "private":
                getter = getattr(event, "get_sender_id", None)
                target = _clean(getter(), 30) if callable(getter) else ""
        else:
            kind, target = self._scope_from_umo(umo)
        if not kind or not target:
            return "无法确定回填的会话。"

        try:
            hour_span = float(hours)
        except (TypeError, ValueError):
            hour_span = 24.0
        hour_span = max(0.5, min(hour_span, 720.0))
        start_ts = _parse_when(start_time, default=0.0)
        cutoff = start_ts or (time.time() - hour_span * 3600)

        anchor = ""
        seen: set[str] = set()
        inserted = 0
        for _ in range(_MAX_PAGES):
            try:
                page = await self._fetch_page(caller, kind, target, anchor)
            except Exception as exc:
                logger.warning(f"[msg_archive] 回填翻页失败: {exc}")
                break
            if not page:
                break
            fresh = [raw for raw in page if _raw_key(raw) not in seen]
            if not fresh:
                break
            for raw in fresh:
                seen.add(_raw_key(raw))
                if self._insert_raw(raw, umo, kind):
                    inserted += 1
            oldest_raw = min(
                (raw for raw in page if _raw_ts(raw)), key=_raw_ts, default=None
            )
            if oldest_raw is None:
                break
            if _raw_ts(oldest_raw) < cutoff:
                break
            next_anchor = _raw_key(oldest_raw)
            if not next_anchor or next_anchor == anchor:
                break
            anchor = next_anchor
        return f"回填完成：新入库 {inserted} 条。"

    def _scope_from_umo(self, umo: str) -> tuple[str, str]:
        parts = umo.split(":")
        kind = parts[1] if len(parts) >= 3 else ""
        target = re.sub(r"\D", "", parts[2]) if len(parts) >= 3 else ""
        if kind == "GroupMessage":
            return "group", target
        if kind in ("FriendMessage", "PrivateMessage"):
            return "private", target
        return "", ""

    def _call_action(self, event: AstrMessageEvent):
        for candidate in (getattr(event, "bot", None), getattr(getattr(event, "bot", None), "api", None)):
            action = getattr(candidate, "call_action", None)
            if callable(action):
                return action
        return None

    async def _fetch_page(self, caller, kind: str, target: str, anchor: str) -> list[dict]:
        action = "get_group_msg_history" if kind == "group" else "get_friend_msg_history"
        key = "group_id" if kind == "group" else "user_id"
        params: dict[str, Any] = {key: int(target), "count": _PAGE_SIZE}
        if anchor:
            params["message_id"] = int(anchor)
            params["reverse_order"] = True
        result = caller(action, **params)
        if hasattr(result, "__await__"):
            import asyncio

            result = await asyncio.wait_for(result, timeout=20)
        return _raw_messages_of(result)

    def _insert_raw(self, raw: dict, umo: str, scope: str) -> bool:
        ts = _raw_ts(raw)
        if not ts:
            return False
        text = _raw_text(raw)
        kind = _raw_kind(raw)
        sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
        sender_id = _clean(sender.get("user_id"), 60)
        sender_name = _clean(sender.get("nickname") or sender.get("card"), 60)
        key = _raw_key(raw)
        if not key:
            key = hashlib.sha1(f"{umo}|{ts:.3f}|{text}".encode()).hexdigest()[:32]
        self._commit(
            "INSERT OR IGNORE INTO messages(ts, platform, session_id, scope, sender_id, sender_name, kind, text, caption, message_id, dedupe_key) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ts, "whalebot2", umo, self._scope_of(umo) if scope == "current" else scope, sender_id, sender_name, kind, text, "", key, key),
        )
        return True

    async def terminate(self):
        try:
            self._conn.close()
        except Exception:
            pass


# ---------- 原始历史消息解析 ----------
def _raw_messages_of(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]
    for key in ("messages", "message_list", "list"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _raw_key(raw: dict) -> str:
    return _clean(raw.get("message_id") or raw.get("real_id") or raw.get("msg_id"), 80)


def _raw_ts(raw: dict) -> float:
    value = raw.get("time") or raw.get("timestamp") or raw.get("send_time")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number > 10_000_000_000:
        number /= 1000.0
    return number if number > 0 else 0.0


def _raw_kind(raw: dict) -> str:
    message = raw.get("message")
    if isinstance(message, list):
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") in ("record",):
                return "voice"
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") in ("image",):
                return "image"
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") in ("video",):
                return "video"
    if isinstance(message, str) and "[CQ:record" in message:
        return "voice"
    if isinstance(message, str) and "[CQ:image" in message:
        return "image"
    return "text"


def _raw_text(raw: dict) -> str:
    message = raw.get("message")
    if isinstance(message, list):
        parts = []
        for seg in message:
            if not isinstance(seg, dict):
                continue
            kind = str(seg.get("type") or "").lower()
            data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
            if kind == "text":
                parts.append(str(data.get("text") or ""))
            elif kind == "at":
                target = str(data.get("qq") or "")
                parts.append("@全体成员" if target == "all" else (f"@{target}" if target else "[@]"))
            elif kind in _RAW_SEGMENT_LABELS and _RAW_SEGMENT_LABELS[kind]:
                parts.append(_RAW_SEGMENT_LABELS[kind])
        text = "".join(parts)
    elif isinstance(message, str):
        text = message
    else:
        text = str(raw.get("raw_message") or "")

    def _cq(match: re.Match) -> str:
        return _RAW_SEGMENT_LABELS.get(match.group(1).lower(), f"[{match.group(1)}]")

    text = _CQ_RE.sub(_cq, text).strip()
    return text[:4000]
