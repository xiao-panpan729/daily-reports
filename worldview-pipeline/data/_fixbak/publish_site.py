#!/usr/bin/env python3
"""发布世界观晨报（B线）到 daily-reports 站点。

位置约定（2026-09-13 定稿）
--------------------------
本脚本住在**它产出的内容旁边**：worldview-pipeline/publish_site.py，与
worldview_commentary.md 同目录 —— 所以源文件不需要任何跨盘探测。

站点逻辑（首页重建）**不在本仓库**，而在站点仓库自身的 scripts/index_builder.py：
首页重建只服务于站点，跟哪个项目产的内容无关。

站点定位规则：**站点仓库与内容项目同级**
    D:\\Worldview  与  D:\\daily-reports    （本机）
    E:\\Worldview  与  E:\\daily-reports    （家机）
其他位置用环境变量 DAILY_REPORTS_ROOT 显式指定站点根目录（不必改代码）。

流程:
  1. 读同目录的 worldview_commentary.md
  2. 复制到 <站点根>/docs/worldview-YYYY-MM-DD.md（保证有 H1 标题）
  3. import <站点根>/scripts/index_builder.py 重建 docs/index.md
  4. git commit + push（origin → GitHub Pages，Actions 自动 mkdocs gh-deploy；再双推 gitee）

用法:
  python publish_site.py                     # 发布今日晨报（并推送）
  python publish_site.py --no-push           # 只生成本地文件，不碰 git（预览用）
  python publish_site.py --date 20260913     # 指定日期回填
  python publish_site.py --force             # 目标已存在时覆盖重发
"""

import os
import re
import sys
import subprocess
from datetime import date

# 输出编码：**只**在被重定向/管道时才强制 utf-8。
#   · 控制台：Windows 控制台走 Unicode API（PEP 528），中文与 emoji 本来就正常；
#     此时若强行 reconfigure，反而会把 GBK(936) 控制台打乱——所以不动它。
#   · 管道/重定向：默认按 locale(cp936) 编码，utf-8 的 emoji 会直接 UnicodeEncodeError → 必须改。
# 这也让调用方无需 chcp 65001（本项目 bat 护栏明令禁止在 GBK bat 里改代码页）。
if not sys.stdout.isatty():
    sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_PATH = os.path.join(HERE, "worldview_commentary.md")

RE_H1_DATE = re.compile(r"^#\s*世界观晨报\s*[·・]\s*(\d{4}-\d{2}-\d{2})", re.M)


def find_site_root() -> str | None:
    """定位 daily-reports 站点仓库根目录（须含 docs/）。

    规则：站点仓库与内容项目同级 —— 从本脚本所在目录逐级上溯找 daily-reports/。
    也可用 DAILY_REPORTS_ROOT（站点根）或 DAILY_REPORTS_DOCS（docs 目录）显式指定。
    本函数在三个发布器里各有一份**极简 bootstrap 副本**（跨仓库无法共享 import：
    必须先找到站点，才能 import 站点里的东西）。
    """
    cands = []
    for var, is_docs in (("DAILY_REPORTS_ROOT", False), ("DAILY_REPORTS_DOCS", True)):
        v = os.environ.get(var)
        if v:
            p = os.path.abspath(v)
            cands.append(os.path.dirname(p) if is_docs else p)

    p = HERE
    for _ in range(5):
        cands.append(os.path.join(p, "daily-reports"))
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent

    for drive in ("D:", "E:", "C:"):
        cands.append(os.path.join(drive + os.sep, "daily-reports"))

    for c in cands:
        if c and os.path.isdir(os.path.join(c, "docs")):
            return os.path.abspath(c)
    return None


SITE_ROOT = find_site_root()
if SITE_ROOT is None:
    print("❌ 找不到 daily-reports 站点仓库（需含 docs/ 目录）")
    print(f"   本脚本位置: {HERE}")
    print("   规则：站点仓库与内容项目同级（如 E:\\Worldview 与 E:\\daily-reports）")
    print("   其他位置请设环境变量 DAILY_REPORTS_ROOT 指向站点仓库根目录")
    sys.exit(1)

# 站点逻辑（首页重建）住在站点仓库里 —— 定位到站点后把它的 scripts/ 加进搜索路径
sys.path.insert(0, os.path.join(SITE_ROOT, "scripts"))
from index_builder import rebuild_index  # noqa: E402  （必须在 sys.path 设置之后）

DEST_DIR = os.path.join(SITE_ROOT, "docs")
INDEX_PATH = os.path.join(DEST_DIR, "index.md")


def resolve_date(text: str) -> str:
    """优先用正文 H1 里的日期（更可靠），否则用今天。返回 YYYY-MM-DD。"""
    m = RE_H1_DATE.search(text)
    if m:
        return m.group(1)
    return date.today().strftime("%Y-%m-%d")


def ensure_h1(text: str, blog_date: str) -> str:
    """保证正文以 H1 开头（llm_commentary.py 兜底产物可能没有标题）。"""
    stripped = text.lstrip("\ufeff \t\r\n")
    if stripped.startswith("# "):
        return stripped
    return f"# 世界观晨报 · {blog_date}\n\n{stripped}"


