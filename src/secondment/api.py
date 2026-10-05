"""标准库 HTTP JSON 接口（零第三方依赖）。

调用者身份通过请求头传递：
- ``X-Actor-Id``：参与者编号（如运营员工号、志愿者编号、监护人编号）
- ``X-Actor-Role``：operator / volunteer / guardian / venue_manager
- ``X-Actor-Venue-Id``：所属场馆（场馆工作人员必填）

错误统一返回 ``{"error": {"code", "message", "details"}}``，
其中 code 为机器可读冲突代码（如 ALREADY_LOANED、CAPACITY_FULL）。
"""
from __future__ import annotations

import json
import re
import traceback
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from . import views
from .models import ActorContext, Role, SensitiveProfile, SkillRequirement
from .service import BAD_REQUEST, SecondmentService, ServiceError, UNAUTHENTICATED

Handler = Callable[[ActorContext, re.Match, dict], tuple[int, dict]]


def _parse_date(value, field: str = "date") -> date:
    if not isinstance(value, str):
        raise ServiceError(400, BAD_REQUEST, f"字段 {field} 须为 YYYY-MM-DD 字符串")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ServiceError(400, BAD_REQUEST, f"字段 {field} 不是合法日期：{value}")


def _parse_date_list(value, field: str) -> list[date] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ServiceError(400, BAD_REQUEST, f"字段 {field} 须为日期数组")
    return [_parse_date(item, field) for item in value]


def _require(body: dict, *fields: str) -> None:
    missing = [f for f in fields if body.get(f) is None]
    if missing:
        raise ServiceError(400, BAD_REQUEST, "缺少必填字段：" + "、".join(missing))


