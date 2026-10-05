"""借调核心流程测试：部分日期接受、临时召回、重复邀请、并发确认。"""
from __future__ import annotations

import sys
import threading
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from secondment import (  # noqa: E402
    Actor,
    ConflictError,
    Guardianship,
    InvitationState,
    LifecycleState,
    PermissionDenied,
    Role,
    SecondmentService,
    StateError,
    ValidationError,
)
from secondment.models import OccupancyState, ShiftState  # noqa: E402

D1, D2, D3 = date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = SecondmentService()
        self.op = Actor(Role.OPERATOR, "op-1")
        self.home = self.svc.register_venue(self.op, "甲馆")
        self.recv = self.svc.register_venue(self.op, "乙馆")
        for day in (D1, D2, D3):
            self.svc.set_capacity(self.op, self.recv.venue_id, day, 1)
        self.home_mgr = Actor(Role.VENUE_MANAGER, "mgr-home", venue_id=self.home.venue_id)
        self.recv_mgr = Actor(Role.VENUE_MANAGER, "mgr-recv", venue_id=self.recv.venue_id)

    def add_volunteer(self, name="王芳", skills=None, guardianship=None, sensitive=None):
        return self.svc.register_volunteer(
            self.op,
            name=name,
            home_venue_id=self.home.venue_id,
            skills=skills or {"讲解": 2},
            sensitive=sensitive or {"phone": "13800000000"},
            guardianship=guardianship,
        )

    def add_request(self, dates=(D1, D2, D3), headcount=1, skills=None):
        return self.svc.create_request(
            self.recv_mgr,
            receiving_venue_id=self.recv.venue_id,
            required_skills=skills or {"讲解": 1},
            dates=list(dates),
            slot="am",
            headcount=headcount,
            travel_policy="高铁二等座实报实销",
            authorization_scope=("常设展讲解",),
        )

    def confirmed_agreement(self, vol, req, home_dates=None, recv_dates=None):
        """完成双边确认并返回邀请。"""
        self.svc.propose(self.recv_mgr, req.request_id)
        inv, _ = self.svc.invite(self.recv_mgr, req.request_id, vol.volunteer_id)
        self.svc.confirm(self.home_mgr, inv.invitation_id, "home_venue", home_dates)
        return self.svc.confirm(self.recv_mgr, inv.invitation_id, "receiving_venue", recv_dates)


