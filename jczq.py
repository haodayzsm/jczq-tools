#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
竞彩足球 胜平负赔率抓取脚本 v1.0
数据源：体彩官方 API（webapi.sporttery.cn），纯标准库，Python 3.8+ 直接跑，无需 pip install。

用法:
  python jczq.py current                        # 抓当前在售场次 + 最新赔率
  python jczq.py history 2026-09-18 2026-10-18  # 抓指定日期区间的完赛结果+收盘赔率
  python jczq.py history 2026-10-18             # 只抓某一天
  python jczq.py all     2026-09-18 2026-10-18  # 在售+历史 一起跑

数据落盘（脚本同目录 data/ 下）:
  onsale.json / onsale.csv      # 在售场次快照（每次覆盖）
  history.json / history.csv    # 历史完赛（按 match_id 去重合并，可反复增量跑）

字段说明:
  胜平负赔率 = odds_win / odds_draw / odds_lose（胜-平-负，竞彩官方收盘价）
  goal_line  = 让球线（+1 主让1球为 -1？注意官方符号：正数=客让？实为盘口线，直接参考官网显示）
  result     = H/D/A（H=主胜 D=平 A=客胜）
  score      = 全场比分 "主:客"；half_score = 半场比分
"""

import sys
import csv
import json
import time
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timedelta

# ── 配置 ─────────────────────────────────────────────
BASE = "https://webapi.sporttery.cn"
DATA_DIR = Path(__file__).resolve().parent / "data"
HIST_FILE = DATA_DIR / "history.json"
HIST_CSV = DATA_DIR / "history.csv"
ONSALE_FILE = DATA_DIR / "onsale.json"
ONSALE_CSV = DATA_DIR / "onsale.csv"

# 体彩是国内站点，直连即可；本机若有全局代理也强制绕开
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
    """GET 体彩官方接口，返回解析后的 JSON dict。失败指数退避重试。"""
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
                time.sleep(2 * (i + 1))  # 退避 2s / 4s
    raise RuntimeError(f"请求失败(重试{retries}次): {last_err}\n  url={url}")


# ── 行映射 ───────────────────────────────────────────
def _num(v):
    """转 float，失败返回空串"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return ""


def map_match(m, fetched_at, has_odds=True):
    """接口字段 → 统一行 dict（在售/历史通用）"""
    # 在售接口: had/hhad 是嵌套对象; 历史接口: h/d/a 直接平铺在 match 上
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
        "away": m.get("awayTeam") or m.get("awayTeamAbbName") or "",
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


# ── 存取 ─────────────────────────────────────────────
def save_json(path, rows):
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


def save_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # BOM: Excel 打开不乱码
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def merge_history(new_rows):
    """按 match_id 去重合并进 history.json，返回 (总数, 新增数)"""
    old = []
    if HIST_FILE.exists():
        old = json.loads(HIST_FILE.read_text(encoding="utf-8"))
    by_id = {r["match_id"]: r for r in old}
    added = 0
    for r in new_rows:
        if r["match_id"] and r["match_id"] not in by_id:
            by_id[r["match_id"]] = r
            added += 1
        elif r["match_id"] and (r["score"] or r["result"]):  # 有新赛果则更新
            by_id[r["match_id"]] = r
    merged = sorted(by_id.values(), key=lambda r: (r["match_date"], r["match_id"]))
    save_json(HIST_FILE, merged)
    save_csv(HIST_CSV, merged)
    return len(merged), added


# ── 命令：在售 ───────────────────────────────────────
def cmd_current():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("=" * 60)
    print(f"  [在售抓取] {now}")
    print("=" * 60)
    data = api_get("/gateway/jc/football/getMatchCalculatorV1.qry",
                   {"poolCode": "hhad,had", "channel": "c"})
    rows = []
    for league in data.get("value", {}).get("matchInfoList", []) or []:
        for m in league.get("subMatchList", []) or []:
            rows.append(map_match(m, now, has_odds=True))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    save_json(ONSALE_FILE, rows)
    save_csv(ONSALE_CSV, rows)
    print(f"  在售场次: {len(rows)} 场 → onsale.json / onsale.csv")
    for r in rows[:5]:
        print(f"    {r['week']}{r['match_num']} {r['league']} {r['home']} vs {r['away']} "
              f"| 胜{r['odds_win']} 平{r['odds_draw']} 负{r['odds_lose']} "
              f"| 让({r['goal_line']}) {r['hhad_win']}/{r['hhad_draw']}/{r['hhad_lose']}")
    if len(rows) > 5:
        print(f"    ... 共 {len(rows)} 场")
    print("CURRENT_OK:" + str(len(rows)))
    return rows


# ── 命令：历史 ───────────────────────────────────────
def daterange(begin, end):
    """按 7 天一窗切分日期区间（官方翻页对大区间不友好，切窗最稳）"""
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
    """抓一个日期窗口（自动翻页），返回原始 match list"""
    all_matches = []
    page = 1
    while True:
        data = api_get("/gateway/uniform/football/getUniformMatchResultV1.qry", {
            "matchPage": "1",           # 固定1=非单页模式（实测必须带）
            "matchBeginDate": begin,
            "matchEndDate": end,
            "leagueId": "",
            "pageSize": "30",
            "pageNo": str(page),
            "isFix": "0",
            "pcOrWap": "1",
        })
        v = data.get("value") or {}
        lst = v.get("matchResult") or []
        all_matches.extend(lst)
        total = int(v.get("total") or 0)
        if page * 30 >= total or not lst:
            break
        page += 1
        time.sleep(0.6)  # 温柔点，别给人家接口干趴了
    return all_matches


def cmd_history(begin, end):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("=" * 60)
    print(f"  [历史抓取] {begin} ~ {end}")
    print("=" * 60)
    raw_all = []
    for wb, we in daterange(begin, end):
        raw = fetch_history_window(wb, we)
        done = sum(1 for m in raw if m.get("winFlag") in ("H", "D", "A"))
        print(f"  窗口 {wb}~{we}: {len(raw)} 场（完赛 {done}）")
        raw_all.extend(raw)
    rows = [map_match(m, now, has_odds=False) for m in raw_all]
    total, added = merge_history(rows)
    print(f"  本轮抓到 {len(rows)} 场，新增 {added} 场，历史库共 {total} 场 → history.json / history.csv")
    print(f"HISTORY_OK:{total}(+{added})")
    return rows


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
            raise SystemExit("用法: python jczq.py history <开始日> [结束日]  如: history 2026-10-12 2026-10-18")
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
