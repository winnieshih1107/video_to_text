"""
YouTube 學習筆記助手 - 桌面操作介面 (tkinter)
支援兩種輸入：
1. 單部影片網址/名稱 -> 直接產生筆記
2. 頻道名稱/網址 -> 列出該頻道影片，勾選後再產生筆記
"""

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import scrolledtext, messagebox

from yt_notes_assistant import (
    is_url,
    is_channel_url,
    extract_video_id,
    resolve_video,
    list_channel_videos,
    process_video,
    process_external_url,
    export_videos_to_excel,
    sanitize_filename,
    JobControl,
    CONTENT_TYPE_LABELS,
)


class App:
    def __init__(self, root):
        self.root = root
        root.title("YouTube 學習筆記助手")
        root.geometry("760x640")

        self.log_queue = queue.Queue()
        self.worker_running = False
        self.video_checkboxes: list[tuple[tk.BooleanVar, dict]] = []
        self.current_channel_name: str | None = None
        self.control = JobControl()

        top = tk.Frame(root, padx=10, pady=10)
        top.pack(fill="x")

        tk.Label(top, text="YouTube 頻道名稱/網址、單部影片網址/名稱，或其他網址（如 Google Drive 影音連結）：").pack(anchor="w")

        input_row = tk.Frame(top)
        input_row.pack(fill="x", pady=(4, 0))

        self.query_entry = tk.Entry(input_row, font=("Microsoft JhengHei", 11))
        self.query_entry.pack(side="left", fill="x", expand=True)
        self.query_entry.bind("<Return>", lambda e: self.on_query())

        self.query_btn = tk.Button(input_row, text="查詢", command=self.on_query)
        self.query_btn.pack(side="left", padx=(8, 0))

        limit_row = tk.Frame(top)
        limit_row.pack(fill="x", pady=(4, 0))
        tk.Label(limit_row, text="頻道最多列出幾部影片（留空＝全部）：").pack(side="left")
        self.limit_entry = tk.Entry(limit_row, width=8)
        self.limit_entry.pack(side="left", padx=(4, 0))

        tk.Label(limit_row, text="    抓取範圍：").pack(side="left")
        self.type_videos_var = tk.BooleanVar(value=True)
        self.type_shorts_var = tk.BooleanVar(value=False)
        self.type_streams_var = tk.BooleanVar(value=False)
        tk.Checkbutton(limit_row, text="影片", variable=self.type_videos_var).pack(side="left")
        tk.Checkbutton(limit_row, text="Shorts", variable=self.type_shorts_var).pack(side="left")
        tk.Checkbutton(limit_row, text="直播", variable=self.type_streams_var).pack(side="left")

        status_row = tk.Frame(root, padx=10)
        status_row.pack(fill="x")
        self.status_label = tk.Label(status_row, text="輸入頻道名稱可列出影片清單；輸入單部影片網址可直接產生筆記", fg="gray")
        self.status_label.pack(anchor="w")

        # 影片清單（勾選區）
        list_frame = tk.LabelFrame(root, text="頻道影片清單（勾選要整理的影片）", padx=8, pady=8)
        list_frame.pack(fill="both", expand=False, padx=10, pady=(8, 0))

        list_toolbar = tk.Frame(list_frame)
        list_toolbar.pack(fill="x")
        tk.Button(list_toolbar, text="全選", command=lambda: self.set_all_checks(True)).pack(side="left")
        tk.Button(list_toolbar, text="全不選", command=lambda: self.set_all_checks(False)).pack(side="left", padx=(6, 0))
        self.generate_selected_btn = tk.Button(
            list_toolbar, text="產生勾選影片的筆記", command=self.on_generate_selected, state="disabled"
        )
        self.generate_selected_btn.pack(side="left", padx=(12, 0))

        self.export_excel_btn = tk.Button(
            list_toolbar, text="匯出清單至 Excel", command=self.on_export_excel, state="disabled"
        )
        self.export_excel_btn.pack(side="left", padx=(12, 0))

        self.fetch_date_var = tk.BooleanVar(value=True)
        tk.Checkbutton(list_toolbar, text="含日期（較慢）", variable=self.fetch_date_var).pack(side="left", padx=(6, 0))

        job_control_row = tk.Frame(list_frame)
        job_control_row.pack(fill="x", pady=(4, 0))
        self.pause_btn = tk.Button(job_control_row, text="暫停", command=self.on_pause_resume, state="disabled")
        self.pause_btn.pack(side="left")
        self.stop_btn = tk.Button(job_control_row, text="停止", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))

        canvas_frame = tk.Frame(list_frame)
        canvas_frame.pack(fill="both", expand=True)
        self.list_canvas = tk.Canvas(canvas_frame, height=180, highlightthickness=0)
        scrollbar = tk.Scrollbar(canvas_frame, orient="vertical", command=self.list_canvas.yview)
        self.checklist_container = tk.Frame(self.list_canvas)
        self.checklist_container.bind(
            "<Configure>", lambda e: self.list_canvas.configure(scrollregion=self.list_canvas.bbox("all"))
        )
        self.list_canvas.create_window((0, 0), window=self.checklist_container, anchor="nw")
        self.list_canvas.configure(yscrollcommand=scrollbar.set)
        self.list_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        mid = tk.Frame(root, padx=10, pady=6)
        mid.pack(fill="both", expand=True)
        tk.Label(mid, text="進度與筆記內容：").pack(anchor="w")
        self.output_text = scrolledtext.ScrolledText(mid, wrap="word", font=("Microsoft JhengHei", 10))
        self.output_text.pack(fill="both", expand=True)

        bottom = tk.Frame(root, padx=10, pady=10)
        bottom.pack(fill="x")
        self.open_folder_btn = tk.Button(bottom, text="開啟輸出資料夾", command=self.open_output_folder)
        self.open_folder_btn.pack(side="left")

        self.root.after(150, self.poll_log_queue)

    # ---------- 共用工具 ----------

    def log(self, msg: str):
        self.log_queue.put(msg)

    def poll_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.output_text.insert("end", msg + "\n")
                self.output_text.see("end")
        except queue.Empty:
            pass
        self.root.after(150, self.poll_log_queue)

    def set_status(self, text: str, color: str = "gray"):
        self.status_label.config(text=text, fg=color)

    def set_busy(self, busy: bool, label: str = "查詢"):
        self.worker_running = busy
        state = "disabled" if busy else "normal"
        self.query_btn.config(state=state, text="處理中..." if busy else label)

    def set_job_controls_enabled(self, enabled: bool):
        self.pause_btn.config(state="normal" if enabled else "disabled", text="暫停")
        self.stop_btn.config(state="normal" if enabled else "disabled")

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
        self.video_checkboxes = []
        self.generate_selected_btn.config(state="disabled")
        self.export_excel_btn.config(state="disabled")

    def set_all_checks(self, value: bool):
        for var, _ in self.video_checkboxes:
            var.set(value)

    def open_output_folder(self):
        path = os.path.abspath(".")
        if sys.platform == "win32":
            os.startfile(path)
        else:
            os.system(f'open "{path}"')

    # ---------- 查詢（單部影片 or 頻道列表） ----------

    def on_query(self):
        if self.worker_running:
            return
        query = self.query_entry.get().strip()
        if not query:
            messagebox.showwarning("提示", "請先輸入頻道名稱/網址，或影片網址/名稱")
            return

        self.output_text.delete("1.0", "end")
        self.clear_checklist()
        self.set_busy(True)
        self.control.reset()
        self.set_job_controls_enabled(True)
        self.set_status("查詢中，請稍候...", "blue")

        limit_text = self.limit_entry.get().strip()
        try:
            max_videos = int(limit_text) if limit_text else None
        except ValueError:
            max_videos = None

        content_types = []
        if self.type_videos_var.get():
            content_types.append("videos")
        if self.type_shorts_var.get():
            content_types.append("shorts")
        if self.type_streams_var.get():
            content_types.append("streams")
        if not content_types:
            content_types = ["videos"]

        threading.Thread(target=self.query_worker, args=(query, max_videos, tuple(content_types)), daemon=True).start()

    def query_worker(self, query: str, max_videos: int | None, content_types: tuple):
        is_single_video = is_url(query) and extract_video_id(query) is not None
        is_external_url = is_url(query) and not is_single_video and not is_channel_url(query)
        try:
            if is_single_video:
                self.log(f"解析影片：{query}")
                video_id, title = resolve_video(query)
                self.log(f"找到影片：{title} ({video_id})")
                self.log("擷取字幕並整理筆記中...")
                output_path = process_video(video_id, title, log=self.log, control=self.control)
                self.log(f"\n學習筆記已儲存：{os.path.abspath(output_path)}")
                self.root.after(0, lambda: self.on_query_done("已產生單部影片筆記", []))
            elif is_external_url:
                self.log(f"非 YouTube 連結，改用 Whisper 直接轉逐字稿：{query}")
                output_path = process_external_url(query, log=self.log, control=self.control)
                self.log(f"\n學習筆記已儲存：{os.path.abspath(output_path)}")
                self.root.after(0, lambda: self.on_query_done("已產生逐字稿筆記", []))
            else:
                self.log(f"查詢頻道：{query}")
                try:
                    channel_name, videos = list_channel_videos(query, max_videos=max_videos, content_types=content_types)
                except ValueError:
                    # resolve_channel 真的找不到任何符合的頻道時，才當作單部影片名稱再試一次
                    self.log(f"找不到頻道，改用影片名稱搜尋：{query}")
                    video_id, title = resolve_video(query)
                    self.log(f"找到影片：{title} ({video_id})")
                    self.log("擷取字幕並整理筆記中...")
                    output_path = process_video(video_id, title, log=self.log, control=self.control)
                    self.log(f"\n學習筆記已儲存：{os.path.abspath(output_path)}")
                    self.root.after(0, lambda: self.on_query_done("已產生單部影片筆記", []))
                    return

                if not videos:
                    type_labels = "、".join(CONTENT_TYPE_LABELS.get(t, t) for t in content_types)
                    raise RuntimeError(
                        f"頻道「{channel_name}」存在，但您勾選的分類（{type_labels}）沒有找到任何影片，"
                        f"請試著勾選「影片」分類再查詢一次。"
                    )
                self.log(f"頻道：{channel_name}，共找到 {len(videos)} 部影片")
                self.root.after(0, lambda: self.on_query_done(
                    f"頻道：{channel_name}，請勾選要整理的影片", videos, channel_name))
        except Exception as e:
            self.log(f"\n發生錯誤：{e}")
            self.root.after(0, lambda: self.on_query_failed(str(e)))

    def on_query_done(self, status_msg: str, videos: list[dict], channel_name: str | None = None):
        self.set_busy(False)
        self.set_job_controls_enabled(False)
        self.set_status(status_msg, "green")
        self.current_channel_name = channel_name
        if videos:
            self.populate_checklist(videos)

    def on_query_failed(self, msg: str):
        self.set_busy(False)
        self.set_job_controls_enabled(False)
        self.set_status("查詢失敗，請查看下方訊息", "red")
        messagebox.showerror("錯誤", msg)

    def populate_checklist(self, videos: list[dict]):
        self.clear_checklist()
        for v in videos:
            var = tk.BooleanVar(value=False)
            label = f"[{v.get('type', '影片')}] {v['title']}"
            cb = tk.Checkbutton(
                self.checklist_container, text=label, variable=var,
                anchor="w", justify="left", wraplength=620, font=("Microsoft JhengHei", 10)
            )
            cb.pack(fill="x", anchor="w")
            self.video_checkboxes.append((var, v))
        self.generate_selected_btn.config(state="normal")
        self.export_excel_btn.config(state="normal")

    # ---------- 產生勾選影片的筆記 ----------

    def on_generate_selected(self):
        if self.worker_running:
            return
        selected = [v for var, v in self.video_checkboxes if var.get()]
        if not selected:
            messagebox.showwarning("提示", "請至少勾選一部影片")
            return

        self.worker_running = True
        self.control.reset()
        self.generate_selected_btn.config(state="disabled", text="處理中...")
        self.set_job_controls_enabled(True)
        self.set_status(f"開始處理 {len(selected)} 部影片...", "blue")

        threading.Thread(target=self.generate_worker, args=(selected,), daemon=True).start()

    def generate_worker(self, selected: list[dict]):
        success, failed, stopped = 0, 0, False
        for v in selected:
            self.control.wait_if_paused()
            if self.control.is_stopped():
                self.log(f"\n使用者已停止，已處理 {success + failed}/{len(selected)} 部")
                stopped = True
                break
            try:
                self.log(f"\n處理：{v['title']}")
                output_path = process_video(v["id"], v["title"], log=self.log, control=self.control)
                self.log(f"已儲存：{os.path.abspath(output_path)}")
                success += 1
            except Exception as e:
                self.log(f"失敗：{v['title']} -> {e}")
                failed += 1
        self.root.after(0, lambda: self.on_generate_done(success, failed, stopped))

    def on_generate_done(self, success: int, failed: int, stopped: bool = False):
        self.worker_running = False
        self.generate_selected_btn.config(state="normal", text="產生勾選影片的筆記")
        self.set_job_controls_enabled(False)
        prefix = "已停止：" if stopped else "完成："
        self.set_status(f"{prefix}成功 {success} 部，失敗 {failed} 部", "green" if (failed == 0 and not stopped) else "orange")

    # ---------- 匯出清單至 Excel ----------

    def on_export_excel(self):
        if self.worker_running:
            return
        videos = [v for var, v in self.video_checkboxes if var.get()]
        if not videos:
            messagebox.showwarning("提示", "請至少勾選一部影片")
            return

        fetch_dates = self.fetch_date_var.get()
        self.worker_running = True
        self.control.reset()
        self.export_excel_btn.config(state="disabled", text="匯出中...")
        self.set_job_controls_enabled(True)
        self.set_status(f"開始匯出 {len(videos)} 部影片清單至 Excel...", "blue")

        threading.Thread(target=self.export_excel_worker, args=(videos, fetch_dates), daemon=True).start()

    def export_excel_worker(self, videos: list[dict], fetch_dates: bool):
        try:
            name_part = sanitize_filename(self.current_channel_name or "頻道")
            filename = f"{name_part}_影片清單.xlsx"
            output_path = os.path.abspath(filename)
            self.log(f"\n匯出至：{output_path}")
            export_videos_to_excel(videos, output_path, log=self.log, fetch_dates=fetch_dates, control=self.control)
            self.log("匯出完成！" if not self.control.is_stopped() else "已停止匯出。")
            self.root.after(0, lambda: self.on_export_done(True, output_path))
        except Exception as e:
            self.log(f"匯出失敗：{e}")
            self.root.after(0, lambda: self.on_export_done(False, str(e)))

    def on_export_done(self, success: bool, info: str):
        self.worker_running = False
        self.export_excel_btn.config(state="normal", text="匯出清單至 Excel")
        self.set_job_controls_enabled(False)
        if success:
            self.set_status(f"已匯出 Excel：{info}", "green")
        else:
            self.set_status("匯出失敗，請查看下方訊息", "red")
            messagebox.showerror("錯誤", info)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
