import asyncio
import re
import sys
import time
import threading
from typing import Any, Dict, Optional
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from llm_agent.coala.pm import ProceduralMemory
from llm_agent.coala.body import Body
from llm_agent.coala.sensor import Sensor

import json
from flask import Flask, jsonify, Response


class _ChatMemory:
    def __init__(self, k: int):
        self.k = k
        self.messages: list[BaseMessage] = []

    def _trim(self) -> None:
        if self.k is None:
            return
        ai_messages = [m for m in self.messages if isinstance(m, AIMessage)]
        if len(ai_messages) <= self.k:
            return
        keep_ai_ids = {id(m) for m in ai_messages[-self.k :]}
        self.messages = [
            m for m in self.messages if not isinstance(m, AIMessage) or id(m) in keep_ai_ids
        ]

    def add_ai_message(self, content: str) -> None:
        self.add_message(AIMessage(content=content))

    def add_user_message(self, content: str) -> None:
        self.add_message(HumanMessage(content=content))

    def add_system_message(self, content: str) -> None:
        self.add_message(SystemMessage(content=content))

    def add_message(self, message: BaseMessage) -> None:
        self.messages.append(message)
        self._trim()


class _WindowedChatMemory:
    def __init__(self, memory_key: str = "chat_history", k: int = 10):
        self.memory_key = memory_key
        self.chat_memory = _ChatMemory(k)

    def load_memory_variables(self, inputs):
        return {self.memory_key: list(self.chat_memory.messages)}


