"""抓取足球新闻（外媒翻译成中文 + 国内中国足球新闻），整理成带图片的早报发送到 QQ 邮箱。

环境变量:
    QQ_EMAIL      发件 QQ 邮箱地址
    QQ_AUTH_CODE  QQ 邮箱 SMTP 授权码（不是 QQ 密码）
    MAIL_TO       收件地址，可选，默认发给 QQ_EMAIL 自己

本地预览（不发邮件、不需要授权码）:
    python main.py --dry-run
"""

import html
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from functools import lru_cache
from urllib.parse import quote
from urllib.request import Request, urlopen

import feedparser
import trafilatura

BEIJING = timezone(timedelta(hours=8))
LOOKBACK_HOURS = 26  # 比 24 小时略长，抵消 GitHub 定时任务的延迟
SUMMARY_MAX_CHARS = 300
ARTICLE_MAX_CHARS = 6000  # 单篇正文上限，防止邮件过长
TRANSLATE_CHUNK_CHARS = 1500  # 翻译接口走 GET 请求，过长的文本要分段
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def google_news_cn(query):
    q = quote(f"{query} when:1d")
    return f"https://news.google.com/rss/search?q={q}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"


# 综合新闻源：各栏目按关键词从这里挑选新闻
GENERAL_FEEDS = [
    ("BBC Sport", "https://feeds.bbci.co.uk/sport/football/rss.xml"),
    ("The Guardian", "https://www.theguardian.com/football/rss"),
    ("ESPN", "https://www.espn.com/espn/rss/soccer/news"),
]

# 按邮件里的显示顺序排列。
# feeds: 专题新闻源，里面的新闻全部归入该栏目
# keywords: 综合新闻源里命中这些词的新闻也归入该栏目
# priority: 一条新闻同时符合多个栏目时，数字小的栏目优先
# translate: 外媒新闻需要翻译，国内新闻不需要
SECTIONS = [
    {
        "name": "皇家马德里资讯",
        "priority": 1,
        "limit": 8,
        "translate": True,
        "feeds": [
            ("Managing Madrid", "https://www.managingmadrid.com/rss/current.xml"),
            ("The Guardian", "https://www.theguardian.com/football/realmadrid/rss"),
        ],
        "keywords": r"real madrid|bernab[eé]u",
    },
    {
        "name": "国际足球资讯（国家队）",
        "priority": 5,
        "limit": 5,
        "translate": True,
        "feeds": [
            ("The Guardian", "https://www.theguardian.com/football/world-cup-2026/rss"),
        ],
        "keywords": r"world cup|nations league|international break|national team|qualif|"
        r"\bfriendl(?:y|ies)\b|euro 20\d\d|copa am[eé]rica|afcon|three lions|lionesses",
    },
    {
        "name": "欧冠资讯",
        "priority": 2,
        "limit": 5,
        "translate": True,
        "feeds": [
            ("The Guardian", "https://www.theguardian.com/football/championsleague/rss"),
            ("BBC Sport", "https://feeds.bbci.co.uk/sport/football/champions-league/rss.xml"),
        ],
        "keywords": r"champions league",
    },
    {
        "name": "西甲资讯",
        "priority": 3,
        "limit": 5,
        "translate": True,
        "feeds": [
            ("The Guardian", "https://www.theguardian.com/football/laligafootball/rss"),
        ],
        "keywords": r"la ?liga|barcelona|bar[cç]a\b|atl[eé]tico madrid",
    },
    {
        "name": "英超资讯",
        "priority": 4,
        "limit": 5,
        "translate": True,
        "feeds": [
            ("The Guardian", "https://www.theguardian.com/football/premierleague/rss"),
            ("BBC Sport", "https://feeds.bbci.co.uk/sport/football/premier-league/rss.xml"),
        ],
        "keywords": r"premier league",
    },
    {
        "name": "中国足球资讯（中超 · 国家队）",
        "priority": 6,
        "limit": 5,
        "translate": False,
        # 国内足球新闻少，放宽时间范围
        "lookback_hours": 48,
        # 中新网体育频道包含所有项目，只保留足球
        "filter": r"足球|中超|中甲|国足|男足|女足|足协|世预赛|亚冠|亚洲杯",
        "feeds": [
            ("中新网", "https://www.chinanews.com.cn/rss/sports.xml"),
            # 补充来源：只有能解析出真实文章地址的条目才会保留
            ("Google 新闻", google_news_cn("中超 OR 国足 OR 中国男足 OR 中国女足")),
        ],
        "keywords": None,
    },
]

