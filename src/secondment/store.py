"""线程安全的内存数据存储。

所有多步写操作都必须持有 ``Store.lock``（可重入），
确认/召回等原子迁移在同一临界区内完成“校验 + 变更”。
"""
from __future__ import annotations

import threading
from collections import defaultdict
from datetime import date

from .models import (
    Capacity,
    GuardianRestriction,
    Invitation,
    LoanAgreement,
    LoanRequest,
    ScheduleEntry,
    Venue,
    Volunteer,
)


class Store:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.venues: dict[str, Venue] = {}
        self.volunteers: dict[str, Volunteer] = {}
        self.restrictions: dict[str, GuardianRestriction] = {}  # volunteer_id -> 监护限制
        self.entries: dict[str, ScheduleEntry] = {}
        self.capacities: dict[tuple[str, date], Capacity] = {}  # (venue_id, day) -> 容量
        self.requests: dict[str, LoanRequest] = {}
        self.invitations: dict[str, Invitation] = {}
        self.agreements: dict[str, LoanAgreement] = {}
        self.events: list[dict] = []  # 审计事件
        self._seq: dict[str, int] = defaultdict(int)

    def next_id(self, prefix: str) -> str:
        """生成确定性递增编号（须在持锁状态下调用）。"""
        self._seq[prefix] += 1
        return f"{prefix}-{self._seq[prefix]:04d}"
