import argparse
import json
import os
import sys
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from tabulate import tabulate

from .manifest import load_manifest, validate_manifest_import, ManifestValidationError
from .models import (
    ApprovalDecision,
    BatchStatus,
    CheckResultStatus,
)
from .rules import RuleEngine, create_engine_from_snapshot, diff_rules, check_default_rules_vs_snapshot, get_default_rules_path
from .storage import DEFAULT_DB_PATH, Storage


STATUS_COLORS = {
    BatchStatus.CREATED: "\033[37m",
    BatchStatus.CHECKING: "\033[33m",
    BatchStatus.CHECK_FAILED: "\033[31m",
    BatchStatus.CHECK_PASSED: "\033[32m",
    BatchStatus.REJECTED: "\033[31m",
    BatchStatus.APPROVED: "\033[32m",
    BatchStatus.PUBLISHED: "\033[34m",
    BatchStatus.REVOKED: "\033[35m",
}
RESET = "\033[0m"


def colorize(text: str, status: BatchStatus) -> str:
    if os.environ.get("NO_COLOR"):
        return text
    color = STATUS_COLORS.get(status, "")
    return f"{color}{text}{RESET}" if color else text


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="patchgate",
        description="本地补丁发布闸门 CLI - 管理补丁清单导入、规则校验、审批和发布流程",
    )
    parser.add_argument(
        "--db", type=str, default=None, help=f"数据库路径 (默认: {DEFAULT_DB_PATH})"
    )
    parser.add_argument(
        "--no-color", action="store_true", help="禁用彩色输出"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # import
    p_import = sub.add_parser("import", help="导入发布清单创建新批次")
    p_import.add_argument("manifest", type=str, help="清单文件路径 (.json/.yaml/.csv)")
    p_import.add_argument("--name", "-n", type=str, default=None, help="批次名称")
    p_import.add_argument("--desc", "-d", type=str, default=None, help="批次描述")
    p_import.add_argument("--id", type=str, default=None, help="自定义批次 ID")

    # check
    p_check = sub.add_parser("check", help="对批次执行规则校验")
    p_check.add_argument("batch_id", type=str, help="批次 ID")
    p_check.add_argument(
        "--rules", "-r", type=str, default=None, help="自定义规则配置文件路径"
    )
    p_check.add_argument(
        "--continue-on-error", action="store_true", help="存在失败项时仍允许后续操作"
    )
    p_check.add_argument(
        "--force", "-f", action="store_true",
        help="强制使用指定规则并创建新快照（当规则与当前快照不同时需用此参数确认）"
    )

    # approve
    p_approve = sub.add_parser("approve", help="审批通过批次")
    p_approve.add_argument("batch_id", type=str, help="批次 ID")
    p_approve.add_argument("--approver", "-a", type=str, required=True, help="审批人姓名")
    p_approve.add_argument("--comment", "-c", type=str, default=None, help="审批备注")
    p_approve.add_argument(
        "--force", action="store_true", help="强制审批，忽略未解决的失败项"
    )

    # reject
    p_reject = sub.add_parser("reject", help="驳回审批")
    p_reject.add_argument("batch_id", type=str, help="批次 ID")
    p_reject.add_argument("--approver", "-a", type=str, required=True, help="审批人姓名")
    p_reject.add_argument("--comment", "-c", type=str, default=None, help="驳回原因 (必填)")

    # publish
    p_pub = sub.add_parser("publish", help="标记批次为已发布")
    p_pub.add_argument("batch_id", type=str, help="批次 ID")
    p_pub.add_argument("--operator", "-o", type=str, required=True, help="发布操作人")
    p_pub.add_argument("--comment", "-c", type=str, default=None, help="发布备注")

    # revoke
    p_rev = sub.add_parser("revoke", help="撤销发布，回退到审批通过状态")
    p_rev.add_argument("batch_id", type=str, help="批次 ID")
    p_rev.add_argument("--operator", "-o", type=str, required=True, help="撤销操作人")
    p_rev.add_argument("--comment", "-c", type=str, default=None, help="撤销/回退原因 (必填)")

    # export
    p_exp = sub.add_parser("export", help="导出发布摘要报告")
    p_exp.add_argument("batch_id", type=str, help="批次 ID")
    p_exp.add_argument("--output", "-o", type=str, default=None, help="输出文件路径 (默认 stdout)")
    p_exp.add_argument(
        "--format", "-f", type=str, choices=["json", "yaml", "markdown"],
        default="json", help="输出格式 (默认 json)"
    )

    # resume
    p_resume = sub.add_parser("resume", help="按批次续跑：从上次中断状态继续")
    p_resume.add_argument("batch_id", type=str, help="批次 ID")
    p_resume.add_argument(
        "--rules", "-r", type=str, default=None, help="自定义规则配置文件路径"
    )
    p_resume.add_argument(
        "--to", type=str,
        choices=["check", "approve", "publish"],
        default="check",
        help="续跑目标阶段 (默认 check)",
    )
    p_resume.add_argument("--approver", "-a", type=str, default=None, help="自动审批时的审批人")
    p_resume.add_argument("--operator", "-o", type=str, default=None, help="自动发布时的操作人")
    p_resume.add_argument(
        "--force", "-f", action="store_true",
        help="强制使用指定规则并创建新快照（当规则与当前快照不同时需用此参数确认）"
    )

    # history
    p_hist = sub.add_parser("history", help="查看批次历史（状态/审批/发布/规则快照）")
    p_hist.add_argument("batch_id", type=str, nargs="?", default=None, help="批次 ID (留空查看所有批次)")
    p_hist.add_argument(
        "--type", "-t", type=str,
        choices=["all", "status", "approval", "publish", "rules"],
        default="all", help="历史类型 (默认 all)",
    )

    # status
    p_stat = sub.add_parser("status", help="查看批次当前状态和检查摘要")
    p_stat.add_argument("batch_id", type=str, help="批次 ID")

    # list
    p_list = sub.add_parser("list", help="列出所有批次")

    return parser


def cmd_import(args, storage: Storage) -> int:
    manifest_path = os.path.abspath(args.manifest)
    items, manifest_hash = load_manifest(manifest_path)

    if not items:
        print("错误: 清单为空，至少需要一个条目", file=sys.stderr)
        return 1

    validation_err = validate_manifest_import(items)
    if validation_err is not None:
        _print_import_validation_errors(validation_err)
        return 2

    batch_id = args.id or f"batch-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
    name = args.name or os.path.basename(manifest_path)
    description = args.desc

    existing = storage.get_batch(batch_id)
    if existing:
        print(f"错误: 批次 ID '{batch_id}' 已存在，请使用 --id 指定其他值", file=sys.stderr)
        return 1

    batch = storage.create_batch(batch_id, name, description, manifest_path, manifest_hash, items)
    print("[OK] 批次创建成功")
    print(f"  批次 ID   : {batch_id}")
    print(f"  名称      : {name}")
    print(f"  清单文件  : {manifest_path}")
    print(f"  清单摘要  : {manifest_hash}")
    print(f"  条目数量  : {len(items)}")
    print(f"  当前状态  : {colorize(batch['status'], BatchStatus(batch['status']))}")
    print()
    _print_next_steps(BatchStatus.CREATED, batch_id, storage)
    return 0


def _print_import_validation_errors(err: ManifestValidationError) -> None:
    print("[FAIL] 清单预检失败，拒绝导入", file=sys.stderr)
    print(f"  共发现 {len(err.errors)} 项错误:", file=sys.stderr)
    print(file=sys.stderr)

    rows = []
    for e in err.errors:
        if e["type"] == "duplicate_package_name":
            rows.append([
                f"#{e['line']}",
                e.get("package_name", ""),
                "包名重复",
                f"同时出现在第 {e['duplicate_lines']} 行 (共 {e['duplicate_count']} 处, 版本: {e['duplicate_versions']})",
            ])
        elif e["type"] == "empty_package_name":
            rows.append([
                f"#{e['line']}",
                "(空)",
                "包名必填",
                "package_name 字段缺失或为空",
            ])
        else:
            rows.append([
                f"#{e['line']}",
                "",
                e["type"],
                e["message"],
            ])
    print(
        tabulate(rows, headers=["行号", "包名", "错误类型", "详细信息"],
                 tablefmt="grid", maxcolwidths=[6, 20, 12, 80]),
        file=sys.stderr,
    )
    print(file=sys.stderr)
    print("修复清单后请重新执行 import 命令", file=sys.stderr)


def cmd_check(args, storage: Storage) -> int:
    batch = storage.get_batch(args.batch_id)
    if not batch:
        print(f"错误: 批次 {args.batch_id} 不存在", file=sys.stderr)
        return 1

    current = BatchStatus(batch["status"])
    if current in (BatchStatus.PUBLISHED,):
        print(f"错误: 批次已发布，不能再执行校验。如需修改请先 revoke", file=sys.stderr)
        return 1

    active_snapshot = storage.get_active_rule_snapshot(args.batch_id)
    force_new_snapshot = getattr(args, "force_rules", False) or getattr(args, "force", False)

    if args.rules:
        new_engine = RuleEngine(args.rules)
        new_sha = new_engine.get_rules_sha256()

        if active_snapshot:
            if active_snapshot["rules_sha256"] == new_sha:
                engine = new_engine
                print(f"[i] 指定的规则文件与当前活动快照一致 (SHA256: {new_sha[:16]}...)")
            else:
                snapshot_engine = create_engine_from_snapshot(active_snapshot)
                diff = diff_rules(snapshot_engine, new_engine)
                print(f"[!] 检测到规则变更：{diff['summary']}")
                print(f"    风险等级: {diff['risk_level'].upper()}")
                print(f"    原快照: #{active_snapshot['id']} ({active_snapshot['snapshot_name']})")
                print(f"    新文件: {args.rules}")
                if diff["added"]:
                    print(f"    新增规则:")
                    for r in diff["added"]:
                        print(f"      + {r['id']}: {r['name']}")
                if diff["removed"]:
                    print(f"    删除规则:")
                    for r in diff["removed"]:
                        print(f"      - {r['id']}: {r['name']}")
                if diff["changed"]:
                    print(f"    修改规则:")
                    for r in diff["changed"]:
                        print(f"      ~ {r['id']}: {r['name']}")
                        for c in r["changes"]:
                            if c["field"] == "params":
                                for pc in c["changes"]:
                                    print(f"          params.{pc['param']}: {pc['old']} -> {pc['new']}")
                            else:
                                print(f"          {c['field']}: {c['old']} -> {c['new']}")

                if not force_new_snapshot:
                    print()
                    print("[!] 规则变更可能影响校验结果的可追溯性。")
                    print("    如需确认使用新规则并创建新快照，请添加 --force 参数。")
                    return 1

                print()
                print("[>] 使用新规则并创建新快照...")
                engine = new_engine
                snapshot_name = f"snapshot-{len(storage.get_rule_snapshots(args.batch_id)) + 1}"
                new_snap_id = storage.add_rule_snapshot(
                    batch_id=args.batch_id,
                    snapshot_name=snapshot_name,
                    rules_config_path=os.path.abspath(args.rules),
                    rules_sha256=new_sha,
                    rules_yaml=new_engine.get_rules_yaml_content(),
                    rule_count=new_engine.get_total_rule_count(),
                    enabled_rule_count=new_engine.get_enabled_rule_count(),
                    summary=new_engine.get_summary(),
                    operator="user",
                    reason=f"手动指定规则文件: {args.rules}",
                )
                storage.supersede_rule_snapshot(
                    args.batch_id, active_snapshot["id"], new_snap_id, "user",
                    f"被新快照 #{new_snap_id} 替代"
                )
                print(f"[OK] 已创建新规则快照 #{new_snap_id} ({snapshot_name})")
                print(f"    旧快照 #{active_snapshot['id']} 已标记为被覆盖")
        else:
            engine = new_engine
            snapshot_name = "snapshot-1"
            snap_id = storage.add_rule_snapshot(
                batch_id=args.batch_id,
                snapshot_name=snapshot_name,
                rules_config_path=os.path.abspath(args.rules),
                rules_sha256=new_sha,
                rules_yaml=new_engine.get_rules_yaml_content(),
                rule_count=new_engine.get_total_rule_count(),
                enabled_rule_count=new_engine.get_enabled_rule_count(),
                summary=new_engine.get_summary(),
                operator="user",
                reason=f"初始规则快照: {args.rules}",
            )
            print(f"[OK] 已创建初始规则快照 #{snap_id} ({snapshot_name})")
    else:
        if active_snapshot:
            engine = create_engine_from_snapshot(active_snapshot)
            consistency = check_default_rules_vs_snapshot(active_snapshot)
            print(f"[i] 沿用当前活动规则快照 #{active_snapshot['id']} ({active_snapshot['snapshot_name']})")
            print(f"    来源: {active_snapshot['rules_config_path']}")
            print(f"    SHA256: {active_snapshot['rules_sha256'][:16]}...")
            print(f"    创建时间: {active_snapshot['created_at']}")
            if not consistency["is_consistent"] and consistency["diff"]:
                diff = consistency["diff"]
                print()
                print(f"[!] 注意：当前默认规则文件 ({consistency['default_rules_path']}) 与本批次活动快照不一致。")
                print(f"    本次校验将继续沿用快照 #{active_snapshot['id']} 的规则，不受外部文件变更影响。")
                print(f"    差异概览: {diff['summary']} (风险: {diff['risk_level'].upper()})")
                print()
                print("    选项:")
                print(f"      · 继续沿用旧快照（本次默认行为，无需额外参数）")
                print(f"      · 切换到当前默认规则并创建新快照:  patchgate check {args.batch_id} --rules {consistency['default_rules_path']} --force")
                print(f"      · 查看快照详情:  patchgate history {args.batch_id} -t rules")
        else:
            engine = RuleEngine()
            snapshot_name = "snapshot-1"
            snap_id = storage.add_rule_snapshot(
                batch_id=args.batch_id,
                snapshot_name=snapshot_name,
                rules_config_path=os.path.abspath(engine.rules_config_path),
                rules_sha256=engine.get_rules_sha256(),
                rules_yaml=engine.get_rules_yaml_content(),
                rule_count=engine.get_total_rule_count(),
                enabled_rule_count=engine.get_enabled_rule_count(),
                summary=engine.get_summary(),
                operator="system",
                reason="首次 check 自动创建规则快照",
            )
            print(f"[OK] 已创建初始规则快照 #{snap_id} ({snapshot_name})")

    active_snapshot_id = storage.get_active_rule_snapshot(args.batch_id)
    active_snapshot_id = active_snapshot_id["id"] if active_snapshot_id else None
    snap_note = f" (规则快照 #{active_snapshot_id})" if active_snapshot_id else ""

    storage.transition_status(args.batch_id, BatchStatus.CHECKING, "system", f"开始规则校验{snap_note}")
    print(f"[>] 开始校验批次 {args.batch_id} ...")

    enabled = engine.get_enabled_rules()
    print(f"  启用规则数: {len(enabled)}")
    for r in enabled:
        sev = r.get("severity", "warning")
        sev_tag = f"[{sev.upper()}]"
        print(f"    - {r['id']}: {r['name']} {sev_tag}")
    print()

    result = engine.run_checks(args.batch_id, storage)

    has_error = result["has_error"]
    if has_error:
        storage.transition_status(
            args.batch_id, BatchStatus.CHECK_FAILED, "system",
            f"校验完成: {result['errors']} 个错误, {result['warnings']} 个警告{snap_note}"
        )
    else:
        storage.transition_status(
            args.batch_id, BatchStatus.CHECK_PASSED, "system",
            f"校验通过: {result['passed']} 项通过, {result['warnings']} 个警告{snap_note}"
        )

    _print_check_report(result, storage, args.batch_id, batch["items"])

    if has_error and not args.continue_on_error:
        print()
        print("[FAIL] 存在未解决的错误项，后续审批被阻塞。")
        print("  如需忽略请使用 --continue-on-error，或修复清单后重新执行 check。")
        _print_next_steps(BatchStatus.CHECK_FAILED, args.batch_id, storage)
        return 2

    if not has_error:
        print()
        print(f"[PASS] 校验通过，状态已更新为: {colorize('CHECK_PASSED', BatchStatus.CHECK_PASSED)}")
        _print_next_steps(BatchStatus.CHECK_PASSED, args.batch_id, storage)
    return 0


def _print_check_report(
    result: Dict[str, Any], storage: Storage, batch_id: str, items: List[Dict[str, Any]]
) -> None:
    results = result["results"]
    items_by_id = {it["id"]: it for it in items}

    print("═════════════════════════════════════════════════")
    print(f"  校验摘要: 共 {result['total_checks']} 项检查")
    print(f"    通过 : {result['passed']}   "
          f"失败 : {result['failed']}   "
          f"警告 : {result['warnings']}   "
          f"错误 : {result['errors']}   "
          f"跳过 : {result['skipped']}")
    print("═════════════════════════════════════════════════")

    failed = [r for r in results if r["status"] == CheckResultStatus.FAILED.value]
    if not failed:
        print("\n[PASS] 无失败项")
        return

    print("\n[FAIL] 失败项详情:")
    rows = []
    for r in failed:
        item = items_by_id.get(r["item_id"]) if r["item_id"] else None
        pkg_name = item["package_name"] if item else "(批次级)"
        idx = f"#{item['item_index'] + 1}" if item else "-"
        sev = r["severity"].upper()
        rows.append([
            idx,
            pkg_name,
            r["rule_name"],
            sev,
            r["message"],
        ])
    print(tabulate(rows, headers=["序号", "包名", "规则", "级别", "详细信息"], tablefmt="grid", maxcolwidths=[6, 20, 22, 6, 60]))

    dup_errors = [r for r in failed if r["rule_id"] == "duplicate_package_name" and r["severity"] == "error"]
    if dup_errors:
        print()
        print("[WARN] 包名重复明细:")
        printed = set()
        for r in dup_errors:
            det = r.get("details", {})
            key = det.get("package_name", "")
            if key in printed:
                continue
            printed.add(key)
            print(f"  - '{key}' 出现在行号: {det.get('duplicate_indexes', [])}  "
                  f"(重复 {det.get('duplicate_count', 0)} 次, "
                  f"版本: {det.get('duplicate_versions', [])})")


def cmd_approve(args, storage: Storage) -> int:
    batch = storage.get_batch(args.batch_id)
    if not batch:
        print(f"错误: 批次 {args.batch_id} 不存在", file=sys.stderr)
        return 1

    current = BatchStatus(batch["status"])
    if current not in (BatchStatus.CHECK_PASSED, BatchStatus.CHECK_FAILED, BatchStatus.REJECTED):
        print(f"错误: 当前状态 {current.value} 不允许审批，需要先通过 check", file=sys.stderr)
        return 1

    if current == BatchStatus.CHECK_FAILED:
        if not args.force:
            print(f"错误: 批次状态为 CHECK_FAILED，存在未解决的失败项。", file=sys.stderr)
            print("  请先修复问题并重新 check，或使用 --force 强制审批。", file=sys.stderr)
            failures = storage.get_check_results(args.batch_id, CheckResultStatus.FAILED)
            print(f"  当前未解决失败项共 {len(failures)} 个：", file=sys.stderr)
            for f in failures[:5]:
                print(f"    - [{f['severity']}] {f['rule_name']}: {f['message']}", file=sys.stderr)
            if len(failures) > 5:
                print(f"    ... 等共 {len(failures)} 项", file=sys.stderr)
            return 1

    has_error = storage.has_unresolved_failures(args.batch_id)
    if has_error and not args.force:
        print("错误: 仍有未解决的失败项 (error 级)，不能批准发布。", file=sys.stderr)
        print("  使用 --force 可强制审批（不推荐）", file=sys.stderr)
        return 1

    storage.add_approval(args.batch_id, args.approver, ApprovalDecision.APPROVE, args.comment)
    storage.transition_status(
        args.batch_id, BatchStatus.APPROVED,
        args.approver,
        args.comment or "审批通过"
    )
    print(f"[PASS] 审批通过")
    print(f"  批次 ID   : {args.batch_id}")
    print(f"  审批人     : {args.approver}")
    print(f"  审批备注   : {args.comment or '(无)'}")
    print(f"  当前状态   : {colorize('APPROVED', BatchStatus.APPROVED)}")
    print()
    _print_next_steps(BatchStatus.APPROVED, args.batch_id, storage)
    return 0


def cmd_reject(args, storage: Storage) -> int:
    batch = storage.get_batch(args.batch_id)
    if not batch:
        print(f"错误: 批次 {args.batch_id} 不存在", file=sys.stderr)
        return 1

    current = BatchStatus(batch["status"])
    if current not in (BatchStatus.CHECK_PASSED, BatchStatus.CHECK_FAILED):
        print(f"错误: 当前状态 {current.value} 不允许驳回，"
              f"只能在 CHECK_PASSED / CHECK_FAILED 状态下驳回", file=sys.stderr)
        return 1

    if not args.comment:
        print("错误: 驳回审批必须提供 --comment 原因", file=sys.stderr)
        return 1

    storage.add_approval(args.batch_id, args.approver, ApprovalDecision.REJECT, args.comment)
    storage.transition_status(
        args.batch_id, BatchStatus.REJECTED,
        args.approver,
        args.comment
    )
    print(f"[FAIL] 审批已驳回")
    print(f"  批次 ID   : {args.batch_id}")
    print(f"  审批人     : {args.approver}")
    print(f"  驳回原因   : {args.comment}")
    print(f"  当前状态   : {colorize('REJECTED', BatchStatus.REJECTED)}")
    print()
    _print_next_steps(BatchStatus.REJECTED, args.batch_id, storage)
    return 0


def cmd_publish(args, storage: Storage) -> int:
    batch = storage.get_batch(args.batch_id)
    if not batch:
        print(f"错误: 批次 {args.batch_id} 不存在", file=sys.stderr)
        return 1

    current = BatchStatus(batch["status"])
    if current != BatchStatus.APPROVED:
        print(f"错误: 当前状态 {current.value} 不允许发布，必须先审批通过 (APPROVED)", file=sys.stderr)
        return 1

    has_error = storage.has_unresolved_failures(args.batch_id)
    if has_error:
        print("警告: 批次中仍存在未解决的错误项，建议先 revoke 后处理", file=sys.stderr)

    storage.add_publish_record(args.batch_id, args.operator, "publish", args.comment)
    storage.transition_status(
        args.batch_id, BatchStatus.PUBLISHED,
        args.operator,
        args.comment or "发布完成"
    )
    print(f"[PASS] 批次已标记为发布")
    print(f"  批次 ID   : {args.batch_id}")
    print(f"  发布人     : {args.operator}")
    print(f"  发布时间   : {datetime.now().isoformat(timespec='seconds')}")
    print(f"  发布备注   : {args.comment or '(无)'}")
    print(f"  当前状态   : {colorize('PUBLISHED', BatchStatus.PUBLISHED)}")
    print()
    _print_next_steps(BatchStatus.PUBLISHED, args.batch_id, storage)
    print(f"  · 导出发布摘要: patchgate export {args.batch_id} -o summary.json")
    return 0


def cmd_revoke(args, storage: Storage) -> int:
    batch = storage.get_batch(args.batch_id)
    if not batch:
        print(f"错误: 批次 {args.batch_id} 不存在", file=sys.stderr)
        return 1

    current = BatchStatus(batch["status"])
    if current != BatchStatus.PUBLISHED:
        print(f"错误: 当前状态 {current.value} 不允许撤销，"
              f"只能在 PUBLISHED 状态下执行撤销回退", file=sys.stderr)
        return 1

    if not args.comment:
        print("错误: 撤销发布必须提供 --comment 回退原因/备注", file=sys.stderr)
        return 1

    storage.add_publish_record(args.batch_id, args.operator, "revoke", args.comment)
    storage.transition_status(
        args.batch_id, BatchStatus.REVOKED,
        args.operator,
        f"撤销发布: {args.comment}"
    )
    approvals = storage.get_approvals(args.batch_id)
    last_approval = next((a for a in approvals if a["decision"] == ApprovalDecision.APPROVE.value), None)
    if last_approval:
        storage.transition_status(
            args.batch_id, BatchStatus.APPROVED,
            args.operator,
            f"回退至审批通过状态 (原审批人: {last_approval['approver']})"
        )
        final_status = BatchStatus.APPROVED
    else:
        final_status = BatchStatus.REVOKED

    print(f"[PASS] 已撤销发布")
    print(f"  批次 ID   : {args.batch_id}")
    print(f"  操作人     : {args.operator}")
    print(f"  回退备注   : {args.comment}")
    print(f"  当前状态   : {colorize(final_status.value, final_status)}")
    if final_status == BatchStatus.APPROVED:
        active_snap = storage.get_active_rule_snapshot(args.batch_id)
        snap_info = ""
        if active_snap:
            snap_info = f" (沿用规则快照 #{active_snap['id']})"
        print(f"  (批次已恢复到审批通过状态{snap_info})")
        print()
        print(f"  状态流转路径:")
        print(f"    PUBLISHED  -(revoke)->  APPROVED")
        print(f"    APPROVED   -(check)->   CHECKING  -(通过)->  CHECK_PASSED  -(approve)->  APPROVED  -(publish)-> PUBLISHED")
        print(f"    APPROVED   -(publish)-> PUBLISHED  (不重跑校验，直接重发)")
        print()
        print(f"  后续操作:")
        print(f"    ① 无需修改直接发布:")
        print(f"       patchgate publish {args.batch_id} --operator <姓名>")
        print(f"    ② 修改清单后重跑校验 (沿用原批次规则快照，不受外部配置变更影响):")
        print(f"       patchgate check {args.batch_id}")
        print(f"       patchgate approve {args.batch_id} --approver <姓名>")
        print(f"       patchgate publish {args.batch_id} --operator <姓名>")
        print(f"    ③ 确认规则快照与校验结果:")
        print(f"       patchgate status {args.batch_id}")
        print(f"       patchgate history {args.batch_id} -t rules")
        print(f"       patchgate history {args.batch_id} -t status")
    print()
    _print_next_steps(final_status, batch_id=args.batch_id, storage=storage)
    return 0


def cmd_export(args, storage: Storage) -> int:
    batch = storage.get_batch(args.batch_id)
    if not batch:
        print(f"错误: 批次 {args.batch_id} 不存在", file=sys.stderr)
        return 1

    checks = storage.get_check_results(args.batch_id)
    approvals = storage.get_approvals(args.batch_id)
    publish_records = storage.get_publish_records(args.batch_id)
    status_history = storage.get_status_history(args.batch_id)
    rule_snapshots = storage.get_rule_snapshots(args.batch_id)
    active_snapshot = storage.get_active_rule_snapshot(args.batch_id)

    summary = {
        "batch_id": batch["id"],
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
            }
            for it in batch["items"]
        ],
        "rule_snapshot": None,
        "rule_snapshots": [],
        "check_summary": {
            "total": len(checks),
            "passed": sum(1 for c in checks if c["status"] == CheckResultStatus.PASSED.value),
            "failed": sum(1 for c in checks if c["status"] == CheckResultStatus.FAILED.value),
            "warnings": sum(1 for c in checks if c["status"] == CheckResultStatus.FAILED.value and c["severity"] == "warning"),
            "errors": sum(1 for c in checks if c["status"] == CheckResultStatus.FAILED.value and c["severity"] == "error"),
            "skipped": sum(1 for c in checks if c["status"] == CheckResultStatus.SKIPPED.value),
        },
        "check_failures": [
            {
                "package_name": next(
                    (it["package_name"] for it in batch["items"] if it["id"] == c["item_id"]),
                    "(批次级)",
                ),
                "rule": c["rule_name"],
                "rule_id": c["rule_id"],
                "severity": c["severity"],
                "message": c["message"],
                "details": c.get("details"),
            }
            for c in checks
            if c["status"] == CheckResultStatus.FAILED.value
        ],
        "approvals": [
            {
                "approver": a["approver"],
                "decision": a["decision"],
                "comment": a.get("comment"),
                "at": a["created_at"],
            }
            for a in approvals
        ],
        "publish_history": [
            {
                "operator": p["operator"],
                "action": p["action"],
                "comment": p.get("comment"),
                "at": p["created_at"],
            }
            for p in publish_records
        ],
        "status_history": [
            {
                "from": s.get("from_status"),
                "to": s["to_status"],
                "operator": s.get("operator"),
                "note": s.get("note"),
                "at": s["created_at"],
            }
            for s in status_history
        ],
        "exported_at": datetime.now().isoformat(timespec="seconds"),
    }

    if active_snapshot:
        consistency = check_default_rules_vs_snapshot(active_snapshot)
        summary["rule_snapshot"] = {
            "id": active_snapshot["id"],
            "name": active_snapshot["snapshot_name"],
            "is_active": active_snapshot["is_active"],
            "rules_config_path": active_snapshot["rules_config_path"],
            "rules_sha256": active_snapshot["rules_sha256"],
            "rule_count": active_snapshot["rule_count"],
            "enabled_rule_count": active_snapshot["enabled_rule_count"],
            "operator": active_snapshot.get("operator"),
            "reason": active_snapshot.get("reason"),
            "created_at": active_snapshot["created_at"],
            "superseded_by": active_snapshot.get("superseded_by"),
            "rules_summary": active_snapshot["summary"],
            "default_rules_consistency": {
                "default_rules_path": consistency["default_rules_path"],
                "default_exists": consistency["default_exists"],
                "is_consistent": consistency["is_consistent"],
                "default_rules_sha256": consistency.get("default_sha"),
                "diff_summary": consistency["diff"]["summary"] if consistency.get("diff") else None,
                "diff_risk_level": consistency["diff"]["risk_level"] if consistency.get("diff") else None,
            },
        }

    summary["rule_snapshots"] = [
        {
            "id": s["id"],
            "name": s["snapshot_name"],
            "is_active": s["is_active"],
            "rules_config_path": s["rules_config_path"],
            "rules_sha256": s["rules_sha256"],
            "rule_count": s["rule_count"],
            "enabled_rule_count": s["enabled_rule_count"],
            "operator": s.get("operator"),
            "reason": s.get("reason"),
            "created_at": s["created_at"],
            "superseded_by": s.get("superseded_by"),
            "rules_summary": s["summary"],
        }
        for s in rule_snapshots
    ]

    fmt = args.format
    if fmt == "json":
        content = json.dumps(summary, ensure_ascii=False, indent=2)
    elif fmt == "yaml":
        import yaml as _yaml
        content = _yaml.safe_dump(summary, allow_unicode=True, sort_keys=False)
    elif fmt == "markdown":
        content = _to_markdown(summary)
    else:
        content = json.dumps(summary, ensure_ascii=False, indent=2)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[PASS] 发布摘要已导出到: {os.path.abspath(args.output)}")
    else:
        print(content)
    return 0


