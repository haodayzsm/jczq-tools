#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
竞彩足球数据分析工具 v1.0
功能：
  1. 在售赔率实时查看（手动刷新 / 5分钟自动刷新）
  2. 历史相似赔率统计：输入目标赔率+浮动范围，查出区间内历史场次及胜平负分布
  3. 一键更新历史数据（增量合并，可指定日期区间）
数据源：体彩官方 API（webapi.sporttery.cn）
运行：python jczq_app.py   （或打包为 exe 直接运行）
数据文件：与本程序同目录的 data/ 下
"""

import sys
import os
import json
import csv
import time
import queue
import sqlite3
import threading
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

APP_VERSION = "v1.4.2"

# 数据目录：exe/script 同目录（打包后也可移植）
if getattr(sys, "frozen", False):          # PyInstaller 打包后
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DB_FILE = DATA_DIR / "jczq.db"              # 内置 SQLite 数据库：全部数据都在这一个文件
HIST_FILE = DATA_DIR / "history.json"       # 旧版 JSON（仅用于自动迁移）
FETCH_LOG_FILE = DATA_DIR / "fetch_log.json"  # 旧版抓取记录（仅用于自动迁移）

BASE = "https://webapi.sporttery.cn"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": "https://www.sporttery.cn/",
}

CSV_FIELDS = [
    "match_id", "week", "match_num", "league", "home", "away",
    "match_date", "match_time", "odds_win", "odds_draw", "odds_lose",
    "goal_line", "hhad_win", "hhad_draw", "hhad_lose",
    "single", "score", "half_score", "result", "fetched_at",
]


# ══════════════════════ 数据层（与 jczq.py 同源） ══════════════════════

def api_get(path, params, retries=3):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    last_err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with OPENER.open(req, timeout=20) as resp:
                data = json.loads(resp.read())
            if data.get("errorCode") != "0":
                raise RuntimeError(f"API错误 {data.get('errorCode')}: {data.get('errorMessage')}")
            return data
        except Exception as e:
            last_err = e
            if i < retries - 1:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"请求失败(重试{retries}次): {last_err}")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return ""


def map_match(m, fetched_at, has_odds=True):
    if has_odds and isinstance(m.get("had"), dict):
        odds = m["had"]
        h, d, a = odds.get("h"), odds.get("d"), odds.get("a")
        hh = m.get("hhad") or {}
        line = hh.get("goalLine")
        hhad = {"h": hh.get("h"), "d": hh.get("d"), "a": hh.get("a")}
    else:
        h, d, a = m.get("h"), m.get("d"), m.get("a")
        line = m.get("goalLine")
        hhad = {}
    return {
        "match_id": m.get("matchId") or "",
        "week": m.get("weekNum") or "",
        "match_num": m.get("matchNumStr") or "",
        "league": m.get("leagueNameAbbr") or m.get("leagueName") or m.get("leagueAbbName") or "",
        "home": m.get("homeTeam") or m.get("homeTeamAbbName") or "",
        "away": m.get("awayTeam") or m.get("awayTeamAbbrName") or m.get("awayTeamAbbName") or "",
        "match_date": m.get("matchDate") or "",
        "match_time": m.get("matchTime") or "",
        "odds_win": _num(h),
        "odds_draw": _num(d),
        "odds_lose": _num(a),
        "goal_line": _num(line),
        "hhad_win": _num(hhad.get("h")) if hhad else "",
        "hhad_draw": _num(hhad.get("d")) if hhad else "",
        "hhad_lose": _num(hhad.get("a")) if hhad else "",
        "single": m.get("bettingSingle") or "",
        "score": (m.get("sectionsNo999") or "").strip(),
        "half_score": (m.get("sectionsNo1") or "").strip(),
        "result": m.get("winFlag") or "",
        "fetched_at": fetched_at,
    }


def _derive_result(row):
    """官方漏发赛果标记时，从比分推导 H/D/A（取消/无效场次不算）"""
    if row.get("result") in ("H", "D", "A"):
        return row["result"]
    score = (row.get("score") or "").strip()
    if ":" in score:
        try:
            hg, ag = [int(x) for x in score.split(":")[:2]]
        except ValueError:
            return ""
        return "H" if hg > ag else ("D" if hg == ag else "A")
    return ""


def repair_results():
    """一次性修补：给库里有比分但没赛果的场次补算 H/D/A。幂等。"""
    db_init()
    fixed = 0
    with DB_LOCK, db() as conn:
        rows = conn.execute(
            "SELECT match_id, score FROM matches "
            "WHERE (result IS NULL OR result='') AND score LIKE '%_%'").fetchall()
        for r in rows:
            res = _derive_result({"result": "", "score": r["score"]})
            if res:
                conn.execute("UPDATE matches SET result=? WHERE match_id=?", (res, r["match_id"]))
                fixed += 1
    return fixed


# ── SQLite 数据库层 ──────────────────────────────────

DB_LOCK = threading.Lock()   # 多线程 GUI 下保护写操作


def db():
    """获取数据库连接（WAL 模式：并发读写不互塞，断电不烂库）"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def db_init():
    """建表 + 索引 + 旧数据自动迁移（幂等，可反复调用）"""
    with DB_LOCK, db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS matches (
            match_id     TEXT PRIMARY KEY,
            week         TEXT, match_num TEXT,
            league       TEXT, home TEXT, away TEXT,
            match_date   TEXT, match_time TEXT,
            odds_win     REAL, odds_draw REAL, odds_lose REAL,
            goal_line    REAL,
            hhad_win     REAL, hhad_draw REAL, hhad_lose REAL,
            single       TEXT, score TEXT, half_score TEXT,
            result       TEXT, fetched_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_matches_date   ON matches(match_date);
        CREATE INDEX IF NOT EXISTS idx_matches_odds   ON matches(odds_win, odds_draw, odds_lose);
        CREATE INDEX IF NOT EXISTS idx_matches_result ON matches(result);
        CREATE INDEX IF NOT EXISTS idx_matches_league ON matches(league);
        CREATE TABLE IF NOT EXISTS fetch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            win_begin TEXT NOT NULL, win_end TEXT NOT NULL,
            UNIQUE(win_begin, win_end)
        );
        CREATE TABLE IF NOT EXISTS onsale (
            match_id TEXT PRIMARY KEY, payload TEXT, fetched_at TEXT
        );
        """)
        # ── 旧版 JSON 自动迁移（一次性）──
        if HIST_FILE.exists():
            try:
                old = json.loads(HIST_FILE.read_text(encoding="utf-8"))
                if old:
                    n = upsert_matches(conn, old)
                    if n >= 0:
                        HIST_FILE.rename(HIST_FILE.with_suffix(".json.migrated"))
                        print(f"[迁移] history.json → 数据库: {len(old)} 条已并入")
            except Exception as e:
                print(f"[迁移] history.json 失败(保留原文件): {e}")
        if FETCH_LOG_FILE.exists():
            try:
                log = json.loads(FETCH_LOG_FILE.read_text(encoding="utf-8"))
                for b, e in log.get("windows", []):
                    conn.execute("INSERT OR IGNORE INTO fetch_log(win_begin, win_end) VALUES(?,?)", (b, e))
                FETCH_LOG_FILE.rename(FETCH_LOG_FILE.with_suffix(".json.migrated"))
                print("[迁移] fetch_log.json → 数据库")
            except Exception as e:
                print(f"[迁移] fetch_log.json 失败(保留原文件): {e}")


def _row_dict(r):
    return {k: ("" if r[k] is None else r[k]) for k in r.keys()} if not isinstance(r, dict) else r


def upsert_matches(conn, rows):
    """UPSERT：按 match_id 插入；已存在且有新赛果才更新。返回新增条数"""
    added = 0
    upd_cols = [k for k in CSV_FIELDS if k != "match_id"]
    for r in rows:
        if not r.get("match_id"):
            continue
        vals = [r.get(k, "") for k in CSV_FIELDS]
        cur = conn.execute(
            "INSERT OR IGNORE INTO matches (" + ",".join(CSV_FIELDS) + ") VALUES(" +
            ",".join("?" * len(CSV_FIELDS)) + ")", vals)
        if cur.rowcount == 1:          # 真新增
            added += 1
            continue
        # 已存在：仅当新数据带赛果（或库里还没比分）才更新，避免旧赛果被无赛果数据冲掉
        conn.execute(
            "UPDATE matches SET " + ",".join(f"{k}=?" for k in upd_cols) +
            " WHERE match_id=? AND (? != '' OR ? != '' OR score = '')",
            [r.get(k, "") for k in upd_cols] + [r["match_id"], r.get("score", ""), r.get("result", "")])
    return added


def load_history():
    """读全部历史（dict 列表，兼容旧接口）"""
    db_init()
    with DB_LOCK, db() as conn:
        rows = conn.execute("SELECT * FROM matches ORDER BY match_date, match_id").fetchall()
    return [_row_dict(r) for r in rows]


def merge_history(new_rows):
    """增量合并入库，返回 (全部行, 新增数)"""
    db_init()
    with DB_LOCK, db() as conn:
        added = upsert_matches(conn, new_rows)
    return load_history(), added


def load_onsale():
    db_init()
    with DB_LOCK, db() as conn:
        rows = conn.execute("SELECT payload, fetched_at FROM onsale").fetchall()
    return [json.loads(r["payload"]) for r in rows], (rows[0]["fetched_at"] if rows else "")


def save_onsale(rows):
    db_init()
    with DB_LOCK, db() as conn:
        conn.execute("DELETE FROM onsale")
        for r in rows:
            conn.execute("INSERT INTO onsale(match_id, payload, fetched_at) VALUES(?,?,?)",
                         (r.get("match_id", ""), json.dumps(r, ensure_ascii=False), r.get("fetched_at", "")))


# ── 抓取记录（fetch_log 表）：已完整落盘的「过去窗口」不再重复抓 ──

def _load_fetch_log():
    db_init()
    with DB_LOCK, db() as conn:
        rows = conn.execute("SELECT win_begin, win_end FROM fetch_log ORDER BY win_begin").fetchall()
    return {"windows": [[r["win_begin"], r["win_end"]] for r in rows]}


def _save_fetch_log(log):
    db_init()
    with DB_LOCK, db() as conn:
        conn.execute("DELETE FROM fetch_log")
        for b, e in log.get("windows", []):
            conn.execute("INSERT OR IGNORE INTO fetch_log(win_begin, win_end) VALUES(?,?)", (b, e))


def _next_day(d):
    return (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def _is_past_window(end_str):
    """窗口结束日早于今天 → 该窗口赛果已定格，抓一次就够（未完赛场次由 match_id 去重兜底）"""
    return end_str < datetime.now().strftime("%Y-%m-%d")


def _window_covered(log, wb, we):
    """窗口是否已被记录覆盖"""
    for b, e in log.get("windows", []):
        if b <= wb and we <= e:
            return True
    return False


def reset_fetch_log():
    """清空抓取记录（数据保留，仅强制下次重抓）"""
    db_init()
    with DB_LOCK, db() as conn:
        conn.execute("DELETE FROM fetch_log")


def fetch_odds_history(match_id):
    """调官方赔率历史接口：返回收盘的 胜平负(had) + 让球胜平负(hhad) 赔率"""
    data = api_get("/gateway/uniform/football/getOddsHistoryV1.qry",
                   {"matchId": str(match_id)})
    v = data.get("value") or {}
    had_list = v.get("hadList") or []
    hhad_list = v.get("hhadList") or []
    # 列表按时间倒序，首条即最后更新（收盘价）
    had = had_list[0] if had_list else {}
    hhad = hhad_list[0] if hhad_list else {}
    return {
        "odds_win": _num(had.get("h")), "odds_draw": _num(had.get("d")), "odds_lose": _num(had.get("a")),
        "goal_line": _num(hhad.get("goalLine")),
        "hhad_win": _num(hhad.get("h")), "hhad_draw": _num(hhad.get("d")), "hhad_lose": _num(hhad.get("a")),
        "odds_snapshots": len(had_list),
    }


def enrich_hhad(progress_cb=None, limit=None):
    """
    补全让球赔率：对库里缺 hhad 的场次逐个调赔率历史接口，取收盘价写入。
    返回 (成功数, 跳过数, 失败数)
    """
    db_init()
    with DB_LOCK, db() as conn:
        rows = conn.execute(
            "SELECT match_id FROM matches WHERE (hhad_win IS NULL OR hhad_win='') "
            "AND result IN ('H','D','A') ORDER BY match_date DESC").fetchall()
    todo = [r["match_id"] for r in rows]
    if limit:
        todo = todo[:limit]
    ok = fail = skip = 0
    for i, mid in enumerate(todo, 1):
        try:
            odds = fetch_odds_history(mid)
            if odds["odds_snapshots"] == 0:
                skip += 1   # 官方无该场赔率记录（老场次或未开售）
            elif odds["hhad_win"] != "":
                with DB_LOCK, db() as conn:
                    conn.execute(
                        "UPDATE matches SET odds_win=?, odds_draw=?, odds_lose=?, goal_line=?, "
                        "hhad_win=?, hhad_draw=?, hhad_lose=? WHERE match_id=?",
                        (odds["odds_win"], odds["odds_draw"], odds["odds_lose"], odds["goal_line"],
                         odds["hhad_win"], odds["hhad_draw"], odds["hhad_lose"], mid))
                ok += 1
            else:
                skip += 1
        except Exception:
            fail += 1
            time.sleep(1)
        if progress_cb and (i % 5 == 0 or i == len(todo)):
            progress_cb(i, len(todo), ok, skip, fail)
        time.sleep(0.35)   # 温柔限速
    return ok, skip, fail


def fetch_onsale():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data = api_get("/gateway/jc/football/getMatchCalculatorV1.qry",
                   {"poolCode": "hhad,had", "channel": "c"})
    rows = []
    for league in data.get("value", {}).get("matchInfoList", []) or []:
        for m in league.get("subMatchList", []) or []:
            rows.append(map_match(m, now, has_odds=True))
    save_onsale(rows)
    return rows


def fetch_history_window(begin, end):
    all_matches = []
    page = 1
    while True:
        data = api_get("/gateway/uniform/football/getUniformMatchResultV1.qry", {
            "matchPage": "1", "matchBeginDate": begin, "matchEndDate": end,
            "leagueId": "", "pageSize": "30", "pageNo": str(page),
            "isFix": "0", "pcOrWap": "1",
        })
        v = data.get("value") or {}
        lst = v.get("matchResult") or []
        all_matches.extend(lst)
        total = int(v.get("total") or 0)
        if page * 30 >= total or not lst:
            break
        page += 1
        time.sleep(0.5)
    return all_matches


def daterange_windows(begin, end, days=7):
    b = datetime.strptime(begin, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    if b > e:
        raise ValueError("起始日期晚于结束日期")
    cur = b
    while cur <= e:
        we = min(cur + timedelta(days=days - 1), e)
        yield cur.strftime("%Y-%m-%d"), we.strftime("%Y-%m-%d")
        cur = we + timedelta(days=1)


def do_fetch_history(begin, end, progress_cb=None):
    """增量抓取：过去且已记录的窗口直接跳过，只抓新增部分。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log = _load_fetch_log()
    raw_all = []
    windows = list(daterange_windows(begin, end))
    skipped = 0
    for i, (wb, we) in enumerate(windows, 1):
        if _is_past_window(we) and _window_covered(log, wb, we):
            skipped += 1
            if progress_cb:
                progress_cb(i, len(windows), wb, we, -1)   # -1 = 跳过
            continue
        raw = fetch_history_window(wb, we)
        raw_all.extend(raw)
        # 只有「过去窗口」才记录为已完成（今天/未来的场次后续会出赛果，要允许重抓）
        if _is_past_window(we):
            log.setdefault("windows", []).append([wb, we])
        if progress_cb:
            progress_cb(i, len(windows), wb, we, len(raw))
    _save_fetch_log(log)
    rows = [map_match(m, now, has_odds=False) for m in raw_all]
    merged, added = merge_history(rows)
    return merged, added, len(rows), skipped


