#!/usr/bin/env python3
import argparse
import subprocess
import sys
import time

import pyautogui


WECHAT_BUNDLE_ID = "com.tencent.xinWeChat"
WECHAT_PROCESS_NAMES = {"微信", "WeChat"}


def applescript_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run_osascript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def activate_wechat() -> None:
    run_osascript(f'tell application id "{WECHAT_BUNDLE_ID}" to activate')
    time.sleep(0.8)


def get_frontmost_app() -> str:
    script = """
    tell application "System Events"
        set frontApp to first application process whose frontmost is true
        return name of frontApp
    end tell
    """
    return run_osascript(script)


def ensure_wechat_frontmost() -> None:
    front_app = get_frontmost_app()
    if front_app not in WECHAT_PROCESS_NAMES:
        raise RuntimeError(
            f"WeChat is not frontmost. Current frontmost app: {front_app}"
        )


def get_wechat_window_bounds() -> tuple[int, int, int, int]:
    script = f"""
    tell application "System Events"
        tell application process "微信"
            set frontmost to true
            tell front window
                set {{xPos, yPos}} to position
                set {{winW, winH}} to size
                return (xPos as text) & "," & (yPos as text) & "," & (winW as text) & "," & (winH as text)
            end tell
        end tell
    end tell
    """
    raw = run_osascript(script)
    x_pos, y_pos, win_w, win_h = (int(part) for part in raw.split(","))
    return x_pos, y_pos, win_w, win_h


def keystroke(text: str) -> None:
    script = f'''
    tell application "System Events"
        keystroke {applescript_quote(text)}
    end tell
    '''
    run_osascript(script)


def keycode(code: int, using: str | None = None) -> None:
    if using:
        script = f'''
        tell application "System Events"
            key code {code} using {using}
        end tell
        '''
    else:
        script = f'''
        tell application "System Events"
            key code {code}
        end tell
        '''
    run_osascript(script)


def set_clipboard(text: str) -> None:
    subprocess.run(["pbcopy"], input=text, text=True, check=True)


def get_clipboard() -> str:
    result = subprocess.run(["pbpaste"], check=True, capture_output=True, text=True)
    return result.stdout


def open_chat(chat_name: str) -> None:
    activate_wechat()
    ensure_wechat_frontmost()
    keycode(3, using="command down")  # Cmd+F
    time.sleep(0.4)
    ensure_wechat_frontmost()
    keycode(0, using="command down")  # Cmd+A
    time.sleep(0.1)
    ensure_wechat_frontmost()
    keystroke(chat_name)
    time.sleep(0.8)
    ensure_wechat_frontmost()
    keycode(36)  # Enter
    time.sleep(0.8)
    ensure_wechat_frontmost()


def focus_input_area() -> None:
    ensure_wechat_frontmost()
    x_pos, y_pos, win_w, win_h = get_wechat_window_bounds()
    click_x = round(x_pos + win_w * 0.70)
    click_y = round(y_pos + win_h * 0.92)
    pyautogui.click(click_x, click_y)
    time.sleep(0.2)
    pyautogui.click(click_x, click_y)
    time.sleep(0.3)


def paste_message(message: str) -> None:
    ensure_wechat_frontmost()
    original_clipboard = get_clipboard()
    try:
        set_clipboard(message)
        keycode(9, using="command down")  # Cmd+V
        time.sleep(0.2)
    finally:
        set_clipboard(original_clipboard)


def send_message(chat_name: str, message: str, dry_run: bool) -> None:
    open_chat(chat_name)
    focus_input_area()
    paste_message(message)
    if not dry_run:
        ensure_wechat_frontmost()
        keycode(36)  # Enter


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Open a WeChat chat and send a message more reliably."
    )
    parser.add_argument("chat_name", help="Target WeChat chat name")
    parser.add_argument("message", help="Message text")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fill the message into the input box but do not send it",
    )
    args = parser.parse_args()

    pyautogui.PAUSE = 0.1
    pyautogui.FAILSAFE = True

    try:
        send_message(args.chat_name, args.message, args.dry_run)
    except Exception as exc:
        print(f"send_wechat_message failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
