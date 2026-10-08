# -*- coding: utf-8 -*-
"""
social_feed.py — 世界观项目 · 讨论度采集模块（v2 第一个外部数据源）

数据源：ApeWisdom（apewisdom.io，免费/免 key）
  统计 r/wallstreetbets、r/stocks、r/CryptoCurrency、4chan /biz 等社区里
  每个 ticker 被提及的次数、upvotes、以及 24h 排名变化 = "讨论度/热度"的现成数字。

设计（对齐 world_feed.py 的 derive/落库体系）：
  - 采集 all-stocks + all-crypto 两个榜单（各约 100 ticker，翻页拿全）
  - 只保留两种：① 命中 FOCUS 的 ticker ② 榜单前 N 名的异动明星（rank 或 mentions 大变化）
  - 落库两个 key：
      social:{TICKER}:mentions   → 提及次数（derive 算变化率 delta / streak）
      social:{TICKER}:rank       → 排名（越小越热；用 rank_24h_ago - rank 作为"上升热度"的原始数）
  - 数据写入 data/snapshots.csv（source='social'），与主采集共用同一张表，供面板一起读

用法：
  python social_feed.py --dry-run   只打印，不落盘
  python social_feed.py             落盘到 data/snapshots.csv

注意：ApeWisdom 是"散户注意力"信号，噪声大，只做辅助权重，不能单独当买卖依据。
"""
import argparse
import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
AW_URL = "https://apewisdom.io/api/v1.0/filter/{flt}/page/{page}"

# 从 world_feed.py 同步 FOCUS（讨论度只对"焦点标的"有意义，避免 200 ticker 全塞进快照）
FOCUS_TICKERS = {
    "NVDA", "AMD", "AVGO", "TSM", "SMH", "AMZN", "META", "MSFT", "GOOGL",
    "BABA", "COIN", "DXY", "JPY", "VIX", "GOLD", "AAPL", "TSLA",
    "BTC", "ETH", "SOL",
}
# 榜单前 N 名的"异动明星"也保留（rank 变化大 = 突然火起来，即使不在 FOCUS）
TOP_SPIKE_N = 10
SPIKE_MIN_RANK_DROP = 5   # rank 上升 ≥5 位算异动


def _load_env():
    """读取 .env（纯手写解析）。脚本目录 → 逐级向上最多 4 级，命中第一个即加载。
    注：项目根的 .env 在 BASE 的上一级，只看 BASE 会加载不到。
    返回实际加载的 .env 路径，未找到返回 None。"""
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
CLASH_PROXY = os.environ.get("CLASH_PROXY", "http://127.0.0.1:7897")


def _aw_get(flt, page=1, timeout=25):
    """拉 ApeWisdom 单页，返回 results 列表。
    连接策略：代理优先、直连兜底。
    实测（2026-09-06）：all-stocks 直连读超时、all-crypto 直连 SSL 握手失败，代理两侧均 5s 内成功。
    旧注释「直连也通」不成立 —— 原来直连在前，每次采集都要白等 25s 超时才降级。"""
    url = AW_URL.format(flt=flt, page=page)
    req = urllib.request.Request(url, headers={"User-Agent": "worldview/0.2"})
    last_err = None
    for proxy in (CLASH_PROXY, None):
        try:
            opener = (urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
                      if proxy else urllib.request.build_opener())
            with opener.open(req, timeout=timeout) as r:
                d = json.loads(r.read().decode())
                return d.get("results", []), d.get("pages", 1)
        except Exception as e:
            last_err = e
    raise last_err


def fetch_aw():
    """拉全量（stocks + crypto 榜单），返回 [(ticker, mentions, upvotes, rank, rank_24h_ago, mentions_24h_ago)]
    实测（2026-09-07）：all-stocks 663 条、all-crypto 176 条均正常（all-trending 已废弃返回空）。
    单榜失败（ApeWisdom 偶发 SSL 瞬断/某榜单下线）只跳过该榜，另一榜照常落盘。"""
    rows = []
    for flt in ("all-stocks", "all-crypto"):
        try:
            page = 1
            while True:
                results, pages = _aw_get(flt, page)
                for r in results:
                    rows.append((
                        r.get("ticker"), r.get("mentions"), r.get("upvotes"),
                        r.get("rank"), r.get("rank_24h_ago"), r.get("mentions_24h_ago"),
                    ))
                if page >= pages or page >= 3:   # 最多翻3页（300条），榜单尾部噪声大没必要全拿
                    break
                page += 1
        except Exception as e:
            print(f"[warn] ApeWisdom {flt}: {str(e)[:60]}（跳过该榜，另一榜照常）")
            continue
    return rows


