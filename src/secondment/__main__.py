"""python -m secondment：启动 HTTP 服务，--seed 注入演示数据。"""
from __future__ import annotations

import argparse
from datetime import date, timedelta

from .api import build_server
from .models import Actor, Guardianship, Role
from .service import SecondmentService

OPERATOR = Actor(Role.OPERATOR, "op-admin")


def build_demo_service() -> SecondmentService:
    """节假日客流突增的演示场景：乙馆向甲馆借调讲解员。"""
    svc = SecondmentService()
    jia = svc.register_venue(OPERATOR, "甲馆·城市文博中心")
    yi = svc.register_venue(OPERATOR, "乙馆·自然科学馆")
    days = [date.today() + timedelta(days=i) for i in (1, 2, 3)]
    for day in days:
        svc.set_capacity(OPERATOR, yi.venue_id, day, 1)
    wang = svc.register_volunteer(
        OPERATOR,
        name="王芳",
        home_venue_id=jia.venue_id,
        skills={"讲解": 3, "急救": 2},
        sensitive={"phone": "13800000001", "id_number": "110101********0021"},
    )
    svc.register_volunteer(
        OPERATOR,
        name="李晓明",
        home_venue_id=jia.venue_id,
        skills={"讲解": 1},
        sensitive={"phone": "13800000002", "guardian_contact": "13900000002"},
        guardianship=Guardianship(
            guardian_id="guardian-li",
            guardian_name="李父",
            requires_consent=True,
            allowed_venue_ids=frozenset({yi.venue_id}),
            forbidden_slots=frozenset({"pm"}),
            max_consecutive_days=2,
        ),
    )
    svc.register_shift(OPERATOR, wang.volunteer_id, days[0], "am")
    manager_yi = Actor(Role.VENUE_MANAGER, "mgr-yi", venue_id=yi.venue_id)
    req = svc.create_request(
        manager_yi,
        receiving_venue_id=yi.venue_id,
        required_skills={"讲解": 1},
        dates=days,
        slot="am",
        headcount=1,
        travel_policy="高铁二等座实报实销，住宿由接收方统一安排",
        authorization_scope=("常设展讲解", "观众引导"),
    )
    svc.propose(manager_yi, req.request_id)
    return svc


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="跨场馆志愿者借调后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--seed", action="store_true", help="注入演示数据")
    args = parser.parse_args(argv)
    service = build_demo_service() if args.seed else SecondmentService()
    server = build_server(service, args.host, args.port)
    print(f"借调后端已启动：http://{args.host}:{args.port}（演示数据：{'是' if args.seed else '否'}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
