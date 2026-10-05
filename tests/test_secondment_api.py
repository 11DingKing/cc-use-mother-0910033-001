"""HTTP 接口测试：身份头、错误格式、全流程与敏感资料视图层级。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from secondment.api import create_server  # noqa: E402
from secondment.service import SecondmentService  # noqa: E402

OP_A = {"X-Actor-Id": "op-a", "X-Actor-Role": "operator", "X-Actor-Venue-Id": "VEN-A"}
MGR_A = {"X-Actor-Id": "mgr-a", "X-Actor-Role": "venue_manager", "X-Actor-Venue-Id": "VEN-A"}
MGR_B = {"X-Actor-Id": "mgr-b", "X-Actor-Role": "venue_manager", "X-Actor-Venue-Id": "VEN-B"}
OP_C = {"X-Actor-Id": "op-c", "X-Actor-Role": "operator", "X-Actor-Venue-Id": "VEN-C"}
ADMIN = {"X-Actor-Id": "op-admin", "X-Actor-Role": "operator"}
DAYS = ["2026-10-01", "2026-10-02", "2026-10-03"]


class ApiTest(unittest.TestCase):
    server = None
    port = 0

    @classmethod
    def setUpClass(cls) -> None:
        # 固定时钟 2026-10-02，落在借调期（10-01~10-03）内，保证状态推导确定
        service = SecondmentService(today=lambda: date(2026, 10, 2))
        cls.server = create_server(service, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        # 共享基线：三个场馆、一名带敏感资料的志愿者、一天原排班、接收方容量
        cls.call_static("POST", "/venues", {"id": "VEN-A", "name": "文博中心"}, ADMIN)
        cls.call_static("POST", "/venues", {"id": "VEN-B", "name": "科技馆"}, ADMIN)
        cls.call_static("POST", "/venues", {"id": "VEN-C", "name": "规划馆"}, ADMIN)
        cls.call_static(
            "POST", "/volunteers",
            {
                "id": "VOL-1", "name": "王五", "home_venue_id": "VEN-B",
                "skills": {"急救": 3}, "sensitive": {"phone": "138"},
            },
            MGR_B,
        )
        cls.call_static("POST", "/schedules", {"volunteer_id": "VOL-1", "date": DAYS[0]}, MGR_B)
        for day in DAYS:
            cls.call_static(
                "POST", "/capacities",
                {"venue_id": "VEN-A", "date": day, "max_inbound": 2}, OP_A,
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    @classmethod
    def call_static(cls, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        url = f"http://127.0.0.1:{cls.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def call(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        return self.call_static(method, path, body, headers)

    def _create_request(self) -> str:
        status, payload = self.call(
            "POST", "/loan-requests",
            {
                "receiving_venue_id": "VEN-A",
                "requirements": [{"skill": "急救", "min_level": 2, "count": 1}],
                "dates": DAYS,
            },
            OP_A,
        )
        self.assertEqual(status, 201)
        return payload["request"]["id"]

    def test_health_is_public(self) -> None:
        status, payload = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    def test_missing_actor_headers_rejected(self) -> None:
        status, payload = self.call("POST", "/venues", {"name": "x"})
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "UNAUTHENTICATED")

    def test_unknown_role_rejected(self) -> None:
        status, payload = self.call(
            "POST", "/venues", {"name": "x"},
            {"X-Actor-Id": "a", "X-Actor-Role": "superman"},
        )
        self.assertEqual(status, 400)
        self.assertIn("operator", str(payload["error"]["details"]["allowed"]))

    def test_unknown_route_404(self) -> None:
        status, payload = self.call("GET", "/nope", headers=ADMIN)
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "NOT_FOUND")

    def test_error_format_on_not_found(self) -> None:
        status, payload = self.call("GET", "/loan-requests/REQ-9999", headers=OP_A)
        self.assertEqual(status, 404)
        self.assertEqual(set(payload["error"]), {"code", "message", "details"})

    def test_full_flow_over_http(self) -> None:
        req_id = self._create_request()
        _, payload = self.call("GET", f"/loan-requests/{req_id}", headers=OP_A)
        self.assertEqual(payload["request"]["status"], "草拟")
        # 候选排序（含解释）
        status, payload = self.call("GET", f"/loan-requests/{req_id}/proposals", headers=OP_A)
        self.assertEqual(status, 200)
        self.assertEqual(payload["candidates"][0]["volunteer_id"], "VOL-1")
        self.assertEqual(payload["candidates"][0]["migratable_dates"], [DAYS[0]])
        self.assertTrue(payload["candidates"][0]["explanation"])
        # 邀请（重复邀请幂等）
        status, payload = self.call(
            "POST", f"/loan-requests/{req_id}/invitations", {"volunteer_id": "VOL-1"}, OP_A
        )
        self.assertEqual(status, 201)
        inv_id = payload["invitation"]["id"]
        status, payload = self.call(
            "POST", f"/loan-requests/{req_id}/invitations", {"volunteer_id": "VOL-1"}, OP_A
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["duplicate"])
        self.assertEqual(payload["invitation"]["id"], inv_id)
        # 原场馆部分日期接受
        status, payload = self.call(
            "POST", f"/invitations/{inv_id}/respond",
            {"accept": True, "accepted_dates": DAYS[:2]}, MGR_B,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["invitation"]["declined_dates"], [DAYS[2]])
        # 接收方确认 → 原子迁移
        status, payload = self.call("POST", f"/invitations/{inv_id}/confirm", {}, MGR_A)
        self.assertEqual(status, 201)
        agr = payload["agreement"]
        self.assertEqual(agr["dates"], DAYS[:2])
        self.assertEqual(len(agr["released_entry_ids"]), 1)  # 第一天的原排班被迁移
        # 重复确认幂等
        status, payload = self.call("POST", f"/invitations/{inv_id}/confirm", {}, MGR_A)
        self.assertEqual(status, 200)
        self.assertFalse(payload["created"])
        # 请求状态联动
        _, payload = self.call("GET", f"/loan-requests/{req_id}", headers=OP_A)
        self.assertIn(payload["request"]["status"], ("已确认", "执行中"))
        # 临时召回一天
        status, payload = self.call(
            "POST", f"/agreements/{agr['id']}/recall",
            {"dates": [DAYS[0]], "reason": "本馆临时缺人"}, MGR_B,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["agreement"]["dates"], [DAYS[1]])
        self.assertEqual(payload["agreement"]["status"], "partially_recalled")
        # 召回后原排班恢复
        _, payload = self.call("GET", "/volunteers/VOL-1/schedule", headers=MGR_B)
        day1 = [e for e in payload["entries"] if e["date"] == DAYS[0] and e["kind"] == "home"]
        self.assertEqual(day1[0]["status"], "active")

    def test_sensitive_profile_hidden_from_outsiders(self) -> None:
        # 无关场馆运营员不可见敏感资料，也不能查看排班去向
        status, payload = self.call("GET", "/volunteers/VOL-1", headers=OP_C)
        self.assertEqual(status, 200)
        self.assertFalse(payload["volunteer"]["sensitive_visible"])
        self.assertNotIn("sensitive", payload["volunteer"])
        status, _ = self.call("GET", "/volunteers/VOL-1/schedule", headers=OP_C)
        self.assertEqual(status, 403)
        # 邀请发出后接收方进入借调链 → 可见
        req_id = self._create_request()
        self.call("POST", f"/loan-requests/{req_id}/invitations", {"volunteer_id": "VOL-1"}, OP_A)
        _, payload = self.call("GET", "/volunteers/VOL-1", headers=OP_A)
        self.assertTrue(payload["volunteer"]["sensitive_visible"])
        self.assertEqual(payload["volunteer"]["sensitive"]["phone"], "138")


if __name__ == "__main__":
    unittest.main()
