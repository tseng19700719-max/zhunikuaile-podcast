#!/usr/bin/env python3
"""
住你快樂podcast：每日新聞過濾 → 依比例精選 → Gemini 寫講稿 → edge-tts 語音 → Podcast RSS

流程
  1. collect   從 Google 新聞關鍵字 + RSS 抓新聞（過去 N 小時）
  2. dedupe    標題相似度去重
  3. select    Gemini 分類＋評分，Python 依比例（60/20/10/10）挑出每類則數
  4. enrich    盡量抓內文，讓摘要不只是標題
  5. script    Gemini 依各類字數配額寫口語講稿（失敗時用標題稿備援）
  6. tts       edge-tts 轉 MP3
  7. publish   更新 episodes.json / feed.xml / index.html，刪除過舊集數

用法
  python podcast.py --site site              # 正式執行
  python podcast.py --site site --dry-run    # 只產講稿不做語音
  python podcast.py --site site --fixture tests/sample_items.json --no-ai --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import calendar
import datetime as dt
import html
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import feedparser
import yaml

log = logging.getLogger("podcast")
ROOT = Path(__file__).resolve().parent
TPE = dt.timezone(dt.timedelta(hours=8))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
CAT_ORDER = ["realestate", "macro", "gov", "bank"]


# ------------------------------------------------------------------ 資料結構
@dataclass
class Item:
    id: str
    title: str
    link: str
    source: str
    published: float          # UTC epoch
    snippet: str = ""
    hint: str = ""            # 抓取時的分類提示
    category: str = ""        # 最終分類
    importance: int = 0
    body: str = ""            # 內文（enrich 後）
    also: list = field(default_factory=list)  # 被合併的重複來源


def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    total = sum(c["share"] for c in cfg["categories"].values())
    if total != 100:
        raise SystemExit(f"config.yaml categories 的 share 加總要等於 100（目前 {total}）")
    return cfg


# ------------------------------------------------------------------ 1. collect
def clean_text(s: str) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def gnews_url(query: str, days: int) -> str:
    q = urllib.parse.quote(f"{query} when:{days}d")
    return f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"


def parse_feed(url: str, hint: str) -> list[Item]:
    try:
        fp = feedparser.parse(url, agent=UA)
    except Exception as e:  # noqa: BLE001
        log.warning("讀取失敗 %s: %s", url, e)
        return []
    if fp.bozo and not fp.entries:
        log.warning("讀取失敗 %s: %s", url, fp.get("bozo_exception"))
        return []
    is_gnews = "news.google.com" in url
    feed_source = clean_text(fp.feed.get("title", "")) or urllib.parse.urlparse(url).netloc
    out = []
    for e in fp.entries:
        t = e.get("published_parsed") or e.get("updated_parsed")
        if not t:
            continue
        title = clean_text(e.get("title", ""))
        source = feed_source
        if is_gnews:
            source = clean_text((e.get("source") or {}).get("title", "")) or "Google 新聞"
            if title.endswith(f" - {source}"):
                title = title[: -len(source) - 3].strip()
        snippet = clean_text(e.get("summary", ""))
        if is_gnews or snippet.startswith(title[:15]):
            snippet = ""  # Google 新聞的 description 只是重複標題
        if not title:
            continue
        out.append(Item(id="", title=title, link=e.get("link", ""), source=source,
                        published=calendar.timegm(t), snippet=snippet[:300], hint=hint))
    return out


def collect(cfg: dict, hours: int) -> list[Item]:
    days = max(1, -(-hours // 24))
    items: list[Item] = []
    local = cfg.get("local_keywords") or []
    for key, cat in cfg["categories"].items():
        queries = list(cat.get("queries") or [])
        if key == "realestate":
            queries += [f"{kw} 房市 OR {kw} 建案 OR {kw} 房價" for kw in local]
        for q in queries:
            got = parse_feed(gnews_url(q, days), key)
            log.info("  [%s] %s → %d", cat["name"], q, len(got))
            items += got
            time.sleep(0.5)
        for f in cat.get("feeds") or []:
            got = parse_feed(f, key)
            log.info("  [%s] RSS %s → %d", cat["name"], f, len(got))
            items += got
    cutoff = time.time() - hours * 3600
    return [i for i in items if i.published >= cutoff]


# ------------------------------------------------------------------ 2. dedupe
def _norm(t: str) -> str:
    t = re.sub(r"[【】\[\]（）()「」『』《》〈〉|｜:：,，.。!！?？、\s\-—–~～/／'\"“”‘’]", "", t)
    return t.lower()


def _bigrams(s: str) -> set:
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def similar(a: str, b: str, th: float = 0.55) -> bool:
    A, B = _bigrams(_norm(a)), _bigrams(_norm(b))
    return len(A & B) / max(1, len(A | B)) >= th


def dedupe(items: list[Item]) -> list[Item]:
    prio = {k: i for i, k in enumerate(CAT_ORDER)}
    # 有內文摘要的（直接 RSS）優先保留，其次依分類優先序、時間新
    items = sorted(items, key=lambda x: (x.snippet == "", prio.get(x.hint, 9), -x.published))
    kept: list[Item] = []
    for it in items:
        dup = next((k for k in kept if similar(k.title, it.title)), None)
        if dup:
            if it.source not in dup.also and it.source != dup.source:
                dup.also.append(it.source)
            continue
        kept.append(it)
    for n, it in enumerate(sorted(kept, key=lambda x: -x.published)):
        it.id = f"n{n:03d}"
    return sorted(kept, key=lambda x: x.id)


# ------------------------------------------------------------------ Gemini
class Gemini:
    def __init__(self, models: list[str]):
        self.models = models
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        self.client = None
        if key:
            from google import genai
            self.client = genai.Client(api_key=key)
        if os.environ.get("GEMINI_MODEL"):
            self.models = [os.environ["GEMINI_MODEL"]] + self.models

    @property
    def ok(self) -> bool:
        return self.client is not None

    def ask(self, prompt: str, as_json: bool = False, temperature: float = 0.4) -> str:
        from google.genai import types
        conf = types.GenerateContentConfig(
            temperature=temperature,
            response_mime_type="application/json" if as_json else "text/plain")
        last = None
        for m in self.models:
            for attempt in range(2):
                try:
                    r = self.client.models.generate_content(model=m, contents=prompt, config=conf)
                    if r.text:
                        log.info("  Gemini 使用模型 %s", m)
                        return r.text
                except Exception as e:  # noqa: BLE001
                    last = e
                    log.warning("  Gemini %s 失敗（第 %d 次）：%s", m, attempt + 1, str(e)[:200])
                    time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"所有 Gemini 模型都失敗：{last}")


def _extract_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    return json.loads(text)


# ------------------------------------------------------------------ 3. select
def quotas(cfg: dict) -> dict:
    total = cfg["episode"]["total_items"]
    q = {k: max(1, round(total * c["share"] / 100)) for k, c in cfg["categories"].items()}
    return q


CLASSIFY_PROMPT = """你是台灣房仲業務的新聞編輯。以下是今天抓到的新聞標題清單（JSON）。
請為每一則判斷分類與重要性，只回傳 JSON 陣列，格式：
[{{"id":"n000","category":"realestate|macro|gov|bank|drop","importance":1-5}}]

