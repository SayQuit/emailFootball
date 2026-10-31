"""整理足球新闻和赛程，用 DeepSeek 生成中文简报，发送到 QQ 邮箱。

外媒新闻以 BBC、卫报、ESPN、Managing Madrid 的 RSS 为主，从原网页提取正文，
提取不到时用 DeepSeek 读取网页 API（/v1/tools/fetch）；再用 DeepSeek 搜索 API
（/v1/tools/search，返回带全文的搜索结果）补充 RSS 没有的新闻。
中国足球新闻和赛程完全由 DeepSeek 搜索查找。
所有新闻都由 DeepSeek 对话模型改写成中文标题、摘要和全文，赛程也由它从搜索结果里提取。

邮件结构:
    一、皇家马德里及西甲
    二、英超焦点新闻
    三、国际足坛其他新闻
    四、中国足球
    五、近期赛程（一周内西甲/英超/欧冠豪门对决、中超未来 3 轮、中国各级国家队、重要大赛）

环境变量:
    QQ_EMAIL          发件 QQ 邮箱地址
    QQ_AUTH_CODE      QQ 邮箱 SMTP 授权码（不是 QQ 密码）
    DEEPSEEK_API_KEY  DeepSeek API Key（必需）
    DEEPSEEK_MODEL    可选，默认 deepseek-chat
    MAIL_TO           可选，收件地址，默认发给 QQ_EMAIL 自己

本地预览（不发邮件）:
    python main.py --dry-run
"""

import html
import json
import os
import re
import smtplib
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from functools import lru_cache
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import feedparser
import trafilatura

BEIJING = timezone(timedelta(hours=8))
LOOKBACK_HOURS = 30  # 新闻发布时间要在这个范围内
MAX_FOREIGN_ARTICLES = 45  # 外媒新闻上限，控制运行时间和 API 费用
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
ARTICLE_MAX_CHARS = 6000
FIXTURE_DAYS = 7
MIN_RERANK_SCORE = 1.0  # DeepSeek 搜索的相关度分数（0–3），太低的结果不要
WORKERS = 5

DEEPSEEK_BASE = "https://api.deepseek.com"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL") or "deepseek-chat"

SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465

SECTIONS = [
    ("madrid", "一、皇家马德里及西甲"),
    ("epl", "二、英超焦点新闻"),
    ("world", "三、国际足坛其他新闻"),
    ("china", "四、中国足球"),
]

# 外媒新闻源：(来源名, RSS 地址, 栏目提示)。栏目提示为 None 的综合源按关键词归类
FEEDS = [
    ("Managing Madrid", "https://www.managingmadrid.com/rss/current.xml", "madrid"),
    ("卫报", "https://www.theguardian.com/football/realmadrid/rss", "madrid"),
    ("卫报", "https://www.theguardian.com/football/laligafootball/rss", "laliga"),
    ("卫报", "https://www.theguardian.com/football/premierleague/rss", "epl"),
    ("BBC Sport", "https://feeds.bbci.co.uk/sport/football/premier-league/rss.xml", "epl"),
    ("卫报", "https://www.theguardian.com/football/championsleague/rss", "world"),
    ("BBC Sport", "https://feeds.bbci.co.uk/sport/football/champions-league/rss.xml", "world"),
    ("卫报", "https://www.theguardian.com/football/world-cup-2026/rss", "world"),
    ("BBC Sport", "https://feeds.bbci.co.uk/sport/football/rss.xml", None),
    ("卫报", "https://www.theguardian.com/football/rss", None),
    ("ESPN", "https://www.espn.com/espn/rss/soccer/news", None),
]
REAL_MADRID_PATTERN = re.compile(r"real madrid|bernab[eé]u", re.IGNORECASE)
CHAMPIONS_LEAGUE_PATTERN = re.compile(r"champions league|europa league|conference league", re.IGNORECASE)
LALIGA_PATTERN = re.compile(
    r"la ?liga|barcelona|bar[cç]a\b|atl[eé]tico madrid|sevilla|villarreal|real betis|real sociedad|"
    r"athletic club|valencia|girona|osasuna|celta vigo|getafe|mallorca|rayo vallecano|alav[eé]s|espanyol",
    re.IGNORECASE,
)
EPL_PATTERN = re.compile(
    r"premier league|arsenal|chelsea|liverpool|manchester united|man utd|manchester city|man city|"
    r"tottenham|spurs|newcastle|aston villa|everton|brighton|crystal palace|fulham|brentford|"
    r"bournemouth|nottingham forest|leeds|sunderland|coventry|ipswich|hull city",
    re.IGNORECASE,
)
SKIP_TITLE_PATTERN = re.compile(r"\bthread\b|podcast|quiz|\bfpl\b|fantasy|gossip", re.IGNORECASE)
OG_IMAGE_PATTERN = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', re.IGNORECASE)
OG_IMAGE_REVERSED_PATTERN = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.IGNORECASE)

