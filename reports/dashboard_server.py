#!/usr/bin/env python3
"""Allowlisted loopback HTTP server for the wallpaper gallery.

The repository is application code, not a runtime collection.  Every runtime
root is supplied explicitly through :class:`ServerConfig` or command-line
arguments.  There is deliberately no generic filesystem route.
"""

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import http.server
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, BinaryIO, Callable, Mapping
import urllib.parse


APP_ROOT = Path(__file__).resolve().parents[1]
APP_REPORTS_ROOT = APP_ROOT / "reports"
APP_SRC_ROOT = APP_ROOT / "src"
if str(APP_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_SRC_ROOT))

from dl_engine import index_library, library_browser, library_transfer  # noqa: E402
from dl_engine.gallery_thumbnails import (  # noqa: E402
    ThumbnailError,
    ensure_thumbnail,
)


_SHA_ROUTE_RE = re.compile(r"/thumb/([0-9A-Fa-f]{64})\.webp\Z")
_ORIGINAL_ROUTE_RE = re.compile(r"/original/([1-9][0-9]*)\Z")
_TRANSFER_JOB_RE = re.compile(
    r"/api/library/transfers/([A-Za-z0-9][A-Za-z0-9._-]{0,127})\Z"
)
_SUGGESTION_RE = re.compile(r"/api/library/suggestions/([1-9][0-9]*)\Z")
_SAFE_HANDLER_RE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_VALID_PERCENT_RE = re.compile(r"%(?:[0-9A-Fa-f]{2})")
_IMAGE_MIME_TYPES = {
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".jfif": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}
_GENERATED_REPORTS = frozenset(
    {"download-queue-dashboard.html", "dashboard.html"}
)
_SOURCE_REPORTS = frozenset({"library-browser.html"})
_READ_ONLY_PATHS = frozenset(
    {
        "/api/operations/status",
        "/api/library",
        "/api/library/status",
        "/api/library/facets",
        "/api/library/tags",
        "/api/library/transfers",
        "/_pause_status",
        "/_rebuild_status",
    }
)
_MUTATION_PATHS = frozenset(
    {"/_pause", "/_resume", "/_rebuild", "/api/library/transfers"}
)
_POST_ONLY_PATHS = frozenset({"/_pause", "/_resume", "/_rebuild"})
_SECURITY_CSP = (
    "default-src 'self'; base-uri 'none'; object-src 'none'; "
    "frame-ancestors 'none'; form-action 'self'; img-src 'self' data:; "
    "connect-src 'self'; style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'"
)
_STREAM_CHUNK_BYTES = 64 * 1024
_MAX_QUEUE_STATE_BYTES = 32 * 1024 * 1024


class RuntimeUnavailable(RuntimeError):
    """A configured runtime dependency could not be safely used."""


class DeniedPath(ValueError):
    """A route-relative path is structurally unsafe."""


@dataclass(frozen=True)
class ServerConfig:
    """Explicit application and runtime paths for one gallery server."""

    collection_root: Path
    library_root: Path
    database_path: Path
    queue_state_path: Path
    report_output_root: Path
    thumbnail_cache_root: Path
    preview_root: Path
    pause_flag_path: Path
    transfer_config_path: Path
    environment_path: Path
    app_reports_root: Path = APP_REPORTS_ROOT

    def normalized(self) -> "ServerConfig":
        """Return absolute paths without requiring live roots to exist."""

        values = {
            name: Path(getattr(self, name)).expanduser().absolute()
            for name in self.__dataclass_fields__
        }
        return type(self)(**values)


@dataclass
class RebuildState:
    at: str | None = None
    ok: bool | None = None
    duration_s: float | None = None
    running: bool = False

    def public_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "ok": self.ok,
            "duration_s": self.duration_s,
            "running": self.running,
        }


