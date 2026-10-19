#!/usr/bin/env python3
"""
phone_proof.py: run the launcher ON THE PHONE (Termux), with no laptop and no FastAPI.

This exists to prove the phone-only idea before anyone builds an Android app. It runs the same
tested code as launcher.py: the Orchestrator, the Bravia client, the atv_remote driver, the
route handlers and the kiosk page. Only the web layer is swapped. FastAPI needs pydantic-core,
which has to be compiled from Rust and rarely builds on a phone, so this file serves the
launcher's handlers from a small asyncio HTTP server built on the standard library.

  python phone_proof.py serve                       # serve the kiosk page (open http://127.0.0.1:8000 in Chrome)
  python phone_proof.py diag pair                   # any diagnose.py command, run from the phone
  python phone_proof.py diag mismatch --channel 725

See PHONE_PROOF.md for the Termux setup and the pass/fail checklist.
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import sys
import types
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("phone_proof")


def _install_fastapi_stub() -> None:
    """Just enough of `fastapi` for launcher.py to import: route decorators become no-ops, and
    the response classes only hold content, status and headers for _respond() to write out."""
    fa, resp = types.ModuleType("fastapi"), types.ModuleType("fastapi.responses")

    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str | None = None):
            super().__init__(detail)
            self.status_code, self.detail = status_code, detail

    class Response:
        media_type = "application/octet-stream"

        def __init__(self, content=b"", status_code=200, headers=None, media_type=None):
            self.content, self.status_code = content, status_code
            self.headers = dict(headers or {})
            self.media_type = media_type or self.media_type

    class HTMLResponse(Response):
        media_type = "text/html; charset=utf-8"

    class JSONResponse(Response):
        media_type = "application/json"

    class FileResponse(Response):
        def __init__(self, path, headers=None, **_):
            super().__init__(Path(path).read_bytes(), headers=headers,
                             media_type=mimetypes.guess_type(str(path))[0] or "application/octet-stream")

    class FastAPI:
        def __init__(self, *args, **kwargs):
            self.state = types.SimpleNamespace()

        def _route(self, *args, **kwargs):
            return lambda fn: fn

        get = post = api_route = _route

    fa.FastAPI, fa.HTTPException, fa.Request = FastAPI, HTTPException, object
    resp.Response, resp.HTMLResponse, resp.JSONResponse, resp.FileResponse = (
        Response, HTMLResponse, JSONResponse, FileResponse)
    fa.responses = resp
    sys.modules["fastapi"], sys.modules["fastapi.responses"] = fa, resp


_install_fastapi_stub()
import launcher as L  # noqa: E402  (must come after the stub)

# (method, path regex, handler, status for a plain-dict result)
ROUTES = [
    ("GET", r"/", lambda r, q: L.index(r), 200),
    ("GET", r"/manifest\.webmanifest", lambda r, q: L.manifest(r), 200),
    ("GET", r"/icon\.svg", lambda r, q: L.icon(), 200),
    ("GET", r"/sw\.js", lambda r, q: L.sw(), 200),
    ("GET", r"/logos/(?P<name>[^/]+)", lambda r, q, name: L.logo(name, r), 200),
    ("POST", r"/api/tune/(?P<number>[^/]+)", lambda r, q, number: L.api_tune(number, r), 202),
    ("POST", r"/api/off", lambda r, q: L.api_off(r), 202),
    ("POST", r"/api/volume/(?P<direction>[^/]+)", lambda r, q, direction: L.api_volume(direction, r), 200),
    ("GET", r"/api/job/(?P<job_id>[^/]+)", lambda r, q, job_id: L.api_job(job_id, r), 200),
    ("GET", r"/api/current", lambda r, q: L.api_current(r), 200),
    ("GET", r"/api/health", lambda r, q: L.api_health(r), 200),
    ("GET|POST", r"/api/button/(?P<button_id>[^/]+)",
     lambda r, q, button_id: L.api_button(button_id, r, q.get("press", "single"), q.get("token", "")), 200),
]
REASONS = {200: "OK", 202: "Accepted", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
           405: "Method Not Allowed", 500: "Internal Server Error", 503: "Service Unavailable"}


async def _dispatch(app, method: str, target: str, headers: dict, body: bytes):
    url = urlsplit(target)
    query = {k: v[0] for k, v in parse_qs(url.query).items()}

    async def _json():
        return json.loads(body or b"{}")

    request = types.SimpleNamespace(app=app, method=method, headers=headers, json=_json)
    for methods, pattern, handler, ok_status in ROUTES:
        m = re.fullmatch(pattern, url.path)
        if m and method in methods.split("|"):
            try:
                return ok_status, await handler(request, query, **m.groupdict())
            except L.HTTPException as e:
                return e.status_code, {"detail": e.detail}
            except Exception:
                log.exception("%s %s failed", method, url.path)
                return 500, {"detail": "internal error"}
    return 404, {"detail": "not found"}


async def _respond(writer, status: int, result) -> None:
    headers: dict = {}
    if isinstance(result, L.Response):
        if result.status_code != 200:  # e.g. the button webhook's explicit 202
            status = result.status_code
        headers, ctype, content = result.headers, result.media_type, result.content
        if isinstance(result, L.JSONResponse):
            content = json.dumps(content, ensure_ascii=False)
    else:
        ctype, content = "application/json", json.dumps(result, ensure_ascii=False)
    data = content.encode() if isinstance(content, str) else content
    head = [f"HTTP/1.1 {status} {REASONS.get(status, 'OK')}", f"Content-Type: {ctype}",
            f"Content-Length: {len(data)}", "Connection: close"]
    head += [f"{k}: {v}" for k, v in headers.items()]
    writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + data)
    await writer.drain()


async def serve() -> None:
    app = types.SimpleNamespace(state=types.SimpleNamespace())
    async with L.lifespan(app):
        cfg = app.state.cfg

        async def handle(reader, writer):
            try:
                raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
                lines = raw.decode("latin-1").split("\r\n")
                method, target, _ = lines[0].split(" ", 2)
                headers = {k.strip().lower(): v.strip() for k, v in
                           (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}
                n = int(headers.get("content-length") or 0)
                body = await asyncio.wait_for(reader.readexactly(n), 15) if n else b""
                await _respond(writer, *await _dispatch(app, method, target, headers, body))
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError,
                    ConnectionError, ValueError):
                pass
            finally:
                writer.close()

        host, port = cfg["server"]["host"], cfg["server"]["port"]
        server = await asyncio.start_server(handle, host, port)
        log.info("serving the kiosk page on http://%s:%s (no FastAPI)",
                 "127.0.0.1" if host in ("0.0.0.0", "") else host, port)
        async with server:
            await server.serve_forever()


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("serve", "diag"):
        print(__doc__)
        return 2
    if sys.argv[1] == "diag":
        import diagnose  # imports launcher, which is already loaded with the stub
        sys.argv = ["diagnose.py", *sys.argv[2:]]
        return asyncio.run(diagnose.main())
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
