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

PATIENT_RE = re.compile(r"^/api/patients/(\d+)$")
PATIENT_TIMELINE_RE = re.compile(r"^/api/patients/(\d+)/timeline$")
AIRCRAFT_RE = re.compile(r"^/api/aircraft/(\d+)$")
HOSPITAL_RE = re.compile(r"^/api/hospitals/(\d+)$")
MISSION_RE = re.compile(r"^/api/missions/(\d+)$")
MISSION_ACTION_RE = re.compile(r"^/api/missions/(\d+)/actions/([a-z_]+)$")
MISSION_TIMELINE_RE = re.compile(r"^/api/missions/(\d+)/timeline$")


def make_handler(service: Any, static_dir: Path, air_service: Any = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ambulance-airground/2.0"

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

        @staticmethod
        def _query_int(query: Dict[str, list], key: str, default: int) -> int:
            try:
                return int(query.get(key, [str(default)])[0])
            except ValueError as exc:
                raise ValidationError("%s必须是整数" % key) from exc

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                path = parsed.path
                if path == "/health":
                    self._send(200, {"status": "ok", "service": "ambulance-airground", "database": service.repository.health()})
                    return
                if path == "/":
                    page = (static_dir / "index.html").read_bytes()
                    self._send(200, page, "text/html; charset=utf-8")
                    return
                actor = self._actor()
                if path == "/api/records":
                    records = service.list_records(actor, state=query.get("state", [None])[0], limit=self._query_int(query, "limit", 100))
                    self._send(200, {"items": records})
                    return
                match = RECORD_RE.match(path)
                if match:
                    self._send(200, service.get_record(actor, int(match.group(1))))
                    return
                match = AUDIT_RE.match(path)
                if match:
                    self._send(200, {"items": service.timeline(actor, int(match.group(1)))})
                    return
                if path == "/api/stats":
                    self._send(200, service.stats(actor))
                    return

                # ---- 空地转运 ----
                if air_service is not None:
                    if path == "/api/patients":
                        self._send(200, {"items": air_service.list_patients(actor, state=query.get("state", [None])[0], limit=self._query_int(query, "limit", 100))})
                        return
                    match = PATIENT_TIMELINE_RE.match(path)
                    if match:
                        self._send(200, {"items": air_service.patient_timeline(actor, int(match.group(1)))})
                        return
                    match = PATIENT_RE.match(path)
                    if match:
                        self._send(200, air_service.get_patient(actor, int(match.group(1))))
                        return
                    if path == "/api/aircraft":
                        self._send(200, {"items": air_service.list_aircraft(actor, status=query.get("status", [None])[0], limit=self._query_int(query, "limit", 100))})
                        return
                    match = AIRCRAFT_RE.match(path)
                    if match:
                        self._send(200, air_service.get_aircraft(actor, int(match.group(1))))
                        return
                    if path == "/api/hospitals":
                        self._send(200, {"items": air_service.list_hospitals(actor, limit=self._query_int(query, "limit", 100))})
                        return
                    match = HOSPITAL_RE.match(path)
                    if match:
                        self._send(200, air_service.get_hospital(actor, int(match.group(1))))
                        return
                    if path == "/api/missions":
                        self._send(200, {"items": air_service.list_missions(actor, state=query.get("state", [None])[0], limit=self._query_int(query, "limit", 100))})
                        return
                    match = MISSION_TIMELINE_RE.match(path)
                    if match:
                        self._send(200, {"items": air_service.mission_timeline(actor, int(match.group(1)))})
                        return
                    match = MISSION_RE.match(path)
                    if match:
                        self._send(200, air_service.get_mission(actor, int(match.group(1))))
                        return
                    if path == "/api/airstats":
                        self._send(200, air_service.stats(actor))
                        return

                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                body = self._body()
                actor = self._actor()
                if path == "/api/records":
                    record = service.create(actor, body.get("reference", ""), body.get("data", {}))
                    self._send(201, record)
                    return
                match = ACTION_RE.match(path)
                if match:
                    version = body.get("expected_version")
                    if not isinstance(version, int):
                        raise ValidationError("expected_version必须是整数")
                    record = service.act(actor, int(match.group(1)), version, match.group(2), body.get("data", {}))
                    self._send(200, record)
                    return

                # ---- 空地转运 ----
                if air_service is not None:
                    if path == "/api/patients":
                        self._send(201, air_service.register_patient(actor, body.get("reference", ""), body.get("data", {})))
                        return
                    if path == "/api/aircraft":
                        self._send(201, air_service.register_aircraft(actor, body.get("data", {})))
                        return
                    if path == "/api/hospitals":
                        self._send(201, air_service.register_hospital(actor, body.get("data", {})))
                        return
                    if path == "/api/missions":
                        self._send(201, air_service.create_mission(actor, body.get("reference", ""), body.get("data", {})))
                        return
                    if path == "/api/air/evaluate":
                        data = body.get("data", {})
                        for key in ("patient_id", "aircraft_id", "hospital_id"):
                            if not isinstance(data.get(key), int):
                                raise ValidationError("%s必须是整数" % key)
                        report = air_service.evaluate(
                            actor, int(data["patient_id"]), int(data["aircraft_id"]), int(data["hospital_id"]), data
                        )
                        self._send(200, report)
                        return
                    match = MISSION_ACTION_RE.match(path)
                    if match:
                        version = body.get("expected_version")
                        if not isinstance(version, int):
                            raise ValidationError("expected_version必须是整数")
                        record = air_service.act(actor, int(match.group(1)), version, match.group(2), body.get("data", {}))
                        self._send(200, record)
                        return

                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

    return Handler


def create_server(host: str, port: int, service: Any, static_dir: Path, air_service: Any = None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, static_dir, air_service))
