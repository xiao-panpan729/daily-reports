# -*- coding: utf-8 -*-
"""
status.py — 世界观项目 · 采集健康度检查（不用翻 CSV，一行看所有数据源状态）

输出：每个数据源最近一次采集时间 + 距今多久 + 是否"新鲜"（今天/昨天/更早）。
判断"采集是否在正常跑"就看这个：各 source 的最新 ts 是不是刚刚。

用法：python status.py
"""
import csv
import os
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")


def main():
    sp = os.path.join(DATA, "snapshots.csv")
    if not os.path.exists(sp):
        print("尚未有 snapshots.csv，先跑一次 world_feed.py")
        return

    last = {}
    count = {}
    with open(sp, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            src = r["source"]
            last[src] = r["ts"]
            count[src] = count.get(src, 0) + 1

    # Polymarket 存独立表，读它的最新 ts（否则只有日期，会误报"需关注"）
    pp = os.path.join(DATA, "pm_markets.csv")
    if os.path.exists(pp):
        with open(pp, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                last["pm"] = r["ts"]
                count["pm"] = count.get("pm", 0) + 1

    now = datetime.now(CST)
    print("=" * 62)
    print("世界观采集健康度  (now = %s CST)" % now.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 62)

    labels = {
        "hl_main": "加密主站(BTC/ETH/SOL)", "hl_xyz": "HIP-3 隔夜温度计(118)",
        "hl_event": "HIP-4 事件市场", "pm": "Polymarket", "fred": "FRED 美债/利率",
        "yahoo": "Yahoo CNH/恒生", "cnbond": "中债 10/30Y", "chinamoney": "USDCNY 即期",
        "social": "ApeWisdom 讨论度",
    }
    for src in sorted(last, key=lambda s: last[s], reverse=True):
        ts = last[src]
        try:
            t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
            age = now - t
            mins = int(age.total_seconds() // 60)
            if mins < 60:
                age_s = f"{mins} 分钟前"
            elif mins < 1440:
                age_s = f"{mins // 60} 小时前"
            else:
                age_s = f"{mins // 1440} 天前"
        except ValueError:
            age_s = ts  # pm 只有日期
        tag = "✓ 新鲜" if age_s.endswith(("分钟前", "小时前")) else "△ 需关注"
        name = labels.get(src, src)
        print(f"  [{tag}]  {name:<22s} {ts}  ({age_s})  累计{count[src]}条")

    print("=" * 62)
    print("说明：'新鲜' = 今天刚采过；'需关注' = 上次采集超过1天。")


if __name__ == "__main__":
    main()
