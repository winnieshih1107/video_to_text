"""
YouTube 學習筆記助手
1. 讀取特定 YouTube 影片（網址或名稱搜尋）
2. 擷取字幕並用規則式方法整理重點
3. 輸出成 Markdown 學習筆記檔

不需要任何 LLM API key，只用：
- yt-dlp：解析網址 / 用名稱搜尋影片 / 下載音訊
- youtube-transcript-api：抓取官方字幕
- faster-whisper：當官方字幕不存在時，本機語音辨識當備案
- jieba：中文斷詞，做詞頻統計與重點句評分
"""

import os
import re
import sys
import tempfile
import threading
import time
from collections import Counter

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import jieba
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
from yt_dlp import YoutubeDL

PREFERRED_LANGS = ["zh-Hant", "zh-TW", "zh-Hans", "zh", "en"]

STOPWORDS = set("""
的 了 是 在 我 你 他 她 它 們 這 那 也 都 就 和 與 或 但 而 又 並
之 於 對 為 等 並且 因為 所以 如果 雖然 然後 還是 可以 可能 一個
一些 這個 那個 這些 那些 自己 大家 我們 你們 他們 不過 而且 其中
這樣 那樣 一下 一直 不會 沒有 已經 還有 就是 不是 還是 比較 非常
其他 包括 進行 透過 以及 以下 以上 例如 像是 這種 那種 什麼 怎麼
為什麼 哪些 如何 嗎 呢 吧 啊 喔 ㄟ 唷 - — ， 。 、 「 」 『 』 （ ） ! ?
""".split())


class JobControl:
    """讓批次處理（多部影片）可以在影片之間暫停/繼續/停止。
    無法中斷單部影片正在進行的字幕擷取或 Whisper 辨識，只能在每部影片處理完後生效。"""

    def __init__(self):
        self._pause = threading.Event()
        self._pause.set()
        self._stop = threading.Event()

    def reset(self):
        self._stop.clear()
        self._pause.set()

    def wait_if_paused(self):
        self._pause.wait()

    def is_paused(self) -> bool:
        return not self._pause.is_set()

    def request_pause(self):
        self._pause.clear()

    def request_resume(self):
        self._pause.set()

    def request_stop(self):
        self._stop.set()
        self._pause.set()

    def is_stopped(self) -> bool:
        return self._stop.is_set()


def is_url(text: str) -> bool:
    return bool(re.match(r"^https?://", text.strip(), re.IGNORECASE))


def extract_video_id(url: str) -> str | None:
    patterns = [
        r"(?:v=|/videos/|embed/|youtu\.be/|/v/|/shorts/)([A-Za-z0-9_-]{11})",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def is_channel_url(text: str) -> bool:
    return bool(re.search(r"youtube\.com/(channel/|@|c/|user/)", text, re.IGNORECASE))


CHANNEL_TAB_SUFFIXES = ("/videos", "/streams", "/shorts", "/featured", "/playlists", "/posts", "/courses")
CONTENT_TYPE_LABELS = {"videos": "影片", "shorts": "Shorts", "streams": "直播"}


def resolve_channel(query: str) -> tuple[str, str]:
    """回傳 (channel_base_url, channel_name)，base_url 不含分類 tab（/videos、/shorts...）。"""
    query = query.strip()
    ydl_opts = {"quiet": True, "skip_download": True, "no_warnings": True, "extract_flat": True}

    if is_channel_url(query):
        base = query.split("?")[0].rstrip("/")
        for suffix in CHANNEL_TAB_SUFFIXES:
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(base + "/videos", download=False)
        return base, info.get("channel") or info.get("title") or query

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch5:{query}", download=False)
        entries = info.get("entries") or []
        if not entries:
            raise ValueError(f"找不到符合「{query}」的頻道")

        candidates = []
        for e in entries:
            url = e.get("channel_url") or e.get("uploader_url")
            name = e.get("channel") or e.get("uploader")
            if url:
                candidates.append((url, name or query))
        if not candidates:
            raise ValueError(f"找不到「{query}」對應的頻道網址")

        # 搜尋結果的第一支影片，上傳頻道未必就是查詢的對象（可能只是標題提到這個名字）。
        # 優先挑「頻道名稱完全符合查詢」的結果，找不到才退而用第一個有頻道資訊的結果。
        for url, name in candidates:
            if name.strip().lower() == query.lower():
                return url.rstrip("/"), name

        channel_url, channel_name = candidates[0]
        return channel_url.rstrip("/"), channel_name


def list_channel_videos(query: str, max_videos: int | None = None,
                         content_types: tuple[str, ...] = ("videos",)) -> tuple[str, list[dict]]:
    """回傳 (頻道名稱, 影片列表)，每個影片是 {id, title, url, type}。
    content_types 可包含 "videos"（一般影片）、"shorts"、"streams"（直播）。
    max_videos=None 表示每個分類都列出全部。"""
    channel_base_url, channel_name = resolve_channel(query)

    videos = []
    seen_ids = set()
    for ctype in content_types:
        ydl_opts = {"quiet": True, "skip_download": True, "no_warnings": True, "extract_flat": True}
        if max_videos is not None:
            ydl_opts["playlistend"] = max_videos
        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"{channel_base_url}/{ctype}", download=False)
        except Exception:
            continue  # 該頻道沒有這個分類（例如沒有直播）

        for entry in info.get("entries") or []:
            if not entry or not entry.get("id") or entry["id"] in seen_ids:
                continue
            seen_ids.add(entry["id"])
            videos.append({
                "id": entry["id"],
                "title": entry.get("title", entry["id"]),
                "url": f"https://www.youtube.com/watch?v={entry['id']}",
                "type": CONTENT_TYPE_LABELS.get(ctype, ctype),
            })
    return channel_name, videos


