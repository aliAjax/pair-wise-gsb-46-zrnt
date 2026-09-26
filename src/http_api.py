"""HTTP 路由与统一错误输出。"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from .domain import Actor, DomainError, PermissionDenied, ValidationError


RECORD_RE = re.compile(r"^/api/records/(\d+)$")
ACTION_RE = re.compile(r"^/api/records/(\d+)/actions/([a-z_]+)$")
AUDIT_RE = re.compile(r"^/api/records/(\d+)/audit$")

AIR_ITEM_RE = re.compile(r"^/api/air/(patients|helicopters|hospitals)/(\d+)$")
AIR_TIMELINE_RE = re.compile(r"^/api/air/patients/(\d+)/timeline$")
AIR_DISPATCH_RE = re.compile(r"^/api/air/patients/(\d+)/dispatch$")
AIR_ACTION_RE = re.compile(r"^/api/air/patients/(\d+)/actions/(takeoff|handover|cancel)$")
AIR_DIVERT_RE = re.compile(r"^/api/air/patients/(\d+)/divert$")
AIR_COLLECTIONS = {"patients": "patient", "helicopters": "helicopter", "hospitals": "hospital"}


def make_handler(service: Any, static_dir: Path, air_service: Any = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ambulance-dispatch/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _actor(self) -> Actor:
            user_id = self.headers.get("X-User-Id", "").strip()
            role = self.headers.get("X-Role", "").strip()
            if not user_id or not role:
                raise PermissionDenied("缺少X-User-Id或X-Role")
            return Actor(user_id=user_id, role=role, organization=self.headers.get("X-Org", ""))

        def _body(self) -> Dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValidationError("Content-Length无效") from exc
            if length > 1024 * 1024:
                raise ValidationError("请求体过大")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体必须是JSON") from exc
            if not isinstance(data, dict):
                raise ValidationError("JSON顶层必须是对象")
            return data

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
            if content_type.startswith("application/json"):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            else:
                body = payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, DomainError):
                self._send(exc.status, {"error": exc.code, "message": str(exc)})
            else:
                self._send(500, {"error": "internal_error", "message": "服务内部错误"})

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    self._send(200, {"status": "ok", "service": "ambulance-dispatch", "database": service.repository.health()})
                    return
                if parsed.path == "/":
                    page = (static_dir / "index.html").read_bytes()
                    self._send(200, page, "text/html; charset=utf-8")
                    return
                if parsed.path == "/api/records":
                    query = parse_qs(parsed.query)
                    records = service.list_records(self._actor(), state=query.get("state", [None])[0], limit=int(query.get("limit", ["100"])[0]))
                    self._send(200, {"items": records})
                    return
                match = RECORD_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_record(self._actor(), int(match.group(1))))
                    return
                match = AUDIT_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.timeline(self._actor(), int(match.group(1)))})
                    return
                if parsed.path == "/api/stats":
                    self._send(200, service.stats(self._actor()))
                    return
                if air_service is not None:
                    if self._air_get(parsed):
                        return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def _air_get(self, parsed) -> bool:
            path = parsed.path
            query = parse_qs(parsed.query)
            if path == "/api/air/stats":
                self._send(200, air_service.overview(self._actor()))
                return True
            match = AIR_TIMELINE_RE.match(path)
            if match:
                self._send(200, {"items": air_service.patient_timeline(self._actor(), int(match.group(1)))})
                return True
            match = AIR_ITEM_RE.match(path)
            if match:
                kind, entity_id = match.group(1), int(match.group(2))
                if kind == "patients":
                    result = air_service.get_patient(self._actor(), entity_id)
                elif kind == "helicopters":
                    result = air_service.get_helicopter(self._actor(), entity_id)
                else:
                    result = air_service.get_hospital(self._actor(), entity_id)
                self._send(200, result)
                return True
            if path in {"/api/air/patients", "/api/air/helicopters", "/api/air/hospitals"}:
                kind = AIR_COLLECTIONS[path.rsplit("/", 1)[-1]]
                state = query.get("state", [None])[0]
                limit = int(query.get("limit", ["100"])[0])
                if kind == "patient":
                    items = air_service.list_patients(self._actor(), state=state, limit=limit)
                elif kind == "helicopter":
                    items = air_service.list_helicopters(self._actor(), state=state, limit=limit)
                else:
                    items = air_service.list_hospitals(self._actor(), limit=limit)
                self._send(200, {"items": items})
                return True
            return False

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                body = self._body()
                if parsed.path == "/api/records":
                    record = service.create(self._actor(), body.get("reference", ""), body.get("data", {}))
                    self._send(201, record)
                    return
                match = ACTION_RE.match(parsed.path)
                if match:
                    version = body.get("expected_version")
                    if not isinstance(version, int):
                        raise ValidationError("expected_version必须是整数")
                    record = service.act(self._actor(), int(match.group(1)), version, match.group(2), body.get("data", {}))
                    self._send(200, record)
                    return
                if air_service is not None and self._air_post(parsed.path, body):
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def _air_post(self, path: str, body: Dict[str, Any]) -> bool:
            actor = self._actor()
            match = AIR_DISPATCH_RE.match(path)
            if match:
                data = body.get("data", {})
                helicopter_id = data.get("helicopter_id")
                hospital_id = data.get("hospital_id")
                if not isinstance(helicopter_id, int) or not isinstance(hospital_id, int):
                    raise ValidationError("helicopter_id与hospital_id必须是整数")
                result = air_service.evaluate_dispatch(actor, int(match.group(1)), helicopter_id, hospital_id, data)
                self._send(200, result)
                return True
            match = AIR_ACTION_RE.match(path)
            if match:
                version = body.get("expected_version")
                if not isinstance(version, int):
                    raise ValidationError("expected_version必须是整数")
                patient_id, action = int(match.group(1)), match.group(2)
                if action == "takeoff":
                    result = air_service.takeoff(actor, patient_id, version, body.get("data", {}))
                elif action == "handover":
                    result = air_service.handover(actor, patient_id, version, body.get("data", {}))
                else:
                    result = air_service.cancel(actor, patient_id, version, body.get("data", {}))
                self._send(200, result)
                return True
            match = AIR_DIVERT_RE.match(path)
            if match:
                version = body.get("expected_version")
                if not isinstance(version, int):
                    raise ValidationError("expected_version必须是整数")
                data = body.get("data", {})
                target = data.get("target_hospital_id")
                if not isinstance(target, int):
                    raise ValidationError("target_hospital_id必须是整数")
                result = air_service.divert(actor, int(match.group(1)), version, target, data.get("note", ""))
                self._send(200, result)
                return True
            if path in {"/api/air/patients", "/api/air/helicopters", "/api/air/hospitals"}:
                kind = AIR_COLLECTIONS[path.rsplit("/", 1)[-1]]
                if kind == "patient":
                    result = air_service.register_patient(actor, body.get("code", ""), body.get("data", {}))
                elif kind == "helicopter":
                    result = air_service.register_helicopter(actor, body.get("code", ""), body.get("data", {}))
                else:
                    result = air_service.register_hospital(actor, body.get("code", ""), body.get("data", {}))
                self._send(201, result)
                return True
            return False

    return Handler


def create_server(host: str, port: int, service: Any, static_dir: Path, air_service: Any = None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, static_dir, air_service))
