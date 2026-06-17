import os
import re
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

import yaml

from .models import CheckResultStatus
from .storage import Storage


RuleFn = Callable[[Any, Dict[str, Any], Storage, str], List[Dict[str, Any]]]


class RuleEngine:
    def __init__(self, rules_config_path: Optional[str] = None):
        if rules_config_path is None:
            rules_config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "config",
                "rules.yaml",
            )
        self.rules_config_path = rules_config_path
        self.rules = self._load_rules()
        self._handlers = self._build_handlers()

    def _load_rules(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.rules_config_path):
            raise FileNotFoundError(f"规则配置文件不存在: {self.rules_config_path}")
        with open(self.rules_config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict) or "rules" not in data:
            raise ValueError("规则配置文件格式错误，必须包含 rules 顶层键")
        return data["rules"]

    def _build_handlers(self) -> Dict[str, RuleFn]:
        return {
            "duplicate_package_name": _check_duplicate_package_name,
            "package_name_required": _check_package_name_required,
            "version_format": _check_version_format,
            "checksum_required": _check_checksum_required,
            "source_path_exists": _check_source_path_exists,
            "batch_size_limit": _check_batch_size_limit,
        }

    def get_enabled_rules(self) -> List[Dict[str, Any]]:
        return [r for r in self.rules if r.get("enabled", True)]

    def run_checks(self, batch_id: str, storage: Storage) -> Dict[str, Any]:
        batch = storage.get_batch(batch_id)
        if not batch:
            raise ValueError(f"批次 {batch_id} 不存在")

        storage.clear_check_results(batch_id)
        enabled_rules = self.get_enabled_rules()

        batch_rules = [r for r in enabled_rules if r.get("scope") == "batch"]
        item_rules = [r for r in enabled_rules if r.get("scope") == "item"]

        results: List[Dict[str, Any]] = []

        for rule in batch_rules:
            handler = self._handlers.get(rule["id"])
            if not handler:
                continue
            try:
                rule_results = handler(batch, rule, storage, batch_id)
            except Exception as e:
                rule_results = [
                    _mk_result(
                        batch_id=batch_id,
                        item_id=None,
                        rule=rule,
                        status=CheckResultStatus.FAILED,
                        message=f"规则执行异常: {e}",
                        details={"error": str(e)},
                    )
                ]
            results.extend(rule_results)

        for item in batch["items"]:
            for rule in item_rules:
                handler = self._handlers.get(rule["id"])
                if not handler:
                    continue
                try:
                    rule_results = handler(item, rule, storage, batch_id)
                except Exception as e:
                    rule_results = [
                        _mk_result(
                            batch_id=batch_id,
                            item_id=item["id"],
                            rule=rule,
                            status=CheckResultStatus.FAILED,
                            message=f"规则执行异常: {e}",
                            details={"error": str(e)},
                        )
                    ]
                results.extend(rule_results)

        for r in results:
            r_copy = dict(r)
            r_copy["status"] = CheckResultStatus(r_copy["status"])
            storage.add_check_result(**r_copy)

        has_error = any(
            r["status"] == CheckResultStatus.FAILED.value and r["severity"] == "error"
            for r in results
        )

        return {
            "total_rules": len(enabled_rules),
            "total_checks": len(results),
            "passed": sum(1 for r in results if r["status"] == CheckResultStatus.PASSED.value),
            "failed": sum(1 for r in results if r["status"] == CheckResultStatus.FAILED.value),
            "warnings": sum(
                1
                for r in results
                if r["status"] == CheckResultStatus.FAILED.value and r["severity"] == "warning"
            ),
            "errors": sum(
                1
                for r in results
                if r["status"] == CheckResultStatus.FAILED.value and r["severity"] == "error"
            ),
            "skipped": sum(1 for r in results if r["status"] == CheckResultStatus.SKIPPED.value),
            "has_error": has_error,
            "results": results,
        }


def _mk_result(
    batch_id: str,
    item_id: Optional[int],
    rule: Dict[str, Any],
    status: CheckResultStatus,
    message: str,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "batch_id": batch_id,
        "item_id": item_id,
        "rule_id": rule["id"],
        "rule_name": rule["name"],
        "status": status.value,
        "severity": rule.get("severity", "warning"),
        "message": message,
        "details": details,
    }


def _check_duplicate_package_name(
    batch: Dict[str, Any], rule: Dict[str, Any], storage: Storage, batch_id: str
) -> List[Dict[str, Any]]:
    items = batch.get("items", [])
    name_indexes: Dict[str, List[int]] = defaultdict(list)
    for idx, item in enumerate(items):
        name = (item.get("package_name") or "").strip()
        if name:
            name_indexes[name].append(idx)

    duplicates = {k: v for k, v in name_indexes.items() if len(v) > 1}

    if not duplicates:
        return [
            _mk_result(
                batch_id=batch_id,
                item_id=None,
                rule=rule,
                status=CheckResultStatus.PASSED,
                message="所有包名唯一，无重复",
            )
        ]

    results: List[Dict[str, Any]] = []
    for pkg_name, indexes in duplicates.items():
        dup_items = [items[i] for i in indexes]
        item_ids = [it["id"] for it in dup_items]
        detail = {
            "package_name": pkg_name,
            "duplicate_count": len(indexes),
            "duplicate_indexes": indexes,
            "duplicate_versions": [it.get("version") for it in dup_items],
        }
        for iid, idx in zip(item_ids, indexes):
            results.append(
                _mk_result(
                    batch_id=batch_id,
                    item_id=iid,
                    rule=rule,
                    status=CheckResultStatus.FAILED,
                    message=(
                        f"包名重复: '{pkg_name}' 在第 {[i + 1 for i in indexes]} 行重复出现"
                        f" (共 {len(indexes)} 处)"
                    ),
                    details=detail,
                )
            )
    return results


