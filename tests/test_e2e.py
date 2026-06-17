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
from patchgate.models import BatchStatus
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


def test_rule_snapshot_basic(examples_dir):
    """回归测试 3: 规则快照基本功能 - check 后自动创建快照，status/history/export 可见"""
    separator("回归测试 3: 规则快照基本功能")

    mid = f"{examples_dir}/manifest_good.json"
    rc = run_cli("import", mid, "--id", "test-snapshot-basic", "--name", "测试-规则快照基础")
    assert rc == 0, "import 失败"

    storage = Storage(TEST_DB)
    assert not storage.has_rule_snapshot("test-snapshot-basic"), "check 前不应有规则快照"
    print("[OK] check 前无规则快照（符合预期）")

    rc = run_cli("check", "test-snapshot-basic")
    assert rc == 0, "check 失败"

    assert storage.has_rule_snapshot("test-snapshot-basic"), "check 后应有规则快照"
    active = storage.get_active_rule_snapshot("test-snapshot-basic")
    assert active is not None, "应有活动快照"
    assert active["is_active"] is True, "快照应处于活动状态"
    assert active["rule_count"] > 0, "规则数应大于 0"
    assert active["enabled_rule_count"] > 0, "启用规则数应大于 0"
    assert len(active["summary"]) == active["rule_count"], "摘要规则数应与总数一致"
    print(f"[OK] 规则快照已创建: #{active['id']} {active['snapshot_name']}")
    print(f"    规则总数: {active['rule_count']}, 启用: {active['enabled_rule_count']}")
    print(f"    SHA256: {active['rules_sha256'][:16]}...")

    snaps = storage.get_rule_snapshots("test-snapshot-basic")
    assert len(snaps) == 1, "应有 1 个快照"
    print("[OK] 快照列表查询正常")

    rc = run_cli("status", "test-snapshot-basic")
    assert rc == 0, "status 失败"
    print("[OK] status 命令正常输出（包含规则快照信息）")

    rc = run_cli("history", "test-snapshot-basic", "--type", "rules")
    assert rc == 0, "history --type rules 失败"
    print("[OK] history --type rules 正常输出")

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8") as tf:
        json_path = tf.name
    try:
        rc = run_cli("export", "test-snapshot-basic", "-o", json_path, "-f", "json")
        assert rc == 0, "export json 失败"
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert "rule_snapshot" in data, "JSON 导出应包含 rule_snapshot"
        assert data["rule_snapshot"] is not None, "rule_snapshot 不应为 null"
        assert data["rule_snapshot"]["id"] == active["id"], "导出的快照 ID 应匹配"
        assert "rules_summary" in data["rule_snapshot"], "应包含 rules_summary"
        assert "rule_snapshots" in data, "应包含 rule_snapshots 历史列表"
        assert len(data["rule_snapshots"]) == 1, "历史列表应有 1 条"
        print("[OK] JSON 导出包含规则快照信息且数据正确")
    finally:
        os.unlink(json_path)

    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as tf:
        md_path = tf.name
    try:
        rc = run_cli("export", "test-snapshot-basic", "-o", md_path, "-f", "markdown")
        assert rc == 0, "export markdown 失败"
        with open(md_path, "r", encoding="utf-8") as f:
            md = f.read()
        assert "规则快照" in md, "Markdown 应包含'规则快照'标题"
        assert "关键校验项" in md, "Markdown 应包含'关键校验项'小节"
        assert active["snapshot_name"] in md, "Markdown 应包含快照名"
        print("[OK] Markdown 导出包含规则快照信息")
    finally:
        os.unlink(md_path)

    print("\n[OK][OK] 回归测试 3 通过: 规则快照基本功能完整 [OK][OK]")


