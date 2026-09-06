r"""Graphical launcher: click once to start a meeting.

Owns everything the command line used to require -- it starts the transcription
server if it is not already up, remembers the settings you used last time, hides
itself while the subtitle overlay is on screen, and comes back afterwards with a
link to the transcript.

    .venv/bin/python -m meeting_subtitles.launcher
"""

import logging
import pathlib
import socket
import subprocess
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from meeting_subtitles import domain as domains
from meeting_subtitles import gnome, paths
from meeting_subtitles.envfix import normalize_proxy_env
from meeting_subtitles.rounded import RoundedWindow, undecorate
from meeting_subtitles.settings import CONFIG_PATH, Settings
from meeting_subtitles.tkfix import child_environment, ensure_cjk_tk

logger = logging.getLogger(__name__)

SERVER_LOG = paths.server_log()

HEALTH_URL = "http://127.0.0.1:8000/health"
SERVER_PORT = 8000


def stop_engine(port: int = SERVER_PORT) -> bool:
    """Ask whatever is listening on the port to exit, freeing the GPU.

    The engine deliberately outlives the launcher that started it, so the next
    meeting starts instantly -- which means we usually do not hold its handle
    any more. Find it by the port it occupies instead, which also works after
    the launcher has been closed and reopened.
    """
    try:
        import psutil
    except ImportError:
        return _stop_engine_by_name()
    stopped = False
    for connection in psutil.net_connections(kind="tcp"):
        if (connection.status != "LISTEN" or not connection.laddr
                or connection.laddr.port != port or not connection.pid):
            continue
        try:
            process = psutil.Process(connection.pid)
            process.terminate()
            try:
                process.wait(timeout=8)
            except psutil.TimeoutExpired:
                process.kill()
            stopped = True
        except psutil.Error as exc:
            logger.warning("无法结束引擎进程 %s: %s", connection.pid, exc)
    return stopped


def _stop_engine_by_name() -> bool:
    """Last resort when psutil is missing: match the engine's command line.

    Less precise than finding the process that holds the port, and it only
    matches an engine started the old way, through ``wlk`` directly.
    """
    try:
        subprocess.run(["pkill", "-f", "wlk --host 127.0.0.1"], check=False)
        return True
    except OSError:
        return False


def server_is_up(timeout: float = 1.5) -> bool:
    """Health-check the local server, bypassing any configured HTTP proxy.

    The user's environment routes everything through a SOCKS proxy; without an
    empty ProxyHandler urllib would try to reach 127.0.0.1 through it.
    """
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(HEALTH_URL, timeout=timeout) as response:
            return response.status == 200
    except (URLError, OSError, ValueError):
        return False