分類定義：
- realestate 房地產營建：房市交易、房價、預售屋、建案、建商與營建股、土地標售、都更危老、營造建材、租屋市場
- macro 總體經濟：GDP、通膨、利率（含美國聯準會）、匯率、出口、景氣、大盤股市整體走勢
- gov 政府政策：內政部、財政部、立法院等關於房地產的稅制、法規、社宅、都市計畫、經紀業規範
- bank 銀行政策：央行利率與信用管制、房貸利率與成數、銀行不動產放款、新青安、金管會房貸措施
- drop：與以上無關、社會新聞、個股（非營建）漲跌、業配或像建案廣告的報導、重複內容

重要性（站在房仲營業員角度）：5=直接影響買賣決策或成交量；4=重要趨勢；3=值得知道；2=次要；1=幾乎無關
{local_rule}
新聞清單：
{items}
"""


def select(items: list[Item], cfg: dict, gem: Gemini | None) -> list[Item]:
    q = quotas(cfg)
    local = cfg.get("local_keywords") or []
    if gem and gem.ok and items:
        payload = [{"id": i.id, "title": i.title, "source": i.source, "hint": i.hint,
                    "snippet": i.snippet[:80]} for i in items[:400]]
        local_rule = (f"若新聞與這些地區有關，重要性 +1（最高 5）：{'、'.join(local)}\n"
                      if local else "")
        try:
            res = _extract_json(gem.ask(CLASSIFY_PROMPT.format(
                local_rule=local_rule, items=json.dumps(payload, ensure_ascii=False)),
                as_json=True, temperature=0.1))
            by_id = {r["id"]: r for r in res if isinstance(r, dict) and "id" in r}
            for it in items:
                r = by_id.get(it.id)
                if r:
                    it.category = r.get("category", it.hint)
                    it.importance = int(r.get("importance", 2))
        except Exception as e:  # noqa: BLE001
            log.warning("Gemini 分類失敗，改用關鍵字分類：%s", e)
    for it in items:  # 沒有 AI 結果時的備援
        if not it.category:
            it.category = it.hint
            it.importance = 3 + int(any(k in it.title for k in local))

    pool = {k: sorted([i for i in items if i.category == k],
                      key=lambda x: (-x.importance, -x.published)) for k in CAT_ORDER}
    picked = {k: pool[k][: q[k]] for k in CAT_ORDER}
    # 某類不足時，空出的名額依序補給 房地產營建 → 總經
    short = sum(q[k] - len(picked[k]) for k in CAT_ORDER)
    for k in ["realestate", "macro", "bank", "gov"]:
        if short <= 0:
            break
        extra = pool[k][len(picked[k]): len(picked[k]) + short]
        picked[k] += extra
        short -= len(extra)
    for k in CAT_ORDER:
        log.info("  %s：候選 %d → 選入 %d（目標 %d）",
                 cfg["categories"][k]["name"], len(pool[k]), len(picked[k]), q[k])
    return [i for k in CAT_ORDER for i in picked[k]]


def enough(items: list[Item], cfg: dict) -> bool:
    """候選量是否足夠（每類至少目標的 1.5 倍；房地產 1.2 倍）"""
    q = quotas(cfg)
    cnt = {k: sum(1 for i in items if i.hint == k) for k in CAT_ORDER}
    return all(cnt[k] >= q[k] * (1.2 if k == "realestate" else 1.5) for k in CAT_ORDER)


# ------------------------------------------------------------------ 4. enrich
def _resolve_gnews(url: str) -> str:
    if "news.google.com" not in url:
        return url
    try:
        from googlenewsdecoder import gnewsdecoder
        r = gnewsdecoder(url, interval=0.5)
        if r.get("status"):
            return r["decoded_url"]
    except Exception as e:  # noqa: BLE001
        log.debug("Google 新聞網址解碼失敗：%s", e)
    return url


def enrich(items: list[Item], budget_sec: int = 240) -> None:
    try:
        import trafilatura
    except ImportError:
        return
    start, ok = time.time(), 0
    for it in items:
        if time.time() - start > budget_sec:
            log.info("  抓內文時間到，略過其餘")
            break
        url = _resolve_gnews(it.link)
        if "news.google.com" in url:
            continue
        it.link = url
        try:
            raw = trafilatura.fetch_url(url)
            text = trafilatura.extract(raw, include_comments=False, include_tables=False) if raw else ""
            if text and len(text) > 80:
                it.body = re.sub(r"\s+", " ", text)[:1500]
                ok += 1
        except Exception as e:  # noqa: BLE001
            log.debug("內文抓取失敗 %s: %s", url, e)
    log.info("  取得內文 %d / %d 則", ok, len(items))


# ------------------------------------------------------------------ 5. script
SCRIPT_PROMPT = """你是 Podcast「{title}」的主持人，聽眾是台灣房仲營業員，在通勤或帶看前收聽。
請用以下新聞寫今天（{date}，{weekday}）的完整口語講稿。