# 论坛帖、直播文字流、视频和播客没有可读的正文，直接跳过
SKIP_TITLE_PATTERN = re.compile(r"\bthread\b|– podcast$", re.IGNORECASE)
SKIP_LINK_PATTERN = re.compile(r"/live/|/videos?/|/audio/")

# 文章页面 <meta property="og:image" content="..."> 里的题图，两种属性顺序都要匹配
OG_IMAGE_PATTERN = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', re.IGNORECASE)
OG_IMAGE_REVERSED_PATTERN = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.IGNORECASE)
# 网站 logo、Google 新闻占位图等不是新闻配图
BAD_IMAGE_PATTERN = re.compile(r"gstatic\.com|googleusercontent\.com|news\.google\.com|logo", re.IGNORECASE)

SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465


def clean_text(text):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return " ".join(text.split())


def truncate(text, limit):
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def normalize(title):
    return re.sub(r"\W+", "", title).lower()


def translate(text):
    """用 Google 翻译免费接口翻成中文，失败时返回原文。"""
    if not text:
        return ""
    url = (
        "https://translate.googleapis.com/translate_a/single"
        f"?client=gtx&sl=auto&tl=zh-CN&dt=t&q={quote(text)}"
    )
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return "".join(seg[0] for seg in data[0] if seg[0])
        except Exception as e:
            print(f"翻译失败（第 {attempt + 1} 次）: {e}")
            time.sleep(2)
    return text


def translate_long(text):
    """按段落分组翻译长文本，保留段落结构。"""
    chunks, current = [], ""
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        while len(para) > TRANSLATE_CHUNK_CHARS:
            chunks.append(para[:TRANSLATE_CHUNK_CHARS])
            para = para[TRANSLATE_CHUNK_CHARS:]
        if current and len(current) + len(para) + 1 > TRANSLATE_CHUNK_CHARS:
            chunks.append(current)
            current = ""
        current = f"{current}\n{para}" if current else para
    if current:
        chunks.append(current)

    translated = []
    for chunk in chunks:
        translated.append(translate(chunk))
        time.sleep(0.3)
    return "\n".join(translated)


def resolve_google_news(url):
    """Google 新闻的链接是跳转页，解析出真实文章地址，失败时原样返回。"""
    if "news.google.com" not in url:
        return url
    try:
        from googlenewsdecoder import gnewsdecoder

        result = gnewsdecoder(url, interval=1)
        if result.get("status"):
            return result["decoded_url"]
        print(f"Google 新闻链接解析失败: {result.get('message')}")
    except Exception as e:
        print(f"Google 新闻链接解析失败: {e}")
    return url


def fetch_article(url):
    """打开原文页面提取正文和题图，失败时返回空值。"""
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

    text = text or ""
    if len(text) > ARTICLE_MAX_CHARS:
        text = text[:ARTICLE_MAX_CHARS].rsplit("\n", 1)[0] + "\n……（正文过长已截断，完整内容请看原文）"
    return text, image


def feed_image(entry):
    """RSS 自带的配图，选宽度最大的一张。"""
    candidates = list(entry.get("media_content", [])) + list(entry.get("media_thumbnail", []))
    candidates += [
        {"url": l.get("href")} for l in entry.get("links", []) if l.get("type", "").startswith("image/")
    ]
    candidates = [c for c in candidates if c.get("url") and not BAD_IMAGE_PATTERN.search(c["url"])]
    if not candidates:
        return ""
    return max(candidates, key=lambda c: int(c.get("width") or 0))["url"]


