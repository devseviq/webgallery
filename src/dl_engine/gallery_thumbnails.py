"""Bounded, disposable thumbnail generation for the local gallery.

The indexed SHA-256 is the cache identity.  Callers are responsible for
resolving that identity through the read-only index and proving that the
source is contained by the configured canonical library root.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import threading
from uuid import uuid4
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError


_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_SUPPORTED_SOURCE_FORMATS = frozenset(
    {"AVIF", "BMP", "GIF", "JPEG", "MPO", "PNG", "TIFF", "WEBP"}
)


class ThumbnailError(RuntimeError):
    """A thumbnail could not be safely read or generated."""


class ThumbnailValidationError(ThumbnailError):
    """A thumbnail input or transform specification is invalid."""


@dataclass(frozen=True)
class ThumbnailSpec:
    """Versioned, bounded thumbnail transform parameters."""

    max_width: int = 640
    max_height: int = 640
    max_source_pixels: int = 120_000_000
    format: str = "WEBP"
    quality: int = 82
    version: int = 1

    def validate(self) -> None:
        if self.max_width < 1 or self.max_height < 1:
            raise ThumbnailValidationError("thumbnail dimensions must be positive")
        if self.max_source_pixels < 1:
            raise ThumbnailValidationError("source pixel limit must be positive")
        if self.format.upper() != "WEBP":
            raise ThumbnailValidationError("only WEBP thumbnail output is supported")
        if not 1 <= self.quality <= 100:
            raise ThumbnailValidationError("thumbnail quality must be between 1 and 100")
        if self.version < 1:
            raise ThumbnailValidationError("thumbnail transform version must be positive")


DEFAULT_THUMBNAIL_SPEC = ThumbnailSpec()
MAX_CONCURRENT_GENERATIONS = 2

_locks_guard = threading.Lock()
_key_locks: dict[str, threading.Lock] = {}
_generation_slots = threading.BoundedSemaphore(MAX_CONCURRENT_GENERATIONS)


def _normalise_sha256(sha256: str) -> str:
    value = str(sha256).strip()
    if not _SHA256_RE.fullmatch(value):
        raise ThumbnailValidationError("sha256 must be exactly 64 hexadecimal characters")
    return value.casefold()


def thumbnail_cache_path(
    cache_root: Path,
    sha256: str,
    spec: ThumbnailSpec = DEFAULT_THUMBNAIL_SPEC,
) -> Path:
    """Return the deterministic path for an indexed SHA and transform."""

    spec.validate()
    digest = _normalise_sha256(sha256)
    filename = (
        f"{digest}-{spec.max_width}x{spec.max_height}-q{spec.quality}.webp"
    )
    return Path(cache_root) / f"v{spec.version}" / digest[:2] / filename


def _lock_for(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path.absolute()))
    with _locks_guard:
        return _key_locks.setdefault(key, threading.Lock())


def _valid_cache_hit(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink() and path.stat().st_size > 0
    except OSError:
        return False


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in {"LA", "PA", "RGBA"} or (
        image.mode == "P" and "transparency" in image.info
    )


def _generate_thumbnail(
    source: Path,
    temporary: Path,
    spec: ThumbnailSpec,
) -> None:
    """Decode, bound, transform, and close one thumbnail temporary file."""

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(source) as opened:
            source_format = (opened.format or "").upper()
            if source_format not in _SUPPORTED_SOURCE_FORMATS:
                raise ThumbnailValidationError(
                    f"unsupported source image format: {source_format or 'unknown'}"
                )
            width, height = opened.size
            if width < 1 or height < 1:
                raise ThumbnailValidationError("source image dimensions are invalid")
            if width * height > spec.max_source_pixels:
                raise ThumbnailValidationError(
                    "source image exceeds the configured pixel limit"
                )
            opened.load()
            oriented = ImageOps.exif_transpose(opened)
            try:
                output_mode = "RGBA" if _has_alpha(oriented) else "RGB"
                output = oriented.convert(output_mode)
            finally:
                if oriented is not opened:
                    oriented.close()

    try:
        output.thumbnail(
            (spec.max_width, spec.max_height),
            Image.Resampling.LANCZOS,
        )
        if output.width < 1 or output.height < 1:
            raise ThumbnailValidationError("thumbnail output dimensions are invalid")
        with temporary.open("xb") as stream:
            output.save(
                stream,
                format=spec.format.upper(),
                quality=spec.quality,
                method=4,
            )
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        output.close()


def ensure_thumbnail(
    source_path: Path,
    cache_root: Path,
    sha256: str,
    spec: ThumbnailSpec = DEFAULT_THUMBNAIL_SPEC,
) -> Path:
    """Return a cache hit or atomically generate one bounded WEBP thumbnail.

    This function never hashes the source.  ``sha256`` is the already-indexed
    identity supplied by the server after its read-only database lookup.
    """

    source = Path(source_path)
    final_path = thumbnail_cache_path(cache_root, sha256, spec)
    if _valid_cache_hit(final_path):
        return final_path

    lock = _lock_for(final_path)
    with lock:
        if _valid_cache_hit(final_path):
            return final_path
        if not source.is_file() or source.is_symlink():
            raise ThumbnailValidationError("thumbnail source is not a regular file")

        try:
            final_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ThumbnailError("thumbnail cache directory is unavailable") from exc

        temporary = final_path.with_name(
            f".{final_path.name}.{uuid4().hex}.tmp"
        )
        try:
            with _generation_slots:
                _generate_thumbnail(source, temporary, spec)
            temporary.replace(final_path)
        except ThumbnailError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            UnidentifiedImageError,
            OSError,
            SyntaxError,
            ValueError,
        ) as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise ThumbnailError("thumbnail generation failed") from exc
        except Exception as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise ThumbnailError("thumbnail encoder failed") from exc

        if not _valid_cache_hit(final_path):
            raise ThumbnailError("thumbnail generation produced no cache artifact")
        return final_path