def get_video_upload_date(video_id: str) -> str | None:
    """回傳影片上架日期，格式 YYYY-MM-DD；查不到則回傳 None。"""
    ydl_opts = {"quiet": True, "skip_download": True, "no_warnings": True}
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    upload_date = info.get("upload_date")
    if upload_date and len(upload_date) == 8:
        return f"{upload_date[0:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
    return None


def export_videos_to_excel(videos: list[dict], output_path: str, log=print, fetch_dates: bool = True,
                            control: "JobControl | None" = None) -> str:
    """把頻道影片清單匯出成 Excel 檔（含日期欄），回傳檔案路徑。
    fetch_dates=True 時會逐部影片查詢上架日期，影片數量多時會比較久。
    control 可傳入 JobControl，讓呼叫端能在影片之間暫停/停止。"""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "頻道影片清單"
    ws.append(["序號", "類型", "標題", "日期", "影片網址", "影片ID"])

    for i, v in enumerate(videos, 1):
        if control:
            control.wait_if_paused()
            if control.is_stopped():
                log(f"已停止，目前已匯出 {i - 1}/{len(videos)} 部")
                break
        date_str = ""
        if fetch_dates:
            log(f"({i}/{len(videos)}) 查詢日期：{v['title']}")
            try:
                date_str = get_video_upload_date(v["id"]) or ""
            except Exception:
                date_str = ""
        ws.append([i, v.get("type", ""), v["title"], date_str, v["url"], v["id"]])

    column_widths = [6, 8, 60, 12, 45, 14]
    for col, width in enumerate(column_widths, 1):
        ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = width

    wb.save(output_path)
    return output_path


def resolve_video(query: str) -> tuple[str, str]:
    """回傳 (video_id, title)。query 可以是網址或影片名稱關鍵字。"""
    query = query.strip()
    if is_url(query):
        vid = extract_video_id(query)
        if not vid:
            raise ValueError(f"無法從網址解析出影片 ID：{query}")
        target = f"https://www.youtube.com/watch?v={vid}"
    else:
        target = f"ytsearch1:{query}"

    ydl_opts = {"quiet": True, "skip_download": True, "no_warnings": True}
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(target, download=False)
        if "entries" in info:
            if not info["entries"]:
                raise ValueError(f"找不到符合「{query}」的影片")
            info = info["entries"][0]
        return info["id"], info.get("title", info["id"])


def fetch_transcript(video_id: str) -> list[dict]:
    api = YouTubeTranscriptApi()
    transcript_list = api.list(video_id)
    try:
        transcript = transcript_list.find_transcript(PREFERRED_LANGS)
    except NoTranscriptFound:
        transcript = next(iter(transcript_list))
        if transcript.is_translatable:
            transcript = transcript.translate("zh-Hant")
    fetched = transcript.fetch()
    return [{"text": s.text, "start": s.start, "duration": s.duration} for s in fetched]


_whisper_model_cache: dict = {}


def _get_whisper_model(model_size: str = "small"):
    if model_size not in _whisper_model_cache:
        from faster_whisper import WhisperModel
        _whisper_model_cache[model_size] = WhisperModel(model_size, device="cpu", compute_type="int8")
    return _whisper_model_cache[model_size]


