# -*- coding: utf-8 -*-
"""
llm_commentary.py — 世界观项目 · LLM 精读写稿（第 5 步）

定位：ai_daily.md 是"给 AI 的数据"，本脚本是"AI 真的读它并写文章"。
输入：data/ai_daily.md（今日状态+判级diff+异动+最近7天记忆）
      data/commentary/ 里最近 2 篇旧稿（延续解读：兑现/证伪/延续昨日结论）
输出：worldview_commentary.md（根目录，bat 会自动弹出）+ data/commentary/日期.md 归档

模型：优先 DASHSCOPE（qwen-plus，便宜），失败降级 DEEPSEEK。
key 复用 quantify-per 的 .env（跨项目只读 LLM key，不改那边任何东西）。
没 key / 调用失败 → 打印原因退出（非 0），bat 里不会弹空文件。

写作要求（用户 2026-09-08 定稿）：内容不要模板化一板一眼；可自行调用背景知识找观点；
内容可读性强；找得到矛盾点；延续前几日解读。
"""
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
CM_DIR = os.path.join(DATA, "commentary")
OUT = os.path.join(BASE, "worldview_commentary.md")
Q_ENV = next((p for p in (r"D:\quantify-per\.env", r"E:\quantify-per\.env") if os.path.exists(p)),
             r"D:\quantify-per\.env")  # LLM key 来源（只读，双机探测：公司 D: / 家里 E:）


def load_keys():
    """本目录 .env 优先，再补 quantify-per 的 LLM key（不覆盖已有）。"""
    loaded = {}
    for path in (os.path.join(BASE, ".env"), os.path.join(os.path.dirname(BASE), ".env"), Q_ENV):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if k and k not in loaded:
                        loaded[k] = v
        except Exception:
            pass
    return loaded


SYSTEM_PROMPT = """你是一位给职业交易员写宏观晨报的资深财经作者，读者是做了 20 年金融的人，讨厌套话和填充物。

写作铁律（违反任何一条即不合格）：
1. 用 prose 讲逻辑，像财经专栏，不是数据罗列。数据表在附录里，正文只在关键处引用数字，一段一个论点，长短句结合。
2. 开头必须是"今日钩子"：今天最重要的一件事。如果今天真没事，就把"没事"本身写透（市场在等什么、什么被定价完了）。
3. 必须至少指出一组矛盾：数据与数据之间、或数据与共识叙事之间。讲清它意味着什么、用什么验证、站在哪边。
4. 允许调用你的背景知识（历史类比、机构行为、市场结构）补充观点——这是你的价值——但必须与数据明确区分："数据说的是X，我的判断是Y"。禁止编造任何数据，禁止给数据加日期以外的想象。
5. 与前几日解读延续：旧稿里的判断，今天兑现了、证伪了、还是延续？必须点名回应，这是晨报的信誉所在。
6. 禁止模板腔：不得用"在当前宏观环境下""综上所述"开头收尾，段落长度不要均等，可以有短到一节的段落。
7. 结尾分两小节：先「观点收束」——把全文散落的信号收拢成不超过 3 条自己的话，每条末尾标注（数据）或（判断），且最后一条必须是「未来 1~2 周最值得盯的交叉点」（说清哪个数据兑现/证伪哪条逻辑）；收束不是复读摘要，要给前文没有明说的合成判断。再「明天盯什么」：具体到指标名和日期（数据日历你按常识推），最多两个，别列清单凑数。
8. 中文，800~1200 字。标题自拟，别叫"每日宏观解读"。
9. 时效铁律：每个数据都有截止日期。禁止对数据日期之后的行情做断言（数据截至 09-03 就不能说"今天的美债如何"）；滞后≥2 个交易日的读数必须在正文标注"截至X日的滞后读数，可能已过期"。
10. 单日≠趋势：变动数据附有连续天数(streak)。streak=±1 是单日波动，禁止据此断言"市场在交易/定价X"——最多写"我推测，需要X验证"。
11. 定价断言必须有概率：断言"市场在定价降息/加息/衰退"必须引用数据包里的 Polymarket 概率；没有概率数据时只能写"手头无 Fed 定价数据，无法判断市场预期方向，以下为推测"。
12. 文章重心之一是「市场在关注什么」：用数据包里的概率变动榜、讨论度榜（ApeWisdom）、资金费率，写市场当下的注意力在哪、在抢什么交易——不许通篇只围绕宏观数字打转。宏观是背景，市场在关心什么才是主线之一。"""


