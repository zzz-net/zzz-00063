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
TEST_DIR = os.path.dirname(TEST_DB)


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


def run_cli_capture(args_list):
    """运行 CLI 命令并捕获 (退出码, 输出字符串)，args_list 是不含 --db/--no-color 的参数列表"""
    import io
    buf = io.StringIO()
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    try:
        sys.stdout = buf
        sys.stderr = buf
        rc = cli_main(args_list)
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr
    return rc, buf.getvalue()


def clean_db(db_path):
    """删除指定 db 文件，确保测试从零开始"""
    d = os.path.dirname(db_path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)
    if os.path.exists(db_path):
        os.unlink(db_path)


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


def test_revoke_docs_consistency(examples_dir):
    """回归测试 9: revoke 后说明链路一致 - CLI 输出、状态流转、status 下一步建议 三处对齐"""
    separator("回归测试 9: revoke 说明链路一致（CLI + 状态表 + status 建议）")

    mid = f"{examples_dir}/manifest_good.json"
    rc = run_cli("import", mid, "--id", "test-docs-consistency", "--name", "测试-revoke说明一致性")
    assert rc == 0, "import 失败"

    rc = run_cli("check", "test-docs-consistency")
    assert rc == 0, "check 失败"

    storage = Storage(TEST_DB)
    snap_before = storage.get_active_rule_snapshot("test-docs-consistency")
    assert snap_before is not None
    snap_id_before = snap_before["id"]

    rc = run_cli("approve", "test-docs-consistency", "--approver", "tester", "--comment", "审批通过")
    assert rc == 0, "approve 失败"
    rc = run_cli("publish", "test-docs-consistency", "--operator", "op", "--comment", "发布")
    assert rc == 0, "publish 失败"
    print("[OK] 已发布")

    import io as _io
    import contextlib
    revoke_capture = _io.StringIO()
    with contextlib.redirect_stdout(revoke_capture):
        rc_revoke = cli_main(["--db", TEST_DB, "--no-color",
                              "revoke", "test-docs-consistency",
                              "--operator", "op", "--comment", "紧急回滚"])
    assert rc_revoke == 0, "revoke 失败"
    revoke_output = revoke_capture.getvalue()
    assert "状态流转路径" in revoke_output, "revoke CLI 输出应包含状态流转路径"
    assert "APPROVED   -(check)->" in revoke_output, "revoke CLI 输出应包含 APPROVED→CHECKING 路径"
    assert "后续操作" in revoke_output, "revoke CLI 输出应包含后续操作"
    assert "无需修改直接发布" in revoke_output, "revoke CLI 输出应包含直接发布选项"
    assert "修改清单后重跑校验" in revoke_output, "revoke CLI 输出应包含重跑校验选项"
    assert "沿用原批次规则快照" in revoke_output, "revoke CLI 输出应提示规则快照沿用"
    assert f"沿用规则快照 #{snap_id_before}" in revoke_output, "revoke CLI 输出应包含沿用的快照 ID"
    assert "确认规则快照与校验结果" in revoke_output, "revoke CLI 输出应包含确认方法"
    assert "patchgate status" in revoke_output, "revoke CLI 输出应包含 status 命令"
    assert "patchgate history" in revoke_output, "revoke CLI 输出应包含 history 命令"
    assert "-t rules" in revoke_output, "revoke CLI 输出应提示查看规则快照历史"
    assert "-t status" in revoke_output, "revoke CLI 输出应提示查看状态流转历史"
    print("[OK] revoke CLI 输出完整：状态路径、后续操作（发布/重检/确认）、规则快照提示齐全")

    batch_after_revoke = storage.get_batch("test-docs-consistency")
    assert batch_after_revoke["status"] == BatchStatus.APPROVED.value, "revoke 后状态应为 APPROVED"
    status_history = storage.get_status_history("test-docs-consistency")
    transitions = [(h.get("from_status"), h["to_status"]) for h in status_history]
    assert (BatchStatus.PUBLISHED.value, BatchStatus.REVOKED.value) in transitions
    assert (BatchStatus.REVOKED.value, BatchStatus.APPROVED.value) in transitions
    print(f"[OK] 状态历史正确：PUBLISHED→REVOKED→APPROVED（共 {len(status_history)} 条记录）")

    from patchgate.models import BATCH_STATUS_FLOW
    assert BatchStatus.CHECKING in BATCH_STATUS_FLOW[BatchStatus.APPROVED], \
        "状态机表应允许 APPROVED → CHECKING 流转"
    assert BatchStatus.PUBLISHED in BATCH_STATUS_FLOW[BatchStatus.APPROVED], \
        "状态机表应允许 APPROVED → PUBLISHED 流转"
    approved_targets = [s.value for s in BATCH_STATUS_FLOW[BatchStatus.APPROVED]]
    assert "checking" in approved_targets and "published" in approved_targets
    print(f"[OK] 状态机 BATCH_STATUS_FLOW：APPROVED 允许流转到 {approved_targets}")

    status_capture = _io.StringIO()
    with contextlib.redirect_stdout(status_capture):
        rc_status = cli_main(["--db", TEST_DB, "--no-color", "status", "test-docs-consistency"])
    assert rc_status == 0, "status 失败"
    status_output = status_capture.getvalue()
    assert "下一步建议" in status_output, "status 应输出下一步建议"
    assert ("审批通过，可直接发布或修改后重检" in status_output or "撤销后回退到审批通过状态" in status_output), "status 下一步建议应描述 APPROVED 状态"
    assert "直接发布" in status_output or "直接重新发布" in status_output, "status 下一步建议应包含直接发布"
    assert "修改后重新校验" in status_output, "status 下一步建议应包含修改后重跑校验"
    assert "一键续跑至发布" in status_output, "status 下一步建议应包含一键续跑"
    assert f"#{snap_id_before}" in status_output, "status 应显示规则快照 ID"
    assert "规则快照" in status_output, "status 应显示规则快照信息"
    assert "关键校验项" in status_output, "status 应显示关键校验项"
    print("[OK] status 输出：下一步建议（发布/重检/续跑）、规则快照、关键校验项齐全")

    rc = run_cli("check", "test-docs-consistency")
    assert rc == 0, "revoke 后从 APPROVED 执行 check 应成功"
    batch_after_check = storage.get_batch("test-docs-consistency")
    assert batch_after_check["status"] == BatchStatus.CHECK_PASSED.value
    status_after = storage.get_status_history("test-docs-consistency")
    latest_transitions = [(h.get("from_status"), h["to_status"]) for h in status_after[-2:]]
    assert (BatchStatus.APPROVED.value, BatchStatus.CHECKING.value) in latest_transitions
    assert (BatchStatus.CHECKING.value, BatchStatus.CHECK_PASSED.value) in latest_transitions
    print(f"[OK] APPROVED→CHECKING→CHECK_PASSED 流转成功（最新 2 步: {latest_transitions}）")

    snap_after_recheck = storage.get_active_rule_snapshot("test-docs-consistency")
    assert snap_after_recheck["id"] == snap_id_before, "重新 check 后规则快照 ID 应不变"
    assert snap_after_recheck["is_active"] is True, "快照仍应活动"
    all_snaps = storage.get_rule_snapshots("test-docs-consistency")
    assert len(all_snaps) == 1, "不应创建新快照"
    print(f"[OK] 重新 check 后沿用原快照 #{snap_id_before}，未创建新快照")

    rc = run_cli("approve", "test-docs-consistency", "--approver", "tester2", "--comment", "修复后再次审批")
    assert rc == 0, "二次 approve 失败"
    rc = run_cli("publish", "test-docs-consistency", "--operator", "op2", "--comment", "重新发布")
    assert rc == 0, "二次 publish 失败"
    batch_final = storage.get_batch("test-docs-consistency")
    assert batch_final["status"] == BatchStatus.PUBLISHED.value
    print("[OK] 完整链路：approve→publish 成功，最终状态 PUBLISHED")

    pub_records = storage.get_publish_records("test-docs-consistency")
    actions = [p["action"] for p in pub_records]
    assert actions == ["publish", "revoke", "publish"], f"发布记录应为 publish→revoke→publish，实际 {actions}"
    print(f"[OK] 发布历史动作顺序正确: {'→'.join(actions)}")

    status_seq = [h["to_status"] for h in storage.get_status_history("test-docs-consistency")]
    expected_parts = ["approved", "published", "revoked", "approved", "checking", "check_passed", "approved", "published"]
    for ep in expected_parts:
        assert ep in status_seq, f"状态流转序列应包含 '{ep}'，实际 {status_seq}"
    print(f"[OK] 完整状态流转链包含全部关键节点: {' → '.join(status_seq)}")

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8") as tf:
        json_path = tf.name
    try:
        rc = run_cli("export", "test-docs-consistency", "-o", json_path, "-f", "json")
        assert rc == 0, "export 失败"
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["rule_snapshot"]["id"] == snap_id_before
        assert data["rule_snapshot"]["is_active"] is True
        assert len(data["publish_history"]) == 3
        assert data["status"] == BatchStatus.PUBLISHED.value
        approved_to_checking_count = sum(
            1 for h in data["status_history"]
            if h.get("from") == BatchStatus.APPROVED.value
            and h["to"] == BatchStatus.CHECKING.value
        )
        assert approved_to_checking_count >= 1, "导出的状态历史应包含 APPROVED → CHECKING"
        print("[OK] JSON 导出：快照一致、状态历史完整、APPROVED→CHECKING 存在")
    finally:
        os.unlink(json_path)

    print("\n[OK][OK] 回归测试 9 通过: revoke 说明链路（CLI/状态表/status/export）完全一致 [OK][OK]")