def transcribe_url_with_whisper(url: str, model_size: str = "small", log=print,
                                 control: "JobControl | None" = None) -> tuple[str, list[dict]]:
    """官方字幕不存在時的備案：用 yt-dlp 從任意網址（YouTube、Google Drive、Vocaroo...等
    yt-dlp 支援的來源）下載音訊，再用本機 faster-whisper 轉成逐字稿。
    完全免費、不需要 API key，但比抓字幕慢，且辨識結果可能有誤差。
    control 可傳入 JobControl，讓呼叫端能在辨識過程中（每一段字幕之間）暫停/停止，
    不用等整部影片辨識完才生效。
    回傳 (標題, segments)。"""
    log("下載音訊中...")
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tempfile.gettempdir(), "yt_notes_audio_%(id)s.%(ext)s"),
        "overwrites": True,
    }
    audio_path = None
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            downloads = info.get("requested_downloads") or []
            audio_path = downloads[0]["filepath"] if downloads else ydl.prepare_filename(info)
            title = info.get("title") or info.get("id") or url

        log("載入 Whisper 模型並進行語音辨識中（依影片長度，可能需要幾分鐘）...")
        model = _get_whisper_model(model_size)
        segments, transcribe_info = model.transcribe(
            audio_path, beam_size=5, vad_filter=True,
            initial_prompt="以下是繁體中文的句子",
        )
        log(f"辨識語言：{transcribe_info.language}（信心度 {transcribe_info.language_probability:.0%}）")

        result = []
        for seg in segments:
            if control:
                control.wait_if_paused()
                if control.is_stopped():
                    log(f"使用者已停止，已辨識到 [{format_timestamp(seg.start)}]，中斷後續辨識。")
                    break
            text = seg.text.strip()
            log(f"[{format_timestamp(seg.start)} -> {format_timestamp(seg.end)}] {text}")
            result.append({"text": text, "start": seg.start, "duration": seg.end - seg.start})
        return title, result
    finally:
        if audio_path and os.path.exists(audio_path):
            os.remove(audio_path)


def transcribe_with_whisper(video_id: str, model_size: str = "small", log=print,
                             control: "JobControl | None" = None) -> list[dict]:
    """YouTube 影片專用的 Whisper 備案（沿用 video_id），內部呼叫 transcribe_url_with_whisper。"""
    _, segments = transcribe_url_with_whisper(
        f"https://www.youtube.com/watch?v={video_id}", model_size=model_size, log=log, control=control
    )
    return segments


SENTENCE_END_RE = re.compile(r"[。！？!?]\s*$")


def build_chunks(segments: list[dict], max_len: int = 80) -> list[dict]:
    """把逐字幕片段合併成適合當「一句重點」的片段，依標點或長度上限切分，
    每個 chunk 都保留對應的開始時間。可避免無標點的自動字幕被當成一整塊文字。"""
    chunks = []
    buf = ""
    start = None
    for seg in segments:
        text = seg["text"].strip()
        if not text:
            continue
        if start is None:
            start = seg["start"]
        sep = "" if re.search(r"[一-鿿]", text) or not buf else " "
        buf += sep + text
        if SENTENCE_END_RE.search(buf) or len(buf) >= max_len:
            chunks.append({"text": buf.strip(), "start": start})
            buf = ""
            start = None
    if buf.strip():
        chunks.append({"text": buf.strip(), "start": start})
    return chunks