def derive(key, value, prev):
    """与 world_feed.derive 一致：水平/变化率/持续天数。返回 (val, delta, streak)"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return value, "", ""
    if key not in prev:
        return round(v, 6), "", ""
    pv, pstreak = prev[key]
    try:
        pv_f = float(pv)
    except ValueError:
        return round(v, 6), "", ""
    if pv_f == 0:
        delta = 0.0
    else:
        delta = (v - pv_f) / abs(pv_f)
    sign = 1 if delta > 0 else (-1 if delta < 0 else 0)
    try:
        ps = int(pstreak)
        old_sign = 1 if ps > 0 else (-1 if ps < 0 else 0)
    except ValueError:
        ps, old_sign = 0, 0
    streak = (ps + sign) if sign == old_sign else sign
    return round(v, 6), round(delta, 6), streak


def load_prev():
    path = os.path.join(DATA, "snapshots.csv")
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[r["key"]] = (r["value"], r["streak"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    now = datetime.now(CST)
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n=== 讨论度快照 {ts} (CST) · ApeWisdom ===\n")

    prev = {} if args.dry_run else load_prev()
    rows = []

    try:
        allrows = fetch_aw()
        print(f"抓到 {len(allrows)} 个 ticker（stocks+crypto 榜单）")
    except Exception as e:
        print(f"[fail] ApeWisdom 不可达：{str(e)[:80]}")
        return

    # 分离：FOCUS 命中 + 异动明星
    focus_hits = [r for r in allrows if r[0] in FOCUS_TICKERS]
    # 异动明星：rank 上升 ≥ SPIKE_MIN_RANK_DROP（rank 数字变小=更热）
    spikes = [r for r in allrows
              if r[4] is not None and r[3] is not None and (r[4] - r[3]) >= SPIKE_MIN_RANK_DROP]
    spikes.sort(key=lambda r: -(r[4] - r[3]))   # rank 上升幅度降序
    spikes = spikes[:TOP_SPIKE_N]

    # 合并去重（按 ticker）
    seen = set()
    picked = []
    for r in focus_hits + spikes:
        if r[0] not in seen:
            seen.add(r[0])
            picked.append(r)

    print(f"焦点命中 {len(focus_hits)} 个 + 异动明星 {len(spikes)} 个 → 去重后 {len(picked)} 个\n")
    print(f"{'ticker':<8}{'mentions':>9}{'upvotes':>9}{'rank':>6}{'rank24h前':>10}{'热度↑':>7}")
    for ticker, mentions, upvotes, rank, rank24, m24 in picked:
        # rank 上升幅度 = rank24 - rank（正=变热）
        heat = (rank24 - rank) if (rank24 is not None and rank is not None) else 0
        heat_s = f"+{heat}" if heat > 0 else str(heat)
        print(f"{ticker:<8}{str(mentions):>9}{str(upvotes):>9}{str(rank):>6}{str(rank24):>10}{heat_s:>7}")

        # 落库：mentions（derive 算变化率）+ rank（原始值，面板读 rank 判断热度）
        val_m, d_m, st_m = derive(f"social:{ticker}:mentions", mentions, prev)
        rows.append(("social", f"social:{ticker}:mentions", val_m, d_m, st_m))
        val_r, d_r, st_r = derive(f"social:{ticker}:rank", rank if rank is not None else "", prev)
        rows.append(("social", f"social:{ticker}:rank", val_r, d_r, st_r))

    # 落盘
    if not args.dry_run:
        os.makedirs(DATA, exist_ok=True)
        sp = os.path.join(DATA, "snapshots.csv")
        new = not os.path.exists(sp)
        with open(sp, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["ts", "source", "key", "value", "delta", "streak"])
            for src, k, v, d, st in rows:
                w.writerow([ts, src, k, v, d, st])
        print(f"\n已落盘 {len(rows)} 条（mentions + rank 各半）→ {DATA}/snapshots.csv")
    else:
        print("\n[dry-run] 未落盘。")


if __name__ == "__main__":
    main()