class Coala:
    def __init__(
        self,
        llm,
        tools=None,
        initial_prompt=None,
        initial_memory=None,
        body=None,
        mcp_servers=None,
        agent_name: str = "coala_agent",
        sync_timeout_seconds: int = 20,
        tool_timeout_seconds: int = 60,
        enable_gui: bool = False,
        gui_host: str = "127.0.0.1",
        gui_port: int = 8001,
    ):
        if tools is None:
            tools = []
        if initial_prompt is None:
            initial_prompt = "You are an intelligent agent that can use tools to accomplish tasks."
        if initial_memory is None:
            initial_memory = {}
        self.llm = llm
        print("Initial prompt: ", initial_prompt)
        self.initial_prompt = initial_prompt
        self.working_memory = _WindowedChatMemory(memory_key="chat_history", k=10)
        self.procedural_memory = ProceduralMemory(llm)
        for t in tools:
            self.procedural_memory.add_tool(t)
        if mcp_servers:
            for server in mcp_servers:
                if isinstance(server, dict):
                    self.procedural_memory.register_mcp_server(**server)
                elif isinstance(server, tuple) and len(server) >= 2:
                    # Support simple tuple usage: (name, server_url)
                    name, server_url = server[0], server[1]
                    self.procedural_memory.register_mcp_server(name=name, server_url=server_url)
                else:
                    raise ValueError(
                        "MCP server entries must be dicts with registration args or (name, server_url) tuples."
                    )
        self.sensor = Sensor()
        if body is not None:
            self.sensor = Body()
        # Add initial prompt to working memory
        self.working_memory.chat_memory.add_system_message(self.initial_prompt)
        self.data = initial_memory
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.cycle_input_tokens = 0
        self.cycle_output_tokens = 0
        normalized_agent_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(agent_name or "").strip())
        self.agent_name = normalized_agent_name or "coala_agent"
        self.sync_timeout_seconds = sync_timeout_seconds
        self.tool_timeout_seconds = tool_timeout_seconds
        self.stop = False
        self.start_time = time.time()
        self.enable_gui = enable_gui
        self.gui_host = gui_host
        self.gui_port = gui_port
        self._gui_thread: Optional[threading.Thread] = None
        self._gui_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._last_run_lock = threading.Lock()
        self._last_run_path = _ROOT.parent / f"last_run_{self.agent_name}.txt"
        self._log_dir = _ROOT / "coala" / "gui_logs"
        self._log_files = {
            "states": self._log_dir / "states.txt",
            "percepts": self._log_dir / "percepts.txt",
            "decisions": self._log_dir / "decisions.txt",
            "memory_history": self._log_dir / "memory_history.txt",
            "current_memory": self._log_dir / "current_memory.txt",
            "goal_prompt": self._log_dir / "goal_prompt.txt",
            "working_memory": self._log_dir / "working_memory.txt",
        }
        self._gui_state: Dict[str, Any] = {
            "states": [],
            "percepts": [],
            "decisions": [],
            "current_memory": None,
            "memory_history": [],
            "goal_prompt": self.initial_prompt,
            "working_memory": [],
            "panel_meta": {
                "states": {"count": 0, "updated_at": None},
                "percepts": {"count": 0, "updated_at": None},
                "decisions": {"count": 0, "updated_at": None},
                "memory_history": {"count": 0, "updated_at": None},
                "working_memory": {"count": 0, "updated_at": None},
                "goal_prompt": {
                    "count": 0,
                    "updated_at": time.time() if self.initial_prompt else None,
                },
            },
        }
        self._init_log_files()
        if self.initial_prompt:
            self._append_log("goal_prompt", {"goal_prompt": self.initial_prompt})
        self._last_state = None
        self._last_cycle_action: dict[str, Any] | None = None
        self._capture_state_from_memory(initial=True)

    def retrieve_observations(self):
        return self.sensor.gather()

    def process_observations(self, observations):
        self.working_memory.chat_memory.add_user_message(observations)
        if observations:
            self._record_percept(observations, source="observation")

    def retrieve_episodic_memory(self, query):  # TODO: update
        # results = self.episodic_memory.similarity_search(query, k=3)
        results = []
        for res in results:
            self.working_memory.chat_memory.add_message(AIMessage(content=res.page_content))

    def retrieve_procedural_memory(self, query):
        tools = self.procedural_memory.retrieve_tools(query)
        for tool in tools:
            print("tool type: ", type(tool))
            tool_description = self._format_tool_description(tool)
            self.working_memory.chat_memory.add_message(
                AIMessage(content=f"Tool available: {tool.name} - {tool_description}")
            )

    def extract_reply(self, text: str) -> str:
        """
        Extracts the reply from a string formatted like:
        '<think>Reasoning</think> Reply'

        If '</think>' is not found, the full input string is returned.

        Parameters:
        - text (str): The input string.

        Returns:
        - str: The extracted reply or the original string if no </think> tag is found.
        """
        if "</think>" not in text:
            return text.strip()

        match = re.search(r"</think>\s*(.*)", text, re.DOTALL)
        return match.group(1).strip() if match else text.strip()

    def _accumulate_usage(self, ai_message: AIMessage) -> None:
        usage = ai_message.usage_metadata
        if not isinstance(usage, dict):
            return
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if isinstance(input_tokens, int):
            self.total_input_tokens += input_tokens
            self.cycle_input_tokens += input_tokens
        if isinstance(output_tokens, int):
            self.total_output_tokens += output_tokens
            self.cycle_output_tokens += output_tokens

    def decide(self, thought):
        print("Deciding phase")
        # Retrieve the working memory content (e.g., chat history)
        memory_context = self.working_memory.load_memory_variables({})
        chat_history = memory_context.get("chat_history", "")

        # Get available tools from procedural memory
        available_tools = self.procedural_memory.retrieve_tools()
        tools_description = "\n".join(
            [f"- {self._format_tool_description(tool)}" for tool in available_tools]
        )

        # Create a decision prompt that includes the initial prompt, working memory, and tools
        decision_prompt = (
            f"Initial Goal and Context:\n{self.initial_prompt}\n\n"
            f"Available Tools:\n{tools_description}\n\n"
            f"Conversation and Observation Context:\n{chat_history}\n\n"
            f"Last thought:\n{thought}\n"
            "Based on the above context, especially relying on the last thought, and available tools, what should I "
            "do next?\n"
            'If no clear action is needed, you can respond with a "noop" (no operation).\n'
            'For tool use, respond with a JSON object containing "tool" and "tool_input" fields.\n'
            'For noop, respond with: {"tool": "noop"}\n'
            'To stop the agent respond with: {"tool": "stop"}\n'
            'For updating the permanent memory, respond with {"tool": "permanent_memory", "field": "field_name", "value":"field_value"}\n'
            'To add a memory to the RAG episodic memory, response with {"tool": "episodic_memory", "memory": "memory_content"}, where "memory_content" is the memory you want tp store\n'
            'To stop the agent respond with: {"tool": "stop"}\n'
            'For normal tool use, respond with: {"tool": tool_name, "tool_input": tool_input} where tool_name is the name of the tool and tool_input is a JSON object with the names of parameters associated with their values. If the tool has no parameter, the tool_input is {}\n'
            "Important: Your response should be valid JSON and should be directly parsable into JSON\n"
        )

        # Use the LLM to decide based on the enriched prompt
        decision = self.llm.invoke(decision_prompt)
        decision_str = ""
        if isinstance(decision, str):
            decision_str = decision
        elif isinstance(decision, AIMessage):
            print("is decision AI message")
            ai_message: AIMessage = decision
            self._accumulate_usage(ai_message)
            decision_str = decision.text()
        print(f"Decision made: {decision_str}")
        self._record_decision(decision_str)
        return self.extract_reply(decision_str)

    def think(self):
        print("Thinking phase")
        # Retrieve the working memory content (e.g., chat history)
        memory_context = self.working_memory.load_memory_variables({})
        chat_history = memory_context.get("chat_history", "")

        # Get available tools from procedural memory
        available_tools = self.procedural_memory.retrieve_tools()
        tools_description = "\n".join(
            [f"- {self._format_tool_description(tool)}" for tool in available_tools]
        )
        print("Tool descriptions: ", tools_description)
        # Create a decision prompt that includes the initial prompt, working memory, and tools
        think_prompt = (
            f"Initial Goal and Context:\n{self.initial_prompt}\n\n"
            f"Available Tools:\n{tools_description}\n\n"
            f"Permanent Memory:\n{self.data}"
            f"Conversation and Observation Context:\n{chat_history}\n\n"
            "Based on the above context and available tools, what should I do next?\n"
            "You can either choose to use a tool, do a noop operation for no operation, or updating a field of the "
            "permanent memory with a given value\n"
            "Please rely on Chain of Thoughts to make your choice. \n"
        )

        # Use the LLM to decide based on the enriched prompt
        thought = self.llm.invoke(think_prompt)
        thought_str = ""
        if isinstance(thought, str):
            thought_str = thought
        elif isinstance(thought, AIMessage):
            print("is think AI message")
            ai_message: AIMessage = thought
            self._accumulate_usage(ai_message)
            thought_str = thought.text()

        self.working_memory.chat_memory.add_ai_message(thought_str)
        print(f"Thought made: {thought}")
        return thought

    async def execute_decision(self, d):
        try:
            parsed = self._parse_decision_payload(d)
            decisions = parsed if isinstance(parsed, list) else [parsed]
            for decision in decisions:
                if not isinstance(decision, dict):
                    raise ValueError("Each decision must be a JSON object.")
                await self._execute_single_decision(decision)
        except Exception as e:
            print(f"No valid JSON for {d} with type: {type(d)}")
            self._record_action("invalid_json", {"raw": d})
            self._record_action_output(error=str(e))
            self._record_decision({"error": "invalid_json", "raw": d, "exception": str(e)})

    def _parse_decision_payload(self, decision_payload: Any) -> Any:
        if isinstance(decision_payload, (dict, list)):
            return decision_payload
        if not isinstance(decision_payload, str):
            raise TypeError("Decision payload must be a JSON string, dict, or list.")

        raw = decision_payload.strip()
        fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", raw, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            raw = fenced.group(1).strip()

        return json.loads(raw)

    async def _execute_single_decision(self, decision: Dict[str, Any]) -> None:
        tool_name = decision["tool"]  # crude parsing to get a tool name
        action_input = decision.get("tool_input")
        if action_input is None:
            action_input = {k: v for k, v in decision.items() if k != "tool"}
        self._record_action(tool_name, action_input)
        print("tool name: ", tool_name)
        self._record_decision(decision)
        if tool_name == "noop":
            print("No operation needed at this time.")
            self.working_memory.chat_memory.add_ai_message("Decided to take no action at this time.")
            self._record_action_output("Decided to take no action at this time.")
            return
        if tool_name == "permanent_memory":
            print(
                "Update permanent memory field: ",
                decision["field"],
                " with value: ",
                decision["value"],
            )
            self.data[decision["field"]] = decision["value"]
            self.working_memory.chat_memory.add_ai_message(
                f"Updated permanent memory field '{decision['field']}' to '{decision['value']}'"
            )
            self._record_action_output(f"Updated permanent memory field '{decision['field']}'")
            return
        if tool_name == "episodic_memory":
            print(
                "Update permanent memory field: ",
                decision["field"],
                " with value: ",
                decision["value"],
            )
            self.data[decision["field"]] = decision["value"]
            self.working_memory.chat_memory.add_ai_message(
                f"Updated permanent memory field '{decision['field']}' to '{decision['value']}'"
            )
            self._record_action_output(f"Updated permanent memory field '{decision['field']}'")
            return
        if tool_name == "stop":
            self.stop = True
            print("Total input tokens: ", self.total_input_tokens)
            print("Total output tokens: ", self.total_output_tokens)
            stop_time = time.time()
            total_time = stop_time - self.start_time
            print("total time: ", total_time)
            self._record_action_output("Agent stopped.")
            return
        if tool_name == "remember":
            # self.episodic_memory.add_texts([decision["memory"]])
            self._record_action_output("Remember action acknowledged.")
            return
        print("before looking for tools")
        tool = self.procedural_memory.get_tool(tool_name)
        print("tool found")
        if tool:
            self._record_action_tool_description(tool)
            try:
                result = None
                tool_input = decision.get("tool_input", {})
                if isinstance(tool_input, dict):
                    tool_input = self._normalize_tool_input(tool_name, tool, tool_input)
                    if not tool_input:
                        print("Invoke tool without params")
                        result = await asyncio.wait_for(
                            tool.ainvoke({}),
                            timeout=self.tool_timeout_seconds,
                        )
                    else:
                        result = await asyncio.wait_for(
                            tool.ainvoke(tool_input),
                            timeout=self.tool_timeout_seconds,
                        )
                    print(f"Executed {tool_name}, result: {result}")
                    self._record_action_output(result)
                else:
                    print("tool input could not be used.")
                    self._record_action_output("Tool input could not be used.")
                percept = (
                    "Tool used: "
                    + tool_name
                    + " Tool input: "
                    + str(tool_input)
                    + ". Tool result: "
                    + str(result)
                )
                print("new percept: " + percept)
                self.sensor.add_percept(percept)
                self._record_percept(percept, source="tool")
                self.working_memory.chat_memory.add_ai_message(percept)
            except asyncio.TimeoutError:
                message = f"Tool '{tool_name}' timed out after {self.tool_timeout_seconds}s"
                print(message)
                self._record_action_output(error=message)
                self.working_memory.chat_memory.add_ai_message(message)
            except Exception as e:
                print(f"Tool execution failed: {e}")
                self._record_action_output(error=str(e))
                self.working_memory.chat_memory.add_ai_message(f"Tool execution failed: {str(e)}")
        else:
            print(f"No tool named '{tool_name}' found. Executing default fallback.")
            self._record_action_output(error=f"Could not find tool '{tool_name}'.")
            self.working_memory.chat_memory.add_ai_message(f"Could not find tool '{tool_name}'.")

    def clean_memory(self):
        history = self.working_memory.chat_memory.messages
        if len(history) > 20:
            self.working_memory.chat_memory.messages = history[-10:]

    def register_mcp_server(
        self,
        name: str,
        *,
        server_url: Optional[str] = None,
        command: Optional[str] = None,
        args=None,
        env=None,
    ):
        """Expose MCP servers as tool providers."""
        if server_url is None and command is None:
            # Allow re-registering by name if it already exists.
            if name in self.procedural_memory.mcp_servers:
                return
        self.procedural_memory.register_mcp_server(
            name=name,
            server_url=server_url,
            command=command,
            args=args,
            env=env,
        )

    async def run_cycle(self):
        start_time = time.time()
        self._last_cycle_action = None
        self.cycle_input_tokens = 0
        self.cycle_output_tokens = 0
        try:
            await asyncio.wait_for(
                self.procedural_memory.sync_mcp_tools(),
                timeout=self.sync_timeout_seconds,
            )
        except TimeoutError:
            print(
                f"MCP tool sync timed out after {self.sync_timeout_seconds}s; using current tool set."
            )
        except Exception as exc:
            print(f"MCP tool sync failed; using current tool set. Error: {exc}")
        observations = self.retrieve_observations()
        self.process_observations(observations)
        self.retrieve_episodic_memory(query=observations)
        self.retrieve_procedural_memory(query=observations)
        thought = self.think()
        decision = self.decide(thought)
        await self.execute_decision(decision)
        self._capture_state_from_memory()
        self.clean_memory()
        self._finalize_cycle_gui_state()
        self._update_last_run_file_from_cycle()
        print("cycle input tokens: ", self.cycle_input_tokens)
        print("cycle output tokens: ", self.cycle_output_tokens)
        end_time = time.time()
        cycle_time = end_time - start_time
        print("cycle time: ", cycle_time)

    async def start(self):
        self._ensure_gui()
        self._initialize_last_run_file()
        while not self.stop:
            await self.run_cycle()

    def _format_tool_description(self, tool) -> str:
        base_desc = ""
        if hasattr(tool, "describe") and callable(getattr(tool, "describe")):
            base_desc = tool.describe() or ""
        else:
            base_desc = getattr(tool, "description", "") or ""

        schema = self._get_tool_input_schema(tool)
        if not isinstance(schema, dict):
            return f"{tool.name}: {base_desc}".strip(": ")

        params = self._extract_param_descriptions(schema)
        if not params:
            return f"{tool.name}: {base_desc}".strip(": ")

        param_lines = []
        for name, desc, required in params:
            required_tag = " (required)" if required else ""
            detail = f"{name}{required_tag}"
            if desc:
                detail = f"{detail} - {desc}"
            param_lines.append(detail)
        param_text = "; ".join(param_lines)
        if base_desc:
            return f"{tool.name}: {base_desc} | params: {param_text}"
        return f"{tool.name}: params: {param_text}"

    def _get_tool_input_schema(self, tool):
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
        if schema is None:
            schema = getattr(tool, "_input_schema", None)
        if schema is not None:
            return schema
        server = getattr(tool, "server", None)
        if server is not None:
            tool_def = getattr(server, "tool_definitions", {}).get(getattr(tool, "name", ""))
            if tool_def is not None:
                return getattr(tool_def, "input_schema", None) or getattr(
                    tool_def, "inputSchema", None
                )
        return None

    def _extract_param_descriptions(self, schema: Dict[str, Any]) -> list[tuple[str, str, bool]]:
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = set(schema.get("required", [])) if isinstance(schema, dict) else set()
        params = []
        if isinstance(properties, dict):
            for name, prop_schema in properties.items():
                if not isinstance(prop_schema, dict):
                    params.append((name, "", name in required))
                    continue
                desc = prop_schema.get("description", "") or prop_schema.get("title", "") or ""
                params.append((name, desc, name in required))
        return params

    def _capture_state_from_memory(self, initial: bool = False):
        current_state = None
        current_memory = None
        if isinstance(self.data, dict):
            current_state = self.data.get("current_state")
            current_memory = dict(self.data)
        if current_state is not None and current_state != self._last_state:
            self._record_state(current_state)
            self._last_state = current_state
        with self._gui_lock:
            self._gui_state["current_memory"] = current_memory
            self._gui_state["goal_prompt"] = self.initial_prompt
            self._gui_state["working_memory"] = list(self.working_memory.chat_memory.messages)
            if current_memory is not None:
                self._gui_state["memory_history"].append(
                    {"timestamp": time.time(), "memory": current_memory}
                )
        if current_memory is not None:
            self._append_log("current_memory", {"memory": current_memory})
            self._append_log("memory_history", {"memory": current_memory})
        self._append_log(
            "working_memory", {"messages": list(self.working_memory.chat_memory.messages)}
        )

    def _record_state(self, state: str):
        with self._gui_lock:
            self._gui_state["states"].append({"timestamp": time.time(), "state": state})
        self._append_log("states", {"state": state})

    def _record_percept(self, percept: str, source: str):
        with self._gui_lock:
            self._gui_state["percepts"].append(
                {"timestamp": time.time(), "source": source, "percept": percept}
            )
        self._append_log("percepts", {"source": source, "percept": percept})

    def _record_decision(self, decision: Any):
        with self._gui_lock:
            self._gui_state["decisions"].append({"timestamp": time.time(), "decision": decision})
        self._append_log("decisions", {"decision": decision})

    def _record_action(self, action: str, tool_input: Any = None) -> None:
        self._last_cycle_action = {
            "timestamp": time.time(),
            "action": action,
            "tool_input": self._to_jsonable(tool_input),
        }

    def _record_action_tool_description(self, tool: Any) -> None:
        if self._last_cycle_action is None or tool is None:
            return
        description = self._format_tool_description(tool).strip()
        if description:
            self._last_cycle_action["tool_description"] = description

    def _record_action_output(self, result: Any = None, error: str | None = None) -> None:
        if self._last_cycle_action is None:
            return
        if error is not None:
            self._last_cycle_action["error"] = str(error)
            self._last_cycle_action.pop("output", None)
            return
        self._last_cycle_action["output"] = self._to_jsonable(result)
        self._last_cycle_action.pop("error", None)

    def _initialize_last_run_file(self) -> None:
        with self._last_run_lock:
            self._last_run_path.write_text("", encoding="utf-8")

    def _update_last_run_file_from_cycle(self) -> None:
        if self._last_cycle_action is None:
            return

        action_name = self._last_cycle_action.get("action", "unknown")
        tool_input = self._last_cycle_action.get("tool_input")
        output = self._last_cycle_action.get("output")
        error = self._last_cycle_action.get("error")
        tool_description = self._last_cycle_action.get("tool_description")

        parts = [f"action: {action_name}"]
        if tool_input not in (None, {}, ""):
            parts.append(f"input: {json.dumps(tool_input, ensure_ascii=True)}")
        if error is not None:
            parts.append(f"output: {error}")
        elif output not in (None, ""):
            parts.append(f"output: {json.dumps(output, ensure_ascii=True)}")
        if tool_description not in (None, ""):
            parts.append(f"tool_description: {json.dumps(tool_description, ensure_ascii=True)}")

        line = " | ".join(parts) + "\n"
        with self._last_run_lock:
            with self._last_run_path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def _finalize_cycle_gui_state(self) -> None:
        now = time.time()
        with self._gui_lock:
            meta = self._gui_state.setdefault("panel_meta", {})
            meta["states"] = {"count": len(self._gui_state.get("states", [])), "updated_at": now}
            meta["percepts"] = {
                "count": len(self._gui_state.get("percepts", [])),
                "updated_at": now,
            }
            meta["decisions"] = {
                "count": len(self._gui_state.get("decisions", [])),
                "updated_at": now,
            }
            meta["memory_history"] = {
                "count": len(self._gui_state.get("memory_history", [])),
                "updated_at": now,
            }
            meta["working_memory"] = {
                "count": len(self._gui_state.get("working_memory", [])),
                "updated_at": now,
            }

    def _init_log_files(self) -> None:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        for path in self._log_files.values():
            path.write_text("", encoding="utf-8")

    def _append_log(self, key: str, payload: Any) -> None:
        path = self._log_files.get(key)
        if path is None:
            return
        entry = {"ts": time.time(), "data": payload}
        line = json.dumps(self._to_jsonable(entry), ensure_ascii=True) + "\n"
        with self._log_lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def _to_jsonable(self, value: Any):
        if isinstance(value, dict):
            return {k: self._to_jsonable(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._to_jsonable(v) for v in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    def _normalize_tool_input(
        self, tool_name: str, tool: Any, tool_input: Dict[str, Any]
    ) -> Dict[str, Any]:
        if tool_name == "update_profile":
            if "nl_context" not in tool_input and "context" in tool_input:
                tool_input = dict(tool_input)
                tool_input["nl_context"] = tool_input.pop("context")
        required_fields = getattr(tool, "required_fields", []) or []
        if len(required_fields) == 1:
            required = required_fields[0]
            if required not in tool_input and "context" in tool_input:
                tool_input = dict(tool_input)
                tool_input[required] = tool_input.pop("context")
        return tool_input

    def _ensure_gui(self):
        if not self.enable_gui or self._gui_thread is not None:
            return
        app = Flask(__name__)

        @app.get("/")
        def index() -> Response:
            html = """
            <!doctype html>
            <html lang="en">
              <head>
                <meta charset="utf-8" />
                <meta name="viewport" content="width=device-width, initial-scale=1" />
                <title>Coala Agent Monitor</title>
                <style>
                  :root { color-scheme: light; }
                  body { font-family: "Georgia", "Times New Roman", serif; background: linear-gradient(120deg, #f5f1e8, #efe6d4); color: #1f1a12; margin: 0; }
                  header { padding: 24px 32px; border-bottom: 1px solid #d8cbb3; background: rgba(255,255,255,0.6); backdrop-filter: blur(8px); }
                  h1 { margin: 0 0 8px 0; font-size: 28px; letter-spacing: 0.5px; }
                  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; padding: 24px 32px 40px; }
                  .panel { background: rgba(255,255,255,0.75); border: 1px solid #d8cbb3; border-radius: 14px; padding: 16px; box-shadow: 0 10px 24px rgba(0,0,0,0.08); }
                  .panel-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin: 0 0 10px; }
                  .panel h2 { margin: 0; font-size: 18px; text-transform: uppercase; letter-spacing: 1px; }
                  .panel-state { font-size: 12px; opacity: 0.7; }
                  .copy-btn { font-size: 12px; padding: 4px 8px; border-radius: 999px; border: 1px solid #c9b99c; background: #fff8ee; cursor: pointer; }
                  .copy-btn:hover { background: #f3e7d5; }
                  .list { max-height: 360px; overflow: auto; padding-right: 8px; }
                  .item { padding: 10px; border-bottom: 1px dashed #d8cbb3; font-size: 14px; }
                  .item:last-child { border-bottom: none; }
                  .meta { font-size: 12px; opacity: 0.7; margin-bottom: 4px; }
                  .current { font-size: 20px; font-weight: 700; }
                  pre { white-space: pre-wrap; }
                  #toast { position: fixed; right: 20px; bottom: 20px; background: #1f1a12; color: #f5f1e8; padding: 10px 14px; border-radius: 10px; font-size: 13px; opacity: 0; transform: translateY(8px); transition: opacity 0.2s ease, transform 0.2s ease; }
                  #toast.show { opacity: 0.95; transform: translateY(0); }
                </style>
              </head>
              <body>
                <header>
                  <h1>Coala Agent Monitor</h1>
                  <div class="current" id="current-state">Current memory: --</div>
                </header>
                <section class="grid">
                  <div class="panel">
                    <div class="panel-header">
                      <h2>States</h2>
                      <div class="panel-state" id="states-meta">--</div>
                      <button class="copy-btn" data-key="states">Copy full history</button>
                    </div>
                    <div class="list" id="states"></div>
                  </div>
                  <div class="panel">
                    <div class="panel-header">
                      <h2>Percepts</h2>
                      <div class="panel-state" id="percepts-meta">--</div>
                      <button class="copy-btn" data-key="percepts">Copy full history</button>
                    </div>
                    <div class="list" id="percepts"></div>
                  </div>
                  <div class="panel">
                    <div class="panel-header">
                      <h2>Decisions</h2>
                      <div class="panel-state" id="decisions-meta">--</div>
                      <button class="copy-btn" data-key="decisions">Copy full history</button>
                    </div>
                    <div class="list" id="decisions"></div>
                  </div>
                  <div class="panel">
                    <div class="panel-header">
                      <h2>Memory Evolution</h2>
                      <div class="panel-state" id="memory-history-meta">--</div>
                      <button class="copy-btn" data-key="memory_history">Copy full history</button>
                    </div>
                    <div class="list" id="memory-history"></div>
                  </div>
                  <div class="panel">
                    <div class="panel-header">
                      <h2>Goal Prompt</h2>
                      <div class="panel-state" id="goal-prompt-meta">--</div>
                      <button class="copy-btn" data-key="goal_prompt">Copy goal prompt</button>
                    </div>
                    <div class="list" id="goal-prompt"></div>
                  </div>
                  <div class="panel">
                    <div class="panel-header">
                      <h2>Working Memory</h2>
                      <div class="panel-state" id="working-memory-meta">--</div>
                      <button class="copy-btn" data-key="working_memory">Copy working memory</button>
                    </div>
                    <div class="list" id="working-memory"></div>
                  </div>
                </section>
                <div id="toast">Copied</div>
                <script>
                  const fmt = (ts) => new Date(ts * 1000).toLocaleTimeString();
                  const copyText = async (text) => {
                    try {
                      await navigator.clipboard.writeText(text);
                    } catch (err) {
                      const area = document.createElement('textarea');
                      area.value = text;
                      document.body.appendChild(area);
                      area.select();
                      document.execCommand('copy');
                      document.body.removeChild(area);
                    }
                  };
                  const toastEl = document.getElementById('toast');
                  let toastTimer = null;
                  const showToast = (msg) => {
                    toastEl.textContent = msg;
                    toastEl.classList.add('show');
                    if (toastTimer) clearTimeout(toastTimer);
                    toastTimer = setTimeout(() => toastEl.classList.remove('show'), 1400);
                  };
                  let fullHistoryText = {};
                  document.querySelectorAll('.copy-btn').forEach((btn) => {
                    btn.addEventListener('click', () => {
                      const key = btn.getAttribute('data-key');
                      const text = fullHistoryText[key] || '';
                      copyText(text);
                      showToast('Copied to clipboard');
                    });
                  });
                  async function refresh() {
                    const res = await fetch('/api/state');
                    const data = await res.json();
                    const memoryText = data.current_memory ? JSON.stringify(data.current_memory) : '--';
                    document.getElementById('current-state').textContent = `Current memory: ${memoryText}`;
                    const meta = data.panel_meta || {};
                    const formatMeta = (entry) => {
                      if (!entry || !entry.updated_at) return '--';
                      return `${fmt(entry.updated_at)} · ${entry.count ?? 0} items`;
                    };
                    document.getElementById('states-meta').textContent = formatMeta(meta.states);
                    document.getElementById('percepts-meta').textContent = formatMeta(meta.percepts);
                    document.getElementById('decisions-meta').textContent = formatMeta(meta.decisions);
                    document.getElementById('memory-history-meta').textContent = formatMeta(meta.memory_history);
                    document.getElementById('goal-prompt-meta').textContent = formatMeta(meta.goal_prompt);
                    document.getElementById('working-memory-meta').textContent = formatMeta(meta.working_memory);
                    const statesData = Array.isArray(data.states) ? data.states : [];
                    const perceptsData = Array.isArray(data.percepts) ? data.percepts : [];
                    const decisionsData = Array.isArray(data.decisions) ? data.decisions : [];
                    const memoryHistoryData = Array.isArray(data.memory_history) ? data.memory_history : [];
                    const workingMemoryData = Array.isArray(data.working_memory) ? data.working_memory : [];
                    const states = statesData.slice().reverse().map(entry => `
                      <div class="item"><div class="meta">${fmt(entry.timestamp)}</div>${entry.state}</div>
                    `).join('');
                    document.getElementById('states').innerHTML = states || '<div class="item">No states yet.</div>';
                    const percepts = perceptsData.slice().reverse().map(entry => `
                      <div class="item"><div class="meta">${fmt(entry.timestamp)} · ${entry.source}</div>${entry.percept}</div>
                    `).join('');
                    document.getElementById('percepts').innerHTML = percepts || '<div class="item">No percepts yet.</div>';
                    const decisions = decisionsData.slice().reverse().map(entry => `
                      <div class="item"><div class="meta">${fmt(entry.timestamp)}</div><pre>${JSON.stringify(entry.decision, null, 2)}</pre></div>
                    `).join('');
                    document.getElementById('decisions').innerHTML = decisions || '<div class="item">No decisions yet.</div>';
                    const history = memoryHistoryData.slice().reverse().map(entry => `
                      <div class="item"><div class="meta">${fmt(entry.timestamp)}</div><pre>${JSON.stringify(entry.memory, null, 2)}</pre></div>
                    `).join('');
                    document.getElementById('memory-history').innerHTML = history || '<div class="item">No memory snapshots yet.</div>';
                    const goalPrompt = data.goal_prompt ? `<div class="item"><pre>${data.goal_prompt}</pre></div>` : '<div class="item">No goal prompt.</div>';
                    document.getElementById('goal-prompt').innerHTML = goalPrompt;
                    const workingMemory = workingMemoryData.map(entry => `
                      <div class="item"><div class="meta">${entry.type || 'message'}</div><pre>${entry.content || ''}</pre></div>
                    `).join('');
                    document.getElementById('working-memory').innerHTML = workingMemory || '<div class="item">No working memory yet.</div>';

                    fullHistoryText = {
                      states: statesData.map(entry => `${fmt(entry.timestamp)} - ${entry.state}`).join('\n'),
                      percepts: perceptsData.map(entry => `${fmt(entry.timestamp)} - ${entry.source} - ${entry.percept}`).join('\n'),
                      decisions: decisionsData.map(entry => `${fmt(entry.timestamp)}\n${JSON.stringify(entry.decision, null, 2)}`).join('\n\n'),
                      memory_history: memoryHistoryData.map(entry => `${fmt(entry.timestamp)}\n${JSON.stringify(entry.memory, null, 2)}`).join('\n\n'),
                      goal_prompt: data.goal_prompt || '',
                      working_memory: workingMemoryData.map(entry => `${entry.type || 'message'}\n${entry.content || ''}`).join('\n\n'),
                    };
                  }
                  refresh();
                  setInterval(refresh, 1500);
                </script>
              </body>
            </html>
            """
            return Response(html, mimetype="text/html")

        @app.get("/api/state")
        def api_state():
            with self._gui_lock:
                return jsonify(self._to_jsonable(self._gui_state))

        def _run():
            app.run(host=self.gui_host, port=self.gui_port, debug=False, use_reloader=False)

        self._gui_thread = threading.Thread(target=_run, daemon=True)
        self._gui_thread.start()
