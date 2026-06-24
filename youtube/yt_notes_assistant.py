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

pip install jieba youtube-transcript-api yt_dlp faster-whisper
"""

import os
import re
import sys
import threading
import time
from collections import Counter

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_TMP_DIR = os.path.join(_SCRIPT_DIR, "tmp")
os.makedirs(_TMP_DIR, exist_ok=True)

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import jieba
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
from yt_dlp import YoutubeDL

PREFERRED_LANGS = ["zh-Hant", "zh-TW", "zh-Hans", "zh", "en"]

# YouTube 對雲端機房（Render 之類）的 IP 常常會比一般家用網路慢、或需要重試更多次，
# 沒有限制的話某些頻道/網路狀況會卡到好幾分鐰看起來像沒回應。所有「只是查 metadata」
# 的 yt-dlp 呼叫都套用這個逾時/重試上限，讓它頂多卡這麼久就會失敗回報，而不是卡死。
#
# 曾經試過在這裡加 extractor_args 把 player_client 換成 "tv"，想繞開
# "Sign in to confirm you're not a bot"，但本機實測「tv」用戶端會讓大量
# 正常影片被誤判成「This video is DRM protected」，導致每一筆都被
# ignoreerrors 悄悄丟掉、查詢結果整批變成 0——比原本被擋的狀況更糟，
# 已經revert。不要再用 "tv" client，要嘗試其他 player_client 前，先用
# 上面的失敗紀錄當教訓，本機驗證過沒有造成新的「entries 全部消失」才能上線。
YDL_NETWORK_OPTS = {"socket_timeout": 15, "retries": 3, "extractor_retries": 1}


_SECRET_COOKIE_PATH = os.environ.get("YT_COOKIES_FILE", "/etc/secrets/cookies.txt")

# yt-dlp 需要看到這些 youtube.com 的登入態 cookie 才會把請求當成「已登入的
# 真人瀏覽器」；如果匯出時其實沒有登入、或擴充套件只存了非關鍵 cookie，
# 檔案格式可能完全正常（行數、tab 分隔都對），但裡面根本沒有任何一個這種
# 關鍵 cookie，一樣會被當成匿名流量擋下。
_KEY_AUTH_COOKIE_NAMES = {
    "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO",
}


def _resolve_cookiefile() -> str | None:
    """雲端機房 IP 常被 YouTube 判定為機器人而擋下metadata 請求（"Sign in to
    confirm you're not a bot" / 429）；帶上登入過的 YouTube 帳號 cookies 可以
    大幅降低被擋的機率。優先找 Render「Secret Files」掛載出來的真實檔案路徑
    （多行檔案內容用 Secret File 比塞進環境變數穩，不必擔心換行被吃掉）；
    找不到才退而讀 YT_COOKIES 環境變數（本機開發或其他平台用，內容就是
    cookies.txt 整份文字，容許用 "\\n" 表示換行）。兩者都沒有就不帶 cookies，
    沿用原本「匿名遊客」的請求方式。"""
    if os.path.isfile(_SECRET_COOKIE_PATH):
        return _SECRET_COOKIE_PATH

    raw = os.environ.get("YT_COOKIES")
    if not raw:
        return None
    path = os.path.join(_TMP_DIR, "cookies.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(raw.replace("\\n", "\n"))
    return path


_COOKIEFILE = _resolve_cookiefile()
if _COOKIEFILE:
    YDL_NETWORK_OPTS["cookiefile"] = _COOKIEFILE


def _cookie_status_note() -> str:
    """讓「查詢失敗」的錯誤訊息直接帶出 cookies 是否生效，不必另外看 Render log
    就能判斷卡在哪一層：沒設定 / 走了哪個來源（Secret File 還是環境變數）/
    檔案格式有沒有壞掉 / 裡面到底有沒有真正能代表「已登入」的關鍵 cookie。
    光看「已套用」看不出這些，行數、格式正常也可能完全沒有登入態 cookie。"""
    used_secret_file = bool(_COOKIEFILE) and _COOKIEFILE == _SECRET_COOKIE_PATH
    source_note = (
        f"找過 Secret File（{_SECRET_COOKIE_PATH}）："
        f"{'存在，使用這份' if used_secret_file else '不存在，退回 YT_COOKIES 環境變數'}"
    )
    if not _COOKIEFILE:
        return f"cookies：未設定（{source_note}；YT_COOKIES 環境變數也沒有）"
    try:
        with open(_COOKIEFILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        cookie_rows = [
            line.rstrip("\n").split("\t") for line in lines
            if line.strip() and not line.startswith("#") and len(line.rstrip("\n").split("\t")) == 7
        ]
        youtube_rows = [row for row in cookie_rows if "youtube.com" in row[0] or "google.com" in row[0]]
        found_auth_names = sorted({row[5] for row in cookie_rows if row[5] in _KEY_AUTH_COOKIE_NAMES})
        return (
            f"cookies：已套用 {_COOKIEFILE}（{source_note}；"
            f"檔案共 {len(lines)} 行，合法格式 {len(cookie_rows)} 行，"
            f"屬於 youtube/google 網域 {len(youtube_rows)} 行，"
            f"關鍵登入 cookie：{'、'.join(found_auth_names) if found_auth_names else '一個都沒找到'}）"
        )
    except Exception as e:
        return f"cookies：已設定但讀取失敗 - {e}（{source_note}）"

# YouTube 暫時擋下雲端機房 IP 時典型的錯誤訊息關鍵字（機器人驗證／限流），
# 用來跟「會員專屬內容」「私人影片」之類正常、預期內、跟新影片無關的單支
# 影片抓取失敗區分開——後者不該觸發查詢失敗，否則每次查詢都會被會員制
# 頻道的雜訊嚇得誤報失敗。
_BLOCKED_ERROR_MARKERS = (
    "sign in to confirm",
    "confirm you're not a bot",
    "429",
    "too many requests",
    "http error 403",
)


class _SuspiciousErrorLogger:
    """傳給 YoutubeDL 的 logger，只記錄看起來像「伺服器被擋」的錯誤訊息
    （ignoreerrors=True 時，這些錯誤原本會被悄悄吞掉、完全沒有痕跡）。"""

    def __init__(self):
        self.suspicious: list[str] = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        low = str(msg).lower()
        if any(marker in low for marker in _BLOCKED_ERROR_MARKERS):
            self.suspicious.append(str(msg))

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
    ydl_opts = {"quiet": True, "skip_download": True, "no_warnings": True, "extract_flat": True, **YDL_NETWORK_OPTS}

    # 若使用者貼的是單支影片網址，從中取出頻道 URL
    if is_url(query) and extract_video_id(query) is not None:
        with YoutubeDL({"quiet": True, "skip_download": True, "no_warnings": True, **YDL_NETWORK_OPTS}) as ydl:
            info = ydl.extract_info(query, download=False)
        channel_url = info.get("channel_url") or info.get("uploader_url")
        channel_name = info.get("channel") or info.get("uploader") or query
        if not channel_url:
            raise ValueError(f"無法從影片網址取得頻道資訊：{query}")
        base = channel_url.rstrip("/")
        return base, channel_name

    if is_channel_url(query):
        base = query.split("?")[0].rstrip("/")
        for suffix in CHANNEL_TAB_SUFFIXES:
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        # 這裡只是要確認頻道存在、拿頻道名稱，不需要列出全部影片；
        # 大型頻道（例如新聞台）可能有上萬支影片，沒限制的話 yt-dlp 會試著整本爬完，
        # 導致這支指令卡很久甚至像沒回應。
        with YoutubeDL({**ydl_opts, "playlistend": 1}) as ydl:
            info = ydl.extract_info(base + "/videos", download=False)
        return base, info.get("channel") or info.get("title") or query

    # 看起來是網址、但不是影片網址也不是頻道網址（例如搜尋結果頁
    # youtube.com/results?search_query=...、播放清單網址等）：
    # 若放行讓下面的 ytsearch5 把整段網址字串當關鍵字模糊搜尋，常常會
    # 配對到完全不相關的頻道，使用者會在不知情的狀況下監控錯頻道。
    if is_url(query):
        raise ValueError(f"不支援的 YouTube 網址格式：{query}\n請改用頻道網址（如 youtube.com/@頻道名）或直接輸入頻道名稱文字。")

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
                         content_types: tuple[str, ...] = ("videos", "streams")) -> tuple[str, list[dict]]:
    """回傳 (頻道名稱, 影片列表)，每個影片是 {id, title, url, type}。
    content_types 可包含 "videos"（一般影片）、"shorts"、"streams"（直播）。
    max_videos=None 表示每個分類都列出全部。"""
    channel_base_url, channel_name = resolve_channel(query)

    videos = []
    seen_ids = set()
    for ctype in content_types:
        ydl_opts = {"quiet": True, "skip_download": True, "no_warnings": True, "extract_flat": True, **YDL_NETWORK_OPTS}
        if max_videos is not None:
            ydl_opts["playlistend"] = max_videos
        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"{channel_base_url}/{ctype}", download=False)
        except Exception:
            # "videos" 分類每個頻道都有，抓取失敗代表查詢本身出問題
            # （例如雲端機房 IP 被 YouTube 擋下），不能當成「沒有這個分類」吞掉，
            # 否則使用者會誤以為頻道沒有新影片。其他分類（shorts/streams）
            # 頻道可能真的沒有，才繼續往下處理其他分類。
            if ctype == "videos":
                raise
            continue

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


def list_channel_videos_since(query: str, since_date: str,
                              content_types: tuple[str, ...] = ("videos", "streams")) -> tuple[str, list[dict]]:
    """回傳 since_date（含）之後發布的影片，每個影片附帶 upload_date 欄位。
    since_date 格式：YYYY-MM-DD。
    注意：yt-dlp dateafter 在 extract_info 模式下不過濾 entries，
    因此改用 extract_flat=False 取得每部影片的 upload_date，再手動比對。"""
    channel_base_url, channel_name = resolve_channel(query)

    # 比對用的無破折號格式（YYYYMMDD），與 yt-dlp upload_date 欄位格式一致
    since_compact = since_date.replace("-", "")

    videos = []
    seen_ids = set()
    for ctype in content_types:
        error_logger = _SuspiciousErrorLogger()
        ydl_opts = {
            "quiet": True,
            "skip_download": True,
            "no_warnings": True,
            "extract_flat": False,  # 需要完整 metadata 才有 upload_date
            "ignoreerrors": True,   # Premiere/直播等特殊影片抓取失敗時繼續處理
            "playlistend": 50,
            "logger": error_logger,
            **YDL_NETWORK_OPTS,
        }
        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"{channel_base_url}/{ctype}", download=False)
        except Exception:
            # 同 list_channel_videos()：videos 分類抓取失敗要往外丟，
            # 否則查詢失敗會被誤判成「沒有新影片」。
            if ctype == "videos":
                raise
            continue

        if info is None:
            # ignoreerrors=True 時，整個分類抓取失敗有時不會丟例外，
            # 而是讓 extract_info 直接回傳 None（例如頻道沒有這個分類，
            # 或抓取過程整批失敗）。沒檢查的話 info.get(...) 會直接炸掉
            # 'NoneType' object has no attribute 'get'。處理方式跟上面
            # except 區塊一致：videos 分類視為查詢失敗，其他分類當作不存在。
            if ctype == "videos":
                raise RuntimeError(
                    f"抓取「{channel_name}」影片清單時沒有取得任何資料"
                    f"（可能是 YouTube 暫時擋下伺服器 IP），請稍後再查詢一次。[{_cookie_status_note()}]"
                )
            continue

        entries = info.get("entries") or []
        if ctype == "videos" and not entries:
            # 能監控到這個頻道，代表加入監控時 resolve_channel 已經確認過
            # 至少有 1 支影片；extract_flat=False 搭配 ignoreerrors=True 時，
            # 每支影片的完整 metadata 抓取若失敗（例如雲端機房 IP 被 YouTube
            # 暫時擋下）會被悄悄跳過、不留下任何痕跡，導致整批結果變成空清單。
            # 這種情況不能當成「真的沒有新影片」，要往外丟成查詢失敗。
            raise RuntimeError(
                f"抓取「{channel_name}」影片清單時沒有任何一支影片成功取得資料"
                f"（可能是 YouTube 暫時擋下伺服器 IP），請稍後再查詢一次。[{_cookie_status_note()}]"
            )

        videos_before = len(videos)
        for entry in entries:
            if not entry or not entry.get("id") or entry["id"] in seen_ids:
                continue
            seen_ids.add(entry["id"])
            raw_date = entry.get("upload_date") or ""  # 格式 YYYYMMDD
            # 手動過濾：只保留 since_date 當天及之後
            if raw_date < since_compact:
                continue
            upload_date = (f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
                           if len(raw_date) == 8 else "")
            videos.append({
                "id": entry["id"],
                "title": entry.get("title", entry["id"]),
                "url": f"https://www.youtube.com/watch?v={entry['id']}",
                "type": CONTENT_TYPE_LABELS.get(ctype, ctype),
                "upload_date": upload_date,
            })

        if len(videos) == videos_before and error_logger.suspicious:
            # 這個分類沒有任何影片通過 since_date 篩選，但同時有一支以上的影片
            # 抓取失敗，錯誤訊息看起來像伺服器被 YouTube 擋下（而不是會員專屬
            # /私人影片之類正常會失敗的情況）。無法排除失敗的那幾支裡有真正
            # 的新影片，不能放行回報「沒有新影片」。
            raise RuntimeError(
                f"抓取「{channel_name}」時有 {len(error_logger.suspicious)} 支影片資料抓取失敗"
                "（看起來是 YouTube 暫時擋下伺服器 IP），無法確定是否有新影片，請稍後再查詢一次。"
                f"[{_cookie_status_note()}；錯誤範例：{error_logger.suspicious[0][:200]}]"
            )
    return channel_name, videos


def get_video_upload_date(video_id: str) -> str | None:
    """回傳影片上架日期，格式 YYYY-MM-DD；查不到則回傳 None。"""
    ydl_opts = {"quiet": True, "skip_download": True, "no_warnings": True, **YDL_NETWORK_OPTS}
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

    ydl_opts = {"quiet": True, "skip_download": True, "no_warnings": True, **YDL_NETWORK_OPTS}
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
        "outtmpl": os.path.join(_TMP_DIR, "yt_notes_audio_%(id)s.%(ext)s"),
        "overwrites": True,
        "socket_timeout": 30,  # 下載本身可以久一點，但單次連線卡住還是要逾時，不要無限等
    }
    audio_path = None
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            downloads = info.get("requested_downloads") or []
            audio_path = downloads[0]["filepath"] if downloads else ydl.prepare_filename(info)
            title = info.get("title") or info.get("id") or url

        log("載入音訊並切割成小段以節省記憶體...")
        from faster_whisper.audio import decode_audio
        SAMPLE_RATE = 16000
        CHUNK_MINUTES = 10
        audio = decode_audio(audio_path)
        chunk_samples = CHUNK_MINUTES * 60 * SAMPLE_RATE
        chunks = [(audio[s: s + chunk_samples], i * CHUNK_MINUTES * 60.0)
                  for i, s in enumerate(range(0, len(audio), chunk_samples))] or [(audio, 0.0)]

        log(f"共 {len(chunks)} 段，載入 Whisper 模型中...")
        model = _get_whisper_model(model_size)

        result = []
        for idx, (chunk, offset) in enumerate(chunks, 1):
            log(f"辨識第 {idx}/{len(chunks)} 段（起始 {format_timestamp(offset)}）...")
            seg_iter, transcribe_info = model.transcribe(
                chunk, beam_size=5, vad_filter=True,
                initial_prompt="以下是繁體中文的句子",
                chunk_length=30,
            )
            if idx == 1:
                log(f"辨識語言：{transcribe_info.language}（信心度 {transcribe_info.language_probability:.0%}）")
            for seg in seg_iter:
                if control:
                    control.wait_if_paused()
                    if control.is_stopped():
                        abs_ts = format_timestamp(seg.start + offset)
                        log(f"使用者已停止，已辨識到 [{abs_ts}]，中斷後續辨識。")
                        return title, result
                text = seg.text.strip()
                abs_start = seg.start + offset
                log(f"[{format_timestamp(abs_start)} -> {format_timestamp(seg.end + offset)}] {text}")
                result.append({"text": text, "start": abs_start, "duration": seg.end - seg.start})
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
