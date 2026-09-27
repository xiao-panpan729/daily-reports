# -*- coding: utf-8 -*-
"""
verify.py — 世界观项目 · 日频数据自检（report 之后跑，零 token）

定位：world_feed / macro_insight 是**生产者**，本脚本是**独立质检**——
不信任生产者的自述，只拿产出物相互对照。自包含（不 import world_feed），
所以它坏不了管线的其他部分。

三项检查
  1) 内部一致性：ai_daily.md 各节出现的同一指标必须同值，且必须等于快照权威值。
     实例（2026-09-15 抓到）：VIX 在 0.65 历史分位节显示 15.84（FRED 滞后值），
     在异动榜显示 17.10（Yahoo 快照）—— 同一份文件两个值，写稿 AI 会引用错的那个。
  2) 双源比对：关键日频指标用独立第二源（新浪 / 东财）核验偏差。
  3) 数据时效：asof.json 里日频标的超过 LAG_DAYS 个自然日即列出。

产出
  data/verify_report.md    人看的自检报告（自动并入 worldview_daily.md 末尾标记块）
  data/verify_todo.md      仅在有异常时生成 → bat 用它决定「要不要叫 CLI 来核验」
  data/verify_result.json  机器可读的完整结果
  data/daily.csv           每日归档：**只装通过自检的值**，主键 (数据日, key)，幂等
  data/verify_log.jsonl    逐次自检留档（追加）→ 可回溯「哪天数据出过什么问题」

用法
  python verify.py              自检（默认）
  python verify.py --snapshot   改码前留底（拷全部脚本 + 记 sha256 / 改前异常数）
  python verify.py --checkfix   CLI 改码后校验：语法 → 导入 → 重跑自检；不过就回滚
  python verify.py --merge      只把 report（+ 修复汇报 + CLI 的 verify_cli.md）并入日报
  python verify.py --self-test  人造异常，验证检查器真的会报（回归用）

自动改码护栏（--snapshot / --checkfix）
  CLI 被授权在数据源失效时直接改 .py 换源。护栏保证它改坏了能自动退回：
  ① 语法 py_compile ② 导入 import ③ 效果 重跑本脚本（异常数不得增加）。
  任一不过 → 用改前副本覆盖回去 + 删掉新增文件 → 最坏结果退化成「没改」。
  无论成败都写 data/verify_fix.md，作为日报末尾的「本次自动修复」段。

注意：本脚本不修改采集脚本（唯一例外是被护栏**回滚**时写入改前副本），
也不改 snapshots/chains 等生产者产出。
唯一常规写入的数据文件是 daily.csv —— 且**只在全部自检通过时**才写，
所以它里面每一个值都是被核对过的；有异常的那天宁可缺失（会在 verify_log 留痕）。
"""
import csv
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

if not sys.stdout.isatty():                       # 管道/重定向时防 cp936 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8")

CST = timezone(timedelta(hours=8))
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")

AI_DAILY = os.path.join(DATA, "ai_daily.md")
SNAPS = os.path.join(DATA, "snapshots.csv")
ASOF = os.path.join(DATA, "asof.json")
DAILY_MD = os.path.join(BASE, "worldview_daily.md")

REPORT = os.path.join(DATA, "verify_report.md")
TODO = os.path.join(DATA, "verify_todo.md")
RESULT = os.path.join(DATA, "verify_result.json")
CLI_IN = os.path.join(DATA, "verify_cli.md")
DAILY_CSV = os.path.join(DATA, "daily.csv")        # 每日归档：只装自检通过的值
VLOG = os.path.join(DATA, "verify_log.jsonl")      # 逐次自检留档（追加，可回溯哪天出过事）

# 自动改码护栏（CLI 改源码换源时的留底 / 校验 / 回滚）
FIXBAK = os.path.join(DATA, "_fixbak")             # 改前源码副本
FIXSTATE = os.path.join(DATA, "_fixstate.json")    # 快照清单：文件名→sha256、改前异常数
FIXMD = os.path.join(DATA, "verify_fix.md")        # 「本次自动修复」汇报段（并入日报）
RE_URL = re.compile(r"https?://[^\s\"'）)】]+")
SKIP_IMPORT = {"verify.py"}                        # 自身：导入它拿不到额外信息
FIX_MAX_AGE_MIN = 120                              # 快照超过这么久就拒绝校验（防误回滚人工改动）

BEGIN, END = "<!-- VERIFY:BEGIN -->", "<!-- VERIFY:END -->"
LAG_DAYS = 4          # 日频超过这么多自然日才算异常（周一早上看到周五收盘 = 3 天，正常）


# ---------------------------------------------------------------- 日频白名单

# 只有日频进校验。周频（WALCL/TGA/初请/COT）与月频（M2/CPI/PMI/社融/日本10Y）排除。
DAILY_KEYS = {
    "bond:CN10Y": "中债10Y", "bond:CN30Y": "中债30Y",
    "cm:COPPER_FUT": "铜期货", "cm:GOLD_FUT": "黄金期货",
    "fred:FEDFUNDS": "联邦基金利率", "fred:HY_OAS": "高收益利差", "fred:IG_OAS": "投资级利差",
    "fred:RRP": "隔夜逆回购", "fred:SOFR": "SOFR", "fred:T10YIE": "盈亏平衡通胀",
    "fred:UST2Y": "美债2Y", "fred:UST10Y": "美债10Y", "fred:UST20Y": "美债20Y",
    "fred:UST30Y": "美债30Y", "fred:UST10Y2Y": "10Y-2Y利差",
    "fred:VIX": "VIX现货", "fred:WTI": "WTI原油",
    "fx:USDCNH": "离岸人民币", "idx:DXY": "美元指数", "idx:HSI": "恒生", "idx:MOVE": "MOVE",
    "vix:9D": "VIX 9日", "vix:3M": "VIX 3月", "vix:6M": "VIX 6月", "vix:VVIX": "VVIX",
}