def port_in_use(port: int = SERVER_PORT) -> bool:
    """True when something already listens on the port (server still loading)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


#: Markers the engine prints, latest stage first. Loading is dominated by a
#: long stretch where nothing is logged at all, so these only bracket the
#: silence -- :func:`disk_read_bytes` is what shows movement inside it.
_STAGES = (
    ("Application startup complete", "引擎即将就绪"),
    ("Loading weights", "正在加载模型权重"),
    ("Waiting for application startup", "正在加载模型"),
    ("Started server process", "正在启动服务"),
    ("WhisperLiveKit", "正在初始化 CUDA"),
)


def engine_stage() -> str:
    """A human name for whatever the engine is currently doing."""
    try:
        text = SERVER_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "正在启动引擎"
    for marker, label in _STAGES:
        if marker in text:
            return label
    return "正在启动引擎"


def disk_read_bytes(pid: int) -> int | None:
    """Bytes this process has actually read from disk, or None.

    The models are about 6 GB of weights and the load is bound by reading
    them, so this is the one number that visibly moves during the minute where
    the log says nothing. It counts real disk reads, not page-cache hits, so
    on a warm start it stays near zero -- which is fine, because a warm start
    is over in seconds.
    """
    try:
        for line in pathlib.Path(f"/proc/{pid}/io").read_text().splitlines():
            if line.startswith("read_bytes:"):
                return int(line.split(":", 1)[1])
    except (OSError, ValueError):
        return None
    return None


def human_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return ""


def last_error_line(limit: int = 400) -> str:
    """Final exception line from the engine log, for the failure hint.

    A bare "startup failed" leaves the user with nothing to act on; the actual
    Python exception almost always names the problem outright.
    """
    try:
        lines = SERVER_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        text = line.strip()
        # Traceback frames start with "File"/"  " noise; the exception itself is
        # the last line that looks like "SomeError: message".
        if text and not text.startswith(("File ", "ERROR:", "INFO:", "^", "~", "|")):
            head = text.split(":", 1)[0]
            if head.endswith(("Error", "Exception")) or "Error" in head:
                return text[:limit]
    return ""



class LauncherApp:
    """The launcher window and the processes it supervises."""

    def __init__(self, *, preview: bool = False, theme: str | None = None) -> None:
        # Screenshots exercise the real widgets without launching the engine.
        self._preview_mode = preview
        self._preview_window = None
        self._preview_canvas = None
        self._preview_rounded = None
        self.settings = Settings()
        self.theme = theme if theme in ("light", "dark") else self.settings["theme"]
        self.settings["theme"] = self.theme
        self.colors = gnome.get_palette(self.theme)
        self.server_process: subprocess.Popen | None = None
        self.meeting_process: subprocess.Popen | None = None
        self.session_dir: Path | None = None
        self.state = "checking"   # checking | offline | starting | ready | running
        self._server_wait_started = 0.0
        # Set when the user presses 关闭引擎, so the poll loop does not helpfully
        # restart the engine they just asked to shut down.
        self._user_stopped = False
        self._drag_origin = (0, 0)
        self._window_origin = (0, 0)

        # className sets the window's WM_CLASS, which is how the desktop
        # associates a running window with its .desktop entry -- for the icon
        # in the dash, and for grouping. Tk's default is the bare "Tk", which
        # every other Tk program on the system also claims.
        self.root = tk.Tk(className="meeting-subtitles")
        self.root.title("会议字幕")
        self.root.resizable(False, False)
        self.font_family = gnome.pick_font(self.root)
        # Fonts are specified in points and grow with screen DPI; canvas
        # geometry is in pixels and does not. Scale it by hand so a 4K panel
        # does not leave button labels hanging outside their backgrounds.
        self.k = gnome.screen_scale(self.root)

        self._build_ui()
        self._center()
        # GNOME's own apps round all four corners; mutter only rounds the top
        # of a server-side-decorated window like ours, so shape the frame.
        undecorate(self.root)
        self.root.update_idletasks()
        self._rounded = RoundedWindow(self.root, radius=int(14 * self.k))
        self._round_job = self.root.after(150, self._round_frame)
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)
        self._poll_job = None
        if not self._preview_mode:
            self._poll_job = self.root.after(200, self._poll)

    # ---------------------------------------------------------------- layout

    def _f(self, size: int, weight: str = "normal") -> tuple:
        return (self.font_family, size, weight)

    def _px(self, value: float) -> int:
        return int(value * self.k)

    def _build_ui(self) -> None:
        """A compact meeting form; display samples appear where captions run."""
        G, C = gnome, self.colors
        self.root.configure(bg=C.WINDOW)
        self.root.option_add("*selectBackground", C.ACCENT)
        self.root.option_add("*selectForeground", C.ACCENT_TEXT)

        header = tk.Frame(self.root, bg=C.HEADERBAR, height=self._px(48))
        header.pack(fill="x")
        header.pack_propagate(False)
        title = tk.Label(header, text="会议字幕", font=self._f(13, "bold"),
                         fg=C.TEXT, bg=C.HEADERBAR)
        title.pack(side="left", padx=self._px(24))
        G.Button(header, "", self._on_window_close, icon="close", flat=True,
                 width=32, height=32, scale=self.k, palette=C, parent_bg=C.HEADERBAR).pack(
                     side="right", padx=(self._px(4), self._px(12)))
        G.Button(header, "", self.root.iconify, icon="minimize", flat=True,
                 width=32, height=32, scale=self.k, palette=C, parent_bg=C.HEADERBAR).pack(side="right")
        self.theme_button = G.Button(
            header, "切换深色" if self.theme == "light" else "切换浅色",
            self._toggle_theme, flat=True, width=88, height=30,
            scale=self.k, palette=C, parent_bg=C.HEADERBAR, font=self._f(9))
        self.theme_button.pack(side="right", padx=(0, self._px(12)))
        for widget in (header, title):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
        tk.Frame(self.root, bg=C.SEPARATOR, height=1).pack(fill="x")

        icon_path = Path(__file__).parent / "assets" / "meeting-subtitles-256.png"
        try:
            self._app_icon = tk.PhotoImage(file=str(icon_path))
            self.root.iconphoto(True, self._app_icon)
        except tk.TclError:
            pass

        width = 552
        body = tk.Frame(self.root, bg=C.WINDOW, width=self._px(width))
        body.pack(fill="both", expand=True, padx=self._px(24),
                  pady=(self._px(20), self._px(16)))
        name = tk.Frame(body, bg=C.WINDOW, height=self._px(42))
        name.pack(fill="x")
        name.pack_propagate(False)
        tk.Label(name, text="会议名称", font=self._f(11), fg=C.TEXT,
                 bg=C.WINDOW).pack(side="left")
        self.title_entry = G.Entry(
            name, placeholder="可留空，按日期保存", width=438,
            scale=self.k, palette=C, font=self._f(11), parent_bg=C.WINDOW)
        self.title_entry.pack(side="right")
        self.title_entry.set(self.settings["title"])

        options = tk.Frame(body, bg=C.WINDOW)
        options.pack(fill="x", pady=(self._px(18), self._px(14)))

        def switch_row(label: str, subtitle: str, value: bool) -> G.Switch:
            if options.winfo_children():
                tk.Frame(options, bg=C.SEPARATOR, height=1).pack(fill="x")
            row = tk.Frame(options, bg=C.WINDOW, height=self._px(62))
            row.pack(fill="x")
            row.pack_propagate(False)
            switch = G.Switch(row, value=value, scale=self.k, palette=C, parent_bg=C.WINDOW)
            switch.pack(side="right")
            labels = tk.Frame(row, bg=C.WINDOW)
            labels.pack(side="left", fill="x", expand=True)
            tk.Label(labels, text=label, font=self._f(11), fg=C.TEXT,
                     bg=C.WINDOW, anchor="w").pack(anchor="w")
            tk.Label(labels, text=subtitle, font=self._f(9), fg=C.TEXT_DIM,
                     bg=C.WINDOW, anchor="w").pack(anchor="w", pady=(self._px(2), 0))
            return switch

        self.mic_toggle = switch_row("录制麦克风", "关闭后只转录电脑播放的声音",
                                     bool(self.settings["record_mic"]))
        self.refine_toggle = switch_row("整句润色", "句子说完后，优化中文译文",
                                        bool(self.settings["refine"]))
        self.domain_toggle = switch_row(
            "计算机与 AI 术语", f"内置 {len(domains.get('cs-ai').terms)} 个领域词",
            self.settings["domain"] == "cs-ai")

        appearance_heading = tk.Frame(body, bg=C.WINDOW)
        appearance_heading.pack(fill="x", pady=(self._px(2), self._px(4)))
        tk.Label(appearance_heading, text="字幕显示", font=self._f(11, "bold"),
                 fg=C.TEXT, bg=C.WINDOW).pack(side="left")
        self.preview_button = G.Button(
            appearance_heading, "预览字幕", self._show_preview, flat=True,
            width=90, height=32, scale=self.k, palette=C, parent_bg=C.WINDOW, font=self._f(10))
        self.preview_button.pack(side="right")

        for label, attribute, minimum, maximum, key, suffix in (
            ("字号", "font_slider", 14, 34, "font_size", ""),
            ("不透明度", "opacity_slider", 40, 100, "opacity", "%"),
        ):
            row = tk.Frame(body, bg=C.WINDOW, height=self._px(42))
            row.pack(fill="x")
            row.pack_propagate(False)
            tk.Label(row, text=label, font=self._f(11), fg=C.TEXT,
                     bg=C.WINDOW).pack(side="left")
            slider = G.Slider(
                row, minimum=minimum, maximum=maximum, value=int(self.settings[key]),
                width=330, scale=self.k, palette=C, font=self._f(10), parent_bg=C.WINDOW,
                suffix=suffix, on_change=lambda _value: self._update_preview())
            slider.pack(side="right")
            setattr(self, attribute, slider)

        tk.Frame(body, bg=C.SEPARATOR, height=1).pack(
            fill="x", pady=(self._px(16), self._px(10)))
        engine = tk.Frame(body, bg=C.WINDOW, height=self._px(34))
        engine.pack(fill="x")
        engine.pack_propagate(False)
        self._status_dot = tk.Canvas(engine, width=self._px(7), height=self._px(7),
                                     bg=C.WINDOW, highlightthickness=0, bd=0)
        self._dot = self._status_dot.create_oval(
            0, 0, self._px(6), self._px(6), fill=C.WARNING, outline="")
        self._status_dot.pack(side="left", padx=(0, self._px(8)))
        self.engine_button = G.Button(
            engine, "关闭引擎", self._toggle_engine, flat=True, width=86, height=30,
            scale=self.k, palette=C, parent_bg=C.WINDOW, font=self._f(9))
        self.engine_button.pack(side="right")
        self.status_label = tk.Label(
            engine, text="正在检查转录引擎", anchor="w", justify="left",
            font=self._f(10), fg=C.TEXT_DIM, bg=C.WINDOW)
        self.status_label.pack(side="left", fill="x", expand=True)

        self.progress = G.IndeterminateBar(body, width=width, scale=self.k, palette=C,
                                           parent_bg=C.WINDOW)
        self.progress.pack(fill="x", pady=(self._px(3), self._px(8)))
        actions = tk.Frame(body, bg=C.WINDOW)
        actions.pack(fill="x")
        self.folder_button = G.Button(
            actions, "打开会议记录", self._open_folder, flat=True,
            width=122, height=40, scale=self.k, palette=C, parent_bg=C.WINDOW, font=self._f(10))
        self.folder_button.pack(side="left")
        self.start_button = G.Button(
            actions, "开始会议", self._on_start, width=132, height=40,
            scale=self.k, palette=C, accent=True, parent_bg=C.WINDOW, font=self._f(11, "bold"))
        self.start_button.pack(side="right")
        self.start_button.set_enabled(False)

        self.hint = tk.Label(body, text="", font=self._f(9), fg=C.TEXT_DIM,
                             bg=C.WINDOW, wraplength=self._px(width),
                             justify="left", height=2, anchor="nw")
        self.hint.pack(fill="x", pady=(self._px(8), 0))

    def _toggle_theme(self) -> None:
        self._set_theme("dark" if self.theme == "light" else "light")

    def _set_theme(self, theme: str) -> None:
        if theme not in ("light", "dark") or theme == self.theme:
            return
        self._remember_options()
        status = self.status_label.cget("text")
        dot = self._status_dot.itemcget(self._dot, "fill")
        status_role = next((role for role in ("SUCCESS", "WARNING", "ERROR", "TEXT_MUTED")
                            if getattr(self.colors, role) == dot), "TEXT_MUTED")
        hint = self.hint.cget("text")
        engine_text = self.engine_button.itemcget(self.engine_button._label, "text")
        start_enabled = self.start_button._enabled
        loading = self.progress._job is not None
        preview_open = self._preview_window is not None
        self.progress.stop()
        self._close_preview()
        self.theme = theme
        self.settings["theme"] = theme
        self.colors = gnome.get_palette(theme)
        # Rebuild only the controls: the root, process handles and health-check
        # callbacks survive, so changing appearance cannot restart a meeting.
        for child in self.root.winfo_children():
            child.destroy()
        self._build_ui()
        self._set_status(status, getattr(self.colors, status_role))
        self._set_hint(hint)
        self.start_button.set_enabled(start_enabled)
        self.engine_button.set_text(engine_text)
        if loading:
            self.progress.start()
        self.root.update_idletasks()
        self._rounded.apply_current()
        self.theme_button.focus_set()
        if preview_open:
            self._show_preview()
        if not self._preview_mode and not self.settings.save():
            self._set_hint(f"外观已切换，但未能记住。请检查配置目录的写入权限：\n{CONFIG_PATH.parent}")

    def _show_preview(self) -> None:
        if self._preview_window is not None and self._preview_window.winfo_exists():
            self._preview_window.lift()
            return
        C = self.colors
        window = tk.Toplevel(self.root)
        window.title("字幕预览")
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        window.configure(bg=C.WINDOW)
        self._preview_window = window
        bar = tk.Frame(window, bg=C.WINDOW)
        bar.pack(fill="x", padx=self._px(20), pady=(self._px(6), 0))
        tk.Label(bar, text="字幕预览 · 示例内容", font=self._f(9),
                 fg=C.TEXT_DIM, bg=C.WINDOW).pack(side="left")
        tk.Button(bar, text="关闭预览", command=self._close_preview,
                  font=self._f(9), bg=C.WINDOW, fg=C.TEXT,
                  activebackground=C.HOVER, activeforeground=C.TEXT,
                  bd=0, relief="flat", highlightthickness=1,
                  highlightbackground=C.WINDOW, highlightcolor=C.TEXT_DIM,
                  padx=self._px(10), pady=self._px(4), cursor="hand2").pack(side="right")
        self._preview_width = min(self._px(960), self.root.winfo_screenwidth() - self._px(48))
        canvas = tk.Canvas(window, width=self._preview_width, bg=C.WINDOW,
                           bd=0, highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        self._preview_canvas = canvas
        self._preview_target = canvas.create_text(
            self._px(24), self._px(6), anchor="nw", text="我们先看一下上周的结果。",
            width=self._preview_width - self._px(48), fill=C.TEXT)
        self._preview_source = canvas.create_text(
            self._px(24), 0, anchor="nw", text="Let's review the results from last week.",
            width=self._preview_width - self._px(48), fill=C.TEXT_DIM)
        window.bind("<Escape>", lambda _e: self._close_preview())
        window.bind("<Map>", lambda _e: self._update_preview())
        self._update_preview()
        window.update_idletasks()
        self._preview_rounded = RoundedWindow(window, radius=self._px(12))
        self._preview_rounded.apply_current()
        window.bind("<Configure>", lambda event: self._preview_rounded.apply_current()
                    if event.widget is window and self._preview_rounded is not None else None)

    def _close_preview(self) -> None:
        if self._preview_rounded is not None:
            self._preview_rounded.close()
            self._preview_rounded = None
        if self._preview_window is not None:
            self._preview_window.destroy()
            self._preview_window = None
        self._preview_canvas = None

    def _update_preview(self) -> None:
        canvas = self._preview_canvas
        if canvas is None or not canvas.winfo_exists():
            return
        size = self.font_slider.get()
        canvas.itemconfigure(self._preview_target, font=self._f(size))
        canvas.itemconfigure(self._preview_source, font=self._f(max(11, size - 3)))
        target_bottom = canvas.bbox(self._preview_target)[3]
        canvas.coords(self._preview_source, self._px(24), target_bottom + self._px(4))
        canvas.configure(height=canvas.bbox(self._preview_source)[3] + self._px(18))
        window = self._preview_window
        window.attributes("-alpha", self.opacity_slider.get() / 100)
        window.update_idletasks()
        height = window.winfo_reqheight()
        x = (self.root.winfo_screenwidth() - self._preview_width) // 2
        y = max(0, self.root.winfo_screenheight() - height - self._px(64))
        window.geometry(f"{self._preview_width}x{height}+{x}+{y}")

    def _drag_start(self, event) -> None:
        self._drag_origin = (event.x_root, event.y_root)
        self._window_origin = (self.root.winfo_x(), self.root.winfo_y())

    def _drag_move(self, event) -> None:
        x = self._window_origin[0] + event.x_root - self._drag_origin[0]
        y = self._window_origin[1] + event.y_root - self._drag_origin[1]
        self.root.geometry(f"+{x}+{y}")

    def _round_frame(self) -> None:
        """Re-shape once the window manager has finished framing the window."""
        self._rounded.apply_current()

    def _center(self) -> None:
        self.root.update_idletasks()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 3
        self.root.geometry(f"+{x}+{y}")

    def _set_status(self, text: str, colour: str) -> None:
        self.status_label.configure(text=text)
        self._status_dot.itemconfigure(self._dot, fill=colour)

    def _loading_detail(self, waited: int) -> str:
        """What to say underneath the bar while the models load.

        Most of the wait is spent reading weights off disk with nothing being
        logged, so the byte counter is the only thing that visibly moves. It
        is the difference between "this is working" and "this is frozen", and
        it is worth more here than any estimate would be.
        """
        if waited > 240:
            return (f"加载时间明显偏长。若下面的读取量长时间不动，"
                    f"多半是卡在联网下载模型上。\n日志: {SERVER_LOG}")
        read = (disk_read_bytes(self.server_process.pid)
                if self.server_process is not None else None)
        # Under ~50 MB the models were already in the page cache and this
        # number would only be noise; the load is quick in that case anyway.
        if read and read > 50 * 1024 * 1024:
            return (f"已从磁盘读取 {human_bytes(read)} 模型权重。\n"
                    f"首次加载约 1-3 分钟，之后有系统缓存会快很多。")
        return "模型已缓存时约 20-40 秒，冷启动 1-3 分钟。\n加载完成后「开始会议」会自动变亮。"

    def _set_hint(self, text: str) -> None:
        self.hint.configure(text=text)

    # ------------------------------------------------------------ server mgmt

    def _start_server(self) -> None:
        """Launch the engine detached, logging to a file.

        ``meeting.serve`` rather than the shell script: same configuration on
        every platform, and the process we spawn *is* the server, so the
        ``poll()`` in :meth:`_apply_state` is a real liveness check rather than
        a check on a wrapper shell that has already exited.
        """
        if port_in_use():
            self.state = "starting"
            return
        try:
            paths.ensure(SERVER_LOG.parent)
            log = open(SERVER_LOG, "w", encoding="utf-8")
        except OSError:
            log = subprocess.DEVNULL
        try:
            self.server_process = subprocess.Popen(
                [paths.interpreter(), "-m", "meeting_subtitles.serve"],
                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env=child_environment(), start_new_session=True,
            )
        except OSError as exc:
            self._set_status("无法启动转录引擎", self.colors.ERROR)
            self._set_hint(f"无法启动引擎进程: {exc}")
            self.state = "offline"
            return
        self.state = "starting"
        self._server_wait_started = datetime.now().timestamp()

    def _stop_server(self) -> None:
        """Ask whatever owns the port to exit, freeing the GPU."""
        stop_engine()
        self.server_process = None
        self._user_stopped = True
        self.state = "offline"

    def _toggle_engine(self) -> None:
        if self._preview_mode:
            return
        if self.state == "running":
            self._set_hint("会议进行中，请先结束会议再关闭引擎。")
            return
        if self.state in ("ready", "starting"):
            self._stop_server()
            self._set_hint("转录引擎已关闭，显存已释放。点击「启动引擎」可重新加载。")
        else:
            self._user_stopped = False
            self._set_hint("正在启动转录引擎…")
            self._start_server()

    # --------------------------------------------------------------- actions

    def _remember_options(self) -> None:
        self.settings["title"] = self.title_entry.get()
        self.settings["record_mic"] = self.mic_toggle.value
        self.settings["refine"] = self.refine_toggle.value
        self.settings["domain"] = "cs-ai" if self.domain_toggle.value else "general"
        self.settings["font_size"] = self.font_slider.get()
        self.settings["opacity"] = self.opacity_slider.get()

    def _persist(self) -> None:
        if self._preview_mode:
            return
        self._remember_options()
        self.settings.save()

    def _on_start(self) -> None:
        if self._preview_mode or self.state != "ready" or self.meeting_process is not None:
            return
        self._persist()
        self._close_preview()
        self._set_hint("")

        title = self.title_entry.get() or "会议记录"
        slug = title.replace("/", "-").replace(" ", "_")
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        self.session_dir = Path(self.settings["output_dir"]).expanduser() / f"{stamp}_{slug}"

        command = [
            paths.interpreter(), "-m", "meeting_subtitles",
            "--session-dir", str(self.session_dir),
            "--title", title,
            "--font-size", str(self.font_slider.get()),
            "--opacity", f"{self.opacity_slider.get() / 100:.2f}",
            "--theme", self.theme,
            "--log-level", "WARNING",
        ]
        if not self.mic_toggle.value:
            command.append("--no-mic")
        if not self.refine_toggle.value:
            command.append("--no-refine")
        command += ["--domain", "cs-ai" if self.domain_toggle.value else "general"]

        try:
            self.meeting_process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL,
            )
        except OSError as exc:
            self._set_hint(f"无法启动会议: {exc}")
            return

        self.state = "running"
        self.root.withdraw()          # get out of the way; the overlay takes over

    def _on_meeting_finished(self) -> None:
        self.meeting_process = None
        self.state = "ready" if server_is_up() else "offline"
        self.root.deiconify()
        self.root.lift()
        if self.session_dir and (self.session_dir / "transcript.md").exists():
            self._set_hint(f"转录已保存到\n{self.session_dir}")
        elif self.session_dir:
            self._set_hint("本场会议没有识别到语音，未生成转录。")

    def _open_folder(self) -> None:
        target = self.session_dir if self.session_dir and self.session_dir.exists() \
            else Path(self.settings["output_dir"]).expanduser()
        try:
            paths.open_in_file_manager(target)
        except OSError:
            self._set_hint(f"无法打开会议记录。请检查目录权限，或手动打开：\n{target}")

    def _on_window_close(self) -> None:
        self._close_preview()
        self._persist()
        if self.meeting_process is not None:
            self.meeting_process.terminate()
        # The server deliberately outlives the launcher so the next meeting
        # starts instantly; "关闭引擎" is the way to free the GPU.
        self.progress.stop()
        self.root.after_cancel(self._round_job)
        if self._poll_job is not None:
            self.root.after_cancel(self._poll_job)
        self._rounded.close()
        self.root.destroy()

    # ------------------------------------------------------------------ loop

    def _poll(self) -> None:
        if self.state == "running":
            if self.meeting_process is not None and self.meeting_process.poll() is not None:
                self._on_meeting_finished()
            else:
                self.progress.stop()
                self._set_status("会议进行中", self.colors.SUCCESS)
                self.start_button.set_enabled(False)
                self.engine_button.set_text("关闭引擎")
        else:
            threading.Thread(target=self._refresh_state, daemon=True).start()
        self._poll_job = self.root.after(1500, self._poll)

    def _refresh_state(self) -> None:
        """Health check off the UI thread; the result is applied back on it.

        The window can be destroyed while a check is still in flight -- closing
        the launcher is exactly when that happens -- and scheduling onto a dead
        interpreter raises. There is nothing left to update at that point, so
        the result is simply dropped.
        """
        up = server_is_up()
        listening = up or port_in_use()
        try:
            self.root.after(0, lambda: self._apply_state(up, listening))
        except (RuntimeError, tk.TclError):
            pass

    def _apply_state(self, up: bool, listening: bool) -> None:
        """Reconcile the UI with the engine's actual state.

        Note that uvicorn binds its port only *after* the lifespan startup
        finishes loading the models, so during the 35 s - 4 min load nothing is
        listening. Liveness therefore has to come from the process we spawned,
        not from the port.
        """
        if self.state == "running":
            return

        if up:
            if self.state != "ready":
                self._set_hint("")
            self.state = "ready"
            self.progress.stop()
            self._set_status("转录引擎已就绪", self.colors.SUCCESS)
            self.start_button.set_enabled(True)
            self.engine_button.set_text("关闭引擎")
            return

        loading = listening or (
            self.server_process is not None and self.server_process.poll() is None
        )
        if loading:
            self.state = "starting"
            self.progress.start()
            waited = int(datetime.now().timestamp() - self._server_wait_started) \
                if self._server_wait_started else 0
            stage = engine_stage()
            self._set_status(f"{stage}…（{waited} 秒）" if waited
                             else f"{stage}…", self.colors.WARNING)
            self.start_button.set_enabled(False)
            self.engine_button.set_text("关闭引擎")
            self._set_hint(self._loading_detail(waited))
            return

        if self.server_process is not None:
            # We launched it and it exited without ever answering /health.
            self.server_process = None
            self.state = "offline"
            self.progress.stop()
            self._set_status("转录引擎启动失败", self.colors.ERROR)
            self.start_button.set_enabled(False)
            self.engine_button.set_text("启动引擎")
            reason = last_error_line()
            self._set_hint(
                f"{reason}\n完整日志: {SERVER_LOG}" if reason
                else f"引擎进程已退出。日志: {SERVER_LOG}"
            )
            return

        if not self._user_stopped:
            self._set_status("正在启动转录引擎…", self.colors.WARNING)
            self.start_button.set_enabled(False)
            self._start_server()
            return

        self.state = "offline"
        self.progress.stop()
        self._set_status("转录引擎已关闭", self.colors.TEXT_MUTED)
        self.start_button.set_enabled(False)
        self.engine_button.set_text("启动引擎")

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    normalize_proxy_env()
    # Must happen before any Tk window exists: this may re-exec the process.
    ensure_cjk_tk(module="meeting_subtitles.launcher")
    paths.ensure(paths.config_dir())
    LauncherApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
