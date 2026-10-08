@echo off
cd /d "%~dp0"

REM ── 解释器探测：优先 miniconda（akshare 依赖完备），防 PATH 被外部注入污染 ──
set "PYTHON=python"
if exist "D://miniconda3//python.exe" set "PYTHON=D://miniconda3//python.exe"

echo.
echo ============================================
echo   Worldview daily collect (one-click)
echo ============================================
echo.

REM ── 0. pull remote latest before collect (fast-forward only, no rebase) ──
echo [0/9] git sync (fetch + fast-forward)
echo --------------------------------------------
set "REPO=%~dp0.."
cd /d "%REPO%"
git fetch origin main
git merge --ff-only --no-edit FETCH_HEAD
if errorlevel 1 echo   [warn] pull skipped: local differs from remote, continue anyway
cd /d "%~dp0"
echo.

echo [1/9] world_feed.py  (main collect)
echo --------------------------------------------
"%PYTHON%" world_feed.py
echo.

echo [2/9] social_feed.py  (ApeWisdom discussion)
echo --------------------------------------------
"%PYTHON%" social_feed.py
echo.

echo [3/9] whale_link.py  (A-line whale snapshot via git fetch, no worktree touch)
echo --------------------------------------------
"%PYTHON%" whale_link.py
echo.

echo [4/9] macro_insight.py  (macro logic chains)
echo --------------------------------------------
"%PYTHON%" macro_insight.py
echo.

echo [5/9] report.py  (generate daily markdown)
echo --------------------------------------------
"%PYTHON%" report.py
echo.

REM ── 6. data self-check (local, zero token) + conditional CLI cross-review/repair ──
REM  本地自检只读数据、不改任何脚本；只有真出现异常才叫 CLI（省钱）。
REM  有异常时 CLI 被授权直接改数据源代码换源；三道校验兜底：
REM  语法(py_compile) → 导入(import) → 效果(重跑自检, 异常数不得增加)。
REM  任一不过则用 --snapshot 的副本自动回滚；改动写进日报末尾「本次自动修复」。
echo [6/9] verify.py  (self-check; CLI cross-review + auto-repair when anomaly)
echo --------------------------------------------
cd /d "%~dp0"
REM fresh-run: 清掉上一轮结论，避免旧核验被并进今天的报告
if exist "data\verify_cli.md" del /q "data\verify_cli.md"
if exist "data\verify_fix.md" del /q "data\verify_fix.md"
"%PYTHON%" verify.py
if exist "data\verify_todo.md" goto :verify_cli
echo   [ok] no anomaly - skip CLI review (saves tokens)
goto :verify_done

:verify_cli
echo   [i] anomaly found - asking CLI to cross-check online; source repair allowed...
REM 改码前留底：CLI 若改了源码，下面的 --checkfix 要拿这份副本回滚
"%PYTHON%" verify.py --snapshot
call codebuddy -y --effort medium -p "Read only these two files: D:\Worldview\worldview-pipeline\data\verify_todo.md, D:\Worldview\worldview-pipeline\data\verify_report.md. Do not explore other directories. Cross-check each listed anomaly against public sources on the web (Wallstreetcn, Cailianshe, Eastmoney, official sites). For each item decide: is our value wrong, is the second source wrong, or is it merely a different instrument or definition. If a data source we use has failed or is lagging, you MAY edit the pipeline .py file to repair it, under these hard rules: (1) only change the data-source endpoint - the URL, the response field name, or the parsing of that response; (2) never change any threshold, tolerance or judgment rule, never change function signatures, never delete a function; (3) the file must stay valid UTF-8 Python and must still compile; (4) keep every edit minimal and directly traceable to one listed anomaly. Your edits will be auto-verified and automatically rolled back if they break compilation, break imports, or increase the number of anomalies. Write your findings in Chinese to D:\Worldview\worldview-pipeline\data\verify_cli.md, following the format given in verify_todo.md, overwriting without asking. End that file with a section listing every code edit as: file / old endpoint to new endpoint / reason (write NONE if you changed no code). CRITICAL: news headlines are only a lead, never a number baseline - do not copy numbers from articles as conclusions." --allowedTools "Read,Write,Edit,Glob,Grep,WebSearch,WebFetch"
REM 三道校验（语法/导入/效果）；任一不过则自动回滚，最坏退化成「没改」
"%PYTHON%" verify.py --checkfix
"%PYTHON%" verify.py --merge

