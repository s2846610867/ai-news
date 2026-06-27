#!/usr/bin/env python3
"""
AI 日报生成器
功能: 每天从 RSS + DuckDuckGo 抓取 AI 新闻, DeepSeek 整理, 生成桌面 HTML 日报
运行: 每天早上 6 点自动执行 (由 launchd 或 GitHub Actions 调度)

路径可通过环境变量覆盖, 以便在云端 CI 中运行:
  AI_NEWS_AGENT_DIR  工作目录 (默认 ~/DeepSeek智能体)
  AI_NEWS_SITE_DIR   站点仓库目录 (默认 AGENT_DIR/ai-news-site, CI 中设为仓库根)
  AI_NEWS_DATA_FILE  历史数据文件 (默认 AGENT_DIR/ai_news_data.json)
  DEEPSEEK_API_KEY   DeepSeek 密钥 (本地放 .env, CI 放 GitHub Secret)
"""

import argparse
import html
import json
import os
import re
import socket
import subprocess
import sys
import time
import concurrent.futures
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

# —— 时区: 统一钉成北京时间, 避免在 UTC 服务器(GitHub Actions)上 datetime.now() 差一天 ——
os.environ["TZ"] = "Asia/Shanghai"
time.tzset()

# —— 路径配置 (均可被环境变量覆盖, 默认与本地 Mac 行为一致) ————————
AGENT_DIR = Path(os.getenv("AI_NEWS_AGENT_DIR", str(Path.home() / "DeepSeek智能体")))
load_dotenv(AGENT_DIR / ".env", override=True)
DATA_FILE    = Path(os.getenv("AI_NEWS_DATA_FILE", str(AGENT_DIR / "ai_news_data.json")))   # 历史数据
LOG_FILE     = Path(os.getenv("AI_NEWS_LOG_FILE", str(AGENT_DIR / "ai_news.log")))          # 运行日志
HTML_FILE    = Path(os.getenv("AI_NEWS_HTML_FILE", str(Path.home() / "AI日报.html")))         # 本地预览网页
SITE_DIR     = Path(os.getenv("AI_NEWS_SITE_DIR", str(AGENT_DIR / "ai-news-site")))         # GitHub Pages 仓库

# —— RSS 订阅源 (中文 AI 媒体) ————————————————————
RSS_FEEDS = [
    # —— 中文 AI / 科技媒体 ——
    ("量子位",    "https://www.qbitai.com/feed"),
    ("36氪",      "https://36kr.com/feed"),
    ("IT之家",    "https://www.ithome.com/rss/"),
    ("少数派",    "https://sspai.com/feed"),
    ("爱范儿",    "https://www.ifanr.com/feed"),
    # —— 英文 AI 媒体(DeepSeek 会翻译并用中文重写) ——
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("The Verge AI",  "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("MIT科技评论",   "https://www.technologyreview.com/feed/"),
]

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# RSS / 网络请求统一超时(秒),防止 launchd 任务永久挂起
NETWORK_TIMEOUT = 20
socket.setdefaulttimeout(NETWORK_TIMEOUT)
RETRY_ATTEMPTS = 3
# DeepSeek 调用超时(秒),防止接口抽风时永久挂起(历史曾卡 3-4 小时)
DEEPSEEK_TIMEOUT = float(os.getenv("DEEPSEEK_TIMEOUT", "300"))


# =========================================================
# 工具函数
# =========================================================

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        AGENT_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        # 日志写失败不应让整个脚本崩溃
        pass


def notify(title: str, message: str):
    """Best-effort macOS notification; failures should not break the job."""
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    try:
        subprocess.run(["osascript", "-e", script], timeout=5, check=False)
    except Exception:
        pass


def retry(label: str, func, attempts=RETRY_ATTEMPTS, delay=5):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as e:
            last_error = e
            if attempt < attempts:
                log(f"⚠️ {label}失败 ({attempt}/{attempts}): {e}, {delay} 秒后重试")
                time.sleep(delay)
            else:
                log(f"❌ {label}失败 ({attempts}/{attempts}): {e}")
    raise last_error


def get_api_key():
    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not key:
        log("❌ 未设置 DEEPSEEK_API_KEY，请在 ~/DeepSeek智能体/.env 中填写")
        notify("AI 日报失败", "未设置 DEEPSEEK_API_KEY，请在 .env 中填写")
        sys.exit(1)
    return key


def normalize_title(title: str) -> set:
    """把标题转成关键词集合, 用于相似度判断"""
    # 只保留字母数字和中文字符,其余替换为空格
    title = re.sub(r"[^\w一-鿿]", " ", title)
    words = [w for w in title.lower().split() if len(w) > 1]
    return set(words)


def is_duplicate(title_a: str, title_b: str, threshold=0.45) -> bool:
    """两个标题关键词重合度超过阈值 → 视为同一条新闻"""
    wa, wb = normalize_title(title_a), normalize_title(title_b)
    if not wa or not wb:
        return False
    overlap = len(wa & wb) / min(len(wa), len(wb))
    return overlap >= threshold


def deduplicate(items: list) -> list:
    """合并去重: 发现重复时保留 snippet 更长 (更详细) 的那条"""
    result = []
    for item in items:
        matched = False
        for i, kept in enumerate(result):
            if is_duplicate(item["title"], kept["title"]):
                # 保留摘要更详细的
                if len(item["snippet"]) > len(kept["snippet"]):
                    result[i] = item
                matched = True
                break
        if not matched:
            result.append(item)
    return result


def clean_text(text: str) -> str:
    """去 HTML 标签 + 还原 HTML 实体"""
    text = re.sub(r"<[^>]+>", "", text or "")
    text = html.unescape(text)
    return text.strip()


# =========================================================
# 抓取新闻
# =========================================================

def _fetch_one_feed(source: str, url: str) -> tuple:
    import feedparser
    feed = feedparser.parse(url)
    items = []
    for entry in feed.entries[:6]:
        title   = clean_text(entry.get("title", ""))
        link    = entry.get("link", "")
        summary = clean_text(entry.get("summary", ""))[:400]
        if title and link:
            items.append({"title": title, "url": link, "snippet": summary, "source": source})
    return source, items


def fetch_rss() -> list:
    try:
        import feedparser  # noqa: F401
    except ImportError:
        log("⚠️ 未安装 feedparser, 跳过 RSS 抓取")
        return []

    results, ok, empty = [], [], []
    # 多个源并行抓取, 总耗时≈最慢的单个源, 某个源挂了/超时只跳过它, 不拖累整体
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(RSS_FEEDS)) as pool:
        futs = {pool.submit(_fetch_one_feed, s, u): s for s, u in RSS_FEEDS}
        try:
            for fut in concurrent.futures.as_completed(futs, timeout=45):
                src = futs[fut]
                try:
                    source, items = fut.result()
                    if items:
                        results += items
                        ok.append(f"{source}:{len(items)}")
                    else:
                        empty.append(source)
                except Exception as e:
                    empty.append(f"{src}(err)")
                    log(f"RSS抓取失败 ({src}): {e}")
        except concurrent.futures.TimeoutError:
            log("⚠️ 部分 RSS 源超时, 使用已返回的结果")
    log(f"RSS 获取 {len(results)} 条 | 有内容: {', '.join(ok) or '无'} | 空或失败: {', '.join(empty) or '无'}")
    return results