class FlowTest(Base):
    def test_full_flow_migrates_occupancy_atomically(self) -> None:
        vol = self.add_volunteer()
        self.svc.register_shift(self.home_mgr, vol.volunteer_id, D1, "am")
        req = self.add_request()
        inv = self.confirmed_agreement(vol, req)

        self.assertEqual(inv.state, InvitationState.CONFIRMED)
        agreement = self.svc.get_agreement(self.op, inv.agreement_id)
        self.assertEqual(agreement.state, LifecycleState.CONFIRMED)
        self.assertEqual(agreement.dates, (D1, D2, D3))
        self.assertEqual(agreement.travel_plan["policy"], "高铁二等座实报实销")
        self.assertEqual(agreement.authorization_scope, ("常设展讲解",))
        # 原排班已迁移，占用已建立，容量已消耗
        shift = next(iter(self.svc.store.shifts.values()))
        self.assertEqual(shift.state, ShiftState.RELEASED)
        self.assertEqual(shift.released_by, agreement.agreement_id)
        self.assertEqual(self.svc.store.capacity_usage(self.recv.venue_id, D1), 1)
        self.assertEqual(self.svc.store.capacity_usage(self.recv.venue_id, D3), 1)
        self.assertEqual(self.svc.get_request(self.op, req.request_id).state, LifecycleState.CONFIRMED)

    def test_partial_date_acceptance_uses_intersection(self) -> None:
        vol = self.add_volunteer()
        self.svc.register_shift(self.home_mgr, vol.volunteer_id, D3, "am")
        req = self.add_request()
        inv = self.confirmed_agreement(vol, req, home_dates=[D1, D2], recv_dates=[D1, D2, D3])

        agreement = self.svc.get_agreement(self.op, inv.agreement_id)
        self.assertEqual(agreement.dates, (D1, D2))  # 只迁移交集日期
        self.assertEqual(self.svc.store.capacity_usage(self.recv.venue_id, D3), 0)
        shift = next(iter(self.svc.store.shifts.values()))
        self.assertEqual(shift.state, ShiftState.ACTIVE)  # D3 原排班不受影响

    def test_no_common_dates_reports_conflict(self) -> None:
        vol = self.add_volunteer()
        req = self.add_request()
        inv = self.confirmed_agreement(vol, req, home_dates=[D1], recv_dates=[D2])
        self.assertEqual(inv.state, InvitationState.CONFLICT)
        self.assertEqual(inv.conflict_reasons[0]["code"], "no_common_dates")

    def test_guardian_consent_required_before_migration(self) -> None:
        guard = Guardianship(guardian_id="g-1", guardian_name="李父", requires_consent=True)
        vol = self.add_volunteer(guardianship=guard)
        req = self.add_request()
        inv = self.confirmed_agreement(vol, req)
        # 双方确认后仍需监护人同意
        self.assertEqual(inv.state, InvitationState.PENDING)
        self.assertIsNone(inv.agreement_id)
        # 无关监护人不能确认
        stranger = Actor(Role.GUARDIAN, "g-x")
        with self.assertRaises(PermissionDenied):
            self.svc.confirm(stranger, inv.invitation_id, "guardian")
        # 监护人确认后迁移完成
        guardian = Actor(Role.GUARDIAN, "g-1")
        inv = self.svc.confirm(guardian, inv.invitation_id, "guardian")
        self.assertEqual(inv.state, InvitationState.CONFIRMED)
        self.assertIsNotNone(inv.agreement_id)

    def test_duplicate_invite_is_idempotent(self) -> None:
        vol = self.add_volunteer()
        req = self.add_request()
        self.svc.propose(self.recv_mgr, req.request_id)
        first, created1 = self.svc.invite(self.recv_mgr, req.request_id, vol.volunteer_id)
        second, created2 = self.svc.invite(self.recv_mgr, req.request_id, vol.volunteer_id)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first.invitation_id, second.invitation_id)
        self.assertEqual(len(self.svc.store.invitations), 1)
        # 拒绝后可以重新邀请（新的一轮）
        self.svc.decline(self.recv_mgr, first.invitation_id, "receiving_venue", "计划调整")
        third, created3 = self.svc.invite(self.recv_mgr, req.request_id, vol.volunteer_id)
        self.assertTrue(created3)
        self.assertNotEqual(first.invitation_id, third.invitation_id)

    def test_recall_restores_shift_and_capacity(self) -> None:
        vol = self.add_volunteer()
        self.svc.register_shift(self.home_mgr, vol.volunteer_id, D2, "am")
        req = self.add_request()
        inv = self.confirmed_agreement(vol, req)
        agreement_id = inv.agreement_id

        # 部分召回 D2：占用回收、排班恢复、容量释放
        agreement = self.svc.recall(self.home_mgr, agreement_id, [D2], "原场馆临时增援")
        self.assertEqual(agreement.state, LifecycleState.CONFIRMED)
        self.assertEqual(agreement.remaining_dates, (D1, D3))
        self.assertEqual(self.svc.store.capacity_usage(self.recv.venue_id, D2), 0)
        shift = next(iter(self.svc.store.shifts.values()))
        self.assertEqual(shift.state, ShiftState.ACTIVE)
        self.assertIsNone(shift.released_by)
        # 接收方无权召回
        with self.assertRaises(PermissionDenied):
            self.svc.recall(self.recv_mgr, agreement_id, [D1], "越权")
        # 重复召回同一日期报错
        with self.assertRaises(ValidationError):
            self.svc.recall(self.home_mgr, agreement_id, [D2], "重复")
        # 全部召回后协议归档，请求回到待核验
        agreement = self.svc.recall(self.home_mgr, agreement_id, [D1, D3], "全面召回")
        self.assertEqual(agreement.state, LifecycleState.ARCHIVED)
        self.assertEqual(agreement.archive_reason, "recalled")
        self.assertEqual(self.svc.get_request(self.op, req.request_id).state, LifecycleState.VERIFYING)

    def test_lifecycle_advance_and_archive(self) -> None:
        vol = self.add_volunteer()
        req = self.add_request()
        inv = self.confirmed_agreement(vol, req)
        agreement = self.svc.advance(self.recv_mgr, inv.agreement_id)
        self.assertEqual(agreement.state, LifecycleState.IN_PROGRESS)
        self.assertEqual(self.svc.get_request(self.op, req.request_id).state, LifecycleState.IN_PROGRESS)
        agreement = self.svc.advance(self.recv_mgr, inv.agreement_id)
        self.assertEqual(agreement.state, LifecycleState.ARCHIVED)
        self.assertEqual(agreement.archive_reason, "completed")
        self.assertEqual(self.svc.get_request(self.op, req.request_id).state, LifecycleState.ARCHIVED)
        with self.assertRaises(StateError):
            self.svc.advance(self.recv_mgr, inv.agreement_id)

    def test_confirm_validation_and_version_guard(self) -> None:
        vol = self.add_volunteer()
        req = self.add_request()
        self.svc.propose(self.recv_mgr, req.request_id)
        inv, _ = self.svc.invite(self.recv_mgr, req.request_id, vol.volunteer_id)
        # 确认日期必须属于提议日期
        with self.assertRaises(ValidationError):
            self.svc.confirm(self.home_mgr, inv.invitation_id, "home_venue", [date(2026, 12, 1)])
        # 乐观锁：版本不一致报冲突
        self.svc.confirm(self.home_mgr, inv.invitation_id, "home_venue")
        with self.assertRaises(ConflictError):
            self.svc.confirm(self.recv_mgr, inv.invitation_id, "receiving_venue", expected_version=0)
        # 同一方相同日期重复确认幂等
        again = self.svc.confirm(self.home_mgr, inv.invitation_id, "home_venue")
        self.assertEqual(again.version, inv.version)


