from typing import Optional, Any
from pathlib import Path

from coala.coala import Coala
from llm import load_llm


from config_loader import load_config

_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_PATH = _ROOT / "config.json"


class WebAgent(Coala):
    """A specialized agent based on Coala architecture that focuses on web interactions and signifier handling."""

    def __init__(
        self,
        initial_prompt,
        llm: Optional[Any] = None,  # qwen3:30b #gpt-4.1-nano
        tools=None,
        mcp_servers=None,
        initial_memory: Optional[dict] = None,
        agent_name: str = "web_agent",
        sync_timeout_seconds: int = 20,
        tool_timeout_seconds: int = 60,
        enable_gui: bool = False,
        gui_host: str = "127.0.0.1",
        gui_port: int = 8001,
    ):

        if llm is None:
            cfg = load_config(str(_CONFIG_PATH))
            llm = load_llm(cfg["llm_agent"]["provider"], cfg["llm_agent"]["model"])

        # Initialize the base Coala agent with our specific configuration
        super().__init__(
            llm=llm,
            tools=tools,
            initial_prompt=initial_prompt,
            initial_memory=initial_memory or {},
            mcp_servers=mcp_servers,
            agent_name=agent_name,
            sync_timeout_seconds=sync_timeout_seconds,
            tool_timeout_seconds=tool_timeout_seconds,
            enable_gui=enable_gui,
            gui_host=gui_host,
            gui_port=gui_port,
        )