def fetch_ddg() -> list:
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            log("⚠️ 未安装 ddgs / duckduckgo_search, 跳过搜索")
            return []

    today = datetime.now().strftime("%Y年%m月%d日")
    queries = [
        f"人工智能 AI 新闻 {today}",
        f"大模型 ChatGPT Claude Gemini {today}",
    ]
    results = []
    seen_urls = set()
    try:
        with DDGS() as ddgs:
            for query in queries:
                try:
                    hits = retry(
                        f"DuckDuckGo 搜索 ({query})",
                        lambda q=query: list(ddgs.text(q, max_results=8, region="cn-zh")),
                        attempts=2,
                        delay=3,
                    )
                    for hit in hits:
                        href = hit.get("href") or hit.get("url", "")
                        if href and href not in seen_urls:
                            seen_urls.add(href)
                            results.append({
                                "title":   clean_text(hit.get("title", "")),
                                "url":     href,
                                "snippet": clean_text(hit.get("body", ""))[:400],
                                "source":  "DuckDuckGo",
                            })
                except Exception as e:
                    log(f"DuckDuckGo 搜索失败: {e}")
                time.sleep(0.5)
    except Exception as e:
        log(f"DuckDuckGo 初始化失败: {e}")
    log(f"DuckDuckGo 获取 {len(results)} 条")
    return results


def fetch_all_news() -> list:
    """RSS 和 DuckDuckGo 并行抓取, 合并去重"""
    rss_items, ddg_items = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_rss = pool.submit(fetch_rss)
        f_ddg = pool.submit(fetch_ddg)
        for fut, name in ((f_rss, "RSS"), (f_ddg, "DuckDuckGo")):
            try:
                items = fut.result(timeout=60)
                if name == "RSS":
                    rss_items = items
                else:
                    ddg_items = items
            except concurrent.futures.TimeoutError:
                log(f"⚠️ {name} 抓取超时, 已跳过")
            except Exception as e:
                log(f"⚠️ {name} 抓取异常: {e}")

    merged = rss_items + ddg_items
    deduped = deduplicate(merged)
    log(f"合并去重后: {len(deduped)} 条")
    return deduped


# =========================================================
# DeepSeek 处理
# =========================================================

def process_with_deepseek(news_items: list, api_key: str, recent_titles: list = None) -> dict:
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com", timeout=DEEPSEEK_TIMEOUT)

    today = datetime.now().strftime("%Y年%m月%d日")
    news_text = "\n\n".join([
        f"[{i+1}] 标题: {item['title']}\n摘要: {item['snippet']}\n来源: {item['source']}\n链接: {item['url']}"
        for i, item in enumerate(news_items[:30])
    ])

    # 最近几天已报道过的标题, 喂给模型避免"换皮"重复报道同一件事
    recent_titles = recent_titles or []
    recent_block = ""
    if recent_titles:
        lines = "\n".join(f"- {t}" for t in recent_titles if t)
        recent_block = (
            "\n\n以下是最近几天【已经报道过】的新闻标题，"
            "今天不要再选同一件事（即使换了说法、换了角度也算重复）:\n" + lines + "\n"
        )

    prompt = f"""你是AI新闻编辑, 今天是{today}。目标读者是AI新手和普通创作者, 不懂技术术语。具体来说, 读者是一个零基础在学AI、正在做AI视频小作品、关注怎么用AI变现的新手, 目前在用Claude/DeepSeek写脚本、用即梦/可灵生图生视频、想把AI技能变成副业。

以下是今天收集的AI相关新闻:
{news_text}
{recent_block}
请按以下标准筛选并排序, 最终挑出最值得关注的约10条:
- 优先选: 新模型/产品发布、重大技术突破、行业政策、公司重要动态
- 去重: 多条其实在讲同一件事时只保留信息最全的一条; 不要选已经在上面【已经报道过】列表里出现过的同一件事; 最终选出的每一条必须来自【不同】的原始新闻编号(source_id 互不相同), 若某条原始新闻同时讲了好几件事, 只取其中最重要的一件, 用别的原始新闻补足到约10条
- 降低权重: 纯营销软文、泛泛的"AI未来展望"类文章
- 排序依据: 对普通读者的实际影响力, 越靠前越重要
- 语言风格: 通俗口语化, 不要夸大, 不要制造焦虑, 不要写投资建议, 不要写未经证实的结论

返回JSON:
{{
  "articles": [
    {{
      "title": "精简中文标题 (20字以内)",
      "summary": "说清楚发生了什么, 用1-2句话 (60字以内)",
      "why": "为什么重要: 重点解释对普通用户、创作者或AI学习者的实际影响 (40字以内, 从普通人视角出发)",
      "category": "分类: 从「模型」「工具」「公司」「应用」「政策」「开源」「视频」「Agent」「机器人」中选一个",
      "beginner_takeaway": "AI新手能从这条新闻学到什么或关注什么 (30字以内, 可以是一个问题或一个值得观察的点)",
      "for_me": "对正在学AI、做AI视频、想用AI变现的新手来说, 这条消息跟他有没有关系、要不要关注、能不能直接用上——大白话说清楚 (50字以内, 说不上来就写「暂时不用管」, 有关系就说具体怎么用或为什么值得看)",
      "source_id": 这条日报主要依据的那条原始新闻开头中括号里的数字(例如 3 表示基于上面第[3]条),
      "url": "原文链接(直接照抄对应那条原始新闻的链接, 不要改写或编造)"
    }}
  ],
  "plain_summary": "今天AI圈大白话总结 (150字左右)。假设读者完全不懂技术, 用口语化表达, 像朋友聊天一样说今天最值得关注的AI动态, 可以带上自己的看法。"
}}

只返回JSON, 不要其他内容。"""

    resp = retry(
        "DeepSeek API 调用",
        lambda: client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        ),
        delay=8,
    )

    content = resp.choices[0].message.content.strip()

    # 去掉可能的 ```json ... ``` 包裹
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        # 兜底: 从文本中提取第一个 {...} 块
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if not m:
            raise ValueError("DeepSeek 返回内容无法解析为 JSON")
        parsed = json.loads(m.group(0))

    # 字段校验,缺失时给默认值,避免后续 KeyError
    parsed.setdefault("articles", [])
    parsed.setdefault("plain_summary", "")

    # 用 source_id 把每条日报映射回原始新闻的真实链接,
    # 修复 DeepSeek 自行填 URL 时张冠李戴(不同新闻共用一个错链接)的老问题。
    # 注意: 只改 URL, 不删任何文章, 避免误伤真新闻。
    for a in parsed["articles"]:
        sid = a.get("source_id")
        try:
            sid = int(sid)
        except (TypeError, ValueError):
            sid = None
        if sid and 1 <= sid <= len(news_items):
            a["url"] = news_items[sid - 1].get("url", a.get("url", "#"))
        a.pop("source_id", None)
    return parsed