# ══════════════════════ 统计引擎 ══════════════════════

SIDE_KEYS = {"win": "odds_win", "draw": "odds_draw", "lose": "odds_lose"}


def _base_filters(r, league="", date_from="", date_to="", finished=True):
    """公共过滤：联赛/日期/完赛"""
    if league and league not in (r.get("league") or ""):
        return False
    md = r.get("match_date") or ""
    if date_from and md < date_from:
        return False
    if date_to and md > date_to:
        return False
    if finished and r.get("result") not in ("H", "D", "A"):
        return False
    return True


def _dist_stats(rows, side):
    """胜平负分布 + 模拟ROI（未完赛场次只计数不进ROI）"""
    n = len(rows)
    w = sum(1 for r in rows if r.get("result") == "H")
    d = sum(1 for r in rows if r.get("result") == "D")
    l = sum(1 for r in rows if r.get("result") == "A")
    key = SIDE_KEYS.get(side)
    roi = 0.0
    if n and side in SIDE_KEYS:
        total_ret = 0.0
        n_fin = 0
        for r in rows:
            if r.get("result") not in ("H", "D", "A"):
                continue
            n_fin += 1
            odds = r.get(key, "")
            if odds == "":
                continue
            hit = {"H": "win", "D": "draw", "A": "lose"}[r["result"]] == side
            total_ret += (float(odds) - 1) * 2 if hit else -2
        roi = (total_ret / (n_fin * 2) * 100) if n_fin else 0
    return {
        "n": n, "win": w, "draw": d, "lose": l,
        "p_win": w / n * 100 if n else 0,
        "p_draw": d / n * 100 if n else 0,
        "p_lose": l / n * 100 if n else 0,
        "roi": roi,
    }


