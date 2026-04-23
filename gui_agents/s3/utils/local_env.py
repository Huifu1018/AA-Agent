import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict

import pyautogui
import pytesseract


def _resolve_path(path: str) -> Path:
    return Path(path).expanduser().resolve()


def copy_file(src: str, dst: str) -> str:
    source = _resolve_path(src)
    target = _resolve_path(dst)
    if not source.exists():
        raise FileNotFoundError(f"Source file does not exist: {source}")
    if not source.is_file():
        raise IsADirectoryError(f"Source is not a file: {source}")
    if not target.parent.exists():
        raise FileNotFoundError(f"Target parent directory does not exist: {target.parent}")
    shutil.copy2(source, target)
    return str(target)


def move_file(src: str, dst: str) -> str:
    source = _resolve_path(src)
    target = _resolve_path(dst)
    if not source.exists():
        raise FileNotFoundError(f"Source file does not exist: {source}")
    if not target.parent.exists():
        raise FileNotFoundError(f"Target parent directory does not exist: {target.parent}")
    shutil.move(str(source), str(target))
    return str(target)


def rename_file(src: str, dst: str) -> str:
    return move_file(src, dst)


def reveal_in_finder(path: str) -> str:
    target = _resolve_path(path)
    if not target.exists():
        raise FileNotFoundError(f"Path does not exist: {target}")
    if sys.platform == "darwin":
        if target.is_dir():
            subprocess.run(["open", str(target)], check=True)
        else:
            subprocess.run(["open", "-R", str(target)], check=True)
    else:
        raise NotImplementedError("reveal_in_finder is only implemented on macOS")
    return str(target)


WECHAT_BUNDLE_ID = "com.tencent.xinWeChat"
WECHAT_PROCESS_NAMES = {"微信", "WeChat"}


def _applescript_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _run_osascript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _wechat_keystroke(text: str) -> None:
    script = f'''
    tell application "System Events"
        keystroke {_applescript_quote(text)}
    end tell
    '''
    _run_osascript(script)


def _wechat_keycode(code: int, using: str | list[str] | tuple[str, ...] | None = None) -> None:
    if using:
        if isinstance(using, str):
            modifiers = [modifier.strip() for modifier in using.split(",") if modifier.strip()]
        else:
            modifiers = [str(modifier).strip() for modifier in using if str(modifier).strip()]
        using_clause = modifiers[0] if len(modifiers) == 1 else "{" + ", ".join(modifiers) + "}"
        script = f'''
        tell application "System Events"
            key code {code} using {using_clause}
        end tell
        '''
    else:
        script = f'''
        tell application "System Events"
            key code {code}
        end tell
        '''
    _run_osascript(script)


def _set_clipboard(text: str) -> None:
    subprocess.run(["pbcopy"], input=text, text=True, check=True)


def _get_clipboard() -> str:
    result = subprocess.run(["pbpaste"], check=True, capture_output=True, text=True)
    return result.stdout


def _get_frontmost_app() -> str:
    script = """
    tell application "System Events"
        set frontApp to first application process whose frontmost is true
        return name of frontApp
    end tell
    """
    return _run_osascript(script)


def _activate_wechat() -> None:
    _run_osascript(f'tell application id "{WECHAT_BUNDLE_ID}" to activate')
    time.sleep(0.6)


def _activate_app_by_name(app_name: str) -> None:
    app_name = (app_name or "").strip()
    if not app_name:
        return
    _run_osascript(f'tell application {_applescript_quote(app_name)} to activate')
    time.sleep(0.35)


def _ensure_wechat_frontmost() -> None:
    front_app = _get_frontmost_app()
    if front_app not in WECHAT_PROCESS_NAMES:
        raise RuntimeError(
            f"WeChat is not frontmost. Current frontmost app: {front_app}"
        )


def _get_wechat_window_bounds() -> tuple[int, int, int, int]:
    script = """
    tell application "System Events"
        set frontApp to first application process whose frontmost is true
        tell frontApp
            set bestBounds to ""
            set bestArea to 0
            repeat with win in windows
                try
                    set {xPos, yPos} to position of win
                    set {winW, winH} to size of win
                    set areaValue to (winW * winH)
                    if areaValue > bestArea then
                        set bestArea to areaValue
                        set bestBounds to (xPos as text) & "," & (yPos as text) & "," & (winW as text) & "," & (winH as text)
                    end if
                end try
            end repeat
            if bestBounds is "" then error "No accessible WeChat window found."
            return bestBounds
        end tell
    end tell
    """
    raw = _run_osascript(script)
    x_pos, y_pos, win_w, win_h = (int(part) for part in raw.split(","))
    return x_pos, y_pos, win_w, win_h


