#!/usr/bin/env python3
"""daily-reports 首页 docs/index.md 的唯一重建入口。

为什么要有这个文件（2026-09-13 建立）
--------------------------------------
原先 rebuild_index() 在 _publish_report.py 和 _publish_industry.py 里各有一份
**完全相同的拷贝**，而且它把 index.md **整体重写**（不是增量插入）。这带来两个后果：

  1. 手工往 index.md 加板块 → 下次跑任一发布脚本就被整个抹掉；
  2. 两份拷贝会逐渐漂移，加板块时漏改一处，另一条管道一跑就会删掉别人的板块。

所以抽成这里一份，所有发布脚本 import 它。以后加板块只改这一个文件。

归属（2026-09-13 收敛）
----------------------
首页重建**只服务于本仓库**（读写的就是本仓库的 docs/index.md），所以它住在站点
仓库里、不属于任何内容项目。三个客户各在各方，依赖方向天然（发布器本来就要往
本仓库写文件 + commit）：

    quantify-per/_publish_report.py                  → import 本模块
    quantify-per/_publish_industry.py                → import 本模块
    Worldview/worldview-pipeline/publish_site.py     → import 本模块

客户定位本模块的方式：先按「站点仓库与内容项目同级」找到站点根，再把
`<站点根>/scripts` 插进 sys.path。见各客户脚本开头的 _find_site_root()。

发布脚本用法
------------
    import os, sys
    sys.path.insert(0, os.path.join(SITE_ROOT, "scripts"))
    from index_builder import rebuild_index

    rebuild_index(DEST_DIR, INDEX_PATH)

直接跑（手动重建首页，不做任何 git 操作）：
    python index_builder.py [docs目录]
"""

import os
import re

# ── 文件命名约定 → 日期 ──────────────────────────────────────────────
# 信源日报     2026-09-13.md            （日报管道）
# 世界观晨报   worldview-2026-09-13.md  （B 线）
# 产业事件分析 industry-2026-09-13.md   （午后管道，2026-07-16 后暂停）
# 注意：三条正则都要求前缀后紧跟日期，所以 worldview-data-*.md 之类不会误匹配。
RE_REPORT = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
RE_WORLDVIEW = re.compile(r"^worldview-(\d{4}-\d{2}-\d{2})\.md$")
RE_INDUSTRY = re.compile(r"^industry-(\d{4}-\d{2}-\d{2})\.md$")

# ── 每个板块首页直接可见的条数（其余折叠）────────────────────────────
REPORT_VISIBLE = 5
WORLDVIEW_VISIBLE = 1
INDUSTRY_VISIBLE = 5


def scan(dest_dir: str):
    """扫描 docs 目录，返回三个倒序日期列表：(reports, worldview, industry)。"""
    files = os.listdir(dest_dir)

    def dates_of(rex):
        found = [m.group(1) for m in (rex.match(f) for f in files) if m]
        return sorted(found, reverse=True)

    return dates_of(RE_REPORT), dates_of(RE_WORLDVIEW), dates_of(RE_INDUSTRY)


def _append_section(lines, dates, *, title, item_tpl, link_tpl, folded_label,
                    visible, empty_text, marker_after_title=None, marker_after_list=None):
    """追加一个「标题 +（可选锚点）+ 链接列表（前 visible 条可见，其余折叠）」板块。"""
    lines.extend(["", "---", "", f"## {title}", ""])

    if marker_after_title:
        lines.extend([marker_after_title, ""])

    if not dates:
        lines.append(empty_text)
    else:
        for d in dates[:visible]:
            lines.append(f"- [{item_tpl.format(d=d)}]({link_tpl.format(d=d)})")

        folded = dates[visible:]
        if folded:
            lines.extend([
                "",
                "<details>",
                f"<summary>{folded_label}（点击展开，共{len(folded)}篇）</summary>",
                "",
            ])
            for d in folded:
                lines.append(f"- [{item_tpl.format(d=d)}]({link_tpl.format(d=d)})")
            lines.extend(["", "</details>"])

    if marker_after_list:
        lines.extend(["", marker_after_list])


def rebuild_index(dest_dir: str, index_path: str):
    """扫描 dest_dir，整体重建 index.md。所有发布脚本共用这一个实现。

    输出行尾用平台默认（Windows 下 CRLF），与 docs/ 下既有文件保持一致。
    """
    report_dates, worldview_dates, industry_dates = scan(dest_dir)

    lines = [
        "# 信源日报",
        "",
        "每日 8 公众号聚合分析 + 宏观共振 + 海外映射",
    ]

    # ① 信源日报（主板块，最新 5 条可见）
    _append_section(
        lines, report_dates,
        title="日报列表",
        item_tpl="{d} 信源日报",
        link_tpl="{d}.md",
        folded_label="历史报告",
        visible=REPORT_VISIBLE,
        empty_text="*尚无已发布的日报。运行 `python _publish_report.py` 来发布。*",
        marker_after_title="<!-- REPORTS_LIST -->",
    )

    # ② 世界观晨报（B 线，最新 1 条可见 —— 保持「小地方」）
    _append_section(
        lines, worldview_dates,
        title="世界观晨报（B线）",
        item_tpl="世界观晨报 {d}",
        link_tpl="worldview-{d}.md",
        folded_label="历史晨报",
        visible=WORLDVIEW_VISIBLE,
        empty_text="*尚无已发布的晨报。运行 `python publish_site.py` 来发布。*",
        marker_after_list="<!-- WORLDVIEW_LIST -->",
    )

    # ③ 产业事件分析（午后管道，2026-07-16 后暂停，保留历史）
    _append_section(
        lines, industry_dates,
        title="产业事件分析（午后管道）",
        item_tpl="产业分析 {d}",
        link_tpl="industry-{d}.md",
        folded_label="历史产业分析",
        visible=INDUSTRY_VISIBLE,
        empty_text="*尚无已发布的产业分析。*",
        marker_after_list="<!-- INDUSTRY_LIST -->",
    )

    lines.extend([
        "",
        "---",
        "",
        "> 数据来源：微信公众号 | 宏观数据 | US ETF 势能 | 基本面因子",
        "> 生成工具：gen_source_summary.py + gen_daily_brief.py + Claude Code AI 验证",
        "> 世界观晨报：worldview-pipeline（world_feed.py + llm_commentary.py 兜底）+ Claude Code AI 撰写",
    ])

    with open(index_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"  ✅ 更新 index.md（{len(report_dates)} 篇日报 / "
          f"{len(worldview_dates)} 篇晨报 / {len(industry_dates)} 篇产业）")


if __name__ == "__main__":
    # 手动重建首页（不做任何 git 操作）
    # 本文件在 <站点根>/scripts/ 下 → 默认目标为上一级的 docs/
    import sys
    dest = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")
    dest = os.path.abspath(dest)
    rebuild_index(dest, os.path.join(dest, "index.md"))