def _to_markdown(s: Dict[str, Any]) -> str:
    lines = []
    lines.append(f"# 发布摘要 - {s['name']}")
    lines.append("")
    lines.append("## 基本信息")
    lines.append("")
    lines.append(f"- **批次 ID**: `{s['batch_id']}`")
    lines.append(f"- **状态**: `{s['status']}`")
    lines.append(f"- **清单文件**: `{s['manifest_path']}`")
    lines.append(f"- **清单摘要**: `{s['manifest_hash']}`")
    lines.append(f"- **条目数量**: {s['item_count']}")
    lines.append(f"- **创建时间**: {s['created_at']}")
    lines.append(f"- **更新时间**: {s['updated_at']}")
    if s.get("description"):
        lines.append(f"- **描述**: {s['description']}")
    lines.append("")

    if s.get("rule_snapshot"):
        rs = s["rule_snapshot"]
        lines.append("## 规则快照（当前活动）")
        lines.append("")
        lines.append(f"- **快照 ID**: #{rs['id']} ({rs['name']})")
        lines.append(f"- **来源文件**: `{rs['rules_config_path']}`")
        lines.append(f"- **SHA256**: `{rs['rules_sha256'][:16]}...`")
        lines.append(f"- **规则总数**: {rs['rule_count']} (启用 {rs['enabled_rule_count']})")
        lines.append(f"- **创建时间**: {rs['created_at']}")
        lines.append(f"- **操作人**: {rs.get('operator') or 'system'}")
        if rs.get("reason"):
            lines.append(f"- **备注**: {rs['reason']}")
        if rs.get("default_rules_consistency"):
            drc = rs["default_rules_consistency"]
            if drc["is_consistent"]:
                lines.append(f"- **与默认规则一致性**: 一致 (`{drc['default_rules_path']}`)")
            else:
                lines.append(f"- **与默认规则一致性**: [不一致]")
                lines.append(f"  - 默认规则文件: `{drc['default_rules_path']}`")
                if drc.get("diff_summary"):
                    lines.append(f"  - 差异概览: {drc['diff_summary']}")
                if drc.get("diff_risk_level"):
                    lines.append(f"  - 风险等级: {drc['diff_risk_level'].upper()}")
                lines.append(f"  - 说明: 本批次校验沿用快照 #{rs['id']} 的规则，不受外部默认规则文件变更影响。")
                lines.append(f"    如需切换到当前默认规则，执行: `patchgate check <BATCH_ID> --rules {drc['default_rules_path']} --force`")
        lines.append("")
        lines.append("### 关键校验项")
        lines.append("")
        lines.append("| 规则 ID | 规则名称 | 级别 | 状态 | 范围 |")
        lines.append("|---------|----------|------|------|------|")
        for rule in rs.get("rules_summary", []):
            status = "启用" if rule["enabled"] else "禁用"
            lines.append(
                f"| {rule['id']} | {rule['name']} | {rule['severity']} | "
                f"{status} | {rule['scope']} |"
            )
        lines.append("")

    if s.get("rule_snapshots") and len(s["rule_snapshots"]) > 1:
        lines.append("## 规则快照历史")
        lines.append("")
        lines.append("| # | 快照名 | 状态 | 操作人 | 规则数 | 启用数 | 创建时间 |")
        lines.append("|---|--------|------|--------|--------|--------|----------|")
        for snap in s["rule_snapshots"]:
            status = "活动" if snap["is_active"] else "已覆盖"
            lines.append(
                f"| {snap['id']} | {snap['name']} | {status} | "
                f"{snap.get('operator') or 'system'} | "
                f"{snap['rule_count']} | {snap['enabled_rule_count']} | "
                f"{snap['created_at']} |"
            )
        lines.append("")

    lines.append("## 清单内容")
    lines.append("")
    lines.append("| # | 包名 | 版本 | 源路径 | 校验和 |")
    lines.append("|---|------|------|--------|--------|")
    for it in s["items"]:
        lines.append(
            f"| {it['index'] + 1} | {it['package_name']} | "
            f"{it.get('version') or '-'} | "
            f"{it.get('source_path') or '-'} | "
            f"{(it.get('checksum') or '')[:16]}{'...' if it.get('checksum') and len(it['checksum']) > 16 else ''} |"
        )
    lines.append("")
    cs = s["check_summary"]
    lines.append("## 校验摘要")
    lines.append("")
    lines.append(f"- 总检查: {cs['total']}，通过: {cs['passed']}，"
                 f"失败: {cs['failed']}，错误: {cs['errors']}，"
                 f"警告: {cs['warnings']}，跳过: {cs['skipped']}")
    lines.append("")
    if s["check_failures"]:
        lines.append("### 失败明细")
        lines.append("")
        lines.append("| 包名 | 规则 | 级别 | 信息 |")
        lines.append("|------|------|------|------|")
        for f in s["check_failures"]:
            msg = f["message"].replace("|", "\\|")
            lines.append(f"| {f['package_name']} | {f['rule']} | {f['severity']} | {msg} |")
        lines.append("")
    lines.append("## 审批记录")
    lines.append("")
    if s["approvals"]:
        lines.append("| 审批人 | 决定 | 备注 | 时间 |")
        lines.append("|--------|------|------|------|")
        for a in s["approvals"]:
            lines.append(f"| {a['approver']} | {a['decision']} | {a.get('comment') or '-'} | {a['at']} |")
    else:
        lines.append("_暂无审批记录_")
    lines.append("")
    lines.append("## 发布与回退历史")
    lines.append("")
    if s["publish_history"]:
        lines.append("| 操作人 | 动作 | 备注 | 时间 |")
        lines.append("|--------|------|------|------|")
        for p in s["publish_history"]:
            lines.append(f"| {p['operator']} | {p['action']} | {p.get('comment') or '-'} | {p['at']} |")
    else:
        lines.append("_暂无发布记录_")
    lines.append("")
    lines.append("## 状态流转")
    lines.append("")
    lines.append("| 从 | 到 | 操作人 | 备注 | 时间 |")
    lines.append("|----|----|--------|------|------|")
    for st in s["status_history"]:
        lines.append(f"| {st.get('from') or '-'} | {st['to']} | {st.get('operator') or '-'} | "
                     f"{st.get('note') or '-'} | {st['at']} |")
    lines.append("")
    lines.append(f"_导出时间: {s['exported_at']}_")
    return "\n".join(lines)