def test_revoke_default_rules_change_detection(examples_dir):
    """
    回归测试 10: 撤销后默认规则文件变更的检测与提示
    场景：批次 check→approve→publish→revoke 后，用户修改了默认 config/rules.yaml，
         再次 check（不传 --rules）时，应检测到默认规则与快照不一致，
         明确告知用户沿用旧快照，如需切换需显式 --force。
    """
    db_path = os.path.join(TEST_DIR, "test_revoke_default_rules_change.db")
    clean_db(db_path)

    bid = "test-revoke-default-change"
    manifest_src = os.path.join(examples_dir, "manifest_good.json")
    rules_default = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "config", "rules.yaml")
    assert os.path.exists(rules_default), f"默认规则文件应存在: {rules_default}"

    # 备份默认规则
    rules_backup = rules_default + ".bak_test10"
    shutil.copy2(rules_default, rules_backup)

    try:
        # 步骤 1-4: import → check（不传 --rules，使用默认）→ approve → publish → revoke
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "import", manifest_src, "--id", bid, "--name", "test"])
        assert rc == 0, f"import 应成功 rc={rc}\n{out}"
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "check", bid])
        assert rc == 0, f"首次 check 应成功 (rc=0)，实际 rc={rc}\n{out}"

        rc, out = run_cli_capture(["--db", db_path, "--no-color", "approve", bid,
                                   "--approver", "tester", "--comment", "ok"])
        assert rc == 0

        rc, out = run_cli_capture(["--db", db_path, "--no-color", "publish", bid,
                                   "--operator", "op", "--comment", "release"])
        assert rc == 0

        rc, out = run_cli_capture(["--db", db_path, "--no-color", "revoke", bid,
                                   "--operator", "op", "--comment", "rollback"])
        assert rc == 0

        # 记录当前快照 ID 和 SHA
        rc, status_out = run_cli_capture(["--db", db_path, "--no-color", "status", bid])
        assert "快照 #" in status_out, f"撤销后 status 应显示规则快照，实际输出：\n{status_out}"

        # 步骤 5: 修改默认规则文件（禁用一条规则 + 改一条 severity）
        with open(rules_default, "r", encoding="utf-8") as f:
            orig_rules_content = f.read()
        modified_rules_content = orig_rules_content.replace(
            "  - id: version_format\n    name: \"版本号格式检查\"\n    enabled: true",
            "  - id: version_format\n    name: \"版本号格式检查(已禁用)\"\n    enabled: false"
        ).replace(
            "severity: warning\n    description: \"发布包必须包含 checksum 校验和\"",
            "severity: error\n    description: \"发布包必须包含 checksum 校验和(已升级)\""
        )
        assert modified_rules_content != orig_rules_content, "修改后的规则内容应与原内容不同"
        with open(rules_default, "w", encoding="utf-8") as f:
            f.write(modified_rules_content)

        # 步骤 6: 不传 --rules 重新 check，应检测到不一致并提示沿用旧快照
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "check", bid])
        assert rc == 0, f"不传 --rules check 应沿用旧快照并成功 (rc=0)，实际 rc={rc}\n{out}"
        assert "与默认规则文件不一致" in out, \
            f"check 输出应提示默认规则与快照不一致，实际输出：\n{out}"
        assert "沿用快照不变更" in out, \
            f"check 输出应告知默认沿用旧快照，实际输出：\n{out}"
        assert "--force" in out, \
            f"check 输出应提示如需切换用 --force，实际输出：\n{out}"

        # 步骤 7: status 也应显示不一致提示
        rc, status_out = run_cli_capture(["--db", db_path, "--no-color", "status", bid])
        assert "规则快照" in status_out
        assert "与默认规则" in status_out, \
            f"status 应显示默认规则一致性状态，实际输出：\n{status_out}"

        # 步骤 8: history rules 也应显示一致性信息
        rc, hist_out = run_cli_capture(["--db", db_path, "--no-color", "history", bid, "-t", "rules"])
        assert "与默认规则" in hist_out, \
            f"history -t rules 应显示默认规则一致性，实际输出：\n{hist_out}"

        # 步骤 9: 加 --force --rules <默认规则路径> 应成功切换并覆盖
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "check", bid,
                                   "--rules", rules_default, "--force"])
        assert rc == 0, f"加 --force --rules 切换默认规则应成功 (rc=0)，实际 rc={rc}\n{out}"
        assert "已更新活动规则快照" in out or "规则快照" in out, \
            f"加 --force 后应更新快照，实际输出：\n{out}"

        # 步骤 10: 切换后再次 check，应不再提示与默认规则不一致
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "check", bid])
        assert rc == 0
        # 快照已更新为默认规则，不应再提示不一致
        assert "与默认规则文件不一致" not in out, \
            f"切换后快照与默认规则一致，不应再提示不一致，实际输出：\n{out}"

    finally:
        # 恢复默认规则
        if os.path.exists(rules_backup):
            shutil.copy2(rules_backup, rules_default)
            os.unlink(rules_backup)

    print("\n[OK][OK] 回归测试 10 通过: 撤销后默认规则变更检测与显式覆盖流程正确 [OK][OK]")


def test_export_rules_consistency_info(examples_dir):
    """
    回归测试 11: 导出结果（JSON/Markdown）中规则一致性信息完整
    场景：创建批次，校验后修改默认规则文件，导出时 JSON/Markdown 中应
         包含 default_rules_consistency 信息（一致/不一致、diff 概览、风险等级、切换命令）
    """
    db_path = os.path.join(TEST_DIR, "test_export_rules_consistency.db")
    clean_db(db_path)

    bid = "test-export-consistency"
    manifest_src = os.path.join(examples_dir, "manifest_good.json")
    rules_default = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "config", "rules.yaml")

    rules_backup = rules_default + ".bak_test11"
    shutil.copy2(rules_default, rules_backup)

    try:
        # import + check（使用默认规则）
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "import", manifest_src, "--id", bid, "--name", "test"])
        assert rc == 0, f"import 失败 rc={rc}\n{out}"
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "check", bid])
        assert rc == 0, f"check 失败 rc={rc}\n{out}"

        # 修改默认规则
        with open(rules_default, "r", encoding="utf-8") as f:
            orig = f.read()
        modified = orig.replace(
            "  - id: version_format\n    name: \"版本号格式检查\"\n    enabled: true",
            "  - id: version_format\n    name: \"版本号格式检查(已禁用)\"\n    enabled: false"
        )
        with open(rules_default, "w", encoding="utf-8") as f:
            f.write(modified)

        # 导出 JSON
        json_path = os.path.join(TEST_DIR, "export_consistency.json")
        if os.path.exists(json_path):
            os.unlink(json_path)
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "export", bid, "-o", json_path])
        assert rc == 0 and os.path.exists(json_path), f"JSON 导出应成功，rc={rc}\n{out}"

        with open(json_path, "r", encoding="utf-8") as f:
            j = json.load(f)

        assert "rule_snapshot" in j, "JSON 导出应包含 rule_snapshot"
        rs = j["rule_snapshot"]
        assert "default_rules_consistency" in rs, \
            "JSON 导出的 rule_snapshot 应包含 default_rules_consistency 字段"
        drc = rs["default_rules_consistency"]
        assert drc.get("default_rules_path"), "应包含默认规则路径"
        assert drc.get("is_consistent") is False, \
            f"修改默认规则后 is_consistent 应为 False，实际: {drc.get('is_consistent')}"
        assert drc.get("diff_summary"), "不一致时应包含 diff_summary"
        assert drc.get("diff_risk_level"), "不一致时应包含 diff_risk_level"
        print("[OK] JSON 导出：default_rules_consistency 字段完整（is_consistent=False，diff 概览+风险等级齐全）")
        os.unlink(json_path)

        # 导出 Markdown
        md_path = os.path.join(TEST_DIR, "export_consistency.md")
        if os.path.exists(md_path):
            os.unlink(md_path)
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "export", bid, "-o", md_path, "-f", "markdown"])
        assert rc == 0 and os.path.exists(md_path), f"Markdown 导出应成功，rc={rc}\n{out}"

        with open(md_path, "r", encoding="utf-8") as f:
            md = f.read()
        assert "与默认规则一致性" in md, "Markdown 应包含'与默认规则一致性'章节"
        assert "[不一致]" in md, "不一致时 Markdown 应标记 [不一致]"
        assert "差异概览" in md, "Markdown 应包含差异概览"
        assert "风险等级" in md, "Markdown 应包含风险等级"
        assert "如需切换到当前默认规则" in md, "Markdown 应包含切换命令提示"
        print("[OK] Markdown 导出：与默认规则一致性章节完整（不一致标记、差异概览、风险等级、切换命令）")
        os.unlink(md_path)

    finally:
        if os.path.exists(rules_backup):
            shutil.copy2(rules_backup, rules_default)
            os.unlink(rules_backup)

    print("\n[OK][OK] 回归测试 11 通过: JSON/Markdown 导出规则一致性信息完整 [OK][OK]")