def test_rule_snapshot_persistence(examples_dir):
    """回归测试 4: 规则快照跨重启持久化 - 重新创建 Storage 后快照数据一致"""
    separator("回归测试 4: 规则快照跨重启持久化")

    storage1 = Storage(TEST_DB)
    snap_before = storage1.get_active_rule_snapshot("test-snapshot-basic")
    assert snap_before is not None, "测试前应有活动快照"
    snap_id_before = snap_before["id"]
    sha_before = snap_before["rules_sha256"]
    summary_before = snap_before["summary"]
    print(f"[INFO] 重启前快照: #{snap_id_before}, SHA256: {sha_before[:16]}...")

    storage2 = Storage(TEST_DB)
    snap_after = storage2.get_active_rule_snapshot("test-snapshot-basic")
    assert snap_after is not None, "重启后活动快照不应丢失"
    assert snap_after["id"] == snap_id_before, "快照 ID 应一致"
    assert snap_after["rules_sha256"] == sha_before, "SHA256 应一致"
    assert len(snap_after["summary"]) == len(summary_before), "规则摘要数量应一致"
    assert snap_after["is_active"] is True, "活动状态应保持"
    print(f"[OK] 重启后快照一致: #{snap_after['id']}, SHA256: {snap_after['rules_sha256'][:16]}...")

    all_snaps = storage2.get_rule_snapshots("test-snapshot-basic")
    assert len(all_snaps) == 1, "快照总数应保持 1"
    print("[OK] 快照列表持久化正常")

    snap_by_id = storage2.get_rule_snapshot_by_id(snap_id_before)
    assert snap_by_id is not None, "按 ID 查询应能找到快照"
    assert snap_by_id["rules_sha256"] == sha_before, "按 ID 查询的 SHA256 应一致"
    print("[OK] 按 ID 查询快照正常")

    print("\n[OK][OK] 回归测试 4 通过: 规则快照跨重启持久化 [OK][OK]")


def test_rule_snapshot_change(examples_dir, config_dir):
    """回归测试 5: 规则变更 - 换 rules 文件时的差异提示、风险告警、强制覆盖、旧快照标记"""
    separator("回归测试 5: 规则文件变更检测与快照覆盖")

    mid = f"{examples_dir}/manifest_good.json"
    rc = run_cli("import", mid, "--id", "test-snapshot-change", "--name", "测试-规则变更")
    assert rc == 0, "import 失败"

    rc = run_cli("check", "test-snapshot-change")
    assert rc == 0, "首次 check 失败"

    storage = Storage(TEST_DB)
    snap1 = storage.get_active_rule_snapshot("test-snapshot-change")
    assert snap1 is not None
    snap1_id = snap1["id"]
    print(f"[OK] 初始快照: #{snap1_id} ({snap1['snapshot_name']})")

    rc = run_cli("reject", "test-snapshot-change", "--approver", "tester",
                 "--comment", "驳回以便重新校验规则")
    assert rc == 0, "reject 失败"
    print("[OK] 已驳回，准备重新校验新规则")

    rules_path = f"{config_dir}/rules_test_checksum_error.yaml"

    rc = run_cli("check", "test-snapshot-change", "--rules", rules_path)
    assert rc != 0, "无 --force 时换规则应被拒绝"
    print("[OK] 无 --force 时换规则被正确拦截")

    snap_check = storage.get_active_rule_snapshot("test-snapshot-change")
    assert snap_check["id"] == snap1_id, "被拒绝后活动快照不应变"
    all_snaps = storage.get_rule_snapshots("test-snapshot-change")
    assert len(all_snaps) == 1, "被拒绝后不应新增快照"
    print("[OK] 被拒绝后快照状态保持不变")

    rc = run_cli("check", "test-snapshot-change", "--rules", rules_path, "--force")
    assert rc == 0, "加 --force 后换规则应成功"
    print("[OK] 加 --force 后规则变更成功")

    snap2 = storage.get_active_rule_snapshot("test-snapshot-change")
    assert snap2 is not None
    assert snap2["id"] != snap1_id, "新快照 ID 应不同"
    assert snap2["is_active"] is True, "新快照应是活动的"
    assert snap2["rules_sha256"] != snap1["rules_sha256"], "SHA256 应不同"
    print(f"[OK] 新活动快照: #{snap2['id']} ({snap2['snapshot_name']})")

    snap1_updated = storage.get_rule_snapshot_by_id(snap1_id)
    assert snap1_updated["is_active"] is False, "旧快照应被标记为非活动"
    assert snap1_updated["superseded_by"] == snap2["id"], "旧快照应记录被谁替代"
    print(f"[OK] 旧快照 #{snap1_id} 已标记为被 #{snap2['id']} 覆盖")

    all_snaps_final = storage.get_rule_snapshots("test-snapshot-change")
    assert len(all_snaps_final) == 2, "现在应有 2 个快照"
    active_count = sum(1 for s in all_snaps_final if s["is_active"])
    assert active_count == 1, "应只有 1 个活动快照"
    print(f"[OK] 共 {len(all_snaps_final)} 个快照，其中 {active_count} 个活动")

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8") as tf:
        json_path = tf.name
    try:
        rc = run_cli("export", "test-snapshot-change", "-o", json_path, "-f", "json")
        assert rc == 0, "export 失败"
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert len(data["rule_snapshots"]) == 2, "导出应包含 2 个快照历史"
        assert data["rule_snapshot"]["id"] == snap2["id"], "活动快照应为最新的"
        assert data["rule_snapshot"]["is_active"] is True
        inactive_in_list = [s for s in data["rule_snapshots"] if not s["is_active"]]
        assert len(inactive_in_list) == 1, "历史中应有 1 个非活动快照"
        assert inactive_in_list[0]["superseded_by"] == snap2["id"], "被替代关系应正确"
        print("[OK] JSON 导出中快照历史完整，包含被覆盖关系")
    finally:
        os.unlink(json_path)

    rc = run_cli("history", "test-snapshot-change", "-t", "rules")
    assert rc == 0, "history rules 失败"
    print("[OK] history --type rules 显示多个快照历史")

    print("\n[OK][OK] 回归测试 5 通过: 规则变更检测与快照覆盖正常 [OK][OK]")


