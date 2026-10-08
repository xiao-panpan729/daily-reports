# worldview-pipeline · B线 · 世界读数采集管线

> **本夹是什么**：世界观项目（`D:\Worldview`，顶层 git 主仓 `xiao-panpan729/worldview`）的 **B线「世界读数底座」**。
> A线（whale-station，知情钱观测站）看"大钱在链上怎么动"；**B线看"世界里正在发生什么"**（宏观、叙事、讨论度），是"读世界"那一侧的采集与管线。
> 与 A线 平级，各自独立成夹，由顶层主仓统一 git 管理。

---

## 一 · 一句话定位

**采集"真实世界信号"进结构化数据**：把 FRED 宏观指标、加密货币全市场行情、Polymarket 事件概率、ApeWisdom 社区讨论度等聚成每日快照，产出人可读的日报——供后续"判断闭环"当观测输入（读世界 → 因果链/叙事 → 假设卡片）。

---

## 二 · 目录内容

```
worldview-pipeline/
├── world_feed.py        ★ 主线采集器 v2：HIP-3 全量118资产行情 + FRED 15宏观序列 + Yahoo/akshare/USDCNY 汇率
│                          （含死标的过滤：OI=0 且 24h 成交=0 的永续不进焦点表）
├── social_feed.py       ★ ApeWisdom 讨论度采集（免key）
├── whale_link.py        ★ A↔B 打通：拉 A线云端鲸鱼快照（只 git fetch，不动工作树）
├── macro_insight.py     ★ 宏观逻辑链引擎（判级 + 异动 + AI 简报 + 历史分位）
├── report.py            ★ 把 CSV 转成人可读 worldview_daily.md 日报（"跑完看得见成品"）
├── verify.py            ★ 数据自检层（2026-09-15 新增）：内部一致性 / 双源比对 / 数据时效 / 数据日新鲜度
│                          ＋每日归档 daily.csv。零 token；有异常才写 verify_todo.md 叫 CLI 核验
├── llm_commentary.py    ★ 出晨报 worldview_commentary.md（[7/9] 步经 codebuddy CLI 调用）
├── publish_site.py      ★ 发布晨报到站点 daily-reports（[9/9] 步调它，2026-09-13 新增）
├── status.py            一键健康度检查（各源最近更新时间/是否过期）
├── backfill.py          历史回补（补缺失日期）
├── backfill_ext.py      扩展源回补（外部宏观/汇率源）
├── panel_v1.html        数据面板 v1（可视化浏览，v2 待固化）
├── 世界观采集.bat        Windows 双击一键全链（**9 步 `[0/9]`~`[9/9]`**：
│                          同步→采集→鲸鱼→逻辑链→日报→**自检**→晨报→归档→发布）
├── vv.bat / vvs.bat     入口别名（vv=全量 9 步出文章 / vvs=轻跑 6 步只补数据、不碰 git）
├── data\                落库数据（默认不入 git；两个例外已放行入库：daily.csv、verify_log.jsonl）
│     daily.csv          ★ 每日归档：只装**通过自检**的值，主键 (数据日, key)，读-改-写幂等
│     verify_log.jsonl   ★ 逐次自检留档（追加式，可回溯"哪天数据出过问题"）
│     snapshots.csv      主快照历史（每次跑追加一整套，未收敛）
│     history.csv        宏观+行情序列（需手工跑 backfill 系列才有）
│     pm_markets.csv / pm_meta.csv   Polymarket 市场
│     asof.json          每个值的"数据日 + 来源"留痕（日报「数据日」列的数据源）
│     verify_report.md / verify_todo.md / verify_result.json   自检三产物
│     _panel.json / _focus33.json    面板/焦点缓存
├── worldview_daily.md   日报产物（report.py 生成，末尾自动挂自检节）
├── worldview_commentary.md  晨报产物（llm_commentary.py 生成，[9/9] 发布到站点）
└── *.md                 线内认知文档（见下）
```

---

## 三 · 认知文档索引（线内）

| 文档 | 讲什么 |
|---|---|
| `03-数据源与采集体系总结.md` | 数据源分层、HIP-3 全量/焦点架构、采集体系 v2 |
| `04-世界观面板视觉设计.md` | 面板 v1→v2 视觉与交互设计 |
| `05-资金流量数据源调研.md` | 资金流量侧外部源调研 |
| `06-认知锚点·B线数据有什么用.md` | **B线哲学**：这些数据到底怎么服务于世界观判断（先读这篇建立直觉） |
| `08-使用说明·怎么跑怎么读.md` | **日常操作手册**：9 步全链、报告怎么看、喂 AI 的正确姿势、已知降级 |
| `09-宏观数据网·信源目录与发布日历.md` | 官方信源目录 + 每月发布日历（L1 事实层 ~ L4 市场验证层） |
| `writing_rules.md` | 晨报写稿规则（人话感、术语翻译表、口径提醒） |
| `CLI接入说明.md` | `[7/9]` 步为什么改成调 codebuddy CLI（含 `call` 陷阱等实测坑） |

