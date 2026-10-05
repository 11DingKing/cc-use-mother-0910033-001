"""标准库 HTTP 接口层：JSON 出入、角色头解析、统一错误格式。

调用者身份通过请求头传递：
- X-Actor-Role: operator / volunteer / guardian / venue_manager
- X-Actor-Id: 调用者编号（监护人、运营员使用）
- X-Venue-Id: 场馆负责人所属场馆
- X-Volunteer-Id: 志愿者本人编号
"""
from __future__ import annotations

import json
import re
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import serialize
from .errors import DomainError, NotFoundError, ValidationError
from .models import Actor, Guardianship, Role
from .service import SecondmentService


def _parse_actor(headers) -> Actor | None:
    role = headers.get("X-Actor-Role")
    if not role:
        return None
    try:
        role_enum = Role(role)
    except ValueError:
        raise ValidationError(f"未知角色：{role}")
    return Actor(
        role=role_enum,
        actor_id=headers.get("X-Actor-Id", "anonymous"),
        venue_id=headers.get("X-Venue-Id"),
        volunteer_id=headers.get("X-Volunteer-Id"),
    )


def _parse_date(value, field: str = "date") -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} 必须是 ISO 日期（YYYY-MM-DD）") from None


def _require(body: dict, *fields: str) -> None:
    missing = [f for f in fields if body.get(f) is None]
    if missing:
        raise ValidationError("缺少字段：" + "、".join(missing))


def _parse_guardianship(data: dict | None) -> Guardianship | None:
    if data is None:
        return None
    allowed = data.get("allowed_venue_ids")
    return Guardianship(
        guardian_id=data.get("guardian_id"),
        guardian_name=data.get("guardian_name"),
        requires_consent=bool(data.get("requires_consent", False)),
        allowed_venue_ids=frozenset(allowed) if allowed is not None else None,
        forbidden_slots=frozenset(data.get("forbidden_slots", ())),
        max_consecutive_days=data.get("max_consecutive_days"),
    )


# ---------- 路由处理 ----------

def _h_health(service, actor, body, query):
    return 200, {"status": "ok", "product": "跨场馆志愿者借调"}


def _h_create_venue(service, actor, body, query):
    _require(body, "name")
    venue = service.register_venue(actor, body["name"])
    return 201, serialize.venue_view(venue)


def _h_set_capacity(service, actor, body, query, venue_id):
    _require(body, "date", "max_loaned")
    rule = service.set_capacity(actor, venue_id, _parse_date(body["date"]), int(body["max_loaned"]))
    used = service.store.capacity_usage(rule.venue_id, rule.day)
    return 200, serialize.capacity_view(rule, used)


def _h_register_volunteer(service, actor, body, query):
    _require(body, "name", "home_venue_id", "skills")
    vol = service.register_volunteer(
        actor,
        name=body["name"],
        home_venue_id=body["home_venue_id"],
        skills=body["skills"],
        sensitive=body.get("sensitive"),
        guardianship=_parse_guardianship(body.get("guardianship")),
    )
    return 201, service.volunteer_view(actor, vol.volunteer_id)


def _h_get_volunteer(service, actor, body, query, volunteer_id):
    return 200, service.volunteer_view(actor, volunteer_id)


def _h_calendar(service, actor, body, query, volunteer_id):
    start = _parse_date(query["start"][0], "start") if query.get("start") else None
    end = _parse_date(query["end"][0], "end") if query.get("end") else None
    shifts, occupancies = service.calendar(actor, volunteer_id, start, end)
    return 200, {
        "volunteer_id": volunteer_id,
        "shifts": [serialize.shift_view(s) for s in shifts],
        "occupancies": [serialize.occupancy_view(o) for o in occupancies],
    }


def _h_add_shift(service, actor, body, query, volunteer_id):
    _require(body, "date", "slot")
    shift = service.register_shift(actor, volunteer_id, _parse_date(body["date"]), body["slot"])
    return 201, serialize.shift_view(shift)


def _h_create_request(service, actor, body, query):
    _require(body, "receiving_venue_id", "required_skills", "dates", "slot", "headcount")
    req = service.create_request(
        actor,
        receiving_venue_id=body["receiving_venue_id"],
        required_skills=body["required_skills"],
        dates=[_parse_date(d, "dates") for d in body["dates"]],
        slot=body["slot"],
        headcount=int(body["headcount"]),
        travel_policy=body.get("travel_policy", ""),
        authorization_scope=tuple(body.get("authorization_scope", ())),
    )
    return 201, serialize.request_view(req)


def _h_get_request(service, actor, body, query, request_id):
    req = service.get_request(actor, request_id)
    invitations = service.store.invitations_for_request(request_id)
    agreements = service.store.agreements_for_request(request_id)
    return 200, serialize.request_view(req, invitations, agreements)


def _h_propose(service, actor, body, query, request_id):
    return 200, serialize.proposal_view(service.propose(actor, request_id))


def _h_candidates(service, actor, body, query, request_id):
    return 200, serialize.proposal_view(service.get_proposal(actor, request_id))


