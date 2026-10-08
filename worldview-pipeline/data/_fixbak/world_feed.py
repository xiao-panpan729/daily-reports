# -*- coding: utf-8 -*-
"""
world_feed.py — 世界观项目 · 世界读数快照 v1

采集（全部只读公开接口，不涉及任何下单/钱包）：
  1) Hyperliquid 主站 allMids        —— 全球加密永续 + HIP-4 事件结果市场(YES/NO 概率)
  2) Hyperliquid 主站 资金费率/持仓量 —— 杠杆与拥挤度
  3) Hyperliquid HIP-3 (xyz dex)     —— 美股/商品/指数 24/7 永续 = 隔夜温度计本体
  4) Polymarket gamma-api            —— 事件概率 + 概率日变动(经 Clash 7897，失败自动降级)
  5) 外部宏观源(链1补全)             —— FRED官方REST API 15序列(代理优先直连兜底) + Yahoo USDCNH/恒生/MOVE/金/铜(Clash)
                                        + akshare中债10/30Y + 中国货币网USDCNY即期

运行环境：建议用 miniconda base 的 python（D:/miniconda3/python.exe，已装 akshare）
  python world_feed.py              # 若系统 python 即 miniconda，则中债/USDCNY 正常启用
  v2：HIP-3 采集层全量(118个全落盘) + 焦点层 FOCUS 只用于展示

派生（第三点：不只存截面，要存状态）：
  水平值 / 变化率 / 持续天数(streak，同方向连续快照数)

用法：
  python world_feed.py --dry-run   只打印读数，不落盘
  python world_feed.py             落盘到 data/ (snapshots.csv + pm_markets.csv + 读数 md)
"""
import argparse
import csv
import json
import math
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
HL_URL = "https://api.hyperliquid.xyz/info"
PM_URL = "https://gamma-api.polymarket.com/markets"
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")


