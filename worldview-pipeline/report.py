# -*- coding: utf-8 -*-
"""
report.py — 世界观项目 · 日报生成器

从 data/snapshots.csv 最新快照生成一份中文 Markdown 日报（worldview_daily.md），
跑完 world_feed + social_feed 后调用，产出"双击就能打开、可直接喂给 ima copilot / LLM 读"的成品。

内容：
  1. 今日态势一句话（规则生成：risk-on/off、哪个方向在动）
  2. 板块涨跌表（焦点 20 + 外部宏观 13）
  3. 讨论度异动（ApeWisdom rank 大变化的 ticker）
  4. Polymarket 事件概率动量
  5. 采集健康度

用法：python report.py  （读最新快照，写 data/worldview_daily.md 并打印路径）
"""
import csv
import json
import os
import re
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
OUT = os.path.join(BASE, "worldview_daily.md")

FOCUS = [
    "xyz:NVDA", "xyz:AMD", "xyz:AVGO", "xyz:TSM", "xyz:SMH",
    "xyz:AMZN", "xyz:META", "xyz:MSFT", "xyz:GOOGL",
    "xyz:BABA", "xyz:COIN", "xyz:XYZ100",
    "idx:DXY", "xyz:JPY", "xyz:JP225",
    "xyz:BRENTOIL", "xyz:NATGAS", "xyz:COPPER", "xyz:GOLD",
]
# 2026-09-15 变更（数据源修复）：
#   删 "xyz:VIX"  —— Hyperliquid 零成交死标的（markPx 恒为占位值 20.0），已被 world_feed 剔除；
#                    VIX 的正确读数改在【三、宏观链】以 fred:VIX 显示（源=Yahoo ^VIX）。
#   "xyz:DXY"→"idx:DXY" —— 同上死标的，美元指数改走 Yahoo DX-Y.NYB（真实 99.54 vs 原占位 97.15）。
EXT = ["fred:UST10Y", "fred:UST20Y", "fred:UST30Y", "fred:UST2Y", "fred:UST10Y2Y",
       "fred:FEDFUNDS", "fred:SOFR", "fred:T10YIE", "fred:HY_OAS", "fred:IG_OAS",
       "fred:VIX", "vix:9D", "vix:3M", "vix:6M", "vix:VVIX",
       "fred:WALCL", "fred:RRP", "fred:TGA", "fred:WTI",
       "fx:USDCNH", "fx:USDCNY", "idx:HSI", "idx:MOVE", "cm:GOLD_FUT", "cm:COPPER_FUT",
       "bond:CN10Y", "bond:CN30Y"]
MAIN = ["BTC", "ETH", "SOL"]

# 周频序列：自身发布节奏就是一周一次，滞后 5~7 天属正常，不该标 ⚠️
WEEKLY_KEYS = {"fred:WALCL", "fred:TGA"}
LAG_DAILY, LAG_WEEKLY = 4, 10

# 中文名映射：日报不显示英文 key，数据背后是什么必须写在脸上
CN_NAMES = {
    "fred:UST10Y": "美债10Y", "fred:UST20Y": "美债20Y", "fred:UST30Y": "美债30Y",
    "fred:UST2Y": "美债2Y", "fred:UST10Y2Y": "10Y-2Y利差(衰退计)",
    "fred:FEDFUNDS": "联邦基金上限", "fred:SOFR": "SOFR隔夜利率",
    "fred:T10YIE": "10Y盈亏平衡通胀", "fred:HY_OAS": "高收益债利差",
    "fred:IG_OAS": "投资级债利差", "fred:VIX": "VIX现货",
    "vix:9D": "VIX 9日", "vix:3M": "VIX 3月", "vix:6M": "VIX 6月",
    "vix:VVIX": "VVIX(波动率的波动率)",
    "fred:WALCL": "Fed总资产", "fred:RRP": "隔夜逆回购", "fred:TGA": "财政部TGA",
    "fred:WTI": "WTI原油",
    "idx:DXY": "美元指数",
    "fx:USDCNH": "离岸人民币", "fx:USDCNY": "在岸人民币",
    "idx:HSI": "恒生指数", "idx:MOVE": "MOVE债市波动",
    "cm:GOLD_FUT": "黄金期货", "cm:COPPER_FUT": "铜期货",
    "bond:CN10Y": "中国国债10Y", "bond:CN30Y": "中国国债30Y",
}


