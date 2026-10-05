"""HTTP 接口端到端测试。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from secondment import SecondmentService  # noqa: E402
from secondment.api import build_server  # noqa: E402

OP = {"X-Actor-Role": "operator", "X-Actor-Id": "op-1"}


def manager(venue_id: str, actor_id: str = "mgr") -> dict:
    return {"X-Actor-Role": "venue_manager", "X-Actor-Id": actor_id, "X-Venue-Id": venue_id}


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = build_server(SecondmentService(), "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            method=method,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def test_end_to_end_flow(self) -> None:
        # 登记场馆、容量、志愿者、原排班
        status, jia = self.call("POST", "/venues", {"name": "甲馆"}, OP)
        self.assertEqual(status, 201)
        status, yi = self.call("POST", "/venues", {"name": "乙馆"}, OP)
        jia_id, yi_id = jia["venue_id"], yi["venue_id"]
        for day in ("2026-10-06", "2026-10-07"):
            status, cap = self.call("POST", f"/venues/{yi_id}/capacity",
                                    {"date": day, "max_loaned": 1}, manager(yi_id))
            self.assertEqual(status, 200)
            self.assertEqual(cap["used"], 0)
        status, vol = self.call("POST", "/volunteers", {
            "name": "王芳", "home_venue_id": jia_id, "skills": {"讲解": 2},
            "sensitive": {"phone": "13800000001"},
        }, OP)
        self.assertEqual(status, 201)
        vol_id = vol["volunteer_id"]
        status, _ = self.call("POST", f"/volunteers/{vol_id}/shifts",
                              {"date": "2026-10-06", "slot": "am"}, manager(jia_id))
        self.assertEqual(status, 201)

        # 借调请求 → 候选（含排序解释）
        status, req = self.call("POST", "/loan-requests", {
            "receiving_venue_id": yi_id, "required_skills": {"讲解": 1},
            "dates": ["2026-10-06", "2026-10-07"], "slot": "am", "headcount": 1,
            "travel_policy": "实报实销", "authorization_scope": ["常设展讲解"],
        }, manager(yi_id))
        self.assertEqual(status, 201)
        self.assertEqual(req["state"], "草拟")
        req_id = req["request_id"]
        status, proposal = self.call("POST", f"/loan-requests/{req_id}/proposals", {}, manager(yi_id))
        self.assertEqual(status, 200)
        self.assertEqual(proposal["candidates"][0]["volunteer_id"], vol_id)
        self.assertTrue(any(p["factor"] == "home_shift" for p in proposal["candidates"][0]["breakdown"]))

        # 邀请 → 双边确认（部分日期）→ 协议
        status, inv = self.call("POST", f"/loan-requests/{req_id}/invitations",
                                {"volunteer_id": vol_id}, manager(yi_id))
        self.assertEqual(status, 201)
        inv_id = inv["invitation_id"]
        status, dup = self.call("POST", f"/loan-requests/{req_id}/invitations",
                                {"volunteer_id": vol_id}, manager(yi_id))
        self.assertEqual((status, dup["invitation_id"]), (200, inv_id))  # 重复邀请幂等
        status, inv = self.call("POST", f"/invitations/{inv_id}/confirm",
                                {"party": "home_venue", "accepted_dates": ["2026-10-06"]}, manager(jia_id))
        self.assertEqual(inv["state"], "待确认")
        self.assertEqual(inv["awaiting"], ["receiving_venue"])
        status, inv = self.call("POST", f"/invitations/{inv_id}/confirm",
                                {"party": "receiving_venue"}, manager(yi_id))
        self.assertEqual(inv["state"], "已确认")
        agreement_id = inv["agreement_id"]
        status, agreement = self.call("GET", f"/agreements/{agreement_id}", headers=OP)
        self.assertEqual(agreement["dates"], ["2026-10-06"])  # 交集
        self.assertEqual(agreement["travel_plan"]["policy"], "实报实销")
        self.assertEqual(agreement["authorization_scope"], ["常设展讲解"])

        # 日历可见占用迁移；召回后恢复
        status, cal = self.call("GET", f"/volunteers/{vol_id}/calendar", headers=manager(jia_id))
        self.assertEqual(cal["shifts"][0]["state"], "已迁移")
        self.assertEqual(cal["occupancies"][0]["state"], "生效中")
        status, agreement = self.call("POST", f"/agreements/{agreement_id}/recall",
                                      {"dates": ["2026-10-06"], "reason": "原场馆临时增援"}, manager(jia_id))
        self.assertEqual(agreement["state"], "已归档")
        status, cal = self.call("GET", f"/volunteers/{vol_id}/calendar", headers=manager(jia_id))
        self.assertEqual(cal["shifts"][0]["state"], "在班")
        self.assertEqual(cal["occupancies"][0]["state"], "已召回")

    def test_visibility_over_http(self) -> None:
        _, jia = self.call("POST", "/venues", {"name": "甲馆2"}, OP)
        _, yi = self.call("POST", "/venues", {"name": "乙馆2"}, OP)
        _, vol = self.call("POST", "/volunteers", {
            "name": "李雷", "home_venue_id": jia["venue_id"], "skills": {"讲解": 1},
            "sensitive": {"phone": "13800000009"},
        }, OP)
        status, view = self.call("GET", f"/volunteers/{vol['volunteer_id']}", headers=manager(yi["venue_id"]))
        self.assertEqual(status, 200)
        self.assertEqual(view["sensitive"], {"phone": "***"})
        status, view = self.call("GET", f"/volunteers/{vol['volunteer_id']}", headers=OP)
        self.assertEqual(view["sensitive"], {"phone": "13800000009"})

    def test_error_shapes(self) -> None:
        status, err = self.call("GET", "/volunteers/vol-9999", headers=OP)
        self.assertEqual((status, err["error"]["code"]), (404, "not_found"))
        status, err = self.call("POST", "/venues", {"name": "越权"}, manager("venue-x"))
        self.assertEqual((status, err["error"]["code"]), (403, "permission_denied"))
        status, err = self.call("POST", "/nope", {})
        self.assertEqual((status, err["error"]["code"]), (404, "not_found"))
        _, jia = self.call("POST", "/venues", {"name": "甲馆3"}, OP)
        status, err = self.call("POST", f"/volunteers/vol-x/shifts",
                                {"date": "not-a-date", "slot": "am"}, OP)
        self.assertEqual((status, err["error"]["code"]), (400, "validation_error"))
        # 无身份调用受保护接口
        status, err = self.call("POST", "/loan-requests", {
            "receiving_venue_id": jia["venue_id"], "required_skills": {"讲解": 1},
            "dates": ["2026-10-06"], "slot": "am", "headcount": 1,
        })
        self.assertEqual((status, err["error"]["code"]), (403, "permission_denied"))


if __name__ == "__main__":
    unittest.main()
