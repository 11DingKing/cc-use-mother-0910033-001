"""模型 → JSON 友好字典的序列化。"""
from __future__ import annotations

from .matching import Proposal
from .models import (
    CapacityRule,
    Invitation,
    InvitationState,
    LoanAgreement,
    LoanRequest,
    Occupancy,
    Shift,
    Venue,
)


def venue_view(venue: Venue) -> dict:
    return {"venue_id": venue.venue_id, "name": venue.name}


def capacity_view(rule: CapacityRule, used: int) -> dict:
    return {
        "venue_id": rule.venue_id,
        "date": rule.day.isoformat(),
        "max_loaned": rule.max_loaned,
        "used": used,
    }


def shift_view(shift: Shift) -> dict:
    return {
        "shift_id": shift.shift_id,
        "volunteer_id": shift.volunteer_id,
        "venue_id": shift.venue_id,
        "date": shift.day.isoformat(),
        "slot": shift.slot,
        "state": shift.state.value,
        "released_by": shift.released_by,
    }


def occupancy_view(occ: Occupancy) -> dict:
    return {
        "occupancy_id": occ.occupancy_id,
        "agreement_id": occ.agreement_id,
        "volunteer_id": occ.volunteer_id,
        "venue_id": occ.venue_id,
        "date": occ.day.isoformat(),
        "slot": occ.slot,
        "state": occ.state.value,
    }


def request_view(req: LoanRequest, invitations=(), agreements=()) -> dict:
    return {
        "request_id": req.request_id,
        "receiving_venue_id": req.receiving_venue_id,
        "required_skills": dict(req.required_skills),
        "dates": [d.isoformat() for d in req.dates],
        "slot": req.slot,
        "headcount": req.headcount,
        "travel_policy": req.travel_policy,
        "authorization_scope": list(req.authorization_scope),
        "state": req.state.value,
        "version": req.version,
        "invitation_ids": [inv.invitation_id for inv in invitations],
        "agreement_ids": [ag.agreement_id for ag in agreements],
    }


def invitation_view(inv: Invitation, requires_guardian: bool = False) -> dict:
    required = ["home_venue", "receiving_venue"] + (["guardian"] if requires_guardian else [])
    awaiting = [p for p in required if p not in inv.confirmations] if inv.state == InvitationState.PENDING else []
    return {
        "invitation_id": inv.invitation_id,
        "request_id": inv.request_id,
        "volunteer_id": inv.volunteer_id,
        "offered_dates": [d.isoformat() for d in inv.offered_dates],
        "state": inv.state.value,
        "awaiting": awaiting,
        "confirmations": {
            party: {
                "accepted_dates": (
                    sorted(d.isoformat() for d in conf.accepted_dates) if conf.accepted_dates is not None else None
                ),
                "at": conf.at.isoformat(),
                "by": conf.by,
            }
            for party, conf in inv.confirmations.items()
        },
        "conflict_reasons": list(inv.conflict_reasons),
        "agreement_id": inv.agreement_id,
        "declined_by": inv.declined_by,
        "decline_reason": inv.decline_reason,
        "version": inv.version,
    }


def agreement_view(agreement: LoanAgreement) -> dict:
    return {
        "agreement_id": agreement.agreement_id,
        "request_id": agreement.request_id,
        "invitation_id": agreement.invitation_id,
        "volunteer_id": agreement.volunteer_id,
        "home_venue_id": agreement.home_venue_id,
        "receiving_venue_id": agreement.receiving_venue_id,
        "dates": [d.isoformat() for d in agreement.dates],
        "recalled_dates": sorted(d.isoformat() for d in agreement.recalled_dates),
        "remaining_dates": [d.isoformat() for d in agreement.remaining_dates],
        "slot": agreement.slot,
        "travel_plan": dict(agreement.travel_plan),
        "authorization_scope": list(agreement.authorization_scope),
        "state": agreement.state.value,
        "archive_reason": agreement.archive_reason,
        "recalls": [
            {
                "dates": [d.isoformat() for d in record.dates],
                "reason": record.reason,
                "by": record.by,
                "at": record.at.isoformat(),
            }
            for record in agreement.recalls
        ],
        "version": agreement.version,
    }


def proposal_view(proposal: Proposal) -> dict:
    return {
        "request_id": proposal.request_id,
        "generated_at": proposal.generated_at.isoformat(),
        "candidates": [
            {
                "volunteer_id": cand.volunteer_id,
                "score": cand.score,
                "available_dates": [d.isoformat() for d in cand.available_dates],
                "breakdown": [
                    {"factor": part.factor, "points": part.points, "detail": part.detail}
                    for part in cand.breakdown
                ],
                "conflicts": [conflict.to_dict() for conflict in cand.conflicts],
            }
            for cand in proposal.candidates
        ],
        "rejected": [
            {
                "volunteer_id": rej.volunteer_id,
                "conflicts": [conflict.to_dict() for conflict in rej.conflicts],
            }
            for rej in proposal.rejected
        ],
    }