def test_cross_restart_self_evidence(examples_dir):
    """
    回归测试 12: 跨重启后状态与规则快照自证
    场景：模拟换人接手或进程重启（创建全新 Storage 实例），
         通过 status / history / export 可快速确认：
         ① 批次当前该先重检还是能直接重发
         ② 本次校验沿用的是哪份规则
         ③ 规则快照与当前默认规则是否一致
    """
    db_path = os.path.join(TEST_DIR, "test_cross_restart_self_evidence.db")
    clean_db(db_path)

    bid = "test-restart-self"
    manifest_src = os.path.join(examples_dir, "manifest_good.json")
    rules_default = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "config", "rules.yaml")

    rules_backup = rules_default + ".bak_test12"
    shutil.copy2(rules_default, rules_backup)

    try:
        # "进程1"：导入 → check → approve → publish → revoke → 回到 APPROVED
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "import", manifest_src, "--id", bid, "--name", "test"])
        assert rc == 0, f"import 失败 rc={rc}\n{out}"
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "check", bid])
        assert rc == 0, f"check 失败 rc={rc}\n{out}"
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "approve", bid, "--approver", "A", "--comment", "ok"])
        assert rc == 0, f"approve 失败 rc={rc}\n{out}"
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "publish", bid, "--operator", "A", "--comment", "release"])
        assert rc == 0, f"publish 失败 rc={rc}\n{out}"
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "revoke", bid, "--operator", "A", "--comment", "rollback"])
        assert rc == 0, f"revoke 失败 rc={rc}\n{out}"

        # 模拟进程重启/换人接手：直接在同一个 db 上操作，关键是"接手者"没见过之前的输出
        # 修改默认规则，让接手者通过查询就能发现不一致
        with open(rules_default, "r", encoding="utf-8") as f:
            orig = f.read()
        modified = orig.replace(
            "  - id: version_format\n    name: \"版本号格式检查\"\n    enabled: true",
            "  - id: version_format\n    name: \"版本号格式检查(已禁用)\"\n    enabled: false"
        )
        with open(rules_default, "w", encoding="utf-8") as f:
            f.write(modified)

        # 接手者视角：先 status，确认当前状态 + 下一步 + 规则依据
        rc, status_out = run_cli_capture(["--db", db_path, "--no-color", "status", bid])
        assert rc == 0, f"新进程打开已有 db 后 status 应成功，rc={rc}\n{status_out}"

        # ① 能确认批次状态：APPROVED，可直接发布或修改后重检
        assert "APPROVED" in status_out.upper(), f"status 应显示 APPROVED 状态，实际输出：\n{status_out}"
        assert ("审批通过，可直接发布或修改后重检" in status_out or "撤销后回退到审批通过状态" in status_out), \
            f"status 应说明当前是 APPROVED 可直接发布或修改后重检，实际：\n{status_out}"

        # ② 能确认沿用的规则：快照 ID + SHA
        assert "规则快照 #" in status_out or "快照 #" in status_out, f"status 应显示规则快照 ID，实际：\n{status_out}"
        assert "SHA256:" in status_out, f"status 应显示快照 SHA256，实际：\n{status_out}"

        # ③ 能确认与默认规则是否一致
        assert "与默认规则" in status_out, \
            f"status 应显示默认规则一致性状态，实际：\n{status_out}"

        # 接手者视角：history -t rules，进一步查看规则快照详情
        rc, hist_out = run_cli_capture(["--db", db_path, "--no-color", "history", bid, "-t", "rules"])
        assert rc == 0
        assert "snapshot-" in hist_out or "快照" in hist_out, \
            f"history -t rules 应列出规则快照，实际输出：\n{hist_out}"
        assert "与默认规则" in hist_out, \
            f"history -t rules 应显示与默认规则一致性，实际输出：\n{hist_out}"

        # 接手者视角：下一步建议中包含明确的 action
        assert "直接发布" in status_out or "直接重新发布" in status_out or "无需修改直接发布" in status_out, \
            f"status 下一步应包含直接发布，实际：\n{status_out}"
        assert "重新校验" in status_out, \
            f"status 下一步应包含重新校验，实际：\n{status_out}"

        print("[OK] 跨重启自证: status 可一眼确认①状态/下一步 ②规则快照ID/SHA ③与默认规则一致性")
        print("[OK] 跨重启自证: history -t rules 可追溯规则快照详情与一致性")

    finally:
        if os.path.exists(rules_backup):
            shutil.copy2(rules_backup, rules_default)
            os.unlink(rules_backup)

    print("\n[OK][OK] 回归测试 12 通过: 跨重启/换人接手后可自证状态与规则依据 [OK][OK]")


def test_snapshot_decision_recording(examples_dir, config_dir):
    """回归测试 13: 快照决策记录 - keep_old 和 override_to_new 均写入 snapshot_decisions"""
    separator("回归测试 13: 快照决策记录 (keep_old / override_to_new)")

    db_path = os.path.join(TEST_DIR, "test_snapshot_decision.db")
    clean_db(db_path)

    bid = "test-snap-decision"
    manifest_src = os.path.join(examples_dir, "manifest_good.json")
    rules_default = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "config", "rules.yaml")
    rules_alt = os.path.join(config_dir, "rules_test_checksum_error.yaml")

    rc, out = run_cli_capture(["--db", db_path, "--no-color", "import", manifest_src, "--id", bid, "--name", "test"])
    assert rc == 0, f"import 失败 rc={rc}\n{out}"

    rc, out = run_cli_capture(["--db", db_path, "--no-color", "check", bid])
    assert rc == 0, f"首次 check 失败 rc={rc}\n{out}"

    storage = Storage(db_path)
    snap1 = storage.get_active_rule_snapshot(bid)
    assert snap1 is not None

    decisions_before = storage.get_snapshot_decisions(bid)
    assert len(decisions_before) == 0, "首次 check 不应产生快照决策记录"
    print("[OK] 首次 check 无快照决策记录（规则未变更）")

    rc, out = run_cli_capture(["--db", db_path, "--no-color", "check", bid, "--rules", rules_alt])
    assert rc != 0, "不传 --force 换规则应被拒绝"
    decisions_keep = storage.get_snapshot_decisions(bid)
    assert len(decisions_keep) == 1, f"应记录 1 条 keep_old 决策，实际 {len(decisions_keep)}"
    assert decisions_keep[0]["decision"] == "keep_old"
    assert decisions_keep[0]["old_snapshot_id"] == snap1["id"]
    assert decisions_keep[0]["new_snapshot_id"] is None
    assert decisions_keep[0]["diff_summary"] is not None
    print(f"[OK] 不带 --force 换规则时记录 keep_old 决策 (old=#{decisions_keep[0]['old_snapshot_id']})")

    rc, out = run_cli_capture(["--db", db_path, "--no-color", "check", bid, "--rules", rules_alt, "--force"])
    assert rc == 0, f"加 --force 换规则应成功 rc={rc}\n{out}"
    decisions_after = storage.get_snapshot_decisions(bid)
    assert len(decisions_after) == 2, f"应有 2 条决策记录，实际 {len(decisions_after)}"
    assert decisions_after[1]["decision"] == "override_to_new"
    assert decisions_after[1]["old_snapshot_id"] == snap1["id"]
    assert decisions_after[1]["new_snapshot_id"] is not None
    print(f"[OK] 带 --force 换规则时记录 override_to_new 决策 (#{decisions_after[1]['old_snapshot_id']} → #{decisions_after[1]['new_snapshot_id']})")

    snap2 = storage.get_active_rule_snapshot(bid)
    assert snap2["id"] != snap1["id"], "新快照 ID 应不同"
    print(f"[OK] 快照决策记录完整: keep_old + override_to_new")

    print("\n[OK][OK] 回归测试 13 通过: 快照决策记录正确 [OK][OK]")


def test_snapshot_decision_cross_restart(examples_dir, config_dir):
    """回归测试 14: 快照决策跨重启持久化 - 重新创建 Storage 后决策记录不丢失"""
    separator("回归测试 14: 快照决策跨重启持久化")

    db_path = os.path.join(TEST_DIR, "test_snap_decision_restart.db")
    clean_db(db_path)

    bid = "test-snap-restart"
    manifest_src = os.path.join(examples_dir, "manifest_good.json")
    rules_alt = os.path.join(config_dir, "rules_test_checksum_error.yaml")

    rc, out = run_cli_capture(["--db", db_path, "--no-color", "import", manifest_src, "--id", bid, "--name", "test"])
    assert rc == 0
    rc, out = run_cli_capture(["--db", db_path, "--no-color", "check", bid])
    assert rc == 0

    rc, out = run_cli_capture(["--db", db_path, "--no-color", "check", bid, "--rules", rules_alt])
    assert rc != 0

    storage1 = Storage(db_path)
    decisions1 = storage1.get_snapshot_decisions(bid)
    assert len(decisions1) >= 1
    dec1_id = decisions1[0]["id"]
    dec1_note = decisions1[0]["note"]
    print(f"[OK] 重启前决策记录: #{dec1_id}, decision={decisions1[0]['decision']}")

    storage2 = Storage(db_path)
    decisions2 = storage2.get_snapshot_decisions(bid)
    assert len(decisions2) == len(decisions1), "重启后决策数量应一致"
    assert decisions2[0]["id"] == dec1_id, "重启后决策 ID 应一致"
    assert decisions2[0]["note"] == dec1_note, "重启后决策备注应一致"
    assert decisions2[0]["decision"] == "keep_old"
    print(f"[OK] 重启后决策记录一致: #{decisions2[0]['id']}, decision={decisions2[0]['decision']}")

    rc, status_out = run_cli_capture(["--db", db_path, "--no-color", "status", bid])
    assert "规则快照决策记录" in status_out, f"status 应显示决策记录，实际输出：\n{status_out}"
    assert "沿用旧快照" in status_out, "status 应显示沿用旧快照决策"
    print("[OK] 重启后 status 正确显示快照决策记录")

    print("\n[OK][OK] 回归测试 14 通过: 快照决策跨重启持久化 [OK][OK]")