# =========================================================
# GitHub 热门 AI 项目
# =========================================================

# 用这些 AI 相关 topic 搜索, 覆盖大模型/智能体/生成式
GH_TOPICS = ["llm", "ai-agent", "generative-ai"]


def _gh_search(query: str, per_page: int = 15) -> list:
    params = urllib.parse.urlencode({
        "q": query, "sort": "stars", "order": "desc", "per_page": per_page,
    })
    url = f"https://api.github.com/search/repositories?{params}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "ai-news-bot",
    })
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    items = []
    for r in data.get("items", []):
        if not r.get("html_url"):
            continue
        items.append({
            "name": r.get("full_name", ""),
            "url": r.get("html_url", ""),
            "stars": r.get("stargazers_count", 0),
            "forks": r.get("forks_count", 0),
            "language": r.get("language") or "",
            "desc": (r.get("description") or "")[:300],
        })
    return items


def _gh_is_quality(r: dict) -> bool:
    """反刷星过滤: 必须有描述; 高星项目的 fork 数要达到星数的 1% 以上
    (真正热门项目 fork 很多, 刷星 spam 几乎没人 fork)"""
    if not (r.get("desc") or "").strip():
        return False
    stars, forks = r.get("stars", 0), r.get("forks", 0)
    if stars >= 3000 and forks < stars * 0.01:
        return False
    return True


def _gh_merge_topics(extra: str = "", want: int = 5) -> list:
    """对多个 AI topic 各搜一次, 合并去重, 过滤刷星, 再按星数取前 want 个"""
    seen, merged = set(), []
    for t in GH_TOPICS:
        q = f"topic:{t}" + (f" {extra}" if extra else "")
        try:
            for r in _gh_search(q, 15):
                if r["name"] and r["name"] not in seen:
                    seen.add(r["name"])
                    merged.append(r)
        except Exception as e:
            log(f"GitHub 搜索失败 (topic:{t} {extra}): {e}")
    quality = [r for r in merged if _gh_is_quality(r)]
    dropped = len(merged) - len(quality)
    if dropped:
        log(f"GitHub 过滤: 候选 {len(merged)} 个, 滤掉疑似刷星/无描述 {dropped} 个")
    quality.sort(key=lambda x: x.get("stars", 0), reverse=True)
    return quality[:want]


def fetch_github() -> dict:
    """两个模块: 星标总榜 + 近两周飙升的 AI 新项目"""
    result = {"top_starred": [], "trending": []}
    try:
        result["top_starred"] = _gh_merge_topics("", 5)
    except Exception as e:
        log(f"GitHub 星标总榜失败: {e}")
    try:
        since = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
        result["trending"] = _gh_merge_topics(f"created:>{since}", 5)
    except Exception as e:
        log(f"GitHub 飙升榜失败: {e}")
    log(f"GitHub 获取: 总榜 {len(result['top_starred'])} 条, 飙升 {len(result['trending'])} 条")
    return result


def add_github_explanations(gh: dict, api_key: str) -> dict:
    """让 DeepSeek 为每个项目写一段面向新手的全面中文解释"""
    repos = (gh.get("top_starred") or []) + (gh.get("trending") or [])
    if not repos:
        return gh
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com", timeout=DEEPSEEK_TIMEOUT)
    listing = "\n".join(
        f"[{i+1}] {r['name']} (★{r['stars']}, {r['language'] or '多语言'})\n描述: {r['desc'] or '无'}"
        for i, r in enumerate(repos)
    )
    prompt = f"""下面是一些 GitHub 上的 AI 开源项目。请为每个项目写【详细的中文讲解】, 面向完全不懂技术的 AI 新手。要让读者不用点进 GitHub 看英文, 光看你的讲解就能明白这是什么、有什么用、适不适合自己。

每个项目给两个字段:
- tagline: 一句话说清这是什么 (15-30字)
- explanation: 详细讲解 (150-300字), 必须涵盖: ①它解决什么问题、为什么有用 ②普通人或创作者具体能拿它做什么(举1-2个实际例子) ③适合什么样的人、上手难不难。用大白话, 不堆术语, 万一用到术语就顺手解释一下。基于给出的信息和你已知的事实, 不要编造。

{listing}

只返回JSON:
{{"items": [{{"id": 1, "tagline": "...", "explanation": "..."}}]}}
id 对应上面方括号里的编号。只返回JSON, 不要其他内容。"""
    try:
        resp = retry("GitHub 项目解释", lambda: client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            response_format={"type": "json_object"},
        ), delay=8)
        content = resp.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", content, re.DOTALL)
            parsed = json.loads(m.group(0)) if m else {"items": []}
        info = {it.get("id"): it for it in parsed.get("items", [])}
        for i, r in enumerate(repos):
            it = info.get(i + 1) or {}
            r["tagline"] = it.get("tagline") or ""
            r["explanation"] = it.get("explanation") or r.get("desc", "")
    except Exception as e:
        log(f"GitHub 解释生成失败, 用原描述兜底: {e}")
        for r in repos:
            r.setdefault("tagline", "")
            r.setdefault("explanation", r.get("desc", ""))
    return gh