def stat_normal(target, pct, side="win", league="", date_from="", date_to=""):
    """
    常规统计：目标赔率 ±pct% 区间筛选。
    side: win/draw/lose/all(=胜平负三项都落在各自区间)
    """
    if side == "all":
        # 全部方向：三赔各自独立区间同时命中
        return stat_all_odds(target, pct, league, date_from, date_to)
    lo, hi = target * (1 - pct / 100), target * (1 + pct / 100)
    hist = load_history()
    out = [r for r in hist
           if _base_filters(r, league, date_from, date_to)
           and r.get(SIDE_KEYS[side], "") != ""
           and lo <= float(r[SIDE_KEYS[side]]) <= hi]
    st = _dist_stats(out, side)
    st["range"] = (round(lo, 2), round(hi, 2))
    st["avg_odds"] = (sum(float(r[SIDE_KEYS[side]]) for r in out) / len(out)) if out else 0
    return out, st


def stat_all_odds(target, pct, league="", date_from="", date_to=""):
    """全部方向：胜/平/负任意一项落在 target±pct% 内即命中"""
    lo, hi = target * (1 - pct / 100), target * (1 + pct / 100)
    hist = load_history()
    out = []
    for r in hist:
        if not _base_filters(r, league, date_from, date_to):
            continue
        vals = [r.get(k, "") for k in ("odds_win", "odds_draw", "odds_lose")]
        nums = [float(v) for v in vals if v != ""]
        if not nums:
            continue
        if any(lo <= v <= hi for v in nums):
            out.append(r)
    st = _dist_stats(out, "win")
    st["range"] = (round(lo, 2), round(hi, 2))
    return out, st


def _implied_probs(ow, od, ol):
    """赔率 → 去水后的隐含概率（胜/平/负三维，和为1）"""
    try:
        iw, idr, il = 1 / float(ow), 1 / float(od), 1 / float(ol)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    s = iw + idr + il
    if s <= 0:
        return None
    return (iw / s, idr / s, il / s)


def _dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def stat_similar_match(ow, od, ol, top_n=20, date_from="", date_to=""):
    """
    相似统计：输入一场比赛的胜平负赔率，按「隐含概率空间欧氏距离」
    找出最相似的历史场次，按相似度降序排列。
    返回 (带similarity字段的行列表, 统计dict)
    """
    tp = _implied_probs(ow, od, ol)
    if not tp:
        return [], {"n": 0}
    hist = load_history()
    scored = []
    for r in hist:
        if not _base_filters(r, "", date_from, date_to):
            continue
        p = _implied_probs(r.get("odds_win"), r.get("odds_draw"), r.get("odds_lose"))
        if not p:
            continue
        scored.append((_dist(tp, p), r))
    scored.sort(key=lambda x: x[0])
    top = scored[:top_n]
    rows = []
    for rank, (d, r) in enumerate(top, 1):
        r2 = dict(r)
        r2["similarity"] = round(max(0.0, 1 - d) * 100, 1)   # 0-100%，1-距离 归一化
        r2["rank"] = rank
        rows.append(r2)
    st = _dist_stats(rows, "win")
    st["target_probs"] = tuple(round(x * 100, 1) for x in tp)
    return rows, st


def list_teams():
    """库里出现过的全部球队名（供输入补全）"""
    hist = load_history()
    teams = set()
    for r in hist:
        if r.get("home"):
            teams.add(r["home"])
        if r.get("away"):
            teams.add(r["away"])
    return sorted(teams)


def list_leagues():
    """库里出现过的全部联赛（供下拉选择）"""
    hist = load_history()
    leagues = [r.get("league") for r in hist if r.get("league")]
    return sorted(set(leagues))


def stat_browse(date_from="", date_to="", team="", league="", only_finished=True):
    """
    数据浏览：按日期/球队/联赛筛选库中数据。
    team 匹配主队或客队；返回 (行列表, 统计dict)
    """
    hist = load_history()
    out = []
    for r in hist:
        md = r.get("match_date") or ""
        if date_from and md < date_from:
            continue
        if date_to and md > date_to:
            continue
        if team and team not in (r.get("home") or "") and team not in (r.get("away") or ""):
            continue
        if league and league != (r.get("league") or ""):
            continue
        if only_finished and r.get("result") not in ("H", "D", "A"):
            continue
        out.append(r)
    out.sort(key=lambda r: r.get("match_date") or "", reverse=True)
    st = _dist_stats(out, "win")
    st["n_unfinished"] = sum(1 for r in hist
                             if (not date_from or (r.get("match_date") or "") >= date_from)
                             and (not date_to or (r.get("match_date") or "") <= date_to)
                             and r.get("result") not in ("H", "D", "A"))
    return out, st


def stat_team_matches(team_a, team_b, date_from="", date_to=""):
    """
    球队统计（不分主客）：
    - 两队都填：查这两队的交锋（主客互换都算）+ 各自其他比赛
    - 只填一队：查该队全部比赛
    """
    hist = load_history()
    a = (team_a or "").strip()
    b = (team_b or "").strip()
    out = []
    for r in hist:
        md = r.get("match_date") or ""
        if date_from and md < date_from:
            continue
        if date_to and md > date_to:
            continue
        h, aw = r.get("home") or "", r.get("away") or ""
        if a and b:
            if {h, aw} == {a, b}:
                r2 = dict(r)
                r2["h2h"] = "交锋"
                out.append(r2)
            elif a in (h, aw) or b in (h, aw):
                r2 = dict(r)
                r2["h2h"] = "相关"
                out.append(r2)
        elif a:  # 单队模式
            if a in (h, aw):
                r2 = dict(r)
                r2["h2h"] = "该队比赛"
                out.append(r2)
    out.sort(key=lambda r: r.get("match_date") or "", reverse=True)
    # 交锋战绩（从 team_a 视角，主客互换时翻转赛果）
    h2h = [r for r in out if r["h2h"] == "交锋"]
    def _perspective(r):
        if r.get("home") == a:
            return r["result"]
        return {"H": "A", "D": "D", "A": "H"}.get(r["result"], r["result"])
    w = sum(1 for r in h2h if _perspective(r) == "H")
    d = sum(1 for r in h2h if _perspective(r) == "D")
    l = sum(1 for r in h2h if _perspective(r) == "A")
    # 单队模式：统计该队整体战绩
    if not b:
        h2h = out
        w = sum(1 for r in out if _perspective(r) == "H")
        d = sum(1 for r in out if _perspective(r) == "D")
        l = sum(1 for r in out if _perspective(r) == "A")
    n = len(h2h)
    st = {
        "n": len(out), "n_h2h": n,
        "win": w, "draw": d, "lose": l,
        "p_win": w / n * 100 if n else 0,
        "p_draw": d / n * 100 if n else 0,
        "p_lose": l / n * 100 if n else 0,
        "roi": 0,
        "single_team": not b,
    }
    return out, st


