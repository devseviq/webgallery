#!/usr/bin/env python3
"""Tiny static server for the dashboard with a /_rebuild endpoint.

Serves F:\\Wallpapers\\reports\\ on http://127.0.0.1:8090/ by default.
GET /_rebuild runs the snapshot + render pipeline (same as a manual
"Rebuild & Reload" press in the browser) and returns a small JSON status.

Run with:
    python dashboard_server.py [--port 8090] [--host 127.0.0.1]

Stop with Ctrl-C.
"""
import argparse
import http.server
import json
import os
import sqlite3
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path
import urllib.parse

ROOT = Path(r"F:\Wallpapers")
REPORTS = ROOT / "reports"
BUILD_SCRIPT = REPORTS / "_build_dashboard.py"
RENDER_SCRIPT = REPORTS / "_render_dashboard.py"
# Served root: F:\Wallpapers, so the live dashboard can fetch
# ../.wallpaper-download-queue/state.json, the preview gallery can pull
# ../temp_downloads/<source>/<sub>/<file>.jpg, and the static dashboard
# can pull ../library/<...>.jpg. / is rewritten to the reports dashboard.
SERVE_ROOT = ROOT
LIBRARY_ROOT = ROOT / "library"
LIBRARY_DB = ROOT / "wallpaper_library.sqlite"
DL_ENGINE_SRC = ROOT / "dl-engine" / "src"
if str(DL_ENGINE_SRC) not in sys.path:
    sys.path.insert(0, str(DL_ENGINE_SRC))

from dl_engine import library_browser, library_transfer  # noqa: E402

PAUSE_FLAG = ROOT / ".wallpaper-download-queue" / "pause.flag"
# Lock so two concurrent /_rebuild requests don't fight over the same files
rebuild_lock = threading.Lock()
last_rebuild = {"at": None, "ok": None, "duration_s": None, "log": ""}
facet_cache_lock = threading.Lock()
facet_cache = {"key": None, "value": None}
TRANSFER_CONFIG_PATH = Path(
    os.environ.get(
        "WALLPAPER_TRANSFER_CONFIG",
        str(ROOT / ".wallpaper-transfer-targets.json"),
    )
)
transfer_manager = library_transfer.ScpTransferManager(
    LIBRARY_DB,
    LIBRARY_ROOT,
    TRANSFER_CONFIG_PATH,
)