class _Router:
    def __init__(self, service: SecondmentService) -> None:
        self.service = service
        self.routes: list[tuple[str, re.Pattern, Handler, bool]] = []
        add = self.add
        add("GET", r"^/health$", self.health, public=True)
        add("POST", r"^/venues$", self.create_venue)
        add("POST", r"^/volunteers$", self.create_volunteer)
        add("GET", r"^/volunteers/(?P<vid>[^/]+)$", self.get_volunteer)
        add("GET", r"^/volunteers/(?P<vid>[^/]+)/schedule$", self.get_volunteer_schedule)
        add("POST", r"^/guardian-restrictions$", self.create_restriction)
        add("POST", r"^/schedules$", self.create_schedule)
        add("POST", r"^/capacities$", self.create_capacity)
        add("POST", r"^/loan-requests$", self.create_request)
        add("GET", r"^/loan-requests/(?P<rid>[^/]+)$", self.get_request)
        add("GET", r"^/loan-requests/(?P<rid>[^/]+)/proposals$", self.get_proposals)
        add("POST", r"^/loan-requests/(?P<rid>[^/]+)/invitations$", self.create_invitation)
        add("GET", r"^/invitations/(?P<iid>[^/]+)$", self.get_invitation)
        add("POST", r"^/invitations/(?P<iid>[^/]+)/respond$", self.respond_invitation)
        add("POST", r"^/invitations/(?P<iid>[^/]+)/confirm$", self.confirm_invitation)
        add("GET", r"^/agreements/(?P<aid>[^/]+)$", self.get_agreement)
        add("POST", r"^/agreements/(?P<aid>[^/]+)/recall$", self.recall_agreement)

    def add(self, method: str, pattern: str, handler: Handler, public: bool = False) -> None:
        self.routes.append((method, re.compile(pattern), handler, public))

    # ---------------- 各端点 ----------------
    def health(self, actor, match, body):
        return 200, {"status": "ok"}

    def create_venue(self, actor, match, body):
        _require(body, "name")
        venue = self.service.register_venue(actor, name=body["name"], venue_id=body.get("id"))
        return 201, {"venue": views.venue_view(venue)}

    def create_volunteer(self, actor, match, body):
        _require(body, "name", "home_venue_id")
        sensitive_raw = body.get("sensitive") or {}
        vol = self.service.register_volunteer(
            actor,
            name=body["name"],
            home_venue_id=body["home_venue_id"],
            skills=body.get("skills"),
            is_minor=bool(body.get("is_minor", False)),
            guardian_id=body.get("guardian_id"),
            sensitive=SensitiveProfile(
                phone=sensitive_raw.get("phone", ""),
                id_number=sensitive_raw.get("id_number", ""),
                emergency_contact=sensitive_raw.get("emergency_contact", ""),
            ),
            volunteer_id=body.get("id"),
        )
        return 201, {"volunteer": views.volunteer_view(self.service, actor, vol)}

    def get_volunteer(self, actor, match, body):
        vol = self.service._volunteer(match.group("vid"))
        return 200, {"volunteer": views.volunteer_view(self.service, actor, vol)}

    def get_volunteer_schedule(self, actor, match, body):
        vol = self.service._volunteer(match.group("vid"))
        if not self.service.can_view_sensitive(actor, vol):
            raise ServiceError(403, "FORBIDDEN", "排班去向仅借调链相关人员可见")
        entries = sorted(
            (e for e in self.service.store.entries.values() if e.volunteer_id == vol.id),
            key=lambda e: (e.day, e.id),
        )
        return 200, {"entries": [views.schedule_entry_view(e) for e in entries]}

    def create_restriction(self, actor, match, body):
        _require(body, "volunteer_id")
        restriction = self.service.register_guardian_restriction(
            actor,
            volunteer_id=body["volunteer_id"],
            max_daily_hours=int(body.get("max_daily_hours", 8)),
            allow_cross_venue=bool(body.get("allow_cross_venue", True)),
            allowed_start=_parse_date(body["allowed_start"], "allowed_start")
            if body.get("allowed_start")
            else None,
            allowed_end=_parse_date(body["allowed_end"], "allowed_end")
            if body.get("allowed_end")
            else None,
            note=body.get("note", ""),
        )
        return 201, {"restriction": views.restriction_view(restriction)}

    def create_schedule(self, actor, match, body):
        _require(body, "volunteer_id", "date")
        entry = self.service.add_schedule_entry(
            actor,
            volunteer_id=body["volunteer_id"],
            day=_parse_date(body["date"]),
            hours=int(body.get("hours", 8)),
            venue_id=body.get("venue_id"),
        )
        return 201, {"entry": views.schedule_entry_view(entry)}

    def create_capacity(self, actor, match, body):
        _require(body, "venue_id", "date", "max_inbound")
        capacity = self.service.register_capacity(
            actor,
            venue_id=body["venue_id"],
            day=_parse_date(body["date"]),
            max_inbound=int(body["max_inbound"]),
        )
        return 201, {"capacity": views.capacity_view(self.service, capacity)}

    def create_request(self, actor, match, body):
        _require(body, "receiving_venue_id", "requirements", "dates")
        requirements = [
            SkillRequirement(
                skill=str(item.get("skill", "")),
                min_level=int(item.get("min_level", 0)),
                count=int(item.get("count", 1)),
            )
            for item in body["requirements"]
        ]
        req = self.service.create_loan_request(
            actor,
            receiving_venue_id=body["receiving_venue_id"],
            requirements=requirements,
            dates=_parse_date_list(body["dates"], "dates") or [],
            daily_hours=int(body.get("daily_hours", 8)),
            note=body.get("note", ""),
        )
        return 201, {"request": views.request_view(self.service, req)}

    def get_request(self, actor, match, body):
        req = self.service._request(match.group("rid"))
        return 200, {"request": views.request_view(self.service, req)}

    def get_proposals(self, actor, match, body):
        return 200, self.service.propose_candidates(actor, match.group("rid"))

    def create_invitation(self, actor, match, body):
        _require(body, "volunteer_id")
        inv, duplicate = self.service.create_invitation(
            actor,
            match.group("rid"),
            body["volunteer_id"],
            dates=_parse_date_list(body.get("dates"), "dates"),
        )
        return (200 if duplicate else 201), {
            "duplicate": duplicate,
            "invitation": views.invitation_view(inv),
        }

    def get_invitation(self, actor, match, body):
        inv = self.service._invitation(match.group("iid"))
        return 200, {"invitation": views.invitation_view(inv)}

    def respond_invitation(self, actor, match, body):
        _require(body, "accept")
        inv = self.service.respond_invitation(
            actor,
            match.group("iid"),
            accept=bool(body["accept"]),
            accepted_dates=_parse_date_list(body.get("accepted_dates"), "accepted_dates"),
        )
        return 200, {"invitation": views.invitation_view(inv)}

    def confirm_invitation(self, actor, match, body):
        agr, created = self.service.confirm_invitation(actor, match.group("iid"))
        return (201 if created else 200), {
            "created": created,
            "agreement": views.agreement_view(self.service, agr),
        }

    def get_agreement(self, actor, match, body):
        agr = self.service._agreement(match.group("aid"))
        return 200, {"agreement": views.agreement_view(self.service, agr)}

    def recall_agreement(self, actor, match, body):
        agr = self.service.recall_agreement(
            actor,
            match.group("aid"),
            dates=_parse_date_list(body.get("dates"), "dates"),
            reason=body.get("reason", ""),
        )
        return 200, {"agreement": views.agreement_view(self.service, agr)}


