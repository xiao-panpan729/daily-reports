"""
whale_link.py — 世界观项目 · A线→B线 鲸鱼快照连接器（2026-09-08 新增）

背景：
  A线 whale-station 跑在云端 GitHub Actions（每 ~3-4h 快照一次，commit 进仓库），
  本地 B线采集时顺手读一眼，让晨报能并排回答"鲸鱼做了什么 + 价格/费率在做什么"。

数据源：github.com/xiao-panpan729/worldview 仓库 **whale-data 分支** 内
  whale-station/last_snapshot.json   快照，v0.4 格式：
      {"pos": {"addr|coin|dir": {szi, entry, val, lev}}, "accs": {addr: 账户价值}}
      （旧格式为顶层直接是 pos；parse_pos 两种都认）
  （data/positions_history.csv 被 gitignore，不入库；latest_report.md 是人读版）
  2026-09-10 起：云端 Actions 只往 whale-data 分支写数据，main 归本机，两边永不打架。

git 策略（铁律适配）：
  只 `git fetch origin whale-data`（读远端）+ `git show FETCH_HEAD:...`（读远端分支内容），
  **绝不 pull/merge/checkout** —— 工作区有用户未提交改动，pull 会搅局。
  2026-09-06 教训：fetch 后 origin/main 引用可能建不起来，FETCH_HEAD 一定有 → 一律走 FETCH_HEAD。

diff 规则：
  与 data/whale_seen.json（上次本地观测缓存）比对：
  - 新建仓 / 平仓：val ≥ $50万 才报
  - 加仓 / 减仓：|Δval| ≥ $50万 且 |Δ| ≥ 20% 才报
  - 方向翻转（同地址同币种 LONG↔SHORT）：必报

降级：
  fetch 失败（断网/Clash 异常/GitHub 抽风）→ 打印警告、保留上次 whale_latest.md，退出码 0，
  绝不阻塞采集管线。快照成功才更新 whale_seen.json 缓存。

输出：
  data/whale_latest.md  供 macro_insight.build_ai_pack 并入 ai_daily（0.9 节）
"""
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
REPO = os.path.dirname(BASE)                      # D:\Worldview（git 主仓根）
SNAP_PATH = "whale-station/last_snapshot.json"
ROSTER = os.path.join(REPO, "whale-station", "05-知情钱画像名单.md")
FETCH_TIMEOUT = 90
MIN_NEW = 500_000                                 # 新建仓/平仓 报告阈值（$）
PCT_TH = 0.20                                     # 增减仓幅度阈值
HIST_DAYS = 10                                    # 历史脉络回看天数（10 天实测能覆盖一个完整行情波段）
HIST_MIN = 2_000_000                              # 历史脉络事件阈值（$，比实时告警高，防刷屏）
HIST_MAX = 8                                      # 历史脉络最多展示条数


