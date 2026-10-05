"""借调核心流程测试：候选排序、双边确认、原子迁移、召回、幂等与并发。"""
from __future__ import annotations

import sys
import threading
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from secondment.models import (  # noqa: E402
    ActorContext,
    AgreementStatus,
    EntryKind,
    EntryStatus,
    InvitationStatus,
    RequestStatus,
    Role,
    SensitiveProfile,
    SkillRequirement,
)
from secondment.service import (  # noqa: E402
    ALREADY_LOANED,
    CAPACITY_FULL,
    FORBIDDEN,
    GUARDIAN_DATE_OUT_OF_WINDOW,
    GUARDIAN_PROFILE_MISSING,
    NOT_CANDIDATE,
    SCHEDULE_CONFLICT,
    SKILL_MISMATCH,
    SecondmentService,
    ServiceError,
)

DAYS = [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)]


def make_service(today: date = date(2026, 10, 2)) -> SecondmentService:
    return SecondmentService(today=lambda: today)


class Fixture:
    """两馆三志愿者基线：VEN-A 为接收方，VEN-B 为原场馆。"""

    def __init__(self) -> None:
        self.service = make_service()
        svc = self.service
        self.admin = ActorContext("op-admin", Role.OPERATOR)
        self.op_a = ActorContext("op-a", Role.OPERATOR, "VEN-A")
        self.mgr_a = ActorContext("mgr-a", Role.VENUE_MANAGER, "VEN-A")
        self.mgr_b = ActorContext("mgr-b", Role.VENUE_MANAGER, "VEN-B")
        self.op_c = ActorContext("op-c", Role.OPERATOR, "VEN-C")
        svc.register_venue(self.admin, name="文博中心", venue_id="VEN-A")
        svc.register_venue(self.admin, name="科技馆", venue_id="VEN-B")
        svc.register_venue(self.admin, name="规划馆", venue_id="VEN-C")
        # VOL-1：技能富余、无排班 → 应排第一
        svc.register_volunteer(
            self.mgr_b, name="王五", home_venue_id="VEN-B",
            skills={"急救": 3, "讲解": 2}, volunteer_id="VOL-1",
        )
        # VOL-2：技能刚好达标、DAYS 中两天有原排班 → 迁移惩罚
        svc.register_volunteer(
            self.mgr_b, name="赵六", home_venue_id="VEN-B",
            skills={"急救": 2}, volunteer_id="VOL-2",
        )
        for day in DAYS[:2]:
            svc.add_schedule_entry(self.mgr_b, volunteer_id="VOL-2", day=day, hours=8)
        # VOL-3：技能不足
        svc.register_volunteer(
            self.mgr_b, name="孙七", home_venue_id="VEN-B",
            skills={"急救": 1}, volunteer_id="VOL-3",
        )
        for day in DAYS:
            svc.register_capacity(
                self.op_a, venue_id="VEN-A", day=day, max_inbound=5
            )

    def create_request(self, **overrides):
        params = dict(
            receiving_venue_id="VEN-A",
            requirements=[SkillRequirement("急救", 2, count=2)],
            dates=DAYS,
            daily_hours=8,
        )
        params.update(overrides)
        return self.service.create_loan_request(self.op_a, **params)


class ProposalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture()

    def test_ranking_and_explanations(self) -> None:
        req = self.fx.create_request()
        report = self.fx.service.propose_candidates(self.fx.op_a, req.id)
        candidates = report["candidates"]
        self.assertEqual([c["volunteer_id"] for c in candidates], ["VOL-1", "VOL-2"])
        top, second = candidates
        self.assertEqual(top["rank"], 1)
        self.assertEqual(top["score"], 50.0)  # 覆盖40 + 富余1级×10 - 0
        self.assertEqual(second["score"], 30.0)  # 覆盖40 + 富余0 - 迁移2天×5
        factors = {e["factor"] for e in top["explanation"]}
        self.assertEqual(factors, {"date_coverage", "skill_margin", "migration_penalty"})
        self.assertEqual(second["migratable_dates"], [d.isoformat() for d in DAYS[:2]])
        # 容量随候选报告一并返回，便于解释
        self.assertEqual(report["capacity"][0]["remaining"], 5)

    def test_rejected_candidates_carry_reasons(self) -> None:
        svc = self.fx.service
        svc.register_volunteer(
            self.fx.mgr_b, name="未成年无登记", home_venue_id="VEN-B",
            skills={"急救": 3}, is_minor=True, volunteer_id="VOL-4",
        )
        svc.register_volunteer(
            self.fx.mgr_b, name="未成年窗口外", home_venue_id="VEN-B",
            skills={"急救": 3}, is_minor=True, guardian_id="GRD-1", volunteer_id="VOL-5",
        )
        svc.register_guardian_restriction(
            self.fx.mgr_b, volunteer_id="VOL-5",
            allowed_start=date(2026, 11, 1), allowed_end=date(2026, 11, 30),
        )
        req = self.fx.create_request()
        report = svc.propose_candidates(self.fx.op_a, req.id)
        reasons = {r["volunteer_id"]: r["reasons"] for r in report["rejected"]}
        self.assertEqual(reasons["VOL-3"][0]["code"], SKILL_MISMATCH)
        self.assertEqual(reasons["VOL-4"][0]["code"], GUARDIAN_PROFILE_MISSING)
        self.assertEqual(reasons["VOL-5"][0]["code"], GUARDIAN_DATE_OUT_OF_WINDOW)
        # 被拒志愿者不能发出邀请
        with self.assertRaises(ServiceError) as ctx:
            svc.create_invitation(self.fx.op_a, req.id, "VOL-3")
        self.assertEqual(ctx.exception.code, NOT_CANDIDATE)
        self.assertEqual(ctx.exception.status, 409)