def _load_env():
    """读取 .env（不依赖 python-dotenv，纯手写解析）。
    查找顺序：脚本目录 → 逐级向上最多 4 级，命中第一个即加载。
    注：项目根的 .env 在 BASE 的上一级，只看 BASE 会加载不到（FRED key 报未配置的根因）。
    KEY=VALUE 每行一条，支持 # 注释与首尾空格；已存在的环境变量优先。
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
# FRED 官方 REST API（免费 key 存 .env，勿硬编码）
FRED_API = "https://api.stlouisfed.org/fred/series/observations"
FRED_KEY = os.environ.get("FRED_API_KEY", "")
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1mo&interval=1d"  # 必须走Clash

# FRED 序列清单（链1 全节点 + 风险偏好硬指标 + 流动性三件套 + 通胀/商品）
#   UST10Y/20Y/30Y  美债长端（链1 第二环）
#   UST2Y           美债短端（对 Fed 预期最敏感）
#   UST10Y2Y        期限利差（倒挂=衰退警报）
#   FEDFUNDS        联邦基金目标利率上限（链1 第一环官方读数）
#   HY_OAS          美高收益债利差（信用风险偏好，risk-on/off）
#   VIX             VIX 现货（⚠️ 2026-09-15 起现值改走 Yahoo ^VIX，本行仅作兜底）
#   WALCL/RRP/TGA   净流动性三件套 = Fed总资产 − 隔夜逆回购 − 财政部TGA
#                   （WALCL/WTREGEN 周频、RRPONTSYD 日频，量纲见 macro_insight.py）
#   SOFR            担保隔夜融资利率（与 FEDFUNDS 偏离=货币市场压力）
#   T10YIE          10Y盈亏平衡通胀（Fed 双使命另一半）
#   WTI             原油（通胀与地缘的交汇点）
#   IG_OAS          投资级利差（与 HY_OAS 配对：同走阔=系统性信用事件）
FRED_SERIES = [
    ("DGS10", "fred:UST10Y"),
    ("DGS20", "fred:UST20Y"),
    ("DGS30", "fred:UST30Y"),
    ("DGS2", "fred:UST2Y"),
    ("T10Y2Y", "fred:UST10Y2Y"),
    ("DFEDTARU", "fred:FEDFUNDS"),
    ("BAMLH0A0HYM2", "fred:HY_OAS"),
    ("VIXCLS", "fred:VIX"),
    ("WALCL", "fred:WALCL"),
    ("RRPONTSYD", "fred:RRP"),
    ("WTREGEN", "fred:TGA"),
    ("SOFR", "fred:SOFR"),
    ("T10YIE", "fred:T10YIE"),
    ("DCOILWTICO", "fred:WTI"),    # 兜底：现值由 Yahoo CL=F 优先（DCOILWTICO 是 EIA 现货，滞后约 4 个交易日）
    ("BAMLC0A0CM", "fred:IG_OAS"),
]

# 焦点层：每天看的约18个（采集层不设限，allMids 一次返回全部 118 个 HIP-3 标的，全量落盘）
# 版本 v2 —— 对照四条假设链修订：
#   链1 Fed→美元→CNH→恒生 : +DXY(HL无美债/CNH/恒生，需外部源补)
#   链3 套息→风险偏好      : +JPY +VIX +JP225
#   链4 AI capex→算力      : +TSM +SMH +AMZN +META(资本开支方)
#   补强                   : +GOLD(避险对价) +NATGAS(能源链放大器)；-CL(与Brent重复) -ALUMINIUM(流动性差)
FOCUS = [
    "xyz:NVDA", "xyz:AMD", "xyz:AVGO", "xyz:TSM", "xyz:SMH",
    "xyz:AMZN", "xyz:META", "xyz:MSFT", "xyz:GOOGL",
    "xyz:BABA", "xyz:COIN", "xyz:XYZ100",
    "xyz:DXY", "xyz:JPY", "xyz:VIX", "xyz:JP225",
    "xyz:BRENTOIL", "xyz:NATGAS", "xyz:COPPER", "xyz:GOLD",
]
PM_KEYWORDS = ["FED", "RATE", "CUT", "HIKE", "CPI", "INFLATION", "OIL", "GOLD", "NVIDIA", "NVDA",
               "AI", "BITCOIN", "BTC", "TARIFF", "CHINA", "RECESSION", "ECB", "BOJ",
               "ISRAEL", "IRAN", "OPEC"]
# 全词匹配集合：AI 用子串会误中 AIRSPACE/MAINTAIN/DETAILS 等噪声
# （2026-09-08 实测：以色列市场就是靠 "AI"RSPACE 歪打正着进来的，好用但属于偶然，
#  故将 ISRAEL/IRAN/OPEC 转正为正式关键词，AI 改为全词匹配防噪声）
PM_WHOLE_WORDS = {"AI"}
# 词根边界匹配（2026-09-10 血案修复）：子串匹配会误中——
#   "RATE" 命中 PI-RATE-S（匹兹堡海盗 vs 芝加哥白袜，MLB 棒球赛）；
#   该赛打完后概率 0.435→0.9985，24h 变动 +0.563 霸占变动榜前两名，
#   把原油(WTI$100)和伊朗停火挤出 top5 → LLM 原料里看不到原油 → 晨报整段漏掉原油。
#   同理 CUT 会命中 EXE-CUT-IVE、HIKE 命中 HI-KE 类噪声。
# 故改为 \b词根S?\b：保留复数（RATES/CUTS/HIKES），排除嵌在别的单词里的情况。
PM_STEM_WORDS = {"RATE", "CUT", "HIKE"}

# ── Yahoo 准实时优先源（2026-09-15 新增）──────────────────────────────────
# 动机：FRED 的日频序列**发布有滞后**，而 _fred_latest() 只取「最新一个非空观测」、
#   不记录也不判断该观测属于哪一天 → 旧值被静默当成"今日值"写进日报，
#   delta 还显示 +0.00%，而日报脚注把 +0.00% 解释成"非更新日正常现象" →
#   滞后被合理化，用户无从察觉（本次事故的本质）。
# 实证（2026-09-15 08:33 采集，FRED/Yahoo/新浪三源交叉验证）：
#   VIXCLS     返回 09-11 的 15.84（滞后 1 个交易日）；真实 09-14 收盘 17.10
#   DCOILWTICO 返回 09-09 的 97.26（滞后 4 个交易日）；真实 102.71
# 处置：这三条改由 Yahoo 准实时源**优先**取数，FRED 仅在 Yahoo 失败时兜底。
# 注：key 仍叫 fred:VIX / fred:WTI 是**历史包袱**——report.py / macro_insight.py 里
#   对它们的引用是行情语义、与数据源无关；改名要动 4 个文件 6 处引用且易漏。
#   真实来源由 snapshots.csv 的 source 列记录（写 yahoo），数据可追溯。
YAHOO_PRIORITY = [
    ("%5EVIX", "fred:VIX", "VIX 现货"),
    ("CL%3DF", "fred:WTI", "WTI 原油"),
    ("DX-Y.NYB", "idx:DXY", "美元指数"),
]

# ── VIX 期限结构（2026-09-15 新增）：替代原先的「VIX 永续 − 现货差」──────
# 原实现 = Hyperliquid xyz:VIX − fred:VIX，但 xyz:VIX 是**零成交死标的**
#   （openInterest=0、dayNtlVlm=0、midPx 为空），markPx 恒等于初始占位值 20.0，
#   于是"永续溢价 4.16"纯属虚构，报告还拿它解读成"赌风险升温"。
# VIX 的正确对照物是**它自己的期限结构**（期货曲线）：
#   VIX/VIX3M < 1 → 正挂 contango（近低远高，市场平静，卖波动率环境）
#   VIX/VIX3M > 1 → 倒挂 backwardation（近高远低，恐慌避险，恐慌溢价）
# Yahoo 免费提供，无需新 key。
VIX_TERM = [
    ("%5EVIX9D", "vix:9D", "VIX 9日"),
    ("%5EVIX3M", "vix:3M", "VIX 3月"),
    ("%5EVIX6M", "vix:6M", "VIX 6月"),
    ("%5EVVIX", "vix:VVIX", "VVIX 波动率的波动率"),
]


def _yahoo_result(sym):
    """Yahoo chart 1mo/1d → chart.result[0]（含 meta 与日线）。走 Clash（直连 403）。"""
    j = _open(YAHOO_CHART.format(sym=sym), proxy=CLASH_PROXY)
    return j["chart"]["result"][0]


def _mark_asof(asof, key, result, source="Yahoo"):
    """记录某 key 的**数据日期**（日报"数据日"列用）。
    Yahoo 时间戳是 UTC epoch，必须叠加交易所本地偏移再取日期，
    否则美东 09-14 收盘会被记成北京 09-15，虚报成"今天"。
    meta.gmtoffset 就是交易所相对 UTC 的秒偏移（如 America/Chicago = -18000）。"""
    meta = result.get("meta") or {}
    t = meta.get("regularMarketTime")
    d = None
    if t:
        try:
            off = int(meta.get("gmtoffset") or 0)
            d = datetime.fromtimestamp(int(t) + off, timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            d = None
    asof[key] = {"date": d or datetime.now(CST).strftime("%Y-%m-%d"), "source": source}


def _pm_hit(q):
    """Polymarket 问题串(已大写)是否命中关键词。AI 全词，其余子串。"""
    for w in PM_KEYWORDS:
        if w in PM_WHOLE_WORDS:
            if re.search(rf"\b{w}\b", q):
                return True
        elif w in PM_STEM_WORDS:
            if re.search(rf"\b{w}S?\b", q):
                return True
        elif w in q:
            return True
    return False


def _open(url, payload=None, proxy=None, timeout=25, retries=2):
    """JSON GET/POST，带重试（Clash 偶发 SSL EOF 瞬断）。"""
    if payload is not None:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url, headers={"User-Agent": "worldview/0.1"})
    last_err = None
    for _ in range(retries):
        try:
            opener = (urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
                      if proxy else urllib.request.build_opener())
            with opener.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last_err = e
    raise last_err


def _isnan(x):
    """统一判空：None / NaN / 空串 / 非数字字符串 都算无值。
    用于拦住休市日数据源返回的 NaN，避免写进 CSV 污染 streak 基线。"""
    if x is None:
        return True
    try:
        return math.isnan(float(x))
    except (TypeError, ValueError):
        return True


def _fred_latest(sid):
    """FRED REST API → 最新一个非空观测值 (date, float)。
    连接策略：代理优先、直连兜底。
    实测（2026-09-06，各 5 次，超时 12s）：直连 1/5 成功且中位 15.4s，代理 5/5 成功中位 2.2s。
    旧注释「纯直连、走 Clash 会越跑越慢」与实测相反，已废弃。
    返回 None 表示无数据。"""
    if not FRED_KEY:
        raise RuntimeError("FRED_API_KEY 未配置（请在项目根 .env 中设置）")
    url = (f"{FRED_API}?series_id={sid}&file_type=json&api_key={FRED_KEY}"
           f"&sort_order=desc&limit=20")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last_err = None
    for proxy in (CLASH_PROXY, None):
        try:
            opener = (urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
                if proxy else urllib.request.build_opener())
            with opener.open(req, timeout=25) as r:
                obs = json.loads(r.read().decode()).get("observations", [])
            for o in obs:
                if o.get("value") not in (".", ""):
                    return o["date"], float(o["value"])
            return None
        except Exception as e:
            last_err = e
    raise last_err


TREASURY_CSV_URL = ("https://home.treasury.gov/resource-center/data-chart-center/"
                    "interest-rates/daily-treasury-rates.csv/{yr}/all"
                    "?type=daily_treasury_yield_curve&field_tdr_date_value={yr}&page&_format=csv")
# 列名与 key 对应：财政部 CSV 列 "2 Yr"/"10 Yr"/"20 Yr"/"30 Yr"，key 沿用 FRED 的
# fred:UST2Y/UST10Y/UST20Y/UST30Y → 下游 chains/分位/报告零改动。
# 2026-09-15 补 "30 Yr"：此前 30Y 只走 FRED DGS30，实测滞后 3 天（财政部 09-14=5.34 时
# FRED 还停在 09-11 的 5.35），而同一份 CSV 里本来就有这列，只是没被映射——同一类"源滞后"问题。
TREASURY_MAP = [("2 Yr", "fred:UST2Y"), ("10 Yr", "fred:UST10Y"),
                ("20 Yr", "fred:UST20Y"), ("30 Yr", "fred:UST30Y")]


def fetch_treasury():
    """美国财政部官方日度收益率曲线（home.treasury.gov CSV，直连，无需代理）。
    动机：FRED 的 DGS2/DGS10 滞后约 1 个交易日（FRED 还停在 09-03 时财政部已出 09-04，
    与东财实时行情一致）。返回最新一行 dict（文件倒序，rows[0] 即最新交易日），无数据返回 []。
    节假日财政部也不更新，与市场日历天然同步。"""
    import io
    import csv as _csv
    now = datetime.now(CST)
    for yr in (now.year, now.year - 1):   # 跨年兜底：年初可能要读上一年文件
        try:
            req = urllib.request.Request(
                TREASURY_CSV_URL.format(yr=yr), headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode("utf-8", "replace")
            rows = list(_csv.DictReader(io.StringIO(text)))
            if rows:
                return rows
        except Exception:
            continue
    return []


def fetch_hl_mids(dex=None):
    p = {"type": "allMids"}
    if dex:
        p["dex"] = dex
    return _open(HL_URL, p)


def fetch_hl_ctx(dex=None):
    """返回 {name: {funding, openInterest, volume24h}}，失败返回 {}"""
    p = {"type": "metaAndAssetCtxs"}
    if dex:
        p["dex"] = dex
    try:
        d = _open(HL_URL, p)
        meta, ctx = d[0], d[1]
        out = {}
        prefix = (dex + ":") if dex else ""
        for m, c in zip(meta.get("universe", []), ctx):
            name = m["name"]
            # HIP-3 universe 名字已带 "xyz:" 前缀，别再加一遍（旧 bug：xyz:xyz:NVDA 永远查不到）
            if dex and not name.startswith(dex + ":"):
                name = prefix + name
            out[name] = {
                "funding": c.get("funding"),
                "openInterest": c.get("openInterest"),
                "volume24h": c.get("dayNtlVlm"),
            }
        return out
    except Exception as e:
        print(f"    [warn] ctx 抓取失败({dex or 'main'}): {str(e)[:60]}")
        return {}


def fetch_pm(limit=300):
    url = f"{PM_URL}?closed=false&limit={limit}&order=volume24hr&ascending=false"
    return _open(url, proxy=CLASH_PROXY)


# ── Polymarket 24h 概率曲线（2026-09-10 新增）──────────────────────────────
# 为什么必须补：一天只在早 8:26 采一次，两次采集之间的变化被完整漏掉。
# 实证：09-09 夜间 Brent +3.36% 破 100，而 WTI$100 题快照停在 09-09 11:12 的 0.465；
#   该曲线 24h 内实为 0.435→0.665（振幅 +0.230），峰值 09-10 07:00，当下 0.62
#   —— 单点快照与真实状态差 15.5 个百分点，这就是「晨报与事实割裂」的量化幅度。
# 接口：CLOB prices-history，interval=1d&fidelity=60 = 过去 24h 每小时一点（实测 25 点）。
# 注：backfill.py 用的是 fidelity=1440（一天一点拉 14 天），那是日线基线，粒度与用途都不同。
PM_CLOB_HIST = "https://clob.polymarket.com/prices-history"


def fetch_pm_curve(token, proxy=None, timeout=20):
    """某市场过去 24h 的概率曲线摘要。失败返回 None（不抛，避免连累主流程）。"""
    try:
        j = _open(f"{PM_CLOB_HIST}?market={token}&interval=1d&fidelity=60",
                  proxy=proxy, timeout=timeout)
        hist = (j or {}).get("history") or []
        pts = [(int(h["t"]), float(h["p"])) for h in hist if h.get("p") is not None]
        if len(pts) < 2:
            return None
        p0, pn = pts[0][1], pts[-1][1]
        lo_t, lo = min(pts, key=lambda x: x[1])
        hi_t, hi = max(pts, key=lambda x: x[1])
        return {"p_24h_ago": round(p0, 4), "p_now": round(pn, 4),
                "p_min": round(lo, 4), "p_max": round(hi, 4),
                "p_chg_24h": round(pn - p0, 4), "p_range": round(hi - lo, 4),
                "peak_ts": datetime.fromtimestamp(hi_t, CST).strftime("%m-%d %H:%M"),
                "trough_ts": datetime.fromtimestamp(lo_t, CST).strftime("%m-%d %H:%M"),
                "n_points": len(pts)}
    except Exception:
        return None


# ── CFTC COT：CME 日元期货非商业持仓（套息交易的「仓位规模」本体）────────
# 为什么必须补它：利差只决定「引信多长」，决定爆炸当量的是有多少仓位挤在门口。
# 2024-08 那次平仓的实质 = 极端净空 + 波动率飙升，不是 15bp 本身。
COT_URL = "https://www.cftc.gov/dea/newcot/FinFutWk.txt"
COT_JPY_PREFIX = '"JAPANESE YEN'          # 精确匹配日元行，避开 "EURO FX/JAPANESE YEN XRATE"
# ⚠️ 2026-09-09 踩坑（务必看）：FinFutWk.txt 是 **TFF 报告**（Traders in Financial Futures），
#   不是 legacy COT，列含义完全不同——
#     8/9  = Dealer（交易商/做市盘）多空。它与投机盘天然对手、方向相反，
#            拿它当"非商业净持仓"会得到 +79,321「净多」，进而误判"套息已平完、接近尾声"。
#     14/15 = Leveraged Funds（杠杆基金 = 真投机盘）多空。净空 -102,188 = 燃料充足。
#   两者方向完全相反，用错列就是把风险读反。列位恒等式已验证：
#     OI(7) = TotRep(20/21) + NonRep(22/23) = 411,882；spreads 合计 77,994 对得上。
COT_COL_ASOF, COT_COL_OI = 1, 7
COT_COL_DEALER_LONG, COT_COL_DEALER_SHORT = 8, 9      # 做市对手方，仅作参考
COT_COL_LEV_LONG, COT_COL_LEV_SHORT = 14, 15          # 杠杆基金 = 真投机盘，主口径


def _cot_fetch(proxy=None, timeout=25):
    """CFTC 官网取数：直连优先（实测国内直连可达），代理兜底。
    该站证书链在国内会被中间设备替换 → 必须关校验，否则 CERTIFICATE_VERIFY_FAILED。
    （2026-09-09 实测：直连 70KB 正常返回；stooq 的日本国债源则被反爬拦，不可用。）"""
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    handlers = [urllib.request.HTTPSHandler(context=ctx)]
    if proxy:
        handlers.insert(0, urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    req = urllib.request.Request(COT_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def fetch_cot_jpy():
    """CME 日元期货持仓 → dict / None。主口径是 **杠杆基金（真投机盘）**，不是交易商。
    lev_net = 杠杆基金多头 − 空头：
      负值 = 净空 → 套息拥挤 = 燃料堆得多（2024-07 崩前即为十几万手净空）
      正值 = 净多 → 套息已平完甚至反手，剩余平仓空间有限
    dealer_net = 交易商（做市对手方）净持仓，天然与投机盘反向，只作交叉校验用。
    周频：每周五发布、反映当周二持仓，滞后约 1 周。只回答「还有多少弹药」，不回答「今天炸没炸」。"""
    txt, last = None, "?"
    for proxy in (None, CLASH_PROXY):
        try:
            txt = _cot_fetch(proxy)
            break
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:60]}"
    if not txt:
        print(f"    [warn] COT 取数失败（{last}）")
        return None
    for line in txt.splitlines():
        if not line.startswith(COT_JPY_PREFIX):
            continue
        cells = next(csv.reader([line]))
        try:
            oi = float(cells[COT_COL_OI])
            ll = float(cells[COT_COL_LEV_LONG])
            ls = float(cells[COT_COL_LEV_SHORT])
            dl = float(cells[COT_COL_DEALER_LONG])
            ds = float(cells[COT_COL_DEALER_SHORT])
        except (ValueError, IndexError):
            print("    [warn] COT 日元行解析失败")
            return None
        if oi <= 0 or abs(ll - ls) > oi or abs(dl - ds) > oi:   # 净持仓不可能超过总持仓
            print(f"    [warn] COT 数值异常 oi={oi} lev={ll}/{ls} dealer={dl}/{ds}")
            return None
        return {"asof": cells[COT_COL_ASOF], "oi": oi,
                "lev_long": ll, "lev_short": ls, "lev_net": ll - ls,
                "dealer_net": dl - ds}
    print("    [warn] COT 未找到日元行")
    return None


def load_prev_streaks():
    """读基线，返回 {key: (value, streak)}。

    基线 = **上一数据日的确认值**（daily.csv，由 verify.py 自检通过后归档）。

    2026-09-15 改：原来读 snapshots.csv 的最后一行，那是「上次采集」而不是
    「昨天」——一天跑 3 次就有 3 套快照，delta 成了「和上次采集的差」
    （同日重跑恒为 +0.00%），根本不是日变动。daily.csv 每个 (数据日, key)
    只有一条且经自检，delta 才是真正的日变动。

    daily.csv 不存在时（首次运行 / 归档未启用）退回 snapshots.csv。
    """
    daily = os.path.join(DATA, "daily.csv")
    if os.path.exists(daily):
        out = {}
        with open(daily, encoding="utf-8") as f:
            for r in csv.DictReader(f):     # date 升序，后行覆盖前行 = 取最近数据日
                out[r["key"]] = (r["value"], r.get("streak") or 0)
        if out:
            return out
    path = os.path.join(DATA, "snapshots.csv")
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):     # 退回：上次采集
            out[r["key"]] = (r["value"], r["streak"])
    return out


def derive(key, value, prev):
    """水平 / 变化率 / 持续天数"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return value, "", ""
    if key not in prev:
        return round(v, 6), "", ""          # 无历史基线
    pv, pstreak = prev[key]
    try:
        pv_f = float(pv)
    except ValueError:
        return round(v, 6), "", ""
    if _isnan(pv_f):            # 历史基线被 nan 污染（如休市日写入的空报价），本次重建基线
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印，不落盘")
    args = ap.parse_args()

    now = datetime.now(CST)
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n=== 世界读数快照 {ts} (CST) ===\n")

    prev = {} if args.dry_run else load_prev_streaks()
    rows = []      # (source, key, value, delta, streak)
    asof = {}      # {key: {"date": "YYYY-MM-DD", "source": ...}} —— 日报"数据日"列用

    # 1) HIP-3 隔夜温度计
    print("[1] HIP-3 美股/商品/指数 24/7（隔夜温度计）")
    dead = set()
    try:
        xyz = fetch_hl_mids("xyz")
        xyz_ctx = fetch_hl_ctx("xyz")
        # 采集层：全量落盘（成本≈0，一次调用全返回）
        # ⚠️ 2026-09-15 新增：**零成交死标的剔除**。
        #   实测全 120 个标的里 16 个 openInterest=0 且 dayNtlVlm=0，markPx 恒等于
        #   初始占位值：xyz:VIX 永远 20.0、xyz:DXY 永远 97.15 —— 不是行情，是没开盘的占位。
        #   此前它们被当正常行情落盘并展示（DXY 报 97.15 而真实 99.54；VIX 报 20.0 而真实 17.10），
        #   还喂给了派生信号（"VIX 永续−现货差 4.16"）。
        #   安全阀：若 ctx 抓取失败（xyz_ctx 为空）则**不过滤**，避免把全量标的一次误杀。
        ctx_ok = bool(xyz_ctx)
        derived_map = {}
        collected = 0
        for k in sorted(xyz):
            if not k.startswith("xyz:"):
                continue
            c = xyz_ctx.get(k) or {}
            try:
                oi = float(c.get("openInterest") or 0)
                vol = float(c.get("volume24h") or 0)
            except (TypeError, ValueError):
                oi = vol = 0.0
            if ctx_ok and oi == 0 and vol == 0:
                dead.add(k)
                continue
            val, d, st = derive(k, xyz[k], prev)
            rows.append(("hl_xyz", k, val, d, st))
            derived_map[k] = (d, st)
            collected += 1
        if dead:
            print(f"    [剔] 零成交死标的 {len(dead)} 个（OI=0 且 24h成交=0，价格是占位值，不落盘）：")
            print(f"        {', '.join(sorted(dead))}")
        if not ctx_ok:
            print("    [warn] ctx 抓取失败 → 本次不做流动性过滤（宁可多留，不可误杀）")
        # 焦点层：只打印每天看的 FOCUS
        for k in FOCUS:
            if k in xyz and k not in dead:
                d, st = derived_map.get(k, (None, None))
                ctx = xyz_ctx.get(k, {})
                f = ctx.get("funding")
                if f is not None:   # 资金费率持久化（拥挤度数据，供 AI 读市场在押哪边）
                    fv, fd, fst = derive(k + ":funding", f, prev)
                    rows.append(("hl_ctx", k + ":funding", fv, fd, fst))
                extra = f"  资金费率={f}" if f is not None else ""
                ds = f"{d:+.2%}" if isinstance(d, float) else "-"
                st_show = str(st) if st not in (None, "") else "-"
                print(f"    {k:16s} {str(xyz[k]):>12s}  变动 {ds:>8s}  持续 {st_show:>4s}{extra}")
        print(f"    (焦点 {len(FOCUS)} 个 / 采集全量 {collected} 个)")
    except Exception as e:
        print(f"    [fail] {str(e)[:80]}")

    # 2) 主站：风险偏好 + 杠杆拥挤度
    print("\n[2] 主站风险偏好 / 杠杆（BTC、ETH、SOL）")
    try:
        main_mids = fetch_hl_mids()
        main_ctx = fetch_hl_ctx()
        for k in ["BTC", "ETH", "SOL"]:
            if k in main_mids:
                val, d, st = derive(k, main_mids[k], prev)
                rows.append(("hl_main", k, val, d, st))
                ctx = main_ctx.get(k, {})
                ds = f"{d:+.2%}" if isinstance(d, float) else "-"
                st_show = str(st) if st not in (None, "") else "-"
                print(f"    {k:16s} {str(val):>12s}  变动 {ds:>8s}  持续 {st_show:>4s}"
                      f"  资金费率={ctx.get('funding')}")
        # 3) HIP-4 事件结果市场
        ev = sorted([k for k in main_mids if k.startswith("#")])
        print(f"\n[3] HIP-4 事件结果市场：{len(ev)} 个 token（约 {len(ev)//2} 个事件，概率成对、和为1）")
        for k in ev[:8]:
            val, d, st = derive(k, main_mids[k], prev)
            rows.append(("hl_event", k, val, d, st))
        for i in range(0, min(len(ev), 8), 2):
            a, b = ev[i], ev[i + 1]
            print(f"    {a}/{b}  YES={main_mids[a]}  NO={main_mids[b]}")
    except Exception as e:
        print(f"    [fail] {str(e)[:80]}")

    # 4) Polymarket
    pm_rows = []
    pm_curve_rows = []
    pm_err = None
    print("\n[4] Polymarket 事件概率动量（经 Clash 7897）")
    try:
        mkts = fetch_pm(200)
        hits = []
        for m in mkts:
            q = (m.get("question") or "").upper()
            if _pm_hit(q):
                hits.append(m)
        hits.sort(key=lambda m: abs(float(m.get("oneDayPriceChange") or 0)), reverse=True)
        # 2026-09-10 修复：原为 hits[:6]，只保留「单日波动最大」的 6 条。
        # 实测血案：Fed 议题命中 5 条，其中「9月会议维持利率不变」vol24h=137.99 万
        # （全榜成交量第 1，是 WTI 题的 14.6 倍），但因日内只动 ±1pp 而排在第 11~23 位，
        # 全部被截断 → pm_markets.csv 里没有 Fed → 下游 0.7 节输出「无 Fed 定价数据」
        # → 晨报只能写「手头无 Fed 定价数据」，而本周恰好是 CPI(9/11)+FOMC(9/16)。
        # 按 |变动| 排序会系统性排除「高成交量 + 低日内波动」的议题，故改为全部保留。
        print(f"    抓到 {len(mkts)} 个市场，关键词命中 {len(hits)}，全部写入（不再按变动截断）：")
        for m in hits:
            try:
                prices = json.loads(m.get("outcomePrices") or "[]")
                yes = prices[0] if prices else None
            except Exception:
                yes = None
            chg = m.get("oneDayPriceChange")
            # 24h 概率曲线：单点快照会漏掉两次采集之间的变化，补一条小时级曲线兜底
            try:
                _tids = json.loads(m.get("clobTokenIds") or "[]")
                _cur = fetch_pm_curve(_tids[0], proxy=CLASH_PROXY) if _tids else None
            except Exception:
                _cur = None
            if _cur:
                pm_curve_rows.append({"ts": ts, "id": m.get("id"),
                                      "question": m.get("question"), **_cur})
            pm_rows.append({
                "ts": ts, "id": m.get("id"), "question": m.get("question"),
                "yes_price": yes, "chg_1d": chg,
                "volume24h": m.get("volume24hr"), "end_date": m.get("endDate"),
                "slug": m.get("slug"),
            })
            cs = f"{float(chg):+.3f}" if chg is not None else "-"
            print(f"    [{cs:>7s}] YES={yes}  {str(m.get('question'))[:58]}")
            print(f"             结算 {str(m.get('endDate'))[:10]}  24h量 {m.get('volume24hr')}")
    except Exception as e:
        pm_err = f"{type(e).__name__}: {str(e)[:120]}"
        print(f"    [降级] Polymarket 不可达（Clash 未开或接口异常）：{str(e)[:70]}")
        print("            → 本次仅 Hyperliquid 数据，不影响其余部分。")
        print("            → ⚠️ pm_markets.csv 本次不写入，下游读到的是上一次成功快照；"
              "若下游不加新鲜度判断，旧概率会被当成今日定价。")

    # 5) 外部宏观源（链1补全：Fed→美债→美元→CNH→恒生）
    print("\n[5] 外部宏观源：美债/联邦基金/利差(FRED REST) / 中债+USDCNY(akshare) / CNH+恒生(Yahoo)")
    ext_print = []
    # 5a-0) 财政部官方收益率曲线（直连，通常比 FRED 新一个交易日）→ 优先采用；
    #       拿到的 key 从 FRED 循环里跳过，保证同一天同一 key 只落一条记录。
    ust_keys = set()
    try:
        trows = fetch_treasury()
        if trows:
            t0, tdate = trows[0], trows[0].get("Date", "")
            for col, key in TREASURY_MAP:
                v = t0.get(col)
                if not v:
                    continue
                val, dd, st = derive(key, float(v), prev)
                rows.append(("treasury", key, val, dd, st))
                ust_keys.add(key)
                # 财政部日期是 MM/DD/YYYY，统一转 ISO 供 report.py 算滞后
                try:
                    _tiso = datetime.strptime(tdate, "%m/%d/%Y").strftime("%Y-%m-%d")
                except Exception:
                    _tiso = tdate
                asof[key] = {"date": _tiso, "source": "财政部"}
                ext_print.append((key, val, dd, st, f"(财政部 {tdate})"))
        else:
            print("    [warn] 财政部收益率: 无数据（FRED 兜底）")
    except Exception as e:
        print(f"    [warn] 财政部收益率: {str(e)[:60]}（FRED 兜底）")
    # 5a-1) Yahoo 准实时优先源（VIX/WTI/DXY）—— 必须排在 FRED 之前。
    #       理由见 YAHOO_PRIORITY 注释：FRED 发布滞后且 _fred_latest 不判断观测日期。
    pre_keys = set()
    for sym, key, cn in YAHOO_PRIORITY:
        try:
            r0 = _yahoo_result(sym)
            v = r0["meta"].get("regularMarketPrice")
            if v is None or _isnan(v):
                raise ValueError("no price")
            val, dd, st = derive(key, v, prev)
            rows.append(("yahoo", key, val, dd, st))
            pre_keys.add(key)
            _mark_asof(asof, key, r0, "Yahoo")
            ext_print.append((key, val, dd, st, f"(Yahoo {cn})"))
        except Exception as e:
            print(f"    [warn] Yahoo 优先源 {key}: {str(e)[:50]} → 回落 FRED/无值")
    # 5a-2) VIX 期限结构（Yahoo）—— 派生信号 VIX/VIX3M 的原料，替代已废的永续差价
    for sym, key, cn in VIX_TERM:
        try:
            r0 = _yahoo_result(sym)
            v = r0["meta"].get("regularMarketPrice")
            if v is None or _isnan(v):
                raise ValueError("no price")
            val, dd, st = derive(key, v, prev)
            rows.append(("yahoo", key, val, dd, st))
            _mark_asof(asof, key, r0, "Yahoo")
            ext_print.append((key, val, dd, st, f"(Yahoo {cn})"))
        except Exception as e:
            print(f"    [warn] Yahoo {key}: {str(e)[:50]}")
    # 5a) FRED 美债/联邦基金/利差/流动性/通胀/商品（官方 REST API，15 个序列）
    for sid, key in FRED_SERIES:
        if key in ust_keys or key in pre_keys:
            continue
        try:
            res = _fred_latest(sid)
            if res is None:
                continue
            d0, v = res
            val, dd, st = derive(key, v, prev)
            rows.append(("fred", key, val, dd, st))
            asof[key] = {"date": d0, "source": "FRED"}
            # 滞后显式化：日频超 4 天、周频超 10 天才喊，别让旧值伪装成"今天没动"
            try:
                lag = (datetime.now(CST).date() - datetime.strptime(d0, "%Y-%m-%d").date()).days
            except Exception:
                lag = 0
            thr = 10 if sid in FRED_WEEKLY else 4
            if lag >= thr:
                print(f"    [滞后] FRED {sid} 最新观测 {d0}（距今 {lag} 天，非今日值）")
            ext_print.append((key, val, dd, st, f"(FRED {d0}{'，滞后%d天' % lag if lag >= thr else ''})"))
        except Exception as e:
            print(f"    [warn] FRED {sid}: {str(e)[:60]}")
    # 5b) Yahoo：USDCNH + 恒生 + MOVE + 金/铜期货（走 Clash，直连403）
    for sym, key in [("CNH=X", "fx:USDCNH"), ("%5EHSI", "idx:HSI"),
                     ("%5EMOVE", "idx:MOVE"), ("GC%3DF", "cm:GOLD_FUT"), ("HG%3DF", "cm:COPPER_FUT")]:
        try:
            r0 = _yahoo_result(sym)
            v = r0["meta"].get("regularMarketPrice")
            if v is None or _isnan(v):
                raise ValueError("no price")
            val, dd, st = derive(key, v, prev)
            rows.append(("yahoo", key, val, dd, st))
            _mark_asof(asof, key, r0, "Yahoo")
            ext_print.append((key, val, dd, st, "(Yahoo)"))
        except Exception as e:
            print(f"    [warn] Yahoo {key}: {str(e)[:60]}")
    # 5c) 中债 10Y/30Y + USDCNY 即期（需 akshare；用 venv python 运行本脚本可启用，缺了自动跳过）
    try:
        import akshare as ak
        df = ak.bond_zh_us_rate()
        lr = df.iloc[-1]
        for col, key in [("中国国债收益率10年", "bond:CN10Y"), ("中国国债收益率30年", "bond:CN30Y")]:
            v = lr.get(col)
            if v is None or str(v).lower() in ("nan", ""):
                continue
            val, dd, st = derive(key, float(v), prev)
            rows.append(("cnbond", key, val, dd, st))
            asof[key] = {"date": str(lr["日期"])[:10], "source": "中债"}
            ext_print.append((key, val, dd, st, f"(中债 {lr['日期']})"))
        # USDCNY 即期（中国货币网，直连）—— 与 fx:USDCNH 的差 = 在岸/离岸情绪差
        fx = ak.fx_spot_quote()
        r0 = fx[fx["货币对"] == "USD/CNY"]
        if not r0.empty:
            bid, ask = r0["买报价"].iloc[0], r0["卖报价"].iloc[0]
            # 休市/非交易时段货币网返回 NaN，必须拦掉：写进 CSV 会污染后续 streak 基线
            if _isnan(bid) or _isnan(ask):
                print("    [warn] 货币网 USD/CNY 无报价（休市或非交易时段）→ 跳过，不写快照")
            else:
                v = (float(bid) + float(ask)) / 2
                val, dd, st = derive("fx:USDCNY", v, prev)
                rows.append(("chinamoney", "fx:USDCNY", val, dd, st))
                asof["fx:USDCNY"] = {"date": now.strftime("%Y-%m-%d"), "source": "货币网"}
                ext_print.append(("fx:USDCNY", val, dd, st, "(货币网)"))
    except ImportError:
        print("    [warn] 本 python 无 akshare → 中债收益率跳过（用 venv python 运行可启用）")
    except Exception as e:
        print(f"    [warn] 中债收益率: {str(e)[:60]}")
    # COT：CME 日元期货非商业持仓（周频，回答「套息还有多少弹药」）
    try:
        cot = fetch_cot_jpy()
        if cot:
            val, dd, st = derive("cot:JPY_LEV_NET", cot["lev_net"], prev)
            rows.append(("cftc", "cot:JPY_LEV_NET", val, dd, st))
            ext_print.append(("cot:JPY_LEV_NET", val, dd, st, f"(COT {cot['asof']} 杠杆基金, 净空=燃料)"))
            for _k, _v in (("cot:JPY_LEV_LONG", cot["lev_long"]),
                           ("cot:JPY_LEV_SHORT", cot["lev_short"]),
                           ("cot:JPY_DEALER_NET", cot["dealer_net"]),
                           ("cot:JPY_OI", cot["oi"])):
                _v2, _d2, _s2 = derive(_k, _v, prev)
                rows.append(("cftc", _k, _v2, _d2, _s2))
    except Exception as e:
        print(f"    [warn] COT: {type(e).__name__}: {str(e)[:60]}")

    for key, val, dd, st, note in ext_print:
        ds = f"{dd:+.3%}" if isinstance(dd, float) else "-"
        st_show = str(st) if st not in (None, "") else "-"
        print(f"    {key:16s} {str(val):>12s}  变动 {ds:>8s}  持续 {st_show:>4s} {note}")

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
        if pm_rows:
            pp = os.path.join(DATA, "pm_markets.csv")
            newp = not os.path.exists(pp)
            with open(pp, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(pm_rows[0].keys()))
                if newp:
                    w.writeheader()
                w.writerows(pm_rows)
        if pm_curve_rows:
            cp = os.path.join(DATA, "pm_curve.csv")
            newc = not os.path.exists(cp)
            with open(cp, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(pm_curve_rows[0].keys()))
                if newc:
                    w.writeheader()
                w.writerows(pm_curve_rows)
        # Polymarket 新鲜度留痕：成功或失败都必须落状态，否则下游无从判断快照新旧。
        # 2026-09-10 事故复盘：Clash 未开 → 采集失败 → pm_markets.csv 静默不更新 →
        # 晨报拿 21 小时前的旧概率写「今天市场在关注什么」，与已破 100 的原油严重割裂。
        try:
            last_ok = ts if pm_rows else ""
            if not last_ok:
                try:
                    with open(os.path.join(DATA, "pm_markets.csv"), encoding="utf-8") as _f:
                        last_ok = max((r.get("ts") or "" for r in csv.DictReader(_f)), default="")
                except Exception:
                    last_ok = ""
            json.dump({"last_attempt_ts": ts, "last_ok_ts": last_ok,
                       "n_markets": len(pm_rows), "error": pm_err},
                      open(os.path.join(DATA, "pm_status.json"), "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"    [warn] pm_status 写入失败: {str(e)[:60]}")
        # 数据日期留痕（2026-09-15 新增）：日报"数据日"列据此标注，
        # 直接回答"这个数到底是哪天的" —— 本次 VIX/WTI 事故的根因就是没人问这句话。
        try:
            json.dump(asof, open(os.path.join(DATA, "asof.json"), "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1, sort_keys=True)
        except Exception as e:
            print(f"    [warn] asof.json 写入失败: {str(e)[:60]}")
        print(f"\n已落盘：{len(rows)} 条数值快照 + {len(pm_rows)} 条事件市场"
              f" + {len(pm_curve_rows)} 条24h曲线 → {DATA}")
        if dead:
            print(f"        已剔除 {len(dead)} 个零成交死标的（见上方 [剔] 行）")
    else:
        print(f"\n[dry-run] 未落盘（{len(rows)} 快照 / {len(pm_rows)} 事件 / "
              f"{len(pm_curve_rows)} 24h曲线）。去掉 --dry-run 即写入 data/。")


if __name__ == "__main__":
    main()