def run_rebuild():
    started = time.time()
    log_lines = []
    ok = True
    with rebuild_lock:
        for script in (BUILD_SCRIPT, RENDER_SCRIPT):
            if not script.exists():
                log_lines.append(f"missing: {script}")
                ok = False
                break
            try:
                cp = subprocess.run(
                    [sys.executable, str(script)],
                    cwd=str(REPORTS),
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                log_lines.append(f"--- {script.name} exit={cp.returncode} ---")
                if cp.stdout:
                    log_lines.append(cp.stdout[-2000:])
                if cp.stderr:
                    log_lines.append("STDERR: " + cp.stderr[-2000:])
                if cp.returncode != 0:
                    ok = False
                    break
            except subprocess.TimeoutExpired:
                log_lines.append(f"timeout running {script}")
                ok = False
                break
            except Exception as e:
                log_lines.append(f"error running {script}: {e}")
                ok = False
                break
        duration = time.time() - started
        last_rebuild.update(
            at=time.strftime("%Y-%m-%d %H:%M:%S"),
            ok=ok,
            duration_s=round(duration, 2),
            log="\n".join(log_lines)[-6000:],
        )
    return last_rebuild


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SERVE_ROOT), **kwargs)

    # Disallow directory listings now that we serve the wider root.
    def list_directory(self, path):
        self.send_error(403, "Directory listing disabled")
        return None

    def log_message(self, fmt, *args):
        # quieter than the default
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def send_json(self, payload, status=200):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self, max_bytes=64 * 1024):
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise ValueError("Content-Length must be an integer") from exc
        if length <= 0:
            raise ValueError("request body is empty")
        if length > max_bytes:
            raise OverflowError(f"request body exceeds {max_bytes} bytes")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def same_origin_request(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        expected = "http://" + self.headers.get("Host", "")
        return origin.rstrip("/") == expected.rstrip("/")

    def transfer_config_requested(self):
        try:
            requested = Path(self.translate_path(self.path)).resolve(strict=False)
            configured = TRANSFER_CONFIG_PATH.resolve(strict=False)
        except OSError:
            return False
        return requested == configured

    def library_query_params(self):
        if len(self.path) > 4096:
            raise ValueError("query string is too long")
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(
            parsed.query,
            keep_blank_values=True,
            max_num_fields=20,
        )
        allowed = {
            "rating", "orientation", "franchise", "bucket", "source",
            "tag", "sort", "limit", "offset",
        }
        unknown = sorted(set(query) - allowed)
        if unknown:
            raise ValueError("unsupported query field(s): " + ", ".join(unknown))
        return {key: values[-1] for key, values in query.items()}

    def library_facets(self):
        stat = LIBRARY_DB.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        with facet_cache_lock:
            if facet_cache["key"] != key:
                facet_cache["value"] = library_browser.library_facets(LIBRARY_DB)
                facet_cache["key"] = key
            return facet_cache["value"]

    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path).path
        if self.transfer_config_requested():
            self.send_error(404, "Not found")
            return
        if parsed_path == "/api/library/transfers":
            try:
                status = transfer_manager.service_status()
            except library_transfer.TransferConfigurationError as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=503)
                return
            self.send_json({"ok": True, **status})
            return
        if parsed_path.startswith("/api/library/transfers/"):
            job_id = parsed_path.removeprefix("/api/library/transfers/")
            job = transfer_manager.get_job(job_id)
            if job is None:
                self.send_json({"ok": False, "error": "transfer job not found"}, status=404)
                return
            self.send_json({"ok": True, "job": job})
            return
        if parsed_path == "/api/library/status":
            try:
                status = library_browser.latest_verification_status(REPORTS, LIBRARY_DB)
                status["facets"] = self.library_facets()
            except (FileNotFoundError, OSError, sqlite3.Error, ValueError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=503)
                return
            self.send_json({"ok": True, **status})
            return
        if parsed_path == "/api/library/facets":
            try:
                facets = self.library_facets()
            except (FileNotFoundError, OSError, sqlite3.Error, ValueError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=503)
                return
            self.send_json({"ok": True, **facets})
            return
        if parsed_path == "/api/library":
            try:
                payload = library_browser.query_library_page(
                    LIBRARY_DB,
                    LIBRARY_ROOT,
                    self.library_query_params(),
                )
                payload["verification"] = library_browser.latest_verification_status(
                    REPORTS, LIBRARY_DB
                )
            except ValueError as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
                return
            except (FileNotFoundError, OSError, sqlite3.Error) as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=503)
                return
            self.send_json({"ok": True, **payload})
            return
        if self.path.startswith("/_pause_status"):
            body = json.dumps({"paused": PAUSE_FLAG.exists()}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/_pause") and not self.path.startswith("/_pause_status"):
            try:
                PAUSE_FLAG.touch()
            except Exception:
                pass
            body = json.dumps({"paused": True, "ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/_resume"):
            try:
                PAUSE_FLAG.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass
            body = json.dumps({"paused": False, "ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/_rebuild_status"):
            body = json.dumps(last_rebuild, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/_rebuild"):
            # background rebuild, return immediately, but if ?wait=1 then wait
            wait = "wait=1" in self.path
            if wait:
                result = run_rebuild()
            else:
                t = threading.Thread(target=run_rebuild, daemon=True)
                t.start()
                result = {"status": "started", "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            body = json.dumps(result, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed_path in {"/library", "/library/"}:
            self.send_response(302)
            self.send_header("Location", "/reports/library-browser.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # Root: redirect to the live dashboard for convenience.
        if self.path == "/" or self.path == "":
            self.send_response(302)
            self.send_header("Location", "/reports/download-queue-dashboard.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # /reports -> /reports/dashboard.html
        if self.path == "/reports" or self.path == "/reports/":
            self.send_response(302)
            self.send_header("Location", "/reports/dashboard.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # Normal static file - strip leading "/"
        return super().do_GET()

    def do_POST(self):
        parsed_path = urllib.parse.urlparse(self.path).path
        if parsed_path != "/api/library/transfers":
            self.send_json({"ok": False, "error": "endpoint not found"}, status=404)
            return
        if not self.same_origin_request():
            self.send_json({"ok": False, "error": "cross-origin transfer denied"}, status=403)
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            self.send_json(
                {"ok": False, "error": "Content-Type must be application/json"},
                status=415,
            )
            return
        try:
            payload = self.read_json_body()
            unknown = sorted(set(payload) - {"target", "image_ids"})
            if unknown:
                raise library_transfer.TransferRequestError(
                    "unsupported request field(s): " + ", ".join(unknown)
                )
            job = transfer_manager.create_job(
                payload.get("target"),
                payload.get("image_ids"),
            )
        except OverflowError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=413)
            return
        except (ValueError, library_transfer.TransferRequestError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        except (
            library_transfer.TransferConfigurationError,
            library_transfer.TransferUnavailableError,
            FileNotFoundError,
            OSError,
            sqlite3.Error,
        ) as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=503)
            return
        self.send_json({"ok": True, "job": job}, status=202)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8090)
    args = p.parse_args()

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((args.host, args.port), Handler) as srv:
        url = f"http://{args.host}:{args.port}/"
        print(f"dashboard server listening on {url}")
        print(f"  * open:    {url}dashboard.html")
        print(f"  * rebuild: POST or GET {url}_rebuild?wait=1")
        print("press Ctrl-C to stop")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nshutting down")


if __name__ == "__main__":
    main()
