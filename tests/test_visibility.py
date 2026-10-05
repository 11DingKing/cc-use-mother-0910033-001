"""敏感资料可见性测试：仅借调链相关人员可见。"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from secondment import Actor, Guardianship, PermissionDenied, Role, SecondmentService  # noqa: E402

D1 = date(2026, 10, 6)


class VisibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = SecondmentService()
        self.op = Actor(Role.OPERATOR, "op-1")
        self.home = self.svc.register_venue(self.op, "甲馆")
        self.recv = self.svc.register_venue(self.op, "乙馆")
        self.outside = self.svc.register_venue(self.op, "丙馆")
        self.svc.set_capacity(self.op, self.recv.venue_id, D1, 1)
        self.home_mgr = Actor(Role.VENUE_MANAGER, "mgr-home", venue_id=self.home.venue_id)
        self.recv_mgr = Actor(Role.VENUE_MANAGER, "mgr-recv", venue_id=self.recv.venue_id)
        self.outside_mgr = Actor(Role.VENUE_MANAGER, "mgr-out", venue_id=self.outside.venue_id)
        self.vol = self.svc.register_volunteer(
            self.op,
            name="李晓明",
            home_venue_id=self.home.venue_id,
            skills={"讲解": 2},
            sensitive={"phone": "13800000002", "guardian_contact": "13900000002"},
            guardianship=Guardianship(guardian_id="g-1", guardian_name="李父", requires_consent=True),
        )

    def _make_request_and_invite(self):
        req = self.svc.create_request(
            self.recv_mgr,
            receiving_venue_id=self.recv.venue_id,
            required_skills={"讲解": 1},
            dates=[D1],
            slot="am",
            headcount=1,
        )
        self.svc.propose(self.recv_mgr, req.request_id)
        inv, _ = self.svc.invite(self.recv_mgr, req.request_id, self.vol.volunteer_id)
        return inv

    def test_outsider_sees_masked_profile(self) -> None:
        view = self.svc.volunteer_view(self.outside_mgr, self.vol.volunteer_id)
        self.assertEqual(view["sensitive"], {"phone": "***", "guardian_contact": "***"})
        self.assertEqual(view["guardianship"], "restricted")
        self.assertEqual(view["skills"], {"讲解": 2})  # 技能不属于敏感资料
        # 匿名调用者同样脱敏
        anon = self.svc.volunteer_view(None, self.vol.volunteer_id)
        self.assertEqual(anon["sensitive"]["phone"], "***")

    def test_chain_parties_see_full_profile(self) -> None:
        for actor in (self.op, self.home_mgr, Actor(Role.GUARDIAN, "g-1"),
                      Actor(Role.VOLUNTEER, "self", volunteer_id=self.vol.volunteer_id)):
            view = self.svc.volunteer_view(actor, self.vol.volunteer_id)
            self.assertEqual(view["sensitive"]["phone"], "13800000002", actor)
        # 接收方在邀请发出后进入借调链
        self.assertEqual(self.svc.volunteer_view(self.recv_mgr, self.vol.volunteer_id)["sensitive"]["phone"], "***")
        self._make_request_and_invite()
        view = self.svc.volunteer_view(self.recv_mgr, self.vol.volunteer_id)
        self.assertEqual(view["sensitive"]["phone"], "13800000002")
        self.assertTrue(view["guardianship"]["requires_consent"])

    def test_chain_breaks_after_archive(self) -> None:
        inv = self._make_request_and_invite()
        self.svc.confirm(self.home_mgr, inv.invitation_id, "home_venue")
        self.svc.confirm(self.recv_mgr, inv.invitation_id, "receiving_venue")
        self.svc.confirm(Actor(Role.GUARDIAN, "g-1"), inv.invitation_id, "guardian")
        agreement_id = inv.agreement_id
        self.assertEqual(self.svc.volunteer_view(self.recv_mgr, self.vol.volunteer_id)["sensitive"]["phone"],
                         "13800000002")
        self.svc.advance(self.recv_mgr, agreement_id)
        self.svc.advance(self.recv_mgr, agreement_id)  # 归档
        view = self.svc.volunteer_view(self.recv_mgr, self.vol.volunteer_id)
        self.assertEqual(view["sensitive"]["phone"], "***")

    def test_calendar_is_chain_only(self) -> None:
        self.svc.register_shift(self.home_mgr, self.vol.volunteer_id, D1, "am")
        with self.assertRaises(PermissionDenied):
            self.svc.calendar(self.outside_mgr, self.vol.volunteer_id)
        shifts, occs = self.svc.calendar(self.home_mgr, self.vol.volunteer_id)
        self.assertEqual(len(shifts), 1)
        self.assertEqual(occs, [])


if __name__ == "__main__":
    unittest.main()