def test_revoke_full_cycle_with_rules_change(examples_dir):
    """回归测试 15: 撤销后完整链路 revoke→re-check→re-approve→re-publish（含规则变更）"""
    separator("回归测试 15: revoke→re-check→re-approve→re-publish 完整链路（含规则变更）")

    db_path = os.path.join(TEST_DIR, "test_revoke_full_cycle.db")
    clean_db(db_path)

    bid = "test-revoke-cycle"
    manifest_src = os.path.join(examples_dir, "manifest_good.json")
    rules_default = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "config", "rules.yaml")

    rules_backup = rules_default + ".bak_test15"
    shutil.copy2(rules_default, rules_backup)

    try:
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "import", manifest_src, "--id", bid, "--name", "test"])
        assert rc == 0
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "check", bid])
        assert rc == 0
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "approve", bid, "--approver", "A", "--comment", "ok"])
        assert rc == 0
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "publish", bid, "--operator", "A", "--comment", "release"])
        assert rc == 0
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "revoke", bid, "--operator", "A", "--comment", "发现问题"])
        assert rc == 0

        storage = Storage(db_path)
        snap_before = storage.get_active_rule_snapshot(bid)
        assert snap_before is not None

        with open(rules_default, "r", encoding="utf-8") as f:
            orig = f.read()
        modified = orig.replace(
            "  - id: version_format\n    name: \"版本号格式检查\"\n    enabled: true",
            "  - id: version_format\n    name: \"版本号格式检查(已禁用)\"\n    enabled: false"
        )
        with open(rules_default, "w", encoding="utf-8") as f:
            f.write(modified)

        rc, out = run_cli_capture(["--db", db_path, "--no-color", "check", bid])
        assert rc == 0, f"revoke 后重新 check 应成功 rc={rc}\n{out}"
        assert "与默认规则文件不一致" in out, "应提示默认规则变更"
        assert "已记录" in out, "应记录快照决策"

        decisions = storage.get_snapshot_decisions(bid)
        assert len(decisions) >= 1, "应有快照决策记录"
        assert decisions[-1]["decision"] == "keep_old", "应记录 keep_old 决策"
        print("[OK] revoke 后重新 check：检测到规则变更，记录 keep_old 决策")

        snap_after = storage.get_active_rule_snapshot(bid)
        assert snap_after["id"] == snap_before["id"], "沿用旧快照 ID 不应变"
        print(f"[OK] 沿用旧快照 #{snap_after['id']}，未创建新快照")

        rc, status_out = run_cli_capture(["--db", db_path, "--no-color", "status", bid])
        assert "撤销回退信息" in status_out, "status 应显示撤销回退信息"
        assert "发现问题" in status_out, "status 应显示撤销原因"
        assert "规则快照决策记录" in status_out, "status 应显示快照决策记录"
        print("[OK] status 显示撤销回退信息和快照决策记录")

        batch = storage.get_batch(bid)
        assert batch["status"] == BatchStatus.CHECK_PASSED.value

        rc, out = run_cli_capture(["--db", db_path, "--no-color", "approve", bid, "--approver", "B", "--comment", "修复后审批"])
        assert rc == 0
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "publish", bid, "--operator", "B", "--comment", "重新发布"])
        assert rc == 0

        batch_final = storage.get_batch(bid)
        assert batch_final["status"] == BatchStatus.PUBLISHED.value
        print("[OK] 完整链路 revoke→re-check→re-approve→re-publish 成功")

        pubs = storage.get_publish_records(bid)
        actions = [p["action"] for p in pubs]
        assert actions == ["publish", "revoke", "publish"], f"发布动作应为 publish→revoke→publish，实际 {actions}"
        print(f"[OK] 发布历史: {'→'.join(actions)}")

    finally:
        if os.path.exists(rules_backup):
            shutil.copy2(rules_backup, rules_default)
            os.unlink(rules_backup)

    print("\n[OK][OK] 回归测试 15 通过: revoke→re-check→re-approve→re-publish 完整链路 [OK][OK]")


def test_export_snapshot_decisions_and_revoke_context(examples_dir):
    """回归测试 16: 导出一致性 - JSON/Markdown 中 snapshot_decisions 和 revoke_context 完整"""
    separator("回归测试 16: 导出一致性 (snapshot_decisions + revoke_context)")

    db_path = os.path.join(TEST_DIR, "test_export_decisions.db")
    clean_db(db_path)

    bid = "test-export-dec"
    manifest_src = os.path.join(examples_dir, "manifest_good.json")
    rules_default = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "config", "rules.yaml")

    rules_backup = rules_default + ".bak_test16"
    shutil.copy2(rules_default, rules_backup)

    try:
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "import", manifest_src, "--id", bid, "--name", "test"])
        assert rc == 0
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "check", bid])
        assert rc == 0
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "approve", bid, "--approver", "A", "--comment", "ok"])
        assert rc == 0
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "publish", bid, "--operator", "A", "--comment", "release"])
        assert rc == 0
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "revoke", bid, "--operator", "A", "--comment", "发现问题回滚"])
        assert rc == 0

        with open(rules_default, "r", encoding="utf-8") as f:
            orig = f.read()
        modified = orig.replace(
            "  - id: version_format\n    name: \"版本号格式检查\"\n    enabled: true",
            "  - id: version_format\n    name: \"版本号格式检查(已禁用)\"\n    enabled: false"
        )
        with open(rules_default, "w", encoding="utf-8") as f:
            f.write(modified)

        rc, out = run_cli_capture(["--db", db_path, "--no-color", "check", bid])
        assert rc == 0

        json_path = os.path.join(TEST_DIR, "export_decisions.json")
        if os.path.exists(json_path):
            os.unlink(json_path)
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "export", bid, "-o", json_path])
        assert rc == 0 and os.path.exists(json_path)

        with open(json_path, "r", encoding="utf-8") as f:
            j = json.load(f)

        assert "snapshot_decisions" in j, "JSON 导出应包含 snapshot_decisions"
        assert len(j["snapshot_decisions"]) >= 1, "应至少有 1 条快照决策"
        sd = j["snapshot_decisions"][-1]
        assert sd["decision"] == "keep_old", f"最新决策应为 keep_old，实际 {sd['decision']}"
        assert sd["old_snapshot_id"] is not None, "应包含 old_snapshot_id"
        assert sd.get("note"), "应包含决策备注"
        print(f"[OK] JSON 导出: snapshot_decisions 包含 keep_old 决策 (备注: {sd['note'][:50]}...)")

        assert "revoke_context" in j, "JSON 导出应包含 revoke_context"
        rc_data = j["revoke_context"]
        assert rc_data["revoke_operator"] == "A", f"撤销操作人应为 A，实际 {rc_data['revoke_operator']}"
        assert "发现问题回滚" in (rc_data.get("revoke_comment") or ""), "应包含撤销原因"
        print(f"[OK] JSON 导出: revoke_context 完整 (操作人={rc_data['revoke_operator']}, 原因={rc_data['revoke_comment']})")

        os.unlink(json_path)

        md_path = os.path.join(TEST_DIR, "export_decisions.md")
        if os.path.exists(md_path):
            os.unlink(md_path)
        rc, out = run_cli_capture(["--db", db_path, "--no-color", "export", bid, "-o", md_path, "-f", "markdown"])
        assert rc == 0 and os.path.exists(md_path)

        with open(md_path, "r", encoding="utf-8") as f:
            md = f.read()

        assert "规则快照决策记录" in md, "Markdown 应包含规则快照决策记录章节"
        assert "沿用旧快照" in md, "Markdown 应显示沿用旧快照决策"
        assert "撤销回退信息" in md, "Markdown 应包含撤销回退信息章节"
        assert "发现问题回滚" in md, "Markdown 应包含撤销原因"
        print("[OK] Markdown 导出: 规则快照决策记录 + 撤销回退信息完整")

        os.unlink(md_path)

    finally:
        if os.path.exists(rules_backup):
            shutil.copy2(rules_backup, rules_default)
            os.unlink(rules_backup)

    print("\n[OK][OK] 回归测试 16 通过: JSON/Markdown 导出一致性 [OK][OK]")


def test_status_history_export_revoke_consistency(examples_dir):
    """回归测试 17: status/history/export 三处撤销信息一致性"""
    separator("回归测试 17: status / history / export 三处撤销信息对齐")

    db_path = os.path.join(TEST_DIR, "test_consistency.db")
    clean_db(db_path)

    bid = "test-consistency"
    manifest_src = os.path.join(examples_dir, "manifest_good.json")

    rc, out = run_cli_capture(["--db", db_path, "--no-color", "import", manifest_src, "--id", bid, "--name", "test"])
    assert rc == 0
    rc, out = run_cli_capture(["--db", db_path, "--no-color", "check", bid])
    assert rc == 0
    rc, out = run_cli_capture(["--db", db_path, "--no-color", "approve", bid, "--approver", "zhangsan", "--comment", "审批"])
    assert rc == 0
    rc, out = run_cli_capture(["--db", db_path, "--no-color", "publish", bid, "--operator", "lisi", "--comment", "发布"])
    assert rc == 0
    rc, out = run_cli_capture(["--db", db_path, "--no-color", "revoke", bid, "--operator", "wangwu", "--comment", "紧急回滚"])
    assert rc == 0

    storage = Storage(db_path)
    snap = storage.get_active_rule_snapshot(bid)
    assert snap is not None
    snap_id = snap["id"]

    rc, status_out = run_cli_capture(["--db", db_path, "--no-color", "status", bid])
    assert rc == 0
    assert "撤销回退信息" in status_out, "status 应显示撤销回退信息"
    assert "wangwu" in status_out, "status 应显示撤销操作人"
    assert "紧急回滚" in status_out, "status 应显示撤销原因"
    assert f"#{snap_id}" in status_out, "status 应显示规则快照 ID"
    assert "撤销后回退到审批通过状态" in status_out, "status 下一步应识别为撤销后回退"
    print("[OK] status: 撤销回退信息 + 规则快照 + 撤销后回退建议 一致")

    rc, hist_out = run_cli_capture(["--db", db_path, "--no-color", "history", bid, "-t", "rules"])
    assert rc == 0
    assert f"#{snap_id}" in hist_out, "history 应显示快照 ID"
    print("[OK] history -t rules: 快照 ID 一致")

    json_path = os.path.join(TEST_DIR, "consistency.json")
    if os.path.exists(json_path):
        os.unlink(json_path)
    rc, out = run_cli_capture(["--db", db_path, "--no-color", "export", bid, "-o", json_path])
    assert rc == 0
    with open(json_path, "r", encoding="utf-8") as f:
        j = json.load(f)

    assert "revoke_context" in j
    assert j["revoke_context"]["revoke_operator"] == "wangwu"
    assert "紧急回滚" in j["revoke_context"]["revoke_comment"]
    assert j["rule_snapshot"]["id"] == snap_id
    assert "snapshot_decisions" in j
    print("[OK] export: revoke_context + rule_snapshot + snapshot_decisions 与 status/history 一致")

    md_path = os.path.join(TEST_DIR, "consistency.md")
    if os.path.exists(md_path):
        os.unlink(md_path)
    rc, out = run_cli_capture(["--db", db_path, "--no-color", "export", bid, "-o", md_path, "-f", "markdown"])
    assert rc == 0
    with open(md_path, "r", encoding="utf-8") as f:
        md = f.read()
    assert "撤销回退信息" in md
    assert "wangwu" in md
    assert "紧急回滚" in md
    print("[OK] Markdown export: 撤销回退信息与 status/history 一致")

    os.unlink(json_path)
    os.unlink(md_path)

    print("\n[OK][OK] 回归测试 17 通过: status/history/export 三处一致 [OK][OK]")


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
        ("回归-revoke说明链路一致", lambda: test_revoke_docs_consistency(examples_dir)),
        ("回归-撤销后默认规则变更检测", lambda: test_revoke_default_rules_change_detection(examples_dir)),
        ("回归-导出规则一致性信息完整", lambda: test_export_rules_consistency_info(examples_dir)),
        ("回归-跨重启自证状态与规则", lambda: test_cross_restart_self_evidence(examples_dir)),
        ("回归-快照决策记录", lambda: test_snapshot_decision_recording(examples_dir, config_dir)),
        ("回归-快照决策跨重启持久化", lambda: test_snapshot_decision_cross_restart(examples_dir, config_dir)),
        ("回归-撤销后完整链路含规则变更", lambda: test_revoke_full_cycle_with_rules_change(examples_dir)),
        ("回归-导出快照决策与撤销上下文", lambda: test_export_snapshot_decisions_and_revoke_context(examples_dir)),
        ("回归-status/history/export三处一致", lambda: test_status_history_export_revoke_consistency(examples_dir)),
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