def cmd_resume(args, storage: Storage) -> int:
    batch = storage.get_batch(args.batch_id)
    if not batch:
        print(f"错误: 批次 {args.batch_id} 不存在", file=sys.stderr)
        return 1

    current = BatchStatus(batch["status"])
    print(f"[>] 续跑批次 {args.batch_id}")
    print(f"  当前状态: {colorize(current.value, current)}")
    print(f"  目标阶段: {args.to}")
    print()

    active_snapshot = storage.get_active_rule_snapshot(args.batch_id)
    force_new_snapshot = getattr(args, "force", False)

    if args.rules:
        new_engine = RuleEngine(args.rules)
        new_sha = new_engine.get_rules_sha256()

        if active_snapshot:
            if active_snapshot["rules_sha256"] == new_sha:
                engine = new_engine
                print(f"[i] 指定的规则文件与当前活动快照一致 (SHA256: {new_sha[:16]}...)")
            else:
                snapshot_engine = create_engine_from_snapshot(active_snapshot)
                diff = diff_rules(snapshot_engine, new_engine)
                print(f"[!] 检测到规则变更：{diff['summary']}")
                print(f"    风险等级: {diff['risk_level'].upper()}")
                print(f"    原快照: #{active_snapshot['id']} ({active_snapshot['snapshot_name']})")
                print(f"    新文件: {args.rules}")
                if diff["added"]:
                    print(f"    新增规则:")
                    for r in diff["added"]:
                        print(f"      + {r['id']}: {r['name']}")
                if diff["removed"]:
                    print(f"    删除规则:")
                    for r in diff["removed"]:
                        print(f"      - {r['id']}: {r['name']}")
                if diff["changed"]:
                    print(f"    修改规则:")
                    for r in diff["changed"]:
                        print(f"      ~ {r['id']}: {r['name']}")
                        for c in r["changes"]:
                            if c["field"] == "params":
                                for pc in c["changes"]:
                                    print(f"          params.{pc['param']}: {pc['old']} -> {pc['new']}")
                            else:
                                print(f"          {c['field']}: {c['old']} -> {c['new']}")

                if not force_new_snapshot:
                    print()
                    print("[!] 规则变更可能影响校验结果的可追溯性。")
                    print("    如需确认使用新规则并创建新快照，请添加 --force 参数。")
                    return 1

                print()
                print("[>] 使用新规则并创建新快照...")
                engine = new_engine
                snapshot_name = f"snapshot-{len(storage.get_rule_snapshots(args.batch_id)) + 1}"
                new_snap_id = storage.add_rule_snapshot(
                    batch_id=args.batch_id,
                    snapshot_name=snapshot_name,
                    rules_config_path=os.path.abspath(args.rules),
                    rules_sha256=new_sha,
                    rules_yaml=new_engine.get_rules_yaml_content(),
                    rule_count=new_engine.get_total_rule_count(),
                    enabled_rule_count=new_engine.get_enabled_rule_count(),
                    summary=new_engine.get_summary(),
                    operator="resume",
                    reason=f"resume 更换规则文件: {args.rules}",
                )
                storage.supersede_rule_snapshot(
                    args.batch_id, active_snapshot["id"], new_snap_id, "resume",
                    f"被新快照 #{new_snap_id} 替代"
                )
                print(f"[OK] 已创建新规则快照 #{new_snap_id} ({snapshot_name})")
                print(f"    旧快照 #{active_snapshot['id']} 已标记为被覆盖")
        else:
            engine = new_engine
            snapshot_name = "snapshot-1"
            snap_id = storage.add_rule_snapshot(
                batch_id=args.batch_id,
                snapshot_name=snapshot_name,
                rules_config_path=os.path.abspath(args.rules),
                rules_sha256=new_sha,
                rules_yaml=new_engine.get_rules_yaml_content(),
                rule_count=new_engine.get_total_rule_count(),
                enabled_rule_count=new_engine.get_enabled_rule_count(),
                summary=new_engine.get_summary(),
                operator="resume",
                reason=f"resume 初始规则快照: {args.rules}",
            )
            print(f"[OK] 已创建初始规则快照 #{snap_id} ({snapshot_name})")
    else:
        if active_snapshot:
            engine = create_engine_from_snapshot(active_snapshot)
            consistency = check_default_rules_vs_snapshot(active_snapshot)
            print(f"[i] 沿用当前活动规则快照 #{active_snapshot['id']} ({active_snapshot['snapshot_name']})")
            print(f"    来源: {active_snapshot['rules_config_path']}")
            print(f"    SHA256: {active_snapshot['rules_sha256'][:16]}...")
            print(f"    创建时间: {active_snapshot['created_at']}")
            if not consistency["is_consistent"] and consistency["diff"]:
                diff = consistency["diff"]
                print()
                print(f"[!] 注意：当前默认规则文件 ({consistency['default_rules_path']}) 与本批次活动快照不一致。")
                print(f"    本次续跑将继续沿用快照 #{active_snapshot['id']} 的规则，不受外部文件变更影响。")
                print(f"    差异概览: {diff['summary']} (风险: {diff['risk_level'].upper()})")
                print()
                print("    选项:")
                print(f"      · 继续沿用旧快照（本次默认行为，无需额外参数）")
                print(f"      · 切换到当前默认规则并创建新快照:  patchgate resume {args.batch_id} --to {args.to} --rules {consistency['default_rules_path']} --force")
                print(f"      · 查看快照详情:  patchgate history {args.batch_id} -t rules")
        else:
            engine = RuleEngine()
            snapshot_name = "snapshot-1"
            snap_id = storage.add_rule_snapshot(
                batch_id=args.batch_id,
                snapshot_name=snapshot_name,
                rules_config_path=os.path.abspath(engine.rules_config_path),
                rules_sha256=engine.get_rules_sha256(),
                rules_yaml=engine.get_rules_yaml_content(),
                rule_count=engine.get_total_rule_count(),
                enabled_rule_count=engine.get_enabled_rule_count(),
                summary=engine.get_summary(),
                operator="resume",
                reason="resume 自动创建规则快照",
            )
            print(f"[OK] 已创建初始规则快照 #{snap_id} ({snapshot_name})")
    print()

    target_to_stage = {
        "check": [BatchStatus.CHECK_PASSED, BatchStatus.CHECK_FAILED],
        "approve": [BatchStatus.APPROVED, BatchStatus.REJECTED],
        "publish": [BatchStatus.PUBLISHED],
    }
    targets = target_to_stage[args.to]
    # --to check 时，即使用户已处于 check_passed/check_failed/approved，也认为用户想重新校验
    force_rerun_check = (args.to == "check" and current in (
        BatchStatus.CHECK_PASSED, BatchStatus.CHECK_FAILED,
        BatchStatus.APPROVED, BatchStatus.REJECTED, BatchStatus.REVOKED,
    ))
    if current in targets and not force_rerun_check:
        print(f"[PASS] 已处于目标阶段，无需续跑")
        return 0

    exit_code = 0
    stage = _current_stage(current)
    to_stage = args.to

    while stage != to_stage:
        rerun_check = force_rerun_check or stage in ("created", "check_failed", "rejected", "revoked", "checking")
        if rerun_check or (stage == "approved" and to_stage == "check"):
            if stage in ("approved", "check_passed", "check_failed", "rejected", "revoked"):
                print("→ 重新执行规则校验 ...")
            else:
                print("→ 执行规则校验 ...")
            storage.transition_status(args.batch_id, BatchStatus.CHECKING, "resume", "续跑-重新校验")
            result = engine.run_checks(args.batch_id, storage)
            _print_check_report(result, storage, args.batch_id, batch["items"])
            if result["has_error"]:
                storage.transition_status(args.batch_id, BatchStatus.CHECK_FAILED, "resume", "续跑-校验失败")
                print(f"\n[FAIL] 续跑中断：校验仍有错误，请修复后再 resume")
                return 2
            storage.transition_status(args.batch_id, BatchStatus.CHECK_PASSED, "resume", "续跑-校验通过")
            print(f"\n[PASS] 校验阶段完成")
            batch = storage.get_batch(args.batch_id)
            current = BatchStatus(batch["status"])
            stage = _current_stage(current)
            if to_stage == "check":
                break
            continue

        if stage in ("check_passed",) and to_stage in ("approve", "publish"):
            if not args.approver:
                print("[FAIL] 续跑至 approve/publish 需要 --approver 参数")
                return 1
            has_error = storage.has_unresolved_failures(args.batch_id)
            if has_error:
                print("[FAIL] 续跑中断：存在未解决的错误项，不能自动审批")
                return 2
            print(f"→ 自动审批 (审批人: {args.approver}) ...")
            storage.add_approval(args.batch_id, args.approver, ApprovalDecision.APPROVE, f"续跑自动审批 -> {to_stage}")
            storage.transition_status(args.batch_id, BatchStatus.APPROVED, args.approver, "续跑自动审批通过")
            print("[PASS] 审批阶段完成")
            batch = storage.get_batch(args.batch_id)
            current = BatchStatus(batch["status"])
            stage = _current_stage(current)
            if to_stage == "approve":
                break
            continue

        if stage == "approved" and to_stage == "publish":
            if not args.operator:
                print("[FAIL] 续跑至 publish 需要 --operator 参数")
                return 1
            print(f"→ 自动发布 (操作人: {args.operator}) ...")
            storage.add_publish_record(args.batch_id, args.operator, "publish", "续跑自动发布")
            storage.transition_status(args.batch_id, BatchStatus.PUBLISHED, args.operator, "续跑自动发布完成")
            print("[PASS] 发布阶段完成")
            batch = storage.get_batch(args.batch_id)
            current = BatchStatus(batch["status"])
            stage = _current_stage(current)
            break

        if stage in ("rejected", "check_failed", "published", "revoked"):
            print(f"[FAIL] 续跑中断：当前状态 {current.value} 需要人工介入")
            return 1

        break

    print()
    print(f"[PASS] 续跑完成，当前状态: {colorize(current.value, current)}")
    return exit_code


