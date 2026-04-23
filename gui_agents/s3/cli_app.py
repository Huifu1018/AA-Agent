import argparse
import datetime
import io
import logging
import os
import platform
import shlex
import pyautogui
import signal
import sys
import time

from PIL import Image

from gui_agents.s3.agents.grounding import OSWorldACI
from gui_agents.s3.agents.agent_s import AgentS3
from gui_agents.s3.utils.local_env import LocalEnv

current_platform = platform.system().lower()

# Global flag to track pause state for debugging
paused = False


def get_char():
    """Get a single character from stdin without pressing Enter"""
    try:
        # Import termios and tty on Unix-like systems
        if platform.system() in ["Darwin", "Linux"]:
            import termios
            import tty

            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setraw(sys.stdin.fileno())
                ch = sys.stdin.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            return ch
        else:
            # Windows fallback
            import msvcrt

            return msvcrt.getch().decode("utf-8", errors="ignore")
    except:
        return input()  # Fallback for non-terminal environments


def signal_handler(signum, frame):
    """Handle Ctrl+C signal for debugging during agent execution"""
    global paused

    if not paused:
        print("\n\n🔸 Agent-S Workflow Paused 🔸")
        print("=" * 50)
        print("Options:")
        print("  • Press Ctrl+C again to quit")
        print("  • Press Esc to resume workflow")
        print("=" * 50)

        paused = True

        while paused:
            try:
                print("\n[PAUSED] Waiting for input... ", end="", flush=True)
                char = get_char()

                if ord(char) == 3:  # Ctrl+C
                    print("\n\n🛑 Exiting Agent-S...")
                    sys.exit(0)
                elif ord(char) == 27:  # Esc
                    print("\n\n▶️  Resuming Agent-S workflow...")
                    paused = False
                    break
                else:
                    print(f"\n   Unknown command: '{char}' (ord: {ord(char)})")

            except KeyboardInterrupt:
                print("\n\n🛑 Exiting Agent-S...")
                sys.exit(0)
    else:
        # Already paused, second Ctrl+C means quit
        print("\n\n🛑 Exiting Agent-S...")
        sys.exit(0)


# Set up signal handler for Ctrl+C
signal.signal(signal.SIGINT, signal_handler)

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

datetime_str: str = datetime.datetime.now().strftime("%Y%m%d@%H%M%S")

log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)

file_handler = logging.FileHandler(
    os.path.join("logs", "normal-{:}.log".format(datetime_str)), encoding="utf-8"
)
debug_handler = logging.FileHandler(
    os.path.join("logs", "debug-{:}.log".format(datetime_str)), encoding="utf-8"
)
stdout_handler = logging.StreamHandler(sys.stdout)
sdebug_handler = logging.FileHandler(
    os.path.join("logs", "sdebug-{:}.log".format(datetime_str)), encoding="utf-8"
)

file_handler.setLevel(logging.INFO)
debug_handler.setLevel(logging.DEBUG)
stdout_handler.setLevel(logging.INFO)
sdebug_handler.setLevel(logging.DEBUG)

formatter = logging.Formatter(
    fmt="\x1b[1;33m[%(asctime)s \x1b[31m%(levelname)s \x1b[32m%(module)s/%(lineno)d-%(processName)s\x1b[1;33m] \x1b[0m%(message)s"
)
file_handler.setFormatter(formatter)
debug_handler.setFormatter(formatter)
stdout_handler.setFormatter(formatter)
sdebug_handler.setFormatter(formatter)

stdout_handler.addFilter(logging.Filter("desktopenv"))
sdebug_handler.addFilter(logging.Filter("desktopenv"))

logger.addHandler(file_handler)
logger.addHandler(debug_handler)
logger.addHandler(stdout_handler)
logger.addHandler(sdebug_handler)

platform_os = platform.system()


def _first_env(*names):
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return ""