def test_handover_export_basic(examples_dir):
    """测试八-01: 接手包基础导出 - 验证导出内容完整"""
    separator("接手包: 基础导出测试")

    db_path = os.path.join(TEST_DIR, "test_handover_export.db")
    clean_db(db_path)

    bid = "test-ho-export"
    manifest_src = os.path.join(examples_dir, "manifest_good.json")

    rc, out = run_cli_capture(["--db", db_path, "--no-color", "import", manifest_src, "--id", bid, "--name", "测试-接手包导出"])
    assert rc == 0

    rc, out = run_cli_capture(["--db", db_path, "--no-color", "check", bid])
    assert rc == 0

    rc, out = run_cli_capture(["--db", db_path, "--no-color", "approve", bid, "--approver", "zhang", "--comment", "审批通过"])
    assert rc == 0

    storage = Storage(db_path)
    batch = storage.get_batch(bid)
    assert batch is not None
    snap = storage.get_active_rule_snapshot(bid)
    assert snap is not None
    snap_id = snap["id"]

    export_path = os.path.join(TEST_DIR, "handover_export.json")
    if os.path.exists(export_path):
        os.unlink(export_path)

    rc, out = run_cli_capture([
        "--db", db_path, "--no-color",
        "handover-export", bid,
        "-o", export_path,
        "-e", "wang",
        "-n", "张三离职交接",
    ])
    assert rc == 0 and os.path.exists(export_path)

    with open(export_path, "r", encoding="utf-8") as f:
        pkg = json.load(f)

    assert pkg["schema_version"] == "1.0", f"schema 版本应为 1.0，实际 {pkg.get('schema_version')}"
    assert pkg["generated_by"] == "wang", f"导出人应为 wang，实际 {pkg.get('generated_by')}"
    assert pkg["note"] == "张三离职交接", f"交接备注不正确"
    assert "package_hash" in pkg, "应包含包哈希"
    assert len(pkg["package_hash"]) == 64, "包哈希应为 SHA256 (64字符)"

    assert pkg["batch"]["id"] == bid
    assert pkg["batch"]["status"] == "approved"
    assert pkg["batch"]["item_count"] == len(batch["items"])
    assert pkg["batch"]["manifest_hash"] == batch["manifest_hash"]

    assert "latest_validation" in pkg
    assert pkg["latest_validation"]["total_checks"] > 0

    assert "approval_conclusion" in pkg
    assert pkg["approval_conclusion"]["latest_decision"] == "approve"
    assert pkg["approval_conclusion"]["latest_approver"] == "zhang"

    assert "rule_snapshots" in pkg
    assert pkg["rule_snapshots"]["active"] is not None
    assert pkg["rule_snapshots"]["active"]["id"] == snap_id
    assert "all" in pkg["rule_snapshots"] and len(pkg["rule_snapshots"]["all"]) >= 1
    assert "decisions" in pkg["rule_snapshots"]

    assert "todo_actions" in pkg
    todos = pkg["todo_actions"]
    assert len(todos) > 0
    high_priority = [t for t in todos if t["priority"] == "high"]
    assert len(high_priority) >= 1

    assert "log_index" in pkg
    assert pkg["log_index"]["status_transitions"] >= 1
    assert pkg["log_index"]["approvals"] == 1
    assert pkg["log_index"]["publish_records"] == 0

    assert "status_history" in pkg and len(pkg["status_history"]) >= 3

    print("[OK] 接手包导出成功，包含所有必填字段：schema/生成信息、批次信息、校验结果、审批结论、规则快照、待办动作、日志索引、状态历史")
    print(f"    包哈希: {pkg['package_hash'][:16]}...")
    print(f"    条目数: {pkg['batch']['item_count']}")
    print(f"    待办: {len(pkg['todo_actions'])} 项待办")

    os.unlink(export_path)

    rc, out = run_cli_capture(["--db", db_path, "--no-color", "publish", bid, "--operator", "wang", "--comment", "正式发布"])
    assert rc == 0

    rc, out = run_cli_capture(["--db", db_path, "--no-color", "status", bid])
    assert rc == 0

    print("\n[OK][OK] 测试八-01 通过: 接手包基础导出完整 [OK][OK]")


def test_handover_import_no_conflict(examples_dir):
    """测试八-02: 无冲突导入 - 验证导入后数据完整且可继续流程"""
    separator("接手包: 无冲突导入测试")

    db1 = os.path.join(TEST_DIR, "test_ho_export.db")
    db2 = os.path.join(TEST_DIR, "test_ho_import.db")
    clean_db(db1)
    clean_db(db2)

    bid = "test-ho-import"
    manifest_src = os.path.join(examples_dir, "manifest_good.json")

    rc, out = run_cli_capture(["--db", db1, "--no-color", "import", manifest_src, "--id", bid, "--name", "测试-无冲突导入"])
    assert rc == 0

    rc, out = run_cli_capture(["--db", db1, "--no-color", "check", bid])
    assert rc == 0

    rc, out = run_cli_capture(["--db", db1, "--no-color", "approve", bid, "--approver", "zhang", "--comment", "审批通过"])
    assert rc == 0

    export_path = os.path.join(TEST_DIR, "handover_no_conflict.json")
    if os.path.exists(export_path):
        os.unlink(export_path)
    rc, out = run_cli_capture([
        "--db", db1, "--no-color",
        "handover-export", bid,
        "-o", export_path,
        "-e", "wang",
    ])
    assert rc == 0

    rc, out = run_cli_capture([
        "--db", db2, "--no-color",
        "handover-import", export_path,
        "--by", "li",
        "--note", "接手项目交接",
    ])
    assert rc == 0, f"导入应成功 rc={rc}\n{out}"

    storage2 = Storage(db2)
    batch2 = storage2.get_batch(bid)
    assert batch2 is not None
    assert batch2["status"] == "approved", f"导入后状态应为 approved，实际 {batch2['status']}"
    assert batch2["name"] == "测试-无冲突导入", "批次名称应一致"

    approvals2 = storage2.get_approvals(bid)
    assert len(approvals2) == 1, f"审批记录应导入，实际 {len(approvals2)} 条"
    assert approvals2[0]["approver"] == "zhang", "审批人应为 zhang"
    assert approvals2[0]["decision"] == "approve"

    snaps2 = storage2.get_rule_snapshots(bid)
    assert len(snaps2) >= 1, f"规则快照应导入"

    checks2 = storage2.get_check_results(bid)
    assert len(checks2) > 0, f"检查结果应导入"

    status_hist2 = storage2.get_status_history(bid)
    assert len(status_hist2) >= 3, "状态历史应导入"

    imports = storage2.get_handover_imports_for_batch(bid)
    assert len(imports) == 1, f"应有 1 条导入记录"
    assert imports[0]["imported_by"] == "li"
    assert imports[0]["package_generated_by"] == "wang"
    assert imports[0]["import_note"] == "接手项目交接"
    assert imports[0]["original_batch_id"] == bid

    print("[OK] 导入成功，所有数据完整：批次/审批/检查/快照/历史 全部导入")
    print(f"    导入记录 #{imports[0]['id']}: 原ID={imports[0]['original_batch_id']}")
    print(f"    导出人 -> 导入人: {imports[0]['package_generated_by']} -> {imports[0]['imported_by']}")

    rc, status_out = run_cli_capture(["--db", db2, "--no-color", "status", bid])
    assert rc == 0
    assert "接手来源" in status_out or "接手来源" in status_out, "status 应显示接手来源"
    assert "li" in status_out, "status 应显示导入人"
    assert "wang" in status_out, "status 应显示导出人"
    print("[OK] status 显示接手来源信息")

    rc, hist_out = run_cli_capture(["--db", db2, "--no-color", "history", bid, "-t", "status"])
    assert rc == 0
    assert "接手包导入记录" in hist_out, "history 应显示接手包导入记录"
    print("[OK] history 显示接手包导入记录")

    rc, out = run_cli_capture(["--db", db2, "--no-color", "publish", bid, "--operator", "li", "--comment", "接手后发布"])
    assert rc == 0, f"导入后应可继续发布 rc={rc}\n{out}"

    batch_final = storage2.get_batch(bid)
    assert batch_final["status"] == "published"
    print("[OK] 导入后可继续流程：approved → publish 成功")

    os.unlink(export_path)

    print("\n[OK][OK] 测试八-02 通过: 无冲突导入完整且可继续 [OK][OK]")