---

## 四 · 每日怎么跑

**双击 `世界观采集.bat`** 即可，一条龙 9 步 `[0/9]`~`[9/9]`：
同步远端 → world_feed → social_feed → whale_link → macro_insight → report → **数据自检** → **出晨报** → git 归档 → **发布上站**。
产出当日 `worldview_daily.md`（日报，末尾自带自检节）+ `worldview_commentary.md`（晨报），晨报自动发到 https://xiao-panpan729.github.io/daily-reports/

> **你只需要双击，不需要多按任何键。** 自检、归档、push 重试、发布全部在 bat 内部自动完成。

> ⚠️ **跑前先开 Clash Verge**，否则 FRED/Yahoo/Polymarket/ApeWisdom 全部拿不到数（Hyperliquid/财政部/akshare 不受影响）。

想**手动补跑单个步骤**（改码/刷新某块）、看**报告怎么读**、**一天多跑（vv/vvs）**、以及各类**坑**——
逐条命令与说明统一在 **`08-使用说明·怎么跑怎么读.md`**（日常操作手册）。健康度检查：`python status.py`。

---

## 五 · 状态

**2026-09-03（M13-M15）**
- ✅ 底座成型：主采集(v2 全量)、FRED 直连、ApeWisdom 讨论度、日报出口、一键 bat、健康检查

**2026-09-15（数据可信度加固，四轮）**
- ✅ **换源纠错**：VIX `FRED VIXCLS`→Yahoo `^VIX`（原滞后 1 交易日）；WTI `FRED DCOILWTICO`（EIA 现货，滞后 4 交易日）→Yahoo `CL=F`；
  DXY `xyz:DXY`（零成交死标的）→Yahoo `DX-Y.NYB`。FRED 降级为兜底
- ✅ **死标的过滤**：`OI=0 且 24h 成交=0` 的永续不落盘（全 universe 剔掉 16 个，含坑过人的 `xyz:VIX`/`xyz:DXY`）
- ✅ **数据日留痕**：`data/asof.json` + 日报新增「数据日」列，区分"何时采集"与"值属于哪天"，超期标 ⚠️
- ✅ **VIX 期限结构**：`^VIX9D/^VIX/^VIX3M/^VIX6M/^VVIX`，取代原来那个建立在假数据上的"永续−现货差"
- ✅ **自检层 `verify.py`**：四层检查 + 零 token 门控（无异常不叫 CLI）+ 每日归档 `daily.csv`
- ✅ **delta 口径修正**：基线从"上次采集"改为"昨日确认值"，一天多跑不再产生假变动
- ✅ **push 被拒自愈**：`[8/9]` 被拒自动 `fetch` + `merge --no-edit FETCH_HEAD` + 重推一次
- ✅ **CLI 自动改码护栏**（放权 + 关进笼子）：源失效时授权 CLI 直接改端点，改完过
  语法/导入/效果三关，**任一不过自动回滚**；改动固定汇报在日报末尾「### 5. 本次自动修复」
- 🔲 下一步见顶层 `evolution-timeline.md` 待办：akshare 人气榜+恐惧贪婪（三因子权重）、面板 v2、定时化
- ⚠️ `data/` **默认不入 git**，仅 `daily.csv` 与 `verify_log.jsonl` 两个文件放行入库（顶层 .gitignore 用三步规则开例外）
- 🔲 **未做（明确搁置）**：布油永续 vs 实盘偏离阈值告警 —— 报告已有 AI 解读、且固定百分比对原油基差不科学，
  结论是先不做，等真出现对不上再按"基差 vs 滚动中位数"设计

---

## 六 · 与 A线 的关系

| | A线 whale-station | B线 worldview-pipeline（本夹） |
|---|---|---|
| 看什么 | 知情钱链上持仓/调仓（行为之眼） | 宏观+行情+概率+讨论度（世界读数） |
| 出口 | 云端每2h快照+≥$100万微信告警 | 每日 worldview_daily.md 日报 |
| 运行地 | GitHub Actions（云端24h） | 本机（8:30-20:30 开机）/ 定时化待做 |

两线在顶层 `evolution-timeline.md` 统一记账；本项目"读世界 → 判断"的完整叙事见 `archive-立项与研究/04-落地研究·鲸鱼本地化与判断之尺.md` 与 `06-认知锚点`。