def _apply_kimi_defaults(args):
    default_openai_model = "gpt-5-2025-08-07"

    if args.provider == "kimi":
        if not args.model or args.model == default_openai_model:
            args.model = _first_env(
                "KIMI_MODEL",
                "ANTHROPIC_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL",
            ) or "kimi-k2.5"
        if not args.model_api_key:
            args.model_api_key = _first_env("KIMI_API_KEY", "ANTHROPIC_API_KEY")
        if not args.model_url:
            args.model_url = _first_env("KIMI_BASE_URL", "ANTHROPIC_BASE_URL")

    if args.ground_provider == "kimi":
        if not args.ground_model:
            args.ground_model = _first_env(
                "KIMI_GROUND_MODEL",
                "KIMI_MODEL",
                "ANTHROPIC_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL",
            ) or "kimi-k2.5"
        if not args.ground_api_key:
            args.ground_api_key = _first_env(
                "KIMI_GROUND_API_KEY",
                "KIMI_API_KEY",
                "ANTHROPIC_API_KEY",
            )
        if not args.ground_url:
            args.ground_url = _first_env(
                "KIMI_GROUND_BASE_URL",
                "KIMI_BASE_URL",
                "ANTHROPIC_BASE_URL",
            )

    return args


def show_permission_dialog(code: str, action_description: str):
    """Show a platform-specific permission dialog and return True if approved."""
    if platform.system() == "Darwin":
        result = os.system(
            f'osascript -e \'display dialog "Do you want to execute this action?\n\n{code} which will try to {action_description}" with title "Action Permission" buttons {{"Cancel", "OK"}} default button "OK" cancel button "Cancel"\''
        )
        return result == 0
    elif platform.system() == "Linux":
        result = os.system(
            f'zenity --question --title="Action Permission" --text="Do you want to execute this action?\n\n{code}" --width=400 --height=200'
        )
        return result == 0
    return False


def scale_screen_dimensions(width: int, height: int, max_dim_size: int):
    scale_factor = min(max_dim_size / width, max_dim_size / height, 1)
    safe_width = int(width * scale_factor)
    safe_height = int(height * scale_factor)
    return safe_width, safe_height