@lru_cache(maxsize=None)
def fetch_feed(source, url):
    feed = feedparser.parse(url, agent=USER_AGENT)
    if feed.get("bozo") and not feed.entries:
        print(f"{source} 抓取失败: {url}: {feed.get('bozo_exception')}")
        return ()

    items = []
    for entry in feed.entries:
        title = clean_text(entry.get("title", ""))
        # Google 新闻标题格式为「标题 - 来源」，来源改用真实媒体名
        real_source = entry.get("source", {}).get("title", "")
        if real_source and title.endswith(f" - {real_source}"):
            title = title[: -len(f" - {real_source}")]

        published = entry.get("published_parsed") or entry.get("updated_parsed")
        items.append({
            "title": title,
            "summary": truncate(clean_text(entry.get("summary", "")), SUMMARY_MAX_CHARS),
            "source": real_source or source,
            "link": entry.get("link", ""),
            "image": feed_image(entry),
            "published": datetime(*published[:6], tzinfo=timezone.utc) if published else None,
        })
    print(f"{source}: {len(items)} 条 ({url[:60]})")
    return tuple(items)


def collect_news():
    """为每个栏目选出候选新闻。候选数量是上限的两倍，给后面解析失败被丢弃的条目留余量。"""
    now = datetime.now(timezone.utc)
    seen = set()
    picked = {}

    def usable(item, cutoff):
        key = normalize(item["title"])
        return (
            key
            and key not in seen
            and item["link"] not in seen
            and not (item["published"] and item["published"] < cutoff)
            and not SKIP_TITLE_PATTERN.search(item["title"])
            and not SKIP_LINK_PATTERN.search(item["link"])
        )

    for section in sorted(SECTIONS, key=lambda s: s["priority"]):
        cutoff = now - timedelta(hours=section.get("lookback_hours", LOOKBACK_HOURS))
        candidates = [dict(i) for source, url in section["feeds"] for i in fetch_feed(source, url)]
        if section.get("filter"):
            pattern = re.compile(section["filter"], re.IGNORECASE)
            candidates = [i for i in candidates if pattern.search(f"{i['title']} {i['summary']}")]
        if section["keywords"]:
            pattern = re.compile(section["keywords"], re.IGNORECASE)
            candidates += [
                dict(i)
                for source, url in GENERAL_FEEDS
                for i in fetch_feed(source, url)
                if pattern.search(f"{i['title']} {i['summary']}")
            ]

        items = []
        for item in sorted(candidates, key=lambda i: i["published"] or cutoff, reverse=True):
            if len(items) >= section["limit"] * 2:
                break
            if usable(item, cutoff):
                seen.update((normalize(item["title"]), item["link"]))
                items.append(item)
        picked[section["name"]] = items

    return [(s, picked[s["name"]]) for s in SECTIONS]


def prepare_items(sections):
    """抓取正文和图片，外媒新闻翻译成中文。每个栏目保留前 limit 条成功处理的新闻。"""
    for section, items in sections:
        kept = []
        for item in items:
            if len(kept) >= section["limit"]:
                break
            item["link"] = resolve_google_news(item["link"])
            if "news.google.com" in item["link"]:
                print(f"[{section['name']}] 跳过（无法获取真实链接）: {item['title']}")
                continue
            body, image = fetch_article(item["link"])
            item["full_text"] = bool(body)
            item["image"] = image or item["image"]
            # 提取不到正文时退回 RSS 简介
            body = body or item["summary"]
            if section["translate"]:
                item["title_zh"] = translate(item["title"])
                item["body_zh"] = translate_long(body)
            else:
                item["title_zh"] = item["title"]
                item["title"] = ""  # 中文新闻不重复显示原标题
                item["body_zh"] = body
            print(f"[{section['name']}] {'正文' if item['full_text'] else '简介'} {len(body)} 字符: {item['title_zh']}")
            kept.append(item)
        items[:] = kept


