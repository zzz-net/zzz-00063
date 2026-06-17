import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional

from .models import (
    ApprovalDecision,
    BatchStatus,
    CheckResultStatus,
    can_transition,
)


DEFAULT_DB_PATH = os.path.join(os.getcwd(), ".patchgate", "patchgate.db")


class Storage:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    manifest_path TEXT NOT NULL,
                    manifest_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS manifest_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    item_index INTEGER NOT NULL,
                    package_name TEXT NOT NULL,
                    version TEXT,
                    source_path TEXT,
                    checksum TEXT,
                    metadata_json TEXT,
                    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_items_batch ON manifest_items(batch_id);
                CREATE INDEX IF NOT EXISTS idx_items_pkg ON manifest_items(batch_id, package_name);

                CREATE TABLE IF NOT EXISTS check_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    item_id INTEGER,
                    rule_id TEXT NOT NULL,
                    rule_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE,
                    FOREIGN KEY (item_id) REFERENCES manifest_items(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_checks_batch ON check_results(batch_id);
                CREATE INDEX IF NOT EXISTS idx_checks_status ON check_results(batch_id, status);

                CREATE TABLE IF NOT EXISTS approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    approver TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    comment TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_approvals_batch ON approvals(batch_id);

                CREATE TABLE IF NOT EXISTS publish_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    action TEXT NOT NULL,
                    comment TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_publish_batch ON publish_records(batch_id);

                CREATE TABLE IF NOT EXISTS status_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    operator TEXT,
                    note TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_status_history_batch ON status_history(batch_id);
                """
            )

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def create_batch(
        self,
        batch_id: str,
        name: str,
        description: Optional[str],
        manifest_path: str,
        manifest_hash: str,
        items: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        now = self._now()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO batches
                   (id, name, description, manifest_path, manifest_hash, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    batch_id,
                    name,
                    description,
                    manifest_path,
                    manifest_hash,
                    BatchStatus.CREATED.value,
                    now,
                    now,
                ),
            )
            for idx, item in enumerate(items):
                conn.execute(
                    """INSERT INTO manifest_items
                       (batch_id, item_index, package_name, version, source_path, checksum, metadata_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        batch_id,
                        idx,
                        item.get("package_name", ""),
                        item.get("version"),
                        item.get("source_path"),
                        item.get("checksum"),
                        json.dumps(item.get("metadata", {}), ensure_ascii=False)
                        if item.get("metadata")
                        else None,
                    ),
                )
            conn.execute(
                """INSERT INTO status_history
                   (batch_id, from_status, to_status, operator, note, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (batch_id, None, BatchStatus.CREATED.value, "system", "batch created", now),
            )
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
            if not row:
                return None
            batch = dict(row)
            item_rows = conn.execute(
                "SELECT * FROM manifest_items WHERE batch_id = ? ORDER BY item_index",
                (batch_id,),
            ).fetchall()
            batch["items"] = [dict(r) for r in item_rows]
            for it in batch["items"]:
                if it.get("metadata_json"):
                    it["metadata"] = json.loads(it["metadata_json"])
                    del it["metadata_json"]
            return batch

    def list_batches(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM batches ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def transition_status(
        self,
        batch_id: str,
        target: BatchStatus,
        operator: str = "system",
        note: str = "",
    ) -> None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT status FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"批次 {batch_id} 不存在")
            current = BatchStatus(row["status"])
            if not can_transition(current, target):
                raise ValueError(
                    f"状态流转不合法: {current.value} -> {target.value}"
                )
            now = self._now()
            conn.execute(
                "UPDATE batches SET status = ?, updated_at = ? WHERE id = ?",
                (target.value, now, batch_id),
            )
            conn.execute(
                """INSERT INTO status_history
                   (batch_id, from_status, to_status, operator, note, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (batch_id, current.value, target.value, operator, note, now),
            )

    def get_current_status(self, batch_id: str) -> BatchStatus:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT status FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"批次 {batch_id} 不存在")
            return BatchStatus(row["status"])

    def clear_check_results(self, batch_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM check_results WHERE batch_id = ?", (batch_id,))

    def add_check_result(
        self,
        batch_id: str,
        item_id: Optional[int],
        rule_id: str,
        rule_name: str,
        status: CheckResultStatus,
        severity: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = self._now()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO check_results
                   (batch_id, item_id, rule_id, rule_name, status, severity, message, details_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    batch_id,
                    item_id,
                    rule_id,
                    rule_name,
                    status.value,
                    severity,
                    message,
                    json.dumps(details, ensure_ascii=False) if details else None,
                    now,
                ),
            )

    def get_check_results(
        self, batch_id: str, status: Optional[CheckResultStatus] = None
    ) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            sql = "SELECT * FROM check_results WHERE batch_id = ?"
            params: list = [batch_id]
            if status:
                sql += " AND status = ?"
                params.append(status.value)
            sql += " ORDER BY id"
            rows = conn.execute(sql, params).fetchall()
            results = [dict(r) for r in rows]
            for r in results:
                if r.get("details_json"):
                    r["details"] = json.loads(r["details_json"])
                    del r["details_json"]
            return results

    def has_unresolved_failures(self, batch_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS cnt FROM check_results
                   WHERE batch_id = ? AND status = ? AND severity = 'error'""",
                (batch_id, CheckResultStatus.FAILED.value),
            ).fetchone()
            return row["cnt"] > 0

    def add_approval(
        self,
        batch_id: str,
        approver: str,
        decision: ApprovalDecision,
        comment: Optional[str],
    ) -> Dict[str, Any]:
        now = self._now()
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO approvals
                   (batch_id, approver, decision, comment, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (batch_id, approver, decision.value, comment, now),
            )
            aid = cur.lastrowid
            row = conn.execute(
                "SELECT * FROM approvals WHERE id = ?", (aid,)
            ).fetchone()
            return dict(row)

    def get_approvals(self, batch_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE batch_id = ? ORDER BY id DESC",
                (batch_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def add_publish_record(
        self,
        batch_id: str,
        operator: str,
        action: str,
        comment: Optional[str],
    ) -> Dict[str, Any]:
        now = self._now()
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO publish_records
                   (batch_id, operator, action, comment, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (batch_id, operator, action, comment, now),
            )
            rid = cur.lastrowid
            row = conn.execute(
                "SELECT * FROM publish_records WHERE id = ?", (rid,)
            ).fetchone()
            return dict(row)

    def get_publish_records(self, batch_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM publish_records WHERE batch_id = ? ORDER BY id DESC",
                (batch_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_status_history(self, batch_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM status_history WHERE batch_id = ? ORDER BY id",
                (batch_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def find_item_id(self, batch_id: str, package_name: str, item_index: Optional[int] = None) -> Optional[int]:
        with self._conn() as conn:
            sql = "SELECT id FROM manifest_items WHERE batch_id = ? AND package_name = ?"
            params: list = [batch_id, package_name]
            if item_index is not None:
                sql += " AND item_index = ?"
                params.append(item_index)
            sql += " ORDER BY item_index LIMIT 1"
            row = conn.execute(sql, params).fetchone()
            return row["id"] if row else None

    def get_item_by_index(self, batch_id: str, item_index: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM manifest_items WHERE batch_id = ? AND item_index = ?",
                (batch_id, item_index),
            ).fetchone()
            return dict(row) if row else None
