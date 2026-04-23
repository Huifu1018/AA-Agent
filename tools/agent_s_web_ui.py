import html
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from gui_agents.s3.utils.gmail_watchdog import (
    finish_gmail_oauth_flow,
    gmail_attachment_catalog_preview,
    get_gmail_attachment_catalog_build_status,
    gmail_attachment_catalog_status,
    gmail_oauth_status,
    get_gmail_watchdog_status,
    reset_gmail_watchdog_seen,
    start_gmail_attachment_catalog_build,
    start_gmail_oauth_flow,
    start_gmail_watchdog,
    stop_gmail_watchdog,
)
from gui_agents.s3.utils.wechat_watchdog import (
    get_wechat_watchdog_status,
    start_wechat_watchdog,
    stop_wechat_watchdog,
)


APP = FastAPI(title="AA-CUA UI")
BASE_DIR = Path(__file__).resolve().parents[1]
LOG_DIR = BASE_DIR / "logs" / "web_ui"
LOG_DIR.mkdir(parents=True, exist_ok=True)
GMAIL_WATCHDOG_DIR = BASE_DIR / "logs" / "gmail_watchdog"
GMAIL_WATCHDOG_DIR.mkdir(parents=True, exist_ok=True)
GMAIL_ATTACHMENT_CONFIG_PATH = GMAIL_WATCHDOG_DIR / "attachment_dirs.json"
GMAIL_ATTACHMENT_CATALOG_CONFIG_PATH = GMAIL_WATCHDOG_DIR / "attachment_catalog_path.json"
DEFAULT_GMAIL_ATTACHMENT_CATALOG_PATH = GMAIL_WATCHDOG_DIR / "attachment_catalog.md"


def _utc_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Job:
    id: str
    task: str
    log_path: Path
    command: list[str]
    status: str = "queued"
    created_at: str = field(default_factory=_utc_now)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    return_code: Optional[int] = None
    pid: Optional[int] = None
    process: Optional[subprocess.Popen] = None


class TaskRequest(BaseModel):
    task: str


class WatchdogRequest(BaseModel):
    interval_seconds: int = 20
    log_path: Optional[str] = None


class GmailWatchdogRequest(BaseModel):
    interval_seconds: int = 60
    log_path: Optional[str] = None


class GmailAttachmentDirsRequest(BaseModel):
    directories: list[str] = []


class GmailAttachmentCatalogPathRequest(BaseModel):
    path: str = ""


JOBS: Dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def _load_gmail_attachment_dirs() -> list[str]:
    if not GMAIL_ATTACHMENT_CONFIG_PATH.exists():
        raw = os.getenv("GMAIL_REPLY_ATTACHMENT_DIRS", "").strip()
        return [item for item in raw.split(os.pathsep) if item.strip()] if raw else []
    try:
        payload = json.loads(GMAIL_ATTACHMENT_CONFIG_PATH.read_text(encoding="utf-8"))
        directories = payload.get("directories", [])
        if isinstance(directories, list):
            return [str(item).strip() for item in directories if str(item).strip()]
    except Exception:
        pass
    return []


def _load_gmail_attachment_catalog_path() -> str:
    if not GMAIL_ATTACHMENT_CATALOG_CONFIG_PATH.exists():
        return gmail_attachment_catalog_status().get("catalog_path", "")
    try:
        payload = json.loads(GMAIL_ATTACHMENT_CATALOG_CONFIG_PATH.read_text(encoding="utf-8"))
        value = str(payload.get("path", "")).strip()
        return value or gmail_attachment_catalog_status().get("catalog_path", "")
    except Exception:
        return gmail_attachment_catalog_status().get("catalog_path", "")