# ══════════════════════ GUI ══════════════════════

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"竞彩足球数据分析工具 {APP_VERSION} - 数据源:体彩官方")
        self.geometry("1180x760")
        self.minsize(1000, 640)
        self.msgq = queue.Queue()
        self._auto_job = None
        self._build_ui()
        self.after(150, self._poll_msg)
        # 启动即加载本地缓存
        self.after(300, lambda: self._bg(self._task_load_local))
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI 骨架 ──
    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=24)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        self.tab_onsale = ttk.Frame(nb)
        self.tab_stat = ttk.Frame(nb)
        self.tab_data = ttk.Frame(nb)
        nb.add(self.tab_onsale, text="  在售赔率(实时)  ")
        nb.add(self.tab_stat, text="  历史相似赔率统计  ")
        nb.add(self.tab_data, text="  数据管理  ")
        self._build_onsale(self.tab_onsale)
        self._build_stat(self.tab_stat)
        self._build_data(self.tab_data)
        self.nb = nb

        # 状态栏
        self.status = tk.StringVar(value="就绪")
        sb = ttk.Label(self, textvariable=self.status, anchor="w", relief="sunken")
        sb.pack(fill="x", side="bottom")

    def _set_status(self, s):
        self.msgq.put(("status", s))

    # ── 在售页 ──
    def _build_onsale(self, tab):
        bar = ttk.Frame(tab)
        bar.pack(fill="x", padx=6, pady=6)
        ttk.Button(bar, text="🔄 立即刷新", command=self._refresh_onsale).pack(side="left")
        self.auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="每5分钟自动刷新", variable=self.auto_var,
                        command=self._toggle_auto).pack(side="left", padx=12)
        ttk.Label(bar, text="(显示当前在售场次与最新固定奖金；让球列=让球线及让球胜平负赔率)").pack(side="left", padx=8)

        cols = ("week", "league", "home", "away", "time", "ow", "od", "oa", "line", "hw", "hd", "ha", "single")
        heads = ("场次", "联赛", "主队", "客队", "开赛", "胜赔", "平赔", "负赔",
                 "让球线", "让球胜", "让球平", "让球负", "单关")
        widths = (70, 70, 130, 130, 140, 60, 60, 60, 70, 70, 70, 70, 50)
        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True, padx=6)
        self.tv_onsale = ttk.Treeview(wrap, columns=cols, show="headings", height=18)
        for c, h, w in zip(cols, heads, widths):
            self.tv_onsale.heading(c, text=h)
            self.tv_onsale.column(c, width=w, anchor="center")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tv_onsale.yview)
        self.tv_onsale.configure(yscrollcommand=vs.set)
        self.tv_onsale.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tv_onsale.tag_configure("odd", background="#f5f7fa")

        self.lbl_onsale_info = ttk.Label(tab, text="")
        self.lbl_onsale_info.pack(fill="x", padx=8, pady=4)

    # ── 统计页 ──
    def _build_stat(self, tab):
        # 模式切换
        modebar = ttk.Frame(tab)
        modebar.pack(fill="x", padx=6, pady=(6, 0))
        self.var_mode = tk.StringVar(value="normal")
        for text, val in (("常规统计", "normal"), ("相似统计", "similar"),
                          ("球队统计", "team"), ("数据浏览", "browse")):
            ttk.Radiobutton(modebar, text=text, value=val, variable=self.var_mode,
                            command=self._switch_mode).pack(side="left", padx=10)

        # ── 常规面板 ──
        self.fr_normal = ttk.LabelFrame(tab, text="常规统计：赔率区间筛选")
        self.fr_normal.pack(fill="x", padx=6, pady=6)
        ttk.Label(self.fr_normal, text="目标赔率:").grid(row=0, column=0, padx=6, pady=8, sticky="e")
        self.e_target = ttk.Entry(self.fr_normal, width=10)
        self.e_target.insert(0, "2.5")
        self.e_target.grid(row=0, column=1, padx=4)
        ttk.Label(self.fr_normal, text="浮动 ±(%):").grid(row=0, column=2, padx=(16, 0), sticky="e")
        self.e_pct = ttk.Entry(self.fr_normal, width=8)
        self.e_pct.insert(0, "8")
        self.e_pct.grid(row=0, column=3, padx=4)
        ttk.Label(self.fr_normal, text="统计方向:").grid(row=0, column=4, padx=(16, 0), sticky="e")
        self.var_side = tk.StringVar(value="胜")
        ttk.Combobox(self.fr_normal, textvariable=self.var_side, width=6, state="readonly",
                     values=["全部", "胜", "平", "负"]).grid(row=0, column=5, padx=4)
        ttk.Label(self.fr_normal, text="(全部=胜平负三项都落在区间内)").grid(row=0, column=6, padx=4)

        ttk.Label(self.fr_normal, text="联赛过滤:").grid(row=1, column=0, padx=6, pady=6, sticky="e")
        self.e_league = ttk.Entry(self.fr_normal, width=10)
        self.e_league.grid(row=1, column=1, padx=4)
        self._dates_normal = self._date_row(self.fr_normal, 1)

        # ── 相似面板 ──
        self.fr_similar = ttk.LabelFrame(tab, text="相似统计：输入一场比赛的胜平负赔率，找历史上最像的场次")
        # 不先 pack，切换时再显示
        ttk.Label(self.fr_similar, text="胜赔:").grid(row=0, column=0, padx=6, pady=8, sticky="e")
        self.e_s_ow = ttk.Entry(self.fr_similar, width=8); self.e_s_ow.insert(0, "2.5")
        self.e_s_ow.grid(row=0, column=1, padx=4)
        ttk.Label(self.fr_similar, text="平赔:").grid(row=0, column=2, padx=(12, 0), sticky="e")
        self.e_s_od = ttk.Entry(self.fr_similar, width=8); self.e_s_od.insert(0, "3.2")
        self.e_s_od.grid(row=0, column=3, padx=4)
        ttk.Label(self.fr_similar, text="负赔:").grid(row=0, column=4, padx=(12, 0), sticky="e")
        self.e_s_ol = ttk.Entry(self.fr_similar, width=8); self.e_s_ol.insert(0, "2.8")
        self.e_s_ol.grid(row=0, column=5, padx=4)
        ttk.Label(self.fr_similar, text="输出数量:").grid(row=0, column=6, padx=(12, 0), sticky="e")
        self.e_s_top = ttk.Entry(self.fr_similar, width=6); self.e_s_top.insert(0, "20")
        self.e_s_top.grid(row=0, column=7, padx=4)
        ttk.Label(self.fr_similar, text="(按相似度排名取前N场)").grid(row=0, column=8, padx=4)
        self._dates_similar = self._date_row(self.fr_similar, 1)

        # ── 球队面板 ──
        self.fr_team = ttk.LabelFrame(tab, text="球队统计：输入两队名字，查历史交锋与相关比赛")
        ttk.Label(self.fr_team, text="主队:").grid(row=0, column=0, padx=6, pady=8, sticky="e")
        self.e_team_h = ttk.Entry(self.fr_team, width=14)
        self.e_team_h.grid(row=0, column=1, padx=4)
        ttk.Label(self.fr_team, text="客队:").grid(row=0, column=2, padx=(12, 0), sticky="e")
        self.e_team_a = ttk.Entry(self.fr_team, width=14)
        self.e_team_a.grid(row=0, column=3, padx=4)
        ttk.Label(self.fr_team, text="(输入时有球队名提示)").grid(row=0, column=4, padx=4)
        self._dates_team = self._date_row(self.fr_team, 1)

        # ── 数据浏览面板 ──
        self.fr_browse = ttk.LabelFrame(tab, text="数据浏览：查看数据库中符合条件的数据")
        ttk.Label(self.fr_browse, text="球队:").grid(row=0, column=0, padx=6, pady=8, sticky="e")
        self.e_b_team = ttk.Entry(self.fr_browse, width=14)
        self.e_b_team.grid(row=0, column=1, padx=4)
        self.e_b_team.bind("<KeyRelease>", self._on_team_typing)
        ttk.Label(self.fr_browse, text="联赛:").grid(row=0, column=2, padx=(12, 0), sticky="e")
        self.var_b_league = tk.StringVar()
        self.cb_b_league = ttk.Combobox(self.fr_browse, textvariable=self.var_b_league, width=10)
        self.cb_b_league.grid(row=0, column=3, padx=4)
        self.cb_b_league.bind("<ButtonPress>", self._fill_leagues)
        ttk.Label(self.fr_browse, text="(下拉选联赛，可输入过滤)").grid(row=0, column=4, padx=4)
        self._dates_browse = self._date_row(self.fr_browse, 1, default_from="yesterday")
        self.var_b_finished = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.fr_browse, text="只看完赛场次", variable=self.var_b_finished).grid(
            row=1, column=0, columnspan=2, padx=6, pady=(0, 6), sticky="w")
        # 日期快捷键
        quick = ttk.Frame(self.fr_browse)
        quick.grid(row=1, column=2, columnspan=4, padx=6, pady=(0, 6), sticky="w")
        ttk.Label(quick, text="快捷:").pack(side="left")
        ttk.Button(quick, text="近7天", width=7,
                   command=lambda: self._quick_browse_dates(7)).pack(side="left", padx=3)
        ttk.Button(quick, text="近30天", width=8,
                   command=lambda: self._quick_browse_dates(30)).pack(side="left", padx=3)
        ttk.Button(quick, text="近一年", width=8,
                   command=lambda: self._quick_browse_dates(365)).pack(side="left", padx=3)
        ttk.Button(quick, text="全部", width=7,
                   command=lambda: self._quick_browse_dates(0)).pack(side="left", padx=3)
        # 分页控件
        pagebar = ttk.Frame(self.fr_browse)
        pagebar.grid(row=2, column=0, columnspan=8, padx=6, pady=(0, 6), sticky="w")
        ttk.Label(pagebar, text="每页:").pack(side="left")
        self.var_b_pagesize = tk.IntVar(value=50)
        for n in (20, 50, 100, 200):
            ttk.Radiobutton(pagebar, text=str(n), value=n, variable=self.var_b_pagesize).pack(side="left", padx=2)
        ttk.Button(pagebar, text="⏮ 首页", width=7, command=lambda: self._browse_page(0)).pack(side="left", padx=(16, 2))
        ttk.Button(pagebar, text="◀ 上一页", width=8, command=lambda: self._browse_page(self._browse_idx - 1)).pack(side="left", padx=2)
        ttk.Button(pagebar, text="下一页 ▶", width=8, command=lambda: self._browse_page(self._browse_idx + 1)).pack(side="left", padx=2)
        ttk.Button(pagebar, text="末页 ⏭", width=7, command=lambda: self._browse_page(10**9)).pack(side="left", padx=2)
        self.lbl_b_page = ttk.Label(pagebar, text="")
        self.lbl_b_page.pack(side="left", padx=10)
        self._browse_idx = 0        # 当前页（0起）
        self._browse_rows = []      # 当前筛选命中的全部行
        # 球队补全列表
        self.lb_teams = tk.Listbox(tab, height=5, exportselection=False)
        self.lb_teams.bind("<ButtonRelease-1>", self._pick_team)
        self._teams_cache = []
        for ent in (self.e_team_h, self.e_team_a):
            ent.bind("<KeyRelease>", self._on_team_typing)

        # 查询/导出按钮行
        btnbar = ttk.Frame(tab)
        btnbar.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(btnbar, text="🔍 查询统计", command=self._do_stat).pack(side="left")
        ttk.Button(btnbar, text="导出CSV", command=self._export_stat).pack(side="left", padx=8)
        self._btnbar_ref = btnbar

        # 统计摘要
        mid = ttk.LabelFrame(tab, text="统计结果")
        mid.pack(fill="x", padx=6, pady=(0, 6))
        self.lbl_stat = tk.Text(mid, height=6, wrap="word", font=("Microsoft YaHei", 10), bg="#fafcff", relief="flat")
        self.lbl_stat.pack(fill="x", padx=6, pady=4)

        # 明细表
        wrap = ttk.LabelFrame(tab, text="匹配场次明细")
        wrap.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        cols = ("date", "week", "league", "home", "away", "ow", "od", "oa", "line", "score", "result", "extra")
        heads = ("日期", "场次", "联赛", "主队", "客队", "胜赔", "平赔", "负赔", "让球", "比分", "赛果", "相似度/交锋")
        self.tv_stat = ttk.Treeview(wrap, columns=cols, show="headings", height=14)
        for c, h in zip(cols, heads):
            self.tv_stat.heading(c, text=h)
            self.tv_stat.column(c, width=90 if c not in ("home", "away", "extra") else (120 if c != "extra" else 100), anchor="center")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tv_stat.yview)
        self.tv_stat.configure(yscrollcommand=vs.set)
        self.tv_stat.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tv_stat.tag_configure("H", foreground="#c0392b")
        self.tv_stat.tag_configure("D", foreground="#7f8c8d")
        self.tv_stat.tag_configure("A", foreground="#2471a3")
        self._stat_rows = []

    def _date_row(self, frame, row, default_from="2015-01-01"):
        """日期区间输入行（各面板共用），返回 (起, 止) 两个 Entry"""
        if default_from == "yesterday":
            d0 = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            d0 = default_from
        ttk.Label(frame, text="日期从:").grid(row=row, column=4, padx=(16, 0), pady=6, sticky="e")
        e = ttk.Entry(frame, width=12)
        e.insert(0, d0)
        e.grid(row=row, column=5, padx=4)
        ttk.Label(frame, text="到:").grid(row=row, column=6, padx=(12, 0), sticky="e")
        e2 = ttk.Entry(frame, width=12)
        e2.insert(0, datetime.now().strftime("%Y-%m-%d"))
        e2.grid(row=row, column=7, padx=4)
        return e, e2

    def _stat_dates(self, mode):
        """取当前模式面板的日期区间"""
        pair = getattr(self, f"_dates_{mode}", None)
        if pair:
            return pair[0].get().strip(), pair[1].get().strip()
        return "", ""

    def _switch_mode(self):
        for f in (self.fr_normal, self.fr_similar, self.fr_team, self.fr_browse):
            f.pack_forget()
        {"normal": self.fr_normal, "similar": self.fr_similar,
         "team": self.fr_team, "browse": self.fr_browse}[self.var_mode.get()].pack(
            fill="x", padx=6, pady=6, before=self._mode_anchor())

    def _mode_anchor(self):
        return self._btnbar_ref

    def _on_team_typing(self, event=None):
        ent = event.widget
        txt = ent.get().strip()
        self._team_entry_active = ent        # 记住当前正在输入的框
        if not txt:
            self.lb_teams.place_forget()
            return
        if not self._teams_cache:
            self._teams_cache = list_teams()   # 首次同步加载（本地SQLite, 毫秒级）
        hits = [t for t in self._teams_cache if txt in t][:12]
        if hits:
            self.lb_teams.delete(0, "end")
            for t in hits:
                self.lb_teams.insert("end", t)
            # 定位在输入框下方
            x = ent.winfo_rootx() - self.winfo_rootx()
            y = ent.winfo_rooty() - self.winfo_rooty() + ent.winfo_height() + 2
            self.lb_teams.place(x=x, y=y, width=max(180, ent.winfo_width() * 2))
            self.lb_teams.lift()
        else:
            self.lb_teams.place_forget()

    def _load_teams(self):
        self._teams_cache = list_teams()

    def _pick_team(self, event=None):
        sel = self.lb_teams.curselection()
        if not sel:
            return
        team = self.lb_teams.get(sel[0])
        # 填到「触发补全的那个输入框」，而不是看当前焦点
        ent = getattr(self, "_team_entry_active", None) or self.e_team_h
        ent.delete(0, "end")
        ent.insert(0, team)
        self.lb_teams.place_forget()
        # 球队统计面板: 焦点挪到另一个框方便连续输入; 其他面板不动
        if ent is self.e_team_h:
            self.e_team_a.focus_set()
        elif ent is self.e_team_a:
            self.e_team_h.focus_set()

    def _fill_leagues(self, event=None):
        """联赛下拉: 点击时懒加载库里全部联赛"""
        if not self.cb_b_league.cget("values"):
            leagues = [""] + list_leagues()   # 首位空 = 不过滤
            self.cb_b_league.config(values=leagues)

    def _quick_browse_dates(self, days):
        """快捷日期: 7/30/365天, 0=全部"""
        d_to = datetime.now().strftime("%Y-%m-%d")
        d_from = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d") if days else "2015-01-01"
        self._dates_browse[0].delete(0, "end"); self._dates_browse[0].insert(0, d_from)
        self._dates_browse[1].delete(0, "end"); self._dates_browse[1].insert(0, d_to)

    def _browse_page(self, idx):
        """翻页: 渲染当前筛选结果的第 idx 页"""
        rows = self._browse_rows
        size = self.var_b_pagesize.get()
        pages = max(1, (len(rows) + size - 1) // size)
        idx = max(0, min(idx, pages - 1))
        self._browse_idx = idx
        chunk = rows[idx * size:(idx + 1) * size]
        self.tv_stat.delete(*self.tv_stat.get_children())
        for r in chunk:
            self.tv_stat.insert("", "end", tags=(r.get("result") or "",), values=(
                r["match_date"], f"{r['week']}{r['match_num']}", r["league"], r["home"], r["away"],
                r["odds_win"], r["odds_draw"], r["odds_lose"], r["goal_line"],
                r["score"], {"H": "胜", "D": "平", "A": "负"}.get(r.get("result"), r.get("result") or ""),
                r.get("h2h", "")))
        self.lbl_b_page.config(text=f"第 {idx+1}/{pages} 页　共 {len(rows)} 条　"
                                    f"本页 {len(chunk)} 条（{idx*size+1}-{idx*size+len(chunk)}）")

    # ── 数据管理页 ──
    def _build_data(self, tab):
        top = ttk.LabelFrame(tab, text="一键更新历史数据（增量合并，已存在的场次自动跳过）")
        top.pack(fill="x", padx=6, pady=6)
        ttk.Label(top, text="起始日期:").grid(row=0, column=0, padx=6, pady=10, sticky="e")
        self.e_hb = ttk.Entry(top, width=12)
        self.e_hb.insert(0, (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"))
        self.e_hb.grid(row=0, column=1, padx=4)
        ttk.Label(top, text="结束日期:").grid(row=0, column=2, padx=(12, 0), sticky="e")
        self.e_he = ttk.Entry(top, width=12)
        self.e_he.insert(0, datetime.now().strftime("%Y-%m-%d"))
        self.e_he.grid(row=0, column=3, padx=4)
        ttk.Button(top, text="⬇️ 更新历史数据", command=self._do_update_hist).grid(row=0, column=4, padx=16)
        ttk.Button(top, text="近30天", command=lambda: self._quick_range(30)).grid(row=0, column=5, padx=4)
        ttk.Button(top, text="近一年", command=lambda: self._quick_range(365)).grid(row=0, column=6, padx=4)
        ttk.Button(top, text="近两年", command=lambda: self._quick_range(730)).grid(row=0, column=7, padx=4)

        self.pbar = ttk.Progressbar(tab, maximum=100, value=0)
        self.pbar.pack(fill="x", padx=10, pady=4)
        self.lbl_progress = ttk.Label(tab, text="")
        self.lbl_progress.pack(fill="x", padx=10)

        # 让球赔率补全
        hhad_box = ttk.LabelFrame(tab, text="让球赔率补全（对历史库缺让球赔率的场次，逐场调官方赔率历史接口取收盘价）")
        hhad_box.pack(fill="x", padx=6, pady=6)
        bar2 = ttk.Frame(hhad_box)
        bar2.pack(fill="x", padx=8, pady=6)
        ttk.Button(bar2, text="⚽ 补全让球赔率", command=self._do_enrich).pack(side="left")
        ttk.Label(bar2, text="每次处理缺数据的完赛场次，带限速（约每秒3场），可反复点按直到补完").pack(side="left", padx=10)
        self.lbl_hhad = ttk.Label(hhad_box, text="")
        self.lbl_hhad.pack(fill="x", padx=8, pady=(0, 4))

        # 库信息
        info = ttk.LabelFrame(tab, text="本地数据库")
        info.pack(fill="both", expand=True, padx=6, pady=6)
        self.txt_info = tk.Text(info, height=10, wrap="word", font=("Microsoft YaHei", 10), bg="#fafcff", relief="flat")
        self.txt_info.pack(fill="both", expand=True, padx=6, pady=4)
        bar = ttk.Frame(info)
        bar.pack(fill="x", padx=6, pady=4)
        ttk.Button(bar, text="打开数据目录", command=self._open_datadir).pack(side="left")
        ttk.Button(bar, text="备份数据", command=self._backup_data).pack(side="left", padx=8)
        ttk.Button(bar, text="重置抓取记录(强制重抓)", command=self._reset_fetchlog).pack(side="left")

    def _backup_data(self):
        """把数据库文件打包成 zip 备份到用户选的位置"""
        import zipfile
        db_init()
        if not DB_FILE.exists():
            messagebox.showinfo("提示", "还没有数据可备份")
            return
        f = filedialog.asksaveasfilename(
            defaultextension=".zip",
            initialfile=f"竞彩数据备份_{datetime.now():%Y%m%d_%H%M}.zip",
            filetypes=[("ZIP", "*.zip")])
        if not f:
            return
        # 先做一份一致性快照，避免备份到写一半的 WAL
        with DB_LOCK, db() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(DB_FILE, DB_FILE.name)
        messagebox.showinfo("完成", f"已备份 {DB_FILE.name} 到:\n{f}")

    def _reset_fetchlog(self):
        log = _load_fetch_log()
        if not log.get("windows"):
            messagebox.showinfo("提示", "抓取记录本来就是空的")
            return
        if messagebox.askyesno("确认", "重置后，下次更新会重新抓取所有窗口（数据本身不会丢，只影响去重判断）。确定？"):
            reset_fetch_log()
            self._show_lib_info()
            messagebox.showinfo("完成", "抓取记录已重置")

    def _quick_range(self, days):
        self.e_hb.delete(0, "end")
        self.e_hb.insert(0, (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"))
        self.e_he.delete(0, "end")
        self.e_he.insert(0, datetime.now().strftime("%Y-%m-%d"))

    # ── 通用后台任务机制 ──
    def _bg(self, fn):
        def run():
            try:
                fn()
            except Exception as e:
                self.msgq.put(("error", str(e)))
        threading.Thread(target=run, daemon=True).start()

    def _poll_msg(self):
        try:
            while True:
                kind, payload = self.msgq.get_nowait()
                if kind == "status":
                    self.status.set(payload)
                elif kind == "error":
                    self.status.set("出错")
                    messagebox.showerror("错误", payload)
                elif kind == "onsale":
                    self._fill_onsale(payload)
                elif kind == "stat":
                    self._fill_stat(payload)
                elif kind == "hist_progress":
                    i, n, wb, we, cnt = payload
                    self.pbar["value"] = i / n * 100
                    if cnt == -1:
                        self.lbl_progress.config(text=f"进度 {i}/{n} 窗口（{wb}~{we} 已有数据，跳过）")
                    else:
                        self.lbl_progress.config(text=f"进度 {i}/{n} 窗口（{wb}~{we} 抓到 {cnt} 场）")
                elif kind == "hist_done":
                    merged, added, fetched, skipped = payload
                    self._show_lib_info()
                    self.lbl_progress.config(
                        text=f"完成：本轮抓取 {fetched} 场，新增 {added} 场，跳过 {skipped} 个已有窗口，库中共 {len(merged)} 场")
                    self.status.set("历史数据更新完成")
                    messagebox.showinfo("完成",
                                        f"更新完成！\n本轮抓取 {fetched} 场\n新增 {added} 场\n"
                                        f"跳过 {skipped} 个已保存的窗口\n本地库共 {len(merged)} 场")
                elif kind == "enrich_progress":
                    self._show_enrich_progress(*payload)
                elif kind == "enrich_done":
                    self._show_enrich_done(*payload)
                elif kind == "local_loaded":
                    self._fill_onsale(payload[0], from_cache=True)
                    self._show_lib_info(payload[1])
        except queue.Empty:
            pass
        self.after(150, self._poll_msg)

    # ── 在售逻辑 ──
    def _refresh_onsale(self):
        self._set_status("正在拉取在售数据...")
        self._bg(self._task_onsale)

    def _task_onsale(self):
        rows = fetch_onsale()
        self.msgq.put(("onsale", rows))
        self._set_status(f"在售数据已更新 {datetime.now().strftime('%H:%M:%S')}")

    def _task_load_local(self):
        rows, fetched_at = load_onsale()
        self.msgq.put(("local_loaded", (rows, load_history())))

    def _fill_onsale(self, rows, from_cache=False):
        self.tv_onsale.delete(*self.tv_onsale.get_children())
        for i, r in enumerate(rows):
            tag = ("odd",) if i % 2 else ()
            self.tv_onsale.insert("", "end", tags=tag, values=(
                f"{r['week']}{r['match_num']}", r["league"], r["home"], r["away"], r["match_time"],
                r["odds_win"], r["odds_draw"], r["odds_lose"],
                r["goal_line"], r["hhad_win"], r["hhad_draw"], r["hhad_lose"],
                "是" if str(r["single"]) in ("1", "True", "true") else ""))
        tag = "（本地缓存）" if from_cache else ""
        self.lbl_onsale_info.config(text=f"在售 {len(rows)} 场 {tag}　更新时间: "
                                         f"{rows[0]['fetched_at'] if rows else '—'}")

    def _toggle_auto(self):
        if self._auto_job:
            self.after_cancel(self._auto_job)
            self._auto_job = None
        if self.auto_var.get():
            self._auto_loop()

    def _auto_loop(self):
        if not self.auto_var.get():
            return
        self._refresh_onsale()
        self._auto_job = self.after(5 * 60 * 1000, self._auto_loop)

    # ── 统计逻辑 ──
    def _do_stat(self):
        mode = self.var_mode.get()
        df, dt = self._stat_dates(mode)
        try:
            datetime.strptime(df, "%Y-%m-%d"); datetime.strptime(dt, "%Y-%m-%d")
        except ValueError:
            messagebox.showwarning("输入有误", "日期格式应为 2015-01-01")
            return
        self._set_status("统计中...")
        if mode == "normal":
            try:
                target = float(self.e_target.get())
                pct = float(self.e_pct.get())
                if target <= 0 or pct < 0:
                    raise ValueError
            except ValueError:
                messagebox.showwarning("输入有误", "赔率必须是正数，浮动百分比必须≥0")
                return
            side = {"全部": "all", "胜": "win", "平": "draw", "负": "lose"}[self.var_side.get()]
            league = self.e_league.get().strip()
            self._bg(lambda: self._task_normal(target, pct, side, league, df, dt))
        elif mode == "similar":
            try:
                ow, od, ol = (float(self.e_s_ow.get()), float(self.e_s_od.get()), float(self.e_s_ol.get()))
                topn = int(self.e_s_top.get())
                if min(ow, od, ol) <= 1 or topn < 1:
                    raise ValueError
            except ValueError:
                messagebox.showwarning("输入有误", "三项赔率必须>1，输出数量≥1")
                return
            self._bg(lambda: self._task_similar(ow, od, ol, topn, df, dt))
        elif mode == "team":
            home = self.e_team_h.get().strip()
            away = self.e_team_a.get().strip()
            if not home and not away:
                messagebox.showwarning("输入有误", "至少填一个球队（只填主队=查该队全部比赛）")
                return
            self._bg(lambda: self._task_team(home, away, df, dt))
        elif mode == "browse":
            team = self.e_b_team.get().strip()
            league = self.var_b_league.get().strip()
            finished = self.var_b_finished.get()
            self._bg(lambda: self._task_browse(df, dt, team, league, finished))

    def _task_normal(self, target, pct, side, league, df, dt):
        rows, stats = stat_normal(target, pct, side, league, df, dt)
        self.msgq.put(("stat", (rows, stats, "normal", (target, pct, side))))
        self._set_status("统计完成")

    def _task_similar(self, ow, od, ol, topn, df, dt):
        rows, stats = stat_similar_match(ow, od, ol, topn, df, dt)
        self.msgq.put(("stat", (rows, stats, "similar", (ow, od, ol, topn))))
        self._set_status("统计完成")

    def _task_team(self, home, away, df, dt):
        rows, stats = stat_team_matches(home, away, df, dt)
        self.msgq.put(("stat", (rows, stats, "team", (home, away))))
        self._set_status("统计完成")

    def _task_browse(self, df, dt, team, league, finished):
        if finished:
            repair_results()   # 顺手补算漏赛果的场次（有比分的）
        rows, stats = stat_browse(df, dt, team, league, finished)
        self.msgq.put(("stat", (rows, stats, "browse", (team, league))))
        self._set_status("查询完成")

    def _fill_stat(self, payload):
        rows, stats, mode, args = payload
        self.lbl_stat.config(state="normal")
        self.lbl_stat.delete("1.0", "end")
        if mode == "normal":
            target, pct, side = args
            lo, hi = stats["range"]
            cn = {"win": "胜", "draw": "平", "lose": "负", "all": "全部"}[side]
            self.lbl_stat.insert("end",
                f"查询条件：{cn}赔 {target} ±{pct}%  →  区间 [{lo} ~ {hi}]　共命中 {stats['n']} 场完赛\n"
                f"赛果分布：主胜 {stats['win']} 场({stats['p_win']:.1f}%)   平局 {stats['draw']} 场({stats['p_draw']:.1f}%)   "
                f"客胜 {stats['lose']} 场({stats['p_lose']:.1f}%)\n"
                + (f"该区间平均{cn}赔率: {stats['avg_odds']:.2f}　" if side != "all" else "")
                + f"模拟回报: 每场买2元「{cn if side != 'all' else '胜'}」，总投入 {stats['n']*2} 元，ROI {stats['roi']:+.1f}%\n"
                f"提示: 赛果颜色 红=胜 灰=平 蓝=负（相对主队）")
        elif mode == "similar":
            ow, od, ol, topn = args
            tp = stats.get("target_probs", ("?","?","?"))
            self.lbl_stat.insert("end",
                f"查询条件：赔率 {ow}/{od}/{ol}（隐含概率 胜{tp[0]}% 平{tp[1]}% 负{tp[2]}%，已去水）\n"
                f"按相似度取前 {stats['n']} 场（相似度=赔率结构吻合度，100%=完全一致）\n"
                f"这批相似场次的赛果分布：主胜 {stats['win']} 场({stats['p_win']:.1f}%)   "
                f"平局 {stats['draw']} 场({stats['p_draw']:.1f}%)   客胜 {stats['lose']} 场({stats['p_lose']:.1f}%)\n"
                f"提示: 排名越靠前，赔率结构与输入越接近")
        elif mode == "browse":
            team, league = args
            cond = []
            if team: cond.append(f"球队含「{team}」")
            if league: cond.append(f"联赛={league}")
            extra = f"（另有未完赛 {stats.get('n_unfinished', 0)} 场未计入，其中取消/无效场次无赛果）" \
                    if stats.get("n_unfinished") else ""
            self.lbl_stat.insert("end",
                f"筛选条件：{'、'.join(cond) or '全部数据'}\n"
                f"命中 {stats['n']} 场{extra}\n"
                f"赛果分布：主胜 {stats['win']} 场({stats['p_win']:.1f}%)   "
                f"平局 {stats['draw']} 场({stats['p_draw']:.1f}%)   客胜 {stats['lose']} 场({stats['p_lose']:.1f}%)\n"
                f"按日期倒序分页展示，可点击「导出CSV」带走全部命中数据")
            self._browse_rows = rows
            self._browse_idx = 0
            self._browse_page(0)
        elif mode == "team":
            home, away = args
            if stats.get("single_team"):
                self.lbl_stat.insert("end",
                    f"查询条件：{home}\n"
                    f"共找到 {stats['n']} 场比赛\n"
                    f"整体战绩（{home} 视角，主客都算）：胜 {stats['win']} 场({stats['p_win']:.1f}%)   "
                    f"平 {stats['draw']} 场({stats['p_draw']:.1f}%)   负 {stats['lose']} 场({stats['p_lose']:.1f}%)\n"
                    f"按日期倒序，明细中「该队比赛」= 该队参与的场次")
            else:
                self.lbl_stat.insert("end",
                    f"查询条件：{home} / {away}（不分主客）\n"
                    f"共找到 {stats['n']} 场相关比赛，其中正面交锋 {stats['n_h2h']} 场\n"
                    f"交锋战绩（{home} 视角）：胜 {stats['win']} 场({stats['p_win']:.1f}%)   "
                    f"平 {stats['draw']} 场({stats['p_draw']:.1f}%)   负 {stats['lose']} 场({stats['p_lose']:.1f}%)\n"
                    f"明细含两队各自的其他比赛（标记「相关」），按日期倒序")
        self.lbl_stat.config(state="disabled")
        if mode != "browse":   # browse 已在分支内分页渲染
            self.tv_stat.delete(*self.tv_stat.get_children())
            for r in rows:
                extra = r.get("similarity", r.get("h2h", ""))
                extra_txt = f"{extra}%" if isinstance(extra, float) else extra
                self.tv_stat.insert("", "end", tags=(r["result"],), values=(
                    r["match_date"], f"{r['week']}{r['match_num']}", r["league"], r["home"], r["away"],
                    r["odds_win"], r["odds_draw"], r["odds_lose"], r["goal_line"],
                    r["score"], {"H": "胜", "D": "平", "A": "负"}.get(r["result"], r["result"]),
                    extra_txt))
        self._stat_rows = rows

    def _export_stat(self):
        if not self._stat_rows:
            messagebox.showinfo("提示", "先查询再导出")
            return
        f = filedialog.asksaveasfilename(defaultextension=".csv",
                                         initialfile=f"统计导出_{datetime.now():%Y%m%d_%H%M}.csv",
                                         filetypes=[("CSV", "*.csv")])
        if not f:
            return
        with open(f, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(self._stat_rows)
        messagebox.showinfo("完成", f"已导出 {len(self._stat_rows)} 场 →\n{f}")

    # ── 历史更新逻辑 ──
    def _do_update_hist(self):
        hb, he = self.e_hb.get().strip(), self.e_he.get().strip()
        try:
            datetime.strptime(hb, "%Y-%m-%d")
            datetime.strptime(he, "%Y-%m-%d")
        except ValueError:
            messagebox.showwarning("输入有误", "日期格式应为 2026-01-01")
            return
        if hb > he:
            messagebox.showwarning("输入有误", "起始日期不能晚于结束日期")
            return
        self.pbar["value"] = 0
        self._set_status("更新历史数据中...")
        self._bg(lambda: self._task_hist(hb, he))

    def _task_hist(self, hb, he):
        merged, added, fetched, skipped = do_fetch_history(hb, he,
            progress_cb=lambda i, n, wb, we, cnt: self.msgq.put(
                ("hist_progress", (i, n, wb, we, cnt))))
        self.msgq.put(("hist_done", (merged, added, fetched, skipped)))

    # ── 让球赔率补全 ──
    def _do_enrich(self):
        self.lbl_hhad.config(text="正在补全…（可在后台跑，期间别关程序）")
        self._set_status("补全让球赔率中...")
        self._bg(self._task_enrich)

    def _task_enrich(self):
        ok, skip, fail = enrich_hhad(
            progress_cb=lambda i, n, o, s, f: self.msgq.put(
                ("enrich_progress", (i, n, o, s, f))))
        self.msgq.put(("enrich_done", (ok, skip, fail)))

    def _show_enrich_progress(self, i, n, ok, skip, fail):
        self.pbar["value"] = i / n * 100 if n else 100
        self.lbl_hhad.config(text=f"进度 {i}/{n}　补上 {ok} 场　无记录 {skip} 场　失败 {fail} 场")

    def _show_enrich_done(self, ok, skip, fail):
        self._show_lib_info()
        self.lbl_hhad.config(text=f"完成：补上让球赔率 {ok} 场，官方无记录 {skip} 场，失败 {fail} 场"
                                   + ("" if fail == 0 else "（失败多为网络波动，再点一次即可续补）"))
        self.status.set("让球赔率补全完成")
        messagebox.showinfo("完成", f"让球赔率补全完成\n补上 {ok} 场\n无记录 {skip} 场\n失败 {fail} 场")

    def _show_lib_info(self, hist=None):
        hist = hist if hist is not None else load_history()
        self.txt_info.config(state="normal")
        self.txt_info.delete("1.0", "end")
        if not hist:
            self.txt_info.insert("end", "本地暂无历史数据，请先更新。")
            self.txt_info.config(state="disabled")
            return
        dates = sorted(r["match_date"] for r in hist if r["match_date"])
        from collections import Counter
        lg = Counter(r["league"] for r in hist).most_common(8)
        done = sum(1 for r in hist if r["result"] in ("H", "D", "A"))
        log = _load_fetch_log()
        cov = "、".join(f"{b}~{e}" for b, e in log.get("windows", [])[:6])
        if len(log.get("windows", [])) > 6:
            cov += " …"
        self.txt_info.insert("end",
            f"库中共 {len(hist)} 场（完赛 {done} 场）\n"
            f"日期覆盖: {dates[0]} ~ {dates[-1]}\n"
            f"已保存区间(更新时自动跳过): {cov or '无'}\n"
            f"场次最多的联赛: " + "、".join(f"{k}({v})" for k, v in lg) + "\n"
            f"文件位置: {DATA_DIR}\n"
            f"存储: 内置 SQLite 数据库 {DB_FILE.name}（单文件，拷走即全部数据）")
        self.txt_info.config(state="disabled")

    def _open_datadir(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(DATA_DIR)
        elif sys.platform == "darwin":
            os.system(f'open "{DATA_DIR}"')
        else:
            os.system(f'xdg-open "{DATA_DIR}"')

    def _on_close(self):
        if self._auto_job:
            self.after_cancel(self._auto_job)
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
