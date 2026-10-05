"""借调业务服务：登记、候选、双边确认、原子迁移、临时召回。

一致性约定：
- 所有写路径持有 store.lock，确认与迁移在同一临界区内完成（原子迁移占用）。
- 重复邀请、重复确认幂等；并发确认由锁串行化，后到者获得冲突原因。
- 部分日期接受：协议日期 = 双方确认日期的交集。
- 临时召回：按日期回收占用、恢复被迁移的原排班，并联动请求状态。
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from . import matching, visibility
from .errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    StateError,
    ValidationError,
)
from .models import (
    SLOTS,
    Actor,
    CapacityRule,
    Confirmation,
    Guardianship,
    Invitation,
    InvitationState,
    LifecycleState,
    LoanAgreement,
    LoanRequest,
    Occupancy,
    OccupancyState,
    RecallRecord,
    Role,
    Shift,
    ShiftState,
    Venue,
    Volunteer,
)
from .store import Store

_CONFIRM_PARTIES = ("home_venue", "receiving_venue", "guardian")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SecondmentService:
    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()

    # ---------- 内部：查找与权限 ----------

    def _venue(self, venue_id: str) -> Venue:
        try:
            return self.store.venues[venue_id]
        except KeyError:
            raise NotFoundError(f"场馆不存在：{venue_id}") from None

    def _volunteer(self, volunteer_id: str) -> Volunteer:
        try:
            return self.store.volunteers[volunteer_id]
        except KeyError:
            raise NotFoundError(f"志愿者不存在：{volunteer_id}") from None

    def _request(self, request_id: str) -> LoanRequest:
        try:
            return self.store.requests[request_id]
        except KeyError:
            raise NotFoundError(f"借调请求不存在：{request_id}") from None

    def _invitation(self, invitation_id: str) -> Invitation:
        try:
            return self.store.invitations[invitation_id]
        except KeyError:
            raise NotFoundError(f"邀请不存在：{invitation_id}") from None

    def _agreement(self, agreement_id: str) -> LoanAgreement:
        try:
            return self.store.agreements[agreement_id]
        except KeyError:
            raise NotFoundError(f"借调协议不存在：{agreement_id}") from None

    @staticmethod
    def _require_actor(actor: Actor | None) -> Actor:
        if actor is None:
            raise PermissionDenied("缺少调用者身份")
        return actor

    def _require_operator(self, actor: Actor | None) -> None:
        actor = self._require_actor(actor)
        if actor.role != Role.OPERATOR:
            raise PermissionDenied("仅文博中心运营员可执行该操作")

    def _require_venue_manager(self, actor: Actor | None, venue_id: str, action: str) -> None:
        actor = self._require_actor(actor)
        if actor.role == Role.OPERATOR:
            return
        if actor.role == Role.VENUE_MANAGER and actor.venue_id == venue_id:
            return
        raise PermissionDenied(f"仅该场馆负责人或运营员可{action}")

    def _authorize_confirm(self, actor: Actor | None, req: LoanRequest, vol: Volunteer, party: str) -> None:
        actor = self._require_actor(actor)
        if actor.role == Role.OPERATOR:
            return
        if party == "home_venue":
            if actor.role == Role.VENUE_MANAGER and actor.venue_id == vol.home_venue_id:
                return
            raise PermissionDenied("仅原场馆负责人可确认")
        if party == "receiving_venue":
            if actor.role == Role.VENUE_MANAGER and actor.venue_id == req.receiving_venue_id:
                return
            raise PermissionDenied("仅接收场馆负责人可确认")
        if party == "guardian":
            if actor.role == Role.GUARDIAN and actor.actor_id == vol.guardianship.guardian_id:
                return
            raise PermissionDenied("仅该志愿者的监护人可确认")
        raise ValidationError(f"未知确认方：{party}")

    # ---------- 登记 ----------

    def register_venue(self, actor: Actor | None, name: str) -> Venue:
        self._require_operator(actor)
        if not name:
            raise ValidationError("场馆名称不能为空")
        with self.store.lock:
            venue = Venue(self.store.new_id("venue"), name)
            self.store.venues[venue.venue_id] = venue
            return venue

    def set_capacity(self, actor: Actor | None, venue_id: str, day: date, max_loaned: int) -> CapacityRule:
        """登记接收方某日可接纳的借调人数上限。"""
        self._require_venue_manager(actor, venue_id, "登记接收容量")
        if max_loaned < 0:
            raise ValidationError("容量上限不能为负")
        with self.store.lock:
            self._venue(venue_id)
            rule = CapacityRule(venue_id, day, max_loaned)
            self.store.capacity[(venue_id, day)] = rule
            return rule

    def register_volunteer(
        self,
        actor: Actor | None,
        *,
        name: str,
        home_venue_id: str,
        skills: dict[str, int],
        sensitive: dict[str, str] | None = None,
        guardianship: Guardianship | None = None,
    ) -> Volunteer:
        self._require_operator(actor)
        if not name:
            raise ValidationError("志愿者姓名不能为空")
        for skill, level in skills.items():
            if not isinstance(level, int) or level < 0:
                raise ValidationError(f"技能 {skill} 等级必须是非负整数")
        with self.store.lock:
            self._venue(home_venue_id)
            vol = Volunteer(
                volunteer_id=self.store.new_id("vol"),
                name=name,
                home_venue_id=home_venue_id,
                skills=dict(skills),
                sensitive=dict(sensitive or {}),
                guardianship=guardianship or Guardianship(),
            )
            self.store.volunteers[vol.volunteer_id] = vol
            return vol

    def register_shift(self, actor: Actor | None, volunteer_id: str, day: date, slot: str) -> Shift:
        """登记原场馆排班（借调冲突检测的基础数据）。"""
        if slot not in SLOTS:
            raise ValidationError(f"未知时段：{slot}")
        with self.store.lock:
            vol = self._volunteer(volunteer_id)
            self._require_venue_manager(actor, vol.home_venue_id, "登记排班")
            if self.store.shifts_on(volunteer_id, (day,), slot):
                raise ConflictError("该日期时段已有排班", {"date": day.isoformat(), "slot": slot})
            shift = Shift(self.store.new_id("shift"), volunteer_id, vol.home_venue_id, day, slot)
            self.store.shifts[shift.shift_id] = shift
            return shift

    def create_request(
        self,
        actor: Actor | None,
        *,
        receiving_venue_id: str,
        required_skills: dict[str, int],
        dates,
        slot: str,
        headcount: int,
        travel_policy: str = "",
        authorization_scope=(),
    ) -> LoanRequest:
        """登记借调请求（技能需求、日期、人数、差旅与授权范围）。"""
        self._require_venue_manager(actor, receiving_venue_id, "登记借调请求")
        if not required_skills:
            raise ValidationError("技能需求不能为空")
        for skill, level in required_skills.items():
            if not isinstance(level, int) or level < 1:
                raise ValidationError(f"技能 {skill} 需求等级必须为正整数")
        days = tuple(sorted(set(dates)))
        if not days:
            raise ValidationError("借调日期不能为空")
        if slot not in SLOTS:
            raise ValidationError(f"未知时段：{slot}")
        if headcount < 1:
            raise ValidationError("需求人数必须为正整数")
        with self.store.lock:
            self._venue(receiving_venue_id)
            req = LoanRequest(
                request_id=self.store.new_id("req"),
                receiving_venue_id=receiving_venue_id,
                required_skills=dict(required_skills),
                dates=days,
                slot=slot,
                headcount=headcount,
                travel_policy=travel_policy,
                authorization_scope=tuple(authorization_scope),
            )
            self.store.requests[req.request_id] = req
            return req

    # ---------- 候选 ----------

    def propose(self, actor: Actor | None, request_id: str) -> matching.Proposal:
        """生成候选方案：排序结果附打分拆解，被拒者附冲突原因。"""
        with self.store.lock:
            req = self._request(request_id)
            self._require_venue_manager(actor, req.receiving_venue_id, "生成候选方案")
            if req.state == LifecycleState.ARCHIVED:
                raise StateError("请求已归档")
            proposal = matching.evaluate(self.store, req)
            self.store.proposals[request_id] = proposal
            if req.state == LifecycleState.DRAFT:
                req.state = LifecycleState.VERIFYING
                req.version += 1
            return proposal

    def get_proposal(self, actor: Actor | None, request_id: str) -> matching.Proposal:
        with self.store.lock:
            req = self._request(request_id)
            self._require_venue_manager(actor, req.receiving_venue_id, "查看候选方案")
            proposal = self.store.proposals.get(request_id)
            if proposal is None:
                raise NotFoundError("尚未生成候选方案")
            return proposal

    # ---------- 邀请与双边确认 ----------

    def invite(
        self,
        actor: Actor | None,
        request_id: str,
        volunteer_id: str,
        dates=None,
    ) -> tuple[Invitation, bool]:
        """发出邀请。重复邀请同一志愿者幂等返回已有邀请。"""
        with self.store.lock:
            req = self._request(request_id)
            self._require_venue_manager(actor, req.receiving_venue_id, "发出邀请")
            if req.state == LifecycleState.DRAFT:
                raise StateError("请先生成候选方案")
            if req.state == LifecycleState.ARCHIVED:
                raise StateError("请求已归档")
            if req.state == LifecycleState.CONFIRMED:
                raise StateError("需求人数已满足，无法继续邀请")
            existing = self.store.active_invitation(request_id, volunteer_id)
            if existing is not None:
                return existing, False  # 重复邀请：幂等
            vol = self._volunteer(volunteer_id)
            proposal = matching.evaluate(self.store, req)
            cand = next((c for c in proposal.candidates if c.volunteer_id == volunteer_id), None)
            if cand is None:
                rej = next((r for r in proposal.rejected if r.volunteer_id == volunteer_id), None)
                reasons = [c.to_dict() for c in rej.conflicts] if rej else []
                raise ConflictError("志愿者当前不满足借调条件", {"conflicts": reasons})
            offered = tuple(sorted(set(dates))) if dates is not None else cand.available_dates
            if not offered:
                raise ValidationError("邀请日期不能为空")
            unavailable = [d for d in offered if d not in cand.available_dates]
            if unavailable:
                reasons = [c.to_dict() for c in cand.conflicts if c.day in set(unavailable)]
                raise ConflictError(
                    "部分日期不可用",
                    {"unavailable": [d.isoformat() for d in unavailable], "conflicts": reasons},
                )
            inv = Invitation(self.store.new_id("inv"), request_id, volunteer_id, offered)
            self.store.invitations[inv.invitation_id] = inv
            return inv, True

    def confirm(
        self,
        actor: Actor | None,
        invitation_id: str,
        party: str,
        accepted_dates=None,
        expected_version: int | None = None,
    ) -> Invitation:
        """一方确认邀请（可只接受部分日期）。双方确认齐备后原子迁移占用。"""
        if party not in _CONFIRM_PARTIES:
            raise ValidationError(f"未知确认方：{party}")
        with self.store.lock:
            inv = self._invitation(invitation_id)
            req = self._request(inv.request_id)
            vol = self._volunteer(inv.volunteer_id)
            self._authorize_confirm(actor, req, vol, party)
            if inv.state in (InvitationState.DECLINED, InvitationState.EXPIRED):
                raise StateError(f"邀请已{inv.state.value}，无法确认")
            if inv.state == InvitationState.CONFIRMED:
                return inv  # 幂等：已确认的邀请重复确认直接返回
            if req.state == LifecycleState.ARCHIVED:
                raise StateError("请求已归档")
            if expected_version is not None and expected_version != inv.version:
                raise ConflictError(
                    "邀请已被他人更新，请刷新后重试",
                    {"current_version": inv.version},
                )
            if party == "guardian":
                if not vol.guardianship.requires_consent:
                    raise ValidationError("该志愿者无需监护人确认")
                conf = Confirmation(party, None, _now(), actor.actor_id)
            else:
                days = frozenset(accepted_dates) if accepted_dates is not None else frozenset(inv.offered_dates)
                if not days:
                    raise ValidationError("确认日期不能为空")
                extra = days - frozenset(inv.offered_dates)
                if extra:
                    raise ValidationError(
                        "确认日期必须属于提议日期",
                        {"extra": sorted(d.isoformat() for d in extra)},
                    )
                conf = Confirmation(party, days, _now(), actor.actor_id)
            prev = inv.confirmations.get(party)
            if (
                prev is not None
                and prev.accepted_dates == conf.accepted_dates
                and inv.state == InvitationState.PENDING
            ):
                return inv  # 幂等：待确认期间同一方相同日期的重复确认
            # 冲突状态下即使日期不变也视为重试：容量或占用可能已释放
            inv.confirmations[party] = conf
            inv.version += 1
            if inv.state == InvitationState.CONFLICT:
                inv.state = InvitationState.PENDING  # 重新确认后再次尝试迁移
                inv.conflict_reasons = []
            self._finalize(inv, req, vol)
            return inv

    def _finalize(self, inv: Invitation, req: LoanRequest, vol: Volunteer) -> None:
        """双方确认齐备后，原子完成占用迁移（须在 store.lock 内调用）。"""
        required = {"home_venue", "receiving_venue"}
        if vol.guardianship.requires_consent:
            required.add("guardian")
        if not required <= inv.confirmations.keys():
            return
        home_dates = inv.confirmations["home_venue"].accepted_dates
        recv_dates = inv.confirmations["receiving_venue"].accepted_dates
        days = sorted(home_dates & recv_dates)
        if not days:
            inv.state = InvitationState.CONFLICT
            inv.conflict_reasons = [
                {"code": "no_common_dates", "message": "双方确认的日期没有交集", "date": None}
            ]
            return
        # 先整体校验，全部通过才落任何变更（原子性）
        conflicts: list[dict] = []
        for day in days:
            occ = self.store.active_occupancy(vol.volunteer_id, day, req.slot)
            if occ is not None:
                venue = self.store.venues[occ.venue_id].name
                conflicts.append(
                    {
                        "code": "date_occupied",
                        "message": f"日期 {day.isoformat()} 志愿者已被借调至 {venue}",
                        "date": day.isoformat(),
                    }
                )
            rule = self.store.capacity.get((req.receiving_venue_id, day))
            if rule is None:
                conflicts.append(
                    {
                        "code": "capacity_unregistered",
                        "message": f"日期 {day.isoformat()} 接收方未登记容量",
                        "date": day.isoformat(),
                    }
                )
            else:
                used = self.store.capacity_usage(req.receiving_venue_id, day)
                if used + 1 > rule.max_loaned:
                    conflicts.append(
                        {
                            "code": "capacity_full",
                            "message": f"日期 {day.isoformat()} 接收方容量已满（{used}/{rule.max_loaned}）",
                            "date": day.isoformat(),
                        }
                    )
        if conflicts:
            inv.state = InvitationState.CONFLICT
            inv.conflict_reasons = conflicts
            return
        agreement = LoanAgreement(
            agreement_id=self.store.new_id("agr"),
            request_id=req.request_id,
            invitation_id=inv.invitation_id,
            volunteer_id=vol.volunteer_id,
            home_venue_id=vol.home_venue_id,
            receiving_venue_id=req.receiving_venue_id,
            dates=tuple(days),
            slot=req.slot,
            travel_plan={"policy": req.travel_policy, "arranged_by": req.receiving_venue_id},
            authorization_scope=req.authorization_scope,
        )
        for day in days:
            for shift in self.store.shifts_on(vol.volunteer_id, (day,), req.slot):
                shift.state = ShiftState.RELEASED
                shift.released_by = agreement.agreement_id
            occ = Occupancy(
                self.store.new_id("occ"),
                agreement.agreement_id,
                vol.volunteer_id,
                req.receiving_venue_id,
                day,
                req.slot,
            )
            self.store.occupancies[occ.occupancy_id] = occ
        self.store.agreements[agreement.agreement_id] = agreement
        inv.state = InvitationState.CONFIRMED
        inv.agreement_id = agreement.agreement_id
        inv.version += 1
        self._refresh_request_state(req)

    def decline(self, actor: Actor | None, invitation_id: str, party: str, reason: str = "") -> Invitation:
        if party not in _CONFIRM_PARTIES:
            raise ValidationError(f"未知确认方：{party}")
        with self.store.lock:
            inv = self._invitation(invitation_id)
            req = self._request(inv.request_id)
            vol = self._volunteer(inv.volunteer_id)
            self._authorize_confirm(actor, req, vol, party)
            if inv.state == InvitationState.CONFIRMED:
                raise StateError("邀请已确认，如需变更请使用召回")
            if inv.state in (InvitationState.DECLINED, InvitationState.EXPIRED):
                return inv  # 幂等
            inv.state = InvitationState.DECLINED
            inv.declined_by = party
            inv.decline_reason = reason
            inv.version += 1
            return inv

    # ---------- 协议：召回与推进 ----------

    def recall(self, actor: Actor | None, agreement_id: str, dates, reason: str) -> LoanAgreement:
        """临时召回：回收指定日期的占用、恢复被迁移的原排班。"""
        if not reason:
            raise ValidationError("召回必须说明原因")
        days = frozenset(dates)
        if not days:
            raise ValidationError("召回日期不能为空")
        with self.store.lock:
            agreement = self._agreement(agreement_id)
            self._require_venue_manager(actor, agreement.home_venue_id, "召回志愿者")
            if agreement.state not in (LifecycleState.CONFIRMED, LifecycleState.IN_PROGRESS):
                raise StateError("协议已归档，无法召回")
            invalid = days - set(agreement.remaining_dates)
            if invalid:
                raise ValidationError(
                    "包含未借调或已召回的日期",
                    {"invalid": sorted(d.isoformat() for d in invalid)},
                )
            for day in sorted(days):
                for occ in self.store.occupancies_for_agreement(agreement_id):
                    if occ.day == day and occ.state == OccupancyState.ACTIVE:
                        occ.state = OccupancyState.RECALLED
                for shift in self.store.shifts.values():
                    if shift.released_by == agreement_id and shift.day == day:
                        shift.state = ShiftState.ACTIVE
                        shift.released_by = None
            agreement.recalls.append(RecallRecord(tuple(sorted(days)), reason, actor.actor_id, _now()))
            agreement.version += 1
            if not agreement.remaining_dates:
                agreement.state = LifecycleState.ARCHIVED
                agreement.archive_reason = "recalled"
            self._refresh_request_state(self._request(agreement.request_id))
            return agreement

    def advance(self, actor: Actor | None, agreement_id: str) -> LoanAgreement:
        """推进协议状态：已确认 → 执行中 → 已归档。"""
        with self.store.lock:
            agreement = self._agreement(agreement_id)
            actor = self._require_actor(actor)
            if actor.role != Role.OPERATOR and not (
                actor.role == Role.VENUE_MANAGER
                and actor.venue_id in (agreement.home_venue_id, agreement.receiving_venue_id)
            ):
                raise PermissionDenied("仅借调链上的场馆负责人或运营员可推进协议")
            if agreement.state == LifecycleState.CONFIRMED:
                agreement.state = LifecycleState.IN_PROGRESS
            elif agreement.state == LifecycleState.IN_PROGRESS:
                agreement.state = LifecycleState.ARCHIVED
                agreement.archive_reason = "completed"
                for occ in self.store.occupancies_for_agreement(agreement_id):
                    if occ.state == OccupancyState.ACTIVE:
                        occ.state = OccupancyState.COMPLETED
            else:
                raise StateError("协议已归档")
            agreement.version += 1
            self._refresh_request_state(self._request(agreement.request_id))
            return agreement

    def _refresh_request_state(self, req: LoanRequest) -> None:
        """根据协议集合重算请求状态，并失效悬空的邀请。"""
        agreements = self.store.agreements_for_request(req.request_id)
        if any(a.state == LifecycleState.IN_PROGRESS for a in agreements):
            new_state = LifecycleState.IN_PROGRESS
        else:
            active = [a for a in agreements if a.state == LifecycleState.CONFIRMED]
            completed = [a for a in agreements if a.archive_reason == "completed"]
            if len(active) >= req.headcount:
                new_state = LifecycleState.CONFIRMED
            elif (
                agreements
                and all(a.state == LifecycleState.ARCHIVED for a in agreements)
                and len(completed) >= req.headcount
            ):
                new_state = LifecycleState.ARCHIVED
            elif req.state != LifecycleState.DRAFT:
                new_state = LifecycleState.VERIFYING
            else:
                new_state = req.state
        if new_state != req.state:
            req.state = new_state
            req.version += 1
        if new_state == LifecycleState.ARCHIVED:
            for inv in self.store.invitations_for_request(req.request_id):
                if inv.state in (InvitationState.PENDING, InvitationState.CONFLICT):
                    inv.state = InvitationState.EXPIRED
                    inv.version += 1

    # ---------- 查询（带可见性） ----------

    def get_request(self, actor: Actor | None, request_id: str) -> LoanRequest:
        with self.store.lock:
            req = self._request(request_id)
            actor = self._require_actor(actor)
            if actor.role == Role.OPERATOR:
                return req
            chain = {req.receiving_venue_id}
            for inv in self.store.invitations_for_request(request_id):
                chain.add(self._volunteer(inv.volunteer_id).home_venue_id)
            if actor.role == Role.VENUE_MANAGER and actor.venue_id in chain:
                return req
            raise PermissionDenied("仅借调链上的场馆负责人或运营员可查看请求")

    def get_invitation(self, actor: Actor | None, invitation_id: str) -> Invitation:
        with self.store.lock:
            inv = self._invitation(invitation_id)
            req = self._request(inv.request_id)
            vol = self._volunteer(inv.volunteer_id)
            actor = self._require_actor(actor)
            if actor.role == Role.OPERATOR:
                return inv
            if actor.role == Role.VENUE_MANAGER and actor.venue_id in (vol.home_venue_id, req.receiving_venue_id):
                return inv
            if actor.role == Role.VOLUNTEER and actor.volunteer_id == vol.volunteer_id:
                return inv
            if actor.role == Role.GUARDIAN and actor.actor_id == vol.guardianship.guardian_id:
                return inv
            raise PermissionDenied("仅借调链相关人员可查看邀请")

    def get_agreement(self, actor: Actor | None, agreement_id: str) -> LoanAgreement:
        with self.store.lock:
            agreement = self._agreement(agreement_id)
            vol = self._volunteer(agreement.volunteer_id)
            actor = self._require_actor(actor)
            if actor.role == Role.OPERATOR:
                return agreement
            if actor.role == Role.VENUE_MANAGER and actor.venue_id in (
                agreement.home_venue_id,
                agreement.receiving_venue_id,
            ):
                return agreement
            if actor.role == Role.VOLUNTEER and actor.volunteer_id == agreement.volunteer_id:
                return agreement
            if actor.role == Role.GUARDIAN and actor.actor_id == vol.guardianship.guardian_id:
                return agreement
            raise PermissionDenied("仅借调链相关人员可查看协议")

    def volunteer_view(self, actor: Actor | None, volunteer_id: str) -> dict:
        """按调用者身份返回志愿者视图，敏感资料仅借调链可见。"""
        with self.store.lock:
            vol = self._volunteer(volunteer_id)
            chain = visibility.chain_venue_ids(self.store, volunteer_id)
            return visibility.volunteer_view(actor, vol, chain)

    def calendar(self, actor: Actor | None, volunteer_id: str, start: date | None = None, end: date | None = None):
        """志愿者的排班与借调占用日历，仅借调链相关人员可见。"""
        with self.store.lock:
            vol = self._volunteer(volunteer_id)
            chain = visibility.chain_venue_ids(self.store, volunteer_id)
            if not visibility.can_view_sensitive(actor, vol, chain):
                raise PermissionDenied("仅借调链相关人员可查看排班与占用")

            def in_range(day: date) -> bool:
                return (start is None or day >= start) and (end is None or day <= end)

            shifts = [s for s in self.store.shifts.values() if s.volunteer_id == volunteer_id and in_range(s.day)]
            occs = [o for o in self.store.occupancies.values() if o.volunteer_id == volunteer_id and in_range(o.day)]
            return sorted(shifts, key=lambda s: (s.day, s.slot)), sorted(occs, key=lambda o: (o.day, o.slot))

    def requires_guardian(self, invitation: Invitation) -> bool:
        return self._volunteer(invitation.volunteer_id).guardianship.requires_consent
