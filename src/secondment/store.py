"""线程安全的内存存储与共享查询。

候选评估与最终迁移使用同一套查询，保证「提议时看到的冲突」与
「确认时强制执行的冲突」一致。所有写操作必须在 store.lock 内完成。
"""
from __future__ import annotations

import itertools
import threading
from datetime import date

from .models import (
    CapacityRule,
    Invitation,
    InvitationState,
    LoanAgreement,
    LoanRequest,
    Occupancy,
    OccupancyState,
    Shift,
    ShiftState,
    Venue,
    Volunteer,
    slots_overlap,
)

_ACTIVE_INVITATION_STATES = (
    InvitationState.PENDING,
    InvitationState.CONFIRMED,
    InvitationState.CONFLICT,
)


class Store:
    """进程内数据存储；RLock 保证并发确认与迁移的原子性。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._seq = itertools.count(1)
        self.venues: dict[str, Venue] = {}
        self.capacity: dict[tuple[str, date], CapacityRule] = {}
        self.volunteers: dict[str, Volunteer] = {}
        self.shifts: dict[str, Shift] = {}
        self.requests: dict[str, LoanRequest] = {}
        self.invitations: dict[str, Invitation] = {}
        self.agreements: dict[str, LoanAgreement] = {}
        self.occupancies: dict[str, Occupancy] = {}
        self.proposals: dict[str, object] = {}  # request_id -> Proposal

    def new_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._seq):04d}"

    # ---- 共享查询 ----

    def active_occupancy(self, volunteer_id: str, day: date, slot: str) -> Occupancy | None:
        for occ in self.occupancies.values():
            if (
                occ.volunteer_id == volunteer_id
                and occ.day == day
                and occ.state == OccupancyState.ACTIVE
                and slots_overlap(occ.slot, slot)
            ):
                return occ
        return None

    def capacity_usage(self, venue_id: str, day: date) -> int:
        return sum(
            1
            for occ in self.occupancies.values()
            if occ.venue_id == venue_id and occ.day == day and occ.state == OccupancyState.ACTIVE
        )

    def shifts_on(
        self,
        volunteer_id: str,
        days,
        slot: str,
        state: ShiftState = ShiftState.ACTIVE,
    ) -> list[Shift]:
        wanted = set(days)
        return [
            shift
            for shift in self.shifts.values()
            if shift.volunteer_id == volunteer_id
            and shift.day in wanted
            and shift.state == state
            and slots_overlap(shift.slot, slot)
        ]

    def active_invitation(self, request_id: str, volunteer_id: str) -> Invitation | None:
        for inv in self.invitations.values():
            if (
                inv.request_id == request_id
                and inv.volunteer_id == volunteer_id
                and inv.state in _ACTIVE_INVITATION_STATES
            ):
                return inv
        return None

    def agreements_for_request(self, request_id: str) -> list[LoanAgreement]:
        return [a for a in self.agreements.values() if a.request_id == request_id]

    def agreements_for_volunteer(self, volunteer_id: str) -> list[LoanAgreement]:
        return [a for a in self.agreements.values() if a.volunteer_id == volunteer_id]

    def invitations_for_request(self, request_id: str) -> list[Invitation]:
        return [i for i in self.invitations.values() if i.request_id == request_id]

    def occupancies_for_agreement(self, agreement_id: str) -> list[Occupancy]:
        return [o for o in self.occupancies.values() if o.agreement_id == agreement_id]
