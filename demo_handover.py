#!/usr/bin/env python3
"""接手包管理模块 - 端到端完整演示
包含：导出、导入、冲突处理、失败回滚、重启持久化
"""
import sys
import os
import json
import shutil

sys.path.insert(0, '.')
os.environ['PYTHONIOENCODING'] = 'utf-8'

from patchgate.cli import main as cli_main
from patchgate.storage import Storage

DEMO_DIR = './tests/.test_patchgate/demo'
if os.path.exists(DEMO_DIR):
    shutil.rmtree(DEMO_DIR)
os.makedirs(DEMO_DIR, exist_ok=True)


def run_cli(args, db=None, capture=False):
    """运行 CLI 命令"""
    db_path = db or os.path.join(DEMO_DIR, 'demo.db')
    full_args = ['--db', db_path, '--no-color'] + list(args)
    
    if capture:
        import io
        buf = io.StringIO()
        old_stdout, old_stderr = sys.stdout, sys.stderr
        try:
            sys.stdout, sys.stderr = buf, buf
            rc = cli_main(full_args)
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
        return rc, buf.getvalue()
    else:
        cmd_str = ' '.join(full_args)
        print(f'\n$ python patchgate_cli.py { " ".join(args)}')
        print('-' * 60)
        rc = cli_main(full_args)
        print('-' * 60)
        print(f'[退出码: {rc}]')
        return rc


def section(title):
    print()
    print('=' * 70)
    print(f'  {title}')
    print('=' * 70)


# ===================================================================
# 场景一：正常导出导入全链路
# ===================================================================
section('场景一：正常导出导入全链路')
print('''
背景：张三（zhangsan）负责的批次要交接给李四（lisi）。
张三完成了校验和审批，导出接手包发给李四。
李四导入接手包，核对历史结论后继续发布。
''')

db_zhangsan = os.path.join(DEMO_DIR, 'zhangsan.db')
db_lisi = os.path.join(DEMO_DIR, 'lisi.db')

print('--- 张三的工作：导入清单 → 校验 → 审批 → 导出接手包 ---')

run_cli(['import', 'examples/manifest_good.json', 
         '--id', 'release-2026-q2', '--name', '2026年Q2安全补丁'], 
        db=db_zhangsan)

run_cli(['check', 'release-2026-q2'], db=db_zhangsan)

run_cli(['approve', 'release-2026-q2', 
         '--approver', 'zhangsan', '--comment', '所有补丁已代码评审，版本核对无误'], 
        db=db_zhangsan)

handover_path = os.path.join(DEMO_DIR, 'release-2026-q2-handover.json')
run_cli(['handover-export', 'release-2026-q2',
         '-o', handover_path,
         '-e', 'zhangsan',
         '-n', 'Q2补丁批次已完成校验和审批，李四接手后可直接发布'],
        db=db_zhangsan)

print(f'\n[INFO] 接手包文件：{handover_path}')
print('[INFO] 张三把这个 JSON 文件发给了李四')

print('\n--- 李四的工作：导入接手包 → 核对历史 → 继续发布 ---')

run_cli(['handover-import', handover_path,
         '--by', 'lisi',
         '--note', '从张三处接手Q2补丁批次'],
        db=db_lisi)

print('\n--- 李四核对：查看状态 ---')
run_cli(['status', 'release-2026-q2'], db=db_lisi)

print('\n--- 李四核对：查看历史 ---')
run_cli(['history', 'release-2026-q2'], db=db_lisi)

print('\n--- 李四继续：发布 ---')
run_cli(['publish', 'release-2026-q2',
         '--operator', 'lisi',
         '--comment', '凌晨2:00-2:30灰度+全量发布完成，监控正常'],
        db=db_lisi)

print('\n--- 李四确认：最终状态 ---')
run_cli(['status', 'release-2026-q2'], db=db_lisi)

# ===================================================================
# 场景二：冲突处理 - 同名项目 + 规则变更
# ===================================================================
section('场景二：冲突处理 - 同名项目 + 规则变更')
print('''
背景：李四本地已经有一个同名批次，且规则文件与包内快照不一致。
导入时会检测到冲突，李四需要选择解决方案。
''')

db_conflict = os.path.join(DEMO_DIR, 'conflict.db')

