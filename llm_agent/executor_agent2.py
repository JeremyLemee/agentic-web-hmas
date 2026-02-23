import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, Optional, TypedDict

from langgraph.graph import END, StateGraph

# Ensure repo root is on sys.path when running this script directly.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_CONFIG_PATH = _ROOT / "config.json"

from config_loader import load_config
from llm import load_llm
from llm_agent.coala.pm import ProceduralMemory


PROFILE_ID = "executor"
PROFILE_URL = "http://localhost:5000/profile/executor"


class _ExecutorState(TypedDict, total=False):
    current_memory: str
    goal_tool: str
    goal_text: str


class ExecutorLangGraph:
    def __init__(
        self,
        *,
        llm=None,
        mcp_server_url: Optional[str] = None,
        initial_memory: Optional[Dict[str, Any]] = None,
    ):
        if llm is None:
            cfg = load_config(str(_CONFIG_PATH))
            llm = load_llm(cfg["llm_agent"]["provider"], cfg["llm_agent"]["model"])
        if mcp_server_url is None:
            cfg = load_config(str(_CONFIG_PATH))
            mcp_server_url = cfg["sem_mcp_endpoint"]

        self.llm = llm
        self.procedural_memory = ProceduralMemory(llm=llm)
        self.procedural_memory.register_mcp_server(name="mcp_sem", server_url=mcp_server_url)
        self.data: Dict[str, Any] = initial_memory or {"current_memory": "start"}

        self._graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(_ExecutorState)
        graph.add_node("sync_tools", self._node_sync_tools)
        graph.add_node("dispatch", self._node_dispatch)
        graph.add_node("do_start", self._node_start)
        graph.add_node("do_read_signifier1", self._node_read_signifier1)
        graph.add_node("do_end", self._node_end)

        graph.set_entry_point("sync_tools")
        graph.add_edge("sync_tools", "dispatch")
        graph.add_conditional_edges(
            "dispatch",
            self._route_state,
            {
                "start": "do_start",
                "read_signifier1": "do_read_signifier1",
                "end": END,
            },
        )
        graph.add_edge("do_start", "dispatch")
        graph.add_edge("do_read_signifier1", "dispatch")
        graph.add_edge("do_end", END)
        return graph.compile()

    async def _node_sync_tools(self, state: _ExecutorState) -> Dict[str, Any]:
        await self.procedural_memory.sync_mcp_tools()
        return {}

    def _node_dispatch(self, state: _ExecutorState) -> Dict[str, Any]:
        if self.data.get("current_memory") is None:
            self._set_state("start")
        return {"current_memory": self.data.get("current_memory")}

    async def _node_start(self, state: _ExecutorState) -> Dict[str, Any]:
        await self._call_tool(
            "update_profile",
            {"profile_id": PROFILE_ID, "nl_context": "The agent is looking for a goal to achieve."},
        )
        self._set_state("read_signifier1")
        return {}

    async def _node_read_signifier1(self, state: _ExecutorState) -> Dict[str, Any]:
        result = await self._call_tool(
            "read_signifiers",
            {"profile_url": PROFILE_URL},
        )
        tools_added = []
        if isinstance(result, dict):
            tools_added = result.get("tools_added") or []

        await self.procedural_memory.sync_mcp_tools()
        goal_tool = self._select_goal_tool(tools_added)
        if not goal_tool:
            raise RuntimeError("No goal signifier tool found after read_signifiers.")

        goal_result = await self._call_tool(goal_tool, {})
        goal_text = self._extract_goal_text(goal_result)
        self._permanent_memory("current goal", goal_text)
        self._set_state("end")
        return {"goal_tool": goal_tool, "goal_text": goal_text}

    def _node_end(self, state: _ExecutorState) -> Dict[str, Any]:
        return {}

    def _route_state(self, state: _ExecutorState) -> str:
        current = self.data.get("current_memory") or "start"
        if current not in ("start", "read_signifier1", "end"):
            return "end"
        return current

    async def _call_tool(self, tool_name: str, tool_input: Dict[str, Any]):
        tool = self.procedural_memory.get_tool(tool_name)
        if tool is None:
            await self.procedural_memory.sync_mcp_tools()
            tool = self.procedural_memory.get_tool(tool_name)
        if tool is None:
            raise RuntimeError(f"Tool '{tool_name}' not found.")
        tool_input = self._normalize_tool_input(tool_name, tool, tool_input)
        return await tool.ainvoke(tool_input)

    @staticmethod
    def _normalize_tool_input(
        tool_name: str, tool: Any, tool_input: Dict[str, Any]
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

    def _select_goal_tool(self, tools_added: list[str]) -> Optional[str]:
        if tools_added:
            for name in tools_added:
                if "goal" in name.lower():
                    return name
            return tools_added[0]

        for name, tool in self.procedural_memory.tools.items():
            desc = (getattr(tool, "description", "") or "").lower()
            if "goal" in name.lower() or "goal" in desc:
                return name
        return None

    @staticmethod
    def _extract_goal_text(result: Any) -> str:
        if isinstance(result, dict):
            content = result.get("content")
            if content is not None:
                return str(content)
        return str(result)

    def _permanent_memory(self, field: str, value: Any) -> None:
        self.data[field] = value

    def _set_state(self, new_state: str) -> None:
        # Use the internal permanent_memory tool semantics for state changes.
        self._permanent_memory("current_memory", new_state)

    async def run(self):
        await self._graph.ainvoke({})


def _main():
    cfg = load_config(str(_CONFIG_PATH))
    llm = load_llm(cfg["llm_agent"]["provider"], cfg["llm_agent"]["model"])
    agent = ExecutorLangGraph(
        llm=llm,
        mcp_server_url=cfg["sem_mcp_endpoint"],
        initial_memory={"current_memory": "start"},
    )
    asyncio.run(agent.run())


if __name__ == "__main__":
    _main()
