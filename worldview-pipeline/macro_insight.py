# -*- coding: utf-8 -*-
"""
macro_insight.py — 世界观项目 · 宏观逻辑链引擎 v1

定位：world_feed 采集的是"原始读数"，本文件把它变成"逻辑"。
数据背后的链路第一次代码化（此前只存在于对话里，没有体现在展示层面）：
  0) 净流动性     WALCL − RRP − TGA（Fed 的"量"，链1 的上游水库）
  1) 货币空转     M2同比 − GDP同比 → 宽松是否进了实体
  2) 通胀消化     M2同比 − CPI同比 → 货币消化缺口（负 = 钱追逐存量资产）
  3) 政策分化     中美10Y利差 → 资本流动方向 → 汇率压力 → 降息空间受限
  4) 经济动能     中美 PMI 同步性 → 全球周期方向
  5) 套息风险     美日利差 + 日元贬值 → 套息膨胀 → 平仓风险
  E) 意外计       金十"今值 vs 预测值"自建宏观意外指数（Citi ESI 的免费近似）

输出（跑完 world_feed 后运行）：
  data/chains.json   机器可读（report.py 读它生成日报章节）
  data/llm_brief.md  给 LLM 的解读原料——直接整段粘贴给 Claude/ima 做深度推演

数据透明原则：每个输入都带"数据日期"。金十源可能滞后（如 2025-09），
滞后就标注滞后，不用新数据冒充、也不用旧数据装新。

运行：D:/miniconda3/python.exe macro_insight.py   （需 akshare；venv python 亦可，缺 akshare 自动降级）
"""
import csv
import json
import math
import os
import statistics
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from world_feed import HL_URL, FRED_API, FRED_KEY, CLASH_PROXY, DATA, CST, _open, _fred_latest

BASE = os.path.dirname(os.path.abspath(__file__))
CHAINS_JSON = os.path.join(DATA, "chains.json")
LLM_BRIEF = os.path.join(DATA, "llm_brief.md")
ES_CSV = os.path.join(DATA, "es_releases.csv")
AI_MEMORY = os.path.join(DATA, "ai_memory.jsonl")   # 机器自动追加的逐日状态（决策记忆）
AI_DAILY = os.path.join(DATA, "ai_daily.md")        # 给 AI 的日更简报（可整段粘给任意 LLM）

CARRY_SPREAD_CAP = 5.0      # 美日利差 5pp 封顶（归一化用）
# ── 套息链新口径常量（2026-09-09）──────────────────────────────────────
# 旧口径只有 JPY_DEPR_CAP（日元贬值才加分），方向是反的：日元一升值、真实平仓
# 发生时，指数反而下降。现拆成「引信 tinder（慢）」与「火 unwind（快）」两个量。
JPY_APP_5D_CAP = 3.0        # 日元 5 日升值 3% 封顶
JPY_APP_20D_CAP = 6.0       # 日元 20 日升值 6% 封顶（2024-08 实为约 9%，超 cap 即满分）
JPY_VOL_CAP = 15.0          # 已实现波动率 15%（年化）封顶
JPY_NET_SHORT_CAP = 100000.0  # 非商业净空 10 万手封顶（2024-07 崩前的量级）


# ---------------------------------------------------------------- 基础工具