def _save_gmail_attachment_dirs(directories: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in directories:
        value = str(item).strip()
        if not value:
            continue
        resolved = str(Path(value).expanduser().resolve())
        if resolved not in seen:
            cleaned.append(resolved)
            seen.add(resolved)
    GMAIL_ATTACHMENT_CONFIG_PATH.write_text(
        json.dumps({"directories": cleaned}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.environ["GMAIL_REPLY_ATTACHMENT_DIRS"] = os.pathsep.join(cleaned)
    return cleaned


def _save_gmail_attachment_catalog_path(path_value: str) -> str:
    resolved = str(Path(path_value).expanduser().resolve())
    GMAIL_ATTACHMENT_CATALOG_CONFIG_PATH.write_text(
        json.dumps({"path": resolved}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.environ["GMAIL_ATTACHMENT_CATALOG_PATH"] = resolved
    catalog_file = Path(resolved)
    if not catalog_file.exists():
        catalog_file.parent.mkdir(parents=True, exist_ok=True)
        catalog_file.write_text(
            """# Gmail 附件知识库\n\n在这里登记允许自动回复时附带发送的本地文件。\n\n## 文件: 示例客户案例集.pdf\n- 路径: /Users/yourname/Documents/materials/示例客户案例集.pdf\n- 类型: pdf\n- 标签: 客户案例, OpenClaw, 智能化转型\n- 摘要: 适合客户交流时发送的案例材料。\n- 适用场景: 客户交流, 材料补充\n- 禁止场景: 合同, 财务\n- 关键词: 案例, 客户案例, 材料\n""",
            encoding="utf-8",
        )
    return resolved


def _gmail_attachment_catalog_path_text() -> str:
    return _load_gmail_attachment_catalog_path() or str(DEFAULT_GMAIL_ATTACHMENT_CATALOG_PATH)


def _get_current_finder_directory() -> str:
    script = """
tell application "Finder"
if (count of windows) is 0 then error "Finder 当前没有打开任何文件夹窗口"
POSIX path of (target of front window as alias)
end tell
""".strip()
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise HTTPException(status_code=400, detail=stderr or "读取当前 Finder 文件夹失败")
    return (result.stdout or "").strip()


def _gmail_attachment_dirs_text() -> str:
    return "\n".join(_load_gmail_attachment_dirs())


os.environ["GMAIL_REPLY_ATTACHMENT_DIRS"] = os.pathsep.join(_load_gmail_attachment_dirs())
os.environ["GMAIL_ATTACHMENT_CATALOG_PATH"] = _load_gmail_attachment_catalog_path()


def _active_job() -> Optional[Job]:
    with JOBS_LOCK:
        for job in JOBS.values():
            if job.status == "running":
                return job
    return None


def _kimi_env_summary() -> str:
    return (
        f"provider={os.getenv('AGENT_S_PROVIDER', 'kimi')}, "
        f"ground_provider={os.getenv('AGENT_S_GROUND_PROVIDER', 'kimi')}, "
        f"model={os.getenv('KIMI_MODEL', os.getenv('AGENT_S_MODEL', 'kimi-k2.5'))}, "
        f"ground_model={os.getenv('KIMI_GROUND_MODEL', os.getenv('AGENT_S_GROUND_MODEL', os.getenv('KIMI_MODEL', 'kimi-k2.5')))}"
    )


def _build_command(task: str) -> list[str]:
    provider = os.getenv("AGENT_S_PROVIDER", "kimi")
    ground_provider = os.getenv("AGENT_S_GROUND_PROVIDER", provider)
    width = os.getenv("AGENT_S_GROUNDING_WIDTH", "1920")
    height = os.getenv("AGENT_S_GROUNDING_HEIGHT", "1080")
    max_trajectory_length = os.getenv("AGENT_S_MAX_TRAJECTORY_LENGTH", "3")
    enable_reflection = os.getenv("AGENT_S_ENABLE_REFLECTION", "0").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

    command = [
        sys.executable,
        "-m",
        "gui_agents.s3.cli_app",
        "--provider",
        provider,
        "--ground_provider",
        ground_provider,
        "--grounding_width",
        width,
        "--grounding_height",
        height,
        "--max_trajectory_length",
        max_trajectory_length,
        "--enable_local_env",
        "--task",
        task,
    ]

    if enable_reflection:
        command.append("--enable_reflection")
    else:
        command.append("--disable_reflection")

    model = os.getenv("AGENT_S_MODEL")
    ground_model = os.getenv("AGENT_S_GROUND_MODEL")
    if model:
        command.extend(["--model", model])
    if ground_model:
        command.extend(["--ground_model", ground_model])

    return command


def _extract_final_summary(log_text: str) -> dict:
    summary = {
        "agent_status": "",
        "agent_reason": "",
        "agent_summary": "",
    }
    for line in log_text.splitlines():
        if "AGENT_S_FINAL_STATUS:" in line:
            summary["agent_status"] = line.split("AGENT_S_FINAL_STATUS:", 1)[1].strip()
        elif "AGENT_S_FINAL_REASON:" in line:
            summary["agent_reason"] = line.split("AGENT_S_FINAL_REASON:", 1)[1].strip()
        elif "AGENT_S_FINAL_SUMMARY:" in line:
            summary["agent_summary"] = line.split("AGENT_S_FINAL_SUMMARY:", 1)[1].strip()
    return summary


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    deduped = []
    for item in items:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _extract_created_paths(log_text: str) -> list[str]:
    clean_text = _strip_ansi(log_text)
    paths: list[str] = []
    for line in clean_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("File created at:", "File created successfully at:", "Target folder:")):
            paths.append(stripped.split(":", 1)[1].strip())
        if "file_path =" in stripped or "target_folder =" in stripped:
            match = re.search(r'["\'](/Users/.+?)["\']', stripped)
            if match:
                paths.append(match.group(1).strip())
    return _dedupe_keep_order(paths)


def _extract_plan_actions(log_text: str, job_status: str, agent_status: str) -> list[dict]:
    matches = re.findall(
        r"\(Next Action\)\n(.*?)\n\n\(Grounded Action\)",
        log_text,
        flags=re.DOTALL,
    )
    actions = [" ".join(match.split()) for match in matches if match.strip()]
    breakdown = []
    for idx, action in enumerate(actions):
        if idx < len(actions) - 1:
            status = "done"
        elif job_status == "running":
            status = "running"
        elif agent_status == "completed":
            status = "done"
        else:
            status = "stopped"
        breakdown.append(
            {
                "kind": "gui",
                "title": action,
                "status": status,
                "summary": "",
            }
        )
    return breakdown


def _extract_code_agent_steps(log_text: str, job_status: str) -> list[dict]:
    steps: list[dict] = []
    current_step: Optional[dict] = None

    for raw_line in log_text.splitlines():
        line = _strip_ansi(raw_line).strip()
        if "Executing SUBTASK:" in line or "Executing FULL TASK:" in line:
            if current_step is not None:
                steps.append(current_step)
            title = re.split(r"Executing (?:SUBTASK|FULL TASK):", line, maxsplit=1)[1].strip()
            kind = "code-full" if "Executing FULL TASK:" in line else "code-subtask"
            current_step = {
                "kind": kind,
                "title": title,
                "status": "running",
                "summary": "",
            }
        elif current_step and "Result - Completion reason:" in line:
            reason = re.split(r"Result - Completion reason:", line, maxsplit=1)[1].strip()
            current_step["status"] = {
                "DONE": "done",
                "FAIL": "failed",
                "BUDGET_EXHAUSTED": "stopped",
            }.get(reason, reason.lower())
        elif current_step and re.search(r"(^Summary:|Result - Completion reason:)", line):
            if line.startswith("Summary:"):
                current_step["summary"] = line.split(":", 1)[1].strip()

    if current_step is not None:
        if current_step["status"] == "running" and job_status != "running":
            current_step["status"] = "stopped"
        steps.append(current_step)

    return steps


def _extract_job_breakdown(log_text: str, job_status: str, agent_status: str) -> list[dict]:
    code_steps = _extract_code_agent_steps(log_text, job_status)
    gui_steps = _extract_plan_actions(log_text, job_status, agent_status)
    return code_steps + gui_steps


def _detect_execution_strategies(task: str, log_text: str) -> list[str]:
    strategies: list[str] = []
    lowered_task = task.lower()
    lowered_log = _strip_ansi(log_text).lower()

    if (
        ("微信" in task or "wechat" in lowered_task)
        and any(
            keyword in lowered_task
            for keyword in ("文件", "附件", "文档", "pdf", "word", "txt", "image", "图片", "file", "document")
        )
    ):
        strategies.append("本次将使用微信附件上传流程，不会默认走剪贴板粘贴文件。")
        strategies.append("本次会优先尝试微信发文件快路径：直接激活微信、搜索目标会话、点击附件按钮、按路径选中文件并发送。")

    if ("微信" in task or "wechat" in lowered_task) and any(
        keyword in lowered_task
        for keyword in ("发消息", "消息", "聊天", "联系人", "message", "chat", "contact", "发给", "给")
    ):
        strategies.append("本次将优先走微信联系人直达聊天路径：先命中联系人精确结果，再直接进入聊天窗口，避免在资料卡里反复试错。")
        strategies.append("本次会按微信小状态机推进：打开微信 → 搜索联系人 → 进入聊天 → 聚焦输入框 → 发送 → 验证。")

    if ("微信" in task or "wechat" in lowered_task) and any(
        keyword in lowered_task
        for keyword in ("表情", "微笑", "笑脸", "emoji", "emoticon", "smile", "smiley")
    ):
        strategies.append("本次将优先走微信表情快捷路径：先精确进入联系人，再直接在输入框输入 emoji 并回车发送。")
        strategies.append("本次会优先尝试微信短消息快路径：直接激活微信、搜索目标会话、聚焦输入框、粘贴内容并发送，尽量不再拆成多步 GUI 试错。")

    if any(keyword in lowered_task for keyword in ("outlook", "email", "mail", "邮件", "邮箱")):
        strategies.append("本次将按邮件抽取规则执行：优先只保留最新正文，排除历史引用内容。")
        if any(
            keyword in lowered_task
            for keyword in ("txt", "text file", "文本", "已有", "现有", "覆盖", "overwrite", "replace", "/users/")
        ):
            strategies.append("如果任务指定了已有文件或明确路径，本次会优先打开并覆盖现有文件，而不是新建 Word 文档。")

    if "/users/" in lowered_task and any(
        keyword in lowered_task
        for keyword in ("finder", "定位", "找到", "显示", "reveal", "locate", "open in finder")
    ):
        strategies.append("本次将优先通过已知路径直接在 Finder 中定位目标。")

    if "grounding agent: calling code agent" in lowered_log:
        strategies.append("本次任务已经触发 CodeAgent 参与执行。")

    return _dedupe_keep_order(strategies)


def _run_job(job: Job) -> None:
    env = os.environ.copy()
    job.started_at = _utc_now()
    job.status = "running"

    with job.log_path.open("w", encoding="utf-8") as log_file:
        log_file.write(f"[{job.started_at}] AA-CUA job started\n")
        log_file.write(f"Task: {job.task}\n")
        log_file.write(f"Command: {shlex.join(job.command)}\n")
        log_file.write(f"Env summary: {_kimi_env_summary()}\n")
        log_file.write("-" * 80 + "\n")
        log_file.flush()

        process = subprocess.Popen(
            job.command,
            cwd=BASE_DIR,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=(os.name != "nt"),
        )
        job.process = process
        job.pid = process.pid
        return_code = process.wait()

        job.return_code = return_code
        job.finished_at = _utc_now()
        if job.status != "stopped":
            job.status = "finished" if return_code == 0 else "failed"
        log_file.write("-" * 80 + "\n")
        log_file.write(
            f"[{job.finished_at}] AA-CUA job ended with status={job.status}, return_code={return_code}\n"
        )


def _render_index() -> str:
    active = _active_job()
    active_html = ""
    active_job_id = ""
    if active:
        active_job_id = active.id
        active_html = (
            f"<p class='status'>Current job: <strong>{html.escape(active.id)}</strong> "
            f"({html.escape(active.status)})</p>"
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AA-CUA UI</title>
  <style>
    :root {{
      --bg: #f4efe6;
      --panel: #fffaf2;
      --line: #d9cdb8;
      --ink: #1d2a33;
      --accent: #2f7d6b;
      --accent-soft: #d7efe7;
      --warn: #8d4f2a;
    }}
    body {{
      margin: 0;
      font-family: "Avenir Next", "PingFang SC", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(47,125,107,0.18), transparent 28rem),
        linear-gradient(180deg, #f6f2ea 0%, var(--bg) 100%);
      color: var(--ink);
    }}
    .shell {{
      max-width: 900px;
      margin: 40px auto;
      padding: 0 20px 40px;
    }}
    .card {{
      background: rgba(255,250,242,0.92);
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: 0 20px 60px rgba(29,42,51,0.08);
      overflow: hidden;
    }}
    .hero {{
      padding: 28px 28px 18px;
      border-bottom: 1px solid var(--line);
      background:
        linear-gradient(135deg, rgba(47,125,107,0.12), transparent 45%),
        linear-gradient(180deg, rgba(255,255,255,0.7), rgba(255,250,242,0.8));
    }}
    h1 {{
      margin: 0 0 6px;
      font-size: 32px;
      line-height: 1.1;
    }}
    p {{
      margin: 0;
      line-height: 1.6;
    }}
    .content {{
      padding: 24px 28px 28px;
      display: grid;
      gap: 18px;
    }}
    textarea {{
      width: 100%;
      min-height: 140px;
      border-radius: 18px;
      border: 1px solid var(--line);
      background: #fffdf9;
      padding: 16px 18px;
      font: inherit;
      color: var(--ink);
      resize: vertical;
      box-sizing: border-box;
    }}
    button {{
      border: 0;
      border-radius: 999px;
      background: var(--accent);
      color: white;
      padding: 12px 18px;
      font: inherit;
      cursor: pointer;
      position: relative;
      z-index: 2;
      pointer-events: auto;
    }}
    button:disabled {{
      cursor: not-allowed;
      opacity: 0.55;
    }}
    button.secondary {{
      background: #e6ddd0;
      color: var(--ink);
    }}
    .row {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: center;
    }}
    .hint {{
      padding: 14px 16px;
      border-radius: 16px;
      background: var(--accent-soft);
      color: #214f43;
    }}
    .status {{
      padding: 10px 14px;
      border-radius: 14px;
      background: #fff5e8;
      color: var(--warn);
    }}
    .summary {{
      padding: 14px 16px;
      border-radius: 16px;
      background: #eef3ff;
      color: #284067;
      white-space: pre-wrap;
      line-height: 1.6;
    }}
    .logbox {{
      white-space: pre-wrap;
      background: #182126;
      color: #e8f2f6;
      border-radius: 18px;
      padding: 16px;
      height: 420px;
      overflow: auto;
      font-family: "SFMono-Regular", Menlo, monospace;
      font-size: 13px;
      line-height: 1.55;
      overscroll-behavior: contain;
    }}
    .meta {{
      color: #5e6d76;
      font-size: 14px;
    }}
    .info-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
    }}
        .info-card {{
      padding: 14px 16px;
      border-radius: 16px;
      border: 1px solid var(--line);
      background: #fffdf9;
      display: flex;
      flex-direction: column;
      min-height: 0;
      position: relative;
      z-index: 1;
    }}
    .info-card h3 {{
      margin: 0 0 8px;
      font-size: 16px;
      flex: 0 0 auto;
    }}
    .info-card .empty {{
      color: #7b8a94;
    }}
    .path-list, .step-list {{
      display: grid;
      gap: 8px;
      max-height: 360px;
      overflow: auto;
      padding-right: 4px;
      overscroll-behavior: contain;
    }}
    .path-item, .step-item {{
      padding: 8px 10px;
      border-radius: 12px;
      background: #f7f2ea;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .step-top {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: flex-start;
      margin-bottom: 6px;
    }}
    .step-title {{
      flex: 1 1 auto;
      min-width: 0;
      white-space: normal;
      overflow-wrap: anywhere;
      word-break: break-word;
      line-height: 1.45;
      display: -webkit-box;
      -webkit-line-clamp: 4;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }}
    .badge {{
      flex: 0 0 auto;
      max-width: 100%;
      border-radius: 999px;
      padding: 2px 9px;
      font-size: 11px;
      line-height: 1.5;
      background: #e6ddd0;
      color: var(--ink);
      white-space: normal;
      overflow-wrap: anywhere;
      word-break: break-word;
      text-align: center;
    }}
    .badge.done {{
      background: #d7efe7;
      color: #214f43;
    }}
    .badge.running {{
      background: #fff5e8;
      color: var(--warn);
    }}
    .badge.failed, .badge.stopped {{
      background: #f8e3df;
      color: #8a3d2d;
    }}
    .step-kind {{
      font-size: 11px;
      color: #68757e;
      margin-bottom: 3px;
    }}
    .step-summary {{
      font-size: 12px;
      color: #495861;
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }}
    .strategy-box {{
      padding: 14px 16px;
      border-radius: 16px;
      background: #eef8f0;
      color: #22533d;
      white-space: pre-wrap;
      line-height: 1.6;
    }}
    .watchdog-box {{
      padding: 14px 16px;
      border-radius: 16px;
      background: #fff6ea;
      color: #6b4a1d;
      white-space: pre-wrap;
      line-height: 1.6;
    }}
    .watchdog-details {{
      display: none;
      gap: 10px;
    }}
    .watchdog-detail-card {{
      padding: 14px 16px;
      border-radius: 16px;
      background: rgba(255, 253, 249, 0.96);
      border: 1px solid var(--line);
    }}
    .watchdog-detail-label {{
      font-size: 13px;
      color: #7d6b57;
      margin-bottom: 6px;
    }}
    .watchdog-detail-value {{
      white-space: pre-wrap;
      line-height: 1.6;
      word-break: break-word;
    }}
    .watchdog-history {{
      display: none;
      gap: 10px;
    }}
    .watchdog-history-item {{
      padding: 14px 16px;
      border-radius: 16px;
      background: rgba(255, 253, 249, 0.96);
      border: 1px solid var(--line);
    }}
    .watchdog-history-title {{
      font-weight: 700;
      margin-bottom: 6px;
    }}
    .watchdog-history-meta {{
      color: #7d6b57;
      font-size: 13px;
      margin-bottom: 8px;
      white-space: pre-wrap;
      line-height: 1.6;
    }}
    .watchdog-history-body {{
      white-space: pre-wrap;
      line-height: 1.6;
      word-break: break-word;
    }}
    .watchdog-history-body-preview {{
      white-space: pre-wrap;
      line-height: 1.6;
      word-break: break-word;
    }}
    .watchdog-history-expand {{
      margin-top: 10px;
    }}
    .watchdog-history-expand summary {{
      cursor: pointer;
      color: var(--accent);
      font-size: 14px;
      user-select: none;
      outline: none;
    }}
    .watchdog-history-expand[open] summary {{
      margin-bottom: 10px;
    }}
  </style>
</head>
<body>
  <div class="shell" data-active-job-id="{html.escape(active_job_id)}">
    <div class="card">
      <div class="hero">
        <h1>AA-CUA 验证台</h1>
        <p>输入一段任务文本，页面会直接启动一次 AA-CUA，并回显原始运行日志，方便你核对是不是通过 AA-CUA 执行的动作。</p>
      </div>
      <div class="content">
        <div class="hint">当前默认会用本机环境变量里的 Kimi 配置，并开启 <code>--enable_local_env</code>。</div>
        {active_html}
        <textarea id="task" placeholder="例如：Rename the file ... then finish."></textarea>
        <div class="row">
          <button id="run" type="button" onclick="startJob(); return false;">发送给 AA-CUA</button>
          <button id="stop" class="secondary" type="button" onclick="stopJob(); return false;">停止当前任务</button>
          <span class="meta" id="job-meta">还没有任务运行</span>
        </div>
        <div class="watchdog-box" id="watchdog-box">微信 watchdog 还没有启动。</div>
        <div class="row">
          <button id="watchdog-start" class="secondary" type="button" onclick="startWatchdog(); return false;">启动微信 Watchdog</button>
          <button id="watchdog-stop" class="secondary" type="button" onclick="stopWatchdog(); return false;">停止微信 Watchdog</button>
          <span class="meta" id="watchdog-meta">尚未巡检</span>
        </div>
        <div class="watchdog-box" id="gmail-watchdog-box">Gmail watchdog 还没有启动。</div>
        <div class="watchdog-history" id="gmail-watchdog-history"></div>
        <div class="row">
          <button id="gmail-oauth-start" class="secondary" type="button" onclick="startGmailOAuth(); return false;">连接 Gmail</button>
          <button id="gmail-watchdog-start" class="secondary" type="button" onclick="startGmailWatchdog(); return false;">启动 Gmail Watchdog</button>
          <button id="gmail-watchdog-stop" class="secondary" type="button" onclick="stopGmailWatchdog(); return false;">停止 Gmail Watchdog</button>
          <button id="gmail-watchdog-rescan" class="secondary" type="button" onclick="rescanGmailWatchdog(); return false;">重新扫描当前未读</button>
          <span class="meta" id="gmail-watchdog-meta">尚未巡检</span>
        </div>
        <div class="strategy-box" id="strategy-box">任务命中特殊执行策略时，这里会显示本次会采用的流程。</div>
        <div class="info-grid">
          <div class="info-card">
            <h3>创建文件路径</h3>
            <div id="path-list" class="path-list"><div class="empty">还没有识别到路径。</div></div>
          </div>
          <div class="info-card">
            <h3>任务拆解</h3>
            <div id="step-list" class="step-list"><div class="empty">还没有拆解信息。</div></div>
          </div>
        </div>
        <div class="summary" id="summary-box">任务结束后，这里会显示 AA-CUA 的完成反馈。</div>
        <div class="logbox" id="logbox">日志会显示在这里。</div>
      </div>
    </div>
  </div>
  <script>
    let currentJobId = null;
    let pollTimer = null;
    let watchdogTimer = null;
    let gmailWatchdogTimer = null;
    let logAutoFollow = true;
    let notifiedTerminalJobId = "";
    let watchdogBusy = false;
    let gmailWatchdogBusy = false;
    let gmailAttachmentDirsBusy = false;
    let gmailAttachmentDirsPickBusy = false;
    let gmailAttachmentCatalogBusy = false;
    let gmailCatalogBuildBusy = false;
    let notifiedCatalogBuildFinishedAt = "";
    const shellEl = document.querySelector(".shell");
    const initialActiveJobId = shellEl ? shellEl.dataset.activeJobId : "";
    const stopButton = document.getElementById("stop");
    const logbox = document.getElementById("logbox");

    if (logbox) {{
      logbox.addEventListener("scroll", () => {{
        const threshold = 24;
        logAutoFollow =
          logbox.scrollTop + logbox.clientHeight >= logbox.scrollHeight - threshold;
      }});
    }}

    function escapeHtml(value) {{
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }}

    function renderPaths(paths) {{
      const pathList = document.getElementById("path-list");
      if (!paths || paths.length === 0) {{
        pathList.innerHTML = '<div class="empty">还没有识别到路径。</div>';
        return;
      }}
      pathList.innerHTML = paths
        .map((path) => `<div class="path-item">${{escapeHtml(path)}}</div>`)
        .join("");
    }}

    function renderSteps(steps) {{
      const stepList = document.getElementById("step-list");
      if (!steps || steps.length === 0) {{
        stepList.innerHTML = '<div class="empty">还没有拆解信息。</div>';
        return;
      }}
      stepList.innerHTML = steps
        .map((step, index) => {{
          const statusClass = escapeHtml(step.status || "stopped");
          const summary = step.summary ? `<div class="step-summary">${{escapeHtml(step.summary)}}</div>` : "";
          return `
            <div class="step-item">
              <div class="step-kind">步骤 ${{index + 1}} · ${{escapeHtml(step.kind || "task")}}</div>
              <div class="step-top">
                <strong class="step-title">${{escapeHtml(step.title || "未命名步骤")}}</strong>
                <span class="badge ${{statusClass}}">${{escapeHtml(step.status || "unknown")}}</span>
              </div>
              ${{summary}}
            </div>
          `;
        }})
        .join("");
    }}

    function renderStrategies(strategies) {{
      const box = document.getElementById("strategy-box");
      if (!strategies || strategies.length === 0) {{
        box.textContent = "任务命中特殊执行策略时，这里会显示本次会采用的流程。";
        return;
      }}
      box.textContent = strategies.join("\\n");
    }}

    function setStopButtonState(isRunning, isStopping = false) {{
      if (!stopButton) {{
        return;
      }}
      stopButton.disabled = !isRunning || isStopping;
      stopButton.textContent = isStopping ? "正在停止..." : "停止当前任务";
    }}

    function renderWatchdog(status) {{
      const box = document.getElementById("watchdog-box");
      const meta = document.getElementById("watchdog-meta");
      const startBtn = document.getElementById("watchdog-start");
      const stopBtn = document.getElementById("watchdog-stop");
      const running = Boolean(status && status.running);
      if (startBtn) {{
        startBtn.disabled = running || watchdogBusy;
        startBtn.textContent = watchdogBusy && !running ? "正在启动..." : "启动微信 Watchdog";
      }}
      if (stopBtn) {{
        stopBtn.disabled = !running || watchdogBusy;
        stopBtn.textContent = watchdogBusy && running ? "正在停止..." : "停止微信 Watchdog";
      }}
      if (!status) {{
        box.textContent = "微信 watchdog 还没有启动。";
        meta.textContent = "尚未巡检";
        return;
      }}
      box.textContent =
        "微信 watchdog 状态: " + (running ? "运行中" : "未运行") + "\\n" +
        "巡检间隔: " + (status.interval_seconds ?? 20) + " 秒\\n" +
        "记录文档: " + (status.log_path || "未设置") + "\\n" +
        "最近一次检测: " + (status.last_detection_at || "暂无") + "\\n" +
        "最近一次命中联系人: " + (status.last_message_contact || "暂无") + "\\n" +
        "最近检测到的未读联系人: " + (status.last_unread_candidates || "暂无") + "\\n" +
        "最近一次回复判断: " + (status.last_reply_assessment || "暂无") + "\\n" +
        "累计记录: " + (status.detections ?? 0) + " 条" +
        (status.last_skip_reason ? "\\n最近跳过原因: " + status.last_skip_reason : "") +
        (status.last_error ? "\\n最近错误: " + status.last_error : "");
      meta.textContent =
        "最近巡检: " + (status.last_scan_at || "暂无") +
        " | 状态: " + (running ? "running" : "stopped");
    }}

    async function refreshWatchdog() {{
      const response = await fetch("/api/watchdog");
      const data = await response.json();
      if (!response.ok) {{
        renderWatchdog({{
          running: false,
          last_error: data.detail || "读取 watchdog 状态失败"
        }});
        return;
      }}
      renderWatchdog(data);
    }}

    function renderGmailWatchdog(status) {{
      const box = document.getElementById("gmail-watchdog-box");
      const meta = document.getElementById("gmail-watchdog-meta");
      const history = document.getElementById("gmail-watchdog-history");
      const startBtn = document.getElementById("gmail-watchdog-start");
      const stopBtn = document.getElementById("gmail-watchdog-stop");
      const rescanBtn = document.getElementById("gmail-watchdog-rescan");
      const running = Boolean(status && status.running);
      if (startBtn) {{
        startBtn.disabled = running || gmailWatchdogBusy;
        startBtn.textContent = gmailWatchdogBusy && !running ? "正在启动..." : "启动 Gmail Watchdog";
      }}
      if (stopBtn) {{
        stopBtn.disabled = !running || gmailWatchdogBusy;
        stopBtn.textContent = gmailWatchdogBusy && running ? "正在停止..." : "停止 Gmail Watchdog";
      }}
      if (rescanBtn) {{
        rescanBtn.disabled = gmailWatchdogBusy;
        rescanBtn.textContent = gmailWatchdogBusy ? "正在处理中..." : "重新扫描当前未读";
      }}
      if (!status) {{
        box.style.display = "block";
        box.textContent = "Gmail watchdog 还没有启动。";
        meta.textContent = "尚未巡检";
        history.style.display = "none";
        history.innerHTML = "";
        return;
      }}
      const currentUnreadEntries = Array.isArray(status.current_unread_entries) ? status.current_unread_entries : [];
      const latestUnread = status.latest_unread && typeof status.latest_unread === "object" ? status.latest_unread : {{}};
      const hasUnread = Boolean(latestUnread.from || latestUnread.subject || latestUnread.snippet || latestUnread.body);
      if (hasUnread) {{
        box.style.display = "none";
        history.style.display = "grid";
        const title = escapeHtml(latestUnread.subject || "无主题");
        const metaText =
          "时间: " + escapeHtml(latestUnread.date || "暂无") + "\\n" +
          "发件人: " + escapeHtml(latestUnread.from || "暂无") + "\\n" +
          "状态: 未读";
        const bodyText = latestUnread.body || latestUnread.snippet || "暂无内容";
        const normalizedBody = String(bodyText || "");
        const previewText =
          normalizedBody.length > 320 ? normalizedBody.slice(0, 320).trim() + "..." : normalizedBody;
        const expandedBody = normalizedBody.length > 320
          ? `
            <details class="watchdog-history-expand">
              <summary>展开全文</summary>
              <div class="watchdog-history-body">${{escapeHtml(normalizedBody)}}</div>
            </details>
          `
          : `<div class="watchdog-history-body">${{escapeHtml(normalizedBody)}}</div>`;
        history.innerHTML = `
          <div class="watchdog-history-item">
            <div class="watchdog-history-title">${{title}}</div>
            <div class="watchdog-history-meta">${{metaText}}</div>
            <div class="watchdog-history-body-preview">${{escapeHtml(previewText)}}</div>
            ${{expandedBody}}
          </div>
        `;
      }} else {{
        box.style.display = "block";
        box.textContent =
          "Gmail watchdog 状态: " + (running ? "运行中" : "未运行") + "\\n" +
          "最近一次巡检: " + (status.last_scan_at || "暂无") + "\\n" +
          "当前未读数: " + currentUnreadEntries.length +
          (status.last_error ? "\\n最近错误: " + status.last_error : "");
        history.style.display = "none";
        history.innerHTML = "";
      }}
      meta.textContent =
        "最近巡检: " + (status.last_scan_at || "暂无") +
        " | 状态: " + (running ? "running" : "stopped");
    }}

    async function startGmailAttachmentCatalogBuild() {{
      alert("当前版本未启用附件知识库初始化。");
    }}

    function setGmailAttachmentDirsSaving(saving, message = null) {{
      gmailAttachmentDirsBusy = saving;
    }}

    function setGmailAttachmentDirsPicking(picking, message = null) {{
      gmailAttachmentDirsPickBusy = picking;
    }}

    function renderGmailAttachmentDirsSavedMessage(count) {{
      return;
    }}

    function setGmailAttachmentCatalogSaving(saving, message = null) {{
      gmailAttachmentCatalogBusy = saving;
    }}

    function useDefaultGmailAttachmentCatalog() {{
      return;
    }}

    async function saveGmailAttachmentCatalogPath() {{
      return;
    }}

    async function refreshGmailWatchdog() {{
      const response = await fetch("/api/gmail-watchdog");
      const data = await response.json();
      if (!response.ok) {{
        renderGmailWatchdog({{
          running: false,
          last_error: data.detail || "读取 Gmail watchdog 状态失败"
        }});
        return;
      }}
      renderGmailWatchdog(data);
    }}

    async function startGmailOAuth() {{
      const response = await fetch("/api/gmail-watchdog/oauth/start", {{
        method: "POST"
      }});
      const data = await response.json();
      if (!response.ok) {{
        alert(data.detail || "启动 Gmail 授权失败");
        await refreshGmailWatchdog();
        return;
      }}
      if (!data.auth_url) {{
        alert("没有拿到 Gmail 授权地址。");
        return;
      }}
      window.location.href = data.auth_url;
    }}

    async function startWatchdog() {{
      watchdogBusy = true;
      document.getElementById("watchdog-meta").textContent = "正在启动微信 watchdog...";
      renderWatchdog(await (await fetch("/api/watchdog")).json());
      const response = await fetch("/api/watchdog/start", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ interval_seconds: 20 }})
      }});
      const data = await response.json();
      watchdogBusy = false;
      if (!response.ok) {{
        alert(data.detail || "启动 watchdog 失败");
        await refreshWatchdog();
        return;
      }}
      renderWatchdog(data);
      if (!watchdogTimer) {{
        watchdogTimer = setInterval(refreshWatchdog, 5000);
      }}
    }}

    async function stopWatchdog() {{
      watchdogBusy = true;
      document.getElementById("watchdog-meta").textContent = "正在停止微信 watchdog...";
      renderWatchdog(await (await fetch("/api/watchdog")).json());
      const response = await fetch("/api/watchdog/stop", {{
        method: "POST"
      }});
      const data = await response.json();
      watchdogBusy = false;
      if (!response.ok) {{
        alert(data.detail || "停止 watchdog 失败");
        await refreshWatchdog();
        return;
      }}
      renderWatchdog(data);
    }}

    async function startGmailWatchdog() {{
      gmailWatchdogBusy = true;
      document.getElementById("gmail-watchdog-meta").textContent = "正在启动 Gmail watchdog...";
      renderGmailWatchdog(await (await fetch("/api/gmail-watchdog")).json());
      const response = await fetch("/api/gmail-watchdog/start", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ interval_seconds: 60 }})
      }});
      const data = await response.json();
      gmailWatchdogBusy = false;
      if (!response.ok) {{
        alert(data.detail || "启动 Gmail watchdog 失败");
        await refreshGmailWatchdog();
        return;
      }}
      renderGmailWatchdog(data);
      if (!gmailWatchdogTimer) {{
        gmailWatchdogTimer = setInterval(refreshGmailWatchdog, 5000);
      }}
    }}

    async function stopGmailWatchdog() {{
      gmailWatchdogBusy = true;
      document.getElementById("gmail-watchdog-meta").textContent = "正在停止 Gmail watchdog...";
      renderGmailWatchdog(await (await fetch("/api/gmail-watchdog")).json());
      const response = await fetch("/api/gmail-watchdog/stop", {{
        method: "POST"
      }});
      const data = await response.json();
      gmailWatchdogBusy = false;
      if (!response.ok) {{
        alert(data.detail || "停止 Gmail watchdog 失败");
        await refreshGmailWatchdog();
        return;
      }}
      renderGmailWatchdog(data);
    }}

    async function rescanGmailWatchdog() {{
      gmailWatchdogBusy = true;
      document.getElementById("gmail-watchdog-meta").textContent = "正在重置并重新扫描 Gmail...";
      renderGmailWatchdog(await (await fetch("/api/gmail-watchdog")).json());
      const response = await fetch("/api/gmail-watchdog/rescan", {{
        method: "POST"
      }});
      const data = await response.json();
      gmailWatchdogBusy = false;
      if (!response.ok) {{
        alert(data.detail || "重新扫描 Gmail 失败");
        await refreshGmailWatchdog();
        return;
      }}
      renderGmailWatchdog(data);
    }}

    async function saveGmailAttachmentDirs() {{
      return;
    }}

    async function pickGmailAttachmentDir() {{
      alert("当前版本未启用本地附件文件夹选择。");
    }}

    async function startJob() {{
      const taskEl = document.getElementById("task");
      const task = taskEl.value.trim();
      if (!task) {{
        alert("先输入一段任务文本。");
        return;
      }}

      const response = await fetch("/api/jobs", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ task }})
      }});
      const data = await response.json();
      if (!response.ok) {{
        alert(data.detail || "启动失败");
        return;
      }}

      currentJobId = data.job_id;
      document.getElementById("job-meta").textContent = "任务 " + data.job_id + " 已启动";
      logbox.textContent = "正在启动 AA-CUA...";
      logAutoFollow = true;
      setStopButtonState(true, false);
      beginPolling();
    }}

    async function stopJob() {{
      if (!currentJobId && initialActiveJobId) {{
        currentJobId = initialActiveJobId;
      }}

      if (!currentJobId) {{
        alert("当前没有可停止的任务。");
        return;
      }}

      setStopButtonState(true, true);
      document.getElementById("job-meta").textContent =
        "任务 " + currentJobId + " | 状态: 正在停止 | 返回码: stopping";

      const response = await fetch("/api/jobs/" + currentJobId + "/stop", {{
        method: "POST"
      }});
      const data = await response.json();
      if (!response.ok) {{
        setStopButtonState(true, false);
        alert(data.detail || "停止失败");
        return;
      }}
      document.getElementById("job-meta").textContent =
        "任务 " + data.job_id + " | 状态: " + data.status + " | 返回码: stopping";
      await refreshLog();
    }}

    async function refreshLog() {{
      if (!currentJobId) {{
        return;
      }}
      const response = await fetch("/api/jobs/" + currentJobId);
      const data = await response.json();
      if (!response.ok) {{
        document.getElementById("logbox").textContent = data.detail || "读取日志失败";
        return;
      }}

      document.getElementById("job-meta").textContent =
        "任务 " + data.job_id + " | 状态: " + data.status + " | 返回码: " + (data.return_code ?? "running");
      document.getElementById("summary-box").textContent =
        data.agent_summary || "AA-CUA 还没有输出最终反馈。";
      renderStrategies(data.execution_strategies || []);
      renderPaths(data.created_paths || []);
      renderSteps(data.task_breakdown || []);
      const shouldFollow = logAutoFollow;
      logbox.textContent = data.log || "还没有日志输出。";
      if (shouldFollow) {{
        logbox.scrollTop = logbox.scrollHeight;
      }}

      setStopButtonState(data.status === "running", false);

      if (["finished", "failed", "stopped"].includes(data.status)) {{
        clearInterval(pollTimer);
        pollTimer = null;
        if (notifiedTerminalJobId !== data.job_id) {{
          notifiedTerminalJobId = data.job_id;
          const summaryText =
            data.agent_summary || ("任务 " + data.job_id + " 已结束，状态: " + data.status);
          alert(summaryText);
        }}
      }}
    }}

    function beginPolling() {{
      if (pollTimer) {{
        clearInterval(pollTimer);
      }}
      refreshLog();
      pollTimer = setInterval(refreshLog, 2000);
    }}

    const bindClick = (id, handler) => {{
      const el = document.getElementById(id);
      if (el) el.addEventListener("click", handler);
    }};
    bindClick("run", startJob);
    bindClick("stop", stopJob);
    bindClick("watchdog-start", startWatchdog);
    bindClick("watchdog-stop", stopWatchdog);
    bindClick("gmail-watchdog-start", startGmailWatchdog);
    bindClick("gmail-watchdog-stop", stopGmailWatchdog);
    bindClick("gmail-watchdog-rescan", rescanGmailWatchdog);
    bindClick("gmail-oauth-start", startGmailOAuth);
    setStopButtonState(Boolean(initialActiveJobId), false);
    refreshWatchdog();
    watchdogTimer = setInterval(refreshWatchdog, 5000);
    refreshGmailWatchdog();
    gmailWatchdogTimer = setInterval(refreshGmailWatchdog, 5000);

    if (initialActiveJobId) {{
      currentJobId = initialActiveJobId;
      beginPolling();
    }}
  </script>
