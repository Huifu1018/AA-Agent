import asyncio
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import anthropic
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import Channel, Chat, User


WATCHDOG_DIR = Path(__file__).resolve().parents[3] / "logs" / "telegram_watchdog"
WATCHDOG_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_STATE_PATH = WATCHDOG_DIR / "telegram_watchdog_state.json"
DEFAULT_CONFIG_PATH = WATCHDOG_DIR / "telegram_watchdog_config.json"
DEFAULT_AUTH_PATH = WATCHDOG_DIR / "telegram_watchdog_auth.json"
DEFAULT_LOG_PATH = WATCHDOG_DIR / "telegram_messages.md"
DEFAULT_SESSION_PATH = WATCHDOG_DIR / "tg_watchdog"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clean_text(text: str) -> str:
    return " ".join((text or "").replace("\x0c", " ").split()).strip()


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _default_config() -> dict:
    return {
        "api_id": os.getenv("TG_API_ID", "").strip(),
        "api_hash": os.getenv("TG_API_HASH", "").strip(),
        "phone": os.getenv("TG_PHONE", "").strip(),
        "session_path": str(Path(os.getenv("TG_SESSION_PATH", str(DEFAULT_SESSION_PATH))).expanduser().resolve()),
    }


def _load_config() -> dict:
    if DEFAULT_CONFIG_PATH.exists():
        try:
            payload = _load_json(DEFAULT_CONFIG_PATH)
            return {
                "api_id": str(payload.get("api_id", "")).strip(),
                "api_hash": str(payload.get("api_hash", "")).strip(),
                "phone": str(payload.get("phone", "")).strip(),
                "session_path": str(
                    Path(payload.get("session_path") or str(DEFAULT_SESSION_PATH)).expanduser().resolve()
                ),
            }
        except Exception:
            pass
    return _default_config()


def _save_config(api_id: str, api_hash: str, phone: str) -> dict:
    payload = _load_config()
    payload["api_id"] = str(api_id).strip()
    payload["api_hash"] = str(api_hash).strip()
    payload["phone"] = str(phone).strip()
    _write_json(DEFAULT_CONFIG_PATH, payload)
    return payload


def _load_auth_state() -> dict:
    if not DEFAULT_AUTH_PATH.exists():
        return {}
    try:
        return _load_json(DEFAULT_AUTH_PATH)
    except Exception:
        return {}


def _save_auth_state(payload: dict) -> None:
    _write_json(DEFAULT_AUTH_PATH, payload)


def _session_exists(session_path: str) -> bool:
    return Path(session_path).with_suffix(".session").exists()


def _telethon_client(config: dict) -> TelegramClient:
    api_id = int(str(config.get("api_id", "0") or "0"))
    api_hash = str(config.get("api_hash", "")).strip()
    session_path = str(config.get("session_path") or DEFAULT_SESSION_PATH)
    if not api_id or not api_hash:
        raise RuntimeError("Telegram API_ID / API_HASH 未配置。")
    return TelegramClient(session_path, api_id, api_hash)


def _get_chat_name(chat) -> str:
    if isinstance(chat, User):
        return " ".join(filter(None, [chat.first_name, chat.last_name])).strip() or chat.username or str(chat.id)
    if isinstance(chat, (Chat, Channel)):
        return getattr(chat, "title", "") or getattr(chat, "username", "") or str(chat.id)
    return "未知会话"


def _get_sender_name(sender) -> str:
    if isinstance(sender, User):
        return " ".join(filter(None, [sender.first_name, sender.last_name])).strip() or sender.username or str(sender.id)
    if isinstance(sender, (Chat, Channel)):
        return getattr(sender, "title", "") or getattr(sender, "username", "") or str(sender.id)
    return "未知发送者"