class GalleryHTTPServer(http.server.ThreadingHTTPServer):
    """Threaded server carrying immutable route configuration."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        config: ServerConfig,
        *,
        transfer_manager: Any | None = None,
        thumbnail_generator: Callable[[Path, Path, str], Path] = ensure_thumbnail,
    ) -> None:
        host = str(server_address[0]).strip()
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host.casefold() == "localhost"
        if not is_loopback:
            raise ValueError("gallery server must bind to a loopback address")
        self.config = config.normalized()
        self.transfer_manager = transfer_manager or library_transfer.ScpTransferManager(
            self.config.database_path,
            self.config.library_root,
            self.config.transfer_config_path,
        )
        self.thumbnail_generator = thumbnail_generator
        self.rebuild_state = RebuildState()
        self.rebuild_lock = threading.Lock()
        self.rebuild_state_lock = threading.Lock()
        self.facet_cache_lock = threading.Lock()
        self.facet_cache: dict[str, Any] = {"key": None, "value": None}
        super().__init__(server_address, Handler)


def create_server(
    server_address: tuple[str, int],
    config: ServerConfig,
    *,
    transfer_manager: Any | None = None,
    thumbnail_generator: Callable[[Path, Path, str], Path] = ensure_thumbnail,
) -> GalleryHTTPServer:
    """Create, but do not start, an isolated gallery server."""

    return GalleryHTTPServer(
        server_address,
        config,
        transfer_manager=transfer_manager,
        thumbnail_generator=thumbnail_generator,
    )


def _utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _public_timestamp(value: object) -> str | None:
    parsed = _utc_timestamp(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed else None


def _safe_last_message(value: object) -> str:
    if not isinstance(value, str):
        return ""
    message = " ".join(value.replace("\x00", "").split())
    message = re.sub(r"(?i)https?://\S+", "[link]", message)
    message = re.sub(r"(?i)www\.\S+", "[link]", message)
    message = re.sub(r"(?i)(?:[A-Za-z]:[\\/]|\\\\)[^\s]+", "[path]", message)
    message = re.sub(
        r"(?i)(api[-_]?key|token|password|secret|cookie|authorization)"
        r"\s*[:=]\s*\S+",
        r"\1=[redacted]",
        message,
    )
    return message[:80]


def _sqlite_cache_key(database_path: Path) -> tuple[tuple[int, int] | None, ...]:
    """Fingerprint the database plus WAL state used by read-only API caches."""

    fingerprints: list[tuple[int, int] | None] = []
    for path in (
        database_path,
        Path(str(database_path) + "-wal"),
        Path(str(database_path) + "-shm"),
    ):
        try:
            stat = path.stat()
        except FileNotFoundError:
            fingerprints.append(None)
        except OSError as exc:
            raise RuntimeUnavailable("library index is unavailable") from exc
        else:
            fingerprints.append((stat.st_mtime_ns, stat.st_size))
    if fingerprints[0] is None:
        raise RuntimeUnavailable("library index is unavailable")
    return tuple(fingerprints)


def _operations_status(config: ServerConfig) -> dict[str, Any]:
    path = config.queue_state_path
    try:
        size = path.stat().st_size
        if size > _MAX_QUEUE_STATE_BYTES:
            raise RuntimeUnavailable("queue state exceeds the read limit")
        raw = path.read_text(encoding="utf-8")
        state = json.loads(raw)
    except RuntimeUnavailable:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeUnavailable("queue state is unavailable") from exc
    if not isinstance(state, dict) or not isinstance(state.get("jobs", []), list):
        raise RuntimeUnavailable("queue state has an invalid shape")

    jobs = [job for job in state.get("jobs", []) if isinstance(job, dict)]
    statuses = Counter(str(job.get("status") or "unknown") for job in jobs)
    failed_handlers: Counter[str] = Counter()
    completed_today = 0
    today = datetime.now(timezone.utc).date()
    for job in jobs:
        if job.get("status") == "failed":
            handler = str(job.get("handler") or "(none)")
            failed_handlers[
                handler if _SAFE_HANDLER_RE.fullmatch(handler) else "(other)"
            ] += 1
        if job.get("status") == "completed":
            finished_at = _utc_timestamp(job.get("finishedAt"))
            if finished_at is not None and finished_at.date() == today:
                completed_today += 1

    counts = {
        "total": len(jobs),
        "completed": statuses.get("completed", 0),
        "failed": statuses.get("failed", 0),
        "pending": statuses.get("pending", 0),
        "running": statuses.get("running", 0),
        "removed": statuses.get("removed", 0),
    }
    return {
        "schema_version": 1,
        "counts": counts,
        "failed_by_handler": dict(sorted(failed_handlers.items())),
        "completed_today_utc": completed_today,
        "updatedAt": _public_timestamp(state.get("updatedAt")),
        "lastWorkerAt": _public_timestamp(state.get("lastWorkerAt")),
        "lastMessage": _safe_last_message(state.get("lastMessage")),
        "paused": config.pause_flag_path.is_file(),
    }


def _public_verification(value: object) -> object:
    if not isinstance(value, dict):
        return value
    cleaned = dict(value)
    report = cleaned.get("last_report")
    if report:
        cleaned["last_report"] = Path(str(report)).name
    cleaned.pop("path", None)
    cleaned.pop("db_path", None)
    cleaned.pop("database_path", None)
    for key in ("reason", "error"):
        if key in cleaned:
            cleaned[key] = _safe_last_message(cleaned[key])
    return cleaned


def _sanitize_library_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Remove filesystem identities and issue only ID/SHA media URLs."""

    cleaned = dict(payload)
    index_payload = cleaned.get("index")
    if isinstance(index_payload, Mapping):
        safe_index = dict(index_payload)
        safe_index.pop("path", None)
        safe_index.pop("db_path", None)
        safe_index.pop("database_path", None)
        cleaned["index"] = safe_index
    cleaned["verification"] = _public_verification(cleaned.get("verification"))

    safe_items: list[Any] = []
    for raw_item in cleaned.get("items", []):
        if not isinstance(raw_item, Mapping):
            continue
        item = dict(raw_item)
        item.pop("path", None)
        image_id = item.get("id")
        media_allowed = bool(item.get("exists")) and bool(
            item.get("original_url") or item.get("url")
        )
        original_url = (
            f"/original/{image_id}"
            if isinstance(image_id, int) and not isinstance(image_id, bool) and image_id > 0
            and media_allowed
            else None
        )
        sha256 = item.get("sha256")
        thumbnail_url = (
            f"/thumb/{sha256.casefold()}.webp"
            if isinstance(sha256, str)
            and re.fullmatch(r"[0-9A-Fa-f]{64}", sha256)
            and media_allowed
            else None
        )
        item["original_url"] = original_url
        item["thumbnail_url"] = thumbnail_url
        item["url"] = original_url
        safe_items.append(item)
    cleaned["items"] = safe_items
    return cleaned


