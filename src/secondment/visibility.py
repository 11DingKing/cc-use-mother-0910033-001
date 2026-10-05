"""敏感资料可见性：仅借调链相关人员可见（关联方权限隔离）。

借调链 = 志愿者的原场馆 + 进行中借调的接收场馆。
借调归档后接收方即退出借调链，敏感资料重新不可见。
"""
from __future__ import annotations

from .models import Actor, InvitationState, LifecycleState, Role, Volunteer

_CHAIN_INVITATION_STATES = (InvitationState.PENDING, InvitationState.CONFLICT)


def chain_venue_ids(store, volunteer_id: str) -> set[str]:
    """借调链上的场馆集合。"""
    vol = store.volunteers[volunteer_id]
    ids = {vol.home_venue_id}
    for inv in store.invitations.values():
        if inv.volunteer_id == volunteer_id and inv.state in _CHAIN_INVITATION_STATES:
            ids.add(store.requests[inv.request_id].receiving_venue_id)
    for agreement in store.agreements.values():
        if agreement.volunteer_id == volunteer_id and agreement.state != LifecycleState.ARCHIVED:
            ids.add(agreement.receiving_venue_id)
    return ids


def can_view_sensitive(actor: Actor | None, volunteer: Volunteer, chain: set[str]) -> bool:
    if actor is None:
        return False
    if actor.role == Role.OPERATOR:
        return True
    if actor.role == Role.VOLUNTEER and actor.volunteer_id == volunteer.volunteer_id:
        return True
    if actor.role == Role.GUARDIAN and actor.actor_id == volunteer.guardianship.guardian_id:
        return True
    if actor.role == Role.VENUE_MANAGER and actor.venue_id in chain:
        return True
    return False


def volunteer_view(actor: Actor | None, volunteer: Volunteer, chain: set[str]) -> dict:
    """按调用者身份生成志愿者视图；链外人员仅见脱敏资料。"""
    view = {
        "volunteer_id": volunteer.volunteer_id,
        "name": volunteer.name,
        "home_venue_id": volunteer.home_venue_id,
        "skills": dict(volunteer.skills),
        "active": volunteer.active,
    }
    if can_view_sensitive(actor, volunteer, chain):
        guard = volunteer.guardianship
        view["sensitive"] = dict(volunteer.sensitive)
        view["guardianship"] = {
            "requires_consent": guard.requires_consent,
            "guardian_name": guard.guardian_name,
            "allowed_venue_ids": sorted(guard.allowed_venue_ids) if guard.allowed_venue_ids is not None else None,
            "forbidden_slots": sorted(guard.forbidden_slots),
            "max_consecutive_days": guard.max_consecutive_days,
        }
    else:
        view["sensitive"] = {key: "***" for key in volunteer.sensitive}
        view["guardianship"] = "restricted"
    return view