def render_item(item, anchor):
    when = item["published"].astimezone(BEIJING).strftime("%m-%d %H:%M") if item["published"] else ""
    meta = " · ".join(p for p in (html.escape(item["source"]), when) if p)
    if not item["full_text"]:
        meta += " · 仅简介，未能获取全文"
    original = (
        f'<div style="color:#999;font-size:12px;margin:2px 0 4px">{html.escape(item["title"])}</div>'
        if item["title"]
        else ""
    )
    image = (
        f'<img src="{html.escape(item["image"])}" alt="" '
        'style="display:block;width:100%;max-width:640px;height:auto;border-radius:6px;margin:0 0 12px">'
        if item["image"]
        else ""
    )
    body = "".join(
        f'<p style="margin:0 0 10px;color:#333;font-size:15px;line-height:1.7">{html.escape(p)}</p>'
        for p in item["body_zh"].split("\n")
        if p.strip()
    )
    return (
        f'<a name="{anchor}"></a>'
        '<div style="margin:0 0 28px;padding-bottom:20px;border-bottom:1px solid #eee">'
        f'<a href="{html.escape(item["link"])}" style="color:#1a56db;font-size:17px;'
        f'text-decoration:none;font-weight:600">{html.escape(item["title_zh"])}</a>'
        f"{original}"
        f'<div style="color:#888;font-size:12px;margin:4px 0 10px">{meta}</div>'
        f"{image}{body}"
        "</div>"
    )


def render_toc(sections):
    """邮件开头的目录，点击跳到对应文章。"""
    groups = []
    for s, (section, items) in enumerate(sections):
        if not items:
            continue
        rows = "".join(
            f'<li style="margin:2px 0"><a href="#a{s}-{i}" style="color:#333;text-decoration:none">'
            f"{html.escape(item['title_zh'])}</a></li>"
            for i, item in enumerate(items)
        )
        groups.append(
            f'<div style="margin-top:8px;color:#1a56db;font-weight:600">{section["name"]}</div>'
            f'<ol style="padding-left:20px;margin:4px 0 0">{rows}</ol>'
        )
    if not groups:
        return ""
    return (
        '<div style="background:#f6f8fa;padding:12px 16px;border-radius:6px;font-size:14px;margin-bottom:8px">'
        f'<b>今日目录</b>{"".join(groups)}</div>'
    )


def render_email(sections, today):
    parts = [
        '<div style="font-family:-apple-system,\'Microsoft YaHei\',sans-serif;max-width:680px;margin:auto">',
        f'<h2 style="margin:0 0 4px">⚽ 足球早报 · {today}</h2>',
        f'<p style="color:#888;font-size:12px;margin:0 0 16px">过去 {LOOKBACK_HOURS} 小时的新闻，'
        "外媒新闻为机器翻译，点击标题阅读原文。图片不显示时，请点邮件顶部的「显示图片」</p>",
        render_toc(sections),
    ]
    for s, (section, items) in enumerate(sections):
        parts.append(
            f'<h3 style="border-left:4px solid #1a56db;padding-left:8px;margin-top:28px">{section["name"]}</h3>'
        )
        if items:
            parts.extend(render_item(item, f"a{s}-{i}") for i, item in enumerate(items))
        else:
            parts.append('<p style="color:#888">暂无新消息</p>')
    parts.append("</div>")
    return "".join(parts)


def send_email(subject, body):
    sender = os.environ["QQ_EMAIL"]
    auth_code = os.environ["QQ_AUTH_CODE"]
    to = os.environ.get("MAIL_TO") or sender

    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("足球早报", sender))
    msg["To"] = to

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.login(sender, auth_code)
        server.sendmail(sender, [to], msg.as_string())


def main():
    dry_run = "--dry-run" in sys.argv
    today = datetime.now(BEIJING).strftime("%Y-%m-%d")

    sections = collect_news()
    if not any(items for _, items in sections):
        sys.exit("所有栏目都没有抓到新闻，请检查新闻源")
    for section, items in sections:
        print(f"{section['name']}: 选出 {len(items)} 条")
    prepare_items(sections)

    body = render_email(sections, today)
    subject = f"⚽ 足球早报 {today}"

    if dry_run:
        with open("preview.html", "w", encoding="utf-8") as f:
            f.write(body)
        print("已生成 preview.html（未发送邮件）")
        return

    send_email(subject, body)
    print("邮件已发送")


if __name__ == "__main__":
    main()
