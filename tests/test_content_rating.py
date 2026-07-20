from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from dl_engine import content_rating as rating
from dl_engine import index_library as idx


class ContentRatingTests(unittest.TestCase):
    def test_source_nsfw_is_authoritative(self) -> None:
        result = rating.classify_content("nsfw", ["landscape"])
        self.assertEqual(result.rating, "nsfw")
        self.assertEqual(result.basis, "source-purity")
        self.assertEqual(result.confidence, 1.0)

    def test_explicit_tag_makes_sfw_label_more_restrictive(self) -> None:
        result = rating.classify_content("sfw", [{"name": "artistic nudity"}])
        self.assertEqual(result.rating, "nsfw")
        self.assertEqual(result.basis, "explicit-tag")
        self.assertIn("nudity", result.reasons)

    def test_suggestive_tag_is_separate_from_nsfw(self) -> None:
        result = rating.classify_content(None, ["long hair", "lingerie"])
        self.assertEqual(result.rating, "suggestive")
        self.assertEqual(result.basis, "suggestive-tag")

    def test_sketchy_source_rating_maps_to_suggestive(self) -> None:
        result = rating.classify_content("sketchy")
        self.assertEqual(result.rating, "suggestive")
        self.assertEqual(result.confidence, 1.0)

    def test_sfw_requires_positive_source_signal(self) -> None:
        self.assertEqual(rating.classify_content("sfw").rating, "sfw")
        self.assertEqual(rating.classify_content(None, ["landscape"]).rating, "unknown")

    def test_terms_match_whole_tokens(self) -> None:
        result = rating.classify_content(None, ["class assignment", "breastplate"])
        self.assertEqual(result.rating, "unknown")

    def test_sqlite_function_matches_python_classifier(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            rating.register_sqlite_function(conn)
            value = conn.execute(
                "SELECT wallpaper_content_rating(?, ?)",
                (None, f"landscape{rating.TAG_SEPARATOR}hentai"),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(value, "nsfw")


class ContentRatingQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = idx.connect(Path(self._tmp.name) / "index.sqlite")
        rows = [
            ("a.jpg", "sfw"),
            ("b.jpg", "nsfw"),
            ("c.jpg", None),
            ("d.jpg", None),
            ("e.jpg", "sketchy"),
        ]
        for filename, purity in rows:
            self.conn.execute(
                "INSERT INTO images ("
                "path, filename, source, purity, enrichment_status, indexed_at"
                ") VALUES (?, ?, 'wallhaven', ?, 'ok', 'now')",
                (str(Path(self._tmp.name) / filename), filename, purity),
            )
        nude_id = self.conn.execute(
            "INSERT INTO tags (id, name, source, tag_type, provenance) "
            "VALUES (1, 'nude', 'wallhaven', 'People', 'wallhaven-api') "
            "RETURNING id"
        ).fetchone()[0]
        image_id = self.conn.execute(
            "SELECT id FROM images WHERE filename='c.jpg'"
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO image_tags (image_id, tag_id, provenance) VALUES (?, ?, ?)",
            (image_id, nude_id, "wallhaven-api"),
        )
        idx.refresh_derived_metadata(self.conn)
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_query_filters_all_four_ratings(self) -> None:
        self.assertEqual(
            [row.filename for row in idx.query(self.conn, content_rating="sfw")],
            ["a.jpg"],
        )
        self.assertEqual(
            [row.filename for row in idx.query(self.conn, content_rating="nsfw")],
            ["b.jpg", "c.jpg"],
        )
        self.assertEqual(
            [row.filename for row in idx.query(self.conn, content_rating="suggestive")],
            ["e.jpg"],
        )
        self.assertEqual(
            [row.filename for row in idx.query(self.conn, content_rating="unknown")],
            ["d.jpg"],
        )

    def test_query_supports_offset_pagination(self) -> None:
        rows = idx.query(self.conn, content_rating="nsfw", limit=1, offset=1)
        self.assertEqual([row.filename for row in rows], ["c.jpg"])

    def test_image_row_exposes_explainable_rating(self) -> None:
        row = idx.query(self.conn, content_rating="nsfw", limit=1)[0]
        self.assertEqual(row.content_rating.rating, "nsfw")
        self.assertTrue(row.content_rating.basis)

    def test_invalid_rating_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "content_rating"):
            idx.query(self.conn, content_rating="maybe")


if __name__ == "__main__":
    unittest.main()