def _current_stage(s: BatchStatus) -> str:
    mapping = {
        BatchStatus.CREATED: "created",
        BatchStatus.CHECKING: "checking",
        BatchStatus.CHECK_FAILED: "check_failed",
        BatchStatus.CHECK_PASSED: "check_passed",
        BatchStatus.REJECTED: "rejected",
        BatchStatus.APPROVED: "approved",
        BatchStatus.PUBLISHED: "published",
        BatchStatus.REVOKED: "revoked",
    }
    return mapping.get(s, "created")


def cmd_history(args, storage: Storage) -> int:
    if not args.batch_id:
        batches = storage.list_batches()
        if not batches:
            print("(无批次记录)")
            return 0
        rows = []
        for b in batches:
            st = BatchStatus(b["status"])
            rows.append([
                b["id"][:16] + "..." if len(b["id"]) > 16 else b["id"],
                b["name"],
                colorize(b["status"], st),
                b["created_at"],
                b["updated_at"],
            ])
        print(tabulate(rows, headers=["批次 ID", "名称", "状态", "创建时间", "更新时间"], tablefmt="grid"))
        return 0

    batch = storage.get_batch(args.batch_id)
    if not batch:
        print(f"错误: 批次 {args.batch_id} 不存在", file=sys.stderr)
        return 1

    hist_type = args.type
    print(f"═════════ 批次 {args.batch_id} 历史 ═════════")
    print(f"  名称: {batch['name']}")
    print(f"  当前状态: {colorize(batch['status'], BatchStatus(batch['status']))}")
    print()

    if hist_type in ("all", "status"):
        print("── 状态流转 ──")
        history = storage.get_status_history(args.batch_id)
        rows = []
        for h in history:
            rows.append([h["id"], h.get("from_status") or "-", h["to_status"], h.get("operator") or "-", h.get("note") or "", h["created_at"]])
        print(tabulate(rows, headers=["#", "从", "到", "操作人", "备注", "时间"], tablefmt="simple"))
        print()

    if hist_type in ("all", "approval"):
        print("── 审批记录 ──")
        approvals = storage.get_approvals(args.batch_id)
        if approvals:
            rows = []
            for a in approvals:
                dec = "[PASS] 通过" if a["decision"] == "approve" else "[FAIL] 驳回"
                rows.append([a["id"], a["approver"], dec, a.get("comment") or "", a["created_at"]])
            print(tabulate(rows, headers=["#", "审批人", "决定", "备注", "时间"], tablefmt="simple"))
        else:
            print("(无)")
        print()

    if hist_type in ("all", "publish"):
        print("── 发布/回退记录 ──")
        pubs = storage.get_publish_records(args.batch_id)
        if pubs:
            rows = []
            for p in pubs:
                act = "发布" if p["action"] == "publish" else ("撤销/回退" if p["action"] == "revoke" else p["action"])
                rows.append([p["id"], p["operator"], act, p.get("comment") or "", p["created_at"]])
            print(tabulate(rows, headers=["#", "操作人", "动作", "备注", "时间"], tablefmt="simple"))
        else:
            print("(无)")
        print()

    if hist_type in ("all", "rules"):
        print("── 规则快照历史 ──")
        snapshots = storage.get_rule_snapshots(args.batch_id)
        if snapshots:
            rows = []
            for s in snapshots:
                status_tag = "活动" if s["is_active"] else "已覆盖"
                sup_by = f"#{s['superseded_by']}" if s.get("superseded_by") else "-"
                rows.append([
                    s["id"],
                    s["snapshot_name"],
                    status_tag,
                    sup_by,
                    s.get("operator") or "system",
                    s["rule_count"],
                    s["enabled_rule_count"],
                    s["created_at"],
                ])
            print(tabulate(
                rows,
                headers=["#", "快照名", "状态", "被替代", "操作人", "规则数", "启用数", "创建时间"],
                tablefmt="simple",
            ))
            print()
            print("  快照详情:")
            for s in snapshots:
                active_marker = " [活动]" if s["is_active"] else ""
                print(f"    #{s['id']} {s['snapshot_name']}{active_marker}")
                print(f"       来源: {s['rules_config_path']}")
                print(f"       SHA256: {s['rules_sha256'][:16]}...")
                if s.get("reason"):
                    print(f"       备注: {s['reason']}")
                if s["is_active"]:
                    consistency = check_default_rules_vs_snapshot(s)
                    if consistency["is_consistent"]:
                        print(f"       与默认规则: 一致 ({consistency['default_rules_path']})")
                    else:
                        diff = consistency["diff"]
                        print(f"       与默认规则: 不一致 (!)")
                        print(f"         默认文件: {consistency['default_rules_path']}")
                        if diff:
                            print(f"         差异: {diff['summary']} (风险: {diff['risk_level'].upper()})")
                            print(f"         如需切换: patchgate check {args.batch_id} --rules {consistency['default_rules_path']} --force")
                print(f"       关键校验项:")
                for rule in s["summary"]:
                    en = "启用" if rule["enabled"] else "禁用"
                    print(f"         - {rule['id']}: {rule['name']} "
                          f"[{rule['severity'].upper()}] [{en}]")
        else:
            print("(无规则快照，执行 check 后会自动创建)")
        print()

    checks = storage.get_check_results(args.batch_id, CheckResultStatus.FAILED)
    if checks and hist_type == "all":
        print("── 未解决失败项 ──")
        rows = []
        for c in checks:
            rows.append([c["rule_name"], c["severity"].upper(), c["message"][:80]])
        print(tabulate(rows, headers=["规则", "级别", "信息"], tablefmt="simple"))
    return 0