class FlowTest(unittest.TestCase):
    """双边确认 + 原子迁移 + 部分接受 + 召回。"""

    def setUp(self) -> None:
        self.fx = Fixture()
        self.req = self.fx.create_request()

    def _invite_accept_confirm(self, volunteer="VOL-1", accepted=None):
        svc = self.fx.service
        inv, dup = svc.create_invitation(self.fx.op_a, self.req.id, volunteer)
        self.assertFalse(dup)
        svc.respond_invitation(self.fx.mgr_b, inv.id, accept=True, accepted_dates=accepted)
        agr, created = svc.confirm_invitation(self.fx.mgr_a, inv.id)
        self.assertTrue(created)
        return inv, agr

    def test_full_flow_migrates_occupancy_atomically(self) -> None:
        svc = self.fx.service
        inv, agr = self._invite_accept_confirm("VOL-2")
        self.assertEqual(agr.dates, DAYS)
        # 原排班被释放、借调占用迁入接收方
        released = [svc.store.entries[i] for i in agr.released_entry_ids]
        self.assertEqual(len(released), 2)
        self.assertTrue(all(e.status is EntryStatus.RELEASED for e in released))
        loans = [svc.store.entries[i] for i in agr.loan_entry_ids]
        self.assertEqual(len(loans), 3)
        self.assertTrue(
            all(e.kind is EntryKind.LOAN and e.venue_id == "VEN-A" for e in loans)
        )
        # 容量被占用
        self.assertEqual(svc.capacity_remaining("VEN-A", DAYS[0]), 4)
        # 邀请与请求状态联动
        self.assertEqual(inv.status, InvitationStatus.CONFIRMED)
        self.assertEqual(svc.request_status(self.req), RequestStatus.ACTIVE)  # 今天在借调期内
        # 借调期间原场馆不能再给该志愿者排同一天的班
        with self.assertRaises(ServiceError) as ctx:
            svc.add_schedule_entry(self.fx.mgr_b, volunteer_id="VOL-2", day=DAYS[2])
        self.assertEqual(ctx.exception.code, SCHEDULE_CONFLICT)

    def test_confirm_is_idempotent(self) -> None:
        svc = self.fx.service
        inv, agr = self._invite_accept_confirm()
        again, created = svc.confirm_invitation(self.fx.mgr_a, inv.id)
        self.assertFalse(created)
        self.assertEqual(again.id, agr.id)
        loan_entries = [
            e for e in svc.store.entries.values() if e.kind is EntryKind.LOAN
        ]
        self.assertEqual(len(loan_entries), 3)  # 没有重复迁移

    def test_partial_date_acceptance(self) -> None:
        svc = self.fx.service
        inv, agr = self._invite_accept_confirm(accepted=[DAYS[0], DAYS[2]])
        self.assertEqual(agr.dates, [DAYS[0], DAYS[2]])
        self.assertEqual(inv.declined_dates, [DAYS[1]])
        # 未接受的日期不产生任何占用
        self.assertIsNone(svc.active_entry("VOL-1", DAYS[1]))

    def test_home_must_respond_before_confirm(self) -> None:
        svc = self.fx.service
        inv, _ = svc.create_invitation(self.fx.op_a, self.req.id, "VOL-1")
        with self.assertRaises(ServiceError) as ctx:
            svc.confirm_invitation(self.fx.mgr_a, inv.id)
        self.assertEqual(ctx.exception.status, 409)

    def test_recall_restores_occupancy_and_capacity(self) -> None:
        svc = self.fx.service
        _, agr = self._invite_accept_confirm("VOL-2")
        # 部分召回第一天：原排班恢复、容量释放
        svc.recall_agreement(self.fx.mgr_b, agr.id, dates=[DAYS[0]], reason="本馆临时缺人")
        home_entry = svc.store.entries[agr.released_entry_ids[0]]
        self.assertEqual(home_entry.day, DAYS[0])
        self.assertEqual(home_entry.status, EntryStatus.ACTIVE)
        loan_entry = svc.store.entries[agr.loan_entry_ids[0]]
        self.assertEqual(loan_entry.status, EntryStatus.CANCELLED)
        self.assertEqual(svc.capacity_remaining("VEN-A", DAYS[0]), 5)
        self.assertEqual(svc.agreement_status(agr), AgreementStatus.PARTIALLY_RECALLED)
        # 全部召回：协议与邀请联动终结，请求归档
        svc.recall_agreement(self.fx.mgr_b, agr.id, reason="节假日提前结束")
        self.assertEqual(svc.agreement_status(agr), AgreementStatus.RECALLED)
        self.assertEqual(svc.request_status(self.req), RequestStatus.ARCHIVED)
        # 全部召回后重复召回（不带日期）幂等返回
        again = svc.recall_agreement(self.fx.mgr_b, agr.id)
        self.assertEqual(again.id, agr.id)

    def test_duplicate_invitation_is_idempotent(self) -> None:
        svc = self.fx.service
        first, dup1 = svc.create_invitation(self.fx.op_a, self.req.id, "VOL-1")
        second, dup2 = svc.create_invitation(self.fx.op_a, self.req.id, "VOL-1")
        self.assertFalse(dup1)
        self.assertTrue(dup2)
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(svc.store.invitations), 1)

    def test_decline_then_reinvite_allowed(self) -> None:
        svc = self.fx.service
        inv, _ = svc.create_invitation(self.fx.op_a, self.req.id, "VOL-1")
        svc.respond_invitation(self.fx.mgr_b, inv.id, accept=False)
        new_inv, dup = svc.create_invitation(self.fx.op_a, self.req.id, "VOL-1")
        self.assertFalse(dup)
        self.assertNotEqual(new_inv.id, inv.id)


