"""Phigros 成绩库：沿用 my_pgr_scores 的 songs/scores 结构，另加 qq 绑定。

- songs: 曲库（曲名/章节/bpm/难度/别名），来源 songs.json
- scores: 每次提交（按 qq 归属），mode 区分宽判(normal，存 acc)与严判(strict，不存 acc)
"""

from __future__ import annotations

import difflib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

CHINA = timezone(timedelta(hours=8))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    song_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    chapter TEXT NOT NULL,
    bpm REAL NOT NULL,
    difficulty_json TEXT NOT NULL,
    alias_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    qq TEXT NOT NULL,
    song_id TEXT NOT NULL REFERENCES songs(song_id),
    chart TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'normal' CHECK(mode IN ('normal', 'strict')),
    score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 1000000),
    acc REAL CHECK(acc IS NULL OR acc BETWEEN 0 AND 100),
    fc INTEGER NOT NULL DEFAULT 0 CHECK(fc IN (0, 1)),
    combo INTEGER,
    max_combo INTEGER,
    perfect INTEGER,
    good INTEGER,
    bad INTEGER,
    miss INTEGER,
    early INTEGER,
    late INTEGER,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scores_qq ON scores(qq, song_id, chart, mode, score DESC);
CREATE TABLE IF NOT EXISTS user_settings (
    qq TEXT PRIMARY KEY,
    mode_filter TEXT NOT NULL DEFAULT 'strict' CHECK(mode_filter IN ('strict', 'normal', 'all')),
    show_details INTEGER NOT NULL DEFAULT 0 CHECK(show_details IN (0, 1)),
    show_sub INTEGER NOT NULL DEFAULT 0 CHECK(show_sub IN (0, 1)),
    fmt TEXT NOT NULL DEFAULT 'text' CHECK(fmt IN ('text', 'image')),
    updated_at TEXT NOT NULL
);
"""

# 旧库补列：识别不到的存 NULL
_EXTRA_COLUMNS = ("combo", "max_combo", "perfect", "good", "bad", "miss", "early", "late")


class ScoreDatabase:
    def __init__(self, db_path: Path, catalog_paths: list[Path] | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=15)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(scores)")}
            for name in _EXTRA_COLUMNS:
                if name not in existing:
                    # 老库补列，历史记录该列自然为 NULL
                    self._conn.execute(f"ALTER TABLE scores ADD COLUMN {name} INTEGER")
            setting_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(user_settings)")}
            if "show_sub" not in setting_columns:
                # 老库补列，默认不含次难度（EZ/HD）
                self._conn.execute(
                    "ALTER TABLE user_settings ADD COLUMN show_sub INTEGER NOT NULL DEFAULT 0"
                )
            if "fmt" not in setting_columns:
                # 老库补列，默认纯文本
                self._conn.execute(
                    "ALTER TABLE user_settings ADD COLUMN fmt TEXT NOT NULL DEFAULT 'text'"
                )
            # 早期版本的 fmt 允许 'markdown'，现在改成 text/image，需要重建表
            table_sql = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'user_settings'"
            ).fetchone()["sql"] or ""
            if "'markdown'" in table_sql:
                self._rebuild_settings_table()
            self._conn.commit()
        self.catalog: list[dict] = []
        self.game_version: str = ""
        loaded = False
        for path in catalog_paths or []:
            if Path(path).is_file():
                self.sync_catalog(Path(path))
                loaded = True
                break
        if not loaded:
            # 没有曲库文件时，用上次同步进库的 songs 表兜底，避免曲名匹配直接失效
            self._load_catalog_from_db()

    def _load_catalog_from_db(self) -> None:
        with self._lock:
            rows = self._conn.execute(
                "SELECT song_id, title, chapter, bpm, difficulty_json, alias_json FROM songs"
            ).fetchall()
        self.catalog = [
            {
                "id": row["song_id"],
                "title": row["title"],
                "chapter": row["chapter"],
                "bpm": row["bpm"],
                "difficulty": json.loads(row["difficulty_json"] or "{}"),
                "alias": json.loads(row["alias_json"] or "[]"),
            }
            for row in rows
        ]

    # ---------- 曲库 ----------
    def sync_catalog(self, path: Path) -> int:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        songs = data.get("data")
        if not isinstance(songs, list):
            raise ValueError("songs.json 里没有 data 数组")
        # 曲库对应的游戏版本：新格式叫 game_version，旧格式叫 version
        self.game_version = str(data.get("game_version") or data.get("version") or "")
        self.catalog = songs
        rows = []
        for song in songs:
            rows.append((
                song["id"], song.get("title") or "", song.get("chapter") or "",
                song.get("bpm") or 0,
                json.dumps(song.get("difficulty") or {}, ensure_ascii=False),
                json.dumps(song.get("alias") or [], ensure_ascii=False),
            ))
        with self._lock:
            self._conn.executemany(
                "INSERT INTO songs(song_id,title,chapter,bpm,difficulty_json,alias_json) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(song_id) DO UPDATE SET "
                "title=excluded.title, chapter=excluded.chapter, bpm=excluded.bpm, "
                "difficulty_json=excluded.difficulty_json, alias_json=excluded.alias_json",
                rows,
            )
            self._conn.commit()
        return len(rows)

    def match_song(self, title: str | None) -> dict | None:
        """按曲名/别名/包含/模糊匹配返回曲库条目（含 difficulty dict）。"""
        if not title:
            return None
        needle = str(title).strip()
        nf = needle.casefold()
        for song in self.catalog:
            if (song.get("title") or "").casefold() == nf:
                return song
        for song in self.catalog:
            for alias in (song.get("alias") or []):
                if str(alias).casefold() == nf:
                    return song
        # 包含匹配：多个命中时取长度最接近的，避免 "GOLDEN" 这类短词配错歌
        candidates = []
        for song in self.catalog:
            t = (song.get("title") or "").casefold()
            if not t or not nf:
                continue
            if nf in t or t in nf:
                candidates.append((abs(len(t) - len(nf)), song))
        if candidates:
            candidates.sort(key=lambda item: item[0])
            return candidates[0][1]
        names = [(song.get("title") or "") for song in self.catalog]
        close = difflib.get_close_matches(needle, names, n=1, cutoff=0.6)
        if close:
            for song in self.catalog:
                if (song.get("title") or "") == close[0]:
                    return song
        return None

    def difficulty_of(self, song: dict | None, chart: str):
        if not song:
            return None
        difficulties = song.get("difficulty") or {}
        value = difficulties.get((chart or "").lower())
        if value == "":
            return None
        return value

    def note_count(self, song: dict | None, chart: str) -> int | None:
        """谱面物量（音符数）。songs.json 目前还没写这个字段，没有就返回 None。"""
        if not song:
            return None
        chart = (chart or "").lower()
        for key in ("notes", "note_count", "noteCount", "note_counts"):
            value = song.get(key)
            if isinstance(value, dict):
                value = value.get(chart)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if value > 0:
                return int(value)
        difficulties = song.get("difficulty")
        if isinstance(difficulties, dict):
            value = difficulties.get(chart)
            if isinstance(value, dict):
                for key in ("notes", "note_count", "count"):
                    inner = value.get(key)
                    if isinstance(inner, (int, float)) and not isinstance(inner, bool) and inner > 0:
                        return int(inner)
        return None

    # ---------- 成绩 ----------
    def add_score(
        self, qq: str, song_id: str, score: int, chart: str = "in",
        *, mode: str = "normal", acc: float | None = None, fc: bool = False,
        combo: int | None = None, max_combo: int | None = None,
        perfect: int | None = None, good: int | None = None,
        bad: int | None = None, miss: int | None = None,
        early: int | None = None, late: int | None = None,
    ) -> int:
        qq = str(qq or "")
        if not qq:
            raise ValueError("qq 不能为空")
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 1_000_000:
            raise ValueError("score 必须是 0~1000000 的整数")
        fc = bool(fc) or score == 1_000_000
        if mode not in ("normal", "strict"):
            raise ValueError("mode 必须是 normal 或 strict")
        if mode == "normal" and acc is None:
            raise ValueError("宽判需要 acc")
        if mode == "strict" and acc is not None:
            raise ValueError("严判不存 acc")
        if acc is not None and not 0 <= acc <= 100:
            raise ValueError("acc 必须在 0~100")
        extra = {
            "combo": combo, "max_combo": max_combo, "perfect": perfect,
            "good": good, "bad": bad, "miss": miss, "early": early, "late": late,
        }
        for name, value in extra.items():
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{name} 必须是非负整数或 None")
        chart = str(chart).lower()
        with self._lock:
            row = self._conn.execute(
                "SELECT difficulty_json FROM songs WHERE song_id = ?", (song_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"未知 song_id: {song_id}")
            difficulties = json.loads(row["difficulty_json"] or "{}")
            if chart not in difficulties or difficulties[chart] == "":
                raise ValueError(f"{song_id} 没有 {chart.upper()} 谱面")
            stamp = datetime.now(CHINA).isoformat(timespec="seconds")
            cur = self._conn.execute(
                "INSERT INTO scores("
                "qq, song_id, chart, mode, score, acc, fc, "
                "combo, max_combo, perfect, good, bad, miss, early, late, recorded_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    qq, song_id, chart, mode, score, acc, int(fc),
                    combo, max_combo, perfect, good, bad, miss, early, late, stamp,
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def recent_scores(
        self, qq: str, limit: int = 10, mode_filter: str = "strict", show_sub: bool = False,
    ) -> list[dict]:
        """按提交时间倒序取最近 limit 条。show_sub=False 时不显示次难度（EZ/HD）。"""
        if mode_filter not in ("strict", "normal", "all"):
            raise ValueError("mode_filter 必须是 strict/normal/all")
        sql = (
            "SELECT s.id, s.song_id, g.title, g.difficulty_json, s.chart, s.mode, "
            "s.score, s.acc, s.fc, s.combo, s.max_combo, s.perfect, s.good, s.bad, "
            "s.miss, s.early, s.late, s.recorded_at "
            "FROM scores s JOIN songs g ON g.song_id = s.song_id "
            "WHERE s.qq = ?"
        )
        params: list = [str(qq)]
        if mode_filter != "all":
            sql += " AND s.mode = ?"
            params.append(mode_filter)
        if not show_sub:
            sql += " AND s.chart NOT IN ('ez', 'hd')"
        sql += " ORDER BY s.id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            difficulties = json.loads(item.pop("difficulty_json") or "{}")
            value = difficulties.get(item["chart"])
            item["difficulty"] = None if value == "" else value
            item["fc"] = bool(item["fc"])
            result.append(item)
        return result

    def count_for(self, qq: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM scores WHERE qq = ?", (str(qq),)
            ).fetchone()
            return int(row["c"])

    # ---------- 显示设置 ----------
    DEFAULT_SETTINGS = {"mode_filter": "strict", "show_details": False,
                        "show_sub": False, "fmt": "text"}

    def _rebuild_settings_table(self) -> None:
        """把旧 fmt 约束（text/markdown）重建为 text/image，保留已有设置。"""
        self._conn.executescript("""
            CREATE TABLE user_settings_new (
                qq TEXT PRIMARY KEY,
                mode_filter TEXT NOT NULL DEFAULT 'strict'
                    CHECK(mode_filter IN ('strict', 'normal', 'all')),
                show_details INTEGER NOT NULL DEFAULT 0 CHECK(show_details IN (0, 1)),
                show_sub INTEGER NOT NULL DEFAULT 0 CHECK(show_sub IN (0, 1)),
                fmt TEXT NOT NULL DEFAULT 'text' CHECK(fmt IN ('text', 'image')),
                updated_at TEXT NOT NULL
            );
            INSERT INTO user_settings_new(qq, mode_filter, show_details, show_sub, fmt, updated_at)
                SELECT qq, mode_filter, show_details, COALESCE(show_sub, 0),
                       CASE WHEN fmt = 'markdown' THEN 'text' ELSE COALESCE(fmt, 'text') END,
                       updated_at
                FROM user_settings;
            DROP TABLE user_settings;
            ALTER TABLE user_settings_new RENAME TO user_settings;
        """)

    def get_settings(self, qq: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT mode_filter, show_details, show_sub, fmt FROM user_settings WHERE qq = ?",
                (str(qq),),
            ).fetchone()
        if row is None:
            return dict(self.DEFAULT_SETTINGS)
        return {
            "mode_filter": row["mode_filter"],
            "show_details": bool(row["show_details"]),
            "show_sub": bool(row["show_sub"]),
            "fmt": row["fmt"] or "text",
        }

    def set_settings(
        self, qq: str, *, mode_filter: str | None = None,
        show_details: bool | None = None, show_sub: bool | None = None,
        fmt: str | None = None,
    ) -> dict:
        current = self.get_settings(qq)
        if mode_filter is not None:
            if mode_filter not in ("strict", "normal", "all"):
                raise ValueError("mode_filter 必须是 strict/normal/all")
            current["mode_filter"] = mode_filter
        if show_details is not None:
            current["show_details"] = bool(show_details)
        if show_sub is not None:
            current["show_sub"] = bool(show_sub)
        if fmt is not None:
            if fmt not in ("text", "image"):
                raise ValueError("fmt 必须是 text 或 image")
            current["fmt"] = fmt
        stamp = datetime.now(CHINA).isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "INSERT INTO user_settings(qq, mode_filter, show_details, show_sub, fmt, updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(qq) DO UPDATE SET "
                "mode_filter=excluded.mode_filter, show_details=excluded.show_details, "
                "show_sub=excluded.show_sub, fmt=excluded.fmt, updated_at=excluded.updated_at",
                (str(qq), current["mode_filter"], int(current["show_details"]),
                 int(current["show_sub"]), current["fmt"], stamp),
            )
            self._conn.commit()
        return current

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
