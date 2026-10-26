# Changelog

## 0.1.2 — 2026-10-27

Second debug round (Codex error-path audit of the 0.1.1 fixes; all 11
findings reproduced against the real code before fixing, none dismissed):

- Usage accounting is now a persisted ledger: totals, billed event ids and
  unsettled minutes are written per event, so retries and crash recovery
  can neither double-count nor silently lose spend; a resumed attempt
  reports only its unsettled delta.
- Ledgers from fixture replays or a different model are never seeded into
  a real run, and fixture-replay evidence is stamped `simulated` like any
  other synthetic run.
- Token usage that is null, empty or structurally unrecognisable is
  treated as unknown and blocks settlement (previously counted as zero);
  the unknown-usage flag itself now survives a crash.
- Report alias protection covers secondary inputs too: a gate report can
  no longer overwrite a Godot verify script, `project.godot`, or the CLIP
  adapter it resolved.
- GUI receipts must be affirmatively successful: an empty or list payload
  no longer passes a screenshot, and contradictory receipts such as
  `{"ok": true, "success": "false"}` are rejected for click/type.
- CLIP weights are structurally validated and loaded by local path only;
  a corrupt cache fails before any load attempt instead of falling back
  to a download.
- The file-picker regression suite now executes the generated script
  against fake panels (behavioural tests) instead of only matching
  source strings.

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