結構與字數（請嚴格遵守比例，總長約 {total} 字）：
1. 開場（約 80 字）：問候、今天日期、一句話預告今日最重要的事
{sections}
{n}. 今日三重點（約 120 字）：用三句話收斂今天最該記住的事
{n2}. 結尾（約 50 字）：固定說「以上內容僅供參考，不構成投資或買賣建議。我們明天見。」

寫作規則：
- 繁體中文、台灣用語，像專業主持人在說話；不要用條列符號、表情符號、markdown、標題符號
- 段落轉換時用口語串場，例如「接下來看總體經濟」
- 數字、日期、百分比、金額只能照新聞原文，不可自行推算或編造；不確定就不要講數字
- 每則新聞提到來源媒體（例如「根據經濟日報報導」）
- 房地產營建段落：重要新聞後加一句「對業務的意義」，例如對買方、屋主或成交量的可能影響，用「可能」「值得留意」等保留語氣
- 同一事件多家報導時合併成一則講
- 只輸出講稿本文

新聞資料（JSON，依分類）：
{news}
"""


def build_script(items: list[Item], cfg: dict, gem: Gemini | None, today: dt.date) -> str:
    weekday = "星期" + "一二三四五六日"[today.weekday()]
    total = cfg["episode"]["target_chars"]
    cats = cfg["categories"]
    body_chars = total - 250
    if gem and gem.ok:
        lines, news = [], {}
        for n, k in enumerate(CAT_ORDER, start=2):
            name = cats[k]["name"]
            lines.append(f"{n}. {name}（約 {round(body_chars * cats[k]['share'] / 100)} 字，佔 {cats[k]['share']}%）")
            news[name] = [{"標題": i.title, "來源": i.source + (f"（另有{'、'.join(i.also[:2])}）" if i.also else ""),
                           "重要性": i.importance, "內容": (i.body or i.snippet)[:900]}
                          for i in items if i.category == k]
        try:
            text = gem.ask(SCRIPT_PROMPT.format(
                title=cfg["podcast"]["title"], date=f"{today.month}月{today.day}日", weekday=weekday,
                total=total, sections="\n".join(lines), n=len(CAT_ORDER) + 2, n2=len(CAT_ORDER) + 3,
                news=json.dumps(news, ensure_ascii=False)), temperature=0.5)
            text = re.sub(r"[#*_`>|]", "", text).strip()
            if len(text) > total * 0.4:
                return text
            log.warning("講稿太短（%d 字），改用備援稿", len(text))
        except Exception as e:  # noqa: BLE001
            log.warning("Gemini 寫稿失敗，改用備援稿：%s", e)
    return fallback_script(items, cfg, today, weekday)


def fallback_script(items: list[Item], cfg: dict, today: dt.date, weekday: str) -> str:
    title = cfg["podcast"]["title"]
    parts = [f"大家早安，歡迎收聽{title}。今天是{today.month}月{today.day}日{weekday}，以下是今天的新聞重點。"]
    for k in CAT_ORDER:
        sel = [i for i in items if i.category == k]
        if not sel:
            continue
        parts.append(f"接下來是{cfg['categories'][k]['name']}。")
        for i in sel:
            s = f"根據{i.source}報導，{i.title}。"
            extra = (i.body or i.snippet)[:120]
            if extra:
                s += extra.rstrip("。") + "。"
            parts.append(s)
    parts.append("以上內容僅供參考，不構成投資或買賣建議。我們明天見。")
    return "\n\n".join(parts)


# ------------------------------------------------------------------ 6. tts
def _chunks(text: str, size: int = 1800) -> list[str]:
    paras, out, cur = re.split(r"\n+", text), [], ""
    for p in paras:
        if len(cur) + len(p) > size and cur:
            out.append(cur)
            cur = ""
        cur += p + "\n"
    if cur.strip():
        out.append(cur)
    return out


async def _tts(text: str, path: Path, voice: str, rate: str) -> None:
    import edge_tts
    with path.open("wb") as f:
        for n, chunk in enumerate(_chunks(text), 1):
            for attempt in range(3):
                try:
                    buf = bytearray()
                    async for msg in edge_tts.Communicate(chunk, voice=voice, rate=rate).stream():
                        if msg["type"] == "audio":
                            buf += msg["data"]
                    if not buf:
                        raise RuntimeError("沒有收到音訊")
                    f.write(buf)
                    break
                except Exception as e:  # noqa: BLE001
                    log.warning("  語音第 %d 段失敗（第 %d 次）：%s", n, attempt + 1, e)
                    if attempt == 2:
                        raise
                    await asyncio.sleep(5 * (attempt + 1))


def synthesize(text: str, path: Path, cfg: dict) -> None:
    asyncio.run(_tts(text, path, cfg["tts"]["voice"], cfg["tts"]["rate"]))


# ------------------------------------------------------------------ 7. publish
def base_url(cfg: dict) -> str:
    u = (os.environ.get("PODCAST_BASE_URL") or cfg["podcast"].get("base_url") or "").rstrip("/")
    if not u and os.environ.get("GITHUB_REPOSITORY"):
        owner, repo = os.environ["GITHUB_REPOSITORY"].split("/", 1)
        u = f"https://{owner.lower()}.github.io/{repo}"
    return u or "http://localhost:8000"


def make_cover(path: Path) -> None:
    """產生 1400x1400 封面（純圖形，不需字型）"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return
    W = 1400
    img = Image.new("RGB", (W, W), (246, 239, 228))
    d = ImageDraw.Draw(img)
    d.ellipse([170, 170, 1230, 1230], fill=(232, 120, 72))
    d.polygon([(700, 360), (1010, 640), (390, 640)], fill=(255, 250, 242))      # 屋頂
    d.rectangle([470, 640, 930, 1000], fill=(255, 250, 242))                    # 屋身
    d.rectangle([640, 790, 760, 1000], fill=(232, 120, 72))                     # 門
    d.rectangle([520, 700, 610, 780], fill=(92, 140, 128))                      # 窗
    d.rectangle([790, 700, 880, 780], fill=(92, 140, 128))
    d.rectangle([860, 430, 920, 560], fill=(255, 250, 242))                     # 煙囪
    img.save(path, "PNG")