def git_commit_and_push(blog_date: str) -> bool:
    """提交并推送。origin（GitHub Pages 源）优先，gitee 失败不阻断。

    2026-09-13 加固（两条判据拆开，不再互相绑架）：
      · 提交判据 = **本次这两个路径**是否有暂存差异（git diff --cached --quiet -- <paths>）
      · 推送判据 = 本地是否领先 origin/main（git rev-list --count origin/main..HEAD）
      不用「整仓库 git status」判提交：仓库里只要有任何无关的未跟踪文件（例如本仓库的
      scripts/ 目录），就会被误判成"有改动"，然后 commit 空跑失败、打出莫名其妙的警告。
      也不用「整仓库是否干净」判推送：一旦 commit 成功但 push 失败（断网/代理抖动），
      下次跑时工作区已干净 → 直接 return，那个提交会永远卡在本地推不上去。拆开后只要
      领先就重推。
    """
    repo_dir = SITE_ROOT
    dest_name = f"worldview-{blog_date}.md"
    rel_paths = [f"docs/{dest_name}", "docs/index.md"]

    def run(cmd, timeout=60):
        return subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True,
                              encoding="utf-8", timeout=timeout)

    try:
        if run(["git", "rev-parse", "--git-dir"], timeout=10).returncode != 0:
            print("  ⚠️  daily-reports 不是 git 仓库，跳过推送")
            return False

        # 只 add/commit 本次发布涉及的两个路径；git commit 带 pathspec 做局部提交，
        # 因此不会顺手把仓库里别人的暂存内容一起提交走。
        run(["git", "add", "--"] + rel_paths, timeout=15)
        if run(["git", "diff", "--cached", "--quiet", "--"] + rel_paths,
               timeout=15).returncode != 0:
            c = run(["git", "commit", "-m", f"add worldview briefing {blog_date}",
                     "--"] + rel_paths, timeout=30)
            if c.returncode != 0:
                out = ((c.stderr or "") + (c.stdout or "")).strip()
                print(f"  ⚠️  commit 失败: {out[:160]}")
                return False
            print("  ✅ commit 成功")
        else:
            print("  ⏭️  发布文件无改动，跳过提交")

        ahead = run(["git", "rev-list", "--count", "origin/main..HEAD"], timeout=15)
        if ahead.returncode != 0:
            # origin/main 引用解析不了（本机曾出现远端跟踪引用落不了盘的情况）——
            # 不能当成"已是最新"而静默跳过推送，直接尝试推一次（没东西可推也是无害的）。
            print("  ℹ️  无法解析 origin/main，直接尝试推送")
        elif ahead.stdout.strip() in ("", "0"):
            print("  ⏭️  远端已是最新，跳过推送")
            return True

        r = run(["git", "push", "origin", "main"], timeout=120)
        if r.returncode != 0:
            print(f"  ⚠️  GitHub 推送失败: {(r.stderr or '').strip()[:200]}")
            return False
        print("  ✅ GitHub 推送成功（Actions 会自动构建站点）")

        r = run(["git", "push", "gitee", "main"], timeout=120)
        if r.returncode == 0:
            print("  ✅ Gitee 镜像推送成功")
        else:
            print("  ⚠️  Gitee 推送失败（不影响 GitHub Pages）")
        return True

    except subprocess.TimeoutExpired:
        print("  ⚠️  git 操作超时")
        return False


def publish(blog_date: str, force: bool = False) -> bool:
    dest_path = os.path.join(DEST_DIR, f"worldview-{blog_date}.md")

    if os.path.exists(dest_path) and not force:
        print(f"  ⏭️  worldview-{blog_date}.md 已存在，跳过（要覆盖加 --force）")
        return False

    if not os.path.isfile(SRC_PATH):
        print(f"  ❌ 找不到源文件：{SRC_PATH}")
        print("     本脚本应与 worldview_commentary.md 同目录（worldview-pipeline/）")
        return False

    with open(SRC_PATH, encoding="utf-8-sig") as f:
        content = f.read()

    content = ensure_h1(content, blog_date)

    # 日期自检：源文件 H1 日期与目标日期不一致时提醒（回填场景）
    src_date = resolve_date(content)
    if src_date != blog_date:
        print(f"  ⚠️  源文件标题日期 {src_date} ≠ 目标日期 {blog_date}，按目标日期命名")

    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  ✅ 写入 docs/worldview-{blog_date}.md（{len(content.splitlines())} 行）")

    rebuild_index(DEST_DIR, INDEX_PATH)
    return True


def main():
    args = sys.argv[1:]
    force = "--force" in args
    no_push = "--no-push" in args

    date_arg = None
    if "--date" in args:
        idx = args.index("--date")
        if idx + 1 < len(args):
            d = args[idx + 1]
            date_arg = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d

    if date_arg:
        blog_date = date_arg
    else:
        if not os.path.isfile(SRC_PATH):
            print(f"❌ 找不到源文件：{SRC_PATH}")
            sys.exit(1)
        with open(SRC_PATH, encoding="utf-8-sig") as f:
            blog_date = resolve_date(f.read())

    print(f"📤 发布世界观晨报 {blog_date}")
    print(f"   站点: {SITE_ROOT}")
    print()

    published = publish(blog_date, force=force)
    if not published:
        print()
        print("✨ 无需发布")

    print()
    if no_push:
        print("  ⏸️  --no-push：未做任何 git 操作（仅本地文件已更新）")
    else:
        git_commit_and_push(blog_date)

    print()
    print("🌐 GitHub: https://xiao-panpan729.github.io/daily-reports/")
    print(f"🌐 本地预览: cd {SITE_ROOT} && mkdocs serve")


if __name__ == "__main__":
    main()
