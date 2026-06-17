# video_to_text — YouTube & Podcast 學習筆記助手

把 YouTube 影片或 Podcast 音訊自動轉成結構化的學習筆記。

## 功能總覽

### YouTube（`youtube/`）

1. **讀取影片內容**
   - 優先抓 YouTube 官方字幕（`youtube-transcript-api`）
   - 沒有官方字幕時，自動切換成本機 **faster-whisper** 語音辨識（完全免費、不需要任何 API key）
   - 支援非 YouTube 來源（例如 Google Drive 影音連結），一律用 Whisper 辨識
2. **整理重點**：用 `jieba` 中文斷詞 + 詞頻統計做規則式摘要，挑出重點句並標上時間戳
3. **產生學習筆記**：輸出成 Markdown，包含重點摘要、關鍵字、空白筆記欄位、完整逐字稿
4. **頻道功能**：輸入頻道名稱或網址，列出影片／Shorts／直播，勾選後批次產生筆記
5. **匯出 Excel**：把頻道影片清單（含上架日期）匯出成 `.xlsx`
6. **批次工作控制**：暫停／繼續／停止，停止訊號在 Whisper 辨識過程中也能即時生效

### Podcast（`podcast/`）

1. **多元輸入來源**
   - Podcast 名稱 → iTunes Search API 自動搜尋，多結果時跳出選台視窗
   - RSS feed 網址 → 解析所有單集，勾選後下載辨識
   - 直連音訊 URL（`.mp3` / `.m4a` 等）→ 直接辨識
   - SoundCloud、YouTube Podcast 等 yt-dlp 支援的平台頁面
2. **音訊下載與辨識**：`yt-dlp` 下載 → `faster_whisper.decode_audio` 載入 → 切成 10 分鐘段落逐段送 Whisper，避免長集數的記憶體不足問題
3. **即時進度顯示**：辨識段落以綠色文字即時呈現，狀態列同步顯示「已到 XX:XX」
4. **整理重點與輸出**：與 YouTube 版相同的 `jieba` 摘要邏輯，輸出成 Markdown（含發布日期、單集描述）
5. **Whisper 模型選擇**：GUI 提供 tiny / base / small / medium / large-v3 選項

## 檔案說明

```
video_to_text/
├── youtube/
│   ├── yt_notes_assistant.py   # YouTube 核心邏輯（CLI 可直接執行）
│   └── yt_notes_gui.py         # YouTube 桌面視窗介面（tkinter）
├── podcast/
│   ├── podcast_notes_assistant.py  # Podcast 核心邏輯（CLI 可直接執行）
│   └── podcast_notes_gui.py        # Podcast 桌面視窗介面（tkinter）
├── docs/
│   └── prompt_log_2026-06-17.md   # 開發紀錄
└── requirements.txt
```

## 安裝

```bash
pip install -r requirements.txt
# Podcast 名稱搜尋需額外安裝：
pip install feedparser
```

## 使用方式

### YouTube

```bash
# 命令列版本
python youtube/yt_notes_assistant.py "YouTube 影片網址或頻道名稱"

# 桌面視窗版本
python youtube/yt_notes_gui.py
```

GUI 輸入框可接受：
- YouTube 單部影片網址或名稱 → 直接產生筆記
- YouTube 頻道名稱或網址 → 列出影片清單，勾選後批次產生筆記或匯出 Excel
- 其他網址（Google Drive 等）→ 直接用 Whisper 辨識

### Podcast

```bash
# 命令列版本
python podcast/podcast_notes_assistant.py "Podcast 名稱或 RSS feed 網址"

# 桌面視窗版本
python podcast/podcast_notes_gui.py
```

GUI 輸入框可接受：
- Podcast 名稱（中英文）→ iTunes 搜尋，選台後列出單集
- RSS feed 網址 → 直接列出單集清單
- SoundCloud / YouTube Podcast 頁面 → yt-dlp 展開單集列表
- 直連音訊網址（`.mp3` / `.m4a` 等）→ 直接辨識產生筆記

## 開發紀錄

詳細的開發過程與每次需求變更，記錄在 [`docs/prompt_log_2026-06-17.md`](docs/prompt_log_2026-06-17.md)。
