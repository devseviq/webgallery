from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from dl_engine import index_library as idx
from dl_engine import library_browser


class LibraryBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.library = self.root / "library"
        self.library.mkdir()
        self.db = self.root / "index.sqlite"

        conn = idx.connect(self.db)
        try:
            rows = [
                ("safe image.jpg", "sfw", "wallhaven", "a" * 64),
                ("adult.jpg", "nsfw", "wallhaven", "b" * 64),
                ("tagged.jpg", None, "anime-pictures", "c" * 64),
                ("unknown.jpg", None, "zerochan", "d" * 64),
                ("review.jpg", "sketchy", "wallhaven", "e" * 64),
            ]
            for filename, purity, source, sha256 in rows:
                path = self.library / filename
                path.write_bytes(b"synthetic image bytes")
                conn.execute(
                    "INSERT INTO images ("
                    "path, filename, source, ext, purity, orientation, "
                    "resolution_bucket, width, height, sha256, "
                    "enrichment_status, indexed_at"
                    ") VALUES (?, ?, ?, 'jpg', ?, 'landscape', '1080p', "
                    "1920, 1080, ?, 'ok', 'now')",
                    (str(path), filename, source, purity, sha256),
                )
            sort_values = {
                "safe image.jpg": ("2026-01-01T00:00:00Z", 100, 1920, 1080),
                "review.jpg": ("2026-01-02T00:00:00Z", 200, 800, 600),
                "unknown.jpg": ("2026-01-03T00:00:00Z", 300, 2560, 1440),
                "tagged.jpg": ("2026-01-04T00:00:00Z", 400, 3840, 2160),
                "adult.jpg": ("2026-01-05T00:00:00Z", 500, 1280, 720),
            }
            for filename, values in sort_values.items():
                conn.execute(
                    "UPDATE images SET download_recorded_at=?, size_bytes=?, "
                    "width=?, height=? WHERE filename=?",
                    (*values, filename),
                )
            conn.execute(
                "UPDATE images SET source_url=? WHERE filename='unknown.jpg'",
                (self.root.as_uri(),),
            )

            conn.executemany(
                "INSERT INTO tags "
                "(id, name, source, tag_type, provenance) VALUES (?, ?, ?, ?, ?)",
                [
                    (1, "nude", "anime-pictures", "content", "provider-api"),
                    (2, "nature", "wallhaven", "subject", "provider-api"),
                ],
            )
            image_ids = {
                row["filename"]: row["id"]
                for row in conn.execute("SELECT id, filename FROM images")
            }
            conn.executemany(
                "INSERT INTO image_tags (image_id, tag_id, provenance) "
                "VALUES (?, ?, 'provider-api')",
                [
                    (image_ids["adult.jpg"], 1),
                    (image_ids["tagged.jpg"], 1),
                    (image_ids["safe image.jpg"], 2),
                ],
            )

            self.unknown_id = image_ids["unknown.jpg"]
            self.pending = idx.upsert_tag_suggestion(
                conn,
                image_id=self.unknown_id,
                label="nude machine guess",
                confidence=0.91,
                generator="visual-test-a",
                model_version="1",
                provenance="synthetic-fixture",
            )
            accepted = idx.upsert_tag_suggestion(
                conn,
                image_id=self.unknown_id,
                label="explicit machine guess",
                confidence=0.82,
                generator="visual-test-b",
                model_version="1",
                provenance="synthetic-fixture",
            )
            idx.review_tag_suggestion(
                conn,
                accepted.id,
                review_status="accepted",
                reviewer="fixture",
                decision_note="accepted suggestion remains non-authoritative",
            )
            rejected = idx.upsert_tag_suggestion(
                conn,
                image_id=self.unknown_id,
                label="adult machine guess",
                confidence=0.73,
                generator="visual-test-c",
                model_version="1",
                provenance="synthetic-fixture",
            )
            idx.review_tag_suggestion(
                conn,
                rejected.id,
                review_status="rejected",
                reviewer="fixture",
                decision_note="rejected fixture",
            )
            idx.refresh_derived_metadata(conn)
            conn.commit()
        finally:
            conn.close()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def assert_path_free(self, value: object) -> None:
        forbidden_keys = {
            "path",
            "db_path",
            "database_path",
            "metadata_path",
            "source_relative_path",
        }
        if isinstance(value, dict):
            self.assertTrue(forbidden_keys.isdisjoint(value))
            for child in value.values():
                self.assert_path_free(child)
        elif isinstance(value, list):
            for child in value:
                self.assert_path_free(child)
        elif isinstance(value, str):
            self.assertNotIn(str(self.root), value)
            self.assertNotIn(str(self.db), value)

    def test_query_page_uses_materialized_ratings_and_safe_media_urls(self) -> None:
        page = library_browser.query_library_page(
            self.db,
            self.library,
            {"rating": "nsfw", "limit": "10"},
        )
        self.assertEqual(page["schema_version"], 2)
        self.assertEqual(
            [item["filename"] for item in page["items"]],
            ["adult.jpg", "tagged.jpg"],
        )
        self.assertTrue(
            all(item["content_rating"] == "nsfw" for item in page["items"])
        )
        for item in page["items"]:
            self.assertEqual(item["url"], item["original_url"])
            self.assertRegex(item["original_url"], r"^/original/[1-9][0-9]*$")
            self.assertRegex(
                item["thumbnail_url"], r"^/thumb/[0-9a-f]{64}\.webp$"
            )
            self.assertTrue(item["exists"])
            self.assertIn("tag_count", item)
            self.assertIn("enrichment_status", item)
            self.assertIn("provider_coverage", item)

        self.assert_path_free(page)
        self.assertEqual(set(page["index"]), {"mtime", "indexed_images"})

    def test_query_page_supports_offset_and_has_more(self) -> None:
        first = library_browser.query_library_page(
            self.db,
            self.library,
            {"rating": "nsfw", "limit": "1"},
        )
        self.assertTrue(first["page"]["has_more"])
        second = library_browser.query_library_page(
            self.db,
            self.library,
            {"rating": "nsfw", "limit": "1", "offset": "1"},
        )
        self.assertFalse(second["page"]["has_more"])
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])

    def test_query_page_remains_readable_during_wal_write_transaction(self) -> None:
        writer = sqlite3.connect(self.db)
        try:
            writer.execute("BEGIN EXCLUSIVE")
            writer.execute(
                "UPDATE images SET filename='uncommitted.jpg' "
                "WHERE filename='safe image.jpg'"
            )
            page = library_browser.query_library_page(
                self.db,
                self.library,
                {"rating": "sfw", "limit": "10"},
            )
        finally:
            writer.rollback()
            writer.close()

        self.assertEqual(page["items"][0]["filename"], "safe image.jpg")

    def test_query_page_supports_stable_full_collection_sorts(self) -> None:
        newest = library_browser.query_library_page(
            self.db, self.library, {"sort": "newest", "limit": "10"}
        )
        self.assertEqual(
            [item["filename"] for item in newest["items"]],
            [
                "adult.jpg",
                "tagged.jpg",
                "unknown.jpg",
                "review.jpg",
                "safe image.jpg",
            ],
        )
        self.assertEqual(newest["filters"]["sort"], "newest")
        self.assertEqual(
            newest["items"][0]["downloaded_at"], "2026-01-05T00:00:00Z"
        )

        quality = library_browser.query_library_page(
            self.db, self.library, {"sort": "resolution_desc", "limit": "2"}
        )
        quality_next = library_browser.query_library_page(
            self.db,
            self.library,
            {"sort": "resolution_desc", "limit": "2", "offset": "2"},
        )
        self.assertEqual(
            [item["filename"] for item in quality["items"]],
            ["tagged.jpg", "unknown.jpg"],
        )
        self.assertEqual(
            [item["filename"] for item in quality_next["items"]],
            ["safe image.jpg", "adult.jpg"],
        )

        least_tagged = library_browser.query_library_page(
            self.db, self.library, {"sort": "least_tagged", "limit": "10"}
        )
        counts = [item["tag_count"] for item in least_tagged["items"]]
        self.assertEqual(counts, sorted(counts))
        self.assertEqual(least_tagged["items"][0]["filename"], "unknown.jpg")

        confidence = library_browser.query_library_page(
            self.db, self.library, {"sort": "rating_confidence", "limit": "10"}
        )
        confidences = [item["rating_confidence"] for item in confidence["items"]]
        self.assertEqual(confidences, sorted(confidences))

    def test_shuffle_seed_is_deterministic_across_pages(self) -> None:
        filters = {"sort": "shuffle", "shuffle_seed": "8675309", "limit": "10"}
        first = library_browser.query_library_page(self.db, self.library, filters)
        again = library_browser.query_library_page(self.db, self.library, filters)
        expected_ids = [item["id"] for item in first["items"]]
        self.assertEqual(expected_ids, [item["id"] for item in again["items"]])
        self.assertEqual(first["filters"]["shuffle_seed"], 8_675_309)

        stitched: list[int] = []
        for offset in (0, 2, 4):
            page = library_browser.query_library_page(
                self.db,
                self.library,
                {
                    "sort": "shuffle",
                    "shuffle_seed": "8675309",
                    "limit": "2",
                    "offset": str(offset),
                },
            )
            stitched.extend(item["id"] for item in page["items"])
        self.assertEqual(stitched, expected_ids)
        self.assertEqual(len(stitched), len(set(stitched)))

    def test_facets_use_materialized_rows_and_report_provider_coverage(self) -> None:
        statements: list[str] = []
        original_open = idx.open_index_read_only

        def traced_open(db_path: Path) -> sqlite3.Connection:
            conn = original_open(db_path)
            conn.set_trace_callback(statements.append)
            return conn

        with mock.patch.object(idx, "open_index_read_only", side_effect=traced_open):
            facets = library_browser.library_facets(self.db)

        self.assertEqual(
            facets["ratings"],
            {"sfw": 1, "suggestive": 1, "nsfw": 2, "unknown": 1},
        )
        self.assertEqual(facets["indexed_images"], 5)
        self.assertEqual(facets["sources"]["wallhaven"], 3)
        self.assertEqual(
            set(facets["provider_coverage"]),
            {
                "total_images",
                "by_source_status",
                "by_tag_count_bucket",
                "by_provenance",
            },
        )
        self.assertEqual(facets["provider_coverage"]["total_images"], 5)
        executed = "\n".join(statements).casefold()
        self.assertNotIn("group_concat", executed)
        self.assertNotIn("wallpaper_content_rating", executed)

    def test_normal_page_uses_constant_shape_tag_and_suggestion_queries(self) -> None:
        def query_statements(limit: int) -> list[str]:
            statements: list[str] = []
            original_open = idx.open_index_read_only

            def traced_open(db_path: Path) -> sqlite3.Connection:
                conn = original_open(db_path)
                conn.set_trace_callback(statements.append)
                return conn

            with mock.patch.object(
                idx, "open_index_read_only", side_effect=traced_open
            ):
                library_browser.query_library_page(
                    self.db, self.library, {"limit": str(limit)}
                )
            return [
                statement
                for statement in statements
                if statement.lstrip().casefold().startswith("select")
            ]

        one = query_statements(1)
        many = query_statements(10)
        self.assertEqual(len(one), len(many))
        self.assertEqual(
            sum(
                "from tags" in statement.casefold()
                and "join image_tags" in statement.casefold()
                for statement in many
            ),
            1,
        )
        self.assertEqual(
            sum("from tag_suggestions" in statement.casefold() for statement in many),
            1,
        )

    def test_counted_autocomplete_is_typed_and_excludes_suggestions(self) -> None:
        payload = library_browser.tag_autocomplete(self.db, "nu", 20)
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["prefix"], "nu")
        self.assertEqual(
            payload["items"],
            [
                {
                    "name": "nude",
                    "type": "content",
                    "provenance": "provider-api",
                    "source": "anime-pictures",
                    "image_count": 2,
                }
            ],
        )
        suggestions_only = library_browser.tag_autocomplete(
            self.db, "nude machine", 20
        )
        self.assertEqual(suggestions_only["items"], [])

    def test_suggestions_remain_separate_at_every_review_status(self) -> None:
        before = library_browser.query_library_page(
            self.db, self.library, {"rating": "unknown", "limit": "10"}
        )
        item = next(item for item in before["items"] if item["id"] == self.unknown_id)
        self.assertIsNone(item["source_url"])
        self.assertEqual(item["tags"], [])
        self.assertEqual(item["tag_count"], 0)
        self.assertEqual(item["content_rating"], "unknown")
        self.assertEqual(
            {suggestion["review_status"] for suggestion in item["tag_suggestions"]},
            {"pending", "accepted", "rejected"},
        )

        conn = idx.open_index_read_only(self.db)
        try:
            before_rating = conn.execute(
                "SELECT content_rating, rating_confidence, tag_count "
                "FROM images WHERE id=?",
                (self.unknown_id,),
            ).fetchone()
            before_tags = conn.execute(
                "SELECT COUNT(*) FROM image_tags WHERE image_id=?",
                (self.unknown_id,),
            ).fetchone()[0]
        finally:
            conn.close()

        reviewed = library_browser.review_tag_suggestion(
            self.db,
            self.pending.id,
            review_status="accepted",
            reviewer="gallery tester",
            decision_note="still a suggestion",
        )
        self.assertEqual(reviewed["suggestion"]["review_status"], "accepted")
        self.assertEqual(reviewed["suggestion"]["reviewer"], "gallery tester")

        conn = idx.open_index_read_only(self.db)
        try:
            after_rating = conn.execute(
                "SELECT content_rating, rating_confidence, tag_count "
                "FROM images WHERE id=?",
                (self.unknown_id,),
            ).fetchone()
            after_tags = conn.execute(
                "SELECT COUNT(*) FROM image_tags WHERE image_id=?",
                (self.unknown_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(tuple(after_rating), tuple(before_rating))
        self.assertEqual(after_tags, before_tags)
        self.assertEqual(
            library_browser.tag_autocomplete(self.db, "nude machine", 20)["items"],
            [],
        )
        with self.assertRaisesRegex(ValueError, "transition"):
            library_browser.review_tag_suggestion(
                self.db,
                self.pending.id,
                review_status="rejected",
                reviewer="gallery tester",
            )

    def test_invalid_filter_and_mutation_values_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "rating"):
            library_browser.query_library_page(
                self.db, self.library, {"rating": "anything"}
            )
        with self.assertRaisesRegex(ValueError, "between"):
            library_browser.query_library_page(
                self.db, self.library, {"limit": "5000"}
            )
        with self.assertRaisesRegex(ValueError, "sort"):
            library_browser.query_library_page(
                self.db, self.library, {"sort": "surprise"}
            )
        with self.assertRaisesRegex(ValueError, "required"):
            library_browser.query_library_page(
                self.db, self.library, {"sort": "shuffle"}
            )
        with self.assertRaisesRegex(ValueError, "between"):
            library_browser.query_library_page(
                self.db,
                self.library,
                {"sort": "shuffle", "shuffle_seed": "-1"},
            )
        with self.assertRaisesRegex(ValueError, "prefix"):
            library_browser.tag_autocomplete(self.db, "", 20)
        with self.assertRaisesRegex(ValueError, "prefix"):
            library_browser.tag_autocomplete(self.db, "x" * 121, 20)
        with self.assertRaisesRegex(ValueError, "between"):
            library_browser.tag_autocomplete(self.db, "nu", 51)
        with self.assertRaisesRegex(ValueError, "review_status"):
            library_browser.review_tag_suggestion(
                self.db,
                self.pending.id,
                review_status="maybe",
                reviewer="tester",
            )
        with self.assertRaisesRegex(ValueError, "reviewer"):
            library_browser.review_tag_suggestion(
                self.db,
                self.pending.id,
                review_status="accepted",
                reviewer="",
            )
        with self.assertRaisesRegex(ValueError, "unknown"):
            library_browser.review_tag_suggestion(
                self.db,
                9_999_999,
                review_status="accepted",
                reviewer="tester",
            )
        with self.assertRaisesRegex(ValueError, "decision_note"):
            library_browser.review_tag_suggestion(
                self.db,
                self.pending.id,
                review_status="accepted",
                reviewer="tester",
                decision_note="x" * 2_001,
            )

    def test_paths_outside_library_are_not_exposed_as_media_urls(self) -> None:
        outside = self.root / "outside.jpg"
        outside.write_bytes(b"synthetic image bytes")
        conn = idx.connect(self.db)
        try:
            cursor = conn.execute(
                "INSERT INTO images ("
                "path, filename, source, ext, purity, sha256, "
                "enrichment_status, indexed_at"
                ") VALUES (?, 'outside.jpg', 'unknown', 'jpg', 'sfw', ?, 'ok', 'now')",
                (str(outside), "f" * 64),
            )
            idx.refresh_derived_metadata(conn, [cursor.lastrowid])
            conn.commit()
        finally:
            conn.close()

        page = library_browser.query_library_page(
            self.db, self.library, {"rating": "sfw", "limit": "10"}
        )
        item = next(item for item in page["items"] if item["filename"] == "outside.jpg")
        self.assertFalse(item["exists"])
        self.assertIsNone(item["url"])
        self.assertIsNone(item["original_url"])
        self.assertIsNone(item["thumbnail_url"])
        self.assertNotIn(str(outside), json.dumps(item))

    def test_verification_status_detects_change_without_exposing_report_path(self) -> None:
        reports = self.root / "reports"
        verify_dir = reports / "maintenance-20260719T000000Z-test"
        verify_dir.mkdir(parents=True)
        verify = verify_dir / "verify.json"
        verify.write_text(
            json.dumps(
                {
                    "ok": True,
                    "status": "ok",
                    "db_path": str(self.db),
                    "library_root": str(self.library),
                    "counts": {"indexed_images": 5},
                }
            ),
            encoding="utf-8",
        )
        now = time.time()
        os.utime(self.db, (now - 10, now - 10))
        os.utime(verify, (now, now))
        current = library_browser.latest_verification_status(
            reports, self.db, self.library
        )
        self.assertTrue(current["verified"])
        self.assertEqual(current["last_report"], "verify.json")
        self.assert_path_free(current)

        wal = Path(str(self.db) + "-wal")
        wal.write_bytes(b"synthetic WAL freshness marker")
        os.utime(wal, (now + 5, now + 5))
        wal_changed = library_browser.latest_verification_status(
            reports, self.db, self.library
        )
        self.assertFalse(wal_changed["verified"])
        self.assertIn("changed after", wal_changed["reason"])
        wal.unlink()

        os.utime(self.db, (now + 10, now + 10))
        changed = library_browser.latest_verification_status(
            reports, self.db, self.library
        )
        self.assertFalse(changed["verified"])
        self.assertIn("changed after", changed["reason"])

    def test_verification_status_rejects_report_for_different_database(self) -> None:
        reports = self.root / "reports"
        verify_dir = reports / "maintenance-20260719T000000Z-test"
        verify_dir.mkdir(parents=True)
        verify = verify_dir / "verify.json"
        verify.write_text(
            json.dumps(
                {
                    "ok": True,
                    "status": "ok",
                    "db_path": str(self.root / "different.sqlite"),
                    "library_root": str(self.library),
                }
            ),
            encoding="utf-8",
        )
        now = time.time()
        os.utime(self.db, (now - 10, now - 10))
        os.utime(verify, (now, now))

        status = library_browser.latest_verification_status(
            reports, self.db, self.library
        )

        self.assertFalse(status["verified"])
        self.assertIn("no maintenance verification report matches", status["reason"])
        self.assertIsNone(status["last_report"])

    def test_verification_status_rejects_report_for_different_library(self) -> None:
        reports = self.root / "reports"
        verify_dir = reports / "maintenance-20260719T000000Z-test"
        verify_dir.mkdir(parents=True)
        verify = verify_dir / "verify.json"
        verify.write_text(
            json.dumps(
                {
                    "ok": True,
                    "status": "ok",
                    "db_path": str(self.db),
                    "library_root": str(self.root / "different-library"),
                }
            ),
            encoding="utf-8",
        )
        now = time.time()
        os.utime(self.db, (now - 10, now - 10))
        os.utime(verify, (now, now))

        status = library_browser.latest_verification_status(
            reports, self.db, self.library
        )

        self.assertFalse(status["verified"])
        self.assertIn("no maintenance verification report matches", status["reason"])
        self.assertIsNone(status["last_report"])

    def test_verification_status_rejects_report_with_missing_identity(self) -> None:
        reports = self.root / "reports"
        verify_dir = reports / "maintenance-20260719T000000Z-test"
        verify_dir.mkdir(parents=True)
        verify = verify_dir / "verify.json"
        verify.write_text(
            json.dumps({"ok": True, "status": "ok"}),
            encoding="utf-8",
        )
        now = time.time()
        os.utime(self.db, (now - 10, now - 10))
        os.utime(verify, (now, now))

        status = library_browser.latest_verification_status(
            reports, self.db, self.library
        )

        self.assertFalse(status["verified"])
        self.assertIn("no maintenance verification report matches", status["reason"])
        self.assertIsNone(status["last_report"])

    def test_verification_status_uses_older_matching_report(self) -> None:
        reports = self.root / "reports"
        matching_dir = reports / "maintenance-20260719T000000Z-gallery"
        unrelated_dir = reports / "maintenance-20260719T010000Z-other"
        malformed_dir = reports / "maintenance-20260719T020000Z-malformed"
        for directory in (matching_dir, unrelated_dir, malformed_dir):
            directory.mkdir(parents=True)

        matching = matching_dir / "verify.json"
        matching.write_text(
            json.dumps(
                {
                    "ok": True,
                    "status": "ok",
                    "db_path": str(self.db),
                    "library_root": str(self.library),
                    "counts": {"indexed_images": 5},
                }
            ),
            encoding="utf-8",
        )
        unrelated = unrelated_dir / "verify.json"
        unrelated.write_text(
            json.dumps(
                {
                    "ok": True,
                    "status": "ok",
                    "db_path": str(self.root / "other.sqlite"),
                    "library_root": str(self.root / "other-library"),
                    "counts": {"indexed_images": 999},
                }
            ),
            encoding="utf-8",
        )
        malformed = malformed_dir / "verify.json"
        malformed.write_text("{not valid JSON", encoding="utf-8")

        now = time.time()
        os.utime(self.db, (now - 30, now - 30))
        os.utime(matching, (now - 20, now - 20))
        os.utime(unrelated, (now - 10, now - 10))
        os.utime(malformed, (now, now))

        status = library_browser.latest_verification_status(
            reports, self.db, self.library
        )

        self.assertTrue(status["verified"])
        self.assertEqual(status["last_counts"], {"indexed_images": 5})
        self.assertIn("still matches", status["reason"])


if __name__ == "__main__":
    unittest.main()