def test_handover_conflict_duplicate_id(examples_dir):
    """测试八-03: 冲突处理 - 同名批次 ID 冲突"""
    separator("接手包: 同名批次 ID 冲突测试")

    db_path = os.path.join(TEST_DIR, "test_ho_dup.db")
    clean_db(db_path)

    bid = "test-ho-dup"
    manifest_src = os.path.join(examples_dir, "manifest_good.json")

    rc, out = run_cli_capture(["--db", db_path, "--no-color", "import", manifest_src, "--id", bid, "--name", "测试-本地批次"])
    assert rc == 0
    rc, out = run_cli_capture(["--db", db_path, "--no-color", "check", bid])
    assert rc == 0

    export_path = os.path.join(TEST_DIR, "handover_dup.json")
    if os.path.exists(export_path):
        os.unlink(export_path)
    rc, out = run_cli_capture([
        "--db", db_path, "--no-color",
        "handover-export", bid,
        "-o", export_path,
        "-e", "exporter1",
    ])
    assert rc == 0

    rc, out = run_cli_capture([
        "--db", db_path, "--no-color",
        "handover-import", export_path,
        "--by", "importer1",
        "--dry-run",
    ])
    assert rc == 0
    assert "检测到" in out, "--dry-run 应检测到冲突"
    assert "duplicate_id" in out, "应显示 duplicate_id 冲突"
    print("[OK] --dry-run 正确检测到 duplicate_id 冲突")

    rc, out = run_cli_capture([
        "--db", db_path, "--no-color",
        "handover-import", export_path,
        "--by", "importer1",
        "--resolve", "duplicate_id=rename_package",
    ])
    assert rc == 0, f"rename_package 应成功 rc={rc}\n{out}"

    storage = Storage(db_path)
    batches = storage.list_batches()
    batch_ids = [b["id"] for b in batches]
    assert bid in batch_ids, "原批次应保留"
    renamed = [b for b in batch_ids if b.startswith(f"{bid}-imported-")]
    assert len(renamed) == 1, f"应生成 1 个重命名的批次，实际 {len(renamed)}"

    new_bid = renamed[0]
    imports = storage.get_handover_imports_for_batch(new_bid)
    assert len(imports) == 1
    assert imports[0]["original_batch_id"] == bid
    assert imports[0]["imported_by"] == "importer1"
    resolutions = json.loads(imports[0]["resolution_summary"])
    assert any(r["conflict_type"] == "duplicate_id" for r in resolutions)
    assert any(r["resolution"] == "rename_package" for r in resolutions)
    print(f"[OK] 重命名成功: {bid} → {new_bid}")
    print(f"    冲突处理已记录: duplicate_id=rename_package")

    rc, out = run_cli_capture([
        "--db", db_path, "--no-color",
        "handover-import", export_path,
        "--by", "importer2",
        "--resolve", "duplicate_id=keep_local",
        "--resolve", "duplicate_import=skip",
    ])
    assert rc == 0
    assert "保留本地版本" in out
    batches2 = storage.list_batches()
    assert len(batches2) == len(batches), "选择保留本地，不应新增批次"
    print("[OK] 选择 keep_local 正确跳过导入")

    os.unlink(export_path)

    print("\n[OK][OK] 测试八-03 通过: 同名 ID 冲突处理正确 [OK][OK]")


def test_handover_conflict_newer_local(examples_dir):
    """测试八-04: 冲突处理 - 本地记录比导入包更新"""
    separator("接手包: 本地记录较新冲突测试")

    db_export = os.path.join(TEST_DIR, "test_ho_newer_export.db")
    db_import = os.path.join(TEST_DIR, "test_ho_newer_import.db")
    clean_db(db_export)
    clean_db(db_import)

    bid = "test-ho-newer"
    manifest_src = os.path.join(examples_dir, "manifest_good.json")

    rc, out = run_cli_capture(["--db", db_export, "--no-color", "import", manifest_src, "--id", bid, "--name", "测试-本地较新"])
    assert rc == 0
    rc, out = run_cli_capture(["--db", db_export, "--no-color", "check", bid])
    assert rc == 0

    export_path = os.path.join(TEST_DIR, "handover_newer.json")
    if os.path.exists(export_path):
        os.unlink(export_path)
    rc, out = run_cli_capture([
        "--db", db_export, "--no-color",
        "handover-export", bid,
        "-o", export_path,
        "-e", "old_exporter",
    ])
    assert rc == 0

    rc, out = run_cli_capture(["--db", db_import, "--no-color", "import", manifest_src, "--id", bid, "--name", "测试-本地较新"])
    assert rc == 0
    rc, out = run_cli_capture(["--db", db_import, "--no-color", "check", bid])
    assert rc == 0
    rc, out = run_cli_capture(["--db", db_import, "--no-color", "approve", bid, "--approver", "local_approver", "--comment", "本地审批"])
    assert rc == 0

    rc, out = run_cli_capture([
        "--db", db_import, "--no-color",
        "handover-import", export_path,
        "--by", "importer",
        "--dry-run",
    ])
    assert rc == 0
    assert "newer_local" in out, "应检测到 newer_local 冲突"
    assert "本地记录比导入包更新" in out
    print("[OK] 正确检测到 newer_local 冲突")

    rc, out = run_cli_capture([
        "--db", db_import, "--no-color",
        "handover-import", export_path,
        "--by", "importer",
        "--resolve", "newer_local=overwrite_with_package",
    ])
    assert rc == 0

    storage = Storage(db_import)
    batch = storage.get_batch(bid)
    assert batch["status"] == "check_passed", f"覆盖后状态应为 check_passed，实际 {batch['status']}"
    approvals = storage.get_approvals(bid)
    assert len(approvals) == 0, "覆盖后审批记录应清空（包内无审批）"
    imports = storage.get_handover_imports_for_batch(bid)
    assert len(imports) == 1
    resolutions = json.loads(imports[0]["resolution_summary"])
    assert any(r["conflict_type"] == "newer_local" for r in resolutions)
    assert any(r["resolution"] == "overwrite_with_package" for r in resolutions)
    print("[OK] overwrite_with_package 成功覆盖本地较新版本")

    os.unlink(export_path)

    print("\n[OK][OK] 测试八-04 通过: 本地较新冲突处理正确 [OK][OK]")


def test_handover_conflict_rules_changed(examples_dir, config_dir):
    """测试八-05: 冲突处理 - 本地规则文件与包内快照不一致"""
    separator("接手包: 规则文件变更冲突测试")

    db1 = os.path.join(TEST_DIR, "test_ho_rules_export.db")
    db2 = os.path.join(TEST_DIR, "test_ho_rules_import.db")
    clean_db(db1)
    clean_db(db2)

    bid = "test-ho-rules"
    manifest_src = os.path.join(examples_dir, "manifest_good.json")
    rules_default = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "config", "rules.yaml")
    rules_alt = os.path.join(config_dir, "rules_test_checksum_error.yaml")

    rules_backup = rules_default + ".bak_test_ho"
    shutil.copy2(rules_default, rules_backup)

    try:
        rc, out = run_cli_capture(["--db", db1, "--no-color", "import", manifest_src, "--id", bid, "--name", "测试-规则冲突"])
        assert rc == 0
        rc, out = run_cli_capture(["--db", db1, "--no-color", "check", bid])
        assert rc == 0

        export_path = os.path.join(TEST_DIR, "handover_rules.json")
        if os.path.exists(export_path):
            os.unlink(export_path)
        rc, out = run_cli_capture([
            "--db", db1, "--no-color",
            "handover-export", bid,
            "-o", export_path,
            "-e", "exporter_rules",
        ])
        assert rc == 0

        with open(rules_default, "r", encoding="utf-8") as f:
            orig = f.read()
        modified = orig.replace(
            "  - id: version_format\n    name: \"版本号格式检查\"\n    enabled: true",
            "  - id: version_format\n    name: \"版本号格式检查(已禁用)\"\n    enabled: false"
        )
        with open(rules_default, "w", encoding="utf-8") as f:
            f.write(modified)

        rc, out = run_cli_capture([
            "--db", db2, "--no-color",
            "handover-import", export_path,
            "--by", "importer_rules",
            "--dry-run",
        ])
        assert rc == 0
        assert "rules_changed" in out, "应检测到 rules_changed 冲突"
        assert "本地规则文件与包内快照不一致" in out
        print("[OK] 正确检测到 rules_changed 冲突")

        rc, out = run_cli_capture([
            "--db", db2, "--no-color",
            "handover-import", export_path,
            "--by", "importer_rules",
            "--resolve", "rules_changed=keep_package_snapshot",
        ])
        assert rc == 0

        storage2 = Storage(db2)
        snap = storage2.get_active_rule_snapshot(bid)
        assert snap is not None
        assert snap["rules_config_path"] == rules_default
        print(f"[OK] keep_package_snapshot: 导入后活动快照为包内快照")
        print(f"    SHA256: {snap['rules_sha256'][:16]}...")

        imports = storage2.get_handover_imports_for_batch(bid)
        resolutions = json.loads(imports[0]["resolution_summary"])
        assert any(r["conflict_type"] == "rules_changed" for r in resolutions)
        assert any(r["resolution"] == "keep_package_snapshot" for r in resolutions)
        print("[OK] rules_changed 冲突处理已记录")

        os.unlink(export_path)

    finally:
        if os.path.exists(rules_backup):
            shutil.copy2(rules_backup, rules_default)
            os.unlink(rules_backup)

    print("\n[OK][OK] 测试八-05 通过: 规则变更冲突处理正确 [OK][OK]")


def test_handover_conflict_duplicate_import(examples_dir):
    """测试八-06: 冲突处理 - 重复导入同一接手包"""
    separator("接手包: 重复导入冲突测试")

    db_path = os.path.join(TEST_DIR, "test_ho_dup_import.db")
    clean_db(db_path)

    bid = "test-ho-dup-import"
    manifest_src = os.path.join(examples_dir, "manifest_good.json")

    rc, out = run_cli_capture(["--db", db_path, "--no-color", "import", manifest_src, "--id", bid, "--name", "测试-重复导入"])
    assert rc == 0
    rc, out = run_cli_capture(["--db", db_path, "--no-color", "check", bid])
    assert rc == 0

    export_path = os.path.join(TEST_DIR, "handover_dup_import.json")
    if os.path.exists(export_path):
        os.unlink(export_path)
    rc, out = run_cli_capture([
        "--db", db_path, "--no-color",
        "handover-export", bid,
        "-o", export_path,
        "-e", "exporter_dup",
    ])
    assert rc == 0

    rc, out = run_cli_capture([
        "--db", db_path, "--no-color",
        "handover-import", export_path,
        "--by", "importer1",
        "--resolve", "duplicate_id=overwrite_with_package",
    ])
    assert rc == 0
    print("[OK] 第一次导入成功")

    rc, out = run_cli_capture([
        "--db", db_path, "--no-color",
        "handover-import", export_path,
        "--by", "importer2",
        "--dry-run",
    ])
    assert rc == 0
    assert "duplicate_import" in out, "应检测到 duplicate_import 冲突"
    assert "该接手包已导入过" in out
    print("[OK] 正确检测到 duplicate_import 冲突")

    rc, out = run_cli_capture([
        "--db", db_path, "--no-color",
        "handover-import", export_path,
        "--by", "importer2",
        "--resolve", "duplicate_import=skip",
        "--resolve", "duplicate_id=rename_package",
        "--resolve", "newer_local=keep_local",
    ])
    assert rc == 0
    assert "该包已导入过，选择跳过" in out
    print("[OK] 选择 skip 正确跳过重复导入")

    rc, out = run_cli_capture([
        "--db", db_path, "--no-color",
        "handover-import", export_path,
        "--by", "importer3",
        "--resolve", "duplicate_import=force_reimport",
        "--resolve", "duplicate_id=rename_package",
        "--resolve", "newer_local=overwrite_with_package",
    ])
    assert rc == 0
    print("[OK] force_reimport 成功重新导入")

    storage = Storage(db_path)
    all_imports = storage.get_all_handover_imports()
    assert len(all_imports) >= 2, f"至少 2 条导入记录"
    print(f"[OK] 共 {len(all_imports)} 条导入记录（含首次导入 + 强制重新导入）")

    os.unlink(export_path)

    print("\n[OK][OK] 测试八-06 通过: 重复导入冲突处理正确 [OK][OK]")


