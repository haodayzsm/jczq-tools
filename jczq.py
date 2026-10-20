#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
竞彩足球 胜平负赔率抓取脚本 v1.2 (SQLite 版)
数据源：体彩官方 API（webapi.sporttery.cn），纯标准库，Python 3.8+ 直接跑，无需 pip install。

用法:
  python jczq.py current                        # 抓当前在售场次 + 最新赔率（含让球赔率）
  python jczq.py history 2026-09-18 2026-10-18  # 历史完赛+收盘赔率（增量：已抓窗口自动跳过）
  python jczq.py history 2026-10-18             # 只抓某一天
  python jczq.py all     2026-09-18 2026-10-18  # 在售+历史 一起跑
  python jczq.py export 2026-09-01 2026-10-18   # 把库导出成 CSV（Excel 可开）

数据存储: data/jczq.db (内置 SQLite 单文件数据库，与 GUI 版 jczq_app.py 共用)
  - 首次运行自动把旧版 history.json / fetch_log.json 迁移进库（原文件改名为 .migrated）
  - 已抓过的「过去窗口」自动跳过，永不重复请求
"""

import sys
import json
import time
import sqlite3
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timedelta

# ── 配置 ─────────────────────────────────────────────
BASE = "https://webapi.sporttery.cn"
DATA_DIR = Path(__file__).resolve().parent / "data"
DB_FILE = DATA_DIR / "jczq.db"
HIST_FILE = DATA_DIR / "history.json"         # 旧版 JSON（仅用于自动迁移）
FETCH_LOG_FILE = DATA_DIR / "fetch_log.json"  # 旧版抓取记录（仅用于自动迁移）
EXPORT_CSV = DATA_DIR / "export.csv"

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
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


# ── HTTP ─────────────────────────────────────────────
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
        except Exception as e:  # noqa: BLE001
            last_err = e
            if i < retries - 1:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"请求失败(重试{retries}次): {last_err}\n  url={url}")


# ── SQLite ───────────────────────────────────────────
def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def db_init():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS matches (
            match_id TEXT PRIMARY KEY, week TEXT, match_num TEXT,
            league TEXT, home TEXT, away TEXT,
            match_date TEXT, match_time TEXT,
            odds_win REAL, odds_draw REAL, odds_lose REAL, goal_line REAL,
            hhad_win REAL, hhad_draw REAL, hhad_lose REAL,
            single TEXT, score TEXT, half_score TEXT, result TEXT, fetched_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_matches_date   ON matches(match_date);
        CREATE INDEX IF NOT EXISTS idx_matches_odds   ON matches(odds_win, odds_draw, odds_lose);
        CREATE INDEX IF NOT EXISTS idx_matches_result ON matches(result);
        CREATE INDEX IF NOT EXISTS idx_matches_league ON matches(league);
        CREATE TABLE IF NOT EXISTS fetch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            win_begin TEXT NOT NULL, win_end TEXT NOT NULL, UNIQUE(win_begin, win_end)
        );
        CREATE TABLE IF NOT EXISTS onsale (
            match_id TEXT PRIMARY KEY, payload TEXT, fetched_at TEXT
        );
        """)
        # 旧版 JSON 自动迁移
        if HIST_FILE.exists():
            try:
                old = json.loads(HIST_FILE.read_text(encoding="utf-8"))
                if old:
                    upsert_matches(conn, old)
                    HIST_FILE.rename(HIST_FILE.with_suffix(".json.migrated"))
                    print(f"[迁移] history.json → 数据库: {len(old)} 条")
            except Exception as e:
                print(f"[迁移] history.json 失败(保留): {e}")
        if FETCH_LOG_FILE.exists():
            try:
                log = json.loads(FETCH_LOG_FILE.read_text(encoding="utf-8"))
                for b, e in log.get("windows", []):
                    conn.execute("INSERT OR IGNORE INTO fetch_log(win_begin, win_end) VALUES(?,?)", (b, e))
                FETCH_LOG_FILE.rename(FETCH_LOG_FILE.with_suffix(".json.migrated"))
                print("[迁移] fetch_log.json → 数据库")
            except Exception as e:
                print(f"[迁移] fetch_log.json 失败(保留): {e}")


def upsert_matches(conn, rows):
    added = 0
    upd_cols = [k for k in CSV_FIELDS if k != "match_id"]
    for r in rows:
        if not r.get("match_id"):
            continue
        vals = [r.get(k, "") for k in CSV_FIELDS]
        cur = conn.execute(
            "INSERT OR IGNORE INTO matches (" + ",".join(CSV_FIELDS) + ") VALUES(" +
            ",".join("?" * len(CSV_FIELDS)) + ")", vals)
        if cur.rowcount == 1:
            added += 1
            continue
        conn.execute(
            "UPDATE matches SET " + ",".join(f"{k}=?" for k in upd_cols) +
            " WHERE match_id=? AND (? != '' OR ? != '' OR score = '')",
            [r.get(k, "") for k in upd_cols] + [r["match_id"], r.get("score", ""), r.get("result", "")])
    return added


# ── 行映射 ───────────────────────────────────────────
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
        "odds_win": _num(h), "odds_draw": _num(d), "odds_lose": _num(a),
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


# ── fetch_log（增量去重） ─────────────────────────────
def _load_fetch_log():
    with db() as conn:
        rows = conn.execute("SELECT win_begin, win_end FROM fetch_log ORDER BY win_begin").fetchall()
    return [[r["win_begin"], r["win_end"]] for r in rows]