def _ocr_region(x_pos: int, y_pos: int, width: int, height: int) -> str:
    screenshot = pyautogui.screenshot(region=(x_pos, y_pos, width, height))
    return pytesseract.image_to_string(screenshot, lang="chi_sim+eng").strip()


def _get_wechat_chat_title() -> str:
    _ensure_wechat_frontmost()
    x_pos, y_pos, win_w, win_h = _get_wechat_window_bounds()
    title_x = round(x_pos + win_w * 0.36)
    title_y = round(y_pos + win_h * 0.01)
    title_w = round(win_w * 0.34)
    title_h = max(50, round(win_h * 0.07))
    return _ocr_region(title_x, title_y, title_w, title_h)


def _select_first_wechat_search_result() -> None:
    _ensure_wechat_frontmost()
    x_pos, y_pos, win_w, win_h = _get_wechat_window_bounds()
    result_x = round(x_pos + win_w * 0.18)
    result_y = round(y_pos + win_h * 0.11)
    pyautogui.doubleClick(result_x, result_y, interval=0.12)
    time.sleep(0.55)


def _open_wechat_chat(chat_name: str) -> None:
    _activate_wechat()
    _ensure_wechat_frontmost()
    _wechat_keycode(3, using="command down")  # Cmd+F
    time.sleep(0.25)
    _ensure_wechat_frontmost()
    _wechat_keycode(0, using="command down")  # Cmd+A
    time.sleep(0.08)
    previous_clipboard = _get_clipboard()
    try:
        _set_clipboard(chat_name)
        _wechat_keycode(9, using="command down")  # Cmd+V
        time.sleep(0.45)
        _ensure_wechat_frontmost()
        # Prefer Enter to open the first exact search match. Fixed-position
        # clicks in the left sidebar can accidentally activate the wrong chat.
        _wechat_keycode(36)
        time.sleep(0.7)
    finally:
        _set_clipboard(previous_clipboard)
    _ensure_wechat_frontmost()


def _focus_wechat_input_area() -> None:
    _ensure_wechat_frontmost()
    x_pos, y_pos, win_w, win_h = _get_wechat_window_bounds()
    click_x = round(x_pos + win_w * 0.70)
    click_y = round(y_pos + win_h * 0.92)
    pyautogui.click(click_x, click_y)
    time.sleep(0.12)
    pyautogui.click(click_x, click_y)
    time.sleep(0.18)


def send_wechat_text(chat_name: str, message: str, press_enter: bool = True) -> str:
    previous_clipboard = _get_clipboard()
    try:
        _open_wechat_chat(chat_name)
        _focus_wechat_input_area()
        _set_clipboard(message)
        _wechat_keycode(9, using="command down")  # Cmd+V
        time.sleep(0.15)
        if press_enter:
            _wechat_keycode(36)  # Enter
            time.sleep(0.2)
    finally:
        _set_clipboard(previous_clipboard)
    return f"Sent WeChat message to {chat_name}: {message}"


def draft_wechat_text(chat_name: str, message: str, replace_existing: bool = False) -> str:
    previous_clipboard = _get_clipboard()
    try:
        _open_wechat_chat(chat_name)
        _focus_wechat_input_area()
        if replace_existing:
            _wechat_keycode(0, using="command down")  # Cmd+A
            time.sleep(0.08)
        _set_clipboard(message)
        _wechat_keycode(9, using="command down")  # Cmd+V
        time.sleep(0.15)
    finally:
        _set_clipboard(previous_clipboard)
    return f"Drafted WeChat message to {chat_name}: {message}"


def draft_current_wechat_input(message: str, replace_existing: bool = False) -> str:
    previous_clipboard = _get_clipboard()
    try:
        _ensure_wechat_frontmost()
        _focus_wechat_input_area()
        if replace_existing:
            _wechat_keycode(0, using="command down")  # Cmd+A
            time.sleep(0.08)
        _set_clipboard(message)
        _wechat_keycode(9, using="command down")  # Cmd+V
        time.sleep(0.15)
    finally:
        _set_clipboard(previous_clipboard)
    return "Drafted message into current WeChat input."


def send_wechat_emoji(chat_name: str, emoji: str = "😊", count: int = 1) -> str:
    count = max(1, int(count))
    message = emoji * count
    return send_wechat_text(chat_name, message, press_enter=True)


def _candidate_search_roots() -> list[Path]:
    cwd = Path.cwd().resolve()
    roots = [cwd]
    roots.extend(cwd.parents[:4])
    deduped = []
    seen = set()
    for root in roots:
        if root not in seen and root.exists():
            deduped.append(root)
            seen.add(root)
    return deduped


