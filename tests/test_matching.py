"""候选排序与冲突原因解释测试。"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from secondment import Actor, Guardianship, Role, SecondmentService  # noqa: E402

D1, D2, D3 = date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)


class MatchingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = SecondmentService()
        self.op = Actor(Role.OPERATOR, "op-1")
        self.home = self.svc.register_venue(self.op, "甲馆")
        self.recv = self.svc.register_venue(self.op, "乙馆")
        for day in (D1, D2, D3):
            self.svc.set_capacity(self.op, self.recv.venue_id, day, 2)
        self.recv_mgr = Actor(Role.VENUE_MANAGER, "mgr-recv", venue_id=self.recv.venue_id)
        self.home_mgr = Actor(Role.VENUE_MANAGER, "mgr-home", venue_id=self.home.venue_id)

    def _volunteer(self, name, skills, guardianship=None, home=None):
        return self.svc.register_volunteer(
            self.op,
            name=name,
            home_venue_id=(home or self.home).venue_id,
            skills=skills,
            guardianship=guardianship,
        )

    def _request(self, dates=(D1, D2, D3), skills=None):
        return self.svc.create_request(
            self.recv_mgr,
            receiving_venue_id=self.recv.venue_id,
            required_skills=skills or {"讲解": 1},
            dates=list(dates),
            slot="am",
            headcount=1,
        )

    def test_ranking_with_score_breakdown(self) -> None:
        strong = self._volunteer("强", {"讲解": 3})
        weak = self._volunteer("弱", {"讲解": 1})
        self.svc.register_shift(self.home_mgr, weak.volunteer_id, D1, "am")
        req = self._request()
        proposal = self.svc.propose(self.recv_mgr, req.request_id)

        self.assertEqual([c.volunteer_id for c in proposal.candidates], [strong.volunteer_id, weak.volunteer_id])
        top = proposal.candidates[0]
        self.assertGreater(top.score, proposal.candidates[1].score)
        factors = {part.factor for part in top.breakdown}
        self.assertIn("skill", factors)
        self.assertIn("availability", factors)
        # 弱候选有原排班迁移扣分说明
        weak_parts = {p.factor: p for p in proposal.candidates[1].breakdown}
        self.assertIn("home_shift", weak_parts)
        self.assertLess(weak_parts["home_shift"].points, 0)
        self.assertIn("迁移", weak_parts["home_shift"].detail)

    def test_rejected_with_conflict_reasons(self) -> None:
        no_skill = self._volunteer("无技能", {"引导": 1})
        same_venue = self._volunteer("本馆", {"讲解": 2}, home=self.recv)
        restricted = self._volunteer(
            "受限", {"讲解": 2},
            guardianship=Guardianship(allowed_venue_ids=frozenset({"venue-else"})),
        )
        req = self._request()
        proposal = self.svc.propose(self.recv_mgr, req.request_id)

        rejected = {r.volunteer_id: r for r in proposal.rejected}
        self.assertEqual(rejected[no_skill.volunteer_id].conflicts[0].code, "skill_missing")
        self.assertEqual(rejected[same_venue.volunteer_id].conflicts[0].code, "same_venue")
        self.assertEqual(rejected[restricted.volunteer_id].conflicts[0].code, "guardian_venue")
        self.assertIn("讲解", rejected[no_skill.volunteer_id].conflicts[0].message)

    def test_partial_availability_and_capacity_explanation(self) -> None:
        vol = self._volunteer("忙", {"讲解": 2})
        req = self._request()
        # 先在 D2 制造一个生效中的占用
        other_req = self._request(dates=(D2,))
        self.svc.propose(self.recv_mgr, other_req.request_id)
        inv, _ = self.svc.invite(self.recv_mgr, other_req.request_id, vol.volunteer_id)
        self.svc.confirm(self.home_mgr, inv.invitation_id, "home_venue")
        self.svc.confirm(self.recv_mgr, inv.invitation_id, "receiving_venue")

        proposal = self.svc.propose(self.recv_mgr, req.request_id)
        cand = next(c for c in proposal.candidates if c.volunteer_id == vol.volunteer_id)
        self.assertEqual(cand.available_dates, (D1, D3))
        self.assertEqual(cand.conflicts[0].code, "date_occupied")
        self.assertEqual(cand.conflicts[0].day, D2)

    def test_unregistered_capacity_blocks_date(self) -> None:
        self._volunteer("闲", {"讲解": 2})
        d4 = date(2026, 10, 9)  # 未登记容量
        req = self._request(dates=(D1, d4))
        proposal = self.svc.propose(self.recv_mgr, req.request_id)
        cand = proposal.candidates[0]
        self.assertEqual(cand.available_dates, (D1,))
        self.assertEqual(cand.conflicts[0].code, "capacity_unregistered")

    def test_guardian_consecutive_days_limit(self) -> None:
        self._volunteer(
            "未成年", {"讲解": 2},
            guardianship=Guardianship(max_consecutive_days=2),
        )
        req = self._request()  # 连续 3 天
        proposal = self.svc.propose(self.recv_mgr, req.request_id)
        self.assertEqual(proposal.candidates, [])
        self.assertEqual(proposal.rejected[0].conflicts[0].code, "guardian_consecutive_days")


if __name__ == "__main__":
    unittest.main()
