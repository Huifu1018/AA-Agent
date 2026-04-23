from functools import partial
from io import BytesIO
import logging
import re
import textwrap
from typing import Dict, List, Optional, Tuple

import pytesseract
from PIL import Image

from gui_agents.s3.agents.grounding import ACI
from gui_agents.s3.core.module import BaseModule
from gui_agents.s3.memory.procedural_memory import PROCEDURAL_MEMORY
from gui_agents.s3.utils.common_utils import (
    call_llm_safe,
    call_llm_formatted,
    parse_code_from_string,
    split_thinking_response,
    create_pyautogui_code,
)
from gui_agents.s3.utils.formatters import (
    SINGLE_ACTION_FORMATTER,
    CODE_VALID_FORMATTER,
)

logger = logging.getLogger("desktopenv.agent")


class Worker(BaseModule):
    FILE_OPERATION_KEYWORDS = (
        "create",
        "new file",
        "write",
        "save",
        "copy",
        "rename",
        "move",
        "document",
        "text file",
        "word file",
        "pdf",
        "文件",
        "文档",
        "新建",
        "创建",
        "写",
        "复制",
        "重命名",
        "移动",
    )
    GUI_FOLLOWUP_KEYWORDS = (
        "wechat",
        "微信",
        "outlook",
        "email",
        "mail",
        "邮件",
        "邮箱",
        "send",
        "发送",
        "upload",
        "share",
        "群",
        "chat",
        "消息",
        "message",
    )
    WECHAT_FILE_KEYWORDS = (
        "file",
        "document",
        "txt",
        "pdf",
        "word",
        "image",
        "附件",
        "文件",
        "文档",
        "图片",
        "发送文件",
    )
    WECHAT_EMOJI_KEYWORDS = (
        "emoji",
        "emoticon",
        "smiley",
        "smile",
        "微笑",
        "笑脸",
        "表情",
        "emoji message",
        "微笑的表情",
    )
    WECHAT_MESSAGE_KEYWORDS = (
        "send",
        "message",
        "chat",
        "contact",
        "微信",
        "发消息",
        "发给",
        "给",
        "联系人",
        "聊天",
        "消息",
        "对话",
    )
    EMAIL_KEYWORDS = (
        "outlook",
        "email",
        "mail",
        "邮件",
        "邮箱",
    )
    EMAIL_EXTRACTION_KEYWORDS = (
        "latest",
        "newest",
        "most recent",
        "first email",
        "邮件内容",
        "最新",
        "最新内容",
        "第一封",
        "正文",
        "thread",
        "quoted",
        "引用",
        "历史内容",
    )
    EXISTING_FILE_KEYWORDS = (
        "existing",
        "already exists",
        "overwrite",
        "replace",
        "update",
        "edit",
        "open the file",
        "现有",
        "已有",
        "覆盖",
        "替换",
        "更新",
        "编辑",
        "打开这个文档",
        "打开该文档",
    )
    FINDER_REVEAL_KEYWORDS = (
        "finder",
        "find in finder",
        "show in finder",
        "reveal",
        "locate",
        "open in finder",
        "前往",
        "定位",
        "找到",
        "打开",
        "显示",
    )

    def __init__(
        self,
        worker_engine_params: Dict,
        grounding_agent: ACI,
        platform: str = "ubuntu",
        max_trajectory_length: int = 8,
        enable_reflection: bool = True,
    ):
        """
        Worker receives the main task and generates actions, without the need of hierarchical planning
        Args:
            worker_engine_params: Dict
                Parameters for the worker agent
            grounding_agent: Agent
                The grounding agent to use
            platform: str
                OS platform the agent runs on (darwin, linux, windows)
            max_trajectory_length: int
                The amount of images turns to keep
            enable_reflection: bool
                Whether to enable reflection
        """
        super().__init__(worker_engine_params, platform)

        self.temperature = worker_engine_params.get("temperature", 0.0)
        self.use_thinking = worker_engine_params.get("model", "") in [
            "claude-opus-4-20250514",
            "claude-sonnet-4-20250514",
            "claude-3-7-sonnet-20250219",
            "claude-sonnet-4-5-20250929",
            "claude-opus-4-5-20251101",
        ]
        self.grounding_agent = grounding_agent
        self.max_trajectory_length = max_trajectory_length
        self.enable_reflection = enable_reflection

        self.reset()

    def reset(self):
        if self.platform != "linux":
            skipped_actions = ["set_cell_values"]
        else:
            skipped_actions = []

        # Hide code agent action entirely if no env/controller is available
        if not getattr(self.grounding_agent, "env", None) or not getattr(
            getattr(self.grounding_agent, "env", None), "controller", None
        ):
            skipped_actions.append("call_code_agent")

        sys_prompt = PROCEDURAL_MEMORY.construct_simple_worker_procedural_memory(
            type(self.grounding_agent), skipped_actions=skipped_actions
        ).replace("CURRENT_OS", self.platform)

        self.generator_agent = self._create_agent(sys_prompt)
        self.reflection_agent = self._create_agent(
            PROCEDURAL_MEMORY.REFLECTION_ON_TRAJECTORY
        )

        self.turn_count = 0
        self.worker_history = []
        self.reflections = []
        self.cost_this_turn = 0
        self.screenshot_inputs = []
        self.wechat_profile_attempts = 0

    @staticmethod
    def _contains_path_hint(instruction: str) -> bool:
        return bool(re.search(r"(~?/[^,\n]+)", instruction))

    @classmethod
    def _contains_any_keyword(cls, instruction: str, keywords: Tuple[str, ...]) -> bool:
        lowered = instruction.lower()
        return any(keyword in lowered for keyword in keywords)

    @staticmethod
    def _strip_file_transfer_assistant_name(instruction: str) -> str:
        return (
            instruction.replace("文件传输助手", "")
            .replace("File Transfer Assistant", "")
            .replace("file transfer assistant", "")
        )

    @classmethod
    def _has_wechat_file_intent(cls, instruction: str) -> bool:
        sanitized = cls._strip_file_transfer_assistant_name(instruction)
        return cls._contains_any_keyword(sanitized, cls.WECHAT_FILE_KEYWORDS)

    @classmethod
    def _has_file_creation_intent(cls, instruction: str) -> bool:
        creation_keywords = (
            "create",
            "new file",
            "write",
            "save",
            "copy",
            "rename",
            "move",
            "新建",
            "创建",
            "写",
            "复制",
            "重命名",
            "移动",
        )
        return cls._contains_any_keyword(instruction, creation_keywords)

    @classmethod
    def _should_prepare_files_with_code_agent(cls, instruction: str) -> bool:
        return (
            cls._contains_path_hint(instruction)
            and cls._contains_any_keyword(instruction, cls.FILE_OPERATION_KEYWORDS)
            and cls._contains_any_keyword(instruction, cls.GUI_FOLLOWUP_KEYWORDS)
        )

    @classmethod
    def _should_reveal_path_with_code_agent(cls, instruction: str) -> bool:
        return cls._contains_path_hint(instruction) and cls._contains_any_keyword(
            instruction, cls.FINDER_REVEAL_KEYWORDS
        )

    @staticmethod
    def _build_file_prep_subtask(instruction: str) -> str:
        return textwrap.dedent(
            f"""\
            Prepare the local file portion of this task first, using the explicit file paths from the original instruction.
            Original instruction: {instruction}

            Requirements:
            - Create, copy, rename, or edit the requested local file(s) at the exact path(s) mentioned in the instruction.
            - Verify the file(s) exist with the requested names and content before stopping.
            - Do not use Finder, WeChat, or any GUI applications.
            - Do not send, upload, or share anything yet.
            - Stop as soon as the file(s) are ready for the remaining GUI steps.
            """
        ).strip()

    @staticmethod
    def _build_finder_reveal_subtask(instruction: str) -> str:
        return textwrap.dedent(
            f"""\
            Use the explicit local path from the original instruction to locate the requested file or folder in Finder.
            Original instruction: {instruction}

            Requirements:
            - Use the exact path mentioned in the instruction.
            - Reveal or open the requested file or folder directly in Finder.
            - Do not manually navigate Finder step-by-step if the path is already known.
            - Stop as soon as Finder is showing the requested item.
            """
        ).strip()

    @classmethod
    def _should_use_wechat_attachment_flow(cls, instruction: str) -> bool:
        return cls._contains_any_keyword(instruction, ("wechat", "微信")) and cls._has_wechat_file_intent(
            instruction
        )

    @staticmethod
    def _wechat_attachment_guidance() -> str:
        return textwrap.dedent(
            """\
            WECHAT FILE-SENDING RULES:
            - For non-text files, do not paste the file or file name into the chat.
            - First confirm the current chat title matches the requested chat.
            - Then click WeChat's attachment/file button in the chat toolbar.
            - Use the system file picker to choose the file and click Open.
            - After the send-confirmation sheet appears, confirm the recipient is correct and then click Send.
            """
        ).strip()

    @classmethod
    def _should_use_wechat_direct_emoji_flow(cls, instruction: str) -> bool:
        return (
            cls._contains_any_keyword(instruction, ("wechat", "微信"))
            and cls._contains_any_keyword(instruction, cls.WECHAT_EMOJI_KEYWORDS)
            and not cls._has_wechat_file_intent(instruction)
        )

    @staticmethod
    def _wechat_direct_emoji_guidance() -> str:
        return textwrap.dedent(
            """\
            WECHAT EMOJI-SENDING FAST PATH:
            - If the task is only to send a single emoji or smiling expression in WeChat, do not open the emoji picker first.
            - Search the requested contact by name, and when the exact contact match appears in results, prefer opening that exact match and verifying the top chat title before sending.
            - After the correct chat is open, focus the message input box, type the emoji character directly, and send it with Enter.
            - For requests like "微笑的表情" or "smiling emoji", prefer directly typing a standard smiling emoji such as 😊 unless the user requests a specific emoji.
            - Avoid repeatedly clicking the toolbar emoji button if the picker does not open after one attempt.
            """
        ).strip()

    @classmethod
    def _should_use_wechat_contact_chat_flow(cls, instruction: str) -> bool:
        return cls._contains_any_keyword(
            instruction, ("wechat", "微信")
        ) and cls._contains_any_keyword(instruction, cls.WECHAT_MESSAGE_KEYWORDS)

    @staticmethod
    def _wechat_contact_chat_guidance() -> str:
        return textwrap.dedent(
            """\
            WECHAT CONTACT-TO-CHAT FAST PATH:
            - When searching for a person in WeChat, treat the goal as opening the chat window directly, not opening the contact profile card.
            - In search results, prefer the exact name match under the 联系人 / Contacts section over group chats, files, or chat history.
            - Prefer opening the exact contact result in a way that directly enters the conversation, such as double-clicking the exact contact result or selecting it and confirming with Enter, before trying profile-style interactions.
            - After opening the result, verify the top chat title matches the requested contact name before sending any message.
            - If a contact profile card appears instead of the chat window, try the explicit 发消息 / Send Message control once. If that still does not open the chat, close the card and return to the search results rather than repeatedly clicking around inside the profile card.
            - Do not spend multiple turns guessing different buttons inside the same profile card. Prefer returning to the exact search result and entering the chat directly.
            """
        ).strip()

    @staticmethod
    def _extract_wechat_target_name(instruction: str) -> str:
        patterns = [
            r"给([^，。,\s]+)的微信",
            r"给微信中的([^，。,\s]+)",
            r"给([^，。,\s]+)发",
            r"微信中的([^，。,\s]+)",
            r"WeChat\s+contact\s+([A-Za-z0-9_\-\u4e00-\u9fff]+)",
            r"to\s+([A-Za-z0-9_\-\u4e00-\u9fff]+)\s+(?:on|in)\s+wechat",
        ]
        for pattern in patterns:
            match = re.search(pattern, instruction, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip("“”\"'")
        return ""

    @staticmethod
    def _extract_requested_emoji_count(instruction: str) -> int:
        match = re.search(r"(\d+)\s*个?[^\n，。, ]*(?:微笑|笑脸|表情|emoji|emoticon|smiley|smile)", instruction, flags=re.IGNORECASE)
        if match:
            return max(1, int(match.group(1)))
        return 1

    @staticmethod
    def _extract_explicit_message_text(instruction: str) -> str:
        quoted_patterns = [
            r"[“\"]([^”\"]+)[”\"]",
            r"‘([^’]+)’",
        ]
        for pattern in quoted_patterns:
            match = re.search(pattern, instruction)
            if match:
                return match.group(1).strip()

        text_patterns = [
            r"(?:发送|发|send)\s*(?:一条)?(?:消息|文本)?\s*[:：]?\s*([^\n]+)$",
            r"(?:内容|message)\s*[:：]?\s*([^\n]+)$",
        ]
        for pattern in text_patterns:
            match = re.search(pattern, instruction, flags=re.IGNORECASE)
            if match:
                candidate = match.group(1).strip().strip("。")
                if candidate:
                    return candidate
        return ""

    @staticmethod
    def _extract_wechat_file_reference(instruction: str) -> str:
        quoted_patterns = [
            r"[“\"]([^”\"]+\.(?:txt|pdf|doc|docx|ppt|pptx|xls|xlsx|csv|png|jpg|jpeg|gif|zip|md))[”\"]",
            r"[“\"]([^”\"]+)[”\"]",
        ]
        for pattern in quoted_patterns:
            match = re.search(pattern, instruction, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()

        file_patterns = [
            r"(?:发送|发|upload|send)\s*([^\n，。,]+?)文件",
            r"(?:发送|发|upload|send)\s*([^\n，。,]+\.(?:txt|pdf|doc|docx|ppt|pptx|xls|xlsx|csv|png|jpg|jpeg|gif|zip|md))",
        ]
        for pattern in file_patterns:
            match = re.search(pattern, instruction, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip().strip("“”\"' ")
        return ""

    def _build_wechat_simple_send_plan(
        self,
        instruction: str,
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        if not self._contains_any_keyword(instruction, ("wechat", "微信")):
            return None, None, None
        if self._has_wechat_file_intent(instruction):
            return None, None, None

        target_name = self._extract_wechat_target_name(instruction)
        if not target_name:
            return None, None, None

        if self._contains_any_keyword(instruction, self.WECHAT_EMOJI_KEYWORDS):
            emoji_count = self._extract_requested_emoji_count(instruction)
            message = "😊" * emoji_count
            summary = f"Fast-path send {emoji_count} emoji(s) to WeChat chat {target_name}."
            code = f"result = send_wechat_emoji({target_name!r}, '😊', {emoji_count})\nprint(result)"
            return message, code, summary

        explicit_message = self._extract_explicit_message_text(instruction)
        if explicit_message:
            summary = f"Fast-path send a WeChat text message to chat {target_name}."
            code = f"result = send_wechat_text({target_name!r}, {explicit_message!r}, press_enter=True)\nprint(result)"
            return explicit_message, code, summary

        return None, None, None

    def _build_wechat_file_send_plan(
        self,
        instruction: str,
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        if not self._contains_any_keyword(instruction, ("wechat", "微信")):
            return None, None, None
        if not self._has_wechat_file_intent(instruction):
            return None, None, None
        if self._has_file_creation_intent(instruction):
            return None, None, None

        target_name = self._extract_wechat_target_name(instruction)
        file_reference = self._extract_wechat_file_reference(instruction)
        if not target_name or not file_reference:
            return None, None, None

        summary = f"Fast-path send file {file_reference} to WeChat chat {target_name}."
        code = f"result = send_wechat_file({target_name!r}, {file_reference!r})\nprint(result)"
        return file_reference, code, summary

    def _maybe_fastpath_wechat_simple_send(
        self, instruction: str, obs: Dict, reflection: str, reflection_thoughts: str
    ):
        if self.turn_count != 0:
            return None

        message_text, python_code, summary = self._build_wechat_simple_send_plan(
            instruction
        )
        if not python_code:
            return None

        controller = getattr(getattr(self.grounding_agent, "env", None), "controller", None)
        if controller is None:
            return None

        plan = (
            "(Previous action verification)\n"
            "No previous action has been taken yet.\n\n"
            "(Screenshot Analysis)\n"
            "This is a simple WeChat send task, so I should avoid repeated GUI planning and use the deterministic local fast path.\n\n"
            "(Next Action)\n"
            "Use the built-in WeChat fast path to activate WeChat, search the exact chat, focus the input box, paste the message, and send it immediately.\n\n"
            "(Grounded Action)\n"
            "```python\n"
            + python_code
            + "\n```"
        )
        self.worker_history.append(plan)
        self.generator_agent.add_message(plan, role="assistant")
        logger.info("PLAN:\n %s", plan)

        result = controller.run_python_script(python_code)
        output_parts = [result.get("output", "").strip(), result.get("error", "").strip()]
        output_summary = "\n".join(part for part in output_parts if part).strip()
        completion_reason = "DONE" if result.get("status") == "ok" else "FAIL"

        self.grounding_agent.last_code_agent_result = {
            "task_instruction": instruction,
            "requested_task": instruction,
            "steps_executed": 1,
            "budget": 1,
            "completion_reason": completion_reason,
            "summary": output_summary or summary,
            "execution_history": [{"action": python_code}],
            "was_full_task": completion_reason == "DONE",
        }
        self.grounding_agent.last_code_agent_was_full_task = completion_reason == "DONE"

        exec_code = (
            self.grounding_agent.done()
            if completion_reason == "DONE"
            else self.grounding_agent.fail()
        )
        executor_info = {
            "plan": plan,
            "plan_code": python_code,
            "exec_code": exec_code,
            "reflection": reflection,
            "reflection_thoughts": reflection_thoughts,
            "code_agent_output": self.grounding_agent.last_code_agent_result,
            "fast_path": "wechat_simple_send",
            "message_text": message_text,
        }
        self.turn_count += 1
        self.screenshot_inputs.append(obs["screenshot"])
        self.flush_messages()
        return executor_info, [exec_code]

    def _maybe_fastpath_wechat_file_send(
        self, instruction: str, obs: Dict, reflection: str, reflection_thoughts: str
    ):
        if self.turn_count != 0:
            return None

        file_reference, python_code, summary = self._build_wechat_file_send_plan(
            instruction
        )
        if not python_code:
            return None

        controller = getattr(getattr(self.grounding_agent, "env", None), "controller", None)
        if controller is None:
            return None

        plan = (
            "(Previous action verification)\n"
            "No previous action has been taken yet.\n\n"
            "(Screenshot Analysis)\n"
            "This is a WeChat file-send task with a specific chat target and file reference, so I should use the deterministic local fast path instead of exploring the GUI step by step.\n\n"
            "(Next Action)\n"
            "Use the built-in WeChat file-send fast path to activate WeChat, search the exact chat, click the attachment button, choose the file directly by path, and send it.\n\n"
            "(Grounded Action)\n"
            "```python\n"
            + python_code
            + "\n```"
        )
        self.worker_history.append(plan)
        self.generator_agent.add_message(plan, role="assistant")
        logger.info("PLAN:\n %s", plan)

        result = controller.run_python_script(python_code)
        output_parts = [result.get("output", "").strip(), result.get("error", "").strip()]
        output_summary = "\n".join(part for part in output_parts if part).strip()
        completion_reason = "DONE" if result.get("status") == "ok" else "FAIL"

        self.grounding_agent.last_code_agent_result = {
            "task_instruction": instruction,
            "requested_task": instruction,
            "steps_executed": 1,
            "budget": 1,
            "completion_reason": completion_reason,
            "summary": output_summary or summary,
            "execution_history": [{"action": python_code}],
            "was_full_task": completion_reason == "DONE",
        }
        self.grounding_agent.last_code_agent_was_full_task = completion_reason == "DONE"

        exec_code = (
            self.grounding_agent.done()
            if completion_reason == "DONE"
            else self.grounding_agent.fail()
        )
        executor_info = {
            "plan": plan,
            "plan_code": python_code,
            "exec_code": exec_code,
            "reflection": reflection,
            "reflection_thoughts": reflection_thoughts,
            "code_agent_output": self.grounding_agent.last_code_agent_result,
            "fast_path": "wechat_file_send",
            "file_reference": file_reference,
        }
        self.turn_count += 1
        self.screenshot_inputs.append(obs["screenshot"])
        self.flush_messages()
        return executor_info, [exec_code]

    @staticmethod
    def _ocr_screen_text(obs: Dict) -> str:
        screenshot = obs.get("screenshot") if obs else None
        if not screenshot:
            return ""
        try:
            image = Image.open(BytesIO(screenshot))
            return pytesseract.image_to_string(image, lang="chi_sim+eng")
        except Exception:
            return ""

    def _infer_wechat_message_stage(self, instruction: str, obs: Dict) -> Tuple[str, str]:
        if not self._should_use_wechat_contact_chat_flow(instruction):
            return "", ""

        target_name = self._extract_wechat_target_name(instruction)
        ocr_text = self._ocr_screen_text(obs)
        lowered = ocr_text.lower()

        if "wechat" not in lowered and "微信" not in ocr_text:
            return "open_wechat", target_name

        if target_name and target_name in ocr_text and any(
            marker in ocr_text for marker in ("发消息", "发送消息", "消息免打扰", "音视频通话", "联系人信息")
        ):
            return "profile_card", target_name

        if target_name and target_name in ocr_text and any(
            marker in ocr_text for marker in ("联系人", "群聊", "聊天记录", "搜索指定内容")
        ):
            return "open_contact_result", target_name

        if target_name and target_name in ocr_text:
            return "chat_open", target_name

        return "search_contact", target_name

    @staticmethod
    def _wechat_stage_guidance(stage: str, target_name: str) -> str:
        target_hint = f"目标联系人是 {target_name}。\n" if target_name else ""
        stage_guidance = {
            "open_wechat": (
                "当前阶段：先把微信切到前台。\n"
                "- 只做能直接打开或切回微信的动作。\n"
                "- 不要在其他应用里继续规划联系人或消息发送。"
            ),
            "search_contact": (
                "当前阶段：搜索联系人。\n"
                "- 先聚焦微信左上角搜索框。\n"
                "- 输入目标联系人名。\n"
                "- 优先用最短路径进入搜索结果，不要在其他窗口停留。"
            ),
            "open_contact_result": (
                "当前阶段：从搜索结果直接进入聊天。\n"
                "- 只选 联系人/Contacts 分区里的精确匹配项。\n"
                "- 优先使用能直接进入聊天的交互方式，不要主动打开资料卡。\n"
                "- 不要点击群聊、聊天记录、文件结果。"
            ),
            "profile_card": (
                "当前阶段：资料卡纠偏。\n"
                "- 资料卡不是目标状态。\n"
                "- 最多只尝试一次明确的 发消息/Send Message。\n"
                "- 如果仍未进入聊天，就关闭资料卡并回到搜索结果，不要继续在资料卡内试错。"
            ),
            "chat_open": (
                "当前阶段：聊天窗口已打开。\n"
                "- 先确认顶部标题就是目标联系人。\n"
                "- 如果是表情任务，直接聚焦输入框并输入 emoji。\n"
                "- 如果是文本任务，直接在输入框中输入并发送。"
            ),
        }.get(stage, "")
        return (target_hint + stage_guidance).strip()

    @classmethod
    def _should_use_email_extraction_flow(cls, instruction: str) -> bool:
        return cls._contains_any_keyword(
            instruction, cls.EMAIL_KEYWORDS
        ) and (
            cls._contains_any_keyword(instruction, cls.EMAIL_EXTRACTION_KEYWORDS)
            or cls._contains_path_hint(instruction)
            or cls._contains_any_keyword(instruction, cls.EXISTING_FILE_KEYWORDS)
        )

    @staticmethod
    def _email_extraction_guidance() -> str:
        return textwrap.dedent(
            """\
            OUTLOOK EMAIL-TO-FILE RULES:
            - If the user asks for the first email's latest content, capture only the newest email body and exclude quoted history, earlier thread replies, signatures, and forwarded context unless explicitly requested.
            - If the task names an existing target file or gives an explicit file path, open that existing file and overwrite or replace its content instead of creating a new document.
            - If the target file is a txt/text file, treat it as a plain text file to edit and save in place. Do not create a new Word document unless the user explicitly asks for a Word file.
            - Before finishing, verify the requested content was actually written into the named target file and saved.
            """
        ).strip()

    def _maybe_prepare_files_first(
        self, instruction: str, obs: Dict, reflection: str, reflection_thoughts: str
    ):
        if self.turn_count != 0:
            return None

        if not self._should_prepare_files_with_code_agent(instruction):
            return None

        subtask = self._build_file_prep_subtask(instruction)
        plan = (
            "(Previous action verification)\n"
            "No previous action has been taken yet.\n\n"
            "(Screenshot Analysis)\n"
            "The desktop state is not important for the first step because the task includes explicit local file paths.\n\n"
            "(Next Action)\n"
            "I should use the code agent first to prepare the required local file(s) at the exact requested path before spending GUI steps on WeChat or Finder navigation.\n\n"
            "(Grounded Action)\n"
            "```python\n"
            f"agent.call_code_agent({subtask!r})\n"
            "```"
        )
        self.worker_history.append(plan)
        self.generator_agent.add_message(plan, role="assistant")
        logger.info("PLAN:\n %s", plan)

        plan_code = f"agent.call_code_agent({subtask!r})"
        exec_code = self.grounding_agent.call_code_agent(subtask)
        executor_info = {
            "plan": plan,
            "plan_code": plan_code,
            "exec_code": exec_code,
            "reflection": reflection,
            "reflection_thoughts": reflection_thoughts,
            "code_agent_output": (
                self.grounding_agent.last_code_agent_result
                if hasattr(self.grounding_agent, "last_code_agent_result")
                and self.grounding_agent.last_code_agent_result is not None
                else None
            ),
        }
        self.turn_count += 1
        self.screenshot_inputs.append(obs["screenshot"])
        self.flush_messages()
        return executor_info, [exec_code]

    def _maybe_reveal_path_first(
        self, instruction: str, obs: Dict, reflection: str, reflection_thoughts: str
    ):
        if self.turn_count != 0:
            return None

        if not self._should_reveal_path_with_code_agent(instruction):
            return None

        subtask = self._build_finder_reveal_subtask(instruction)
        plan = (
            "(Previous action verification)\n"
            "No previous action has been taken yet.\n\n"
            "(Screenshot Analysis)\n"
            "The task already includes an explicit local path, so I do not need to navigate manually through Finder first.\n\n"
            "(Next Action)\n"
            "I should use the code agent to reveal the requested path directly in Finder before taking any additional GUI actions.\n\n"
            "(Grounded Action)\n"
            "```python\n"
            f"agent.call_code_agent({subtask!r})\n"
            "```"
        )
        self.worker_history.append(plan)
        self.generator_agent.add_message(plan, role="assistant")
        logger.info("PLAN:\n %s", plan)

        plan_code = f"agent.call_code_agent({subtask!r})"
        exec_code = self.grounding_agent.call_code_agent(subtask)
        executor_info = {
            "plan": plan,
            "plan_code": plan_code,
            "exec_code": exec_code,
            "reflection": reflection,
            "reflection_thoughts": reflection_thoughts,
            "code_agent_output": (
                self.grounding_agent.last_code_agent_result
                if hasattr(self.grounding_agent, "last_code_agent_result")
                and self.grounding_agent.last_code_agent_result is not None
                else None
            ),
        }
        self.turn_count += 1
        self.screenshot_inputs.append(obs["screenshot"])
        self.flush_messages()
        return executor_info, [exec_code]

    def flush_messages(self):
        """Flush messages based on the model's context limits.

        This method ensures that the agent's message history does not exceed the maximum trajectory length.

        Side Effects:
            - Modifies the messages of generator, reflection, and bon_judge agents to fit within the context limits.
        """
        engine_type = self.engine_params.get("engine_type", "")

        # Flush strategy for long-context models: keep all text, only keep latest images
        if engine_type in ["anthropic", "openai", "gemini", "kimi"]:
            max_images = self.max_trajectory_length
            for agent in [self.generator_agent, self.reflection_agent]:
                if agent is None:
                    continue
                # keep latest k images
                img_count = 0
                for i in range(len(agent.messages) - 1, -1, -1):
                    for j in range(len(agent.messages[i]["content"])):
                        if "image" in agent.messages[i]["content"][j].get("type", ""):
                            img_count += 1
                            if img_count > max_images:
                                del agent.messages[i]["content"][j]

        # Flush strategy for non-long-context models: drop full turns
        else:
            # generator msgs are alternating [user, assistant], so 2 per round
            if len(self.generator_agent.messages) > 2 * self.max_trajectory_length + 1:
                self.generator_agent.messages.pop(1)
                self.generator_agent.messages.pop(1)
            # reflector msgs are all [(user text, user image)], so 1 per round
            if len(self.reflection_agent.messages) > self.max_trajectory_length + 1:
                self.reflection_agent.messages.pop(1)

    def _generate_reflection(self, instruction: str, obs: Dict) -> Tuple[str, str]:
        """
        Generate a reflection based on the current observation and instruction.

        Args:
            instruction (str): The task instruction.
            obs (Dict): The current observation containing the screenshot.

        Returns:
            Optional[str, str]: The generated reflection text and thoughts, if any (turn_count > 0).

        Side Effects:
            - Updates reflection agent's history
            - Generates reflection response with API call
        """
        reflection = None
        reflection_thoughts = None
        if self.enable_reflection:
            # Load the initial message
            if self.turn_count == 0:
                text_content = textwrap.dedent(
                    f"""
                    Task Description: {instruction}
                    Current Trajectory below:
                    """
                )
                updated_sys_prompt = (
                    self.reflection_agent.system_prompt + "\n" + text_content
                )
                self.reflection_agent.add_system_prompt(updated_sys_prompt)
                self.reflection_agent.add_message(
                    text_content="The initial screen is provided. No action has been taken yet.",
                    image_content=obs["screenshot"],
                    role="user",
                )
            # Load the latest action
            else:
                self.reflection_agent.add_message(
                    text_content=self.worker_history[-1],
                    image_content=obs["screenshot"],
                    role="user",
                )
                full_reflection = call_llm_safe(
                    self.reflection_agent,
                    temperature=self.temperature,
                    use_thinking=self.use_thinking,
                )
                reflection, reflection_thoughts = split_thinking_response(
                    full_reflection
                )
                self.reflections.append(reflection)
                logger.info("REFLECTION THOUGHTS: %s", reflection_thoughts)
                logger.info("REFLECTION: %s", reflection)
        return reflection, reflection_thoughts

    def generate_next_action(self, instruction: str, obs: Dict) -> Tuple[Dict, List]:
        """
        Predict the next action(s) based on the current observation.
        """

        self.grounding_agent.assign_screenshot(obs)
        self.grounding_agent.set_task_instruction(instruction)

        generator_message = (
            ""
            if self.turn_count > 0
            else "The initial screen is provided. No action has been taken yet."
        )

        # Load the task into the system prompt
        if self.turn_count == 0:
            prompt_with_instructions = self.generator_agent.system_prompt.replace(
                "TASK_DESCRIPTION", instruction
            )
            self.generator_agent.add_system_prompt(prompt_with_instructions)

        # Get the per-step reflection
        reflection, reflection_thoughts = self._generate_reflection(instruction, obs)
        if reflection:
            generator_message += f"REFLECTION: You may use this reflection on the previous action and overall trajectory:\n{reflection}\n"

        wechat_stage, wechat_target = self._infer_wechat_message_stage(instruction, obs)
        if wechat_stage:
            if wechat_stage == "profile_card":
                self.wechat_profile_attempts += 1
            else:
                self.wechat_profile_attempts = 0

            stage_guidance = self._wechat_stage_guidance(wechat_stage, wechat_target)
            if self.wechat_profile_attempts > 1 and wechat_stage == "profile_card":
                stage_guidance += (
                    "\n- 你已经在资料卡阶段停留超过一次。下一步不要继续点资料卡按钮，"
                    "应优先关闭资料卡并返回搜索结果。"
                )

            generator_message += "\nSPECIAL INSTRUCTION:\n" + stage_guidance + "\n"

        if self._should_use_wechat_attachment_flow(instruction):
            generator_message += (
                "\nSPECIAL INSTRUCTION:\n"
                + self._wechat_attachment_guidance()
                + "\n"
            )

        if self._should_use_wechat_contact_chat_flow(instruction):
            generator_message += (
                "\nSPECIAL INSTRUCTION:\n"
                + self._wechat_contact_chat_guidance()
                + "\n"
            )

        if self._should_use_wechat_direct_emoji_flow(instruction):
            generator_message += (
                "\nSPECIAL INSTRUCTION:\n"
                + self._wechat_direct_emoji_guidance()
                + "\n"
            )

        if self._should_use_email_extraction_flow(instruction):
            generator_message += (
                "\nSPECIAL INSTRUCTION:\n"
                + self._email_extraction_guidance()
                + "\n"
            )

        file_prep_result = self._maybe_prepare_files_first(
            instruction, obs, reflection, reflection_thoughts
        )
        if file_prep_result is not None:
            return file_prep_result

        wechat_fastpath_result = self._maybe_fastpath_wechat_simple_send(
            instruction, obs, reflection, reflection_thoughts
        )
        if wechat_fastpath_result is not None:
            return wechat_fastpath_result

        wechat_file_fastpath_result = self._maybe_fastpath_wechat_file_send(
            instruction, obs, reflection, reflection_thoughts
        )
        if wechat_file_fastpath_result is not None:
            return wechat_file_fastpath_result

        finder_reveal_result = self._maybe_reveal_path_first(
            instruction, obs, reflection, reflection_thoughts
        )
        if finder_reveal_result is not None:
            return finder_reveal_result

        # Get the grounding agent's knowledge base buffer
        generator_message += (
            f"\nCurrent Text Buffer = [{','.join(self.grounding_agent.notes)}]\n"
        )

        # Add code agent result from previous step if available (from full task or subtask execution)
        if (
            hasattr(self.grounding_agent, "last_code_agent_result")
            and self.grounding_agent.last_code_agent_result is not None
        ):
            code_result = self.grounding_agent.last_code_agent_result

            requested_task = code_result.get("requested_task")
            requested_task_matches_instruction = (
                self.grounding_agent._normalize_task_text(requested_task)
                == self.grounding_agent._normalize_task_text(instruction)
            )
            completed_full_task = (
                code_result.get("completion_reason") == "DONE"
                and (
                    code_result.get("was_full_task") is True
                    or requested_task_matches_instruction
                )
            )

            logger.info(
                "WORKER_CODE_AGENT_STATUS - completion_reason=%s, was_full_task=%s, requested_task_matches_instruction=%s",
                code_result.get("completion_reason"),
                code_result.get("was_full_task"),
                requested_task_matches_instruction,
            )

            # If the code agent completed the full task successfully, avoid re-calling it.
            if completed_full_task:
                plan = (
                    "(Previous action verification)\n"
                    "The previous action was a full-task code agent execution, and it completed successfully.\n\n"
                    "(Screenshot Analysis)\n"
                    "The current desktop state does not require additional GUI interaction for this task.\n\n"
                    "(Next Action)\n"
                    "The full task has already been completed by the code agent, so I should finish the task.\n\n"
                    "(Grounded Action)\n"
                    "```python\n"
                    "agent.done()\n"
                    "```"
                )
                self.worker_history.append(plan)
                self.generator_agent.add_message(plan, role="assistant")
                logger.info(
                    "PLAN:\n %s", plan
                )
                exec_code = self.grounding_agent.done()
                executor_info = {
                    "plan": plan,
                    "plan_code": "agent.done()",
                    "exec_code": exec_code,
                    "reflection": reflection,
                    "reflection_thoughts": reflection_thoughts,
                    "code_agent_output": code_result,
                }
                self.grounding_agent.last_code_agent_result = None
                self.grounding_agent.last_code_agent_was_full_task = False
                self.turn_count += 1
                self.screenshot_inputs.append(obs["screenshot"])
                self.flush_messages()
                return executor_info, [exec_code]

            generator_message += f"\nCODE AGENT RESULT:\n"
            generator_message += (
                f"Task/Subtask Instruction: {code_result['task_instruction']}\n"
            )
            generator_message += f"Steps Completed: {code_result['steps_executed']}\n"
            generator_message += f"Max Steps: {code_result['budget']}\n"
            generator_message += (
                f"Completion Reason: {code_result['completion_reason']}\n"
            )
            generator_message += f"Summary: {code_result['summary']}\n"
            if code_result["execution_history"]:
                generator_message += f"Execution History:\n"
                for i, step in enumerate(code_result["execution_history"]):
                    action = step["action"]
                    # Format code snippets with proper backticks
                    if "```python" in action:
                        # Extract Python code and format it
                        code_start = action.find("```python") + 9
                        code_end = action.find("```", code_start)
                        if code_end != -1:
                            python_code = action[code_start:code_end].strip()
                            generator_message += (
                                f"Step {i+1}: \n```python\n{python_code}\n```\n"
                            )
                        else:
                            generator_message += f"Step {i+1}: \n{action}\n"
                    elif "```bash" in action:
                        # Extract Bash code and format it
                        code_start = action.find("```bash") + 7
                        code_end = action.find("```", code_start)
                        if code_end != -1:
                            bash_code = action[code_start:code_end].strip()
                            generator_message += (
                                f"Step {i+1}: \n```bash\n{bash_code}\n```\n"
                            )
                        else:
                            generator_message += f"Step {i+1}: \n{action}\n"
                    else:
                        generator_message += f"Step {i+1}: \n{action}\n"
            generator_message += "\n"

            # Log the code agent result section for debugging (truncated execution history)
            log_message = f"\nCODE AGENT RESULT:\n"
            log_message += (
                f"Task/Subtask Instruction: {code_result['task_instruction']}\n"
            )
            log_message += f"Steps Completed: {code_result['steps_executed']}\n"
            log_message += f"Max Steps: {code_result['budget']}\n"
            log_message += f"Completion Reason: {code_result['completion_reason']}\n"
            log_message += f"Summary: {code_result['summary']}\n"
            if code_result["execution_history"]:
                log_message += f"Execution History (truncated):\n"
                # Only log first 3 steps and last 2 steps to keep logs manageable
                total_steps = len(code_result["execution_history"])
                for i, step in enumerate(code_result["execution_history"]):
                    if i < 3 or i >= total_steps - 2:  # First 3 and last 2 steps
                        action = step["action"]
                        if "```python" in action:
                            code_start = action.find("```python") + 9
                            code_end = action.find("```", code_start)
                            if code_end != -1:
                                python_code = action[code_start:code_end].strip()
                                log_message += (
                                    f"Step {i+1}: ```python\n{python_code}\n```\n"
                                )
                            else:
                                log_message += f"Step {i+1}: {action}\n"
                        elif "```bash" in action:
                            code_start = action.find("```bash") + 7
                            code_end = action.find("```", code_start)
                            if code_end != -1:
                                bash_code = action[code_start:code_end].strip()
                                log_message += (
                                    f"Step {i+1}: ```bash\n{bash_code}\n```\n"
                                )
                            else:
                                log_message += f"Step {i+1}: {action}\n"
                        else:
                            log_message += f"Step {i+1}: {action}\n"
                    elif i == 3 and total_steps > 5:
                        log_message += f"... (truncated {total_steps - 5} steps) ...\n"

            logger.info(
                f"WORKER_CODE_AGENT_RESULT_SECTION - Step {self.turn_count + 1}: Code agent result added to generator message:\n{log_message}"
            )

            # Reset the code agent result after adding it to context
            self.grounding_agent.last_code_agent_result = None
            self.grounding_agent.last_code_agent_was_full_task = False

        # Finalize the generator message
        self.generator_agent.add_message(
            generator_message, image_content=obs["screenshot"], role="user"
        )

        # Generate the plan and next action
        format_checkers = [
            SINGLE_ACTION_FORMATTER,
            partial(CODE_VALID_FORMATTER, self.grounding_agent, obs),
        ]
        plan = call_llm_formatted(
            self.generator_agent,
            format_checkers,
            temperature=self.temperature,
            use_thinking=self.use_thinking,
        )
        self.worker_history.append(plan)
        self.generator_agent.add_message(plan, role="assistant")
        logger.info("PLAN:\n %s", plan)

        # Extract the next action from the plan
        plan_code = parse_code_from_string(plan)
        try:
            assert plan_code, "Plan code should not be empty"
            exec_code = create_pyautogui_code(self.grounding_agent, plan_code, obs)
        except Exception as e:
            logger.error(
                f"Could not evaluate the following plan code:\n{plan_code}\nError: {e}"
            )
            exec_code = self.grounding_agent.wait(
                1.333
            )  # Skip a turn if the code cannot be evaluated

        executor_info = {
            "plan": plan,
            "plan_code": plan_code,
            "exec_code": exec_code,
            "reflection": reflection,
            "reflection_thoughts": reflection_thoughts,
            "code_agent_output": (
                self.grounding_agent.last_code_agent_result
                if hasattr(self.grounding_agent, "last_code_agent_result")
                and self.grounding_agent.last_code_agent_result is not None
                else None
            ),
        }
        self.turn_count += 1
        self.screenshot_inputs.append(obs["screenshot"])
        self.flush_messages()
        return executor_info, [exec_code]