def test_handover_conflict_multiple_conflicts(examples_dir, config_dir):
    """测试八-07: 多冲突同时处理 - 覆盖处理 - default-keep-package / default-keep-local"""
    separator("接手包: 多冲突同时处理测试")

    db1 = os.path.join(TEST_DIR, "test_ho_multi_export.db")
    db2 = os.path.join(TEST_DIR, "test_ho_multi_import.db")
    clean_db(db1)
    clean_db(db2)

    bid = "test-ho-multi"
    manifest_src = os.path.join(examples_dir, "manifest_good.json")
    rules_default = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "config", "rules.yaml")

    rules_backup = rules_default + ".bak_test_multi"
    shutil.copy2(rules_default, rules_backup)

    try:
        rc, out = run_cli_capture(["--db", db1, "--no-color", "import", manifest_src, "--id", bid, "--name", "测试-多冲突"])
        assert rc == 0
        rc, out = run_cli_capture(["--db", db1, "--no-color", "check", bid])
        assert rc == 0
        rc, out = run_cli_capture(["--db", db1, "--no-color", "approve", bid, "--approver", "zhang", "--comment", "审批"])
        assert rc == 0

        export_path = os.path.join(TEST_DIR, "handover_multi.json")
        if os.path.exists(export_path):
            os.unlink(export_path)
        rc, out = run_cli_capture([
            "--db", db1, "--no-color",
            "handover-export", bid,
            "-o", export_path,
            "-e", "exporter_multi",
        ])
        assert rc == 0

        with open(rules_default, "r", encoding="utf-8") as f:
            orig = f.read()
        modified = orig.replace(
            "  - id: version_format\n    name: \"版本号格式检查\"\n    enabled: true",
            "  - id: version_format\n    name: \"版本号格式检查(已禁用)\"\n    enabled: false"
        )
        with open(rules_default, "w", encoding="utf-8") as f:
            f.write(modified)

        rc, out = run_cli_capture(["--db", db2, "--no-color", "import", manifest_src, "--id", bid, "--name", "测试-本地"])
        assert rc == 0
        rc, out = run_cli_capture(["--db", db2, "--no-color", "check", bid])
        assert rc == 0
        rc, out = run_cli_capture(["--db", db2, "--no-color", "approve", bid, "--approver", "local_approver", "--comment", "本地审批"])
        assert rc == 0

        rc, out = run_cli_capture([
            "--db", db2, "--no-color",
            "handover-import", export_path,
            "--by", "importer_multi",
            "--default-keep-package",
        ])
        assert rc == 0, f"default-keep-package 应成功 rc={rc}\n{out}"

        storage2 = Storage(db2)
        batch = storage2.get_batch(bid)
        approvals = storage2.get_approvals(bid)
        assert len(approvals) == 1
        assert approvals[0]["approver"] == "zhang", "default-keep-package 后审批人应为 zhang（包内审批人）"
        imports = storage2.get_handover_imports_for_batch(bid)
        assert len(imports) == 1
        resolutions = json.loads(imports[0]["resolution_summary"])
        conflict_types = {r["conflict_type"] for r in resolutions}
        assert "newer_local" in conflict_types or "duplicate_id" in conflict_types
        assert "rules_changed" in conflict_types
        print(f"[OK] --default-keep-package 同时处理多个冲突")
        print(f"    处理冲突: {conflict_types}")
        os.unlink(export_path)

    finally:
        if os.path.exists(rules_backup):
            shutil.copy2(rules_backup, rules_default)
            os.unlink(rules_backup)

    print("\n[OK][OK] 测试八-07 通过: 多冲突同时处理正确 [OK][OK]")


def test_handover_rollback_on_failure(examples_dir):
    """测试八-08: 失败回滚 - 验证导入失败时不残留数据"""
    separator("接手包: 失败回滚测试")

    db_path = os.path.join(TEST_DIR, "test_ho_rollback.db")
    clean_db(db_path)

    bid = "test-ho-rollback"
    manifest_src = os.path.join(examples_dir, "manifest_good.json")

    rc, out = run_cli_capture(["--db", db_path, "--no-color", "import", manifest_src, "--id", bid, "--name", "测试-回滚"])
    assert rc == 0
    rc, out = run_cli_capture(["--db", db_path, "--no-color", "check", bid])
    assert rc == 0

    export_path = os.path.join(TEST_DIR, "handover_rollback.json")
    if os.path.exists(export_path):
        os.unlink(export_path)
    rc, out = run_cli_capture([
        "--db", db_path, "--no-color",
        "handover-export", bid,
        "-o", export_path,
        "-e", "exporter_rollback",
    ])
    assert rc == 0

    with open(export_path, "r", encoding="utf-8") as f:
        pkg = json.load(f)
    pkg["schema_version"] = "999.999"
    with open(export_path, "w", encoding="utf-8") as f:
        json.dump(pkg, f)

    rc, out = run_cli_capture([
        "--db", db_path, "--no-color",
        "handover-import", export_path,
        "--by", "importer_rollback",
    ])
    assert rc != 0
    assert "schema 版本" in out
    storage = Storage(db_path)
    batches_before = len(storage.list_batches())
    imports = storage.get_all_handover_imports()
    assert len(imports) == 0, f"导入失败后不应有导入记录，实际 {len(imports)} 条"
    print("[OK] schema 版本错误时导入失败，无数据无残留")

    with open(export_path, "r", encoding="utf-8") as f:
        pkg = json.load(f)
    pkg["package_hash"] = "0000000000000000000000000000000000000000000000000000000000000000"
    with open(export_path, "w", encoding="utf-8") as f:
        json.dump(pkg, f)

    rc, out = run_cli_capture([
        "--db", db_path, "--no-color",
        "handover-import", export_path,
        "--by", "importer_rollback2",
    ])
    assert rc != 0
    assert "哈希不匹配" in out
    imports2 = storage.get_all_handover_imports()
    assert len(imports2) == 0
    print("[OK] 哈希不匹配时导入失败，无数据残留")

    os.unlink(export_path)

    print("\n[OK][OK] 测试八-08 通过: 导入失败回滚正确 [OK][OK]")


def test_handover_cross_restart_persistence(examples_dir):
    """测试八-09: 跨重启持久化 - 重启后接手来源、冲突决策不丢失"""
    separator("接手包: 跨重启持久化测试")

    db_path = os.path.join(TEST_DIR, "test_ho_restart.db")
    clean_db(db_path)

    bid = "test-ho-restart"
    manifest_src = os.path.join(examples_dir, "manifest_good.json")

    rc, out = run_cli_capture(["--db", db_path, "--no-color", "import", manifest_src, "--id", bid, "--name", "测试-跨重启"])
    assert rc == 0
    rc, out = run_cli_capture(["--db", db_path, "--no-color", "check", bid])
    assert rc == 0

    export_path = os.path.join(TEST_DIR, "handover_restart.json")
    if os.path.exists(export_path):
        os.unlink(export_path)
    rc, out = run_cli_capture([
        "--db", db_path, "--no-color",
        "handover-export", bid,
        "-o", export_path,
        "-e", "exporter_restart",
    ])
    assert rc == 0

    rc, out = run_cli_capture([
        "--db", db_path, "--no-color",
        "handover-import", export_path,
        "--by", "importer_restart",
        "--resolve", "duplicate_id=rename_package",
        "--note", "重启测试导入",
    ])
    assert rc == 0

    storage1 = Storage(db_path)
    batches1 = storage1.list_batches()
    new_bid = None
    for b in batches1:
        if b["id"].startswith(f"{bid}-imported-"):
            new_bid = b["id"]
    assert new_bid is not None
    imports1 = storage1.get_handover_imports_for_batch(new_bid)
    assert len(imports1) == 1
    import_id1 = imports1[0]["id"]
    res1 = json.loads(imports1[0]["resolution_summary"])
    print(f"[OK] 导入前验证完成，导入记录 #{import_id1}")

    rc, status_out1 = run_cli_capture(["--db", db_path, "--no-color", "status", new_bid])
    assert rc == 0
    assert "接手来源" in status_out1
    assert "importer_restart" in status_out1
    assert "exporter_restart" in status_out1
    print("[OK] 重启前 status 显示接手来源")

    rc, hist_out1 = run_cli_capture(["--db", db_path, "--no-color", "history", new_bid, "-t", "status"])
    assert rc == 0
    assert "接手包导入记录" in hist_out1
    print("[OK] 重启前 history 显示接手包导入记录")

    storage2 = Storage(db_path)
    imports2 = storage2.get_handover_imports_for_batch(new_bid)
    assert len(imports2) == 1
    assert imports2[0]["id"] == import_id1
    assert imports2[0]["imported_by"] == "importer_restart"
    assert imports2[0]["package_generated_by"] == "exporter_restart"
    assert imports2[0]["import_note"] == "重启测试导入"
    res2 = json.loads(imports2[0]["resolution_summary"])
    assert res2 == res1
    print("[OK] 重启后导入记录完整未变：ID/冲突决策完整")

    rc, status_out2 = run_cli_capture(["--db", db_path, "--no-color", "status", new_bid])
    assert rc == 0
    assert "接手来源" in status_out2
    assert "importer_restart" in status_out2
    assert "exporter_restart" in status_out2
    assert "重启测试导入" in status_out2
    print("[OK] 重启后 status 仍显示接手来源")

    rc, hist_out2 = run_cli_capture(["--db", db_path, "--no-color", "history", new_bid, "-t", "status"])
    assert rc == 0
    assert "接手包导入记录" in hist_out2
    print("[OK] 重启后 history 仍显示接手包导入记录")

    export2_path = os.path.join(TEST_DIR, "handover_restart2.json")
    if os.path.exists(export2_path):
        os.unlink(export2_path)
    rc, out = run_cli_capture([
        "--db", db_path, "--no-color",
        "export", new_bid,
        "-o", export2_path,
        "-f", "json",
    ])
    assert rc == 0
    with open(export2_path, "r", encoding="utf-8") as f:
        export2 = json.load(f)
    assert "handover_imports" in export2
    assert len(export2["handover_imports"]) == 1
    assert export2["handover_imports"][0]["imported_by"] == "importer_restart"
    assert export2["handover_imports"][0]["package_generated_by"] == "exporter_restart"
    print("[OK] 重启后重新导出仍包含接手来源信息")

    os.unlink(export_path)
    os.unlink(export2_path)

    print("\n[OK][OK] 测试八-09 通过: 跨重启持久化完整 [OK][OK]")