def test_rule_snapshot_revoke_republish(examples_dir):
    """回归测试 6: revoke 后重发 - 完整链路 publish→revoke→check→approve→publish，验证规则快照和状态历史"""
    separator("回归测试 6: revoke 后重发(完整链路)的规则快照一致性与状态历史")

    mid = f"{examples_dir}/manifest_good.json"
    rc = run_cli("import", mid, "--id", "test-snapshot-revoke", "--name", "测试-revoke重发快照")
    assert rc == 0, "import 失败"

    rc = run_cli("check", "test-snapshot-revoke")
    assert rc == 0, "check 失败"

    storage = Storage(TEST_DB)
    snap_before = storage.get_active_rule_snapshot("test-snapshot-revoke")
    assert snap_before is not None
    sha_before = snap_before["rules_sha256"]
    snap_id_before = snap_before["id"]
    rules_yaml_before = snap_before["rules_yaml"]
    rule_count_before = snap_before["rule_count"]
    enabled_count_before = snap_before["enabled_rule_count"]
    print(f"[OK] 发布前快照: #{snap_id_before}, SHA256: {sha_before[:16]}...")

    rc = run_cli("approve", "test-snapshot-revoke", "--approver", "tester", "--comment", "审批通过")
    assert rc == 0, "approve 失败"
    rc = run_cli("publish", "test-snapshot-revoke", "--operator", "op", "--comment", "发布")
    assert rc == 0, "publish 失败"
    batch = storage.get_batch("test-snapshot-revoke")
    assert batch["status"] == BatchStatus.PUBLISHED.value
    print("[OK] 已发布")

    snap_after_publish = storage.get_active_rule_snapshot("test-snapshot-revoke")
    assert snap_after_publish["id"] == snap_id_before, "发布后快照 ID 应不变"
    all_snaps_pub = storage.get_rule_snapshots("test-snapshot-revoke")
    assert len(all_snaps_pub) == 1, "发布后不应新增快照"
    print("[OK] 发布后规则快照保持不变")

    rc = run_cli("revoke", "test-snapshot-revoke", "--operator", "op", "--comment", "发现问题回滚")
    assert rc == 0, "revoke 失败"
    batch_after_revoke = storage.get_batch("test-snapshot-revoke")
    assert batch_after_revoke["status"] == BatchStatus.APPROVED.value, "revoke 后状态应为 APPROVED"
    print("[OK] 已撤销发布，状态已回退至 APPROVED")

    snap_after_revoke = storage.get_active_rule_snapshot("test-snapshot-revoke")
    assert snap_after_revoke is not None
    assert snap_after_revoke["id"] == snap_id_before, "revoke 后快照 ID 不应变"
    assert snap_after_revoke["rules_sha256"] == sha_before, "revoke 后 SHA256 不应变"
    assert snap_after_revoke["rules_yaml"] == rules_yaml_before, "revoke 后规则内容不应变"
    assert snap_after_revoke["is_active"] is True, "revoke 后快照仍应活动"
    all_snaps_revoke = storage.get_rule_snapshots("test-snapshot-revoke")
    assert len(all_snaps_revoke) == 1, "revoke 后不应新增快照"
    print("[OK] revoke 后规则快照保持不变，未创建新快照")

    status_history_before_recheck = storage.get_status_history("test-snapshot-revoke")
    print(f"[OK] revoke 后状态历史共 {len(status_history_before_recheck)} 条")

    rc = run_cli("check", "test-snapshot-revoke")
    assert rc == 0, "revoke 后重新 check 应成功（APPROVED -> CHECKING 流转）"
    batch_after_recheck = storage.get_batch("test-snapshot-revoke")
    assert batch_after_recheck["status"] == BatchStatus.CHECK_PASSED.value, "重新 check 后状态应为 CHECK_PASSED"
    print("[OK] revoke 后重新 check 成功（APPROVED → CHECKING → CHECK_PASSED）")

    snap_after_recheck = storage.get_active_rule_snapshot("test-snapshot-revoke")
    assert snap_after_recheck["id"] == snap_id_before, "重新 check 后仍应沿用原快照"
    assert snap_after_recheck["rules_sha256"] == sha_before, "重新 check 后 SHA256 应一致"
    assert snap_after_recheck["rule_count"] == rule_count_before, "重新 check 后规则总数应一致"
    assert snap_after_recheck["enabled_rule_count"] == enabled_count_before, "重新 check 后启用规则数应一致"
    all_snaps_after_recheck = storage.get_rule_snapshots("test-snapshot-revoke")
    assert len(all_snaps_after_recheck) == 1, "重新 check 不应创建新快照"
    print("[OK] revoke 后重新 check 沿用原规则快照，未创建新快照")

    status_history = storage.get_status_history("test-snapshot-revoke")
    print(f"[OK] 当前状态历史共 {len(status_history)} 条")

    status_values = [h["to_status"] for h in status_history]
    assert status_values[-3:] == [
        BatchStatus.REVOKED.value,
        BatchStatus.APPROVED.value,
        BatchStatus.CHECKING.value,
    ] or status_values[-2:] == [
        BatchStatus.CHECKING.value,
        BatchStatus.CHECK_PASSED.value,
    ], f"状态历史应包含 REVOKED→APPROVED→CHECKING→CHECK_PASSED，实际: {status_values[-5:]}"

    found_approved_to_checking = False
    for i in range(len(status_history) - 1):
        if (status_history[i]["to_status"] == BatchStatus.APPROVED.value
                and status_history[i + 1]["from_status"] == BatchStatus.APPROVED.value
                and status_history[i + 1]["to_status"] == BatchStatus.CHECKING.value):
            found_approved_to_checking = True
            break
    if not found_approved_to_checking:
        for h in status_history:
            if (h.get("from_status") == BatchStatus.APPROVED.value
                    and h["to_status"] == BatchStatus.CHECKING.value):
                found_approved_to_checking = True
                break
    assert found_approved_to_checking, "状态历史中应存在 APPROVED → CHECKING 的流转记录"
    print("[OK] 状态历史包含完整流转链：APPROVED → CHECKING → CHECK_PASSED")

    rc = run_cli("approve", "test-snapshot-revoke", "--approver", "tester2", "--comment", "修复后重新审批")
    assert rc == 0, "重新 approve 失败"
    rc = run_cli("publish", "test-snapshot-revoke", "--operator", "op2", "--comment", "重新发布")
    assert rc == 0, "重新 publish 失败"
    print("[OK] 已重新审批并发布")

    snap_final = storage.get_active_rule_snapshot("test-snapshot-revoke")
    assert snap_final["id"] == snap_id_before, "重新发布后快照 ID 仍应一致"
    assert snap_final["rules_sha256"] == sha_before, "重新发布后 SHA256 仍应一致"
    assert snap_final["rules_yaml"] == rules_yaml_before, "重新发布后规则内容仍应一致"
    all_snaps_final = storage.get_rule_snapshots("test-snapshot-revoke")
    assert len(all_snaps_final) == 1, "整个链路中只有 1 个快照"
    print("[OK] 完整链路 publish→revoke→check→approve→publish 中规则快照保持一致")

    pub_records = storage.get_publish_records("test-snapshot-revoke")
    assert len(pub_records) >= 3, f"发布历史应至少 3 条（发布+撤销+重发），实际 {len(pub_records)}"
    actions = [p["action"] for p in pub_records]
    assert actions[-3:] == ["publish", "revoke", "publish"], f"发布动作顺序应为 publish→revoke→publish，实际 {actions}"
    print(f"[OK] 发布历史共 {len(pub_records)} 条，动作顺序：{'→'.join(actions)}")

    status_history_final = storage.get_status_history("test-snapshot-revoke")
    print(f"[OK] 完整状态历史共 {len(status_history_final)} 条")
    status_seq = [h["to_status"] for h in status_history_final]
    print(f"     状态流转链: {' → '.join(status_seq)}")

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8") as tf:
        json_path = tf.name
    try:
        rc = run_cli("export", "test-snapshot-revoke", "-o", json_path, "-f", "json")
        assert rc == 0, "export 失败"
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["rule_snapshot"]["id"] == snap_id_before, "导出的活动快照 ID 应匹配"
        assert data["rule_snapshot"]["is_active"] is True, "导出的活动快照应标记为活动"
        assert len(data["rule_snapshots"]) == 1, "导出的快照历史应为 1 条"
        assert len(data["publish_history"]) >= 3, f"导出的发布历史应至少 3 条，实际 {len(data['publish_history'])}"
        assert "rules_summary" in data["rule_snapshot"], "应包含 rules_summary"
        assert len(data["status_history"]) >= len(status_history_final), "导出的状态历史应完整"
        print("[OK] 导出的 JSON 中规则快照、发布历史、状态历史一致")
    finally:
        os.unlink(json_path)

    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as tf:
        md_path = tf.name
    try:
        rc = run_cli("export", "test-snapshot-revoke", "-o", md_path, "-f", "markdown")
        assert rc == 0, "export markdown 失败"
        with open(md_path, "r", encoding="utf-8") as f:
            md = f.read()
        assert "规则快照" in md, "Markdown 应包含规则快照章节"
        assert str(snap_id_before) in md, "Markdown 应包含快照 ID"
        assert "关键校验项" in md, "Markdown 应包含关键校验项"
        assert "发布与回退历史" in md, "Markdown 应包含发布与回退历史"
        assert "状态流转" in md, "Markdown 应包含状态流转历史"
        print("[OK] 导出的 Markdown 中包含规则快照、发布历史、状态流转信息")
    finally:
        os.unlink(md_path)

    rc = run_cli("status", "test-snapshot-revoke")
    assert rc == 0, "status 失败"
    print("[OK] status 命令输出正常")

    print("\n[OK][OK] 回归测试 6 通过: revoke→check→approve→publish 全链路正常 [OK][OK]")


