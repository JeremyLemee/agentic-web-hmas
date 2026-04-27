# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **Signifier Exposure Mechanism (SEM)** for the Agentic Web — a research prototype implementing semantic middleware that enables intelligent agents to discover and interact with heterogeneous web services. It integrates MCP, A2A, WoT, and UTCP protocols into a unified environment where agents find affordances (signifiers) matching their goals via LLM-based semantic matching.

## Commands

**Setup:**
```bash
uv sync                          # Install all dependencies (Python 3.12)
```

**Running the full stack:**
```bash
bash run.sh                      # Start all 7 microservices in order with proper delays
```

**Individual services:**
```bash
uv run app.py                    # SEM Flask app only (port 5000)
uv run mcp_sem/sem_mcp.py        # SEM MCP server (port 8200)
uv run llm_agent/executor_agent.py  # Executor agent
```

**Code quality:**
```bash
uv run ruff check .              # Lint (max line length 100, rules E/F, ignores E402/E501)
uv run pyright                   # Static type checking
```

**Tests:**
```bash
uv run pytest                                              # Full test suite
uv run pytest tests/test_artifacts_list.py -q             # Single test file
```

**Evaluation:**
```bash
uv run evaluation.py             # Multi-LLM signifier matching evaluation
uv run agent_evaluation.py       # Full agent evaluation
```

## Architecture

### Multi-Service Stack

Seven services run together, started by `run.sh` in dependency order:

| Service | Port(s) | Role |
|---|---|---|
| Cherrybot Simulation | 8099 | Virtual robot (WoT) |
| Formalizer CoALA Agent | 9997 | A2A agent: NL → formal commands |
| Goal MCP | 9996, 5002 | MCP server + GUI for goal state |
| Cherrybot Proxy | 8086, 8090 | Flask + MCP bridge to robot |
| SEM Flask App | 5000 | Core SEM server |
| SEM MCP | 8200 | MCP interface to SEM |
| Executor Agent | — | Orchestrates the full workflow |

### Core SEM (`app.py`)

The main server maintains a global RDF knowledge graph (`envKG`) alongside per-profile and per-artifact graphs. Key responsibilities:

- **Artifact registration** — `register_*` functions convert MCP, A2A, WoT Thing Descriptions, and UTCP manuals into RDF profiles stored in `artifacts`
- **Profile management** — Agent profiles (with natural-language context) stored in `profiles`
- **Signifier matching** — `signifier_filter()` uses an LLM to semantically match signifier contexts against an agent's current context; `selection()` returns the filtered set for a profile
- **RDF namespaces** — HMAS (`https://purl.org/hmas/`), HCTL, TD, HTTP, JS from W3C/WoT specs

### CoALA Agent Framework (`llm_agent/coala/coala.py`)

A custom LLM agent architecture implementing:
- **Procedural memory** — persistent state between agent steps
- **Sensory input** — signifier discovery and LLM-based tool filtering
- **Body** — tool execution with async timeouts
- **Windowed chat memory** — configurable conversation history

### Executor Agent (`llm_agent/executor_agent.py`)

Deterministic 8-state workflow using a single `current_memory` field as the source of truth:
1. Update profile context → 2. Read signifiers → 3. Fetch goal → 4. Signal formalization need → 5. Find formalizer signifier → 6. Formalize NL goal → 7. Signal execution ready → 8. Execute robot action

### Protocol Adapters

- **`a2a_sem/`** — A2A formalizer agent; converts NL goals to `move(dist)` / `rotate(angle)` commands with unit conversion; exposes agent card at `/.well-known/agent-card.json`
- **`mcp_sem/sem_mcp.py`** — Dynamically generates MCP tools from signifiers; maps JSON Schema to Python types
- **`mcp_sem/goal_mcp.py`** — Thread-safe goal state with read (`CurrentGoal`) and write (`provide_feedback`) tools
- **`wot_sem/`** — WoT Thing Description consumer; Cherrybot proxy bridges WoT and MCP interfaces
- **`utcp_sem/`** — UTCP client that creates profiles from protocol manuals

### Configuration (`config.json`)

Runtime LLM configuration for each subsystem:
```json
{
  "sem": { "provider": "ollama", "model": "ministral-3:latest" },
  "llm_agent": { "provider": "openai", "model": "gpt-4.1-mini" },
  "formalizer_agent": { "provider": "openai", "model": "gpt-4.1-mini" },
  "sem_mcp_endpoint": "http://localhost:8200/"
}
```
Supported providers: `ollama`, `openai`. API key in `API_KEY.txt` (not committed).

### RDF/Semantic Layer

The system uses `rdflib` throughout. Profiles and artifacts are stored as RDF graphs using HMAS signifier vocabulary. The `signifier.py` module provides the `Signifier` abstraction. Turtle and JSON-LD serialization formats are both supported for artifact retrieval.

## Key Design Patterns

- **Signifier pattern** — Affordances are context-dependent: the same service exposes different tools depending on the agent's declared context
- **LLM-as-router** — `signifier_filter()` uses an LLM (configurable) to decide relevance, not hardcoded rules
- **Protocol unification** — MCP, A2A, WoT TD, and UTCP are all converted to the same HMAS RDF profile format on registration
- **Type-safe dynamic tools** — CoALA maps JSON Schema types to Python at runtime for tool call validation
