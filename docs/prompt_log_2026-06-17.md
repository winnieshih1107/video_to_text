# 開發紀錄 — 2026-06-17

紀錄「YouTube & Podcast 學習筆記助手」（video_to_text）從零開始建置的需求演進與每次調整。

---

## YouTube 學習筆記助手

## 1. 建立助手（初版）
需求：讀取特定 YouTube 影片、整理影片重點、生成學習筆記。
- 不接 LLM API，改用 `youtube-transcript-api` 抓字幕 + `jieba` 斷詞做詞頻統計的規則式摘要
- 輸出 Markdown 筆記（重點摘要、關鍵字、空白筆記欄位）
- 修正：無標點的自動字幕會被當成一整塊文字，改成依標點/長度切成有時間戳的片段

## 2. 建立使用者操作介面
- 改用 tkinter 桌面視窗，輸入框 + 按鈕 + 進度顯示區
- 背景執行緒處理，避免視窗卡死

## 3. 頻道功能：列出影片、勾選整理
需求：輸入頻道名稱，列出該頻道所有影片，勾選後再整理。
- 用 `yt-dlp` 的搜尋與頻道頁解析頻道網址與影片列表，不需要 YouTube Data API key
- GUI 加上勾選清單、全選/全不選

## 4. 修正「只抓 30 部」與擴大抓取範圍
- 原本程式寫死只列前 30 部，改成可自訂或列出全部
- 加上「抓取範圍」勾選：影片／Shorts／直播（YouTube 沒有「Reels」，對應的是 Shorts）

## 5. 字幕抓不到時的處理：本機 Whisper 備案
- 診斷出某些影片（例如直播存檔）沒有任何官方字幕
- 加入 `faster-whisper` 本機語音辨識當免費備案，不需要 API key
- 修正：Whisper 的辨識結果原本一次性等待完才顯示，改成逐段即時輸出，讓使用者看到進度

## 6. Excel 匯出
- 頻道影片清單可以匯出成 `.xlsx`，含序號、類型、標題、上架日期、網址
- 修正：原本會匯出全部影片，改成只匯出勾選的項目

## 7. 暫停／停止
- 批次產生筆記、批次匯出 Excel 都加上暫停/繼續/停止
- 修正：停止原本只在「兩部影片之間」生效，改成可以在 Whisper 逐段辨識過程中即時生效

## 8. 支援非 YouTube 連結
需求：用 Google Drive 等網址直接產生逐字稿。
- 非 YouTube、非頻道網址時，直接用 Whisper 下載音訊轉逐字稿
- 修正：輸出檔名殘留原始副檔名（如 `.m4a`）的問題

## 9. 頻道查詢失敗的疑難排解
發現兩類「找不到頻道」其實是誤判：
- 勾選的分類（例如只勾「直播」）在該頻道沒有任何內容時，誤判成「頻道不存在」並錯誤 fallback 成單部影片搜尋 → 改成準確區分「頻道真的找不到」與「頻道存在但分類是空的」
- 用人名搜尋頻道時，搜尋結果第一筆影片的上傳頻道未必是查詢對象 → 改成優先比對頻道名稱完全相符的結果

## 10. 整理成果並推送到 GitHub
- 撰寫 README.md 說明專案與使用方式
- 撰寫本開發紀錄
- 推送到 `https://github.com/winnieshih1107/video_to_text`

---

## Podcast 學習筆記助手

## 11. 建立 Podcast 筆記助手
需求：比照 YouTube 版的抓影方式，改為抓取 Podcast 音訊檔並產生學習筆記。
- 核心邏輯：`yt-dlp` 下載音訊 → `faster-whisper` 語音辨識 → `jieba` 摘要 → Markdown 輸出
- 支援三種輸入：RSS feed URL（用 `feedparser` 解析單集列表）、SoundCloud／YouTube Podcast 頁面（用 yt-dlp 展開）、直連音訊 URL（`.mp3` / `.m4a` 等，直接辨識）
- Markdown 筆記額外包含發布日期、單集描述

## 12. 加入 GUI 介面
- 建立 `podcast_notes_gui.py`，tkinter 桌面視窗，比照 `yt_notes_gui.py` 架構
- 單集勾選清單（含發布日期）、全選/全不選、暫停/停止
- Whisper 模型下拉選單（tiny / base / small / medium / large-v3）
- 開啟輸出資料夾按鈕

## 13. 加入 Podcast 名稱搜尋
需求：可以直接輸入頻道名稱，不必知道 RSS feed 網址。
- 非 URL 輸入時，呼叫 iTunes Search API（免費、不需 API key）搜尋 Podcast
- 唯一結果直接載入 RSS feed；多個結果在 GUI 跳出選台對話框（Listbox，支援雙擊選擇）
- CLI 版本顯示候選編號讓使用者輸入

## 14. 語音辨識文字同步呈現執行進度
需求：辨識文字要即時顯示，讓使用者清楚看到進度。
- 辨識段落（`[時間 -> 時間] 文字`）用綠色字體顯示，與一般狀態訊息視覺上分開
- 狀態列即時更新「語音辨識中… 已到 XX:XX」
- 語言偵測結果（`辨識語言：zh 95%`）改為灰色斜體，降低視覺干擾

## 15. 修正長集數記憶體不足（OOM）
問題：50 分鐘 Podcast 整段送 Whisper 時，numpy 嘗試分配 928 MiB 失敗。
- 第一次嘗試：用 ffmpeg 把音訊切成 10 分鐘的檔案段落再逐段辨識 → 失敗，因為 ffmpeg 不在系統 PATH
- 第二次嘗試：`shutil.which` + yt-dlp 內部路徑偵測找 ffmpeg → 仍找不到，改加 `chunk_length=30` 讓 faster-whisper 內部分段 → 依然 OOM（記憶體問題在音訊載入階段，不在 Whisper 推理）
- 最終解法：用 `faster_whisper.audio.decode_audio` 把全檔載入成 float32 numpy array（~192 MB / 50 分鐘），再用 Python slice 切成 10 分鐘段落，逐段送 Whisper。此方式使用 faster-whisper 自帶的解碼器，不需 ffmpeg 在 PATH，每段記憶體峰值 < 100 MB

## 16. 整理目錄結構並更新 GitHub
- 重組 repo：YouTube 工具移至 `youtube/`，Podcast 工具置於 `podcast/`
- 更新 README.md 涵蓋兩個工具的功能說明與使用方式
- 更新本開發紀錄
- 推送至 `https://github.com/winnieshih1107/video_to_text`