class ConcurrencyTest(unittest.TestCase):
    """并发确认：同一临界区内校验+迁移，保证只有一个成功。"""

    def _run_concurrently(self, tasks):
        barrier = threading.Barrier(len(tasks))
        results = []

        def run(fn):
            barrier.wait(timeout=5)
            try:
                results.append(("ok", fn()))
            except ServiceError as err:
                results.append(("err", err.code))

        threads = [threading.Thread(target=run, args=(t,)) for t in tasks]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(len(results), len(tasks))
        return results

    def test_concurrent_confirm_same_volunteer(self) -> None:
        fx = Fixture()
        svc = fx.service
        # 两个场馆同时向 VOL-1 发出重叠日期的借调请求
        req_a = fx.create_request()
        svc.register_capacity(fx.op_c, venue_id="VEN-C", day=DAYS[0], max_inbound=1)
        req_c = svc.create_loan_request(
            fx.op_c,
            receiving_venue_id="VEN-C",
            requirements=[SkillRequirement("急救", 2)],
            dates=[DAYS[0]],
        )
        inv_a, _ = svc.create_invitation(fx.op_a, req_a.id, "VOL-1", dates=[DAYS[0]])
        inv_c, _ = svc.create_invitation(fx.op_c, req_c.id, "VOL-1", dates=[DAYS[0]])
        svc.respond_invitation(fx.mgr_b, inv_a.id, accept=True)
        svc.respond_invitation(fx.mgr_b, inv_c.id, accept=True)
        results = self._run_concurrently(
            [
                lambda: svc.confirm_invitation(fx.mgr_a, inv_a.id),
                lambda: svc.confirm_invitation(fx.op_c, inv_c.id),
            ]
        )
        outcomes = sorted(r[0] for r in results)
        self.assertEqual(outcomes, ["err", "ok"])
        error = next(r for r in results if r[0] == "err")
        self.assertEqual(error[1], ALREADY_LOANED)
        # 同一日期至多一条有效借调占用
        active_loans = [
            e
            for e in svc.store.entries.values()
            if e.volunteer_id == "VOL-1"
            and e.day == DAYS[0]
            and e.kind is EntryKind.LOAN
            and e.status is EntryStatus.ACTIVE
        ]
        self.assertEqual(len(active_loans), 1)

    def test_concurrent_confirm_capacity_race(self) -> None:
        fx = Fixture()
        svc = fx.service
        for day in DAYS:
            svc.register_capacity(fx.op_a, venue_id="VEN-A", day=day, max_inbound=1)
        req = fx.create_request()
        inv1, _ = svc.create_invitation(fx.op_a, req.id, "VOL-1", dates=[DAYS[0]])
        inv2, _ = svc.create_invitation(fx.op_a, req.id, "VOL-2", dates=[DAYS[0]])
        svc.respond_invitation(fx.mgr_b, inv1.id, accept=True)
        svc.respond_invitation(fx.mgr_b, inv2.id, accept=True)
        results = self._run_concurrently(
            [
                lambda: svc.confirm_invitation(fx.mgr_a, inv1.id),
                lambda: svc.confirm_invitation(fx.mgr_a, inv2.id),
            ]
        )
        outcomes = sorted(r[0] for r in results)
        self.assertEqual(outcomes, ["err", "ok"])
        error = next(r for r in results if r[0] == "err")
        self.assertEqual(error[1], CAPACITY_FULL)
        self.assertEqual(svc.used_inbound("VEN-A", DAYS[0]), 1)


class VisibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture()
        svc = self.fx.service
        svc.register_volunteer(
            self.fx.mgr_b, name="李四", home_venue_id="VEN-B",
            skills={"急救": 3}, is_minor=True, guardian_id="GRD-1",
            volunteer_id="VOL-9",
            sensitive=SensitiveProfile(
                phone="13800000000", id_number="110101201001010000", emergency_contact="13900000000"
            ),
        )

    def test_sensitive_only_visible_to_chain(self) -> None:
        svc = self.fx.service
        vol = svc.store.volunteers["VOL-9"]
        # 无关场馆运营员不可见
        self.assertFalse(svc.can_view_sensitive(self.fx.op_c, vol))
        # 原场馆、本人、监护人可见
        self.assertTrue(svc.can_view_sensitive(self.fx.mgr_b, vol))
        self.assertTrue(
            svc.can_view_sensitive(ActorContext("VOL-9", Role.VOLUNTEER), vol)
        )
        self.assertTrue(
            svc.can_view_sensitive(ActorContext("GRD-1", Role.GUARDIAN), vol)
        )
        # 邀请发出后接收方进入借调链 → 可见
        svc.register_guardian_restriction(self.fx.mgr_b, volunteer_id="VOL-9")
        req = self.fx.create_request()
        svc.create_invitation(self.fx.op_a, req.id, "VOL-9")
        self.assertTrue(svc.can_view_sensitive(self.fx.op_a, vol))
        # 邀请被拒绝后借调链未成立 → 接收方不可见
        svc2 = Fixture().service
        req2 = svc2.create_loan_request(
            ActorContext("op-a", Role.OPERATOR, "VEN-A"),
            receiving_venue_id="VEN-A",
            requirements=[SkillRequirement("急救", 2)],
            dates=DAYS,
        )
        inv2, _ = svc2.create_invitation(
            ActorContext("op-a", Role.OPERATOR, "VEN-A"), req2.id, "VOL-1"
        )
        svc2.respond_invitation(
            ActorContext("mgr-b", Role.VENUE_MANAGER, "VEN-B"), inv2.id, accept=False
        )
        vol1 = svc2.store.volunteers["VOL-1"]
        self.assertFalse(
            svc2.can_view_sensitive(ActorContext("op-a", Role.OPERATOR, "VEN-A"), vol1)
        )


class AuthorizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture()
        self.req = self.fx.create_request()

    def test_only_home_venue_can_respond(self) -> None:
        svc = self.fx.service
        inv, _ = svc.create_invitation(self.fx.op_a, self.req.id, "VOL-1")
        with self.assertRaises(ServiceError) as ctx:
            svc.respond_invitation(self.fx.op_a, inv.id, accept=True)
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.code, FORBIDDEN)

    def test_only_receiver_can_confirm(self) -> None:
        svc = self.fx.service
        inv, _ = svc.create_invitation(self.fx.op_a, self.req.id, "VOL-1")
        svc.respond_invitation(self.fx.mgr_b, inv.id, accept=True)
        with self.assertRaises(ServiceError) as ctx:
            svc.confirm_invitation(self.fx.mgr_b, inv.id)
        self.assertEqual(ctx.exception.status, 403)

    def test_only_chain_venues_can_recall(self) -> None:
        svc = self.fx.service
        inv, _ = svc.create_invitation(self.fx.op_a, self.req.id, "VOL-1")
        svc.respond_invitation(self.fx.mgr_b, inv.id, accept=True)
        agr, _ = svc.confirm_invitation(self.fx.mgr_a, inv.id)
        with self.assertRaises(ServiceError) as ctx:
            svc.recall_agreement(self.fx.op_c, agr.id)
        self.assertEqual(ctx.exception.status, 403)

    def test_volunteer_cannot_create_request(self) -> None:
        svc = self.fx.service
        with self.assertRaises(ServiceError) as ctx:
            svc.create_loan_request(
                ActorContext("VOL-1", Role.VOLUNTEER),
                receiving_venue_id="VEN-A",
                requirements=[SkillRequirement("急救", 2)],
                dates=DAYS,
            )
        self.assertEqual(ctx.exception.status, 403)


class LifecycleTest(unittest.TestCase):
    def test_request_status_follows_contract_states(self) -> None:
        fx = Fixture()
        svc = fx.service
        req = fx.create_request()
        # 草拟 → 待核验 → 执行中（Fixture 时钟 2026-10-02 落在借调期内）
        self.assertEqual(svc.request_status(req), RequestStatus.DRAFT)
        inv, _ = svc.create_invitation(fx.op_a, req.id, "VOL-1")
        self.assertEqual(svc.request_status(req), RequestStatus.PENDING)
        svc.respond_invitation(fx.mgr_b, inv.id, accept=True)
        svc.confirm_invitation(fx.mgr_a, inv.id)
        self.assertEqual(svc.request_status(req), RequestStatus.ACTIVE)
        # 全部召回后归档
        agr = svc.store.agreements[inv.agreement_id]
        svc.recall_agreement(fx.mgr_b, agr.id)
        self.assertEqual(svc.request_status(req), RequestStatus.ARCHIVED)

    def test_confirmed_status_when_loan_not_started(self) -> None:
        clock = {"today": date(2026, 9, 1)}  # 借调尚未开始
        fx = Fixture()
        fx.service._today = lambda: clock["today"]
        req = fx.create_request()
        inv, _ = fx.service.create_invitation(fx.op_a, req.id, "VOL-1")
        fx.service.respond_invitation(fx.mgr_b, inv.id, accept=True)
        fx.service.confirm_invitation(fx.mgr_a, inv.id)
        self.assertEqual(fx.service.request_status(req), RequestStatus.CONFIRMED)


if __name__ == "__main__":
    unittest.main()
