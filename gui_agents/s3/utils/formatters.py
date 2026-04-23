"""This file contains various formatting checks used to reprompt an agent for correctly formatted responses."""

import ast
import inspect

from gui_agents.s3.utils.common_utils import (
    extract_agent_functions,
    parse_code_from_string,
    split_thinking_response,
)

single_action_check = (
    lambda response: len(extract_agent_functions(parse_code_from_string(response))) == 1
)
single_action_error_msg = (
    "Incorrect code: There must be a single agent action in the code response."
)
SINGLE_ACTION_FORMATTER = lambda response: (
    single_action_check(response),
    single_action_error_msg,
)


def _validate_agent_action(agent, code):
    """Validate a proposed agent action without executing side effects."""
    if not code.strip():
        return False

    try:
        parsed = ast.parse(code.strip(), mode="eval")
        call = parsed.body
        if not isinstance(call, ast.Call):
            return False

        func = call.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "agent"
        ):
            return False

        method = getattr(agent, func.attr, None)
        if method is None:
            return False

        args = [ast.literal_eval(arg) for arg in call.args]
        kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords}
        inspect.signature(method).bind(*args, **kwargs)
        return True
    except Exception:
        return False


code_valid_check = (
    lambda agent, obs, response: _validate_agent_action(
        agent, parse_code_from_string(response)
    )
)
code_valid_error_msg = "Incorrect code: The agent action must be a valid function and use valid parameters from the docstring list."
CODE_VALID_FORMATTER = lambda agent, obs, response: (
    code_valid_check(agent, obs, response),
    code_valid_error_msg,
)

thoughts_answer_tag_check = lambda response: split_thinking_response(response)[1] != ""
thoughts_answer_tag_error_msg = "Incorrect response: The response must contain both <thoughts>...</thoughts> and <answer>...</answer> tags."
THOUGHTS_ANSWER_TAG_FORMATTER = lambda response: (
    thoughts_answer_tag_check(response),
    thoughts_answer_tag_error_msg,
)

integer_answer_check = (
    lambda response: split_thinking_response(response)[0].strip().isdigit()
)
integer_answer_error_msg = (
    "Incorrect response: The <answer>...</answer> tag must contain a single integer."
)
INTEGER_ANSWER_FORMATTER = lambda response: (
    integer_answer_check(response),
    integer_answer_error_msg,
)
