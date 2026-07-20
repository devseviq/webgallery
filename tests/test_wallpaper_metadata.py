from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from dl_engine import wallpaper_metadata as metadata


def _valid_document(filename: str | None = None) -> dict:
    filename = filename or (
        "src=anime-pictures__id=921265__size=3986x2304"
        "__slug=azur-lane-evertsen.png"
    )
    return {
        "schema_version": 1,
        "source": "anime-pictures",
        "source_id": "921265",
        "source_url": "https://anime-pictures.net/pictures/view_post/921265",
        "original_filename": "921265-3986x2304-azur+lane-evertsen.png",
        "canonical_filename": filename,
        "slug": "azur-lane-evertsen",
        "file": {
            "sha256": "a" * 64,
            "size_bytes": 1234,
            "extension": ".png",
            "width": 3986,
            "height": 2304,
        },
        "classification": {
            "resolution_bucket": "4K",
            "orientation": "landscape",
        },
        "download": {
            "transport": "queue-browser",
            "source_relative_path": "anime-pictures-full/original.png",
            "recorded_at": "2026-07-15T12:30:00+00:00",
        },
        "tags": [
            {
                "name": "Azur Lane",
                "slug": "azur-lane",
                "type": "franchise",
                "provenance": "anime-pictures-url",
            }
        ],
        "search_origins": ["azur lane", "evertsen"],
    }


class CanonicalFilenameTests(unittest.TestCase):
    def test_parses_base_filename(self) -> None:
        parsed = metadata.parse_canonical_filename(
            "src=wallhaven__id=g7xd1d__size=2000x2999__slug=genshin-impact.jpg"
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.source, "wallhaven")
        self.assertEqual(parsed.source_id, "g7xd1d")
        self.assertEqual((parsed.width, parsed.height), (2000, 2999))
        self.assertEqual(parsed.slug, "genshin-impact")
        self.assertIsNone(parsed.collision_hash)
        self.assertEqual(parsed.extension, ".jpg")

    def test_parses_collision_suffix(self) -> None:
        parsed = metadata.parse_canonical_filename(
            "src=zerochan__id=4606461__size=2500x3500__slug=red-hair"
            "__sha=012345abcdef.png"
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.collision_hash, "012345abcdef")

    def test_collision_suffix_must_match_sha256(self) -> None:
        filename = (
            "src=anime-pictures__id=921265__size=3986x2304"
            "__slug=azur-lane-evertsen__sha=012345abcdef.png"
        )
        document = _valid_document(filename)
        errors = metadata.validate_metadata(document)
        self.assertIn(
            "canonical_filename collision suffix does not match file.sha256",
            errors,
        )
        document["file"]["sha256"] = "012345abcdef" + "0" * 52
        self.assertEqual(metadata.validate_metadata(document), [])

    def test_parses_total_unknown_dimensions(self) -> None:
        parsed = metadata.parse_canonical_filename(
            "src=unknown__id=legacy-1__size=0x0__slug=unreadable.webp"
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual((parsed.width, parsed.height), (0, 0))

    def test_rejects_noncanonical_values(self) -> None:
        self.assertIsNone(metadata.parse_canonical_filename("wallhaven_abc_1x1.jpg"))
        self.assertIsNone(
            metadata.parse_canonical_filename(
                "src=wallhaven__id=abc__size=1x1__slug=UPPER.jpg"
            )
        )


class MetadataValidationTests(unittest.TestCase):
    def test_valid_contract(self) -> None:
        self.assertEqual(metadata.validate_metadata(_valid_document()), [])

    def test_nullable_source_url(self) -> None:
        document = _valid_document()
        document["source_url"] = None
        self.assertEqual(metadata.validate_metadata(document), [])

    def test_missing_and_extra_fields_are_rejected(self) -> None:
        document = _valid_document()
        del document["search_origins"]
        document["extra"] = True
        errors = metadata.validate_metadata(document)
        self.assertTrue(any("search_origins is required" in error for error in errors))
        self.assertTrue(any("extra is not allowed" in error for error in errors))

    def test_only_canonical_source_enum_is_accepted(self) -> None:
        document = _valid_document()
        document["source"] = "anime_pictures"
        self.assertTrue(metadata.validate_metadata(document))

    def test_wrong_json_types_return_errors_instead_of_raising(self) -> None:
        document = _valid_document()
        document["schema_version"] = True
        document["source"] = []
        document["classification"]["resolution_bucket"] = []
        document["classification"]["orientation"] = {}
        errors = metadata.validate_metadata(document)
        self.assertTrue(any("schema_version" in error for error in errors))
        self.assertTrue(any(error.startswith("source must") for error in errors))
        self.assertTrue(any("resolution_bucket" in error for error in errors))
        self.assertTrue(any("orientation" in error for error in errors))

    def test_canonical_fields_must_match_document(self) -> None:
        document = _valid_document()
        document["source_id"] = "different"
        errors = metadata.validate_metadata(document)
        self.assertIn("canonical_filename id does not match source_id", errors)

    def test_recorded_at_requires_datetime_and_timezone(self) -> None:
        for invalid in ("2026-07-15", "2026-07-15T12:30:00"):
            document = _valid_document()
            document["download"]["recorded_at"] = invalid
            self.assertTrue(
                any(
                    "RFC3339" in error
                    for error in metadata.validate_metadata(document)
                )
            )

    def test_total_unknown_dimensions_are_valid(self) -> None:
        document = _valid_document(
            "src=anime-pictures__id=921265__size=0x0"
            "__slug=azur-lane-evertsen.png"
        )
        document["file"]["width"] = 0
        document["file"]["height"] = 0
        document["classification"] = {
            "resolution_bucket": "_UnknownResolution",
            "orientation": "unknown",
        }
        self.assertEqual(metadata.validate_metadata(document), [])

    def test_partial_unknown_dimensions_are_rejected(self) -> None:
        document = _valid_document()
        document["file"]["width"] = 0
        self.assertTrue(
            any("both be zero" in error for error in metadata.validate_metadata(document))
        )

    def test_unknown_classification_with_known_dimensions_is_rejected(self) -> None:
        document = _valid_document()
        document["classification"] = {
            "resolution_bucket": "_UnknownResolution",
            "orientation": "unknown",
        }
        self.assertTrue(
            any("known dimensions" in error for error in metadata.validate_metadata(document))
        )

    def test_load_validates_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "image.wallpaper.json"
            path.write_text(json.dumps(_valid_document()), encoding="utf-8")
            loaded = metadata.load_metadata(path)
            self.assertEqual(loaded["schema_version"], 1)

            invalid = deepcopy(loaded)
            invalid["schema_version"] = 2
            path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaises(metadata.MetadataValidationError):
                metadata.load_metadata(path)

    def test_sidecar_name_uses_image_stem(self) -> None:
        image = Path("C:/library/4K/portrait/wallhaven/example.jpg")
        self.assertEqual(
            metadata.sidecar_path_for(image).name,
            "example.wallpaper.json",
        )

    def test_json_schema_couples_unknown_dimensions_and_classification(self) -> None:
        schema_path = Path(__file__).parents[1] / "schemas" / "wallpaper-metadata.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        self.assertEqual(len(schema["oneOf"]), 2)
        unknown_branch = schema["oneOf"][0]["properties"]
        self.assertEqual(
            unknown_branch["classification"]["properties"]["resolution_bucket"]["const"],
            "_UnknownResolution",
        )


if __name__ == "__main__":
    unittest.main()
