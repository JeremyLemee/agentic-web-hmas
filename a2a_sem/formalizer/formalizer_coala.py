import asyncio
import json
import re
import sys
from math import pi
from pathlib import Path

import uvicorn

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2a.utils import new_agent_text_message

ROOT = Path(__file__).resolve().parents[2]
COALA_ROOT = ROOT / "llm_agent"
COALA_DIR = COALA_ROOT / "coala"
for path in (ROOT, COALA_ROOT, COALA_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from llm_agent.coala.coala import Coala
from llm_agent.coala.tools.coala_tool import CoalaTool

from llm import load_llm


_FORMAL_PATTERN = re.compile(r"(move|rotate)\((-?\d+(?:\.\d+)?)\)")
_NUMBER_TOKEN_PATTERN = re.compile(
    r"\b(zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b"
)
_DISTANCE_TO_METERS = {
    "mm": 0.001,
    "millimeter": 0.001,
    "millimeters": 0.001,
    "cm": 0.01,
    "centimeter": 0.01,
    "centimeters": 0.01,
    "m": 1.0,
    "meter": 1.0,
    "meters": 1.0,
    "km": 1000.0,
    "kilometer": 1000.0,
    "kilometers": 1000.0,
    "in": 0.0254,
    "inch": 0.0254,
    "inches": 0.0254,
    "ft": 0.3048,
    "foot": 0.3048,
    "feet": 0.3048,
    "yd": 0.9144,
    "yard": 0.9144,
    "yards": 0.9144,
    "mi": 1609.344,
    "mile": 1609.344,
    "miles": 1609.344,
}
_ANGLE_TO_DEGREES = {
    "deg": 1.0,
    "degree": 1.0,
    "degrees": 1.0,
    "rad": 180.0 / pi,
    "radian": 180.0 / pi,
    "radians": 180.0 / pi,
    "turn": 360.0,
    "turns": 360.0,
}
_NUMBER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

SYSTEM_PROMPT = (
    "You are a formalizer for robot goals.\n"
    "Convert the user input into exactly one command in this format:\n"
    "- move(d) where d is a distance in centimeters. If the movement is backward, d should be negative.\n"
    "- rotate(a) where a is an angle in degrees between 0 to 180, positive for counterclockwise, negative for clockwise\n"
    "Rules:\n"
    "1) Use tools to convert units (including non-metric distances and radians/degrees). If the user input refers to a distance not in cm, or an angle that is not in degrees, then you must use an appropriate tool.\n"
    "2) If the user mentions rotation or turning, use rotate(a).\n"
    "3) If the user mentions forward/backward movement, use move(d).\n"
    "4) Respond with only the command, no extra text.\n"
    '5) To return the final command, call the tool emit_formal_command with tool_input {"command": "move(10)"}.\n'
)


def _normalize_unit(unit: str) -> str:
    return (unit or "").strip().lower()


def _replace_number_words(text: str) -> str:
    return _NUMBER_TOKEN_PATTERN.sub(lambda m: str(_NUMBER_WORDS[m.group(1)]), text)


def _format_number(value: float) -> str:
    rounded = round(value, 6)
    if float(rounded).is_integer():
        return str(int(rounded))
    return str(rounded).rstrip("0").rstrip(".")


def _extract_value_and_unit(text: str):
    match = re.search(
        r"(-?\d+(?:\.\d+)?)\s*([a-zA-Z]+)?",
        text,
    )
    if not match:
        return None, None
    value = float(match.group(1))
    unit = _normalize_unit(match.group(2) or "")
    return value, unit


def _rule_based_formalize(user_text: str) -> str | None:
    raw = _replace_number_words((user_text or "").lower())
    if not raw:
        return None

    if any(token in raw for token in ("rotate", "turn", "clockwise", "counterclockwise")):
        value, unit = _extract_value_and_unit(raw)
        if value is None:
            return None
        unit = unit or "deg"
        if unit not in _ANGLE_TO_DEGREES:
            return None
        angle_deg = value * _ANGLE_TO_DEGREES[unit]
        if any(token in raw for token in ("clockwise", "right")):
            angle_deg = -abs(angle_deg)
        elif any(token in raw for token in ("counterclockwise", "left")):
            angle_deg = abs(angle_deg)
        return f"rotate({_format_number(angle_deg)})"

    if any(token in raw for token in ("move", "forward", "backward", "back")):
        value, unit = _extract_value_and_unit(raw)
        if value is None:
            return None
        unit = unit or "cm"
        if unit not in _DISTANCE_TO_METERS:
            return None
        cm = (value * _DISTANCE_TO_METERS[unit]) / _DISTANCE_TO_METERS["cm"]
        if any(token in raw for token in ("backward", "back")):
            cm = -abs(cm)
        return f"move({_format_number(cm)})"

    return None


class _ConvertDistanceTool(CoalaTool):
    def __init__(self):
        super().__init__("convert_distance")

    @property
    def description(self):
        return (
            "Convert distance between units (mm, cm, m, km, in, ft, yd, mi). "
            'Input: {"value": number, "from_unit": string, "to_unit": string}. Output: number.'
        )

    async def ainvoke(self, tool_input):
        value_raw = tool_input.get("value")
        from_raw = tool_input.get("from_unit")
        to_raw = tool_input.get("to_unit")
        if value_raw is None or from_raw is None or to_raw is None:
            raise ValueError("convert_distance requires value, from_unit, and to_unit")
        value = float(value_raw)
        from_unit = _normalize_unit(str(from_raw))
        to_unit = _normalize_unit(str(to_raw))
        if from_unit not in _DISTANCE_TO_METERS:
            raise ValueError(f"Unknown distance unit: {from_unit}")
        if to_unit not in _DISTANCE_TO_METERS:
            raise ValueError(f"Unknown distance unit: {to_unit}")
        meters = value * _DISTANCE_TO_METERS[from_unit]
        return meters / _DISTANCE_TO_METERS[to_unit]


class _ConvertAngleTool(CoalaTool):
    def __init__(self):
        super().__init__("convert_angle")

    @property
    def description(self):
        return (
            "Convert angle between units (deg, rad, turns). "
            'Input: {"value": number, "from_unit": string, "to_unit": string}. Output: number.'
        )

    async def ainvoke(self, tool_input):
        value_raw = tool_input.get("value")
        from_raw = tool_input.get("from_unit")
        to_raw = tool_input.get("to_unit")
        if value_raw is None or from_raw is None or to_raw is None:
            raise ValueError("convert_angle requires value, from_unit, and to_unit")
        value = float(value_raw)
        from_unit = _normalize_unit(str(from_raw))
        to_unit = _normalize_unit(str(to_raw))
        if from_unit not in _ANGLE_TO_DEGREES:
            raise ValueError(f"Unknown angle unit: {from_unit}")
        if to_unit not in _ANGLE_TO_DEGREES:
            raise ValueError(f"Unknown angle unit: {to_unit}")
        degrees_value = value * _ANGLE_TO_DEGREES[from_unit]
        return degrees_value / _ANGLE_TO_DEGREES[to_unit]


class _EmitFormalCommandTool(CoalaTool):
    def __init__(self):
        super().__init__("emit_formal_command")
        self.last_command = None

    @property
    def description(self):
        return 'Return the final formal command. Input: {"command": "move(10)"}.'

    async def ainvoke(self, tool_input):
        command = (
            tool_input.get("command", "") if isinstance(tool_input, dict) else str(tool_input or "")
        )
        command = str(command).strip()
        self.last_command = command
        return command


class _NoopEpisodicMemory:
    def similarity_search(self, _query, k=3):
        return []

    def add_texts(self, _texts):
        return None


def _load_config() -> dict:
    config_path = Path(__file__).resolve().parents[2] / "config.json"
    return json.loads(config_path.read_text())


_LLM = None


def _get_llm():
    global _LLM
    if _LLM is not None:
        return _LLM
    config = _load_config()
    _LLM = load_llm(config["formalizer_agent"]["provider"], config["formalizer_agent"]["model"])
    return _LLM


def _build_agent():
    emit_tool = _EmitFormalCommandTool()
    tools = [_ConvertDistanceTool(), _ConvertAngleTool(), emit_tool]
    agent = Coala(
        llm=_get_llm(),
        tools=tools,
        initial_prompt=SYSTEM_PROMPT,
        initial_memory={},
        agent_name="formalizer_coala_agent",
        sync_timeout_seconds=5,
        tool_timeout_seconds=180,
    )
    setattr(agent, "episodic_memory", _NoopEpisodicMemory())
    return agent, emit_tool


def _extract_last_message_text(agent) -> str:
    messages = getattr(agent.working_memory.chat_memory, "messages", [])
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if content:
            return str(content).strip()
    return ""


def _run_coroutine(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    new_loop = asyncio.new_event_loop()
    try:
        return new_loop.run_until_complete(coro)
    finally:
        new_loop.close()


def _formalize_goal_sync(user_text: str) -> str:
    rule_based = _rule_based_formalize(user_text)
    if rule_based:
        return rule_based

    agent, emit_tool = _build_agent()
    agent.sensor.add_percept(user_text)

    for _ in range(2):
        _run_coroutine(agent.run_cycle())

        if emit_tool.last_command:
            match = _FORMAL_PATTERN.search(emit_tool.last_command)
            if match:
                return match.group(0)

        last_text = _extract_last_message_text(agent)
        match = _FORMAL_PATTERN.search(last_text)
        if match:
            return match.group(0)

        if agent.stop:
            break

    return "move(0)"


class FormalizerAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_text = context.get_user_input() or ""
        formal = await asyncio.to_thread(_formalize_goal_sync, user_text)
        await event_queue.enqueue_event(new_agent_text_message(formal))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise Exception("cancel not supported")


if __name__ == "__main__":
    formalizer_skill = AgentSkill(
        id="formalize_goal",
        name="Formalize goal",
        description="Converts a natural language goal into move(d) or rotate(a).",
        tags=["formalizer", "goal"],
        examples=[
            "move forward 2 meters",
            "rotate 90 degrees",
            "turn left 45 degrees",
        ],
    )

    agent_card = AgentCard(
        name="Formalizer Agent",
        description="Formalizes goal descriptions into move/rotate commands.",
        url="http://localhost:9997/",
        version="1.0.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        skills=[formalizer_skill],
        supports_authenticated_extended_card=False,
    )

    request_handler = DefaultRequestHandler(
        agent_executor=FormalizerAgentExecutor(),
        task_store=InMemoryTaskStore(),
    )

    server = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )

    uvicorn.run(server.build(), host="0.0.0.0", port=9997)