# DeepSeek 搜索：(栏目, 最多几条, 搜索词)。外媒栏目是对 RSS 的补充，中国足球完全靠搜索
SEARCH_SECTIONS = [
    ("madrid", 6, ["Real Madrid lang:en", "LaLiga OR Barcelona OR Atletico Madrid lang:en"]),
    ("epl", 5, ["Premier League lang:en"]),
    ("world", 5, ["Champions League OR national team football lang:en", "Serie A OR Bundesliga OR Ligue 1 lang:en"]),
    ("china", 12, [
        "中超 lang:zh",
        "中甲 lang:zh",
    ]),
]

# 重点关注的话题：这些新闻不受条数上限限制，排在所在栏目最前面，并标「重点」
PRIORITY_SEARCHES = [
    ("world", "Cristiano Ronaldo OR Al Nassr lang:en"),
    ("madrid", "Mbappe lang:en"),
    ("china", "国足 lang:zh"),
    ("china", "U23国足 OR 国奥 lang:zh"),
    ("china", "U21国足 OR U20国足 OR U19国足 OR U17国足 OR U15国足 lang:zh"),
    ("china", "中国 年轻球员 OR 新星 OR 小将 OR 留洋 足球 lang:zh"),
]
PRIORITY_LIMIT_PER_QUERY = 6
PRIORITY_PATTERN = re.compile(
    r"ronaldo|al[- ]?nassr|mbapp[eé]|C罗|罗纳尔多|利雅得胜利|姆巴佩|国足|国奥|中国男足|U\d\d国足|留洋|小将|新星",
    re.IGNORECASE,
)
PRIORITY_TOPICS = "C罗、利雅得胜利、姆巴佩、中国男足各级国家队（国足、国奥、U23、U21、U20、U17 等）、中国年轻潜力球员"

# 赛程搜索词。搜索结果交给 DeepSeek 提取未来一周的比赛
FIXTURE_QUERIES = [
    "西甲 赛程 北京时间",
    "英超 赛程 北京时间",
    "欧冠 赛程 北京时间",
    "中超 赛程 轮次 北京时间",
    "中超 下一轮 对阵",
    "国足 比赛 时间 对手",
    "U23国足 OR 国奥 比赛 赛程",
    "U21国足 OR U20国足 OR U19国足 OR U17国足 比赛 赛程",
    # 正在进行或即将开始的重要大赛
    "世界杯 OR 欧洲杯 OR 亚洲杯 OR 美洲杯 OR 非洲杯 赛程 北京时间",
    "U23亚洲杯 OR 世青赛 OR 世少赛 OR 奥运会男足 赛程",
]
FIXTURE_LOOKAHEAD_DAYS = 90  # 中超 3 轮、国家队比赛、大赛可能在一周之后，提取范围放宽到三个月
BIG_CLUBS = "皇马、巴萨、马竞、阿森纳、切尔西、利物浦、曼城、曼联、热刺、拜仁、多特蒙德、勒沃库森、尤文、国米、AC米兰、那不勒斯、巴黎圣日耳曼"

