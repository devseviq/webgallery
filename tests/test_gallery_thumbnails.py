from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from PIL import Image

from dl_engine import gallery_thumbnails
from dl_engine.gallery_thumbnails import (
    MAX_CONCURRENT_GENERATIONS,
    ThumbnailError,
    ThumbnailSpec,
    ThumbnailValidationError,
    ensure_thumbnail,
    thumbnail_cache_path,
)


SHA = "ab" + "1" * 62


class GalleryThumbnailTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cache = self.root / "cache"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def image(
        self,
        name: str,
        size: tuple[int, int],
        *,
        mode: str = "RGB",
        color: object = (30, 80, 140),
        format: str | None = None,
        exif: Image.Exif | None = None,
    ) -> Path:
        path = self.root / name
        with Image.new(mode, size, color) as fixture:
            save_options: dict[str, object] = {"format": format}
            if exif is not None:
                save_options["exif"] = exif
            fixture.save(path, **save_options)
        return path

    def test_stable_cache_identity_and_version_separation(self) -> None:
        expected = self.cache / "v1" / "ab" / f"{SHA}-640x640-q82.webp"
        self.assertEqual(thumbnail_cache_path(self.cache, SHA.upper()), expected)
        version_two = thumbnail_cache_path(
            self.cache, SHA, ThumbnailSpec(version=2)
        )
        self.assertNotEqual(version_two, expected)
        self.assertEqual(version_two.parts[-3], "v2")

    def test_landscape_portrait_transparent_and_no_upscaling(self) -> None:
        fixtures = [
            (self.image("landscape.jpg", (1200, 600)), "2" * 64, (640, 320), "RGB"),
            (self.image("portrait.png", (300, 900)), "3" * 64, (213, 640), "RGB"),
            (
                self.image(
                    "transparent.png", (120, 80), mode="RGBA", color=(1, 2, 3, 40)
                ),
                "4" * 64,
                (120, 80),
                "RGBA",
            ),
        ]
        for source, digest, expected_size, expected_mode in fixtures:
            with self.subTest(source=source.name):
                result = ensure_thumbnail(source, self.cache, digest)
                with Image.open(result) as thumb:
                    self.assertEqual(thumb.size, expected_size)
                    self.assertEqual(thumb.mode, expected_mode)
                    self.assertEqual(thumb.format, "WEBP")

    def test_exif_orientation_is_applied(self) -> None:
        exif = Image.Exif()
        exif[274] = 6
        source = self.image("rotated.jpg", (800, 400), exif=exif)
        result = ensure_thumbnail(source, self.cache, "5" * 64)
        with Image.open(result) as thumb:
            self.assertEqual(thumb.size, (320, 640))
            self.assertNotIn("exif", thumb.info)

    def test_pillow_decoded_mpo_source_generates_bounded_webp(self) -> None:
        exif = Image.Exif()
        exif[270] = "synthetic MPO metadata"
        source = self.image("synthetic-mpo.jpg", (80, 40), exif=exif)
        original_open = Image.open
        source_loaded = False

        def open_as_mpo(*args: object, **kwargs: object) -> Image.Image:
            nonlocal source_loaded
            opened = original_open(*args, **kwargs)
            opened.format = "MPO"
            original_load = opened.load

            def tracked_load() -> object:
                nonlocal source_loaded
                source_loaded = True
                return original_load()

            opened.load = tracked_load
            return opened

        with mock.patch.object(
            gallery_thumbnails.Image,
            "open",
            side_effect=open_as_mpo,
        ):
            result = ensure_thumbnail(source, self.cache, "e" * 64)

        self.assertTrue(source_loaded)
        with original_open(result) as thumb:
            self.assertEqual(thumb.size, (80, 40))
            self.assertEqual(thumb.format, "WEBP")
            self.assertNotIn("exif", thumb.info)

    def test_dimension_pixel_caps_and_invalid_spec(self) -> None:
        source = self.image("wide.png", (20, 10))
        with self.assertRaisesRegex(ThumbnailValidationError, "pixel limit"):
            ensure_thumbnail(
                source,
                self.cache,
                "6" * 64,
                ThumbnailSpec(max_source_pixels=199),
            )
        with self.assertRaisesRegex(ThumbnailValidationError, "dimensions"):
            thumbnail_cache_path(
                self.cache, "6" * 64, ThumbnailSpec(max_width=0)
            )

    def test_cache_hit_does_not_rewrite_mtime(self) -> None:
        source = self.image("cached.jpg", (1000, 500))
        result = ensure_thumbnail(source, self.cache, "7" * 64)
        old = time.time() - 100
        os.utime(result, (old, old))
        before = result.stat().st_mtime_ns
        self.assertEqual(ensure_thumbnail(source, self.cache, "7" * 64), result)
        self.assertEqual(result.stat().st_mtime_ns, before)

    def test_invalid_sha_corrupt_and_unsupported_images_fail_closed(self) -> None:
        source = self.image("source.png", (20, 20))
        for digest in ("", "a" * 63, "g" * 64, "../" + "a" * 64):
            with self.subTest(digest=digest):
                with self.assertRaises(ThumbnailValidationError):
                    thumbnail_cache_path(self.cache, digest)

        corrupt = self.root / "corrupt.jpg"
        corrupt.write_bytes(b"not an image")
        with self.assertRaises(ThumbnailError):
            ensure_thumbnail(corrupt, self.cache, "8" * 64)

        unsupported = self.image("unsupported.ico", (20, 20), format="ICO")
        with self.assertRaisesRegex(ThumbnailValidationError, "unsupported"):
            ensure_thumbnail(unsupported, self.cache, "9" * 64)

    def test_oversized_source_header_is_rejected_before_load(self) -> None:
        source = self.image("oversized.png", (101, 100))
        spec = ThumbnailSpec(max_source_pixels=10_000)
        with mock.patch.object(Image.Image, "load", side_effect=AssertionError("loaded")):
            with self.assertRaisesRegex(ThumbnailValidationError, "pixel limit"):
                ensure_thumbnail(source, self.cache, "a" * 64, spec)

    def test_pillow_decompression_warning_is_a_controlled_failure(self) -> None:
        source = self.image("warning.png", (20, 20))
        with mock.patch.object(Image, "MAX_IMAGE_PIXELS", 300):
            with self.assertRaises(ThumbnailError):
                ensure_thumbnail(source, self.cache, "b" * 64)

    def test_encoder_failure_cleans_only_its_temporary_file(self) -> None:
        source = self.image("encoder.png", (40, 40))
        shard = thumbnail_cache_path(self.cache, "c" * 64).parent
        shard.mkdir(parents=True)
        unrelated = shard / ".unrelated.tmp"
        unrelated.write_bytes(b"keep")
        with mock.patch.object(Image.Image, "save", side_effect=OSError("encoder")):
            with self.assertRaises(ThumbnailError):
                ensure_thumbnail(source, self.cache, "c" * 64)
        self.assertEqual(unrelated.read_bytes(), b"keep")
        self.assertEqual(
            [path for path in shard.iterdir() if path != unrelated],
            [],
        )

    def test_interruption_re_raises_and_cleans_only_its_temporary_file(self) -> None:
        source = self.image("interrupted.png", (40, 40))
        cases = (
            (KeyboardInterrupt("cancelled"), "e" * 64),
            (SystemExit(17), "f" * 64),
        )
        for interruption, digest in cases:
            with self.subTest(interruption=type(interruption).__name__):
                final_path = thumbnail_cache_path(self.cache, digest)
                final_path.parent.mkdir(parents=True, exist_ok=True)
                unrelated = final_path.parent / ".unrelated.tmp"
                unrelated.write_bytes(b"keep")
                temporary_paths: list[Path] = []

                def interrupt_after_temporary_exists(
                    _source: Path,
                    temporary: Path,
                    _spec: ThumbnailSpec,
                ) -> None:
                    temporary.write_bytes(b"partial")
                    temporary_paths.append(temporary)
                    raise interruption

                with mock.patch.object(
                    gallery_thumbnails,
                    "_generate_thumbnail",
                    side_effect=interrupt_after_temporary_exists,
                ):
                    with self.assertRaises(type(interruption)) as raised:
                        ensure_thumbnail(source, self.cache, digest)

                self.assertIs(raised.exception, interruption)
                self.assertEqual(len(temporary_paths), 1)
                self.assertFalse(temporary_paths[0].exists())
                self.assertEqual(unrelated.read_bytes(), b"keep")
                self.assertFalse(final_path.exists())

    def test_ordinary_generation_failures_keep_diagnostic_wrapping(self) -> None:
        source = self.image("ordinary-failure.png", (40, 40))
        cases = (
            (OSError("decode"), "thumbnail generation failed", "1" * 64),
            (RuntimeError("encoder"), "thumbnail encoder failed", "2" * 64),
        )
        for failure, message, digest in cases:
            with self.subTest(failure=type(failure).__name__):
                final_path = thumbnail_cache_path(self.cache, digest)
                with mock.patch.object(
                    gallery_thumbnails,
                    "_generate_thumbnail",
                    side_effect=failure,
                ):
                    with self.assertRaisesRegex(ThumbnailError, message) as raised:
                        ensure_thumbnail(source, self.cache, digest)

                self.assertIs(raised.exception.__cause__, failure)
                self.assertFalse(final_path.exists())
                self.assertEqual(list(final_path.parent.glob("*.tmp")), [])

    def test_concurrent_calls_publish_one_stable_artifact(self) -> None:
        source = self.image("concurrent.jpg", (1600, 900))

        def generate(_index: int) -> Path:
            return ensure_thumbnail(source, self.cache, "d" * 64)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(generate, range(24)))
        self.assertEqual(len(set(results)), 1)
        self.assertGreater(results[0].stat().st_size, 0)
        self.assertEqual(list(results[0].parent.glob("*.tmp")), [])

    def test_different_keys_share_a_small_global_generation_bound(self) -> None:
        source = self.image("bounded.png", (320, 180))
        original = gallery_thumbnails._generate_thumbnail
        state_lock = threading.Lock()
        two_entered = threading.Event()
        release = threading.Event()
        active = 0
        peak = 0

        def observed(*args: object, **kwargs: object) -> None:
            nonlocal active, peak
            with state_lock:
                active += 1
                peak = max(peak, active)
                if active == MAX_CONCURRENT_GENERATIONS:
                    two_entered.set()
            try:
                self.assertTrue(release.wait(2), "generation test release timed out")
                original(*args, **kwargs)
            finally:
                with state_lock:
                    active -= 1

        with mock.patch.object(
            gallery_thumbnails, "_generate_thumbnail", side_effect=observed
        ):
            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = [
                    pool.submit(
                        ensure_thumbnail,
                        source,
                        self.cache,
                        f"{index:064x}",
                    )
                    for index in range(1, 7)
                ]
                self.assertTrue(two_entered.wait(2), "generation slots were not filled")
                time.sleep(0.05)
                with state_lock:
                    self.assertEqual(peak, MAX_CONCURRENT_GENERATIONS)
                release.set()
                results = [future.result(timeout=3) for future in futures]

        self.assertEqual(len(set(results)), 6)


if __name__ == "__main__":
    unittest.main()
