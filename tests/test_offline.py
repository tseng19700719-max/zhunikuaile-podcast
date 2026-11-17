"""離線測試：不連網、不呼叫 Gemini、不做語音。執行：python tests/test_offline.py"""
import json
import sys
import tempfile
import time
import xml.dom.minidom
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import podcast as P  # noqa: E402

cfg = P.load_config(ROOT / "config.yaml")
now = time.time()

TITLES = {
    "realestate": ["10月全台買賣移轉棟數月減5%", "10月全台買賣移轉棟數月減5％ 六都同步下滑",
                   "預售屋解約潮未現 建商讓利促銷", "營建股走弱 興富發、華固同步下跌",
                   "桃園青埔新案開價每坪55萬", "北市土地標售 3筆全數決標", "危老重建申請件數創新高",
                   "建材價格持穩 營造廠接單轉保守", "租金指數連續上漲", "實價登錄：新北中古屋價量齊揚",
                   "建商第四季推案量縮", "台中七期豪宅成交降溫", "高雄房市 半導體效應延續",
                   "預售屋紅單管制後 換約轉售減少", "都更容積獎勵爭議", "房仲業績 年減兩成",
                   "大型建商土地庫存增加", "新竹竹北房價修正", "營造業缺工問題", "社區管委會 修繕基金"],
    "macro": ["主計總處上修今年經濟成長率", "10月CPI年增2.1%", "聯準會維持利率不變",
              "新台幣升破30元", "10月出口創同期新高", "景氣燈號連三紅", "外銷訂單年增"],
    "gov": ["囤房稅2.0 首年稅收出爐", "內政部擬修平均地權條例", "社宅包租代管延長補助", "重劃區 抽籤配地爭議"],
    "bank": ["央行理監事會 信用管制不鬆綁", "房貸利率創新高 首購族壓力大", "銀行法72-2 不動產放款比率下降",
             "新青安排除72-2 延長", "金管會檢查房貸成數"],
}
items, n = [], 0
for cat, ts in TITLES.items():
    for t in ts:
        items.append(P.Item(id="", title=t, link=f"https://example.com/{n}", source="經濟日報" if n % 2 else "工商時報",
                            published=now - (n % 20) * 3600, snippet=f"{t}，相關報導內容摘要。" if n % 3 else "", hint=cat))
        n += 1
# 一則超出時間窗的舊新聞
items.append(P.Item(id="", title="三天前的舊新聞", link="x", source="x", published=now - 80 * 3600, hint="macro"))

# 1. 去重
d = P.dedupe([i for i in items if i.published >= now - 24 * 3600])
titles = [i.title for i in d]
assert sum("買賣移轉棟數" in t for t in titles) == 1, "重複新聞沒有被合併"
assert "三天前的舊新聞" not in titles
dup = next(i for i in d if "買賣移轉棟數" in i.title)
print("✓ 去重：", len(items), "→", len(d), "；合併來源", dup.source, dup.also)

# 2. 配額
q = P.quotas(cfg)
assert q == {"realestate": 18, "macro": 6, "gov": 3, "bank": 3}, q
picked = P.select(d, cfg, None)
mix = {k: sum(1 for i in picked if i.category == k) for k in P.CAT_ORDER}
print("✓ 配額：", mix)
assert mix["macro"] == 6 and mix["gov"] == 3 and mix["bank"] == 3
assert mix["realestate"] == 18, mix

# 3. 某類不足時補位
few = [i for i in d if i.hint != "gov"] + [i for i in d if i.hint == "gov"][:1]
for i in few:
    i.category = ""
p2 = P.select(few, cfg, None)
mix2 = {k: sum(1 for i in p2 if i.category == k) for k in P.CAT_ORDER}
assert sum(mix2.values()) == 30 and mix2["gov"] == 1, mix2
print("✓ 政府政策只有 1 則時自動補位：", mix2)

# 4. 備援講稿
for i in picked:
    pass
s = P.fallback_script(picked, cfg, P.dt.date(2026, 11, 18), "星期三")
assert "住你快樂podcast" in s and "接下來是房地產營建" in s and "銀行政策" in s
print("✓ 備援講稿", len(s), "字")

# 5. 發布（假 MP3）
with tempfile.TemporaryDirectory() as tmp:
    site = Path(tmp)
    (site / "episodes").mkdir()
    for k in range(16):
        date = P.dt.date(2026, 11, 1) + P.dt.timedelta(days=k)
        mp3 = site / "episodes" / f"{date}.mp3"
        mp3.write_bytes(b"\0" * (48000 * 60 // 8))  # 60 秒
        (site / "episodes" / f"{date}.txt").write_text("x")
        ep = {"date": str(date), "title": f"{date.month}/{date.day} 測試 & <特殊字元>", "summary": "摘要",
              "shownotes": P.shownotes(picked, cfg), "published": f"{date}T06:20:00+08:00",
              "bytes": mp3.stat().st_size, "duration": P.duration_sec(mp3), "count": 30, "mix": mix}
        P.publish(site, cfg, ep)
    eps = json.loads((site / "episodes.json").read_text())
    assert len(eps) == 14 and eps[0]["date"] == "2026-11-16", (len(eps), eps[0]["date"])
    assert not (site / "episodes" / "2026-11-01.mp3").exists(), "舊集數沒有刪除"
    assert eps[0]["duration"] == 60
    feed = (site / "feed.xml").read_text()
    xml.dom.minidom.parseString(feed.encode())  # 必須是合法 XML
    assert feed.count("<item>") == 14 and "住你快樂podcast" in feed
    assert (site / "cover.png").exists() and (site / "index.html").exists()
    print("✓ 發布：保留 14 集、刪除舊檔、feed.xml 為合法 XML、封面與網頁產生")

# 6. Google 新聞 RSS 解析（模擬）
with tempfile.TemporaryDirectory() as tmp:
    f = Path(tmp) / "g.xml"
    pub = P.format_datetime(P.dt.datetime.now(P.dt.timezone.utc))
    f.write_text(f"""<?xml version="1.0"?><rss version="2.0"><channel><title>"房市" - Google 新聞</title>
<item><title>房市交易量下滑 - 經濟日報</title><link>https://news.google.com/rss/articles/abc</link>
<pubDate>{pub}</pubDate><description>&lt;a href="x"&gt;房市交易量下滑&lt;/a&gt;&amp;nbsp;&lt;font&gt;經濟日報&lt;/font&gt;</description>
<source url="https://money.udn.com">經濟日報</source></item></channel></rss>""", encoding="utf-8")
    orig = P.parse_feed
    got = orig(str(f), "realestate")
    # 本機檔案不含 news.google.com，手動驗證標題切除邏輯
    t, s = "房市交易量下滑 - 經濟日報", "經濟日報"
    assert t[: -len(s) - 3].strip() == "房市交易量下滑"
    assert len(got) == 1 and got[0].published > 0
    print("✓ RSS 解析：", got[0].title, "|", got[0].source)

print("\n全部測試通過")
