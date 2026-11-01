"""本地 Pillow 表格渲染：不依赖浏览器 / 网络 / t2i 服务。"""

from __future__ import annotations

import math
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .render import MODE_LABEL, table_headers, table_values

PAD_X = 16
PAD_Y = 10
MARGIN = 26
TITLE_SIZE = 21
BODY_SIZE = 16
HEAD_SIZE = 15

BG = (246, 247, 249)
CARD = (255, 255, 255)
HEAD_BG = (238, 241, 246)
ROW_ALT = (250, 251, 252)
TEXT = (31, 35, 40)
MUTED = (74, 85, 104)
LINE = (238, 241, 244)

MAX_TITLE_COL = 560  # 曲名列最大像素宽度
_FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = ["msyhbd.ttc", "msyh.ttc", "simhei.ttf"] if bold else [
        "msyh.ttc", "msyhbd.ttc", "simhei.ttf", "Deng.ttf", "simsun.ttc"
    ]
    for name in names:
        path = _FONT_DIR / name
        if not path.exists():
            continue
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default()


def _ellipsize(text: str, draw: ImageDraw.ImageDraw, font, max_width: float) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "…"
    while text and draw.textlength(text + ellipsis, font=font) > max_width:
        text = text[:-1]
    return text + ellipsis


def _draw_cell(draw, text, x, width, align, font, fill, y) -> None:
    tw = draw.textlength(text, font=font)
    if align == "right":
        tx = x + width - PAD_X - tw
    elif align == "center":
        tx = x + (width - tw) / 2
    else:
        tx = x + PAD_X
    draw.text((tx, y), text, font=font, fill=fill)


def render_table_image(
    rows: list[dict], requested: int, mode_filter: str,
    show_details: bool, show_sub: bool, out_path: Path,
) -> Path:
    show_acc = mode_filter != "strict"  # 只看严判时不显示 acc 列
    headers = table_headers(show_details, show_acc)
    values = [table_values(row, show_details, show_acc) for row in rows]

    body_font = _font(BODY_SIZE)
    head_font = _font(HEAD_SIZE, bold=True)
    title_font = _font(TITLE_SIZE, bold=True)

    scratch = Image.new("RGB", (1, 1))
    measure = ImageDraw.Draw(scratch)

    widths: list[int] = []
    for ci, (name, _align) in enumerate(headers):
        width = measure.textlength(name, font=head_font)
        for row_values in values:
            width = max(width, measure.textlength(row_values[ci], font=body_font))
        widths.append(math.ceil(width) + PAD_X * 2 + 2)
    widths[1] = min(widths[1], MAX_TITLE_COL)

    tags = [MODE_LABEL.get(mode_filter, mode_filter)]
    if show_details:
        tags.append("细节")
    if show_sub:
        tags.append("含次难度")
    title = f"最近 {len(rows)} 条（{' · '.join(tags)}）"

    table_w = sum(widths)
    canvas_w = max(table_w, int(measure.textlength(title, font=title_font))) + MARGIN * 2

    head_h = HEAD_SIZE + PAD_Y * 2 + 6
    row_h = BODY_SIZE + PAD_Y * 2 + 6
    table_h = head_h + row_h * len(values)
    note_h = 26 if requested > len(rows) else 0
    canvas_h = MARGIN + TITLE_SIZE + 16 + table_h + note_h + MARGIN

    img = Image.new("RGB", (canvas_w, canvas_h), BG)
    draw = ImageDraw.Draw(img)

    x0 = (canvas_w - table_w) // 2
    y0 = MARGIN + TITLE_SIZE + 16
    draw.text((x0, MARGIN), title, font=title_font, fill=TEXT)

    # 卡片底
    draw.rounded_rectangle((x0, y0, x0 + table_w, y0 + table_h), radius=10, fill=CARD)

    # 表头（上圆角、下直角）
    radius = 10
    draw.rounded_rectangle((x0, y0, x0 + table_w, y0 + head_h + radius), radius=radius, fill=HEAD_BG)
    draw.rectangle((x0, y0 + head_h, x0 + table_w, y0 + head_h + radius), fill=HEAD_BG)
    draw.rectangle((x0, y0 + head_h - 1, x0 + table_w, y0 + head_h), fill=HEAD_BG)

    # 表头文字
    cx = x0
    head_text_y = y0 + (head_h - HEAD_SIZE) / 2 - 2
    for (name, align), width in zip(headers, widths):
        _draw_cell(draw, name, cx, width, align, head_font, MUTED, head_text_y)
        cx += width

    # 数据行
    for ri, row_values in enumerate(values):
        top = y0 + head_h + ri * row_h
        if ri % 2 == 1:
            draw.rectangle((x0, top, x0 + table_w, top + row_h), fill=ROW_ALT)
        draw.line((x0, top, x0 + table_w, top), fill=LINE, width=1)

        cx = x0
        text_y = top + (row_h - BODY_SIZE) / 2 - 2
        for ci, ((name, align), width) in enumerate(zip(headers, widths)):
            text = _ellipsize(row_values[ci], measure, body_font, width - PAD_X * 2)
            _draw_cell(draw, text, cx, width, align, body_font, TEXT, text_y)
            cx += width

    draw.line(
        (x0, y0 + head_h + row_h * len(values), x0 + table_w, y0 + head_h + row_h * len(values)),
        fill=LINE, width=1,
    )

    if requested > len(rows):
        draw.text(
            (x0, y0 + table_h + 10), f"一共只有 {len(rows)} 条",
            font=_font(13), fill=(138, 148, 166),
        )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return out_path
