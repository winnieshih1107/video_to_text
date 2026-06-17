"""
Podcast 學習筆記助手 - 桌面操作介面 (tkinter)
支援三種輸入：
1. RSS feed 網址 → 列出所有單集，勾選後產生筆記
2. 直連音訊網址（.mp3 / .m4a 等）→ 直接辨識產生筆記
3. SoundCloud / YouTube Podcast 等 yt-dlp 支援的頁面 → 列出單集
"""

import os
import queue
import re
import sys
import threading
import tkinter as tk
from tkinter import scrolledtext, messagebox, ttk

from podcast_notes_assistant import (
    is_url,
    is_rss_feed,
    resolve_episodes,
    list_rss_episodes,
    process_episode,
    sanitize_filename,
    JobControl,
    PodcastCandidates,
)


class App:
    def __init__(self, root):
        self.root = root
        root.title("Podcast 學習筆記助手")
        root.geometry("780x680")

        self.log_queue = queue.Queue()
        self.worker_running = False
        self.episode_checkboxes: list[tuple[tk.BooleanVar, dict]] = []
        self.current_podcast_name: str | None = None
        self.control = JobControl()

        # ── 頂部輸入區 ──────────────────────────────────────────────
        top = tk.Frame(root, padx=10, pady=10)
        top.pack(fill="x")

        tk.Label(top, text="Podcast 名稱、RSS feed 網址、單集音訊直連，或 SoundCloud／YouTube Podcast 頁面：").pack(anchor="w")

        input_row = tk.Frame(top)
        input_row.pack(fill="x", pady=(4, 0))

        self.query_entry = tk.Entry(input_row, font=("Microsoft JhengHei", 11))
        self.query_entry.pack(side="left", fill="x", expand=True)
        self.query_entry.bind("<Return>", lambda e: self.on_query())

        self.query_btn = tk.Button(input_row, text="查詢", command=self.on_query)
        self.query_btn.pack(side="left", padx=(8, 0))

        opt_row = tk.Frame(top)
        opt_row.pack(fill="x", pady=(6, 0))
        tk.Label(opt_row, text="Whisper 模型：").pack(side="left")
        self.model_var = tk.StringVar(value="small")
        model_combo = ttk.Combobox(
            opt_row, textvariable=self.model_var,
            values=["tiny", "base", "small", "medium", "large-v3"],
            state="readonly", width=10,
        )
        model_combo.pack(side="left", padx=(4, 0))
        tk.Label(opt_row, text="  （模型越大越準，但速度越慢）", fg="gray").pack(side="left")

        # ── 狀態列 ──────────────────────────────────────────────────
        status_row = tk.Frame(root, padx=10)
        status_row.pack(fill="x")
        self.status_label = tk.Label(
            status_row,
            text="輸入 Podcast 名稱可搜尋；輸入 RSS feed 可列出單集；輸入直連音訊網址可直接辨識",
            fg="gray",
        )
        self.status_label.pack(anchor="w")

        # ── 單集清單（勾選區）──────────────────────────────────────
        list_frame = tk.LabelFrame(root, text="Podcast 單集清單（勾選要整理的單集）", padx=8, pady=8)
        list_frame.pack(fill="both", expand=False, padx=10, pady=(8, 0))

        list_toolbar = tk.Frame(list_frame)
        list_toolbar.pack(fill="x")
        tk.Button(list_toolbar, text="全選", command=lambda: self.set_all_checks(True)).pack(side="left")
        tk.Button(list_toolbar, text="全不選", command=lambda: self.set_all_checks(False)).pack(side="left", padx=(6, 0))
        self.generate_btn = tk.Button(
            list_toolbar, text="產生勾選單集的筆記", command=self.on_generate_selected, state="disabled"
        )
        self.generate_btn.pack(side="left", padx=(12, 0))

        job_row = tk.Frame(list_frame)
        job_row.pack(fill="x", pady=(4, 0))
        self.pause_btn = tk.Button(job_row, text="暫停", command=self.on_pause_resume, state="disabled")
        self.pause_btn.pack(side="left")
        self.stop_btn = tk.Button(job_row, text="停止", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))

        canvas_frame = tk.Frame(list_frame)
        canvas_frame.pack(fill="both", expand=True)
        self.list_canvas = tk.Canvas(canvas_frame, height=180, highlightthickness=0)
        scrollbar = tk.Scrollbar(canvas_frame, orient="vertical", command=self.list_canvas.yview)
        self.checklist_container = tk.Frame(self.list_canvas)
        self.checklist_container.bind(
            "<Configure>",
            lambda e: self.list_canvas.configure(scrollregion=self.list_canvas.bbox("all")),
        )
        self.list_canvas.create_window((0, 0), window=self.checklist_container, anchor="nw")
        self.list_canvas.configure(yscrollcommand=scrollbar.set)
        self.list_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # ── 進度 / 筆記輸出區 ────────────────────────────────────
        mid = tk.Frame(root, padx=10, pady=6)
        mid.pack(fill="both", expand=True)
        tk.Label(mid, text="進度與筆記內容：").pack(anchor="w")
        self.output_text = scrolledtext.ScrolledText(mid, wrap="word", font=("Microsoft JhengHei", 10))
        self.output_text.pack(fill="both", expand=True)
        self.output_text.tag_configure("transcript", foreground="#1a7a3c",
                                       font=("Microsoft JhengHei", 10))
        self.output_text.tag_configure("dimmed", foreground="#888888",
                                       font=("Microsoft JhengHei", 9, "italic"))
        self._seg_re = re.compile(r"^\[(\d{1,2}:\d{2}(?::\d{2})?) -> \d{1,2}:\d{2}(?::\d{2})?\] (.+)$")

        # ── 底部按鈕 ─────────────────────────────────────────────
        bottom = tk.Frame(root, padx=10, pady=10)
        bottom.pack(fill="x")
        tk.Button(bottom, text="開啟輸出資料夾", command=self.open_output_folder).pack(side="left")

        self.root.after(150, self.poll_log_queue)

    # ── 共用工具 ───────────────────────────────────────────────────

    def log(self, msg: str):
        self.log_queue.put(msg)

    def poll_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                m = self._seg_re.match(msg)
                if m:
                    # 辨識段落：綠色顯示，狀態列同步更新時間進度
                    self.output_text.insert("end", msg + "\n", "transcript")
                    self.set_status(f"語音辨識中… 已到 {m.group(1)}", "blue")
                else:
                    tag = "dimmed" if msg.startswith("辨識語言") else ""
                    self.output_text.insert("end", msg + "\n", tag)
                self.output_text.see("end")
        except queue.Empty:
            pass
        self.root.after(150, self.poll_log_queue)

    def set_status(self, text: str, color: str = "gray"):
        self.status_label.config(text=text, fg=color)

    def set_busy(self, busy: bool, label: str = "查詢"):
        self.worker_running = busy
        self.query_btn.config(state="disabled" if busy else "normal",
                              text="處理中..." if busy else label)

    def set_job_controls_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self.pause_btn.config(state=state, text="暫停")
        self.stop_btn.config(state=state)

    def on_pause_resume(self):
        if self.control.is_paused():
            self.control.request_resume()
            self.pause_btn.config(text="暫停")
            self.set_status("已繼續...", "blue")
        else:
            self.control.request_pause()
            self.pause_btn.config(text="繼續")
            self.set_status("已暫停，按「繼續」恢復", "orange")

    def on_stop(self):
        self.control.request_stop()
        self.stop_btn.config(state="disabled")
        self.pause_btn.config(state="disabled")
        self.set_status("正在停止...", "orange")

    def clear_checklist(self):
        for child in self.checklist_container.winfo_children():
            child.destroy()
        self.episode_checkboxes = []
        self.generate_btn.config(state="disabled")

    def set_all_checks(self, value: bool):
        for var, _ in self.episode_checkboxes:
            var.set(value)

    def open_output_folder(self):
        path = os.path.abspath(".")
        if sys.platform == "win32":
            os.startfile(path)
        else:
            os.system(f'open "{path}"')

    # ── 查詢（RSS / 平台清單 / 直連音訊）─────────────────────────

    def on_query(self):
        if self.worker_running:
            return
        query = self.query_entry.get().strip()
        if not query:
            messagebox.showwarning("提示", "請先輸入 Podcast 網址")
            return

        self.output_text.delete("1.0", "end")
        self.clear_checklist()
        self.control.reset()
        self.set_busy(True)
        self.set_job_controls_enabled(True)
        self.set_status("查詢中，請稍候...", "blue")

        threading.Thread(target=self.query_worker, args=(query,), daemon=True).start()

    def query_worker(self, query: str):
        is_direct_audio = is_url(query) and bool(
            re.search(r"\.(mp3|m4a|ogg|opus|wav|aac|flac)(\?|$)", query, re.I)
        )
        try:
            if is_direct_audio:
                self.log(f"直連音訊，直接辨識：{query}")
                episode = {"title": "", "url": query, "pub_date": "", "description": ""}
                model_size = self.model_var.get()
                output_path = process_episode(
                    episode, podcast_name="",
                    output_dir=".", model_size=model_size,
                    log=self.log, control=self.control,
                )
                self.log(f"\n學習筆記已儲存：{os.path.abspath(output_path)}")
                self.root.after(0, lambda: self.on_query_done("已產生逐字稿筆記", [], None))
            else:
                self.log(f"解析 Podcast：{query}")
                try:
                    podcast_name, episodes = resolve_episodes(query)
                except PodcastCandidates as pc:
                    self.log(f"搜尋到 {len(pc.candidates)} 個 Podcast，請選擇")
                    self.root.after(0, lambda c=pc.candidates: self.show_podcast_picker(c))
                    return
                if not episodes:
                    raise RuntimeError("找不到任何單集，請確認網址是否正確。")
                self.log(f"找到 {len(episodes)} 集")
                self.root.after(0, lambda: self.on_query_done(
                    f"{podcast_name}，共 {len(episodes)} 集，請勾選要整理的單集",
                    episodes, podcast_name,
                ))
        except Exception as e:
            self.log(f"\n發生錯誤：{e}")
            self.root.after(0, lambda: self.on_query_failed(str(e)))

    def on_query_done(self, status_msg: str, episodes: list[dict], podcast_name: str | None):
        self.set_busy(False)
        self.set_job_controls_enabled(False)
        self.set_status(status_msg, "green")
        self.current_podcast_name = podcast_name
        if episodes:
            self.populate_checklist(episodes)

    def on_query_failed(self, msg: str):
        self.set_busy(False)
        self.set_job_controls_enabled(False)
        self.set_status("查詢失敗，請查看下方訊息", "red")
        messagebox.showerror("錯誤", msg)

    def show_podcast_picker(self, candidates: list[dict]):
        """跳出選台對話框，讓使用者從搜尋結果中選一個 Podcast。"""
        dialog = tk.Toplevel(self.root)
        dialog.title("選擇 Podcast")
        dialog.geometry("540x340")
        dialog.grab_set()

        tk.Label(dialog, text="搜尋到以下 Podcast，請選擇：",
                 font=("Microsoft JhengHei", 10)).pack(anchor="w", padx=10, pady=(10, 4))

        frame = tk.Frame(dialog)
        frame.pack(fill="both", expand=True, padx=10)
        sb = tk.Scrollbar(frame)
        sb.pack(side="right", fill="y")
        lb = tk.Listbox(frame, yscrollcommand=sb.set, font=("Microsoft JhengHei", 10),
                        selectmode="single", activestyle="dotbox")
        lb.pack(side="left", fill="both", expand=True)
        sb.config(command=lb.yview)

        for c in candidates:
            lb.insert("end", f"{c['name']}  （{c['author']}）")
        lb.selection_set(0)

        def on_confirm():
            sel = lb.curselection()
            if not sel:
                return
            chosen = candidates[sel[0]]
            dialog.destroy()
            self.log(f"已選擇：{chosen['name']}，載入單集清單中...")
            self.set_status("載入單集清單中...", "blue")

            def load_worker():
                try:
                    podcast_name, episodes = list_rss_episodes(chosen["feed_url"])
                    self.log(f"找到 {len(episodes)} 集")
                    self.root.after(0, lambda: self.on_query_done(
                        f"{podcast_name}，共 {len(episodes)} 集，請勾選要整理的單集",
                        episodes, podcast_name,
                    ))
                except Exception as e:
                    self.log(f"載入失敗：{e}")
                    self.root.after(0, lambda: self.on_query_failed(str(e)))

            threading.Thread(target=load_worker, daemon=True).start()

        btn_row = tk.Frame(dialog)
        btn_row.pack(pady=8)
        tk.Button(btn_row, text="確定", width=10, command=on_confirm).pack(side="left", padx=6)
        tk.Button(btn_row, text="取消", width=10,
                  command=lambda: (dialog.destroy(), self.on_query_done("已取消", [], None))).pack(side="left")

        lb.bind("<Double-Button-1>", lambda e: on_confirm())

    def populate_checklist(self, episodes: list[dict]):
        self.clear_checklist()
        for ep in episodes:
            var = tk.BooleanVar(value=False)
            date_tag = f" ({ep['pub_date']})" if ep.get("pub_date") else ""
            label = f"{ep['title']}{date_tag}"
            cb = tk.Checkbutton(
                self.checklist_container, text=label, variable=var,
                anchor="w", justify="left", wraplength=640,
                font=("Microsoft JhengHei", 10),
            )
            cb.pack(fill="x", anchor="w")
            self.episode_checkboxes.append((var, ep))
        self.generate_btn.config(state="normal")

    # ── 產生勾選單集的筆記 ─────────────────────────────────────────

    def on_generate_selected(self):
        if self.worker_running:
            return
        selected = [ep for var, ep in self.episode_checkboxes if var.get()]
        if not selected:
            messagebox.showwarning("提示", "請至少勾選一集")
            return

        self.worker_running = True
        self.control.reset()
        self.generate_btn.config(state="disabled", text="處理中...")
        self.set_job_controls_enabled(True)
        self.set_status(f"開始處理 {len(selected)} 集...", "blue")

        model_size = self.model_var.get()
        podcast_name = self.current_podcast_name or ""
        threading.Thread(
            target=self.generate_worker,
            args=(selected, podcast_name, model_size),
            daemon=True,
        ).start()

    def generate_worker(self, selected: list[dict], podcast_name: str, model_size: str):
        success, failed, stopped = 0, 0, False
        for ep in selected:
            self.control.wait_if_paused()
            if self.control.is_stopped():
                self.log(f"\n使用者已停止，已處理 {success + failed}/{len(selected)} 集")
                stopped = True
                break
            try:
                self.log(f"\n處理：{ep['title']}")
                output_path = process_episode(
                    ep, podcast_name,
                    output_dir=".", model_size=model_size,
                    log=self.log, control=self.control,
                )
                self.log(f"已儲存：{os.path.abspath(output_path)}")
                success += 1
            except Exception as e:
                self.log(f"失敗：{ep['title']} → {e}")
                failed += 1
        self.root.after(0, lambda: self.on_generate_done(success, failed, stopped))

    def on_generate_done(self, success: int, failed: int, stopped: bool = False):
        self.worker_running = False
        self.generate_btn.config(state="normal", text="產生勾選單集的筆記")
        self.set_job_controls_enabled(False)
        prefix = "已停止：" if stopped else "完成："
        color = "green" if (failed == 0 and not stopped) else "orange"
        self.set_status(f"{prefix}成功 {success} 集，失敗 {failed} 集", color)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