def _media_desc(message) -> str:
    if getattr(message, "photo", None):
        return "[图片]"
    if getattr(message, "video", None):
        return "[视频]"
    if getattr(message, "audio", None):
        return "[音频]"
    if getattr(message, "voice", None):
        return "[语音]"
    if getattr(message, "document", None):
        return "[文件]"
    if getattr(message, "sticker", None):
        return "[贴纸]"
    return ""


def _render_record(record: dict) -> str:
    lines = [
        f"## {record.get('chat_name') or '未命名会话'}",
        f"- 时间: {record.get('timestamp') or ''}",
        f"- 联系人: {record.get('sender_name') or ''}",
        f"- 会话类型: {'群聊/频道' if record.get('is_group') else '私聊'}",
        f"- 消息预览: {record.get('content') or record.get('media') or '空消息'}",
        f"- need_reply: {record.get('need_reply') or 'unknown'}",
        f"- 理由: {record.get('reason') or '暂无'}",
        f"- 建议回复: {record.get('draft_reply') or '暂无'}",
        "",
    ]
    return "\n".join(lines)


def _append_record(log_path: str, record: dict) -> None:
    path = Path(log_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_render_record(record))


def _assess_reply_need(sender: str, chat_name: str, content: str, is_group: bool) -> dict:
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
    scope_rule = (
        "这是群聊或频道消息，除非消息明显直接点名我、明确提问我、或需要我执行动作，否则通常不需要回复。\n"
        if is_group
        else "这是私聊消息，请按正常个人沟通标准判断是否需要回复。\n"
    )
    prompt = (
        "你在做 Telegram 消息值守。请判断下面这条新消息是否需要回复。\n\n"
        f"会话: {chat_name}\n"
        f"发送者: {sender}\n"
        f"消息内容: {content}\n\n"
        f"{scope_rule}"
        "请严格使用下面格式输出：\n"
        "NEED_REPLY: yes 或 no\n"
        "REASON: 一句话中文说明原因\n"
        "DRAFT_REPLY: 如果 need_reply=yes，给出一段可直接发送的中文回复；如果不需要回复，写 N/A\n"
    )
    response = client.messages.create(
        model=model,
        max_tokens=500,
        temperature=0.1,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "\n".join(getattr(block, "text", "") for block in response.content).strip()
    need_reply = "unknown"
    reason = ""
    draft_reply = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        upper = line.upper()
        if upper.startswith("NEED_REPLY:"):
            value = line.split(":", 1)[1].strip().lower()
            need_reply = "yes" if value.startswith("y") else "no" if value.startswith("n") else "unknown"
        elif upper.startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
        elif upper.startswith("DRAFT_REPLY:"):
            draft_reply = line.split(":", 1)[1].strip()
    return {
        "need_reply": need_reply,
        "reason": reason or "暂无",
        "draft_reply": "" if draft_reply == "N/A" else draft_reply,
    }


@dataclass
class TelegramWatchdogSnapshot:
    running: bool = False
    interval_seconds: int = 5
    log_path: str = str(DEFAULT_LOG_PATH)
    last_scan_at: str = ""
    last_detection_at: str = ""
    last_error: str = ""
    last_chat_name: str = ""
    last_sender_name: str = ""
    last_content: str = ""
    last_need_reply: str = ""
    last_reply_reason: str = ""
    last_reply_draft: str = ""
    detections: int = 0
    recent_messages: list[dict] = field(default_factory=list)


class TelegramWatchdog:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client: Optional[TelegramClient] = None
        self._snapshot = TelegramWatchdogSnapshot()
        self._state_path = DEFAULT_STATE_PATH
        self._load_state()

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            payload = _load_json(self._state_path)
        except Exception:
            return
        self._snapshot = TelegramWatchdogSnapshot(
            running=False,
            interval_seconds=int(payload.get("interval_seconds") or 5),
            log_path=str(payload.get("log_path") or DEFAULT_LOG_PATH),
            last_scan_at=str(payload.get("last_scan_at") or ""),
            last_detection_at=str(payload.get("last_detection_at") or ""),
            last_error=str(payload.get("last_error") or ""),
            last_chat_name=str(payload.get("last_chat_name") or ""),
            last_sender_name=str(payload.get("last_sender_name") or ""),
            last_content=str(payload.get("last_content") or ""),
            last_need_reply=str(payload.get("last_need_reply") or ""),
            last_reply_reason=str(payload.get("last_reply_reason") or ""),
            last_reply_draft=str(payload.get("last_reply_draft") or ""),
            detections=int(payload.get("detections") or 0),
            recent_messages=list(payload.get("recent_messages") or []),
        )

    def _save_state_unlocked(self) -> None:
        _write_json(self._state_path, asdict(self._snapshot))

    def _status_unlocked(self) -> dict:
        config = _load_config()
        auth = _load_auth_state()
        return {
            **asdict(self._snapshot),
            "config": {
                "api_id": str(config.get("api_id", "")).strip(),
                "api_hash_configured": bool(str(config.get("api_hash", "")).strip()),
                "phone": str(config.get("phone", "")).strip(),
                "session_path": str(config.get("session_path", "")),
                "session_exists": _session_exists(str(config.get("session_path", ""))),
            },
            "auth": {
                "pending": bool(auth.get("phone_code_hash")),
                "sent_at": str(auth.get("sent_at", "")),
                "phone": str(auth.get("phone", "")),
            },
        }

    def status(self) -> dict:
        with self._lock:
            return self._status_unlocked()

    def start(self, interval_seconds: int = 5, log_path: Optional[str] = None) -> dict:
        config = _load_config()
        if not config.get("api_id") or not config.get("api_hash"):
            raise RuntimeError("请先配置 Telegram API_ID / API_HASH / 手机号。")
        if not _session_exists(str(config.get("session_path"))):
            raise RuntimeError("Telegram 还没有完成登录。请先发送验证码并完成登录。")
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self._status_unlocked()
            self._stop_event.clear()
            self._snapshot.running = True
            self._snapshot.interval_seconds = max(3, int(interval_seconds))
            self._snapshot.log_path = str(Path(log_path).expanduser().resolve()) if log_path else str(DEFAULT_LOG_PATH)
            self._snapshot.last_error = ""
            self._save_state_unlocked()
            self._thread = threading.Thread(target=self._thread_main, daemon=True)
            self._thread.start()
            return self._status_unlocked()

    def stop(self) -> dict:
        with self._lock:
            self._stop_event.set()
            self._snapshot.running = False
            self._save_state_unlocked()
            if self._loop and self._client:
                try:
                    self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._client.disconnect()))
                except Exception:
                    pass
            return self._status_unlocked()

    def _record_message(self, record: dict) -> None:
        with self._lock:
            self._snapshot.last_scan_at = _now()
            self._snapshot.last_detection_at = record.get("timestamp", "")
            self._snapshot.last_chat_name = record.get("chat_name", "")
            self._snapshot.last_sender_name = record.get("sender_name", "")
            self._snapshot.last_content = record.get("content", "") or record.get("media", "")
            self._snapshot.last_need_reply = record.get("need_reply", "")
            self._snapshot.last_reply_reason = record.get("reason", "")
            self._snapshot.last_reply_draft = record.get("draft_reply", "")
            self._snapshot.detections += 1
            recent = [record] + list(self._snapshot.recent_messages or [])
            self._snapshot.recent_messages = recent[:5]
            self._save_state_unlocked()
        _append_record(self._snapshot.log_path, record)

    async def _handle_event(self, event) -> None:
        chat = await event.get_chat()
        sender = await event.get_sender()
        text = _clean_text(event.raw_text or "")
        record = {
            "timestamp": _now(),
            "chat_id": str(event.chat_id),
            "chat_name": _get_chat_name(chat),
            "sender_name": _get_sender_name(sender),
            "message_id": event.message.id,
            "content": text[:1000],
            "media": _media_desc(event.message),
            "is_group": isinstance(chat, (Chat, Channel)),
            "outgoing": bool(event.out),
        }
        try:
            assessment = _assess_reply_need(
                record["sender_name"],
                record["chat_name"],
                record["content"] or record["media"] or "",
                bool(record["is_group"]),
            )
        except Exception as exc:
            assessment = {
                "need_reply": "unknown",
                "reason": f"回复判断失败: {exc}",
                "draft_reply": "",
            }
        record.update(assessment)
        self._record_message(record)

    async def _async_main(self) -> None:
        config = _load_config()
        self._client = _telethon_client(config)
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise RuntimeError("Telegram session 未授权，请先完成登录。")

        @self._client.on(events.NewMessage(incoming=True))
        async def handler(event):
            await self._handle_event(event)

        while not self._stop_event.is_set():
            with self._lock:
                self._snapshot.last_scan_at = _now()
                self._save_state_unlocked()
            await asyncio.sleep(self._snapshot.interval_seconds)
        await self._client.disconnect()

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_main())
        except Exception as exc:
            with self._lock:
                self._snapshot.running = False
                self._snapshot.last_error = str(exc)
                self._save_state_unlocked()
        finally:
            with self._lock:
                self._snapshot.running = False
                self._save_state_unlocked()
            try:
                self._loop.close()
            except Exception:
                pass
            self._loop = None
            self._client = None


