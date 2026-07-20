from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import http.client
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import quote

from PIL import Image

from dl_engine import index_library as idx


SERVER_PATH = Path(__file__).parents[1] / "reports" / "dashboard_server.py"
SPEC = importlib.util.spec_from_file_location("test_dashboard_server_module", SERVER_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import setup guard
    raise RuntimeError("could not load dashboard_server.py")
dashboard_server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard_server)


class FakeTransferManager:
    def __init__(self) -> None:
        self.created: list[tuple[object, object]] = []
        self.jobs = {
            "job-1": {
                "id": "job-1",
                "status": "completed",
                "item_count": 1,
                "target_label": "Fixture target",
            }
        }

    def service_status(self) -> dict[str, object]:
        return {
            "enabled": True,
            "max_items": 100,
            "copy_only": True,
            "targets": [{"id": "fixture", "label": "Fixture target"}],
            "recent_jobs": list(self.jobs.values()),
        }

    def get_job(self, job_id: str) -> dict[str, object] | None:
        return self.jobs.get(job_id)

    def create_job(self, target: object, image_ids: object) -> dict[str, object]:
        self.created.append((target, image_ids))
        return {
            "id": "job-new",
            "status": "queued",
            "item_count": len(image_ids) if isinstance(image_ids, list) else 0,
            "target_label": "Fixture target",
        }


class DashboardServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.collection = self.root / "collection"
        self.library = self.collection / "library"
        self.preview = self.collection / "temp_downloads"
        self.output_reports = self.collection / "reports"
        self.app_reports = self.root / "app-reports"
        self.cache = self.collection / "thumb-cache"
        self.queue_dir = self.collection / ".wallpaper-download-queue"
        for directory in (
            self.library,
            self.preview,
            self.output_reports,
            self.app_reports,
            self.queue_dir,
        ):
            directory.mkdir(parents=True)

        (self.output_reports / "download-queue-dashboard.html").write_text(
            "<h1>operations</h1>", encoding="utf-8"
        )
        (self.output_reports / "dashboard.html").write_text(
            "<h1>snapshot</h1>", encoding="utf-8"
        )
        (self.app_reports / "library-browser.html").write_text(
            "<h1>library</h1>", encoding="utf-8"
        )
        (self.output_reports / "private.csv").write_text(
            "secret,data", encoding="utf-8"
        )
        (self.output_reports / "diagnostic.log").write_text(
            "secret log", encoding="utf-8"
        )

        self.preview_image = self.preview / "queue folder" / "preview one.png"
        self.preview_image.parent.mkdir()
        with Image.new("RGB", (30, 20), (10, 20, 30)) as image:
            image.save(self.preview_image)

        self.canonical = self.library / "safe image.jpg"
        with Image.new("RGB", (900, 450), (60, 90, 120)) as image:
            image.save(self.canonical)
        self.second = self.library / "second.png"
        with Image.new("RGB", (40, 50), (100, 20, 10)) as image:
            image.save(self.second)

        self.outside = self.root / "outside.jpg"
        self.outside.write_bytes(b"outside")
        self.unsupported = self.library / "notes.txt"
        self.unsupported.write_text("not an image", encoding="utf-8")
        self.missing = self.library / "missing.jpg"
        self.duplicate_one = self.library / "duplicate-one.jpg"
        self.duplicate_two = self.library / "duplicate-two.jpg"
        self.duplicate_one.write_bytes(self.canonical.read_bytes())
        self.duplicate_two.write_bytes(self.canonical.read_bytes())

        self.sha = "a" * 64
        self.duplicate_sha = "d" * 64
        self.db = self.collection / "wallpaper_library.sqlite"
        conn = idx.connect(self.db)
        try:
            self.image_id = self._insert_image(
                conn, self.canonical, sha256=self.sha, purity="sfw"
            )
            self.second_id = self._insert_image(
                conn, self.second, sha256="b" * 64, purity="sfw"
            )
            self.outside_id = self._insert_image(
                conn, self.outside, sha256="c" * 64, purity="sfw"
            )
            self.unsupported_id = self._insert_image(
                conn, self.unsupported, sha256="e" * 64, ext=".txt"
            )
            self.missing_id = self._insert_image(
                conn, self.missing, sha256="f" * 64, ext=".jpg"
            )
            self._insert_image(conn, self.duplicate_one, sha256=self.duplicate_sha)
            self._insert_image(conn, self.duplicate_two, sha256=self.duplicate_sha)
            conn.commit()
        finally:
            conn.close()

        now = datetime.now(timezone.utc)
        same_instant_with_offset = now.astimezone(timezone(timedelta(hours=-7)))
        self.queue_state = self.queue_dir / "state.json"
        self.queue_state.write_text(
            json.dumps(
                {
                    "updatedAt": now.isoformat(),
                    "lastWorkerAt": now.isoformat(),
                    "lastMessage": (
                        "downloaded https://example.invalid/private "
                        "token=hunter2 F:\\Wallpapers\\secret.jpg"
                    ),
                    "credential": "must-not-leak",
                    "jobs": [
                        {
                            "status": "completed",
                            "finishedAt": same_instant_with_offset.isoformat(),
                            "url": "https://secret.invalid/job",
                            "args": ["--api-key", "secret"],
                        },
                        {
                            "status": "failed",
                            "handler": "wallpaper-download",
                            "url": "https://secret.invalid/failure",
                        },
                        {"status": "pending", "target_path": "F:/private"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.pause_flag = self.queue_dir / "pause.flag"
        self.transfer_config = self.collection / ".wallpaper-transfer-targets.json"
        self.environment = self.collection / ".env"
        self.environment.write_text("API_KEY=top-secret", encoding="utf-8")

        self.config = dashboard_server.ServerConfig(
            collection_root=self.collection,
            library_root=self.library,
            database_path=self.db,
            queue_state_path=self.queue_state,
            report_output_root=self.output_reports,
            thumbnail_cache_root=self.cache,
            preview_root=self.preview,
            pause_flag_path=self.pause_flag,
            transfer_config_path=self.transfer_config,
            environment_path=self.environment,
            app_reports_root=self.app_reports,
        )
        self.transfer_manager = FakeTransferManager()
        self.server = dashboard_server.create_server(
            ("127.0.0.1", 0),
            self.config,
            transfer_manager=self.transfer_manager,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address[:2]
        self.origin = f"http://{self.host}:{self.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    @staticmethod
    def _insert_image(
        conn: sqlite3.Connection,
        path: Path,
        *,
        sha256: str,
        purity: str | None = None,
        ext: str | None = None,
    ) -> int:
        cursor = conn.execute(
            "INSERT INTO images ("
            "path, filename, source, ext, purity, orientation, resolution_bucket, "
            "width, height, sha256, enrichment_status, indexed_at"
            ") VALUES (?, ?, 'fixture', ?, ?, 'landscape', 'SD', 30, 20, ?, 'ok', 'now')",
            (str(path), path.name, ext or path.suffix, purity, sha256),
        )
        return int(cursor.lastrowid)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            payload = response.read()
            return response.status, {key.casefold(): value for key, value in response.getheaders()}, payload
        finally:
            connection.close()

    def json_request(
        self,
        method: str,
        path: str,
        *,
        value: object | None = None,
        origin: str | None = None,
    ) -> tuple[int, dict[str, str], dict[str, object]]:
        body = None if value is None else json.dumps(value)
        headers: dict[str, str] = {}
        if value is not None:
            headers["Content-Type"] = "application/json"
        if origin is not None:
            headers["Origin"] = origin
        status, response_headers, payload = self.request(
            method, path, body=body, headers=headers
        )
        return status, response_headers, json.loads(payload or b"{}")

    def assert_security_headers(self, headers: dict[str, str]) -> None:
        self.assertEqual(headers["referrer-policy"], "no-referrer")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertIn("default-src 'self'", headers["content-security-policy"])

    def test_exact_static_allowlist_redirects_and_security_headers(self) -> None:
        for path, location in (
            ("/", "/reports/download-queue-dashboard.html"),
            ("/library", "/reports/library-browser.html"),
            ("/reports", "/reports/dashboard.html"),
        ):
            with self.subTest(path=path):
                status, headers, payload = self.request("GET", path)
                self.assertEqual(status, 302)
                self.assertEqual(headers["location"], location)
                self.assertEqual(payload, b"")
                self.assert_security_headers(headers)

        expected = {
            "/reports/download-queue-dashboard.html": b"<h1>operations</h1>",
            "/reports/dashboard.html": b"<h1>snapshot</h1>",
            "/reports/library-browser.html": b"<h1>library</h1>",
        }
        for path, body in expected.items():
            with self.subTest(path=path):
                status, headers, payload = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(payload, body)
                self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
                self.assert_security_headers(headers)

    def test_denies_workspace_secrets_traversal_and_arbitrary_reports(self) -> None:
        denied = (
            "/.wallpaper-download-queue/state.json",
            "/.env",
            "/.env.bak",
            "/anime_pictures_config.json",
            "/zerochan_config.json",
            "/wallpaper_library.sqlite",
            "/reports/private.csv",
            "/reports/diagnostic.log",
            "/reports/dashboard_server.py",
            "/reports/README_live_dashboard.md",
            "/reports/../.env",
            "/reports/%2e%2e/.env",
            "/media/preview/../.env",
            "/media/preview/%2e%2e/.env",
            "/media/preview/C%3A/Windows/secret.jpg",
            "/media/preview/%2F%2Fserver/share/secret.jpg",
            "/media/preview/name.jpg%3Astream",
            "/media/preview/folder%5Csecret.jpg",
            "/media/preview/queue%20folder%2Fpreview%20one.png",
            "/media/preview/%25002e%25002e/secret.jpg",
        )
        for path in denied:
            with self.subTest(path=path):
                status, headers, payload = self.request("HEAD", path)
                self.assertEqual(status, 404)
                self.assertEqual(payload, b"")
                self.assert_security_headers(headers)

    def test_operations_status_is_aggregate_sanitized_and_utc_aware(self) -> None:
        status, headers, payload = self.json_request("GET", "/api/operations/status")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            payload["counts"],
            {
                "total": 3,
                "completed": 1,
                "failed": 1,
                "pending": 1,
                "running": 0,
                "removed": 0,
            },
        )
        self.assertEqual(payload["failed_by_handler"], {"wallpaper-download": 1})
        self.assertEqual(payload["completed_today_utc"], 1)
        self.assertFalse(payload["paused"])
        serialized = json.dumps(payload)
        for secret in (
            "must-not-leak",
            "secret.invalid",
            "hunter2",
            "top-secret",
            "F:\\\\Wallpapers",
            "target_path",
            '"jobs"',
            '"args"',
            '"credential"',
        ):
            self.assertNotIn(secret, serialized)
        self.assertLessEqual(len(str(payload["lastMessage"])), 80)
        self.assert_security_headers(headers)

    def test_library_api_removes_paths_and_replaces_legacy_urls(self) -> None:
        status, _headers, payload = self.json_request(
            "GET", "/api/library?limit=20"
        )
        self.assertEqual(status, 200)
        self.assertNotIn("path", payload["index"])
        items = {item["id"]: item for item in payload["items"]}
        canonical = items[self.image_id]
        self.assertNotIn("path", canonical)
        self.assertEqual(canonical["url"], f"/original/{self.image_id}")
        self.assertEqual(canonical["original_url"], f"/original/{self.image_id}")
        self.assertEqual(canonical["thumbnail_url"], f"/thumb/{self.sha}.webp")
        self.assertIsNone(items[self.outside_id]["url"])
        self.assertIsNone(items[self.missing_id]["url"])
        self.assertNotIn(str(self.root), json.dumps(payload))

        status, _headers, bad = self.json_request("GET", "/api/library?unknown=1")
        self.assertEqual(status, 400)
        self.assertFalse(bad["ok"])

    def test_library_status_facets_tags_and_transfer_get_routes(self) -> None:
        for path in ("/api/library/status", "/api/library/facets"):
            with self.subTest(path=path):
                status, _headers, payload = self.json_request("GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(payload["ok"])
                self.assertNotIn(str(self.root), json.dumps(payload))

        with mock.patch.object(
            dashboard_server.library_browser,
            "tag_autocomplete",
            create=True,
            return_value={"items": [{"name": "sky", "count": 2}]},
        ) as autocomplete:
            status, _headers, payload = self.json_request(
                "GET", "/api/library/tags?prefix=sk&limit=5"
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"][0]["name"], "sky")
        autocomplete.assert_called_once_with(self.db.absolute(), "sk", 5)

        status, _headers, payload = self.json_request("GET", "/api/library/transfers")
        self.assertEqual(status, 200)
        self.assertTrue(payload["enabled"])
        status, _headers, payload = self.json_request(
            "GET", "/api/library/transfers/job-1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["job"]["status"], "completed")
        status, _headers, _payload = self.json_request(
            "GET", "/api/library/transfers/missing"
        )
        self.assertEqual(status, 404)

    def test_library_facet_cache_detects_wal_only_commits(self) -> None:
        main_before = self.db.stat()
        with mock.patch.object(
            dashboard_server.library_browser,
            "library_facets",
            side_effect=[{"generation": 1}, {"generation": 2}],
        ) as facets:
            status, _headers, first = self.json_request(
                "GET", "/api/library/facets"
            )
            self.assertEqual(status, 200)
            self.assertEqual(first["generation"], 1)

            writer = sqlite3.connect(self.db)
            try:
                self.assertEqual(
                    writer.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(),
                    "wal",
                )
                writer.execute(
                    "UPDATE images SET indexed_at='wal-refresh' WHERE id=?",
                    (self.image_id,),
                )
                writer.commit()
                self.assertTrue(Path(str(self.db) + "-wal").is_file())
                main_after = self.db.stat()
                self.assertEqual(
                    (main_before.st_mtime_ns, main_before.st_size),
                    (main_after.st_mtime_ns, main_after.st_size),
                )

                status, _headers, second = self.json_request(
                    "GET", "/api/library/facets"
                )
                self.assertEqual(status, 200)
                self.assertEqual(second["generation"], 2)
            finally:
                writer.close()

        self.assertEqual(facets.call_count, 2)

    def test_media_original_and_thumbnail_streaming(self) -> None:
        preview_url = "/media/preview/" + "/".join(
            quote(part, safe="")
            for part in self.preview_image.relative_to(self.preview).parts
        )
        for path, expected in (
            (preview_url, self.preview_image.read_bytes()),
            ("/media/library/" + quote(self.canonical.name, safe=""), self.canonical.read_bytes()),
            (f"/original/{self.image_id}", self.canonical.read_bytes()),
        ):
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(body, expected)
                self.assertTrue(headers["content-type"].startswith("image/"))

        thumb_path = f"/thumb/{self.sha}.webp"
        status, headers, first = self.request("GET", thumb_path)
        self.assertEqual(status, 200)
        self.assertGreater(len(first), 0)
        self.assertEqual(headers["content-type"], "image/webp")
        self.assertEqual(
            headers["cache-control"], "public, max-age=31536000, immutable"
        )
        artifacts = list(self.cache.rglob("*.webp"))
        self.assertEqual(len(artifacts), 1)
        before = artifacts[0].stat().st_mtime_ns
        status, _headers, second = self.request("GET", thumb_path)
        self.assertEqual(status, 200)
        self.assertEqual(second, first)
        self.assertEqual(artifacts[0].stat().st_mtime_ns, before)

    def test_indexed_media_fail_closed_for_bad_identity_or_source(self) -> None:
        expectations = {
            f"/original/{self.outside_id}": 503,
            f"/original/{self.unsupported_id}": 503,
            f"/original/{self.missing_id}": 503,
            "/original/0": 400,
            "/original/not-an-id": 400,
            "/thumb/not-a-sha.webp": 400,
            f"/thumb/{'9' * 64}.webp": 404,
            f"/thumb/{self.duplicate_sha}.webp": 503,
        }
        for path, expected in expectations.items():
            with self.subTest(path=path):
                status, headers, _payload = self.request("GET", path)
                self.assertEqual(status, expected)
                self.assert_security_headers(headers)

    def test_missing_runtime_inputs_return_service_unavailable(self) -> None:
        original = self.server.config
        try:
            self.server.config = replace(
                original, queue_state_path=self.root / "missing-state.json"
            )
            status, _headers, payload = self.json_request(
                "GET", "/api/operations/status"
            )
            self.assertEqual(status, 503)
            self.assertFalse(payload["ok"])

            self.server.config = replace(
                original, database_path=self.root / "missing-index.sqlite"
            )
            status, _headers, _payload = self.request(
                "GET", f"/original/{self.image_id}"
            )
            self.assertEqual(status, 503)
        finally:
            self.server.config = original

        self.cache.write_bytes(b"not a directory")
        status, _headers, _payload = self.request(
            "GET", f"/thumb/{'b' * 64}.webp"
        )
        self.assertEqual(status, 503)

    def test_mutations_require_post_and_same_origin(self) -> None:
        for path in ("/_pause", "/_resume", "/_rebuild"):
            with self.subTest(path=path):
                status, headers, _body = self.request("GET", path)
                self.assertEqual(status, 405)
                self.assertEqual(headers["allow"], "POST")

        status, _headers, payload = self.json_request(
            "POST", "/_pause", origin="https://evil.invalid"
        )
        self.assertEqual(status, 403)
        self.assertFalse(self.pause_flag.exists())
        self.assertFalse(payload["ok"])

        status, _headers, payload = self.json_request(
            "POST", "/_pause", value={}, origin=self.origin
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["paused"])
        status, _headers, status_payload = self.json_request("GET", "/_pause_status")
        self.assertEqual(status, 200)
        self.assertTrue(status_payload["paused"])
        status, _headers, payload = self.json_request(
            "POST", "/_resume", value={}, origin=self.origin
        )
        self.assertEqual(status, 200)
        self.assertFalse(payload["paused"])

        status, _headers, payload = self.json_request(
            "POST", "/_pause", value={"unexpected": True}, origin=self.origin
        )
        self.assertEqual(status, 400)
        self.assertFalse(self.pause_flag.exists())
        self.assertFalse(payload["ok"])

        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            connection.request(
                "POST",
                "/_pause",
                body="{}",
                headers={
                    "Content-Type": "application/json",
                    "Origin": self.origin,
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.request("GET", "/_pause_status")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(json.loads(response.read())["paused"])
        finally:
            connection.close()
        self.pause_flag.unlink()

        status, headers, _payload = self.json_request("POST", "/api/library")
        self.assertEqual(status, 405)
        self.assertEqual(headers["allow"], "GET, HEAD")
        status, headers, _body = self.request("PUT", f"/original/{self.image_id}")
        self.assertEqual(status, 405)
        self.assertEqual(headers["allow"], "GET, HEAD")
        status, headers, _body = self.request("TRACE", "/api/library")
        self.assertEqual(status, 405)
        self.assertEqual(headers["allow"], "GET, HEAD")

        with self.assertRaisesRegex(ValueError, "loopback"):
            dashboard_server.create_server(
                ("0.0.0.0", 0),
                self.config,
                transfer_manager=self.transfer_manager,
            )

    def test_rebuild_status_ordering_explicit_subprocess_paths_and_no_logs(self) -> None:
        status, _headers, payload = self.json_request("GET", "/_rebuild_status")
        self.assertEqual(status, 200)
        self.assertIsNone(payload["ok"])
        self.assertNotIn("log", payload)

        with mock.patch.object(
            dashboard_server.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0),
        ) as runner:
            status, _headers, payload = self.json_request(
                "POST", "/_rebuild?wait=1", value={}, origin=self.origin
            )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertNotIn("log", payload)
        self.assertEqual(runner.call_count, 2)
        flattened = " ".join(
            str(part)
            for call in runner.call_args_list
            for part in call.args[0]
        )
        for required in (
            str(self.collection.absolute()),
            str(self.queue_state.absolute()),
            str(self.library.absolute()),
            str(self.preview.absolute()),
            str(self.output_reports.absolute()),
            str(self.pause_flag.absolute()),
        ):
            self.assertIn(required, flattened)

        status, _headers, payload = self.json_request("GET", "/_rebuild_status")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertNotIn("log", payload)

    def test_transfer_and_suggestion_post_contracts(self) -> None:
        status, _headers, payload = self.json_request(
            "POST",
            "/api/library/transfers",
            value={"target": "fixture", "image_ids": [self.image_id]},
            origin=self.origin,
        )
        self.assertEqual(status, 202)
        self.assertEqual(payload["job"]["id"], "job-new")
        self.assertEqual(
            self.transfer_manager.created, [("fixture", [self.image_id])]
        )

        with mock.patch.object(
            dashboard_server.library_browser,
            "review_tag_suggestion",
            create=True,
            return_value={"suggestion": {"id": 7, "review_status": "accepted"}},
        ) as review:
            status, _headers, payload = self.json_request(
                "POST",
                "/api/library/suggestions/7",
                value={
                    "review_status": "accepted",
                    "reviewer": "local-user",
                    "decision_note": "looks right",
                },
                origin=self.origin,
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["suggestion"]["review_status"], "accepted")
        review.assert_called_once_with(
            self.db.absolute(),
            7,
            review_status="accepted",
            reviewer="local-user",
            decision_note="looks right",
        )

    def test_head_is_explicit_and_threaded_requests_overlap(self) -> None:
        status, headers, body = self.request(
            "HEAD", "/reports/download-queue-dashboard.html"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(int(headers["content-length"]), len(b"<h1>operations</h1>"))

        barrier = threading.Barrier(2)

        def slow_status(_config: object) -> dict[str, object]:
            barrier.wait(timeout=2)
            time.sleep(0.05)
            return {
                "schema_version": 1,
                "counts": {},
                "failed_by_handler": {},
                "completed_today_utc": 0,
                "updatedAt": None,
                "lastWorkerAt": None,
                "lastMessage": "",
                "paused": False,
            }

        with mock.patch.object(
            dashboard_server, "_operations_status", side_effect=slow_status
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(
                    pool.map(
                        lambda _value: self.request("GET", "/api/operations/status"),
                        range(2),
                    )
                )
        self.assertEqual([result[0] for result in results], [200, 200])


if __name__ == "__main__":
    unittest.main()
