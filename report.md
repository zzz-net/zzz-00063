# 发布摘要 - manifest_good.json

## 基本信息

- **批次 ID**: `demo-01`
- **状态**: `published`
- **清单文件**: `D:\workSpace\AI__SPACE\zzz-00063\examples\manifest_good.json`
- **清单摘要**: `aa2fbfa9d4ad2e65`
- **条目数量**: 5
- **创建时间**: 2026-06-18T00:38:10
- **更新时间**: 2026-06-18T00:38:13

## 清单内容

| # | 包名 | 版本 | 源路径 | 校验和 |
|---|------|------|--------|--------|
| 1 | auth-service | 2.4.1 | ./artifacts/auth-service-2.4.1.tar.gz | a1b2c3d4e5f6a7b8... |
| 2 | order-service | 3.1.0 | ./artifacts/order-service-3.1.0.tar.gz | b2c3d4e5f6a7b8c9... |
| 3 | payment-gateway | 1.9.3 | ./artifacts/payment-gateway-1.9.3.tar.gz | c3d4e5f6a7b8c9d0... |
| 4 | user-profile | 5.0.2 | ./artifacts/user-profile-5.0.2.tar.gz | d4e5f6a7b8c9d0e1... |
| 5 | notification-hub | 4.2.0 | ./artifacts/notification-hub-4.2.0.tar.gz | e5f6a7b8c9d0e1f2... |

## 校验摘要

- 总检查: 16，通过: 11，失败: 5，错误: 0，警告: 5，跳过: 0

### 失败明细

| 包名 | 规则 | 级别 | 信息 |
|------|------|------|------|
| auth-service | 源路径存在性检查 | warning | 源路径不存在: ./artifacts/auth-service-2.4.1.tar.gz |
| order-service | 源路径存在性检查 | warning | 源路径不存在: ./artifacts/order-service-3.1.0.tar.gz |
| payment-gateway | 源路径存在性检查 | warning | 源路径不存在: ./artifacts/payment-gateway-1.9.3.tar.gz |
| user-profile | 源路径存在性检查 | warning | 源路径不存在: ./artifacts/user-profile-5.0.2.tar.gz |
| notification-hub | 源路径存在性检查 | warning | 源路径不存在: ./artifacts/notification-hub-4.2.0.tar.gz |

## 审批记录

| 审批人 | 决定 | 备注 | 时间 |
|--------|------|------|------|
| zhangsan | approve | 已核对 | 2026-06-18T00:38:12 |

## 发布与回退历史

| 操作人 | 动作 | 备注 | 时间 |
|--------|------|------|------|
| lisi | publish | - | 2026-06-18T00:38:13 |

## 状态流转

| 从 | 到 | 操作人 | 备注 | 时间 |
|----|----|--------|------|------|
| - | created | system | batch created | 2026-06-18T00:38:10 |
| created | checking | system | 开始规则校验 | 2026-06-18T00:38:11 |
| checking | check_passed | system | 校验通过: 11 项通过, 5 个警告 | 2026-06-18T00:38:11 |
| check_passed | approved | zhangsan | 已核对 | 2026-06-18T00:38:12 |
| approved | published | lisi | 发布完成 | 2026-06-18T00:38:13 |

_导出时间: 2026-06-18T00:38:13_