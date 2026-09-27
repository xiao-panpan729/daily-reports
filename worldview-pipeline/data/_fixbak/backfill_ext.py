# -*- coding: utf-8 -*-
"""
backfill_ext.py — 世界观项目 · 外部宏观源历史回溯 v2

给 world_feed.py 第 [5] 段的外部 key 补"持续天数/变化率"基线：
  fred:* 7个序列                    ← FRED 官方 REST API（api.stlouisfed.org，纯直连）
                                          UST10Y/20Y/30Y/2Y/10Y2Y/FEDFUNDS/HY_OAS
  fx:USDCNH / idx:HSI             ← Yahoo chart API（经 Clash 7897；Yahoo外汇历史稀疏，USDCNH 基线短属正常）
  bond:CN10Y / bond:CN30Y         ← akshare bond_zh_us_rate
  fx:USDCNY                       ← akshare fx_spot_quote（中国货币网即期，仅当日1点，历史自积累起）

写入策略：
  - 追加 data/history.csv（不动 HL/Polymarket 的已有历史）
  - snapshots.csv 只补缺失键（不覆盖，与 backfill.py 的补种逻辑一致）

用法（用 miniconda base python，akshare 已装）：
  D:\\miniconda3\\python.exe backfill_ext.py --days 30
"""
import argparse
import csv
import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")


def _load_env():
    """读取 .env（纯手写解析，不依赖 python-dotenv）。
    查找顺序：脚本目录 → 逐级向上最多 4 级，命中第一个即加载。
    注：项目根的 .env 在 BASE 的上一级，只看 BASE 会加载不到（FRED key 报未配置的根因）。
    已存在的环境变量优先；返回实际加载的 .env 路径，未找到返回 None。"""
    here = os.path.abspath(BASE)
    for _ in range(4):
        path = os.path.join(here, ".env")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if k and k not in os.environ:
                        os.environ[k] = v
            return path
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return None


_load_env()

CLASH = os.environ.get("CLASH_PROXY", "http://127.0.0.1:7897")
FRED_API = "https://api.stlouisfed.org/fred/series/observations"
FRED_KEY = os.environ.get("FRED_API_KEY", "")
FRED_SERIES = [
    ("DGS10", "fred:UST10Y"), ("DGS20", "fred:UST20Y"), ("DGS30", "fred:UST30Y"),
    ("DGS2", "fred:UST2Y"), ("T10Y2Y", "fred:UST10Y2Y"),
    ("DFEDTARU", "fred:FEDFUNDS"), ("BAMLH0A0HYM2", "fred:HY_OAS"),
    # v3（2026-09-07）：VIX现货/流动性三件套/SOFR/盈亏平衡/WTI/IG利差
    ("VIXCLS", "fred:VIX"), ("WALCL", "fred:WALCL"), ("RRPONTSYD", "fred:RRP"),
    ("WTREGEN", "fred:TGA"), ("SOFR", "fred:SOFR"), ("T10YIE", "fred:T10YIE"),
    ("DCOILWTICO", "fred:WTI"), ("BAMLC0A0CM", "fred:IG_OAS"),
]
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d"


def _get(url, proxy=None, timeout=25, as_json=False):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    op = (urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
          if proxy else urllib.request.build_opener())
    with op.open(req, timeout=timeout) as r:
        raw = r.read().decode()
    return json.loads(raw) if as_json else raw


def _get_dual(url, as_json=False, timeout=25):
    """双路取数：代理优先，失败降级直连。
    实测（2026-09-06，各 5 次 / 超时 12s）：FRED 直连 1/5 成功且中位 15.4s、代理 5/5 中位 2.2s；
    Yahoo 直连 403。旧注释「FRED 纯直连、走 Clash 越跑越慢」与实测相反，已废弃。"""
    last_err = None
    for p in (CLASH, None):
        try:
            return _get(url, proxy=p, timeout=timeout, as_json=as_json)
        except Exception as e:
            last_err = e
    raise last_err


def with_derived_dates(points):
    """points: [(date_str, value)] 按时间正序 → [(date, value, delta, streak)]"""
    rows, prev_v, streak, prev_sign = [], None, 0, 0
    for dt, v in points:
        if prev_v is None or prev_v == 0:
            delta = ""
        else:
            d = (v - prev_v) / abs(prev_v)
            delta = round(d, 6)
            sign = 1 if d > 0 else (-1 if d < 0 else 0)
            streak = (streak + sign) if sign == prev_sign else sign
            prev_sign = sign if sign else prev_sign
        rows.append((dt, round(v, 6), delta, streak))
        prev_v = v
    return rows


def fred_series(sid, days):
    """FRED REST API → 最近 N 天 [(date, value)]，代理优先/直连兜底。缺数据日('.')剔除"""
    cutoff = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d")
    url = (f"{FRED_API}?series_id={sid}&file_type=json&api_key={FRED_KEY}"
           f"&observation_start={cutoff}&sort_order=asc&limit=500")
    j = _get_dual(url, as_json=True)
    pts = []
    for o in j.get("observations", []):
        v = o.get("value")
        if v in (".", "", None):
            continue
        pts.append((o["date"], float(v)))
    return sorted(pts)


