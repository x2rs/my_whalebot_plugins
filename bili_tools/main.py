"""B站搜索工具：给 AstrBot 的 AI 提供视频搜索、UP 主搜索和 UP 主投稿查询。

三个 LLM 工具：
- bili_search_video  按关键词搜视频，返回标题/UP主/播放量/时长/链接
- bili_search_user   按名字搜 UP 主，返回 UID/粉丝数/投稿数/主页
- bili_user_videos   按 UID 拉 UP 主最新投稿，返回标题/播放量/时长/链接

依赖 bilibili-api-python（万能解析器 astrbot_plugin_parser 已带此依赖）。
所有请求做了 1.5 秒节流，避免 B 站搜索接口风控。
"""

import asyncio
import json
import re
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # bilibili-api 由 astrbot_plugin_parser 的依赖提供
    from bilibili_api import search as bili_search
    from bilibili_api import user as bili_user
    from bilibili_api.search import SearchObjectType

    _BILI_OK = True
    _BILI_ERR = ""
except Exception as _exc:  # pragma: no cover - 依赖缺失时给出可读提示
    _BILI_OK = False
    _BILI_ERR = str(_exc)

_TAG_RE = re.compile(r"<[^>]+>")
_LAST_CALL = 0.0
_MIN_INTERVAL = 1.5
_FALLBACK_TYPES = {"VIDEO": "video", "USER": "bili_user"}
_ENUM_NAMES = {"VIDEO": ("VIDEO",), "USER": ("BILI_USER", "USER")}


