"""Local hand CLI. All managed files live beneath the installation directory."""

import argparse
import json
import shutil
from pathlib import Path
import sys
import uuid

import yaml

from .contracts import load_task
from .graph import HandRunner
from .tools import project_path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def demo_task(task_id: str) -> dict:
    return {
        "id": task_id, "title": "HAND Package 1 fake demo", "goal": "Verify the offline skeleton",
        "tool_class": "text", "context_files": [], "spec": "Create a clearly labelled fake text artifact",
        "redlines": ["No network, models or real tools"], "gates": ["fake_artifact_check"],
        "budget": {"max_minutes": 1, "max_tokens": 100},
        "deliverables": [{"path": "demo.txt", "type": "text"}],
        "approval_points": [{"id": "demo-approval", "reason": "Demonstrate interrupt/resume", "triggered": True}],
    }


def _checkpoint_summary(root: Path) -> dict:
    """Read latest top-level checkpoints without creating a DB or resuming work."""
    from contextlib import closing
    import sqlite3

    try:
        path = project_path(root, "work/checkpoints.sqlite")
        if not path.is_file():
            return {"status": "unconfigured", "counts": {}, "total": 0}
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        serde = JsonPlusSerializer()
        counts, errors = {}, []
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            rows = conn.execute("""
                SELECT c.thread_id,c.kind,c.data FROM checkpoints c
                JOIN (SELECT thread_id,MAX(checkpoint_id) latest FROM checkpoints
                      WHERE namespace='' GROUP BY thread_id) t
                ON c.thread_id=t.thread_id AND c.checkpoint_id=t.latest
                WHERE c.namespace=''
            """).fetchall()
        for task_id, kind, data in rows:
            try:
                status = serde.loads_typed((kind, data))["channel_values"].get("status", "UNKNOWN")
                if not isinstance(status, str):
                    raise ValueError("Checkpoint status must be a string")
                counts[status] = counts.get(status, 0) + 1
            except Exception as exc:
                errors.append({"task_id": task_id, "error": str(exc)})
        return {"status": "partial" if errors else "ok", "counts": counts, "total": len(rows), "errors": errors}
    except Exception as exc:
        return {"status": "fail", "error": str(exc), "counts": {}}