def fred_window(sid, limit=90):
    """FRED 序列最近 N 个非空观测 → [(date, value)] 按日期升序。"""
    if not FRED_KEY:
        raise RuntimeError("FRED_API_KEY 未配置")
    url = (f"{FRED_API}?series_id={sid}&file_type=json&api_key={FRED_KEY}"
           f"&sort_order=desc&limit={limit}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last_err = None
    for proxy in (CLASH_PROXY, None):
        try:
            opener = (urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
                if proxy else urllib.request.build_opener())
            with opener.open(req, timeout=25) as r:
                obs = json.loads(r.read().decode()).get("observations", [])
            out = [(o["date"], float(o["value"])) for o in obs
                   if o.get("value") not in (".", "", None)]
            return sorted(out)
        except Exception as e:
            last_err = e
    raise last_err


def load_snap_latest():
    """读 snapshots.csv 最新值 → {key: (value, delta, streak)}（与 report.load_latest 同构）。"""
    sp = os.path.join(DATA, "snapshots.csv")
    out = {}
    if not os.path.exists(sp):
        return out
    with open(sp, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                out[r["key"]] = float(r["value"])
            except (TypeError, ValueError):
                pass
    return out


def load_snap_history(key, days=30):
    """history.csv 里某 key 最近 N 天的 (date, value) 升序列列（算趋势用）。"""
    hp = os.path.join(DATA, "history.csv")
    out = []
    if not os.path.exists(hp):
        return out
    cutoff = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d")
    with open(hp, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["key"] == key and r["date"] >= cutoff:
                try:
                    out.append((r["date"], float(r["value"])))
                except (TypeError, ValueError):
                    pass
    return sorted(out)


def pct_change(series, lookback=20):
    """[(date,value)] 取最近第 lookback 个样本到最新的涨跌幅（%）。"""
    if len(series) < 3:
        return None, None
    idx = min(lookback, len(series) - 1)
    old_d, old = series[-idx]
    new = series[-1][1]
    if old == 0:
        return None, None
    return (new - old) / abs(old) * 100, (old_d, series[-1][0])


# ---------------------------------------------------------------- 0) 净流动性

def calc_net_liq():
    """净流动性 = WALCL(百万$) − RRPONTSYD(十亿$)×1000 − WTREGEN(百万$)。
    WALCL/WTREGEN 周频、RRP 日频 → 按 WALCL 最新日期对齐三者的最近观测。"""
    walcl = fred_window("WALCL", 30)
    tga = fred_window("WTREGEN", 30)
    rrp = dict(fred_window("RRPONTSYD", 30))
    base_d = walcl[-1][0]                      # 以 Fed 总资产表为锚（周三发布）
    tga_d = dict(tga).get(base_d)              # TGA 同为周频，日期一般能对上
    if tga_d is None:                          # 对不上则取 ≤ base_d 最近一个
        older = [d for d, _ in tga if d <= base_d]
        tga_d = dict(tga)[older[-1]] if older else None
    rrp_d = None                               # RRP 日频，取 ≤ base_d 最近一个
    for d, v in sorted(rrp.items()):
        if d <= base_d:
            rrp_d = v
    if tga_d is None or rrp_d is None:
        return None
    net = walcl[-1][1] - rrp_d * 1000 - tga_d
    # 30 日前对照
    old = [w for w in walcl if w[0] <= (datetime.strptime(base_d, "%Y-%m-%d")
                                        - timedelta(days=30)).strftime("%Y-%m-%d")]
    chg = None
    if old:
        # 粗对照：30 天前的净流动性用当期 RRP/TGA 近似（周频量变动小，误差可忽略）
        net_old = old[-1][1] - rrp_d * 1000 - tga_d
        chg = (net - net_old) / 1e6            # 万亿$ 单位的变化
    return {
        "date": base_d,
        "walcl_t": walcl[-1][1] / 1e6,         # 万亿$
        "rrp_b": rrp_d / 1e3,                  # 万亿$（十亿→万亿）
        "tga_t": tga_d / 1e6,
        "net_t": net / 1e6,
        "chg30_t": chg,
    }


# ---------------------------------------------------------------- E) 意外计

# 金十源（商品/日期/今值/预测值/前值）。地区分组决定进美国指数还是中国指数。
# 注意：金十部分序列更新滞后（实测 2025-09 停更），滞后表现为"不再有新发布日期"，
# 意外计对滞后天然免疫（不重复计入），只是覆盖变窄——所以每次输出都报"最近事件日期"。
ESI_BASKET = [
    ("US_ISM_PMI",      "美国ISM制造业PMI", "US", 1.0),
    ("US_CORE_CPI_MoM", "美国核心CPI月率",  "US", 1.0),
    ("CN_CPI_YoY",      "中国CPI年率",      "CN", 1.0),
    ("CN_GDP_YoY",      "中国GDP年率",      "CN", 1.5),
]


def _es_fetch(fn_map):
    """拉金十序列 → {sid: [(date, actual, forecast)]}（含今值的行，升序）。"""
    try:
        import akshare as ak
    except ImportError:
        return None
    out = {}
    for sid, fn_name in fn_map.items():
        try:
            df = getattr(ak, fn_name)()
            rows = []
            for _, r in df.iterrows():
                a, f = r.get("今值"), r.get("预测值")
                if a is None or (isinstance(a, float) and math.isnan(a)):
                    continue
                f = None if (f is None or (isinstance(f, float) and math.isnan(f))) else float(f)
                rows.append((str(r["日期"]), float(a), f))
            out[sid] = sorted(rows)
        except Exception as e:
            print(f"    [warn] 意外计 {sid}: {str(e)[:60]}")
    return out or None


def calc_esi():
    """自建宏观意外指数（Citi ESI 免费近似版）：
    surprise = 今值 − 预测值（预测值缺失记 0，"市场没预期就没意外"）；
    单序列标准化：surprise / 该序列近24次意外的标准差（缺历史则回退 1）；
    ESI = 最近 90 天内各发布标准化意外的加权和（权重见 ESI_BASKET）。
    量纲："标准差点数"，0=符合预期，正=数据强于预期。与花旗官方口径（更宽因子、
    逐日衰减半衰期）不同源不同量纲——只做方向性对照，不冒充花旗。"""
    fn_map = {
        "US_ISM_PMI": "macro_usa_ism_pmi",
        "US_CORE_CPI_MoM": "macro_usa_core_cpi_monthly",
        "CN_CPI_YoY": "macro_china_cpi_yearly",
        "CN_GDP_YoY": "macro_china_gdp_yearly",
    }
    fetched = _es_fetch(fn_map)
    if fetched is None:
        return None
    # 1) 逐序列历史意外（含本次之前）→ 标准差
    hist_std = {}
    for sid in fn_map:
        sur = [a - (f if f is not None else 0.0) for _, a, f in fetched.get(sid, [])]
        recent = sur[-24:]
        hist_std[sid] = statistics.pstdev(recent) if len(recent) >= 4 else 1.0
        if hist_std[sid] == 0:
            hist_std[sid] = 1.0
    # 2) 已记录的发布（es_releases.csv）→ 只增量记录新日期
    seen = set()
    if os.path.exists(ES_CSV):
        with open(ES_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                seen.add((r["series"], r["date"]))
    new_rows, events = [], []
    for sid, cn, region, w in ESI_BASKET:
        for d, a, f in fetched.get(sid, []):
            if (sid, d) in seen:
                continue
            sur = a - (f if f is not None else 0.0)
            z = sur / hist_std[sid]
            new_rows.append({"series": sid, "name": cn, "region": region,
                             "date": d, "actual": a, "forecast": f,
                             "surprise": round(sur, 4), "z": round(z, 4)})
            events.append((sid, cn, region, d, a, f, z, w))
    if new_rows:
        newfile = not os.path.exists(ES_CSV)
        with open(ES_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["series", "name", "region", "date",
                                              "actual", "forecast", "surprise", "z"])
            if newfile:
                w.writeheader()
            w.writerows(new_rows)
    else:
        # 无新发布时，从历史表重建 90 天窗口
        with open(ES_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                events.append((r["series"], r["name"], r["region"], r["date"],
                               float(r["actual"]), r["forecast"] and float(r["forecast"]),
                               float(r["z"]), next(x[3] for x in ESI_BASKET if x[0] == r["series"])))
    # 3) 90 天窗口合成
    cutoff = (datetime.now(CST) - timedelta(days=90)).strftime("%Y-%m-%d")
    esi = {"US": 0.0, "CN": 0.0}
    detail = {"US": [], "CN": []}
    latest = {}
    for sid, cn, region, d, a, f, z, w in events:
        latest[sid] = (d, a, f)
        if d >= cutoff:
            esi[region] += z * w
            detail[region].append(f"{cn} {d}：今值 {a} vs 预测 {'缺' if f is None else f} → z={z:+.2f}")
    last_ev = max((e[3] for e in events), default=None)
    return {"esi": esi, "detail": detail, "latest": latest, "last_event": last_ev,
            "note": "金十源，覆盖 CPI/PMI/GDP；与花旗 ESI 不同源不同量纲，只做方向对照"}


# ---------------------------------------------------------------- 官方宏观端点
# 改自 a-stock-data V3.7.0 宏观层（github.com/simonlin1212/a-stock-data，Apache-2.0）
# 动机：akshare 金十源 2025-09 后停更（CPI/ISM/GDP 冻结在 2025-08/09），
#       改走人民银行/统计局官网直连（零鉴权、fail-fast），CPI 为本项目按同套路自写。
import io as _io
import re as _re

_NBS_UA = {"User-Agent": "Mozilla/5.0"}
NBS_INDEX = "https://www.stats.gov.cn/sj/zxfb/"
PBC_BASE = "https://www.pbc.gov.cn"
PBC_INDEX = f"{PBC_BASE}/diaochatongjisi/116219/116319/index.html"


def _gov_get(url, timeout=30):
    import requests
    r = requests.get(url, headers=_NBS_UA, timeout=timeout)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def nbs_latest_title(kw):
    """统计局最新发布页找含 kw 的最新条目 → (title, url)。"""
    idx = _gov_get(NBS_INDEX)
    links = _re.findall(r'<a[^>]+href="([^"]+)"[^>]*>\s*([^<]{6,80}?)\s*</a>', idx)
    hit = next(((u, t) for u, t in links if kw in t), None)
    if not hit:
        raise RuntimeError(f"统计局最新发布页未找到含「{kw}」的条目")
    return hit[1], (hit[0] if hit[0].startswith("http") else NBS_INDEX + hit[0].lstrip("./"))


def nbs_cpi():
    """国家统计局最新 CPI（标题即含同比：'2026年7月份居民消费价格同比上涨0.5%'）。
    返回 (yoy_pct, period)；正文再挖环比，挖不到不影响主输出。"""
    title, url = nbs_latest_title("居民消费价格")
    yoy = _re.search(r"居民消费价格同比上涨([\d.]+)%", title)
    if not yoy:
        raise RuntimeError(f"CPI 标题措辞已变更：{title}")
    ym = _re.search(r"(\d{4})年(\d{1,2})月份", title)
    period = f"{ym.group(1)}-{int(ym.group(2)):02d}" if ym else title
    return float(yoy.group(1)), f"{period} (统计局)"


def nbs_pmi():
    """国家统计局最新 PMI（制造业/非制造业/综合）。返回 (manufacturing, period)。"""
    title, url = nbs_latest_title("采购经理指数")
    html = _gov_get(url)
    text = _re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=_re.S)
    text = _re.sub(r"<[^>]+>", "", text)
    text = _re.sub(r"[\s\u3000\xa0]+", "", text)   # 全角括号内带空格，必须全删
    m = _re.search(r"(?<!非)制造业采购经理指数（PMI）为([\d.]+)%", text)
    if not m:
        raise RuntimeError(f"PMI 正文措辞已变更，请核对：{url}")
    ym = _re.search(r"(\d{4})年(\d{1,2})月", title)
    period = f"{ym.group(1)}-{int(ym.group(2)):02d}" if ym else title
    return float(m.group(1)), f"{period} (统计局)"


def pboc_social_financing(year=None):
    """人民银行「社会融资规模增量统计表」— 月度 DataFrame（单位亿元）。
    三级跳（索引→年份页→专题页→xls 附件），任何一级结构变更即抛错，不静默返回空。
    仅支持 2021 起（旧版式表头合并，解析不可靠）。"""
    import pandas as pd
    idx = _gov_get(PBC_INDEX)
    years = _re.findall(r"""href=["']([^"']+)["'][^>]*>\s*(\d{4})年统计数据\s*</a>""", idx)
    if not years:
        raise RuntimeError("人民银行索引页未找到「XXXX年统计数据」链接")
    table = {int(y): href for href, y in years}
    target = max(table) if year is None else year
    if target not in table:
        raise ValueError(f"人民银行无 {target} 年数据")
    ypage = _gov_get(PBC_BASE + table[target] if not table[target].startswith("http") else table[target])
    topics = _re.findall(r"""href=["']([^"']+)["'][^>]*>\s*(社会融资规模)\s*</a>""", ypage)
    if not topics:
        raise RuntimeError(f"{target} 年页未找到「社会融资规模」专题链接")
    tpage = _gov_get(PBC_BASE + topics[0][0] if not topics[0][0].startswith("http") else topics[0][0])
    books = _re.findall(r"""href=["']([^"']+\.xlsx?)["']""", tpage)
    if not books:
        raise RuntimeError(f"{target} 年社融专题页未找到 xls/xlsx 附件")
    import requests
    content = requests.get(PBC_BASE + books[0] if not books[0].startswith("http") else books[0],
                           headers=_NBS_UA, timeout=60).content
    raw = pd.read_excel(_io.BytesIO(content), header=None)
    start = next((i for i in range(len(raw)) if str(raw.iloc[i, 0]).strip() == "月份"), None)
    if start is None:
        raise RuntimeError(f"{target} 年社融表无「月份」表头（旧版式不支持）")
    cols = ["month", "afre_total", "rmb_loans", "fx_loans", "entrusted_loans",
            "trust_loans", "bankers_acceptance", "corporate_bonds", "government_bonds",
            "equity_financing", "abs_by_depository", "loans_written_off"]
    df = raw.iloc[start + 3:].copy().iloc[:, :len(cols)]
    df.columns = cols
    df = df[df["month"].astype(str).str.match(r"^\d{4}\.\d{1,2}$", na=False)].copy()
    for c in cols[1:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    def _label(v):
        m = _re.match(r"^(\d{4})\.(\d{1,2})$", str(v).strip())
        if not m:
            return None
        mon = m.group(2) if len(m.group(2)) == 2 else m.group(2) + "0"  # 2026.1 → 10 月
        return f"{m.group(1)}-{int(mon):02d}"

    df["month"] = [_label(v) for v in df["month"]]
    df = df[df["month"].notna() & df["month"].str.startswith(f"{target}-")]
    df = df.dropna(subset=["afre_total"]).reset_index(drop=True)
    if df.empty:
        raise RuntimeError(f"社融表解析后无有效月份（{target} 年）")
    return df


# ---------------------------------------------------------------- 1-5) 因果链

def _st(v, lo_red, lo_yel):
    """通用阈值 → (图标, 等级)。v 越大越危险。"""
    if v is None:
        return "⚪", "数据不足"
    if v >= lo_red:
        return "🔴", "危险"
    if v >= lo_yel:
        return "🟡", "偏高"
    return "🟢", "正常"


def china_monthly():
    """中国月度数据：CPI/PMI 优先统计局官网（金十已停更），M2 走 akshare，
    社融走人民银行官网。全部带数据日期。"""
    out = {}
    try:
        import akshare as ak
    except ImportError:
        ak = None
        print("    [warn] 无 akshare → M2/GDP 降级")
    if ak is not None:
        try:
            df = ak.macro_china_money_supply()      # 降序，head 最新
            r = df.head(1).iloc[0]
            out["cn_m2"] = (float(r["货币和准货币(M2)-同比增长"]), str(r["月份"]))
        except Exception as e:
            print(f"    [warn] M2: {str(e)[:50]}")
    try:
        p, d = nbs_pmi()                            # 统计局官网（首选，金十停更）
        out["cn_pmi"] = (p, d)
    except Exception as e:
        print(f"    [warn] 统计局PMI: {str(e)[:50]}")
        if ak is not None:
            try:
                df = ak.macro_china_pmi()
                r = df.head(1).iloc[0]
                out["cn_pmi"] = (float(r["制造业-指数"]), str(r["月份"]))
            except Exception as e2:
                print(f"    [warn] PMI: {str(e2)[:50]}")
    try:
        v, d = nbs_cpi()                            # 统计局官网（首选）
        out["cn_cpi"] = (v, d)
    except Exception as e:
        print(f"    [warn] 统计局CPI: {str(e)[:50]}")
        if ak is not None:
            try:
                df = ak.macro_china_cpi_yearly()
                df = df.dropna(subset=["今值"])
                r = df.tail(1).iloc[0]
                out["cn_cpi"] = (float(r["今值"]), str(r["日期"]))
            except Exception as e2:
                print(f"    [warn] CPI: {str(e2)[:50]}")
    try:
        cur = pboc_social_financing()               # 人民银行官网社融
        out["cn_sf_latest"] = (float(cur.iloc[-1]["afre_total"]), str(cur.iloc[-1]["month"]))
        # 滚动12月合计 + 同比（同比需要上上月同期 → 取前两年拼 24+ 个月）
        try:
            cur_y = int(cur.iloc[-1]["month"][:4])
            allv = []
            for y in (cur_y - 2, cur_y - 1, cur_y):
                try:
                    allv += list(pboc_social_financing(y)["afre_total"])
                except Exception:
                    pass
            if len(allv) >= 24:
                r12 = sum(allv[-12:])
                r12_prev = sum(allv[-24:-12])
                if r12_prev:
                    out["cn_sf_r12"] = (r12, (r12 - r12_prev) / r12_prev * 100)
        except Exception:
            pass
    except Exception as e:
        print(f"    [warn] 社融: {str(e)[:50]}")
    if ak is not None:
        try:
            df = ak.macro_china_gdp_yearly()
            df = df.dropna(subset=["今值"])
            r = df.tail(1).iloc[0]
            out["cn_gdp"] = (float(r["今值"]), str(r["日期"]))
        except Exception as e:
            print(f"    [warn] GDP: {str(e)[:50]}")
    return out


def us_monthly():
    """美国月度数据（FRED）：M2同比 / CPI同比（自算 YoY）。"""
    out = {}
    try:
        m2 = fred_window("M2SL", 15)
        if len(m2) >= 13:
            yoy = (m2[-1][1] - m2[-13][1]) / m2[-13][1] * 100
            out["us_m2"] = (round(yoy, 2), f"{m2[-1][0]} (同比自算)")
    except Exception as e:
        print(f"    [warn] M2SL: {str(e)[:50]}")
    try:
        cpi = fred_window("CPIAUCSL", 15)
        if len(cpi) >= 13:
            yoy = (cpi[-1][1] - cpi[-13][1]) / cpi[-13][1] * 100
            out["us_cpi"] = (round(yoy, 2), f"{cpi[-1][0]} (同比自算)")
    except Exception as e:
        print(f"    [warn] CPIAUCSL: {str(e)[:50]}")
    try:
        # 初请失业金（周频，FRED 日更）——金十 ISM 停更后的美国动能高频替补
        ic = fred_window("ICSA", 80)
        avg4 = sum(v for _, v in ic[-4:]) / 4
        out["us_icsa"] = (round(ic[-1][1]), f"{ic[-1][0]} (周频)")
        out["us_icsa_avg4"] = round(avg4)
    except Exception as e:
        print(f"    [warn] ICSA: {str(e)[:50]}")
    return out


def jpy_series(days=45):
    """USD/JPY 日线 [(date, close)]，涨 = 日元贬值。返回 (series, src)。
    Yahoo 日线优先（需 Clash；代理未开必然失败）；失败用 snapshots.csv 的 xyz:JPY 兜底
    （按日去重、每日取最后一笔）。src ∈ {'yahoo','snapshot'}，用于报告里标注口径与样本长度。
    注：HL candleSnapshot 对主站法币对返回 500，实测不可用，别走那条路。"""
    try:
        now_s = int(time.time())
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/JPY%3DX"
               f"?period1={now_s - days * 86400}&period2={now_s}&interval=1d")
        j = _open(url, proxy=CLASH_PROXY)
        r = j["chart"]["result"][0]
        pts = sorted((datetime.fromtimestamp(t, CST).strftime("%Y-%m-%d"), c)
                     for t, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"])
                     if c is not None)
        if len(pts) >= 5:
            return pts, "yahoo"
    except Exception:
        pass
    out = {}
    sp = os.path.join(DATA, "snapshots.csv")
    if os.path.exists(sp):
        with open(sp, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["key"] == "xyz:JPY":
                    try:
                        out[row["ts"][:10]] = float(row["value"])
                    except (TypeError, ValueError):
                        pass
    return sorted(out.items()), "snapshot"


def jpy_move(series, n):
    """n 个样本前的变动 %（正 = 日元贬值，负 = 日元升值）。样本不足返回 None。"""
    if not series or len(series) < 3:
        return None
    return pct_change(series, n)[0]


def jpy_realized_vol(series, n=20):
    """已实现波动率（年化 %）。样本不足返回 None。对数收益标准差 × sqrt(252) × 100。"""
    if not series or len(series) < 4:
        return None
    px = [v for _, v in series][-(n + 1):]
    rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px)) if px[i - 1] > 0]
    if len(rets) < 3:
        return None
    return statistics.pstdev(rets) * math.sqrt(252) * 100


def calc_chains(snaps):
    """5 条因果链。输入：snapshots 最新值 dict；月度数据现取。返回 chains 列表。"""
    cn = china_monthly()
    us = us_monthly()
    chains = []

    # 链1 货币空转：CN M2同比 − GDP同比
    cn_m2, cn_gdp = cn.get("cn_m2"), cn.get("cn_gdp")
    gap1 = (cn_m2[0] - cn_gdp[0]) if (cn_m2 and cn_gdp) else None
    ic1, lv1 = _st(gap1, 3.0, 1.0)
    chains.append({
        "id": "idle_money", "name": "货币空转（中国）",
        "logic": "M2增速 > GDP增速 → 钱没进实体 → 在金融体系空转 → 风险积累",
        "value": gap1,
        "value_text": f"M2 {cn_m2[0]}% − GDP {cn_gdp[0]}% = {gap1:.1f}pp" if gap1 is not None else "缺数据",
        "status": ic1 + " " + lv1,
        "details": ([f"中国M2同比 {cn_m2[0]}%（{cn_m2[1]}）", f"中国GDP同比 {cn_gdp[0]}%（{cn_gdp[1]}）"]
                    if cn_m2 and cn_gdp else []),
        "implication": "缺口越大，宽松越进不了实体，越利好金融资产/债市、越挤压银行息差" if gap1 is not None else "",
    })

    # 链2 通胀消化：M2同比 − CPI同比（中 + 美）
    cn_cpi, us_m2, us_cpi = cn.get("cn_cpi"), us.get("us_m2"), us.get("us_cpi")
    gap_cn = (cn_m2[0] - cn_cpi[0]) if (cn_m2 and cn_cpi) else None
    gap_us = (us_m2[0] - us_cpi[0]) if (us_m2 and us_cpi) else None
    worst = max(x for x in (gap_cn, gap_us) if x is not None) if (gap_cn is not None or gap_us is not None) else None
    ic2, lv2 = _st(worst, 6.0, 3.0)
    txt = []
    if gap_cn is not None:
        txt.append(f"中国 {cn_m2[0]}−{cn_cpi[0]} = {gap_cn:.1f}pp")
    if gap_us is not None:
        txt.append(f"美国 {us_m2[0]}−{us_cpi[0]} = {gap_us:.1f}pp")
    chains.append({
        "id": "inflation_absorb", "name": "通胀消化缺口（中美）",
        "logic": "M2增速 − CPI增速 = 货币消化能力；缺口大 = 发的钱没被通胀吸收 → 堆在资产价格里",
        "value": worst,
        "value_text": "；".join(txt) if txt else "缺数据",
        "status": ic2 + " " + lv2,
        "details": ([f"中国M2同比 {cn_m2[0]}%（{cn_m2[1]}）", f"中国CPI同比 {cn_cpi[0]}%（{cn_cpi[1]}）"]
                    if cn_m2 and cn_cpi else []) +
                   ([f"美国M2同比 {us_m2[0]}%（{us_m2[1]}）", f"美国CPI同比 {us_cpi[0]}%（{us_cpi[1]}）"]
                    if us_m2 and us_cpi else []),
        "implication": "中国缺口大=极弱通胀、宽松无效化；美国缺口小=货币正常化接近完成",
    })

    # 链3 货币政策分化：CN10Y − US10Y（日频，取自快照）
    cn10, us10 = snaps.get("bond:CN10Y"), snaps.get("fred:UST10Y")
    spread3 = (cn10 - us10) if (cn10 and us10) else None
    ic3, lv3 = _st(-spread3, 2.0, 1.0) if spread3 is not None else ("⚪", "数据不足")
    chains.append({
        "id": "policy_divergence", "name": "货币政策分化（中美利差）",
        "logic": "中美10Y利差倒挂 → 资本外流压力 → 人民币承压 → 央行降息空间受限",
        "value": spread3,
        "value_text": f"CN10Y {cn10:.2f}% − US10Y {us10:.2f}% = {spread3:+.2f}pp" if spread3 is not None else "缺数据",
        "status": ic3 + " " + lv3,
        "details": [f"中国10Y {cn10:.3f}%（中债，快照）", f"美国10Y {us10:.2f}%（FRED，快照）"] if spread3 is not None else [],
        "implication": "倒挂越深，降息越受汇率约束，宽松需要'精准滴灌'而非大水漫灌" if spread3 is not None else "",
    })

    # 链4 经济动能：中美 PMI + 美国初请（高频替补金十停更的 ISM）
    cn_pmi, us_pmi = cn.get("cn_pmi"), None
    try:  # 美国 ISM（金十，实测 2025-09 起停更——仅作参考，主力换 ICSA 周频）
        import akshare as ak
        df = ak.macro_usa_ism_pmi().dropna(subset=["今值"])
        r = df.tail(1).iloc[0]
        us_pmi = (float(r["今值"]), str(r["日期"]))
    except Exception:
        pass
    ic = us.get("us_icsa")
    ic4v = us.get("us_icsa_avg4")
    both = cn_pmi and us_pmi
    if both:
        same_up = cn_pmi[0] >= 50 and us_pmi[0] >= 50
        both_down = cn_pmi[0] < 50 and us_pmi[0] < 50
        ic4_ = "🟢" if same_up else ("🔴" if both_down else "🟡")
        lv4 = "同步扩张" if same_up else ("同步收缩" if both_down else "分化")
    else:
        ic4_, lv4 = "⚪", "数据不足"
    chains.append({
        "id": "momentum", "name": "经济动能对比（PMI + 初请）",
        "logic": "中美 PMI 同步性 → 全球周期方向；美国初请失业金（周频）是动能的高频验证器",
        "value": None,
        "value_text": (f"中国 {cn_pmi[0]}（{cn_pmi[1]}）vs 美国 ISM {us_pmi[0]}（{us_pmi[1]}，金十停更仅参考）"
                       + (f"；美国初请 {ic[0]}（4周均值 {ic4v}）" if ic and ic4v else "")
                       if both else
                       (f"中国 {cn_pmi[0]}（{cn_pmi[1]}）" + (f"；美国初请 {ic[0]}（4周均值 {ic4v}）" if ic and ic4v else "")
                        if cn_pmi else "缺数据")),
        "status": ic4_ + " " + lv4,
        "details": ([f"中国制造业PMI {cn_pmi[0]}（{cn_pmi[1]}，统计局官网）"] if cn_pmi else []) +
                   ([f"美国ISM制造业PMI {us_pmi[0]}（{us_pmi[1]}，金十源停更，仅作历史对照）"] if us_pmi else []) +
                   ([f"美国初请失业金 {ic[0]}（{ic[1]}，FRED，4周均值 {ic4v}）"] if ic and ic4v else []),
        "implication": "同扩张利好全球风险资产；初请持续抬升=美国动能降温先兆" if cn_pmi else "",
    })

    # 链5 日本套息 = 引信（慢：利差 + COT净空）× 火（快：升值速度 + 波动率）
    # 2026-09-09 重写。旧口径只建模「引信长度」，且日元升值时反而扣分（方向反了），
    # 于是真实平仓发生时指数下降——正是今天（日元破 153）会踩的坑。现拆成两个量：
    #   tinder 引信 — 燃料堆了多少、还能烧多久。慢变量，月/周级。
    #   unwind 火   — 现在烧得旺不旺。快变量，日级。判断「正在平仓」只能看它。
    us10v = snaps.get("fred:UST10Y")
    jgb_v = jgb_d = None
    try:
        jgb = fred_window("IRLTLT01JPM156N", 6)     # 日本10Y国债（OECD月频）
        jgb_v, jgb_d = jgb[-1][1], jgb[-1][0]
    except Exception as e:
        print(f"    [warn] 日债10Y: {str(e)[:50]}")

    ser, ser_src = jpy_series(45)
    m5, m20 = jpy_move(ser, 5), jpy_move(ser, 20)
    vol = jpy_realized_vol(ser, 20)

    def _c01(x, cap):
        """clamp 到 [0, cap] 再归一到 [0,1]；None 直传。"""
        return None if x is None else min(max(x, 0.0), cap) / cap

    tinder = spread5 = None
    if us10v is not None and jgb_v is not None:
        spread5 = us10v - jgb_v
        sp = _c01(spread5, CARRY_SPREAD_CAP)
        net_v = snaps.get("cot:JPY_LEV_NET")
        if net_v is not None:
            crowd = _c01(-float(net_v), JPY_NET_SHORT_CAP)   # 净空（负）越深 = 燃料越多
            tinder = 0.6 * sp + 0.4 * crowd
        else:
            tinder = sp                                      # 无 COT 时退化为纯利差口径

    parts, ud = [], []
    if m5 is not None:
        parts.append((0.40, _c01(-m5, JPY_APP_5D_CAP)))      # 日元升值 = m5 为负
        ud.append(f"5日{'升值' if m5 < 0 else '贬值'} {abs(m5):.2f}%")
    if m20 is not None:
        parts.append((0.35, _c01(-m20, JPY_APP_20D_CAP)))
        ud.append(f"20日{'升值' if m20 < 0 else '贬值'} {abs(m20):.2f}%")
    if vol is not None:
        parts.append((0.25, _c01(vol, JPY_VOL_CAP)))
        ud.append(f"已实现波动率 {vol:.1f}%（年化）")
    unwind = (sum(w * v for w, v in parts) / sum(w for w, _ in parts)) if parts else None

    if tinder is None and unwind is None:
        chains.append({"id": "carry_trade", "name": "日元套息风险", "logic": "…",
                       "value": None, "value_text": "缺数据", "status": "⚪ 数据不足",
                       "details": [], "implication": ""})
    else:
        lvl = max(tinder or 0.0, unwind or 0.0)
        ic5 = "🔴" if lvl >= 0.6 else ("🟡" if lvl >= 0.3 else "🟢")
        lv5 = "高" if lvl >= 0.6 else ("偏高" if lvl >= 0.3 else "正常")
        tv = f"{tinder:.2f}" if tinder is not None else "—"
        uv = f"{unwind:.2f}" if unwind is not None else "—"
        details = [
            f"引信 {tv}（慢）= 美日利差×0.6 + COT净空拥挤×0.4；"
            f"平仓强度 {uv}（快）= 5日升值×0.4 + 20日升值×0.35 + 波动率×0.25",
            "日元序列：" + "、".join(ud) + f"（口径 {ser_src}，样本 {len(ser)} 天）",
        ]
        if spread5 is not None:
            details.append(f"美国10Y {us10v:.2f}% − 日本10Y {jgb_v:.2f}%（{jgb_d}，OECD月频）"
                           f"= 利差 {spread5:+.2f}pp")
        else:
            details.append("美日利差缺（日债10Y 取不到）→ 引信项缺失")
        net_v = snaps.get("cot:JPY_LEV_NET")
        if net_v is not None:
            details.append(f"COT 杠杆基金(投机盘)净持仓 {float(net_v):+,.0f} 手"
                           f"（负=净空=燃料；周频，滞后约1周）")
        chains.append({
            "id": "carry_trade", "name": "日元套息风险",
            "logic": "低日息+日元贬值 → 借日元买全球资产 → 套息膨胀（引信）；日元快速反弹+波动率抬升 → 平仓潮（火）",
            "value": round(lvl, 3),
            "value_text": f"引信 {tv} / 平仓强度 {uv}",
            "status": ic5 + " " + lv5,
            "details": details,
            "implication": "引信高=燃料多但还没烧；平仓强度高=正在烧。两者同高才是最危险的时刻",
        })

    # 链6 中国流动性脉冲：社融增量滚动12月同比（人民银行官网，领先指标）
    sf_lat, sf_r12 = cn.get("cn_sf_latest"), cn.get("cn_sf_r12")
    if sf_lat:
        if sf_r12:
            r12, yoy = sf_r12
            ic6 = "🟢" if yoy >= 5 else ("🟡" if yoy >= -5 else "🔴")
            lv6 = "融资发力" if yoy >= 5 else ("平稳" if yoy >= -5 else "收缩")
            chains.append({
                "id": "cn_credit_pulse", "name": "中国流动性脉冲（社融）",
                "logic": "社融增量是实体融资需求的领先指标：滚动12月收缩 → 盈利下修 → A股分子端承压；政府债放量支撑=财政主导而非内生需求",
                "value": round(yoy, 2),
                "value_text": f"滚动12月社融增量 {r12:,.0f} 亿，同比 {yoy:+.1f}%；最新月 {sf_lat[0]:,.0f} 亿（{sf_lat[1]}）",
                "status": ic6 + " " + lv6,
                "details": [f"最新月社融增量 {sf_lat[0]:,.0f} 亿元（{sf_lat[1]}，人民银行官网）",
                            f"滚动12月合计 {r12:,.0f} 亿元，同比 {yoy:+.1f}%（自算）"],
                "implication": "同比转负=紧信用，利空顺周期；正增长且加速=宽信用传导，利好 A 股分子端",
            })
        else:
            chains.append({
                "id": "cn_credit_pulse", "name": "中国流动性脉冲（社融）",
                "logic": "社融增量是实体融资需求的领先指标",
                "value": None,
                "value_text": f"最新月 {sf_lat[0]:,.0f} 亿（{sf_lat[1]}）；滚动12月同比缺上年数据",
                "status": "🟡 平稳（口径不全）",
                "details": [f"最新月社融增量 {sf_lat[0]:,.0f} 亿元（{sf_lat[1]}，人民银行官网）"],
                "implication": "",
            })
    else:
        chains.append({"id": "cn_credit_pulse", "name": "中国流动性脉冲（社融）",
                       "logic": "…", "value": None, "value_text": "缺数据",
                       "status": "⚪ 数据不足", "details": [], "implication": ""})
    return chains


# ---------------------------------------------------------------- 主流程

def load_snap_with_delta():
    """snapshots.csv 最新值+变动+持续天数 → {key: (value, delta, streak)}（供异动榜）。"""
    sp = os.path.join(DATA, "snapshots.csv")
    out = {}
    if not os.path.exists(sp):
        return out
    with open(sp, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                v = float(r["value"])
                d = float(r["delta"]) if r["delta"] not in ("", None) else None
                st = int(float(r["streak"])) if r["streak"] not in ("", None) else None
            except (TypeError, ValueError):
                continue
            out[r["key"]] = (v, d, st)
    return out


MOVE_WATCH = [
    "xyz:NVDA", "xyz:AMD", "xyz:TSM", "xyz:BABA", "xyz:COIN", "xyz:XYZ100",
    "xyz:JPY", "xyz:JP225", "xyz:BRENTOIL", "xyz:GOLD",
    "BTC", "ETH",
    "fred:UST10Y", "fred:UST2Y", "fred:VIX", "fred:HY_OAS", "fred:IG_OAS",
    "fred:T10YIE", "fred:WTI", "idx:HSI", "idx:MOVE", "idx:DXY",
    "cm:GOLD_FUT", "cm:COPPER_FUT", "vix:3M", "fx:USDCNH", "fx:USDCNY", "bond:CN10Y",
]
# 2026-09-15：移除 xyz:DXY / xyz:VIX —— 两者是 Hyperliquid 零成交死标的，
#   已被 world_feed 的流动性过滤剔除（不再落盘）。DXY 改走 Yahoo idx:DXY。
MOVE_NAMES = {
    "xyz:NVDA": "英伟达(永续)", "xyz:AMD": "AMD(永续)", "xyz:TSM": "台积电(永续)",
    "xyz:BABA": "阿里(永续)", "xyz:COIN": "Coinbase(永续)", "xyz:XYZ100": "纳斯达克100(永续)",
    "xyz:JPY": "日元(永续)", "xyz:JP225": "日经(永续)", "xyz:BRENTOIL": "布油(永续)",
    "xyz:GOLD": "黄金(永续)",
    "BTC": "比特币", "ETH": "以太坊",
    "fred:UST10Y": "美债10Y", "fred:UST2Y": "美债2Y", "fred:VIX": "VIX现货",
    "fred:HY_OAS": "高收益利差", "fred:IG_OAS": "投资级利差", "fred:T10YIE": "盈亏平衡通胀",
    "fred:WTI": "WTI原油", "idx:HSI": "恒生", "idx:MOVE": "MOVE", "idx:DXY": "美元指数",
    "cm:GOLD_FUT": "黄金期货", "cm:COPPER_FUT": "铜期货", "vix:3M": "VIX 3月(期限结构)",
    "fx:USDCNH": "离岸人民币", "fx:USDCNY": "在岸人民币", "bond:CN10Y": "中债10Y",
}


def load_pm_rate_events():
    """读 pm_markets.csv 最新快照里的 Fed/利率事件概率。
    返回 [(question, yes, chg)]；空列表=本次快照没有利率事件（这本身就是重要信息）。"""
    pp = os.path.join(DATA, "pm_markets.csv")
    if not os.path.exists(pp):
        return []
    rows = list(csv.DictReader(open(pp, encoding="utf-8")))
    if not rows:
        return []
    ts = max(r.get("ts") or "" for r in rows)
    out = []
    for r in rows:
        if r.get("ts") != ts:
            continue
        q = (r.get("question") or "").upper()
        if any(w in q for w in ("FED", "RATE", "CUT", "HIKE", "DECISION", "RECESSION", "CPI")):
            try:
                out.append((r["question"], float(r["yes_price"]),
                            float(r["chg_1d"]) if r.get("chg_1d") not in ("", None) else None))
            except (TypeError, ValueError):
                pass
    return out


def data_freshness():
    """各关键源最新数据日期，给 AI 的'时效边界'。

    ⚠️ 2026-09-10 修复：原实现**只读 history.csv**（backfill.py 生成的静态文件），
    但正文里的"现值"是从 FRED API 现拉的 —— 两者不同步会让 asof 日期**虚标滞后**。
    实测：高收益利差现值 2.67 实为 09-08 观测，asof 却仍报 09-03，凭空多出 5 天，
    导致 LLM 和我都误以为"数据过期 7 天、结论不可信"，过度自我怀疑。
    现在对 FRED 系列改用 API 实际最新观测日覆盖（history.csv 只作兜底）。

    ⚠️ 2026-09-15 再修：新增 data/asof.json（world_feed 采集时写）作为**首选**来源。
    原因：VIX/WTI 的现值已改由 Yahoo 提供，若继续用 FRED 的 VIXCLS/DCOILWTICO 观测日
    去覆盖，会把当天的新值虚标成"滞后 1~4 天"——与 09-10 修的那个 bug 是同一类
    （用另一个源的日期去标注本源的现值）。优先级改为：
        asof.json（采集时落的真实数据日） > FRED API 实际观测日 > history.csv"""
    latest = {}
    # 0) asof.json —— 采集时记录的真实数据日期，首选
    ap = os.path.join(DATA, "asof.json")
    if os.path.exists(ap):
        try:
            for k, v in json.load(open(ap, encoding="utf-8")).items():
                if isinstance(v, dict) and v.get("date"):
                    latest[k] = str(v["date"])[:10]
        except Exception:
            pass
    # 1) history.csv 兜底
    hp = os.path.join(DATA, "history.csv")
    if os.path.exists(hp):
        with open(hp, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                k = r["key"]
                if r["date"] > latest.get(k, ""):
                    latest[k] = r["date"]
    # 2) FRED API 实际观测日覆盖（fred:VIX / fred:WTI 已改源，不再用 FRED 日期覆盖）
    for _k, _sid in (("fred:UST2Y", "DGS2"), ("fred:UST10Y", "DGS10"),
                     ("fred:HY_OAS", "BAMLH0A0HYM2"), ("fred:IG_OAS", "BAMLC0A0CM")):
        try:
            pts = [p for p in fred_window(_sid, 12) if p[1] is not None]
            if pts:
                d = max(d for d, _v in pts)
                if d > latest.get(_k, ""):
                    latest[_k] = d
        except Exception:
            pass
    return latest


# ------------------------------------------------------------ 历史分位（补历史，不只喂截面）

# 「现值与历史同口径」的一组：VIX 的 FRED VIXCLS 与 Yahoo ^VIX 是同一个 CBOE 指数，
# 所以可以用 FRED 拉一年历史、再用快照真值补尾，不会串口径。
PCT_KEYS = [
    ("fred:UST2Y", "DGS2", "美债2Y"), ("fred:UST10Y", "DGS10", "美债10Y"),
    ("fred:UST10Y2Y", "T10Y2Y", "10Y-2Y利差"), ("fred:HY_OAS", "BAMLH0A0HYM2", "高收益利差"),
    ("fred:IG_OAS", "BAMLC0A0CM", "投资级利差"), ("fred:T10YIE", "T10YIE", "盈亏平衡通胀"),
    ("fred:VIX", "VIXCLS", "VIX"),
]
# ⚠️ WTI 不能走上面那条路：FRED DCOILWTICO 是 EIA **现货**、快照已改用 Yahoo CL=F **期货**，
# 两者差一个基差（2026-09 实测同期 0.5$ 上下），再叠加 4 个交易日的发布滞后，
# 把期货值塞进现货序列算分位 = 把基差和时差读成"行情" → 整条序列必须改走 Yahoo。
# (Yahoo symbol, 快照 key, 显示名)
PCT_YAHOO = [("CL%3DF", "fred:WTI", "WTI")]


def _yahoo_year_closes(sym):
    """Yahoo 一年日线收盘 → [(date, value)] 升序。sym 需 URL 编码（CL=F 写作 CL%3DF）。"""
    now_s = int(time.time())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
           f"?period1={now_s - 370 * 86400}&period2={now_s}&interval=1d")
    j = _open(url, proxy=CLASH_PROXY)
    r = j["chart"]["result"][0]
    out = []
    for t, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"]):
        if c is not None:
            out.append((datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"), c))
    return sorted(out)


def _percentile(cur, window):
    below = sum(1 for v in window if v <= cur)
    return round(below / len(window) * 100)


def calc_percentiles():
    """近 1 年历史分位：FRED 7 序列 + WTI(Yahoo 期货) + 铜金比(Yahoo 期货日线)。
    让 AI 说"历史高位/低位"时有据可查，而不是拍脑袋。返回 [(名称, 现值, 分位, 样本数)]。

    2026-09-15 修：现值一律取**快照真值**，不再用 FRED 序列尾值。
    原事故：VIX 在 0.65 节显示 15.84（FRED VIXCLS 尾值，滞后 1 个交易日），
    而同一份 ai_daily.md 的异动榜是 17.10（Yahoo 快照）—— 同一指标在同一份文件里两个值，
    写稿 AI 大概率引用那个戴着"近1年分位23%"权威外壳的旧值。
    """
    out = []
    snaps = load_snap_latest()
    cutoff = (datetime.now(CST) - timedelta(days=365)).strftime("%Y-%m-%d")

    # A) FRED 历史 + 快照真值补尾（限「历史与现值同口径」的序列）
    for disp, sid, cn in PCT_KEYS:
        try:
            pts = fred_window(sid, 550)
            window = [v for d, v in pts if d >= cutoff]
            if len(window) < 100:
                continue
            cur = snaps.get(disp)
            if cur is None:
                cur = window[-1]                      # 快照缺失 → 退回 FRED 尾值（自检会报滞后）
            elif abs(cur - window[-1]) > 1e-9:
                window = window + [cur]               # FRED 未出这天：用快照真值补尾，防现值/分位脱节
            out.append((cn, round(cur, 4), _percentile(cur, window), len(window)))
        except Exception as e:
            print(f"    [warn] 分位 {cn}: {str(e)[:50]}")

    # B) Yahoo 历史序列（现值同样取快照真值——否则"分位节 102.47 / 异动榜 102.75"又是两个值）
    for sym, key, cn in PCT_YAHOO:
        try:
            pts = _yahoo_year_closes(sym)
            window = [v for d, v in pts if d >= cutoff]
            if len(window) < 100:
                print(f"    [warn] 分位 {cn}: 样本不足（{len(window)} 期）")
                continue
            cur = snaps.get(key)
            if cur is None:
                cur = window[-1]
            elif abs(cur - window[-1]) > 1e-9:
                window = window + [cur]
            out.append((cn, round(cur, 4), _percentile(cur, window), len(window)))
        except Exception as e:
            print(f"    [warn] 分位 {cn}: {str(e)[:50]}")
    # 铜金比：Yahoo 期货 1 年日线
    try:
        import json as _json
        import time as _time
        now_s = int(_time.time())
        for sym, key in (("GC%3DF", "gold"), ("HG%3DF", "copper")):
            url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
                   f"?period1={now_s - 370*86400}&period2={now_s}&interval=1d")
            j = _open(url, proxy=CLASH_PROXY)
            r = j["chart"]["result"][0]
            closes = {t: c for t, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"])
                      if c is not None}
            globals()[f"_pct_{key}"] = closes   # 暂存，下一步按日期对齐
        g, c = globals().get("_pct_gold", {}), globals().get("_pct_copper", {})
        common = sorted(set(g) & set(c))
        if len(common) >= 200:
            ratios = [c[t] / g[t] for t in common]
            out.append(("铜金比", round(ratios[-1], 4), _percentile(ratios[-1], ratios), len(ratios)))
    except Exception as e:
        print(f"    [warn] 铜金比分位: {str(e)[:50]}")
    return out


def market_pulse():
    """市场在讨论/交易什么：Polymarket 变动榜 + ApeWisdom 讨论度 + HL 资金费率极值。
    这些数据我们天天采，此前从没喂给写稿 AI——它当然只会对着宏观数字打转。"""
    out = {"pm_movers": [], "social": [], "funding": [], "pm_ts": None}
    # 1) Polymarket 概率变动榜（全部命中市场，不只利率）
    pp = os.path.join(DATA, "pm_markets.csv")
    if os.path.exists(pp):
        rows = list(csv.DictReader(open(pp, encoding="utf-8")))
        if rows:
            ts = max(r.get("ts") or "" for r in rows)
            latest = [r for r in rows if r.get("ts") == ts]
            ev = []
            for r in latest:
                try:
                    ev.append((r["question"], float(r["yes_price"]),
                               float(r["chg_1d"]) if r.get("chg_1d") not in ("", None) else 0.0))
                except (TypeError, ValueError):
                    pass
            ev.sort(key=lambda x: abs(x[2]), reverse=True)
            out["pm_movers"] = ev[:5]
            out["pm_ts"] = ts
    # 2) ApeWisdom 讨论度 top（rank 越小越热）
    sp = os.path.join(DATA, "snapshots.csv")
    if os.path.exists(sp):
        soc = {}
        with open(sp, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["source"] != "social":
                    continue
                k = r["key"]
                if ":rank" in k:
                    t = k.replace("social:", "").replace(":rank", "")
                    try:
                        soc.setdefault(t, {})["rank"] = float(r["value"])
                    except (TypeError, ValueError):
                        pass
                elif ":mentions" in k:
                    t = k.replace("social:", "").replace(":mentions", "")
                    soc.setdefault(t, {})["mentions"] = r["value"]
        top = sorted((v.get("rank", 999), t, v.get("mentions", "-")) for t, v in soc.items())
        out["social"] = [(t, rk, m) for rk, t, m in top[:8]]
    # 3) HL 资金费率极值（拥挤度；09-08 起才持久化，之前无数据）
    fund = []
    for k, (v, d, st) in load_snap_with_delta().items():
        if k.endswith(":funding") and isinstance(v, float):
            fund.append((k.replace(":funding", "").replace("xyz:", ""), v))
    fund.sort(key=lambda x: abs(x[1]), reverse=True)
    # 1b) Polymarket 24h 概率曲线（小时级）：单点快照只反映采集那一刻，
    #     曲线才看得出「两次采集之间发生了什么」。2026-09-10 事故后新增。
    cpath = os.path.join(DATA, "pm_curve.csv")
    if os.path.exists(cpath):
        try:
            crows = list(csv.DictReader(open(cpath, encoding="utf-8")))
            if crows:
                cts = max(r.get("ts") or "" for r in crows)
                out["pm_curve"] = [r for r in crows if r.get("ts") == cts]
        except Exception:
            pass
    out["funding"] = fund[:5]
    return out


def build_ai_pack(out):
    """生成给 AI 的日更闭环：
    1) ai_memory.jsonl 追加今日机器状态（判级/净流动性/ESI/异动）——决策记忆，永续滚动
    2) ai_daily.md = 使用指令 + 判级 diff + 异动榜 + 今日完整状态 + 最近7天记忆
    AI 每天读这一份就能"不丢环境"地延续解读；它输出的结论由人贴回 ima/收藏，或下期人工注入。"""
    snaps = load_snap_with_delta()
    # 异动榜：|delta| 降序，取 >0.8% 的前 6；带 streak（连续同向天数）——单日波动≠趋势
    moves = []
    for k in MOVE_WATCH:
        if k in snaps and snaps[k][1] is not None:
            moves.append((MOVE_NAMES.get(k, k), snaps[k][1], snaps[k][0], snaps[k][2]))
    moves.sort(key=lambda x: abs(x[1]), reverse=True)
    moves = [(n, d, v, st) for n, d, v, st in moves if abs(d) > 0.008][:6]

    today = out["generated"][:10]
    state = {
        "date": today,
        "chains": {c["id"]: c["status"] for c in out["chains"]},
        "net_liq_t": (out.get("net_liquidity") or {}).get("net_t"),
        "esi_us": (out.get("esi") or {}).get("esi", {}).get("US"),
        "esi_cn": (out.get("esi") or {}).get("esi", {}).get("CN"),
        "moves": [(n, round(d * 100, 2)) for n, d, _, _ in moves],
    }
    # 1) 追加决策记忆（同日重跑先删旧行）
    rows = []
    if os.path.exists(AI_MEMORY):
        with open(AI_MEMORY, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip() and not l.startswith("#")]
    rows = [r for r in rows if r.get("date") != today]
    rows.append(state)
    with open(AI_MEMORY, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 2) 判级 diff（vs 上一条记忆）
    prev = rows[-2] if len(rows) >= 2 else None
    L = []
    L.append(f"# AI 宏观日更 · {today}")
    L.append("")
    L.append("> 这份文件是给 AI 的（人看不懂没关系，人看 worldview_daily.md）。")
    L.append("> 把全文粘给任意 LLM，它就能带着历史上下文做今日解读——不丢环境。")
    L.append("")
    L.append("## 0. 你的任务（AI 请照做）")
    L.append("你是全球宏观解读助手。基于下面几节：")
    L.append("1. 对比「判级变化」和「异动榜」，指出今天真正的重点（没有就明说'今日无实质变化'）")
    L.append("2. 对每条判级变化给出'为什么变'或'判级是否合理'")
    L.append("3. 输出 ≤3 句白话结论（给人看的，别用术语堆砌）")
    L.append("4. 指出未来 1-2 周最值得盯的 1-2 个数据点（用本文出现的指标名）")
    L.append("5. **专门写一段「市场在关注什么」**：以「市场脉搏」节的概率变动榜、讨论度榜、资金费率为据——"
             "这是文章主线之一，不许只围绕宏观数字打转。历史分位节用来判断'现在是高是低'，别再拍脑袋。")
    L.append("")
    L.append("## 0.5 硬性写作纪律（违反即废稿）")
    L.append("- **时效边界**：各数据的最新日期见「数据时效」节。禁止对数据日期之后的行情做任何断言；"
             "滞后≥2个交易日的读数，正文引用时必须注明'截至X日的滞后读数'。")
    L.append("- **单日≠趋势**：异动榜的变动附有连续天数(streak)。streak=±1 就是单日波动，"
             "禁止据单日变动断言'市场在交易/定价X'——那最多是你的推测，要标注。")
    L.append("- **定价断言必须有概率数据**：断言'市场在定价降息/加息/衰退'必须有真实概率数据支撑。"
             "优先引用「市场概率」节的 Polymarket 数据；该节为空时，**可以引用新闻里的 CME FedWatch、"
             "利率期货等公开定价数据（须注明来源）**；两者都没有时，才写"
             "'手头无 Fed 定价数据，无法判断市场预期方向'。禁止仅凭收益率曲线形态断言市场预期。")
    L.append("")
    # 新闻背景层：没有这一层，AI 只能对着数字脑补，会写出品种混淆这类逻辑扭曲。
    # 2026-09-10 实证：缺少背景时，AI（和人）把「布伦特破 100」与「押 WTI 摸 100 的概率」
    # 当成同一件事写，读者看成「都 101 了还在猜会不会到 100」，逻辑完全扭曲。
    _ncp = os.path.join(DATA, "news_context.md")
    if os.path.exists(_ncp):
        try:
            with open(_ncp, encoding="utf-8") as _f:
                _news = _f.read().strip()
            if _news:
                L.append("## 0.55 今日新闻背景（世界发生了什么——写稿前必读）")
                L.append("")
                L.append("> 本節是事实层：谁在何时做了什么、数是多少。禁止凭数字脑补背景。")
                L.append("> 观点由你基于事实形成；若你的判断与本節事实冲突，以本節为准或显式说明分歧。")
                L.append("")
                L.append(_news)
                L.append("")
        except Exception:
            pass
    L.append("## 0.6 数据时效（各源最新数据日期，滞后即边界）")
    L.append("")
    fresh = data_freshness()
    warn_lag = []
    for k in ("fred:UST2Y", "fred:UST10Y", "fred:HY_OAS", "cm:GOLD_FUT", "idx:HSI",
              "bond:CN10Y", "fred:VIX", "fred:WTI", "vix:3M"):
        if k in fresh:
            lag = (datetime.now(CST).date() - datetime.strptime(fresh[k], "%Y-%m-%d").date()).days
            tag = f"（滞后 {lag} 天，警惕过期）" if lag >= 2 else ""
            if lag >= 2:
                warn_lag.append(k)
            L.append(f"- {MOVE_NAMES.get(k, k)}：截至 {fresh[k]}{tag}")
    L.append("")
    L.append("## 0.7 市场概率（Polymarket 利率/衰退相关事件）")
    L.append("")
    pm_ev = load_pm_rate_events()
    if pm_ev:
        for q, y, ch in pm_ev:
            chs = f"（24h {ch:+.3f}）" if ch is not None else ""
            L.append(f"- YES={y:.3f}{chs}  {q}")
    else:
        L.append("- **本次快照未筛到 Fed/利率/衰退事件**（Polymarket 取 24h 成交额榜后按关键词筛，"
                 "未命中 FED/RATE/CUT/HIKE/DECISION/RECESSION/CPI）。"
                 "⚠️ 这表示「**没抓到**」，**不等于「市场没有这类定价」**——公开源（CME FedWatch、"
                 "利率期货、新闻）里可能仍有。写作时可以说'本管线无 Fed 定价数据'，"
                 "但不能引申为'市场没有预期'或'预期没变化'。")
    L.append("")
    L.append("## 0.65 历史分位（近1年，说高低用这个，别拍脑袋）")
    L.append("")
    try:
        pcts = calc_percentiles()
        out["percentiles"] = [(cn, v, p, n) for cn, v, p, n in pcts]
        for cn, v, p, n in pcts:
            lv = "高位" if p >= 80 else ("低位" if p <= 20 else "中位")
            L.append(f"- {cn}：现值 {v}，近1年分位 **{p}%**（{lv}，样本 {n} 期）")
        if not pcts:
            L.append("- （分位计算失败）")
    except Exception as e:
        L.append(f"- （分位计算失败：{str(e)[:60]}）")
    L.append("")
    L.append("## 0.8 市场脉搏（市场在讨论/交易什么——文章主线之一）")
    L.append("")
    try:
        pulse = market_pulse()
        out["market_pulse"] = {k: v for k, v in pulse.items()}
        _pm_ts = pulse.get("pm_ts") or ""
        if _pm_ts:
            try:
                _age = (datetime.now() - datetime.strptime(_pm_ts, "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600.0
            except Exception:
                _age = 0.0
            if _age > 12:
                L.append(f"> ⚠️ **数据陈旧警告**：下列概率来自 **{_pm_ts}** 的快照，距今 **{_age:.1f} 小时**——"
                         f"本次采集失败（几乎总是 Clash 未运行导致 Polymarket 不可达）。")
                L.append(f"> 这些数字**不是今日定价**。写「市场在关注什么」时：引用本节必须显式写明数据时间；"
                         f"任何依赖本节的判级必须先声明「基于 {_age:.0f} 小时前的旧数据」；"
                         f"**禁止**把旧概率当作市场当下的即时注意力来写。")
                L.append("")
        L.append("**Polymarket 概率变动榜（24h）：**")
        if pulse["pm_movers"]:
            for q, y, ch in pulse["pm_movers"]:
                L.append(f"- {ch:+.3f}  YES={y:.3f}  {q}")
        else:
            L.append("- 无数据")
        L.append("")
        if pulse.get("pm_curve"):
            L.append("**Polymarket 24h 概率曲线（小时级，补单点快照之不足）：**")
            for r in pulse["pm_curve"][:6]:
                try:
                    L.append(f"- {str(r.get('question'))[:58]}：24h前 {float(r['p_24h_ago']):.3f}"
                             f" → 现在 {float(r['p_now']):.3f}（区间 {float(r['p_min']):.3f}"
                             f"~{float(r['p_max']):.3f}，振幅 {float(r['p_range']):+.3f}，"
                             f"峰值 {r.get('peak_ts')}）")
                except (TypeError, ValueError):
                    continue
            L.append("")
        L.append("**ApeWisdom 讨论度 TOP（散户注意力，rank 小=热）：**")
        if pulse["social"]:
            for t, rk, m in pulse["social"]:
                L.append(f"- {t}（rank {rk:.0f}，提及 {m}）")
        else:
            L.append("- 无数据")
        L.append("")
        L.append("**HL 资金费率极值（|值|大=该方向拥挤）：**")
        if pulse["funding"]:
            for t, v in pulse["funding"]:
                L.append(f"- {t}: {v:+.5f}")
        else:
            L.append("- 暂无（资金费率 09-08 起才持久化，几天后可用）")
    except Exception as e:
        L.append(f"- （市场脉搏失败：{str(e)[:60]}）")
    L.append("")
    L.append("## 0.9 A线知情钱（Hyperliquid 鲸鱼，云端 Actions 每~3-4h 快照，whale_link 拉取）")
    L.append("")
    wp = os.path.join(DATA, "whale_latest.md")
    if os.path.exists(wp):
        try:
            L.append(open(wp, encoding="utf-8").read().strip())
        except Exception as e:
            L.append(f"- （鲸鱼快照读取失败：{str(e)[:60]}）")
    else:
        L.append("- 尚无数据（whale_link.py 首次运行后生成）")
    L.append("")
    L.append("## 1. 判级变化（vs 上一期）")
    L.append("")
    if prev:
        changed = 0
        for c in out["chains"]:
            old = prev.get("chains", {}).get(c["id"])
            if old and old != c["status"]:
                L.append(f"- **{c['name']}：{old} → {c['status']}**（{c['value_text']}）")
                changed += 1
            elif old:
                pass
        if changed == 0:
            L.append("- 全部无变化（判级与上期一致）")
    else:
        L.append("- （首期，无对照）")
    L.append("")
    L.append("## 2. 异动榜（快照内 |变动| > 0.8%，前 6；streak=连续同向天数，±1 即单日波动）")
    L.append("")
    if moves:
        for n, d, v, st in moves:
            sts = f"，streak {st:+d}" if st not in (None, 0) else "（首日，无趋势）"
            L.append(f"- {n}：{d:+.2%}（现值 {v}{sts}）")
    else:
        L.append("- 无超阈值异动")
    L.append("")
    L.append("## 3. 今日完整状态")
    L.append("")
    net = out.get("net_liquidity")
    if net:
        chg = f"，30日 {net['chg30_t']:+.2f}" if net.get("chg30_t") is not None else ""
        L.append(f"- 净流动性 {net['net_t']:.2f} 万亿$（{net['date']}）{chg}万亿$；Fed总资产 {net['walcl_t']:.2f} / RRP {net['rrp_b']:.3f} / TGA {net['tga_t']:.2f}")
    esi = out.get("esi")
    if esi:
        L.append(f"- 意外计（自建）：美国 {esi['esi']['US']:+.2f} / 中国 {esi['esi']['CN']:+.2f}（最近事件 {esi.get('last_event')}）")
    for c in out["chains"]:
        L.append(f"- [{c['status']}] {c['name']}：{c['value_text']}")
        for d in c["details"]:
            L.append(f"    · {d}")
    for d in (out.get("derived") or {}).values():
        L.append(f"- {d['name']} {d['value']}（{d['note']}）")
    L.append("")
    L.append("## 4. 最近 7 天记忆（机器自动累积，倒序）")
    L.append("")
    for r in sorted(rows, key=lambda x: x["date"], reverse=True)[:7]:
        mv = "，".join(f"{n}{d:+.1f}%" for n, d in (r.get("moves") or [])[:3]) or "无异动"
        net_s = f"{r['net_liq_t']:.2f}万亿" if r.get("net_liq_t") else "-"
        L.append(f"- **{r['date']}** 净流动性 {net_s}｜异动：{mv}｜判级：{' / '.join(r['chains'].values())}")
    L.append("")
    with open(AI_DAILY, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def main():
    os.makedirs(DATA, exist_ok=True)
    now = datetime.now(CST)
    print(f"\n=== 宏观逻辑链引擎 {now.strftime('%Y-%m-%d %H:%M')} (CST) ===\n")

    snaps = load_snap_latest()
    print("[0] 净流动性三件套（WALCL − RRP − TGA）")
    net = None
    try:
        net = calc_net_liq()
        if net:
            chg = f"{net['chg30_t']:+.2f} 万亿$/30天" if net.get("chg30_t") is not None else "-"
            print(f"    净流动性 {net['net_t']:.2f} 万亿$（{net['date']}）  Fed总资产 {net['walcl_t']:.2f}  RRP {net['rrp_b']:.3f}  TGA {net['tga_t']:.2f}  30日变化 {chg}")
    except Exception as e:
        print(f"    [fail] 净流动性: {str(e)[:70]}")

    print("\n[E] 宏观意外计（金十 今值vs预测值，Citi ESI 免费近似）")
    esi = None
    try:
        esi = calc_esi()
        if esi:
            print(f"    美国ESI {esi['esi']['US']:+.2f}  中国ESI {esi['esi']['CN']:+.2f}  （最近事件 {esi['last_event']}）")
            for line in esi["detail"]["US"][-3:]:
                print(f"      US: {line}")
            for line in esi["detail"]["CN"][-2:]:
                print(f"      CN: {line}")
    except Exception as e:
        print(f"    [fail] 意外计: {str(e)[:70]}")

    print("\n[1-5] 五条因果链")
    chains = calc_chains(snaps)
    for c in chains:
        print(f"    {c['status']}  {c['name']}：{c['value_text']}")
        for d in c["details"]:
            print(f"        · {d}")

    # ---- 派生信号（快照内自算，中文命名）----
    derived = {}
    # VIX 期限结构比（2026-09-15 替换原「VIX 永续−现货差」）
    # 原实现 = snaps["xyz:VIX"] − snaps["fred:VIX"]。但 xyz:VIX 是**零成交死标的**
    #   （openInterest=0、dayNtlVlm=0、midPx 为空），markPx 恒等于初始占位值 20.0 →
    #   算出的"永续溢价 4.16"纯属虚构，而报告还拿它解读成"赌风险升温"（同日 MOVE 才是真在动的那个）。
    # 正解：VIX 的正确对照物是**它自己的期限结构**（期货曲线形状）：
    #   VIX/VIX3M < 1 → 正挂 contango（近低远高，市场平静）
    #   VIX/VIX3M > 1 → 倒挂 backwardation（近高远低，恐慌避险溢价）
    # 原料由 world_feed 从 Yahoo ^VIX / ^VIX3M 采集（key: fred:VIX / vix:3M）。
    vix_spot, vix_3m = snaps.get("fred:VIX"), snaps.get("vix:3M")
    if vix_spot and vix_3m:
        ratio = vix_spot / vix_3m
        if ratio > 1:
            _vt_note = "倒挂=恐慌避险（近月高于远月，为当下风险付溢价）"
        elif ratio > 0.9:
            _vt_note = "正挂但差距收窄=情绪偏紧（近月低于远月）"
        else:
            _vt_note = "正挂=市场平静（近月低于远月，卖波动率环境）"
        derived["vix_term"] = {"name": "VIX/VIX3M 期限结构比",
                               "value": round(ratio, 3), "note": _vt_note}
    gold, copper = snaps.get("cm:GOLD_FUT"), snaps.get("cm:COPPER_FUT")
    if gold and copper:
        derived["cu_au"] = {"name": "铜金比", "value": round(copper / gold, 4),
                            "note": "全球增长预期代理，上行=risk-on"}
    sofr, fed = snaps.get("fred:SOFR"), snaps.get("fred:FEDFUNDS")
    if sofr and fed:
        derived["sofr_gap"] = {"name": "SOFR−联邦基金上限", "value": round(sofr - fed, 3),
                               "note": "走正=货币市场承压（repo 危机式先兆）"}
    cnh, cny = snaps.get("fx:USDCNH"), snaps.get("fx:USDCNY")
    if cnh and cny:
        derived["cny_gap"] = {"name": "在岸−离岸CNH差", "value": round(cny - cnh, 4),
                              "note": "缝撕开=央行干预痕迹"}
    move = snaps.get("idx:MOVE")
    if move:
        # 2026-09-15：原解读写作"VIX平+MOVE高 = 风险从股市向债市转移"——
        # 那个"VIX平"是假象：当时 VIX 取自 Hyperliquid 零成交永续（恒 20.0 不动）。
        # 改用真实 VIX 后它每天都在动，故本句改为**不预设状态**的中性口径，避免说错话。
        derived["move"] = {"name": "MOVE 债市波动率", "value": move,
                           "note": "债市隐含波动率；与 VIX 同步抬升=跨市场避险，仅它抬升=压力偏向债市"}
    if derived:
        print("\n[+] 派生信号")
        for d in derived.values():
            print(f"    {d['name']} = {d['value']}   ({d['note']})")

    out = {
        "generated": now.strftime("%Y-%m-%d %H:%M"),
        "net_liquidity": net,
        "esi": esi,
        "chains": chains,
        "derived": derived,
    }
    with open(CHAINS_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    write_llm_brief(out)
    build_ai_pack(out)
    print(f"\n已落盘：{CHAINS_JSON}\n          {LLM_BRIEF}\n          {AI_DAILY}（AI 日更简报 + ai_memory.jsonl 滚动记忆）")


def write_llm_brief(out):
    """生成给 LLM 的解读原料：全部关键数字 + 逻辑框架 + 推演任务。
    设计原则：数字自己带日期、带口径；推演任务开放式——最大限度利用 LLM 自身理解能力。"""
    L = []
    L.append(f"# 宏观解读原料 · {out['generated']} (CST)")
    L.append("")
    L.append("> 本文档是给 LLM 的推演原料：数据全部自动采集（源+日期+口径已标注），")
    L.append("> 请把它整段交给 Claude/ima/其他 LLM，让它基于以下数字做逻辑推演，不要让它重新找数。")
    L.append("")
    L.append("## 一、原始读数（全部免费公开源）")
    L.append("")
    net = out.get("net_liquidity")
    if net:
        L.append(f"- 净流动性 **{net['net_t']:.2f} 万亿$**（{net['date']}）= Fed总资产 {net['walcl_t']:.2f} − 逆回购 {net['rrp_b']:.3f} − TGA {net['tga_t']:.2f}"
                 + (f"，30日变化 {net['chg30_t']:+.2f} 万亿$" if net.get('chg30_t') is not None else ""))
    esi = out.get("esi")
    if esi:
        L.append(f"- 宏观意外计（自建，金十今值vs预测值，90天窗口）：美国 **{esi['esi']['US']:+.2f}**，中国 **{esi['esi']['CN']:+.2f}**（0=符合预期，正=强于预期）")
        for line in esi["detail"]["US"][-2:] + esi["detail"]["CN"][-2:]:
            L.append(f"  - {line}")
        L.append(f"  - 口径：{esi['note']}")
    for c in out["chains"]:
        L.append(f"- {c['name']}：**{c['value_text']}**（{c['status']}）")
        for d in c["details"]:
            L.append(f"  - {d}")
    for d in out.get("derived", {}).values():
        L.append(f"- {d['name']}：**{d['value']}**（{d['note']}）")
    L.append("")
    L.append("## 二、内置逻辑链（代码自动判级，请 LLM 审视判级是否合理并推演后续）")
    L.append("")
    for c in out["chains"]:
        L.append(f"### {c['status']} {c['name']}")
        L.append(f"- 逻辑：{c['logic']}")
        L.append(f"- 当前：{c['value_text']}")
        L.append(f"- 含义：{c['implication']}")
        L.append("")
    L.append("## 三、推演任务（交给 LLM）")
    L.append("")
    L.append("""请基于上面的数字回答（不要重新找数据，直接用给出的数字和日期）：
1. 五条链哪几条在互相加强？哪几条在互相抵消？（例：政策分化是否限制了中国对冲货币空转的工具）
2. 未来 4-6 周最可能先断/先爆的是哪条链？给出可被证伪的观察指标（用上面的 key 名）。
3. 对 A 股/港股/人民币/商品各给一个方向倾向 + 一句理由。
4. 指出本次数据里至少一个"与共识叙事矛盾"的点（如果有）。
5. 每条链给出你自己的置信度（0-100），与代码判级不一致处请明说。""")
    L.append("")
    with open(LLM_BRIEF, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


if __name__ == "__main__":
    main()