# =========================================================
# HTML 生成
# =========================================================

def format_date(date_str: str) -> str:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return dt.strftime(f"%Y年%m月%d日 {WEEKDAYS[dt.weekday()]}")


def generate_html(data: dict):
    days_html = ""
    for day in data["days"]:
        date_display = format_date(day["date"])
        articles_html = "".join([
            f"""<div class="news-item">
                    <span class="news-num">{i}</span>
                    <div class="news-content">
                        <a href="{html.escape(a.get('url', '#'))}" target="_blank" class="news-title">{html.escape(a.get('title', ''))}</a>
                        <p class="news-summary">{html.escape(a.get('summary', ''))}</p>
                        {f'<p class="news-why">💡 {html.escape(a["why"])}</p>' if a.get("why") else ""}
                        {f'<p class="news-for-me">👤 对我：{html.escape(a["for_me"])}</p>' if a.get("for_me") else ""}
                    </div>
                </div>"""
            for i, a in enumerate(day["articles"], 1)
        ])
        days_html += f"""
        <div class="day-section" id="{day['date']}">
          <div class="day-header"><h2>{date_display}</h2></div>
          <div class="news-list">{articles_html}</div>
          <div class="plain-summary">
            <div class="summary-label">🤖 小白看这里</div>
            <p>{html.escape(day['plain_summary'])}</p>
          </div>
        </div>"""

    last_update = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = len(data["days"])

    # nav: 直接用日期字符串,不再用 format_date()[:10] 截半截中文
    nav_links = "".join(
        f'<a href="#{d["date"]}">{d["date"]}</a>' for d in data["days"]
    )

    html_out = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>AI 日报</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",Arial,sans-serif;background:#f0f2f5;color:#1d1d1f;line-height:1.6}}
