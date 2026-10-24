"""Synchronous LangGraph saver using stdlib SQLite; no extra direct dependency."""

from collections.abc import Iterator, Sequence
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple, WRITES_IDX_MAP


class SQLiteCheckpointer(BaseCheckpointSaver):
    """Persist checkpoint data AND pending writes for cross-process resume."""

    def __init__(self, path: Path):
        super().__init__()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.lock = RLock()
        self.conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS checkpoints (
                thread_id TEXT NOT NULL, namespace TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL, parent_id TEXT,
                kind TEXT NOT NULL, data BLOB NOT NULL,
                metadata_kind TEXT NOT NULL, metadata BLOB NOT NULL,
                PRIMARY KEY(thread_id, namespace, checkpoint_id)
            );
            CREATE TABLE IF NOT EXISTS writes (
                thread_id TEXT NOT NULL, namespace TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL, task_id TEXT NOT NULL,
                idx INTEGER NOT NULL, channel TEXT NOT NULL,
                kind TEXT NOT NULL, data BLOB NOT NULL,
                PRIMARY KEY(thread_id, namespace, checkpoint_id, task_id, idx)
            );
        """)

    @staticmethod
    def _config(thread: str, namespace: str, checkpoint_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread, "checkpoint_ns": namespace,
                                 "checkpoint_id": checkpoint_id}}

    def get_tuple(self, config: dict[str, Any]) -> CheckpointTuple | None:
        cfg = config["configurable"]
        thread, namespace = cfg["thread_id"], cfg.get("checkpoint_ns", "")
        query = "SELECT checkpoint_id,parent_id,kind,data,metadata_kind,metadata FROM checkpoints WHERE thread_id=? AND namespace=?"
        args: list[Any] = [thread, namespace]
        if checkpoint_id := cfg.get("checkpoint_id"):
            query += " AND checkpoint_id=?"
            args.append(checkpoint_id)
        query += " ORDER BY checkpoint_id DESC LIMIT 1"
        with self.lock:
            row = self.conn.execute(query, args).fetchone()
            if row is None:
                return None
            cid, parent, kind, data, metadata_kind, metadata = row
            pending = self.conn.execute(
                "SELECT task_id,channel,kind,data FROM writes WHERE thread_id=? AND namespace=? AND checkpoint_id=? ORDER BY task_id,idx",
                (thread, namespace, cid),
            ).fetchall()
            return CheckpointTuple(
                config=self._config(thread, namespace, cid),
                checkpoint=self.serde.loads_typed((kind, data)),
                metadata=self.serde.loads_typed((metadata_kind, metadata)),
                parent_config=self._config(thread, namespace, parent) if parent else None,
                pending_writes=[(tid, channel, self.serde.loads_typed((typ, blob)))
                                for tid, channel, typ, blob in pending],
            )

    def list(self, config: dict[str, Any] | None, *, filter: dict[str, Any] | None = None,
             before: dict[str, Any] | None = None, limit: int | None = None) -> Iterator[CheckpointTuple]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT thread_id,namespace,checkpoint_id FROM checkpoints ORDER BY checkpoint_id DESC"
            ).fetchall()
        count = 0
        for thread, namespace, cid in rows:
            if config:
                cfg = config["configurable"]
                if thread != cfg["thread_id"] or namespace != cfg.get("checkpoint_ns", ""):
                    continue
            if before and cid >= before["configurable"]["checkpoint_id"]:
                continue
            item = self.get_tuple(self._config(thread, namespace, cid))
            if item is None or (filter and any(item.metadata.get(k) != v for k, v in filter.items())):
                continue
            if limit is not None and count >= limit:
                break
            yield item
            count += 1

    def put(self, config: dict[str, Any], checkpoint: dict[str, Any],
            metadata: dict[str, Any], new_versions: dict[str, Any]) -> dict[str, Any]:
        cfg = config["configurable"]
        thread, namespace = cfg["thread_id"], cfg.get("checkpoint_ns", "")
        kind, data = self.serde.dumps_typed(checkpoint)
        metadata_kind, meta = self.serde.dumps_typed(metadata)
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?,?,?,?,?)",
                (thread, namespace, checkpoint["id"], cfg.get("checkpoint_id"), kind, data, metadata_kind, meta),
            )
        return self._config(thread, namespace, checkpoint["id"])

    def put_writes(self, config: dict[str, Any], writes: Sequence[tuple[str, Any]],
                   task_id: str, task_path: str = "") -> None:
        cfg = config["configurable"]
        with self.lock, self.conn:
            for idx, (channel, value) in enumerate(writes):
                kind, data = self.serde.dumps_typed(value)
                # LangGraph reserved writes can be replaced; normal task writes
                # remain idempotent if a completed node is replayed after a crash.
                mode = "REPLACE" if channel in WRITES_IDX_MAP else "IGNORE"
                self.conn.execute(
                    f"INSERT OR {mode} INTO writes VALUES (?,?,?,?,?,?,?,?)",
                    (cfg["thread_id"], cfg.get("checkpoint_ns", ""), cfg["checkpoint_id"],
                     task_id, WRITES_IDX_MAP.get(channel, idx), channel, kind, data),
                )

    def delete_thread(self, thread_id: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM writes WHERE thread_id=?", (thread_id,))
            self.conn.execute("DELETE FROM checkpoints WHERE thread_id=?", (thread_id,))

    def close(self) -> None:
        self.conn.close()
