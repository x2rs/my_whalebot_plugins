"""Phigros 传分插件。

用法：引用一张成绩图，回复 /传分（群聊、私聊皆可）。图片也可直接和 /传分 发在同一条消息里。
识别后按发送者 QQ 落库。
"""

from __future__ import annotations

import base64
import io
import re
import time
from pathlib import Path

from PIL import Image as PILImage
from PIL import ImageOps

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.components import Image, Reply
from astrbot.core.star.filter.command import GreedyStr

from .core.db import ScoreDatabase
from .core.recognize import COMBO_PROMPT, PROMPT, derive_records, fc_by_notes, parse_output
from .core.render import MODE_LABEL, fmt_diff, render_recent, render_settings
from .core.table_image import render_table_image

PLUGIN_NAME = "astrbot_plugin_pgr_score"

HELP_TEXT = """Phigros 传分 / 查询
/pgr r10      最近 10 条（按你的默认设置）
/pgr r10 -n   只看宽判（-w 同义）；-s 严判，-a 全部
/pgr r10 -d   这次显示判定细节
/pgr r10 -e   这次带上 EZ/HD（次难度）
/pgr r10 -i   这次用图片表格
/pgr u        引用一张成绩图后传分（同 /pgru、/传分）
/pgr b10      单曲 rks 排行（暂未实现）
/pgr set      查看/修改默认显示设置
/pgr help     显示这份帮助"""

CATALOG_CANDIDATES = [
    Path("C:/Users/x2rs/Desktop/my_pgr_scores/songs.json"),  # 现有曲库
    Path(__file__).resolve().parent / "songs.json",           # 插件自带（可选）
]