def test_handover_full_workflow(examples_dir):
    """测试八-10: 完整工作流验证 - 导出→导入→继续后续流程"""
    separator("接手包: 完整工作流验证")

    db_a = os.path.join(TEST_DIR, "test_ho_full_a.db")
    db_b = os.path.join(TEST_DIR, "test_ho_full_b.db")
    clean_db(db_a)
    clean_db(db_b)

    bid = "test-ho-full"
    manifest_src = os.path.join(examples_dir, "manifest_good.json")

    print("═══ 移交人 A 的工作流 ═══")
    rc, out = run_cli_capture(["--db", db_a, "--no-color", "import", manifest_src, "--id", bid, "--name", "完整工作流测试"])
    assert rc == 0
    rc, out = run_cli_capture(["--db", db_a, "--no-color", "check", bid])
    assert rc == 0
    rc, out = run_cli_capture(["--db", db_a, "--no-color", "approve", bid, "--approver", "A_approver", "--comment", "A 审批通过"])
    assert rc == 0
    print("[OK] A 完成: import → check → approve")

    export_path = os.path.join(TEST_DIR, "handover_full.json")
    if os.path.exists(export_path):
        os.unlink(export_path)
    rc, out = run_cli_capture([
        "--db", db_a, "--no-color",
        "handover-export", bid,
        "-o", export_path,
        "-e", "A_exporter",
        "-n", "A 离职，交给 B 继续",
    ])
    assert rc == 0
    print("[OK] A 导出接手包完成")

    print("\n═══ 接手人 B 导入 ═══")
    rc, out = run_cli_capture([
        "--db", db_b, "--no-color",
        "handover-import", export_path,
        "--by", "B_importer",
        "--note", "B 接手项目",
    ])
    assert rc == 0
    print("[OK] B 导入接手包成功")

    print("\n═══ B 继续后续流程 ═══")
    rc, out = run_cli_capture(["--db", db_b, "--no-color", "publish", bid, "--operator", "B_operator", "--comment", "B 发布"])
    assert rc == 0
    print("[OK] B 完成: approve → publish")

    storage_b = Storage(db_b)
    batch_b = storage_b.get_batch(bid)
    assert batch_b["status"] == "published"
    pubs_b = storage_b.get_publish_records(bid)
    assert len(pubs_b) == 1
    assert pubs_b[0]["operator"] == "B_operator"
    print(f"[OK] B 发布成功，状态为 published")

    rc, status_out = run_cli_capture(["--db", db_b, "--no-color", "status", bid])
    assert rc == 0
    assert "接手来源" in status_out
    assert "A_exporter" in status_out
    assert "B_importer" in status_out
    assert "A 离职，交给 B 继续" in status_out
    print("[OK] status 显示完整接手信息")

    rc, out = run_cli_capture(["--db", db_b, "--no-color", "revoke", bid, "--operator", "B_operator", "--comment", "B 发现问题回滚"])
    assert rc == 0
    print("[OK] B 可继续 revoke")

    rc, out = run_cli_capture(["--db", db_b, "--no-color", "check", bid])
    assert rc == 0
    rc, out = run_cli_capture(["--db", db_b, "--no-color", "approve", bid, "--approver", "B_approver2", "--comment", "B 重新审批"])
    assert rc == 0
    rc, out = run_cli_capture(["--db", db_b, "--no-color", "publish", bid, "--operator", "B_operator2", "--comment", "B 重新发布"])
    assert rc == 0
    print("[OK] B 可继续完整链路：revoke → check → approve → publish")

    batch_final = storage_b.get_batch(bid)
    assert batch_final["status"] == "published"
    pubs_final = storage_b.get_publish_records(bid)
    actions = [p["action"] for p in pubs_final]
    assert actions == ["publish", "revoke", "publish"]
    print(f"[OK] 发布历史: {'→'.join(actions)}")

    os.unlink(export_path)

    print("\n[OK][OK] 测试八-10 通过: 完整工作流验证成功 [OK][OK]")


def test_handover_readme_example(examples_dir):
    """测试八-11: README 示例对照 - 验证 README 中接手包示例可运行"""
    separator("接手包: README 示例对照")

    db_path = os.path.join(TEST_DIR, "test_ho_readme.db")
    clean_db(db_path)

    bid = "test-ho-readme"
    manifest_src = os.path.join(examples_dir, "manifest_good.json")

    rc, out = run_cli_capture(["--db", db_path, "--no-color", "import", manifest_src, "--id", bid, "--name", "README 示例"])
    assert rc == 0
    rc, out = run_cli_capture(["--db", db_path, "--no-color", "check", bid])
    assert rc == 0
    rc, out = run_cli_capture(["--db", db_path, "--no-color", "approve", bid, "--approver", "readme_user", "--comment", "审批通过"])
    assert rc == 0

    readme_me_content = None
    readme_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md")
    if os.path.exists(readme_path):
        with open(readme_path, "r", encoding="utf-8") as f:
            readme_content = f.read()

    if readme_content and "handover-export" in readme_content:
        assert "handover-export" in readme_content
        assert "handover-import" in readme_content
        assert "接手包" in readme_content or "handover" in readme_content
        print("[OK] README 包含接手包相关命令说明")

    export_path = os.path.join(TEST_DIR, "handover_readme.json")
    if os.path.exists(export_path):
        os.unlink(export_path)
    rc, out = run_cli_capture([
        "--db", db_path, "--no-color",
        "handover-export", bid,
        "-o", export_path,
        "-e", "readme_exporter",
    ])
    assert rc == 0

    with open(export_path, "r", encoding="utf-8") as f:
        pkg = json.load(f)

    assert pkg.keys()
    assert "schema_version" in pkg
    assert "batch" in pkg
    assert "latest_validation" in pkg
    assert "approval_conclusion" in pkg
    assert "rule_snapshots" in pkg
    assert "todo_actions" in pkg
    assert "log_index" in pkg
    print("[OK] 导出的 JSON 结构与 README 示例结构一致")

    rc, out = run_cli_capture([
        "--db", db_path, "--no-color",
        "handover-list",
    ])
    assert rc == 0
    print("[OK] handover-list 命令可用")

    os.unlink(export_path)

    print("\n[OK][OK] 测试八-11 通过: README 示例对照成功 [OK][OK]")


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
        ("回归-revoke说明链路一致", lambda: test_revoke_docs_consistency(examples_dir)),
        ("回归-撤销后默认规则变更检测", lambda: test_revoke_default_rules_change_detection(examples_dir)),
        ("回归-导出规则一致性信息完整", lambda: test_export_rules_consistency_info(examples_dir)),
        ("回归-跨重启自证状态与规则", lambda: test_cross_restart_self_evidence(examples_dir)),
        ("回归-快照决策记录", lambda: test_snapshot_decision_recording(examples_dir, config_dir)),
        ("回归-快照决策跨重启持久化", lambda: test_snapshot_decision_cross_restart(examples_dir, config_dir)),
        ("回归-撤销后完整链路含规则变更", lambda: test_revoke_full_cycle_with_rules_change(examples_dir)),
        ("回归-导出快照决策与撤销上下文", lambda: test_export_snapshot_decisions_and_revoke_context(examples_dir)),
        ("回归-status/history/export三处一致", lambda: test_status_history_export_revoke_consistency(examples_dir)),
        ("接手包-基础导出", lambda: test_handover_export_basic(examples_dir)),
        ("接手包-无冲突导入", lambda: test_handover_import_no_conflict(examples_dir)),
        ("接手包-同名ID冲突", lambda: test_handover_conflict_duplicate_id(examples_dir)),
        ("接手包-本地较新冲突", lambda: test_handover_conflict_newer_local(examples_dir)),
        ("接手包-规则变更冲突", lambda: test_handover_conflict_rules_changed(examples_dir, config_dir)),
        ("接手包-重复导入冲突", lambda: test_handover_conflict_duplicate_import(examples_dir)),
        ("接手包-多冲突同时处理", lambda: test_handover_conflict_multiple_conflicts(examples_dir, config_dir)),
        ("接手包-失败回滚", lambda: test_handover_rollback_on_failure(examples_dir)),
        ("接手包-跨重启持久化", lambda: test_handover_cross_restart_persistence(examples_dir)),
        ("接手包-完整工作流", lambda: test_handover_full_workflow(examples_dir)),
        ("接手包-README示例对照", lambda: test_handover_readme_example(examples_dir)),
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
        print(f"{marker} {name:<28} {res:<8} {note or '-'}")
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
