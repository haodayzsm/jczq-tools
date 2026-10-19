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
import threading
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

APP_VERSION = "v1.1"

# 数据目录：exe/script 同目录（打包后也可移植）
if getattr(sys, "frozen", False):          # PyInstaller 打包后
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
HIST_FILE = DATA_DIR / "history.json"
HIST_CSV = DATA_DIR / "history.csv"
ONSALE_FILE = DATA_DIR / "onsale.json"
ONSALE_CSV = DATA_DIR / "onsale.csv"
FETCH_LOG_FILE = DATA_DIR / "fetch_log.json"   # 抓取记录：哪些日期已落盘，防重复抓

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


def load_history():
    if HIST_FILE.exists():
        try:
            return json.loads(HIST_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def merge_history(new_rows):
    old = load_history()
    by_id = {r["match_id"]: r for r in old}
    added = 0
    for r in new_rows:
        if r["match_id"] and r["match_id"] not in by_id:
            by_id[r["match_id"]] = r
            added += 1
        elif r["match_id"] and (r["score"] or r["result"]):
            by_id[r["match_id"]] = r
    merged = sorted(by_id.values(), key=lambda r: (r["match_date"], r["match_id"]))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HIST_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(HIST_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(merged)
    return merged, added


def fetch_onsale():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data = api_get("/gateway/jc/football/getMatchCalculatorV1.qry",
                   {"poolCode": "hhad,had", "channel": "c"})
    rows = []
    for league in data.get("value", {}).get("matchInfoList", []) or []:
        for m in league.get("subMatchList", []) or []:
            rows.append(map_match(m, now, has_odds=True))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ONSALE_FILE.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(ONSALE_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
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


# ── 抓取记录（fetch_log）：已完整落盘的「过去窗口」不再重复抓 ──

def _load_fetch_log():
    if FETCH_LOG_FILE.exists():
        try:
            return json.loads(FETCH_LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"windows": []}


def _save_fetch_log(log):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 压缩合并相邻窗口，控制文件体积
    ws = sorted(set(tuple(w) for w in log.get("windows", [])))
    merged = []
    for b, e in ws:
        if merged and b <= _next_day(merged[-1][1]):
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([b, e])
    FETCH_LOG_FILE.write_text(json.dumps({"windows": merged}, ensure_ascii=False, indent=1),
                              encoding="utf-8")


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

def stat_similarity(target, pct, side="win", league="", date_from="", date_to=""):
    """
    在历史数据中找与 target 赔率相似的场次。
    target: 目标赔率(如2.5)  pct: 浮动百分比(如8 → ±8%)
    side:   win=胜赔 / draw=平赔 / lose=负赔
    返回 (匹配列表, 统计dict)
    """
    lo, hi = target * (1 - pct / 100), target * (1 + pct / 100)
    hist = load_history()
    out = []
    for r in hist:
        v = r.get({"win": "odds_win", "draw": "odds_draw", "lose": "odds_lose"}[side], "")
        if v == "" or not (lo <= float(v) <= hi):
            continue
        if league and league not in (r.get("league") or ""):
            continue
        if date_from and r.get("match_date", "") < date_from:
            continue
        if date_to and r.get("match_date", "") > date_to:
            continue
        # 只要完赛的
        if r.get("result") not in ("H", "D", "A"):
            continue
        out.append(r)
    n = len(out)
    w = sum(1 for r in out if r["result"] == "H")
    d = sum(1 for r in out if r["result"] == "D")
    l = sum(1 for r in out if r["result"] == "A")
    # 模拟回报率：每场固定 1 注 2 元买「side」方向
    def _ret(r):
        key = {"win": "odds_win", "draw": "odds_draw", "lose": "odds_lose"}[side]
        hit = {"H": "win", "D": "draw", "A": "lose"}[r["result"]] == side
        return (float(r[key]) - 1) * 2 if hit else -2
    total_stake = n * 2
    total_ret = sum(_ret(r) for r in out)
    roi = (total_ret / total_stake * 100) if total_stake else 0
    stats = {
        "range": (round(lo, 2), round(hi, 2)), "n": n,
        "win": w, "draw": d, "lose": l,
        "p_win": w / n * 100 if n else 0,
        "p_draw": d / n * 100 if n else 0,
        "p_lose": l / n * 100 if n else 0,
        "roi": roi,
        "avg_odds": (sum(float(r[{"win": "odds_win", "draw": "odds_draw", "lose": "odds_lose"}[side]]) for r in out) / n) if n else 0,
    }
    return out, stats


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
        top = ttk.LabelFrame(tab, text="查询条件")
        top.pack(fill="x", padx=6, pady=6)

        ttk.Label(top, text="目标赔率:").grid(row=0, column=0, padx=6, pady=8, sticky="e")
        self.e_target = ttk.Entry(top, width=10)
        self.e_target.insert(0, "2.5")
        self.e_target.grid(row=0, column=1, padx=4)
        ttk.Label(top, text="浮动 ±(%):").grid(row=0, column=2, padx=(16, 0), sticky="e")
        self.e_pct = ttk.Entry(top, width=8)
        self.e_pct.insert(0, "8")
        self.e_pct.grid(row=0, column=3, padx=4)

        ttk.Label(top, text="统计方向:").grid(row=0, column=4, padx=(16, 0), sticky="e")
        self.var_side = tk.StringVar(value="win")
        sidebox = ttk.Combobox(top, textvariable=self.var_side, width=6, state="readonly",
                               values=["win", "draw", "lose"])
        sidebox.grid(row=0, column=5, padx=4)
        ttk.Label(top, text="(win=按胜赔查 draw=按平赔 lose=按负赔查)").grid(row=0, column=6, padx=4)

        ttk.Label(top, text="联赛过滤:").grid(row=1, column=0, padx=6, pady=6, sticky="e")
        self.e_league = ttk.Entry(top, width=10)
        self.e_league.grid(row=1, column=1, padx=4)
        ttk.Label(top, text="日期从:").grid(row=1, column=2, padx=(16, 0), sticky="e")
        self.e_from = ttk.Entry(top, width=12)
        self.e_from.insert(0, "2015-01-01")
        self.e_from.grid(row=1, column=3, padx=4)
        ttk.Label(top, text="到:").grid(row=1, column=4, padx=(16, 0), sticky="e")
        self.e_to = ttk.Entry(top, width=12)
        self.e_to.insert(0, datetime.now().strftime("%Y-%m-%d"))
        self.e_to.grid(row=1, column=5, padx=4)

        ttk.Button(top, text="🔍 查询统计", command=self._do_stat).grid(row=0, column=7, rowspan=2, padx=20)
        ttk.Button(top, text="导出CSV", command=self._export_stat).grid(row=0, column=8, rowspan=2, padx=4)

        # 统计摘要
        mid = ttk.LabelFrame(tab, text="统计结果")
        mid.pack(fill="x", padx=6, pady=(0, 6))
        self.lbl_stat = tk.Text(mid, height=6, wrap="word", font=("Microsoft YaHei", 10), bg="#fafcff", relief="flat")
        self.lbl_stat.pack(fill="x", padx=6, pady=4)

        # 明细表
        wrap = ttk.LabelFrame(tab, text="匹配场次明细")
        wrap.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        cols = ("date", "week", "league", "home", "away", "ow", "od", "oa", "line", "score", "result")
        heads = ("日期", "场次", "联赛", "主队", "客队", "胜赔", "平赔", "负赔", "让球", "比分", "赛果")
        self.tv_stat = ttk.Treeview(wrap, columns=cols, show="headings", height=14)
        for c, h in zip(cols, heads):
            self.tv_stat.heading(c, text=h)
            self.tv_stat.column(c, width=90 if c not in ("home", "away") else 120, anchor="center")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tv_stat.yview)
        self.tv_stat.configure(yscrollcommand=vs.set)
        self.tv_stat.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tv_stat.tag_configure("H", foreground="#c0392b")
        self.tv_stat.tag_configure("D", foreground="#7f8c8d")
        self.tv_stat.tag_configure("A", foreground="#2471a3")
        self._stat_rows = []

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
        """把 data/ 打包成 zip 备份到用户选的位置"""
        import zipfile
        if not DATA_DIR.exists():
            messagebox.showinfo("提示", "还没有数据可备份")
            return
        f = filedialog.asksaveasfilename(
            defaultextension=".zip",
            initialfile=f"竞彩数据备份_{datetime.now():%Y%m%d_%H%M}.zip",
            filetypes=[("ZIP", "*.zip")])
        if not f:
            return
        with zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
            for p in DATA_DIR.iterdir():
                if p.is_file():
                    z.write(p, p.name)
        messagebox.showinfo("完成", f"已备份到:\n{f}")

    def _reset_fetchlog(self):
        if not FETCH_LOG_FILE.exists():
            messagebox.showinfo("提示", "抓取记录本来就是空的")
            return
        if messagebox.askyesno("确认", "重置后，下次更新会重新抓取所有窗口（数据本身不会丢，只影响去重判断）。确定？"):
            FETCH_LOG_FILE.unlink()
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
                    self._fill_stat(*payload)
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
        rows = []
        if ONSALE_FILE.exists():
            try:
                rows = json.loads(ONSALE_FILE.read_text(encoding="utf-8"))
            except Exception:
                rows = []
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
        try:
            target = float(self.e_target.get())
            pct = float(self.e_pct.get())
            if target <= 0 or pct < 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("输入有误", "赔率必须是正数，浮动百分比必须≥0")
            return
        side = self.var_side.get()
        league = self.e_league.get().strip()
        df, dt = self.e_from.get().strip(), self.e_to.get().strip()
        self._set_status("统计中...")
        self._bg(lambda: self._task_stat(target, pct, side, league, df, dt))

    def _task_stat(self, target, pct, side, league, df, dt):
        rows, stats = stat_similarity(target, pct, side, league, df, dt)
        self.msgq.put(("stat", (rows, stats, target, pct, side)))
        self._set_status("统计完成")

    def _fill_stat(self, rows, stats, target, pct, side):
        lo, hi = stats["range"]
        cn = {"win": "胜", "draw": "平", "lose": "负"}[side]
        self.lbl_stat.config(state="normal")
        self.lbl_stat.delete("1.0", "end")
        self.lbl_stat.insert("end",
            f"查询条件：{cn}赔 {target} ±{pct}%  →  区间 [{lo} ~ {hi}]　共命中 {stats['n']} 场完赛\n"
            f"赛果分布：主胜 {stats['win']} 场({stats['p_win']:.1f}%)   平局 {stats['draw']} 场({stats['p_draw']:.1f}%)   "
            f"客胜 {stats['lose']} 场({stats['p_lose']:.1f}%)\n"
            f"该区间平均{cn}赔率: {stats['avg_odds']:.2f}　"
            f"模拟回报: 每场买2元「{cn}」，总投入 {stats['n']*2} 元，ROI {stats['roi']:+.1f}%\n"
            f"提示: 赛果颜色 红=胜 灰=平 蓝=负（相对主队）")
        self.lbl_stat.config(state="disabled")
        self.tv_stat.delete(*self.tv_stat.get_children())
        for r in rows:
            self.tv_stat.insert("", "end", tags=(r["result"],), values=(
                r["match_date"], f"{r['week']}{r['match_num']}", r["league"], r["home"], r["away"],
                r["odds_win"], r["odds_draw"], r["odds_lose"], r["goal_line"],
                r["score"], {"H": "胜", "D": "平", "A": "负"}.get(r["result"], r["result"])))
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
            f"文件: history.json / history.csv（长期保存，随更新增量追加）")
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