# 赛程里的图标：ESPN 的公开图片（只是图片地址，不调用它的接口）
ESPN_IMG = "https://a.espncdn.com/combiner/i?img=/i/{path}.png&w={size}&h={size}"
COMPETITION_ICONS = {"西甲": "leaguelogos/soccer/500/15", "英超": "leaguelogos/soccer/500/23", "欧冠": "leaguelogos/soccer/500/2"}
TEAM_ICONS = {
    "皇马": 86, "巴萨": 83, "马竞": 1068, "阿森纳": 359, "切尔西": 363, "利物浦": 364, "曼城": 382,
    "曼联": 360, "热刺": 367, "拜仁": 132, "多特蒙德": 124, "勒沃库森": 131, "尤文": 111,
    "国米": 110, "AC米兰": 103, "那不勒斯": 114, "巴黎圣日耳曼": 160,
}
CHINA_TEAM_PATTERN = re.compile(r"国足|国奥|中国")
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 论坛帖、直播、视频、播客、竞猜等没有新闻价值
SKIP_URL_PATTERN = re.compile(r"/live/|/videos?/|/audio/|/podcast|/sounds/|/iplayer/|/quiz", re.IGNORECASE)
BAD_IMAGE_PATTERN = re.compile(r"logo|icon|avatar|placeholder|sprite|pixel|1x1|\.svg|\.gif", re.IGNORECASE)
MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)")

EDITOR_RULES = (
    "你是资深足球新闻编辑，为中国读者整理每日足球简报。"
    "全部使用简体中文，不要出现英文句子；外国人名、球队名使用中文通用译名"
    "（如维尼修斯、姆巴佩、贝林厄姆、亚马尔、B费、阿尔特塔、马竞、国米）。"
    "只陈述资料中的事实，不编造，不加评论。只输出 JSON。"
)


# ---------- DeepSeek ----------

def deepseek_post(path, payload, timeout):
    """调用 DeepSeek 接口，429/5xx 自动重试，失败返回 None。"""
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}"}
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(3):
        try:
            with urlopen(Request(DEEPSEEK_BASE + path, data=body, headers=headers), timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            print(f"DeepSeek {path} 返回 {e.code}（第 {attempt + 1} 次）")
            if e.code not in (408, 429, 500, 502, 503, 504):
                return None
        except Exception as e:
            print(f"DeepSeek {path} 调用失败（第 {attempt + 1} 次）: {e}")
        time.sleep(3 * (attempt + 1))
    return None


def search(query, count=10):
    """DeepSeek 搜索，返回带全文的结果列表。"""
    data = deepseek_post("/v1/tools/search", {"query": query[:128], "count": count, "need_content": True}, timeout=60)
    results = (data or {}).get("results", [])
    print(f"搜索「{query}」: {len(results)} 条")
    return results


def fetch_page(url):
    """DeepSeek 读取网页，返回 Markdown 正文，失败返回空字符串。"""
    data = deepseek_post("/v1/tools/fetch", {"url": url}, timeout=60)
    return (data or {}).get("content", "")


def chat(user, max_tokens=800):
    """DeepSeek 对话，要求返回 JSON，解析失败返回 None。"""
    data = deepseek_post("/chat/completions", {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "system", "content": EDITOR_RULES}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }, timeout=240)  # 全文翻译输出较长，超时留足
    try:
        return json.loads(data["choices"][0]["message"]["content"])
    except Exception:
        return None


# ---------- 新闻 ----------

def normalize(title):
    return re.sub(r"\W+", "", title or "").lower()