def _clean(value: Any) -> str:
    text = _TAG_RE.sub("", str(value or ""))
    text = (
        text.replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return " ".join(text.split())


def _fmt_num(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return _clean(value) or "0"
    if number >= 100_000_000:
        return f"{number / 100_000_000:.1f}亿"
    if number >= 10_000:
        return f"{number / 10_000:.1f}万"
    return str(number)


def _search_type(name: str) -> Any:
    """优先用枚举（不同版本成员名不同），再回退到字符串值。"""
    for candidate in _ENUM_NAMES.get(name, (name,)):
        try:
            return getattr(SearchObjectType, candidate)
        except Exception:
            continue
    return _FALLBACK_TYPES.get(name, name.lower())


def _unavailable() -> str:
    return json.dumps(
        {
            "status": "unavailable",
            "message": "bilibili-api 未安装，无法搜索。可在插件管理里为 bili_tools 安装依赖，或先安装 astrbot_plugin_parser 后重载。",
            "error": _BILI_ERR,
        },
        ensure_ascii=False,
    )


async def _throttle() -> None:
    global _LAST_CALL
    wait = _MIN_INTERVAL - (time.monotonic() - _LAST_CALL)
    if wait > 0:
        await asyncio.sleep(wait)
    _LAST_CALL = time.monotonic()


def _limit(value: Any, default: int = 5, maximum: int = 8) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(number, maximum))


def _extract_uid(raw: Any) -> str:
    match = re.search(r"(\d{3,})", str(raw or ""))
    return match.group(1) if match else ""


def _error(message: str, hint: str = "") -> str:
    payload = {"status": "error", "message": message}
    if hint:
        payload["hint"] = hint
    return json.dumps(payload, ensure_ascii=False)


@register("bili_tools", "whalebot2", "B站搜索工具", "1.0.0")
class BiliTools(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.llm_tool(name="bili_search_video")
    async def bili_search_video(
        self, event: AstrMessageEvent, keyword: str, limit: int = 5
    ) -> str:
        """搜索 B 站视频，返回标题、UP 主、播放量、时长和链接。

        Args:
            keyword(string): 搜索关键词，例如 UP 主名字或视频主题
            limit(number): 返回条数，默认 5，最大 8
        """
        if not _BILI_OK:
            return _unavailable()
        keyword = _clean(keyword)
        if not keyword:
            return _error("关键词不能为空")
        count = _limit(limit)
        await _throttle()
        try:
            data = await asyncio.wait_for(
                bili_search.search_by_type(
                    keyword, search_type=_search_type("VIDEO"), page=1
                ),
                timeout=15,
            )
        except asyncio.TimeoutError:
            return _error("B 站搜索超时（15 秒没返回），稍后再试。")
        except Exception as exc:
            logger.warning(f"[bili_tools] 视频搜索失败: {exc}")
            return _error(f"搜索失败：{exc}", "B 站搜索接口可能触发风控，稍等几秒再试。")

        results = []
        for item in (data.get("result") or [])[:count] if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            bvid = _clean(item.get("bvid"))
            results.append(
                {
                    "标题": _clean(item.get("title")),
                    "UP主": _clean(item.get("author")),
                    "播放": _fmt_num(item.get("play")),
                    "弹幕": _fmt_num(item.get("video_review")),
                    "时长": _clean(item.get("duration")),
                    "发布时间": time.strftime(
                        "%Y-%m-%d", time.localtime(int(item.get("pubdate") or 0))
                    ),
                    "链接": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
                }
            )
        return json.dumps(
            {"status": "ok", "keyword": keyword, "count": len(results), "results": results},
            ensure_ascii=False,
            indent=2,
        )

    @filter.llm_tool(name="bili_search_user")
    async def bili_search_user(
        self, event: AstrMessageEvent, name: str, limit: int = 5
    ) -> str:
        """按名字搜索 B 站 UP 主，返回 UID、粉丝数、投稿数和主页链接。

        Args:
            name(string): UP 主昵称或关键词
            limit(number): 返回条数，默认 5，最大 8
        """
        if not _BILI_OK:
            return _unavailable()
        keyword = _clean(name)
        if not keyword:
            return _error("名字不能为空")
        count = _limit(limit)
        await _throttle()
        try:
            data = await asyncio.wait_for(
                bili_search.search_by_type(
                    keyword, search_type=_search_type("USER"), page=1
                ),
                timeout=15,
            )
        except asyncio.TimeoutError:
            return _error("B 站搜索超时（15 秒没返回），稍后再试。")
        except Exception as exc:
            logger.warning(f"[bili_tools] UP 主搜索失败: {exc}")
            return _error(f"搜索失败：{exc}", "B 站搜索接口可能触发风控，稍等几秒再试。")

        results = []
        for item in (data.get("result") or [])[:count] if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            mid = _clean(item.get("mid"))
            results.append(
                {
                    "昵称": _clean(item.get("uname")),
                    "UID": mid,
                    "粉丝": _fmt_num(item.get("fans")),
                    "投稿数": _fmt_num(item.get("videos")),
                    "签名": _clean(item.get("usign")),
                    "主页": f"https://space.bilibili.com/{mid}" if mid else "",
                }
            )
        return json.dumps(
            {"status": "ok", "keyword": keyword, "count": len(results), "results": results},
            ensure_ascii=False,
            indent=2,
        )

    @filter.llm_tool(name="bili_user_videos")
    async def bili_user_videos(
        self, event: AstrMessageEvent, uid: str, limit: int = 5
    ) -> str:
        """查询指定 UP 主的最新投稿，返回标题、播放量、时长和链接。

        Args:
            uid(string): UP 主的 UID 或主页链接，例如 123456 或 https://space.bilibili.com/123456
            limit(number): 返回条数，默认 5，最大 8
        """
        if not _BILI_OK:
            return _unavailable()
        target = _extract_uid(uid)
        if not target:
            return _error("UID 无效，请提供数字 UID 或空间主页链接")
        count = _limit(limit)
        await _throttle()
        try:
            user = bili_user.User(int(target))
            data = await asyncio.wait_for(user.get_videos(), timeout=15)
        except asyncio.TimeoutError:
            return _error("获取 UP 主投稿超时（15 秒没返回），稍后再试。")
        except Exception as exc:
            logger.warning(f"[bili_tools] 获取 UP 主投稿失败: {exc}")
            return _error(
                f"获取投稿失败：{exc}",
                "空间投稿接口对未登录请求较严格，必要时给解析器配 B 站 Cookie 后再试。",
            )

        payload = data.get("list") if isinstance(data, dict) else data
        if isinstance(payload, dict):
            vlist = payload.get("vlist") or []
        elif isinstance(payload, list):
            vlist = payload
        else:
            vlist = []

        results = []
        for item in vlist[:count]:
            if not isinstance(item, dict):
                continue
            bvid = _clean(item.get("bvid"))
            results.append(
                {
                    "标题": _clean(item.get("title")),
                    "播放": _fmt_num(item.get("play")),
                    "时长": _clean(item.get("length")),
                    "发布时间": time.strftime(
                        "%Y-%m-%d", time.localtime(int(item.get("created") or 0))
                    ),
                    "链接": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
                }
            )
        return json.dumps(
            {"status": "ok", "uid": target, "count": len(results), "results": results},
            ensure_ascii=False,
            indent=2,
        )

    async def terminate(self):
        """插件卸载时的清理钩子。"""
        return None