def _strict_relative_parts(encoded: str) -> tuple[str, ...]:
    """Decode one URL path tail and reject Windows/path traversal forms."""

    if not encoded or len(encoded) > 2048:
        raise DeniedPath("relative path is empty or too long")
    if re.search(r"(?i)%(?:2f|5c)", encoded):
        raise DeniedPath("encoded path separators are not allowed")
    malformed = re.sub(_VALID_PERCENT_RE, "", encoded)
    if "%" in malformed:
        raise DeniedPath("malformed percent escape")
    try:
        decoded = urllib.parse.unquote_to_bytes(encoded).decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise DeniedPath("path is not valid UTF-8") from exc
    if (
        "\x00" in decoded
        or "\\" in decoded
        or ":" in decoded
        or "%" in decoded
        or decoded.startswith("/")
        or decoded.startswith("//")
    ):
        raise DeniedPath("unsafe path syntax")
    parts = tuple(decoded.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise DeniedPath("unsafe path segment")
    return parts


def _contained_file(root: Path, parts: tuple[str, ...]) -> Path:
    try:
        resolved_root = root.resolve(strict=True)
        candidate = resolved_root.joinpath(*parts).resolve(strict=True)
        candidate.relative_to(resolved_root)
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:
        raise RuntimeUnavailable("configured media is unavailable") from exc
    except ValueError as exc:
        raise DeniedPath("media path escapes its configured root") from exc
    if not candidate.is_file():
        raise RuntimeUnavailable("configured media is unavailable")
    return candidate


def _mime_for_image(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix not in index_library.IMAGE_EXTENSIONS:
        raise DeniedPath("unsupported image extension")
    mime = _IMAGE_MIME_TYPES.get(suffix)
    if mime is None:
        guessed, _encoding = mimetypes.guess_type(path.name)
        mime = guessed if guessed and guessed.startswith("image/") else None
    if mime is None:
        raise DeniedPath("unsupported image type")
    return mime


def _indexed_image_by_id(config: ServerConfig, image_id: int) -> tuple[Path, str]:
    try:
        conn = index_library.open_index_read_only(config.database_path)
        try:
            rows = conn.execute(
                "SELECT path, ext FROM images WHERE id = ? LIMIT 2", (image_id,)
            ).fetchall()
        finally:
            conn.close()
    except (FileNotFoundError, OSError, sqlite3.Error) as exc:
        raise RuntimeUnavailable("library index is unavailable") from exc
    if not rows:
        raise FileNotFoundError("image id is not indexed")
    if len(rows) != 1:
        raise RuntimeUnavailable("image identity is ambiguous")
    return Path(rows[0]["path"]), str(rows[0]["ext"] or "")


def _indexed_image_by_sha(config: ServerConfig, sha256: str) -> tuple[Path, str]:
    try:
        conn = index_library.open_index_read_only(config.database_path)
        try:
            rows = conn.execute(
                "SELECT path, ext FROM images WHERE lower(sha256) = ? LIMIT 2",
                (sha256.casefold(),),
            ).fetchall()
        finally:
            conn.close()
    except (FileNotFoundError, OSError, sqlite3.Error) as exc:
        raise RuntimeUnavailable("library index is unavailable") from exc
    if not rows:
        raise FileNotFoundError("image SHA is not indexed")
    if len(rows) != 1:
        raise RuntimeUnavailable("image SHA identity is ambiguous")
    return Path(rows[0]["path"]), str(rows[0]["ext"] or "")


def _canonical_file(config: ServerConfig, indexed_path: Path, indexed_ext: str) -> Path:
    if not indexed_path.is_absolute():
        raise RuntimeUnavailable("indexed image path is not absolute")
    try:
        root = config.library_root.resolve(strict=True)
        resolved = indexed_path.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:
        raise RuntimeUnavailable("indexed image is unavailable") from exc
    except ValueError as exc:
        raise RuntimeUnavailable("indexed image is outside the library") from exc
    if not resolved.is_file():
        raise RuntimeUnavailable("indexed image is unavailable")
    suffix = resolved.suffix.casefold()
    if indexed_ext and str(indexed_ext).casefold().lstrip(".") != suffix.lstrip("."):
        raise RuntimeUnavailable("indexed image extension does not match")
    _mime_for_image(resolved)
    return resolved


class Handler(http.server.BaseHTTPRequestHandler):
    """Method-aware handler whose only filesystem reads are explicit routes."""

    server: GalleryHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write(
            "[%s] %s\n" % (self.log_date_time_string(), fmt % args)
        )

    def end_headers(self) -> None:
        self.send_header("Content-Security-Policy", _SECURITY_CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        del explain
        public = message or http.server.BaseHTTPRequestHandler.responses.get(
            code, ("Request failed", "")
        )[0]
        self.send_json(
            {"ok": False, "error": public},
            status=code,
            head_only=self.command == "HEAD",
        )

    def send_json(
        self,
        payload: Mapping[str, Any],
        *,
        status: int = 200,
        head_only: bool = False,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _redirect(self, location: str, *, head_only: bool = False) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _method_not_allowed(self, allow: str) -> None:
        self.send_json(
            {"ok": False, "error": "method not allowed"},
            status=405,
            head_only=self.command == "HEAD",
            extra_headers={"Allow": allow},
        )

    def _parsed_target(self) -> tuple[str, str]:
        if len(self.path) > 4096 or "\x00" in self.path:
            raise ValueError("request target is invalid")
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
            raise ValueError("request target is invalid")
        return parsed.path, parsed.query

    def _same_origin_request(self) -> bool:
        if self.headers.get("Sec-Fetch-Site", "").casefold() == "cross-site":
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        if not host:
            return False
        expected = {f"http://{host}", f"https://{host}"}
        return origin.rstrip("/") in expected

    def _read_json_body(self, max_bytes: int = 64 * 1024) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            raise TypeError("Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise ValueError("Content-Length must be an integer") from exc
        if length <= 0:
            raise ValueError("request body is empty")
        if length > max_bytes:
            raise OverflowError(f"request body exceeds {max_bytes} bytes")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def _accept_empty_control_body(self) -> bool:
        """Consume the optional ``{}`` body used by dashboard control fetches."""

        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self.send_json(
                {"ok": False, "error": "chunked control bodies are not supported"},
                status=400,
            )
            return False
        length_text = self.headers.get("Content-Length")
        if length_text in {None, "", "0"}:
            return True
        try:
            length = int(length_text)
        except ValueError:
            self.close_connection = True
            self.send_json(
                {"ok": False, "error": "Content-Length must be an integer"},
                status=400,
            )
            return False
        if length < 0 or length > 1024:
            self.close_connection = True
            self.send_json(
                {"ok": False, "error": "control request body is too large"},
                status=413,
            )
            return False
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            self.close_connection = True
            self.send_json(
                {"ok": False, "error": "Content-Type must be application/json"},
                status=415,
            )
            return False
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(
                {"ok": False, "error": "control body must be valid UTF-8 JSON"},
                status=400,
            )
            return False
        if payload != {}:
            self.send_json(
                {"ok": False, "error": "control body must be an empty JSON object"},
                status=400,
            )
            return False
        return True

    def _library_query_params(self, query_string: str) -> dict[str, str]:
        query = urllib.parse.parse_qs(
            query_string,
            keep_blank_values=True,
            max_num_fields=24,
            strict_parsing=False,
        )
        allowed = {
            "rating",
            "orientation",
            "franchise",
            "bucket",
            "source",
            "tag",
            "sort",
            "limit",
            "offset",
            "shuffle_seed",
        }
        unknown = sorted(set(query) - allowed)
        if unknown:
            raise ValueError("unsupported query field(s): " + ", ".join(unknown))
        if any(len(values) != 1 for values in query.values()):
            raise ValueError("query fields must not be repeated")
        return {key: values[0] for key, values in query.items()}

    def _library_facets(self) -> dict[str, Any]:
        key = _sqlite_cache_key(self.server.config.database_path)
        with self.server.facet_cache_lock:
            if self.server.facet_cache["key"] != key:
                self.server.facet_cache["value"] = library_browser.library_facets(
                    self.server.config.database_path
                )
                self.server.facet_cache["key"] = key
            return dict(self.server.facet_cache["value"])

    def _send_open_file(
        self,
        handle: BinaryIO,
        *,
        content_type: str,
        cache_control: str,
        head_only: bool,
    ) -> None:
        stat = os.fstat(handle.fileno())
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(stat.st_size))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        if head_only:
            return
        while True:
            chunk = handle.read(_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            self.wfile.write(chunk)

    def _send_static_report(self, name: str, *, head_only: bool) -> None:
        root = (
            self.server.config.report_output_root
            if name in _GENERATED_REPORTS
            else self.server.config.app_reports_root
        )
        try:
            resolved_root = root.resolve(strict=True)
            file_path = (resolved_root / name).resolve(strict=True)
            file_path.relative_to(resolved_root)
            if not file_path.is_file():
                raise FileNotFoundError(name)
            with file_path.open("rb") as handle:
                self._send_open_file(
                    handle,
                    content_type="text/html; charset=utf-8",
                    cache_control="no-cache",
                    head_only=head_only,
                )
        except (FileNotFoundError, NotADirectoryError, OSError, ValueError):
            self.send_error(404, "not found")

    def _send_preview_media(
        self, encoded_tail: str, *, library: bool, head_only: bool
    ) -> None:
        try:
            parts = _strict_relative_parts(encoded_tail)
            root = self.server.config.library_root if library else self.server.config.preview_root
            path = _contained_file(root, parts)
            mime = _mime_for_image(path)
            with path.open("rb") as handle:
                self._send_open_file(
                    handle,
                    content_type=mime,
                    cache_control="private, max-age=300",
                    head_only=head_only,
                )
        except DeniedPath:
            self.send_error(404, "not found")
        except RuntimeUnavailable:
            self.send_error(503, "media is unavailable")
        except OSError:
            self.send_error(503, "media is unavailable")

    def _send_original(self, image_id: int, *, head_only: bool) -> None:
        try:
            indexed_path, indexed_ext = _indexed_image_by_id(
                self.server.config, image_id
            )
            path = _canonical_file(self.server.config, indexed_path, indexed_ext)
            mime = _mime_for_image(path)
            # Keep this exact handle open through the stream; never reopen by a
            # request-controlled value after containment has been proven.
            with path.open("rb") as handle:
                self._send_open_file(
                    handle,
                    content_type=mime,
                    cache_control="private, no-cache",
                    head_only=head_only,
                )
        except FileNotFoundError:
            self.send_error(404, "image not found")
        except (RuntimeUnavailable, DeniedPath, OSError):
            self.send_error(503, "image is unavailable")

    def _send_thumbnail(self, sha256: str, *, head_only: bool) -> None:
        try:
            indexed_path, indexed_ext = _indexed_image_by_sha(
                self.server.config, sha256
            )
            source = _canonical_file(
                self.server.config, indexed_path, indexed_ext
            )
            thumbnail = Path(
                self.server.thumbnail_generator(
                    source,
                    self.server.config.thumbnail_cache_root,
                    sha256.casefold(),
                )
            )
            cache_root = self.server.config.thumbnail_cache_root.resolve(strict=True)
            resolved = thumbnail.resolve(strict=True)
            resolved.relative_to(cache_root)
            if not resolved.is_file() or resolved.suffix.casefold() != ".webp":
                raise RuntimeUnavailable("thumbnail artifact is invalid")
            with resolved.open("rb") as handle:
                self._send_open_file(
                    handle,
                    content_type="image/webp",
                    cache_control="public, max-age=31536000, immutable",
                    head_only=head_only,
                )
        except FileNotFoundError:
            self.send_error(404, "thumbnail not found")
        except (
            RuntimeUnavailable,
            DeniedPath,
            ThumbnailError,
            OSError,
            ValueError,
        ):
            self.send_error(503, "thumbnail is unavailable")

    def _send_library_api(self, query: str, *, head_only: bool) -> None:
        try:
            payload = library_browser.query_library_page(
                self.server.config.database_path,
                self.server.config.library_root,
                self._library_query_params(query),
            )
            payload["verification"] = library_browser.latest_verification_status(
                self.server.config.report_output_root,
                self.server.config.database_path,
            )
            public = _sanitize_library_payload(payload)
        except ValueError as exc:
            self.send_json(
                {"ok": False, "error": str(exc)}, status=400, head_only=head_only
            )
            return
        except (FileNotFoundError, OSError, sqlite3.Error):
            self.send_json(
                {"ok": False, "error": "library index is unavailable"},
                status=503,
                head_only=head_only,
            )
            return
        self.send_json({"ok": True, **public}, head_only=head_only)

    def _send_tags_api(self, query: str, *, head_only: bool) -> None:
        helper = getattr(library_browser, "tag_autocomplete", None)
        if not callable(helper):
            self.send_json(
                {"ok": False, "error": "tag autocomplete is unavailable"},
                status=503,
                head_only=head_only,
            )
            return
        try:
            values = urllib.parse.parse_qs(
                query, keep_blank_values=True, max_num_fields=2
            )
            if set(values) - {"prefix", "limit"} or any(
                len(items) != 1 for items in values.values()
            ):
                raise ValueError("expected one prefix and optional limit")
            prefix = values.get("prefix", [""])[0]
            limit_text = values.get("limit", ["20"])[0]
            limit = int(limit_text)
            payload = helper(self.server.config.database_path, prefix, limit)
        except (TypeError, ValueError) as exc:
            self.send_json(
                {"ok": False, "error": str(exc)}, status=400, head_only=head_only
            )
            return
        except (FileNotFoundError, OSError, sqlite3.Error):
            self.send_json(
                {"ok": False, "error": "tag autocomplete is unavailable"},
                status=503,
                head_only=head_only,
            )
            return
        body = payload if isinstance(payload, Mapping) else {"items": payload}
        self.send_json({"ok": True, **body}, head_only=head_only)

    def _serve_read(self, *, head_only: bool) -> None:
        try:
            path, query = self._parsed_target()
        except ValueError:
            self.send_error(400, "invalid request target")
            return

        if path in _POST_ONLY_PATHS or _SUGGESTION_RE.fullmatch(path):
            self._method_not_allowed("POST")
            return
        if path == "/" and not query:
            self._redirect("/reports/download-queue-dashboard.html", head_only=head_only)
            return
        if path in {"/library", "/library/"} and not query:
            self._redirect("/reports/library-browser.html", head_only=head_only)
            return
        if path in {"/reports", "/reports/"} and not query:
            self._redirect("/reports/dashboard.html", head_only=head_only)
            return
        if path.startswith("/reports/"):
            name = path.removeprefix("/reports/")
            if not query and name in _GENERATED_REPORTS | _SOURCE_REPORTS:
                self._send_static_report(name, head_only=head_only)
            else:
                self.send_error(404, "not found")
            return
        if path == "/api/operations/status":
            if query:
                self.send_error(400, "query parameters are not supported")
                return
            try:
                status = _operations_status(self.server.config)
            except RuntimeUnavailable:
                self.send_json(
                    {"ok": False, "error": "operations status is unavailable"},
                    status=503,
                    head_only=head_only,
                )
                return
            self.send_json({"ok": True, **status}, head_only=head_only)
            return
        if path == "/api/library":
            self._send_library_api(query, head_only=head_only)
            return
        if path == "/api/library/status":
            if query:
                self.send_error(400, "query parameters are not supported")
                return
            try:
                status = library_browser.latest_verification_status(
                    self.server.config.report_output_root,
                    self.server.config.database_path,
                )
                status["facets"] = self._library_facets()
            except (FileNotFoundError, OSError, sqlite3.Error, ValueError):
                self.send_json(
                    {"ok": False, "error": "library status is unavailable"},
                    status=503,
                    head_only=head_only,
                )
                return
            self.send_json(
                {"ok": True, **_public_verification(status)}, head_only=head_only
            )
            return
        if path == "/api/library/facets":
            if query:
                self.send_error(400, "query parameters are not supported")
                return
            try:
                facets = self._library_facets()
            except (FileNotFoundError, OSError, sqlite3.Error, ValueError, RuntimeUnavailable):
                self.send_json(
                    {"ok": False, "error": "library facets are unavailable"},
                    status=503,
                    head_only=head_only,
                )
                return
            self.send_json({"ok": True, **facets}, head_only=head_only)
            return
        if path == "/api/library/tags":
            self._send_tags_api(query, head_only=head_only)
            return
        if path == "/api/library/transfers":
            if query:
                self.send_error(400, "query parameters are not supported")
                return
            try:
                status = self.server.transfer_manager.service_status()
            except (library_transfer.TransferConfigurationError, OSError):
                self.send_json(
                    {"ok": False, "error": "transfer service is unavailable"},
                    status=503,
                    head_only=head_only,
                )
                return
            self.send_json({"ok": True, **status}, head_only=head_only)
            return
        transfer_match = _TRANSFER_JOB_RE.fullmatch(path)
        if transfer_match:
            if query:
                self.send_error(400, "query parameters are not supported")
                return
            try:
                job = self.server.transfer_manager.get_job(transfer_match.group(1))
            except (library_transfer.TransferConfigurationError, OSError):
                self.send_json(
                    {"ok": False, "error": "transfer service is unavailable"},
                    status=503,
                    head_only=head_only,
                )
                return
            if job is None:
                self.send_json(
                    {"ok": False, "error": "transfer job not found"},
                    status=404,
                    head_only=head_only,
                )
            else:
                self.send_json({"ok": True, "job": job}, head_only=head_only)
            return
        if path == "/_pause_status":
            if query:
                self.send_error(400, "query parameters are not supported")
            else:
                self.send_json(
                    {"ok": True, "paused": self.server.config.pause_flag_path.is_file()},
                    head_only=head_only,
                )
            return
        if path == "/_rebuild_status":
            if query:
                self.send_error(400, "query parameters are not supported")
                return
            with self.server.rebuild_state_lock:
                status = self.server.rebuild_state.public_dict()
            self.send_json({"ok": True, **status}, head_only=head_only)
            return
        thumb_match = _SHA_ROUTE_RE.fullmatch(path)
        if thumb_match:
            if query:
                self.send_error(400, "query parameters are not supported")
            else:
                self._send_thumbnail(thumb_match.group(1), head_only=head_only)
            return
        if path.startswith("/thumb/"):
            self.send_error(400, "invalid thumbnail identity")
            return
        original_match = _ORIGINAL_ROUTE_RE.fullmatch(path)
        if original_match:
            if query:
                self.send_error(400, "query parameters are not supported")
            else:
                self._send_original(int(original_match.group(1)), head_only=head_only)
            return
        if path.startswith("/original/"):
            self.send_error(400, "invalid image identity")
            return
        if path.startswith("/media/preview/"):
            if query:
                self.send_error(400, "query parameters are not supported")
            else:
                self._send_preview_media(
                    path.removeprefix("/media/preview/"),
                    library=False,
                    head_only=head_only,
                )
            return
        if path.startswith("/media/library/"):
            if query:
                self.send_error(400, "query parameters are not supported")
            else:
                self._send_preview_media(
                    path.removeprefix("/media/library/"),
                    library=True,
                    head_only=head_only,
                )
            return
        self.send_error(404, "not found")

    def do_GET(self) -> None:
        self._serve_read(head_only=False)

    def do_HEAD(self) -> None:
        self._serve_read(head_only=True)

    def _post_pause(self, paused: bool) -> None:
        try:
            flag = self.server.config.pause_flag_path
            if paused:
                flag.parent.mkdir(parents=True, exist_ok=True)
                flag.touch(exist_ok=True)
            else:
                flag.unlink(missing_ok=True)
        except OSError:
            self.send_json(
                {"ok": False, "error": "pause state is unavailable"}, status=503
            )
            return
        self.send_json({"ok": True, "paused": paused})

    def _run_rebuild(self, *, lock_already_held: bool = False) -> None:
        if not lock_already_held and not self.server.rebuild_lock.acquire(blocking=False):
            return
        started = time.monotonic()
        with self.server.rebuild_state_lock:
            self.server.rebuild_state.running = True
            self.server.rebuild_state.at = datetime.now(timezone.utc).isoformat()
        ok = True
        try:
            config = self.server.config
            commands = (
                [
                    sys.executable,
                    str(config.app_reports_root / "_build_dashboard.py"),
                    "--collection-root",
                    str(config.collection_root),
                    "--queue-state-path",
                    str(config.queue_state_path),
                    "--library-root",
                    str(config.library_root),
                    "--preview-root",
                    str(config.preview_root),
                    "--report-output-root",
                    str(config.report_output_root),
                    "--pause-flag-path",
                    str(config.pause_flag_path),
                ],
                [
                    sys.executable,
                    str(config.app_reports_root / "_render_dashboard.py"),
                    "--snapshot-path",
                    str(config.report_output_root / "_snapshot.json"),
                    "--output-path",
                    str(config.report_output_root / "download-queue-dashboard.html"),
                ],
            )
            for command in commands:
                completed = subprocess.run(
                    command,
                    cwd=str(config.app_reports_root),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=180,
                    check=False,
                )
                if completed.returncode != 0:
                    ok = False
                    break
        except (OSError, subprocess.SubprocessError):
            ok = False
        finally:
            with self.server.rebuild_state_lock:
                self.server.rebuild_state.ok = ok
                self.server.rebuild_state.duration_s = round(
                    time.monotonic() - started, 2
                )
                self.server.rebuild_state.running = False
            self.server.rebuild_lock.release()

    def _post_rebuild(self, query: str) -> None:
        values = urllib.parse.parse_qs(query, keep_blank_values=True)
        if set(values) - {"wait"} or any(len(items) != 1 for items in values.values()):
            self.send_json({"ok": False, "error": "invalid rebuild query"}, status=400)
            return
        wait_value = values.get("wait", ["0"])[0]
        if wait_value not in {"0", "1"}:
            self.send_json({"ok": False, "error": "wait must be 0 or 1"}, status=400)
            return
        if not self.server.rebuild_lock.acquire(blocking=False):
            self.send_json({"ok": False, "error": "rebuild already running"}, status=409)
            return
        if wait_value == "1":
            self._run_rebuild(lock_already_held=True)
            with self.server.rebuild_state_lock:
                result = self.server.rebuild_state.public_dict()
            self.send_json({"ok": bool(result["ok"]), **result}, status=200 if result["ok"] else 503)
        else:
            threading.Thread(
                target=self._run_rebuild,
                kwargs={"lock_already_held": True},
                name="gallery-dashboard-rebuild",
                daemon=True,
            ).start()
            self.send_json({"ok": True, "status": "started"}, status=202)

    def _post_transfer(self) -> None:
        try:
            payload = self._read_json_body()
            unknown = sorted(set(payload) - {"target", "image_ids"})
            if unknown:
                raise library_transfer.TransferRequestError(
                    "unsupported request field(s): " + ", ".join(unknown)
                )
            job = self.server.transfer_manager.create_job(
                payload.get("target"), payload.get("image_ids")
            )
        except TypeError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=415)
            return
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
        ):
            self.send_json(
                {"ok": False, "error": "transfer service is unavailable"},
                status=503,
            )
            return
        self.send_json({"ok": True, "job": job}, status=202)

    def _post_suggestion(self, suggestion_id: int) -> None:
        helper = getattr(library_browser, "review_tag_suggestion", None)
        if not callable(helper):
            self.send_json(
                {"ok": False, "error": "suggestion review is unavailable"}, status=503
            )
            return
        try:
            payload = self._read_json_body()
            unknown = sorted(
                set(payload) - {"review_status", "reviewer", "decision_note"}
            )
            if unknown:
                raise ValueError(
                    "unsupported request field(s): " + ", ".join(unknown)
                )
            result = helper(
                self.server.config.database_path,
                suggestion_id,
                review_status=payload.get("review_status"),
                reviewer=payload.get("reviewer"),
                decision_note=payload.get("decision_note"),
            )
        except TypeError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=415)
            return
        except OverflowError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=413)
            return
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        except (FileNotFoundError, OSError, sqlite3.Error):
            self.send_json(
                {"ok": False, "error": "suggestion review is unavailable"}, status=503
            )
            return
        body = result if isinstance(result, Mapping) else {"suggestion": result}
        self.send_json({"ok": True, **body})

    def do_POST(self) -> None:
        try:
            path, query = self._parsed_target()
        except ValueError:
            self.send_error(400, "invalid request target")
            return
        if not self._same_origin_request():
            self.close_connection = True
            self.send_json({"ok": False, "error": "cross-origin request denied"}, status=403)
            return
        if path == "/_pause" and not query:
            if not self._accept_empty_control_body():
                return
            self._post_pause(True)
            return
        if path == "/_resume" and not query:
            if not self._accept_empty_control_body():
                return
            self._post_pause(False)
            return
        if path == "/_rebuild":
            if not self._accept_empty_control_body():
                return
            self._post_rebuild(query)
            return
        if path == "/api/library/transfers" and not query:
            self._post_transfer()
            return
        suggestion_match = _SUGGESTION_RE.fullmatch(path)
        if suggestion_match and not query:
            self._post_suggestion(int(suggestion_match.group(1)))
            return
        if path in _READ_ONLY_PATHS or path.startswith(("/thumb/", "/original/", "/media/", "/reports/")):
            self.close_connection = True
            self._method_not_allowed("GET, HEAD")
            return
        self.close_connection = True
        self.send_error(404, "not found")

    def _unsupported_method(self) -> None:
        try:
            path, _query = self._parsed_target()
        except ValueError:
            self.send_error(400, "invalid request target")
            return
        if path in _POST_ONLY_PATHS or _SUGGESTION_RE.fullmatch(path):
            allow = "POST"
        elif path == "/api/library/transfers":
            allow = "GET, HEAD, POST"
        elif path in _READ_ONLY_PATHS or path == "/" or path.startswith(
            ("/thumb/", "/original/", "/media/", "/reports/")
        ):
            allow = "GET, HEAD"
        else:
            allow = "GET, HEAD, POST"
        self._method_not_allowed(allow)

    do_DELETE = _unsupported_method
    do_OPTIONS = _unsupported_method
    do_PATCH = _unsupported_method
    do_PUT = _unsupported_method
    do_TRACE = _unsupported_method

    def do_CONNECT(self) -> None:
        self._method_not_allowed("GET, HEAD, POST")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--collection-root", type=Path, required=True)
    parser.add_argument("--library-root", type=Path, required=True)
    parser.add_argument("--database-path", type=Path, required=True)
    parser.add_argument("--queue-state-path", type=Path, required=True)
    parser.add_argument("--report-output-root", type=Path, required=True)
    parser.add_argument("--thumbnail-cache-root", type=Path, required=True)
    parser.add_argument("--preview-root", type=Path, required=True)
    parser.add_argument("--pause-flag-path", type=Path, required=True)
    parser.add_argument("--transfer-config-path", type=Path, required=True)
    parser.add_argument("--environment-path", type=Path, required=True)
    parser.add_argument("--app-reports-root", type=Path, default=APP_REPORTS_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = ServerConfig(
        collection_root=args.collection_root,
        library_root=args.library_root,
        database_path=args.database_path,
        queue_state_path=args.queue_state_path,
        report_output_root=args.report_output_root,
        thumbnail_cache_root=args.thumbnail_cache_root,
        preview_root=args.preview_root,
        pause_flag_path=args.pause_flag_path,
        transfer_config_path=args.transfer_config_path,
        environment_path=args.environment_path,
        app_reports_root=args.app_reports_root,
    )
    with create_server((args.host, args.port), config) as server:
        host, port = server.server_address[:2]
        print(f"gallery server listening on http://{host}:{port}/")
        print("only explicit report, API, thumbnail, original, and media routes are served")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nshutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
