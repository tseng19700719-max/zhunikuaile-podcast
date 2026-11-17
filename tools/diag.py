"""診斷：測試候選 RSS 來源與 Google 新聞網址解碼（暫時性工具）"""
import time, traceback, feedparser, trafilatura, requests
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
FEEDS = [
 "https://money.udn.com/rssfeed/news/1001/5621?ch=money",
 "https://money.udn.com/rssfeed/news/1001/5621",
 "https://money.udn.com/rssfeed/news/1001/5588?ch=money",
 "https://udn.com/rssfeed/news/2/6644?ch=news",
 "https://feeds.feedburner.com/ettoday/house",
 "https://feeds.feedburner.com/ettoday/finance",
 "https://feeds.feedburner.com/rsscna/finance",
 "https://news.ltn.com.tw/rss/business.xml",
 "https://estate.ltn.com.tw/rss/news.xml",
 "https://www.chinatimes.com/rss/realtimenews-money.xml",
 "https://www.ctee.com.tw/feed",
 "https://www.ctee.com.tw/rss_web/livenews/house",
 "https://tw.news.yahoo.com/rss/real-estate",
 "https://tw.news.yahoo.com/rss/finance",
 "https://news.housefun.com.tw/rss",
 "https://www.businesstoday.com.tw/rss",
 "https://www.storm.mg/feeds/category/22172",
]
now = time.time()
for u in FEEDS:
    try:
        r = requests.get(u, headers={"User-Agent": UA}, timeout=15)
        fp = feedparser.parse(r.content)
        n = len(fp.entries)
        fresh = sum(1 for e in fp.entries if (e.get("published_parsed") or e.get("updated_parsed")) and now - time.mktime(e.get("published_parsed") or e.get("updated_parsed")) < 86400*2)
        ex = fp.entries[0] if n else {}
        print(f"FEED {r.status_code} n={n} fresh48h={fresh} {u}\n   e.g. {ex.get('title','')[:40]} | {ex.get('link','')[:90]}")
        if n:
            raw = trafilatura.fetch_url(ex.get("link"))
            txt = trafilatura.extract(raw) if raw else None
            print(f"   body={len(txt or '')}")
    except Exception as e:
        print(f"FEED ERR {u}: {e}")

print("\n=== Google News decode ===")
fp = feedparser.parse("https://news.google.com/rss/search?q=%E6%88%BF%E5%B8%82+when:1d&hl=zh-TW&gl=TW&ceid=TW:zh-Hant", agent=UA)
links = [e.link for e in fp.entries[:3]]
print("sample:", links[:1])
try:
    from googlenewsdecoder import gnewsdecoder
    for l in links:
        try:
            print("decode:", gnewsdecoder(l, interval=1))
        except Exception:
            traceback.print_exc()
except Exception:
    traceback.print_exc()
# 方法 2：直接跟隨轉址
for l in links[:2]:
    try:
        r = requests.get(l, headers={"User-Agent": UA}, timeout=15, allow_redirects=True)
        print("redirect:", r.status_code, r.url[:100], len(r.text))
    except Exception as e:
        print("redirect err", e)
