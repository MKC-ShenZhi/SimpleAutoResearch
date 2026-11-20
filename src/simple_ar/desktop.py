"""Native desktop workbench for SimpleAutoResearch.

Runs the shared local HTTP Workspace server in a daemon thread and renders the
modern desktop UI inside a native WebView2 window (via pywebview), with a
browser fallback and guaranteed clean shutdown on window close.
"""
from __future__ import annotations

import argparse
import io
import os
from pathlib import Path
import sys
import threading
import webbrowser

from simple_ar.ui import Workspace, server


class _DesktopBridge:
    """Narrow JS bridge for native OS dialogs (never bypasses Workspace grants)."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = str(project_root)
        self._window = None

    def bind(self, window) -> None:
        self._window = window

    def pick_files(self) -> list[str]:
        if self._window is None:
            return []
        import webview

        result = self._window.create_file_dialog(
            webview.FileDialog.OPEN,
            directory=self._project_root,
            allow_multiple=True,
        )
        return [str(p) for p in result] if result else []

    def pick_folder(self) -> list[str]:
        if self._window is None:
            return []
        import webview

        result = self._window.create_file_dialog(
            webview.FileDialog.FOLDER,
            directory=self._project_root,
        )
        return [str(p) for p in result] if result else []


def _shutdown_workspace(httpd, workspace: Workspace) -> None:
    """Stop local HTTP server and signal active task workers to pause cleanly."""
    try:
        httpd.shutdown()
    except Exception:
        pass
    try:
        httpd.server_close()
    except Exception:
        pass
    with workspace.lock:
        workers = list(workspace.workers.values())
        for _, stop in workers:
            stop.set()
    for thread, _ in workers:
        thread.join(timeout=3.0)


def _load_project_env(project_root: Path) -> None:
    """Load project .env (without overriding shell env) and default to project models.toml if present."""
    from dotenv import load_dotenv

    env_file = project_root / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=False)
    if not os.environ.get("SIMPLE_AR_MODELS_CONFIG", "").strip():
        for candidate in (project_root / ".local" / "models.toml", project_root / "models.toml"):
            if candidate.is_file():
                os.environ["SIMPLE_AR_MODELS_CONFIG"] = str(candidate.resolve())
                break


def run(project_root: Path, read_roots: list[Path], task_root: Path, *, browser: bool = False) -> None:
    assets_index = Path(__file__).parent / "desktop_assets" / "index.html"
    if not assets_index.is_file():
        raise RuntimeError(
            "Desktop UI assets not found. Build desktop-ui first: cd desktop-ui && npm run build"
        )

    resolved_project = project_root.expanduser().resolve()
    _load_project_env(resolved_project)

    workspace = Workspace(
        resolved_project,
        [p.expanduser().resolve() for p in read_roots],
        task_root.expanduser().resolve(),
    )
    httpd = server(workspace, port=0)
    url = f"{httpd.origin}/desktop/#{httpd.session_token}"

    serve_thread = threading.Thread(target=httpd.serve_forever, name="sar-desktop-http", daemon=True)
    serve_thread.start()

    if browser:
        print(f"SimpleAutoResearch Desktop (browser mode): {url}", flush=True)
        webbrowser.open(url)
        try:
            serve_thread.join()
        except KeyboardInterrupt:
            pass
        finally:
            _shutdown_workspace(httpd, workspace)
        return

    try:
        import webview
    except ImportError as exc:
        _shutdown_workspace(httpd, workspace)
        raise SystemExit(
            "pywebview is not installed. Install with `uv pip install pywebview` "
            "or launch with `--browser`."
        ) from exc

    icon_path = Path(__file__).parent / "desktop_icon.ico"
    icon_arg = str(icon_path) if icon_path.is_file() else None

    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SimpleAutoResearch.Desktop")
        except Exception:
            pass

    bridge = _DesktopBridge(workspace.project)
    window = webview.create_window(
        title="SimpleAutoResearch",
        url=url,
        width=1360,
        height=860,
        min_size=(960, 640),
        background_color="#141518",
        js_api=bridge,
    )
    bridge.bind(window)

    def _apply_windows_icon() -> None:
        if os.name != "nt" or not icon_arg:
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, "SimpleAutoResearch")
            if not hwnd:
                return
            IMAGE_ICON = 1
            LR_LOADFROMFILE = 0x0010
            LR_DEFAULTSIZE = 0x0040
            WM_SETICON = 0x0080
            hicon_small = user32.LoadImageW(None, icon_arg, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
            hicon_big = user32.LoadImageW(None, icon_arg, IMAGE_ICON, 32, 32, LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if hicon_small:
                user32.SendMessageW(hwnd, WM_SETICON, 0, hicon_small)
            if hicon_big:
                user32.SendMessageW(hwnd, WM_SETICON, 1, hicon_big)
        except Exception:
            pass

    def on_shown() -> None:
        _apply_windows_icon()

    def on_closing() -> bool:
        with workspace.lock:
            running = any(thread.is_alive() for thread, _ in workspace.workers.values())
        if running:
            # Signal cooperative boundary pause so shutdown is clean.
            with workspace.lock:
                for _, stop in workspace.workers.values():
                    stop.set()
        return True

    window.events.shown += on_shown
    window.events.closing += on_closing

    try:
        webview.start(debug=False, icon=icon_arg)
    finally:
        _shutdown_workspace(httpd, workspace)
        # Ensure no lingering non-daemon threads keep pythonw.exe alive on Windows.
        os._exit(0)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="SimpleAutoResearch desktop workbench")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--read-root", type=Path, action="append", default=[])
    parser.add_argument("--task-root", type=Path, default=Path.cwd() / "runs")
    parser.add_argument("--browser", action="store_true", help="Open the desktop UI in the system browser instead of a native window")
    args = parser.parse_args(argv)
    if sys.stdin is None or not getattr(sys.stdin, "isatty", lambda: False)():
        sys.stdin = io.StringIO()
    run(args.project_root, args.read_root, args.task_root, browser=args.browser)


if __name__ == "__main__":
    main()