def yahoo_series(sym, days, rng="3mo"):
    """Yahoo 日K → [(date, close)]，剔除 None 收盘"""
    j = _get(YAHOO_CHART.format(sym=sym, rng=rng), proxy=CLASH, as_json=True)
    res = j["chart"]["result"][0]
    ts = res.get("timestamp") or []
    closes = res["indicators"]["quote"][0].get("close") or []
    cutoff = datetime.now(CST) - timedelta(days=days)
    pts = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        dt = datetime.fromtimestamp(t, CST)
        if dt >= cutoff:
            pts.append((dt.strftime("%Y-%m-%d"), float(c)))
    # 同日多条取最后一条
    out = {}
    for d0, v in pts:
        out[d0] = v
    return sorted(out.items())


def cnbond_series(days):
    """akshare 中美国债 → {key: [(date, value)]}，需要 akshare"""
    import akshare as ak
    df = ak.bond_zh_us_rate()
    cutoff = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d")
    cols = {"bond:CN10Y": "中国国债收益率10年", "bond:CN30Y": "中国国债收益率30年"}
    out = {k: [] for k in cols}
    for _, r in df.iterrows():
        d0 = str(r["日期"])
        if d0 < cutoff:
            continue
        for k, col in cols.items():
            v = r.get(col)
            if v is not None and str(v).lower() not in ("nan", ""):
                out[k].append((d0, float(v)))
    for k in out:
        out[k] = sorted(out[k])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    os.makedirs(DATA, exist_ok=True)
    hist_path = os.path.join(DATA, "history.csv")
    snap_path = os.path.join(DATA, "snapshots.csv")
    series = {}   # key -> [(date, value)]

    print(f"[1] FRED 美债/联邦基金/利差（REST 直连，{args.days} 天，7 序列）")
    for sid, key in FRED_SERIES:
        try:
            pts = fred_series(sid, args.days)
            series[key] = pts
            print(f"    {key:16s} {len(pts):3d} 点  {pts[0][0]} → {pts[-1][0]}  最新 {pts[-1][1]}")
        except Exception as e:
            print(f"    {key:16s} 失败 {str(e)[:60]}")

    print(f"\n[2] Yahoo USDCNH / 恒生（经 Clash 7897，{args.days} 天）")
    for sym, key in [("CNH=X", "fx:USDCNH"), ("%5EHSI", "idx:HSI"),
                     ("%5EMOVE", "idx:MOVE"), ("GC%3DF", "cm:GOLD_FUT"), ("HG%3DF", "cm:COPPER_FUT")]:
        try:
            pts = yahoo_series(sym, args.days)
            series[key] = pts
            print(f"    {key:14s} {len(pts):3d} 点  {pts[0][0]} → {pts[-1][0]}  最新 {pts[-1][1]}")
        except Exception as e:
            print(f"    {key:14s} 失败 {str(e)[:60]}")

    print(f"\n[3] 中债 10Y/30Y + USDCNY即期（akshare，{args.days} 天）")
    try:
        import akshare as ak   # cnbond_series 内部也 import，这里保证主流程可用
        for key, pts in cnbond_series(args.days).items():
            if pts:
                series[key] = pts
                print(f"    {key:14s} {len(pts):3d} 点  {pts[0][0]} → {pts[-1][0]}  最新 {pts[-1][1]}")
            else:
                print(f"    {key:14s} 近 {args.days} 天无数据")
        # USDCNY 即期（中国货币网，直连）—— 只有当日一个点，历史从现在开始自己积累
        df = ak.fx_spot_quote()
        row = df[df["货币对"] == "USD/CNY"]
        if not row.empty:
            mid = (float(row["买报价"].iloc[0]) + float(row["卖报价"].iloc[0])) / 2
            today = datetime.now(CST).strftime("%Y-%m-%d")
            series["fx:USDCNY"] = [(today, mid)]
            print(f"    fx:USDCNY      即期中间 {mid}（{today}，历史自今日起积累）")
    except ImportError:
        print("    [warn] 本 python 无 akshare → 中债/USDCNY 跳过（用 venv python 重跑可补）")
    except Exception as e:
        print(f"    [warn] akshare 段失败 {str(e)[:60]}")

    # 追加 history.csv
    def src_of(key):
        if key.startswith("fred:"):
            return "fred"
        if key.startswith("bond:"):
            return "cnbond"
        if key == "fx:USDCNY":
            return "chinamoney"
        return "yahoo"   # fx:USDCNH / idx:HSI

    total = 0
    new_file = not os.path.exists(hist_path)
    with open(hist_path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["date", "source", "key", "value", "delta", "streak"])
        for key, pts in series.items():
            for dt, v, d, st in with_derived_dates(pts):
                w.writerow([dt, src_of(key), key, v, d, st])
            total += len(pts)
    print(f"\n追加历史 {total} 行 → {hist_path}")

    # 补种 snapshots.csv（只补缺失键）
    existing = set()
    if os.path.exists(snap_path):
        with open(snap_path, encoding="utf-8") as f:
            existing = {r["key"] for r in csv.DictReader(f)}
    added = 0
    with open(snap_path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not existing:
            w.writerow(["ts", "source", "key", "value", "delta", "streak"])
        for key, pts in series.items():
            if key in existing or not pts:
                continue
            rows = with_derived_dates(pts)
            dt, v, d, st = rows[-1]
            w.writerow([dt, src_of(key), key, v, d, st])
            added += 1
    print(f"补种 {added} 个缺失键（原有 {len(existing)} 键保留）→ 次日 world_feed.py 的 delta/streak 即可计算")


if __name__ == "__main__":
    main()
