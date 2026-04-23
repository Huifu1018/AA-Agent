import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen


BASE_DIR = Path(__file__).resolve().parents[1]
STATE_DIR = BASE_DIR / "logs" / "web_ui"
STATE_DIR.mkdir(parents=True, exist_ok=True)
PID_PATH = STATE_DIR / "aa_cua_ui.pid"
LOG_PATH = Path("/tmp/aa_cua_ui.log")
HOST = os.getenv("AGENT_S_UI_HOST", "127.0.0.1")
PORT = int(os.getenv("AGENT_S_UI_PORT", "8787"))
HEALTH_URL = f"http://127.0.0.1:{PORT}/healthz"
PYTHON_BIN = BASE_DIR / ".venv" / "bin" / "python"


def _is_healthy() -> bool:
    try:
        with urlopen(HEALTH_URL, timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def _read_pid() -> int | None:
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _stop_existing() -> None:
    pid = _read_pid()
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except Exception:
        return

    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        except Exception:
            break
        time.sleep(0.1)


def _start() -> int:
    env = os.environ.copy()
    env.setdefault("AGENT_S_UI_HOST", HOST)
    python_bin = str(PYTHON_BIN if PYTHON_BIN.exists() else Path(sys.executable))
    command = [python_bin, str(BASE_DIR / "tools" / "agent_s_web_ui.py")]
    with LOG_PATH.open("a", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            command,
            cwd=str(BASE_DIR),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    PID_PATH.write_text(str(process.pid), encoding="utf-8")
    return process.pid


def main() -> None:
    if _is_healthy():
        print(f"AA-CUA UI already healthy at {HEALTH_URL}")
        return

    _stop_existing()
    pid = _start()

    deadline = time.time() + 20
    while time.time() < deadline:
        if _is_healthy():
            print(f"AA-CUA UI started successfully on port {PORT} (pid={pid})")
            return
        time.sleep(0.5)

    print(f"AA-CUA UI failed to become healthy on port {PORT}. See {LOG_PATH}", file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
