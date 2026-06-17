import json
import os
import hashlib
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .models import BatchStatus, CheckResultStatus, ApprovalDecision
from .rules import RuleEngine, create_engine_from_snapshot, diff_rules, check_default_rules_vs_snapshot
from .storage import Storage


HANDOVER_SCHEMA_VERSION = "1.0"


def build_handover_package(
    storage: Storage,
    batch_id: str,
    exporter: str = "unknown",
    note: Optional[str] = None,
) -> Dict[str, Any]:
    batch = storage.get_batch(batch_id)
    if not batch:
        raise ValueError(f"批次 {batch_id} 不存在")

    checks = storage.get_check_results(batch_id)
    approvals = storage.get_approvals(batch_id)
    publish_records = storage.get_publish_records(batch_id)
    status_history = storage.get_status_history(batch_id)
    rule_snapshots = storage.get_rule_snapshots(batch_id)
    active_snapshot = storage.get_active_rule_snapshot(batch_id)
    snapshot_decisions = storage.get_snapshot_decisions(batch_id)
    revoke_ctx = storage.get_last_revoke_context(batch_id)

    has_error = any(
        c["status"] == CheckResultStatus.FAILED.value and c["severity"] == "error"
        for c in checks
    )
    last_approval = next(
        (a for a in approvals if a["decision"] == ApprovalDecision.APPROVE.value), None
    )
    last_rejection = next(
        (a for a in approvals if a["decision"] == ApprovalDecision.REJECT.value), None
    )

    todo_actions = _generate_todo_actions(
        BatchStatus(batch["status"]), batch_id, has_error, last_approval, last_rejection
    )

    log_index = _build_log_index(status_history, approvals, publish_records, snapshot_decisions)

    package = {
        "schema_version": HANDOVER_SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "generated_by": exporter,
        "note": note,
        "package_hash": "",
        "batch": {
            "id": batch["id"],
            "name": batch["name"],
            "description": batch.get("description"),
            "manifest_path": batch["manifest_path"],
            "manifest_hash": batch["manifest_hash"],
            "status": batch["status"],
            "created_at": batch["created_at"],
            "updated_at": batch["updated_at"],
            "item_count": len(batch["items"]),
            "items": [
                {
                    "index": it["item_index"],
                    "package_name": it["package_name"],
                    "version": it.get("version"),
                    "source_path": it.get("source_path"),
                    "checksum": it.get("checksum"),
                    "metadata": it.get("metadata", {}),
                }
                for it in batch["items"]
            ],
        },
        "latest_validation": {
            "has_error": has_error,
            "total_checks": len(checks),
            "passed": sum(1 for c in checks if c["status"] == CheckResultStatus.PASSED.value),
            "failed": sum(1 for c in checks if c["status"] == CheckResultStatus.FAILED.value),
            "warnings": sum(
                1 for c in checks
                if c["status"] == CheckResultStatus.FAILED.value and c["severity"] == "warning"
            ),
            "errors": sum(
                1 for c in checks
                if c["status"] == CheckResultStatus.FAILED.value and c["severity"] == "error"
            ),
            "skipped": sum(1 for c in checks if c["status"] == CheckResultStatus.SKIPPED.value),
            "failures": [
                {
                    "package_name": next(
                        (it["package_name"] for it in batch["items"] if it["id"] == c["item_id"]),
                        "(批次级)",
                    ),
                    "item_index": next(
                        (it["item_index"] for it in batch["items"] if it["id"] == c["item_id"]),
                        None,
                    ),
                    "rule_id": c["rule_id"],
                    "rule_name": c["rule_name"],
                    "severity": c["severity"],
                    "message": c["message"],
                    "details": c.get("details"),
                }
                for c in checks
                if c["status"] == CheckResultStatus.FAILED.value
            ],
        },
        "approval_conclusion": {
            "latest_decision": last_approval["decision"] if last_approval else (
                last_rejection["decision"] if last_rejection else None
            ),
            "latest_approver": last_approval["approver"] if last_approval else (
                last_rejection["approver"] if last_rejection else None
            ),
            "latest_comment": last_approval.get("comment") if last_approval else (
                last_rejection.get("comment") if last_rejection else None
            ),
            "latest_at": last_approval["created_at"] if last_approval else (
                last_rejection["created_at"] if last_rejection else None
            ),
            "all_approvals": [
                {
                    "approver": a["approver"],
                    "decision": a["decision"],
                    "comment": a.get("comment"),
                    "created_at": a["created_at"],
                }
                for a in approvals
            ],
        },
        "rule_snapshots": {
            "active": _serialize_snapshot(active_snapshot) if active_snapshot else None,
            "all": [_serialize_snapshot(s) for s in rule_snapshots],
            "decisions": [
                {
                    "decision": sd["decision"],
                    "old_snapshot_id": sd.get("old_snapshot_id"),
                    "new_snapshot_id": sd.get("new_snapshot_id"),
                    "diff_summary": sd.get("diff_summary"),
                    "risk_level": sd.get("risk_level"),
                    "operator": sd.get("operator"),
                    "note": sd.get("note"),
                    "created_at": sd["created_at"],
                }
                for sd in snapshot_decisions
            ],
        },
        "todo_actions": todo_actions,
        "log_index": log_index,
        "publish_history": [
            {
                "operator": p["operator"],
                "action": p["action"],
                "comment": p.get("comment"),
                "created_at": p["created_at"],
            }
            for p in publish_records
        ],
        "status_history": [
            {
                "from_status": s.get("from_status"),
                "to_status": s["to_status"],
                "operator": s.get("operator"),
                "note": s.get("note"),
                "created_at": s["created_at"],
            }
            for s in status_history
        ],
        "revoke_context": {
            "revoke_operator": revoke_ctx.get("revoke_operator"),
            "revoke_comment": revoke_ctx.get("revoke_comment"),
            "revoke_time": revoke_ctx.get("created_at"),
            "restore_note": revoke_ctx.get("restore_note"),
        } if revoke_ctx else None,
    }

    package["package_hash"] = _calculate_package_hash(package)
    return package