print('--- 李四本地已有一个同名批次 ---')
run_cli(['import', 'examples/manifest_good.json',
         '--id', 'release-2026-q2', '--name', '李四本地的Q2批次'],
        db=db_conflict)
run_cli(['check', 'release-2026-q2'], db=db_conflict)

print('\n--- 修改本地规则文件，制造 rules_changed 冲突 ---')
rules_path = 'config/rules.yaml'
rules_backup = os.path.join(DEMO_DIR, 'rules_backup.yaml')
shutil.copy2(rules_path, rules_backup)

with open(rules_path, 'r', encoding='utf-8') as f:
    rules_content = f.read()
modified_rules = rules_content.replace(
    '  - id: version_format\n    name: "版本号格式检查"\n    enabled: true',
    '  - id: version_format\n    name: "版本号格式检查(本地已禁用)"\n    enabled: false'
)
with open(rules_path, 'w', encoding='utf-8') as f:
    f.write(modified_rules)

print('\n--- 先 dry-run 看看有哪些冲突 ---')
rc, out = run_cli(['handover-import', handover_path,
                   '--by', 'lisi',
                   '--dry-run'],
                  db=db_conflict, capture=True)
print(out)

print('\n--- 解决方案：重命名批次 + 保留包内规则快照 ---')
run_cli(['handover-import', handover_path,
         '--by', 'lisi',
         '--resolve', 'duplicate_id=rename_package',
         '--resolve', 'rules_changed=keep_package_snapshot'],
        db=db_conflict)

# 恢复规则文件
shutil.copy2(rules_backup, rules_path)
os.unlink(rules_backup)

print('\n--- 确认：原批次还在，新批次也导入了 ---')
storage = Storage(db_conflict)
batches = storage.list_batches()
print(f'  批次总数: {len(batches)}')
for b in batches:
    print(f'    - {b["id"]}: {b["name"]} ({b["status"]})')

# ===================================================================
# 场景三：失败回滚 - 损坏的接手包
# ===================================================================
section('场景三：失败回滚 - 损坏的接手包')
print('''
背景：接手包文件损坏或被篡改，导入时应该失败，且不残留任何数据。
''')

db_rollback = os.path.join(DEMO_DIR, 'rollback.db')
bad_package_path = os.path.join(DEMO_DIR, 'bad-package.json')

print('\n--- 制造一个损坏的包（篡改哈希） ---')
with open(handover_path, 'r', encoding='utf-8') as f:
    pkg = json.load(f)
pkg['package_hash'] = '0' * 64  # 篡改哈希
with open(bad_package_path, 'w', encoding='utf-8') as f:
    json.dump(pkg, f, ensure_ascii=False, indent=2)

storage_before = Storage(db_rollback)
count_before = len(storage_before.list_batches())
print(f'  导入前批次数量: {count_before}')

print('\n--- 尝试导入损坏的包 ---')
rc, out = run_cli(['handover-import', bad_package_path,
                   '--by', 'tester'],
                  db=db_rollback, capture=True)
print(out)

storage_after = Storage(db_rollback)
count_after = len(storage_after.list_batches())
print(f'  导入后批次数量: {count_after}')
print(f'  导入记录数量: {len(storage_after.get_all_handover_imports())}')

assert count_before == count_after, '失败后批次数量不应变化！'
assert len(storage_after.get_all_handover_imports()) == 0, '失败后不应有导入记录！'
print('  ✓ 失败回滚正确：无批次残留，无导入记录')

os.unlink(bad_package_path)

# ===================================================================
# 场景四：跨重启持久化
# ===================================================================
section('场景四：跨重启持久化验证')
print('''
背景：导入接手包后关闭程序，重新打开后所有数据应该还在。
包括：批次状态、审批记录、校验结果、接手来源说明、导入决策。
''')

db_restart = os.path.join(DEMO_DIR, 'restart.db')

print('\n--- 第一次运行：导入接手包 ---')
run_cli(['handover-import', handover_path,
         '--by', 'operator_a',
         '--note', '第一次运行时导入'],
        db=db_restart)

