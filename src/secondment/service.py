"""跨场馆志愿者借调核心业务逻辑。

对应 domain/contract.json 的四条不变量：
- 跨馆资格约束：候选必须满足技能等级与监护限制，且不与既有借调占用冲突。
- 双边确认流程：原场馆接受（可仅部分日期）后，接收方确认才生效。
- 排班占用迁移：确认时在同一临界区内完成冲突复核、容量扣减与占用迁移，整体原子生效。
- 关联方权限隔离：敏感资料仅志愿者本人、监护人、原场馆与借调链接收方可见。
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
from typing import Callable

from .models import (
    STAFF_ROLES,
    ActorContext,
    AgreementStatus,
    Capacity,
    EntryKind,
    EntryStatus,
    GuardianRestriction,
    Invitation,
    InvitationStatus,
    LIVE_INVITATION_STATUSES,
    LoanAgreement,
    LoanRequest,
    RecallRecord,
    RequestStatus,
    Role,
    ScheduleEntry,
    SensitiveProfile,
    SkillRequirement,
    Venue,
    Volunteer,
)
from .store import Store

# 冲突/错误代码（接口响应中附带中文说明，便于群聊协调时直接转述）
SKILL_MISMATCH = "SKILL_MISMATCH"  # 技能不满足需求
GUARDIAN_PROFILE_MISSING = "GUARDIAN_PROFILE_MISSING"  # 未成年缺少监护限制登记
GUARDIAN_CROSS_VENUE_FORBIDDEN = "GUARDIAN_CROSS_VENUE_FORBIDDEN"  # 监护限制不允许跨馆
GUARDIAN_DATE_OUT_OF_WINDOW = "GUARDIAN_DATE_OUT_OF_WINDOW"  # 日期超出监护允许窗口
GUARDIAN_HOURS_EXCEEDED = "GUARDIAN_HOURS_EXCEEDED"  # 超出监护允许的每日时长
ALREADY_LOANED = "ALREADY_LOANED"  # 日期已被其他借调占用
CAPACITY_FULL = "CAPACITY_FULL"  # 接收方当日容量已满
SCHEDULE_CONFLICT = "SCHEDULE_CONFLICT"  # 当日已存在有效排班
NOT_CANDIDATE = "NOT_CANDIDATE"  # 当前不是可行候选
INVALID_STATE = "INVALID_STATE"  # 当前状态不允许该操作
NOT_FOUND = "NOT_FOUND"
FORBIDDEN = "FORBIDDEN"
BAD_REQUEST = "BAD_REQUEST"
UNAUTHENTICATED = "UNAUTHENTICATED"


class ServiceError(Exception):
    """业务错误：status 为 HTTP 状态码，code 为机器可读冲突代码。"""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


class SecondmentService:
    """借调领域服务。``today``/``now`` 可注入，便于测试确定性。"""

    def __init__(
        self,
        store: Store | None = None,
        *,
        today: Callable[[], date] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store or Store()
        self._today = today or date.today
        self._now = now or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    # 基础校验
    # ------------------------------------------------------------------
    def _venue(self, venue_id: str) -> Venue:
        venue = self.store.venues.get(venue_id)
        if venue is None:
            raise ServiceError(404, NOT_FOUND, f"场馆不存在：{venue_id}")
        return venue

    def _volunteer(self, volunteer_id: str) -> Volunteer:
        vol = self.store.volunteers.get(volunteer_id)
        if vol is None:
            raise ServiceError(404, NOT_FOUND, f"志愿者不存在：{volunteer_id}")
        return vol

    def _request(self, request_id: str) -> LoanRequest:
        req = self.store.requests.get(request_id)
        if req is None:
            raise ServiceError(404, NOT_FOUND, f"借调请求不存在：{request_id}")
        return req

    def _invitation(self, invitation_id: str) -> Invitation:
        inv = self.store.invitations.get(invitation_id)
        if inv is None:
            raise ServiceError(404, NOT_FOUND, f"邀请不存在：{invitation_id}")
        return inv

    def _agreement(self, agreement_id: str) -> LoanAgreement:
        agr = self.store.agreements.get(agreement_id)
        if agr is None:
            raise ServiceError(404, NOT_FOUND, f"借调协议不存在：{agreement_id}")
        return agr

    @staticmethod
    def _require_staff(actor: ActorContext) -> None:
        if actor.role not in STAFF_ROLES:
            raise ServiceError(403, FORBIDDEN, "仅场馆工作人员可执行此操作")

    def _require_venue_staff(self, actor: ActorContext, venue_id: str, who: str) -> None:
        if actor.role not in STAFF_ROLES or actor.venue_id != venue_id:
            raise ServiceError(403, FORBIDDEN, f"仅{who}可执行此操作", {"venue_id": venue_id})

    def _event(self, event_type: str, actor: ActorContext, detail: dict) -> None:
        self.store.events.append(
            {
                "at": self._now().isoformat(),
                "type": event_type,
                "actor_id": actor.actor_id,
                "role": actor.role.value,
                "venue_id": actor.venue_id,
                "detail": detail,
            }
        )

    # ------------------------------------------------------------------
    # 登记：场馆 / 志愿者 / 监护限制 / 原排班 / 接收方容量 / 借调请求
    # ------------------------------------------------------------------
    def register_venue(self, actor: ActorContext, *, name: str, venue_id: str | None = None) -> Venue:
        self._require_staff(actor)
        with self.store.lock:
            vid = venue_id or self.store.next_id("VEN")
            if vid in self.store.venues:
                raise ServiceError(409, BAD_REQUEST, f"场馆编号已存在：{vid}")
            venue = Venue(id=vid, name=name)
            self.store.venues[vid] = venue
            self._event("venue_registered", actor, {"venue_id": vid})
            return venue

    def register_volunteer(
        self,
        actor: ActorContext,
        *,
        name: str,
        home_venue_id: str,
        skills: dict[str, int] | None = None,
        is_minor: bool = False,
        guardian_id: str | None = None,
        sensitive: SensitiveProfile | None = None,
        volunteer_id: str | None = None,
    ) -> Volunteer:
        self._venue(home_venue_id)
        self._require_venue_staff(actor, home_venue_id, "原场馆工作人员")
        with self.store.lock:
            vid = volunteer_id or self.store.next_id("VOL")
            if vid in self.store.volunteers:
                raise ServiceError(409, BAD_REQUEST, f"志愿者编号已存在：{vid}")
            vol = Volunteer(
                id=vid,
                name=name,
                home_venue_id=home_venue_id,
                skills=dict(skills or {}),
                is_minor=is_minor,
                guardian_id=guardian_id,
                sensitive=sensitive or SensitiveProfile(),
            )
            self.store.volunteers[vid] = vol
            self._event("volunteer_registered", actor, {"volunteer_id": vid})
            return vol

    def register_guardian_restriction(
        self,
        actor: ActorContext,
        *,
        volunteer_id: str,
        max_daily_hours: int = 8,
        allow_cross_venue: bool = True,
        allowed_start: date | None = None,
        allowed_end: date | None = None,
        note: str = "",
    ) -> GuardianRestriction:
        vol = self._volunteer(volunteer_id)
        is_guardian = actor.role is Role.GUARDIAN and actor.actor_id == vol.guardian_id
        is_home_staff = actor.role in STAFF_ROLES and actor.venue_id == vol.home_venue_id
        if not (is_guardian or is_home_staff):
            raise ServiceError(403, FORBIDDEN, "仅监护人或原场馆工作人员可登记监护限制")
        if max_daily_hours < 1:
            raise ServiceError(400, BAD_REQUEST, "每日时长上限必须为正数")
        if allowed_start and allowed_end and allowed_start > allowed_end:
            raise ServiceError(400, BAD_REQUEST, "允许窗口起点不能晚于终点")
        with self.store.lock:
            restriction = GuardianRestriction(
                volunteer_id=volunteer_id,
                max_daily_hours=max_daily_hours,
                allow_cross_venue=allow_cross_venue,
                allowed_start=allowed_start,
                allowed_end=allowed_end,
                note=note,
            )
            self.store.restrictions[volunteer_id] = restriction
            self._event("guardian_restriction_registered", actor, {"volunteer_id": volunteer_id})
            return restriction

    def add_schedule_entry(
        self,
        actor: ActorContext,
        *,
        volunteer_id: str,
        day: date,
        hours: int = 8,
        venue_id: str | None = None,
    ) -> ScheduleEntry:
        vol = self._volunteer(volunteer_id)
        target_venue = venue_id or vol.home_venue_id
        self._venue(target_venue)
        self._require_venue_staff(actor, target_venue, "排班所属场馆工作人员")
        if not 1 <= hours <= 24:
            raise ServiceError(400, BAD_REQUEST, "每日时长须在 1-24 小时之间")
        with self.store.lock:
            existing = self.active_entry(volunteer_id, day)
            if existing is not None:
                raise ServiceError(
                    409,
                    SCHEDULE_CONFLICT,
                    "当日已存在有效排班（可能在原场馆或借调中）",
                    {
                        "date": day.isoformat(),
                        "existing_entry_id": existing.id,
                        "kind": existing.kind.value,
                        "venue_id": existing.venue_id,
                    },
                )
            entry = ScheduleEntry(
                id=self.store.next_id("SCH"),
                volunteer_id=volunteer_id,
                day=day,
                hours=hours,
                venue_id=target_venue,
            )
            self.store.entries[entry.id] = entry
            self._event("schedule_entry_added", actor, {"entry_id": entry.id, "volunteer_id": volunteer_id})
            return entry

    def register_capacity(
        self, actor: ActorContext, *, venue_id: str, day: date, max_inbound: int
    ) -> Capacity:
        self._venue(venue_id)
        self._require_venue_staff(actor, venue_id, "本场馆工作人员")
        if max_inbound < 0:
            raise ServiceError(400, BAD_REQUEST, "容量不能为负数")
        with self.store.lock:
            capacity = Capacity(venue_id=venue_id, day=day, max_inbound=max_inbound)
            self.store.capacities[(venue_id, day)] = capacity
            self._event("capacity_registered", actor, {"venue_id": venue_id, "date": day.isoformat()})
            return capacity

    def create_loan_request(
        self,
        actor: ActorContext,
        *,
        receiving_venue_id: str,
        requirements: list[SkillRequirement],
        dates: list[date],
        daily_hours: int = 8,
        note: str = "",
    ) -> LoanRequest:
        self._venue(receiving_venue_id)
        self._require_venue_staff(actor, receiving_venue_id, "接收方场馆工作人员")
        if not requirements:
            raise ServiceError(400, BAD_REQUEST, "技能需求不能为空")
        for req_slot in requirements:
            if not req_slot.skill or req_slot.min_level < 1 or req_slot.count < 1:
                raise ServiceError(400, BAD_REQUEST, "技能需求须包含技能名、最低等级≥1、人数≥1")
        unique_dates = sorted(set(dates))
        if not unique_dates:
            raise ServiceError(400, BAD_REQUEST, "借调日期不能为空")
        if not 1 <= daily_hours <= 24:
            raise ServiceError(400, BAD_REQUEST, "每日时长须在 1-24 小时之间")
        with self.store.lock:
            req = LoanRequest(
                id=self.store.next_id("REQ"),
                receiving_venue_id=receiving_venue_id,
                requirements=list(requirements),
                dates=unique_dates,
                daily_hours=daily_hours,
                note=note,
                created_by=actor.actor_id,
            )
            self.store.requests[req.id] = req
            self._event("loan_request_created", actor, {"request_id": req.id})
            return req

    # ------------------------------------------------------------------
    # 占用与容量查询
    # ------------------------------------------------------------------
    def active_entry(self, volunteer_id: str, day: date) -> ScheduleEntry | None:
        for entry in self.store.entries.values():
            if (
                entry.volunteer_id == volunteer_id
                and entry.day == day
                and entry.status is EntryStatus.ACTIVE
            ):
                return entry
        return None

    def used_inbound(self, venue_id: str, day: date) -> int:
        """某日已迁入该场馆的借调占用人数（由有效借调排班推导，不产生对账漂移）。"""
        return len(
            {
                entry.volunteer_id
                for entry in self.store.entries.values()
                if entry.venue_id == venue_id
                and entry.day == day
                and entry.kind is EntryKind.LOAN
                and entry.status is EntryStatus.ACTIVE
            }
        )

    def capacity_remaining(self, venue_id: str, day: date) -> int | None:
        """剩余容量；未登记容量时返回 None 表示不限。"""
        capacity = self.store.capacities.get((venue_id, day))
        if capacity is None:
            return None
        return capacity.max_inbound - self.used_inbound(venue_id, day)

    # ------------------------------------------------------------------
    # 候选评估与排序（含解释）
    # ------------------------------------------------------------------
    def _confirmed_skill_counts(self, request_id: str) -> Counter:
        counts: Counter = Counter()
        for agr in self.store.agreements.values():
            if agr.request_id == request_id and self.agreement_status(agr) in (
                AgreementStatus.ACTIVE,
                AgreementStatus.PARTIALLY_RECALLED,
            ):
                counts[agr.skill] += 1
        return counts

    def _pick_requirement(self, req: LoanRequest, vol: Volunteer) -> SkillRequirement | None:
        """在志愿者满足的需求中，优先填补缺口最大的技能槽。"""
        matched = [r for r in req.requirements if vol.skills.get(r.skill, 0) >= r.min_level]
        if not matched:
            return None
        confirmed = self._confirmed_skill_counts(req.id)
        return min(matched, key=lambda r: (confirmed.get(r.skill, 0) - r.count, r.skill))

    def evaluate(self, req: LoanRequest, vol: Volunteer) -> dict:
        """评估单个志愿者：技能、监护限制、逐日期占用。

        返回 ``ok``（是否为可行候选）、``reasons``（硬性冲突原因）、
        以及逐日期分类：空闲 / 可迁移（有原排班，确认后迁移）/ 阻塞。
        """
        reasons: list[dict] = []
        matched = self._pick_requirement(req, vol)
        if matched is None:
            reasons.append(
                {
                    "code": SKILL_MISMATCH,
                    "message": "技能不满足借调请求的任何需求",
                    "requirements": [
                        {
                            "skill": r.skill,
                            "min_level": r.min_level,
                            "actual_level": vol.skills.get(r.skill, 0),
                        }
                        for r in req.requirements
                    ],
                }
            )
        if vol.is_minor:
            restriction = self.store.restrictions.get(vol.id)
            if restriction is None:
                reasons.append(
                    {"code": GUARDIAN_PROFILE_MISSING, "message": "未成年志愿者缺少监护限制登记"}
                )
            else:
                if not restriction.allow_cross_venue:
                    reasons.append(
                        {
                            "code": GUARDIAN_CROSS_VENUE_FORBIDDEN,
                            "message": "监护限制不允许跨场馆借调",
                        }
                    )
                outside = [
                    d
                    for d in req.dates
                    if (restriction.allowed_start and d < restriction.allowed_start)
                    or (restriction.allowed_end and d > restriction.allowed_end)
                ]
                if outside:
                    reasons.append(
                        {
                            "code": GUARDIAN_DATE_OUT_OF_WINDOW,
                            "message": "部分日期超出监护允许窗口",
                            "dates": [d.isoformat() for d in outside],
                        }
                    )
                if req.daily_hours > restriction.max_daily_hours:
                    reasons.append(
                        {
                            "code": GUARDIAN_HOURS_EXCEEDED,
                            "message": f"每日 {req.daily_hours} 小时超出监护允许的 {restriction.max_daily_hours} 小时",
                        }
                    )
        free_dates: list[date] = []
        migratable_dates: list[date] = []
        blocked_dates: list[dict] = []
        for day in req.dates:
            entry = self.active_entry(vol.id, day)
            if entry is None:
                free_dates.append(day)
            elif entry.kind is EntryKind.LOAN:
                blocked_dates.append(
                    {
                        "date": day.isoformat(),
                        "code": ALREADY_LOANED,
                        "message": "当日已被其他借调占用",
                        "venue_id": entry.venue_id,
                        "agreement_id": entry.agreement_id,
                    }
                )
            else:
                migratable_dates.append(day)
        if blocked_dates and not free_dates and not migratable_dates:
            reasons.append(
                {
                    "code": ALREADY_LOANED,
                    "message": "全部请求日期均已被其他借调占用",
                    "dates": [b["date"] for b in blocked_dates],
                }
            )
        return {
            "volunteer_id": vol.id,
            "ok": not reasons,
            "reasons": reasons,
            "matched_requirement": matched,
            "free_dates": free_dates,
            "migratable_dates": migratable_dates,
            "blocked_dates": blocked_dates,
        }

    def _score(self, req: LoanRequest, vol: Volunteer, evaluation: dict) -> dict:
        """确定性打分并生成逐项解释。

        构成：日期覆盖（满分 40）+ 技能富余（每级 10，上限 30）- 迁移惩罚（每天 5）。
        """
        matched: SkillRequirement = evaluation["matched_requirement"]
        cover = len(evaluation["free_dates"]) + len(evaluation["migratable_dates"])
        coverage_pts = round(40 * cover / len(req.dates), 2)
        level = vol.skills.get(matched.skill, 0)
        margin = min(level - matched.min_level, 3)
        skill_pts = 10 * margin
        migration_penalty = 5 * len(evaluation["migratable_dates"])
        score = round(coverage_pts + skill_pts - migration_penalty, 2)
        explanation = [
            {
                "factor": "date_coverage",
                "points": coverage_pts,
                "detail": f"可覆盖 {cover}/{len(req.dates)} 个请求日期",
            },
            {
                "factor": "skill_margin",
                "points": skill_pts,
                "detail": f"{matched.skill} 等级 {level}，要求 {matched.min_level}，富余 {margin} 级",
            },
            {
                "factor": "migration_penalty",
                "points": -migration_penalty,
                "detail": (
                    f"{len(evaluation['migratable_dates'])} 天已有原场馆排班，确认后需迁移"
                    if migration_penalty
                    else "无原场馆排班需要迁移"
                ),
            },
        ]
        return {
            "volunteer_id": vol.id,
            "name": vol.name,
            "home_venue_id": vol.home_venue_id,
            "matched_requirement": {
                "skill": matched.skill,
                "min_level": matched.min_level,
                "volunteer_level": level,
            },
            "free_dates": [d.isoformat() for d in evaluation["free_dates"]],
            "migratable_dates": [d.isoformat() for d in evaluation["migratable_dates"]],
            "blocked_dates": evaluation["blocked_dates"],
            "score": score,
            "explanation": explanation,
        }

    def propose_candidates(self, actor: ActorContext, request_id: str) -> dict:
        """为借调请求生成候选排序与被拒名单（均附原因）。"""
        req = self._request(request_id)
        self._require_staff(actor)
        with self.store.lock:
            candidates: list[dict] = []
            rejected: list[dict] = []
            for vol in self.store.volunteers.values():
                if vol.home_venue_id == req.receiving_venue_id:
                    continue  # 本馆志愿者不属于跨馆借调范围
                evaluation = self.evaluate(req, vol)
                if evaluation["ok"]:
                    candidates.append(self._score(req, vol, evaluation))
                else:
                    rejected.append(
                        {
                            "volunteer_id": vol.id,
                            "name": vol.name,
                            "home_venue_id": vol.home_venue_id,
                            "reasons": evaluation["reasons"],
                        }
                    )
            candidates.sort(key=lambda c: (-c["score"], c["volunteer_id"]))
            rejected.sort(key=lambda r: r["volunteer_id"])
            for rank, candidate in enumerate(candidates, start=1):
                candidate["rank"] = rank
            capacity = []
            for day in req.dates:
                remaining = self.capacity_remaining(req.receiving_venue_id, day)
                registered = self.store.capacities.get((req.receiving_venue_id, day))
                capacity.append(
                    {
                        "date": day.isoformat(),
                        "max_inbound": registered.max_inbound if registered else None,
                        "used_inbound": self.used_inbound(req.receiving_venue_id, day),
                        "remaining": remaining,
                    }
                )
            return {
                "request_id": req.id,
                "generated_at": self._now().isoformat(),
                "capacity": capacity,
                "candidates": candidates,
                "rejected": rejected,
            }

    # ------------------------------------------------------------------
    # 邀请（重复邀请幂等）
    # ------------------------------------------------------------------
    def create_invitation(
        self,
        actor: ActorContext,
        request_id: str,
        volunteer_id: str,
        dates: list[date] | None = None,
    ) -> tuple[Invitation, bool]:
        """发出邀请。返回 (邀请, 是否重复)。

        同一请求对同一志愿者只存在一条有效邀请：重复邀请幂等返回原邀请。
        """
        req = self._request(request_id)
        self._require_venue_staff(actor, req.receiving_venue_id, "接收方场馆工作人员")
        vol = self._volunteer(volunteer_id)
        with self.store.lock:
            for inv in self.store.invitations.values():
                if (
                    inv.request_id == req.id
                    and inv.volunteer_id == vol.id
                    and inv.status in LIVE_INVITATION_STATUSES
                ):
                    return inv, True
            if vol.home_venue_id == req.receiving_venue_id:
                raise ServiceError(
                    409, NOT_CANDIDATE, "志愿者已属于接收方场馆，无需跨馆借调"
                )
            evaluation = self.evaluate(req, vol)
            if not evaluation["ok"]:
                raise ServiceError(
                    409,
                    NOT_CANDIDATE,
                    "该志愿者当前不是可行候选",
                    {"reasons": evaluation["reasons"]},
                )
            allowed = set(evaluation["free_dates"]) | set(evaluation["migratable_dates"])
            proposed = sorted(set(dates)) if dates is not None else sorted(allowed)
            if not proposed:
                raise ServiceError(400, BAD_REQUEST, "提议日期不能为空")
            not_allowed = [d for d in proposed if d not in allowed]
            if not_allowed:
                raise ServiceError(
                    409,
                    ALREADY_LOANED,
                    "部分提议日期已被其他借调占用",
                    {"dates": [d.isoformat() for d in not_allowed]},
                )
            inv = Invitation(
                id=self.store.next_id("INV"),
                request_id=req.id,
                volunteer_id=vol.id,
                home_venue_id=vol.home_venue_id,
                receiving_venue_id=req.receiving_venue_id,
                skill=evaluation["matched_requirement"].skill,
                proposed_dates=proposed,
            )
            self.store.invitations[inv.id] = inv
            self._event(
                "invitation_created",
                actor,
                {
                    "request_id": req.id,
                    "invitation_id": inv.id,
                    "volunteer_id": vol.id,
                    "dates": [d.isoformat() for d in proposed],
                },
            )
            return inv, False

    # ------------------------------------------------------------------
    # 双边确认：原场馆应答（支持部分日期）→ 接收方确认（原子迁移）
    # ------------------------------------------------------------------
    def respond_invitation(
        self,
        actor: ActorContext,
        invitation_id: str,
        accept: bool,
        accepted_dates: list[date] | None = None,
    ) -> Invitation:
        """原场馆应答。接受时可仅接受部分提议日期；相同决定的重复应答幂等。"""
        inv = self._invitation(invitation_id)
        self._require_venue_staff(actor, inv.home_venue_id, "原场馆工作人员")
        with self.store.lock:
            if inv.status is InvitationStatus.PROPOSED:
                if accept:
                    chosen = (
                        sorted(set(accepted_dates))
                        if accepted_dates is not None
                        else list(inv.proposed_dates)
                    )
                    if not chosen:
                        raise ServiceError(400, BAD_REQUEST, "接受日期不能为空")
                    extra = [d for d in chosen if d not in inv.proposed_dates]
                    if extra:
                        raise ServiceError(
                            400,
                            BAD_REQUEST,
                            "接受日期必须属于提议日期",
                            {"extra_dates": [d.isoformat() for d in extra]},
                        )
                    inv.accepted_dates = chosen
                    inv.declined_dates = [d for d in inv.proposed_dates if d not in chosen]
                    inv.status = InvitationStatus.HOME_ACCEPTED
                else:
                    inv.status = InvitationStatus.HOME_DECLINED
                    inv.declined_dates = list(inv.proposed_dates)
                self._event(
                    "invitation_responded",
                    actor,
                    {
                        "invitation_id": inv.id,
                        "accept": accept,
                        "accepted_dates": [d.isoformat() for d in inv.accepted_dates],
                    },
                )
                return inv
            # 幂等重放：相同决定直接返回现状（以首次应答为准）
            if accept and inv.status is InvitationStatus.HOME_ACCEPTED:
                return inv
            if not accept and inv.status is InvitationStatus.HOME_DECLINED:
                return inv
            raise ServiceError(
                409,
                INVALID_STATE,
                f"邀请当前状态为 {inv.status.value}，无法重复应答",
            )

    def confirm_invitation(
        self, actor: ActorContext, invitation_id: str
    ) -> tuple[LoanAgreement, bool]:
        """接收方确认：在同一临界区内复核冲突与容量并原子迁移占用。

        返回 (协议, 是否新建)。重复确认幂等返回原协议，不会重复迁移。
        """
        inv = self._invitation(invitation_id)
        self._require_venue_staff(actor, inv.receiving_venue_id, "接收方场馆工作人员")
        with self.store.lock:
            if inv.status is InvitationStatus.CONFIRMED:
                return self.store.agreements[inv.agreement_id], False
            if inv.status is not InvitationStatus.HOME_ACCEPTED:
                raise ServiceError(
                    409,
                    INVALID_STATE,
                    f"邀请当前状态为 {inv.status.value}，须先由原场馆接受",
                )
            req = self.store.requests[inv.request_id]
            vol = self.store.volunteers[inv.volunteer_id]
            dates = list(inv.accepted_dates)
            # 1) 占用冲突复核：并发确认时只有一个请求能通过
            conflicts = []
            for day in dates:
                entry = self.active_entry(vol.id, day)
                if entry is not None and entry.kind is EntryKind.LOAN:
                    conflicts.append(
                        {
                            "date": day.isoformat(),
                            "code": ALREADY_LOANED,
                            "message": "当日已被其他借调占用",
                            "venue_id": entry.venue_id,
                            "agreement_id": entry.agreement_id,
                        }
                    )
            if conflicts:
                raise ServiceError(
                    409, ALREADY_LOANED, "部分日期已被其他借调占用", {"conflicts": conflicts}
                )
            # 2) 接收方容量复核
            full = []
            for day in dates:
                remaining = self.capacity_remaining(inv.receiving_venue_id, day)
                if remaining is not None and remaining <= 0:
                    capacity = self.store.capacities[(inv.receiving_venue_id, day)]
                    full.append(
                        {
                            "date": day.isoformat(),
                            "code": CAPACITY_FULL,
                            "max_inbound": capacity.max_inbound,
                            "used_inbound": self.used_inbound(inv.receiving_venue_id, day),
                        }
                    )
            if full:
                raise ServiceError(409, CAPACITY_FULL, "接收方当日容量已满", {"dates": full})
            # 3) 原子迁移占用：全部校验通过后才变更，任何失败都不落部分结果
            agr = LoanAgreement(
                id=self.store.next_id("AGR"),
                request_id=req.id,
                invitation_id=inv.id,
                volunteer_id=vol.id,
                home_venue_id=inv.home_venue_id,
                receiving_venue_id=inv.receiving_venue_id,
                skill=inv.skill,
                dates=dates,
            )
            for day in dates:
                entry = self.active_entry(vol.id, day)
                if entry is not None and entry.kind is EntryKind.HOME:
                    entry.status = EntryStatus.RELEASED
                    agr.released_entry_ids.append(entry.id)
                loan = ScheduleEntry(
                    id=self.store.next_id("SCH"),
                    volunteer_id=vol.id,
                    day=day,
                    hours=req.daily_hours,
                    venue_id=inv.receiving_venue_id,
                    kind=EntryKind.LOAN,
                    status=EntryStatus.ACTIVE,
                    agreement_id=agr.id,
                )
                self.store.entries[loan.id] = loan
                agr.loan_entry_ids.append(loan.id)
            self.store.agreements[agr.id] = agr
            inv.status = InvitationStatus.CONFIRMED
            inv.agreement_id = agr.id
            self._event(
                "agreement_confirmed",
                actor,
                {
                    "agreement_id": agr.id,
                    "invitation_id": inv.id,
                    "request_id": req.id,
                    "volunteer_id": vol.id,
                    "dates": [d.isoformat() for d in dates],
                },
            )
            return agr, True

    # ------------------------------------------------------------------
    # 临时召回：占用原子迁回原场馆
    # ------------------------------------------------------------------
    def recall_agreement(
        self,
        actor: ActorContext,
        agreement_id: str,
        dates: list[date] | None = None,
        reason: str = "",
    ) -> LoanAgreement:
        """召回部分或全部日期。借调双方场馆均可发起；全部召回后邀请联动为已召回。"""
        agr = self._agreement(agreement_id)
        if actor.role not in STAFF_ROLES or actor.venue_id not in (
            agr.home_venue_id,
            agr.receiving_venue_id,
        ):
            raise ServiceError(403, FORBIDDEN, "仅借调双方场馆工作人员可召回")
        with self.store.lock:
            if agr.status is AgreementStatus.RECALLED:
                if dates is None:
                    return agr  # 幂等重放
                raise ServiceError(409, INVALID_STATE, "协议已全部召回")
            target = sorted(set(dates)) if dates is not None else list(agr.dates)
            if not target:
                raise ServiceError(400, BAD_REQUEST, "召回日期不能为空")
            outside = [d for d in target if d not in agr.dates]
            if outside:
                raise ServiceError(
                    400,
                    BAD_REQUEST,
                    "召回日期必须属于协议当前有效日期",
                    {"extra_dates": [d.isoformat() for d in outside]},
                )
            target_set = set(target)
            for day in target:
                for entry_id in agr.loan_entry_ids:
                    entry = self.store.entries[entry_id]
                    if entry.day == day and entry.status is EntryStatus.ACTIVE:
                        entry.status = EntryStatus.CANCELLED
                for entry_id in agr.released_entry_ids:
                    entry = self.store.entries[entry_id]
                    if entry.day == day and entry.status is EntryStatus.RELEASED:
                        entry.status = EntryStatus.ACTIVE  # 原排班恢复
            agr.dates = [d for d in agr.dates if d not in target_set]
            agr.recalls.append(
                RecallRecord(
                    dates=target,
                    by_actor=actor.actor_id,
                    by_venue_id=actor.venue_id,
                    reason=reason,
                    at=self._now(),
                )
            )
            agr.status = (
                AgreementStatus.RECALLED if not agr.dates else AgreementStatus.PARTIALLY_RECALLED
            )
            if agr.status is AgreementStatus.RECALLED:
                inv = self.store.invitations[agr.invitation_id]
                inv.status = InvitationStatus.RECALLED
            self._event(
                "agreement_recalled",
                actor,
                {
                    "agreement_id": agr.id,
                    "dates": [d.isoformat() for d in target],
                    "reason": reason,
                },
            )
            return agr

    # ------------------------------------------------------------------
    # 派生状态（生命周期由数据推导，避免状态机漂移）
    # ------------------------------------------------------------------
    def agreement_status(self, agr: LoanAgreement) -> AgreementStatus:
        if agr.status is AgreementStatus.RECALLED:
            return AgreementStatus.RECALLED
        if agr.dates and all(d < self._today() for d in agr.dates):
            return AgreementStatus.COMPLETED
        return agr.status

    def request_status(self, req: LoanRequest) -> RequestStatus:
        agreements = [a for a in self.store.agreements.values() if a.request_id == req.id]
        invitations = [i for i in self.store.invitations.values() if i.request_id == req.id]
        active = [
            a
            for a in agreements
            if self.agreement_status(a)
            in (AgreementStatus.ACTIVE, AgreementStatus.PARTIALLY_RECALLED)
        ]
        if active:
            today = self._today()
            if any(min(a.dates) <= today <= max(a.dates) for a in active):
                return RequestStatus.ACTIVE
            return RequestStatus.CONFIRMED
        if any(
            i.status in (InvitationStatus.PROPOSED, InvitationStatus.HOME_ACCEPTED)
            for i in invitations
        ):
            return RequestStatus.PENDING
        if agreements or invitations:
            return RequestStatus.ARCHIVED
        return RequestStatus.DRAFT

    # ------------------------------------------------------------------
    # 关联方权限隔离
    # ------------------------------------------------------------------
    def can_view_sensitive(self, actor: ActorContext, vol: Volunteer) -> bool:
        """敏感资料仅对借调链相关人员可见：

        志愿者本人、其监护人、原场馆工作人员，以及借调链上的接收方场馆
        （存在未被拒绝的邀请或曾达成协议的场馆）。
        """
        if actor.role is Role.VOLUNTEER and actor.actor_id == vol.id:
            return True
        if actor.role is Role.GUARDIAN and vol.guardian_id and actor.actor_id == vol.guardian_id:
            return True
        if actor.role in STAFF_ROLES and actor.venue_id:
            if actor.venue_id == vol.home_venue_id:
                return True
            for inv in self.store.invitations.values():
                if (
                    inv.volunteer_id == vol.id
                    and inv.receiving_venue_id == actor.venue_id
                    and inv.status is not InvitationStatus.HOME_DECLINED
                ):
                    return True
        return False
