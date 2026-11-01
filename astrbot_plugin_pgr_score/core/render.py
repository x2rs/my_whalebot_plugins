"""查分结果的渲染：纯文本 + 表格单元格（纯函数，方便单测）。"""

from __future__ import annotations

from datetime import datetime

MODE_LABEL = {"strict": "严判", "normal": "宽判", "all": "全部"}

SETTINGS_HINT = (
    "/pgr set mode 严|宽|全\n"
    "/pgr set detail 开|关\n"
    "/pgr set sub 开|关\n"
    "/pgr set fmt 文本|图片"
)


def fmt_time(value) -> str:
    """recorded_at 的 ISO 时间 → MM-DD HH:MM。"""
    try:
        return datetime.fromisoformat(str(value)).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(value or "")


def fmt_diff(value) -> str:
    """定数统一显示一位小数（16 → 16.0）。"""
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return str(value)


def acc_of(row: dict) -> float | None:
    """宽判用图上读到的 acc；严判/课题按 分数/1000000 折算。"""
    if row.get("mode") == "normal":
        acc = row.get("acc")
        return float(acc) if acc is not None else None
    score = row.get("score")
    if score is None:
        return None
    return round(score / 1_000_000 * 100, 2)


def fmt_acc(acc: float | None) -> str:
    """acc 统一两位小数四舍五入。"""
    return "—" if acc is None else f"{acc:.2f}%"


def detail_parts(row: dict) -> list[str]:
    """第二行：判定数 / Early·Late / 连击 / AP·FC。"""
    parts: list[str] = []
    p, g, b, m = row.get("perfect"), row.get("good"), row.get("bad"), row.get("miss")
    if None not in (p, g, b, m):
        parts.append(f"P{p} G{g} B{b} M{m}")
    early, late = row.get("early"), row.get("late")
    if early is not None and late is not None:
        parts.append(f"E{early} L{late}")
    if row.get("combo") is not None:
        parts.append(f"连击 {row['combo']}")
    elif row.get("max_combo") is not None:
        parts.append(f"最大连击 {row['max_combo']}")
    if row.get("score") == 1_000_000:
        parts.append("AP")
    elif row.get("fc"):
        parts.append("FC")
    return parts or ["判定 —"]


def format_row(row: dict, show_details: bool, show_acc: bool = True) -> list[str]:
    """返回 1 行（简版）或 2 行（细节版）。"""
    head = (
        f"{fmt_time(row.get('recorded_at'))}  {row['title']} "
        f"{str(row['chart']).upper()} {fmt_diff(row.get('difficulty'))}"
        f" | {row['score']}"
    )
    if show_acc:
        head += f" | acc {fmt_acc(acc_of(row))}"
    if not show_details:
        return [head]
    return [head, "  " + " | ".join(detail_parts(row))]


def render_recent(
    rows: list[dict], requested: int, mode_filter: str,
    show_details: bool, show_sub: bool = False,
) -> str:
    label = MODE_LABEL.get(mode_filter, mode_filter)
    tags = [label]
    if show_details:
        tags.append("细节")
    if show_sub:
        tags.append("含次难度")
    title = " · ".join(tags)
    if not rows:
        return f"还没有记录（{title}）。引用一张成绩图发 /pgr u 传一张？"
    show_acc = mode_filter != "strict"  # 只看严判时不显示 acc 列
    head = f"最近 {len(rows)} 条（{title}）"
    lines = [head]
    for row in rows:
        lines.extend(format_row(row, show_details, show_acc))
    if requested > len(rows):
        lines.append(f"（一共只有 {len(rows)} 条）")
    return "\n".join(lines)


def table_headers(show_details: bool, show_acc: bool = True) -> list[tuple[str, str]]:
    """表头：[(列名, 对齐)]。"""
    if show_details:
        headers = [("时间", "left"), ("曲名", "left"), ("难度", "left"), ("分数", "right")]
        if show_acc:
            headers.append(("acc", "right"))
        headers += [("判定", "left"), ("E/L", "left"), ("连击", "right"), ("标记", "center")]
        return headers
    headers = [("时间", "left"), ("曲名", "left"), ("难度", "left"), ("分数", "right")]
    if show_acc:
        headers.append(("acc", "right"))
    return headers


def table_values(row: dict, show_details: bool, show_acc: bool = True) -> list[str]:
    """一行的单元格文本，顺序与 table_headers 对应。"""
    cells = [
        fmt_time(row.get("recorded_at")),
        row["title"],
        f"{str(row['chart']).upper()} {fmt_diff(row.get('difficulty'))}",
        str(row["score"]),
    ]
    if show_acc:
        cells.append(fmt_acc(acc_of(row)))
    if show_details:
        p, g, b, m = row.get("perfect"), row.get("good"), row.get("bad"), row.get("miss")
        judge = f"P{p} G{g} B{b} M{m}" if None not in (p, g, b, m) else "—"
        early, late = row.get("early"), row.get("late")
        el = f"E{early} L{late}" if early is not None and late is not None else "—"
        combo = row.get("combo")
        if combo is None:
            combo = row.get("max_combo")
        mark = "AP" if row["score"] == 1_000_000 else ("FC" if row.get("fc") else "—")
        cells += [judge, el, str(combo) if combo is not None else "—", mark]
    return cells


def render_settings(settings: dict) -> str:
    mode = MODE_LABEL.get(settings["mode_filter"], settings["mode_filter"])
    detail = "显示判定细节" if settings["show_details"] else "不显示判定细节"
    sub = "含次难度" if settings.get("show_sub") else "不含次难度"
    fmt = "图片" if settings.get("fmt") == "image" else "文本"
    return f"当前设置：{mode} · {detail} · {sub} · {fmt}\n{SETTINGS_HINT}"
