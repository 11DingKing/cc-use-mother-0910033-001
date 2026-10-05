"""借调领域模型。状态取值遵循 domain/contract.json 的约定。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime


class Role(str, enum.Enum):
    """契约角色。"""

    OPERATOR = "operator"  # 文博中心运营员
    VOLUNTEER = "volunteer"  # 志愿者
    GUARDIAN = "guardian"  # 监护人
    VENUE_MANAGER = "venue_manager"  # 场馆负责人


class LifecycleState(str, enum.Enum):
    """契约约定的借调生命周期状态（请求与协议共用）。"""

    DRAFT = "草拟"
    VERIFYING = "待核验"
    CONFIRMED = "已确认"
    IN_PROGRESS = "执行中"
    ARCHIVED = "已归档"


class InvitationState(str, enum.Enum):
    """候选邀请状态。"""

    PENDING = "待确认"
    CONFIRMED = "已确认"
    DECLINED = "已拒绝"
    CONFLICT = "冲突"
    EXPIRED = "已失效"


class ShiftState(str, enum.Enum):
    """原排班状态。"""

    ACTIVE = "在班"
    RELEASED = "已迁移"


class OccupancyState(str, enum.Enum):
    """借调占用状态。"""

    ACTIVE = "生效中"
    RECALLED = "已召回"
    COMPLETED = "已完成"


SLOTS = ("am", "pm", "full")


def slots_overlap(a: str, b: str) -> bool:
    """两个时段是否冲突（full 与任何时段冲突）。"""
    return a == b or a == "full" or b == "full"


@dataclass(frozen=True)
class Actor:
    """接口调用者身份。"""

    role: Role
    actor_id: str
    venue_id: str | None = None  # 场馆负责人所属场馆
    volunteer_id: str | None = None  # 志愿者本人


@dataclass
class Venue:
    venue_id: str
    name: str


@dataclass
class CapacityRule:
    """接收方某日可接纳的借调人数上限。"""

    venue_id: str
    day: date
    max_loaned: int


@dataclass
class Guardianship:
    """监护限制。"""

    guardian_id: str | None = None
    guardian_name: str | None = None
    requires_consent: bool = False
    allowed_venue_ids: frozenset[str] | None = None  # None 表示不限场馆
    forbidden_slots: frozenset[str] = frozenset()
    max_consecutive_days: int | None = None


@dataclass
class Volunteer:
    volunteer_id: str
    name: str
    home_venue_id: str
    skills: dict[str, int]  # 技能 -> 等级
    sensitive: dict[str, str]  # 敏感资料：电话、证件号、紧急联系人等
    guardianship: Guardianship = field(default_factory=Guardianship)
    active: bool = True


@dataclass
class Shift:
    """原场馆排班。"""

    shift_id: str
    volunteer_id: str
    venue_id: str
    day: date
    slot: str
    state: ShiftState = ShiftState.ACTIVE
    released_by: str | None = None  # 迁移该占用的借调协议编号


@dataclass
class LoanRequest:
    """借调请求。"""

    request_id: str
    receiving_venue_id: str
    required_skills: dict[str, int]
    dates: tuple[date, ...]
    slot: str
    headcount: int
    travel_policy: str  # 差旅安排说明
    authorization_scope: tuple[str, ...]  # 授权范围
    state: LifecycleState = LifecycleState.DRAFT
    version: int = 0


@dataclass
class Confirmation:
    """一方对邀请的确认。"""

    party: str  # home_venue / receiving_venue / guardian
    accepted_dates: frozenset[date] | None  # 监护人确认为 None（对整个邀请同意）
    at: datetime
    by: str


@dataclass
class Invitation:
    """针对某志愿者的候选邀请（双边确认单）。"""

    invitation_id: str
    request_id: str
    volunteer_id: str
    offered_dates: tuple[date, ...]
    state: InvitationState = InvitationState.PENDING
    confirmations: dict[str, Confirmation] = field(default_factory=dict)
    conflict_reasons: list[dict] = field(default_factory=list)
    agreement_id: str | None = None
    declined_by: str | None = None
    decline_reason: str | None = None
    version: int = 0


@dataclass
class RecallRecord:
    dates: tuple[date, ...]
    reason: str
    by: str
    at: datetime


@dataclass
class LoanAgreement:
    """借调协议：双方确认后原子迁移占用的结果。"""

    agreement_id: str
    request_id: str
    invitation_id: str
    volunteer_id: str
    home_venue_id: str
    receiving_venue_id: str
    dates: tuple[date, ...]
    slot: str
    travel_plan: dict  # 差旅安排
    authorization_scope: tuple[str, ...]  # 授权范围
    state: LifecycleState = LifecycleState.CONFIRMED
    archive_reason: str | None = None  # completed / recalled
    recalls: list[RecallRecord] = field(default_factory=list)
    version: int = 0

    @property
    def recalled_dates(self) -> frozenset[date]:
        return frozenset(d for record in self.recalls for d in record.dates)

    @property
    def remaining_dates(self) -> tuple[date, ...]:
        return tuple(d for d in self.dates if d not in self.recalled_dates)


@dataclass
class Occupancy:
    """迁移后的借调占用（消耗接收方容量）。"""

    occupancy_id: str
    agreement_id: str
    volunteer_id: str
    venue_id: str
    day: date
    slot: str
    state: OccupancyState = OccupancyState.ACTIVE