def format_timestamp(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def summarize(segments: list[dict], top_n: int = 8) -> tuple[list[dict], list[tuple[str, float]]]:
    chunks = build_chunks(segments)
    full_text = "".join(c["text"] for c in chunks)

    words = [w for w in jieba.cut(full_text) if w.strip() and w not in STOPWORDS and len(w.strip()) > 1]
    freq = Counter(words)

    scored = []
    for idx, chunk in enumerate(chunks):
        chunk_words = [w for w in jieba.cut(chunk["text"]) if w.strip() and w not in STOPWORDS and len(w.strip()) > 1]
        if not chunk_words:
            continue
        score = sum(freq[w] for w in chunk_words) / len(chunk_words)
        scored.append((idx, chunk, score))

    top = sorted(scored, key=lambda x: x[2], reverse=True)[:top_n]
    top_in_order = [t for t in sorted(top, key=lambda x: x[0])]
    key_chunks = [t[1] for t in top_in_order]

    keywords = freq.most_common(15)
    return key_chunks, keywords


def build_notes_markdown(url: str, title: str,
                          key_chunks: list[dict], keywords: list[tuple[str, float]],
                          transcript_chunks: list[dict] | None = None,
                          transcript_source: str = "YouTube 字幕") -> str:
    lines = [
        f"# 學習筆記：{title}",
        "",
        f"- 來源連結：{url}",
        f"- 逐字稿來源：{transcript_source}",
        "",
        "## 重點摘要",
        "",
    ]
    for chunk in key_chunks:
        prefix = f"[{format_timestamp(chunk['start'])}] " if chunk.get("start") is not None else ""
        lines.append(f"- {prefix}{chunk['text']}")

    lines += ["", "## 關鍵字", ""]
    lines.append("、".join(f"{w}（{c}）" for w, c in keywords))

    lines += [
        "",
        "## 我的筆記",
        "",
        "- ",
        "",
        "## 待複習 / 問題",
        "",
        "- ",
        "",
    ]

    if transcript_chunks:
        lines += ["## 完整逐字稿", ""]
        for chunk in transcript_chunks:
            prefix = f"[{format_timestamp(chunk['start'])}] " if chunk.get("start") is not None else ""
            lines.append(f"{prefix}{chunk['text']}")
            lines.append("")

    return "\n".join(lines)


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip()[:80]


def write_text_with_retry(path: str, content: str, retries: int = 3, delay: float = 1.0):
    """寫入檔案，失敗時重試幾次。Windows 上防毒軟體/雲端同步常常會在檔案剛建立時
    短暫鎖定，造成偶發的 PermissionError，重試通常就能解決。"""
    last_error = None
    for attempt in range(retries):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return
        except PermissionError as e:
            last_error = e
            if attempt < retries - 1:
                time.sleep(delay)
    raise last_error


def process_video(video_id: str, title: str, output_dir: str = ".", log=print,
                   control: "JobControl | None" = None) -> str:
    """抓字幕、整理重點、附上完整逐字稿，輸出 Markdown 筆記檔，回傳檔案路徑。
    沒有官方字幕時，自動改用本機 Whisper 語音辨識當備案。"""
    transcript_source = "YouTube 字幕"
    try:
        segments = fetch_transcript(video_id)
    except (TranscriptsDisabled, NoTranscriptFound):
        log("這部影片沒有官方字幕，改用本機 Whisper 語音辨識...")
        segments = transcribe_with_whisper(video_id, log=log, control=control)
        transcript_source = "Whisper 語音辨識（自動生成，可能有誤差）"

    key_chunks, keywords = summarize(segments)
    transcript_chunks = build_chunks(segments)
    url = f"https://www.youtube.com/watch?v={video_id}"
    markdown = build_notes_markdown(url, title, key_chunks, keywords, transcript_chunks, transcript_source)

    filename = f"{sanitize_filename(title)}_筆記.md"
    output_path = os.path.join(output_dir, filename)
    write_text_with_retry(output_path, markdown)
    return output_path


def process_external_url(url: str, output_dir: str = ".", model_size: str = "small", log=print,
                          control: "JobControl | None" = None) -> str:
    """處理非 YouTube 字幕來源的影片/音訊網址（例如 Google Drive、Vocaroo 等 yt-dlp 支援的連結）。
    一律用本機 Whisper 語音辨識產生逐字稿，再整理成 Markdown 筆記檔，回傳檔案路徑。"""
    title, segments = transcribe_url_with_whisper(url, model_size=model_size, log=log, control=control)

    key_chunks, keywords = summarize(segments)
    transcript_chunks = build_chunks(segments)
    markdown = build_notes_markdown(
        url, title, key_chunks, keywords, transcript_chunks,
        transcript_source="Whisper 語音辨識（自動生成，可能有誤差）",
    )

    title_no_ext = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", title)
    filename = f"{sanitize_filename(title_no_ext)}_筆記.md"
    output_path = os.path.join(output_dir, filename)
    write_text_with_retry(output_path, markdown)
    return output_path


def run(query: str, output_dir: str = ".") -> str:
    print(f"解析影片：{query}")
    video_id, title = resolve_video(query)
    print(f"找到影片：{title} ({video_id})")

    print("擷取字幕中並整理重點...")
    output_path = process_video(video_id, title, output_dir)

    print(f"學習筆記已儲存：{output_path}")
    return output_path


if __name__ == "__main__":
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
    else:
        query = input("請輸入 YouTube 影片網址、影片名稱、或頻道名稱/網址：").strip()

    if is_url(query) and not is_channel_url(query) and extract_video_id(query) is None:
        print(f"非 YouTube 連結，改用 Whisper 直接轉逐字稿：{query}")
        path = process_external_url(query)
        print(f"已儲存：{path}")
    elif is_channel_url(query) or not is_url(query):
        try:
            channel_name, videos = list_channel_videos(query)
        except ValueError:
            videos = []
        if videos:
            print(f"頻道：{channel_name}，共找到 {len(videos)} 部影片：")
            for i, v in enumerate(videos, 1):
                print(f"  {i}. {v['title']}")
            choice = input("請輸入要整理的影片編號（多個用逗號分隔，例如 1,3）：").strip()
            indices = [int(x) for x in re.split(r"[,，\s]+", choice) if x.strip()]
            for i in indices:
                v = videos[i - 1]
                print(f"\n處理：{v['title']}")
                path = process_video(v["id"], v["title"])
                print(f"已儲存：{path}")
        else:
            run(query)
    else:
        run(query)