.header{{background:linear-gradient(135deg,#1a1a2e,#16213e,#0f3460);color:#fff;padding:44px 20px 32px;text-align:center}}
.header h1{{font-size:2em;font-weight:700;letter-spacing:3px;margin-bottom:8px}}
.header p{{color:rgba(255,255,255,.5);font-size:.85em}}
.nav{{background:#fff;padding:12px 20px;text-align:center;box-shadow:0 1px 8px rgba(0,0,0,.06);position:sticky;top:0;z-index:10;overflow-x:auto;white-space:nowrap}}
.nav a{{display:inline-block;margin:0 6px;padding:4px 12px;border-radius:20px;font-size:.8em;color:#667eea;text-decoration:none;border:1px solid #667eea}}
.nav a:hover{{background:#667eea;color:#fff}}
.container{{max-width:780px;margin:0 auto;padding:28px 16px}}
.day-section{{background:#fff;border-radius:18px;margin-bottom:26px;overflow:hidden;box-shadow:0 2px 14px rgba(0,0,0,.07)}}
.day-header{{background:linear-gradient(135deg,#667eea,#764ba2);padding:13px 22px}}
.day-header h2{{color:#fff;font-size:1em;font-weight:600}}
.news-list{{padding:6px 0}}
.news-item{{display:flex;align-items:flex-start;padding:13px 22px;border-bottom:1px solid #f2f2f2;gap:13px}}
.news-item:last-child{{border-bottom:none}}
.news-item:hover{{background:#fafbff}}
.news-num{{background:#667eea;color:#fff;width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:.72em;font-weight:700;flex-shrink:0;margin-top:3px}}
.news-content{{flex:1;min-width:0}}
.news-title{{color:#1a1a2e;font-weight:600;font-size:.93em;text-decoration:none;display:block;margin-bottom:4px}}
.news-title:hover{{color:#667eea}}
.news-summary{{color:#777;font-size:.83em;line-height:1.55}}
.news-why{{color:#667eea;font-size:.8em;margin-top:4px;font-style:italic}}
.news-for-me{{color:#2d8a6a;font-size:.8em;margin-top:4px;background:#f0fdf7;border-left:3px solid #52c5a8;padding:4px 8px;border-radius:0 6px 6px 0}}
.plain-summary{{background:linear-gradient(135deg,#f8f9ff,#eef0ff);border-top:2px solid #667eea;padding:18px 22px}}
.summary-label{{font-weight:700;color:#667eea;margin-bottom:8px;font-size:.9em}}
.plain-summary p{{color:#444;font-size:.9em;line-height:1.9}}
.footer{{text-align:center;color:#bbb;font-size:.76em;padding:10px 20px 32px}}
</style>
</head>
<body>
<div class="header">
  <h1>🤖 AI 日报</h1>
  <p>每天早上 6 点自动更新 · 精选 10 条 AI 资讯 · 共 {total} 天记录</p>
</div>
<div class="nav">{nav_links}</div>
<div class="container">{days_html}</div>
<div class="footer">最后更新: {last_update} · 由 DeepSeek 整理生成</div>
</body>
</html>"""

    HTML_FILE.write_text(html_out, encoding="utf-8")
    log(f"网页已生成: {HTML_FILE}")


def export_json(data: dict):
    """把每天的新闻数据写成独立 JSON 文件，供前端读取"""
    data_dir = SITE_DIR / "data"
    data_dir.mkdir(exist_ok=True)
    for day in data["days"]:
        (data_dir / f"{day['date']}.json").write_text(
            json.dumps(day, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    index = {"dates": [d["date"] for d in data["days"]]}
    (data_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"JSON 已导出: {data_dir} ({len(data['days'])} 天)")


def generate_site_html(latest_day=None):
    """生成 GitHub Pages 增强版页面（视觉升级版），预埋当天数据避免加载闪烁"""
    preloaded = json.dumps(latest_day or {}, ensure_ascii=False)

    template = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>AI 日报｜给 AI 新手看的每日 AI 简报</title>
<meta name="description" content="每天自动整理值得关注的AI资讯，帮助AI新手和普通创作者快速了解AI圈发生了什么、为什么重要、可以学到什么。">
<meta property="og:title" content="AI 日报｜每日 AI 简报">
<meta property="og:description" content="给AI新手和普通创作者看的每日AI简报，每天自动更新。">
<meta property="og:type" content="website">
<meta property="og:url" content="https://s2846610867.github.io/ai-news/">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Helvetica Neue",Arial,sans-serif;background:#f4f6f9;color:#1e2433;line-height:1.65;-webkit-font-smoothing:antialiased}
a{text-decoration:none;color:inherit}
/* === 粘性顶部栏 === */
.topbar{background:#fff;border-bottom:1px solid #eaedf2;padding:0 20px;height:50px;display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:100;box-shadow:0 1px 8px rgba(0,0,0,.04)}
.topbar-brand{font-size:.88em;font-weight:700;color:#1e2433;letter-spacing:1px;white-space:nowrap;flex-shrink:0}
.topbar-nav{flex:1;overflow-x:auto;white-space:nowrap;text-align:right;scrollbar-width:none;-ms-overflow-style:none}
.topbar-nav::-webkit-scrollbar{display:none}
.topbar-nav a{display:inline-block;margin:0 3px;padding:3px 10px;border-radius:14px;font-size:.74em;color:#6b7280;border:1px solid #e5e7eb;background:#fff}
.topbar-nav a:hover,.topbar-nav a.cur{background:#4f6ef7;color:#fff;border-color:#4f6ef7}
/* === Hero === */
.hero{background:linear-gradient(145deg,#eef2ff 0%,#e8f3fe 45%,#eafaf5 100%);padding:52px 20px 44px;text-align:center;border-bottom:1px solid rgba(79,110,247,.07)}
.hero-badge{display:inline-block;font-size:.68em;font-weight:700;letter-spacing:.18em;text-transform:uppercase;color:#4f6ef7;background:rgba(79,110,247,.1);padding:4px 14px;border-radius:20px;margin-bottom:16px}
.hero h1{font-size:clamp(1.9em,5vw,2.6em);font-weight:800;color:#1e2433;letter-spacing:2px;margin-bottom:10px;line-height:1.15}
.hero-sub{font-size:clamp(.95em,2.5vw,1.08em);color:#374151;font-weight:500;margin-bottom:14px}
.hero-desc{font-size:.875em;color:#6b7280;line-height:1.9;max-width:520px;margin:0 auto 22px}
.hero-tags{display:flex;justify-content:center;flex-wrap:wrap;gap:8px}
.hero-tag{font-size:.76em;color:#6b7280;background:rgba(255,255,255,.75);border:1px solid rgba(0,0,0,.08);padding:4px 12px;border-radius:20px}
/* === 主容器 === */
.wrap{max-width:1200px;margin:0 auto;padding:32px 16px 16px}
/* === 区块标题 === */
.sec-hd{display:flex;align-items:baseline;flex-wrap:wrap;gap:8px 12px;margin-bottom:16px}
.sec-hd-title{font-size:.76em;font-weight:700;text-transform:uppercase;letter-spacing:.15em;color:#4f6ef7}
.sec-hd-sub{font-size:.78em;color:#9ca3af}
/* === 今日最值得关注 3 条 · 三列网格 === */
.top3-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:36px}
@media(max-width:860px){.top3-grid{grid-template-columns:1fr}}
.t3-card{background:#fff;border-radius:16px;padding:20px;box-shadow:0 2px 14px rgba(0,0,0,.055);border:1px solid #eaedf2;display:flex;flex-direction:column;gap:10px;position:relative;overflow:hidden}
.t3-card::before{content:"";position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#4f6ef7,#52c5a8)}
.t3-rank{font-size:.7em;font-weight:700;color:#4f6ef7;letter-spacing:.1em;background:rgba(79,110,247,.08);padding:3px 10px;border-radius:10px;align-self:flex-start}
.t3-title{font-size:.94em;font-weight:700;color:#1e2433;line-height:1.5;display:block;margin-bottom:2px}
.t3-title:hover{color:#4f6ef7}
.t3-sum{font-size:.82em;color:#6b7280;line-height:1.65;flex:1}
.t3-why{background:#f5f3ff;border-radius:8px;padding:8px 10px;font-size:.78em;color:#4b3f8a;line-height:1.6}
.t3-why-lbl{font-weight:700;color:#4f6ef7;margin-right:4px}
.t3-why-default{background:#f9fafb;border-radius:8px;padding:7px 10px;font-size:.76em;color:#9ca3af;line-height:1.6;font-style:italic}
.t3-link{align-self:flex-start;font-size:.76em;color:#4f6ef7;border:1px solid rgba(79,110,247,.25);border-radius:10px;padding:3px 12px;margin-top:auto}
.t3-link:hover{background:#4f6ef7;color:#fff}
/* === GitHub 精选入口 === */
.gh-entry{display:flex;align-items:center;justify-content:space-between;gap:14px;background:#fff;border:1px solid #eaedf2;border-left:4px solid #4f6ef7;border-radius:16px;padding:18px 22px;margin-bottom:36px;box-shadow:0 2px 14px rgba(0,0,0,.055)}
.gh-entry:hover{box-shadow:0 4px 20px rgba(79,110,247,.16);border-color:#4f6ef7}
.gh-entry-main{display:flex;align-items:center;gap:14px;min-width:0}
.gh-entry-icon{font-size:1.6em;flex-shrink:0}
.gh-entry-text{display:flex;flex-direction:column;min-width:0}
.gh-entry-text b{font-size:.98em;font-weight:800;color:#1e2433}
.gh-entry-text i{font-size:.8em;color:#6b7280;font-style:normal;margin-top:2px}
.gh-entry-arrow{font-size:.82em;font-weight:700;color:#4f6ef7;white-space:nowrap;flex-shrink:0}
@media(max-width:600px){.gh-entry{padding:16px}.gh-entry-text i{font-size:.74em}}
/* === 日期块 === */
.day-block{background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 2px 14px rgba(0,0,0,.055);border:1px solid #eaedf2;margin-bottom:28px}
.day-hdr{background:linear-gradient(90deg,#4f6ef7,#764ba2);padding:12px 22px}
.day-hdr h2{font-size:.92em;font-weight:700;color:#fff;letter-spacing:.5px}
/* === 新闻卡片 === */
.nc{padding:16px 22px;border-bottom:1px solid #f4f5f8;display:flex;gap:14px;align-items:flex-start}
.nc:last-child{border-bottom:none}
.nc:hover{background:#fafbff}
.nc-num{width:26px;height:26px;border-radius:50%;background:linear-gradient(135deg,#4f6ef7,#7c9dff);color:#fff;font-size:.7em;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:2px}
.nc-body{flex:1;min-width:0}
.nc-title{font-size:.93em;font-weight:700;color:#1e2433;display:block;margin-bottom:5px;line-height:1.5}
.nc-title:hover{color:#4f6ef7}
.nc-sum{font-size:.82em;color:#6b7280;line-height:1.6}
.nc-why{background:#f0f4ff;border-left:3px solid #4f6ef7;padding:6px 10px;border-radius:0 8px 8px 0;font-size:.78em;color:#374151;margin-top:6px;line-height:1.55}
.nc-why-lbl{font-weight:700;color:#4f6ef7;margin-right:4px}
.nc-learn{background:#f0fff8;border-left:3px solid #52c5a8;padding:5px 10px;border-radius:0 8px 8px 0;font-size:.78em;color:#374151;margin-top:5px;line-height:1.55}
.nc-learn-lbl{font-weight:700;color:#52c5a8;margin-right:4px}
.nc-for-me{background:#f0fdf7;border-left:3px solid #34d399;padding:6px 10px;border-radius:0 8px 8px 0;font-size:.8em;color:#1a4a38;margin-top:6px;line-height:1.6}
.nc-for-me-lbl{font-weight:700;color:#059669;margin-right:4px}
.nc-foot{display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin-top:8px}
.nc-tag{font-size:.69em;padding:2px 8px;border-radius:9px;background:#f3f4f6;color:#9ca3af;border:1px solid #e5e7eb}
.nc-cat{font-size:.69em;padding:2px 8px;border-radius:9px;background:rgba(79,110,247,.08);color:#4f6ef7;border:1px solid rgba(79,110,247,.15)}
.nc-link{font-size:.76em;color:#4f6ef7;border:1px solid rgba(79,110,247,.25);border-radius:10px;padding:2px 10px}
.nc-link:hover{background:#4f6ef7;color:#fff}
/* === 小白总结 === */
.plain-sum{background:linear-gradient(135deg,#f8f9ff,#f0f4fd);border-top:1px solid #e8ecff;padding:18px 22px}
.plain-sum-lbl{font-size:.78em;font-weight:700;color:#4f6ef7;margin-bottom:7px;letter-spacing:.06em}
.plain-sum p{font-size:.86em;color:#4b5563;line-height:1.95}
/* === 底部 === */
.footer{text-align:center;padding:20px 20px 44px;border-top:1px solid #eaedf2;color:#9ca3af;font-size:.75em;line-height:2.3;margin-top:8px}
/* === 其他 === */
.loading{text-align:center;padding:60px 20px;color:#9ca3af;font-size:.9em}
/* === 移动端补丁 === */
@media(max-width:600px){
  .hero{padding:38px 16px 32px}
  .wrap{padding:24px 12px 12px}
  .t3-card{padding:16px}
  .nc{padding:14px 16px}
  .day-hdr{padding:11px 16px}
  .plain-sum{padding:14px 16px}
}
</style>
</head>
<body>

<!-- 粘性顶部：品牌 + 日期导航 -->
<header class="topbar">
  <span class="topbar-brand">🤖 AI 日报</span>
  <nav class="topbar-nav" id="nav"></nav>
</header>

<!-- Hero 浅色渐变 -->
<section class="hero">
  <div class="hero-badge">DAILY AI BRIEFING</div>
  <h1>AI 日报</h1>
  <p class="hero-sub">给 AI 新手和普通创作者看的每日 AI 简报</p>
  <p class="hero-desc">每天自动整理 10 条值得关注的 AI 资讯，用更容易理解的方式告诉你：今天 AI 圈发生了什么，为什么重要，以及普通人可以学到什么。</p>
  <div class="hero-tags">
    <span class="hero-tag">📅 每天早上 6 点自动更新</span>
    <span class="hero-tag">🤖 由 AI 辅助整理</span>
    <span class="hero-tag">✨ 持续人工优化中</span>
  </div>
</section>

<!-- 主内容区 -->
<main class="wrap">

  <!-- 今日最值得关注的 3 条 -->
  <div class="sec-hd">
    <span class="sec-hd-title">⭐ 今日最值得关注的 3 条</span>
    <span class="sec-hd-sub">如果你今天只看 3 条，先看这里。</span>
  </div>
  <div class="top3-grid" id="top3"></div>

  <!-- GitHub 精选入口 -->
  <a href="github.html" class="gh-entry">
    <span class="gh-entry-main">
      <span class="gh-entry-icon">💻</span>
      <span class="gh-entry-text"><b>GitHub 精选</b><i>每天精选 10 个热门 AI 开源项目，附详细中文讲解</i></span>
    </span>
    <span class="gh-entry-arrow">进入查看 →</span>
  </a>

  <!-- 所有日报 -->
  <div class="sec-hd"><span class="sec-hd-title">📰 今日 AI 资讯</span></div>
  <div id="news-list"><div class="loading">正在加载今日资讯…</div></div>

</main>

<!-- 底部 -->
<footer class="footer">
  内容由 AI 辅助整理 · 信息来自公开来源 · 仅供学习参考 · 持续优化中<br>
  <span id="footer-ts"></span>
</footer>
<noscript><p style="text-align:center;padding:60px 20px;color:#9ca3af">请启用 JavaScript 以浏览 AI 日报内容。</p></noscript>

<script>
const WD=["周日","周一","周二","周三","周四","周五","周六"];
const EMBEDDED=__PRELOADED_JSON__;
function esc(s){return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");}
function fmtDate(d){const[y,m,day]=d.split("-");return y+"年"+m+"月"+day+"日 "+WD[new Date(d+"T00:00:00").getDay()];}

function renderTop3(day){
  if(!day||!day.articles||!day.articles.length)return;
  const ranks=["#01","#02","#03"];
  const html=day.articles.slice(0,3).map((a,i)=>{
    const w=a.why||a.why_it_matters||"";
    const whyH=w
      ?`<div class="t3-why"><span class="t3-why-lbl">💡 为什么重要</span>${esc(w)}</div>`
      :`<div class="t3-why-default">值得关注它对普通用户、创作者或 AI 学习者的实际影响。</div>`;
    const lnk=a.url&&a.url!="#"?`<a href="${esc(a.url)}" target="_blank" rel="noopener" class="t3-link">阅读原文 →</a>`:"";
    return `<div class="t3-card">
      <span class="t3-rank">${ranks[i]}</span>
      <a href="${esc(a.url||"#")}" target="_blank" rel="noopener" class="t3-title">${esc(a.title||"")}</a>
      <p class="t3-sum">${esc(a.summary||"")}</p>
      ${whyH}${lnk}
    </div>`;
  }).join("");
  document.getElementById("top3").innerHTML=html;
}

function renderDay(day){
  const items=day.articles.map((a,i)=>{
    const w=a.why||a.why_it_matters||"";
    const whyH=w?`<div class="nc-why"><span class="nc-why-lbl">💡 为什么重要</span>${esc(w)}</div>`:"";
    const catH=a.category?`<span class="nc-cat">${esc(a.category)}</span>`:`<span class="nc-tag">AI 资讯</span>`;
    const lrnH=a.beginner_takeaway?`<div class="nc-learn"><span class="nc-learn-lbl">📖 新手可学</span>${esc(a.beginner_takeaway)}</div>`:"";
    const forMeH=a.for_me?`<div class="nc-for-me"><span class="nc-for-me-lbl">👤 对我意味着</span>${esc(a.for_me)}</div>`:"";
    const lnkH=a.url&&a.url!="#"?`<a href="${esc(a.url)}" target="_blank" rel="noopener" class="nc-link">原文 →</a>`:"";
    return `<div class="nc">
      <span class="nc-num">${i+1}</span>
      <div class="nc-body">
        <a href="${esc(a.url||"#")}" target="_blank" rel="noopener" class="nc-title">${esc(a.title||"")}</a>
        <p class="nc-sum">${esc(a.summary||"")}</p>
        ${whyH}${forMeH}${lrnH}
        <div class="nc-foot">${catH}${lnkH}</div>
      </div>
    </div>`;
  }).join("");
  return `<div class="day-block" id="${day.date}">
    <div class="day-hdr"><h2>${fmtDate(day.date)}</h2></div>
    <div class="news-list">${items}</div>
    <div class="plain-sum"><div class="plain-sum-lbl">🤖 小白看这里</div><p>${esc(day.plain_summary||"")}</p></div>
  </div>`;
}

if(EMBEDDED&&EMBEDDED.articles&&EMBEDDED.articles.length){
  renderTop3(EMBEDDED);
  document.getElementById("news-list").innerHTML=renderDay(EMBEDDED);
}

async function load(){
  try{
    const {dates}=await fetch("data/index.json").then(r=>r.json());
    const days=await Promise.all(dates.map(d=>fetch("data/"+d+".json").then(r=>r.json())));
    document.getElementById("nav").innerHTML=dates.map((d,i)=>`<a href="#${d}"${i===0?' class="cur"':""}>${d}</a>`).join("");
    renderTop3(days[0]);
    document.getElementById("news-list").innerHTML=days.map(renderDay).join("");
    const ts=document.getElementById("footer-ts");
    if(ts)ts.textContent="最后更新："+dates[0];
  }catch(e){console.warn("fetch failed:",e);}
}
load();
</script>
</body>
</html>"""

    content = template.replace("__PRELOADED_JSON__", preloaded)
    (SITE_DIR / "index.html").write_text(content, encoding="utf-8")
    log(f"站点页面已生成: {SITE_DIR / 'index.html'}")


def generate_github_html(day):
    """生成独立的「GitHub 精选」页面(github.html), 服务端直出, 含详细讲解"""
    g = (day or {}).get("github") or {}
    date = (day or {}).get("date", "")

    def cards(arr):
        if not arr:
            return '<p class="ghp-empty">今日暂无数据</p>'
        out = []
        for i, r in enumerate(arr, 1):
            url = html.escape(r.get("url", "#"))
            lang = f'<span class="ghp-lang">{html.escape(r.get("language") or "")}</span>' if r.get("language") else ""
            tag = html.escape(r.get("tagline") or "")
            exp = html.escape(r.get("explanation") or r.get("desc") or "")
            tag_h = f'<div class="ghp-tag">{tag}</div>' if tag else ""
            out.append(f"""<div class="ghp-card">
        <div class="ghp-hd">
          <span class="ghp-rank">{i}</span>
          <a href="{url}" target="_blank" rel="noopener" class="ghp-name">{html.escape(r.get('name',''))}</a>
          <span class="ghp-star">★ {html.escape(str(r.get('stars',0)))}</span>
        </div>
        {tag_h}
        <div class="ghp-exp">{exp}</div>
        <div class="ghp-foot">{lang}<a href="{url}" target="_blank" rel="noopener" class="ghp-open">打开 GitHub →</a></div>
      </div>""")
        return "\n".join(out)

    top_html = cards(g.get("top_starred"))
    trend_html = cards(g.get("trending"))
    page = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>GitHub 精选 ｜ AI 日报</title>
<meta name="description" content="每天精选热门 AI 开源项目，附详细中文讲解，不用看英文也能看懂这个项目能做什么。">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Helvetica Neue",Arial,sans-serif;background:#f4f6f9;color:#1e2433;line-height:1.7;-webkit-font-smoothing:antialiased}
a{text-decoration:none;color:inherit}
.top{background:#fff;border-bottom:1px solid #eaedf2;padding:0 20px;height:50px;display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:100;box-shadow:0 1px 8px rgba(0,0,0,.04)}
.top a{font-size:.82em;color:#4f6ef7;font-weight:600}
.top-brand{font-size:.86em;font-weight:700;margin-left:auto;color:#1e2433}
.hero{background:linear-gradient(145deg,#eef2ff 0%,#e8f3fe 45%,#eafaf5 100%);padding:42px 20px 34px;text-align:center;border-bottom:1px solid rgba(79,110,247,.07)}
.hero h1{font-size:clamp(1.6em,5vw,2.1em);font-weight:800;letter-spacing:1px;margin-bottom:8px}
.hero p{font-size:.86em;color:#6b7280}
.wrap{max-width:860px;margin:0 auto;padding:30px 16px 50px}
.sec{font-size:1.05em;font-weight:800;color:#1e2433;margin:10px 0 16px;display:flex;align-items:center;gap:8px}
.sec.second{margin-top:38px}
.ghp-card{background:#fff;border-radius:16px;padding:18px 20px;margin-bottom:16px;box-shadow:0 2px 14px rgba(0,0,0,.055);border:1px solid #eaedf2}
.ghp-hd{display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap}
.ghp-rank{width:24px;height:24px;border-radius:50%;background:linear-gradient(135deg,#4f6ef7,#7c9dff);color:#fff;font-size:.74em;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.ghp-name{font-size:1em;font-weight:800;color:#1e2433;word-break:break-all}
.ghp-name:hover{color:#4f6ef7}
.ghp-star{font-size:.78em;color:#f59e0b;font-weight:700;white-space:nowrap;margin-left:auto}
.ghp-tag{font-size:.9em;color:#4f6ef7;font-weight:600;margin-bottom:8px}
.ghp-exp{font-size:.9em;color:#4b5563;line-height:1.85}
.ghp-foot{display:flex;align-items:center;gap:10px;margin-top:12px}
.ghp-lang{font-size:.7em;color:#6b7280;background:#f3f4f6;border:1px solid #e5e7eb;padding:2px 9px;border-radius:9px}
.ghp-open{font-size:.78em;color:#4f6ef7;border:1px solid rgba(79,110,247,.3);border-radius:10px;padding:3px 12px;margin-left:auto}
.ghp-open:hover{background:#4f6ef7;color:#fff}
.ghp-empty{color:#9ca3af;font-size:.9em;padding:20px 0}
.foot{text-align:center;color:#9ca3af;font-size:.75em;padding:10px 20px 30px}
@media(max-width:600px){.wrap{padding:22px 12px 40px}.ghp-card{padding:16px}}
</style>
</head>
<body>
<header class="top">
  <a href="index.html">← 返回 AI 日报</a>
  <span class="top-brand">🤖 AI 日报</span>
</header>
<section class="hero">
  <h1>💻 GitHub 精选</h1>
  <p>每天精选热门 AI 开源项目 · 附详细中文讲解 · 不用看英文也能懂</p>
</section>
<main class="wrap">
  <div class="sec">⭐ AI 星标总榜</div>
  __TOP__
  <div class="sec second">🔥 近期飙升</div>
  __TREND__
</main>
<footer class="foot">数据来自 GitHub · 讲解由 AI 生成 · 仅供参考 · 最后更新 __DATE__</footer>
</body>
</html>"""
    page = page.replace("__TOP__", top_html).replace("__TREND__", trend_html).replace("__DATE__", html.escape(date))
    (SITE_DIR / "github.html").write_text(page, encoding="utf-8")
    log(f"GitHub 精选页已生成: {SITE_DIR / 'github.html'}")


# =========================================================
# 主流程
# =========================================================

def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            log("⚠️ 历史数据损坏, 尝试从 data/ 重建")
    # 历史文件不存在(如云端 CI)时, 从站点 data/ 目录重建历史, 让仓库成为唯一数据源
    data_dir = SITE_DIR / "data"
    if data_dir.exists():
        days = []
        for f in sorted(data_dir.glob("*.json")):
            if f.name == "index.json":
                continue
            try:
                day = json.loads(f.read_text(encoding="utf-8"))
                if day.get("date"):
                    days.append(day)
            except Exception:
                continue
        if days:
            days.sort(key=lambda d: d["date"], reverse=True)
            log(f"从 data/ 重建历史: {len(days)} 天")
            return {"days": days}
    return {"days": []}


def save_data(data: dict):
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args():
    parser = argparse.ArgumentParser(description="生成 AI 日报")
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使今天已经生成过，也重新抓取并覆盖今天的日报",
    )
    return parser.parse_args()


def main(force=False):
    today = datetime.now().strftime("%Y-%m-%d")
    log("=" * 40)
    log("AI 日报生成器启动")

    data = load_data()

    if any(d["date"] == today for d in data["days"]) and not force:
        log(f"今天 ({today}) 已生成, 退出")
        return
    if force:
        log(f"强制重新生成今天 ({today}) 的日报")
        data["days"] = [d for d in data["days"] if d["date"] != today]

    news_items = fetch_all_news()
    if len(news_items) < 3:
        log("❌ 新闻数量不足, 退出")
        notify("AI 日报失败", f"新闻数量不足: {len(news_items)} 条")
        sys.exit(1)

    # —— 跨天去重: 把最近 7 天已出现过的链接从候选里剔除 ——
    prev_days = data["days"]  # 此时今天还没插入, 全是历史
    recent_urls = set()
    for d in prev_days[:7]:
        for a in d.get("articles", []):
            u = (a.get("url") or "").strip()
            if u:
                recent_urls.add(u)
    filtered = [it for it in news_items if (it.get("url") or "").strip() not in recent_urls]
    if len(filtered) >= 8 and len(filtered) < len(news_items):
        log(f"跨天去重: 剔除最近7天已出现过的 {len(news_items) - len(filtered)} 条")
        news_items = filtered
    # 最近 5 天的标题, 交给 DeepSeek 避免换皮重复报道同一件事
    recent_titles = [a.get("title", "") for d in prev_days[:5] for a in d.get("articles", [])]

    log("DeepSeek 整理中...")
    api_key = get_api_key()
    try:
        processed = process_with_deepseek(news_items, api_key, recent_titles)
    except Exception as e:
        log(f"❌ DeepSeek 处理失败: {e}")
        notify("AI 日报失败", f"DeepSeek 处理失败: {e}")
        sys.exit(1)

    if not processed["articles"]:
        log("❌ DeepSeek 未返回任何文章, 退出")
        notify("AI 日报失败", "DeepSeek 未返回任何文章")
        sys.exit(1)

    # GitHub 热门 AI 项目板块(失败不影响日报主体)
    github = {}
    try:
        github = fetch_github()
        if github.get("top_starred") or github.get("trending"):
            add_github_explanations(github, api_key)
    except Exception as e:
        log(f"GitHub 板块失败(已跳过): {e}")
        github = {}

    data["days"].insert(0, {
        "date":          today,
        "articles":      processed["articles"],
        "plain_summary": processed["plain_summary"],
        "github":        github,
    })
    data["days"] = data["days"][:30]  # 保留最近 30 天

    save_data(data)
    export_json(data)
    generate_site_html(data["days"][0] if data["days"] else None)
    generate_github_html(data["days"][0] if data["days"] else None)
    generate_html(data)
    log("✅ 完成!")


if __name__ == "__main__":
    try:
        args = parse_args()
        main(force=args.force)
    except Exception as e:
        log(f"❌ 未捕获异常: {e}")
        notify("AI 日报失败", str(e))
        sys.exit(1)
