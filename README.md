# Agentic Web hMAS

The project was coded with the help of OpenAI Codex and Claude Code.

## The Signifier Exposure Mechanism (SEM)

The file [`app.py`](app.py) is the main file implementing and running the SEM server. The SEM runs on localhost, port 5000.

http://localhost:5000/artifacts/list indicates the list of artifact profiles.

An MCP server to interact with the SEM is provided by [`sem_mcp.py`](mcp_sem/sem_mcp.py). The MCP server runs on localhost, port 8200.

To create profiles for MCP servers, the code is available here: [`here`](mcp_sem/mcp_interact.py).

To create profiles for A2A agents, the code is available here: [`here`](a2a_sem/a2a_interact.py).

To create profiles for UTCP manuals, the code is available here: [`here`](utcp_sem/utcp_interact.py).

To create profiles for WoT Things, the code is available here: [`here`](wot_sem/wot_interact.py).

## Agent and Environment

### Goal MCP

The code for the Goal MCP server is available [`here`](mcp_sem/goal_mcp.py). The MCP server is available at: http://localhost:9996/mcp. The GUI is available at http://localhost:5002.

### A2A Formalizer Agent

The code for the A2A Formalizer Agent is available [`here`](a2a_sem/formalizer/formalizer_coala.py). Its A2A card is available at: http://localhost:9997/.well-known/agent-card.json

### Cherrybot proxy

The code for the cherrybot proxy is available [`here`](wot_sem/cherrybot_proxy.py) and cherrybot proxy is available at http://localhost:8086/.

The cherrybot proxy can rely on a cherrybot simulation, whose code is available [`here`](wot_sem/cherrybot_simulation.py) 

### Executor Agent

The code for the executor agent is available [`here`](llm_agent/executor_agent.py). It relies on our implementation of the CoALA architecture [`here`](llm_agent/coala/coala.py).

## Run

Set the [`OpenAI API key`](API_KEY.txt) if an OpenAI model is used Otherwise, local Ollama models are also supported.

Use ```./run.sh``` to run the environment with UTCP manual being used to register the robot proxy, a simulation of the robot is used, and the goal "Rotate the robot by 12 degrees".

Use ```./wot_run.sh``` to run the environment with the WoT TD being used to register the robot proxy, a simulation of the robot is used, and the goal "Move the robot by 10 centimeters".

Use ```./run_real.sh``` to run the environment with UTCP manual being used to register the robot proxy, the real robot is used,  and the goal "Rotate the robot by 12 degrees".

Use ```./wot_run_real.sh``` to run the environment with the WoT TD being used to register the robot proxy, the real robot is used, and the goal "Move the robot by 10 centimeters".

Use ```uv run llm_agent/executor_agent.py``` to run the executor agent


## Evaluation

The script [`evaluation.py`](evaluation.py) performs the evaluation. The results are presented in the file [`results.txt`](results.txt).

The script [`agent_evaluation.py`](agent_evaluation.py).) performs the evaluation. The results are presented in the file [`agent_results.txt`](agent_results.txt).).

