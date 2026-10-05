"""节假日客流突增场景演示：从登记到候选、双边确认、原子迁移与临时召回。

运行：python3 tools/demo_scenario.py
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from secondment import views  # noqa: E402
from secondment.models import ActorContext, Role, SkillRequirement  # noqa: E402
from secondment.service import SecondmentService, ServiceError  # noqa: E402

DAYS = [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)]


def show(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    svc = SecondmentService(today=lambda: date(2026, 10, 2))
    admin = ActorContext("op-admin", Role.OPERATOR)
    op_a = ActorContext("op-a", Role.OPERATOR, "VEN-A")  # 文博中心（接收方）
    mgr_a = ActorContext("mgr-a", Role.VENUE_MANAGER, "VEN-A")
    mgr_b = ActorContext("mgr-b", Role.VENUE_MANAGER, "VEN-B")  # 科技馆（原场馆）

    svc.register_venue(admin, name="文博中心", venue_id="VEN-A")
    svc.register_venue(admin, name="科技馆", venue_id="VEN-B")
    svc.register_volunteer(mgr_b, name="王五", home_venue_id="VEN-B",
                           skills={"急救": 3, "讲解": 2}, volunteer_id="VOL-1")
    svc.register_volunteer(mgr_b, name="赵六", home_venue_id="VEN-B",
                           skills={"急救": 2}, volunteer_id="VOL-2")
    svc.register_volunteer(mgr_b, name="孙七", home_venue_id="VEN-B",
                           skills={"急救": 1}, volunteer_id="VOL-3")
    # 赵六假期前两天本有原场馆排班 —— 群聊时代无法发现，系统会如实呈现
    svc.add_schedule_entry(mgr_b, volunteer_id="VOL-2", day=DAYS[0])
    svc.add_schedule_entry(mgr_b, volunteer_id="VOL-2", day=DAYS[1])
    for day in DAYS:
        svc.register_capacity(op_a, venue_id="VEN-A", day=day, max_inbound=2)

    req = svc.create_loan_request(
        op_a,
        receiving_venue_id="VEN-A",
        requirements=[SkillRequirement("急救", 2, count=2)],
        dates=DAYS,
        note="国庆客流突增，急需急救岗",
    )
    show("借调请求", views.request_view(svc, req))

    report = svc.propose_candidates(op_a, req.id)
    show("候选排序（含逐项解释）与被拒原因", report)

    inv1, _ = svc.create_invitation(op_a, req.id, "VOL-1")
    inv2, _ = svc.create_invitation(op_a, req.id, "VOL-2")
    dup, is_dup = svc.create_invitation(op_a, req.id, "VOL-1")
    print(f"\n重复邀请幂等：{dup.id} == {inv1.id} -> {is_dup and dup.id == inv1.id}")

    # 王五全日期接受；赵六原场馆只放前两天（部分日期接受）
    svc.respond_invitation(mgr_b, inv1.id, accept=True)
    svc.respond_invitation(mgr_b, inv2.id, accept=True, accepted_dates=DAYS[:2])
    agr1, _ = svc.confirm_invitation(mgr_a, inv1.id)
    agr2, _ = svc.confirm_invitation(mgr_a, inv2.id)
    show("协议一（王五，全日期）", views.agreement_view(svc, agr1))
    show("协议二（赵六，部分日期）", views.agreement_view(svc, agr2))

    # 并发场景演示：此时再想把王五借给第三家会冲突
    svc.register_venue(admin, name="规划馆", venue_id="VEN-C")
    op_c = ActorContext("op-c", Role.OPERATOR, "VEN-C")
    req_c = svc.create_loan_request(
        op_c, receiving_venue_id="VEN-C",
        requirements=[SkillRequirement("急救", 2)], dates=[DAYS[0]],
    )
    try:
        svc.create_invitation(op_c, req_c.id, "VOL-1", dates=[DAYS[0]])
    except ServiceError as err:
        print(f"\n第三家场馆邀请王五被拒：{err.code} - {err.message}")

    # 临时召回：科技馆 10-03 临时缺人，召回王五最后一天
    svc.recall_agreement(mgr_b, agr1.id, dates=[DAYS[2]], reason="本馆临时缺人")
    show("召回后的协议一", views.agreement_view(svc, agr1))
    show("请求最终状态", views.request_view(svc, svc.store.requests[req.id]))


if __name__ == "__main__":
    main()
