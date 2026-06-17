"""
Podcast 學習筆記助手
1. 輸入 Podcast RSS feed 網址、單集音訊網址，或 yt-dlp 支援的 Podcast 平台連結
2. 用 yt-dlp 下載音訊，再用本機 faster-whisper 語音辨識轉成逐字稿
3. 用 jieba 詞頻統計整理重點，輸出成 Markdown 學習筆記檔

不需要任何 LLM API key，只用：
- yt-dlp：下載 Podcast 音訊（支援 SoundCloud、Spotify、Apple Podcasts、RSS 直連等）
- feedparser：解析 RSS feed，列出所有單集
- faster-whisper：本機語音辨識（不需 API key）
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
from yt_dlp import YoutubeDL

STOPWORDS = set("""
的 了 是 在 我 你 他 她 它 們 這 那 也 都 就 和 與 或 但 而 又 並
之 於 對 為 等 並且 因為 所以 如果 雖然 然後 還是 可以 可能 一個
一些 這個 那個 這些 那些 自己 大家 我們 你們 他們 不過 而且 其中
這樣 那樣 一下 一直 不會 沒有 已經 還有 就是 不是 還是 比較 非常
其他 包括 進行 透過 以及 以下 以上 例如 像是 這種 那種 什麼 怎麼
為什麼 哪些 如何 嗎 呢 吧 啊 喔 ㄟ 唷 - — ， 。 、 「 」 『 』 （ ） ! ?
""".split())


class JobControl:
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


def is_rss_feed(url: str) -> bool:
    """判斷網址是否為 RSS/Atom feed（嘗試抓取 header 或副檔名判斷）。"""
    lower = url.lower()
    if any(k in lower for k in ("/feed", "/rss", ".xml", "/podcast", "feed=rss")):
        return True
    return False


# ---------------------------------------------------------------------------
# iTunes Search API（依名稱搜尋 Podcast）
# ---------------------------------------------------------------------------

def search_itunes_podcasts(name: str, limit: int = 10) -> list[dict]:
    """用 iTunes Search API 搜尋 Podcast 名稱，回傳候選清單。
    每個項目：{name, author, feed_url}。不需要 API key，完全免費。"""
    import json
    import urllib.parse
    import urllib.request

    params = urllib.parse.urlencode({
        "term": name, "media": "podcast", "entity": "podcast", "limit": limit,
    })
    req = urllib.request.Request(
        f"https://itunes.apple.com/search?{params}",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    results = []
    for r in data.get("results", []):
        feed_url = r.get("feedUrl", "")
        if not feed_url:
            continue
        results.append({
            "name": r.get("collectionName", "未知"),
            "author": r.get("artistName", ""),
            "feed_url": feed_url,
        })
    return results


# ---------------------------------------------------------------------------
# RSS feed 解析
# ---------------------------------------------------------------------------

def list_rss_episodes(feed_url: str) -> tuple[str, list[dict]]:
    """解析 RSS feed，回傳 (podcast 名稱, 單集列表)。
    每個單集是 {title, url, pub_date, duration, description}。
    需要 feedparser 套件：pip install feedparser"""
    try:
        import feedparser
    except ImportError:
        raise ImportError("請先安裝 feedparser：pip install feedparser")

    feed = feedparser.parse(feed_url)
    if feed.bozo and not feed.entries:
        raise ValueError(f"無法解析 RSS feed：{feed_url}")

    podcast_title = feed.feed.get("title", "未知 Podcast")
    episodes = []
    for entry in feed.entries:
        # 找音訊 enclosure（RSS Podcast 標準欄位）
        audio_url = ""
        for enc in getattr(entry, "enclosures", []):
            if enc.get("type", "").startswith("audio"):
                audio_url = enc.get("href", "") or enc.get("url", "")
                break
        if not audio_url:
            # 有些 feed 把音訊放在 link 而非 enclosure
            audio_url = entry.get("link", "")

        pub_date = ""
        if hasattr(entry, "published"):
            pub_date = entry.published[:10] if len(entry.published) >= 10 else entry.published

        episodes.append({
            "title": entry.get("title", "未知標題"),
            "url": audio_url,
            "pub_date": pub_date,
            "description": re.sub(r"<[^>]+>", "", entry.get("summary", "")),
        })
    return podcast_title, episodes


# ---------------------------------------------------------------------------
# yt-dlp 平台解析（SoundCloud、Spotify 等）
# ---------------------------------------------------------------------------

def list_ydlp_playlist(url: str) -> tuple[str, list[dict]]:
    """用 yt-dlp 展開播放清單/頻道，回傳 (名稱, 單集列表)。
    適用 SoundCloud 用戶頁、YouTube Podcast 頻道、等 yt-dlp 支援的來源。"""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if "entries" not in info:
        # 單集直連，不是清單
        return info.get("title", ""), [{
            "title": info.get("title", url),
            "url": url,
            "pub_date": info.get("upload_date", ""),
            "description": info.get("description", ""),
        }]

    name = info.get("title") or info.get("channel") or url
    episodes = []
    for e in (info.get("entries") or []):
        if not e:
            continue
        ep_url = e.get("url") or e.get("webpage_url") or ""
        if not ep_url.startswith("http"):
            ep_url = f"https://www.youtube.com/watch?v={e['id']}" if e.get("id") else ep_url
        episodes.append({
            "title": e.get("title", ep_url),
            "url": ep_url,
            "pub_date": e.get("upload_date", ""),
            "description": e.get("description", ""),
        })
    return name, episodes


def resolve_episodes(query: str) -> tuple[str, list[dict]]:
    """統一入口：
    - 非 URL → iTunes 名稱搜尋，若找到唯一結果直接載入；多個結果拋出 PodcastCandidates
    - RSS feed URL → list_rss_episodes
    - 其他 URL → list_ydlp_playlist
    """
    query = query.strip()
    if not is_url(query):
        candidates = search_itunes_podcasts(query)
        if not candidates:
            raise ValueError(f"找不到符合「{query}」的 Podcast，請改用 RSS feed 網址直接輸入")
        if len(candidates) == 1:
            return list_rss_episodes(candidates[0]["feed_url"])
        raise PodcastCandidates(candidates)
    if is_rss_feed(query):
        return list_rss_episodes(query)
    return list_ydlp_playlist(query)


class PodcastCandidates(Exception):
    """搜尋到多個 Podcast 候選時拋出，攜帶候選清單供上層選擇。"""
    def __init__(self, candidates: list[dict]):
        self.candidates = candidates
        super().__init__(f"找到 {len(candidates)} 個 Podcast，請選擇其中一個")


# ---------------------------------------------------------------------------
# 音訊下載 + Whisper 辨識
# ---------------------------------------------------------------------------

_whisper_model_cache: dict = {}


def _get_whisper_model(model_size: str = "small"):
    if model_size not in _whisper_model_cache:
        from faster_whisper import WhisperModel
        _whisper_model_cache[model_size] = WhisperModel(model_size, device="cpu", compute_type="int8")
    return _whisper_model_cache[model_size]


def _load_and_chunk_audio(audio_path: str, chunk_minutes: int = 10) -> list[tuple]:
    """用 faster-whisper 內建的音訊解碼器（不需 ffmpeg 在 PATH）把音訊載入成 float32 numpy
    array，再切成 chunk_minutes 分鐘的段落。
    回傳 [(chunk_array_or_path, offset_seconds)]。"""
    from faster_whisper.audio import decode_audio
    import numpy as np
    SAMPLE_RATE = 16000
    audio = decode_audio(audio_path)          # float32 @ 16 kHz，全長約 192 MB / 50 min
    chunk_samples = chunk_minutes * 60 * SAMPLE_RATE
    chunks = []
    for i, start in enumerate(range(0, len(audio), chunk_samples)):
        chunks.append((audio[start: start + chunk_samples], i * chunk_minutes * 60.0))
    return chunks if chunks else [(audio_path, 0.0)]


def _transcribe_chunk(model, chunk_path: str, offset: float, is_first: bool,
                      log=print, control: "JobControl | None" = None) -> tuple[list[dict], bool]:
    """辨識單一音訊段落，時間戳加上 offset，回傳 (segments, stopped)。
    chunk_length=30 讓 faster-whisper 內部以 30 秒為單位處理，降低記憶體峰值。"""
    seg_iter, info = model.transcribe(
        chunk_path, beam_size=5, vad_filter=True,
        initial_prompt="以下是繁體中文的句子",
        chunk_length=30,
    )
    if is_first:
        log(f"辨識語言：{info.language}（信心度 {info.language_probability:.0%}）")
    result = []
    for seg in seg_iter:
        if control:
            control.wait_if_paused()
            if control.is_stopped():
                log(f"使用者已停止，已辨識到 [{format_timestamp(seg.start + offset)}]。")
                return result, True
        text = seg.text.strip()
        abs_start = seg.start + offset
        log(f"[{format_timestamp(abs_start)} -> {format_timestamp(seg.end + offset)}] {text}")
        result.append({"text": text, "start": abs_start, "duration": seg.end - seg.start})
    return result, False


def download_and_transcribe(url: str, title_hint: str = "", model_size: str = "small",
                             chunk_minutes: int = 10,
                             log=print, control: "JobControl | None" = None) -> tuple[str, list[dict]]:
    """用 yt-dlp 下載任意音訊 URL，切成 chunk_minutes 分鐘段落後逐段用 faster-whisper 辨識，
    回傳 (標題, segments)。分段處理可避免長 Podcast 的記憶體不足問題。"""
    log("下載音訊中...")
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tempfile.gettempdir(), "podcast_audio_%(id)s.%(ext)s"),
        "overwrites": True,
    }
    audio_path = None
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            downloads = info.get("requested_downloads") or []
            audio_path = downloads[0]["filepath"] if downloads else ydl.prepare_filename(info)
            title = info.get("title") or title_hint or info.get("id") or url

        log(f"載入並切割音訊（每段 {chunk_minutes} 分鐘）...")
        chunks = _load_and_chunk_audio(audio_path, chunk_minutes)
        log(f"共 {len(chunks)} 段，載入 Whisper 模型中...")
        model = _get_whisper_model(model_size)

        result = []
        for idx, (chunk, offset) in enumerate(chunks, 1):
            log(f"辨識第 {idx}/{len(chunks)} 段（起始 {format_timestamp(offset)}）...")
            segs, stopped = _transcribe_chunk(
                model, chunk, offset, is_first=(idx == 1),
                log=log, control=control,
            )
            result.extend(segs)
            if stopped:
                break

        return title, result
    finally:
        if audio_path and os.path.exists(audio_path):
            os.remove(audio_path)


# ---------------------------------------------------------------------------
# 摘要 + Markdown 輸出（與 yt_notes_assistant 邏輯相同）
# ---------------------------------------------------------------------------

SENTENCE_END_RE = re.compile(r"[。！？!?]\s*$")


def build_chunks(segments: list[dict], max_len: int = 80) -> list[dict]:
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
    top_in_order = sorted(top, key=lambda x: x[0])
    key_chunks = [t[1] for t in top_in_order]

    keywords = freq.most_common(15)
    return key_chunks, keywords


def build_notes_markdown(url: str, title: str, podcast_name: str,
                          key_chunks: list[dict], keywords: list[tuple[str, float]],
                          transcript_chunks: list[dict] | None = None,
                          pub_date: str = "", description: str = "") -> str:
    lines = [
        f"# 學習筆記：{title}",
        "",
        f"- Podcast：{podcast_name}" if podcast_name else "",
        f"- 來源連結：{url}",
        f"- 發布日期：{pub_date}" if pub_date else "",
        f"- 逐字稿來源：Whisper 語音辨識（自動生成，可能有誤差）",
        "",
    ]
    if description:
        lines += [f"> {description[:200]}{'...' if len(description) > 200 else ''}", ""]

    lines += ["## 重點摘要", ""]
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

    return "\n".join(line for line in lines if line is not None)


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip()[:80]


def write_text_with_retry(path: str, content: str, retries: int = 3, delay: float = 1.0):
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


def process_episode(episode: dict, podcast_name: str, output_dir: str = ".",
                    model_size: str = "small", log=print,
                    control: "JobControl | None" = None) -> str:
    """下載單集音訊、辨識、整理重點，輸出 Markdown 筆記檔，回傳檔案路徑。"""
    title, segments = download_and_transcribe(
        episode["url"], title_hint=episode.get("title", ""),
        model_size=model_size, log=log, control=control,
    )
    display_title = episode.get("title") or title

    key_chunks, keywords = summarize(segments)
    transcript_chunks = build_chunks(segments)
    markdown = build_notes_markdown(
        url=episode["url"],
        title=display_title,
        podcast_name=podcast_name,
        key_chunks=key_chunks,
        keywords=keywords,
        transcript_chunks=transcript_chunks,
        pub_date=episode.get("pub_date", ""),
        description=episode.get("description", ""),
    )

    filename = f"{sanitize_filename(display_title)}_筆記.md"
    output_path = os.path.join(output_dir, filename)
    write_text_with_retry(output_path, markdown)
    return output_path


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def _pick_episodes_and_process(podcast_name: str, episodes: list[dict],
                                output_dir: str, model_size: str):
    """列出單集清單，讓使用者選擇後處理。"""
    if not episodes:
        print("找不到任何單集，請確認網址是否正確。")
        return

    print(f"\nPodcast：{podcast_name}，共找到 {len(episodes)} 集：")
    for i, ep in enumerate(episodes, 1):
        date_tag = f" ({ep['pub_date']})" if ep.get("pub_date") else ""
        print(f"  {i:>3}. {ep['title']}{date_tag}")

    choice = input("\n請輸入要整理的單集編號（多個用逗號分隔，例如 1,3；輸入 all 全部處理）：").strip()
    if choice.lower() == "all":
        indices = list(range(1, len(episodes) + 1))
    else:
        indices = [int(x) for x in re.split(r"[,，\s]+", choice) if x.strip().isdigit()]

    for i in indices:
        ep = episodes[i - 1]
        print(f"\n處理第 {i} 集：{ep['title']}")
        try:
            path = process_episode(ep, podcast_name, output_dir=output_dir, model_size=model_size)
            print(f"已儲存：{path}")
        except Exception as e:
            print(f"處理失敗：{e}")


def run(query: str, output_dir: str = ".", model_size: str = "small"):
    query = query.strip()

    # 單集直連音訊（.mp3/.m4a/.ogg 等），直接辨識
    if is_url(query) and re.search(r"\.(mp3|m4a|ogg|opus|wav|aac|flac)(\?|$)", query, re.I):
        print(f"直連音訊檔，直接辨識：{query}")
        episode = {"title": "", "url": query, "pub_date": "", "description": ""}
        path = process_episode(episode, podcast_name="", output_dir=output_dir, model_size=model_size)
        print(f"學習筆記已儲存：{path}")
        return

    # RSS feed 或平台 URL 或名稱搜尋
    print(f"解析 Podcast：{query}")
    try:
        podcast_name, episodes = resolve_episodes(query)
        _pick_episodes_and_process(podcast_name, episodes, output_dir, model_size)
    except PodcastCandidates as pc:
        print(f"\n搜尋到 {len(pc.candidates)} 個 Podcast，請選擇：")
        for i, c in enumerate(pc.candidates, 1):
            print(f"  {i:>2}. {c['name']}  （{c['author']}）")
        choice = input("請輸入編號：").strip()
        if not choice.isdigit() or not (1 <= int(choice) <= len(pc.candidates)):
            print("無效的編號，已取消。")
            return
        chosen = pc.candidates[int(choice) - 1]
        print(f"\n已選擇：{chosen['name']}")
        podcast_name, episodes = list_rss_episodes(chosen["feed_url"])
        _pick_episodes_and_process(podcast_name, episodes, output_dir, model_size)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
    else:
        query = input(
            "請輸入 Podcast RSS feed 網址、單集音訊直連、或 SoundCloud/YouTube Podcast 頁面網址：\n> "
        ).strip()

    run(query)
