import argparse
import json
import os
import sys
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from tabulate import tabulate

from .manifest import load_manifest
from .models import (
    ApprovalDecision,
    BatchStatus,
    CheckResultStatus,
)
from .rules import RuleEngine
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

    # history
    p_hist = sub.add_parser("history", help="查看批次历史（状态/审批/发布）")
    p_hist.add_argument("batch_id", type=str, nargs="?", default=None, help="批次 ID (留空查看所有批次)")
    p_hist.add_argument(
        "--type", "-t", type=str,
        choices=["all", "status", "approval", "publish"],
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
    print("下一步建议:")
    print(f"  patchgate check {batch_id}")
    return 0


def cmd_check(args, storage: Storage) -> int:
    batch = storage.get_batch(args.batch_id)
    if not batch:
        print(f"错误: 批次 {args.batch_id} 不存在", file=sys.stderr)
        return 1

    current = BatchStatus(batch["status"])
    if current in (BatchStatus.PUBLISHED,):
        print(f"错误: 批次已发布，不能再执行校验。如需修改请先 revoke", file=sys.stderr)
        return 1

    storage.transition_status(args.batch_id, BatchStatus.CHECKING, "system", "开始规则校验")
    print(f"[>] 开始校验批次 {args.batch_id} ...")

    engine = RuleEngine(args.rules)
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
            f"校验完成: {result['errors']} 个错误, {result['warnings']} 个警告"
        )
    else:
        storage.transition_status(
            args.batch_id, BatchStatus.CHECK_PASSED, "system",
            f"校验通过: {result['passed']} 项通过, {result['warnings']} 个警告"
        )

    _print_check_report(result, storage, args.batch_id, batch["items"])

    if has_error and not args.continue_on_error:
        print()
        print("[FAIL] 存在未解决的错误项，后续审批被阻塞。")
        print("  如需忽略请使用 --continue-on-error，或修复清单后重新执行 check。")
        return 2

    if not has_error:
        print()
        print(f"[PASS] 校验通过，状态已更新为: {colorize('CHECK_PASSED', BatchStatus.CHECK_PASSED)}")
        print(f"  下一步: patchgate approve {args.batch_id} --approver <姓名>")
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
    print(f"  下一步: patchgate publish {args.batch_id} --operator <姓名>")
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
    print(f"  修复后请重新执行: patchgate check {args.batch_id}")
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
    print(f"  导出发布摘要: patchgate export {args.batch_id} -o summary.json")
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
        print(f"  (批次已恢复到审批通过状态，可再次发布或修改)")
        print(f"  重新发布: patchgate publish {args.batch_id} --operator <姓名>")
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

    target_to_stage = {
        "check": [BatchStatus.CHECK_PASSED, BatchStatus.CHECK_FAILED],
        "approve": [BatchStatus.APPROVED, BatchStatus.REJECTED],
        "publish": [BatchStatus.PUBLISHED],
    }
    targets = target_to_stage[args.to]
    if current in targets:
        print(f"[PASS] 已处于目标阶段，无需续跑")
        return 0

    exit_code = 0
    stage = _current_stage(current)
    to_stage = args.to

    while stage != to_stage:
        if stage == "created" or (stage in ("check_failed", "rejected", "revoked", "checking") and to_stage in ("check", "approve", "publish")):
            print("→ 执行规则校验 ...")
            storage.transition_status(args.batch_id, BatchStatus.CHECKING, "resume", "续跑-重新校验")
            engine = RuleEngine(args.rules)
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
    return 0


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
