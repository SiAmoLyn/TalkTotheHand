# Changelog

## 0.1.1 — 2026-10-24

Hardening release from a two-auditor cross-check (Codex + Gemini, findings
verified one by one before fixing). No new features; every change closes a
way the hand could claim success it had not earned:

- Blender runs now pass `--python-exit-code 1`: a script exception can no
  longer exit 0 and be reported as passed.
- Path guards compare case-folded components, so `.GIT`, `.SSH` or
  `Memory.md` spellings cannot bypass them on case-insensitive filesystems;
  the framework's own `tools/` directory is no longer task-writable.
- Gate/report outputs can never overwrite their own inputs (all tools).
- Evidence from the simulated executor is stamped `simulated`, never
  `verified`; a task must declare at least one acceptance gate.
- OpenCode stream accounting: a new step revokes an earlier terminal
  marker; missing token usage blocks settlement instead of counting as
  zero; a crashed attempt's spend is persisted incrementally and seeded
  into the resumed run.
- CLIP adapter: degenerate/non-finite embeddings are rejected instead of
  being clamped into a valid score, and missing local weights are refused
  outright (the adapter never downloads).
- Watchdog: closure is recorded only after the closing action's receipt
  passes; the recovery confirmation is persisted so the closing action
  cannot deadlock against its own cooldown; bracketed IPv6 log targets
  parse correctly.
- GUI tools: screenshots must carry the PNG signature and an honestly-true
  receipt; the file picker only drives file-open panels and requires the
  target row to be verifiably selected before confirming.

## 0.1.0 — 2026-10-24

First public release: LangGraph task state machine with SQLite checkpoints
and approval interrupts, Pydantic task/evidence contracts, OpenCode
executor, MCP server, code-enforced acceptance gates (AABB, CLIP
similarity, render-review packaging, Godot verify), SSH dispatch, a Day8
blackhole watchdog, and Cua-driven GUI tools — all behind evidence
packages that must pass mechanical verification before a run may call
itself complete.