def parse_date(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def small_image(url):
    """把常见网站的配图换成 320–460 宽的小图。"""
    url = html.unescape(url or "")
    # BBC: /ace/branded_sport/1200/cpsprodpb/... → /news/320/cpsprodpb/...
    url = re.sub(r"ichef\.bbci\.co\.uk/(?:ace/[a-z_]+|news)/\d+/", "ichef.bbci.co.uk/news/320/", url)
    if "managingmadrid.com" in url:
        parts = urlsplit(url)
        query = [(k, "320" if k == "w" else v) for k, v in parse_qsl(parts.query) if k != "crop"]
        url = urlunsplit(parts._replace(query=urlencode(query)))
    return url


def first_image(markdown):
    for url in MARKDOWN_IMAGE_PATTERN.findall(markdown or ""):
        if not BAD_IMAGE_PATTERN.search(url):
            return small_image(url)
    return ""


def clean_text(text):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return " ".join(text.split())


def feed_image(entry):
    """RSS 自带配图，选宽度最接近 460 的一张（卫报的图片地址带签名，只能用 RSS 里现成的尺寸）。"""
    candidates = [c for c in list(entry.get("media_content", [])) + list(entry.get("media_thumbnail", [])) if c.get("url")]
    if not candidates:
        return ""
    return min(candidates, key=lambda c: abs(int(c.get("width") or 1200) - 460))["url"]


@lru_cache(maxsize=None)
def fetch_feed(source, url):
    feed = feedparser.parse(url, agent=USER_AGENT)
    if feed.get("bozo") and not feed.entries:
        print(f"{source} 抓取失败: {url}: {feed.get('bozo_exception')}")
        return ()
    items = []
    for entry in feed.entries:
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        items.append({
            "title": clean_text(entry.get("title", "")),
            "summary": clean_text(entry.get("summary", ""))[:600],
            "source": source,
            "link": entry.get("link", ""),
            "image": feed_image(entry),
            "published": datetime(*published[:6], tzinfo=timezone.utc) if published else None,
        })
    print(f"{source}: {len(items)} 条 ({url[:60]})")
    return tuple(items)


def classify(item, hint):
    text = f"{item['title']} {item['summary']}"
    if REAL_MADRID_PATTERN.search(text):
        return "madrid"
    if hint == "world" or CHAMPIONS_LEAGUE_PATTERN.search(text):
        return "world"
    if hint in ("madrid", "laliga") or LALIGA_PATTERN.search(text):
        return "madrid"
    if hint == "epl" or EPL_PATTERN.search(text):
        return "epl"
    return "world"


def collect_foreign_news(seen):
    """读外媒 RSS，正文在 summarize 时再去原网页提取。"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    items = []
    for source, url, hint in FEEDS:
        for raw in fetch_feed(source, url):
            item = dict(raw)
            key = normalize(item["title"])
            if not key or key in seen or item["link"] in seen:
                continue
            if not item["published"] or item["published"] < cutoff:
                continue
            if SKIP_TITLE_PATTERN.search(item["title"]) or SKIP_URL_PATTERN.search(item["link"]):
                continue
            seen.update((key, item["link"]))
            item["section"] = classify(item, hint)
            item["priority"] = bool(PRIORITY_PATTERN.search(f"{item['title']} {item['summary']}"))
            items.append(item)
    # 重点话题全部保留，其余按时间取最新的
    priority = [i for i in items if i["priority"]]
    others = sorted((i for i in items if not i["priority"]), key=lambda i: i["published"], reverse=True)
    kept = priority + others[:max(0, MAX_FOREIGN_ARTICLES - len(priority))]
    print(f"外媒新闻 {len(items)} 条，保留 {len(kept)} 条（重点 {len(priority)} 条）")
    return kept


def fetch_article(url):
    """打开原文提取正文和 og:image，失败返回空值。"""
    try:
        downloaded = trafilatura.fetch_url(url)
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=False) if downloaded else None
    except Exception as e:
        print(f"正文提取失败 {url}: {e}")
        return "", ""
    if not downloaded:
        return "", ""
    match = OG_IMAGE_PATTERN.search(downloaded) or OG_IMAGE_REVERSED_PATTERN.search(downloaded)
    image = html.unescape(match.group(1)) if match else ""
    if not image.startswith("http") or BAD_IMAGE_PATTERN.search(image):
        image = ""
    return (text or "")[:ARTICLE_MAX_CHARS], image


def search_news(query, section, seen, priority=False):
    """搜索一个词，只保留时间范围内、相关度够高、有正文、seen 里没有的结果，按相关度排序。"""
    after = (datetime.now(BEIJING) - timedelta(days=2)).strftime("%Y-%m-%d")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    found = []
    for r in search(f"{query} after:{after}"):
        published = parse_date(r.get("date"))
        title_key = normalize(r.get("title"))
        if not published or published < cutoff:
            continue
        if (r.get("rerank_score") or 0) < MIN_RERANK_SCORE or len(r.get("content") or "") < 300:
            continue
        if r.get("url") in seen or title_key in seen or SKIP_URL_PATTERN.search(r.get("url", "")):
            continue
        seen.update((r.get("url"), title_key))
        found.append({
            "section": section,
            "title": r.get("title", ""),
            "source": r.get("site_name") or urlsplit(r.get("url", "")).hostname or "",
            "published": published,
            "content": (r.get("content") or "")[:ARTICLE_MAX_CHARS],
            "image": first_image(r.get("content")),
            "score": r.get("rerank_score") or 0,
            "priority": priority or bool(PRIORITY_PATTERN.search(r.get("title", ""))),
        })
    # 相关度高的优先，再按时间
    return sorted(found, key=lambda i: (i["score"], i["published"]), reverse=True)


def collect_search_news(seen):
    """用 DeepSeek 搜索：先搜重点话题，再给外媒栏目补充 RSS 没有的新闻，并找中国足球新闻。
    seen 里已有的（RSS 已收录的）跳过。"""
    items = []
    for section, query in PRIORITY_SEARCHES:
        found = search_news(query, section, seen, priority=True)[:PRIORITY_LIMIT_PER_QUERY]
        items.extend(found)
        print(f"[重点] {query}: {len(found)} 条")
    for section, limit, queries in SEARCH_SECTIONS:
        candidates = [i for q in queries for i in search_news(q, section, seen)]
        candidates.sort(key=lambda i: (i["score"], i["published"]), reverse=True)
        # 搜到的重点话题不占条数
        kept = [i for i in candidates if i["priority"]] + [i for i in candidates if not i["priority"]][:limit]
        items.extend(kept)
        print(f"[{section}] 搜索补充 {len(kept)} 条（候选 {len(candidates)} 条）")
    return items


def summarize(item):
    """生成中文标题、1–2 句摘要和全文。返回 None 表示这条不要。"""
    if "content" not in item:
        # RSS 新闻：去原网页提取正文；提取不到（例如 ESPN 靠 JavaScript 渲染）就让 DeepSeek 读取，再不行用 RSS 简介
        body, og_image = fetch_article(item["link"])
        if len(body) < 300:
            page = fetch_page(item["link"])
            if len(page) > len(body):
                body = page[:ARTICLE_MAX_CHARS]
                og_image = og_image or first_image(page)
        item["content"] = body or item["summary"]
        # 卫报的 og:image 是带签名的大图，优先用 RSS 里的小图
        item["image"] = small_image(item["image"] if "guim.co.uk" in item["image"] else (og_image or item["image"]))
    result = chat(
        f"媒体：{item['source']}\n原标题：{item['title']}\n正文：\n{item['content']}\n\n"
        "请输出 JSON：{\"title\": \"简短的中文标题，不超过 20 字\", "
        "\"summary\": \"1–2 句中文摘要，50–90 字，只讲最关键的事实、比分或数据\", "
        "\"source\": \"媒体名称，如 BBC Sport、卫报、ESPN、马卡报、新浪体育\", "
        f"\"priority\": 这条新闻是否主要关于{PRIORITY_TOPICS}（true 或 false）, "
        "\"paragraphs\": [\"正文整理成通顺的中文全文（英文原文就完整翻译，中文原文就保留原文措辞），"
        "按原文分段，每段一个字符串；去掉与新闻无关的内容（广告、导航、评论区、相关链接、图片说明、版权声明）\"]}。"
        "如果这不是一条足球新闻报道（例如赛程表、节目预告、竞猜、问答、广告、球队主页），"
        "或者讲的是一个多月前的旧事，只输出 {\"skip\": true}。",
        max_tokens=6000,
    )
    if not result or result.get("skip") or not result.get("title") or not result.get("summary"):
        print(f"跳过: {item['title']}")
        return None
    item["title_zh"], item["summary_zh"] = result["title"].strip(), result["summary"].strip()
    item["source"] = str(result.get("source") or item["source"]).strip()
    item["paragraphs"] = [p.strip() for p in result.get("paragraphs") or [] if isinstance(p, str) and p.strip()]
    item["priority"] = item.get("priority") or result.get("priority") is True
    return item


def summarize_all(items):
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return [i for i in pool.map(summarize, items) if i]


def drop_duplicates(items):
    """去掉报道同一事件的重复条目。"""
    listing = "\n".join(f"{n}. {i['title_zh']}：{i['summary_zh']}" for n, i in enumerate(items))
    result = chat(
        f"以下是今天的足球新闻（编号. 标题：摘要）：\n{listing}\n\n"
        "请输出 JSON：{\"duplicates\": [报道同一事件、应当删掉的较差条目编号]}。没有重复就输出空列表。",
        max_tokens=500,
    ) or {}
    drop = {d for d in result.get("duplicates", []) if isinstance(d, int)}
    if drop:
        print(f"合并重复报道 {len(drop)} 条")
    return [i for n, i in enumerate(items) if n not in drop]


# ---------- 赛程 ----------

def collect_fixtures():
    """搜索赛程资料，让 DeepSeek 提取接下来的比赛，按赛事分组。"""
    now = datetime.now(BEIJING)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=FIXTURE_LOOKAHEAD_DAYS)
    week_end = start + timedelta(days=FIXTURE_DAYS - 1)
    after = (now - timedelta(days=30)).strftime("%Y-%m-%d")

    sources = []
    for query in FIXTURE_QUERIES:
        for r in search(f"{query} after:{after}", count=5):
            sources.append(f"【{r.get('title', '')}】（{r.get('date') or '日期未知'}）\n{(r.get('content') or r.get('summary') or '')[:1500]}")

    result = chat(
        f"今天是北京时间 {now:%Y-%m-%d %A}。以下是搜索到的赛程资料：\n\n" + "\n\n".join(sources) + "\n\n"
        "请从资料中提取今天及以后的比赛（北京时间），输出 JSON：\n"
        "{\"groups\": [{\"name\": \"赛事组名\", \"matches\": [{\"date\": \"YYYY-MM-DD\", \"time\": \"HH:MM，北京时间，不确定就写待定\", "
        "\"home\": \"主队\", \"away\": \"客队\", \"note\": \"轮次或赛事名，可留空\"}]}]}\n"
        "分组规则（按这个顺序，没有比赛的组不要输出）：\n"
        f"1. 西甲、英超、欧冠：每项赛事一组，组名就叫「西甲」「英超」「欧冠」，只列 {start:%Y-%m-%d} 到 {week_end:%Y-%m-%d} 之间的比赛。"
        f"只列两队都属于这些豪门的对决：{BIG_CLUBS}；皇马的比赛不论对手都要列出。\n"
        "2. 中超：一组，组名「中超」，列出接下来 3 个比赛日（3 轮）的全部比赛，note 写第几轮。赛季已经结束就不输出这一组。\n"
        f"3. 中国各级男子国家队（国足、国奥、U23、U21、U20、U19、U17 等）：一组，组名「中国国家队」，"
        f"列出 {(end - timedelta(days=1)):%Y-%m-%d} 之前的全部比赛；home 写我方球队（如「国足」「U23国足」），away 写对手，note 写赛事名。\n"
        "4. 重要大赛（世界杯、欧洲杯、亚洲杯、美洲杯、非洲杯、U23亚洲杯、世青赛、奥运会男足等）：如果正在进行，或者三个月内开始，"
        f"每项大赛一组，组名用大赛名称（如「2027亚洲杯」），列出 {(start + timedelta(days=13)):%Y-%m-%d} 之前的比赛；"
        "还没到比赛日期的，列出开幕后的前几场比赛。已经结束的大赛不要输出。\n"
        f"球队名请用中文简称，豪门统一写成：{BIG_CLUBS}；中国男足成年队写「国足」。\n"
        "只列资料里明确写了日期的比赛，资料互相矛盾或看不出日期的不要列，不要凭记忆补充。",
        max_tokens=6000,
    ) or {}

    groups = []
    for g in result.get("groups", []):
        if not isinstance(g, dict):
            continue
        matches = []
        for m in g.get("matches", []):
            try:
                day = datetime.strptime(str(m.get("date")), "%Y-%m-%d").replace(tzinfo=BEIJING)
            except (ValueError, TypeError):
                continue
            if not (start <= day < end):
                continue
            time_text = str(m.get("time") or "").strip()
            if re.fullmatch(r"\d{1,2}:\d{2}", time_text):
                hour, minute = map(int, time_text.split(":"))
                day = day.replace(hour=hour, minute=minute)
            else:
                time_text = "待定"
            matches.append({
                "kickoff": day, "time": time_text, "note": str(m.get("note") or ""),
                "home": str(m.get("home") or ""), "away": str(m.get("away") or ""),
            })
        if matches:
            groups.append({"name": str(g.get("name") or ""), "matches": sorted(matches, key=lambda m: m["kickoff"])})
    print(f"赛程：{sum(len(g['matches']) for g in groups)} 场，{len(groups)} 组")
    return groups


# ---------- 邮件 ----------

FONT = "-apple-system,'PingFang SC','Microsoft YaHei',sans-serif"


def icon_img(path, size):
    url = ESPN_IMG.format(path=path, size=size * 2)
    return f'<img src="{url}" width="{size}" height="{size}" alt="" style="vertical-align:middle;margin-right:4px;border:0">'


def team_icon(name):
    if name in TEAM_ICONS:
        return icon_img(f"teamlogos/soccer/500/{TEAM_ICONS[name]}", 18)
    if CHINA_TEAM_PATTERN.search(name):
        return icon_img("teamlogos/countries/500/chn", 18)
    return ""


def render_card(n, item):
    when = item["published"].astimezone(BEIJING)
    tag = (
        '<span style="display:inline-block;background:#f53f3f;color:#fff;font-size:11px;font-weight:600;'
        'padding:1px 6px;border-radius:4px;margin-right:6px;vertical-align:2px">重点</span>'
        if item.get("priority") else ""
    )
    image = (
        f'<td width="104" valign="top" style="padding:0 14px 0 0">'
        f'<img src="{html.escape(item["image"])}" width="104" height="104" alt="" '
        'style="display:block;width:104px;height:104px;object-fit:cover;border-radius:12px;border:0"></td>'
        if item["image"] else ""
    )
    # 全文放在 <details> 里，点击展开；不支持 <details> 的邮箱会直接显示全文
    full_text = "".join(
        f'<p style="margin:0 0 10px;color:#3a3f47;font-size:14px;line-height:1.8">{html.escape(p)}</p>'
        for p in item.get("paragraphs", [])
    )
    details = (
        '<details style="margin:10px 0 0">'
        '<summary style="cursor:pointer;color:#1a56db;font-size:13px">展开全文</summary>'
        f'<div style="margin-top:10px;padding:12px 14px;background:#f6f8fa;border-radius:8px">{full_text}</div>'
        "</details>"
        if full_text else ""
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 22px">'
        f"<tr>{image}<td valign=\"top\">"
        f'<div style="color:#1d2129;font-size:16px;font-weight:700;line-height:1.45">{n}. {tag}{html.escape(item["title_zh"])}</div>'
        f'<div style="color:#3a3f47;font-size:14px;line-height:1.7;margin:6px 0">{html.escape(item["summary_zh"])}</div>'
        f'<div style="color:#86909c;font-size:12px">来源：{html.escape(item["source"])}，{when.month}月{when.day}日</div>'
        "</td></tr>"
        f'<tr><td colspan="2">{details}</td></tr>'
        "</table>"
    )


def render_fixtures(groups):
    if not groups:
        return '<p style="color:#86909c">未找到近期的相关赛程。</p>'
    cell = "padding:6px 8px;border-bottom:1px solid #eee"
    parts = ['<p style="color:#3a3f47;font-size:14px;margin:0 0 10px">以下均为北京时间：</p>']
    for g in groups:
        icon = icon_img(COMPETITION_ICONS[g["name"]], 22) if g["name"] in COMPETITION_ICONS else ""
        rows = []
        for m in g["matches"]:
            k = m["kickoff"]
            weight = "font-weight:700;" if "皇马" in (m["home"], m["away"]) else ""
            note = f' <span style="color:#86909c">（{html.escape(m["note"])}）</span>' if m["note"] else ""
            rows.append(
                f'<tr style="{weight}">'
                f'<td style="{cell};white-space:nowrap">{k.month}月{k.day}日 {WEEKDAYS[k.weekday()]}</td>'
                f'<td style="{cell}">{m["time"]}</td>'
                f'<td style="{cell}">{team_icon(m["home"])}{html.escape(m["home"])}'
                f' <span style="color:#86909c">vs</span> {team_icon(m["away"])}{html.escape(m["away"])}{note}</td></tr>'
            )
        parts.append(
            f'<div style="font-size:15px;font-weight:700;margin:16px 0 6px">{icon}{html.escape(g["name"])}</div>'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:13px;color:#1d2129;border-collapse:collapse">'
            '<tr style="color:#86909c"><td style="padding:4px 8px">日期</td><td style="padding:4px 8px">时间</td><td style="padding:4px 8px">对阵</td></tr>'
            + "".join(rows) + "</table>"
        )
    parts.append('<p style="color:#86909c;font-size:12px;margin-top:10px">赛程由 DeepSeek 从搜索结果中整理，具体开球时间请以赛事官方赛程为准。</p>')
    return "".join(parts)


def section_title(text):
    return f'<h2 style="font-size:19px;margin:30px 0 16px;color:#1d2129">{text}</h2>'


def render_email(items, groups, today):
    parts = [
        f'<div style="font-family:{FONT};max-width:680px;margin:auto;color:#1d2129">',
        f'<h1 style="font-size:22px;margin:0 0 4px">⚽ 足球简报 · {today}</h1>',
        '<p style="color:#86909c;font-size:12px;margin:0">近一天的足球新闻，点击「展开全文」查看完整内容</p>',
    ]
    n = 0
    for key, title in SECTIONS:
        section_items = sorted((i for i in items if i["section"] == key), key=lambda i: i["published"], reverse=True)
        if key == "madrid":
            # 皇马新闻排在其他西甲新闻前面
            section_items.sort(key=lambda i: "皇马" not in i["title_zh"] and "皇家马德里" not in i["title_zh"])
        # 重点话题排在最前面（sort 是稳定的，上面的顺序在组内保留）
        section_items.sort(key=lambda i: not i.get("priority"))
        parts.append(section_title(title))
        if not section_items:
            parts.append('<p style="color:#86909c">暂无新消息。</p>')
        for item in section_items:
            n += 1
            parts.append(render_card(n, item))
    parts.append(section_title("五、近期赛程"))
    parts.append(render_fixtures(groups))
    parts.append("</div>")
    return "".join(parts)


def send_email(subject, body):
    sender = os.environ["QQ_EMAIL"]
    auth_code = os.environ["QQ_AUTH_CODE"]
    to = os.environ.get("MAIL_TO") or sender

    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("足球简报", sender))
    msg["To"] = to

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.login(sender, auth_code)
        server.sendmail(sender, [to], msg.as_string())


def main():
    dry_run = "--dry-run" in sys.argv
    today = datetime.now(BEIJING).strftime("%Y-%m-%d")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        sys.exit("没有设置 DEEPSEEK_API_KEY")

    seen = set()  # RSS 和搜索共用，避免同一篇文章收录两次
    items = collect_foreign_news(seen)
    items += collect_search_news(seen)
    if not items:
        sys.exit("没有找到任何新闻，请检查新闻源和 DeepSeek API Key")
    items = drop_duplicates(summarize_all(items))
    groups = collect_fixtures()

    body = render_email(items, groups, today)
    subject = f"⚽ 足球简报 {today}"

    if dry_run:
        with open("preview.html", "w", encoding="utf-8") as f:
            f.write(body)
        print("已生成 preview.html（未发送邮件）")
        return

    send_email(subject, body)
    print(f"邮件已发送：{len(items)} 条新闻，{sum(len(g['matches']) for g in groups)} 场赛程")


if __name__ == "__main__":
    main()