def _check_package_name_required(
    item: Dict[str, Any], rule: Dict[str, Any], storage: Storage, batch_id: str
) -> List[Dict[str, Any]]:
    name = (item.get("package_name") or "").strip()
    if name:
        return [
            _mk_result(
                batch_id=batch_id,
                item_id=item["id"],
                rule=rule,
                status=CheckResultStatus.PASSED,
                message=f"包名存在: {name}",
            )
        ]
    return [
        _mk_result(
            batch_id=batch_id,
            item_id=item["id"],
            rule=rule,
            status=CheckResultStatus.FAILED,
            message=f"第 {item['item_index'] + 1} 条清单缺少 package_name 字段或为空",
            details={"item_index": item["item_index"]},
        )
    ]


def _check_version_format(
    item: Dict[str, Any], rule: Dict[str, Any], storage: Storage, batch_id: str
) -> List[Dict[str, Any]]:
    version = item.get("version")
    if not version:
        return [
            _mk_result(
                batch_id=batch_id,
                item_id=item["id"],
                rule=rule,
                status=CheckResultStatus.SKIPPED,
                message="版本号为空，跳过格式检查",
            )
        ]
    params = rule.get("params", {})
    pattern = params.get("pattern", r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
    if re.match(pattern, str(version)):
        return [
            _mk_result(
                batch_id=batch_id,
                item_id=item["id"],
                rule=rule,
                status=CheckResultStatus.PASSED,
                message=f"版本号格式合法: {version}",
            )
        ]
    return [
        _mk_result(
            batch_id=batch_id,
            item_id=item["id"],
            rule=rule,
            status=CheckResultStatus.FAILED,
            message=f"版本号 '{version}' 不符合语义化版本格式 (x.y.z)",
            details={"version": version, "pattern": pattern},
        )
    ]


def _check_checksum_required(
    item: Dict[str, Any], rule: Dict[str, Any], storage: Storage, batch_id: str
) -> List[Dict[str, Any]]:
    checksum = (item.get("checksum") or "").strip()
    if checksum:
        return [
            _mk_result(
                batch_id=batch_id,
                item_id=item["id"],
                rule=rule,
                status=CheckResultStatus.PASSED,
                message=f"校验和存在 (长度: {len(checksum)})",
            )
        ]
    return [
        _mk_result(
            batch_id=batch_id,
            item_id=item["id"],
            rule=rule,
            status=CheckResultStatus.FAILED,
            message=f"包 '{item.get('package_name', '(无名)')}' 缺少 checksum 校验和",
            details={"package_name": item.get("package_name")},
        )
    ]


def _check_source_path_exists(
    item: Dict[str, Any], rule: Dict[str, Any], storage: Storage, batch_id: str
) -> List[Dict[str, Any]]:
    src = item.get("source_path")
    if not src:
        return [
            _mk_result(
                batch_id=batch_id,
                item_id=item["id"],
                rule=rule,
                status=CheckResultStatus.SKIPPED,
                message="source_path 为空，跳过存在性检查",
            )
        ]
    if os.path.exists(src):
        return [
            _mk_result(
                batch_id=batch_id,
                item_id=item["id"],
                rule=rule,
                status=CheckResultStatus.PASSED,
                message=f"源路径存在: {src}",
            )
        ]
    return [
        _mk_result(
            batch_id=batch_id,
            item_id=item["id"],
            rule=rule,
            status=CheckResultStatus.FAILED,
            message=f"源路径不存在: {src}",
            details={"source_path": src, "package_name": item.get("package_name")},
        )
    ]


def _check_batch_size_limit(
    batch: Dict[str, Any], rule: Dict[str, Any], storage: Storage, batch_id: str
) -> List[Dict[str, Any]]:
    params = rule.get("params", {})
    max_items = int(params.get("max_items", 100))
    count = len(batch.get("items", []))
    if count <= max_items:
        return [
            _mk_result(
                batch_id=batch_id,
                item_id=None,
                rule=rule,
                status=CheckResultStatus.PASSED,
                message=f"批次规模 {count} 件，未超过上限 {max_items}",
            )
        ]
    return [
        _mk_result(
            batch_id=batch_id,
            item_id=None,
            rule=rule,
            status=CheckResultStatus.FAILED,
            message=f"批次规模 {count} 件，超过上限 {max_items}",
            details={"count": count, "max_items": max_items},
        )
    ]
