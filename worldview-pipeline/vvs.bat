@echo off
cd /d "%~dp0"
REM ─────────────────────────────────────────────
REM  vvs = 世界观轻跑（盘中随时跑，一天N次）：只补数据不出新文章
REM  步骤 = world_feed + social_feed + whale_link + macro_insight + report + verify
REM  不碰 llm_commentary：零 LLM token，旧文章原样保留
REM  ai_memory/snapshots 同日重跑自动去重/取最新，随便跑不脏数据
REM ─────────────────────────────────────────────
set "PYTHON=python"
if exist "D://miniconda3//python.exe" set "PYTHON=D://miniconda3//python.exe"

echo.
echo ============================================
echo   Worldview QUICK collect (data only, no LLM)
echo ============================================
echo.

echo [1/6] world_feed.py
"%PYTHON%" world_feed.py || goto :err
echo.

echo [2/6] social_feed.py
"%PYTHON%" social_feed.py || goto :err
echo.

echo [3/6] whale_link.py  (A-line whale via git fetch)
"%PYTHON%" whale_link.py || goto :err
echo.

echo [4/6] macro_insight.py
"%PYTHON%" macro_insight.py || goto :err
echo.

echo [5/6] report.py  (refresh data report, article untouched)
"%PYTHON%" report.py || goto :err
echo.

echo [6/6] verify.py  (local self-check, no CLI, zero token)
REM fresh-run: 清掉上一轮结论，避免早上的核验被并进午间的报告
if exist "data\verify_cli.md" del /q "data\verify_cli.md"
if exist "data\verify_fix.md" del /q "data\verify_fix.md"
"%PYTHON%" verify.py
if errorlevel 1 echo   [warn] verify failed (non-fatal, data unchanged)
echo.

echo Opening data report...
start "" worldview_daily.md

echo.
echo ============================================
echo   Quick run done. LLM article NOT rewritten
echo   (要更新文章请跑 vv.bat 或 python llm_commentary.py)
echo ============================================
pause >nul
exit /b 0

:err
echo.
echo [FAIL] 某一步出错，向上看第一条非零退出的步骤。数据层有容错，通常是网络/代理问题，重跑即可。
pause >nul
exit /b 1
