"""领域对象的接口序列化（含敏感资料可见性过滤）。"""
from __future__ import annotations

from .models import (
    AGREEMENT_LABELS,
    INVITATION_LABELS,
    ActorContext,
    AgreementStatus,
    Capacity,
    GuardianRestriction,
    Invitation,
    LoanAgreement,
    LoanRequest,
    ScheduleEntry,
    Venue,
    Volunteer,
)
from .service import SecondmentService


def venue_view(venue: Venue) -> dict:
    return {"id": venue.id, "name": venue.name}


def volunteer_view(service: SecondmentService, actor: ActorContext, vol: Volunteer) -> dict:
    """敏感资料仅借调链相关人员可见，其余调用方只看到脱敏视图。"""
    visible = service.can_view_sensitive(actor, vol)
    view = {
        "id": vol.id,
        "name": vol.name,
        "home_venue_id": vol.home_venue_id,
        "skills": dict(vol.skills),
        "is_minor": vol.is_minor,
        "guardian_id": vol.guardian_id,
        "sensitive_visible": visible,
    }
    if visible:
        view["sensitive"] = {
            "phone": vol.sensitive.phone,
            "id_number": vol.sensitive.id_number,
            "emergency_contact": vol.sensitive.emergency_contact,
        }
    return view


def restriction_view(restriction: GuardianRestriction) -> dict:
    return {
        "volunteer_id": restriction.volunteer_id,
        "max_daily_hours": restriction.max_daily_hours,
        "allow_cross_venue": restriction.allow_cross_venue,
        "allowed_start": restriction.allowed_start.isoformat() if restriction.allowed_start else None,
        "allowed_end": restriction.allowed_end.isoformat() if restriction.allowed_end else None,
        "note": restriction.note,
    }


def schedule_entry_view(entry: ScheduleEntry) -> dict:
    return {
        "id": entry.id,
        "volunteer_id": entry.volunteer_id,
        "date": entry.day.isoformat(),
        "hours": entry.hours,
        "venue_id": entry.venue_id,
        "kind": entry.kind.value,
        "status": entry.status.value,
        "agreement_id": entry.agreement_id,
    }


def capacity_view(service: SecondmentService, capacity: Capacity) -> dict:
    return {
        "venue_id": capacity.venue_id,
        "date": capacity.day.isoformat(),
        "max_inbound": capacity.max_inbound,
        "used_inbound": service.used_inbound(capacity.venue_id, capacity.day),
        "remaining": service.capacity_remaining(capacity.venue_id, capacity.day),
    }


def request_view(service: SecondmentService, req: LoanRequest) -> dict:
    confirmed = service._confirmed_skill_counts(req.id)
    invitations = [i for i in service.store.invitations.values() if i.request_id == req.id]
    invitation_counts: dict[str, int] = {}
    for inv in invitations:
        invitation_counts[inv.status.value] = invitation_counts.get(inv.status.value, 0) + 1
    events = [
        e for e in service.store.events if e["detail"].get("request_id") == req.id
    ]
    return {
        "id": req.id,
        "receiving_venue_id": req.receiving_venue_id,
        "requirements": [
            {
                "skill": slot.skill,
                "min_level": slot.min_level,
                "count": slot.count,
                "confirmed": confirmed.get(slot.skill, 0),
                "remaining": max(slot.count - confirmed.get(slot.skill, 0), 0),
            }
            for slot in req.requirements
        ],
        "dates": [d.isoformat() for d in req.dates],
        "daily_hours": req.daily_hours,
        "note": req.note,
        "created_by": req.created_by,
        "status": service.request_status(req).value,
        "invitation_counts": invitation_counts,
        "events": events,
    }


def invitation_view(inv: Invitation) -> dict:
    return {
        "id": inv.id,
        "request_id": inv.request_id,
        "volunteer_id": inv.volunteer_id,
        "home_venue_id": inv.home_venue_id,
        "receiving_venue_id": inv.receiving_venue_id,
        "skill": inv.skill,
        "proposed_dates": [d.isoformat() for d in inv.proposed_dates],
        "accepted_dates": [d.isoformat() for d in inv.accepted_dates],
        "declined_dates": [d.isoformat() for d in inv.declined_dates],
        "status": inv.status.value,
        "status_label": INVITATION_LABELS[inv.status],
        "agreement_id": inv.agreement_id,
    }


def agreement_view(service: SecondmentService, agr: LoanAgreement) -> dict:
    status = service.agreement_status(agr)
    return {
        "id": agr.id,
        "request_id": agr.request_id,
        "invitation_id": agr.invitation_id,
        "volunteer_id": agr.volunteer_id,
        "home_venue_id": agr.home_venue_id,
        "receiving_venue_id": agr.receiving_venue_id,
        "skill": agr.skill,
        "dates": [d.isoformat() for d in agr.dates],
        "status": status.value,
        "status_label": AGREEMENT_LABELS[status],
        "loan_entry_ids": list(agr.loan_entry_ids),
        "released_entry_ids": list(agr.released_entry_ids),
        "recalls": [
            {
                "dates": [d.isoformat() for d in recall.dates],
                "by_actor": recall.by_actor,
                "by_venue_id": recall.by_venue_id,
                "reason": recall.reason,
                "at": recall.at.isoformat(),
            }
            for recall in agr.recalls
        ],
    }
