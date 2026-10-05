"""跨场馆志愿者借调领域模型。

与 domain/contract.json 对齐：角色（文博中心运营员/志愿者/监护人/场馆负责人）、
请求生命周期状态（草拟/待核验/已确认/执行中/已归档）。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime


class Role(str, enum.Enum):
    """契约中的四类参与者。"""

    OPERATOR = "operator"  # 文博中心运营员
    VOLUNTEER = "volunteer"  # 志愿者
    GUARDIAN = "guardian"  # 监护人
    VENUE_MANAGER = "venue_manager"  # 场馆负责人


ROLE_LABELS = {
    Role.OPERATOR: "文博中心运营员",
    Role.VOLUNTEER: "志愿者",
    Role.GUARDIAN: "监护人",
    Role.VENUE_MANAGER: "场馆负责人",
}

# 可代表场馆行事的角色
STAFF_ROLES = (Role.OPERATOR, Role.VENUE_MANAGER)


class RequestStatus(str, enum.Enum):
    """借调请求生命周期，取值与 domain/contract.json 的 states 一致。"""

    DRAFT = "草拟"
    PENDING = "待核验"
    CONFIRMED = "已确认"
    ACTIVE = "执行中"
    ARCHIVED = "已归档"


class InvitationStatus(str, enum.Enum):
    PROPOSED = "proposed"  # 已提出，待原场馆应答
    HOME_ACCEPTED = "home_accepted"  # 原场馆已接受（可能仅部分日期）
    HOME_DECLINED = "home_declined"  # 原场馆已拒绝
    CONFIRMED = "confirmed"  # 接收方已确认，占用已迁移
    RECALLED = "recalled"  # 协议被全部召回后联动


INVITATION_LABELS = {
    InvitationStatus.PROPOSED: "待原场馆应答",
    InvitationStatus.HOME_ACCEPTED: "原场馆已接受",
    InvitationStatus.HOME_DECLINED: "原场馆已拒绝",
    InvitationStatus.CONFIRMED: "双方已确认",
    InvitationStatus.RECALLED: "已召回",
}

# 仍处于“借调链”上的邀请状态（用于重复邀请幂等判断与敏感资料可见性）
LIVE_INVITATION_STATUSES = (
    InvitationStatus.PROPOSED,
    InvitationStatus.HOME_ACCEPTED,
    InvitationStatus.CONFIRMED,
)


class AgreementStatus(str, enum.Enum):
    ACTIVE = "active"  # 执行中
    PARTIALLY_RECALLED = "partially_recalled"  # 部分日期已召回
    RECALLED = "recalled"  # 全部召回
    COMPLETED = "completed"  # 全部日期已结束


AGREEMENT_LABELS = {
    AgreementStatus.ACTIVE: "执行中",
    AgreementStatus.PARTIALLY_RECALLED: "部分日期已召回",
    AgreementStatus.RECALLED: "已召回",
    AgreementStatus.COMPLETED: "已完成",
}


class EntryKind(str, enum.Enum):
    HOME = "home"  # 原场馆排班
    LOAN = "loan"  # 借调占用（迁移至接收方）


class EntryStatus(str, enum.Enum):
    ACTIVE = "active"  # 有效占用
    RELEASED = "released"  # 原排班已被借调释放（召回时可恢复）
    CANCELLED = "cancelled"  # 已取消


@dataclass
class ActorContext:
    """一次请求的调用者身份（来自请求头）。"""

    actor_id: str
    role: Role
    venue_id: str | None = None


@dataclass
class Venue:
    id: str
    name: str


@dataclass
class SensitiveProfile:
    """敏感资料：仅借调链相关人员可见。"""

    phone: str = ""
    id_number: str = ""
    emergency_contact: str = ""


@dataclass
class Volunteer:
    id: str
    name: str
    home_venue_id: str
    skills: dict[str, int] = field(default_factory=dict)  # 技能 -> 等级
    is_minor: bool = False
    guardian_id: str | None = None
    sensitive: SensitiveProfile = field(default_factory=SensitiveProfile)


@dataclass
class GuardianRestriction:
    """监护限制：未成年志愿者跨馆借调的硬性约束。"""

    volunteer_id: str
    max_daily_hours: int = 8
    allow_cross_venue: bool = True
    allowed_start: date | None = None  # 允许借调窗口（含边界）
    allowed_end: date | None = None
    note: str = ""


@dataclass
class ScheduleEntry:
    """排班占用。同一志愿者同一日期至多一条 ACTIVE 记录。"""

    id: str
    volunteer_id: str
    day: date
    hours: int
    venue_id: str
    kind: EntryKind = EntryKind.HOME
    status: EntryStatus = EntryStatus.ACTIVE
    agreement_id: str | None = None


@dataclass
class Capacity:
    """接收方某日可接纳的借调志愿者上限。"""

    venue_id: str
    day: date
    max_inbound: int


@dataclass
class SkillRequirement:
    skill: str
    min_level: int
    count: int = 1


@dataclass
class LoanRequest:
    """借调请求：接收方登记的技能需求与日期。"""

    id: str
    receiving_venue_id: str
    requirements: list[SkillRequirement]
    dates: list[date]
    daily_hours: int = 8
    note: str = ""
    created_by: str = ""


@dataclass
class Invitation:
    """针对某志愿者发出的借调邀请（同一请求同一志愿者唯一）。"""

    id: str
    request_id: str
    volunteer_id: str
    home_venue_id: str
    receiving_venue_id: str
    skill: str  # 该志愿者匹配的需求技能
    proposed_dates: list[date]
    status: InvitationStatus = InvitationStatus.PROPOSED
    accepted_dates: list[date] = field(default_factory=list)
    declined_dates: list[date] = field(default_factory=list)
    agreement_id: str | None = None


@dataclass
class RecallRecord:
    dates: list[date]
    by_actor: str
    by_venue_id: str | None
    reason: str
    at: datetime


@dataclass
class LoanAgreement:
    """借调协议：双边确认后原子迁移占用的结果。"""

    id: str
    request_id: str
    invitation_id: str
    volunteer_id: str
    home_venue_id: str
    receiving_venue_id: str
    skill: str
    dates: list[date]  # 当前仍有效的借调日期（召回会缩减）
    loan_entry_ids: list[str] = field(default_factory=list)
    released_entry_ids: list[str] = field(default_factory=list)
    recalls: list[RecallRecord] = field(default_factory=list)
    status: AgreementStatus = AgreementStatus.ACTIVE