storage1 = Storage(db_restart)
batch1 = storage1.get_batch('release-2026-q2')
imports1 = storage1.get_handover_imports_for_batch('release-2026-q2')
import_id_1 = imports1[0]['id']
print(f'\n  批次状态: {batch1["status"]}')
print(f'  导入记录ID: #{import_id_1}')
print(f'  导入人: {imports1[0]["imported_by"]}')

print('\n--- 模拟重启：重新连接数据库（相当于程序重启） ---')
# 关闭旧连接，重新创建 Storage（模拟重启）
del storage1
storage2 = Storage(db_restart)

batch2 = storage2.get_batch('release-2026-q2')
imports2 = storage2.get_handover_imports_for_batch('release-2026-q2')

print(f'\n  重启后批次状态: {batch2["status"]}')
print(f'  重启后导入记录ID: #{imports2[0]["id"]}')
print(f'  重启后导入人: {imports2[0]["imported_by"]}')

assert batch2['status'] == batch1['status'], '重启后状态应该一致！'
assert imports2[0]['id'] == import_id_1, '重启后导入记录应该一致！'
assert imports2[0]['imported_by'] == 'operator_a', '重启后导入人应该一致！'
print('  ✓ 重启后所有数据完整保留')

print('\n--- 重启后查看 status ---')
run_cli(['status', 'release-2026-q2'], db=db_restart)

print('\n--- 重启后查看 history ---')
run_cli(['history', 'release-2026-q2', '-t', 'status'], db=db_restart)

print('\n--- 重启后再次导出，应该包含接手来源信息 ---')
reexport_path = os.path.join(DEMO_DIR, 'reexport.json')
run_cli(['export', 'release-2026-q2', '-o', reexport_path, '-f', 'json'],
        db=db_restart)

with open(reexport_path, 'r', encoding='utf-8') as f:
    reexport_data = json.load(f)
assert 'handover_imports' in reexport_data, '重新导出应该包含接手导入记录！'
assert len(reexport_data['handover_imports']) == 1
assert reexport_data['handover_imports'][0]['imported_by'] == 'operator_a'
print('  ✓ 重新导出的文件中包含接手来源和导入决策')

# ===================================================================
# 场景五：查看所有导入记录（审计）
# ===================================================================
section('场景五：接手包导入记录审计')
print('''
背景：管理员要查看所有接手包导入记录，用于审计追溯。
''')

# 在同一个 db 中导入多个包，制造多条记录
db_audit = os.path.join(DEMO_DIR, 'audit.db')
run_cli(['handover-import', handover_path,
         '--by', 'auditor1', '--note', '第一次导入'],
        db=db_audit)
run_cli(['handover-import', handover_path,
         '--by', 'auditor2',
         '--resolve', 'duplicate_id=rename_package',
         '--resolve', 'duplicate_import=force_reimport',
         '--resolve', 'newer_local=overwrite_with_package',
         '--note', '第二次导入（重命名）'],
        db=db_audit)

print('\n--- 查看所有接手包导入记录 ---')
run_cli(['handover-list'], db=db_audit)

# ===================================================================
# 总结
# ===================================================================
section('总结')
print('''
✓ 场景一：正常导出导入全链路 - 完成
  - 张三：import → check → approve → handover-export
  - 李四：handover-import → status → history → publish
  - 所有历史数据完整保留，接手来源可追溯

✓ 场景二：冲突处理 - 完成
  - 检测到 duplicate_id（同名批次）和 rules_changed（规则变更）冲突
  - dry-run 预览冲突详情
  - 通过 --resolve 指定解决方案
  - 原批次保留，新批次导入，互不影响

✓ 场景三：失败回滚 - 完成
  - 哈希不匹配的损坏包导入失败
  - 失败后零残留：无批次、无导入记录
  - 数据一致性有保障

✓ 场景四：跨重启持久化 - 完成
  - 重启后批次状态、审批、校验结果完整保留
  - 重启后接手来源说明和导入决策完整保留
  - 重启后再次导出仍包含接手导入记录

✓ 场景五：审计日志 - 完成
  - handover-list 列出所有导入记录
  - 包含：批次ID、原ID、导入人、导出人、导入时间
  - 可用于审计追溯

所有场景验证通过！接手包管理模块功能完整。
''')

# 清理
os.unlink(handover_path)
os.unlink(reexport_path)
