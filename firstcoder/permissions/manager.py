"""权限统一决策入口。"""

from __future__ import annotations

from firstcoder.permissions.grants import PermissionGrantStore
from firstcoder.permissions.policy import DefaultPermissionPolicy
from firstcoder.permissions.types import PermissionDecision, PermissionMode, PermissionRequest


class PermissionManager:
    """组合长期授权和默认策略。

    后续阶段的用户确认、pending tool execution 和持久化都会接在这个入口后面；
    第一阶段先保证所有权限请求都能通过同一个纯函数式预检路径。
    """

    def __init__(
        self,
        *,
        policy: DefaultPermissionPolicy,
        grants: PermissionGrantStore | None = None,
        mode: PermissionMode = PermissionMode.STANDARD,
    ) -> None:
        self.policy = policy
        self.grants = grants or PermissionGrantStore()
        self.mode = mode

    def preflight(self, request: PermissionRequest) -> PermissionDecision:
        grant_decision = self.grants.matching_decision(request)
        if grant_decision is not None:
            return grant_decision
        return self.policy.decide(request, mode=self.mode)
