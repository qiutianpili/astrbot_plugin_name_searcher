"""NameSearcher 本地识别工具 · 图形界面。

双击“本地识别GUI.bat”（或 pythonw name_searcher_gui.py）打开。
选文件夹 → 开始识别 → 完成后得到识别包 zip → 到 AstrBot 插件页“导入识别包”。
识别逻辑与命令行工具 name_searcher_local.py 完全相同。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = Path(__file__).resolve().parent
SETTINGS = HERE / "gui_settings.json"
REQUIREMENTS = HERE / "requirements-local.txt"
STATUS_NAMES = {
    "extracted": "已识别",
    "no_text": "无文字",
    "stored_only": "仅保存",
    "failed": "识别失败",
    "unavailable": "读取失败",
    "pending": "待识别",
    "ok": "已保存",
}


def _load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_settings(data: dict) -> None:
    try:
        SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _open_path(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class App:
    def __init__(self, root: tk.Tk, initial_folder: str = "") -> None:
        self.root = root
        self.tool = None  # name_searcher_local 模块，后台加载
        self.worker: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.last_bundle: Path | None = None
        self.closing = False
        settings = _load_settings()

        root.title("NameSearcher 本地识别")
        root.geometry("760x600")
        root.minsize(620, 480)
        try:
            ttk.Style().theme_use("vista" if sys.platform.startswith("win") else "clam")
        except tk.TclError:
            pass

        self.folder = tk.StringVar(value=initial_folder or settings.get("folder", ""))
        self.work = tk.StringVar(value=settings.get("work", str(HERE / "ns_local_work")))
        self.lang = tk.StringVar(value=settings.get("lang", "chi_sim+eng"))
        self.max_mb = tk.StringVar(value=str(settings.get("max_mb", 200)))
        self.archives = tk.BooleanVar(value=settings.get("archives", True))
        self.status_text = tk.StringVar(value="正在加载识别引擎…")
        self.count_text = tk.StringVar(value="")

        pad = {"padx": 10, "pady": 4}
        box = ttk.LabelFrame(root, text="要识别的文件夹")
        box.pack(fill="x", **pad)
        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=6)
        ttk.Entry(row, textvariable=self.folder).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="选择…", command=self.pick_folder).pack(side="left", padx=(6, 0))

        opts = ttk.LabelFrame(root, text="选项")
        opts.pack(fill="x", **pad)
        grid = ttk.Frame(opts)
        grid.pack(fill="x", padx=8, pady=6)
        grid.columnconfigure(1, weight=1)
        ttk.Label(grid, text="工作目录").grid(row=0, column=0, sticky="w")
        ttk.Entry(grid, textvariable=self.work).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(grid, text="选择…", command=self.pick_work).grid(row=0, column=2)
        ttk.Button(grid, text="打开", command=self.open_work).grid(row=0, column=3, padx=(6, 0))
        line = ttk.Frame(grid)
        line.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Label(line, text="单文件上限 (MB)").pack(side="left")
        ttk.Spinbox(line, from_=1, to=4096, width=6, textvariable=self.max_mb).pack(side="left", padx=(4, 16))
        ttk.Label(line, text="Tesseract 语言").pack(side="left")
        ttk.Entry(line, textvariable=self.lang, width=14).pack(side="left", padx=(4, 16))
        ttk.Checkbutton(line, text="展开压缩包", variable=self.archives).pack(side="left")

        actions = ttk.Frame(root)
        actions.pack(fill="x", **pad)
        self.btn_run = ttk.Button(actions, text="▶ 识别并打包", command=lambda: self.start("run"))
        self.btn_scan = ttk.Button(actions, text="只识别", command=lambda: self.start("scan"))
        self.btn_pack = ttk.Button(actions, text="打包已识别结果", command=lambda: self.start("pack"))
        self.btn_status = ttk.Button(actions, text="查看状态", command=lambda: self.start("status"))
        self.btn_stop = ttk.Button(actions, text="■ 停止", command=self.stop, state="disabled")
        self.btn_bundle = ttk.Button(actions, text="打开识别包位置", command=self.open_bundle, state="disabled")
        for button in (self.btn_run, self.btn_scan, self.btn_pack, self.btn_status, self.btn_stop):
            button.pack(side="left", padx=(0, 6))
        self.btn_bundle.pack(side="right")
        self.busy_buttons = (self.btn_run, self.btn_scan, self.btn_pack, self.btn_status)

        prog = ttk.Frame(root)
        prog.pack(fill="x", **pad)
        self.bar = ttk.Progressbar(prog, mode="determinate")
        self.bar.pack(fill="x")
        info = ttk.Frame(prog)
        info.pack(fill="x", pady=(4, 0))
        ttk.Label(info, textvariable=self.status_text).pack(side="left")
        ttk.Label(info, textvariable=self.count_text).pack(side="right")

        logbox = ttk.LabelFrame(root, text="日志")
        logbox.pack(fill="both", expand=True, **pad)
        self.log_widget = tk.Text(logbox, height=12, wrap="word", state="disabled", font=("Consolas", 10))
        scroll = ttk.Scrollbar(logbox, command=self.log_widget.yview)
        self.log_widget.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_widget.pack(fill="both", expand=True, padx=(6, 0), pady=6)
        self.log_widget.tag_configure("err", foreground="#c0392b")
        self.log_widget.tag_configure("ok", foreground="#1e8449")

        hint = "完成后到 AstrBot 的 NameSearcher 插件页点“导入识别包”，选生成的 zip。可随时停止，下次会跳过已完成的文件。"
        ttk.Label(root, text=hint, foreground="#666", wraplength=720).pack(fill="x", padx=12, pady=(0, 8))

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.set_busy(True, allow_stop=False)
        threading.Thread(target=self.load_engine, daemon=True).start()
        root.after(100, self.poll)

    # ---------- 引擎加载 ----------
    def load_engine(self) -> None:
        try:
            sys.path.insert(0, str(HERE))
            import name_searcher_local as tool  # noqa: PLC0415

            self.events.put(("engine", tool))
        except ModuleNotFoundError as exc:
            if exc.name in {"main", "name_searcher_local"}:
                self.events.put(("engine_error", f"找不到 {exc.name}.py：{exc}"))
            else:
                self.events.put(("engine_missing", str(exc)))
        except BaseException:  # noqa: BLE001
            self.events.put(("engine_error", traceback.format_exc()))

    def install_requirements(self) -> None:
        self.log(f"正在安装依赖：{REQUIREMENTS.name}（需要联网，可能要几分钟）")
        self.status_text.set("正在安装依赖…")
        self.set_busy(True, allow_stop=False)

        def job() -> None:
            cmd = [sys.executable.replace("pythonw.exe", "python.exe"), "-m", "pip", "install", "-r", str(REQUIREMENTS)]
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", creationflags=flags,
                )
                for line in proc.stdout or []:
                    line = line.rstrip()
                    if line:
                        self.events.put(("log", line, ""))
                code = proc.wait()
            except Exception as exc:  # noqa: BLE001
                self.events.put(("log", f"安装失败：{exc}", "err"))
                code = 1
            self.events.put(("installed", code))

        threading.Thread(target=job, daemon=True).start()

    # ---------- 界面辅助 ----------
    def log(self, text: str, tag: str = "") -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", text + "\n", tag or ())
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def set_busy(self, busy: bool, allow_stop: bool = True) -> None:
        for button in self.busy_buttons:
            button.configure(state="disabled" if busy or self.tool is None else "normal")
        self.btn_stop.configure(state="normal" if busy and allow_stop else "disabled")

    def pick_folder(self) -> None:
        path = filedialog.askdirectory(title="选择要识别的文件夹", initialdir=self.folder.get() or str(Path.home()))
        if path:
            self.folder.set(path)

    def pick_work(self) -> None:
        path = filedialog.askdirectory(title="选择工作目录", initialdir=self.work.get() or str(HERE))
        if path:
            self.work.set(path)

    def open_work(self) -> None:
        work = Path(self.work.get()).expanduser()
        work.mkdir(parents=True, exist_ok=True)
        _open_path(work)

    def open_bundle(self) -> None:
        if not self.last_bundle:
            return
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", str(self.last_bundle)])
        else:
            _open_path(self.last_bundle.parent)

    def remember(self) -> None:
        _save_settings({
            "folder": self.folder.get(),
            "work": self.work.get(),
            "lang": self.lang.get(),
            "max_mb": self.max_mb.get(),
            "archives": bool(self.archives.get()),
        })

    # ---------- 任务 ----------
    def build_args(self) -> argparse.Namespace | None:
        try:
            max_mb = float(self.max_mb.get())
            if max_mb <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("参数错误", "单文件上限需要是正数")
            return None
        return argparse.Namespace(lang=self.lang.get().strip() or "chi_sim+eng", max_mb=max_mb, no_archives=not self.archives.get())

    def start(self, command: str) -> None:
        if self.tool is None or (self.worker and self.worker.is_alive()):
            return
        args = self.build_args()
        if args is None:
            return
        work = Path(self.work.get()).expanduser().resolve()
        folder = None
        if command in {"run", "scan"}:
            folder = Path(self.folder.get()).expanduser()
            if not self.folder.get().strip() or not folder.is_dir():
                messagebox.showwarning("没有文件夹", "请先选择要识别的文件夹")
                return
            folder = folder.resolve()
        self.remember()
        work.mkdir(parents=True, exist_ok=True)
        self.stop_flag.clear()
        self.bar.configure(value=0, maximum=1)
        self.count_text.set("")
        self.tally = {}
        self.status_text.set({"run": "正在识别…", "scan": "正在识别…", "pack": "正在打包…", "status": "正在读取…"}[command])
        self.set_busy(True, allow_stop=command in {"run", "scan"})
        self.worker = threading.Thread(target=self.job, args=(command, folder, work, args), daemon=True)
        self.worker.start()

    def job(self, command: str, folder: Path | None, work: Path, args: argparse.Namespace) -> None:
        tool = self.tool
        put = self.events.put
        log = lambda text: put(("log", text, "err" if "✗" in text else ""))  # noqa: E731
        try:
            if command == "status":
                put(("status", tool._status_counts(work), str(work)))
                return
            if command in {"run", "scan"}:
                code = asyncio.run(tool._scan(
                    folder, work, args, log=log,
                    progress=lambda i, n, s: put(("progress", i, n, s)),
                    should_stop=self.stop_flag.is_set,
                ))
                if code == 130:
                    put(("done", "已停止", None))
                    return
                if code or command == "scan":
                    put(("done", "识别完成" if not code else "没有可识别的文件", None))
                    return
            label = folder.name if folder else ""
            bundle = tool._pack(work, None, label, log=log)
            put(("done", "完成，识别包已生成", bundle))
        except SystemExit as exc:
            put(("log", str(exc), "err"))
            put(("done", "未完成", None))
        except BaseException:  # noqa: BLE001
            put(("log", traceback.format_exc(), "err"))
            put(("done", "出错了，详见日志", None))

    def stop(self) -> None:
        self.stop_flag.set()
        self.btn_stop.configure(state="disabled")
        self.status_text.set("正在停止（等当前文件处理完）…")

    def on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("正在识别", "还在识别中。停止并退出吗？\n（会等当前文件处理完，已完成的不会丢）"):
                return
            self.closing = True
            self.stop()
            return
        self.remember()
        self.root.destroy()

    # ---------- 事件循环 ----------
    def poll(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                self.handle(event)
        except queue.Empty:
            pass
        if self.closing and not (self.worker and self.worker.is_alive()):
            self.remember()
            self.root.destroy()
            return
        self.root.after(100, self.poll)

    def handle(self, event: tuple) -> None:
        kind = event[0]
        if kind == "engine":
            self.tool = event[1]
            self.status_text.set("就绪")
            self.set_busy(False)
            self.log("识别引擎已加载。选择文件夹后点“识别并打包”。", "ok")
        elif kind == "engine_missing":
            self.status_text.set("缺少依赖")
            self.log(f"缺少依赖：{event[1]}", "err")
            if REQUIREMENTS.exists() and messagebox.askyesno("缺少依赖", f"{event[1]}\n\n现在自动安装 {REQUIREMENTS.name} 吗？"):
                self.install_requirements()
        elif kind == "engine_error":
            self.status_text.set("加载失败")
            self.log(event[1], "err")
            self.log("请确认本工具和插件的 main.py 放在一起（同一文件夹或上一级）。", "err")
        elif kind == "installed":
            if event[1] == 0:
                self.log("依赖安装完成，正在重新加载…", "ok")
                for name in [m for m in sys.modules if m in {"main", "name_searcher_local"}]:
                    del sys.modules[name]
                threading.Thread(target=self.load_engine, daemon=True).start()
            else:
                self.status_text.set("依赖安装失败")
                self.log("依赖安装失败，可在终端运行：pip install -r requirements-local.txt", "err")
        elif kind == "log":
            self.log(event[1], event[2])
        elif kind == "progress":
            index, total, status = event[1], event[2], event[3]
            self.bar.configure(maximum=max(1, total), value=index)
            self.tally[status] = self.tally.get(status, 0) + 1
            parts = [f"{STATUS_NAMES.get(k, k)} {v}" for k, v in self.tally.items()]
            self.count_text.set(f"{index}/{total} · " + "  ".join(parts))
            if not self.stop_flag.is_set():
                self.status_text.set("正在识别…")
        elif kind == "status":
            counts, work = event[1], event[2]
            total = sum(counts.values())
            parts = "，".join(f"{STATUS_NAMES.get(k, k)} {v}" for k, v in counts.items()) or "无"
            self.log(f"工作目录 {work}：共 {total} 个文件（{parts}）")
            self.status_text.set("就绪")
            self.set_busy(False)
        elif kind == "done":
            message, bundle = event[1], event[2]
            self.status_text.set(message)
            self.set_busy(False)
            if bundle:
                self.last_bundle = Path(bundle)
                self.btn_bundle.configure(state="normal")
                self.bar.configure(value=self.bar["maximum"])
                self.log(f"识别包：{bundle}", "ok")
                if not self.closing and messagebox.askyesno("完成", f"识别包已生成：\n{bundle}\n\n打开所在文件夹吗？"):
                    self.open_bundle()


def main() -> None:
    root = tk.Tk()
    initial = sys.argv[1] if len(sys.argv) > 1 and Path(sys.argv[1]).is_dir() else ""
    App(root, initial)
    root.mainloop()


if __name__ == "__main__":
    main()