def cmd_status(args, storage: Storage) -> int:
    batch = storage.get_batch(args.batch_id)
    if not batch:
        print(f"错误: 批次 {args.batch_id} 不存在", file=sys.stderr)
        return 1
    st = BatchStatus(batch["status"])
    print(f"批次 {args.batch_id}")
    print(f"  名称       : {batch['name']}")
    print(f"  描述       : {batch.get('description') or '(无)'}")
    print(f"  清单文件   : {batch['manifest_path']}")
    print(f"  清单摘要   : {batch['manifest_hash']}")
    print(f"  条目数     : {len(batch['items'])}")
    print(f"  当前状态   : {colorize(st.value, st)}")
    print(f"  创建时间   : {batch['created_at']}")
    print(f"  更新时间   : {batch['updated_at']}")
    print()

    active_snapshot = storage.get_active_rule_snapshot(args.batch_id)
    if active_snapshot:
        consistency = check_default_rules_vs_snapshot(active_snapshot)
        print(f"规则快照 (当前活动): #{active_snapshot['id']} {active_snapshot['snapshot_name']}")
        print(f"  来源文件   : {active_snapshot['rules_config_path']}")
        print(f"  SHA256     : {active_snapshot['rules_sha256'][:16]}...")
        print(f"  规则总数   : {active_snapshot['rule_count']} (启用 {active_snapshot['enabled_rule_count']})")
        print(f"  创建时间   : {active_snapshot['created_at']}")
        print(f"  创建人     : {active_snapshot.get('operator') or 'system'}")
        if active_snapshot.get("reason"):
            print(f"  备注       : {active_snapshot['reason']}")
        all_snapshots = storage.get_rule_snapshots(args.batch_id)
        superseded = [s for s in all_snapshots if not s["is_active"]]
        if superseded:
            print(f"  历史快照   : {len(superseded)} 个已被覆盖")
        if consistency["is_consistent"]:
            print(f"  与默认规则 : 一致 ({consistency['default_rules_path']})")
        else:
            diff = consistency["diff"]
            print(f"  与默认规则 : 不一致 (!)")
            print(f"               默认文件: {consistency['default_rules_path']}")
            if diff:
                print(f"               差异: {diff['summary']} (风险: {diff['risk_level'].upper()})")
                print(f"               如需切换到默认规则: patchgate check {args.batch_id} --rules {consistency['default_rules_path']} --force")
        print()
        print(f"  关键校验项:")
        for rule in active_snapshot["summary"]:
            status_tag = "启用" if rule["enabled"] else "禁用"
            print(f"    - {rule['id']}: {rule['name']} "
                  f"[{rule['severity'].upper()}] [{status_tag}] "
                  f"({rule['scope']})")
        print()
    else:
        print("规则快照: 尚无 (执行 check 后会自动创建)")
        print()

    checks = storage.get_check_results(args.batch_id)
    if checks:
        passed = sum(1 for c in checks if c["status"] == CheckResultStatus.PASSED.value)
        failed = sum(1 for c in checks if c["status"] == CheckResultStatus.FAILED.value)
        warnings = sum(1 for c in checks if c["status"] == CheckResultStatus.FAILED.value and c["severity"] == "warning")
        errors = sum(1 for c in checks if c["status"] == CheckResultStatus.FAILED.value and c["severity"] == "error")
        skipped = sum(1 for c in checks if c["status"] == CheckResultStatus.SKIPPED.value)
        print(f"校验结果: 总数 {len(checks)}, 通过 {passed}, 失败 {failed} (错误 {errors}, 警告 {warnings}), 跳过 {skipped}")
        if failed > 0:
            for c in checks:
                if c["status"] == CheckResultStatus.FAILED.value:
                    item = next((it for it in batch["items"] if it["id"] == c["item_id"]), None)
                    pkg = item["package_name"] if item else "(批次级)"
                    print(f"  [{c['severity'].upper()}] {c['rule_name']} - {pkg}: {c['message']}")
        print()

    approvals = storage.get_approvals(args.batch_id)
    if approvals:
        print(f"审批记录:")
        for a in approvals:
            dec = "通过" if a["decision"] == "approve" else "驳回"
            print(f"  [{dec}] {a['approver']} @ {a['created_at']}: {a.get('comment') or '(无)'}")
        print()

    pubs = storage.get_publish_records(args.batch_id)
    if pubs:
        print(f"发布/回退记录:")
        for p in pubs:
            act = "发布" if p["action"] == "publish" else "撤销回退"
            print(f"  [{act}] {p['operator']} @ {p['created_at']}: {p.get('comment') or '(无)'}")
    print()
    _print_next_steps(st, args.batch_id, storage)
    return 0