@register(PLUGIN_NAME, "whalebot2", "Phigros 传分", "1.0.0")
class PgrScore(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = ScoreDatabase(self.data_dir / "scores.db", catalog_paths=CATALOG_CANDIDATES)

    async def terminate(self):
        self.db.close()

    @filter.command("传分")
    async def cmd_upload(self, event: AstrMessageEvent):
        async for result in self._upload_flow(event):
            yield result

    @filter.command("pgru")
    async def cmd_pgru(self, event: AstrMessageEvent):
        async for result in self._upload_flow(event):
            yield result

    @filter.command("pgr")
    async def cmd_pgr(self, event: AstrMessageEvent, args: GreedyStr):
        text = str(args or "").strip()
        low = text.casefold()

        if not text or low in ("help", "h", "?", "？"):
            yield event.plain_result(HELP_TEXT)
            return

        if low in ("u", "upload"):
            async for result in self._upload_flow(event):
                yield result
            return

        if low == "set" or low.startswith("set "):
            yield event.plain_result(self._handle_set(event, text))
            return

        parsed = _parse_query(text)
        if parsed is None:
            yield event.plain_result(HELP_TEXT)
            return

        kind, number, mode, detail, sub, image = parsed
        if kind == "b":
            yield event.plain_result("b（单曲 rks 排行）还没做，先用 r 吧。")
            return
        yield await self._recent_result(event, number, mode, detail, sub, image)

    def _handle_set(self, event: AstrMessageEvent, text: str) -> str:
        qq = str(event.get_sender_id() or "")
        tokens = text.split()[1:]
        if not tokens:
            return render_settings(self.db.get_settings(qq))

        key = tokens[0].casefold()
        value = tokens[1] if len(tokens) > 1 else ""
        try:
            if key in ("mode", "模式"):
                mode = _parse_mode(value)
                if mode is None:
                    return "用法：/pgr set mode 严|宽|全"
                self.db.set_settings(qq, mode_filter=mode)
            elif key in ("detail", "细节"):
                flag = _parse_bool(value)
                if flag is None:
                    return "用法：/pgr set detail 开|关"
                self.db.set_settings(qq, show_details=flag)
            elif key in ("sub", "次难度", "ez"):
                flag = _parse_bool(value)
                if flag is None:
                    return "用法：/pgr set sub 开|关"
                self.db.set_settings(qq, show_sub=flag)
            elif key in ("fmt", "格式"):
                fmt = _parse_fmt(value)
                if fmt is None:
                    return "用法：/pgr set fmt 文本|图片"
                self.db.set_settings(qq, fmt=fmt)
            else:
                return (
                    "用法：/pgr set mode 严|宽|全  或  /pgr set detail 开|关"
                    "  或  /pgr set sub 开|关  或  /pgr set fmt 文本|图片"
                )
        except ValueError as exc:
            return f"设置失败：{exc}"
        return render_settings(self.db.get_settings(qq))

    async def _upload_flow(self, event: AstrMessageEvent):
        if event.get_extra("pgr_upload_handled"):
            return
        event.set_extra("pgr_upload_handled", True)

        qq = str(event.get_sender_id() or "")

        try:
            image_data = await self._extract_image(event)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return

        try:
            provider = self.context.get_using_provider(event.unified_msg_origin)
            if provider is None:
                yield event.plain_result("没找到可用的视觉模型，传分失败。")
                return
            data_uri = self._to_data_uri(image_data)
            raw = await self._call_vision(provider, PROMPT, data_uri, max_tokens=1500)
            data = parse_output(raw)
        except Exception as exc:
            logger.warning("[pgr_score] 识别失败: %s", exc)
            yield event.plain_result("图没认出来，重拍一张或换清晰点的？")
            return

        category = data.get("category")
        if category in ("other", None):
            yield event.plain_result("这好像不是 Phigros 成绩图。")
            return

        records, warnings, total = derive_records(data)

        # HUD 的 COMBO 首位常被判定线压住，放大补读一次
        if category == "hud" and records:
            try:
                combo = await self._re_read_combo(provider, image_data)
                if combo is not None:
                    records[0]["combo"] = combo
            except Exception as exc:
                logger.debug("[pgr_score] combo 补读失败: %s", exc)

        if not records:
            text = "没读到任何一行成绩。"
            if warnings:
                text += "\n⚠ " + "；".join(warnings)
            yield event.plain_result(text)
            return

        label = "宽判" if category == "single" else ("课题" if category == "course" else "严判")
        lines: list[str] = []
        stored = 0
        for record in records:
            song = self.db.match_song(record["title"])
            if song is None:
                lines.append(f"✗ {record['title']}（曲库找不到，跳过）")
                continue
            diff_val = self.db.difficulty_of(song, record["chart"]) or record["level"]
            # 连击打满物量也算 FC（物量还没进曲库时 note_count 返回 None，这条自动跳过）
            fc = record["fc"] or fc_by_notes(
                self.db.note_count(song, record["chart"]),
                record.get("combo"),
                record.get("max_combo"),
            )
            try:
                self.db.add_score(
                    qq,
                    song["id"],
                    record["score"],
                    chart=record["chart"],
                    mode=record["mode"],
                    acc=record["acc"],
                    fc=fc,
                    combo=record.get("combo"),
                    max_combo=record.get("max_combo"),
                    perfect=record.get("perfect"),
                    good=record.get("good"),
                    bad=record.get("bad"),
                    miss=record.get("miss"),
                    early=record.get("early"),
                    late=record.get("late"),
                )
                stored += 1
                lines.append(self._fmt_line(record, diff_val, label, category, song["title"]))
            except ValueError as exc:
                lines.append(f"✗ {record['title']} {record['chart_label']}（{exc}）")

        if stored == 0:
            yield event.plain_result("一条都没记上：\n" + "\n".join(lines))
            return

        if category == "course":
            head = f"课题三首已记录（总分 {total}）"
        else:
            head = "已记录"
        body = "\n".join(lines)
        if warnings:
            body += "\n⚠ " + "；".join(warnings)
        yield event.plain_result(f"{head}\n{body}")

    # ---------- 辅助 ----------
    async def _recent_result(
        self, event: AstrMessageEvent, number: int,
        mode: str | None, detail: bool | None, sub: bool | None, image: bool | None,
    ):
        qq = str(event.get_sender_id() or "")
        settings = self.db.get_settings(qq)
        mode_filter = mode or settings["mode_filter"]
        show_details = settings["show_details"] if detail is None else detail
        show_sub = settings["show_sub"] if sub is None else sub
        fmt = "image" if image else settings["fmt"]
        limit = max(1, min(int(number), 50))
        rows = self.db.recent_scores(qq, limit, mode_filter=mode_filter, show_sub=show_sub)

        if fmt == "image" and rows:
            try:
                img_dir = self.data_dir / "images"
                img_dir.mkdir(parents=True, exist_ok=True)
                path = img_dir / f"pgr_{int(time.time() * 1000)}.png"
                render_table_image(rows, limit, mode_filter, show_details, show_sub, path)
                self._cleanup_images(img_dir)
                return event.image_result(str(path))
            except Exception as exc:
                logger.warning("[pgr_score] 表格渲染失败，回退文本: %s", exc)
        return event.plain_result(render_recent(rows, limit, mode_filter, show_details, show_sub))

    @staticmethod
    def _cleanup_images(img_dir: Path, keep: int = 50) -> None:
        try:
            files = sorted(img_dir.glob("pgr_*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in files[keep:]:
                old.unlink(missing_ok=True)
        except OSError:
            pass

    async def _call_vision(self, provider, prompt: str, image_url: str, max_tokens: int = 1500) -> str:
        result = await provider.text_chat(prompt=prompt, image_urls=[image_url], max_tokens=max_tokens)
        return str(getattr(result, "completion_text", result) or "").strip()

    async def _extract_image(self, event: AstrMessageEvent) -> bytes:
        chain = list(event.get_messages() or [])
        reply = next((seg for seg in chain if isinstance(seg, Reply)), None)

        image = self._first_image(chain)
        if image is None and reply is not None:
            image = self._first_image(getattr(reply, "chain", None) or [])

        if image is not None:
            data = await self._image_bytes(image)
            if data:
                return data

        # 引用链没带图片内容时，走 OneBot get_msg 兜底
        if reply is not None:
            data = await self._download_quoted_image(event, reply)
            if data:
                return data

        raise ValueError("引用一张成绩图再发 /pgr u（或 /传分、/pgru）。")

    @staticmethod
    def _first_image(chain) -> Image | None:
        for seg in chain or []:
            if isinstance(seg, Image):
                return seg
        return None

    @staticmethod
    async def _image_bytes(image: Image) -> bytes:
        data = None
        try:
            data = await image.get_data()
        except Exception:
            data = None
        if not data:
            path = await image.convert_to_file_path()
            data = Path(path).read_bytes()
        return data or b""

    async def _download_quoted_image(self, event: AstrMessageEvent, reply) -> bytes:
        msg_id = None
        for attr in ("id", "message_id", "msg_id"):
            value = getattr(reply, attr, None)
            if value:
                msg_id = value
                break
        if not msg_id:
            return b""
        caller = None
        for candidate in (getattr(event, "bot", None), getattr(getattr(event, "bot", None), "api", None)):
            if callable(getattr(candidate, "call_action", None)):
                caller = candidate.call_action
                break
        if caller is None:
            return b""
        try:
            result = caller("get_msg", message_id=int(str(msg_id)))
            if hasattr(result, "__await__"):
                import asyncio

                result = await asyncio.wait_for(result, timeout=20)
        except Exception:
            return b""
        payload = result
        if isinstance(result, dict) and isinstance(result.get("data"), dict):
            payload = result["data"]
        segments = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(segments, list):
            return b""
        for seg in segments:
            if not isinstance(seg, dict) or seg.get("type") != "image":
                continue
            seg_data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
            url = seg_data.get("url") or seg_data.get("file")
            if url:
                data = await self._download_url(url)
                if data:
                    return data
        return b""

    @staticmethod
    async def _download_url(url: str) -> bytes:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        return await resp.read()
        except Exception:
            return b""
        return b""

    async def _re_read_combo(self, provider, image_data: bytes) -> int | None:
        im = ImageOps.exif_transpose(PILImage.open(io.BytesIO(image_data)).convert("RGB"))
        w, h = im.size
        box = (int(w * 0.40), int(h * 0.08), int(w * 0.62), int(h * 0.24))
        crop = im.crop(box)
        crop = crop.resize((crop.width * 4, crop.height * 4), PILImage.LANCZOS)
        buf = io.BytesIO()
        crop.save(buf, "JPEG", quality=92)
        uri = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        text = await self._call_vision(provider, COMBO_PROMPT, uri, max_tokens=32)
        match = re.search(r"\d+", text)
        return int(match.group()) if match else None

    @staticmethod
    def _to_data_uri(data: bytes) -> str:
        im = ImageOps.exif_transpose(PILImage.open(io.BytesIO(data)).convert("RGB"))
        im.thumbnail((1600, 1600))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

    @staticmethod
    def _fmt_line(record: dict, diff_val, label: str, category: str, title: str) -> str:
        acc = ""
        if record["mode"] == "normal" and record["acc"] is not None:
            acc = f" | acc {record['acc']:g}%"
        combo = ""
        if category == "hud" and record.get("combo") is not None:
            combo = f" | 当前连击 {record['combo']}"
        elif category == "single" and record.get("max_combo") is not None:
            combo = f" | 最大连击 {record['max_combo']}"
        mark = ""
        if record["score"] == 1_000_000:
            mark = "AP"
        elif record["fc"]:
            mark = "FC"
        return f"{title} {record['chart_label']} {fmt_diff(diff_val)} | {label} | {record['score']}{acc}{combo} {mark}".strip()


def _parse_mode(value: str) -> str | None:
    value = (value or "").strip().casefold()
    if value in ("严", "严判", "strict", "s"):
        return "strict"
    if value in ("宽", "宽判", "normal", "n", "w"):
        return "normal"
    if value in ("全", "全部", "所有", "all", "a"):
        return "all"
    return None


def _parse_bool(value: str) -> bool | None:
    value = (value or "").strip().casefold()
    if value in ("开", "显示", "on", "1", "true", "yes", "y"):
        return True
    if value in ("关", "不显示", "off", "0", "false", "no", "n"):
        return False
    return None


def _parse_fmt(value: str) -> str | None:
    value = (value or "").strip().casefold()
    if value in ("图片", "图", "表格", "image", "img", "pic"):
        return "image"
    if value in ("文本", "文字", "纯文本", "text", "txt"):
        return "text"
    return None


def _parse_query(text: str):
    """解析 "r10 -nd" → ("r", 10, None, True, None, None)。

    没写的开关返回 None（表示"沿用用户设置"），只有显式写了 -d/-e/-i 才是 True。
    """
    tokens = text.split()
    if not tokens:
        return None
    head = re.fullmatch(r"([rb])(\d{1,3})?", tokens[0], re.I)
    if not head:
        return None
    kind = head.group(1).casefold()
    number = head.group(2)
    rest = tokens[1:]

    if number is None:
        if rest and rest[0].isdigit():
            number = rest.pop(0)
        else:
            return None

    mode = None
    detail = None
    sub = None
    image = None
    for token in rest:
        if not token.startswith("-") or len(token) < 2:
            return None
        for flag in token[1:].casefold():
            if flag == "s":
                mode = "strict"
            elif flag in ("n", "w"):
                mode = "normal"
            elif flag == "a":
                mode = "all"
            elif flag == "d":
                detail = True
            elif flag == "e":
                sub = True
            elif flag == "i":
                image = True
            else:
                return None
    return kind, int(number), mode, detail, sub, image
