# CLI 接入说明 · 2026-09-10（2026-09-15 更新：CLI 多了一个身份；当日再更新：质检员被授权改码）

> 起因：codebuddy CLI 接进晨报管线后，一次真实运行「跑完了但什么都没写」。
> 本文记录根因和正确的用法。**结论在第一节，照做就行。**
>
> **2026-09-15 起，CLI 在管线里有两个身份**（两处调用，互不合并）：
>
> | 身份 | 步骤 | 干什么 | 权限 |
> |---|---|---|---|
> | **质检员**（新） | `[6/9]` | 只在 `verify.py` 报出异常时才被叫起来：上网核验异常项 + **可直接改数据源代码换源** | 含 `Edit`（**2026-09-15 放权**）；改完有三道校验，不过自动回滚 |
> | **写手** | `[7/9]` | 把 `ai_daily.md` 写成晨报 | 含 `Write,Edit`（要写文件） |
>
> **为什么不合并成一次调用**：质检要结构化输出、可能有副作用、结论要先落到 `verify_report.md` 供写手参考；
> 写稿要创作。混在一起，它有可能拿"改数据前"的数字去写文章。
>
> ⚠️ 质检员的硬约束：**新闻只当线索，不能当数值基准**（快讯多为盘中价、约数、转述；
> 收盘报道可用作"源是不是没发布"的判断）。合格产出 = 「可取的 API/官方端点 + 字段说明」，不是手抄的数字。
>
> ⚠️ **质检员改码的三道闸门**（`verify.py --snapshot` / `--checkfix`，2026-09-15 增）：
> 它改的是"下一轮还要跑"的代码，所以 `[6/9]` 的异常分支变成
> `--snapshot`（留底）→ CLI（可 Edit）→ `--checkfix`（语法→导入→重跑自检）→ `--merge`。
> 任一关不过就用副本自动回滚 + 删掉新增文件；**最坏结果退化成"没改"，而不是"管线断掉"**。
> 改动固定汇报在日报末尾「### 5. 本次自动修复」（含旧端点 → 新端点）。
> 详见 `CLAUDE.md`「CLI 自动改码的护栏」一节。

---

## 一、为什么它跑完却没写文件（两个拦截）

### 拦截 1：`-p` 模式下，需要弹窗批准的工具会被自动拒绝

`-p`（非交互）意味着**没人能点"允许"**，所以任何需要批准的工具直接拒绝。日志原文：

```
Permission to use Write has been denied because this tool requires approval
but permission prompts are not available in non-interactive mode.
```

实测这一轮被拒的：`Bash`、`WebFetch`、`Write`、`Edit`（只有 `WebSearch` 自动放行）。
结果：模型读到了资料、写好了稿子，**但存不下去**，只能把内容打到屏幕上，文件时间戳没变。

> ⚠️ 反直觉的一点：`--allowedTools "Read,Write,Edit,…"` **放行不了** Write/Edit（实测无效）。
> 干净解法是在 `~/.codebuddy/settings.json` 里把默认权限模式改成 `bypassPermissions`
> —— 这样**任何**会话（交互的、脚本的）都不会再弹确认。见第四节。

### 拦截 2（更底层）：`data/` 目录整个不见了