# ai_daily.md 里出现的显示名 → 快照 key（同一指标的别名要能对上，否则一致性别名检查失效）
NAME2KEY = {
    "VIX": "fred:VIX", "VIX现货": "fred:VIX",
    "WTI": "fred:WTI", "WTI原油": "fred:WTI",
    "美债2Y": "fred:UST2Y", "美债10Y": "fred:UST10Y",
    "10Y-2Y利差": "fred:UST10Y2Y",
    "高收益利差": "fred:HY_OAS", "投资级利差": "fred:IG_OAS",
    "盈亏平衡通胀": "fred:T10YIE",
}


# ---------------------------------------------------------------- 第二源取数

def _http(url, enc="utf-8", referer=None, timeout=15):
    h = {"User-Agent": "Mozilla/5.0"}
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, headers=h)
    return urllib.request.build_opener().open(req, timeout=timeout).read().decode(enc, "ignore")


def _sina(code):
    """新浪 hq 行情串 → 字段列表；取不到返回 None。"""
    t = _http(f"https://hq.sinajs.cn/rn=1&list={code}", "gbk", "https://finance.sina.com.cn")
    m = re.search(r'"(.*)"', t)
    if not m or not m.group(1).strip():
        return None
    return m.group(1).split(",")


def _sina_fut(code):
    """新浪外盘期货：字段[0]=价、[6]=时间、[12]=日期。"""
    f = _sina(code)
    if not f or not f[0]:
        return None, None, None
    return (float(f[0]), f[12] if len(f) > 12 else None, f[6] if len(f) > 6 else None)


def _sina_vix():
    """新浪 VIX 恐慌指数：字段[1]=值、[6]=日期、[7]=时间。"""
    f = _sina("znb_VIX")
    if not f or len(f) < 2 or not f[1]:
        return None, None, None
    return (float(f[1]), f[6] if len(f) > 6 else None, f[7] if len(f) > 7 else None)


# 2026-09-23：东财报价端点对 secid=100.UDI 全线失效（push2delay / push2 / push2his
# 三域名连续 RemoteDisconnected，见 verify_todo 第 1 项；该端点 2026-09-15 起就是间歇性
# 拒连，现为常态失败）→ 换用 CNBC 报价端点。同为 ICE 美元指数（.DXY），
# 与主源 Yahoo DX-Y.NYB 是同一标的、不同数据商。
CNBC_DXY = ("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
            "?symbols=.DXY&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1&output=json")


def _em_udi():
    """CNBC 美元指数 .DXY：last = 现值（已是绝对值，无需缩放）；last_time = 带时区时间戳。"""
    t = _http(CNBC_DXY, "utf-8", "https://www.cnbc.com/", timeout=10)
    q = (json.loads(t).get("FormattedQuoteResult") or {}).get("FormattedQuote") or []
    d = q[0] if q else {}
    v = d.get("last")
    if v in (None, ""):
        raise RuntimeError("CNBC .DXY 无 last")
    ds = dt = None
    if d.get("last_time"):
        try:
            o = datetime.strptime(d["last_time"], "%Y-%m-%dT%H:%M:%S.%f%z").astimezone(CST)
            ds, dt = o.strftime("%Y-%m-%d"), o.strftime("%H:%M")
        except Exception:
            pass
    return float(v), ds, dt


def _src_state(tdate, ttime):
    """第二源的时态：盘中（时间戳距今 <30 分钟）/ 定格于某时刻。

    这是新鲜度的关键——「盘中价」与「收盘值」本来就不该相等，
    标出来才不会把时差误判成数据错误。"""
    if not tdate:
        return "时间未知"
    t = (ttime or "00:00")[:5]                 # "04:12:16" → "04:12"
    try:
        o = datetime.strptime(f"{tdate} {t}", "%Y-%m-%d %H:%M").replace(tzinfo=CST)
    except Exception:
        return f"定格 {tdate}"
    if (datetime.now(CST) - o).total_seconds() / 60 <= 30:
        return "盘中"
    return f"定格 {tdate} {t}" if ttime else f"定格 {tdate}"


# advisory=True → 口径不是同一个工具（永续 vs 期货），偏差只提示、不计入异常
# 每个 fn 统一返回 (值, 数据日, 数据时间)
SECOND = [
    dict(key="fred:VIX", name="VIX", src="新浪 znb_VIX", fn=_sina_vix, tol=0.02),
    dict(key="fred:WTI", name="WTI原油", src="新浪 hf_CL", fn=lambda: _sina_fut("hf_CL"), tol=0.02),
    dict(key="cm:GOLD_FUT", name="黄金期货", src="新浪 hf_GC", fn=lambda: _sina_fut("hf_GC"), tol=0.02),
    dict(key="idx:DXY", name="美元指数", src="CNBC .DXY", fn=_em_udi, tol=0.02),
    dict(key="xyz:BRENTOIL", name="布油(永续)", src="新浪 hf_OIL",
         fn=lambda: _sina_fut("hf_OIL"), tol=0.10, advisory=True),
]


