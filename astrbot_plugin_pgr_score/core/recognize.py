"""VLM 识别、字段校验与落库字段派生。"""

from __future__ import annotations

import json
import re

# 三类图 → 判种：宽判(单曲)带 acc，严判(HUD)与课题都没有 acc
CATEGORY_TO_MODE = {"single": "normal", "hud": "strict", "course": "strict"}

PROMPT = """你是音游 Phigros 的成绩截图识别器。读图，只输出一个 JSON，不要解释、不要 markdown 代码块、不要多余文字。

【第一步：判断类型 category】
hud    = 游玩中界面。特征：左上角暂停键、屏幕中央 COMBO 计数、有判定线，没有任何判定统计。
course = 课题模式结算。特征：纵向三行成绩，每行都有曲名、难度、分数、Perfect/Good/Bad/Miss、Early/Late，底部有总分和大字母。
single = 单曲结算。特征：左侧曲绘、右侧一个大分数、下方 Max Combo 和 Accuracy、一组判定统计。

【第二步：按下面的格式输出】
键一个都不能少，图上没有的填 null：
{"category":"hud","total_score":null,"songs":[{"title":null,"difficulty":null,"level":null,"score":null,"score_raw":null,"perfect":null,"good":null,"bad":null,"miss":null,"early":null,"late":null,"combo":null,"max_combo":null,"accuracy":null}]}

【读数规则】
- score_raw 是图上原样的数字串。course、single 的分数是 7 位补零，前导零必须保留，例如 "0955954"；hud 一般不补零。
- score 是同一个数的整数形式，例如 955954，不要千分位。
- difficulty 只能是 EZ / HD / IN / AT 之一；level 是紧跟在难度后面的整数，例如 "IN Lv.15" → difficulty="IN", level=15。
- accuracy 去掉百分号、保留两位小数，例如 "99.89%" → 99.89。
- perfect / good / bad / miss / early / late 都是非负整数；early 和 late 是 good 的细分，early + late 应当等于 good。
- 曲名照抄图上原文，日文、中文、英文都原样保留，不翻译、不加引号、不补全；整段看不清就填 null。
- 曲名要取完整全名：曲绘上如果同时有大号短标题和一行小字全名（例如大号写 GOLDEN、小字写 Silence is Golden, Speech is Golden），必须输出那行完整的全名，不要只给大号短标题。
- course 的 songs 必须正好 3 个，按从上到下的视觉顺序；三首可能是同一首歌，也可能完全不同，不许合并、不许按难度重排。
- hud 的 songs 只有 1 个；判定字段全部 null；顶部 COMBO 是**当前连击**（停下时打到多少连），不是最大连击，填到 combo 字段，max_combo 填 null；accuracy 填 null。
- single 的 songs 只有 1 个；Max Combo 才是最大连击，填到 max_combo，combo 填 null。
- total_score：course 填图上印的总分（没印就把三行 score 相加）；hud、single 填该曲的分数。

【禁止猜测】
数字被判定线、反光、屏幕边框挡住，或任何一位看不清时，宁可把 score_raw 和 score 一起填 null，也不要编一个看起来合理的数。判定数、连击数同理：不确定就 null。曲名读不全也填 null。
如果这张图根本不是音游成绩图，输出 {"category":"other","total_score":null,"songs":[]}。"""

COMBO_PROMPT = "这是音游 HUD 的 COMBO 计数区域。只输出这个数字，不要解释、不要逗号；数字被线条压住看不清就输出 null。"


def clean_json(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j != -1 and j > i:
        text = text[i:j + 1]
    return text


def parse_output(text: str) -> dict:
    return json.loads(clean_json(text))


def fc_by_notes(notes, *combos) -> bool:
    """连击数达到谱面物量即 FC。物量未知（None/0）时返回 False。"""
    if not notes:
        return False
    return any(isinstance(value, int) and value >= notes for value in combos if value is not None)


def derive_records(data: dict):
    """把 VLM 输出转成待落库记录，返回 (records, warnings, total_score)。"""
    category = data.get("category")
    mode = CATEGORY_TO_MODE.get(category)
    songs = data.get("songs") or []
    total = data.get("total_score")
    records: list[dict] = []
    warnings: list[str] = []

    for song in songs:
        title = song.get("title")
        score = song.get("score")
        raw = song.get("score_raw")
        difficulty = song.get("difficulty")
        level = song.get("level")
        p, g, b, m = song.get("perfect"), song.get("good"), song.get("bad"), song.get("miss")
        early, late = song.get("early"), song.get("late")
        acc = song.get("accuracy")

        # HUD 顶部的 COMBO 是「当前连击」，不是最大连击；single 的 Max Combo 才是最大连击
        max_combo = song.get("max_combo")
        combo = song.get("combo")
        if category == "hud":
            current_combo = combo if combo is not None else max_combo  # 兼容旧输出
            max_combo = None
        else:
            current_combo = None

        if mode is None or category == "other":
            warnings.append("没认出是哪种成绩图")
            continue
        if score is None:
            warnings.append(f"{title or '某行'}：分数没读到，跳过")
            continue
        if title is None:
            warnings.append("有一行曲名没读到，跳过")
            continue

        # acc：宽判直接读图上值；读不到就用判定反推
        if mode == "normal" and acc is None and None not in (p, g, b, m):
            n = p + g + b + m
            if n:
                acc = round((p + 0.65 * g) / n * 100, 2)

        # FC 判定（当前口径）：
        #   1) AP（满分）即 FC；
        #   2) 判定齐全且 bad=0、miss=0 —— 等价于「连击打满物量」，不需要曲库物量就能判；
        #   3) combo / max_combo 达到物量 —— 物量还没写进 songs.json，等有了由 main.py 补判。
        # 注意：HUD 的当前连击不能单独用来判 FC（中途截图时连击可能还会断）。
        fc = score == 1_000_000
        if None not in (p, g, b, m):
            fc = (b == 0 and m == 0) or fc

        # 判定条目的自洽检查（所有类型都做）
        if None not in (p, g, b, m):
            if early is not None and late is not None and early + late != g:
                warnings.append(f"{title}：early+late={early + late}，但 good={g}")

        # 分数公式校验：只有宽判做（严判/课题不校验）
        if mode == "normal" and None not in (p, g, b, m):
            n = p + g + b + m
            if n:
                acc_calc = (p + 0.65 * g) / n * 100
                if max_combo is not None:
                    pred = round(900_000 * acc_calc / 100 + 100_000 * max_combo / n)
                    if abs(pred - score) > 1:
                        warnings.append(f"{title}：分数 {score} 与判定对不上（公式 {pred}）")
                if acc is not None and abs(acc - acc_calc) > 0.05:
                    warnings.append(f"{title}：acc {acc} 与公式 {acc_calc:.2f} 差较大")

        records.append({
            "title": title,
            "chart": (difficulty or "IN").upper().lower(),
            "chart_label": (difficulty or "IN").upper(),
            "level": level,
            "mode": mode,
            "score": score,
            "score_raw": raw if isinstance(raw, str) else str(score),
            "acc": acc,
            "fc": bool(fc),
            "combo": current_combo,
            "max_combo": max_combo,
            "perfect": p,
            "good": g,
            "bad": b,
            "miss": m,
            "early": early,
            "late": late,
        })

    return records, warnings, total