:verify_done
echo.

echo [7/9] codebuddy CLI  (article w/ web-research layer)
echo --------------------------------------------
cd /d "%~dp0"
REM stale-output guard: rename target so we can detect whether the CLI really wrote it
if exist worldview_commentary.md ren worldview_commentary.md worldview_commentary.prev.md
REM 注意：codebuddy 在 Windows 上是 codebuddy.cmd（批处理），
REM 不加 call 会转移控制权、本 bat 到此结束（2026-09-10 实测踩坑），必须用 call。
call codebuddy -y --effort xhigh -p "Read these four files (the fourth may not exist - skip it if so): D:\Worldview\worldview-pipeline\data\ai_daily.md, D:\Worldview\worldview-pipeline\data\news_context.md, D:\Worldview\worldview-pipeline\writing_rules.md, D:\Worldview\worldview-pipeline\data\verify_report.md. Do not explore other directories, do not read any other files. Then search the web for today's key events on the main topics in ai_daily.md. If verify_report.md flags any value as suspicious or inconsistent, do not cite that value as fact. Write the daily commentary in Chinese, strictly following writing_rules.md. Save it to D:\Worldview\worldview-pipeline\worldview_commentary.md, overwriting without asking." --allowedTools "Read,Write,Edit,Glob,Grep,WebSearch,WebFetch"
if not exist worldview_commentary.md (
  echo   [warn] CLI wrote nothing - fallback to llm_commentary.py
  "%PYTHON%" llm_commentary.py
)
if exist worldview_commentary.prev.md del /q worldview_commentary.prev.md
echo.

REM ── 7. archive: commit + push ──
echo [8/9] git commit + push
echo --------------------------------------------
set "STAMP=%date:~0,4%-%date:~5,2%-%date:~8,2%"
cd /d "%~dp0.."
git add -A
git diff --cached --quiet
if errorlevel 1 (
  git commit -m "daily snapshot %STAMP%"
  git push
  REM 被拒说明远端（云端 Actions/另一台机）已前进，同步后重推一次；不够 ff 就 merge。
  REM --no-edit 必须加：否则 git 会开编辑器，bat 会永久卡死在那一行。
  if errorlevel 1 (
    echo   [i] push rejected - remote moved, syncing and retrying once...
    git fetch origin main
    git merge --no-edit FETCH_HEAD
    if errorlevel 1 (
      git merge --abort 2>nul
      echo   [warn] merge conflict - aborted, please resolve manually
    ) else (
      git push
      if errorlevel 1 echo   [warn] push failed twice, please run git push manually
    )
  )
) else (
  echo   nothing new to commit
)
cd /d "%~dp0"
echo.

REM ── 8. publish briefing to daily-reports site (GitHub Pages) ──
echo [9/9] publish briefing to site (daily-reports)
echo --------------------------------------------
REM 发布器与本 bat 同目录(publish_site.py)，它自己按站点与项目同级规则定位 daily-reports
REM --force: 重跑时用最新晨报覆盖站点文件；内容没变则 git 无 diff，不会产生多余提交
"%PYTHON%" publish_site.py --force
if errorlevel 1 echo   [warn] publish failed - site not updated, check output above
cd /d "%~dp0"
echo.
echo Opening reports...
start "" worldview_daily.md
if exist worldview_commentary.md start "" worldview_commentary.md

echo ============================================
echo   Health check (status.py)
echo ============================================
"%PYTHON%" status.py
echo.

echo Done. Daily report saved to worldview_daily.md
pause >nul