def _merge_windows(ws):
    """合并相邻/重叠区间"""
    ws = sorted(set((b, e) for b, e in ws))
    merged = []
    for b, e in ws:
        if merged:
            nb = (datetime.strptime(merged[-1][1], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            if b <= nb:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
                continue
        merged.append((b, e))
    return merged


def _save_fetch_log(windows):
    with db() as conn:
        conn.execute("DELETE FROM fetch_log")
        for b, e in _merge_windows(windows):
            conn.execute("INSERT INTO fetch_log(win_begin, win_end) VALUES(?,?)", (b, e))


def _window_covered(windows, wb, we):
    for b, e in windows:
        if b <= wb and we <= e:
            return True
    return False


# ── 命令：在售 ───────────────────────────────────────
def cmd_current():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("=" * 60)
    print(f"  [在售抓取] {now}")
    print("=" * 60)
    db_init()
    data = api_get("/gateway/jc/football/getMatchCalculatorV1.qry",
                   {"poolCode": "hhad,had", "channel": "c"})
    rows = []
    for league in data.get("value", {}).get("matchInfoList", []) or []:
        for m in league.get("subMatchList", []) or []:
            rows.append(map_match(m, now, has_odds=True))
    with db() as conn:
        conn.execute("DELETE FROM onsale")
        for r in rows:
            conn.execute("INSERT INTO onsale(match_id, payload, fetched_at) VALUES(?,?,?)",
                         (r["match_id"], json.dumps(r, ensure_ascii=False), r["fetched_at"]))
    print(f"  在售场次: {len(rows)} 场 → 数据库 onsale 表")
    for r in rows[:5]:
        print(f"    {r['week']}{r['match_num']} {r['league']} {r['home']} vs {r['away']} "
              f"| 胜{r['odds_win']} 平{r['odds_draw']} 负{r['odds_lose']} "
              f"| 让({r['goal_line']}) {r['hhad_win']}/{r['hhad_draw']}/{r['hhad_lose']}")
    if len(rows) > 5:
        print(f"    ... 共 {len(rows)} 场")
    print("CURRENT_OK:" + str(len(rows)))


# ── 命令：历史 ───────────────────────────────────────
def daterange(begin, end):
    b = datetime.strptime(begin, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    if b > e:
        raise SystemExit(f"起始日期 {begin} 晚于结束日期 {end}")
    cur = b
    while cur <= e:
        win_end = min(cur + timedelta(days=6), e)
        yield cur.strftime("%Y-%m-%d"), win_end.strftime("%Y-%m-%d")
        cur = win_end + timedelta(days=1)


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
        time.sleep(0.6)
    return all_matches


def cmd_history(begin, end):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("=" * 60)
    print(f"  [历史抓取] {begin} ~ {end}")
    print("=" * 60)
    db_init()
    windows_log = _load_fetch_log()
    today = datetime.now().strftime("%Y-%m-%d")
    raw_all = []
    skipped = 0
    new_windows = []
    for wb, we in daterange(begin, end):
        if we < today and _window_covered(windows_log, wb, we):
            skipped += 1
            print(f"  窗口 {wb}~{we}: 已有数据，跳过")
            continue
        raw = fetch_history_window(wb, we)
        done = sum(1 for m in raw if m.get("winFlag") in ("H", "D", "A"))
        print(f"  窗口 {wb}~{we}: {len(raw)} 场（完赛 {done}）")
        raw_all.extend(raw)
        if we < today:
            new_windows.append((wb, we))
    _save_fetch_log(windows_log + new_windows)
    rows = [map_match(m, now, has_odds=False) for m in raw_all]
    with db() as conn:
        added = upsert_matches(conn, rows)
    total = 0
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    print(f"  本轮抓到 {len(rows)} 场，新增 {added} 场，跳过 {skipped} 个已存窗口，库共 {total} 场")
    print(f"HISTORY_OK:{total}(+{added})")


# ── 命令：导出 CSV ───────────────────────────────────
def cmd_export(begin=None, end=None):
    import csv as _csv
    db_init()
    with db() as conn:
        if begin and end:
            rows = conn.execute("SELECT * FROM matches WHERE match_date BETWEEN ? AND ? "
                                "ORDER BY match_date, match_id", (begin, end)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM matches ORDER BY match_date, match_id").fetchall()
    with open(EXPORT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = _csv.writer(f)
        w.writerow(CSV_FIELDS)
        for r in rows:
            w.writerow(["" if r[k] is None else r[k] for k in CSV_FIELDS])
    print(f"  导出 {len(rows)} 场 → {EXPORT_CSV} (UTF-8 BOM, Excel直接开)")
    print(f"EXPORT_OK:{len(rows)}")


# ── 入口 ─────────────────────────────────────────────
def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "current":
        cmd_current()
    elif cmd == "history":
        if len(sys.argv) >= 4:
            cmd_history(sys.argv[2], sys.argv[3])
        elif len(sys.argv) == 3:
            cmd_history(sys.argv[2], sys.argv[2])
        else:
            raise SystemExit("用法: python jczq.py history <开始日> [结束日]")
    elif cmd == "export":
        if len(sys.argv) >= 4:
            cmd_export(sys.argv[2], sys.argv[3])
        else:
            cmd_export()
    elif cmd == "all":
        if len(sys.argv) >= 4:
            cmd_current()
            cmd_history(sys.argv[2], sys.argv[3])
        else:
            raise SystemExit("用法: python jczq.py all <开始日> <结束日>")
    else:
        print(__doc__)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