def _git(*args):
    r = subprocess.run(["git", "-C", REPO, *args],
                       capture_output=True, timeout=FETCH_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace").strip()[:120])
    return r.stdout.decode("utf-8", "replace")


def _usd(v):
    """$12.6M / $834K / $167.6万 风格缩写（中文报告用 万/M 混排）"""
    v = float(v)
    if abs(v) >= 1e6:
        return f"${v/1e6:.2f}M"
    if abs(v) >= 1e4:
        return f"${v/1e4:.1f}万"
    return f"${v:.0f}"


def load_roster():
    """读 A线画像名单（05-知情钱画像名单.md），返回 {addr: (排名, 胜率%, 已实现盈亏$, 存活天)}
    名单表按已实现盈亏降序 → 行号即排名。给鲸鱼地址一个看得懂的中文身份。"""
    out = {}
    if not os.path.exists(ROSTER):
        return out
    rank = 0
    for line in open(ROSTER, encoding="utf-8"):
        line = line.strip()
        if not line.startswith("| 0x"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5 or "地址" in cells[0]:
            continue
        rank += 1
        try:
            out[cells[0]] = (rank, cells[2], float(cells[3].replace("$", "").replace(",", "")),
                             float(cells[4]))
        except ValueError:
            continue
    return out


def _label(addr, roster):
    """0xa2ce501d… → 知情钱#1（胜率66%·+$167.6万）；不在名单 → 未列名鲸鱼"""
    info = roster.get(addr)
    if info:
        rank, win, pnl, _days = info
        sign = "+" if pnl >= 0 else "-"
        return f"知情钱#{rank}（胜率{win}·累计盈亏{sign}{_usd(abs(pnl))}）"
    return f"未列名鲸鱼（{addr[:10]}…）"


def _dir_cn(d):
    return {"LONG": "多", "SHORT": "空"}.get(d, d)


def _ts_cn(ts):
    """2026-09-08T08:32:31Z → 09-08 16:32（北京时间）"""
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (dt + timedelta(hours=8)).strftime("%m-%d %H:%M") + "（北京时间）"
    except ValueError:
        return ts


def load_remote():
    """fetch + 从 FETCH_HEAD 读快照。返回 (positions_dict, snapshot_utc_str, fetch_ok)
    2026-09-10 起：快照数据由云端 Actions 写在 whale-data 分支（本机 main 不再有这些数据），
    所以 fetch 目标改成 whale-data；若失败再退回 main（兼容旧布局）。
    仍失败则降级读上次成功拉取的 FETCH_HEAD（本地留存的远端内容，标注稍旧仍比没有强）。"""
    fetch_ok = True
    try:
        _git("fetch", "origin", "whale-data", "--quiet")
    except Exception as e:
        try:
            _git("fetch", "origin", "--quiet")   # 降级：旧布局下数据还在 main
        except Exception as e2:
            fetch_ok = False
            print(f"    [warn] git fetch 失败，改用本地留存的最近一次远端快照：{str(e)[:80]}")
    raw = _git("show", f"FETCH_HEAD:{SNAP_PATH}")
    cur = json.loads(raw)
    try:
        subject = _git("log", "-1", "--format=%s", "FETCH_HEAD").strip()
        ts = subject.replace("whale snapshot", "").strip() or "unknown"
    except Exception:
        ts = "unknown"
    return cur, ts, fetch_ok


def parse_pos(cur):
    """{key: {val:float, dir, addr8, addr, coin}}；key 原样保留做集合比对

    兼容两种快照格式（2026-09-17 修）：
      - v0.4 新格式 {"pos": {持仓...}, "accs": {地址: 账户价值}}
        （云端 cloud_entry.py v0.4 起，为补"抽资/入金"盲区加的 accs 层）
      - 旧格式：顶层直接就是持仓 dict
    云端 cloud_entry.py / replay.py 都做了同样的双格式兼容，此处对齐。
    """
    if isinstance(cur, dict) and isinstance(cur.get("pos"), dict):
        cur = cur["pos"]                   # v0.4：剥离 pos 包裹层
    out = {}
    for k, v in cur.items():
        try:
            addr, coin, d = k.split("|")
            out[k] = {"val": float(v.get("val") or 0), "dir": d,
                      "addr8": addr[:10], "addr": addr, "coin": coin}
        except (ValueError, AttributeError):
            continue
    return out


def diff(cur, seen, min_new=MIN_NEW, pct_th=PCT_TH):
    """返回 [(kind, addr8, coin, dir_new, old_val, new_val)]，按 |Δval| 降序

    min_new / pct_th 可覆盖：历史脉络回放时用更高阈值，避免把日常小调仓全抖出来。
    """
    rows = []
    seen_dir = {}
    for k, v in seen.items():
        seen_dir.setdefault((v["addr8"], v["coin"]), []).append((k, v))
    cur_dir = {}
    for k, v in cur.items():
        cur_dir.setdefault((v["addr8"], v["coin"]), []).append((k, v))

    for k, v in cur.items():
        pair = (v["addr8"], v["coin"])
        olds = seen_dir.get(pair, [])
        old = next((s for sk, s in olds if sk == k), None)
        if old is None:
            # 新 key：新建仓 或 翻转（同地址同币种原本是另一方向）
            flip = next((s for sk, s in olds if s["dir"] != v["dir"]), None)
            if flip and v["val"] >= min_new:
                rows.append(("翻转", v["addr8"], v["coin"], v["dir"], flip["val"], v["val"]))
            elif v["val"] >= min_new:
                rows.append(("新建仓", v["addr8"], v["coin"], v["dir"], 0.0, v["val"]))
            continue
        dv = v["val"] - old["val"]
        if abs(dv) >= min_new and old["val"] != 0 and abs(dv / abs(old["val"])) >= pct_th:
            kind = "加仓" if dv > 0 else "减仓"
            rows.append((kind, v["addr8"], v["coin"], v["dir"], old["val"], v["val"]))

    for k, v in seen.items():
        if k not in cur and v["val"] >= min_new:
            rows.append(("平仓", v["addr8"], v["coin"], "-", v["val"], 0.0))

    rows.sort(key=lambda r: abs(r[5] - r[4]), reverse=True)
    return rows


def summarize(cur):
    """多空名义合计 + 地址数 + 第一大持仓集中度"""
    longs = sum(v["val"] for v in cur.values() if v["dir"] == "LONG")
    shorts = sum(v["val"] for v in cur.values() if v["dir"] == "SHORT")
    addrs = len({v["addr8"] for v in cur.values()})
    top = max(cur.values(), key=lambda v: v["val"]) if cur else None
    return longs, shorts, addrs, top


def load_history_events(days=HIST_DAYS, min_flow=HIST_MIN, max_rows=HIST_MAX):
    """回放 whale-data 分支近 N 天快照 → 跨期变动脉络 [(epoch, kind, addr8, coin, dir, old, new)]

    为什么要它：whale_seen.json 是本地滑动基线，只反映"与上次观测的差"；
    首次建立基线时一条变动都看不到。这里直接从 git 历史把连续几期的动作重放出来，
    让晨报 0.9 节上线即有上下文，不必干等几天攒基线。

    复用 diff() 同一套判定逻辑，只调高阈值聚焦大动作。任何失败返回 []，不阻塞采集。
    """
    try:
        log = _git("log", "FETCH_HEAD", "--format=%H|%at", "--grep=whale snapshot",
                   f"--since={days} days ago", "--reverse")
    except Exception:
        return []
    events, prev = [], None
    for line in log.splitlines():
        if not line.strip():
            continue
        try:
            sha, at = line.split("|")
            cur = parse_pos(json.loads(_git("show", f"{sha}:{SNAP_PATH}")))
        except Exception:
            continue                       # 个别提交缺文件/格式异常 → 跳过不中断
        if prev is not None:
            for kind, addr8, coin, d, ov, nv in diff(cur, prev,
                                                     min_new=min_flow, pct_th=PCT_TH):
                events.append((int(at), kind, addr8, coin, d, ov, nv))
        prev = cur
    # 先按金额取最大的几条，再按时间正序排——读起来是脉络，不是排行榜
    events.sort(key=lambda e: abs(e[6] - e[5]), reverse=True)
    return sorted(events[:max_rows], key=lambda e: e[0])


def _who(addr8, roster):
    """addr8（前10位）→ 名单里的中文身份；不在名单则回落未列名。"""
    return next((_label(a, roster) for a in roster if a.startswith(addr8)), None) \
        or f"未列名鲸鱼（{addr8}…）"


def main():
    seen = {}
    seen_path = os.path.join(DATA, "whale_seen.json")
    if os.path.exists(seen_path):
        try:
            seen = parse_pos(json.load(open(seen_path, encoding="utf-8")))
        except Exception:
            seen = {}

    try:
        cur_raw, ts, fetch_ok = load_remote()
    except Exception as e:
        print(f"[warn] A线鲸鱼快照读取失败（从无任何本地留存），本次跳过不阻塞：{str(e)[:80]}")
        return 0

    cur = parse_pos(cur_raw)
    if not cur:
        # 别只说"异常"——把顶层键打出来，下次 schema 一变就能直接看出对不上
        keys = list(cur_raw)[:5] if isinstance(cur_raw, dict) else type(cur_raw).__name__
        print(f"[warn] A线快照解析为空，跳过本次（快照顶层键={keys}，"
              f"疑似云端 schema 又变了 → 对照 parse_pos 检查兼容）")
        return 0

    roster = load_roster()
    longs, shorts, addrs, top = summarize(cur)
    rows = diff(cur, seen) if seen else []

    L = []
    if not fetch_ok:
        L.append(f"> ⚠ 本次未连上 GitHub，以下为本地留存的最近一次远端快照（{_ts_cn(ts)}），可能略旧")
    L.append(f"> 云端快照：{_ts_cn(ts)} ｜ 地址 {addrs} 个 ｜ 多头名义 {_usd(longs)} / 空头名义 {_usd(shorts)}"
             + (f" ｜ 第一大持仓 {top['coin']} {_dir_cn(top['dir'])} {_usd(top['val'])}"
                f"（{_label(top.get('addr',''), roster)}）" if top else ""))
    if not seen:
        L.append("- （首次建立基线：本次只记录，不出变动。下次运行开始比对）")
    elif rows:
        L.append(f"变动（与上次本地观测比，阈值 {_usd(MIN_NEW)} 且 ±{int(PCT_TH*100)}%）：")
        for kind, addr8, coin, d, ov, nv in rows[:8]:
            if kind == "平仓":
                L.append(f"- [{kind}] {_who(addr8, roster)} · {coin} 平掉 {_usd(ov)}")
            else:
                dv = nv - ov
                pct = f"（{dv/abs(ov)*100:+.0f}%）" if ov else ""
                L.append(f"- [{kind}] {_who(addr8, roster)} · {coin} {_dir_cn(d)} "
                         f"{_usd(ov)} → {_usd(nv)}{pct}")
    else:
        L.append("- 与上次本地观测无显著变化（阈值内）")

    # 跨期脉络：回放云端 git 历史，补上"首次基线无变动可报"的空窗
    hist = load_history_events()
    if hist:
        L.append("")
        L.append(f"近 {HIST_DAYS} 天重要变动（阈值 {_usd(HIST_MIN)}，回放云端快照历史）：")
        for at, kind, addr8, coin, d, ov, nv in hist:
            # 注意别用 ts 当循环变量：会覆盖上面 load_remote 拿到的快照时间
            ev_ts = datetime.fromtimestamp(at, tz=timezone.utc) + timedelta(hours=8)
            if kind == "平仓":
                L.append(f"- {ev_ts:%m-%d %H:%M} [{kind}] {_who(addr8, roster)} · "
                         f"{coin} 平掉 {_usd(ov)}")
            else:
                dv = nv - ov
                pct = f"（{dv/abs(ov)*100:+.0f}%）" if ov else ""
                L.append(f"- {ev_ts:%m-%d %H:%M} [{kind}] {_who(addr8, roster)} · {coin} "
                         f"{_dir_cn(d)} {_usd(ov)} → {_usd(nv)}{pct}")

    md = "\n".join(L) + "\n"

    with open(os.path.join(DATA, "whale_latest.md"), "w", encoding="utf-8") as f:
        f.write(md)
    # 只有成功拿到新快照才更新缓存（fetch 失败不覆盖基线）
    json.dump(cur_raw, open(seen_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=0)

    print(f"    云端快照 {ts}｜地址 {addrs}｜多 {_usd(longs)} / 空 {_usd(shorts)}｜"
          f"本次变动 {len(rows)} 条｜历史脉络 {len(hist)} 条")
    print("    → data/whale_latest.md（并入 ai_daily 0.9 节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
