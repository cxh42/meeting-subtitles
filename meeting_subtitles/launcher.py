r"""Graphical launcher: click once to start a meeting.

Owns everything the command line used to require -- it starts the transcription
server if it is not already up, remembers the settings you used last time, hides
itself while the subtitle overlay is on screen, and comes back afterwards with a
link to the transcript.

    .venv/bin/python -m meeting_subtitles.launcher
"""

import json
import logging
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
from meeting_subtitles.tkfix import child_environment, ensure_cjk_tk

logger = logging.getLogger(__name__)

CONFIG_PATH = paths.settings_path()
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


class Settings:
    """Last-used launcher options, persisted between runs."""

    DEFAULTS = {
        "title": "",
        "context": "",
        "record_mic": True,
        "refine": True,
        "domain": "cs-ai",
        "font_size": 20,
        "output_dir": str(paths.default_output_dir()),
    }

    def __init__(self) -> None:
        self.data = dict(self.DEFAULTS)
        try:
            self.data.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("无法保存设置: %s", exc)

    def __getitem__(self, key):
        return self.data.get(key, self.DEFAULTS.get(key))

    def __setitem__(self, key, value):
        self.data[key] = value


class LauncherApp:
    """The launcher window and the processes it supervises."""

    def __init__(self) -> None:
        self.settings = Settings()
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
        self.root.after(150, self._round_frame)
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)
        self.root.after(200, self._poll)

    # ---------------------------------------------------------------- layout

    def _f(self, size: int, weight: str = "normal") -> tuple:
        return (self.font_family, size, weight)

    def _px(self, value: float) -> int:
        return int(value * self.k)

    def _f(self, size: int, weight: str = "normal") -> tuple:
        return (self.font_family, size, weight)

    def _px(self, value: float) -> int:
        return int(value * self.k)

    def _build_ui(self) -> None:
        """A header bar and three boxed lists, the way GNOME Settings is built.

        Deliberately no tagline or feature blurb: a tool the user opens every
        day should show its controls, not describe itself.
        """
        G = gnome
        self.root.configure(bg=G.WINDOW)

        # We drop the WM title bar to get all four corners rounded, so the
        # window needs its own -- which is also what GNOME's own apps do.
        header = tk.Frame(self.root, bg=G.HEADERBAR, height=self._px(44))
        header.pack(fill="x", side="top")
        header.pack_propagate(False)
        tk.Label(header, text="会议字幕", font=self._f(11, "bold"),
                 fg=G.TEXT, bg=G.HEADERBAR).place(relx=0.5, rely=0.5, anchor="center")
        close = tk.Label(header, text="✕", font=self._f(11), fg=G.TEXT_DIM,
                         bg=G.HEADERBAR, cursor="hand2",
                         padx=self._px(12), pady=self._px(6))
        close.pack(side="right", padx=(0, self._px(8)))
        close.bind("<Button-1>", lambda _e: self._on_window_close())
        close.bind("<Enter>", lambda _e: close.configure(fg=G.TEXT))
        close.bind("<Leave>", lambda _e: close.configure(fg=G.TEXT_DIM))
        for widget in (header,):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)

        body = tk.Frame(self.root, bg=G.WINDOW)
        body.pack(fill="both", expand=True,
                  padx=self._px(20), pady=(self._px(20), self._px(20)))
        W = 340

        # --- status ---------------------------------------------------------
        status_box = G.BoxedList(body, width=W, scale=self.k, parent_bg=G.WINDOW)
        row = status_box.add_row(50)
        self._status_dot = tk.Canvas(row, width=self._px(10), height=self._px(10),
                                     bg=G.CARD, highlightthickness=0, bd=0)
        self._dot = self._status_dot.create_oval(
            0, 0, self._px(9), self._px(9), fill=G.WARNING, outline="")
        self._status_dot.pack(side="left", padx=(self._px(16), self._px(10)))
        self.status_label = tk.Label(row, text="正在检查转录引擎", anchor="w",
                                     font=self._f(11), fg=G.TEXT, bg=G.CARD)
        self.status_label.pack(side="left", fill="x", expand=True)
        self.engine_button = G.Button(
            row, "关闭引擎", self._toggle_engine, width=88, height=30,
            scale=self.k, parent_bg=G.CARD, font=self._f(9))
        self.engine_button.pack(side="right", padx=(0, self._px(12)))
        status_box.render()
        status_box.pack(anchor="w", pady=(0, self._px(18)))

        # --- text fields ----------------------------------------------------
        fields = G.BoxedList(body, width=W, scale=self.k, parent_bg=G.WINDOW)
        row = fields.add_row(56)
        tk.Label(row, text="会议名称", font=self._f(11), fg=G.TEXT,
                 bg=G.CARD).pack(side="left", padx=(self._px(16), 0))
        self.title_entry = G.Entry(row, placeholder="可留空", width=170,
                                   scale=self.k, font=self._f(10), parent_bg=G.CARD)
        self.title_entry.pack(side="right", padx=(0, self._px(12)))
        self.title_entry.set(self.settings["title"])

        row = fields.add_row(56)
        tk.Label(row, text="人名与项目名", font=self._f(11), fg=G.TEXT,
                 bg=G.CARD).pack(side="left", padx=(self._px(16), 0))
        self.context_entry = G.Entry(row, placeholder="Anirudh, Helios",
                                     width=170, scale=self.k, font=self._f(10),
                                     parent_bg=G.CARD)
        self.context_entry.pack(side="right", padx=(0, self._px(12)))
        self.context_entry.set(self.settings["context"])
        fields.render()
        fields.pack(anchor="w", pady=(0, self._px(18)))

        # --- switches -------------------------------------------------------
        options = G.BoxedList(body, width=W, scale=self.k, parent_bg=G.WINDOW)

        def switch_row(label: str, subtitle: str, value: bool) -> G.Switch:
            row = options.add_row(58 if subtitle else 50)
            text = tk.Frame(row, bg=G.CARD)
            text.pack(side="left", fill="both", expand=True,
                      padx=(self._px(16), 0))
            tk.Label(text, text=label, font=self._f(11), fg=G.TEXT, bg=G.CARD,
                     anchor="w").pack(fill="x", pady=(self._px(9) if subtitle else 0, 0))
            if subtitle:
                tk.Label(text, text=subtitle, font=self._f(8), fg=G.TEXT_DIM,
                         bg=G.CARD, anchor="w").pack(fill="x")
            switch = G.Switch(row, value=value, scale=self.k, parent_bg=G.CARD)
            switch.pack(side="right", padx=(0, self._px(14)))
            return switch

        self.mic_toggle = switch_row("录制麦克风", "把你说的话也计入转录",
                                     bool(self.settings["record_mic"]))
        self.refine_toggle = switch_row("整句润色", "说完一句后重新翻译，占用约 8 GB 显存",
                                        bool(self.settings["refine"]))
        self.domain_toggle = switch_row(
            "CS / AI 术语", f"内置 {len(domains.get('cs-ai').terms)} 个领域词",
            self.settings["domain"] == "cs-ai")

        row = options.add_row(50)
        tk.Label(row, text="字幕大小", font=self._f(11), fg=G.TEXT,
                 bg=G.CARD).pack(side="left", padx=(self._px(16), 0))
        self.font_slider = G.Slider(
            row, minimum=14, maximum=34, value=int(self.settings["font_size"]),
            width=175, scale=self.k, font=self._f(9), parent_bg=G.CARD)
        self.font_slider.pack(side="right", padx=(0, self._px(12)))
        options.render()
        options.pack(anchor="w", pady=(0, self._px(22)))

        # --- primary action -------------------------------------------------
        self.start_button = G.Button(
            body, "开始会议", self._on_start, width=W, height=44, scale=self.k,
            accent=True, parent_bg=G.WINDOW, font=self._f(12, "bold"))
        self.start_button.pack(anchor="w")
        self.start_button.set_enabled(False)

        footer = tk.Frame(body, bg=G.WINDOW)
        footer.pack(fill="x", pady=(self._px(12), 0))
        self.folder_button = G.Button(
            footer, "打开转录文件夹", self._open_folder, width=W, height=34,
            scale=self.k, parent_bg=G.WINDOW, font=self._f(10))
        self.folder_button.pack(anchor="w")

        self.hint = tk.Label(body, text="", font=self._f(8), fg=G.TEXT_DIM,
                             bg=G.WINDOW, wraplength=self._px(W), justify="left")
        self.hint.pack(anchor="w", pady=(self._px(10), 0))

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
            self._set_status("无法启动转录引擎", gnome.ERROR)
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
        if self.state == "running":
            self._set_hint("会议进行中，请先结束会议再关闭引擎。")
            return
        if self.state in ("ready", "starting"):
            self._stop_server()
            self._set_hint("转录引擎已关闭，显存已释放。下次开始会议会自动重新加载。")
        else:
            self._user_stopped = False
            self._set_hint("正在启动转录引擎…")
            self._start_server()

    # --------------------------------------------------------------- actions

    def _persist(self) -> None:
        self.settings["title"] = self.title_entry.get()
        self.settings["context"] = self.context_entry.get()
        self.settings["record_mic"] = self.mic_toggle.value
        self.settings["refine"] = self.refine_toggle.value
        self.settings["domain"] = "cs-ai" if self.domain_toggle.value else "general"
        self.settings["font_size"] = self.font_slider.get()
        self.settings.save()

    def _on_start(self) -> None:
        if self.state != "ready" or self.meeting_process is not None:
            return
        self._persist()

        title = self.title_entry.get() or "会议记录"
        slug = title.replace("/", "-").replace(" ", "_")
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        self.session_dir = Path(self.settings["output_dir"]).expanduser() / f"{stamp}_{slug}"

        command = [
            paths.interpreter(), "-m", "meeting_subtitles",
            "--session-dir", str(self.session_dir),
            "--title", title,
            "--font-size", str(self.font_slider.get()),
            "--log-level", "WARNING",
        ]
        if self.context_entry.get():
            command += ["--context", self.context_entry.get()]
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
        paths.open_in_file_manager(target)

    def _on_window_close(self) -> None:
        self._persist()
        if self.meeting_process is not None:
            self.meeting_process.terminate()
        # The server deliberately outlives the launcher so the next meeting
        # starts instantly; "关闭引擎" is the way to free the GPU.
        self.root.destroy()

    # ------------------------------------------------------------------ loop

    def _poll(self) -> None:
        if self.state == "running":
            if self.meeting_process is not None and self.meeting_process.poll() is not None:
                self._on_meeting_finished()
            else:
                self._set_status("会议进行中", gnome.SUCCESS)
                self.start_button.set_enabled(False)
                self.engine_button.set_text("关闭引擎")
        else:
            threading.Thread(target=self._refresh_state, daemon=True).start()
        self.root.after(1500, self._poll)

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
            self.state = "ready"
            self._set_status("转录引擎已就绪", gnome.SUCCESS)
            self.start_button.set_enabled(True)
            self.engine_button.set_text("关闭引擎")
            return

        loading = listening or (
            self.server_process is not None and self.server_process.poll() is None
        )
        if loading:
            self.state = "starting"
            waited = int(datetime.now().timestamp() - self._server_wait_started) \
                if self._server_wait_started else 0
            suffix = f"（已等待 {waited} 秒）" if waited else ""
            self._set_status(f"正在加载模型…{suffix}", gnome.WARNING)
            self.start_button.set_enabled(False)
            self.engine_button.set_text("关闭引擎")
            if waited > 150:
                # Well past a normal cold load: something is wrong, and staring
                # at a spinner tells the user nothing.
                self._set_hint(
                    f"加载时间明显偏长，通常是卡在联网下载模型上。\n"
                    f"查看日志: {SERVER_LOG}"
                )
            else:
                self._set_hint("模型已缓存时约 20-40 秒；需要下载时会久一些。"
                               "加载完成后「开始会议」按钮会自动变亮。")
            return

        if self.server_process is not None:
            # We launched it and it exited without ever answering /health.
            self.server_process = None
            self.state = "offline"
            self._set_status("转录引擎启动失败", gnome.ERROR)
            self.start_button.set_enabled(False)
            self.engine_button.set_text("启动引擎")
            reason = last_error_line()
            self._set_hint(
                f"{reason}\n完整日志: {SERVER_LOG}" if reason
                else f"引擎进程已退出。日志: {SERVER_LOG}"
            )
            return

        if not self._user_stopped:
            self._set_status("正在启动转录引擎…", gnome.WARNING)
            self.start_button.set_enabled(False)
            self._start_server()
            return

        self.state = "offline"
        self._set_status("转录引擎未运行", gnome.ERROR)
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