</body>
</html>"""


@APP.get("/", response_class=HTMLResponse)
def index() -> str:
    return _render_index()


@APP.post("/api/jobs")
def create_job(payload: TaskRequest) -> dict:
    task = payload.task.strip()
    if not task:
        raise HTTPException(status_code=400, detail="Task cannot be empty.")

    if _active_job() is not None:
        raise HTTPException(
            status_code=409, detail="Another AA-CUA job is still running."
        )

    if not (os.getenv("KIMI_API_KEY") or os.getenv("ANTHROPIC_API_KEY")):
        raise HTTPException(
            status_code=400,
            detail="Missing KIMI_API_KEY or compatible API key in environment.",
        )

    job_id = uuid.uuid4().hex[:8]
    log_path = LOG_DIR / f"{job_id}.log"
    job = Job(
        id=job_id,
        task=task,
        log_path=log_path,
        command=_build_command(task),
    )
    with JOBS_LOCK:
        JOBS[job_id] = job

    thread = threading.Thread(target=_run_job, args=(job,), daemon=True)
    thread.start()
    return {"job_id": job_id, "status": job.status}


@APP.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    log_text = ""
    if job.log_path.exists():
        log_text = job.log_path.read_text(encoding="utf-8", errors="replace")
    final_summary = _extract_final_summary(log_text)
    created_paths = _extract_created_paths(log_text)
    task_breakdown = _extract_job_breakdown(
        log_text,
        job.status,
        final_summary.get("agent_status", ""),
    )
    execution_strategies = _detect_execution_strategies(job.task, log_text)

    return {
        "job_id": job.id,
        "status": job.status,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "return_code": job.return_code,
        "pid": job.pid,
        "command": shlex.join(job.command),
        "execution_strategies": execution_strategies,
        "created_paths": created_paths,
        "task_breakdown": task_breakdown,
        **final_summary,
        "log": log_text[-50000:],
    }


@APP.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.process is None or job.status != "running":
        raise HTTPException(status_code=409, detail="Job is not running.")

    job.status = "stopped"
    if os.name == "nt":
        job.process.terminate()
    else:
        try:
            os.killpg(os.getpgid(job.process.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception:
            job.process.send_signal(signal.SIGTERM)
    job.finished_at = _utc_now()
    return {"job_id": job.id, "status": job.status}


@APP.get("/api/watchdog")
def get_watchdog() -> dict:
    return get_wechat_watchdog_status()


@APP.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "service": "aa-cua-ui"}


@APP.post("/api/watchdog/start")
def start_watchdog(payload: WatchdogRequest) -> dict:
    return start_wechat_watchdog(
        interval_seconds=payload.interval_seconds,
        log_path=payload.log_path,
    )


@APP.post("/api/watchdog/stop")
def stop_watchdog() -> dict:
    return stop_wechat_watchdog()


@APP.get("/api/gmail-watchdog")
def get_gmail_watchdog() -> dict:
    payload = get_gmail_watchdog_status()
    payload.update(gmail_oauth_status())
    payload["gmail_attachment_dirs"] = _load_gmail_attachment_dirs()
    catalog_status = gmail_attachment_catalog_status()
    payload["gmail_attachment_catalog_path"] = _load_gmail_attachment_catalog_path() or catalog_status.get("catalog_path", "")
    payload["gmail_attachment_catalog_exists"] = Path(payload["gmail_attachment_catalog_path"]).expanduser().exists() if payload.get("gmail_attachment_catalog_path") else catalog_status.get("catalog_exists", False)
    payload["gmail_attachment_catalog_build"] = get_gmail_attachment_catalog_build_status()
    payload["gmail_attachment_catalog_preview"] = gmail_attachment_catalog_preview(limit=12)
    return payload


@APP.post("/api/gmail-watchdog/start")
def start_gmail_watchdog_api(payload: GmailWatchdogRequest) -> dict:
    try:
        return start_gmail_watchdog(
            interval_seconds=payload.interval_seconds,
            log_path=payload.log_path,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@APP.post("/api/gmail-watchdog/catalog-build/start")
def start_gmail_watchdog_catalog_build_api() -> dict:
    try:
        start_gmail_attachment_catalog_build()
        return get_gmail_watchdog()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@APP.post("/api/gmail-watchdog/stop")
def stop_gmail_watchdog_api() -> dict:
    return stop_gmail_watchdog()


@APP.post("/api/gmail-watchdog/rescan")
def rescan_gmail_watchdog_api() -> dict:
    reset_gmail_watchdog_seen()
    return get_gmail_watchdog()


@APP.post("/api/gmail-watchdog/attachment-dirs")
def save_gmail_watchdog_attachment_dirs(payload: GmailAttachmentDirsRequest) -> dict:
    directories = _save_gmail_attachment_dirs(payload.directories or [])
    response = get_gmail_watchdog()
    response["gmail_attachment_dirs"] = directories
    return response


@APP.post("/api/gmail-watchdog/attachment-dirs/current-finder")
def choose_gmail_watchdog_attachment_dir() -> dict:
    return {"directory": _get_current_finder_directory()}


@APP.post("/api/gmail-watchdog/attachment-catalog-path")
def save_gmail_watchdog_attachment_catalog_path(payload: GmailAttachmentCatalogPathRequest) -> dict:
    path_value = _save_gmail_attachment_catalog_path(payload.path)
    response = get_gmail_watchdog()
    response["gmail_attachment_catalog_path"] = path_value
    response["gmail_attachment_catalog_exists"] = Path(path_value).expanduser().exists()
    return response


@APP.post("/api/gmail-watchdog/oauth/start")
def start_gmail_watchdog_oauth() -> dict:
    return start_gmail_oauth_flow()


@APP.get("/api/gmail-watchdog/oauth/callback")
def gmail_watchdog_oauth_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        return HTMLResponse(
            f"<html><body><h3>Gmail 授权失败</h3><p>{html.escape(error)}</p><p><a href='/'>返回 AA-CUA</a></p></body></html>",
            status_code=400,
        )
    try:
        finish_gmail_oauth_flow(code, state)
    except Exception as exc:
        return HTMLResponse(
            f"<html><body><h3>Gmail 授权失败</h3><pre>{html.escape(str(exc))}</pre><p><a href='/'>返回 AA-CUA</a></p></body></html>",
            status_code=400,
        )
    return RedirectResponse(url="/")


app = APP


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("AGENT_S_UI_HOST", "127.0.0.1"),
        port=int(os.getenv("AGENT_S_UI_PORT", "8787")),
        reload=False,
    )


if __name__ == "__main__":
    main()