def test_rule_snapshot_re_export(examples_dir):
    """回归测试 7: 旧批次重新导出 - 跨时间导出内容一致，规则快照可追溯"""
    separator("回归测试 7: 旧批次重新导出的一致性")

    mid = f"{examples_dir}/manifest_good.json"
    rc = run_cli("import", mid, "--id", "test-snapshot-reexport", "--name", "测试-重新导出一致性")
    assert rc == 0, "import 失败"

    rc = run_cli("check", "test-snapshot-reexport")
    assert rc == 0, "check 失败"

    rc = run_cli("approve", "test-snapshot-reexport", "--approver", "tester", "--comment", "审批")
    assert rc == 0, "approve 失败"
    rc = run_cli("publish", "test-snapshot-reexport", "--operator", "op", "--comment", "发布")
    assert rc == 0, "publish 失败"

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8") as tf:
        export1_path = tf.name
    try:
        rc = run_cli("export", "test-snapshot-reexport", "-o", export1_path, "-f", "json")
        assert rc == 0, "第一次 export 失败"
        with open(export1_path, "r", encoding="utf-8") as f:
            export1 = json.load(f)
        print(f"[OK] 第一次导出完成，规则快照 ID: {export1['rule_snapshot']['id']}")

        snap_id_1 = export1["rule_snapshot"]["id"]
        sha_1 = export1["rule_snapshot"]["rules_sha256"]
        rules_count_1 = export1["rule_snapshot"]["rule_count"]
        summary_count_1 = len(export1["rule_snapshot"]["rules_summary"])

        storage = Storage(TEST_DB)
        snap_db = storage.get_active_rule_snapshot("test-snapshot-reexport")
        assert snap_db["id"] == snap_id_1, "导出的快照 ID 应与数据库一致"
        assert snap_db["rules_sha256"] == sha_1, "导出的 SHA256 应与数据库一致"
        print("[OK] 第一次导出内容与数据库一致")

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8") as tf2:
            export2_path = tf2.name
        try:
            rc = run_cli("export", "test-snapshot-reexport", "-o", export2_path, "-f", "json")
            assert rc == 0, "第二次 export 失败"
            with open(export2_path, "r", encoding="utf-8") as f:
                export2 = json.load(f)
            print(f"[OK] 第二次导出完成，规则快照 ID: {export2['rule_snapshot']['id']}")

            assert export2["rule_snapshot"]["id"] == snap_id_1, "第二次导出的快照 ID 应不变"
            assert export2["rule_snapshot"]["rules_sha256"] == sha_1, "SHA256 应不变"
            assert export2["rule_snapshot"]["rule_count"] == rules_count_1, "规则数应不变"
            assert len(export2["rule_snapshot"]["rules_summary"]) == summary_count_1, "摘要数应不变"
            assert len(export2["rule_snapshots"]) == len(export1["rule_snapshots"]), "历史快照数应不变"
            print("[OK] 两次导出的规则快照完全一致")

            assert export2["batch_id"] == export1["batch_id"], "批次 ID 应一致"
            assert export2["status"] == export1["status"], "状态应一致"
            assert export2["item_count"] == export1["item_count"], "条目数应一致"
            print("[OK] 两次导出的批次基本信息一致")

        finally:
            os.unlink(export2_path)

    finally:
        os.unlink(export1_path)

    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as tf:
        md_path = tf.name
    try:
        rc = run_cli("export", "test-snapshot-reexport", "-o", md_path, "-f", "markdown")
        assert rc == 0, "markdown export 失败"
        with open(md_path, "r", encoding="utf-8") as f:
            md = f.read()
        assert "规则快照（当前活动）" in md, "Markdown 应包含活动规则快照章节"
        assert "关键校验项" in md, "Markdown 应包含关键校验项表格"
        assert f"#{snap_id_1}" in md or str(snap_id_1) in md, "Markdown 应包含快照 ID"
        print("[OK] Markdown 导出包含完整的规则快照信息")
    finally:
        os.unlink(md_path)

    print("\n[OK][OK] 回归测试 7 通过: 旧批次重新导出一致且可追溯 [OK][OK]")


