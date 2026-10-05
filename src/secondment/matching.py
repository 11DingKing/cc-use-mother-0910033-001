"""候选评估：给出排序解释（打分拆解）与冲突原因。

评估为纯函数：只读 Store，不写任何状态。排序规则确定可复现：
分数降序，同分按志愿者编号升序。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from .models import LoanRequest


@dataclass
class Conflict:
    """一条冲突原因。"""

    code: str
    message: str
    day: date | None = None

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "date": self.day.isoformat() if self.day else None,
        }


@dataclass
class ScorePart:
    """打分拆解中的一项。"""

    factor: str
    points: int
    detail: str


@dataclass
class Candidate:
    volunteer_id: str
    score: int
    breakdown: list[ScorePart]
    available_dates: tuple[date, ...]
    conflicts: list[Conflict]  # 不可用日期的原因（信息性）


@dataclass
class Rejected:
    volunteer_id: str
    conflicts: list[Conflict]


@dataclass
class Proposal:
    request_id: str
    generated_at: datetime
    candidates: list[Candidate]
    rejected: list[Rejected]


def _longest_run(days: tuple[date, ...]) -> int:
    """日期序列中最长连续段的长度。"""
    if not days:
        return 0
    best = run = 1
    for prev, cur in zip(days, days[1:]):
        if (cur - prev).days == 1:
            run += 1
            best = max(best, run)
        else:
            run = 1
    return best


def evaluate(store, request: LoanRequest) -> Proposal:
    """对全体志愿者逐一评估，返回排序后的候选与被拒原因。"""
    candidates: list[Candidate] = []
    rejected: list[Rejected] = []
    for vol in sorted(store.volunteers.values(), key=lambda v: v.volunteer_id):
        global_conflicts: list[Conflict] = []
        if not vol.active:
            global_conflicts.append(Conflict("inactive", "志愿者已停用"))
        if vol.home_venue_id == request.receiving_venue_id:
            global_conflicts.append(Conflict("same_venue", "与接收场馆相同，无需借调"))
        for skill, need in request.required_skills.items():
            have = vol.skills.get(skill, 0)
            if have < need:
                global_conflicts.append(
                    Conflict("skill_missing", f"缺少技能：{skill}（需 L{need}，实际 L{have}）")
                )
        guard = vol.guardianship
        if guard.allowed_venue_ids is not None and request.receiving_venue_id not in guard.allowed_venue_ids:
            global_conflicts.append(Conflict("guardian_venue", "监护限制：不允许前往该场馆"))
        if request.slot in guard.forbidden_slots:
            global_conflicts.append(Conflict("guardian_slot", f"监护限制：禁止时段 {request.slot}"))
        if guard.max_consecutive_days is not None and _longest_run(request.dates) > guard.max_consecutive_days:
            global_conflicts.append(
                Conflict("guardian_consecutive_days", f"监护限制：连续服务超过 {guard.max_consecutive_days} 天上限")
            )
        if global_conflicts:
            rejected.append(Rejected(vol.volunteer_id, global_conflicts))
            continue

        # 逐日检查：占用与接收方容量
        day_conflicts: list[Conflict] = []
        available: list[date] = []
        for day in request.dates:
            occ = store.active_occupancy(vol.volunteer_id, day, request.slot)
            if occ is not None:
                venue = store.venues[occ.venue_id].name
                day_conflicts.append(Conflict("date_occupied", f"日期 {day.isoformat()} 已被借调至 {venue}", day))
                continue
            rule = store.capacity.get((request.receiving_venue_id, day))
            if rule is None:
                day_conflicts.append(Conflict("capacity_unregistered", f"日期 {day.isoformat()} 接收方未登记容量", day))
                continue
            used = store.capacity_usage(request.receiving_venue_id, day)
            if used + 1 > rule.max_loaned:
                day_conflicts.append(
                    Conflict("capacity_full", f"日期 {day.isoformat()} 接收方容量已满（{used}/{rule.max_loaned}）", day)
                )
                continue
            available.append(day)
        if not available:
            rejected.append(Rejected(vol.volunteer_id, day_conflicts))
            continue

        # 打分：技能、可覆盖天数、原排班迁移成本、场馆熟悉度
        breakdown: list[ScorePart] = []
        score = 0
        for skill, need in request.required_skills.items():
            have = vol.skills.get(skill, 0)
            points = 10 + min(have - need, 3) * 2
            breakdown.append(ScorePart("skill", points, f"技能 {skill} L{have}/需 L{need}"))
            score += points
        coverage = round(10 * len(available) / len(request.dates))
        breakdown.append(ScorePart("availability", coverage, f"可覆盖 {len(available)}/{len(request.dates)} 天"))
        score += coverage
        home_shifts = store.shifts_on(vol.volunteer_id, request.dates, request.slot)
        if home_shifts:
            points = -3 * len(home_shifts)
            breakdown.append(
                ScorePart("home_shift", points, f"{len(home_shifts)} 天原场馆已有排班，确认后将迁移占用")
            )
            score += points
        if any(a.receiving_venue_id == request.receiving_venue_id for a in store.agreements_for_volunteer(vol.volunteer_id)):
            breakdown.append(ScorePart("familiarity", 4, "曾借调至该馆，熟悉环境"))
            score += 4
        candidates.append(Candidate(vol.volunteer_id, score, breakdown, tuple(available), day_conflicts))

    candidates.sort(key=lambda c: (-c.score, c.volunteer_id))
    return Proposal(request.request_id, datetime.now(timezone.utc), candidates, rejected)
