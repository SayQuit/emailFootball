"""抓取外国媒体的皇马和国际足球新闻，翻译成中文后整理成早报发送到 QQ 邮箱。

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

# (来源名, RSS 地址, 是否皇马专题)
FEEDS = [
    ("Managing Madrid", "https://www.managingmadrid.com/rss/current.xml", True),
    ("The Guardian", "https://www.theguardian.com/football/realmadrid/rss", True),
    ("BBC Sport", "https://feeds.bbci.co.uk/sport/football/rss.xml", False),
    ("The Guardian", "https://www.theguardian.com/football/rss", False),
    ("ESPN", "https://www.espn.com/espn/rss/soccer/news", False),
]

# 综合新闻源里提到这些词的新闻也归入皇马栏目
REAL_MADRID_PATTERN = re.compile(r"real madrid|bernab[eé]u", re.IGNORECASE)

# 论坛帖、直播文字流、视频页没有可读的正文，直接跳过
SKIP_TITLE_PATTERN = re.compile(r"\bthread\b", re.IGNORECASE)
SKIP_LINK_PATTERN = re.compile(r"/live/|/videos?/|/audio/")

REAL_MADRID_LIMIT = 8
WORLD_LIMIT = 10

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


def fetch_article(url):
    """打开原文页面提取正文，失败返回空字符串。"""
    try:
        downloaded = trafilatura.fetch_url(url)
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=False) if downloaded else None
    except Exception as e:
        print(f"正文提取失败 {url}: {e}")
        return ""
    if not text:
        return ""
    if len(text) > ARTICLE_MAX_CHARS:
        text = text[:ARTICLE_MAX_CHARS].rsplit("\n", 1)[0] + "\n……（正文过长已截断，完整内容请看原文）"
    return text


def fetch_feed(source, url):
    feed = feedparser.parse(url, agent=USER_AGENT)
    if feed.get("bozo") and not feed.entries:
        raise RuntimeError(f"{source} 抓取失败: {feed.get('bozo_exception')}")

    items = []
    for entry in feed.entries:
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        items.append({
            "title": clean_text(entry.get("title", "")),
            "summary": truncate(clean_text(entry.get("summary", "")), SUMMARY_MAX_CHARS),
            "source": source,
            "link": entry.get("link", ""),
            "published": datetime(*published[:6], tzinfo=timezone.utc) if published else None,
        })
    return items


def collect_news():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    real_madrid, world = [], []
    seen = set()
    errors = []

    for source, url, is_real_madrid in FEEDS:
        try:
            items = fetch_feed(source, url)
        except Exception as e:
            errors.append(str(e))
            continue
        print(f"{source}: {len(items)} 条")

        for item in items:
            key = normalize(item["title"])
            if not key or key in seen or item["link"] in seen:
                continue
            if item["published"] and item["published"] < cutoff:
                continue
            if SKIP_TITLE_PATTERN.search(item["title"]) or SKIP_LINK_PATTERN.search(item["link"]):
                continue
            seen.update((key, item["link"]))
            text = f"{item['title']} {item['summary']}"
            if is_real_madrid or REAL_MADRID_PATTERN.search(text):
                real_madrid.append(item)
            else:
                world.append(item)

    if len(errors) == len(FEEDS):
        sys.exit("所有新闻源都抓取失败:\n" + "\n".join(errors))
    for e in errors:
        print(e)

    def newest(items, limit):
        return sorted(items, key=lambda i: i["published"] or cutoff, reverse=True)[:limit]

    return [
        ("皇家马德里", newest(real_madrid, REAL_MADRID_LIMIT)),
        ("国际足球", newest(world, WORLD_LIMIT)),
    ]


def translate_items(sections):
    for _, items in sections:
        for item in items:
            item["title_zh"] = translate(item["title"])
            body = fetch_article(item["link"])
            item["full_text"] = bool(body)
            # 提取不到正文时退回 RSS 简介
            item["body_zh"] = translate_long(body or item["summary"])
            print(f"{'正文' if body else '简介'} {len(body or item['summary'])} 字符: {item['title']}")


def render_item(item):
    when = item["published"].astimezone(BEIJING).strftime("%m-%d %H:%M") if item["published"] else ""
    meta = " · ".join(p for p in (html.escape(item["source"]), when) if p)
    if not item["full_text"]:
        meta += " · 仅简介，未能获取全文"
    body = "".join(
        f'<p style="margin:0 0 10px;color:#333;font-size:15px;line-height:1.7">{html.escape(p)}</p>'
        for p in item["body_zh"].split("\n")
        if p.strip()
    )
    return (
        '<div style="margin:0 0 28px;padding-bottom:20px;border-bottom:1px solid #eee">'
        f'<a href="{html.escape(item["link"])}" style="color:#1a56db;font-size:17px;'
        f'text-decoration:none;font-weight:600">{html.escape(item["title_zh"])}</a>'
        f'<div style="color:#999;font-size:12px;margin:2px 0 4px">{html.escape(item["title"])}</div>'
        f'<div style="color:#888;font-size:12px;margin-bottom:10px">{meta}</div>'
        f"{body}"
        "</div>"
    )


def render_toc(sections):
    """邮件开头的目录，点击跳到对应文章。"""
    rows = []
    for s, (name, items) in enumerate(sections):
        for i, item in enumerate(items):
            rows.append(
                f'<li style="margin:2px 0"><a href="#a{s}-{i}" style="color:#333;text-decoration:none">'
                f"<span style=\"color:#888\">[{name}]</span> {html.escape(item['title_zh'])}</a></li>"
            )
    if not rows:
        return ""
    return (
        '<div style="background:#f6f8fa;padding:12px 16px;border-radius:6px;font-size:14px;margin-bottom:8px">'
        f'<b>今日目录</b><ol style="padding-left:20px;margin:6px 0 0">{"".join(rows)}</ol></div>'
    )


def render_email(sections, today):
    parts = [
        '<div style="font-family:-apple-system,\'Microsoft YaHei\',sans-serif;max-width:680px;margin:auto">',
        f'<h2 style="margin:0 0 4px">⚽ 足球早报 · {today}</h2>',
        f'<p style="color:#888;font-size:12px;margin:0 0 16px">过去 {LOOKBACK_HOURS} 小时的外媒新闻，'
        "机器翻译，点击标题阅读英文原文</p>",
        render_toc(sections),
    ]
    for s, (name, items) in enumerate(sections):
        parts.append(f'<h3 style="border-left:4px solid #1a56db;padding-left:8px;margin-top:24px">{name}</h3>')
        if items:
            parts.extend(f'<a name="a{s}-{i}"></a>{render_item(item)}' for i, item in enumerate(items))
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
    for name, items in sections:
        print(f"{name}: 选出 {len(items)} 条")
    translate_items(sections)

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