def load_latest():
    """读 snapshots.csv，返回 {key: (value, delta, streak)} 最新值 + 最新 ts"""
    sp = os.path.join(DATA, "snapshots.csv")
    if not os.path.exists(sp):
        return {}, ""
    out = {}
    ts = ""
    with open(sp, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ts = r["ts"]
            try:
                v = float(r["value"])
            except (TypeError, ValueError):
                v = r["value"]
            try:
                d = float(r["delta"]) if r["delta"] not in ("", None) else None
            except (TypeError, ValueError):
                d = None
            try:
                st = int(float(r["streak"])) if r["streak"] not in ("", None) else None
            except (TypeError, ValueError):
                st = None
            out[r["key"]] = (v, d, st, r["source"])
    return out, ts


def load_pm():
    pp = os.path.join(DATA, "pm_markets.csv")
    if not os.path.exists(pp):
        return []
    rows = []
    with open(pp, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


# ---- Polymarket 事件标题中译（2026-09-08）----
# 事件名是 Polymarket 英文原文，人读报告必须翻译。市场句式高度模板化，
# 用"精确词表 + 正则句式"覆盖；都未命中则保留原文（宁可英文也不机翻瞎猜）。
_PM_MONTHS = {"January": "1月", "February": "2月", "March": "3月", "April": "4月",
              "May": "5月", "June": "6月", "July": "7月", "August": "8月",
              "September": "9月", "October": "10月", "November": "11月", "December": "12月"}
_PM_EXACT = {
    "Israel closes its airspace by September 30?": "9月30日前以色列关闭领空？",
    "Presidential Election Winner 2028": "2028年美国总统大选赢家",
    "Fed rate hike by...?": "联储加息到多少？",
}
_PM_RULES = [
    (r"^Fed Decision in (\w+)\?$",
     lambda m: f"{_PM_MONTHS.get(m.group(1), m.group(1))}联储决议？"),
    (r"^Will there be no change in Fed interest rates after the (\w+) (\d{4}) meeting\?$",
     lambda m: f"{m.group(2)}年{_PM_MONTHS.get(m.group(1), m.group(1))}联储会议利率不变？"),
    (r"^How many Fed rate cuts in (\d{4})\?$",
     lambda m: f"{m.group(1)}年联储降息几次？"),
    (r"^How many Fed rate hikes in (\d{4})\?$",
     lambda m: f"{m.group(1)}年联储加息几次？"),
    (r"^Israel closes its airspace by ([\w\s,]+?)\?$",
     lambda m: f"{m.group(1).strip()}前以色列关闭领空？"),
    (r"^(Republican|Democratic) Presidential Nominee (\d{4})$",
     lambda m: f"{m.group(2)}年{'共和党' if m.group(1) == 'Republican' else '民主党'}总统候选人"),
    (r"^Largest Company end of (\w+)\?$",
     lambda m: f"{_PM_MONTHS.get(m.group(1), m.group(1))}底全球市值第一公司？"),
]


def _pm_cn(q):
    """Polymarket 英文事件名 → 中文；词表/句式都未命中则原样返回"""
    if q in _PM_EXACT:
        return _PM_EXACT[q]
    for pat, fn in _PM_RULES:
        m = re.match(pat, q)
        if m:
            try:
                return fn(m)
            except Exception:
                pass
    return q


def load_social():
    """读 social 数据，返回 [(ticker, mentions, rank, rank24, heat)] 按 heat 降序"""
    sp = os.path.join(DATA, "snapshots.csv")
    if not os.path.exists(sp):
        return []
    latest = {}
    with open(sp, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["source"] != "social":
                continue
            k = r["key"]
            latest[k] = (r["value"], r["ts"])
    # 配对 mentions + rank
    ticks = {}
    for k, (v, ts) in latest.items():
        if ":mentions" in k:
            t = k.replace("social:", "").replace(":mentions", "")
            ticks.setdefault(t, {})["mentions"] = v
        elif ":rank" in k:
            t = k.replace("social:", "").replace(":rank", "")
            ticks.setdefault(t, {})["rank"] = v
    out = []
    for t, d in ticks.items():
        try:
            rank = float(d.get("rank", 0))
        except (TypeError, ValueError):
            rank = None
        try:
            mentions = float(d.get("mentions", 0))
        except (TypeError, ValueError):
            mentions = 0
        out.append((t, mentions, rank))
    # rank 越小越热 → 按 rank 升序（None 排最后）
    out.sort(key=lambda x: (x[2] is None, x[2] if x[2] is not None else 9999))
    return out


def fmt_delta(d):
    if d is None:
        return "-"
    return f"{d:+.2%}"


def fmt_streak(st):
    if st is None:
        return "-"
    return f"{st:+d}"


def load_chains():
    """读 macro_insight.py 产出的 chains.json，None=当天没跑过。"""
    p = os.path.join(DATA, "chains.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_asof():
    """读 world_feed 采集时落的 data/asof.json → {key: 数据日(YYYY-MM-DD)}。

    这张表直接回答"这个数到底是哪天的"——2026-09-15 VIX/WTI 事故的根因就是：
    日报只标了"我们何时采集"，从不标"这个值属于哪一天"，
    于是 FRED 滞后 1~4 个交易日的旧值被当成今日读数，还配了个像模像样的涨跌幅。"""
    p = os.path.join(DATA, "asof.json")
    if not os.path.exists(p):
        return {}
    try:
        raw = json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for k, v in (raw or {}).items():
        out[k] = (v.get("date") if isinstance(v, dict) else str(v)) or ""
    return out


def fmt_asof(key, ad):
    """数据日单元格：带滞后警示（周频序列用更宽的阈值，别把正常节奏当故障）"""
    if not ad:
        return "-"
    try:
        lag = (datetime.now(CST).date() - datetime.strptime(ad[:10], "%Y-%m-%d").date()).days
    except Exception:
        return ad
    thr = LAG_WEEKLY if key in WEEKLY_KEYS else LAG_DAILY
    if lag >= thr:
        return f"{ad} ⚠️滞后{lag}天"
    return ad


def main():
    data, ts = load_latest()
    if not data:
        print("无快照数据，先跑 world_feed.py")
        return

    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    lines = []
    lines.append(f"# 世界观日报 · {ts[:16]}")
    lines.append("")
    lines.append(f"> 生成于 {now} (CST) · 数据源：Hyperliquid / Polymarket / FRED / Yahoo / akshare / ApeWisdom")
    lines.append("")

    # ---- 今日态势 ----
    up = [k for k in FOCUS + MAIN if k in data and isinstance(data[k][1], float) and data[k][1] > 0.0005]
    down = [k for k in FOCUS + MAIN if k in data and isinstance(data[k][1], float) and data[k][1] < -0.0005]
    lines.append("## 一、今日态势")
    lines.append("")
    if len(up) > len(down):
        lines.append(f"**偏多**：焦点+主站共 {len(up)} 个上涨、{len(down)} 个下跌。领涨：{', '.join(u.split(':')[-1] for u in up[:5])}。")
    elif len(down) > len(up):
        lines.append(f"**偏空**：焦点+主站共 {len(up)} 个上涨、{len(down)} 个下跌。领跌：{', '.join(d.split(':')[-1] for d in down[:5])}。")
    else:
        lines.append(f"**震荡**：焦点+主站 {len(up)} 涨 {len(down)} 跌，多空胶着。")
    lines.append("")

    # ---- 焦点 + 主站涨跌表 ----
    # 数量动态计算：死标的（xyz:VIX / xyz:DXY）被采集层剔除后，硬编码的 (20) 会撒谎
    n_show = sum(1 for k in FOCUS + MAIN if k in data)
    lines.append(f"## 二、焦点标的（{n_show}）")
    lines.append("")
    lines.append("| 标的 | 值 | 变动 | 持续 |")
    lines.append("|---|---|---|---|")
    for k in FOCUS + MAIN:
        if k not in data:
            continue
        v, d, st, _ = data[k]
        name = k.split(":")[-1]
        vv = f"{v:,.2f}" if isinstance(v, float) else str(v)
        lines.append(f"| {name} | {vv} | {fmt_delta(d)} | {fmt_streak(st)} |")
    lines.append("")

    # ---- 宏观链 ----
    aof = load_asof()
    lines.append("## 三、宏观链读数（Fed→美债→美元→CNH→恒生 + 流动性/信用/商品）")
    lines.append("")
    lines.append("| 指标 | 值 | 变动 | 数据日 |")
    lines.append("|---|---|---|---|")
    for k in EXT:
        if k not in data:
            continue
        v, d, st, _ = data[k]
        name = CN_NAMES.get(k, k)
        vv = f"{v:,.4f}" if isinstance(v, float) else str(v)
        lines.append(f"| {name} | {vv} | {fmt_delta(d)} | {fmt_asof(k, aof.get(k, ''))} |")
    lines.append("")
    lines.append("> 注：**「数据日」= 这个值属于哪一天**，与标题的快照时间不是一回事"
                 "（标题是「我们什么时候采的」）。")
    lines.append("> WALCL/TGA 为周频，滞后 5~7 天属正常节奏；带 ⚠️ 的表示已超出该源的正常更新节奏。"
                 "变动显示 +0.00% 只代表「源还没出新值」，**不代表真的没动**。")
    lines.append("> 逻辑判级见「六、宏观逻辑链」；深度推演原料在 data/llm_brief.md（可直接整段粘给任意 LLM）。")
    lines.append("")

    # ---- 讨论度异动 ----
    social = load_social()
    if social:
        lines.append("## 四、讨论度 TOP（ApeWisdom · Reddit/4chan 提及热度）")
        lines.append("")
        lines.append("| ticker | 提及次数 | 排名 |")
        lines.append("|---|---|---|")
        for t, mentions, rank in social[:12]:
            mm = str(mentions) if mentions else "-"
            rr = f"{rank:.0f}" if rank else "-"
            lines.append(f"| {t} | {mm} | {rr} |")
        lines.append("")
        lines.append("> 排名越小越热。这是散户注意力信号，噪声大，仅作辅助。")
        lines.append("")

    # ---- Polymarket ----
    pm = load_pm()
    if pm:
        # 去重按 id 取最新
        seen = {}
        for m in pm:
            seen[m["id"]] = m
        uniq = list(seen.values())
        # 只保留最近一次快照的事件：采集失败时（如 Clash 未开）旧数据不得冒充今天
        snap_ts = max((m.get("ts") or "") for m in uniq)
        uniq = [m for m in uniq if (m.get("ts") or "") == snap_ts]
        uniq.sort(key=lambda m: abs(float(m.get("chg_1d") or 0)), reverse=True)
        lines.append("## 五、Polymarket 事件概率动量（top 8）")
        lines.append("")
        if snap_ts[:10] != now[:10]:
            lines.append(f"> 数据为 **{snap_ts}** 的旧快照（本次采集未更新，别当今天读）")
        else:
            lines.append("> 快照 " + snap_ts + " · YES价=市场定价概率；"
                         "越接近 0.5 信息量越大，接近 0 或 1 说明已定价完成")
        lines.append("")
        lines.append("| 事件 | YES价 | 24h变动 | 到期 |")
        lines.append("|---|---|---|---|")
        for m in uniq[:8]:
            # 不再截断 40 字符：事件名尾部常带关键日期（如 "by September 3, 2026"）
            # 事件名翻译成中文（_pm_cn 未命中则保留原文）
            q = _pm_cn((m.get("question") or "").replace("|", "/").strip())
            yes = m.get("yes_price")
            chg = m.get("chg_1d")
            ys = f"{float(yes):.3f}" if yes not in (None, "") else "-"
            cs = f"{float(chg):+.3f}" if chg not in (None, "") else "-"
            end = (m.get("end_date") or "")[:10] or "-"
            lines.append(f"| {q} | {ys} | {cs} | {end} |")
        lines.append("")

    # ---- 宏观逻辑链（macro_insight.py 产出：数据背后的"为什么"） ----
    ch = load_chains()
    if ch:
        stale = ch.get("generated", "")[:10] != now[:10]
        lines.append("## 六、宏观逻辑链（数据→逻辑→判级）")
        lines.append("")
        if stale:
            lines.append(f"> ⚠️ 链数据为 **{ch['generated']}** 生成（今天还没跑 macro_insight.py，别当今天读）")
            lines.append("")
        net = ch.get("net_liquidity")
        if net:
            chg = f"，30日 {net['chg30_t']:+.2f} 万亿$" if net.get("chg30_t") is not None else ""
            lines.append(f"**净流动性 {net['net_t']:.2f} 万亿$**（{net['date']}）= Fed总资产 {net['walcl_t']:.2f} − 逆回购 {net['rrp_b']:.3f} − TGA {net['tga_t']:.2f}{chg}")
            lines.append("")
        esi = ch.get("esi")
        if esi:
            lines.append(f"**宏观意外计**（自建·金十今值vs预测值·90天窗口）：美国 **{esi['esi']['US']:+.2f}** / 中国 **{esi['esi']['CN']:+.2f}**"
                         f"（0=符合预期，正=强于预期；最近事件 {esi.get('last_event')}）")
            lines.append("")
        lines.append("| 因果链 | 状态 | 当前读数 |")
        lines.append("|---|---|---|")
        for c in ch.get("chains", []):
            vt = (c.get("value_text") or "").replace("|", "/")
            lines.append(f"| {c['name']} | {c['status']} | {vt} |")
        lines.append("")
        lines.append("> 每条链的逻辑与市场含义见 data/llm_brief.md；给 AI 的日更简报（带7天记忆、可整段粘给 LLM）在 data/ai_daily.md。日常操作见 08-使用说明。")
        lines.append("")
        dv = ch.get("derived") or {}
        if dv:
            lines.append("**派生信号**（快照内自算）：")
            lines.append("")
            lines.append("| 信号 | 值 | 解读 |")
            lines.append("|---|---|---|")
            for d in dv.values():
                lines.append(f"| {d['name']} | {d['value']} | {d['note']} |")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*本报告由 report.py 自动生成，数据快照时间见标题。宏观链数据由 macro_insight.py 生成。*")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    # 措辞 2026-09-09 改：原「已生成日报」会让人以为 LLM 文章也重写了。
    # daily = 数据报告（表格拼装，零 token，vvs 每次都刷）；commentary = LLM 文章（花钱，一天一次）。
    print(f"已刷新【数据报告】：{OUT}")
    print("  说明：worldview_daily.md 是表格拼装的数据报告，零 token、可随时重刷。")
    print("        LLM 文章 worldview_commentary.md 本步不触碰；要更新请跑 vv.bat 或 llm_commentary.py。")


if __name__ == "__main__":
    main()