def resolve_file_reference(file_reference: str) -> Path:
    candidate = Path(file_reference).expanduser()
    if candidate.exists():
        return candidate.resolve()

    file_name = candidate.name
    matches: list[Path] = []
    for root in _candidate_search_roots():
        try:
            matches.extend(path for path in root.rglob(file_name) if path.is_file())
        except Exception:
            continue
        if matches:
            break

    if not matches:
        raise FileNotFoundError(f"Could not resolve file reference: {file_reference}")

    matches.sort(key=lambda p: (len(str(p)), str(p)))
    return matches[0].resolve()


def _click_wechat_attachment_button() -> None:
    _ensure_wechat_frontmost()
    x_pos, y_pos, win_w, win_h = _get_wechat_window_bounds()
    click_x = round(x_pos + win_w * 0.125)
    click_y = round(y_pos + win_h * 0.92)
    pyautogui.click(click_x, click_y)
    time.sleep(0.35)


def _wechat_has_open_panel() -> bool:
    script = """
    tell application "System Events"
        set frontApp to first application process whose frontmost is true
        tell frontApp
            try
                return exists sheet 1 of front window
            on error
                return false
            end try
        end tell
    end tell
    """
    try:
        return _run_osascript(script).strip().lower() == "true"
    except Exception:
        return False


def _wait_for_wechat_open_panel(timeout_seconds: float = 2.5) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if _wechat_has_open_panel():
            return True
        time.sleep(0.1)
    return False


def prepare_wechat_file(chat_name: str, file_reference: str) -> str:
    resolved_file = resolve_file_reference(file_reference)
    previous_clipboard = _get_clipboard()
    try:
        _open_wechat_chat(chat_name)
        # Safer path: enter the target chat first, then copy the actual file
        # object from Finder and paste it into the active WeChat input area.
        # This prepares a real file card in the chat input without sending it.
        subprocess.run(["open", "-R", str(resolved_file)], check=True)
        time.sleep(0.9)
        _wechat_keycode(8, using="command down")  # Cmd+C in Finder
        time.sleep(0.35)
        _activate_wechat()
        _ensure_wechat_frontmost()
        _focus_wechat_input_area()
        _wechat_keycode(9, using="command down")  # Cmd+V
        time.sleep(0.5)
    finally:
        _set_clipboard(previous_clipboard)
    return f"Prepared WeChat file in {chat_name}: {resolved_file}"


def send_wechat_file(chat_name: str, file_reference: str) -> str:
    prepared = prepare_wechat_file(chat_name, file_reference)
    _wechat_keycode(36)  # Enter to send the prepared file card
    time.sleep(0.35)
    return prepared.replace("Prepared", "Sent")


class LocalController:
    """Minimal controller to execute bash and python code locally.

    WARNING: Executing arbitrary code is dangerous. Only enable/use this in trusted
    environments and with trusted inputs.
    """

    def run_bash_script(self, code: str, timeout: int = 30) -> Dict:
        try:
            proc = subprocess.run(
                ["/bin/bash", "-lc", code],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = (proc.stdout or "") + (proc.stderr or "")

            print("BASH OUTPUT =======================================")
            print(output)
            print("BASH OUTPUT =======================================")

            return {
                "status": "ok" if proc.returncode == 0 else "error",
                "returncode": proc.returncode,
                "output": output,
                "error": "",
            }
        except subprocess.TimeoutExpired as e:
            return {
                "status": "error",
                "returncode": -1,
                "output": e.stdout or "",
                "error": f"TimeoutExpired: {str(e)}",
            }
        except Exception as e:
            return {
                "status": "error",
                "returncode": -1,
                "output": "",
                "error": str(e),
            }

    def run_python_script(self, code: str) -> Dict:
        try:
            helper_preamble = """
from gui_agents.s3.utils.local_env import (
    copy_file,
    draft_current_wechat_input,
    draft_wechat_text,
    move_file,
    prepare_wechat_file,
    rename_file,
    reveal_in_finder,
    send_wechat_file,
    send_wechat_text,
    send_wechat_emoji,
)
from gui_agents.s3.utils.wechat_watchdog import (
    get_wechat_watchdog_status,
    start_wechat_watchdog,
    stop_wechat_watchdog,
)
"""
            proc = subprocess.run(
                [sys.executable, "-c", helper_preamble + "\n" + code],
                capture_output=True,
                text=True,
            )
            print("PYTHON OUTPUT =======================================")
            print(proc.stdout or "")
            print("PYTHON OUTPUT =======================================")
            return {
                "status": "ok" if proc.returncode == 0 else "error",
                "return_code": proc.returncode,
                "output": proc.stdout or "",
                "error": proc.stderr or "",
            }
        except Exception as e:
            return {
                "status": "error",
                "return_code": -1,
                "output": "",
                "error": str(e),
            }


class LocalEnv:
    """Simple environment that provides a controller compatible with CodeAgent."""

    def __init__(self):
        self.controller = LocalController()
