# -*- coding: utf-8 -*-
"""
backfill.py — 世界观项目 · 历史回溯 v1

把 world_feed.py 需要的"持续天数/变化率"基线一次性补齐，不用等一个月。

数据源（全部只读公开接口）：
  1) Hyperliquid candleSnapshot —— 日线，单次可取 >=30 根（BTC、xyz:NVDA、xyz:BRENTOIL...）
  2) Hyperliquid HIP-4 事件 token —— 同接口，但事件市场较新，历史较短（实测约 6 根）
  3) Polymarket CLOB prices-history —— 单次窗口上限 14 天，用 fidelity=1440 分段拉取

产出：
  data/history.csv   完整历史（date, source, key, value, delta, streak）
  data/snapshots.csv 用最后一条历史做种子，保证次日 world_feed.py 的 delta/streak 有基线

用法：
  python backfill.py --days 30 --pm-limit 20
  python backfill.py --days 30 --pm-limit 0     # 只回溯 Hyperliquid
"""
import argparse
import csv
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
HL = "https://api.hyperliquid.xyz/info"
PM_GAMMA = "https://gamma-api.polymarket.com/markets"
PM_CLOB = "https://clob.polymarket.com/prices-history"
CLASH = "http://127.0.0.1:7897"
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")

# 焦点层 v2（与 world_feed.py 保持一致；实际回溯按 HIP-3 全量执行）
# ⚠️ 2026-09-15：本列表里的 "xyz:DXY" / "xyz:VIX" 是 Hyperliquid **零成交死标的**
#   （openInterest=0、dayNtlVlm=0，markPx 恒为初始占位值 97.15 / 20.0）。
#   world_feed.py 已在采集层按流动性剔除它们，但本回填脚本尚未同步——
#   若重跑回填，请先确认这两项不会被写入历史（否则会把占位值当行情污染分位）。
FOCUS = [
    "xyz:NVDA", "xyz:AMD", "xyz:AVGO", "xyz:TSM", "xyz:SMH",
    "xyz:AMZN", "xyz:META", "xyz:MSFT", "xyz:GOOGL",
    "xyz:BABA", "xyz:COIN", "xyz:XYZ100",
    "xyz:DXY", "xyz:JPY", "xyz:VIX", "xyz:JP225",
    "xyz:BRENTOIL", "xyz:NATGAS", "xyz:COPPER", "xyz:GOLD",
]
MAIN = ["BTC", "ETH", "SOL"]
PM_KEYWORDS = ["FED", "RATE", "CPI", "INFLATION", "OIL", "GOLD", "NVIDIA", "NVDA",
               "AI", "BITCOIN", "BTC", "TARIFF", "CHINA", "RECESSION", "ECB", "BOJ"]
CHUNK_DAYS = 14  # Polymarket 单次窗口上限


def _open(url, payload=None, proxy=None, timeout=25):
    if payload is not None:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url, headers={"User-Agent": "worldview/0.1",
                                                   "Accept": "application/json"})
    op = (urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
          if proxy else urllib.request.build_opener())
    with op.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def hl_candles(coin, days):
    now = int(time.time() * 1000)
    start = now - days * 86400 * 1000
    d = _open(HL, {"type": "candleSnapshot",
                   "req": {"coin": coin, "interval": "1d", "startTime": start, "endTime": now}})
    return [(c["t"], float(c["c"])) for c in d if isinstance(d, list)]


def pm_history(token, days, proxy=CLASH):
    """分段拉，每段 <=14 天"""
    now = int(time.time())
    start = now - days * 86400
    out = []
    s = start
    while s < now:
        e = min(s + CHUNK_DAYS * 86400, now)
        u = f"{PM_CLOB}?market={token}&startTs={s}&endTs={e}&fidelity=1440"
        try:
            d = _open(u, proxy=proxy)
            for p in d.get("history", []):
                out.append((p["t"], float(p["p"])))
        except urllib.error.HTTPError as ex:
            print(f"      [段失败 {datetime.fromtimestamp(s, CST):%m-%d}] HTTP {ex.code}")
        s = e
        time.sleep(0.2)
    return sorted(set(out))