WATCHDOG = TelegramWatchdog()


def save_telegram_watchdog_config(api_id: str, api_hash: str, phone: str) -> dict:
    config = _save_config(api_id, api_hash, phone)
    return {
        "ok": True,
        "config": {
            "api_id": config["api_id"],
            "api_hash_configured": bool(config["api_hash"]),
            "phone": config["phone"],
            "session_path": config["session_path"],
        },
    }


def send_telegram_watchdog_code() -> dict:
    config = _load_config()
    phone = str(config.get("phone", "")).strip()
    if not phone:
        raise RuntimeError("请先配置 Telegram 手机号。")

    async def _run() -> dict:
        client = _telethon_client(config)
        await client.connect()
        try:
            sent = await client.send_code_request(phone)
            _save_auth_state(
                {
                    "phone": phone,
                    "phone_code_hash": sent.phone_code_hash,
                    "sent_at": _now(),
                }
            )
            return {
                "ok": True,
                "phone": phone,
                "sent_at": _now(),
            }
        finally:
            await client.disconnect()

    return asyncio.run(_run())


def complete_telegram_watchdog_login(code: str, password: str = "") -> dict:
    auth = _load_auth_state()
    config = _load_config()
    phone = str(auth.get("phone") or config.get("phone") or "").strip()
    phone_code_hash = str(auth.get("phone_code_hash", "")).strip()
    if not phone or not phone_code_hash:
        raise RuntimeError("没有待完成的 Telegram 验证码状态，请先发送验证码。")
    code = str(code).strip()
    password = str(password).strip()
    if not code:
        raise RuntimeError("请输入 Telegram 验证码。")

    async def _run() -> dict:
        client = _telethon_client(config)
        await client.connect()
        try:
            try:
                await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            except SessionPasswordNeededError:
                if not password:
                    raise RuntimeError("该 Telegram 账号开启了两步验证，请填写密码。")
                await client.sign_in(password=password)
            me = await client.get_me()
            _save_auth_state({})
            return {
                "ok": True,
                "authorized": True,
                "name": _get_sender_name(me),
                "username": getattr(me, "username", "") or "",
            }
        finally:
            await client.disconnect()

    return asyncio.run(_run())


def get_telegram_watchdog_status() -> dict:
    return WATCHDOG.status()


def start_telegram_watchdog(interval_seconds: int = 5, log_path: Optional[str] = None) -> dict:
    return WATCHDOG.start(interval_seconds=interval_seconds, log_path=log_path)


def stop_telegram_watchdog() -> dict:
    return WATCHDOG.stop()
