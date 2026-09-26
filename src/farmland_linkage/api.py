"""无第三方依赖的耕地保护与宅基地退出联动 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import LinkageError, ValidationFailed
from .service import LinkageService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: LinkageService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)
            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(payload["user_id"], payload["display_name"], payload["role"], payload.get("household_id")))
            if method == "POST" and path == "/parcels/versions":
                return Response(201, self.service.register_parcel_version(actor, payload))
            if method == "POST" and path == "/protection-rules/import":
                return Response(201, self.service.import_protection_rules(actor, payload))
            if method == "POST" and path == "/households":
                return Response(201, self.service.create_household(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "households":
                return Response(200, self.service.household_view(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "households" and parts[2] == "authorizations":
                return Response(201, self.service.create_authorization(actor, dict(payload, household_id=parts[1])))
            if method == "POST" and len(parts) == 5 and parts[0] == "households" and parts[2] == "members" and parts[4] == "withdraw":
                return Response(200, self.service.withdraw_member_eligibility(actor, parts[1], parts[3], payload["reason"]))
            if method == "POST" and path == "/commitments":
                return Response(201, self.service.create_commitment(actor, payload))
            if method == "POST" and path == "/projects":
                return Response(201, self.service.create_project(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "start-construction":
                return Response(200, self.service.start_construction(actor, parts[1], int(payload["expected_revision"])))
            if method == "POST" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "resolve":
                return Response(200, self.service.resolve_project(actor, parts[1], payload["action"], payload["note"], int(payload["expected_revision"])))
            if method == "POST" and path == "/applications":
                return Response(201, self.service.accept_application(actor, payload))
            if method == "POST" and path == "/plans":
                return Response(201, self.service.create_plan(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "plans":
                return Response(200, self.service.plan_view(actor, parts[1]))
            if method == "GET" and len(parts) == 4 and parts[0] == "plans" and parts[2] == "exclusions":
                return Response(200, self.service.parcel_explanation(actor, parts[1], parts[3]))
            if method == "POST" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "confirm":
                return Response(200, self.service.confirm_plan(actor, parts[1], int(payload["expected_revision"])))
            if method == "POST" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "deliver":
                return Response(200, self.service.deliver_plan(actor, parts[1], int(payload["expected_revision"])))
            if method == "POST" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "resolve":
                return Response(200, self.service.resolve_plan(actor, parts[1], payload["action"], payload["note"], int(payload["expected_revision"])))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except LinkageError as exc:
            error: dict[str, Any] = {"code": exc.code, "message": str(exc)}
            if exc.details:
                error["details"] = exc.details
            return Response(exc.status, {"error": error})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FarmlandLinkage/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动耕地保护与宅基地退出联动服务")
    parser.add_argument("--database", type=Path, default=Path("farmland_linkage.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(LinkageService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
