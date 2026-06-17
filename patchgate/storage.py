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
        dir_path = os.path.dirname(self.db_path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
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

                CREATE TABLE IF NOT EXISTS snapshot_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    old_snapshot_id INTEGER,
                    new_snapshot_id INTEGER,
                    diff_summary TEXT,
                    risk_level TEXT,
                    operator TEXT,
                    note TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE,
                    FOREIGN KEY (old_snapshot_id) REFERENCES rule_snapshots(id) ON DELETE SET NULL,
                    FOREIGN KEY (new_snapshot_id) REFERENCES rule_snapshots(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_snapshot_decisions_batch ON snapshot_decisions(batch_id);

                CREATE TABLE IF NOT EXISTS rule_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    snapshot_name TEXT NOT NULL,
                    rules_config_path TEXT NOT NULL,
                    rules_sha256 TEXT NOT NULL,
                    rules_yaml TEXT NOT NULL,
                    rule_count INTEGER NOT NULL,
                    enabled_rule_count INTEGER NOT NULL,
                    summary_json TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    superseded_by INTEGER,
                    operator TEXT,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE,
                    FOREIGN KEY (superseded_by) REFERENCES rule_snapshots(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_rule_snapshots_batch ON rule_snapshots(batch_id);
                CREATE INDEX IF NOT EXISTS idx_rule_snapshots_active ON rule_snapshots(batch_id, is_active);

                CREATE TABLE IF NOT EXISTS handover_imports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    original_batch_id TEXT,
                    package_hash TEXT NOT NULL,
                    package_generated_at TEXT NOT NULL,
                    package_generated_by TEXT,
                    package_note TEXT,
                    imported_at TEXT NOT NULL,
                    imported_by TEXT,
                    import_note TEXT,
                    resolution_summary TEXT,
                    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_handover_imports_batch ON handover_imports(batch_id);
                CREATE INDEX IF NOT EXISTS idx_handover_imports_hash ON handover_imports(package_hash);
                """
            )
        with self._conn() as conn:
            try:
                conn.execute("ALTER TABLE handover_imports ADD COLUMN package_note TEXT")
            except Exception:
                pass

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

    def add_rule_snapshot(
        self,
        batch_id: str,
        snapshot_name: str,
        rules_config_path: str,
        rules_sha256: str,
        rules_yaml: str,
        rule_count: int,
        enabled_rule_count: int,
        summary: List[Dict[str, Any]],
        operator: str = "system",
        reason: str = "",
    ) -> int:
        now = self._now()
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO rule_snapshots
                   (batch_id, snapshot_name, rules_config_path, rules_sha256, rules_yaml,
                    rule_count, enabled_rule_count, summary_json, is_active,
                    operator, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                (
                    batch_id,
                    snapshot_name,
                    rules_config_path,
                    rules_sha256,
                    rules_yaml,
                    rule_count,
                    enabled_rule_count,
                    json.dumps(summary, ensure_ascii=False),
                    operator,
                    reason,
                    now,
                ),
            )
            return cur.lastrowid

    def get_active_rule_snapshot(self, batch_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM rule_snapshots WHERE batch_id = ? AND is_active = 1 ORDER BY id DESC LIMIT 1",
                (batch_id,),
            ).fetchone()
            if not row:
                return None
            snap = dict(row)
            if snap.get("summary_json"):
                snap["summary"] = json.loads(snap["summary_json"])
                del snap["summary_json"]
            snap["is_active"] = bool(snap["is_active"])
            return snap

    def get_rule_snapshots(self, batch_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM rule_snapshots WHERE batch_id = ? ORDER BY id",
                (batch_id,),
            ).fetchall()
            snaps = []
            for row in rows:
                snap = dict(row)
                if snap.get("summary_json"):
                    snap["summary"] = json.loads(snap["summary_json"])
                    del snap["summary_json"]
                snap["is_active"] = bool(snap["is_active"])
                snaps.append(snap)
            return snaps

    def get_rule_snapshot_by_id(self, snapshot_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM rule_snapshots WHERE id = ?",
                (snapshot_id,),
            ).fetchone()
            if not row:
                return None
            snap = dict(row)
            if snap.get("summary_json"):
                snap["summary"] = json.loads(snap["summary_json"])
                del snap["summary_json"]
            snap["is_active"] = bool(snap["is_active"])
            return snap

    def supersede_rule_snapshot(
        self,
        batch_id: str,
        old_snapshot_id: int,
        new_snapshot_id: int,
        operator: str = "system",
        reason: str = "",
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE rule_snapshots SET is_active = 0, superseded_by = ? WHERE id = ? AND batch_id = ?",
                (new_snapshot_id, old_snapshot_id, batch_id),
            )

    def has_rule_snapshot(self, batch_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM rule_snapshots WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            return row["cnt"] > 0

    def add_snapshot_decision(
        self,
        batch_id: str,
        decision: str,
        old_snapshot_id: Optional[int],
        new_snapshot_id: Optional[int],
        diff_summary: Optional[str] = None,
        risk_level: Optional[str] = None,
        operator: str = "system",
        note: Optional[str] = None,
    ) -> int:
        now = self._now()
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO snapshot_decisions
                   (batch_id, decision, old_snapshot_id, new_snapshot_id,
                    diff_summary, risk_level, operator, note, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (batch_id, decision, old_snapshot_id, new_snapshot_id,
                 diff_summary, risk_level, operator, note, now),
            )
            return cur.lastrowid

    def get_snapshot_decisions(self, batch_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM snapshot_decisions WHERE batch_id = ? ORDER BY id",
                (batch_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_last_revoke_context(self, batch_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            revoke_row = conn.execute(
                """SELECT * FROM status_history
                   WHERE batch_id = ? AND to_status = ?
                   ORDER BY id DESC LIMIT 1""",
                (batch_id, BatchStatus.REVOKED.value),
            ).fetchone()
            if not revoke_row:
                return None
            revoke_info = dict(revoke_row)

            publish_row = conn.execute(
                """SELECT * FROM publish_records
                   WHERE batch_id = ? AND action = 'revoke'
                   ORDER BY id DESC LIMIT 1""",
                (batch_id,),
            ).fetchone()
            if publish_row:
                revoke_info["revoke_operator"] = dict(publish_row)["operator"]
                revoke_info["revoke_comment"] = dict(publish_row).get("comment")

            approved_row = conn.execute(
                """SELECT * FROM status_history
                   WHERE batch_id = ? AND to_status = ? AND id > ?
                   ORDER BY id LIMIT 1""",
                (batch_id, BatchStatus.APPROVED.value, revoke_info["id"]),
            ).fetchone()
            if approved_row:
                revoke_info["restore_note"] = dict(approved_row).get("note")

            return revoke_info

    def delete_batch(self, batch_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM batches WHERE id = ?", (batch_id,))

    def force_set_status(
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

    def add_status_history_entry(
        self,
        batch_id: str,
        from_status: Optional[str],
        to_status: str,
        operator: str = "system",
        note: str = "",
        created_at: Optional[str] = None,
    ) -> int:
        now = created_at or self._now()
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO status_history
                   (batch_id, from_status, to_status, operator, note, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (batch_id, from_status, to_status, operator, note, now),
            )
            return cur.lastrowid

    def add_handover_import_record(
        self,
        batch_id: str,
        package_hash: str,
        package_generated_at: str,
        package_generated_by: str,
        imported_by: str,
        import_note: Optional[str],
        resolution_summary: str,
        original_batch_id: Optional[str] = None,
        package_note: Optional[str] = None,
    ) -> int:
        now = self._now()
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO handover_imports
                   (batch_id, original_batch_id, package_hash, package_generated_at,
                    package_generated_by, package_note, imported_at, imported_by, import_note,
                    resolution_summary)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    batch_id,
                    original_batch_id,
                    package_hash,
                    package_generated_at,
                    package_generated_by,
                    package_note,
                    now,
                    imported_by,
                    import_note,
                    resolution_summary,
                ),
            )
            return cur.lastrowid

    def get_handover_import_by_hash(self, package_hash: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM handover_imports WHERE package_hash = ? ORDER BY id DESC LIMIT 1",
                (package_hash,),
            ).fetchone()
            if not row:
                return None
            return dict(row)

    def get_handover_imports_for_batch(self, batch_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM handover_imports WHERE batch_id = ? ORDER BY id",
                (batch_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_all_handover_imports(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM handover_imports ORDER BY id DESC"
            ).fetchall()
            return [dict(r) for r in rows]