# ---------------------------------------------------------------- 取数

def load_snaps():
    out = {}
    if not os.path.exists(SNAPS):
        return out
    with open(SNAPS, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                out[r["key"]] = float(r["value"])
            except (TypeError, ValueError):
                pass
    return out


RE_PV = re.compile(r'^-\s*(.+?)：现值\s*(-?[\d.]+)')                  # - VIX：现值 15.84，…
RE_ALT = re.compile(r'^-\s*(.+?)：[+-][\d.]+%（现值\s*(-?[\d.]+)')     # - VIX现货：+7.95%（现值 17.1…


def _short(s, n=16):
    """节名瘦身：砍掉括号里的补充说明。
    不只是好看——像「2. 异动榜（快照内 |变动| > 0.8%…）」里的竖线会**破坏 markdown 表格**。"""
    s = re.split(r"[（(]", s)[0].strip()
    return s if len(s) <= n else s[:n] + "…"


def parse_ai_daily(text):
    """从 ai_daily.md 抽出各节出现的「指标 → 现值」→ [(节名, 显示名, 值)]。"""
    out, sec = [], ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("## "):
            sec = _short(s[3:].strip())
            continue
        if not s.startswith("- "):
            continue
        m = RE_PV.match(s) or RE_ALT.match(s)
        if m:
            try:
                out.append((sec, m.group(1).strip(), float(m.group(2))))
            except ValueError:
                pass
    return out


# ---------------------------------------------------------------- 三项检查

def check_consistency(parsed, snaps):
    """① 节间互比 + ② 与快照权威值比。"""
    rows, issues = [], []

    # 节间：同一 key 在不同节必须同值
    bykey = defaultdict(list)
    for sec, name, v in parsed:
        k = NAME2KEY.get(name)
        if k:
            bykey[k].append((sec, name, v))
    for k, items in bykey.items():
        if len({round(v, 4) for _, _, v in items}) > 1:
            detail = " / ".join(f"{s} 节 {v}" for s, _, v in items)
            issues.append(dict(kind="consistency", key=k,
                               msg=f"{DAILY_KEYS.get(k, k)}：同一份报告里出现多个值（{detail}）"))

    # 与快照比
    for sec, name, v in parsed:
        k = NAME2KEY.get(name)
        if not k or k not in snaps:
            continue
        ref = snaps[k]
        bad = abs(v - ref) > max(abs(ref) * 0.005, 0.02)
        rows.append(dict(key=k, name=name, sec=sec, value=v, ref=ref, bad=bad))
        if bad:
            issues.append(dict(kind="consistency", key=k,
                               msg=f"{name}：{sec} 显示 {v}，快照权威值 {ref}"
                                   f"（偏差 {abs(v - ref) / max(abs(ref), 1e-9) * 100:.1f}%）"))
    return rows, issues


def judge_row(ours, theirs, tdate, ttime, ours_date, tol, advisory=False):
    """单行判定（纯函数，便于回归测试）→ (偏差, 是否异常, 第二源是否更新, 时态, 说明)。

    分层判据，顺序不能乱：
      1. 口径不同（永续 vs 期货）→ 只提示，永不判异常
      2. 值未超阈值            → 一致，哪怕跨了日
      3. 第二源是盘中价        → 时差，不判异常（收盘才作数）
      4. 源已进新一天 + 值偏离 → **我方滞后**（昨天的 WTI 事故就长这样）
      5. 其余偏离              → 同日内分歧，待查
    """
    dev = (theirs - ours) / abs(ours)
    state = _src_state(tdate, ttime)
    newer = bool(tdate and ours_date and tdate > ours_date)
    if advisory:
        return dev, False, newer, state, "仅提示：口径非同一工具"
    if abs(dev) <= tol:
        return dev, False, newer, state, ""
    if state == "盘中":
        return dev, False, newer, state, "盘中价 vs 我方收盘，时差所致（待收盘复验）"
    if newer:
        return dev, True, newer, state, f"我方停在 {ours_date}，第二源已更新至 {tdate}"
    return dev, True, newer, state, "同数据日内分歧，需查"


def check_second_source(snaps, aof):
    """③ 双源比对 + ④ 数据日新鲜度。

    两个判据合看才准：
      · 值偏离        → 说明两边不一致
      · 第二源数据日  → 说明市场是否已经走出更新的一天
    单看值：把「盘中时差」误判成错误；单看日期：跨日但值未变时白警。
    只有「源已进新一天」+「值明显偏离」才判定为我方滞后（可复现昨天的 WTI 事故）。"""
    rows, issues = [], []
    for cfg in SECOND:
        key, name = cfg["key"], cfg["name"]
        ours = snaps.get(key)
        ours_date = (aof.get(key) or {}).get("date")
        if ours is None:
            rows.append(dict(name=name, ours=None, theirs=None, dev=None, bad=False,
                             src=cfg["src"], note="快照无此 key"))
            continue
        theirs = tdate = ttime = None
        err = None
        for attempt in range(2):                  # 轻量重试，滤掉偶发抖动
            try:
                theirs, tdate, ttime = cfg["fn"]()
                if theirs is not None:
                    err = None
                    break
                err = RuntimeError("返回空")
            except Exception as e:
                err = e
            if attempt == 0:
                time.sleep(0.8)
        if theirs is None:
            rows.append(dict(name=name, ours=ours, theirs=None, dev=None, bad=False,
                             src=cfg["src"], ours_date=ours_date,
                             note=f"第二源不可用（{type(err).__name__}）"))
            issues.append(dict(kind="source_down", key=key,
                               msg=f"{name} 的第二源 {cfg['src']} 取数失败"
                                   f"（{type(err).__name__}）—— 需查该源是否已失效，并给替代端点"))
            continue

        dev, bad, newer, state, note = judge_row(
            ours, theirs, tdate, ttime, ours_date, cfg["tol"], cfg.get("advisory"))
        rows.append(dict(name=name, ours=ours, theirs=theirs, dev=dev, bad=bad, src=cfg["src"],
                         ours_date=ours_date, theirs_date=tdate, theirs_time=ttime,
                         state=state, newer=newer, note=note))
        if bad:
            issues.append(dict(kind="stale" if newer else "second_source", key=key,
                               msg=f"{name}：我方 {ours}（数据日 {ours_date}）"
                                   f"{'疑似过期' if newer else '偏离'}，{cfg['src']} "
                                   f"{tdate or ''} {ttime or ''} = {theirs}"
                                   f"（偏差 {dev * 100:+.2f}%）"))
    return rows, issues


def check_lag():
    rows, issues = [], []
    if not os.path.exists(ASOF):
        return rows, issues
    aof = json.load(open(ASOF, encoding="utf-8"))
    today = datetime.now(CST).date()
    for key, info in sorted(aof.items()):
        if key not in DAILY_KEYS:
            continue
        try:
            d = datetime.strptime(info["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        lag = (today - d).days
        bad = lag > LAG_DAYS
        rows.append(dict(key=key, name=DAILY_KEYS[key], date=info["date"],
                         src=info.get("source", ""), lag=lag, bad=bad))
        if bad:
            issues.append(dict(kind="lag", key=key,
                               msg=f"{DAILY_KEYS[key]}：数据日 {info['date']}，距今 {lag} 天"))
    return rows, issues


# ---------------------------------------------------------------- 报告

def build_report(c_rows, c_iss, s_rows, s_iss, l_rows, l_iss, now):
    n = len(c_iss) + len(s_iss) + len(l_iss)
    L = [f"## 附：数据自检 · {now}",
         "",
         "> 由 `verify.py` 本地生成（零 token）。四项：内部一致性 / 双源比对 / **数据日** / 数据时效。",
         f"> **结论：{'发现 %d 项需关注' % n if n else '全部通过'}**",
         ""]

    L += ["### 1. 内部一致性（同一指标在报告各节必须同值）", ""]
    if c_rows:
        L += ["| 指标 | 出现位置 | 报告值 | 快照（权威） | 判定 |", "|---|---|---|---|---|"]
        for r in c_rows:
            L.append(f"| {r['name']} | {r['sec']} | {r['value']} | {r['ref']} | "
                     f"{'⚠️ 不一致' if r['bad'] else '一致'} |")
    else:
        L.append("（无可比对项）")
    L.append("")

    L += ["### 2. 双源比对（我方 vs 独立第二源）", "",
          "| 指标 | 我方 | 第二源 | 偏差 | 第二源时态 | 判定 |", "|---|---|---|---|---|---|"]
    for r in s_rows:
        if r.get("theirs") is None:
            L.append(f"| {r['name']} | {r['ours']} | — | — | — | {r.get('note', '—')} |")
            continue
        st = r.get("state") or "—"
        if r.get("bad"):
            tag = "⚠️ 我方滞后" if r.get("newer") else "⚠️ 可疑"
        elif r.get("note"):
            tag = "仅供参照"
        else:
            tag = "一致" if abs(r["dev"]) <= 0.01 else "接近（未超阈值）"
        L.append(f"| {r['name']} | {r['ours']} | {r['theirs']}（{r['src']}） | "
                 f"{r['dev'] * 100:+.2f}% | {st} | {tag} |")
    L += ["",
          "> 第二源是公开行情接口，**正在交易时取到的是盘中价**；与我方收盘值有时差属正常，",
          "> 标「盘中」的行不参与偏差定级。只有「源已进新一天 + 值明显偏离」才判我方滞后。",
          ""]

    L += ["### 3. 数据日（我方 asof ←→ 第二源时间戳 ←→ 今天）", "",
          "| 指标 | 我方数据日 | 第二源数据时间 | 状态 |", "|---|---|---|---|"]
    for r in s_rows:
        if r.get("theirs") is None:
            continue
        td = f"{r.get('theirs_date') or '—'} {r.get('theirs_time') or ''}".strip()
        if r.get("bad") and r.get("newer"):
            stt = "⚠️ **我方落后**"
        elif r.get("bad"):
            stt = "⚠️ 值分歧"
        elif r.get("state") == "盘中":
            stt = "容忍内（源仍在交易）"
        else:
            stt = "容忍内"
        L.append(f"| {r['name']} | {r.get('ours_date') or '—'} | {td} | {stt} |")
    L.append("")

    bad_l = [r for r in l_rows if r["bad"]]
    if bad_l:
        L += ["其余日频指标中，以下数据日超出容忍：", "",
              "| 指标 | 数据日 | 距今 | 源 |", "|---|---|---|---|"]
        for r in bad_l:
            L.append(f"| ⚠️ {r['name']} | {r['date']} | {r['lag']} 天 | {r['src']} |")
    else:
        L.append(f"（其余 {len(l_rows)} 项日频指标的数据日均在 {LAG_DAYS} 天内）")
    L += ["",
          "### 未覆盖说明",
          "",
          "- Polymarket / ApeWisdom / HIP-3 永续无官方对照源，不做双源比对",
          "- 周频（WALCL / TGA / 初请 / COT）、月频（M2 / CPI / PMI / 社融）不在日频校验范围",
          ""]
    return "\n".join(L)


def build_todo(issues, now):
    kind_cn = {"consistency": "内部不一致", "second_source": "双源偏差",
               "stale": "数据日滞后", "lag": "数据时效", "source_down": "第二源失效"}
    L = [f"# 待核验清单 · {now}", "",
         "本地自检判定以下项异常。请上网核验，**优先用最近交易日的正式收盘报道**：",
         "华尔街见闻《美股收盘》/ 财联社收盘稿 / 东财收盘播报 / 官方行情页。", "",
         "## 口径（务必遵守）",
         "",
         "1. **收盘报道 ≠ 快讯**。快讯多是盘中价、约数、转述，只能当线索；",
         "   收盘报道（如「美股收盘：… VIX 收于 17.10」）是事后确认值，可用于核对数据日。",
         "2. 报道的作用是**验证「最新交易日是哪天」**，不是一个数字基准；",
         "   最终数值基准仍须落在**行情接口或官方端点**上。",
         "3. 每项都要给出判定：我方错 / 对方错 / 口径不同（工具、现期货、时区）。",
         "4. 若我方取数源失效或滞后，**给出替代端点 URL + 要读的字段名**。",
         "",
         "## 待核验项",
         ""]
    for i, it in enumerate(issues, 1):
        L.append(f"{i}. [{kind_cn.get(it['kind'], it['kind'])}] {it['msg']}")
    L += ["", "---", "",
          "结论写入 `data/verify_cli.md`，每项一行：",
          "",
          "`- [项号] 判定｜最新交易日（依据的报道标题+日期）｜替代源 URL 与字段｜一句话依据`",
          "",
          "末尾一行：`**总结**：…`",
          ""]
    return "\n".join(L)


def merge_into_daily(body):
    """把自检块写进 worldview_daily.md（幂等：先删旧块再写）。"""
    if not os.path.exists(DAILY_MD) or not body.strip():
        return False
    text = open(DAILY_MD, encoding="utf-8").read()
    if BEGIN in text and END in text:
        text = text[:text.index(BEGIN)] + text[text.index(END) + len(END):]
    text = text.rstrip("\n") + "\n\n" + BEGIN + "\n" + body.strip() + "\n" + END + "\n"
    with open(DAILY_MD, "w", encoding="utf-8") as f:
        f.write(text)
    return True


def read_or(path, default=""):
    try:
        return open(path, encoding="utf-8").read()
    except Exception:
        return default


# ---------------------------------------------------------------- 每日归档

ARCHIVE_COLS = ["date", "key", "value", "delta", "streak", "source", "collected_at"]


def archive_daily(now):
    """把本次的日频值归档进 data/daily.csv —— **只装通过自检的值**。

    主键 (数据日, key)：读-改-写，同一数据日重跑覆盖为最新，不留冗余行，
    所以文件天然幂等、按日期有序，可以直接当时间序列用（趋势 / 分位 / 回看）。

    date 取 asof.json 的**数据日**，不是采集日 —— FRED 滞后时值挂在自己的
    观测日上，不会伪装成今天的读数；等真实观测日到了再覆盖，自动收敛。

    返回归档条数。任何异常都不抛，只告警（归档失败不该让自检整体失败）。
    """
    if not os.path.exists(SNAPS):
        print("[archive] 跳过：无快照")
        return 0
    try:
        aof = json.load(open(ASOF, encoding="utf-8")) if os.path.exists(ASOF) else {}
    except Exception:
        aof = {}

    latest = {}
    with open(SNAPS, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            latest[r["key"]] = r                 # 后行覆盖前行 = 本次采集的最新值

    table = {}
    if os.path.exists(DAILY_CSV):
        with open(DAILY_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                table[(r["date"], r["key"])] = r

    n = 0
    for key in DAILY_KEYS:
        row = latest.get(key)
        if not row:
            continue
        date = ((aof.get(key) or {}).get("date") or "").strip()
        if not date:
            continue                             # 没有数据日的值不入档（宁缺勿错）
        table[(date, key)] = dict(
            date=date, key=key, value=row.get("value", ""),
            delta=row.get("delta") or "", streak=row.get("streak") or "",
            source=row.get("source") or (aof.get(key) or {}).get("source", ""),
            collected_at=now)
        n += 1

    try:
        with open(DAILY_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=ARCHIVE_COLS, extrasaction="ignore")
            w.writeheader()
            w.writerows(table[k] for k in sorted(table))
    except Exception as e:
        print(f"[archive] 写入失败：{type(e).__name__}: {e}")
        return 0
    return n


def append_log(now, issues, archived):
    """逐次自检留档：以后能回答「哪天数据出过什么问题」。"""
    rec = dict(ts=now, ok=not issues, n_issues=len(issues),
               kinds=sorted({i["kind"] for i in issues}),
               archived=archived,
               msgs=[i["msg"] for i in issues][:10])
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(VLOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[warn] verify_log 写入失败：{type(e).__name__}: {e}")


# ---------------------------------------------------------------- self-test

def self_test():
    """人造异常，验证检查器真的会报（不是"看起来在检查"）。"""
    ok = True
    snaps = {"fred:VIX": 17.10}

    bad_case = "## 0.65 历史分位（近1年）\n\n- VIX：现值 15.84，近1年分位 **23%**（中位，样本 257 期）\n"
    _, iss = check_consistency(parse_ai_daily(bad_case), snaps)
    print(f"  [T1] 人造 VIX=15.84 vs 快照 17.10 → 检出 {len(iss)} 项")
    if iss:
        print(f"       ✓ {iss[0]['msg']}")
    else:
        print("       ✗ 未检出 —— 检查器失效"); ok = False

    good_case = "## 0.65 历史分位（近1年）\n\n- VIX：现值 17.10，近1年分位 **49%**（中位，样本 258 期）\n"
    _, iss2 = check_consistency(parse_ai_daily(good_case), snaps)
    print(f"  [T2] 正确值 VIX=17.10 → 检出 {len(iss2)} 项（应为 0）")
    if iss2:
        print("       ✗ 误报"); ok = False
    else:
        print("       ✓ 无误报")

    both = ("## 0.65 历史分位（近1年）\n\n- VIX：现值 15.84，近1年分位 **23%**\n\n"
            "## 2. 异动榜\n\n- VIX现货：+7.95%（现值 17.1，streak +1）\n")
    _, iss3 = check_consistency(parse_ai_daily(both), snaps)
    cross = [i for i in iss3 if "多个值" in i["msg"]]
    print(f"  [T3] 同一份报告 VIX 两个值 → 节间矛盾检出 {len(cross)} 项")
    if cross:
        print(f"       ✓ {cross[0]['msg']}")
    else:
        print("       ✗ 未检出节间矛盾"); ok = False

    # T4~T6：新鲜度判据（用相对日期，避免随时间失效）
    _d0 = datetime.now(CST)
    _today = _d0.strftime("%Y-%m-%d")
    _yday = (_d0 - timedelta(days=1)).strftime("%Y-%m-%d")
    _3ago = (_d0 - timedelta(days=3)).strftime("%Y-%m-%d")
    _hm = _d0.strftime("%H:%M")
    cases = [
        ("T4 我方滞后（我方 97.26@3天前 vs 源 102.75@今天）",
         97.26, 102.75, _today, "09:00", _3ago, True),
        ("T5 跨日但值一致（102.75 vs 102.75）",
         102.75, 102.75, _today, "09:00", _yday, False),
        ("T6 盘中偏离（102.75@昨天 vs 110.0@此刻）",
         102.75, 110.0, _today, _hm, _yday, False),
    ]
    for label, ours, theirs, tdate, ttime, ours_date, want_bad in cases:
        _, bad, _, state, note = judge_row(ours, theirs, tdate, ttime, ours_date, 0.02)
        good = (bad == want_bad)
        print(f"  [{label}] → bad={bad}（期望 {want_bad}）{'✓' if good else '✗'}  [{state}] {note}")
        if not good:
            ok = False

    print("  self-test:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# ---------------------------------------------------------------- 自动改码护栏
# 背景：CLI 被授权在数据源失效时**直接改代码换源**。但它改的是「下一轮还要跑」的代码，
# 所以必须有一道闸门。三道校验，任一不过即回滚：
#   ① 语法  py_compile      —— 拦缩进/括号这类硬错误
#   ② 导入  import <模块>   —— 拦模块级 NameError / 坏 import
#   ③ 效果  重跑 verify.py  —— 拦「改了但更差」（异常数不得增加）
# 回滚 = 改前副本覆盖 + 删掉新增文件。最坏结果退化成「没改」，而不是「管线断掉」。


def _code_files():
    """管线目录下的采集/计算脚本。下划线开头的是临时补丁脚本，不纳入。"""
    try:
        names = os.listdir(BASE)
    except OSError:
        return []
    return sorted(f for f in names if f.endswith(".py") and not f.startswith("_"))


def _rb(path):
    with open(path, "rb") as f:
        return f.read()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _rt(path):
    try:
        return open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return ""


def _issue_count():
    """本次自检的异常条数（读 verify_result.json）；读不到返回 None。"""
    try:
        with open(RESULT, encoding="utf-8") as f:
            return len(json.load(f).get("issues") or [])
    except Exception:
        return None


def snapshot_code(now):
    """改码前留底：备份全部脚本 + 记下 sha256 与当前异常数。"""
    os.makedirs(FIXBAK, exist_ok=True)
    for old in os.listdir(FIXBAK):
        try:
            os.remove(os.path.join(FIXBAK, old))
        except OSError:
            pass
    files, total = {}, 0
    for name in _code_files():
        raw = _rb(os.path.join(BASE, name))
        with open(os.path.join(FIXBAK, name), "wb") as f:
            f.write(raw)
        files[name] = _sha(raw)
        total += len(raw)
    before = _issue_count()
    with open(FIXSTATE, "w", encoding="utf-8") as f:
        json.dump(dict(ts=now, files=files, issues_before=before), f,
                  ensure_ascii=False, indent=2)
    print(f"[fixbak] 已备份 {len(files)} 个脚本（{total / 1024:.0f} KB）| "
          f"改前异常 {before} 项 → {FIXBAK}")
    return 0


def _diff_against(state):
    """按 sha256 比出 (改动, 新增, 删除) 三类文件名。"""
    changed, added = [], []
    for name in _code_files():
        if name not in state["files"]:
            added.append(name)
        elif state["files"][name] != _sha(_rb(os.path.join(BASE, name))):
            changed.append(name)
    removed = [n for n in state["files"] if not os.path.exists(os.path.join(BASE, n))]
    return changed, added, removed


def _rollback(state):
    """还原：新增的删掉，改动的用副本覆盖。返回被还原的文件名。"""
    done = []
    for name in _code_files():
        if name not in state["files"]:
            try:
                os.remove(os.path.join(BASE, name))
                done.append(name + "（新增，已删）")
            except OSError:
                pass
    for name in state["files"]:
        bak = os.path.join(FIXBAK, name)
        if not os.path.exists(bak):
            continue
        cur = os.path.join(BASE, name)
        if (not os.path.exists(cur)) or _sha(_rb(cur)) != state["files"][name]:
            with open(cur, "wb") as f:
                f.write(_rb(bak))
            done.append(name)
    return done


def _compile_ok(name):
    """① 语法。py_compile 到临时 cfile，不污染 __pycache__。"""
    import py_compile
    try:
        py_compile.compile(os.path.join(BASE, name),
                           cfile=os.path.join(FIXBAK, "_pyc_check"), doraise=True)
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _import_ok(name):
    """② 导入。子进程 import，抓模块级错误。"""
    try:
        p = subprocess.run([sys.executable, "-c", f"import {name[:-3]}"],
                           cwd=BASE, capture_output=True, timeout=120)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if p.returncode == 0:
        return True, ""
    err = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()
    return False, (err[-1] if err else f"exit {p.returncode}")[:160]


def _rerun_verify():
    """③ 效果。子进程重跑自检，返回 (异常数, 备注)。"""
    try:
        p = subprocess.run([sys.executable, "verify.py"], cwd=BASE,
                           capture_output=True, timeout=420)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    n = _issue_count()
    if n is None:
        tail = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return None, (tail[-1] if tail else f"exit {p.returncode}")[:160]
    return n, ""


def _change_summary(changed):
    """抓改动摘要（旧端点 → 新端点、行级改动数）。

    必须在**回滚之前**调用 —— 回滚会把文件还原，之后再比对就什么都看不到了。
    """
    out = []
    for name in changed:
        old = _rt(os.path.join(FIXBAK, name))
        new = _rt(os.path.join(BASE, name))
        ou, nu = set(RE_URL.findall(old)), set(RE_URL.findall(new))
        d = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0))
        nchg = sum(1 for x in d if x[:1] in "+-" and not x.startswith(("+++", "---")))
        out.append(dict(name=name, removed=sorted(ou - nu), added=sorted(nu - ou), nchg=nchg))
    return out


def write_fix_md(now, state, summary, added, removed, steps, fail, rolled, after):
    """写「本次自动修复」汇报段 —— 让人一眼看到 CLI 到底动了什么。"""
    before = state.get("issues_before")
    L = [f"### 5. 本次自动修复 · {now}", ""]

    if not (summary or added or removed):
        L += ["- 结论：CLI 本次**未改动任何代码**（只做了核对）。", ""]
    else:
        L += [f"- 结论：{'**已回滚**（未通过校验，源码已还原）' if fail else '**已生效**（三道校验通过）'}"]
        if fail:
            L.append(f"- 回滚原因：{fail}")
            if rolled:
                L.append("- 已还原：" + "、".join(f"`{r}`" for r in rolled))
        L.append("")

        if summary:
            L += ["**改动文件**", ""]
            for c in summary:
                L.append(f"- `{c['name']}`（行级改动 {c['nchg']} 行）")
                for u in c["removed"]:
                    L.append(f"  - 移除端点：`{u}`")
                for u in c["added"]:
                    L.append(f"  - 新增端点：`{u}`")
            L.append("")
        if added:
            L += ["**新增文件**：" + "、".join(f"`{x}`" for x in added), ""]
        if removed:
            L += ["**删除文件**：" + "、".join(f"`{x}`" for x in removed), ""]

        L += ["**校验**", "", "| 关口 | 结果 | 说明 |", "|---|---|---|"]
        for label, ok, msg in steps:
            L.append(f"| {label} | {'通过' if ok else '失败'} | {(msg or '').replace('|', '/')[:120]} |")
        L.append("")
        if not fail and after is not None:
            L += [f"- 自检异常数：{before} → {after}", ""]

    L += ["---", ""]
    try:
        with open(FIXMD, "w", encoding="utf-8") as f:
            f.write("\n".join(L))
    except Exception as e:
        print(f"[fixchk] 汇报写入失败：{type(e).__name__}: {e}")


def check_code(now):
    """CLI 改码后的校验入口。本函数只报告、不抛异常（不该阻断 bat）。"""
    if not os.path.exists(FIXSTATE):
        print("[fixchk] 无快照（未跑 --snapshot）— 跳过校验")
        return 0
    try:
        with open(FIXSTATE, encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:
        print(f"[fixchk] 快照读取失败：{type(e).__name__}: {e}")
        return 0

    # 快照过期就罢手：snapshot→checkfix 在 bat 里只隔几分钟，
    # 若中间隔了几小时，期间落盘的更可能是**人工改动**，回滚掉就是误伤。
    try:
        age = (datetime.now(CST) - datetime.strptime(
            state.get("ts", ""), "%Y-%m-%d %H:%M").replace(tzinfo=CST)).total_seconds() / 60
    except Exception:
        age = None
    if age is not None and age > FIX_MAX_AGE_MIN:
        print(f"[fixchk] 快照已过期（{age:.0f} 分钟前）— 跳过校验，避免误回滚人工改动")
        return 0

    changed, added, removed = _diff_against(state)
    if not (changed or added or removed):
        print("[fixchk] CLI 未改代码 — 无需校验")
        write_fix_md(now, state, [], [], [], [], "", [], None)
        return 0

    summary = _change_summary(changed)       # 必须在回滚前抓，否则还原后什么都看不到
    names = changed + added
    steps, fail, after = [], "", None
    print(f"[fixchk] 检出改动：{len(changed)} 改 / {len(added)} 新增 / {len(removed)} 删除 → {'、'.join(names)}")

    for name in names:                                   # ① 语法
        ok, msg = _compile_ok(name)
        steps.append((f"语法 {name}", ok, msg))
        if not ok:
            fail = f"语法错误（{name}）"
            break

    if not fail:                                         # ② 导入
        for name in names:
            if name in SKIP_IMPORT:
                continue
            ok, msg = _import_ok(name)
            steps.append((f"导入 {name[:-3]}", ok, msg))
            if not ok:
                fail = f"导入失败（{name}）"
                break

    if not fail:                                         # ③ 效果
        after, note = _rerun_verify()
        before = state.get("issues_before")
        if after is None:
            steps.append(("效果 重跑 verify.py", False, note))
            fail = "重跑自检拿不到结果"
        elif before is not None and after > before:
            steps.append(("效果 重跑 verify.py", False, f"异常 {before} → {after}（变差）"))
            fail = f"异常数由 {before} 增至 {after}"
        else:
            steps.append(("效果 重跑 verify.py", True, f"异常 {before} → {after}"))

    rolled = []
    if fail:
        rolled = _rollback(state)
        _rerun_verify()                                  # 还原后再跑一次，让报告/归档回到一致状态
        print(f"[fixchk] ✗ {fail} — 已回滚 {len(rolled)} 个文件：{'、'.join(rolled) or '无'}")
    else:
        print(f"[fixchk] ✓ 三道校验通过 → {FIXMD}")

    write_fix_md(now, state, summary, added, removed, steps, fail, rolled, after)
    return 0


def merge_all():
    """把「自检报告 + 本次自动修复 + CLI 核验结论」合成一块并入日报（幂等）。"""
    body = read_or(REPORT)
    fix = read_or(FIXMD)
    if fix.strip():
        body = body.rstrip() + "\n\n" + fix.strip() + "\n"
    cli = read_or(CLI_IN)
    if cli.strip():
        body = body.rstrip() + "\n\n### 4. CLI 核验结论\n\n" + cli.strip() + "\n"
    return merge_into_daily(body)


# ---------------------------------------------------------------- main

def main():
    args = sys.argv[1:]
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")

    if "--self-test" in args:
        return self_test()

    if "--snapshot" in args:
        return snapshot_code(now)

    if "--checkfix" in args:
        return check_code(now)

    if "--merge" in args:
        if merge_all():
            print(f"[merge] 已并入 {os.path.basename(DAILY_MD)}")
        else:
            print("[merge] 无可并入内容")
        return 0

    if not os.path.exists(AI_DAILY):
        print("[fail] data/ai_daily.md 不存在 —— 先跑 macro_insight.py")
        return 1

    snaps = load_snaps()
    parsed = parse_ai_daily(read_or(AI_DAILY))
    try:
        aof = json.load(open(ASOF, encoding="utf-8")) if os.path.exists(ASOF) else {}
    except Exception:
        aof = {}

    c_rows, c_iss = check_consistency(parsed, snaps)
    s_rows, s_iss = check_second_source(snaps, aof)
    l_rows, l_iss = check_lag()
    issues = c_iss + s_iss + l_iss

    with open(REPORT, "w", encoding="utf-8") as f:
        f.write(build_report(c_rows, c_iss, s_rows, s_iss, l_rows, l_iss, now))
    with open(RESULT, "w", encoding="utf-8") as f:
        json.dump(dict(ts=now, issues=issues,
                       consistency=c_rows, second_source=[
                           {k: v for k, v in r.items() if k != "fn"} for r in s_rows],
                       lag=l_rows), f, ensure_ascii=False, indent=2)

    if issues:
        with open(TODO, "w", encoding="utf-8") as f:
            f.write(build_todo(issues, now))
    elif os.path.exists(TODO):
        os.remove(TODO)          # 无异常 → 删掉开关，bat 就不会叫 CLI

    print(f"[verify] 解析 ai_daily {len(parsed)} 处现值 | 一致性 {len(c_rows)} 项 | "
          f"双源 {len(s_rows)} 项 | 日频时效 {len(l_rows)} 项")
    for it in issues:
        print(f"    [!] {it['kind']}: {it['msg']}")
    print(f"[verify] {'发现 %d 项异常' % len(issues) if issues else '全部通过'} → {REPORT}")

    # 归档：**只有全部通过才写** daily.csv。有异常时宁可那天缺，也不把没确认的值写成历史。
    archived = archive_daily(now) if not issues else 0
    if issues:
        print(f"[archive] 跳过（有 {len(issues)} 项异常，未确认的值不入档）")
    elif archived:
        print(f"[archive] 已归档 {archived} 个日频值 → {os.path.basename(DAILY_CSV)}")
    append_log(now, issues, archived)

    merge_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