def _print_next_steps(st: BatchStatus, batch_id: Optional[str] = None, storage: Optional[Storage] = None):
    mapping = {
        BatchStatus.CREATED: {
            "desc": "已导入但尚未校验",
            "steps": [
                "执行规则校验:  patchgate check <BATCH_ID>",
                "或一键续跑:    patchgate resume <BATCH_ID> --to check",
            ],
        },
        BatchStatus.CHECKING: {
            "desc": "校验中（应尽快完成 check 或重新 check 以避免锁定状态，或调用 history 查看结果）",
            "steps": [
                "重新执行校验:  patchgate check <BATCH_ID>",
                "查看校验详情:  patchgate history <BATCH_ID>",
            ],
        },
        BatchStatus.CHECK_FAILED: {
            "desc": "校验未通过，存在未解决失败项",
            "steps": [
                "修正清单后重跑:  patchgate check <BATCH_ID>",
                "或驳回处理:      patchgate reject <BATCH_ID>",
                "查看失败项:      patchgate status <BATCH_ID>",
            ],
        },
        BatchStatus.CHECK_PASSED: {
            "desc": "校验通过，等待审批",
            "steps": [
                "审批通过:  patchgate approve <BATCH_ID> --approver <姓名>",
                "驳回:      patchgate reject <BATCH_ID>",
            ],
        },
        BatchStatus.REJECTED: {
            "desc": "已驳回，可修改后重新校验",
            "steps": [
                "修改清单后重跑校验:  patchgate check <BATCH_ID>",
                "查看规则快照:        patchgate history <BATCH_ID> -t rules",
            ],
        },
        BatchStatus.APPROVED: {
            "desc": "审批通过，可直接发布或修改后重检（可能是首次审批通过，也可能是 revoke 撤销后回退到此）",
            "steps": [
                "① 无需修改直接发布:               patchgate publish <BATCH_ID> --operator <姓名>",
                "② 修改后重新校验 (沿用批次内规则快照):  patchgate check <BATCH_ID>",
                "   然后审批+发布:                  patchgate approve <BATCH_ID> --approver <姓名>",
                "                                   patchgate publish <BATCH_ID> --operator <姓名>",
                "③ 一键续跑至发布:                  patchgate resume <BATCH_ID> --to publish --operator <姓名>",
                "④ 确认当前规则快照与校验依据:       patchgate status <BATCH_ID>",
                "                                   patchgate history <BATCH_ID> -t rules",
            ],
        },
        BatchStatus.PUBLISHED: {
            "desc": "已发布",
            "steps": [
                "查看发布历史:   patchgate history <BATCH_ID>",
                "发现问题撤销:   patchgate revoke <BATCH_ID> --operator <姓名> --comment <原因>",
            ],
        },
        BatchStatus.REVOKED: {
            "desc": "已撤销发布（通常会自动回退到 APPROVED）",
            "steps": [
                "查看当前状态:       patchgate status <BATCH_ID>",
                "按 APPROVED 建议:   patchgate status <BATCH_ID>（revoke 后自动回退）",
            ],
        },
    }
    info = mapping.get(st)
    if not info:
        return
    print(f"下一步建议 ({info['desc']}):")
    snap_info = ""
    if batch_id and storage:
        snap = storage.get_active_rule_snapshot(batch_id)
        if snap:
            snap_info = f"  · 当前校验规则依据: 快照 #{snap['id']} ({snap['snapshot_name']}), SHA256: {snap['rules_sha256'][:16]}..."
            print(snap_info)
            consistency = check_default_rules_vs_snapshot(snap)
            if not consistency["is_consistent"] and consistency["diff"]:
                print(f"  · [!] 与默认规则文件不一致: {consistency['diff']['summary']} (风险: {consistency['diff']['risk_level'].upper()})")
                print(f"      沿用快照不变更，如需切换默认规则: patchgate check {batch_id} --rules {consistency['default_rules_path']} --force")
    for s in info['steps']:
        bid = batch_id or "<BATCH_ID>"
        print(f"  · {s.replace('<BATCH_ID>', bid)}")