def test_rule_snapshot_resume(examples_dir, config_dir):
    """回归测试 8: resume 的规则快照 - 默认沿用快照，显式换 rules 报差异"""
    separator("回归测试 8: resume 命令的规则快照行为")

    mid = f"{examples_dir}/manifest_good.json"
    rc = run_cli("import", mid, "--id", "test-snapshot-resume", "--name", "测试-resume快照")
    assert rc == 0, "import 失败"

    rc = run_cli("check", "test-snapshot-resume")
    assert rc == 0, "check 失败"

    storage = Storage(TEST_DB)
    snap_before = storage.get_active_rule_snapshot("test-snapshot-resume")
    snap_id_before = snap_before["id"]
    print(f"[OK] 初始快照: #{snap_id_before}")

    rc = run_cli("reject", "test-snapshot-resume", "--approver", "tester", "--comment", "打回重做")
    assert rc == 0, "reject 失败"
    print("[OK] 已驳回，准备 resume 测试")

    rc = run_cli("resume", "test-snapshot-resume", "--to", "check")
    assert rc == 0, "resume 到 check 失败"

    snap_after_resume = storage.get_active_rule_snapshot("test-snapshot-resume")
    assert snap_after_resume["id"] == snap_id_before, "resume 后应沿用原快照"
    all_snaps = storage.get_rule_snapshots("test-snapshot-resume")
    assert len(all_snaps) == 1, "resume 不应创建新快照"
    print("[OK] resume（不带 --rules）沿用原快照，未创建新快照")

    rules_path = f"{config_dir}/rules_test_checksum_error.yaml"
    rc = run_cli("resume", "test-snapshot-resume", "--to", "check", "--rules", rules_path)
    assert rc != 0, "resume 不带 --force 换规则应被拒绝"
    print("[OK] resume 不带 --force 换规则被正确拦截")

    rc = run_cli(
        "resume", "test-snapshot-resume",
        "--to", "check",
        "--rules", rules_path,
        "--force",
    )
    assert rc == 0, "resume 带 --force 换规则应成功"
    print("[OK] resume 带 --force 换规则成功")

    snap_new = storage.get_active_rule_snapshot("test-snapshot-resume")
    assert snap_new["id"] != snap_id_before, "应创建新快照"
    assert snap_new["is_active"] is True, "新快照应活动"
    all_snaps_final = storage.get_rule_snapshots("test-snapshot-resume")
    assert len(all_snaps_final) == 2, "现在应有 2 个快照"
    print(f"[OK] 创建了新快照 #{snap_new['id']}，旧快照被标记为被覆盖")

    old_snap = storage.get_rule_snapshot_by_id(snap_id_before)
    assert old_snap["is_active"] is False, "旧快照应非活动"
    assert old_snap["superseded_by"] == snap_new["id"], "旧快照应记录被谁替代"
    print("[OK] 新旧快照的替代关系正确")

    print("\n[OK][OK] 回归测试 8 通过: resume 的规则快照行为正确 [OK][OK]")


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
        ("回归-规则快照基本功能", lambda: test_rule_snapshot_basic(examples_dir)),
        ("回归-规则快照跨重启持久化", lambda: test_rule_snapshot_persistence(examples_dir)),
        ("回归-规则变更检测与覆盖", lambda: test_rule_snapshot_change(examples_dir, config_dir)),
        ("回归-revoke后重发快照一致", lambda: test_rule_snapshot_revoke_republish(examples_dir)),
        ("回归-旧批次重新导出一致", lambda: test_rule_snapshot_re_export(examples_dir)),
        ("回归-resume的规则快照", lambda: test_rule_snapshot_resume(examples_dir, config_dir)),
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
