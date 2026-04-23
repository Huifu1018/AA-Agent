import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import anthropic
import pyautogui
import pytesseract
from PIL import Image, ImageOps
from Quartz import (
    CGDataProviderCopyData,
    CGImageGetBitsPerPixel,
    CGImageGetBytesPerRow,
    CGImageGetDataProvider,
    CGImageGetHeight,
    CGImageGetWidth,
    CGWindowListCopyWindowInfo,
    CGWindowListCreateImage,
    CGRectNull,
    kCGNullWindowID,
    kCGWindowImageBoundsIgnoreFraming,
    kCGWindowListOptionAll,
    kCGWindowListOptionIncludingWindow,
)

from gui_agents.s3.utils import local_env


WATCHDOG_DIR = Path(__file__).resolve().parents[3] / "logs" / "wechat_watchdog"
WATCHDOG_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_LOG_PATH = WATCHDOG_DIR / "wechat_personal_messages.md"
DEFAULT_STATE_PATH = WATCHDOG_DIR / "wechat_watchdog_state.json"
DEBUG_SCAN_PATH = WATCHDOG_DIR / "wechat_watchdog_debug.json"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clean_text(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text.replace("\x0c", " ")).strip()
    return cleaned


def _normalize_name(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", (text or "")).lower()


def _row_signature(title: str, preview: str) -> str:
    payload = f"{_clean_text(title)}|{_clean_text(preview)}".encode("utf-8", errors="ignore")
    return hashlib.sha1(payload).hexdigest()


def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def _ocr_image(image, psm: int = 6) -> str:
    config = f"--psm {psm}"
    text = pytesseract.image_to_string(image, lang="chi_sim+eng", config=config)
    return _clean_text(text)


def _prepare_ocr_image(image: Image.Image, scale: int = 3) -> Image.Image:
    gray = image.convert("L")
    autocontrast = ImageOps.autocontrast(gray)
    enlarged = autocontrast.resize(
        (autocontrast.width * scale, autocontrast.height * scale),
        Image.Resampling.LANCZOS,
    )
    thresholded = enlarged.point(lambda px: 255 if px > 170 else 0)
    return thresholded


def _contact_name_score(text: str) -> int:
    text = (text or "").strip()
    if not text:
        return -100
    score = 0
    chinese_chars = re.findall(r"[\u4e00-\u9fff]", text)
    alpha_num = re.findall(r"[A-Za-z0-9]", text)
    punct = re.findall(r"[;|`~<>{}\\/_]", text)
    score += len(chinese_chars) * 6
    score += len(alpha_num) * 2
    score -= len(punct) * 8
    if 2 <= len(text) <= 12:
        score += 8
    if _looks_like_reasonable_contact_name(text):
        score += 20
    return score


def _best_contact_name(image: Image.Image) -> str:
    candidates = []
    for scale in (3, 4):
        prepared = _prepare_ocr_image(image, scale=scale)
        for psm in (7, 6, 13):
            text = _ocr_image(prepared, psm=psm)
            if text:
                candidates.append(text)
    if not candidates:
        return ""
    candidates = sorted(set(candidates), key=_contact_name_score, reverse=True)
    return candidates[0]


def _best_preview_text(image: Image.Image) -> str:
    candidates = []
    for scale in (2, 3):
        prepared = _prepare_ocr_image(image, scale=scale)
        for psm in (7, 6):
            text = _ocr_image(prepared, psm=psm)
            if text:
                candidates.append(text)
    if not candidates:
        return ""
    candidates = sorted(set(candidates), key=lambda text: (len(text), text), reverse=True)
    return candidates[0]


def _red_pixel_count(image) -> int:
    red_pixels = 0
    for red, green, blue in image.convert("RGB").getdata():
        if red >= 180 and green <= 120 and blue <= 120:
            red_pixels += 1
    return red_pixels


def _is_red_pixel(red: int, green: int, blue: int) -> bool:
    return red >= 180 and green <= 120 and blue <= 120


def _looks_like_reasonable_contact_name(title: str) -> bool:
    title = (title or "").strip()
    if len(title) < 2 or len(title) > 24:
        return False
    if re.fullmatch(r"[\W_]+", title):
        return False
    if re.search(r"[;|`~]+", title):
        return False
    meaningful_chars = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", title)
    if len(meaningful_chars) < 2:
        return False
    alpha_num = "".join(meaningful_chars)
    if not _has_cjk(title):
        if len(alpha_num) < 3:
            return False
        if alpha_num.isalpha() and alpha_num.islower() and len(alpha_num) <= 3:
            return False
    if len(alpha_num) <= 4 and alpha_num.lower() in {"cn", "rs", "as", "mm", "aaa", "pare"}:
        return False
    return True


def _is_probably_personal_chat(title: str, preview: str) -> bool:
    if not title:
        return False
    if not _looks_like_reasonable_contact_name(title):
        return False
    if title in {"文件传输助手", "File Transfer Assistant"}:
        return False
    if any(
        keyword in title
        for keyword in (
            "公众号",
            "订阅号",
            "服务号",
            "微信团队",
            "社区",
            "公司",
            "平台",
            "大厦",
            "大楼",
            "物业",
            "办公室",
            "coreteam",
            "manager",
        )
    ):
        return False
    if re.search(r"[（(]\d+[)）]", title):
        return False
    if any(keyword in title for keyword in ("群", "服务平台", "项目研发", "交流", "通知", "公告")):
        return False
    if any(keyword in preview for keyword in ("公众号", "服务号", "订阅号")):
        return False
    if re.match(r"^[^:：]{1,10}[:：]", preview):
        return False
    return True


def _personal_chat_rejection_reason(title: str, preview: str) -> str:
    if not title:
        return "empty_title"
    if not _looks_like_reasonable_contact_name(title):
        return "bad_contact_name"
    if title in {"文件传输助手", "File Transfer Assistant"}:
        return "file_transfer_assistant"
    if any(
        keyword in title
        for keyword in (
            "公众号",
            "订阅号",
            "服务号",
            "微信团队",
            "社区",
            "公司",
            "平台",
            "大厦",
            "大楼",
            "物业",
            "办公室",
            "coreteam",
            "manager",
        )
    ):
        return "official_account"
    if re.search(r"[（(]\d+[)）]", title):
        return "group_counter"
    if any(keyword in title for keyword in ("群", "服务平台", "项目研发", "交流", "通知", "公告")):
        return "group_or_service_title"
    if any(keyword in preview for keyword in ("公众号", "服务号", "订阅号")):
        return "official_account_preview"
    if re.match(r"^[^:：]{1,10}[:：]", preview):
        return "group_like_preview"
    return ""


def _largest_wechat_window_bounds() -> tuple[int, int, int, int]:
    raw = local_env._run_osascript(  # noqa: SLF001
        """
        tell application "System Events"
            set appName to ""
            if exists application process "WeChat" then
                set appName to "WeChat"
            else if exists application process "微信" then
                set appName to "微信"
            else
                error "WeChat is not running."
            end if
            tell application process appName
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
    )
    x_pos, y_pos, win_w, win_h = (int(part) for part in raw.split(","))
    return x_pos, y_pos, win_w, win_h


def _find_wechat_chat_window_id() -> Optional[int]:
    windows = CGWindowListCopyWindowInfo(kCGWindowListOptionAll, kCGNullWindowID) or []
    best_window = None
    best_area = 0
    for window in windows:
        owner = str(window.get("kCGWindowOwnerName", ""))
        name = str(window.get("kCGWindowName", ""))
        if owner != "微信":
            continue
        bounds = window.get("kCGWindowBounds") or {}
        width = int(bounds.get("Width", 0) or 0)
        height = int(bounds.get("Height", 0) or 0)
        if width < 500 or height < 400:
            continue
        # Prefer the actual chat/content window instead of menu-bar / helper windows.
        if name and "窗口" not in name and "WeChat" not in name and "微信" not in name:
            continue
        area = width * height
        if area > best_area:
            best_area = area
            best_window = window
    if not best_window:
        return None
    return int(best_window["kCGWindowNumber"])


def _capture_window_image(window_id: int) -> Optional[Image.Image]:
    cg_image = CGWindowListCreateImage(
        CGRectNull,
        kCGWindowListOptionIncludingWindow,
        window_id,
        kCGWindowImageBoundsIgnoreFraming,
    )
    if cg_image is None:
        return None
    width = CGImageGetWidth(cg_image)
    height = CGImageGetHeight(cg_image)
    bytes_per_row = CGImageGetBytesPerRow(cg_image)
    bits_per_pixel = CGImageGetBitsPerPixel(cg_image)
    provider = CGImageGetDataProvider(cg_image)
    data = CGDataProviderCopyData(provider)
    mode = "RGBA" if bits_per_pixel == 32 else "RGB"
    image = Image.frombuffer(
        mode,
        (width, height),
        bytes(data),
        "raw",
        "BGRA" if mode == "RGBA" else "BGR",
        bytes_per_row,
        1,
    )
    return image.convert("RGB")


def _capture_frontmost_wechat_image() -> Image.Image:
    local_env._ensure_wechat_frontmost()  # noqa: SLF001
    x_pos, y_pos, win_w, win_h = local_env._get_wechat_window_bounds()  # noqa: SLF001
    return pyautogui.screenshot(region=(x_pos, y_pos, win_w, win_h)).convert("RGB")


def _capture_detection_wechat_image() -> Optional[Image.Image]:
    try:
        return _capture_frontmost_wechat_image()
    except Exception:
        return None


def _capture_unread_candidates() -> tuple[list[dict], list[dict]]:
    window_id = _find_wechat_chat_window_id()
    if not window_id:
        raise RuntimeError("No accessible WeChat chat window found for watchdog capture.")
    screenshot = _capture_window_image(window_id)
    if screenshot is None:
        raise RuntimeError("Failed to capture WeChat window image.")
    win_w, win_h = screenshot.size

    sidebar_w = round(win_w * 0.35)
    # The chat list starts noticeably higher than the previous heuristic,
    # so use a shallower top offset to avoid clicking the next row down.
    list_top = round(win_h * 0.07)
    row_h = max(70, min(98, round(win_h * 0.056)))
    row_count = max(8, min(14, round((win_h * 0.76) / row_h)))

    candidates: list[dict] = []
    debug_rows: list[dict] = []
    for index in range(row_count):
        row_top = list_top + index * row_h
        if row_top + row_h > win_h:
            break
        row_img = screenshot.crop((0, row_top, sidebar_w, row_top + row_h))
        badge_img = row_img.crop(
            (
                round(sidebar_w * 0.03),
                round(row_h * 0.10),
                round(sidebar_w * 0.16),
                round(row_h * 0.55),
            )
        )
        red_count = _red_pixel_count(badge_img)
        if red_count < 90:
            debug_rows.append(
                {
                    "row_index": index,
                    "red_count": red_count,
                    "accepted": False,
                    "reason": "red_count_too_low",
                    "title": "",
                    "preview": "",
                }
            )
            continue

        title_img = row_img.crop(
            (
                round(sidebar_w * 0.20),
                round(row_h * 0.05),
                round(sidebar_w * 0.78),
                round(row_h * 0.46),
            )
        )
        preview_img = row_img.crop(
            (
                round(sidebar_w * 0.20),
                round(row_h * 0.42),
                round(sidebar_w * 0.85),
                round(row_h * 0.90),
            )
        )
        title = _best_contact_name(title_img)
        preview = _best_preview_text(preview_img)
        rejection_reason = _personal_chat_rejection_reason(title, preview)
        if rejection_reason:
            debug_rows.append(
                {
                    "row_index": index,
                    "red_count": red_count,
                    "accepted": False,
                    "reason": rejection_reason,
                    "title": title,
                    "preview": preview,
                    "title_score": _contact_name_score(title),
                    "row_top": row_top,
                    "row_center_y": row_top + round(row_h * 0.5),
                }
            )
            continue

        title_score = _contact_name_score(title)
        if not _has_cjk(title) and title_score < 40:
            debug_rows.append(
                {
                    "row_index": index,
                    "red_count": red_count,
                    "accepted": False,
                    "reason": "low_confidence_title",
                    "title": title,
                    "preview": preview,
                    "title_score": title_score,
                    "row_top": row_top,
                    "row_center_y": row_top + round(row_h * 0.5),
                }
            )
            continue

        candidate = {
            "title": title,
            "preview": preview,
            "red_count": red_count,
            "title_score": title_score,
            "row_center_x": round(sidebar_w * 0.50),
            "row_center_y": row_top + round(row_h * 0.5),
        }
        candidates.append(candidate)
        debug_rows.append(
                {
                    "row_index": index,
                    "red_count": red_count,
                    "accepted": True,
                    "reason": "accepted",
                    "title": title,
                    "preview": preview,
                    "title_score": _contact_name_score(title),
                    "row_top": row_top,
                    "row_center_y": row_top + round(row_h * 0.5),
                }
            )
    candidates.sort(key=lambda item: (item["title_score"], item["red_count"]), reverse=True)
    return candidates, debug_rows


def _open_candidate_chat(candidate: dict) -> bool:
    local_env._activate_wechat()  # noqa: SLF001
    x_pos, y_pos, _, _ = _largest_wechat_window_bounds()
    pyautogui.click(x_pos + candidate["row_center_x"], y_pos + candidate["row_center_y"])
    time.sleep(0.55)
    try:
        current_title = _clean_text(local_env._get_wechat_chat_title())  # noqa: SLF001
    except Exception:
        return False
    expected = _normalize_name(candidate["title"])
    actual = _normalize_name(current_title)
    if not expected or not actual:
        return False
    return expected in actual or actual in expected


def _capture_recent_chat_text() -> str:
    screenshot = _capture_frontmost_wechat_image()
    win_w, win_h = screenshot.size
    chat_img = screenshot.crop(
        (
            round(win_w * 0.36),
            round(win_h * 0.12),
            round(win_w * 0.96),
            round(win_h * 0.82),
        )
    )
    return _ocr_image(chat_img, psm=6)


def _message_fingerprint(title: str, content: str) -> str:
    payload = f"{title}\n{content}".encode("utf-8", errors="ignore")
    return hashlib.sha1(payload).hexdigest()


def _append_markdown(log_path: Path, title: str, preview: str, content: str) -> None:
    _append_markdown_with_assessment(log_path, title, preview, content, None)


def _append_markdown_with_assessment(
    log_path: Path,
    title: str,
    preview: str,
    content: str,
    assessment: Optional[dict],
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"## {_now()}\n")
        handle.write(f"- 联系人: {title}\n")
        handle.write(f"- 预览: {preview or '无'}\n")
        handle.write("- 内容:\n\n")
        handle.write("```\n")
        handle.write((content or "").strip() + "\n")
        handle.write("```\n\n")
        if assessment:
            handle.write(f"- 建议回复: {assessment.get('need_reply', 'unknown')}\n")
            handle.write(f"- 判断理由: {assessment.get('reason', '无')}\n")
            handle.write(f"- 草稿已写入输入框: {assessment.get('drafted_to_input', 'no')}\n")
            draft = (assessment.get("draft_reply") or "").strip()
            if draft:
                handle.write("- 建议回复草稿:\n\n")
                handle.write("```\n")
                handle.write(draft + "\n")
                handle.write("```\n\n")


def _capture_unread_candidates_v2() -> tuple[list[dict], list[dict]]:
    screenshot = _capture_detection_wechat_image()
    if screenshot is None:
        return [], []
    win_w, win_h = screenshot.size

    # WeChat desktop chat list is much narrower than the previous heuristic.
    # A too-wide crop pulls in the right-hand chat content, which corrupts OCR.
    list_left = round(win_w * 0.02)
    list_right = round(win_w * 0.20)
    list_top = round(win_h * 0.045)
    list_bottom = round(win_h * 0.92)
    list_width = list_right - list_left
    list_height = list_bottom - list_top
    row_h = max(78, min(96, round(win_h * 0.074)))

    list_img = screenshot.crop((list_left, list_top, list_right, list_bottom)).convert("RGB")
    badge_scan_left = round(list_width * 0.00)
    badge_scan_right = round(list_width * 0.20)

    row_count = max(8, min(14, round(list_height / row_h)))

    candidates: list[dict] = []
    debug_rows: list[dict] = []
    for index in range(row_count):
        row_top_local = index * row_h
        row_bottom_local = min(list_height, row_top_local + row_h)
        if row_bottom_local - row_top_local < 40:
            continue
        row_img = list_img.crop((0, row_top_local, list_width, row_bottom_local))
        badge_img = row_img.crop(
            (
                badge_scan_left,
                round(row_img.height * 0.08),
                badge_scan_right,
                round(row_img.height * 0.60),
            )
        )
        red_count = _red_pixel_count(badge_img)
        title_img = row_img.crop(
            (
                round(list_width * 0.20),
                round(row_img.height * 0.05),
                round(list_width * 0.80),
                round(row_img.height * 0.46),
            )
        )
        preview_img = row_img.crop(
            (
                round(list_width * 0.20),
                round(row_img.height * 0.42),
                round(list_width * 0.90),
                round(row_img.height * 0.90),
            )
        )
        title_hint = _best_contact_name(title_img)
        preview_hint = _best_preview_text(preview_img)
        row_center_y = list_top + row_top_local + round((row_bottom_local - row_top_local) * 0.5)
        accepted = red_count >= 10
        debug_rows.append(
            {
                "row_index": index,
                "red_count": red_count,
                "accepted": accepted,
                "reason": "accepted_by_row_badge" if accepted else "red_count_too_low",
                "title": title_hint,
                "preview": preview_hint,
                "title_score": _contact_name_score(title_hint),
                "row_top": list_top + row_top_local,
                "row_center_x": list_left + round(list_width * 0.34),
                "row_center_y": row_center_y,
            }
        )
        if accepted:
            candidates.append(
                {
                    "title_hint": title_hint,
                    "preview_hint": preview_hint,
                    "red_count": red_count,
                    "row_center_x": list_left + round(list_width * 0.34),
                    "row_center_y": row_center_y,
                }
            )
    candidates.sort(key=lambda item: item["row_center_y"])
    return candidates, debug_rows


def _open_candidate_chat_v2(candidate: dict) -> str:
    local_env._activate_wechat()  # noqa: SLF001
    pyautogui.moveTo(candidate["row_center_x"], candidate["row_center_y"])
    pyautogui.click(candidate["row_center_x"], candidate["row_center_y"])
    time.sleep(0.55)
    try:
        return _clean_text(local_env._get_wechat_chat_title())  # noqa: SLF001
    except Exception:
        return ""


def _scroll_wechat_chat_list(direction: str = "up", amount: int = 700) -> None:
    local_env._ensure_wechat_frontmost()  # noqa: SLF001
    x_pos, y_pos, win_w, win_h = local_env._get_wechat_window_bounds()  # noqa: SLF001
    scroll_x = round(x_pos + win_w * 0.12)
    scroll_y = round(y_pos + win_h * 0.45)
    pyautogui.moveTo(scroll_x, scroll_y)
    pyautogui.scroll(amount if direction == "up" else -amount)
    time.sleep(0.5)


@dataclass
class WatchdogSnapshot:
    running: bool = False
    interval_seconds: int = 20
    log_path: str = str(DEFAULT_LOG_PATH)
    last_scan_at: str = ""
    last_detection_at: str = ""
    last_error: str = ""
    last_skip_reason: str = ""
    last_message_contact: str = ""
    last_unread_candidates: str = ""
    last_reply_assessment: str = ""
    detections: int = 0


class WeChatWatchdogService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._snapshot = WatchdogSnapshot()
        self._state_path = DEFAULT_STATE_PATH
        self._debug_path = DEBUG_SCAN_PATH
        self._seen_keys: dict[str, str] = {}
        self._candidate_hits: dict[str, int] = {}
        self._row_signatures: dict[str, str] = {}
        self._row_positions: dict[str, int] = {}

    def _snapshot_dict_unlocked(self) -> dict:
        alive = bool(self._thread and self._thread.is_alive() and not self._stop_event.is_set())
        self._snapshot.running = alive
        return {
            "running": self._snapshot.running,
            "interval_seconds": self._snapshot.interval_seconds,
            "log_path": self._snapshot.log_path,
            "last_scan_at": self._snapshot.last_scan_at,
            "last_detection_at": self._snapshot.last_detection_at,
            "last_error": self._snapshot.last_error,
            "last_skip_reason": self._snapshot.last_skip_reason,
            "last_message_contact": self._snapshot.last_message_contact,
            "last_unread_candidates": self._snapshot.last_unread_candidates,
            "last_reply_assessment": self._snapshot.last_reply_assessment,
            "detections": self._snapshot.detections,
        }

    def _load_state(self) -> None:
        if self._state_path.exists():
            try:
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._seen_keys = {
                        str(key): str(value) for key, value in data.get("seen_keys", {}).items()
                    }
                    self._candidate_hits = {
                        str(key): int(value) for key, value in data.get("candidate_hits", {}).items()
                    }
                    self._row_signatures = {
                        str(key): str(value) for key, value in data.get("row_signatures", {}).items()
                    }
                    self._row_positions = {
                        str(key): int(value) for key, value in data.get("row_positions", {}).items()
                    }
            except Exception:
                self._seen_keys = {}
                self._candidate_hits = {}
                self._row_signatures = {}
                self._row_positions = {}

    def _save_state(self) -> None:
        payload = {
            "seen_keys": self._seen_keys,
            "candidate_hits": self._candidate_hits,
            "row_signatures": self._row_signatures,
            "row_positions": self._row_positions,
        }
        self._state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _save_debug_scan(self, rows: list[dict]) -> None:
        payload = {
            "saved_at": _now(),
            "rows": rows,
        }
        self._debug_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def start(self, interval_seconds: int = 20, log_path: Optional[str] = None) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self._snapshot_dict_unlocked()
            self._stop_event.clear()
            self._snapshot = WatchdogSnapshot(
                running=True,
                interval_seconds=max(10, int(interval_seconds)),
                log_path=str(Path(log_path).expanduser().resolve()) if log_path else str(DEFAULT_LOG_PATH),
            )
            self._load_state()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            return self._snapshot_dict_unlocked()

    def stop(self) -> dict:
        with self._lock:
            self._stop_event.set()
            self._snapshot.running = False
            return self._snapshot_dict_unlocked()

    def status(self) -> dict:
        with self._lock:
            return self._snapshot_dict_unlocked()

    def _assess_reply_need(self, title: str, content: str) -> dict:
        api_key = os.getenv("KIMI_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            return {
                "need_reply": "unknown",
                "reason": "No Kimi/Anthropic API key available for reply assessment.",
                "draft_reply": "",
            }
        model = os.getenv("KIMI_MODEL", os.getenv("ANTHROPIC_MODEL", "kimi-k2.5"))
        base_url = os.getenv("KIMI_BASE_URL", os.getenv("ANTHROPIC_BASE_URL", "https://api.moonshot.cn/anthropic"))
        client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
        prompt = f"""你在做微信消息值守。请判断下面这条来自个人联系人的最新消息是否需要回复。\n\n联系人：{title}\n消息正文：{content}\n\n请只用下面格式回答：\nNEED_REPLY: yes 或 no\nREASON: 一句简短中文理由\nDRAFT_REPLY: 如果需要回复，给一句简短中文建议回复；如果不需要，写 N/A"""
        response = client.messages.create(
            model=model,
            max_tokens=220,
            messages=[{"role": "user", "content": prompt}],
        )
        text_parts = []
        for block in response.content:
            block_text = getattr(block, "text", "")
            if block_text:
                text_parts.append(block_text)
        text = "\n".join(text_parts).strip()
        need_reply = "unknown"
        reason = ""
        draft_reply = ""
        for line in text.splitlines():
            if line.startswith("NEED_REPLY:"):
                value = line.split(":", 1)[1].strip().lower()
                need_reply = "yes" if value.startswith("y") else "no" if value.startswith("n") else "unknown"
            elif line.startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()
            elif line.startswith("DRAFT_REPLY:"):
                draft_reply = line.split(":", 1)[1].strip()
        return {
            "need_reply": need_reply,
            "reason": reason or "No reason returned.",
            "draft_reply": "" if draft_reply == "N/A" else draft_reply,
            "drafted_to_input": "no",
        }

    def _record_detection(self, title: str, preview: str, content: str, assessment: Optional[dict]) -> None:
        log_path = Path(self._snapshot.log_path)
        _append_markdown_with_assessment(log_path, title, preview, content, assessment)
        self._snapshot.last_detection_at = _now()
        self._snapshot.last_message_contact = title
        if assessment:
            self._snapshot.last_reply_assessment = (
                f"{title}: need_reply={assessment.get('need_reply', 'unknown')}; "
                f"reason={assessment.get('reason', '')}; "
                f"drafted={assessment.get('drafted_to_input', 'no')}"
            )
        self._snapshot.detections += 1

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            previous_front_app = ""
            try:
                previous_front_app = local_env._get_frontmost_app()  # noqa: SLF001
                local_env._activate_wechat()  # noqa: SLF001
                candidates, debug_rows = _capture_unread_candidates_v2()
                if not candidates:
                    combined_rows = list(debug_rows)
                    for scroll_attempt in range(2):
                        _scroll_wechat_chat_list(direction="up", amount=700)
                        more_candidates, more_rows = _capture_unread_candidates_v2()
                        for row in more_rows:
                            row["scan_pass"] = scroll_attempt + 2
                        combined_rows.extend(more_rows)
                        if more_candidates:
                            candidates = more_candidates
                            debug_rows = combined_rows
                            break
                    else:
                        debug_rows = combined_rows
                enriched_candidates: list[dict] = []
                next_row_signatures: dict[str, str] = {}
                next_row_positions: dict[str, int] = {}
                reason_by_key: dict[str, str] = {}
                for row in debug_rows:
                    row_key = f"row_{row.get('row_index', -1)}"
                    title = row.get("title", "")
                    preview = row.get("preview", "")
                    signature = _row_signature(title, preview)
                    next_row_signatures[row_key] = signature
                    next_row_positions[row_key] = int(row.get("row_index", -1))
                    previous_signature = self._row_signatures.get(row_key)
                    previous_position = self._row_positions.get(row_key)
                    changed = bool(previous_signature and previous_signature != signature)
                    moved_up = previous_position is not None and previous_position - int(row.get("row_index", -1)) >= 2
                    accepted = bool(row.get("accepted"))
                    reason = row.get("reason", "")
                    title_score = _contact_name_score(title)
                    title_looks_valid = _looks_like_reasonable_contact_name(title)
                    preview_looks_group_like = bool(re.match(r"^[^:：]{1,10}[:：]", preview or ""))
                    content_change_eligible = (
                        int(row.get("row_index", 99)) <= 4
                        and bool(preview)
                        and not preview_looks_group_like
                        and title_looks_valid
                        and (_has_cjk(title) or title_score >= 55)
                    )
                    if not accepted and content_change_eligible and (changed or moved_up):
                        accepted = True
                        if changed and moved_up:
                            reason = "accepted_by_content_change_and_position_jump"
                        elif changed:
                            reason = "accepted_by_content_change"
                        else:
                            reason = "accepted_by_position_jump"
                    elif not accepted and (changed or moved_up):
                        if not title_looks_valid:
                            reason = "content_changed_but_bad_contact_name"
                        elif preview_looks_group_like:
                            reason = "content_changed_but_group_like_preview"
                        else:
                            reason = "content_changed_but_low_confidence_title"
                    reason_by_key[row_key] = reason
                    row["accepted"] = accepted
                    row["reason"] = reason
                    if accepted:
                        enriched_candidates.append(
                            {
                                "title_hint": title,
                                "preview_hint": preview,
                                "red_count": row.get("red_count", 0),
                                "row_center_x": round((row.get("row_center_x") or 0)),
                                "row_center_y": row.get("row_center_y", 0),
                                "row_index": row.get("row_index", -1),
                                "row_key": row_key,
                            }
                        )
                if not candidates:
                    candidates = enriched_candidates
                else:
                    existing_indexes = {candidate.get("row_center_y") for candidate in candidates}
                    for candidate in enriched_candidates:
                        if candidate.get("row_center_y") not in existing_indexes:
                            candidates.append(candidate)
                candidates.sort(key=lambda item: item["row_center_y"])
                safe_candidates = []
                for candidate in candidates:
                    title_hint = _clean_text(candidate.get("title_hint", ""))
                    preview_hint = _clean_text(candidate.get("preview_hint", ""))
                    title_score = _contact_name_score(title_hint)
                    if not _looks_like_reasonable_contact_name(title_hint):
                        continue
                    if not (_has_cjk(title_hint) or title_score >= 55):
                        continue
                    if not _is_probably_personal_chat(title_hint, preview_hint):
                        continue
                    safe_candidates.append(candidate)
                candidates = safe_candidates
                self._save_debug_scan(debug_rows)
                self._snapshot.last_scan_at = _now()
                self._snapshot.last_error = ""
                self._snapshot.last_unread_candidates = ", ".join(
                    candidate.get("title_hint", "") or f"row@{candidate['row_center_y']}"
                    for candidate in candidates[:8]
                )
                if not candidates:
                    self._snapshot.last_skip_reason = "No unread candidates detected during foreground WeChat check."
                    self._row_signatures = next_row_signatures
                    self._row_positions = next_row_positions
                    self._save_state()
                    if previous_front_app not in local_env.WECHAT_PROCESS_NAMES:  # noqa: SLF001
                        local_env._activate_app_by_name(previous_front_app)  # noqa: SLF001
                    self._stop_event.wait(self._snapshot.interval_seconds)
                    continue
                self._snapshot.last_skip_reason = "Unread chats confirmed. Processing in WeChat one by one."
                for candidate in candidates:
                    actual_title = _open_candidate_chat_v2(candidate)
                    if not actual_title:
                        continue
                    preview_text = candidate.get("preview_hint", "")
                    if not _is_probably_personal_chat(actual_title, preview_text):
                        continue
                    content = _capture_recent_chat_text()
                    if not content:
                        continue
                    fingerprint = _message_fingerprint(actual_title, content)
                    if self._seen_keys.get(actual_title) == fingerprint:
                        continue
                    assessment = self._assess_reply_need(actual_title, content)
                    answer_text = "Yes" if assessment.get("need_reply") == "yes" else "No"
                    local_env.draft_current_wechat_input(answer_text, replace_existing=True)
                    assessment["drafted_to_input"] = "yes"
                    assessment["draft_reply"] = answer_text
                    self._seen_keys[actual_title] = fingerprint
                    self._save_state()
                    self._record_detection(actual_title, preview_text, content, assessment)
                self._row_signatures = next_row_signatures
                self._row_positions = next_row_positions
                self._save_state()
                if previous_front_app not in local_env.WECHAT_PROCESS_NAMES:  # noqa: SLF001
                    local_env._activate_app_by_name(previous_front_app)  # noqa: SLF001
            except Exception as exc:
                self._snapshot.last_scan_at = _now()
                self._snapshot.last_error = str(exc)
                self._snapshot.last_skip_reason = ""
                if previous_front_app and previous_front_app not in local_env.WECHAT_PROCESS_NAMES:  # noqa: SLF001
                    try:
                        local_env._activate_app_by_name(previous_front_app)  # noqa: SLF001
                    except Exception:
                        pass
            self._stop_event.wait(self._snapshot.interval_seconds)


WATCHDOG = WeChatWatchdogService()


def start_wechat_watchdog(interval_seconds: int = 20, log_path: Optional[str] = None) -> dict:
    return WATCHDOG.start(interval_seconds=interval_seconds, log_path=log_path)


def stop_wechat_watchdog() -> dict:
    return WATCHDOG.stop()


def get_wechat_watchdog_status() -> dict:
    return WATCHDOG.status()