def cmd_list(args, storage: Storage) -> int:
    batches = storage.list_batches()
    if not batches:
        print("(暂无批次，使用 import 命令创建)")
        return 0
    rows = []
    for b in batches:
        st = BatchStatus(b["status"])
        rows.append([
            b["id"],
            b["name"],
            colorize(b["status"], st),
            b.get("description") or "",
            b["created_at"],
        ])
    print(tabulate(rows, headers=["批次 ID", "名称", "状态", "描述", "创建时间"], tablefmt="grid", maxcolwidths=[32, 20, 14, 30, 20]))
    return 0


COMMAND_HANDLERS = {
    "import": cmd_import,
    "check": cmd_check,
    "approve": cmd_approve,
    "reject": cmd_reject,
    "publish": cmd_publish,
    "revoke": cmd_revoke,
    "export": cmd_export,
    "resume": cmd_resume,
    "history": cmd_history,
    "status": cmd_status,
    "list": cmd_list,
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = _make_parser()
    args = parser.parse_args(argv)

    if args.no_color:
        os.environ["NO_COLOR"] = "1"

    storage = Storage(args.db)

    handler = COMMAND_HANDLERS.get(args.command)
    if not handler:
        parser.print_help()
        return 1
    try:
        return handler(args, storage)
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except UnicodeEncodeError as e:
        print(f"错误: 终端编码不支持输出某些字符 ({e})，请设置 PYTHONIOENCODING=utf-8", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"错误: 未预期的异常 [{type(e).__name__}]: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
