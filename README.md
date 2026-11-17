# 住你快樂podcast

每天早上自動產生一集房地產新聞 Podcast，台灣時間 **約 06:30 前**完成，手機打開 Podcast App 就能聽。

| 內容 | 佔比 | 每集則數 |
|---|---|---|
| 房地產營建 | 60% | 18 |
| 總體經濟 | 20% | 6 |
| 政府政策 | 10% | 3 |
| 銀行政策 | 10% | 3 |

比例同時套用在**新聞則數**和**講稿字數**上（預設每集約 4,500 字、15–18 分鐘）。

## 運作方式

```
Google 新聞關鍵字 + 經濟日報／中央社／自由時報 RSS（過去 24 小時）
  → 標題相似度去重（同一事件多家報導合併）
  → Gemini 分類＋評分（站在房仲營業員角度），依比例挑選
  → 抓內文 → Gemini 寫口語講稿（房地產新聞附一句「對業務的意義」）
  → edge-tts 轉語音 → 更新 Podcast RSS（保留最近 14 集）
```

保護機制：
- 週末或連假新聞不夠時，自動把時間放寬到 48、72 小時
- 某一類新聞不夠時，空出的名額優先補給房地產營建
- Gemini 失敗時會換下一個模型；全部失敗就改用「標題＋摘要」的備援稿，當天照樣有節目

## 安裝步驟（約 10 分鐘，跟 Sandy Podcast 相同）

1. **建立新 repo**：GitHub → New repository，名稱建議 `zhunikuaile-podcast`（英數字），選 **Public**（免費 Pages 需要公開 repo）。
2. **上傳檔案**：把這個資料夾裡的所有檔案上傳，包含 `.github/workflows/daily.yml`。
3. **設定 Gemini 金鑰**：repo → Settings → Secrets and variables → Actions → New repository secret
   - Name：`GEMINI_API_KEY`
   - Value：你的 Gemini API Key（可以跟 Sandy Podcast 共用同一把）
4. **第一次手動執行**：Actions → 「住你快樂podcast 每日更新」→ Run workflow，約 5–10 分鐘。
5. **開啟 Pages**：Settings → Pages → Source 選 **Deploy from a branch**，Branch 選 **gh-pages / (root)** → Save。
6. **訂閱**：等 1–2 分鐘後，訂閱網址是
   `https://<你的GitHub帳號>.github.io/zhunikuaile-podcast/feed.xml`
   - iPhone「Podcast」App：資料庫 → 右上角「⋯」→「以 URL 追蹤節目」→ 貼上網址
   - Android：Pocket Casts 或 Podcast Addict →「以網址新增」
   - 也可以直接用瀏覽器開 `https://<帳號>.github.io/zhunikuaile-podcast/` 收聽

之後每天自動更新，不用再動。

## 常改的設定（config.yaml）

| 想要 | 改哪裡 |
|---|---|
| 加入業務負責區域（優先排序＋多抓當地新聞） | `local_keywords: ["桃園", "中壢", "青埔"]` |
| 節目長一點或短一點 | `episode.target_chars`（4500 ≈ 15–18 分鐘） |
| 換男聲 | `tts.voice: "zh-TW-YunJheNeural"` |
| 調整比例 | `categories.*.share`（加總必須 = 100） |
| 加關鍵字 | `categories.*.queries` |
| 改發布時間 | `.github/workflows/daily.yml` 的 `cron`（UTC 時間，台灣 −8 小時） |

## 費用

- GitHub Actions + Pages：公開 repo 免費
- edge-tts：免費
- Gemini：每天約 2 次呼叫，使用 flash-lite 等級模型，費用極低

## 隱私

- RSS 已加 `itunes:block`，不會被 Apple Podcasts 目錄收錄；網頁設了 `noindex`
- repo 是公開的，**不要**在 config.yaml 寫入個人電話、公司名稱等資料
- 節目內容只是公開新聞的整理，不含任何物件廣告，不涉及《不動產經紀業管理條例》的廣告規定

## 本機測試

```bash
pip install -r requirements.txt
python tests/test_offline.py                      # 離線單元測試
python podcast.py --site site --dry-run           # 實際抓新聞、只印講稿（需網路）
GEMINI_API_KEY=xxx python podcast.py --site site  # 完整產出一集
```