def _actor_from_headers(headers) -> ActorContext:
    actor_id = headers.get("X-Actor-Id")
    role_raw = headers.get("X-Actor-Role")
    if not actor_id or not role_raw:
        raise ServiceError(
            401, UNAUTHENTICATED, "缺少 X-Actor-Id / X-Actor-Role 请求头"
        )
    try:
        role = Role(role_raw)
    except ValueError:
        raise ServiceError(
            400,
            BAD_REQUEST,
            f"未知角色：{role_raw}",
            {"allowed": [r.value for r in Role]},
        )
    return ActorContext(
        actor_id=actor_id, role=role, venue_id=headers.get("X-Actor-Venue-Id") or None
    )


def make_handler(service: SecondmentService):
    router = _Router(service)

    class SecondmentHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):  # 保持测试输出安静
            pass

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            try:
                path = self.path.split("?", 1)[0]
                for route_method, pattern, handler, public in router.routes:
                    if route_method != method:
                        continue
                    match = pattern.match(path)
                    if not match:
                        continue
                    body = self._read_body()
                    actor = None if public else _actor_from_headers(self.headers)
                    status, payload = handler(actor, match, body)
                    self._respond(status, payload)
                    return
                self._respond(
                    404,
                    {"error": {"code": "NOT_FOUND", "message": "接口不存在", "details": {}}},
                )
            except ServiceError as err:
                self._respond(
                    err.status,
                    {
                        "error": {
                            "code": err.code,
                            "message": err.message,
                            "details": err.details,
                        }
                    },
                )
            except Exception:  # pragma: no cover - 兜底
                traceback.print_exc()
                self._respond(
                    500,
                    {"error": {"code": "INTERNAL", "message": "服务内部错误", "details": {}}},
                )

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ServiceError(400, BAD_REQUEST, "请求体不是合法 JSON")
            if not isinstance(value, dict):
                raise ServiceError(400, BAD_REQUEST, "请求体须为 JSON 对象")
            return value

        def _respond(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return SecondmentHandler


def create_server(
    service: SecondmentService, host: str = "127.0.0.1", port: int = 8000
) -> ThreadingHTTPServer:
    """创建多线程 HTTP 服务；并发确认由服务层临界区保证一致性。"""
    server = ThreadingHTTPServer((host, port), make_handler(service))
    server.daemon_threads = True
    return server