def _serialize_snapshot(snap: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not snap:
        return None
    return {
        "id": snap["id"],
        "snapshot_name": snap["snapshot_name"],
        "is_active": snap["is_active"],
        "rules_config_path": snap["rules_config_path"],
        "rules_sha256": snap["rules_sha256"],
        "rules_yaml": snap["rules_yaml"],
        "rule_count": snap["rule_count"],
        "enabled_rule_count": snap["enabled_rule_count"],
        "summary": snap["summary"],
        "operator": snap.get("operator"),
        "reason": snap.get("reason"),
        "created_at": snap["created_at"],
        "superseded_by": snap.get("superseded_by"),
    }


def _generate_todo_actions(
    status: BatchStatus,
    batch_id: str,
    has_error: bool,
    last_approval: Optional[Dict[str, Any]],
    last_rejection: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    todos = []
    st = status

    if st == BatchStatus.CREATED:
        todos.append({
            "priority": "high",
            "action": "check",
            "description": "执行规则校验",
            "command": f"patchgate check {batch_id}",
        })
    elif st == BatchStatus.CHECKING:
        todos.append({
            "priority": "high",
            "action": "check",
            "description": "重新执行规则校验（上一次校验被中断）",
            "command": f"patchgate check {batch_id}",
        })
    elif st == BatchStatus.CHECK_FAILED:
        todos.append({
            "priority": "high",
            "action": "fix_and_check",
            "description": "修复清单问题后重新校验",
            "command": f"patchgate check {batch_id}",
        })
        if has_error:
            todos.append({
                "priority": "medium",
                "action": "reject",
                "description": "如无法修复可选择驳回",
                "command": f"patchgate reject {batch_id} --approver <姓名> --comment <原因>",
            })
    elif st == BatchStatus.CHECK_PASSED:
        todos.append({
            "priority": "high",
            "action": "approve",
            "description": "审批通过批次",
            "command": f"patchgate approve {batch_id} --approver <姓名>",
        })
        todos.append({
            "priority": "medium",
            "action": "reject",
            "description": "驳回审批",
            "command": f"patchgate reject {batch_id} --approver <姓名> --comment <原因>",
        })
    elif st == BatchStatus.REJECTED:
        todos.append({
            "priority": "high",
            "action": "fix_and_check",
            "description": f"已被驳回（{last_rejection['approver'] if last_rejection else 'unknown'}），修复后重新校验",
            "command": f"patchgate check {batch_id}",
        })
        if last_rejection and last_rejection.get("comment"):
            todos.append({
                "priority": "info",
                "action": "review_rejection",
                "description": f"驳回原因: {last_rejection['comment']}",
                "command": None,
            })
    elif st == BatchStatus.APPROVED:
        todos.append({
            "priority": "high",
            "action": "publish",
            "description": f"已通过审批（{last_approval['approver'] if last_approval else 'unknown'}），可标记发布",
            "command": f"patchgate publish {batch_id} --operator <姓名>",
        })
        todos.append({
            "priority": "medium",
            "action": "recheck",
            "description": "如需修改可重新校验",
            "command": f"patchgate check {batch_id}",
        })
    elif st == BatchStatus.PUBLISHED:
        todos.append({
            "priority": "info",
            "action": "review",
            "description": "已发布，可查看历史或撤销",
            "command": f"patchgate history {batch_id}",
        })
        todos.append({
            "priority": "low",
            "action": "revoke",
            "description": "如发现问题可撤销发布",
            "command": f"patchgate revoke {batch_id} --operator <姓名> --comment <原因>",
        })
    elif st == BatchStatus.REVOKED:
        todos.append({
            "priority": "high",
            "action": "check_status",
            "description": "查看撤销后状态",
            "command": f"patchgate status {batch_id}",
        })

    todos.append({
        "priority": "info",
        "action": "view_status",
        "description": "查看完整状态详情",
        "command": f"patchgate status {batch_id}",
    })
    todos.append({
        "priority": "info",
        "action": "view_history",
        "description": "查看完整历史记录",
        "command": f"patchgate history {batch_id}",
    })

    return todos


def _build_log_index(
    status_history: List[Dict[str, Any]],
    approvals: List[Dict[str, Any]],
    publish_records: List[Dict[str, Any]],
    snapshot_decisions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "status_transitions": len(status_history),
        "approvals": len(approvals),
        "publish_records": len(publish_records),
        "snapshot_decisions": len(snapshot_decisions),
        "latest_entries": {
            "status": status_history[-1] if status_history else None,
            "approval": approvals[0] if approvals else None,
            "publish": publish_records[0] if publish_records else None,
            "snapshot_decision": snapshot_decisions[-1] if snapshot_decisions else None,
        },
    }


def _calculate_package_hash(package: Dict[str, Any]) -> str:
    data = {k: v for k, v in package.items() if k != "package_hash"}
    content = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def validate_handover_package(package: Dict[str, Any]) -> Tuple[bool, List[str]]:
    errors = []

    if package.get("schema_version") != HANDOVER_SCHEMA_VERSION:
        errors.append(
            f"不支持的 schema 版本: {package.get('schema_version')}, 期望: {HANDOVER_SCHEMA_VERSION}"
        )

    required_fields = ["batch", "latest_validation", "approval_conclusion",
                       "rule_snapshots", "todo_actions", "log_index"]
    for field in required_fields:
        if field not in package:
            errors.append(f"缺少必填字段: {field}")

    if "batch" in package:
        batch = package["batch"]
        for f in ["id", "name", "status", "manifest_hash", "items"]:
            if f not in batch:
                errors.append(f"batch 缺少必填字段: {f}")

    if "rule_snapshots" in package and package["rule_snapshots"].get("active"):
        active = package["rule_snapshots"]["active"]
        for f in ["rules_sha256", "rules_yaml", "summary"]:
            if f not in active:
                errors.append(f"rule_snapshots.active 缺少必填字段: {f}")

    if "package_hash" in package:
        expected = _calculate_package_hash(package)
        if package["package_hash"] != expected:
            errors.append("包完整性校验失败，哈希不匹配")

    return len(errors) == 0, errors


def detect_import_conflicts(
    storage: Storage,
    package: Dict[str, Any],
) -> List[Dict[str, Any]]:
    conflicts = []
    batch_id = package["batch"]["id"]

    existing = storage.get_batch(batch_id)
    if existing:
        pkg_updated = package["batch"]["updated_at"]
        local_updated = existing["updated_at"]

        if local_updated > pkg_updated:
            conflicts.append({
                "type": "newer_local",
                "severity": "high",
                "description": "本地记录比导入包更新",
                "details": {
                    "local_updated_at": local_updated,
                    "package_updated_at": pkg_updated,
                    "local_status": existing["status"],
                    "package_status": package["batch"]["status"],
                },
                "resolution_options": ["keep_local", "overwrite_with_package"],
            })
        else:
            conflicts.append({
                "type": "duplicate_id",
                "severity": "medium",
                "description": "批次 ID 已存在",
                "details": {
                    "local_updated_at": local_updated,
                    "package_updated_at": pkg_updated,
                    "local_status": existing["status"],
                    "package_status": package["batch"]["status"],
                },
                "resolution_options": ["keep_local", "overwrite_with_package", "rename_package"],
            })

    active_snap = package["rule_snapshots"].get("active")
    if active_snap:
        pkg_sha = active_snap["rules_sha256"]
        default_path = active_snap["rules_config_path"]

        if os.path.exists(default_path):
            engine = RuleEngine(default_path)
            local_sha = engine.get_rules_sha256()
            if local_sha != pkg_sha:
                snapshot_engine = create_engine_from_snapshot(active_snap)
                diff = diff_rules(snapshot_engine, engine)
                conflicts.append({
                    "type": "rules_changed",
                    "severity": "medium",
                    "description": "本地规则文件与包内快照不一致",
                    "details": {
                        "package_rules_sha256": pkg_sha,
                        "local_rules_sha256": local_sha,
                        "diff_summary": diff["summary"],
                        "risk_level": diff["risk_level"],
                        "rules_path": default_path,
                    },
                    "resolution_options": ["keep_package_snapshot", "switch_to_local_rules"],
                })
        else:
            conflicts.append({
                "type": "rules_missing",
                "severity": "medium",
                "description": "包内引用的规则文件在本地不存在",
                "details": {
                    "rules_path": default_path,
                    "package_rules_sha256": pkg_sha,
                },
                "resolution_options": ["use_package_snapshot", "specify_rules_path"],
            })

    existing_import = storage.get_handover_import_by_hash(package["package_hash"])
    if existing_import:
        conflicts.append({
            "type": "duplicate_import",
            "severity": "low",
            "description": "该接手包已导入过",
            "details": {
                "previous_import_at": existing_import["imported_at"],
                "previous_imported_by": existing_import.get("imported_by", "unknown"),
                "previous_resolution": existing_import.get("resolution_summary"),
            },
            "resolution_options": ["skip", "force_reimport"],
        })

    return conflicts


def import_handover_package(
    storage: Storage,
    package: Dict[str, Any],
    resolutions: Dict[str, str],
    imported_by: str = "unknown",
    import_note: Optional[str] = None,
) -> Dict[str, Any]:
    is_valid, errors = validate_handover_package(package)
    if not is_valid:
        raise ValueError(f"接手包验证失败: {'; '.join(errors)}")

    batch_id = package["batch"]["id"]
    conflicts = detect_import_conflicts(storage, package)

    resolution_summary = []
    for conflict in conflicts:
        ctype = conflict["type"]
        resolution = resolutions.get(ctype)
        if not resolution:
            raise ValueError(f"冲突类型 {ctype} 未提供解决方案")
        if resolution not in conflict["resolution_options"]:
            raise ValueError(
                f"冲突类型 {ctype} 的解决方案 {resolution} 无效，可用选项: {conflict['resolution_options']}"
            )
        resolution_summary.append({
            "conflict_type": ctype,
            "resolution": resolution,
            "details": conflict["details"],
        })

    if any(r["resolution"] == "keep_local" for r in resolution_summary):
        return {
            "success": True,
            "action": "skipped",
            "batch_id": batch_id,
            "reason": "选择保留本地版本，跳过导入",
            "resolution_summary": resolution_summary,
        }

    if any(r["resolution"] == "skip" for r in resolution_summary):
        return {
            "success": True,
            "action": "skipped",
            "batch_id": batch_id,
            "reason": "该包已导入过，选择跳过",
            "resolution_summary": resolution_summary,
        }

    new_batch_id = batch_id
    if any(r["resolution"] == "rename_package" for r in resolution_summary):
        new_batch_id = f"{batch_id}-imported-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    if any(r["resolution"] == "overwrite_with_package" for r in resolution_summary):
        storage.delete_batch(batch_id)

    batch_data = package["batch"]
    items = []
    for it in batch_data["items"]:
        item = {
            "package_name": it["package_name"],
            "version": it.get("version"),
            "source_path": it.get("source_path"),
            "checksum": it.get("checksum"),
            "metadata": it.get("metadata", {}),
        }
        items.append(item)

    batch = storage.create_batch(
        batch_id=new_batch_id,
        name=batch_data["name"],
        description=batch_data.get("description"),
        manifest_path=batch_data["manifest_path"],
        manifest_hash=batch_data["manifest_hash"],
        items=items,
    )

    target_status = BatchStatus(batch_data["status"])
    current_status = BatchStatus(batch["status"])
    if current_status != target_status:
        storage.force_set_status(new_batch_id, target_status, "handover-import",
                                 f"从接手包导入，设置状态为 {target_status.value}")

    for snap_data in package["rule_snapshots"]["all"]:
        snap_id = storage.add_rule_snapshot(
            batch_id=new_batch_id,
            snapshot_name=snap_data["snapshot_name"],
            rules_config_path=snap_data["rules_config_path"],
            rules_sha256=snap_data["rules_sha256"],
            rules_yaml=snap_data["rules_yaml"],
            rule_count=snap_data["rule_count"],
            enabled_rule_count=snap_data["enabled_rule_count"],
            summary=snap_data["summary"],
            operator=snap_data.get("operator", "handover-import"),
            reason=snap_data.get("reason", "从接手包导入"),
        )
        if not snap_data["is_active"]:
            storage.supersede_rule_snapshot(
                new_batch_id, snap_id, snap_data.get("superseded_by", snap_id),
                "handover-import", "从接手包导入快照状态"
            )

    for sd in package["rule_snapshots"]["decisions"]:
        storage.add_snapshot_decision(
            batch_id=new_batch_id,
            decision=sd["decision"],
            old_snapshot_id=sd.get("old_snapshot_id"),
            new_snapshot_id=sd.get("new_snapshot_id"),
            diff_summary=sd.get("diff_summary"),
            risk_level=sd.get("risk_level"),
            operator=sd.get("operator", "handover-import"),
            note=sd.get("note"),
        )

    for st in package["status_history"]:
        storage.add_status_history_entry(
            batch_id=new_batch_id,
            from_status=st.get("from_status"),
            to_status=st["to_status"],
            operator=st.get("operator", "handover-import"),
            note=st.get("note"),
            created_at=st["created_at"],
        )

    for a in package["approval_conclusion"]["all_approvals"]:
        storage.add_approval(
            batch_id=new_batch_id,
            approver=a["approver"],
            decision=ApprovalDecision(a["decision"]),
            comment=a.get("comment"),
        )

    for p in package["publish_history"]:
        storage.add_publish_record(
            batch_id=new_batch_id,
            operator=p["operator"],
            action=p["action"],
            comment=p.get("comment"),
        )

    for f in package["latest_validation"]["failures"]:
        item_id = None
        if f.get("item_index") is not None:
            item = storage.get_item_by_index(new_batch_id, f["item_index"])
            if item:
                item_id = item["id"]
        storage.add_check_result(
            batch_id=new_batch_id,
            item_id=item_id,
            rule_id=f["rule_id"],
            rule_name=f["rule_name"],
            status=CheckResultStatus.FAILED,
            severity=f["severity"],
            message=f["message"],
            details=f.get("details"),
        )

    import_id = storage.add_handover_import_record(
        batch_id=new_batch_id,
        package_hash=package["package_hash"],
        package_generated_at=package["generated_at"],
        package_generated_by=package.get("generated_by", "unknown"),
        imported_by=imported_by,
        import_note=import_note,
        resolution_summary=json.dumps(resolution_summary, ensure_ascii=False),
        original_batch_id=batch_id,
        package_note=package.get("note"),
    )

    storage.add_status_history_entry(
        batch_id=new_batch_id,
        from_status=None,
        to_status=batch_data["status"],
        operator="handover-import",
        note=f"从接手包导入 (import_id={import_id}, 原 ID={batch_id})",
    )

    return {
        "success": True,
        "action": "imported",
        "batch_id": new_batch_id,
        "original_batch_id": batch_id,
        "import_id": import_id,
        "resolution_summary": resolution_summary,
    }


def export_handover_to_file(
    storage: Storage,
    batch_id: str,
    output_path: str,
    exporter: str = "unknown",
    note: Optional[str] = None,
) -> str:
    package = build_handover_package(storage, batch_id, exporter, note)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(package, f, ensure_ascii=False, indent=2)
    return output_path


def load_handover_from_file(file_path: str) -> Dict[str, Any]:
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"接手包文件不存在: {file_path}")
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)