def duration_sec(mp3: Path) -> int:
    # edge-tts 預設輸出 48 kbps 單聲道
    return int(mp3.stat().st_size * 8 / 48000)


def publish(site: Path, cfg: dict, ep: dict) -> None:
    eps_file = site / "episodes.json"
    eps = json.loads(eps_file.read_text(encoding="utf-8")) if eps_file.exists() else []
    eps = [e for e in eps if e["date"] != ep["date"]] + [ep]
    eps.sort(key=lambda e: e["date"], reverse=True)
    keep = cfg["podcast"]["keep_episodes"]
    for old in eps[keep:]:
        for ext in ("mp3", "txt"):
            (site / "episodes" / f"{old['date']}.{ext}").unlink(missing_ok=True)
    eps = eps[:keep]
    eps_file.write_text(json.dumps(eps, ensure_ascii=False, indent=1), encoding="utf-8")

    if not (site / "cover.png").exists():
        make_cover(site / "cover.png")
    (site / ".nojekyll").touch()
    (site / "feed.xml").write_text(render_feed(cfg, eps), encoding="utf-8")
    (site / "index.html").write_text(render_index(cfg, eps), encoding="utf-8")


def render_feed(cfg: dict, eps: list[dict]) -> str:
    p, b = cfg["podcast"], base_url(cfg)
    items = []
    for e in eps:
        url = f"{b}/episodes/{e['date']}.mp3"
        pub = dt.datetime.fromisoformat(e["published"])
        items.append(f"""  <item>
   <title>{escape(e['title'])}</title>
   <description>{escape(e['summary'])}</description>
   <content:encoded><![CDATA[{e['shownotes']}]]></content:encoded>
   <enclosure url="{escape(url)}" length="{e['bytes']}" type="audio/mpeg"/>
   <guid isPermaLink="false">{escape(p['title'])}-{e['date']}</guid>
   <pubDate>{format_datetime(pub)}</pubDate>
   <itunes:duration>{e['duration']}</itunes:duration>
   <itunes:explicit>false</itunes:explicit>
  </item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:atom="http://www.w3.org/2005/Atom">
 <channel>
  <title>{escape(p['title'])}</title>
  <link>{escape(b)}/</link>
  <atom:link href="{escape(b)}/feed.xml" rel="self" type="application/rss+xml"/>
  <description>{escape(p['description'])}</description>
  <language>{p['language']}</language>
  <itunes:author>{escape(p['author'])}</itunes:author>
  <itunes:image href="{escape(b)}/cover.png"/>
  <itunes:category text="News"><itunes:category text="Business News"/></itunes:category>
  <itunes:explicit>false</itunes:explicit>
  <itunes:block>Yes</itunes:block>
{chr(10).join(items)}
 </channel>
</rss>
"""


def render_index(cfg: dict, eps: list[dict]) -> str:
    p, b = cfg["podcast"], base_url(cfg)
    rows = "\n".join(
        f"<article><h2>{html.escape(e['title'])}</h2>"
        f"<audio controls preload=\"none\" src=\"episodes/{e['date']}.mp3\"></audio>"
        f"<details><summary>本集新聞（{e['count']} 則）</summary>{e['shownotes']}</details></article>"
        for e in eps)
    return f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>{html.escape(p['title'])}</title>
<style>body{{font-family:-apple-system,"PingFang TC","Noto Sans TC",sans-serif;max-width:720px;margin:auto;padding:20px;background:#f6efe4;color:#2b2b2b}}
header{{display:flex;gap:16px;align-items:center}}header img{{width:88px;border-radius:16px}}
article{{background:#fff;border-radius:14px;padding:14px 18px;margin:14px 0}}h2{{font-size:17px;margin:4px 0 10px}}
audio{{width:100%}}details{{margin-top:8px;font-size:14px}}li{{margin:4px 0}}code{{word-break:break-all}}</style></head>
<body><header><img src="cover.png" alt=""><div><h1>{html.escape(p['title'])}</h1><p>{html.escape(p['description'])}</p></div></header>
<p>Podcast 訂閱網址：<code>{html.escape(b)}/feed.xml</code></p>
{rows}
</body></html>
"""


def shownotes(items: list[Item], cfg: dict) -> str:
    out = []
    for k in CAT_ORDER:
        sel = [i for i in items if i.category == k]
        if sel:
            lis = "".join(f'<li><a href="{html.escape(i.link)}">{html.escape(i.title)}</a>（{html.escape(i.source)}）</li>'
                          for i in sel)
            out.append(f"<p><b>{html.escape(cfg['categories'][k]['name'])}</b></p><ul>{lis}</ul>")
    return "".join(out)


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default="site", help="輸出網站資料夾")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--date", help="集數日期 YYYY-MM-DD（預設今天，台灣時間）")
    ap.add_argument("--fixture", help="用測試資料取代線上抓取")
    ap.add_argument("--no-ai", action="store_true", help="不呼叫 Gemini")
    ap.add_argument("--dry-run", action="store_true", help="不做語音、不更新 feed")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    cfg = load_config(Path(a.config))
    site = Path(a.site)
    (site / "episodes").mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(TPE)
    today = dt.date.fromisoformat(a.date) if a.date else now.date()
    gem = None if a.no_ai else Gemini(cfg["gemini"]["models"])
    if gem and not gem.ok:
        log.warning("沒有設定 GEMINI_API_KEY，改用標題備援稿")

    log.info("1/6 抓取新聞")
    if a.fixture:
        raw = [Item(**d) for d in json.loads(Path(a.fixture).read_text(encoding="utf-8"))]
    else:
        hours = cfg["episode"]["window_hours"]
        raw = collect(cfg, hours)
        while not enough(dedupe(list(raw)), cfg) and hours < cfg["episode"]["max_window_hours"]:
            hours = min(hours + 24, cfg["episode"]["max_window_hours"])
            log.info("新聞量不足，放寬到過去 %d 小時", hours)
            raw = collect(cfg, hours)
    log.info("  共 %d 則", len(raw))

    log.info("2/6 去重")
    items = dedupe(raw)
    log.info("  去重後 %d 則", len(items))
    if not items:
        log.error("沒有抓到任何新聞，停止")
        return 1

    log.info("3/6 分類與精選")
    picked = select(items, cfg, gem)

    log.info("4/6 抓內文")
    if not a.fixture:
        enrich(picked)

    log.info("5/6 撰寫講稿")
    script = build_script(picked, cfg, gem, today)
    (site / "episodes" / f"{today}.txt").write_text(script, encoding="utf-8")
    log.info("  講稿 %d 字", len(script))
    if a.dry_run:
        print(script)
        return 0

    log.info("6/6 語音合成與發布")
    mp3 = site / "episodes" / f"{today}.mp3"
    synthesize(script, mp3, cfg)
    wd = "一二三四五六日"[today.weekday()]
    first = next((i.title for i in picked if i.category == "realestate"), picked[0].title)
    ep = {
        "date": str(today),
        "title": f"{today.month}/{today.day}（{wd}）{first[:28]}",
        "summary": "、".join(i.title[:20] for i in picked[:5]),
        "shownotes": shownotes(picked, cfg),
        "published": now.replace(microsecond=0).isoformat(),
        "bytes": mp3.stat().st_size,
        "duration": duration_sec(mp3),
        "count": len(picked),
        "mix": {cfg["categories"][k]["name"]: sum(1 for i in picked if i.category == k) for k in CAT_ORDER},
    }
    publish(site, cfg, ep)
    log.info("完成：%s（%d 秒，%s）", ep["title"], ep["duration"], ep["mix"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
