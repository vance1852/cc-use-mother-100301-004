"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .chemical_service import ChemicalService
from .service import DomainService
from .storage import Database


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None,
          chemical: ChemicalService | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        result = _route_chemical(chemical, method, parsed, body, actor_id)
        if result is not None:
            return result
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        payload = {"error": exc.code, "message": str(exc)}
        violations = getattr(exc, "violations", None)
        if violations:
            payload["violations"] = violations
        return exc.status, payload
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _receipt_status(receipt) -> int:
    return 200 if getattr(receipt, "replayed", False) else 201


def _route_chemical(chemical, method, parsed, body, actor_id):
    if chemical is None:
        return None
    parts = [segment for segment in parsed.path.split("/") if segment]
    query = parse_qs(parsed.query)
    path = parsed.path

    if method != "POST":
        if method == "GET" and path == "/chemical/stock-balances":
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, {"items": chemical.stock_balances(site_id)}
        if method == "GET" and len(parts) == 4 and parts[:2] == ["chemical", "batches"] \
                and parts[3] == "trace":
            return 200, chemical.batch_trace(parts[2])
        if method == "GET" and len(parts) == 4 and parts[:2] == ["chemical", "locations"] \
                and parts[3] == "snapshot":
            at = query.get("at", [""])[0]
            if not at:
                raise ValidationError("at 不能为空")
            return 200, chemical.location_snapshot_at(parts[2], at)
        if method == "GET" and path == "/chemical/alerts":
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, {
                "certifications": chemical.expiring_certifications(
                    site_id, query["cert_before"][0]) if "cert_before" in query else [],
                "isolations": chemical.isolation_due(
                    site_id, query["iso_before"][0] if "iso_before" in query else None),
                "batches": chemical.expiring_batches(
                    site_id, query["batch_before"][0]) if "batch_before" in query else [],
            }
        return None

    routes = {
        "/chemical/units": chemical.register_containment_unit,
        "/chemical/locations": chemical.register_location,
        "/chemical/locations/update": chemical.update_location,
        "/chemical/safety-sheets": chemical.register_safety_sheet,
        "/chemical/rule-books": chemical.publish_rule_book,
        "/chemical/certifications": chemical.grant_certification,
        "/chemical/inbound": chemical.inbound_chemical,
        "/chemical/relocate": chemical.relocate,
        "/chemical/issue": chemical.issue,
        "/chemical/loss": chemical.record_loss,
        "/chemical/returns": chemical.return_to_storage,
        "/chemical/disposal": chemical.dispose_batch,
        "/chemical/container-change": chemical.change_container,
        "/chemical/isolation/impose": chemical.impose_isolation,
        "/chemical/isolation/lift": chemical.lift_isolation,
        "/chemical/stock-counts": chemical.count_stock,
    }
    if path == "/chemical/propose":
        return 200, chemical.propose_placement(actor_id=actor_id, **body)
    if path == "/chemical/expired":
        return 200, chemical.mark_expired(actor_id=actor_id, **body)
    handler = routes.get(path)
    if handler is None:
        return None
    # actor 一律以 X-Actor-Id 头为准，忽略请求体中的同名字段
    arguments = {key: value for key, value in body.items() if key != "actor_id"}
    receipt = handler(actor_id=actor_id, **arguments)
    return _receipt_status(receipt), receipt.__dict__


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    chemical: ChemicalService | None = None

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")},
                                chemical=self.chemical)
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动极地科考站协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
    Handler.chemical = ChemicalService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