def status_all(root: Path, *, nodes_file: Path | None = None,
               monitor_state: Path | None = None, monitor_config: Path | None = None,
               ssh_executable: str | None = None, probe_timeout: float = 10) -> dict:
    import os
    from .monitor import load_config, read_state

    result = {"nodes": {"status": "unconfigured", "items": {}}}
    configured = nodes_file if nodes_file is not None else os.environ.get("HAND_NODES_FILE")
    if configured:
        try:
            from .nodes import load_nodes
            from .tools import ssh_dispatch
            config, source = load_nodes(configured, workspace=root)
            items = {}
            for name in config.nodes:
                try:
                    report = ssh_dispatch(name, ["true"], workspace=root, nodes_file=source,
                                          executable=ssh_executable, timeout=probe_timeout,
                                          connect_timeout=max(1, min(5, int(probe_timeout))),
                                          result_path=project_path(root, f"work/status-all/{uuid.uuid4().hex}/{name}.json"))
                    items[name] = {"status": "ok" if report["passed"] else "fail", "report": report}
                except Exception as exc:
                    items[name] = {"status": "fail", "error": str(exc)}
            result["nodes"] = {"status": "ok" if all(i["status"] == "ok" for i in items.values()) else "partial", "items": items}
        except Exception as exc:
            result["nodes"] = {"status": "fail", "items": {}, "error": getattr(exc, "report", str(exc))}
    result["checkpoints"] = _checkpoint_summary(root)
    try:
        configured_state = monitor_state if monitor_state is not None else os.environ.get("HAND_MONITOR_STATE")
        configured_monitor = monitor_config if monitor_config is not None else os.environ.get("HAND_MONITOR_CONFIG")
        if configured_state:
            path = project_path(root, configured_state)
        elif configured_monitor:
            path = load_config(Path(configured_monitor), root=root)["state_file"]
        else:
            path = None
        if path is None or not path.is_file():
            result["monitor"] = {"status": "unconfigured"}
        else:
            result["monitor"] = {"status": "ok", "state_file": str(path), "state": read_state(path)}
    except Exception as exc:
        result["monitor"] = {"status": "fail", "error": str(exc)}
    result["status"] = "ok" if all(result[key]["status"] == "ok" for key in ("nodes", "checkpoints", "monitor")) else "partial"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hand", description="HAND Packages 1–2 (offline FakeExecutor by default)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run").add_argument("task_yaml")
    commands.add_parser("status").add_argument("task_id")
    resume = commands.add_parser("resume", help="Explicitly approve a waiting task, or recover after a process crash")
    resume.add_argument("task_id")
    resume.add_argument("--reject", action="store_true", help="Reject the pending approval")
    demo = commands.add_parser("demo", help="Generate an offline demo and approve only its own pause")
    demo.add_argument("--executor", choices=["fake", "opencode-fixture"], default="fake")
    commands.add_parser("mcp-serve", help="Serve the local machine interface over MCP stdio")
    monitor = commands.add_parser("monitor", help="Run one durable, configured watchdog tick")
    monitor.add_argument("--once", required=True, action="store_true")
    monitor.add_argument("--config", type=Path)
    monitor.add_argument("--log-dir", type=Path)
    monitor.add_argument("--state-file", type=Path)
    monitor.add_argument("--dry-run", action="store_true")
    overview = commands.add_parser("status-all", help="Report nodes, checkpoint counts and monitor independently")
    overview.add_argument("--nodes-file", type=Path)
    overview.add_argument("--monitor-state", type=Path)
    overview.add_argument("--monitor-config", type=Path)
    overview.add_argument("--probe-timeout", type=float, default=10)
    args = parser.parse_args(argv)
    try:
        if args.command == "monitor":
            import os
            from .monitor import monitor_once
            configured = args.config if args.config is not None else os.environ.get("HAND_MONITOR_CONFIG")
            result = (monitor_once(Path(configured), root=PROJECT_ROOT, log_dir=args.log_dir,
                                   state_file=args.state_file, dry_run=args.dry_run) if configured else
                      {"status": "blocked", "passed": False, "error": "Monitor not configured (--config / HAND_MONITOR_CONFIG)"})
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
            return 0 if result["passed"] else 1
        if args.command == "status-all":
            print(json.dumps(status_all(PROJECT_ROOT, nodes_file=args.nodes_file, monitor_state=args.monitor_state,
                                        monitor_config=args.monitor_config, probe_timeout=args.probe_timeout),
                             indent=2, ensure_ascii=False, allow_nan=False))
            return 0
        if args.command == "mcp-serve":
            from .mcp_server import serve
            serve(PROJECT_ROOT)
            return 0
        with HandRunner(PROJECT_ROOT) as runner:
            if args.command == "run":
                path = project_path(PROJECT_ROOT, Path(args.task_yaml).absolute())
                result = runner.run(load_task(path))
            elif args.command == "status":
                result = runner.status(args.task_id)
            elif args.command == "resume":
                result = runner.resume(args.task_id, approved=not args.reject)
            else:
                task_id = f"demo-{uuid.uuid4().hex[:12]}"
                path = project_path(PROJECT_ROOT, f"inbox/{task_id}/task.yaml")
                path.parent.mkdir(parents=True, exist_ok=True)
                data = demo_task(task_id)
                if args.executor == "opencode-fixture":
                    workspace = project_path(PROJECT_ROOT, f"work/{task_id}")
                    workspace.mkdir(parents=True, exist_ok=True)
                    for source, target in (("opencode-normal.jsonl", "events.jsonl"), ("aabb.json", "bounds.json")):
                        shutil.copyfile(Path(__file__).parent / "fixtures" / source, workspace / target)
                    data.update(title="HAND Package 2 synthetic fixture demo", executor_hint="opencode-fixture",
                                spec="Replay synthetic JSONL and gate pre-staged AABB fixture; no real model/media acceptance",
                                deliverables=[{"path": "bounds.json", "type": "aabb-json"}], gates=["gate_aabb"],
                                opencode={"fixture": "events.jsonl"},
                                gate_options={"gate_aabb": {"artifact": "bounds.json", "ground_tolerance": 0.001,
                                                           "expected_dimensions": {"part": [1, 2, 3], "other": [1, 2, 3]}}})
                path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
                paused = runner.run(load_task(path))
                if paused["status"] != "WAITING_APPROVAL":
                    raise ValueError("Demo did not reach its approval interrupt")
                print(f"Demo interrupted for approval: {task_id}")
                # Only the generated offline fake/fixture demo is automatically approved.
                result = runner.resume(task_id)
                if result["status"] != "RETURNED":
                    raise ValueError(f"Demo failed: {result.get('reason')}")
                print(f"Demo passed: outbox/{task_id}/evidence.json")
            printable = dict(result)
            printable.pop("__interrupt__", None)
            print(json.dumps(printable, indent=2, ensure_ascii=False))
            return 1 if result["status"] == "BLOCKED" else 0
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