def with_derived(points):
    """给一串 (t, value) 计算 delta 与 streak（按时间正序）"""
    rows = []
    prev_v, streak, prev_sign = None, 0, 0
    for t, v in points:
        if prev_v is None or prev_v == 0:
            delta, streak = "", 0
        else:
            d = (v - prev_v) / abs(prev_v)
            delta = round(d, 6)
            sign = 1 if d > 0 else (-1 if d < 0 else 0)
            streak = (streak + sign) if sign == prev_sign else sign
            prev_sign = sign if sign else prev_sign
        dt = datetime.fromtimestamp(t / 1000 if t > 1e12 else t, CST).strftime("%Y-%m-%d")
        rows.append((dt, round(v, 6), delta, streak))
        prev_v = v
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--pm-limit", type=int, default=20, help="回溯多少个 Polymarket 市场（0=跳过）")
    args = ap.parse_args()

    os.makedirs(DATA, exist_ok=True)
    hist_path = os.path.join(DATA, "history.csv")
    snap_path = os.path.join(DATA, "snapshots.csv")
    total = 0

    with open(hist_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "source", "key", "value", "delta", "streak"])

        # 1) Hyperliquid 主站 + HIP-3 全量
        print(f"[1] Hyperliquid 日线回溯（{args.days} 天，HIP-3 全量）")
        try:
            xyz_keys = sorted([k for k in _open(HL, {"type": "allMids", "dex": "xyz"})
                               if k.startswith("xyz:")])
        except Exception:
            xyz_keys = list(FOCUS)   # 拿不到全量时退回焦点层
        for coin in MAIN + xyz_keys:
            try:
                pts = hl_candles(coin, args.days)
                if not pts:
                    print(f"    {coin:16s} 无数据")
                    continue
                src = "hl_xyz" if coin.startswith("xyz:") else "hl_main"
                rows = with_derived(pts)
                for dt, v, d, st in rows:
                    w.writerow([dt, src, coin, v, d, st])
                total += len(rows)
                print(f"    {coin:16s} {len(rows):3d} 根  {rows[0][0]} {rows[0][1]} → {rows[-1][0]} {rows[-1][1]}")
            except Exception as e:
                print(f"    {coin:16s} 失败 {str(e)[:60]}")

        # 2) HIP-4 事件 token
        print(f"\n[2] HIP-4 事件市场回溯")
        try:
            mids = _open(HL, {"type": "allMids"})
            ev = sorted([k for k in mids if k.startswith("#")])
            got = 0
            for k in ev:
                try:
                    pts = hl_candles(k, args.days)
                    if not pts:
                        continue
                    for dt, v, d, st in with_derived(pts):
                        w.writerow([dt, "hl_event", k, v, d, st])
                    got += 1
                    total += len(pts)
                except Exception:
                    pass
            print(f"    事件 token {len(ev)} 个，取到历史 {got} 个（事件市场较新，历史偏短属正常）")
        except Exception as e:
            print(f"    失败 {str(e)[:70]}")

        # 3) Polymarket
        if args.pm_limit > 0:
            print(f"\n[3] Polymarket 概率历史回溯（top {args.pm_limit}，分段 14 天）")
            try:
                mkts = _open(PM_GAMMA + f"?closed=false&limit=200&order=volume24hr&ascending=false",
                             proxy=CLASH)
                hits = [m for m in mkts
                        if any(kw in (m.get("question") or "").upper() for kw in PM_KEYWORDS)]
                hits.sort(key=lambda m: float(m.get("volume24hr") or 0), reverse=True)
                hits = hits[:args.pm_limit]
                for m in hits:
                    try:
                        tok = json.loads(m.get("clobTokenIds"))[0]
                    except Exception:
                        continue
                    pts = pm_history(tok, args.days)
                    if not pts:
                        print(f"    无历史: {str(m.get('question'))[:45]}")
                        continue
                    key = f"pm:{m.get('id')}"
                    rows = with_derived(pts)
                    for dt, v, d, st in rows:
                        w.writerow([dt, "pm", key, v, d, st])
                    total += len(rows)
                    print(f"    {len(rows):3d} 点  {str(m.get('question'))[:50]}")
                    with open(os.path.join(DATA, "pm_meta.csv"), "a", newline="", encoding="utf-8") as mf:
                        mw = csv.writer(mf)
                        mw.writerow([m.get("id"), m.get("question"), tok,
                                     m.get("endDate"), m.get("volume24hr")])
            except Exception as e:
                print(f"    Polymarket 不可达（Clash 未开？）: {str(e)[:70]}")

    print(f"\n历史总行数: {total} → {hist_path}")

    # 4) 用最后一条历史补种 snapshots.csv（只补缺失键，不覆盖已有键）
    last = {}
    with open(hist_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            last[r["key"]] = r
    existing = set()
    if os.path.exists(snap_path):
        with open(snap_path, encoding="utf-8") as f:
            existing = {r["key"] for r in csv.DictReader(f)}
    added = 0
    with open(snap_path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not existing:
            w.writerow(["ts", "source", "key", "value", "delta", "streak"])
        for k, r in last.items():
            if k not in existing:
                w.writerow([r["date"], r["source"], k, r["value"], r["delta"], r["streak"]])
                added += 1
    print(f"补种 {added} 个缺失键（原有 {len(existing)} 键保留）→ 次日 delta/持续天数即可计算")


if __name__ == "__main__":
    main()