class ConcurrencyTest(Base):
    def test_concurrent_confirm_same_volunteer_only_one_wins(self) -> None:
        """两个场馆同时抢同一名志愿者的同一日期：只有一个协议生效。"""
        other = self.svc.register_venue(self.op, "丙馆")
        self.svc.set_capacity(self.op, other.venue_id, D1, 1)
        other_mgr = Actor(Role.VENUE_MANAGER, "mgr-other", venue_id=other.venue_id)
        vol = self.add_volunteer()
        req_a = self.add_request(dates=(D1,))
        req_b = self.svc.create_request(
            other_mgr,
            receiving_venue_id=other.venue_id,
            required_skills={"讲解": 1},
            dates=[D1],
            slot="am",
            headcount=1,
        )
        invitations = []
        for req, mgr in ((req_a, self.recv_mgr), (req_b, other_mgr)):
            self.svc.propose(mgr, req.request_id)
            inv, _ = self.svc.invite(mgr, req.request_id, vol.volunteer_id)
            self.svc.confirm(self.home_mgr, inv.invitation_id, "home_venue")
            invitations.append((inv, mgr))

        barrier = threading.Barrier(2)
        results = []

        def race(inv, mgr):
            barrier.wait()
            results.append(self.svc.confirm(mgr, inv.invitation_id, "receiving_venue"))

        threads = [threading.Thread(target=race, args=pair) for pair in invitations]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        states = sorted((inv.state for inv in results), key=list(InvitationState).index)
        self.assertEqual(states, [InvitationState.CONFIRMED, InvitationState.CONFLICT])
        loser = next(inv for inv in results if inv.state == InvitationState.CONFLICT)
        self.assertEqual(loser.conflict_reasons[0]["code"], "date_occupied")
        # 全局只有一份协议、一条占用
        self.assertEqual(len(self.svc.store.agreements), 1)
        occs = [o for o in self.svc.store.occupancies.values() if o.state == OccupancyState.ACTIVE]
        self.assertEqual(len(occs), 1)
        # 冲突方可解释、可重试：胜方召回后，负方重新确认成功
        winner = next(inv for inv in results if inv.state == InvitationState.CONFIRMED)
        self.svc.recall(self.home_mgr, winner.agreement_id, [D1], "测试召回")
        retried = self.svc.confirm(other_mgr if loser.request_id == req_b.request_id else self.recv_mgr,
                                   loser.invitation_id, "receiving_venue")
        self.assertEqual(retried.state, InvitationState.CONFIRMED)

    def test_concurrent_confirm_capacity_race(self) -> None:
        """容量只剩 1 时两人同时确认：后到者得到容量冲突原因。"""
        vol1 = self.add_volunteer("甲")
        vol2 = self.add_volunteer("乙")
        req = self.add_request(dates=(D1,), headcount=2)
        self.svc.propose(self.recv_mgr, req.request_id)
        invitations = []
        for vol in (vol1, vol2):
            inv, _ = self.svc.invite(self.recv_mgr, req.request_id, vol.volunteer_id)
            self.svc.confirm(self.home_mgr, inv.invitation_id, "home_venue")
            invitations.append(inv)

        barrier = threading.Barrier(2)
        results = []

        def race(inv):
            barrier.wait()
            results.append(self.svc.confirm(self.recv_mgr, inv.invitation_id, "receiving_venue"))

        threads = [threading.Thread(target=race, args=(inv,)) for inv in invitations]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        states = sorted((inv.state for inv in results), key=list(InvitationState).index)
        self.assertEqual(states, [InvitationState.CONFIRMED, InvitationState.CONFLICT])
        loser = next(inv for inv in results if inv.state == InvitationState.CONFLICT)
        self.assertEqual(loser.conflict_reasons[0]["code"], "capacity_full")
        self.assertEqual(self.svc.store.capacity_usage(self.recv.venue_id, D1), 1)
        # 扩容后重新确认成功，请求人数满足
        self.svc.set_capacity(self.op, self.recv.venue_id, D1, 2)
        retried = self.svc.confirm(self.recv_mgr, loser.invitation_id, "receiving_venue")
        self.assertEqual(retried.state, InvitationState.CONFIRMED)
        self.assertEqual(self.svc.get_request(self.op, req.request_id).state, LifecycleState.CONFIRMED)


if __name__ == "__main__":
    unittest.main()