def _h_invite(service, actor, body, query, request_id):
    _require(body, "volunteer_id")
    dates = None
    if body.get("dates") is not None:
        dates = [_parse_date(d, "dates") for d in body["dates"]]
    inv, created = service.invite(actor, request_id, body["volunteer_id"], dates)
    return (201 if created else 200), serialize.invitation_view(inv, service.requires_guardian(inv))


def _h_get_invitation(service, actor, body, query, invitation_id):
    inv = service.get_invitation(actor, invitation_id)
    return 200, serialize.invitation_view(inv, service.requires_guardian(inv))


def _h_confirm(service, actor, body, query, invitation_id):
    _require(body, "party")
    dates = None
    if body.get("accepted_dates") is not None:
        dates = [_parse_date(d, "accepted_dates") for d in body["accepted_dates"]]
    inv = service.confirm(actor, invitation_id, body["party"], dates, body.get("expected_version"))
    return 200, serialize.invitation_view(inv, service.requires_guardian(inv))


def _h_decline(service, actor, body, query, invitation_id):
    _require(body, "party")
    inv = service.decline(actor, invitation_id, body["party"], body.get("reason", ""))
    return 200, serialize.invitation_view(inv, service.requires_guardian(inv))


def _h_get_agreement(service, actor, body, query, agreement_id):
    return 200, serialize.agreement_view(service.get_agreement(actor, agreement_id))


def _h_recall(service, actor, body, query, agreement_id):
    _require(body, "dates", "reason")
    dates = [_parse_date(d, "dates") for d in body["dates"]]
    return 200, serialize.agreement_view(service.recall(actor, agreement_id, dates, body["reason"]))


def _h_advance(service, actor, body, query, agreement_id):
    return 200, serialize.agreement_view(service.advance(actor, agreement_id))


ROUTES = [
    ("GET", re.compile(r"^/health$"), _h_health),
    ("POST", re.compile(r"^/venues$"), _h_create_venue),
    ("POST", re.compile(r"^/venues/(?P<venue_id>[^/]+)/capacity$"), _h_set_capacity),
    ("POST", re.compile(r"^/volunteers$"), _h_register_volunteer),
    ("GET", re.compile(r"^/volunteers/(?P<volunteer_id>[^/]+)$"), _h_get_volunteer),
    ("GET", re.compile(r"^/volunteers/(?P<volunteer_id>[^/]+)/calendar$"), _h_calendar),
    ("POST", re.compile(r"^/volunteers/(?P<volunteer_id>[^/]+)/shifts$"), _h_add_shift),
    ("POST", re.compile(r"^/loan-requests$"), _h_create_request),
    ("GET", re.compile(r"^/loan-requests/(?P<request_id>[^/]+)$"), _h_get_request),
    ("POST", re.compile(r"^/loan-requests/(?P<request_id>[^/]+)/proposals$"), _h_propose),
    ("GET", re.compile(r"^/loan-requests/(?P<request_id>[^/]+)/candidates$"), _h_candidates),
    ("POST", re.compile(r"^/loan-requests/(?P<request_id>[^/]+)/invitations$"), _h_invite),
    ("GET", re.compile(r"^/invitations/(?P<invitation_id>[^/]+)$"), _h_get_invitation),
    ("POST", re.compile(r"^/invitations/(?P<invitation_id>[^/]+)/confirm$"), _h_confirm),
    ("POST", re.compile(r"^/invitations/(?P<invitation_id>[^/]+)/decline$"), _h_decline),
    ("GET", re.compile(r"^/agreements/(?P<agreement_id>[^/]+)$"), _h_get_agreement),
    ("POST", re.compile(r"^/agreements/(?P<agreement_id>[^/]+)/recall$"), _h_recall),
    ("POST", re.compile(r"^/agreements/(?P<agreement_id>[^/]+)/advance$"), _h_advance),
]


def dispatch(service: SecondmentService, method: str, raw_path: str, body: dict, actor: Actor | None):
    parsed = urlparse(raw_path)
    query = parse_qs(parsed.query)
    for route_method, pattern, handler in ROUTES:
        if route_method != method:
            continue
        match = pattern.match(parsed.path)
        if match:
            return handler(service, actor, body, query, **match.groupdict())
    raise NotFoundError(f"接口不存在：{method} {parsed.path}")


def make_handler(service: SecondmentService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SecondmentAPI/0.1"

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method: str) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                if not isinstance(body, dict):
                    raise ValidationError("请求体必须是 JSON 对象")
                actor = _parse_actor(self.headers)
                status, payload = dispatch(service, method, self.path, body, actor)
                self._send(status, payload)
            except DomainError as err:
                self._send(err.status, {"error": err.to_dict()})
            except json.JSONDecodeError:
                self._send(400, {"error": {"code": "bad_json", "message": "请求体不是合法 JSON", "details": {}}})
            except Exception as err:  # noqa: BLE001 - 兜底，避免连接直接断开
                self.log_error("未处理异常：%r", err)
                self._send(500, {"error": {"code": "internal_error", "message": "服务内部错误", "details": {}}})

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def log_message(self, fmt, *args):  # 保持测试输出安静
            pass

    return Handler


def build_server(service: SecondmentService, host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service))