def build_user_prompt(today):
    with open(os.path.join(DATA, "ai_daily.md"), encoding="utf-8") as f:
        daily = f.read()
    # 最近 2 篇旧稿（延续解读用）
    olds = []
    if os.path.isdir(CM_DIR):
        for fn in sorted(os.listdir(CM_DIR), reverse=True):
            if fn.endswith(".md") and fn[:10] != today:
                p = os.path.join(CM_DIR, fn)
                try:
                    txt = open(p, encoding="utf-8").read()
                    olds.append((fn[:10], txt))
                except Exception:
                    pass
            if len(olds) >= 2:
                break
    parts = [f"今天日期：{today}。", "＝＝＝ 今日世界读数（含最近7天机器记忆，数据均带日期）＝＝＝", daily]
    if olds:
        parts.append("＝＝＝ 你前几日的解读（必须回应其判断的兑现/证伪/延续）＝＝＝")
        for d0, txt in olds:
            parts.append(f"--- {d0} 的解读 ---\n{txt}")
    else:
        parts.append("（注意：今天没有你的旧稿存档。写法上可以谈'接下来要持续验证什么'，"
                     "但禁止虚构'我们之前说过/回看前几日'之类的往期判断。）")
    parts.append("现在写今天的宏观解读。直接给文章，不要任何前置说明。")
    return "\n\n".join(parts)


def call_llm(keys, user_prompt):
    """降级链（2026-09-08 定）：免费优先——NVIDIA NIM 的 deepseek-v4-flash、
    SenseNova 新平台的 deepseek-v4-flash / 6.8-flash-lite（各 500~1500 次/5h 免费），
    全挂了才落付费的 DEEPSEEK。
    注意：NVIDIA v4-flash 是 reasoning 模型，max_tokens 太小会全烧在思维链上
    （实测 60 全空、2000 够一句），写文章给 4000。"""
    import requests
    attempts = []   # (显示名, 真模型ID, url, key, max_tokens)
    if keys.get("NVIDIA_API_KEY"):
        attempts.append(("nvidia:deepseek-v4-flash", "deepseek-ai/deepseek-v4-flash-0731",
                         "https://integrate.api.nvidia.com/v1/chat/completions",
                         keys["NVIDIA_API_KEY"], 4000))
    if keys.get("SENSENOVA_API_KEY"):
        attempts.append(("sensenova:deepseek-v4-flash", "deepseek-v4-flash",
                         "https://token.sensenova.cn/v1/chat/completions",
                         keys["SENSENOVA_API_KEY"], 4000))
        attempts.append(("sensenova:6.8-flash-lite", "sensenova-6.8-flash-lite",
                         "https://token.sensenova.cn/v1/chat/completions",
                         keys["SENSENOVA_API_KEY"], 4000))
    if keys.get("DEEPSEEK_API_KEY"):
        attempts.append(("deepseek-chat", "deepseek-chat", "https://api.deepseek.com/chat/completions",
                         keys["DEEPSEEK_API_KEY"], 2400))
    if not attempts:
        raise RuntimeError("无可用 LLM key（NVIDIA / SENSENOVA / DEEPSEEK 任一即可）")
    last = None
    for disp, model, url, key, max_toks in attempts:
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {key}",
                                            "Content-Type": "application/json"},
                              json={"model": model, "temperature": 0.7, "max_tokens": max_toks,
                                    "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                                 {"role": "user", "content": user_prompt}]},
                              timeout=300)
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            txt = (msg.get("content") or "").strip()
            if not txt:      # reasoning 模型 token 被思维链吃光时会返回空 content
                fr = r.json()["choices"][0].get("finish_reason")
                raise RuntimeError(f"content 为空（finish={fr}，加大 max_tokens 或换模型）")
            return model, txt
        except Exception as e:
            body = ""
            try:
                body = r.text[:120].replace("\n", " ")   # noqa: F821
            except Exception:
                pass
            last = e
            print(f"    [warn] {disp} 失败: {str(e)[:80]} {body}")
    raise last


def main():
    today = datetime.now(CST).strftime("%Y-%m-%d")
    daily_path = os.path.join(DATA, "ai_daily.md")
    if not os.path.exists(daily_path):
        print("[fail] data/ai_daily.md 不存在——先跑 macro_insight.py")
        sys.exit(1)
    keys = load_keys()
    prompt = build_user_prompt(today)
    print(f"[1] LLM 精读中（ai_daily {len(prompt)} 字符 + 旧稿延续）…")
    model, txt = call_llm(keys, prompt)
    os.makedirs(CM_DIR, exist_ok=True)
    head = (f"# 世界观晨报 · {today}\n\n> LLM（{model}）精读 ai_daily.md 后撰写；"
            "数据事实与作者判断已在文中区分。数据底稿见 data/ai_daily.md。\n\n---\n\n")
    article = head + txt.strip() + "\n"
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(article)
    with open(os.path.join(CM_DIR, f"{today}.md"), "w", encoding="utf-8") as f:
        f.write(article)
    print(f"[2] 已写稿（{model}）→ {OUT}")
    print("    开头 200 字预览：")
    print("    " + txt.strip()[:200].replace("\n", "\n    "))


if __name__ == "__main__":
    main()
