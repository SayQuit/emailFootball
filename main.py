"""抓取外媒足球新闻，用 DeepSeek 改写成中文简报，附带未来赛程，发送到 QQ 邮箱。

邮件结构:
    一、皇家马德里及西甲
    二、英超焦点新闻
    三、国际足坛其他新闻
    四、未来一周的豪门对决（西甲、英超、欧冠，带赛事图标和队徽）

环境变量:
    QQ_EMAIL          发件 QQ 邮箱地址
    QQ_AUTH_CODE      QQ 邮箱 SMTP 授权码（不是 QQ 密码）
    DEEPSEEK_API_KEY  DeepSeek API Key；没有时退回免费机器翻译
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
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import feedparser
import trafilatura

BEIJING = timezone(timedelta(hours=8))
LOOKBACK_HOURS = 26  # 比 24 小时略长，抵消 GitHub 定时任务的延迟
MAX_ARTICLES = 45  # 控制运行时间和 API 费用
ARTICLE_MAX_CHARS = 6000
FIXTURE_DAYS = 7  # 只看豪门对决，场次少，所以看一周
WORKERS = 5
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL") or "deepseek-chat"

SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465

# (来源名, RSS 地址, 栏目提示)。栏目提示为 None 的综合源按关键词归类
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

SECTIONS = [
    ("madrid", "一、皇家马德里及西甲"),
    ("epl", "二、英超焦点新闻"),
    ("world", "三、国际足坛其他新闻"),
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
# 论坛帖、直播、播客、视频、测验、梦幻足球、八卦汇总没有新闻价值
SKIP_PATTERN = re.compile(
    r"\bthread\b|podcast|quiz|\bfpl\b|fantasy|gossip|/live/|/videos?/|/audio/|/sounds/|/iplayer/",
    re.IGNORECASE,
)
BAD_IMAGE_PATTERN = re.compile(r"logo|placeholder|default", re.IGNORECASE)
OG_IMAGE_PATTERN = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', re.IGNORECASE)
OG_IMAGE_REVERSED_PATTERN = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.IGNORECASE)

# ESPN 赛程接口的赛事代码 → 中文名，按邮件里的显示顺序排列
LEAGUES = [
    ("esp.1", "西甲"),
    ("eng.1", "英超"),
    ("uefa.champions", "欧冠"),
]
REAL_MADRID_ESPN_ID = "86"
# 赛程只列豪门之间的对决（皇马的比赛总是列出）。ESPN 球队 ID
BIG_CLUBS = {
    "86", "83", "1068",                   # 皇马、巴萨、马竞
    "359", "363", "364", "382", "360", "367",  # 阿森纳、切尔西、利物浦、曼城、曼联、热刺
    "132", "124", "131",                  # 拜仁、多特蒙德、勒沃库森
    "111", "110", "103", "114",           # 尤文、国米、AC米兰、那不勒斯
    "160",                                # 巴黎圣日耳曼
}
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

EDITOR_RULES = (
    "你是资深足球新闻编辑，为中国读者整理每日足球简报。"
    "全部使用简体中文，不要出现英文句子；人名、球队名使用中文通用译名"
    "（如维尼修斯、姆巴佩、贝林厄姆、亚马尔、B费、阿尔特塔、马竞、国米）。"
    "只陈述原文中的事实，不编造，不加评论。只输出 JSON。"
)


# ---------- 通用工具 ----------

def clean_text(text):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return " ".join(text.split())


def normalize(title):
    return re.sub(r"\W+", "", title).lower()


def http_get_json(url, timeout=20):
    with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def google_translate(text):
    """免费机器翻译，只在 DeepSeek 不可用时兜底，失败返回原文。"""
    if not text:
        return ""
    url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=zh-CN&dt=t&q={quote(text)}"
    try:
        data = http_get_json(url, timeout=15)
        return "".join(seg[0] for seg in data[0] if seg[0])
    except Exception as e:
        print(f"机器翻译失败: {e}")
        return text


def deepseek(system, user, max_tokens=800):
    """调用 DeepSeek 并解析 JSON 回复。没有 Key 或多次失败时返回 None。"""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return None
    body = json.dumps({
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    for attempt in range(3):
        try:
            # 全文翻译输出较长，超时留足
            with urlopen(Request(DEEPSEEK_URL, data=body, headers=headers), timeout=240) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return json.loads(data["choices"][0]["message"]["content"])
        except Exception as e:
            print(f"DeepSeek 调用失败（第 {attempt + 1} 次）: {e}")
            time.sleep(3)
    return None


# ---------- 新闻 ----------

def small_image(url):
    """把常见网站的配图换成 320–460 宽的小图。"""
    if not url:
        return ""
    url = html.unescape(url)
    # BBC: /ace/branded_sport/1200/cpsprodpb/... → /news/320/cpsprodpb/...
    url = re.sub(r"ichef\.bbci\.co\.uk/(?:ace/[a-z_]+|news)/\d+/", "ichef.bbci.co.uk/news/320/", url)
    if "managingmadrid.com" in url:
        parts = urlsplit(url)
        query = [(k, "320" if k == "w" else v) for k, v in parse_qsl(parts.query) if k != "crop"]
        url = urlunsplit(parts._replace(query=urlencode(query)))
    return url


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
        return "madrid", 0  # 第二个值用于皇马排在西甲前面
    if hint == "world" or CHAMPIONS_LEAGUE_PATTERN.search(text):
        return "world", 1
    if hint in ("madrid", "laliga") or LALIGA_PATTERN.search(text):
        return "madrid", 1
    if hint == "epl" or EPL_PATTERN.search(text):
        return "epl", 1
    return "world", 1


def collect_news():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    seen, items = set(), []
    for source, url, hint in FEEDS:
        for raw in fetch_feed(source, url):
            item = dict(raw)
            key = normalize(item["title"])
            if not key or key in seen or item["link"] in seen:
                continue
            if item["published"] and item["published"] < cutoff:
                continue
            if SKIP_PATTERN.search(f"{item['title']} {item['link']}"):
                continue
            seen.update((key, item["link"]))
            item["section"], item["order"] = classify(item, hint)
            items.append(item)
    items.sort(key=lambda i: i["published"] or cutoff, reverse=True)
    print(f"候选新闻 {len(items)} 条，处理最新的 {min(len(items), MAX_ARTICLES)} 条")
    return items[:MAX_ARTICLES]


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


def summarize(item):
    """读原文，生成中文标题和 1–2 句摘要。返回 None 表示这条不是有价值的新闻。"""
    body, og_image = fetch_article(item["link"])
    # 卫报的 og:image 是带签名的大图，优先用 RSS 里的小图
    item["image"] = small_image(item["image"] if "guim.co.uk" in item["image"] else (og_image or item["image"]))

    text = body or item["summary"]
    prompt = (
        f"媒体：{item['source']}\n英文标题：{item['title']}\n"
        f"正文：\n{text}\n\n"
        "请输出 JSON：{\"title\": \"简短的中文标题，不超过 20 字\", "
        "\"summary\": \"1–2 句中文摘要，50–90 字，只讲最关键的事实、比分或数据\", "
        "\"paragraphs\": [\"把正文完整翻译成通顺的中文，按原文分段，每段一个字符串；"
        "去掉与新闻无关的内容（广告、评论区规则、相关链接、图片说明）\"]}。"
        "如果这不是一条足球新闻（例如节目预告、竞猜、问答、广告），只输出 {\"skip\": true}。"
    )
    result = deepseek(EDITOR_RULES, prompt, max_tokens=6000)
    if result and result.get("skip"):
        print(f"跳过（非新闻）: {item['title']}")
        return None
    if result and result.get("title") and result.get("summary"):
        item["title_zh"], item["summary_zh"] = result["title"].strip(), result["summary"].strip()
        item["paragraphs"] = [p.strip() for p in result.get("paragraphs") or [] if isinstance(p, str) and p.strip()]
    else:
        # DeepSeek 拒绝或失败时退回机器翻译，保证邮件照常发出
        print(f"改用机器翻译: {item['title']}")
        item["title_zh"] = google_translate(item["title"])
        first = re.split(r"(?<=[.!?])\s", text, maxsplit=2)
        item["summary_zh"] = google_translate(" ".join(first[:2])[:400])
        item["paragraphs"] = [google_translate(p) for p in text.split("\n") if len(p.strip()) > 40][:15]
    return item


def summarize_all(items):
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return [i for i in pool.map(summarize, items) if i]


def drop_duplicates(items):
    """去掉报道同一事件的重复条目。"""
    listing = "\n".join(f"{n}. {i['title_zh']}：{i['summary_zh']}" for n, i in enumerate(items))
    prompt = (
        f"以下是今天的足球新闻（编号. 标题：摘要）：\n{listing}\n\n"
        "请输出 JSON：{\"duplicates\": [报道同一事件、应当删掉的较差条目编号]}。没有重复就输出空列表。"
    )
    result = deepseek(EDITOR_RULES, prompt, max_tokens=500) or {}
    drop = {d for d in result.get("duplicates", []) if isinstance(d, int)}
    if drop:
        print(f"合并重复报道 {len(drop)} 条")
    return [i for n, i in enumerate(items) if n not in drop]


# ---------- 赛程 ----------

def espn_icon(href, size=20):
    if not href:
        return ""
    path = href.replace("https://a.espncdn.com", "")
    return f"https://a.espncdn.com/combiner/i?img={path}&w={size * 2}&h={size * 2}"


def collect_fixtures():
    """从 ESPN 公开赛程接口拉取未来几天的比赛，按赛事分组。"""
    now_bj = datetime.now(timezone.utc).astimezone(BEIJING)
    start = now_bj.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=FIXTURE_DAYS)
    groups = []
    for code, name in LEAGUES:
        events, logo = {}, ""
        # 北京时间的一天跨两个 UTC 日期，所以多查前一天
        for offset in range(-1, FIXTURE_DAYS):
            day = (start + timedelta(days=offset)).strftime("%Y%m%d")
            try:
                data = http_get_json(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/scoreboard?dates={day}")
            except Exception:
                continue
            if not logo and data.get("leagues"):
                logo = (data["leagues"][0].get("logos") or [{}])[0].get("href", "")
            for e in data.get("events", []):
                events[e["id"]] = e

        matches = []
        for e in events.values():
            kickoff = datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(BEIJING)
            if not (now_bj - timedelta(hours=2) <= kickoff < end):
                continue
            teams = {c["homeAway"]: c["team"] for c in e["competitions"][0]["competitors"]}
            home, away = teams.get("home", {}), teams.get("away", {})
            ids = {home.get("id"), away.get("id")}
            if REAL_MADRID_ESPN_ID not in ids and not ids <= BIG_CLUBS:
                continue
            matches.append({
                "kickoff": kickoff,
                "home": home.get("displayName", ""), "home_logo": home.get("logo", ""),
                "away": away.get("displayName", ""), "away_logo": away.get("logo", ""),
                "madrid": REAL_MADRID_ESPN_ID in ids,
            })
        if matches:
            groups.append({"name": name, "logo": logo, "matches": sorted(matches, key=lambda m: m["kickoff"])})
    print(f"赛程：{sum(len(g['matches']) for g in groups)} 场，{len(groups)} 项赛事")
    return groups


def translate_team_names(groups):
    names = sorted({m[k] for g in groups for m in g["matches"] for k in ("home", "away")})
    if not names:
        return
    result = deepseek(
        EDITOR_RULES,
        "把下面的球队英文名翻译成中国球迷常用的中文简称，输出 JSON：{\"names\": {\"英文名\": \"中文名\"}}。\n"
        + "\n".join(names),
        max_tokens=3000,
    ) or {}
    mapping = result.get("names", {})
    for g in groups:
        for m in g["matches"]:
            m["home"] = mapping.get(m["home"], m["home"])
            m["away"] = mapping.get(m["away"], m["away"])


# ---------- 邮件 ----------

FONT = "-apple-system,'PingFang SC','Microsoft YaHei',sans-serif"


def render_card(n, item):
    when = item["published"].astimezone(BEIJING) if item["published"] else None
    date = f"，{when.month}月{when.day}日" if when else ""
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
        f'<div style="color:#1d2129;font-size:16px;font-weight:700;line-height:1.45">{n}. {html.escape(item["title_zh"])}</div>'
        f'<div style="color:#3a3f47;font-size:14px;line-height:1.7;margin:6px 0">{html.escape(item["summary_zh"])}</div>'
        f'<div style="color:#86909c;font-size:12px">来源：{html.escape(item["source"])}{date}</div>'
        "</td></tr>"
        f'<tr><td colspan="2">{details}</td></tr>'
        "</table>"
    )


def render_fixtures(groups):
    if not groups:
        return '<p style="color:#86909c">未来一周西甲、英超、欧冠没有豪门对决。</p>'
    parts = ['<p style="color:#3a3f47;font-size:14px;margin:0 0 10px">以下均为北京时间：</p>']
    for g in groups:
        icon = f'<img src="{espn_icon(g["logo"], 22)}" width="22" height="22" alt="" style="vertical-align:middle;margin-right:6px;border:0">' if g["logo"] else ""
        rows = []
        for m in g["matches"]:
            k = m["kickoff"]
            weight = "font-weight:700;" if m["madrid"] else ""
            crest = lambda logo: f'<img src="{espn_icon(logo, 18)}" width="18" height="18" alt="" style="vertical-align:middle;margin-right:4px;border:0">' if logo else ""
            rows.append(
                f'<tr style="{weight}">'
                f'<td style="padding:6px 8px;border-bottom:1px solid #eee;white-space:nowrap">{k.month}月{k.day}日 {WEEKDAYS[k.weekday()]}</td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #eee">{k:%H:%M}</td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #eee">{crest(m["home_logo"])}{html.escape(m["home"])}'
                f' <span style="color:#86909c">vs</span> {crest(m["away_logo"])}{html.escape(m["away"])}</td></tr>'
            )
        parts.append(
            f'<div style="font-size:15px;font-weight:700;margin:16px 0 6px">{icon}{g["name"]}</div>'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:13px;color:#1d2129;border-collapse:collapse">'
            '<tr style="color:#86909c"><td style="padding:4px 8px">日期</td><td style="padding:4px 8px">时间</td><td style="padding:4px 8px">对阵</td></tr>'
            + "".join(rows) + "</table>"
        )
    parts.append('<p style="color:#86909c;font-size:12px;margin-top:10px">比赛日期依据当地赛程换算北京时间，具体开球时间请以赛事官方赛程为准。</p>')
    return "".join(parts)


def section_title(text):
    return f'<h2 style="font-size:19px;margin:30px 0 16px;color:#1d2129">{text}</h2>'


def render_email(items, groups, today):
    parts = [
        f'<div style="font-family:{FONT};max-width:680px;margin:auto;color:#1d2129">',
        f'<h1 style="font-size:22px;margin:0 0 4px">⚽ 足球简报 · {today}</h1>',
        f'<p style="color:#86909c;font-size:12px;margin:0">过去 {LOOKBACK_HOURS} 小时的外媒新闻，点击「展开全文」查看完整内容</p>',
    ]
    n = 0
    for key, title in SECTIONS:
        section_items = sorted(
            (i for i in items if i["section"] == key),
            key=lambda i: (i["order"], -(i["published"].timestamp() if i["published"] else 0)),
        )
        parts.append(section_title(title))
        if not section_items:
            parts.append('<p style="color:#86909c">暂无新消息。</p>')
        for item in section_items:
            n += 1
            parts.append(render_card(n, item))
    parts.append(section_title("四、未来一周的豪门对决"))
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
        print("警告：没有设置 DEEPSEEK_API_KEY，将使用机器翻译")

    items = collect_news()
    if not items:
        sys.exit("没有抓到任何新闻，请检查新闻源")
    items = drop_duplicates(summarize_all(items))

    groups = collect_fixtures()
    translate_team_names(groups)

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
