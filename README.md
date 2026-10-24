# Talk to the Hand

> Talk to the hand — it talks back with evidence.

A contract-driven task agent ("a hand") for a personal AI assistant: work is
dispatched as **task packages**, executed by pluggable executors, and can only
be marked complete after passing **code-enforced acceptance gates** — never on
an executor's say-so.

**中文简介**：talktothehand 是一个为个人 AI 助手定制的任务执行代理：任务以「任务包」
形式派发、由可插拔执行器干活、必须通过代码强制的验收门（数值校验、几何校验、
相似度校验）才能进入「完成」状态，并为每项工作产出可核查的「证据包」。

## Why

Session-based coding agents produce work scattered across chat transcripts,
and quality gates written into prompts can be skipped — by the executor, or by
a lenient self-review. talktothehand moves the contract and the gates into code:

- A task that fails a gate **cannot** reach the `RETURNED` state; the state
  machine routes it to retry or `BLOCKED` instead.
- Every run returns an **evidence package**: artifacts with SHA-256 hashes,
  gate reports, logs, and cost — independently re-checkable after the fact.

## Features

- **Lifecycle state machine** (LangGraph): `RECEIVED → VALIDATED → PLANNED →
  WAITING_APPROVAL → RUNNING → GATING → RETURNED`, with SQLite checkpoints
  (custom saver, stdlib `sqlite3`), durable resume, and approval interrupts
  that executors cannot self-grant.
- **Pydantic contracts** for task packages and evidence packages; invalid
  input is rejected before any execution.
- **Executors**: a deterministic `FakeExecutor` for offline tests, and an
  OpenCode CLI executor (JSON event stream, session resume, hard timeouts
  with byte-preserving partial logs, isolated config/HOME, credential
  hygiene) plus an offline fixture-replay mode.
- **Tools & gates**: Blender and Godot runners, AABB geometry checks,
  CLIP-similarity gate (local adapter), SSH dispatch to configured nodes,
  and a render-review bundler that packages artifacts + reference hashes
  for human/model review without auto-grading.
- **MCP server** (official Python SDK, stdio): `hand_submit_task`,
  `hand_task_status`, `hand_task_resume`, `hand_get_evidence` — so other
  agents can drive the hand machine-to-machine, with structured error codes
  and no way to bypass approvals through tool parameters.
- **Path safety**: workspace guards, allow-listed directories, and
  SHA-256-locked reference files.

## Quickstart

Requires Python 3.12.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'

# Offline demo (fake executor + gates, no models or network):
.venv/bin/python -m hand demo        # or: hand demo

# Run the test suite:
.venv/bin/python -m pytest
```

Optional real integrations are configured by environment variables, e.g.
`HAND_OPENCODE` (OpenCode binary), `HAND_CLIPSIM_SCRIPT` (project-local CLIP
adapter), `HAND_NODES_FILE` (SSH node config; see `examples/`).

## Layout

- `src/hand/` — state machine, contracts, executors, tools, MCP server
- `tests/` — offline test suite (199 tests; fake executors and fixtures only)
- `examples/` — example task package and node config (placeholders only)
- `tools/` — project-local adapters (e.g. CLIP similarity)

## License

MIT — see [LICENSE](LICENSE).
