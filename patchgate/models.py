from enum import Enum


class BatchStatus(str, Enum):
    CREATED = "created"
    CHECKING = "checking"
    CHECK_FAILED = "check_failed"
    CHECK_PASSED = "check_passed"
    REJECTED = "rejected"
    APPROVED = "approved"
    PUBLISHED = "published"
    REVOKED = "revoked"


class CheckResultStatus(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ApprovalDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


BATCH_STATUS_FLOW = {
    BatchStatus.CREATED: [BatchStatus.CHECKING],
    BatchStatus.CHECKING: [BatchStatus.CHECK_FAILED, BatchStatus.CHECK_PASSED],
    BatchStatus.CHECK_FAILED: [BatchStatus.CHECKING, BatchStatus.REJECTED],
    BatchStatus.CHECK_PASSED: [BatchStatus.APPROVED, BatchStatus.REJECTED, BatchStatus.CHECKING],
    BatchStatus.REJECTED: [BatchStatus.CHECKING],
    BatchStatus.APPROVED: [BatchStatus.PUBLISHED, BatchStatus.CHECKING],
    BatchStatus.PUBLISHED: [BatchStatus.REVOKED],
    BatchStatus.REVOKED: [BatchStatus.CHECKING, BatchStatus.APPROVED],
}


def can_transition(current: BatchStatus, target: BatchStatus) -> bool:
    return target in BATCH_STATUS_FLOW.get(current, [])
