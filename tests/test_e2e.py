#!/usr/bin/env python3
"""patchgate 端到端测试脚本 - 覆盖所有核心链路"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from patchgate.cli import main as cli_main
from patchgate.storage import Storage


TEST_DB = os.path.join(os.path.dirname(__file__), ".test_patchgate", "test.db")


def run_cli(*args):
    print(f"\n$ patchgate {' '.join(args)}")
    rc = cli_main(["--db", TEST_DB, "--no-color"] + list(args))
    print(f"[退出码: {rc}]")
    return rc


def separator(title):
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}")


def cleanup():
    d = os.path.dirname(TEST_DB)
    if os.path.exists(d):
        shutil.rmtree(d)
    os.makedirs(d, exist_ok=True)
    examples_dir = Path(_PROJECT_ROOT) / "examples"
    config_dir = Path(_PROJECT_ROOT) / "config"
    return examples_dir, config_dir


def test_normal_pipeline(examples_dir):
    """测试一: 正常发布全链路"""
    separator("测试一: 正常发布全链路")

    mid = f"{examples_dir}/manifest_good.json"
    rc = run_cli("import", mid, "--id", "test-normal-01", "--name", "测试-正常批次")
    assert rc == 0, "import 失败"

    rc = run_cli("check", "test-normal-01")
    # 源路径不存在所以只有 warning，exit code 应该是 0
    assert rc == 0, f"check 失败 (预期只有 warning)，实际 rc={rc}"

    rc = run_cli("approve", "test-normal-01", "--approver", "tester1", "--comment", "正常审批")
    assert rc == 0, "approve 失败"

    rc = run_cli("publish", "test-normal-01", "--operator", "operator1", "--comment", "窗口发布完成")
    assert rc == 0, "publish 失败"

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8") as tf:
        out_path = tf.name
    try:
        rc = run_cli("export", "test-normal-01", "-o", out_path, "-f", "json")
        assert rc == 0, "export 失败"
        with open(out_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["batch_id"] == "test-normal-01"
        assert data["status"] == "published"
        assert data["item_count"] == 5
        assert len(data["approvals"]) == 1
        assert data["approvals"][0]["approver"] == "tester1"
        assert len(data["publish_history"]) == 1
        assert data["publish_history"][0]["action"] == "publish"
        print("[OK] 导出 JSON 内容验证通过")
    finally:
        os.unlink(out_path)

    print("\n[OK][OK] 测试一通过: 正常发布全链路 [OK][OK]")


def test_failure_pipeline(examples_dir, config_dir):
    """测试二: 失败链路 - check 阶段错误、审批被阻塞、驳回"""
    separator("测试二: 失败链路（check 阶段错误 + 审批阻塞 + 驳回）")

    mid = f"{examples_dir}/manifest_check_stage_errors.json"
    rules_path = f"{config_dir}/rules_test_checksum_error.yaml"
    rc = run_cli("import", mid, "--id", "test-error-01", "--name", "测试-check阶段错误")
    assert rc == 0, "import 失败（清单无重复包名，应通过预检）"

    rc = run_cli("check", "test-error-01", "--rules", rules_path)
    assert rc == 2, f"check 应返回退出码 2 (有 error)，实际 {rc}"

    rc = run_cli("approve", "test-error-01", "--approver", "tester2", "--comment", "不应通过")
    assert rc != 0, "存在未解决失败项时 approve 应被拒绝"

    rc = run_cli("reject", "test-error-01", "--approver", "tester2", "--comment", "缺少校验和，需补充后重新发布")
    assert rc == 0, "reject 失败"

    storage = Storage(TEST_DB)
    st = storage.get_current_status("test-error-01")
    assert st.value == "rejected", f"状态应为 rejected，实际 {st}"
    print("[OK] 状态已更新为 rejected")

    approvals = storage.get_approvals("test-error-01")
    assert len(approvals) == 1
    assert approvals[0]["decision"] == "reject"
    assert "校验和" in approvals[0]["comment"]
    print("[OK] 驳回审批记录已落盘")

    checks = storage.get_check_results("test-error-01")
    cs_errors = [c for c in checks if c["rule_id"] == "checksum_required" and c["severity"] == "error"]
    print(f"[OK] checksum_required 检测到 {len(cs_errors)} 条 error 记录（check 阶段正常工作）")
    assert len(cs_errors) >= 3, "3 个缺少 checksum 的条目应有对应错误"

    print("\n[OK][OK] 测试二通过: 失败链路完整 [OK][OK]")


def test_revoke_and_publish_again():
    """测试三: 发布 → 撤销回退 → 重新发布"""
    separator("测试三: 发布 → 撤销回退 → 重新发布")

    storage = Storage(TEST_DB)

    rc = run_cli("revoke", "test-normal-01", "--operator", "operator1", "--comment", "发现问题紧急回滚")
    assert rc == 0, "revoke 失败"

    st = storage.get_current_status("test-normal-01")
    assert st.value == "approved", f"撤销后应自动回退到 approved，实际 {st}"
    print(f"[OK] 撤销后状态: {st} (正确)")

    pubs = storage.get_publish_records("test-normal-01")
    assert len(pubs) == 2
    assert pubs[0]["action"] == "revoke"
    assert "发现问题" in pubs[0]["comment"]
    print(f"[OK] 撤销/回退记录已落盘: 操作人={pubs[0]['operator']}, 备注={pubs[0]['comment']}")

    sh = storage.get_status_history("test-normal-01")
    published_to_revoked = any(
        h["from_status"] == "published" and h["to_status"] == "revoked" for h in sh
    )
    revoked_to_approved = any(
        h["from_status"] == "revoked" and h["to_status"] == "approved" for h in sh
    )
    assert published_to_revoked and revoked_to_approved, "状态流转历史不完整"
    print("[OK] 状态流转历史完整记录 (published→revoked→approved)")

    rc = run_cli("publish", "test-normal-01", "--operator", "operator2", "--comment", "修复后重新发布")
    assert rc == 0, "重新发布失败"
    st = storage.get_current_status("test-normal-01")
    assert st.value == "published", f"重新发布后状态应为 published，实际 {st}"
    print(f"[OK] 重新发布后状态: {st} (正确)")

    pubs = storage.get_publish_records("test-normal-01")
    assert len(pubs) == 3
    print(f"[OK] 发布历史共 {len(pubs)} 条: 发布 → 撤销 → 重新发布")

    print("\n[OK][OK] 测试三通过: 撤销回退与重新发布 [OK][OK]")


def test_resume_pipeline(examples_dir, config_dir):
    """测试四: 按批次续跑"""
    separator("测试四: 按批次续跑（resume 从 created 一键到 published）")

    mid = f"{examples_dir}/manifest_good.json"
    rc = run_cli("import", mid, "--id", "test-resume-01", "--name", "测试-续跑批次")
    assert rc == 0, "import 失败"

    rc = run_cli(
        "resume", "test-resume-01",
        "--to", "publish",
        "--approver", "auto-approver",
        "--operator", "auto-operator",
    )
    assert rc == 0, f"resume 一键发布失败, rc={rc}"

    storage = Storage(TEST_DB)
    st = storage.get_current_status("test-resume-01")
    assert st.value == "published", f"续跑后状态应为 published，实际 {st}"

    approvals = storage.get_approvals("test-resume-01")
    assert len(approvals) == 1
    assert approvals[0]["approver"] == "auto-approver"
    pubs = storage.get_publish_records("test-resume-01")
    assert len(pubs) == 1
    assert pubs[0]["operator"] == "auto-operator"
    print(f"[OK] resume 一键完成 check→approve→publish，审批人和发布人均已落盘")

    # 测试续跑 check_failed 会中断
    mid2 = f"{examples_dir}/manifest_check_stage_errors.json"
    rules_path = f"{config_dir}/rules_test_checksum_error.yaml"
    rc = run_cli("import", mid2, "--id", "test-resume-02", "--name", "测试-续跑失败中断")
    assert rc == 0, "import 失败（清单无重复，应通过预检）"
    rc = run_cli("resume", "test-resume-02", "--to", "approve", "--approver", "tester", "--rules", rules_path)
    assert rc == 2, "有未解决错误时 resume 到 approve 应中断"
    st = storage.get_current_status("test-resume-02")
    assert st.value == "check_failed", f"续跑中断后状态应为 check_failed，实际 {st}"
    print(f"[OK] 续跑中途遇到错误会正确中断，状态停留在 check_failed")

    print("\n[OK][OK] 测试四通过: 按批次续跑 [OK][OK]")


def test_persistence(examples_dir):
    """测试五: 持久化 - 重新打开后数据一致"""
    separator("测试五: 数据持久化验证（重新创建 Storage）")

    expected = {
        "test-normal-01": {
            "status": "published",
            "approvals": 1,
            "publish_records": 3,
            "name": "测试-正常批次",
        },
        "test-error-01": {
            "status": "rejected",
            "approvals": 1,
            "publish_records": 0,
        },
        "test-resume-01": {
            "status": "published",
            "approvals": 1,
            "publish_records": 1,
        },
        "test-resume-02": {
            "status": "check_failed",
            "approvals": 0,
            "publish_records": 0,
        },
    }

    storage2 = Storage(TEST_DB)
    for bid, exp in expected.items():
        batch = storage2.get_batch(bid)
        assert batch is not None, f"批次 {bid} 丢失！"
        actual_st = batch["status"]
        assert actual_st == exp["status"], (
            f"批次 {bid} 状态不一致: 期望 {exp['status']}, 实际 {actual_st}"
        )
        act_approvals = len(storage2.get_approvals(bid))
        assert act_approvals == exp["approvals"], (
            f"批次 {bid} 审批记录数量不一致: 期望 {exp['approvals']}, 实际 {act_approvals}"
        )
        act_pubs = len(storage2.get_publish_records(bid))
        assert act_pubs == exp["publish_records"], (
            f"批次 {bid} 发布记录数量不一致: 期望 {exp['publish_records']}, 实际 {act_pubs}"
        )
        print(f"[OK] {bid}: 状态={actual_st}, 审批记录={act_approvals}, 发布记录={act_pubs}  [OK]")

    # 验证审批人、回退备注等细节
    pubs_normal = storage2.get_publish_records("test-normal-01")
    revoke_record = next(p for p in pubs_normal if p["action"] == "revoke")
    assert "紧急回滚" in revoke_record["comment"], "撤销回退备注未持久化"
    print("[OK] 撤销回退备注持久化正确: " + revoke_record["comment"])

    # 导出摘要重新跑一次对比
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as tf:
        md_path = tf.name
    try:
        rc = run_cli("export", "test-normal-01", "-o", md_path, "-f", "markdown")
        assert rc == 0, "markdown 导出失败"
        with open(md_path, "r", encoding="utf-8") as f:
            md_content = f.read()
        assert "紧急回滚" in md_content, "Markdown 导出中缺失回退备注"
        assert "修复后重新发布" in md_content, "Markdown 导出中缺失重新发布备注"
        print("[OK] Markdown 摘要导出包含所有历史细节")
    finally:
        os.unlink(md_path)

    print("\n[OK][OK] 测试五通过: 数据持久化完整 [OK][OK]")


def test_status_history_and_list():
    """测试六: history / status / list 命令"""
    separator("测试六: history / status / list 命令")

    rc = run_cli("list")
    assert rc == 0, "list 失败"

    rc = run_cli("status", "test-normal-01")
    assert rc == 0, "status 失败"

    rc = run_cli("history")
    assert rc == 0, "history (全局列表) 失败"

    rc = run_cli("history", "test-normal-01", "--type", "all")
    assert rc == 0, "history --type all 失败"

    rc = run_cli("history", "test-normal-01", "-t", "approval")
    assert rc == 0, "history --type approval 失败"

    rc = run_cli("history", "test-normal-01", "-t", "publish")
    assert rc == 0, "history --type publish 失败"

    rc = run_cli("history", "test-normal-01", "-t", "status")
    assert rc == 0, "history --type status 失败"

    print("\n[OK][OK] 测试六通过: history/status/list 全部正常 [OK][OK]")


def test_illegal_transitions(examples_dir):
    """测试七: 非法状态流转被拒绝"""
    separator("测试七: 非法状态流转拦截")

    mid = f"{examples_dir}/manifest_good.json"
    run_cli("import", mid, "--id", "test-illegal-01", "--name", "测试-非法流转")

    # created 状态不能直接 approve
    rc = run_cli("approve", "test-illegal-01", "--approver", "tester")
    assert rc != 0, "created → approved 应被拒绝"
    print("[OK] created → approved 被正确拦截")

    # created 状态不能直接 publish
    rc = run_cli("publish", "test-illegal-01", "--operator", "op")
    assert rc != 0, "created → published 应被拒绝"
    print("[OK] created → published 被正确拦截")

    # 先 check + approve
    run_cli("check", "test-illegal-01")
    run_cli("approve", "test-illegal-01", "--approver", "tester")

    # approved 状态不能 revoke（只有 published 可以 revoke）
    rc = run_cli("revoke", "test-illegal-01", "--operator", "op", "--comment", "xx")
    assert rc != 0, "approved → revoked 应被拒绝"
    print("[OK] approved → revoked 被正确拦截（仅 published 可撤销）")

    # 先 publish，再尝试重复 approve
    run_cli("publish", "test-illegal-01", "--operator", "op")
    rc = run_cli("approve", "test-illegal-01", "--approver", "tester2")
    assert rc != 0, "published 状态下 approve 应被拒绝"
    print("[OK] published 状态下 approve 被正确拦截")

    # published 状态下 check 被拒绝
    rc = run_cli("check", "test-illegal-01")
    assert rc != 0, "published 状态下 check 应被拒绝"
    print("[OK] published 状态下 check 被正确拦截")

    print("\n[OK][OK] 测试七通过: 非法状态流转全部被拦截 [OK][OK]")


def test_import_prevalidation_no_residue(examples_dir):
    """回归测试 1: 错误清单导入失败且数据库里没有新批次、没有残留检查结果/发布记录"""
    separator("回归测试 1: import 预检失败无任何数据残留")

    storage = Storage(TEST_DB)
    batches_before = storage.list_batches()
    count_before = len(batches_before)
    print(f"[INFO] 测试前数据库已有 {count_before} 个批次")

    mid = f"{examples_dir}/manifest_with_errors.json"
    rc = run_cli("import", mid, "--id", "should-not-exist-01", "--name", "不应存在的批次")
    assert rc == 2, f"预检失败应返回退出码 2，实际 {rc}"
    print("[OK] import 预检失败，退出码 = 2（正确）")

    rc_list = run_cli("list")
    print(f"[INFO] list 命令返回码: {rc_list}")

    storage2 = Storage(TEST_DB)
    batches_after = storage2.list_batches()
    count_after = len(batches_after)
    print(f"[INFO] 测试后数据库有 {count_after} 个批次")

    batch_ids_after = {b["id"] for b in batches_after}
    assert "should-not-exist-01" not in batch_ids_after, "预检失败的批次 ID 不应存在于数据库"
    assert count_after == count_before, f"批次数量不应变化，之前 {count_before}，之后 {count_after}"
    print("[OK] 数据库批次数量未变，没有新批次残留")

    import sqlite3
    conn = sqlite3.connect(TEST_DB)
    cur = conn.cursor()
    tables = ["batches", "manifest_items", "check_results", "approvals", "publish_records", "status_history"]
    for tbl in tables:
        if tbl == "batches":
            cur.execute(f"SELECT COUNT(*) FROM {tbl} WHERE id = 'should-not-exist-01'")
        else:
            cur.execute(f"SELECT COUNT(*) FROM {tbl} WHERE batch_id = 'should-not-exist-01'")
        cnt = cur.fetchone()[0]
        assert cnt == 0, f"表 {tbl} 中不应有批次 should-not-exist-01 的数据，实际有 {cnt} 条"
    conn.close()
    print("[OK] 所有表中均无残留批次数据 (batches/items/checks/approvals/publish/history)")

    print("\n[OK][OK] 回归测试 1 通过: 预检失败无任何残留 [OK][OK]")


def test_import_prevalidation_no_pollution(examples_dir):
    """回归测试 2: 已有正常发布数据不受 import 预检失败污染"""
    separator("回归测试 2: import 预检失败不污染已有正常发布数据")

    storage = Storage(TEST_DB)
    mid = f"{examples_dir}/manifest_good.json"
    rc = run_cli("import", mid, "--id", "preserve-01", "--name", "保护批次")
    assert rc == 0, "保护批次 import 失败"
    rc = run_cli("check", "preserve-01")
    assert rc == 0, "保护批次 check 失败"
    rc = run_cli("approve", "preserve-01", "--approver", "guardian", "--comment", "保护数据完整性")
    assert rc == 0, "保护批次 approve 失败"
    rc = run_cli("publish", "preserve-01", "--operator", "publisher", "--comment", "正式发布")
    assert rc == 0, "保护批次 publish 失败"
    print("[OK] 保护批次已创建并发布完成")

    batch_before = storage.get_batch("preserve-01")
    checks_before = storage.get_check_results("preserve-01")
    approvals_before = storage.get_approvals("preserve-01")
    publish_before = storage.get_publish_records("preserve-01")
    assert batch_before is not None
    assert len(approvals_before) == 1
    assert len(publish_before) == 1
    print("[OK] 保护数据快照已记录")

    print("\n[INFO] 现在尝试 import 错误清单（预检失败）...")
    mid_err = f"{examples_dir}/manifest_with_errors.json"
    rc = run_cli("import", mid_err, "--id", "intruder-01", "--name", "入侵者")
    assert rc == 2, "预检失败应返回退出码 2"
    print("[OK] 错误清单预检失败，符合预期")

    storage2 = Storage(TEST_DB)
    batch_after = storage2.get_batch("preserve-01")
    checks_after = storage2.get_check_results("preserve-01")
    approvals_after = storage2.get_approvals("preserve-01")
    publish_after = storage2.get_publish_records("preserve-01")
    intruder = storage2.get_batch("intruder-01")

    assert intruder is None, "入侵者批次不应存在"
    assert batch_after is not None
    assert batch_after["status"] == "published"
    assert len(approvals_after) == 1
    assert approvals_after[0]["approver"] == "guardian"
    assert approvals_after[0]["decision"] == "approve"
    assert len(publish_after) == 1
    assert publish_after[0]["operator"] == "publisher"
    assert len(checks_after) == len(checks_before)
    print("[OK] 保护批次的所有数据完整未变：")
    print(f"    - 状态: {batch_after['status']}")
    print(f"    - 审批人: {approvals_after[0]['approver']}")
    print(f"    - 发布人: {publish_after[0]['operator']}")
    print(f"    - 检查结果数: {len(checks_after)}")
    print(f"    - 入侵者批次: 不存在（正确）")

    print("\n[OK][OK] 回归测试 2 通过: 已有正常发布数据未受污染 [OK][OK]")


def main():
    examples_dir, config_dir = cleanup()
    all_passed = True
    tests = [
        ("正常发布全链路", lambda: test_normal_pipeline(examples_dir)),
        ("失败链路", lambda: test_failure_pipeline(examples_dir, config_dir)),
        ("撤销回退与重新发布", test_revoke_and_publish_again),
        ("按批次续跑", lambda: test_resume_pipeline(examples_dir, config_dir)),
        ("持久化验证", lambda: test_persistence(examples_dir)),
        ("history/status/list", test_status_history_and_list),
        ("非法状态流转拦截", lambda: test_illegal_transitions(examples_dir)),
        ("回归-预检失败无残留", lambda: test_import_prevalidation_no_residue(examples_dir)),
        ("回归-正常数据不被污染", lambda: test_import_prevalidation_no_pollution(examples_dir)),
    ]
    results = []
    for name, fn in tests:
        try:
            fn()
            results.append((name, "PASS", None))
        except AssertionError as e:
            results.append((name, "FAIL", str(e)))
            all_passed = False
            print(f"\n[断言失败] {name}: {e}")
        except Exception as e:
            results.append((name, "ERROR", f"{type(e).__name__}: {e}"))
            all_passed = False
            import traceback
            traceback.print_exc()

    separator("测试总览")
    print(f"{'测试名称':<30} {'结果':<8} 备注")
    print("-" * 70)
    for name, res, note in results:
        marker = "[OK]" if res == "PASS" else ("[X]" if res == "FAIL" else "!")
        print(f"{marker} {name:<28} {res:<8} {note or ''}")
    print("-" * 70)
    passed = sum(1 for _, r, _ in results if r == "PASS")
    total = len(results)
    print(f"\n通过率: {passed}/{total}")
    if all_passed:
        print("[!!!] 所有端到端测试通过！")
    else:
        print("[WARN] 部分测试失败，请查看上方详情")
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