CLI 第一步去读 `data/ai_daily.md`，拿到 `File does not exist` —— 因为
`D:\Worldview\worldview-pipeline\data\` 这个目录当时已经不存在了
（`ai_daily.md` / `news_context.md` / `pm_curve.csv` / `pm_status.json` 全没了）。

- 时间线：15:23 还在（`llm_commentary.py` 写过 `data/commentary/`）→ 15:47 有一次
  `main ↔ whale-data` 来回切分支 → 16:02 CLI 已看不到。
- 推测：那次切分支时跑过 `git clean -x`。`-x` 会**连 `.gitignore` 里忽略的目录一起删**，
  `data/` 正好在里面；而 `git clean` 不进 reflog，事后无法追查。
- 影响：**不致命**。`data/` 本来就是被忽略的中间产物，重跑一次管线就重建。
  代码改动（`world_feed.py` 的 `PM_STEM_WORDS` / `fetch_pm_curve` / `pm_status`、
  `macro_insight.py` 读 `news_context.md`）**都还在**。

---

## 二、现在要做的两步

### 第 1 步：先把数据重建出来（否则 CLI 没东西可读）

最简单：**双击 `世界观采集.bat` 走一遍**。
（`[7/9]` 那步可能仍会报错，但现在有兜底，会自己退回 `llm_commentary.py`，不影响产出。）

或者只要数据不要文章，PowerShell 里手动跑前 5 步：

```powershell
cd D:\Worldview\worldview-pipeline
D:\miniconda3\python.exe world_feed.py
D:\miniconda3\python.exe social_feed.py
D:\miniconda3\python.exe whale_link.py
D:\miniconda3\python.exe macro_insight.py
D:\miniconda3\python.exe report.py
```

> 跑之前确认 Clash 开着（混合端口 7897）。FRED / Polymarket / Yahoo 都走代理。

### 第 2 步：验证现在真的不会卡了（30 秒，便宜）

**故意不加 `-y`** —— 如果全局设置生效，这样也该写成功：

```powershell
cd D:\Worldview\worldview-pipeline
codebuddy --effort low -p "Use the Write tool to create the file D:\Worldview\worldview-pipeline\_cli_perm_test.txt with exactly the content: OK. Then reply DONE. Do not use any other tools."
```

跑完检查文件（应该有一个写着 `OK` 的小文件）：

```powershell
Get-Content D:\Worldview\worldview-pipeline\_cli_perm_test.txt
Remove-Item D:\Worldview\worldview-pipeline\_cli_perm_test.txt
```

看到 `OK` = 权限通了，以后不用再担心卡在确认上。
若还是 "denied"，把 `--permission-mode bypassPermissions` 显式加到命令行再试（那说明设置没被读到）。

---

## 三、正式让 CLI 写晨报

```powershell
cd D:\Worldview\worldview-pipeline
codebuddy -y --effort xhigh -p "Read these three files only: D:\Worldview\worldview-pipeline\data\ai_daily.md, D:\Worldview\worldview-pipeline\data\news_context.md, D:\Worldview\worldview-pipeline\writing_rules.md. Do not explore other directories, do not read any other files. Then search the web for today's key events on the main topics in ai_daily.md. Write the daily commentary in Chinese, strictly following writing_rules.md. Save it to D:\Worldview\worldview-pipeline\worldview_commentary.md, overwriting without asking." --allowedTools "Read,Write,Edit,Glob,Grep,WebSearch,WebFetch"
```

跑完看 `worldview_commentary.md` 的时间戳有没有变成刚才 —— **不要看退出码**，
CLI 可能返回 0 却什么都没写（这就是这次踩的坑）。

> `-y` 现在其实可以省掉了（全局模式已经是 `bypassPermissions`），
> 但脚本里留着它更保险 —— 万一哪天设置被改回去，它还能兜住。两条防线不冲突。

> **2026-09-15 起的实际 prompt**（bat `[7/9]` 步里，已从「三文件」扩到「四文件」）：
> 多读一个 `data/verify_report.md`，并加了一条约束——
> **「If verify_report.md flags any value as suspicious or inconsistent, do not cite that value as fact.」**
> 这样自检抓到的问题值不会被写进晨报当事实。质检（`[6/9]`）与写稿（`[7/9]`）是两次独立调用。

---

## 四、已经开好的「不再被卡住」全局设置

改的是 `C:\Users\Administrator\.codebuddy\settings.json`（备份 `.bak_20260910_before_permmode`）：

```json
{
  "trustedDirectories": ["D:/**", "C:/Users/Administrator/**"],
  "model": "deepseek-v4.1-flash",
  "reasoningEffort": "xhigh",
  "permissions": { "defaultMode": "bypassPermissions" }
}
```

生效验证（不用猜，这条命令直接读 CLI 自己的配置）：

```powershell
codebuddy config get permissions     # → { "defaultMode": "bypassPermissions" }
```

### 权限模式一共有 7 档，别选错

| 模式 | 名字 | 实际行为 |
|---|---|---|
| `default` | 默认 | 每个工具第一次用都要问 ← 脚本里必卡 |
| `acceptEdits` | 自动通过编辑 | 只免掉文件编辑 |
| `plan` | 规划模式 | 只分析，不改任何文件 |
| `auto` | 自动 | AI 分类器判断：安全放行、高风险拒绝 |
| **`dontAsk`** | **免打扰** | ⚠️ **陷阱**：不弹窗，但「**拒绝需要审批的操作**」 |
| **`bypassPermissions`** | **跳过权限确认** | 跳过所有权限确认 ← **已设成这个** |
| `fullAccess` | 完全访问 | 跳过所有权限检查，**连危险命令也不问** |

> ⚠️ `dontAsk`（"免打扰"）看起来最像"不打扰你"，但它的语义是**直接拒绝 + 不告诉你**。
> 这正是这次踩的坑的"正式形态"—— 跑完、退出码 0、文件没变。**永远别选它。**

### 为什么选 `bypassPermissions` 而不是更强的那档

`fullAccess` 比它多免一层："危险命令也不问"。差别就是**删文件 / `git reset --hard` / `git clean -x`
这类操作以后连问都不问**。而这次 `data/` 消失，八成就是这么来的。

所以先停在 `bypassPermissions`：**它已经解决了"被卡住"**（不再有任何确认弹窗），
只是不给"自毁"开绿灯。哪天你真的撞上"某个危险命令被直接拒绝"，改一个词升到 `fullAccess` 就行。

另外两个安全网（本来就开着）：

- `fileCheckpointingEnabled: true` —— CLI 每次改文件前存检查点，
  交互模式下**连按两次 Esc** 可以回退代码（等于 Ctrl+Z）。
- `sandbox.enabled: false` —— CLI 自己那层沙箱是关的，不会限制读写。

---

## 五、bat 已经改好了什么

`世界观采集.bat` 第 `[7/9]` 步（备份 `世界观采集.bat.bak_20260910_before_-y`；2026-09-15 插入自检步后由 `[6/8]` 变为 `[7/9]`）：

1. **加 `-y`** —— 真正放行 Write。
2. **prompt 加约束** —— "Read these three files only… Do not explore other directories"，
   避免它像上次那样白跑好几轮 Glob 找不存在的目录。
3. **加了"写没写"校验** —— 跑前把 `worldview_commentary.md` 改名成 `.prev`，
   跑完 `if not exist` 就自动退回 `llm_commentary.py`，最后删掉 `.prev`。
4. 🔴 **`codebuddy` 改成了 `call codebuddy`** —— 这个坑最阴：

   Windows 上 `codebuddy` 实际是 **`codebuddy.cmd`（一个批处理文件）**。
   **在 .bat 里调用另一个 .cmd 不加 `call`，控制权就转移过去、永不返回。**

   实测症状（2026-09-10 17:05 那次）：CLI **成功写出了文件**，然后
   黑框直接关闭、`.prev` 没删、git 归档那一步没跑（当时编号 `[7/7]`；2026-09-13 加发布步 → `[7/8]`；2026-09-15 加自检步 → 现为 `[7/9]`）
   —— 因为 bat 在那一行就整体结束了。
   连带后果：上面第 3 条那个"写没写"校验**在下一次修复前根本没执行过**。

   > 通用规则：**在 .bat 里调用任何外部命令前，先确认它是不是 .cmd/.bat；是就必须加 `call`。**
   > npm 全局包（`xxx.cmd` shim）全都适用这条。

> 第 3 条才是关键。原来的 `if errorlevel 1` 是**真漏洞**：
> 这次 CLI 退出码是 0 却什么都没写，兜底不会触发 → **晨报静默留着旧稿，没人发现**。
> 跟前面 Polymarket 快照的"静默降级"是同一类病：**判成败要看产出物，不看退出码。**

---

## 六、速查

| 现象 | 原因 | 解 |
|---|---|---|
| `Permission to use X has been denied …non-interactive mode` | `-p` 下没人点允许 | 全局设 `permissions.defaultMode` |
| `File does not exist` 读不到 `data/xxx` | `data/` 被清掉了 | 重跑管线 |
| 退出码 0 但文件没变 | 工具被拒后它只打了 stdout | 全局设权限；脚本里校验文件时间戳 |
| 「免打扰」模式看起来最省事 | 它其实是**静默拒绝** | 永远别用 `dontAsk` |
| 状态栏显示 "Sonnet" 但配的是 deepseek | UI 槽位显示名，非真实模型 | 看 `~/.codebuddy/projects/<项目>/*.jsonl` 的 `providerData.model` |
| 中文 prompt 写进 bat 后闪退/乱码 | bat 是 GBK 无 BOM | 用 Python 按 GBK 读写改，改完跑 `scripts/check_bat_encoding.py` |
| **bat 卡在 `[8/9]` 归档步不动** | 漏了 `--no-edit`，git 打开了编辑器在等输入 | 补 `git merge --no-edit FETCH_HEAD`（2026-09-15 已内置） |
| **`git push` 被拒 `(fetch first)`** | 云端 Actions 抢先推了 whale 快照到同一个 main | bat 已内置"同步+重推一次"，无需手工；真冲突才停下报 warn |
| **日报某个值明显不对** | 源滞后或失效（VIX 滞后 1 天 / WTI 滞后 4 天 / 死标的恒值） | 先看日报末尾「附：数据自检」节；需要时跑 `python verify.py` |
| 想撤销 CLI 刚才改的文件 | 检查点是开着的 | 交互模式下输入框留空，**连按两次 Esc** |

CLI 参数（`codebuddy --help` 实测）：

- `-y, --dangerously-skip-permissions` ← 非交互写文件的"命令行侧"保险
- `--permission-mode <acceptEdits|bypassPermissions|default|plan|dontAsk|auto>`
- `--tools` 限定工具集 / `--allowedTools` 放行名单（**对审批类工具无效**）
- `--effort <minimal|low|medium|high|xhigh|max>`（`/model` 界面调不了强度）
- `--append-system-prompt-file <path>` ← 以后 `writing_rules.md` 可以这样注入，不必塞进 prompt

CLI 自带诊断命令（不用猜配置，直接读它自己的值）：

```powershell
codebuddy config list                 # 全部生效配置
codebuddy config get permissions      # 只看权限模式
codebuddy config set permissions.defaultMode bypassPermissions
```
