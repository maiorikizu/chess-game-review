import sys
import os
import threading
from datetime import datetime
import webview

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import app_paths


class _TeeStream:

    def __init__(self, stream, log_file):
        self._stream = stream
        self._log = log_file

    def write(self, data):
        try:
            if self._stream is not None:
                self._stream.write(data)
        except Exception:
            pass
        try:
            self._log.write(data)
            self._log.flush()
        except Exception:
            pass

    def flush(self):
        for target in (self._stream, self._log):
            try:
                if target is not None:
                    target.flush()
            except Exception:
                pass


def setup_runtime_logging():
    if not app_paths.is_frozen():
        return
    try:
        log_path = os.path.join(app_paths.get_state_dir(), "runtime.log")
        log_file = open(log_path, "a", encoding="utf-8", buffering=1)
        log_file.write(f"\n===== Session started {datetime.now().isoformat(timespec='seconds')} =====\n")
        sys.stdout = _TeeStream(sys.stdout, log_file)
        sys.stderr = _TeeStream(sys.stderr, log_file)
    except Exception:
        pass


def fail_visible(msg, _test_hook=None):
    msg = str(msg)
    try:
        log_dir = app_paths.get_state_dir()
        log_path = os.path.join(log_dir, "startup-error.log")
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass
    try:
        if _test_hook is not None:
            _test_hook(msg)
        else:
            import ctypes
            MB_ICONERROR = 0x10
            ctypes.windll.user32.MessageBoxW(
                0, msg, "Chess Game Review - Startup Error", MB_ICONERROR
            )
    except Exception:
        pass
    sys.exit(1)


try:
    from server import start_server
except Exception as _exc:
    fail_visible(f"Failed to import the backend server module.\n\n{_exc!r}")


def _shutdown_server(server):
    try:
        server.shutdown()
    except Exception:
        pass
    try:
        server.server_close()
    except Exception:
        pass


def run_app():
    try:
        server, port = start_server(port=None, open_browser=False)
    except Exception as exc:
        fail_visible(f"Could not start the local backend server.\n\n{exc!r}")

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    app_url = f"http://127.0.0.1:{port}/index.html"
    print(f"[Desktop App] Backend started on {app_url}. Initializing native WebView...")

    try:
        webview.create_window(
            title="Chess Game Review",
            url=app_url,
            width=1440,
            height=920,
            min_size=(1080, 700),
            background_color="#121211",
            text_select=True,
            zoomable=True,
        )

        webview.start(private_mode=False)
    except SystemExit:
        raise
    except Exception as exc:
        print("[Desktop App] WebView startup failed; shutting down server...")
        _shutdown_server(server)
        fail_visible(
            "The desktop WebView failed to start. Is the Microsoft Edge WebView2 "
            f"Runtime installed?\n\n{exc!r}"
        )
    finally:
        print("[Desktop App] Closing application...")
        _shutdown_server(server)
    sys.exit(0)


if __name__ == "__main__":
    setup_runtime_logging()
    try:
        run_app()
    except SystemExit:
        raise
    except Exception as _exc:
        fail_visible(f"Unexpected startup failure.\n\n{_exc!r}")