def _truncate_text(text: str, max_len: int = 700) -> str:
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def _escape_applescript(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def show_status_dialog(title: str, message: str):
    safe_title = _escape_applescript(title)
    safe_message = _escape_applescript(_truncate_text(message, 1600))
    if platform.system() == "Darwin":
        os.system(
            f'osascript -e \'display dialog "{safe_message}" with title "{safe_title}" buttons {{"OK"}} default button "OK"\''
        )
    elif platform.system() == "Linux":
        os.system(
            f'zenity --info --title={shlex.quote(title)} --text={shlex.quote(message)} --width=420 --height=240'
        )


def _format_duration(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    minutes, secs = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分 {secs} 秒"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def build_task_feedback(
    status: str,
    reason: str,
    steps_completed: int,
    max_steps: int,
    last_info: dict | None,
    last_code: str | None,
    duration_seconds: float | None = None,
):
    headline = {
        "completed": "AA-CUA 已完成任务。",
        "failed": "AA-CUA 未完成任务。",
        "incomplete": "AA-CUA 未在步数上限内完成任务。",
    }.get(status, "AA-CUA 任务已结束。")

    last_action = ""
    if last_info:
        last_action = (
            last_info.get("plan_code")
            or last_info.get("exec_code")
            or last_code
            or ""
        )

    code_agent_output = last_info.get("code_agent_output") if last_info else None
    code_agent_summary = ""
    if code_agent_output:
        code_agent_summary = code_agent_output.get("summary", "")

    plan_summary = last_info.get("plan", "") if last_info else ""

    lines = [
        headline,
        f"已执行步数: {steps_completed}/{max_steps}",
        f"结束原因: {reason}",
    ]
    if duration_seconds is not None:
        lines.insert(2, f"任务耗时: {_format_duration(duration_seconds)}")
    if last_action:
        lines.append(f"最后一步动作: {_truncate_text(last_action, 240)}")
    if code_agent_summary:
        lines.append(f"代码代理摘要: {_truncate_text(code_agent_summary, 320)}")
    elif plan_summary:
        lines.append(f"最后计划摘要: {_truncate_text(plan_summary, 320)}")

    message = "\n".join(lines)
    logger.info("AGENT_S_FINAL_STATUS: %s", status)
    logger.info("AGENT_S_FINAL_REASON: %s", reason)
    logger.info("AGENT_S_FINAL_SUMMARY: %s", message.replace("\n", " | "))
    return message


def run_agent(agent, instruction: str, scaled_width: int, scaled_height: int):
    global paused
    started_at = time.time()
    obs = {}
    traj = "Task:\n" + instruction
    subtask_traj = ""
    max_steps = int(os.getenv("AGENT_S_MAX_STEPS", "25"))
    last_info = None
    last_code = None
    final_status = "incomplete"
    final_reason = "Reached the maximum step budget without an explicit completion signal."
    steps_completed = 0

    for step in range(max_steps):
        # Check if we're in paused state and wait
        while paused:
            time.sleep(0.1)
        print(f"\n🔄 Step {step + 1}/{max_steps}: Capturing screen and preparing next action...")
        # Get screen shot using pyautogui
        screenshot = pyautogui.screenshot()
        screenshot = screenshot.resize((scaled_width, scaled_height), Image.LANCZOS)

        # Save the screenshot to a BytesIO object
        buffered = io.BytesIO()
        screenshot.save(buffered, format="PNG")

        # Get the byte value of the screenshot
        screenshot_bytes = buffered.getvalue()
        # Convert to base64 string.
        obs["screenshot"] = screenshot_bytes

        # Check again for pause state before prediction
        while paused:
            time.sleep(0.1)

        print(f"🤖 Step {step + 1}/{max_steps}: Getting next action from agent...")

        # Get next action code from the agent
        try:
            info, code = agent.predict(instruction=instruction, observation=obs)
        except Exception as exc:
            final_status = "failed"
            final_reason = f"Agent planning failed with: {exc}"
            logger.exception("AGENT_S_PREDICTION_FAILED")
            break
        last_info = info
        last_code = code[0] if code else ""
        steps_completed = step + 1

        if "done" in code[0].lower() or "fail" in code[0].lower():
            final_status = "completed" if "done" in code[0].lower() else "failed"
            final_reason = (
                "The agent emitted agent.done()."
                if final_status == "completed"
                else "The agent emitted agent.fail()."
            )
            break

        if "next" in code[0].lower():
            continue

        if "wait" in code[0].lower():
            print("⏳ Agent requested wait...")
            time.sleep(5)
            continue

        else:
            time.sleep(1.0)
            print("EXECUTING CODE:", code[0])

            # Check for pause state before execution
            while paused:
                time.sleep(0.1)

            # Ask for permission before executing
            try:
                exec(code[0])
            except Exception as exc:
                final_status = "failed"
                final_reason = f"Action execution failed with: {exc}"
                logger.exception("AGENT_S_ACTION_EXECUTION_FAILED")
                break
            time.sleep(1.0)

            # Update task and subtask trajectories
            if "reflection" in info and "executor_plan" in info:
                traj += (
                    "\n\nReflection:\n"
                    + str(info["reflection"])
                    + "\n\n----------------------\n\nPlan:\n"
                    + info["executor_plan"]
                )

    duration_seconds = time.time() - started_at

    feedback = build_task_feedback(
        status=final_status,
        reason=final_reason,
        steps_completed=steps_completed,
        max_steps=max_steps,
        last_info=last_info,
        last_code=last_code,
        duration_seconds=duration_seconds,
    )
    dialog_title = (
        "AA-CUA 任务完成" if final_status == "completed" else "AA-CUA 任务未完成"
    )
    show_status_dialog(dialog_title, feedback)
    return {
        "status": final_status,
        "reason": final_reason,
        "steps_completed": steps_completed,
        "max_steps": max_steps,
        "duration_seconds": duration_seconds,
        "summary": feedback,
    }


def main():
    parser = argparse.ArgumentParser(description="Run AgentS3 with specified model.")
    parser.add_argument(
        "--provider",
        type=str,
        default="openai",
        help="Specify the provider to use (e.g., openai, anthropic, kimi, etc.)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gpt-5-2025-08-07",
        help="Specify the model to use (e.g., gpt-5-2025-08-07)",
    )
    parser.add_argument(
        "--model_url",
        type=str,
        default="",
        help="The URL of the main generation model API.",
    )
    parser.add_argument(
        "--model_api_key",
        type=str,
        default="",
        help="The API key of the main generation model.",
    )
    parser.add_argument(
        "--model_temperature",
        type=float,
        default=None,
        help="Temperature to fix the generation model at (e.g. o3 can only be run with 1.0)",
    )

    # Grounding model config: Self-hosted endpoint based (required)
    parser.add_argument(
        "--ground_provider",
        type=str,
        required=True,
        help="The provider for the grounding model",
    )
    parser.add_argument(
        "--ground_url",
        type=str,
        default="",
        help="The URL of the grounding model",
    )
    parser.add_argument(
        "--ground_api_key",
        type=str,
        default="",
        help="The API key of the grounding model.",
    )
    parser.add_argument(
        "--ground_model",
        type=str,
        default="",
        help="The model name for the grounding model",
    )
    parser.add_argument(
        "--grounding_width",
        type=int,
        required=True,
        help="Width of screenshot image after processor rescaling",
    )
    parser.add_argument(
        "--grounding_height",
        type=int,
        required=True,
        help="Height of screenshot image after processor rescaling",
    )

    # AgentS3 specific arguments
    parser.add_argument(
        "--max_trajectory_length",
        type=int,
        default=8,
        help="Maximum number of image turns to keep in trajectory",
    )
    parser.add_argument(
        "--enable_reflection",
        action="store_true",
        default=None,
        help="Enable reflection agent to assist the worker agent",
    )
    parser.add_argument(
        "--disable_reflection",
        action="store_false",
        dest="enable_reflection",
        help="Disable reflection agent for faster execution",
    )
    parser.add_argument(
        "--enable_local_env",
        action="store_true",
        default=False,
        help="Enable local coding environment for code execution (WARNING: Executes arbitrary code locally)",
    )
    parser.add_argument(
        "--task",
        type=str,
        help="The task instruction for Agent-S3 to perform.",
    )

    args = _apply_kimi_defaults(parser.parse_args())

    if args.enable_reflection is None:
        args.enable_reflection = os.getenv("AGENT_S_ENABLE_REFLECTION", "0").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

    if not args.model:
        parser.error("--model is required")
    if not args.ground_model:
        parser.error("--ground_model is required")
    if args.ground_provider != "kimi" and not args.ground_url:
        parser.error("--ground_url is required unless --ground_provider kimi can resolve it from env")

    # Re-scales screenshot size to ensure it fits in UI-TARS context limit
    screen_width, screen_height = pyautogui.size()
    scaled_width, scaled_height = scale_screen_dimensions(
        screen_width, screen_height, max_dim_size=2400
    )

    # Load the general engine params
    engine_params = {
        "engine_type": args.provider,
        "model": args.model,
        "base_url": args.model_url,
        "api_key": args.model_api_key,
        "temperature": getattr(args, "model_temperature", None),
    }

    # Load the grounding engine from a custom endpoint
    engine_params_for_grounding = {
        "engine_type": args.ground_provider,
        "model": args.ground_model,
        "base_url": args.ground_url,
        "api_key": args.ground_api_key,
        "grounding_width": args.grounding_width,
        "grounding_height": args.grounding_height,
    }

    # Initialize environment based on user preference
    local_env = None
    if args.enable_local_env:
        print(
            "⚠️  WARNING: Local coding environment enabled. This will execute arbitrary code locally!"
        )
        local_env = LocalEnv()

    grounding_agent = OSWorldACI(
        env=local_env,
        platform=current_platform,
        engine_params_for_generation=engine_params,
        engine_params_for_grounding=engine_params_for_grounding,
        width=screen_width,
        height=screen_height,
    )

    agent = AgentS3(
        engine_params,
        grounding_agent,
        platform=current_platform,
        max_trajectory_length=args.max_trajectory_length,
        enable_reflection=args.enable_reflection,
    )

    task = args.task

    # handle query from command line
    if isinstance(task, str) and task.strip():
        agent.reset()
        result = run_agent(agent, task, scaled_width, scaled_height)
        if result.get("status") != "completed":
            sys.exit(1)
        return

    while True:
        query = input("Query: ")

        agent.reset()

        # Run the agent on your own device
        result = run_agent(agent, query, scaled_width, scaled_height)

        response = input("Would you like to provide another query? (y/n): ")
        if response.lower() != "y":
            break


if __name__ == "__main__":
    main()
