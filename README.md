# video_to_text — YouTube 學習筆記助手

把 YouTube 影片（單部影片、整個頻道、甚至非 YouTube 的影音連結）自動轉成結構化的學習筆記。

## 功能

1. **讀取影片內容**
   - 優先抓 YouTube 官方字幕（`youtube-transcript-api`）
   - 沒有官方字幕時，自動切換成本機 **faster-whisper** 語音辨識當備案（完全免費、不需要任何 API key）
   - 支援非 YouTube 來源（例如 Google Drive 影音連結），一律用 Whisper 辨識
2. **整理重點**：用 `jieba` 中文斷詞 + 詞頻統計做規則式摘要，挑出重點句並標上時間戳，不需要 LLM API
3. **產生學習筆記**：輸出成 Markdown，包含重點摘要、關鍵字、空白筆記欄位、完整逐字稿
4. **頻道功能**：輸入頻道名稱或網址，列出該頻道的影片／Shorts／直播，勾選後批次產生筆記
5. **匯出 Excel**：把頻道影片清單（含上架日期）匯出成 `.xlsx`
6. **批次工作控制**：產生筆記、匯出 Excel 都可以暫停／繼續／停止，停止訊號在 Whisper 辨識過程中也能即時生效

## 檔案說明

| 檔案 | 說明 |
|---|---|
| `yt_notes_assistant.py` | 核心邏輯：影片/頻道解析、字幕擷取、Whisper 備案、摘要、Excel 匯出，可直接當 CLI 執行 |
| `yt_notes_gui.py` | 桌面視窗操作介面（tkinter），包住上述核心邏輯 |
| `requirements.txt` | 所需的 Python 套件 |

## 使用方式

```bash
pip install -r requirements.txt

# 命令列版本
python yt_notes_assistant.py "YouTube 影片網址或頻道名稱"

# 桌面視窗版本
python yt_notes_gui.py
```

GUI 輸入框可接受：
- YouTube 單部影片網址或名稱 → 直接產生筆記
- YouTube 頻道名稱或網址 → 列出影片清單，勾選後批次產生筆記，或匯出清單至 Excel
- 其他網址（例如 Google Drive 影音連結）→ 直接用 Whisper 轉逐字稿

## 開發紀錄

詳細的開發過程與每次需求變更，記錄在 [`docs/prompt_log_2026-06-17.md`](docs/prompt_log_2026-06-17.md)。
原始 prompt 紀錄見 [`docs/prompts_raw_2026-06-17.md`](docs/prompts_raw_2026-06-17.md)。
