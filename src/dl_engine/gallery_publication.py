"""Fail-closed publication primitives for the schema-4 gallery index.

The module deliberately separates read-only inspection from mutation.  All
paths are caller supplied; there are no production defaults and no operation in
this module owns the queue hold, scheduled task, gallery listener, media tree,
or provider ledgers.

The JSON Schema in ``schemas/gallery-publication-manifest.schema.json`` is the
shape authority.  This module implements the schema-v1 keyword subset with the
standard library and then applies the cross-field/byte-identity checks which a
JSON Schema cannot express.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import ntpath
import os
import re
import shutil
import sqlite3
import stat
import time
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Protocol, Sequence


MANIFEST_SCHEMA_VERSION = 1
SEMANTIC_CONTRACT_VERSION = 1
REPORT_FORMAT_VERSION = 1
JOURNAL_FORMAT_VERSION = 1
SQLITE_SCHEMA_VERSION = 4
HASH_BUFFER_SIZE = 1024 * 1024
MAX_MANIFEST_CHAIN_DEPTH = 64
UTC_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,9}))?Z$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ARTIFACT_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:\\")
IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".jfif", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp", ".avif", ".heic"}
)
PATH_VALUE_FIELDS = frozenset(
    {
        "path", "final_path", "target_path", "manifest_path",
        "pre_activation_manifest_path", "database_path", "library_root",
        "canonical_database", "candidate_database", "wallhaven_ledger",
        "provider_ledger", "verification_report_root", "manifest",
        "backup_directory", "recovery_journal", "recovery_result_root",
        "queue_state", "hold_path", "sibling_database", "hold_file",
        "pause_file", "executable_path", "working_directory",
        "canonical_database", "backup_directory", "verifier_path",
    }
)
REQUIRED_SCHEMA4_COLUMNS: Mapping[str, frozenset[str]] = {
    "images": frozenset(
        {
            "id", "path", "filename", "source", "ext", "width", "height",
            "orientation", "resolution_bucket", "source_site_id", "franchise",
            "purity", "enrichment_status", "indexed_at", "metadata_path",
            "source_url", "original_filename", "canonical_filename", "slug",
            "sha256", "size_bytes", "transport", "source_relative_path",
            "download_recorded_at", "search_origins_json", "content_rating",
            "rating_confidence", "rating_basis", "rating_reasons_json",
            "nsfw_subcategory", "tag_count",
        }
    ),
    "tags": frozenset(
        {"id", "name", "category_id", "category", "source", "slug", "tag_type", "provenance"}
    ),
    "image_tags": frozenset({"image_id", "tag_id", "provenance"}),
    "enrichment_progress": frozenset({"source", "last_processed_source_site_id", "updated_at"}),
    "schema_metadata": frozenset({"key", "value"}),
    "library_facets": frozenset({"facet", "value", "count", "refreshed_at"}),
    "tag_suggestions": frozenset(
        {
            "id", "image_id", "label", "normalized_label", "confidence",
            "generator", "model_version", "provenance", "review_status",
            "created_at", "reviewed_at", "reviewer", "decision_note",
        }
    ),
}


class PublicationError(RuntimeError):
    """Base class for publication failures with a stable machine-readable code."""

    code = "publication-error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class StrictJsonError(PublicationError):
    code = "invalid-json"


class SchemaValidationError(PublicationError):
    code = "manifest-schema-invalid"


class SemanticValidationError(PublicationError):
    code = "manifest-semantic-invalid"


class PathSafetyError(PublicationError):
    code = "unsafe-path"


class ArtifactCollisionError(PublicationError):
    code = "artifact-collision"


class ActivationError(PublicationError):
    code = "activation-failed"


class RollbackError(PublicationError):
    code = "rollback-failed"


def _validate_artifact_token(value: object, label: str) -> str:
    if not isinstance(value, str) or ARTIFACT_TOKEN_RE.fullmatch(value) is None:
        raise SemanticValidationError(
            f"{label} must be a filename-safe token of at most 128 characters"
        )
    return value


@dataclass(frozen=True)
class FileIdentity:
    path: Path
    final_path: Path
    exists: bool
    size_bytes: int
    sha256: str | None
    mtime_utc: str | None
    volume_serial: str

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "final_path": str(self.final_path),
            "exists": self.exists,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "mtime_utc": self.mtime_utc,
            "volume_serial": self.volume_serial,
        }


@dataclass(frozen=True)
class DatabaseSetIdentity:
    main: FileIdentity
    wal: FileIdentity
    shm: FileIdentity
    aggregate_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "main": self.main.as_dict(),
            "wal": self.wal.as_dict(),
            "shm": self.shm.as_dict(),
            "aggregate_sha256": self.aggregate_sha256,
        }


@dataclass(frozen=True)
class SQLiteIdentity:
    pragma_user_version: int
    metadata_schema_version: int | None
    quick_check: str
    journal_mode: str
    connection_closed: bool
    wal_checkpointed: bool
    required_schema4_columns_present: bool
    table_counts: tuple[tuple[str, int], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "pragma_user_version": self.pragma_user_version,
            "metadata_schema_version": self.metadata_schema_version,
            "quick_check": self.quick_check,
            "journal_mode": self.journal_mode,
            "connection_closed": self.connection_closed,
            "wal_checkpointed": self.wal_checkpointed,
            "required_schema4_columns_present": self.required_schema4_columns_present,
            "table_counts": dict(self.table_counts),
        }


@dataclass(frozen=True)
class InputFingerprint:
    path: Path
    final_path: Path
    kind: str
    exists: bool
    captured_at: str
    entry_count: int
    content_size_bytes: int | None
    content_sha256: str | None
    sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "final_path": str(self.final_path),
            "kind": self.kind,
            "exists": self.exists,
            "captured_at": self.captured_at,
            "entry_count": self.entry_count,
            "content_size_bytes": self.content_size_bytes,
            "content_sha256": self.content_sha256,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class DurableInputs:
    generation_id: str
    aggregate_sha256: str
    library: InputFingerprint
    sidecars: InputFingerprint
    wallhaven_ledger: InputFingerprint
    provider_ledger: InputFingerprint

    def as_dict(self) -> dict[str, object]:
        return {
            "generation_id": self.generation_id,
            "aggregate_sha256": self.aggregate_sha256,
            "library": self.library.as_dict(),
            "sidecars": self.sidecars.as_dict(),
            "wallhaven_ledger": self.wallhaven_ledger.as_dict(),
            "provider_ledger": self.provider_ledger.as_dict(),
        }


@dataclass(frozen=True)
class ValidatedManifest:
    path: Path
    sha256: str
    document: dict[str, object]


@dataclass(frozen=True)
class JournalChain:
    transaction_id: str
    generation_id: str
    records: tuple[dict[str, object], ...]
    segments: tuple[dict[str, object], ...]
    head_sha256: str | None
    tail_segment_sha256: str
    derived_status: str


@dataclass(frozen=True)
class ActivationOutcome:
    canonical_before: DatabaseSetIdentity
    canonical_before_sqlite: SQLiteIdentity
    canonical: DatabaseSetIdentity
    sqlite: SQLiteIdentity
    candidate: DatabaseSetIdentity
    candidate_sqlite: SQLiteIdentity
    backup: DatabaseSetIdentity
    journal: FileIdentity
    journal_evidence: dict[str, object]
    rolled_back: bool


@dataclass(frozen=True)
class PublicationPaths:
    canonical_database: Path
    candidate_database: Path
    library_root: Path
    wallhaven_ledger: Path
    provider_ledger: Path
    verification_report_root: Path
    manifest: Path
    backup_directory: Path
    recovery_journal: Path
    recovery_result_root: Path
    queue_state: Path
    hold_path: Path
    sibling_database: Path

    def as_dict(self) -> dict[str, object]:
        return {
            name: normalise_windows_path(value)
            for name, value in (
                ("canonical_database", self.canonical_database),
                ("candidate_database", self.candidate_database),
                ("library_root", self.library_root),
                ("wallhaven_ledger", self.wallhaven_ledger),
                ("provider_ledger", self.provider_ledger),
                ("verification_report_root", self.verification_report_root),
                ("manifest", self.manifest),
                ("backup_directory", self.backup_directory),
                ("recovery_journal", self.recovery_journal),
                ("recovery_result_root", self.recovery_result_root),
                ("queue_state", self.queue_state),
                ("hold_path", self.hold_path),
                ("sibling_database", self.sibling_database),
            )
        }

    def explicit_mapping(self) -> dict[str, Path]:
        return {key: Path(str(value)) for key, value in self.as_dict().items()}


@dataclass(frozen=True)
class PrepareOutcome:
    generation_id: str
    manifest: ValidatedManifest
    candidate: DatabaseSetIdentity
    sqlite: SQLiteIdentity
    durable_inputs: DurableInputs
    verification: dict[str, object]
    build_stats: Mapping[str, object]


@dataclass(frozen=True)
class PublishOutcome:
    manifest: ValidatedManifest
    activation: ActivationOutcome
    post_verification: dict[str, object]


class VerificationCallable(Protocol):
    def __call__(self, database: Path, library_root: Path, **kwargs: object) -> Mapping[str, object]: ...


@dataclass
class PublicationHooks:
    """Injectable mutation/verification seams used by failure-matrix tests."""

    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    sleep: Callable[[float], None] = time.sleep
    copy_file: Callable[[Path, Path], object] = lambda source, target: shutil.copyfile(source, target)
    replace: Callable[[Path, Path], None] = lambda source, target: os.replace(source, target)
    unlink: Callable[[Path], None] = lambda path: path.unlink()
    fsync_file: Callable[[int], None] = os.fsync
    fsync_directory: Callable[[Path], None] = lambda path: _flush_directory(path)
    checkpoint: Callable[[str], None] = lambda _name: None


def _reject_json_constant(value: str) -> object:
    raise StrictJsonError(f"non-finite JSON number is forbidden: {value}")


def _pairs_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJsonError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def loads_json_strict(payload: str | bytes) -> object:
    """Decode strict UTF-8 JSON, rejecting duplicates and non-finite values."""

    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StrictJsonError(f"JSON is not UTF-8: {exc}") from exc
    else:
        text = payload
    if text.startswith("\ufeff"):
        raise StrictJsonError("UTF-8 BOM is forbidden")
    try:
        return json.loads(
            text,
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_json_constant,
        )
    except StrictJsonError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise StrictJsonError(f"invalid JSON: {exc}") from exc


def load_json_strict(path: Path) -> dict[str, object]:
    document = loads_json_strict(Path(path).read_bytes())
    if not isinstance(document, dict):
        raise StrictJsonError(f"JSON root must be an object: {path}")
    return document


def validate_utc_timestamp(value: str) -> str:
    """Validate strict UTC text and return nine-digit fractional canonical form."""

    match = UTC_RE.fullmatch(value)
    if match is None:
        raise SemanticValidationError(f"invalid UTC timestamp: {value!r}")
    fraction = match.group("fraction") or ""
    # datetime validates calendar and clock fields.  It supports six fraction
    # digits, while the remaining three are retained exactly for canonical form.
    try:
        datetime.strptime(
            f"{match.group('date')}T{match.group('time')}.{(fraction + '000000')[:6]}Z",
            "%Y-%m-%dT%H:%M:%S.%fZ",
        )
    except ValueError as exc:
        raise SemanticValidationError(f"invalid UTC timestamp: {value!r}") from exc
    return f"{match.group('date')}T{match.group('time')}.{fraction.ljust(9, '0')}Z"


def utc_now_text(now: datetime | None = None) -> str:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("clock must return an aware datetime")
    instant = instant.astimezone(timezone.utc)
    return instant.strftime("%Y-%m-%dT%H:%M:%S.") + f"{instant.microsecond:06d}000Z"


def _normalise_json(
    value: object,
    *,
    digest: bool,
    field_name: str | None = None,
) -> object:
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StrictJsonError("non-finite JSON numbers are forbidden")
        return value
    if isinstance(value, str):
        text = unicodedata.normalize("NFC", value)
        is_timestamp_field = field_name is not None and (
            field_name.endswith("_at")
            or field_name.endswith("_utc")
            or field_name in {"sampled_at", "captured_at", "recorded_at"}
        )
        if is_timestamp_field and UTC_RE.fullmatch(text):
            return validate_utc_timestamp(text)
        is_path_field = field_name in PATH_VALUE_FIELDS or (
            field_name is not None
            and (field_name.endswith("_path") or field_name.endswith("_root") or field_name.endswith("_directory"))
        )
        if digest and is_path_field and WINDOWS_ABSOLUTE_RE.match(text.replace("/", "\\")):
            return normalise_windows_path(text).casefold()
        return text
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise StrictJsonError("JSON object keys must be strings")
            key = unicodedata.normalize("NFC", raw_key)
            if key in result:
                raise StrictJsonError(f"normalization creates duplicate key: {key!r}")
            result[key] = _normalise_json(item, digest=digest, field_name=key)
        return result
    if isinstance(value, (list, tuple)):
        return [_normalise_json(item, digest=digest, field_name=field_name) for item in value]
    raise StrictJsonError(f"unsupported JSON value: {type(value).__name__}")


def canonical_json_bytes(value: object, *, digest: bool = False) -> bytes:
    normalized = _normalise_json(value, digest=digest)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value, digest=True)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(HASH_BUFFER_SIZE)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def normalise_windows_path(path: str | os.PathLike[str]) -> str:
    text = os.fspath(path).replace("/", "\\")
    normalized = ntpath.normpath(text)
    drive, tail = ntpath.splitdrive(normalized)
    if not drive or not tail.startswith("\\"):
        raise PathSafetyError(f"path is not an absolute Windows path: {text!r}")
    return drive.upper() + tail


def path_key(path: str | os.PathLike[str]) -> str:
    return normalise_windows_path(path).casefold()


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _nearest_existing_parent(path: Path) -> tuple[Path, tuple[str, ...]]:
    missing: list[str] = []
    cursor = path
    while not cursor.exists():
        if cursor.parent == cursor:
            raise PathSafetyError(f"no existing ancestor for path: {path}")
        missing.append(cursor.name)
        cursor = cursor.parent
    return cursor, tuple(reversed(missing))


def resolve_final_path(path: Path, *, allow_missing: bool = False) -> Path:
    literal = Path(normalise_windows_path(path))
    if literal.exists():
        cursor = Path(literal.anchor)
        for part in literal.parts[1:]:
            cursor = cursor / part
            if _is_reparse(cursor):
                raise PathSafetyError(f"reparse points are forbidden: {cursor}")
        return literal.resolve(strict=True)
    if not allow_missing:
        raise FileNotFoundError(literal)
    parent, suffix = _nearest_existing_parent(literal)
    cursor = Path(parent.anchor)
    for part in parent.parts[1:]:
        cursor = cursor / part
        if _is_reparse(cursor):
            raise PathSafetyError(f"reparse points are forbidden: {cursor}")
    resolved = parent.resolve(strict=True)
    for part in suffix:
        resolved = resolved / part
    return Path(normalise_windows_path(resolved))


def require_descendant(path: Path, root: Path, *, allow_missing: bool = True) -> None:
    child = path_key(resolve_final_path(path, allow_missing=allow_missing))
    parent = path_key(resolve_final_path(root, allow_missing=False)).rstrip("\\")
    if child == parent or not child.startswith(parent + "\\"):
        raise PathSafetyError(f"{path} must be a child of {root}")


def require_distinct_paths(named_paths: Mapping[str, Path]) -> None:
    observed: dict[str, tuple[str, Path]] = {}
    for name, value in named_paths.items():
        literal = Path(normalise_windows_path(value))
        final = resolve_final_path(literal, allow_missing=True)
        key = path_key(final)
        previous = observed.get(key)
        if previous is not None:
            raise PathSafetyError(f"{name} aliases {previous[0]}: {value}")
        if literal.exists():
            for previous_name, previous_path in observed.values():
                if previous_path.exists() and os.path.samefile(literal, previous_path):
                    raise PathSafetyError(
                        f"{name} aliases {previous_name} through the same file identity: {value}"
                    )
        observed[key] = (name, literal)


def _volume_serial_windows(path: Path) -> str:
    final = resolve_final_path(path, allow_missing=True)
    root = Path(final.anchor)
    if os.name != "nt":
        return f"dev-{root.stat().st_dev:x}"
    serial = ctypes.c_uint32()
    maximum = ctypes.c_uint32()
    flags = ctypes.c_uint32()
    volume_name = ctypes.create_unicode_buffer(261)
    filesystem = ctypes.create_unicode_buffer(261)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(str(root)),
        volume_name,
        len(volume_name),
        ctypes.byref(serial),
        ctypes.byref(maximum),
        ctypes.byref(flags),
        filesystem,
        len(filesystem),
    )
    if not ok:
        raise ctypes.WinError()
    return f"{serial.value:08X}"


def database_handles_free(main_path: Path) -> bool:
    """Return true only when every existing SQLite member opens exclusively."""

    for member in _database_member_paths(Path(main_path)):
        if not member.exists():
            continue
        if os.name != "nt":
            try:
                descriptor = os.open(member, os.O_RDONLY)
            except OSError:
                return False
            else:
                os.close(descriptor)
                continue
        create_file = ctypes.windll.kernel32.CreateFileW
        create_file.restype = ctypes.c_void_p
        handle = create_file(
            str(member),
            0x80000000,  # GENERIC_READ
            0,  # no sharing: fail when any process retains a handle
            None,
            3,  # OPEN_EXISTING
            0x80,  # FILE_ATTRIBUTE_NORMAL
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle in (None, invalid):
            return False
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
    return True


def _mtime_utc_ns(mtime_ns: int) -> str:
    seconds, nanos = divmod(mtime_ns, 1_000_000_000)
    base = datetime.fromtimestamp(seconds, timezone.utc)
    return base.strftime("%Y-%m-%dT%H:%M:%S.") + f"{nanos:09d}Z"


def fingerprint_file(
    path: Path,
    *,
    allow_absent: bool = False,
    require_nonempty: bool = False,
    volume_serial_fn: Callable[[Path], str] = _volume_serial_windows,
) -> FileIdentity:
    literal = Path(normalise_windows_path(path))
    final = resolve_final_path(literal, allow_missing=allow_absent)
    volume = volume_serial_fn(final)
    if not literal.exists():
        if not allow_absent:
            raise FileNotFoundError(literal)
        return FileIdentity(literal, final, False, 0, None, None, volume)
    if not literal.is_file():
        raise PathSafetyError(f"expected a regular file: {literal}")
    with literal.open("rb") as handle:
        before = os.fstat(handle.fileno())
        digest = hashlib.sha256()
        while chunk := handle.read(HASH_BUFFER_SIZE):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, name) != getattr(after, name) for name in stable_fields):
        raise SemanticValidationError(f"file changed while it was fingerprinted: {literal}")
    observed_final = resolve_final_path(literal, allow_missing=False)
    if path_key(observed_final) != path_key(final):
        raise PathSafetyError(f"file path changed while it was fingerprinted: {literal}")
    pathname_state = literal.stat()
    if any(getattr(after, name) != getattr(pathname_state, name) for name in stable_fields):
        raise SemanticValidationError(f"file identity changed while it was fingerprinted: {literal}")
    size = after.st_size
    if require_nonempty and size == 0:
        raise SemanticValidationError(f"file must not be empty: {literal}")
    return FileIdentity(
        literal,
        final,
        True,
        size,
        digest.hexdigest(),
        _mtime_utc_ns(after.st_mtime_ns),
        volume,
    )


def _database_member_paths(main_path: Path) -> tuple[Path, Path, Path]:
    main = Path(normalise_windows_path(main_path))
    return main, Path(str(main) + "-wal"), Path(str(main) + "-shm")


def fingerprint_database_set(
    main_path: Path,
    *,
    require_closed: bool = False,
    require_checkpointed: bool = False,
    volume_serial_fn: Callable[[Path], str] = _volume_serial_windows,
) -> DatabaseSetIdentity:
    main_path, wal_path, shm_path = _database_member_paths(main_path)
    if not main_path.exists() and (wal_path.exists() or shm_path.exists()):
        raise SemanticValidationError(f"orphan SQLite sidecar without main database: {main_path}")
    main = fingerprint_file(main_path, require_nonempty=True, volume_serial_fn=volume_serial_fn)
    wal = fingerprint_file(wal_path, allow_absent=True, volume_serial_fn=volume_serial_fn)
    shm = fingerprint_file(shm_path, allow_absent=True, volume_serial_fn=volume_serial_fn)
    if require_checkpointed and wal.exists and wal.size_bytes != 0:
        raise SemanticValidationError(f"database WAL is not checkpointed: {wal.path}")
    if require_closed and shm.exists:
        raise SemanticValidationError(f"closed database has an SHM sidecar: {shm.path}")
    payload = {"main": main.as_dict(), "wal": wal.as_dict(), "shm": shm.as_dict()}
    return DatabaseSetIdentity(main, wal, shm, canonical_sha256(payload))


def cleanup_owned_sqlite_sidecars(
    main_path: Path,
    *,
    hooks: PublicationHooks | None = None,
    journal: "JournalWriter | None" = None,
    handle_free: Callable[[Path], bool] | None = None,
) -> None:
    """Checkpoint and remove sidecars created by this process's own verifier.

    Callers must establish that the database set was closed before their
    verifier ran.  Canonical cleanup is journaled; candidate cleanup is safe on
    its unique rebuildable output.
    """

    active = hooks or PublicationHooks()
    database = Path(normalise_windows_path(main_path))
    if handle_free is not None and not handle_free(database):
        raise SemanticValidationError("database handle-free proof failed before owned sidecar cleanup")
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    finally:
        connection.close()
    for sidecar in _database_member_paths(database)[1:]:
        before = fingerprint_file(sidecar, allow_absent=True)
        if not before.exists:
            continue
        if sidecar.name.endswith("-wal") and before.size_bytes != 0:
            raise SemanticValidationError(f"refusing to remove nonzero WAL: {sidecar}")
        intended = _project_file_identity(before, sidecar, exists=False)
        operation_id: str | None = None
        intent_sequence: int | None = None
        if journal is not None:
            operation_id, intent_sequence = journal.intent(
                "database-sidecar-remove", sidecar, before.as_dict(), intended.as_dict()
            )
        try:
            active.unlink(sidecar)
            active.fsync_directory(sidecar.parent)
            observed = fingerprint_file(sidecar, allow_absent=True)
            if observed.exists:
                raise SemanticValidationError(f"owned SQLite sidecar still exists: {sidecar}")
            if journal is not None:
                assert operation_id is not None and intent_sequence is not None
                journal.complete(
                    operation_id,
                    intent_sequence,
                    "database-sidecar-remove",
                    sidecar,
                    before.as_dict(),
                    intended.as_dict(),
                    observed.as_dict(),
                )
        except (KeyboardInterrupt, SystemExit) as exc:
            if journal is not None and operation_id is not None and intent_sequence is not None:
                journal.error(
                    operation_id,
                    intent_sequence,
                    "database-sidecar-remove",
                    sidecar,
                    before.as_dict(),
                    intended.as_dict(),
                    exc,
                )
            raise
        except Exception as exc:
            if journal is not None and operation_id is not None and intent_sequence is not None:
                journal.error(
                    operation_id,
                    intent_sequence,
                    "database-sidecar-remove",
                    sidecar,
                    before.as_dict(),
                    intended.as_dict(),
                    exc,
                )
            raise


def _flush_directory(path: Path) -> None:
    """Best-effort directory flush, using a real Windows directory handle."""

    directory = Path(path)
    if os.name != "nt":
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return
    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(directory),
        0x40000000,  # GENERIC_WRITE is required for FlushFileBuffers on a directory
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,  # OPEN_EXISTING
        0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        raise ctypes.WinError()
    try:
        if not ctypes.windll.kernel32.FlushFileBuffers(ctypes.c_void_p(handle)):
            raise ctypes.WinError()
    finally:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))


def _write_create_new(path: Path, payload: bytes, hooks: PublicationHooks) -> FileIdentity:
    target = Path(normalise_windows_path(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    resolve_final_path(target, allow_missing=True)
    try:
        with target.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            hooks.fsync_file(handle.fileno())
    except FileExistsError as exc:
        raise ArtifactCollisionError(f"refusing to overwrite artifact: {target}") from exc
    hooks.fsync_directory(target.parent)
    return fingerprint_file(target, require_nonempty=True)


def write_json_create_new(
    path: Path,
    document: Mapping[str, object],
    *,
    hooks: PublicationHooks | None = None,
) -> FileIdentity:
    active = hooks or PublicationHooks()
    return _write_create_new(path, canonical_json_bytes(document) + b"\n", active)


def replace_json_compare_and_swap(
    path: Path,
    document: Mapping[str, object],
    *,
    expected_sha256: str,
    archive_path: Path,
    hooks: PublicationHooks | None = None,
) -> FileIdentity:
    """Archive exact current bytes, then replace only if their hash is unchanged."""

    active = hooks or PublicationHooks()
    target = Path(normalise_windows_path(path))
    current = fingerprint_file(target, require_nonempty=True)
    if current.sha256 != expected_sha256:
        raise SemanticValidationError("manifest compare-and-swap precondition failed", code="manifest-cas-conflict")
    archive = Path(normalise_windows_path(archive_path))
    require_descendant(archive, target.parent)
    _write_create_new(archive, target.read_bytes(), active)
    if sha256_file(target) != expected_sha256:
        raise SemanticValidationError("manifest changed after archival", code="manifest-cas-conflict")
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_create_new(temporary, canonical_json_bytes(document) + b"\n", active)
        if sha256_file(target) != expected_sha256:
            raise SemanticValidationError("manifest changed before replacement", code="manifest-cas-conflict")
        active.replace(temporary, target)
        active.fsync_directory(target.parent)
    finally:
        if temporary.exists():
            temporary.unlink()
    return fingerprint_file(target, require_nonempty=True)


def transition_json_compare_and_swap(
    path: Path,
    *,
    expected_sha256: str,
    archive_path: Path,
    build_document: Callable[[FileIdentity], Mapping[str, object]],
    hooks: PublicationHooks | None = None,
) -> tuple[FileIdentity, dict[str, object]]:
    """Archive current bytes and build a CAS replacement bound to that archive."""

    active = hooks or PublicationHooks()
    target = Path(normalise_windows_path(path))
    current = fingerprint_file(target, require_nonempty=True)
    if current.sha256 != expected_sha256:
        raise SemanticValidationError("manifest compare-and-swap precondition failed", code="manifest-cas-conflict")
    archive = Path(normalise_windows_path(archive_path))
    require_descendant(archive, target.parent)
    archive_identity = _write_create_new(archive, target.read_bytes(), active)
    if archive_identity.sha256 != expected_sha256:
        raise SemanticValidationError("archived manifest does not match compare value")
    document = dict(build_document(archive_identity))
    if sha256_file(target) != expected_sha256:
        raise SemanticValidationError("manifest changed after archival", code="manifest-cas-conflict")
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_create_new(temporary, canonical_json_bytes(document) + b"\n", active)
        if sha256_file(target) != expected_sha256:
            raise SemanticValidationError("manifest changed before replacement", code="manifest-cas-conflict")
        active.replace(temporary, target)
        active.fsync_directory(target.parent)
    finally:
        if temporary.exists():
            temporary.unlink()
    return fingerprint_file(target, require_nonempty=True), document


def replace_json_with_precreated_archive(
    path: Path,
    document: Mapping[str, object],
    *,
    expected_sha256: str,
    archive_identity: FileIdentity,
    hooks: PublicationHooks | None = None,
) -> FileIdentity:
    """CAS-replace a manifest after a separately flushed exact archive exists."""

    active = hooks or PublicationHooks()
    target = Path(normalise_windows_path(path))
    if archive_identity.sha256 != expected_sha256 or sha256_file(archive_identity.path) != expected_sha256:
        raise SemanticValidationError("precreated manifest archive does not match CAS value")
    if sha256_file(target) != expected_sha256:
        raise SemanticValidationError("manifest compare-and-swap precondition failed", code="manifest-cas-conflict")
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_create_new(temporary, canonical_json_bytes(document) + b"\n", active)
        if sha256_file(target) != expected_sha256:
            raise SemanticValidationError("manifest changed before replacement", code="manifest-cas-conflict")
        active.replace(temporary, target)
        active.fsync_directory(target.parent)
    finally:
        if temporary.exists():
            temporary.unlink()
    return fingerprint_file(target, require_nonempty=True)


def _json_equal(left: object, right: object) -> bool:
    """JSON equality which does not equate booleans with integers."""

    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return not isinstance(left, bool) and not isinstance(right, bool) and left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_json_equal(a, b) for a, b in zip(left, right))
    return left == right


def _schema_type_matches(instance: object, name: str) -> bool:
    if name == "null":
        return instance is None
    if name == "boolean":
        return isinstance(instance, bool)
    if name == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if name == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool) and math.isfinite(instance)
    if name == "string":
        return isinstance(instance, str)
    if name == "array":
        return isinstance(instance, list)
    if name == "object":
        return isinstance(instance, dict)
    raise SchemaValidationError(f"unsupported schema type keyword: {name}")


def _resolve_schema_pointer(root: Mapping[str, object], reference: str) -> Mapping[str, object]:
    if not reference.startswith("#/"):
        raise SchemaValidationError(f"only local schema references are supported: {reference}")
    current: object = root
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            raise SchemaValidationError(f"unresolved schema reference: {reference}")
        current = current[part]
    if not isinstance(current, dict):
        raise SchemaValidationError(f"schema reference is not an object: {reference}")
    return current


def validate_json_schema_subset(
    instance: object,
    schema: Mapping[str, object],
    *,
    root_schema: Mapping[str, object] | None = None,
    location: str = "$",
) -> None:
    """Validate the Draft-2020-12 keyword subset owned by manifest schema v1."""

    root = root_schema or schema
    reference = schema.get("$ref")
    if reference is not None:
        if not isinstance(reference, str):
            raise SchemaValidationError(f"{location}: invalid $ref")
        validate_json_schema_subset(
            instance,
            _resolve_schema_pointer(root, reference),
            root_schema=root,
            location=location,
        )

    all_of = schema.get("allOf")
    if all_of is not None:
        if not isinstance(all_of, list):
            raise SchemaValidationError(f"{location}: malformed allOf")
        for branch in all_of:
            if not isinstance(branch, dict):
                raise SchemaValidationError(f"{location}: malformed allOf branch")
            validate_json_schema_subset(instance, branch, root_schema=root, location=location)

    one_of = schema.get("oneOf")
    if one_of is not None:
        if not isinstance(one_of, list):
            raise SchemaValidationError(f"{location}: malformed oneOf")
        matches = 0
        branch_errors: list[str] = []
        for branch in one_of:
            if not isinstance(branch, dict):
                raise SchemaValidationError(f"{location}: malformed oneOf branch")
            try:
                validate_json_schema_subset(instance, branch, root_schema=root, location=location)
            except SchemaValidationError as exc:
                branch_errors.append(str(exc))
            else:
                matches += 1
        if matches != 1:
            detail = "; ".join(branch_errors[:2])
            raise SchemaValidationError(f"{location}: expected exactly one matching oneOf branch, got {matches}; {detail}")

    negated = schema.get("not")
    if negated is not None:
        if not isinstance(negated, dict):
            raise SchemaValidationError(f"{location}: malformed not")
        try:
            validate_json_schema_subset(instance, negated, root_schema=root, location=location)
        except SchemaValidationError:
            pass
        else:
            raise SchemaValidationError(f"{location}: value matches forbidden schema")

    conditional = schema.get("if")
    if conditional is not None:
        if not isinstance(conditional, dict):
            raise SchemaValidationError(f"{location}: malformed if")
        try:
            validate_json_schema_subset(instance, conditional, root_schema=root, location=location)
        except SchemaValidationError:
            branch = schema.get("else")
        else:
            branch = schema.get("then")
        if branch is not None:
            if not isinstance(branch, dict):
                raise SchemaValidationError(f"{location}: malformed conditional branch")
            validate_json_schema_subset(instance, branch, root_schema=root, location=location)

    if "type" in schema:
        types = schema["type"]
        accepted = [types] if isinstance(types, str) else types
        if not isinstance(accepted, list) or not all(isinstance(item, str) for item in accepted):
            raise SchemaValidationError(f"{location}: malformed type keyword")
        if not any(_schema_type_matches(instance, item) for item in accepted):
            raise SchemaValidationError(f"{location}: expected type {accepted}, got {type(instance).__name__}")

    if "const" in schema and not _json_equal(instance, schema["const"]):
        raise SchemaValidationError(f"{location}: value does not match const")
    if "enum" in schema:
        choices = schema["enum"]
        if not isinstance(choices, list) or not any(_json_equal(instance, item) for item in choices):
            raise SchemaValidationError(f"{location}: value is not in enum")

    if isinstance(instance, str):
        length = len(instance)
        minimum_length = schema.get("minLength")
        if isinstance(minimum_length, int) and length < minimum_length:
            raise SchemaValidationError(f"{location}: string is shorter than {minimum_length}")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, instance) is None:
            raise SchemaValidationError(f"{location}: string does not match {pattern!r}")
        if schema.get("format") == "date-time":
            try:
                validate_utc_timestamp(instance)
            except SemanticValidationError as exc:
                raise SchemaValidationError(f"{location}: {exc}") from exc

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and instance < minimum:
            raise SchemaValidationError(f"{location}: number is below minimum {minimum}")
        if isinstance(maximum, (int, float)) and instance > maximum:
            raise SchemaValidationError(f"{location}: number exceeds maximum {maximum}")

    if isinstance(instance, list):
        minimum_items = schema.get("minItems")
        maximum_items = schema.get("maxItems")
        if isinstance(minimum_items, int) and len(instance) < minimum_items:
            raise SchemaValidationError(f"{location}: array has fewer than {minimum_items} items")
        if isinstance(maximum_items, int) and len(instance) > maximum_items:
            raise SchemaValidationError(f"{location}: array has more than {maximum_items} items")
        item_schema = schema.get("items")
        if item_schema is not None:
            if not isinstance(item_schema, dict):
                raise SchemaValidationError(f"{location}: malformed items schema")
            for index, item in enumerate(instance):
                validate_json_schema_subset(item, item_schema, root_schema=root, location=f"{location}[{index}]")

    if isinstance(instance, dict):
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise SchemaValidationError(f"{location}: malformed required keyword")
        missing = [item for item in required if item not in instance]
        if missing:
            raise SchemaValidationError(f"{location}: missing required properties: {', '.join(missing)}")
        minimum_properties = schema.get("minProperties")
        if isinstance(minimum_properties, int) and len(instance) < minimum_properties:
            raise SchemaValidationError(f"{location}: too few object properties")
        property_names = schema.get("propertyNames")
        if property_names is not None:
            if not isinstance(property_names, dict):
                raise SchemaValidationError(f"{location}: malformed propertyNames")
            for key in instance:
                validate_json_schema_subset(key, property_names, root_schema=root, location=f"{location}.<key>")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise SchemaValidationError(f"{location}: malformed properties")
        for key, item in instance.items():
            child_schema = properties.get(key)
            if child_schema is not None:
                if not isinstance(child_schema, dict):
                    raise SchemaValidationError(f"{location}.{key}: malformed property schema")
                validate_json_schema_subset(item, child_schema, root_schema=root, location=f"{location}.{key}")
                continue
            additional = schema.get("additionalProperties", True)
            if additional is False:
                raise SchemaValidationError(f"{location}: unknown property {key!r}")
            if isinstance(additional, dict):
                validate_json_schema_subset(item, additional, root_schema=root, location=f"{location}.{key}")


def load_manifest_schema(path: Path | None = None) -> dict[str, object]:
    schema_path = path or Path(__file__).resolve().parents[2] / "schemas" / "gallery-publication-manifest.schema.json"
    schema = load_json_strict(schema_path)
    if schema.get("$id") != "https://local.wallpapers/schemas/gallery-publication-manifest-v1.json":
        raise SchemaValidationError(f"unexpected manifest schema identity: {schema_path}")
    return schema


def read_sqlite_identity(
    main_path: Path,
    database: DatabaseSetIdentity | None = None,
    *,
    require_schema4: bool = False,
) -> SQLiteIdentity:
    db_set = database or fingerprint_database_set(main_path, require_checkpointed=True)
    if path_key(db_set.main.path) != path_key(main_path):
        raise SemanticValidationError("SQLite identity path does not match database-set main")
    if db_set.wal.exists and db_set.wal.size_bytes != 0:
        raise SemanticValidationError("SQLite identity requires a checkpointed WAL")
    uri = Path(main_path).resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    closed = False
    try:
        quick_rows = [str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall()]
        quick_ok = bool(quick_rows) and all(item.strip().casefold() == "ok" for item in quick_rows)
        if not quick_ok:
            raise SemanticValidationError(f"SQLite quick_check failed: {quick_rows!r}")
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
        if journal_mode not in {"wal", "delete"}:
            raise SemanticValidationError(f"unsupported SQLite journal mode: {journal_mode}")
        table_names = sorted(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        )
        table_counts = tuple(
            (name, int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]))
            for name in table_names
        )
        columns: dict[str, set[str]] = {}
        for name in REQUIRED_SCHEMA4_COLUMNS:
            if name in table_names:
                columns[name] = {
                    str(row[1]) for row in connection.execute(f'PRAGMA table_info("{name}")').fetchall()
                }
        required_present = all(
            name in table_names and expected.issubset(columns.get(name, set()))
            for name, expected in REQUIRED_SCHEMA4_COLUMNS.items()
        )
        metadata_version: int | None = None
        if "schema_metadata" in table_names and {"key", "value"}.issubset(columns.get("schema_metadata", set())):
            row = connection.execute(
                "SELECT value FROM schema_metadata WHERE key='schema_version'"
            ).fetchone()
            if row is not None:
                try:
                    metadata_version = int(str(row[0]))
                except ValueError as exc:
                    raise SemanticValidationError("schema_metadata.schema_version is not an integer") from exc
    finally:
        connection.close()
        closed = True
    identity = SQLiteIdentity(
        pragma_user_version=user_version,
        metadata_schema_version=metadata_version,
        quick_check="ok",
        journal_mode=journal_mode,
        connection_closed=closed,
        wal_checkpointed=not db_set.wal.exists or db_set.wal.size_bytes == 0,
        required_schema4_columns_present=required_present,
        table_counts=table_counts,
    )
    if require_schema4 and (
        identity.pragma_user_version != SQLITE_SCHEMA_VERSION
        or identity.metadata_schema_version != SQLITE_SCHEMA_VERSION
        or not identity.required_schema4_columns_present
    ):
        raise SemanticValidationError("database is not a complete schema-4 index")
    return identity


def _inventory_records(root: Path, *, sidecars: bool) -> list[dict[str, object]]:
    final_root = resolve_final_path(root, allow_missing=False)
    if not final_root.is_dir():
        raise NotADirectoryError(final_root)
    records: list[dict[str, object]] = []
    seen: dict[str, str] = {}
    for current_text, directory_names, file_names in os.walk(final_root, followlinks=False):
        current = Path(current_text)
        if _is_reparse(current):
            raise PathSafetyError(f"reparse directory in library: {current}")
        kept_directories: list[str] = []
        for name in sorted(directory_names, key=lambda item: (item.casefold(), item)):
            child = current / name
            if _is_reparse(child):
                raise PathSafetyError(f"reparse directory in library: {child}")
            if child == final_root / "_metadata":
                continue
            kept_directories.append(name)
        directory_names[:] = kept_directories
        for name in sorted(file_names, key=lambda item: (item.casefold(), item)):
            entry = current / name
            if _is_reparse(entry):
                raise PathSafetyError(f"reparse file in library: {entry}")
            is_sidecar = name.casefold().endswith(".wallpaper.json")
            if sidecars != is_sidecar:
                if sidecars or entry.suffix.casefold() not in IMAGE_EXTENSIONS:
                    continue
            relative = str(entry.relative_to(final_root)).replace("/", "\\")
            folded = relative.casefold()
            prior = seen.get(folded)
            if prior is not None:
                raise PathSafetyError(f"case-folded duplicate inventory path: {prior!r} and {relative!r}")
            seen[folded] = relative
            info = entry.stat()
            records.append(
                {
                    "path": unicodedata.normalize("NFC", relative),
                    "size_bytes": info.st_size,
                    "mtime_ns": info.st_mtime_ns,
                    "sha256": sha256_file(entry),
                }
            )
    records.sort(key=lambda item: (str(item["path"]).casefold(), str(item["path"])))
    return records


def _inventory_fingerprint(
    root: Path,
    *,
    kind: str,
    sidecars: bool,
    captured_at: str,
) -> InputFingerprint:
    final = resolve_final_path(root, allow_missing=False)
    records = _inventory_records(final, sidecars=sidecars)
    inventory_bytes = canonical_json_bytes(records, digest=True)
    content_sha = hashlib.sha256(inventory_bytes).hexdigest()
    envelope = {
        "bytes_sha256": content_sha,
        "entry_count": len(records),
        "exists": True,
        "length": len(inventory_bytes),
        "path": str(final),
    }
    return InputFingerprint(
        path=Path(normalise_windows_path(root)),
        final_path=final,
        kind=kind,
        exists=True,
        captured_at=captured_at,
        entry_count=len(records),
        content_size_bytes=len(inventory_bytes),
        content_sha256=content_sha,
        sha256=canonical_sha256(envelope),
    )


def _ledger_fingerprint(path: Path, *, kind: str, captured_at: str) -> InputFingerprint:
    literal = Path(normalise_windows_path(path))
    final = resolve_final_path(literal, allow_missing=True)
    if not literal.exists():
        envelope = {"entry_count": 0, "exists": False, "path": str(final)}
        return InputFingerprint(literal, final, kind, False, captured_at, 0, None, None, canonical_sha256(envelope))
    if not literal.is_file():
        raise PathSafetyError(f"ledger is not a regular file: {literal}")
    payload = literal.read_bytes()
    entries = 0
    for line_number, line in enumerate(payload.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            loads_json_strict(line)
        except StrictJsonError as exc:
            raise StrictJsonError(f"invalid ledger JSONL at {literal}:{line_number}: {exc}") from exc
        entries += 1
    content_sha = hashlib.sha256(payload).hexdigest()
    envelope = {
        "bytes_sha256": content_sha,
        "entry_count": entries,
        "exists": True,
        "length": len(payload),
        "path": str(final),
    }
    return InputFingerprint(
        literal,
        final,
        kind,
        True,
        captured_at,
        entries,
        len(payload),
        content_sha,
        canonical_sha256(envelope),
    )


def fingerprint_durable_inputs(
    library_root: Path,
    wallhaven_ledger: Path,
    provider_ledger: Path,
    generation_id: str,
    *,
    now: datetime | None = None,
) -> DurableInputs:
    if not generation_id:
        raise SemanticValidationError("generation_id must not be empty")
    captured_at = utc_now_text(now)
    library = _inventory_fingerprint(
        library_root, kind="library-inventory", sidecars=False, captured_at=captured_at
    )
    sidecars = _inventory_fingerprint(
        library_root, kind="sidecar-inventory", sidecars=True, captured_at=captured_at
    )
    wallhaven = _ledger_fingerprint(
        wallhaven_ledger, kind="wallhaven-ledger", captured_at=captured_at
    )
    provider = _ledger_fingerprint(
        provider_ledger, kind="provider-ledger", captured_at=captured_at
    )
    components = {
        "library": library.as_dict(),
        "sidecars": sidecars.as_dict(),
        "wallhaven_ledger": wallhaven.as_dict(),
        "provider_ledger": provider.as_dict(),
    }
    return DurableInputs(
        generation_id,
        _durable_aggregate(generation_id, components),
        library,
        sidecars,
        wallhaven,
        provider,
    )


def compare_durable_inputs(expected: Mapping[str, object], observed: DurableInputs) -> None:
    actual = observed.as_dict()
    # captured_at is observational rather than content identity.
    for name in ("library", "sidecars", "wallhaven_ledger", "provider_ledger"):
        left = dict(expected[name])  # type: ignore[arg-type]
        right = dict(actual[name])  # type: ignore[arg-type]
        left.pop("captured_at", None)
        right.pop("captured_at", None)
        if not _json_equal(left, right):
            raise SemanticValidationError(f"durable input changed: {name}")
    if expected.get("generation_id") != observed.generation_id:
        raise SemanticValidationError("durable input generation_id changed")
    if expected.get("aggregate_sha256") != observed.aggregate_sha256:
        raise SemanticValidationError("durable input aggregate changed")


def _durable_aggregate(
    generation_id: str,
    fingerprints: Mapping[str, Mapping[str, object]],
) -> str:
    return canonical_sha256(
        {
            "generation_id": generation_id,
            "library": fingerprints["library"]["sha256"],
            "sidecars": fingerprints["sidecars"]["sha256"],
            "wallhaven_ledger": fingerprints["wallhaven_ledger"]["sha256"],
            "provider_ledger": fingerprints["provider_ledger"]["sha256"],
        }
    )


def _taxonomy_from_issues(issues: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    counts: Counter[str] = Counter()
    for issue in issues:
        code = issue.get("code")
        if not isinstance(code, str) or not code:
            raise SemanticValidationError("verifier issue has no nonempty code")
        counts[code] += 1
    return [{"code": code, "count": counts[code]} for code in sorted(counts)]


def run_exhaustive_verification(
    verifier: VerificationCallable,
    database: Path,
    library_root: Path,
    *,
    generation_id: str,
    database_sha256: str,
    durable_inputs_sha256: str,
    verified_under_hold: bool,
    now: datetime | None = None,
) -> dict[str, object]:
    """Run the existing verifier while retaining an untruncated issue taxonomy."""

    observed_issues: list[Mapping[str, object]] = []

    def observe(issue: Mapping[str, object]) -> None:
        observed_issues.append(dict(issue))

    raw = dict(verifier(Path(database), Path(library_root), issue_observer=observe))
    if raw.get("schema_version") != REPORT_FORMAT_VERSION:
        raise SemanticValidationError("verifier report format version is not 1")
    raw_issue_total = raw.get("issues_total")
    if not isinstance(raw_issue_total, int) or isinstance(raw_issue_total, bool):
        raise SemanticValidationError("verifier issues_total is not an integer")
    if len(observed_issues) != raw_issue_total:
        raise SemanticValidationError(
            "verifier issue observer did not receive the exhaustive issue set"
        )
    taxonomy = _taxonomy_from_issues(observed_issues)
    issue_count = sum(int(item["count"]) for item in taxonomy)
    ok = raw.get("ok") is True and issue_count == 0
    exit_code = 0 if ok else 1
    generated_at = utc_now_text(now)
    return {
        "schema_version": REPORT_FORMAT_VERSION,
        "report_format_version": REPORT_FORMAT_VERSION,
        "generation_id": generation_id,
        "generated_at": generated_at,
        "database_path": normalise_windows_path(database),
        "library_root": normalise_windows_path(library_root),
        "database_sha256": database_sha256,
        "durable_inputs_sha256": durable_inputs_sha256,
        "exit_code": exit_code,
        "ok": ok,
        "exhaustive": True,
        "issue_count": issue_count,
        "issue_taxonomy": taxonomy,
        "verified_under_hold": verified_under_hold,
        "verifier": raw,
    }


def verification_evidence_from_report(
    report_path: Path,
    report_document: Mapping[str, object],
) -> dict[str, object]:
    identity = fingerprint_file(report_path, require_nonempty=True).as_dict()
    return {
        "report": identity,
        "report_format_version": report_document["report_format_version"],
        "exit_code": report_document["exit_code"],
        "ok": report_document["ok"],
        "exhaustive": report_document["exhaustive"],
        "issue_count": report_document["issue_count"],
        "issue_taxonomy": report_document["issue_taxonomy"],
        "database_path": report_document["database_path"],
        "library_root": report_document["library_root"],
        "database_sha256": report_document["database_sha256"],
        "durable_inputs_sha256": report_document["durable_inputs_sha256"],
        "generated_at": report_document["generated_at"],
        "verified_under_hold": report_document["verified_under_hold"],
    }


def write_verification_report(
    path: Path,
    report_document: Mapping[str, object],
    *,
    hooks: PublicationHooks | None = None,
) -> dict[str, object]:
    write_json_create_new(path, report_document, hooks=hooks)
    return verification_evidence_from_report(path, report_document)


def _stored_file_identity(document: Mapping[str, object]) -> FileIdentity:
    return FileIdentity(
        path=Path(str(document["path"])),
        final_path=Path(str(document["final_path"])),
        exists=bool(document["exists"]),
        size_bytes=int(document["size_bytes"]),
        sha256=None if document["sha256"] is None else str(document["sha256"]),
        mtime_utc=None if document["mtime_utc"] is None else str(document["mtime_utc"]),
        volume_serial=str(document["volume_serial"]),
    )


def _stored_database_set(document: Mapping[str, object]) -> DatabaseSetIdentity:
    return DatabaseSetIdentity(
        main=_stored_file_identity(document["main"]),  # type: ignore[arg-type]
        wal=_stored_file_identity(document["wal"]),  # type: ignore[arg-type]
        shm=_stored_file_identity(document["shm"]),  # type: ignore[arg-type]
        aggregate_sha256=str(document["aggregate_sha256"]),
    )


def _stored_sqlite_identity(document: Mapping[str, object]) -> SQLiteIdentity:
    table_counts = document.get("table_counts")
    if not isinstance(table_counts, dict):
        raise SemanticValidationError("stored SQLite table counts are invalid")
    return SQLiteIdentity(
        pragma_user_version=int(document["pragma_user_version"]),
        metadata_schema_version=None if document["metadata_schema_version"] is None else int(document["metadata_schema_version"]),
        quick_check=str(document["quick_check"]),
        journal_mode=str(document["journal_mode"]),
        connection_closed=bool(document["connection_closed"]),
        wal_checkpointed=bool(document["wal_checkpointed"]),
        required_schema4_columns_present=bool(document["required_schema4_columns_present"]),
        table_counts=tuple(sorted((str(key), int(value)) for key, value in table_counts.items())),
    )


def _assert_file_identity(
    expected: Mapping[str, object],
    *,
    allow_absent: bool,
    content_only: bool = False,
) -> FileIdentity:
    stored = _stored_file_identity(expected)
    observed = fingerprint_file(stored.path, allow_absent=allow_absent)
    fields = ("exists", "size_bytes", "sha256") if content_only else (
        "path", "final_path", "exists", "size_bytes", "sha256", "mtime_utc", "volume_serial"
    )
    for name in fields:
        left = getattr(stored, name)
        right = getattr(observed, name)
        if name in {"path", "final_path"}:
            if path_key(left) != path_key(right):
                raise SemanticValidationError(f"file identity {name} changed: {stored.path}")
        elif left != right:
            raise SemanticValidationError(f"file identity {name} changed: {stored.path}")
    return observed


def _validate_taxonomy(evidence: Mapping[str, object]) -> None:
    taxonomy = evidence.get("issue_taxonomy")
    issue_count = evidence.get("issue_count")
    if not isinstance(taxonomy, list) or not isinstance(issue_count, int) or isinstance(issue_count, bool):
        raise SemanticValidationError("verification taxonomy fields are malformed")
    codes: set[str] = set()
    total = 0
    for item in taxonomy:
        if not isinstance(item, dict):
            raise SemanticValidationError("verification taxonomy entry is not an object")
        code = item.get("code")
        count = item.get("count")
        if not isinstance(code, str) or not code or code in codes:
            raise SemanticValidationError("verification issue codes must be unique and nonempty")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise SemanticValidationError("verification issue count is invalid")
        codes.add(code)
        total += count
    if total != issue_count:
        raise SemanticValidationError("verification issue_count does not equal taxonomy sum")
    success = evidence.get("exit_code") == 0 and evidence.get("ok") is True
    if success != (issue_count == 0 and not taxonomy):
        raise SemanticValidationError("verification success and issue evidence disagree")


def validate_verification_evidence(
    evidence: Mapping[str, object],
    *,
    expected_database: Path,
    expected_library: Path,
    expected_database_sha256: str,
    expected_durable_inputs_sha256: str,
    expected_generation_id: str,
    report_root: Path,
    require_success: bool,
    require_under_hold: bool,
) -> dict[str, object]:
    _validate_taxonomy(evidence)
    report_identity = evidence.get("report")
    if not isinstance(report_identity, dict):
        raise SemanticValidationError("verification report identity is missing")
    report_path = Path(str(report_identity.get("path")))
    require_descendant(report_path, report_root, allow_missing=False)
    _assert_file_identity(report_identity, allow_absent=False)
    report = load_json_strict(report_path)
    expected_report_keys = {
        "schema_version", "report_format_version", "generation_id", "generated_at",
        "database_path", "library_root", "database_sha256", "durable_inputs_sha256",
        "exit_code", "ok", "exhaustive", "issue_count", "issue_taxonomy",
        "verified_under_hold", "verifier",
    }
    if set(report) != expected_report_keys:
        unknown = sorted(set(report) - expected_report_keys)
        missing = sorted(expected_report_keys - set(report))
        raise SemanticValidationError(
            f"verification report shape is invalid; missing={missing}, unknown={unknown}"
        )
    if report.get("schema_version") != REPORT_FORMAT_VERSION:
        raise SemanticValidationError("report schema_version is not report format 1")
    if report.get("generation_id") != expected_generation_id:
        raise SemanticValidationError("verification report generation ID does not match candidate")
    keys = (
        "report_format_version", "exit_code", "ok", "exhaustive", "issue_count",
        "issue_taxonomy", "database_path", "library_root", "database_sha256",
        "durable_inputs_sha256", "generated_at", "verified_under_hold",
    )
    for key in keys:
        if not _json_equal(report.get(key), evidence.get(key)):
            raise SemanticValidationError(f"verification report/evidence mismatch: {key}")
    if path_key(str(evidence["database_path"])) != path_key(expected_database):
        raise SemanticValidationError("verification names the wrong database")
    if path_key(str(evidence["library_root"])) != path_key(expected_library):
        raise SemanticValidationError("verification names the wrong library")
    if evidence.get("database_sha256") != expected_database_sha256:
        raise SemanticValidationError("verification database hash does not match candidate")
    if evidence.get("durable_inputs_sha256") != expected_durable_inputs_sha256:
        raise SemanticValidationError("verification durable-input hash does not match")
    if evidence.get("report_format_version") != REPORT_FORMAT_VERSION:
        raise SemanticValidationError("verification report_format_version is not 1")
    if evidence.get("exhaustive") is not True:
        raise SemanticValidationError("verification is not exhaustive")
    raw = report.get("verifier")
    raw_keys = {
        "schema_version", "ok", "status", "library_root", "db_path",
        "quick_check", "counts", "issues_total", "issues_truncated", "issues",
    }
    if not isinstance(raw, dict) or set(raw) != raw_keys:
        raise SemanticValidationError("embedded verifier report shape is invalid")
    if raw.get("schema_version") != REPORT_FORMAT_VERSION:
        raise SemanticValidationError("embedded verifier report format is invalid")
    if raw.get("ok") is not evidence.get("ok"):
        raise SemanticValidationError("embedded verifier success disagrees with evidence")
    expected_status = "ok" if evidence.get("ok") is True else "failed"
    if raw.get("status") != expected_status:
        raise SemanticValidationError("embedded verifier status disagrees with evidence")
    if path_key(str(raw.get("db_path"))) != path_key(expected_database):
        raise SemanticValidationError("embedded verifier names the wrong database")
    if path_key(str(raw.get("library_root"))) != path_key(expected_library):
        raise SemanticValidationError("embedded verifier names the wrong library")
    if raw.get("issues_total") != evidence.get("issue_count"):
        raise SemanticValidationError("embedded verifier issue total disagrees with evidence")
    raw_issues = raw.get("issues")
    if not isinstance(raw_issues, list) or not all(isinstance(item, dict) for item in raw_issues):
        raise SemanticValidationError("embedded verifier issue details are malformed")
    truncated = raw.get("issues_truncated")
    if truncated is not (int(evidence["issue_count"]) > len(raw_issues)):
        raise SemanticValidationError("embedded verifier truncation flag is invalid")
    taxonomy_counts = {
        str(item["code"]): int(item["count"])
        for item in evidence["issue_taxonomy"]  # type: ignore[union-attr]
    }
    raw_counts = Counter(str(item.get("code")) for item in raw_issues)
    if any(code not in taxonomy_counts or count > taxonomy_counts[code] for code, count in raw_counts.items()):
        raise SemanticValidationError("embedded verifier issues disagree with taxonomy")
    if not truncated and raw_counts != Counter(taxonomy_counts):
        raise SemanticValidationError("complete embedded verifier issues disagree with taxonomy")
    validate_utc_timestamp(str(evidence["generated_at"]))
    if require_success and not (
        evidence.get("exit_code") == 0
        and evidence.get("ok") is True
        and evidence.get("issue_count") == 0
        and evidence.get("issue_taxonomy") == []
    ):
        raise SemanticValidationError("candidate verification is blocked")
    if require_under_hold and evidence.get("verified_under_hold") is not True:
        raise SemanticValidationError("verification was not performed under the current hold")
    return report


def _register_report_identity(
    evidence: Mapping[str, object],
    *,
    role: str,
    seen_paths: MutableMapping[str, tuple[str, str]],
    seen_hashes: MutableMapping[str, tuple[str, str]],
) -> None:
    """Reject one report artifact reused for a distinct verification event."""

    report = evidence.get("report")
    if not isinstance(report, Mapping):
        raise SemanticValidationError("verification report identity is missing")
    report_path = path_key(str(report.get("path")))
    report_sha = report.get("sha256")
    if not isinstance(report_sha, str) or SHA256_RE.fullmatch(report_sha) is None:
        raise SemanticValidationError("verification report hash is invalid")
    prior_path = seen_paths.get(report_path)
    if prior_path is not None and prior_path != (report_sha, role):
        raise SemanticValidationError(
            "verification report path is reused across distinct attempts"
        )
    prior_hash = seen_hashes.get(report_sha)
    if prior_hash is not None and prior_hash != (report_path, role):
        raise SemanticValidationError(
            "verification report bytes are reused across distinct attempts"
        )
    seen_paths[report_path] = (report_sha, role)
    seen_hashes[report_sha] = (report_path, role)


def _recompute_database_set_aggregate(document: Mapping[str, object]) -> str:
    return canonical_sha256({"main": document["main"], "wal": document["wal"], "shm": document["shm"]})


def _validate_database_set_document(document: Mapping[str, object]) -> None:
    if document.get("aggregate_sha256") != _recompute_database_set_aggregate(document):
        raise SemanticValidationError("database-set aggregate_sha256 is invalid")
    volume_serials: set[str] = set()
    for member in ("main", "wal", "shm"):
        identity = document.get(member)
        if not isinstance(identity, dict):
            raise SemanticValidationError(f"database-set {member} identity is missing")
        if identity.get("exists") is True:
            digest = identity.get("sha256")
            if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
                raise SemanticValidationError(f"database-set {member} hash is invalid")
        volume_serial = identity.get("volume_serial")
        if not isinstance(volume_serial, str) or not volume_serial:
            raise SemanticValidationError(
                f"database-set {member} volume identity is invalid"
            )
        volume_serials.add(volume_serial)
    if len(volume_serials) != 1:
        raise SemanticValidationError("database-set members span multiple volumes")


def _database_set_volume(document: Mapping[str, object]) -> str:
    main = document.get("main")
    if not isinstance(main, Mapping):
        raise SemanticValidationError("database-set main identity is missing")
    volume = main.get("volume_serial")
    if not isinstance(volume, str) or not volume:
        raise SemanticValidationError("database-set volume identity is invalid")
    return volume


def _assert_database_set_paths(
    document: Mapping[str, object],
    main_path: Path,
) -> None:
    expected = _database_member_paths(Path(main_path))
    for name, path in zip(("main", "wal", "shm"), expected):
        identity = document.get(name)
        if not isinstance(identity, Mapping):
            raise SemanticValidationError(f"database-set {name} identity is missing")
        if path_key(str(identity.get("path"))) != path_key(path):
            raise SemanticValidationError(f"database-set {name} names the wrong path")


def _database_documents_match_content(
    left: Mapping[str, object],
    right: Mapping[str, object],
) -> bool:
    return _database_content_matches(_stored_database_set(left), _stored_database_set(right))


def _validate_input_fingerprint(document: Mapping[str, object]) -> None:
    exists = document.get("exists")
    final_path = str(document.get("final_path"))
    if exists is True:
        envelope = {
            "bytes_sha256": document.get("content_sha256"),
            "entry_count": document.get("entry_count"),
            "exists": True,
            "length": document.get("content_size_bytes"),
            "path": final_path,
        }
    else:
        envelope = {"entry_count": 0, "exists": False, "path": final_path}
    if document.get("sha256") != canonical_sha256(envelope):
        raise SemanticValidationError(f"invalid {document.get('kind')} fingerprint digest")


def validate_settled_samples(
    samples: Sequence[Mapping[str, object]],
    *,
    minimum_seconds: float = 30.0,
) -> None:
    if len(samples) < 2:
        raise SemanticValidationError("at least two settled samples are required")
    sequences: list[int] = []
    timestamps: list[datetime] = []
    elapsed_values: list[float] = []
    stable_fields = ("durable_inputs_sha256",)
    first = samples[0]
    for sample in samples:
        sequence = sample.get("sequence")
        elapsed = sample.get("elapsed_seconds")
        captured = sample.get("sampled_at")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise SemanticValidationError("settled sample sequence is invalid")
        if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool):
            raise SemanticValidationError("settled sample elapsed_seconds is invalid")
        if not isinstance(captured, str):
            raise SemanticValidationError("settled sample sampled_at is invalid")
        validate_utc_timestamp(captured)
        sequences.append(sequence)
        elapsed_values.append(float(elapsed))
        timestamps.append(datetime.fromisoformat(captured.replace("Z", "+00:00")))
        for field_name in stable_fields:
            if sample.get(field_name) != first.get(field_name):
                raise SemanticValidationError(f"settled samples are unstable: {field_name}")
        for database_field in ("candidate", "canonical"):
            database = sample.get(database_field)
            first_database = first.get(database_field)
            if not isinstance(database, dict) or not isinstance(first_database, dict):
                raise SemanticValidationError(f"settled sample {database_field} identity is invalid")
            _validate_database_set_document(database)
            if database.get("aggregate_sha256") != first_database.get("aggregate_sha256"):
                raise SemanticValidationError(f"settled samples are unstable: {database_field}")
    if sequences != list(range(sequences[0], sequences[0] + len(sequences))):
        raise SemanticValidationError("settled sample sequences are not contiguous")
    if any(right <= left for left, right in zip(elapsed_values, elapsed_values[1:])):
        raise SemanticValidationError("settled sample elapsed time is not increasing")
    if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
        raise SemanticValidationError("settled sample timestamps are not increasing")
    elapsed_window = elapsed_values[-1] - elapsed_values[0]
    wall_window = (timestamps[-1] - timestamps[0]).total_seconds()
    if elapsed_window < minimum_seconds or wall_window < minimum_seconds:
        raise SemanticValidationError("settled sample interval is too short")
    if abs(elapsed_window - wall_window) > 2.0:
        raise SemanticValidationError(
            "settled sample elapsed and wall-clock intervals disagree"
        )


def _validate_manifest_semantics(
    document: Mapping[str, object],
    *,
    manifest_path: Path | None,
    expected_paths: Mapping[str, Path] | None,
    check_current: bool,
    seen_manifests: set[str],
    seen_report_paths: MutableMapping[str, tuple[str, str]],
    seen_report_hashes: MutableMapping[str, tuple[str, str]],
    depth: int,
) -> None:
    if depth > MAX_MANIFEST_CHAIN_DEPTH:
        raise SemanticValidationError("manifest history is too deep")
    if document.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        raise SemanticValidationError("future or unsupported manifest schema version")
    if document.get("semantic_contract_version") != SEMANTIC_CONTRACT_VERSION:
        raise SemanticValidationError("future or unsupported semantic contract version")
    paths = document.get("paths")
    candidate = document.get("candidate")
    durable = document.get("durable_inputs")
    if not isinstance(paths, dict) or not isinstance(candidate, dict) or not isinstance(durable, dict):
        raise SemanticValidationError("manifest core evidence is missing")
    if expected_paths is not None:
        for name, expected in expected_paths.items():
            stored = paths.get(name)
            if stored is None or path_key(str(stored)) != path_key(expected):
                raise SemanticValidationError(f"manifest path does not match explicit argument: {name}")
    if manifest_path is not None and path_key(str(paths["manifest"])) != path_key(manifest_path):
        raise SemanticValidationError("manifest path does not match its paths.manifest field")
    require_distinct_paths(
        {
            "canonical_database": Path(str(paths["canonical_database"])),
            "candidate_database": Path(str(paths["candidate_database"])),
            "sibling_database": Path(str(paths["sibling_database"])),
        }
    )
    generation_id = candidate.get("generation_id")
    _validate_artifact_token(generation_id, "candidate generation_id")
    if generation_id != durable.get("generation_id"):
        raise SemanticValidationError("candidate and durable-input generation IDs differ")
    for name in ("library", "sidecars", "wallhaven_ledger", "provider_ledger"):
        fingerprint = durable.get(name)
        if not isinstance(fingerprint, dict):
            raise SemanticValidationError(f"durable input is missing: {name}")
        _validate_input_fingerprint(fingerprint)
    durable_path_bindings = {
        "library": paths["library_root"],
        "sidecars": paths["library_root"],
        "wallhaven_ledger": paths["wallhaven_ledger"],
        "provider_ledger": paths["provider_ledger"],
    }
    for name, expected_path in durable_path_bindings.items():
        fingerprint = durable[name]
        assert isinstance(fingerprint, Mapping)
        if path_key(str(fingerprint.get("path"))) != path_key(str(expected_path)):
            raise SemanticValidationError(f"durable input names the wrong path: {name}")
    if durable.get("aggregate_sha256") != _durable_aggregate(str(generation_id), durable):
        raise SemanticValidationError("durable-input aggregate_sha256 is invalid")
    candidate_database = candidate.get("database")
    candidate_sqlite = candidate.get("sqlite")
    if not isinstance(candidate_database, dict) or not isinstance(candidate_sqlite, dict):
        raise SemanticValidationError("candidate database/SQLite evidence is missing")
    _validate_database_set_document(candidate_database)
    transaction_volume = _database_set_volume(candidate_database)
    _assert_database_set_paths(
        candidate_database, Path(str(paths["candidate_database"]))
    )
    candidate_main = candidate_database.get("main")
    if not isinstance(candidate_main, dict):
        raise SemanticValidationError("candidate main identity is missing")
    if path_key(str(candidate_main["path"])) != path_key(str(paths["candidate_database"])):
        raise SemanticValidationError("candidate database identity names the wrong path")
    if candidate_sqlite.get("pragma_user_version") != SQLITE_SCHEMA_VERSION or candidate_sqlite.get("metadata_schema_version") != SQLITE_SCHEMA_VERSION:
        raise SemanticValidationError("candidate SQLite schema markers are not both 4")
    verification = document.get("verification")
    state = document.get("state")
    if verification is not None:
        if not isinstance(verification, dict):
            raise SemanticValidationError("verification evidence is malformed")
        _register_report_identity(
            verification,
            role=(
                "candidate-verification"
                if state == "candidate-verified"
                else "under-hold-verification"
            ),
            seen_paths=seen_report_paths,
            seen_hashes=seen_report_hashes,
        )
        validate_verification_evidence(
            verification,
            expected_database=Path(str(paths["candidate_database"])),
            expected_library=Path(str(paths["library_root"])),
            expected_database_sha256=str(candidate_main["sha256"]),
            expected_durable_inputs_sha256=str(durable["aggregate_sha256"]),
            expected_generation_id=str(generation_id),
            report_root=Path(str(paths["verification_report_root"])),
            require_success=True,
            require_under_hold=state in {"ready-to-publish", "published", "rolled-back"},
        )
    failed = document.get("last_failed_verification")
    if isinstance(failed, dict):
        _register_report_identity(
            failed,
            role="failed-verification",
            seen_paths=seen_report_paths,
            seen_hashes=seen_report_hashes,
        )
        validate_verification_evidence(
            failed,
            expected_database=Path(str(paths["candidate_database"])),
            expected_library=Path(str(paths["library_root"])),
            expected_database_sha256=str(candidate_main["sha256"]),
            expected_durable_inputs_sha256=str(durable["aggregate_sha256"]),
            expected_generation_id=str(generation_id),
            report_root=Path(str(paths["verification_report_root"])),
            require_success=False,
            require_under_hold=False,
        )
        if isinstance(verification, dict) and _utc_instant(
            failed.get("generated_at"), "failed verification"
        ) <= _utc_instant(verification.get("generated_at"), "accepted verification"):
            raise SemanticValidationError("failed verification is not newer than the accepted attempt")
        if document.get("result", {}).get("status") not in {"blocked", "failed", "rolled-back"}:  # type: ignore[union-attr]
            raise SemanticValidationError("failed verification coexists with a successful/in-progress result")
    transition = document.get("state_transition")
    if not isinstance(transition, dict):
        raise SemanticValidationError("state transition is missing")
    allowed_from = {
        "candidate-built": None,
        "candidate-verified": "candidate-built",
        "ready-to-publish": "candidate-verified",
        "published": "ready-to-publish",
        "rolled-back": {"ready-to-publish", "published"},
    }
    prior_state = transition.get("from_state")
    allowed = allowed_from.get(str(state))
    if isinstance(allowed, set):
        valid_edge = prior_state in allowed
    else:
        valid_edge = prior_state == allowed
    if not valid_edge:
        raise SemanticValidationError(f"invalid manifest state transition: {prior_state!r} -> {state!r}")
    created_at = _utc_instant(document.get("created_at"), "manifest creation")
    entered_at = _utc_instant(transition.get("entered_at"), "state transition")
    updated_at = _utc_instant(document.get("updated_at"), "manifest update")
    if not created_at <= entered_at <= updated_at:
        raise SemanticValidationError("manifest creation/transition/update times are not monotonic")
    authorization = document.get("authorization")
    if isinstance(authorization, Mapping) and authorization.get("apply") is True:
        authorized_at = _utc_instant(
            authorization.get("authorized_at"), "publication authorization"
        )
        if authorized_at > updated_at:
            raise SemanticValidationError("publication authorization follows the manifest update")
    previous = transition.get("previous_manifest")
    prior_document: Mapping[str, object] | None = None
    if previous is not None:
        if not isinstance(previous, dict):
            raise SemanticValidationError("previous manifest identity is malformed")
        previous_path = Path(str(previous["path"]))
        require_descendant(previous_path, Path(str(paths["manifest"])).parent, allow_missing=False)
        observed = _assert_file_identity(previous, allow_absent=False)
        key = path_key(observed.final_path)
        if key in seen_manifests:
            raise SemanticValidationError("manifest history contains a cycle")
        seen_manifests.add(key)
        prior_document = load_json_strict(previous_path)
        if prior_document.get("state") != prior_state:
            raise SemanticValidationError("archived manifest state does not match transition")
        if entered_at < _utc_instant(
            prior_document.get("updated_at"), "prior manifest update"
        ):
            raise SemanticValidationError(
                "manifest transition predates its archived predecessor"
            )
        schema = load_manifest_schema()
        validate_json_schema_subset(prior_document, schema, root_schema=schema)
        _validate_manifest_semantics(
            prior_document,
            manifest_path=None,
            expected_paths=None,
            check_current=False,
            seen_manifests=seen_manifests,
            seen_report_paths=seen_report_paths,
            seen_report_hashes=seen_report_hashes,
            depth=depth + 1,
        )
        for immutable_field in (
            "manifest_schema_version",
            "semantic_contract_version",
            "created_at",
            "paths",
            "candidate",
            "durable_inputs",
        ):
            if not _json_equal(
                document.get(immutable_field),
                prior_document.get(immutable_field),
            ):
                raise SemanticValidationError(
                    f"manifest transition changed immutable field: {immutable_field}"
                )
        current_machine = document.get("machine_identity")
        prior_machine = prior_document.get("machine_identity")
        stable_machine_fields = (
            "status",
            "machine_id",
            "instance_id",
            "computer_name",
            "qualified_user",
            "verifier_path",
        )
        if not isinstance(current_machine, Mapping) or not isinstance(
            prior_machine, Mapping
        ) or any(
            current_machine.get(field) != prior_machine.get(field)
            for field in stable_machine_fields
        ):
            raise SemanticValidationError(
                "manifest transition changed stable machine identity"
            )
        if state in {"published", "rolled-back"} and prior_state in {
            "ready-to-publish",
            "published",
        }:
            for retained_field in ("verification", "maintenance", "authorization"):
                if not _json_equal(
                    document.get(retained_field),
                    prior_document.get(retained_field),
                ):
                    raise SemanticValidationError(
                        f"terminal transition changed retained field: {retained_field}"
                    )
        if state == "rolled-back" and prior_state == "published":
            for retained_field in ("backup", "post_publish"):
                if not _json_equal(
                    document.get(retained_field),
                    prior_document.get(retained_field),
                ):
                    raise SemanticValidationError(
                        f"rollback changed published evidence: {retained_field}"
                    )
            current_activation = document.get("activation")
            prior_activation = prior_document.get("activation")
            if not isinstance(current_activation, Mapping) or not isinstance(
                prior_activation, Mapping
            ):
                raise SemanticValidationError(
                    "published rollback lacks activation continuity"
                )
            for retained_field in (
                "started_at",
                "canonical_before",
                "canonical_after",
                "canonical_before_sqlite",
                "canonical_after_sqlite",
            ):
                if not _json_equal(
                    current_activation.get(retained_field),
                    prior_activation.get(retained_field),
                ):
                    raise SemanticValidationError(
                        f"rollback changed activation anchor: {retained_field}"
                    )
    maintenance = document.get("maintenance")
    settled_canonical: Mapping[str, object] | None = None
    if isinstance(maintenance, dict):
        settled = maintenance.get("settled_samples")
        if isinstance(settled, list):
            validate_settled_samples(settled, minimum_seconds=float(maintenance.get("minimum_window_seconds", 30)))
            for sample in settled:
                if not isinstance(sample, Mapping):
                    raise SemanticValidationError("settled sample is malformed")
                sampled_candidate = sample.get("candidate")
                if (
                    not isinstance(sampled_candidate, Mapping)
                    or sampled_candidate.get("aggregate_sha256")
                    != candidate_database.get("aggregate_sha256")
                    or sample.get("durable_inputs_sha256") != durable.get("aggregate_sha256")
                ):
                    raise SemanticValidationError("settled sample is not bound to candidate/input evidence")
                sampled_canonical = sample.get("canonical")
                if not isinstance(sampled_canonical, Mapping):
                    raise SemanticValidationError("settled canonical sample is malformed")
                _assert_database_set_paths(
                    sampled_canonical, Path(str(paths["canonical_database"]))
                )
                if _database_set_volume(sampled_candidate) != transaction_volume or _database_set_volume(
                    sampled_canonical
                ) != transaction_volume:
                    raise SemanticValidationError(
                        "settled candidate/canonical evidence spans multiple volumes"
                    )
            settled_canonical = settled[-1].get("canonical")  # type: ignore[union-attr]
        hold = maintenance.get("hold")
        task = maintenance.get("scheduled_task")
        if isinstance(hold, dict):
            if path_key(str(hold["hold_file"]["path"])) != path_key(str(paths["hold_path"])):  # type: ignore[index]
                raise SemanticValidationError("hold evidence names the wrong token path")
            expected_hold_paths = {
                "pause_file": Path(str(paths["queue_state"])) / "pause.flag",
                "queue_state": Path(str(paths["queue_state"])) / "state.json",
            }
            for name, expected_path in expected_hold_paths.items():
                identity = hold.get(name)
                if not isinstance(identity, Mapping) or path_key(
                    str(identity.get("path"))
                ) != path_key(expected_path):
                    raise SemanticValidationError(f"hold evidence names the wrong {name} path")
            if hold.get("publisher_may_release") is not False:
                raise SemanticValidationError("publisher must not own hold release")
            if not isinstance(task, Mapping) or hold.get(
                "acknowledgement_sha256"
            ) != hold_acknowledgement_sha256(hold, task):
                raise SemanticValidationError("embedded hold acknowledgement digest is invalid")
            hold_acquired = _utc_instant(hold.get("acquired_at"), "hold acquisition")
            hold_acknowledged = _utc_instant(
                hold.get("acknowledged_at"), "hold acknowledgement"
            )
            hold_expiry = _utc_instant(hold.get("expires_at"), "hold expiry")
            window_started = _utc_instant(
                maintenance.get("window_started_at"), "maintenance window start"
            )
            window_ended = _utc_instant(
                maintenance.get("window_ended_at"), "maintenance window end"
            )
            verification_started = _utc_instant(
                maintenance.get("verification_started_at"), "verification start"
            )
            verification_completed = _utc_instant(
                maintenance.get("verification_completed_at"), "verification completion"
            )
            evidence_checked = _utc_instant(
                maintenance.get("evidence_checked_at"), "maintenance evidence check"
            )
            if not (
                hold_acquired
                <= hold_acknowledged
                <= window_started
                <= window_ended
                <= verification_started
                <= verification_completed
                <= evidence_checked
                <= _utc_instant(
                    authorization.get("authorized_at"), "publication authorization"  # type: ignore[union-attr]
                )
                < hold_expiry
            ):
                raise SemanticValidationError("hold/maintenance evidence times are not monotonic")
            task_observed = _utc_instant(
                task.get("observed_at"), "scheduled-task observation"  # type: ignore[union-attr]
            )
            task_acknowledged = _utc_instant(
                task.get("acknowledged_at"), "scheduled-task acknowledgement"  # type: ignore[union-attr]
            )
            if not (
                task.get("path") == "\\"  # type: ignore[union-attr]
                and task.get("name") == "Wallpaper Download Queue"  # type: ignore[union-attr]
                and hold_acquired
                <= task_observed
                <= task_acknowledged
                == hold_acknowledged
            ):
                raise SemanticValidationError(
                    "embedded scheduled-task acknowledgement is invalid"
                )
            writers = maintenance.get("writer_samples")
            if not isinstance(writers, list) or len(writers) < 2:
                raise SemanticValidationError(
                    "embedded writer observation window is incomplete"
                )
            writer_sequences: list[int] = []
            writer_elapsed: list[float] = []
            writer_times: list[datetime] = []
            for sample in writers:
                if (
                    not isinstance(sample, Mapping)
                    or not isinstance(sample.get("sequence"), int)
                    or isinstance(sample.get("sequence"), bool)
                    or not isinstance(sample.get("elapsed_seconds"), (int, float))
                    or isinstance(sample.get("elapsed_seconds"), bool)
                    or sample.get("downloader_descendant_count") != 0
                    or sample.get("index_writer_count") != 0
                    or sample.get("process_ids") != []
                ):
                    raise SemanticValidationError(
                        "embedded writer observation is malformed"
                    )
                writer_sequences.append(int(sample["sequence"]))
                writer_elapsed.append(float(sample["elapsed_seconds"]))
                writer_times.append(
                    _utc_instant(sample.get("sampled_at"), "writer sample")
                )
            if (
                writer_sequences
                != list(
                    range(
                        writer_sequences[0],
                        writer_sequences[0] + len(writer_sequences),
                    )
                )
                or any(
                    right <= left
                    for left, right in zip(writer_elapsed, writer_elapsed[1:])
                )
                or any(
                    right <= left
                    for left, right in zip(writer_times, writer_times[1:])
                )
                or writer_elapsed[-1] - writer_elapsed[0] < 30.0
                or writer_times[0] < window_started
                or writer_times[-1] > window_ended
                or (window_ended - window_started).total_seconds() < 30.0
            ):
                raise SemanticValidationError(
                    "embedded writer/window timing is invalid"
                )
            if isinstance(verification, Mapping):
                verification_generated = _utc_instant(
                    verification.get("generated_at"),
                    "verification report generation",
                )
                if not verification_started <= verification_generated <= verification_completed:
                    raise SemanticValidationError(
                        "verification report timestamp falls outside its execution interval"
                    )
            settled_samples = maintenance.get("settled_samples")
            if isinstance(settled_samples, list) and settled_samples:
                settled_times = [
                    _utc_instant(sample.get("sampled_at"), "settled sample")
                    for sample in settled_samples
                    if isinstance(sample, Mapping)
                ]
                if len(settled_times) != len(settled_samples) or not (
                    window_started <= settled_times[0]
                    and settled_times[-1] <= window_ended
                ):
                    raise SemanticValidationError(
                        "settled samples fall outside the maintenance window"
                    )
                settled_elapsed = [
                    float(sample.get("elapsed_seconds", -1.0))
                    for sample in settled_samples
                    if isinstance(sample, Mapping)
                ]
                if len(settled_elapsed) != len(settled_samples) or abs(
                    (settled_elapsed[-1] - settled_elapsed[0])
                    - float(maintenance.get("actual_window_seconds", -1.0))
                ) > 0.001:
                    raise SemanticValidationError(
                        "declared settled window duration is invalid"
                    )
        listener = maintenance.get("cutover_listener")
        if isinstance(listener, Mapping):
            expected_listener_ack = canonical_sha256(
                {
                    key: listener[key]
                    for key in (
                        "before_snapshot",
                        "before_process",
                        "after_snapshot",
                        "stopped_acknowledged_at",
                        "stopped_acknowledged_by",
                        "recovery_runbook",
                        "recovery_step",
                        "external_recovery_owner",
                        "restart_automatic",
                    )
                }
            )
            if listener.get("stopped_acknowledgement_sha256") != expected_listener_ack:
                raise SemanticValidationError("embedded listener acknowledgement digest is invalid")
            stopped_at = _utc_instant(
                listener.get("stopped_acknowledged_at"), "listener stop acknowledgement"
            )
            before = listener.get("before_snapshot")
            process = listener.get("before_process")
            after = listener.get("after_snapshot")
            if not isinstance(before, Mapping) or not isinstance(
                process, Mapping
            ) or not isinstance(after, Mapping):
                raise SemanticValidationError(
                    "embedded listener evidence is incomplete"
                )
            bindings = before.get("bindings")
            if not isinstance(bindings, list) or before.get("listen_count") != len(
                bindings
            ):
                raise SemanticValidationError(
                    "embedded listener binding count is invalid"
                )
            binding_keys: set[tuple[object, object, object]] = set()
            for binding in bindings:
                if not isinstance(binding, Mapping) or binding.get("pid") != process.get(
                    "pid"
                ):
                    raise SemanticValidationError(
                        "embedded listener binding has the wrong owner"
                    )
                binding_key = (
                    binding.get("address"),
                    binding.get("port"),
                    binding.get("pid"),
                )
                if binding_key in binding_keys:
                    raise SemanticValidationError(
                        "embedded listener bindings are duplicated"
                    )
                binding_keys.add(binding_key)
            if after.get("listen_count") != 0 or after.get("bindings") != []:
                raise SemanticValidationError(
                    "embedded listener after-snapshot is not stopped"
                )
            before_at = _utc_instant(before.get("observed_at"), "listener before")
            process_at = _utc_instant(
                process.get("observed_at"), "listener process"
            )
            after_at = _utc_instant(after.get("observed_at"), "listener after")
            if not before_at <= process_at <= after_at <= stopped_at <= evidence_checked:
                raise SemanticValidationError(
                    "embedded listener timestamps are not monotonic"
                )
            if not isinstance(authorization, Mapping) or stopped_at > _utc_instant(
                authorization.get("authorized_at"), "publication authorization"
            ):
                raise SemanticValidationError("listener stop acknowledgement follows authorization/update")
    backup_document = document.get("backup")
    backup_database: Mapping[str, object] | None = None
    backup_sqlite: SQLiteIdentity | None = None
    if isinstance(backup_document, Mapping):
        raw_backup = backup_document.get("database")
        raw_source_sqlite = backup_document.get("source_sqlite")
        if not isinstance(raw_backup, Mapping) or not isinstance(raw_source_sqlite, Mapping):
            raise SemanticValidationError("backup database/SQLite evidence is malformed")
        _validate_database_set_document(raw_backup)
        if _database_set_volume(raw_backup) != transaction_volume:
            raise SemanticValidationError("backup database is on a different volume")
        backup_main_path = Path(str(paths["backup_directory"])) / Path(
            str(paths["canonical_database"])
        ).name
        _assert_database_set_paths(raw_backup, backup_main_path)
        if settled_canonical is None or not _database_documents_match_content(
            raw_backup, settled_canonical
        ):
            raise SemanticValidationError("backup does not map the final settled canonical set")
        if backup_document.get("source_database_sha256") != settled_canonical["main"].get("sha256"):  # type: ignore[index]
            raise SemanticValidationError("backup source hash differs from settled canonical main")
        backup_database = raw_backup
        backup_sqlite = _stored_sqlite_identity(raw_source_sqlite)
        if check_current:
            observed_backup = fingerprint_database_set(
                backup_main_path, require_checkpointed=True
            )
            if not _json_equal(observed_backup.as_dict(), raw_backup):
                raise SemanticValidationError("backup bytes no longer match manifest")
            observed_backup_sqlite = read_sqlite_identity(
                backup_main_path, observed_backup
            )
            if not _sqlite_identity_matches(
                observed_backup_sqlite, backup_sqlite
            ):
                raise SemanticValidationError(
                    "backup SQLite identity no longer matches manifest"
                )
    activation_chain: JournalChain | None = None
    activation = document.get("activation")
    if isinstance(activation, dict):
        journal = activation.get("journal")
        if isinstance(journal, dict):
            activation_chain = _validate_journal_evidence(journal, paths=paths)
            if journal.get("generation_id") != generation_id:
                raise SemanticValidationError("activation journal generation differs from candidate")
            expected_preactivation: Mapping[str, object] | None = None
            if state == "published" and isinstance(previous, Mapping):
                expected_preactivation = previous
            elif state == "rolled-back" and prior_state == "ready-to-publish" and isinstance(
                previous, Mapping
            ):
                expected_preactivation = previous
            elif state == "rolled-back" and prior_state == "published" and isinstance(
                prior_document, Mapping
            ):
                prior_activation = prior_document.get("activation")
                if isinstance(prior_activation, Mapping):
                    prior_journal = prior_activation.get("journal")
                    if isinstance(prior_journal, Mapping) and isinstance(
                        prior_journal.get("pre_activation_manifest"), Mapping
                    ):
                        expected_preactivation = prior_journal[
                            "pre_activation_manifest"
                        ]  # type: ignore[assignment]
            journal_preactivation = journal.get("pre_activation_manifest")
            if expected_preactivation is not None and (
                not isinstance(journal_preactivation, Mapping)
                or not _json_equal(journal_preactivation, expected_preactivation)
            ):
                raise SemanticValidationError(
                    "activation journal pre-activation anchor differs from transition history"
                )
            recovery_result = journal.get("recovery_result")
            if isinstance(recovery_result, Mapping):
                recovery_document = recovery_result.get("document")
                if not isinstance(recovery_document, Mapping):
                    raise SemanticValidationError("activation recovery result document is missing")
                if (
                    _utc_instant(
                        recovery_document.get("completed_at"),
                        "recovery result completion",
                    )
                    != updated_at
                    or not _stable_machine_identity_matches(
                        recovery_document.get("machine_identity"),
                        document.get("machine_identity"),
                    )
                    or
                    recovery_document.get("manifest_pre_cas_matches_expected") is not True
                    or recovery_document.get("manifest_pre_cas_observed_sha256")
                    != recovery_document.get("manifest_cas_expected_sha256")
                    or recovery_document.get("manifest_cas_target_state") != state
                ):
                    raise SemanticValidationError("recovery result does not prove its manifest CAS precondition")
                expected_cas_anchor: object
                if state == "rolled-back":
                    expected_cas_anchor = previous.get("sha256") if isinstance(previous, Mapping) else None
                else:
                    preactivation_anchor = journal.get("pre_activation_manifest")
                    expected_cas_anchor = (
                        preactivation_anchor.get("sha256")
                        if isinstance(preactivation_anchor, Mapping)
                        else None
                    )
                if recovery_document.get("manifest_cas_expected_sha256") != expected_cas_anchor:
                    raise SemanticValidationError("recovery result CAS hash differs from its manifest anchor")
        canonical_before = activation.get("canonical_before")
        canonical_after = activation.get("canonical_after")
        canonical_before_sqlite = activation.get("canonical_before_sqlite")
        if not isinstance(canonical_before, Mapping) or not isinstance(
            canonical_before_sqlite, Mapping
        ):
            raise SemanticValidationError("activation pre-state evidence is malformed")
        _validate_database_set_document(canonical_before)
        _assert_database_set_paths(
            canonical_before, Path(str(paths["canonical_database"]))
        )
        if _database_set_volume(canonical_before) != transaction_volume:
            raise SemanticValidationError("activation pre-state is on a different volume")
        if settled_canonical is None or not _database_documents_match_content(
            canonical_before, settled_canonical
        ):
            raise SemanticValidationError("activation pre-state differs from settled canonical")
        if backup_sqlite is not None and not _sqlite_identity_matches(
            _stored_sqlite_identity(canonical_before_sqlite), backup_sqlite
        ):
            raise SemanticValidationError("activation pre-state SQLite differs from backup source")
        if isinstance(canonical_after, Mapping):
            _validate_database_set_document(canonical_after)
            _assert_database_set_paths(
                canonical_after, Path(str(paths["canonical_database"]))
            )
            if _database_set_volume(canonical_after) != transaction_volume:
                raise SemanticValidationError(
                    "activation post-state is on a different volume"
                )
        activation_started = _utc_instant(
            activation.get("started_at"), "activation start"
        )
        activation_completed = _utc_instant(
            activation.get("completed_at"), "activation completion"
        )
        if not isinstance(authorization, Mapping) or not (
            _utc_instant(
                authorization.get("authorized_at"), "publication authorization"
            )
            <= activation_started
            <= activation_completed
            <= updated_at
        ):
            raise SemanticValidationError("activation times are not monotonic")
        if isinstance(backup_document, Mapping) and _utc_instant(
            backup_document.get("created_at"), "backup creation"
        ) > activation_started:
            raise SemanticValidationError("backup creation follows activation start")
    post_publish = document.get("post_publish")
    if isinstance(post_publish, Mapping):
        if not isinstance(activation, Mapping) or not isinstance(
            activation.get("canonical_after"), Mapping
        ):
            raise SemanticValidationError("post-publish evidence has no activation output")
        activation_after = activation["canonical_after"]
        assert isinstance(activation_after, Mapping)
        if (
            post_publish.get("canonical_database_sha256")
            != activation_after["main"].get("sha256")  # type: ignore[index]
            or post_publish.get("canonical_database_sha256") != candidate_main.get("sha256")
            or post_publish.get("candidate_database_sha256") != candidate_main.get("sha256")
            or not _database_documents_match_content(activation_after, candidate_database)
        ):
            raise SemanticValidationError("post-publish database hashes do not bind activation/candidate")
        post_sqlite = post_publish.get("canonical_sqlite")
        activation_sqlite = activation.get("canonical_after_sqlite")
        if (
            not isinstance(post_sqlite, Mapping)
            or not isinstance(activation_sqlite, Mapping)
            or not _sqlite_identity_matches(
                _stored_sqlite_identity(post_sqlite), _stored_sqlite_identity(activation_sqlite)
            )
            or not _sqlite_identity_matches(
                _stored_sqlite_identity(post_sqlite), _stored_sqlite_identity(candidate_sqlite)
            )
        ):
            raise SemanticValidationError("post-publish SQLite evidence does not bind candidate")
        post_verification = post_publish.get("verification")
        if not isinstance(post_verification, dict):
            raise SemanticValidationError("post-publish verification is absent")
        _register_report_identity(
            post_verification,
            role="post-publish-verification",
            seen_paths=seen_report_paths,
            seen_hashes=seen_report_hashes,
        )
        validate_verification_evidence(
            post_verification,
            expected_database=Path(str(paths["canonical_database"])),
            expected_library=Path(str(paths["library_root"])),
            expected_database_sha256=str(candidate_main["sha256"]),
            expected_durable_inputs_sha256=str(durable["aggregate_sha256"]),
            expected_generation_id=str(generation_id),
            report_root=Path(str(paths["verification_report_root"])),
            require_success=True,
            require_under_hold=True,
        )
        post_verified_at = _utc_instant(
            post_publish.get("verified_at"), "post-publish verification"
        )
        post_report_generated = _utc_instant(
            post_verification.get("generated_at"),
            "post-publish verification report",
        )
        if post_verified_at > updated_at:
            raise SemanticValidationError("post-publish verification follows manifest update")
        if not isinstance(activation, Mapping) or not (
            _utc_instant(activation.get("started_at"), "activation start")
            <= post_report_generated
            <= post_verified_at
        ):
            raise SemanticValidationError(
                "post-publish verification report timestamp is outside activation"
            )
    rollback = document.get("rollback")
    if isinstance(rollback, Mapping) and rollback.get("succeeded") is True:
        restored_database = rollback.get("restored_database")
        restored_sqlite = rollback.get("restored_sqlite")
        if (
            backup_database is None
            or backup_sqlite is None
            or not isinstance(restored_database, Mapping)
            or not isinstance(restored_sqlite, Mapping)
            or not _database_documents_match_content(restored_database, backup_database)
            or not _sqlite_identity_matches(
                _stored_sqlite_identity(restored_sqlite), backup_sqlite
            )
        ):
            raise SemanticValidationError("rollback restoration does not bind the verified backup")
        assert isinstance(restored_database, Mapping)
        _validate_database_set_document(restored_database)
        if _database_set_volume(restored_database) != transaction_volume:
            raise SemanticValidationError("rollback restoration is on a different volume")
        _assert_database_set_paths(
            restored_database, Path(str(paths["canonical_database"]))
        )
        if rollback.get("restored_sha256_matches_backup") is not True:
            raise SemanticValidationError("successful rollback does not assert backup equality")
        if isinstance(activation, Mapping) and isinstance(activation.get("journal"), Mapping):
            recovery_result = activation["journal"].get("recovery_result")  # type: ignore[index,union-attr]
            if isinstance(recovery_result, Mapping):
                recovery_document = recovery_result.get("document")
                if (
                    not isinstance(recovery_document, Mapping)
                    or recovery_document.get("status") != "rolled-back"
                    or not isinstance(recovery_document.get("terminal_database"), Mapping)
                    or not _database_documents_match_content(
                        recovery_document["terminal_database"], restored_database  # type: ignore[arg-type,index]
                    )
                    or not isinstance(recovery_document.get("terminal_sqlite"), Mapping)
                    or not _sqlite_identity_matches(
                        _stored_sqlite_identity(recovery_document["terminal_sqlite"]),  # type: ignore[arg-type,index]
                        _stored_sqlite_identity(restored_sqlite),
                    )
                ):
                    raise SemanticValidationError("rollback recovery result bindings are invalid")
    if isinstance(rollback, Mapping):
        rollback_attempted = _utc_instant(
            rollback.get("attempted_at"), "rollback attempt"
        )
        rollback_completed = _utc_instant(
            rollback.get("completed_at"), "rollback completion"
        )
        if not rollback_attempted <= rollback_completed <= updated_at:
            raise SemanticValidationError("rollback times are not monotonic")
    failure_chain: JournalChain | None = None
    pre_activation_failure = document.get("pre_activation_failure")
    if isinstance(pre_activation_failure, Mapping):
        observed_outputs = pre_activation_failure.get("observed_outputs")
        if not isinstance(observed_outputs, list):
            raise SemanticValidationError(
                "pre-activation failure output inventory is missing"
            )
        canonical_name = Path(str(paths["canonical_database"])).name
        backup_main = Path(str(paths["backup_directory"])) / canonical_name
        exact_output_paths = {
            path_key(paths["recovery_journal"]),
            path_key(backup_main),
            path_key(str(backup_main) + "-wal"),
            path_key(str(backup_main) + "-shm"),
        }
        journal_root = Path(str(paths["recovery_journal"])).parent
        journal_stem = Path(str(paths["recovery_journal"])).stem
        result_root = Path(str(paths["recovery_result_root"]))
        for output in observed_outputs:
            if not isinstance(output, Mapping):
                raise SemanticValidationError(
                    "pre-activation failure output identity is malformed"
                )
            observed_output = _assert_file_identity(output, allow_absent=True)
            if observed_output.volume_serial != transaction_volume:
                raise SemanticValidationError(
                    "pre-activation failure output is on a different volume"
                )
            output_key = path_key(observed_output.path)
            allowed = output_key in exact_output_paths
            if not allowed and observed_output.path.name.startswith(
                f"{journal_stem}.recovery-"
            ) and observed_output.path.name.endswith(".segment.jsonl"):
                require_descendant(observed_output.path, journal_root)
                allowed = True
            if not allowed:
                try:
                    require_descendant(observed_output.path, result_root)
                except PathSafetyError:
                    pass
                else:
                    allowed = True
            if not allowed:
                raise SemanticValidationError(
                    "pre-activation failure output path is outside dedicated roots"
                )
        failed_database = pre_activation_failure.get("canonical_database")
        if (
            settled_canonical is None
            or not isinstance(failed_database, Mapping)
            or not _database_documents_match_content(failed_database, settled_canonical)
        ):
            raise SemanticValidationError("pre-activation failure does not prove canonical unchanged")
        assert isinstance(failed_database, Mapping)
        _validate_database_set_document(failed_database)
        if _database_set_volume(failed_database) != transaction_volume:
            raise SemanticValidationError(
                "pre-activation failure database is on a different volume"
            )
        _assert_database_set_paths(
            failed_database, Path(str(paths["canonical_database"]))
        )
        failure_journal = pre_activation_failure.get("journal")
        if isinstance(failure_journal, Mapping):
            failure_chain = _validate_journal_evidence(
                failure_journal, paths=paths
            )
            recovery_result = failure_journal.get("recovery_result")
            preactivation_anchor = failure_journal.get("pre_activation_manifest")
            if isinstance(recovery_result, Mapping):
                recovery_document = recovery_result.get("document")
                if (
                    not isinstance(recovery_document, Mapping)
                    or recovery_document.get("status") != "aborted-before-mutation"
                    or recovery_document.get("manifest_cas_target_state") != "ready-to-publish"
                    or recovery_document.get("manifest_pre_cas_matches_expected") is not True
                    or recovery_document.get("manifest_pre_cas_observed_sha256")
                    != recovery_document.get("manifest_cas_expected_sha256")
                    or not isinstance(preactivation_anchor, Mapping)
                    or recovery_document.get("manifest_cas_expected_sha256")
                    != preactivation_anchor.get("sha256")
                    or not isinstance(recovery_document.get("terminal_database"), Mapping)
                    or not _database_documents_match_content(
                        recovery_document["terminal_database"], failed_database  # type: ignore[arg-type,index]
                    )
                    or not isinstance(
                        pre_activation_failure.get("canonical_sqlite"), Mapping
                    )
                    or not isinstance(
                        recovery_document.get("terminal_sqlite"), Mapping
                    )
                    or not _sqlite_identity_matches(
                        _stored_sqlite_identity(
                            recovery_document["terminal_sqlite"]  # type: ignore[arg-type,index]
                        ),
                        _stored_sqlite_identity(
                            pre_activation_failure["canonical_sqlite"]  # type: ignore[arg-type,index]
                        ),
                    )
                ):
                    raise SemanticValidationError("pre-activation recovery result bindings are invalid")
        failure_closure = pre_activation_failure.get("closure_kind")
        failure_recovery_hold = pre_activation_failure.get("recovery_hold")
        if failure_closure == "pre-journal-recovery-close":
            historical_hold = (
                maintenance.get("hold")
                if isinstance(maintenance, Mapping)
                else None
            )
            if (
                failure_journal is not None
                or not isinstance(failure_recovery_hold, Mapping)
                or not isinstance(historical_hold, Mapping)
            ):
                raise SemanticValidationError(
                    "pre-journal recovery close lacks its hold-bound null-journal evidence"
                )
            _validate_historical_recovery_hold(
                failure_recovery_hold,
                authorized_head=None,
                historical_token_sha256=str(historical_hold.get("token_sha256")),
                historical_token_id=str(historical_hold.get("token_id")),
                paths=paths,
            )
            recovery_verified = _utc_instant(
                failure_recovery_hold.get("verified_at"),
                "pre-journal recovery verification",
            )
            failure_time = _utc_instant(
                pre_activation_failure.get("failed_at"),
                "pre-journal recovery failure",
            )
            recovery_hold_document = failure_recovery_hold.get("hold")
            if not isinstance(recovery_hold_document, Mapping) or not (
                recovery_verified
                <= failure_time
                == updated_at
                < _utc_instant(
                    recovery_hold_document.get("expires_at"),
                    "pre-journal recovery hold expiry",
                )
            ):
                raise SemanticValidationError(
                    "pre-journal recovery close falls outside its fresh hold"
                )
    journal_chain = activation_chain or failure_chain
    if journal_chain is not None:
        _validate_journal_database_member_coverage(
            journal_chain,
            settled_database=settled_canonical,
            backup_database=backup_database,
            rollback=rollback if isinstance(rollback, Mapping) else None,
            paths=paths,
        )
    result = document.get("result")
    if isinstance(result, Mapping) and result.get("terminal_at") is not None:
        if _utc_instant(result.get("terminal_at"), "terminal result") != updated_at:
            raise SemanticValidationError("terminal result time differs from manifest update")
    if check_current:
        current_candidate = fingerprint_database_set(
            Path(str(paths["candidate_database"])), require_closed=True, require_checkpointed=True
        )
        if not _json_equal(current_candidate.as_dict(), candidate_database):
            raise SemanticValidationError("candidate database bytes no longer match manifest")
        current_sqlite = read_sqlite_identity(
            Path(str(paths["candidate_database"])), current_candidate, require_schema4=True
        )
        if not _json_equal(current_sqlite.as_dict(), candidate_sqlite):
            raise SemanticValidationError("candidate SQLite identity no longer matches manifest")
        current_inputs = fingerprint_durable_inputs(
            Path(str(paths["library_root"])),
            Path(str(paths["wallhaven_ledger"])),
            Path(str(paths["provider_ledger"])),
            str(generation_id),
        )
        compare_durable_inputs(durable, current_inputs)
        if state == "published":
            if not isinstance(activation, Mapping) or not isinstance(
                activation.get("canonical_after"), Mapping
            ):
                raise SemanticValidationError("published manifest lacks canonical activation output")
            expected_current = activation["canonical_after"]
        elif state == "rolled-back":
            if not isinstance(rollback, Mapping) or not isinstance(
                rollback.get("restored_database"), Mapping
            ):
                raise SemanticValidationError("rolled-back manifest lacks restored database evidence")
            expected_current = rollback["restored_database"]
        elif state == "ready-to-publish" and (
            activation is None or pre_activation_failure is not None
        ):
            expected_current = settled_canonical
        else:
            expected_current = None
        if isinstance(expected_current, Mapping):
            observed_canonical = fingerprint_database_set(
                Path(str(paths["canonical_database"])), require_checkpointed=True
            )
            if not _json_equal(observed_canonical.as_dict(), expected_current):
                raise SemanticValidationError("current canonical database does not match terminal manifest state")
            observed_canonical_sqlite = read_sqlite_identity(
                Path(str(paths["canonical_database"])), observed_canonical
            )
            if state == "published":
                expected_current_sqlite = (
                    activation.get("canonical_after_sqlite")
                    if isinstance(activation, Mapping)
                    else None
                )
            elif state == "rolled-back":
                expected_current_sqlite = (
                    rollback.get("restored_sqlite")
                    if isinstance(rollback, Mapping)
                    else None
                )
            else:
                expected_current_sqlite = (
                    pre_activation_failure.get("canonical_sqlite")
                    if isinstance(pre_activation_failure, Mapping)
                    else None
                )
            if isinstance(expected_current_sqlite, Mapping) and not _sqlite_identity_matches(
                    observed_canonical_sqlite,
                    _stored_sqlite_identity(expected_current_sqlite),
                ):
                raise SemanticValidationError(
                    "current canonical SQLite identity does not match terminal manifest state"
                )


def validate_manifest_document(
    document: Mapping[str, object],
    *,
    expected_paths: Mapping[str, Path] | None = None,
    manifest_path: Path | None = None,
    schema_path: Path | None = None,
    check_current: bool = True,
) -> None:
    schema = load_manifest_schema(schema_path)
    validate_json_schema_subset(document, schema, root_schema=schema)
    seen = set()
    if manifest_path is not None:
        seen.add(path_key(resolve_final_path(manifest_path, allow_missing=True)))
    _validate_manifest_semantics(
        document,
        manifest_path=manifest_path,
        expected_paths=expected_paths,
        check_current=check_current,
        seen_manifests=seen,
        seen_report_paths={},
        seen_report_hashes={},
        depth=0,
    )


def validate_manifest_file(
    manifest_path: Path,
    *,
    expected_paths: Mapping[str, Path] | None = None,
    schema_path: Path | None = None,
    check_current: bool = True,
) -> ValidatedManifest:
    path = Path(normalise_windows_path(manifest_path))
    identity = fingerprint_file(path, require_nonempty=True)
    document = load_json_strict(path)
    validate_manifest_document(
        document,
        expected_paths=expected_paths,
        manifest_path=path,
        schema_path=schema_path,
        check_current=check_current,
    )
    assert identity.sha256 is not None
    return ValidatedManifest(path, identity.sha256, document)


def _machine_identity_document(value: Mapping[str, object]) -> dict[str, object]:
    keys = (
        "status", "machine_id", "instance_id", "computer_name", "qualified_user",
        "verified_at", "verifier_path",
    )
    missing = [key for key in keys if key not in value]
    if missing:
        raise SemanticValidationError(f"machine identity is incomplete: {', '.join(missing)}")
    return {key: value[key] for key in keys}


def _validate_current_machine_identity(
    value: Mapping[str, object],
    *,
    now: datetime,
    maximum_age_seconds: int = 300,
) -> dict[str, object]:
    document = _machine_identity_document(value)
    if document.get("status") != "VERIFIED":
        raise SemanticValidationError("machine identity is not VERIFIED")
    _assert_fresh_instant(
        _utc_instant(document.get("verified_at"), "machine identity verification"),
        now,
        maximum_age_seconds,
        "machine identity verification",
    )
    return document


def _stable_machine_identity_matches(
    left: object,
    right: object,
) -> bool:
    fields = (
        "status",
        "machine_id",
        "instance_id",
        "computer_name",
        "qualified_user",
        "verifier_path",
    )
    return isinstance(left, Mapping) and isinstance(right, Mapping) and all(
        left.get(field) == right.get(field) for field in fields
    )


def _in_progress_result(*, listener_restore_required: bool = False) -> dict[str, object]:
    return {
        "status": "in-progress",
        "release_eligible": False,
        "listener_restore_required": listener_restore_required,
        "issue_taxonomy": [],
        "terminal_at": None,
        "error_code": None,
        "error_message": None,
    }


def _candidate_built_manifest(
    *,
    paths: PublicationPaths,
    machine_identity: Mapping[str, object],
    generation_id: str,
    built_at: str,
    candidate: DatabaseSetIdentity,
    sqlite_identity: SQLiteIdentity,
    durable_inputs: DurableInputs,
) -> dict[str, object]:
    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "semantic_contract_version": SEMANTIC_CONTRACT_VERSION,
        "state": "candidate-built",
        "state_transition": {
            "from_state": None,
            "entered_at": built_at,
            "previous_manifest": None,
        },
        "created_at": built_at,
        "updated_at": built_at,
        "machine_identity": _machine_identity_document(machine_identity),
        "paths": paths.as_dict(),
        "candidate": {
            "built_at": built_at,
            "generation_id": generation_id,
            "database": candidate.as_dict(),
            "sqlite": sqlite_identity.as_dict(),
        },
        "durable_inputs": durable_inputs.as_dict(),
        "verification": None,
        "last_failed_verification": None,
        "maintenance": None,
        "authorization": {
            "apply": False,
            "cutover": False,
            "authorized_at": None,
            "authorized_by": None,
        },
        "backup": None,
        "pre_activation_failure": None,
        "activation": None,
        "post_publish": None,
        "rollback": None,
        "result": _in_progress_result(),
    }


def inspect_publication(
    paths: PublicationPaths,
    *,
    include_inputs: bool = True,
    generation_id: str = "inspection",
) -> dict[str, object]:
    """Read-only inspection.  Missing output paths are reported, never created."""

    result: dict[str, object] = {
        "mode": "inspect",
        "paths": paths.as_dict(),
        "canonical": None,
        "candidate": None,
        "manifest": None,
        "durable_inputs": None,
    }
    if Path(paths.canonical_database).is_file():
        canonical = fingerprint_database_set(paths.canonical_database)
        result["canonical"] = {
            "database": canonical.as_dict(),
            "sqlite": read_sqlite_identity(paths.canonical_database, canonical).as_dict(),
        }
    if Path(paths.candidate_database).is_file():
        candidate = fingerprint_database_set(paths.candidate_database)
        result["candidate"] = {
            "database": candidate.as_dict(),
            "sqlite": read_sqlite_identity(paths.candidate_database, candidate).as_dict(),
        }
    if Path(paths.manifest).is_file():
        identity = fingerprint_file(paths.manifest, require_nonempty=True)
        document = load_json_strict(paths.manifest)
        result["manifest"] = {"file": identity.as_dict(), "state": document.get("state")}
    if include_inputs:
        result["durable_inputs"] = fingerprint_durable_inputs(
            paths.library_root,
            paths.wallhaven_ledger,
            paths.provider_ledger,
            generation_id,
        ).as_dict()
    return result


def prepare_candidate(
    paths: PublicationPaths,
    *,
    machine_identity: Mapping[str, object],
    builder: Callable[[Path, Path, Path, Path], Mapping[str, object]],
    verifier: VerificationCallable,
    generation_id: str | None = None,
    hooks: PublicationHooks | None = None,
) -> PrepareOutcome:
    """Build and verify a new candidate without touching the canonical DB set."""

    active = hooks or PublicationHooks()
    generation = generation_id or uuid.uuid4().hex
    _validate_artifact_token(generation, "candidate generation_id")
    for label, target in (
        ("candidate", paths.candidate_database),
        ("manifest", paths.manifest),
    ):
        if Path(target).exists():
            raise ArtifactCollisionError(f"{label} output already exists: {target}")
    require_distinct_paths(
        {
            "canonical_database": paths.canonical_database,
            "candidate_database": paths.candidate_database,
            "sibling_database": paths.sibling_database,
        }
    )
    before_inputs = fingerprint_durable_inputs(
        paths.library_root,
        paths.wallhaven_ledger,
        paths.provider_ledger,
        generation,
        now=active.now(),
    )
    build_stats = dict(
        builder(
            paths.candidate_database,
            paths.library_root,
            paths.wallhaven_ledger,
            paths.provider_ledger,
        )
    )
    active.checkpoint("candidate-built")
    # SQLite may leave a permitted empty WAL after a clean builder close.  The
    # candidate is owned by this operation, so normalize those sidecars before
    # freezing its identity rather than treating absent and empty WAL states as
    # different publication content later.
    cleanup_owned_sqlite_sidecars(paths.candidate_database, hooks=active)
    candidate = fingerprint_database_set(
        paths.candidate_database, require_closed=True, require_checkpointed=True
    )
    sqlite_identity = read_sqlite_identity(
        paths.candidate_database, candidate, require_schema4=True
    )
    cleanup_owned_sqlite_sidecars(paths.candidate_database, hooks=active)
    candidate = fingerprint_database_set(
        paths.candidate_database, require_closed=True, require_checkpointed=True
    )
    after_inputs = fingerprint_durable_inputs(
        paths.library_root,
        paths.wallhaven_ledger,
        paths.provider_ledger,
        generation,
        now=active.now(),
    )
    compare_durable_inputs(before_inputs.as_dict(), after_inputs)
    built_at = utc_now_text(active.now())
    built_manifest = _candidate_built_manifest(
        paths=paths,
        machine_identity=machine_identity,
        generation_id=generation,
        built_at=built_at,
        candidate=candidate,
        sqlite_identity=sqlite_identity,
        durable_inputs=before_inputs,
    )
    validate_manifest_document(
        built_manifest,
        expected_paths=paths.explicit_mapping(),
        manifest_path=paths.manifest,
        check_current=True,
    )
    manifest_identity = write_json_create_new(paths.manifest, built_manifest, hooks=active)
    assert manifest_identity.sha256 is not None
    attempt_id = uuid.uuid4().hex
    report_path = paths.verification_report_root / f"{generation}.initial.{attempt_id}.report.json"
    require_descendant(report_path, paths.verification_report_root)
    report = run_exhaustive_verification(
        verifier,
        paths.candidate_database,
        paths.library_root,
        generation_id=generation,
        database_sha256=str(candidate.main.sha256),
        durable_inputs_sha256=before_inputs.aggregate_sha256,
        verified_under_hold=False,
        now=active.now(),
    )
    cleanup_owned_sqlite_sidecars(paths.candidate_database, hooks=active)
    candidate_after_verify = fingerprint_database_set(
        paths.candidate_database, require_closed=True, require_checkpointed=True
    )
    if not _database_content_matches(candidate_after_verify, candidate):
        raise SemanticValidationError("candidate changed during exhaustive verification")
    verification = write_verification_report(report_path, report, hooks=active)
    if report["ok"] is not True:
        failed_at = utc_now_text(active.now())
        failure_archive = paths.manifest.with_name(
            f"{paths.manifest.stem}.{generation}.candidate-built-failed.{attempt_id}.json"
        )

        def build_blocked(_previous: FileIdentity) -> Mapping[str, object]:
            blocked = dict(built_manifest)
            blocked.update(
                {
                    "updated_at": failed_at,
                    "last_failed_verification": verification,
                    "result": {
                        "status": "blocked",
                        "release_eligible": False,
                        "listener_restore_required": False,
                        "issue_taxonomy": verification["issue_taxonomy"],
                        "terminal_at": failed_at,
                        "error_code": "candidate-verification-blocked",
                        "error_message": (
                            f"Candidate exhaustive verification reported "
                            f"{report['issue_count']} issue(s)."
                        ),
                    },
                }
            )
            validate_manifest_document(
                blocked,
                expected_paths=paths.explicit_mapping(),
                manifest_path=paths.manifest,
                check_current=True,
            )
            return blocked

        transition_json_compare_and_swap(
            paths.manifest,
            expected_sha256=str(manifest_identity.sha256),
            archive_path=failure_archive,
            build_document=build_blocked,
            hooks=active,
        )
        raise SemanticValidationError(
            f"candidate exhaustive verification reported {report['issue_count']} issue(s)",
            code="candidate-verification-blocked",
        )
    archive_path = paths.manifest.with_name(
        f"{paths.manifest.stem}.{generation}.candidate-built.{attempt_id}.json"
    )

    def build_verified(previous: FileIdentity) -> Mapping[str, object]:
        verified_at = utc_now_text(active.now())
        next_document = dict(built_manifest)
        next_document.update(
            {
                "state": "candidate-verified",
                "state_transition": {
                    "from_state": "candidate-built",
                    "entered_at": verified_at,
                    "previous_manifest": previous.as_dict(),
                },
                "updated_at": verified_at,
                "verification": verification,
            }
        )
        validate_manifest_document(
            next_document,
            expected_paths=paths.explicit_mapping(),
            manifest_path=paths.manifest,
            check_current=True,
        )
        return next_document

    transitioned, verified_document = transition_json_compare_and_swap(
        paths.manifest,
        expected_sha256=str(manifest_identity.sha256),
        archive_path=archive_path,
        build_document=build_verified,
        hooks=active,
    )
    assert transitioned.sha256 is not None
    validated = validate_manifest_file(
        paths.manifest,
        expected_paths=paths.explicit_mapping(),
        check_current=True,
    )
    return PrepareOutcome(
        generation,
        validated,
        candidate,
        sqlite_identity,
        before_inputs,
        dict(verification),
        build_stats,
    )


def _assert_current_hold_evidence(
    maintenance: Mapping[str, object],
    *,
    paths: PublicationPaths,
    now: datetime,
) -> None:
    schema = load_manifest_schema()
    maintenance_schema = _resolve_schema_pointer(schema, "#/$defs/maintenance")
    validate_json_schema_subset(
        maintenance,
        maintenance_schema,
        root_schema=schema,
        location="$.maintenance",
    )
    hold = maintenance.get("hold")
    task = maintenance.get("scheduled_task")
    if not isinstance(hold, dict) or not isinstance(task, dict):
        raise SemanticValidationError("maintenance evidence has no hold")
    maximum_age = int(maintenance.get("maximum_evidence_age_seconds", 0))
    if maximum_age != 300:
        raise SemanticValidationError("maintenance evidence age limit must be 300 seconds")
    _validate_external_hold(
        hold,
        task,
        hold_path=paths.hold_path,
        queue_state_path=paths.queue_state,
        now=now,
        maximum_age_seconds=maximum_age,
    )
    window_started = _utc_instant(maintenance.get("window_started_at"), "maintenance window start")
    window_ended = _utc_instant(maintenance.get("window_ended_at"), "maintenance window end")
    verification_started = _utc_instant(
        maintenance.get("verification_started_at"), "maintenance verification start"
    )
    verification_completed = _utc_instant(
        maintenance.get("verification_completed_at"), "maintenance verification completion"
    )
    evidence_checked = _utc_instant(
        maintenance.get("evidence_checked_at"), "maintenance evidence check"
    )
    if not (
        window_started <= window_ended
        and window_ended <= verification_started <= verification_completed <= evidence_checked
    ):
        raise SemanticValidationError("maintenance timestamps are not monotonic")
    _assert_fresh_instant(evidence_checked, now, maximum_age, "maintenance evidence")
    actual_window = float(maintenance.get("actual_window_seconds", 0.0))
    if actual_window < 30.0 or (window_ended - window_started).total_seconds() < 30.0:
        raise SemanticValidationError("maintenance observation window is shorter than 30 seconds")
    for required_true in (
        "fingerprints_stable",
        "durable_inputs_current",
        "canonical_wal_checkpointed",
        "canonical_shm_handle_free",
    ):
        if maintenance.get(required_true) is not True:
            raise SemanticValidationError(f"maintenance proof is false: {required_true}")
    writers = maintenance.get("writer_samples")
    if not isinstance(writers, list) or len(writers) < 2:
        raise SemanticValidationError("writer observation window is incomplete")
    for sample in writers:
        if not isinstance(sample, dict) or sample.get("downloader_descendant_count") != 0 or sample.get("index_writer_count") != 0 or sample.get("process_ids") != []:
            raise SemanticValidationError("writer observation did not prove a zero-writer window")
    writer_sequences = [int(sample["sequence"]) for sample in writers]
    writer_elapsed = [float(sample["elapsed_seconds"]) for sample in writers]
    if writer_sequences != list(range(writer_sequences[0], writer_sequences[0] + len(writers))):
        raise SemanticValidationError("writer sample sequences are not contiguous")
    if any(right <= left for left, right in zip(writer_elapsed, writer_elapsed[1:])):
        raise SemanticValidationError("writer sample elapsed time is not increasing")
    if writer_elapsed[-1] - writer_elapsed[0] < 30.0:
        raise SemanticValidationError("writer observation interval is shorter than 30 seconds")
    writer_times = [
        _utc_instant(sample.get("sampled_at"), "writer sample")
        for sample in writers
        if isinstance(sample, dict)
    ]
    if len(writer_times) != len(writers) or any(
        right <= left for left, right in zip(writer_times, writer_times[1:])
    ):
        raise SemanticValidationError("writer sample timestamps are not increasing")
    if writer_times[0] < window_started or writer_times[-1] > window_ended:
        raise SemanticValidationError("writer samples fall outside the maintenance evidence window")
    settled_samples = maintenance.get("settled_samples")
    if not isinstance(settled_samples, list) or not settled_samples:
        raise SemanticValidationError("settled database evidence is missing")
    settled_times = [
        _utc_instant(sample.get("sampled_at"), "settled sample")
        for sample in settled_samples
        if isinstance(sample, Mapping)
    ]
    if len(settled_times) != len(settled_samples) or not (
        window_started <= settled_times[0]
        and settled_times[-1] <= window_ended
    ):
        raise SemanticValidationError(
            "settled samples fall outside the maintenance evidence window"
        )
    settled_elapsed = [
        float(sample.get("elapsed_seconds", -1.0))
        for sample in settled_samples
        if isinstance(sample, Mapping)
    ]
    if len(settled_elapsed) != len(settled_samples) or abs(
        (settled_elapsed[-1] - settled_elapsed[0]) - actual_window
    ) > 0.001:
        raise SemanticValidationError(
            "declared maintenance window differs from settled sample timing"
        )
    listener = maintenance.get("cutover_listener")
    if not isinstance(listener, dict):
        raise SemanticValidationError("cutover listener evidence is missing")
    before = listener.get("before_snapshot")
    process = listener.get("before_process")
    after = listener.get("after_snapshot")
    if not isinstance(before, dict) or not isinstance(process, dict) or not isinstance(after, dict):
        raise SemanticValidationError("cutover listener evidence is incomplete")
    before_bindings = before.get("bindings")
    if not isinstance(before_bindings, list) or before.get("listen_count") != len(before_bindings):
        raise SemanticValidationError("pre-cutover listener count does not match its bindings")
    binding_keys: set[tuple[object, object, object]] = set()
    for binding in before_bindings:
        if not isinstance(binding, dict) or binding.get("pid") != process.get("pid"):
            raise SemanticValidationError("pre-cutover binding is not owned by the captured process")
        key = (binding.get("address"), binding.get("port"), binding.get("pid"))
        if key in binding_keys:
            raise SemanticValidationError("pre-cutover listener bindings are duplicated")
        binding_keys.add(key)
    if after.get("listen_count") != 0 or after.get("bindings") != []:
        raise SemanticValidationError("port 8090 has not been proven stopped")
    stopped_at = _utc_instant(listener.get("stopped_acknowledged_at"), "listener stop acknowledgement")
    before_at = _utc_instant(before.get("observed_at"), "listener before snapshot")
    process_at = _utc_instant(process.get("observed_at"), "listener process observation")
    after_at = _utc_instant(after.get("observed_at"), "listener stopped snapshot")
    if not before_at <= process_at <= after_at <= stopped_at <= evidence_checked:
        raise SemanticValidationError("listener stop acknowledgement timing is invalid")
    for instant, label in (
        (before_at, "listener-before evidence"),
        (process_at, "listener-process evidence"),
        (after_at, "listener-after evidence"),
        (stopped_at, "listener acknowledgement"),
    ):
        _assert_fresh_instant(instant, now, maximum_age, label)
    _assert_file_identity(listener["recovery_runbook"], allow_absent=False)  # type: ignore[arg-type]
    expected_listener_ack = canonical_sha256(
        {
            key: listener[key]
            for key in (
                "before_snapshot",
                "before_process",
                "after_snapshot",
                "stopped_acknowledged_at",
                "stopped_acknowledged_by",
                "recovery_runbook",
                "recovery_step",
                "external_recovery_owner",
                "restart_automatic",
            )
        }
    )
    if listener.get("stopped_acknowledgement_sha256") != expected_listener_ack:
        raise SemanticValidationError("listener stop acknowledgement digest is invalid")
    smoke = maintenance.get("smoke_port")
    if not isinstance(smoke, dict) or smoke.get("port") != 8091 or smoke.get("listen_count") != 0:
        raise SemanticValidationError("port 8091 is not proven free")
    _assert_fresh_instant(
        _utc_instant(smoke.get("observed_at"), "smoke-port observation"),
        now,
        maximum_age,
        "smoke-port evidence",
    )


def _utc_instant(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise SemanticValidationError(f"{label} timestamp is missing")
    validate_utc_timestamp(value)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _assert_fresh_instant(
    observed: datetime,
    now: datetime,
    maximum_age_seconds: int,
    label: str,
) -> None:
    current = now.astimezone(timezone.utc)
    if observed > current:
        raise SemanticValidationError(f"{label} is future-dated")
    if (current - observed).total_seconds() > maximum_age_seconds:
        raise SemanticValidationError(f"{label} is stale")


def hold_acknowledgement_sha256(
    hold: Mapping[str, object],
    scheduled_task: Mapping[str, object],
) -> str:
    """Return the contract-v1 digest for external pause acknowledgement."""

    return canonical_sha256(
        {
            "hold_format_version": 1,
            "owner": hold.get("owner"),
            "token_id": hold.get("token_id"),
            "acquired_at": hold.get("acquired_at"),
            "expires_at": hold.get("expires_at"),
            "task_acknowledged": hold.get("task_acknowledged"),
            "hold_path": hold.get("hold_file", {}).get("path")  # type: ignore[union-attr]
            if isinstance(hold.get("hold_file"), Mapping)
            else None,
            "pause_path": hold.get("pause_file", {}).get("path")  # type: ignore[union-attr]
            if isinstance(hold.get("pause_file"), Mapping)
            else None,
            "pause_file_sha256": hold.get("pause_file", {}).get("sha256")  # type: ignore[union-attr]
            if isinstance(hold.get("pause_file"), Mapping)
            else None,
            "queue_state_path": hold.get("queue_state", {}).get("path")  # type: ignore[union-attr]
            if isinstance(hold.get("queue_state"), Mapping)
            else None,
            "queue_state_sha256": hold.get("queue_state", {}).get("sha256")  # type: ignore[union-attr]
            if isinstance(hold.get("queue_state"), Mapping)
            else None,
            "queue_state_schema_version": hold.get("queue_state_schema_version"),
            "task_path": scheduled_task.get("path"),
            "task_name": scheduled_task.get("name"),
            "task_definition_sha256": scheduled_task.get("definition_sha256"),
            "task_observed_at": scheduled_task.get("observed_at"),
            "acknowledged_at": hold.get("acknowledged_at"),
        }
    )


def _validate_external_hold(
    hold: Mapping[str, object],
    scheduled_task: Mapping[str, object],
    *,
    hold_path: Path,
    queue_state_path: Path,
    now: datetime,
    maximum_age_seconds: int,
) -> None:
    if hold.get("externally_owned") is not True or hold.get("publisher_may_release") is not False:
        raise SemanticValidationError("publication hold ownership is invalid")
    if hold.get("task_acknowledged") is not True:
        raise SemanticValidationError("scheduled workflow did not acknowledge the hold")
    expected_paths = {
        "hold_file": Path(hold_path),
        "pause_file": Path(queue_state_path) / "pause.flag",
        "queue_state": Path(queue_state_path) / "state.json",
    }
    for key, expected_path in expected_paths.items():
        identity = hold.get(key)
        if not isinstance(identity, Mapping):
            raise SemanticValidationError(f"hold {key} identity is missing")
        observed = _assert_file_identity(identity, allow_absent=False)
        if path_key(observed.path) != path_key(expected_path):
            raise SemanticValidationError(f"hold {key} path is invalid")
    hold_file = hold["hold_file"]
    assert isinstance(hold_file, Mapping)
    if hold.get("token_sha256") != hold_file.get("sha256"):
        raise SemanticValidationError("hold token hash does not match hold-file bytes")
    token_document = load_json_strict(Path(str(hold_file["path"])))
    expected_token_document = {
        "hold_format_version": 1,
        "owner": hold.get("owner"),
        "token_id": hold.get("token_id"),
        "acquired_at": hold.get("acquired_at"),
        "expires_at": hold.get("expires_at"),
        "acknowledged_at": hold.get("acknowledged_at"),
        "acknowledgement_sha256": hold.get("acknowledgement_sha256"),
        "task_acknowledged": hold.get("task_acknowledged"),
        "hold_path": str(hold_file.get("path")),
        "pause_path": str(hold["pause_file"].get("path")),  # type: ignore[index,union-attr]
        "pause_file_sha256": hold["pause_file"].get("sha256"),  # type: ignore[index,union-attr]
        "queue_state_path": str(hold["queue_state"].get("path")),  # type: ignore[index,union-attr]
        "queue_state_sha256": hold["queue_state"].get("sha256"),  # type: ignore[index,union-attr]
        "queue_state_schema_version": hold.get("queue_state_schema_version"),
        "task_path": scheduled_task.get("path"),
        "task_name": scheduled_task.get("name"),
        "task_definition_sha256": scheduled_task.get("definition_sha256"),
        "task_observed_at": scheduled_task.get("observed_at"),
    }
    if not _json_equal(token_document, expected_token_document):
        raise SemanticValidationError("hold token document is not the closed contract-v1 acknowledgement")
    queue_identity = hold["queue_state"]
    assert isinstance(queue_identity, Mapping)
    queue_document = load_json_strict(Path(str(queue_identity["path"])))
    if queue_document.get("schemaVersion") != 1 or hold.get("queue_state_schema_version") != 1:
        raise SemanticValidationError("queue state schema version is not 1")
    acquired = _utc_instant(hold.get("acquired_at"), "hold acquisition")
    acknowledged = _utc_instant(hold.get("acknowledged_at"), "hold acknowledgement")
    expiry = _utc_instant(hold.get("expires_at"), "hold expiry")
    current = now.astimezone(timezone.utc)
    if not acquired <= acknowledged < expiry or expiry <= current:
        raise SemanticValidationError("publication hold timing is invalid or expired")
    last_worker = _utc_instant(queue_document.get("lastWorkerAt"), "queue pause acknowledgement")
    queue_updated = _utc_instant(queue_document.get("updatedAt"), "queue state update")
    if (
        queue_document.get("lastMessage")
        != "Worker paused by dashboard (pause.flag present)."
        or not acquired <= last_worker <= queue_updated <= acknowledged
    ):
        raise SemanticValidationError("queue state does not acknowledge the pause after acquisition")
    _assert_fresh_instant(
        queue_updated,
        now,
        maximum_age_seconds,
        "queue state update",
    )
    jobs = queue_document.get("jobs")
    if not isinstance(jobs, list) or any(
        not isinstance(job, Mapping)
        or not isinstance(job.get("status"), str)
        or not str(job.get("status")).strip()
        or str(job.get("status", "")).casefold() in {"running", "downloading"}
        for job in jobs
    ):
        raise SemanticValidationError("queue state contains an active or malformed job set")
    _assert_fresh_instant(acknowledged, now, maximum_age_seconds, "hold acknowledgement")
    if scheduled_task.get("path") != "\\" or scheduled_task.get("name") != "Wallpaper Download Queue":
        raise SemanticValidationError("scheduled-task authority is invalid")
    task_observed = _utc_instant(scheduled_task.get("observed_at"), "scheduled-task observation")
    task_acknowledged = _utc_instant(
        scheduled_task.get("acknowledged_at"), "scheduled-task acknowledgement"
    )
    if task_acknowledged != acknowledged or task_observed > task_acknowledged:
        raise SemanticValidationError("scheduled-task acknowledgement timing is invalid")
    _assert_fresh_instant(
        task_observed,
        now,
        maximum_age_seconds,
        "scheduled-task observation",
    )
    _assert_fresh_instant(task_acknowledged, now, maximum_age_seconds, "scheduled-task acknowledgement")
    if hold.get("acknowledgement_sha256") != hold_acknowledgement_sha256(hold, scheduled_task):
        raise SemanticValidationError("hold acknowledgement digest is invalid")


def _backup_document(
    backup: DatabaseSetIdentity,
    source: DatabaseSetIdentity,
    source_sqlite: SQLiteIdentity,
    *,
    created_at: str,
) -> dict[str, object]:
    return {
        "created_at": created_at,
        "same_volume": True,
        "collision_free": True,
        "database": backup.as_dict(),
        "source_database_sha256": source.main.sha256,
        "source_sqlite": source_sqlite.as_dict(),
    }


def _failure_output_identities(paths: PublicationPaths) -> list[dict[str, object]]:
    targets = [
        Path(paths.recovery_journal),
        *_database_member_paths(Path(paths.backup_directory) / Path(paths.canonical_database).name),
    ]
    return [fingerprint_file(path, allow_absent=True).as_dict() for path in targets]


def _reconcile_publish_failure(
    *,
    paths: PublicationPaths,
    ready_identity: FileIdentity,
    ready_document: Mapping[str, object],
    ready_archive: FileIdentity,
    expected_canonical_before: DatabaseSetIdentity,
    started_at: str,
    error: Exception,
    hooks: PublicationHooks,
) -> ValidatedManifest:
    """Record a synchronous terminal outcome after activation raised.

    Exact database restoration is decided solely from fresh bytes and the
    parsed journal.  A manifest CAS which completed before reporting an error is
    archived and reconciled as a published -> rolled-back edge.
    """

    completed_at = utc_now_text(hooks.now())
    error_code = getattr(error, "code", "activation-failed")
    error_message = _record_error_text(error)
    canonical = fingerprint_database_set(
        paths.canonical_database, require_closed=True, require_checkpointed=True
    )
    chain: JournalChain | None = None
    journal_evidence: dict[str, object] | None = None
    journal_path = Path(paths.recovery_journal)
    if journal_path.is_file():
        try:
            chain = parse_journal_chain(journal_path)
        except PublicationError:
            chain = None
        if chain is not None and chain.records:
            journal_evidence = _journal_evidence_from_chain(
                chain,
                manifest_path=paths.manifest,
                pre_activation_manifest=ready_archive,
                recovery_result=None,
            )
    backup_main = Path(paths.backup_directory) / Path(paths.canonical_database).name
    backup: DatabaseSetIdentity | None = None
    if backup_main.is_file():
        candidate_backup = fingerprint_database_set(backup_main, require_checkpointed=True)
        if _database_content_matches(candidate_backup, expected_canonical_before):
            backup = candidate_backup
    current_manifest = fingerprint_file(paths.manifest, require_nonempty=True)
    previous_manifest = ready_archive
    previous_state = "ready-to-publish"
    if current_manifest.sha256 != ready_identity.sha256:
        current_document = load_json_strict(paths.manifest)
        current_candidate = current_document.get("candidate")
        expected_candidate = ready_document.get("candidate")
        if (
            current_document.get("state") != "published"
            or not isinstance(current_candidate, dict)
            or not isinstance(expected_candidate, dict)
            or current_candidate.get("generation_id") != expected_candidate.get("generation_id")
        ):
            raise SemanticValidationError(
                "manifest changed to an unrelated document during activation",
                code="manifest-cas-conflict",
            )
        published_archive_path = paths.manifest.with_name(
            f"{paths.manifest.stem}.{expected_candidate['generation_id']}.published-failed.{uuid.uuid4().hex}.json"
        )
        previous_manifest = _write_create_new(
            published_archive_path, paths.manifest.read_bytes(), hooks
        )
        previous_state = "published"

    main_mutation_intent = bool(
        chain is not None
        and any(
            record.get("action") == "main-replace" and record.get("phase") == "intent"
            for record in chain.records
        )
    )
    replacement = dict(ready_document)
    replacement["updated_at"] = completed_at
    if not main_mutation_intent:
        if not _database_content_matches(canonical, expected_canonical_before):
            raise RollbackError("canonical changed without a recorded main-replace intent")
        canonical_sqlite = read_sqlite_identity(paths.canonical_database, canonical)
        usable_journal = (
            journal_evidence
            if chain is not None and chain.derived_status == "aborted-before-mutation"
            else None
        )
        replacement.update(
            {
                "backup": None
                if backup is None
                else _backup_document(
                    backup,
                    expected_canonical_before,
                    canonical_sqlite,
                    created_at=started_at,
                ),
                "pre_activation_failure": {
                    "phase": "pre-canonical-mutation-abort"
                    if usable_journal is not None
                    else "journal-create",
                    "failed_at": completed_at,
                    "error_code": error_code,
                    "error_message": error_message,
                    "canonical_unchanged": True,
                    "canonical_database": canonical.as_dict(),
                    "canonical_sqlite": canonical_sqlite.as_dict(),
                    "closure_kind": "publisher-abort"
                    if usable_journal is not None
                    else "synchronous-unstarted-failure",
                    "recovery_hold": None,
                    "observed_outputs": _failure_output_identities(paths),
                    "journal": usable_journal,
                },
                "activation": None,
                "post_publish": None,
                "rollback": None,
                "result": {
                    "status": "failed",
                    "release_eligible": False,
                    "listener_restore_required": True,
                    "issue_taxonomy": [],
                    "terminal_at": completed_at,
                    "error_code": error_code,
                    "error_message": error_message,
                },
            }
        )
    else:
        if backup is None or journal_evidence is None or chain is None:
            raise RollbackError("activation failure has no complete verified backup/journal evidence")
        backup_sqlite = read_sqlite_identity(backup.main.path, backup)
        backup_document = _backup_document(
            backup,
            expected_canonical_before,
            backup_sqlite,
            created_at=started_at,
        )
        if chain.derived_status == "restored" and _database_content_matches(canonical, backup):
            restored_sqlite = read_sqlite_identity(paths.canonical_database, canonical)
            if not _sqlite_identity_matches(restored_sqlite, backup_sqlite):
                raise RollbackError("restored canonical SQLite identity differs from verified backup")
            replacement.update(
                {
                    "state": "rolled-back",
                    "state_transition": {
                        "from_state": previous_state,
                        "entered_at": completed_at,
                        "previous_manifest": previous_manifest.as_dict(),
                    },
                    "backup": backup_document,
                    "pre_activation_failure": None,
                    "activation": {
                        "status": "failed",
                        "started_at": started_at,
                        "completed_at": completed_at,
                        "journal": journal_evidence,
                        "canonical_before": expected_canonical_before.as_dict(),
                        "canonical_after": None,
                        "canonical_before_sqlite": backup_sqlite.as_dict(),
                        "canonical_after_sqlite": None,
                        "main_replace_same_volume_atomic": True,
                        "multi_file_atomic_claim": False,
                        "rollback_required": True,
                        "error_code": error_code,
                        "error_message": error_message,
                    },
                    "post_publish": None,
                    "rollback": {
                        "attempted_at": started_at,
                        "completed_at": completed_at,
                        "trigger": "automatic-publication-failure",
                        "succeeded": True,
                        "restored_database": canonical.as_dict(),
                        "restored_sqlite": restored_sqlite.as_dict(),
                        "restored_sha256_matches_backup": True,
                        "hold_must_remain": True,
                        "listener_restore_required": True,
                        "error": None,
                    },
                    "result": {
                        "status": "rolled-back",
                        "release_eligible": False,
                        "listener_restore_required": True,
                        "issue_taxonomy": [],
                        "terminal_at": completed_at,
                        "error_code": error_code,
                        "error_message": error_message,
                    },
                }
            )
        elif chain.derived_status == "recovery-required" and previous_state == "ready-to-publish":
            current_sqlite: SQLiteIdentity | None = None
            try:
                current_sqlite = read_sqlite_identity(paths.canonical_database, canonical)
            except (PublicationError, sqlite3.Error):
                pass
            replacement.update(
                {
                    "backup": backup_document,
                    "pre_activation_failure": None,
                    "activation": {
                        "status": "failed",
                        "started_at": started_at,
                        "completed_at": completed_at,
                        "journal": journal_evidence,
                        "canonical_before": expected_canonical_before.as_dict(),
                        "canonical_after": canonical.as_dict(),
                        "canonical_before_sqlite": backup_sqlite.as_dict(),
                        "canonical_after_sqlite": None
                        if current_sqlite is None
                        else current_sqlite.as_dict(),
                        "main_replace_same_volume_atomic": True,
                        "multi_file_atomic_claim": False,
                        "rollback_required": True,
                        "error_code": error_code,
                        "error_message": error_message,
                    },
                    "post_publish": None,
                    "rollback": {
                        "attempted_at": started_at,
                        "completed_at": completed_at,
                        "trigger": "automatic-publication-failure",
                        "succeeded": False,
                        "restored_database": None,
                        "restored_sqlite": None,
                        "restored_sha256_matches_backup": False,
                        "hold_must_remain": True,
                        "listener_restore_required": True,
                        "error": error_message,
                    },
                    "result": {
                        "status": "failed",
                        "release_eligible": False,
                        "listener_restore_required": True,
                        "issue_taxonomy": [],
                        "terminal_at": completed_at,
                        "error_code": error_code,
                        "error_message": error_message,
                    },
                }
            )
        else:
            raise RollbackError(
                f"activation failure journal is not terminally reconcilable: {chain.derived_status}"
            )
    validate_manifest_document(
        replacement,
        expected_paths=paths.explicit_mapping(),
        manifest_path=paths.manifest,
        check_current=True,
    )
    reconciled_identity = replace_json_with_precreated_archive(
        paths.manifest,
        replacement,
        expected_sha256=str(current_manifest.sha256),
        archive_identity=previous_manifest,
        hooks=hooks,
    )
    assert reconciled_identity.sha256 is not None
    return ValidatedManifest(paths.manifest, reconciled_identity.sha256, replacement)


def publish_candidate(
    paths: PublicationPaths,
    *,
    runtime_evidence: Mapping[str, object],
    verifier: VerificationCallable,
    canonical_handle_free: Callable[[Path], bool],
    authorized_by: str,
    hooks: PublicationHooks | None = None,
) -> PublishOutcome:
    """Revalidate under a caller-owned hold, activate, and CAS to published."""

    active = hooks or PublicationHooks()
    current = validate_manifest_file(
        paths.manifest,
        expected_paths=paths.explicit_mapping(),
        check_current=True,
    )
    if current.document.get("state") != "candidate-verified":
        raise SemanticValidationError("publish requires a candidate-verified manifest")
    if runtime_evidence.get("apply") is not True or runtime_evidence.get("cutover_authorized") is not True:
        raise SemanticValidationError("publish requires explicit apply and cutover authorization")
    machine_identity = runtime_evidence.get("machine_identity")
    maintenance = runtime_evidence.get("maintenance")
    if not isinstance(machine_identity, dict) or not isinstance(maintenance, dict):
        raise SemanticValidationError("publish runtime evidence is incomplete")
    now = active.now()
    fresh_machine = _validate_current_machine_identity(machine_identity, now=now)
    prior_machine = current.document.get("machine_identity")
    if not isinstance(prior_machine, dict) or any(
        fresh_machine[key] != prior_machine.get(key)
        for key in ("status", "machine_id", "instance_id", "computer_name", "qualified_user", "verifier_path")
    ):
        raise SemanticValidationError("fresh machine identity differs from candidate manifest")
    _assert_current_hold_evidence(maintenance, paths=paths, now=now)
    candidate_document = current.document["candidate"]
    durable_document = current.document["durable_inputs"]
    assert isinstance(candidate_document, dict) and isinstance(durable_document, dict)
    candidate_database_document = candidate_document["database"]
    assert isinstance(candidate_database_document, dict)
    candidate_main_document = candidate_database_document["main"]
    assert isinstance(candidate_main_document, dict)
    fresh_inputs = fingerprint_durable_inputs(
        paths.library_root,
        paths.wallhaven_ledger,
        paths.provider_ledger,
        str(candidate_document["generation_id"]),
        now=now,
    )
    compare_durable_inputs(durable_document, fresh_inputs)
    settled = maintenance.get("settled_samples")
    if not isinstance(settled, list):
        raise SemanticValidationError("settled database samples are missing")
    validate_settled_samples(settled)
    for sample in settled:
        assert isinstance(sample, dict)
        if sample.get("durable_inputs_sha256") != fresh_inputs.aggregate_sha256:
            raise SemanticValidationError("settled sample input hash is stale")
        candidate_sample = sample.get("candidate")
        if not isinstance(candidate_sample, dict) or candidate_sample.get("aggregate_sha256") != candidate_database_document.get("aggregate_sha256"):
            raise SemanticValidationError("settled sample candidate identity is stale")
    final_settled = settled[-1]
    assert isinstance(final_settled, dict) and isinstance(final_settled.get("canonical"), dict)
    expected_canonical_before = _stored_database_set(final_settled["canonical"])
    observed_canonical_before = fingerprint_database_set(
        paths.canonical_database, require_closed=True, require_checkpointed=True
    )
    if not _json_equal(observed_canonical_before.as_dict(), expected_canonical_before.as_dict()):
        raise SemanticValidationError("canonical database differs from the final settled sample")

    def revalidate_candidate_authority(
        *,
        expected_manifest_sha256: str,
        maintenance_document: Mapping[str, object],
        require_canonical_before: bool,
    ) -> None:
        checked_at = active.now()
        _validate_current_machine_identity(machine_identity, now=checked_at)
        _assert_current_hold_evidence(
            maintenance_document,
            paths=paths,
            now=checked_at,
        )
        if sha256_file(paths.manifest) != expected_manifest_sha256:
            raise SemanticValidationError(
                "manifest changed during protected publication",
                code="manifest-cas-conflict",
            )
        current_inputs = fingerprint_durable_inputs(
            paths.library_root,
            paths.wallhaven_ledger,
            paths.provider_ledger,
            str(candidate_document["generation_id"]),
            now=checked_at,
        )
        compare_durable_inputs(durable_document, current_inputs)
        current_candidate = fingerprint_database_set(
            paths.candidate_database,
            require_closed=True,
            require_checkpointed=True,
        )
        if not _database_content_matches(
            current_candidate,
            _stored_database_set(candidate_database_document),
        ):
            raise SemanticValidationError(
                "candidate bytes changed during protected publication"
            )
        current_candidate_sqlite = read_sqlite_identity(
            paths.candidate_database,
            current_candidate,
            require_schema4=True,
        )
        candidate_sqlite_document = candidate_document.get("sqlite")
        if not isinstance(candidate_sqlite_document, dict) or not _sqlite_identity_matches(
            current_candidate_sqlite,
            _stored_sqlite_identity(candidate_sqlite_document),
        ):
            raise SemanticValidationError(
                "candidate SQLite identity changed during protected publication"
            )
        if require_canonical_before:
            current_canonical = fingerprint_database_set(
                paths.canonical_database,
                require_closed=True,
                require_checkpointed=True,
            )
            if not _json_equal(
                current_canonical.as_dict(),
                expected_canonical_before.as_dict(),
            ):
                raise SemanticValidationError(
                    "canonical database changed before ready-state publication"
                )
        if not canonical_handle_free(paths.canonical_database):
            raise SemanticValidationError("canonical handle-free proof is not current")

    attempt_id = uuid.uuid4().hex
    under_hold_report_path = paths.verification_report_root / (
        f"{candidate_document['generation_id']}.under-hold.{attempt_id}.report.json"
    )
    require_descendant(under_hold_report_path, paths.verification_report_root)
    under_hold_started_at = active.now()
    under_hold_report = run_exhaustive_verification(
        verifier,
        paths.candidate_database,
        paths.library_root,
        generation_id=str(candidate_document["generation_id"]),
        database_sha256=str(candidate_main_document["sha256"]),
        durable_inputs_sha256=fresh_inputs.aggregate_sha256,
        verified_under_hold=True,
        now=active.now(),
    )
    under_hold_verification = write_verification_report(
        under_hold_report_path, under_hold_report, hooks=active
    )
    # SQLite read-only verification can still create empty WAL/SHM sidecars.
    # The candidate is publication-owned, so normalize those transient files
    # before freezing the ready/failed receipt and prove the main DB unchanged
    # in the authority revalidation below.
    cleanup_owned_sqlite_sidecars(paths.candidate_database, hooks=active)
    under_hold_completed_at = active.now()
    if under_hold_report["ok"] is not True:
        revalidate_candidate_authority(
            expected_manifest_sha256=current.sha256,
            maintenance_document=maintenance,
            require_canonical_before=True,
        )
        failed_at = utc_now_text(active.now())
        failed_document = dict(current.document)
        failed_document.update(
            {
                # Failure evidence does not advance the manifest state or alter
                # its established candidate-built -> candidate-verified edge.
                "updated_at": failed_at,
                "machine_identity": fresh_machine,
                "last_failed_verification": under_hold_verification,
                "result": {
                    "status": "blocked",
                    "release_eligible": False,
                    "listener_restore_required": False,
                    "issue_taxonomy": list(under_hold_report["issue_taxonomy"]),
                    "terminal_at": failed_at,
                    "error_code": "under-hold-verification-blocked",
                    "error_message": (
                        "fresh under-hold exhaustive verification reported "
                        f"{under_hold_report['issue_count']} issue(s)"
                    ),
                },
            }
        )
        validate_manifest_document(
            failed_document,
            expected_paths=paths.explicit_mapping(),
            manifest_path=paths.manifest,
            check_current=True,
        )
        failure_archive = paths.manifest.with_name(
            f"{paths.manifest.stem}.{candidate_document['generation_id']}."
            f"candidate-verified.failed-under-hold.{attempt_id}.json"
        )
        replace_json_compare_and_swap(
            paths.manifest,
            failed_document,
            expected_sha256=current.sha256,
            archive_path=failure_archive,
            hooks=active,
        )
        raise SemanticValidationError(
            "fresh under-hold verification failed",
            code="under-hold-verification-blocked",
        )
    ready_maintenance = dict(maintenance)
    ready_maintenance.update(
        {
            "verification_started_at": utc_now_text(under_hold_started_at),
            "verification_completed_at": utc_now_text(under_hold_completed_at),
            "evidence_checked_at": utc_now_text(active.now()),
        }
    )
    revalidate_candidate_authority(
        expected_manifest_sha256=current.sha256,
        maintenance_document=ready_maintenance,
        require_canonical_before=True,
    )
    ready_maintenance["evidence_checked_at"] = utc_now_text(active.now())
    _assert_current_hold_evidence(
        ready_maintenance,
        paths=paths,
        now=active.now(),
    )
    verified_archive_path = paths.manifest.with_name(
        f"{paths.manifest.stem}.{candidate_document['generation_id']}.candidate-verified.{attempt_id}.json"
    )

    def build_ready(previous: FileIdentity) -> Mapping[str, object]:
        entered = utc_now_text(active.now())
        ready = dict(current.document)
        ready.update(
            {
                "state": "ready-to-publish",
                "state_transition": {
                    "from_state": "candidate-verified",
                    "entered_at": entered,
                    "previous_manifest": previous.as_dict(),
                },
                "updated_at": entered,
                "machine_identity": fresh_machine,
                "verification": under_hold_verification,
                "last_failed_verification": None,
                "maintenance": dict(ready_maintenance),
                "authorization": {
                    "apply": True,
                    "cutover": True,
                    "authorized_at": entered,
                    "authorized_by": authorized_by,
                },
                "result": _in_progress_result(listener_restore_required=True),
            }
        )
        validate_manifest_document(
            ready,
            expected_paths=paths.explicit_mapping(),
            manifest_path=paths.manifest,
            check_current=True,
        )
        ready_build_holder["document"] = ready
        return ready

    ready_build_holder: dict[str, object] = {}
    ready_commit_interrupt: BaseException | None = None
    try:
        ready_identity, ready_document = transition_json_compare_and_swap(
            paths.manifest,
            expected_sha256=current.sha256,
            archive_path=verified_archive_path,
            build_document=build_ready,
            hooks=active,
        )
    except BaseException as ready_commit_error:
        intended_ready = ready_build_holder.get("document")
        observed_manifest = fingerprint_file(paths.manifest, require_nonempty=True)
        intended_sha = (
            hashlib.sha256(canonical_json_bytes(intended_ready) + b"\n").hexdigest()
            if isinstance(intended_ready, Mapping)
            else None
        )
        if intended_sha is not None and observed_manifest.sha256 == intended_sha:
            # The replace committed before its durability call/interrupt was
            # reported. Reflush and adopt only the exact validated ready bytes.
            try:
                active.fsync_directory(paths.manifest.parent)
            except BaseException:
                _flush_directory(paths.manifest.parent)
            ready_identity = fingerprint_file(paths.manifest, require_nonempty=True)
            if ready_identity.sha256 != intended_sha:
                raise ActivationError(
                    "ready manifest changed during commit revalidation"
                ) from ready_commit_error
            ready_document = dict(intended_ready)
            if isinstance(ready_commit_error, (KeyboardInterrupt, SystemExit)):
                ready_commit_interrupt = ready_commit_error
        elif observed_manifest.sha256 == current.sha256:
            # The advancing CAS did not commit. Record a terminal ready receipt
            # so the stopped-listener obligation remains explicit.
            revalidate_candidate_authority(
                expected_manifest_sha256=current.sha256,
                maintenance_document=ready_maintenance,
                require_canonical_before=True,
            )
            failed_at = utc_now_text(active.now())
            current_canonical = fingerprint_database_set(
                paths.canonical_database,
                require_closed=True,
                require_checkpointed=True,
            )
            current_canonical_sqlite = read_sqlite_identity(
                paths.canonical_database,
                current_canonical,
            )

            def build_failed_ready(previous: FileIdentity) -> Mapping[str, object]:
                failed_ready = dict(current.document)
                failed_ready.update(
                    {
                        "state": "ready-to-publish",
                        "state_transition": {
                            "from_state": "candidate-verified",
                            "entered_at": failed_at,
                            "previous_manifest": previous.as_dict(),
                        },
                        "updated_at": failed_at,
                        "machine_identity": fresh_machine,
                        "verification": under_hold_verification,
                        "last_failed_verification": None,
                        "maintenance": dict(ready_maintenance),
                        "authorization": {
                            "apply": True,
                            "cutover": True,
                            "authorized_at": failed_at,
                            "authorized_by": authorized_by,
                        },
                        "backup": None,
                        "pre_activation_failure": {
                            "phase": "journal-create",
                            "failed_at": failed_at,
                            "error_code": getattr(
                                ready_commit_error,
                                "code",
                                "ready-manifest-commit-failed",
                            ),
                            "error_message": _record_error_text(
                                ready_commit_error
                            ),
                            "canonical_unchanged": True,
                            "canonical_database": current_canonical.as_dict(),
                            "canonical_sqlite": current_canonical_sqlite.as_dict(),
                            "closure_kind": "synchronous-unstarted-failure",
                            "recovery_hold": None,
                            "observed_outputs": [],
                            "journal": None,
                        },
                        "activation": None,
                        "post_publish": None,
                        "rollback": None,
                        "result": {
                            "status": "failed",
                            "release_eligible": False,
                            "listener_restore_required": True,
                            "issue_taxonomy": [],
                            "terminal_at": failed_at,
                            "error_code": getattr(
                                ready_commit_error,
                                "code",
                                "ready-manifest-commit-failed",
                            ),
                            "error_message": _record_error_text(
                                ready_commit_error
                            ),
                        },
                    }
                )
                validate_manifest_document(
                    failed_ready,
                    expected_paths=paths.explicit_mapping(),
                    manifest_path=paths.manifest,
                    check_current=True,
                )
                return failed_ready

            failed_archive_path = paths.manifest.with_name(
                f"{paths.manifest.stem}.{candidate_document['generation_id']}."
                f"candidate-verified.ready-failed.{attempt_id}.json"
            )
            transition_json_compare_and_swap(
                paths.manifest,
                expected_sha256=current.sha256,
                archive_path=failed_archive_path,
                build_document=build_failed_ready,
                hooks=active,
            )
            raise ready_commit_error
        else:
            raise SemanticValidationError(
                "manifest changed ambiguously during ready-state commit",
                code="manifest-cas-conflict",
            ) from ready_commit_error
    assert ready_identity.sha256 is not None
    ready_archive_path = paths.manifest.with_name(
        f"{paths.manifest.stem}.{candidate_document['generation_id']}.ready-to-publish.{attempt_id}.json"
    )
    try:
        ready_archive = _write_create_new(
            ready_archive_path,
            paths.manifest.read_bytes(),
            active,
        )
        if ready_archive.sha256 != ready_identity.sha256:
            raise SemanticValidationError(
                "ready manifest archive does not match current manifest"
            )
    except BaseException as ready_archive_error:
        fallback_archive_path = paths.manifest.with_name(
            f"{paths.manifest.stem}.{candidate_document['generation_id']}."
            f"ready-to-publish.failure-anchor.{uuid.uuid4().hex}.json"
        )
        ready_archive = _write_create_new(
            fallback_archive_path,
            paths.manifest.read_bytes(),
            active,
        )
        _reconcile_publish_failure(
            paths=paths,
            ready_identity=ready_identity,
            ready_document=ready_document,
            ready_archive=ready_archive,
            expected_canonical_before=expected_canonical_before,
            started_at=utc_now_text(active.now()),
            error=ActivationError(
                f"ready manifest archive failed: {_record_error_text(ready_archive_error)}"
            ),
            hooks=active,
        )
        raise ready_archive_error
    if ready_commit_interrupt is not None:
        _reconcile_publish_failure(
            paths=paths,
            ready_identity=ready_identity,
            ready_document=ready_document,
            ready_archive=ready_archive,
            expected_canonical_before=expected_canonical_before,
            started_at=utc_now_text(active.now()),
            error=ActivationError(
                f"ready manifest commit interrupted: {_record_error_text(ready_commit_interrupt)}"
            ),
            hooks=active,
        )
        raise ready_commit_interrupt
    canonical_report_holder: dict[str, object] = {}

    def post_verify(canonical_path: Path) -> None:
        observed = fingerprint_database_set(
            canonical_path, require_closed=True, require_checkpointed=True
        )
        report_path = paths.verification_report_root / (
            f"{candidate_document['generation_id']}.canonical.{attempt_id}.report.json"
        )
        require_descendant(report_path, paths.verification_report_root)
        report = run_exhaustive_verification(
            verifier,
            canonical_path,
            paths.library_root,
            generation_id=str(candidate_document["generation_id"]),
            database_sha256=str(observed.main.sha256),
            durable_inputs_sha256=fresh_inputs.aggregate_sha256,
            verified_under_hold=True,
            now=active.now(),
        )
        evidence = write_verification_report(report_path, report, hooks=active)
        if report["ok"] is not True:
            raise ActivationError("post-publication exhaustive verification failed")
        canonical_report_holder.update(evidence)

    def revalidate_publication_authority() -> None:
        revalidate_candidate_authority(
            expected_manifest_sha256=str(ready_identity.sha256),
            maintenance_document=ready_maintenance,
            require_canonical_before=False,
        )

    started_at = utc_now_text(active.now())
    published_holder: dict[str, object] = {}

    def finalize_publication(activation_outcome: ActivationOutcome) -> None:
        completed_at = utc_now_text(active.now())
        published_document = dict(ready_document)
        published_document.update(
            {
                "state": "published",
                "state_transition": {
                    "from_state": "ready-to-publish",
                    "entered_at": completed_at,
                    "previous_manifest": ready_archive.as_dict(),
                },
                "updated_at": completed_at,
                "backup": _backup_document(
                    activation_outcome.backup,
                    activation_outcome.canonical_before,
                    activation_outcome.canonical_before_sqlite,
                    created_at=started_at,
                ),
                "activation": {
                    "status": "verified",
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "journal": activation_outcome.journal_evidence,
                    "canonical_before": activation_outcome.canonical_before.as_dict(),
                    "canonical_after": activation_outcome.canonical.as_dict(),
                    "canonical_before_sqlite": activation_outcome.canonical_before_sqlite.as_dict(),
                    "canonical_after_sqlite": activation_outcome.sqlite.as_dict(),
                    "main_replace_same_volume_atomic": True,
                    "multi_file_atomic_claim": False,
                    "rollback_required": False,
                    "error_code": None,
                    "error_message": None,
                },
                "post_publish": {
                    "verified_at": completed_at,
                    "canonical_database_sha256": activation_outcome.canonical.main.sha256,
                    "candidate_database_sha256": activation_outcome.candidate.main.sha256,
                    "bytes_match_candidate": True,
                    "canonical_sqlite": activation_outcome.sqlite.as_dict(),
                    "verification": dict(canonical_report_holder),
                },
                "result": {
                    "status": "succeeded",
                    "release_eligible": True,
                    "listener_restore_required": False,
                    "issue_taxonomy": [],
                    "terminal_at": completed_at,
                    "error_code": None,
                    "error_message": None,
                },
            }
        )
        # Keep the intended terminal bytes available if replace succeeds but its
        # durability call reports an error; the activation boundary will restore
        # the database and reconcile the now-published manifest backward.
        published_holder["document"] = published_document
        validate_manifest_document(
            published_document,
            expected_paths=paths.explicit_mapping(),
            manifest_path=paths.manifest,
            check_current=True,
        )
        revalidate_publication_authority()
        expected_published_sha = hashlib.sha256(
            canonical_json_bytes(published_document) + b"\n"
        ).hexdigest()
        published_holder["expected_sha256"] = expected_published_sha
        try:
            published_identity = replace_json_with_precreated_archive(
                paths.manifest,
                published_document,
                expected_sha256=str(ready_identity.sha256),
                archive_identity=ready_archive,
                hooks=active,
            )
        except BaseException as commit_error:
            # os.replace may have committed before an injected interruption or
            # directory-flush error was reported.  Once the exact published
            # receipt exists, appending rollback records to its embedded sealed
            # journal would invalidate that receipt.  Reflush and prove the
            # exact terminal bytes, then preserve interrupt semantics outside
            # the activation rollback boundary.
            observed_manifest = fingerprint_file(paths.manifest, require_nonempty=True)
            if observed_manifest.sha256 != expected_published_sha:
                raise
            try:
                active.fsync_directory(paths.manifest.parent)
            except BaseException:
                _flush_directory(paths.manifest.parent)
            published_identity = fingerprint_file(paths.manifest, require_nonempty=True)
            if published_identity.sha256 != expected_published_sha:
                raise ActivationError("published manifest changed during commit revalidation")
            if isinstance(commit_error, (KeyboardInterrupt, SystemExit)):
                published_holder["post_commit_interrupt"] = commit_error
        assert published_identity.sha256 is not None
        published_holder["manifest"] = ValidatedManifest(
            paths.manifest, published_identity.sha256, published_document
        )

    def published_manifest_committed() -> bool:
        """Freshly observe the exact terminal receipt before any rollback write."""

        expected_sha = published_holder.get("expected_sha256")
        return isinstance(expected_sha, str) and sha256_file(paths.manifest) == expected_sha

    try:
        activation_outcome = activate_database(
            canonical_database=paths.canonical_database,
            candidate_database=paths.candidate_database,
            backup_directory=paths.backup_directory,
            recovery_journal=paths.recovery_journal,
            pre_activation_manifest=ready_archive,
            manifest_path=paths.manifest,
            generation_id=str(candidate_document["generation_id"]),
            post_verify=post_verify,
            canonical_handle_free=canonical_handle_free,
            hooks=active,
            expected_canonical_before=expected_canonical_before,
            finalize=finalize_publication,
            terminal_commit_observed=published_manifest_committed,
            revalidate_authority=revalidate_publication_authority,
        )
    except Exception as activation_error:
        try:
            _reconcile_publish_failure(
                paths=paths,
                ready_identity=ready_identity,
                ready_document=ready_document,
                ready_archive=ready_archive,
                expected_canonical_before=expected_canonical_before,
                started_at=started_at,
                error=activation_error,
                hooks=active,
            )
        except Exception as reconciliation_error:
            raise ActivationError(
                f"publication failed ({_record_error_text(activation_error)}); terminal manifest "
                f"reconciliation also failed ({_record_error_text(reconciliation_error)})"
            ) from activation_error
        raise
    published = published_holder.get("manifest")
    if not isinstance(published, ValidatedManifest):
        raise ActivationError("activation returned without a durable published manifest")
    post_commit_interrupt = published_holder.get("post_commit_interrupt")
    if isinstance(post_commit_interrupt, (KeyboardInterrupt, SystemExit)):
        raise post_commit_interrupt
    return PublishOutcome(published, activation_outcome, dict(canonical_report_holder))


def _hash_header(header: Mapping[str, object]) -> str:
    payload = dict(header)
    payload.pop("header_sha256", None)
    return canonical_sha256(payload)


def _hash_record(record: Mapping[str, object]) -> str:
    payload = dict(record)
    payload.pop("record_sha256", None)
    return canonical_sha256(payload)


def _record_error_text(error: BaseException) -> str:
    text = " ".join(str(error).split())
    return f"{type(error).__name__}: {text}"[:1000]


class JournalWriter:
    """Durable append-only writer for one initial recovery-journal segment."""

    def __init__(
        self,
        path: Path,
        *,
        transaction_id: str,
        generation_id: str,
        manifest_path: Path,
        pre_activation_manifest: FileIdentity,
        hooks: PublicationHooks,
        segment_index: int = 0,
        segment_kind: str = "initial",
        recovery_hold: Mapping[str, object] | None = None,
        previous_segment_sha256: str | None = None,
        anchor_record_sha256: str | None = None,
        invalid_predecessor_tail_sha256: str | None = None,
        sequence_offset: int = 0,
    ) -> None:
        self.path = Path(normalise_windows_path(path))
        self.transaction_id = transaction_id
        self.generation_id = generation_id
        self.hooks = hooks
        self.records: list[dict[str, object]] = []
        self.sequence_offset = sequence_offset
        self.anchor_record_sha256 = anchor_record_sha256
        self.header: dict[str, object] = {
            "journal_format_version": JOURNAL_FORMAT_VERSION,
            "transaction_id": transaction_id,
            "generation_id": generation_id,
            "segment_index": segment_index,
            "segment_kind": segment_kind,
            "created_at": utc_now_text(hooks.now()),
            "manifest_path": normalise_windows_path(manifest_path),
            "pre_activation_manifest_path": normalise_windows_path(pre_activation_manifest.path),
            "pre_activation_manifest_sha256": pre_activation_manifest.sha256,
            "recovery_hold": None if recovery_hold is None else dict(recovery_hold),
            "previous_segment_sha256": previous_segment_sha256,
            "anchor_record_sha256": anchor_record_sha256,
            "invalid_predecessor_tail_sha256": invalid_predecessor_tail_sha256,
            "header_sha256": "",
        }
        self.header["header_sha256"] = _hash_header(self.header)
        _write_create_new(self.path, canonical_json_bytes(self.header) + b"\n", hooks)

    @classmethod
    def open_existing(cls, path: Path, *, hooks: PublicationHooks) -> "JournalWriter":
        header, records, segment = _parse_journal_segment(path)
        if segment["torn_final_record"]:
            raise SemanticValidationError("cannot append to a torn journal segment")
        transaction_id = header.get("transaction_id")
        generation_id = header.get("generation_id")
        if not isinstance(transaction_id, str) or not transaction_id:
            raise SemanticValidationError("journal transaction ID is invalid")
        if not isinstance(generation_id, str) or not generation_id:
            raise SemanticValidationError("journal generation ID is invalid")
        _validate_record_chain(records, transaction_id=transaction_id)
        _validate_intent_outcomes(records)
        writer = cls.__new__(cls)
        writer.path = Path(normalise_windows_path(path))
        writer.transaction_id = transaction_id
        writer.generation_id = generation_id
        writer.hooks = hooks
        writer.records = list(records)
        writer.sequence_offset = 0
        writer.anchor_record_sha256 = None
        writer.header = dict(header)
        return writer

    @property
    def head_sha256(self) -> str | None:
        return self.anchor_record_sha256 if not self.records else str(self.records[-1]["record_sha256"])

    def append(
        self,
        *,
        operation_id: str,
        intent_sequence: int,
        action: str,
        phase: str,
        target_path: Path,
        before: Mapping[str, object] | None,
        intended_after: Mapping[str, object] | None,
        observed_after: Mapping[str, object] | None,
        outcome: str | None,
        error: str | None,
    ) -> dict[str, object]:
        sequence = self.sequence_offset + len(self.records)
        record: dict[str, object] = {
            "transaction_id": self.transaction_id,
            "operation_id": operation_id,
            "sequence": sequence,
            "intent_sequence": intent_sequence,
            "recorded_at": utc_now_text(self.hooks.now()),
            "action": action,
            "phase": phase,
            "outcome": outcome,
            "target_path": normalise_windows_path(target_path),
            "before": before,
            "intended_after": intended_after,
            "observed_after": observed_after,
            "previous_record_sha256": self.head_sha256,
            "record_sha256": "",
            "error": error,
        }
        record["record_sha256"] = _hash_record(record)
        with self.path.open("ab") as handle:
            handle.write(canonical_json_bytes(record) + b"\n")
            handle.flush()
            # Once flush returns, the complete line is observable in this
            # process even if the following durability call fails.  Retaining
            # it in the in-memory chain prevents a duplicate sequence/outcome;
            # any later successful append flushes this prefix as well.
            self.records.append(record)
            self.hooks.fsync_file(handle.fileno())
        return record

    def terminal_outcome(
        self,
        operation_id: str,
        intent_sequence: int,
    ) -> dict[str, object] | None:
        for record in reversed(self.records):
            if (
                record.get("operation_id") == operation_id
                and record.get("intent_sequence") == intent_sequence
                and record.get("phase") in {"complete", "error"}
            ):
                return record
        return None

    def intent(
        self,
        action: str,
        target_path: Path,
        before: Mapping[str, object] | None,
        intended_after: Mapping[str, object] | None,
    ) -> tuple[str, int]:
        operation_id = uuid.uuid4().hex
        intent_sequence = self.sequence_offset + len(self.records)
        self.append(
            operation_id=operation_id,
            intent_sequence=intent_sequence,
            action=action,
            phase="intent",
            target_path=target_path,
            before=before,
            intended_after=intended_after,
            observed_after=None,
            outcome=None,
            error=None,
        )
        return operation_id, intent_sequence

    def complete(
        self,
        operation_id: str,
        intent_sequence: int,
        action: str,
        target_path: Path,
        before: Mapping[str, object] | None,
        intended_after: Mapping[str, object] | None,
        observed_after: Mapping[str, object],
        *,
        outcome: str = "applied",
    ) -> dict[str, object]:
        return self.append(
            operation_id=operation_id,
            intent_sequence=intent_sequence,
            action=action,
            phase="complete",
            target_path=target_path,
            before=before,
            intended_after=intended_after,
            observed_after=observed_after,
            outcome=outcome,
            error=None,
        )

    def error(
        self,
        operation_id: str,
        intent_sequence: int,
        action: str,
        target_path: Path,
        before: Mapping[str, object] | None,
        intended_after: Mapping[str, object] | None,
        error: BaseException,
    ) -> dict[str, object]:
        existing = self.terminal_outcome(operation_id, intent_sequence)
        if existing is not None:
            return existing
        observed: Mapping[str, object] | None
        try:
            observed = fingerprint_file(target_path, allow_absent=True).as_dict()
        except Exception:
            observed = None
        return self.append(
            operation_id=operation_id,
            intent_sequence=intent_sequence,
            action=action,
            phase="error",
            target_path=target_path,
            before=before,
            intended_after=intended_after,
            observed_after=observed,
            outcome=None,
            error=_record_error_text(error),
        )

    def evidence(
        self,
        *,
        pre_activation_manifest: FileIdentity,
        status: str | None = None,
    ) -> dict[str, object]:
        if not self.records:
            raise SemanticValidationError("journal evidence requires at least one record")
        file_identity = fingerprint_file(self.path, require_nonempty=True)
        derived = status or derive_journal_status(self.records)
        segment = {
            "header": self.header,
            "file": file_identity.as_dict(),
            "valid_prefix_size_bytes": file_identity.size_bytes,
            "torn_final_record": False,
            "trailing_bytes_sha256": None,
            "parse_error": None,
            "valid_prefix_durably_flushed": True,
            "records": list(self.records),
        }
        return {
            "journal_format_version": JOURNAL_FORMAT_VERSION,
            "transaction_id": self.transaction_id,
            "generation_id": self.generation_id,
            "starting_state": "ready-to-publish",
            "manifest_path": self.header["manifest_path"],
            "pre_activation_manifest": pre_activation_manifest.as_dict(),
            "recovery_result": None,
            "status": derived,
            "last_sequence": len(self.records) - 1,
            "head_sha256": self.records[-1]["record_sha256"],
            "tail_segment_sha256": file_identity.sha256,
            "segments": [segment],
        }


def _validate_record_chain(
    records: Sequence[Mapping[str, object]],
    *,
    transaction_id: str,
    starting_sequence: int = 0,
    previous_hash: str | None = None,
) -> str | None:
    head = previous_hash
    prior_recorded_at: datetime | None = None
    for offset, record in enumerate(records):
        sequence = starting_sequence + offset
        if record.get("transaction_id") != transaction_id:
            raise SemanticValidationError("journal transaction ID drift")
        if record.get("sequence") != sequence:
            raise SemanticValidationError("journal record sequence is not contiguous")
        if record.get("previous_record_sha256") != head:
            raise SemanticValidationError("journal previous-record hash is invalid")
        digest = _hash_record(record)
        if record.get("record_sha256") != digest:
            raise SemanticValidationError("journal record hash is invalid")
        recorded_at = _utc_instant(record.get("recorded_at"), "journal record")
        if prior_recorded_at is not None and recorded_at < prior_recorded_at:
            raise SemanticValidationError("journal record timestamps are not monotonic")
        prior_recorded_at = recorded_at
        head = digest
    return head


def _outcome_proves_intended_state(
    intent: Mapping[str, object], outcome: Mapping[str, object]
) -> bool:
    return (
        outcome.get("phase") == "complete"
        and outcome.get("outcome") == "applied"
        and _json_equal(
            outcome.get("observed_after"), intent.get("intended_after")
        )
    ) or (
        outcome.get("phase") == "error"
        and isinstance(outcome.get("observed_after"), Mapping)
        and _json_equal(
            outcome.get("observed_after"), intent.get("intended_after")
        )
    )


def _validate_intent_outcomes(
    records: Sequence[Mapping[str, object]],
    *,
    segment_end_sequences: set[int] | None = None,
) -> None:
    intents: dict[tuple[str, int], Mapping[str, object]] = {}
    outcomes: dict[tuple[str, int], Mapping[str, object]] = {}
    control_operation_ids: set[str] = set()
    prior_recorded_at: datetime | None = None
    for index, record in enumerate(records):
        key = (str(record.get("operation_id")), int(record.get("intent_sequence", -1)))
        phase = record.get("phase")
        recorded_at = _utc_instant(record.get("recorded_at"), "journal record")
        if prior_recorded_at is not None and recorded_at < prior_recorded_at:
            raise SemanticValidationError("journal record timestamps are not monotonic")
        prior_recorded_at = recorded_at
        target_path = record.get("target_path")
        for identity_field in ("before", "intended_after", "observed_after"):
            identity = record.get(identity_field)
            if isinstance(identity, Mapping) and path_key(
                str(identity.get("path"))
            ) != path_key(str(target_path)):
                raise SemanticValidationError(
                    f"journal {identity_field} identity does not name target_path"
                )
        if phase == "intent":
            if record.get("intent_sequence") != record.get("sequence"):
                raise SemanticValidationError("journal intent does not point to itself")
            operation_id = key[0]
            if key in intents or operation_id in control_operation_ids:
                raise SemanticValidationError("duplicate journal operation ID")
            control_operation_ids.add(operation_id)
            intents[key] = record
            continue
        if record.get("action") == "transaction-abort":
            operation_id = key[0]
            valid_abort_boundaries = (
                {len(records) - 1}
                if segment_end_sequences is None
                else segment_end_sequences
            )
            if (
                int(record.get("sequence", -1)) not in valid_abort_boundaries
                or record.get("intent_sequence") != record.get("sequence")
                or record.get("phase") != "complete"
                or record.get("outcome") != "aborted"
                or not isinstance(record.get("before"), Mapping)
                or not _json_equal(record.get("before"), record.get("intended_after"))
                or not _json_equal(record.get("before"), record.get("observed_after"))
            ):
                raise SemanticValidationError(
                    "transaction-abort is not a terminal self-attestation"
                )
            if operation_id in control_operation_ids:
                raise SemanticValidationError("duplicate journal operation ID")
            control_operation_ids.add(operation_id)
            continue
        intent = intents.get(key)
        if intent is None:
            raise SemanticValidationError("journal outcome has no earlier intent")
        if key in outcomes:
            raise SemanticValidationError("journal intent has more than one terminal outcome")
        for field_name in ("action", "target_path", "before", "intended_after"):
            if not _json_equal(record.get(field_name), intent.get(field_name)):
                raise SemanticValidationError(f"journal outcome changes intent field {field_name}")
        if phase == "complete":
            observed = record.get("observed_after")
            expected = intent.get("intended_after") if record.get("outcome") == "applied" else intent.get("before")
            if not _json_equal(observed, expected):
                raise SemanticValidationError("journal completion outcome does not match observed state")
        outcomes[key] = record

    backup_intents = [
        (key, intent)
        for key, intent in intents.items()
        if intent.get("action") == "backup"
    ]
    main_intents = [
        intent for intent in intents.values() if intent.get("action") == "main-replace"
    ]
    if len(main_intents) > 1:
        raise SemanticValidationError("journal contains more than one forward main replacement")
    for main_intent in main_intents:
        main_sequence = int(main_intent["sequence"])
        main_target = Path(str(main_intent.get("target_path")))
        mapped_main_backups = [
            (key, backup_intent)
            for key, backup_intent in backup_intents
            if Path(str(backup_intent.get("target_path"))).name == main_target.name
        ]
        if len(mapped_main_backups) != 1:
            raise SemanticValidationError(
                "main replacement has no mapped main-database backup"
            )
        for key, backup_intent in backup_intents:
            outcome = outcomes.get(key)
            if (
                outcome is None
                or outcome.get("phase") != "complete"
                or outcome.get("outcome") != "applied"
                or int(outcome["sequence"]) >= main_sequence
                or int(backup_intent["sequence"]) >= main_sequence
            ):
                raise SemanticValidationError(
                    "main replacement is not preceded by every verified backup"
                )
    applied_main_sequences = [
        int(outcome["sequence"])
        for key, outcome in outcomes.items()
        if intents[key].get("action") == "main-replace"
        and outcome.get("phase") == "complete"
        and outcome.get("outcome") == "applied"
    ]
    for intent in intents.values():
        if intent.get("action") == "canonical-verify" and not any(
            sequence < int(intent["sequence"]) for sequence in applied_main_sequences
        ):
            raise SemanticValidationError(
                "canonical verification is not ordered after an applied main replacement"
            )
    rollback_intents = [
        (key, intent)
        for key, intent in intents.items()
        if intent.get("action") == "rollback-restore"
    ]
    applied_mutation_sequences = [
        int(outcome["sequence"])
        for key, outcome in outcomes.items()
        if intents[key].get("action") in {"main-replace", "database-sidecar-remove"}
        and _outcome_proves_intended_state(intents[key], outcome)
    ]
    for _, rollback_intent in rollback_intents:
        if not any(
            sequence < int(rollback_intent["sequence"])
            for sequence in applied_mutation_sequences
        ):
            raise SemanticValidationError(
                "rollback restoration is not ordered after canonical mutation"
            )
    for intent in intents.values():
        if intent.get("action") != "rollback-verify":
            continue
        verify_sequence = int(intent["sequence"])
        restore_targets = {
            path_key(str(restore_intent.get("target_path")))
            for _, restore_intent in rollback_intents
            if int(restore_intent["sequence"]) < verify_sequence
        }
        if not restore_targets or path_key(str(intent.get("target_path"))) not in restore_targets:
            raise SemanticValidationError(
                "rollback verification has no mapped main restoration"
            )
        for restore_target in restore_targets:
            successful_restores = [
                outcome
                for key, restore_intent in rollback_intents
                if path_key(str(restore_intent.get("target_path")))
                == restore_target
                and int(restore_intent["sequence"]) < verify_sequence
                and (outcome := outcomes.get(key)) is not None
                and outcome.get("phase") == "complete"
                and outcome.get("outcome") == "applied"
                and int(outcome["sequence"]) < verify_sequence
            ]
            if not successful_restores:
                raise SemanticValidationError(
                    "rollback verification precedes a mapped restoration"
                )


def derive_journal_status(records: Sequence[Mapping[str, object]]) -> str:
    if not records:
        return "recovery-required"
    intents: dict[tuple[str, int], Mapping[str, object]] = {}
    outcomes: dict[tuple[str, int], Mapping[str, object]] = {}
    for record in records:
        key = (str(record.get("operation_id")), int(record.get("intent_sequence", -1)))
        if record.get("phase") == "intent":
            intents[key] = record
        elif record.get("action") != "transaction-abort":
            outcomes[key] = record
    unmatched = [key for key in intents if key not in outcomes]
    last = records[-1]
    applied = [
        item
        for item in records
        if item.get("phase") == "complete" and item.get("outcome") == "applied"
    ]
    applied_actions = [str(item.get("action")) for item in applied]
    canonical_mutated = any(
        action in {"main-replace", "database-sidecar-remove"}
        for action in applied_actions
    ) or any(
        intent.get("action") in {"main-replace", "database-sidecar-remove"}
        and (outcome := outcomes.get(key)) is not None
        and outcome.get("phase") == "error"
        and _outcome_proves_intended_state(intent, outcome)
        for key, intent in intents.items()
    )
    abort_terminal = (
        last.get("action") == "transaction-abort"
        and last.get("phase") == "complete"
        and last.get("outcome") == "aborted"
    )
    if abort_terminal:
        return (
            "aborted-before-mutation"
            if not unmatched and not canonical_mutated
            else "recovery-required"
        )
    rollback_records = [
        item
        for item in applied
        if item.get("action") == "rollback-verify"
    ]
    rollback_verified = bool(rollback_records)
    if rollback_verified and not unmatched:
        terminal_sequence = int(rollback_records[-1].get("sequence", -1))
        if not canonical_mutated or any(
            item.get("phase") == "error"
            and int(item.get("sequence", -1)) > terminal_sequence
            for item in records
        ) or any(
            item.get("action") == "rollback-restore"
            and int(item.get("sequence", -1)) > terminal_sequence
            for item in records
        ):
            return "recovery-required"
        return "restored"
    if any(
        intent.get("action") == "rollback-restore"
        for intent in intents.values()
    ):
        return "rollback-started"
    if unmatched:
        if any(intents[key].get("action") in {"rollback-restore", "rollback-verify"} for key in unmatched):
            return "rollback-started"
        return "recovery-required"
    if any(item.get("phase") == "error" for item in records):
        return "recovery-required"
    canonical_verifications = [
        item
        for item in applied
        if item.get("action") == "canonical-verify"
    ]
    if canonical_verifications:
        verify_sequence = int(canonical_verifications[-1].get("sequence", -1))
        main_sequences = [
            int(item.get("sequence", -1))
            for item in applied
            if item.get("action") == "main-replace"
        ]
        if main_sequences and max(main_sequences) < verify_sequence:
            return "canonical-verified"
        return "recovery-required"
    if canonical_mutated:
        return "main-replaced"
    backup_intents = [item for item in records if item.get("action") == "backup" and item.get("phase") == "intent"]
    backup_completions = [
        item
        for item in applied
        if item.get("action") == "backup"
    ]
    if backup_intents and len(backup_intents) == len(backup_completions):
        return "backup-verified"
    if backup_intents:
        return "prepared"
    return "recovery-required"


def _journal_has_valid_header(path: Path) -> bool:
    """Return whether the first complete line is an authenticated v1 header.

    Later corruption deliberately does not affect this classification: once a
    valid header exists, recovery must preserve and continue that journal
    rather than downgrade it to a pre-journal failure receipt.
    """

    try:
        payload = Path(path).read_bytes()
    except OSError:
        return False
    boundary = payload.find(b"\n")
    if boundary < 0:
        return False
    try:
        header = loads_json_strict(payload[:boundary])
        if not isinstance(header, dict):
            return False
        if header.get("header_sha256") != _hash_header(header):
            return False
        schema = load_manifest_schema()
        validate_json_schema_subset(
            header,
            _resolve_schema_pointer(schema, "#/$defs/journalSegmentHeader"),
            root_schema=schema,
            location="$.journal.header",
        )
    except PublicationError:
        return False
    return True


def _parse_journal_segment(path: Path) -> tuple[dict[str, object], list[dict[str, object]], dict[str, object]]:
    identity = fingerprint_file(path, require_nonempty=True)
    payload = Path(path).read_bytes()
    complete_length = len(payload)
    trailing = b""
    parse_error: str | None = None
    torn = not payload.endswith(b"\n")
    if torn:
        boundary = payload.rfind(b"\n") + 1
        trailing = payload[boundary:]
        complete_length = boundary
        payload = payload[:boundary]
        parse_error = "final JSONL record is not newline terminated"
    lines = payload.splitlines()
    if not lines:
        raise SemanticValidationError("journal segment has no valid header")
    header = loads_json_strict(lines[0])
    if not isinstance(header, dict):
        raise SemanticValidationError("journal header is not an object")
    if header.get("header_sha256") != _hash_header(header):
        raise SemanticValidationError("journal header hash is invalid")
    schema = load_manifest_schema()
    header_schema = _resolve_schema_pointer(schema, "#/$defs/journalSegmentHeader")
    record_schema = _resolve_schema_pointer(schema, "#/$defs/journalRecord")
    validate_json_schema_subset(
        header,
        header_schema,
        root_schema=schema,
        location="$.journal.header",
    )
    records: list[dict[str, object]] = []
    for line_number, line in enumerate(lines[1:], start=2):
        item = loads_json_strict(line)
        if not isinstance(item, dict):
            raise SemanticValidationError(f"journal record {line_number} is not an object")
        validate_json_schema_subset(
            item,
            record_schema,
            root_schema=schema,
            location=f"$.journal.records[{line_number - 2}]",
        )
        records.append(item)
    segment = {
        "header": header,
        "file": identity.as_dict(),
        "valid_prefix_size_bytes": complete_length,
        "torn_final_record": torn,
        "trailing_bytes_sha256": hashlib.sha256(trailing).hexdigest() if torn else None,
        "parse_error": parse_error,
        "valid_prefix_durably_flushed": True,
        "records": records,
    }
    return header, records, segment


def parse_journal_chain(
    initial_segment: Path,
    *,
    continuation_segments: Sequence[Path] = (),
) -> JournalChain:
    segment_paths = (Path(initial_segment), *map(Path, continuation_segments))
    segments: list[dict[str, object]] = []
    records: list[dict[str, object]] = []
    transaction_id: str | None = None
    generation_id: str | None = None
    previous_segment_sha: str | None = None
    previous_head: str | None = None
    segment_end_sequences: set[int] = set()
    for index, segment_path in enumerate(segment_paths):
        header, segment_records, segment = _parse_journal_segment(segment_path)
        _validate_artifact_token(header.get("transaction_id"), "journal transaction_id")
        _validate_artifact_token(header.get("generation_id"), "journal generation_id")
        if header.get("segment_index") != index:
            raise SemanticValidationError("journal segment indices are not contiguous")
        if index == 0:
            transaction_id = str(header.get("transaction_id"))
            generation_id = str(header.get("generation_id"))
            if header.get("segment_kind") != "initial" or header.get("previous_segment_sha256") is not None:
                raise SemanticValidationError("invalid initial journal segment anchors")
        else:
            if header.get("transaction_id") != transaction_id or header.get("generation_id") != generation_id:
                raise SemanticValidationError("journal continuation anchor drift")
            if header.get("previous_segment_sha256") != previous_segment_sha:
                raise SemanticValidationError("journal continuation predecessor hash is invalid")
            if header.get("anchor_record_sha256") != previous_head:
                raise SemanticValidationError("journal continuation record anchor is invalid")
            if header.get("recovery_hold") is None:
                raise SemanticValidationError("journal continuation lacks a fresh recovery hold")
            prior_segment = segments[-1]
            expected_torn_hash = prior_segment.get("trailing_bytes_sha256")
            if header.get("invalid_predecessor_tail_sha256") != expected_torn_hash:
                raise SemanticValidationError("journal continuation torn-tail anchor is invalid")
            expected_kind = (
                "torn-continuation"
                if prior_segment.get("torn_final_record") is True
                else "recovery-continuation"
            )
            if header.get("segment_kind") != expected_kind:
                raise SemanticValidationError("journal continuation kind does not match its predecessor")
        assert transaction_id is not None
        previous_head = _validate_record_chain(
            segment_records,
            transaction_id=transaction_id,
            starting_sequence=len(records),
            previous_hash=previous_head,
        )
        records.extend(segment_records)
        if segment_records:
            segment_end_sequences.add(len(records) - 1)
        segments.append(segment)
        previous_segment_sha = str(segment["file"]["sha256"])  # type: ignore[index]
    if transaction_id is None or generation_id is None or previous_segment_sha is None:
        raise SemanticValidationError("journal chain has no valid segment")
    _validate_intent_outcomes(
        records,
        segment_end_sequences=segment_end_sequences,
    )
    derived_status = derive_journal_status(records)
    if segments and not segments[-1].get("records"):
        derived_status = "recovery-required"
    return JournalChain(
        transaction_id,
        generation_id,
        tuple(records),
        tuple(segments),
        previous_head,
        previous_segment_sha,
        derived_status,
    )


def _validate_journal_target(record: Mapping[str, object], paths: Mapping[str, object]) -> None:
    action = record.get("action")
    target = Path(str(record.get("target_path")))
    canonical = Path(str(paths["canonical_database"]))
    if action == "backup":
        backup_main = Path(str(paths["backup_directory"])) / canonical.name
        allowed = {
            path_key(backup_main),
            path_key(str(backup_main) + "-wal"),
            path_key(str(backup_main) + "-shm"),
        }
        if path_key(target) not in allowed:
            raise SemanticValidationError("journal backup action targets a forbidden path")
    elif action in {"main-replace", "canonical-verify", "rollback-verify", "transaction-abort"}:
        if path_key(target) != path_key(canonical):
            raise SemanticValidationError(f"journal action {action} targets a forbidden path")
    elif action == "database-sidecar-remove":
        allowed = {path_key(str(canonical) + "-wal"), path_key(str(canonical) + "-shm")}
        if path_key(target) not in allowed:
            raise SemanticValidationError("journal sidecar action targets a forbidden path")
    elif action == "rollback-restore":
        allowed = {path_key(canonical), path_key(str(canonical) + "-wal"), path_key(str(canonical) + "-shm")}
        if path_key(target) not in allowed:
            raise SemanticValidationError("journal rollback targets a forbidden path")


def _validate_journal_evidence(
    evidence: Mapping[str, object], *, paths: Mapping[str, object]
) -> JournalChain:
    schema = load_manifest_schema()
    journal_schema = _resolve_schema_pointer(schema, "#/$defs/journalEvidence")
    validate_json_schema_subset(
        evidence,
        journal_schema,
        root_schema=schema,
        location="$.journal",
    )
    segments = evidence.get("segments")
    if not isinstance(segments, list) or not segments:
        raise SemanticValidationError("journal evidence has no segments")
    canonical_volume = _volume_serial_windows(Path(str(paths["canonical_database"])))
    for segment in segments:
        segment_file = segment.get("file") if isinstance(segment, Mapping) else None
        if not isinstance(segment_file, Mapping) or segment_file.get(
            "volume_serial"
        ) != canonical_volume:
            raise SemanticValidationError(
                "journal segment is not on the canonical volume"
            )
    parsed_paths = [Path(str(segment["file"]["path"])) for segment in segments]  # type: ignore[index]
    if path_key(parsed_paths[0]) != path_key(str(paths["recovery_journal"])):
        raise SemanticValidationError("journal evidence names the wrong initial segment")
    if len({path_key(path) for path in parsed_paths}) != len(parsed_paths):
        raise SemanticValidationError("journal evidence repeats a segment path")
    for continuation_path in parsed_paths[1:]:
        require_descendant(
            continuation_path,
            Path(str(paths["recovery_journal"])).parent,
            allow_missing=False,
        )
    chain = parse_journal_chain(parsed_paths[0], continuation_segments=parsed_paths[1:])
    if not _json_equal(list(chain.segments), segments):
        raise SemanticValidationError("embedded journal segment view does not match journal bytes")
    if chain.transaction_id != evidence.get("transaction_id") or chain.generation_id != evidence.get("generation_id"):
        raise SemanticValidationError("journal evidence transaction/generation mismatch")
    if evidence.get("last_sequence") != len(chain.records) - 1:
        raise SemanticValidationError("journal evidence last_sequence is invalid")
    if evidence.get("head_sha256") != chain.head_sha256:
        raise SemanticValidationError("journal evidence head hash is invalid")
    if evidence.get("tail_segment_sha256") != chain.tail_segment_sha256:
        raise SemanticValidationError("journal evidence tail-segment hash is invalid")
    if evidence.get("status") != chain.derived_status:
        raise SemanticValidationError("journal status is not derived from its record chain")
    if path_key(str(evidence.get("manifest_path"))) != path_key(str(paths["manifest"])):
        raise SemanticValidationError("journal anchors the wrong manifest path")
    preactivation = evidence.get("pre_activation_manifest")
    if not isinstance(preactivation, dict):
        raise SemanticValidationError("journal lacks its pre-activation manifest identity")
    observed_preactivation = _assert_file_identity(preactivation, allow_absent=False)
    if observed_preactivation.volume_serial != canonical_volume:
        raise SemanticValidationError(
            "journal pre-activation snapshot is not on the canonical volume"
        )
    manifest_root = Path(str(paths["manifest"])).parent
    require_descendant(
        observed_preactivation.path,
        manifest_root,
        allow_missing=False,
    )
    if path_key(observed_preactivation.path) == path_key(str(paths["manifest"])):
        raise SemanticValidationError(
            "journal pre-activation snapshot aliases the live manifest"
        )
    preactivation_document = load_json_strict(observed_preactivation.path)
    preactivation_maintenance = preactivation_document.get("maintenance")
    historical_hold = (
        preactivation_maintenance.get("hold")
        if isinstance(preactivation_maintenance, Mapping)
        else None
    )
    if not isinstance(historical_hold, Mapping):
        raise SemanticValidationError(
            "journal pre-activation manifest lacks historical hold evidence"
        )
    prior_boundary = _utc_instant(
        preactivation_document.get("updated_at"),
        "pre-activation manifest update",
    )
    historical_expiry = _utc_instant(
        historical_hold.get("expires_at"), "historical hold expiry"
    )
    for index, segment in enumerate(chain.segments):
        header = segment.get("header")
        segment_records = segment.get("records")
        if not isinstance(header, Mapping) or not isinstance(
            segment_records, list
        ):
            raise SemanticValidationError("journal segment timing evidence is malformed")
        created_at = _utc_instant(header.get("created_at"), "journal segment creation")
        if created_at < prior_boundary:
            raise SemanticValidationError(
                "journal segment predates its manifest/predecessor boundary"
            )
        if index == 0:
            authority_expiry = historical_expiry
        else:
            segment_hold = header.get("recovery_hold")
            if not isinstance(segment_hold, Mapping):
                raise SemanticValidationError(
                    "journal continuation lacks timed recovery authority"
                )
            segment_hold_document = segment_hold.get("hold")
            if not isinstance(segment_hold_document, Mapping):
                raise SemanticValidationError(
                    "journal continuation recovery hold is malformed"
                )
            hold_verified = _utc_instant(
                segment_hold.get("verified_at"),
                "journal continuation hold verification",
            )
            authority_expiry = _utc_instant(
                segment_hold_document.get("expires_at"),
                "journal continuation hold expiry",
            )
            if created_at < hold_verified:
                raise SemanticValidationError(
                    "journal continuation predates its recovery verification"
                )
        if created_at >= authority_expiry:
            raise SemanticValidationError(
                "journal segment was created outside its hold authority"
            )
        record_boundary = created_at
        for record in segment_records:
            if not isinstance(record, Mapping):
                raise SemanticValidationError("journal segment record is malformed")
            recorded_at = _utc_instant(
                record.get("recorded_at"), "journal record"
            )
            if recorded_at < record_boundary or recorded_at >= authority_expiry:
                raise SemanticValidationError(
                    "journal record falls outside its segment/hold boundary"
                )
            record_boundary = recorded_at
        prior_boundary = record_boundary
    first_header = chain.segments[0]["header"]
    if (
        first_header.get("transaction_id") != chain.transaction_id
        or first_header.get("generation_id") != chain.generation_id
        or path_key(str(first_header.get("manifest_path")))
        != path_key(str(paths["manifest"]))
        or
        first_header.get("pre_activation_manifest_sha256") != preactivation.get("sha256")
        or path_key(str(first_header.get("pre_activation_manifest_path")))
        != path_key(str(preactivation.get("path")))
    ):
        raise SemanticValidationError("journal pre-activation manifest anchor is invalid")
    recovery_attempts: set[str] = set()
    recovery_token_ids: set[object] = set()
    recovery_token_hashes: set[object] = set()
    for segment in chain.segments[1:]:
        header = segment.get("header")
        if not isinstance(header, Mapping):
            raise SemanticValidationError("journal continuation header is malformed")
        recovery_hold = header.get("recovery_hold")
        if not isinstance(recovery_hold, Mapping):
            raise SemanticValidationError("journal continuation lacks recovery authority")
        _validate_historical_recovery_hold(
            recovery_hold,
            authorized_head=header.get("anchor_record_sha256"),
            historical_token_sha256=str(historical_hold.get("token_sha256")),
            historical_token_id=str(historical_hold.get("token_id")),
            paths=paths,
        )
        attempt_id = str(recovery_hold.get("recovery_attempt_id"))
        hold_document = recovery_hold.get("hold")
        assert isinstance(hold_document, Mapping)
        token_id = hold_document.get("token_id")
        token_sha256 = hold_document.get("token_sha256")
        if (
            attempt_id in recovery_attempts
            or token_id in recovery_token_ids
            or token_sha256 in recovery_token_hashes
        ):
            raise SemanticValidationError(
                "journal recovery attempts or hold tokens are reused"
            )
        recovery_attempts.add(attempt_id)
        recovery_token_ids.add(token_id)
        recovery_token_hashes.add(token_sha256)
    for record in chain.records:
        _validate_journal_target(record, paths)
        for identity_field in ("before", "intended_after", "observed_after"):
            identity = record.get(identity_field)
            if isinstance(identity, Mapping) and identity.get(
                "volume_serial"
            ) != canonical_volume:
                raise SemanticValidationError(
                    "journal file identity is not on the canonical volume"
                )
    recovery_result = evidence.get("recovery_result")
    if isinstance(recovery_result, Mapping):
        result_file = recovery_result.get("file")
        result_document = recovery_result.get("document")
        if not isinstance(result_file, Mapping) or not isinstance(result_document, Mapping):
            raise SemanticValidationError("journal recovery result evidence is malformed")
        observed_result = _assert_file_identity(result_file, allow_absent=False)
        if observed_result.volume_serial != canonical_volume:
            raise SemanticValidationError(
                "recovery result is not on the canonical volume"
            )
        require_descendant(
            observed_result.path,
            Path(str(paths["recovery_result_root"])),
            allow_missing=False,
        )
        if observed_result.path.read_bytes() != canonical_json_bytes(result_document) + b"\n":
            raise SemanticValidationError("recovery result bytes differ from its inline document")
        if (
            result_document.get("transaction_id") != chain.transaction_id
            or result_document.get("generation_id") != chain.generation_id
            or result_document.get("journal_head_sha256") != chain.head_sha256
            or result_document.get("journal_tail_segment_sha256") != chain.tail_segment_sha256
            or path_key(str(result_document.get("manifest_path")))
            != path_key(str(paths["manifest"]))
            or path_key(str(result_document.get("canonical_database")))
            != path_key(str(paths["canonical_database"]))
            or path_key(str(result_document.get("backup_directory")))
            != path_key(str(paths["backup_directory"]))
        ):
            raise SemanticValidationError("recovery result is not bound to its journal/paths")
        terminal_database = result_document.get("terminal_database")
        if isinstance(terminal_database, Mapping):
            _validate_database_set_document(terminal_database)
            _assert_database_set_paths(
                terminal_database,
                Path(str(paths["canonical_database"])),
            )
            if _database_set_volume(terminal_database) != canonical_volume:
                raise SemanticValidationError(
                    "recovery terminal database is on a different volume"
                )
        final_hold = chain.segments[-1]["header"].get("recovery_hold")
        if final_hold is None or not _json_equal(
            result_document.get("recovery_hold"), final_hold
        ):
            raise SemanticValidationError("recovery result does not bind the final recovery hold")
        if not isinstance(final_hold, Mapping) or result_document.get(
            "recovery_attempt_id"
        ) != final_hold.get("recovery_attempt_id"):
            raise SemanticValidationError(
                "recovery result attempt ID differs from the final hold"
            )
        final_hold_document = final_hold.get("hold")
        if not isinstance(final_hold_document, Mapping):
            raise SemanticValidationError(
                "recovery result final hold is malformed"
            )
        recovery_completed = _utc_instant(
            result_document.get("completed_at"), "recovery completion"
        )
        final_verified = _utc_instant(
            final_hold.get("verified_at"), "final recovery verification"
        )
        final_expiry = _utc_instant(
            final_hold_document.get("expires_at"), "final recovery hold expiry"
        )
        final_recorded = _utc_instant(
            chain.records[-1].get("recorded_at"), "final recovery record"
        )
        if not final_verified <= final_recorded <= recovery_completed < final_expiry:
            raise SemanticValidationError(
                "recovery result completion falls outside its final hold/journal boundary"
            )
    return chain


def _validate_journal_database_member_coverage(
    chain: JournalChain,
    *,
    settled_database: Mapping[str, object] | None,
    backup_database: Mapping[str, object] | None,
    rollback: Mapping[str, object] | None,
    paths: Mapping[str, object],
) -> None:
    """Bind journal operations to the exact canonical main/WAL/SHM sets."""

    if settled_database is None:
        raise SemanticValidationError(
            "journal evidence lacks the settled canonical database set"
        )
    records = chain.records
    intents: dict[tuple[str, int], Mapping[str, object]] = {
        (str(record.get("operation_id")), int(record.get("intent_sequence", -1))): record
        for record in records
        if record.get("phase") == "intent"
    }
    outcomes: dict[tuple[str, int], Mapping[str, object]] = {
        (str(record.get("operation_id")), int(record.get("intent_sequence", -1))): record
        for record in records
        if record.get("phase") != "intent"
        and record.get("action") != "transaction-abort"
    }
    canonical_main = Path(str(paths["canonical_database"]))
    backup_main = Path(str(paths["backup_directory"])) / canonical_main.name
    canonical_targets = {
        "main": canonical_main,
        "wal": Path(str(canonical_main) + "-wal"),
        "shm": Path(str(canonical_main) + "-shm"),
    }
    backup_targets = {
        "main": backup_main,
        "wal": Path(str(backup_main) + "-wal"),
        "shm": Path(str(backup_main) + "-shm"),
    }
    backup_intents = [
        (key, intent)
        for key, intent in intents.items()
        if intent.get("action") == "backup"
    ]
    forward_replace_started = any(
        intent.get("action") == "main-replace" for intent in intents.values()
    )
    if backup_database is not None or forward_replace_started:
        if backup_database is None:
            raise SemanticValidationError(
                "a journaled main replacement lacks its complete backup set"
            )
        for member_name, target in backup_targets.items():
            source_member = settled_database.get(member_name)
            backup_member = backup_database.get(member_name)
            if not isinstance(source_member, Mapping) or not isinstance(
                backup_member, Mapping
            ):
                raise SemanticValidationError(
                    f"journal backup coverage lacks {member_name} identity"
                )
            matching = [
                (key, intent)
                for key, intent in backup_intents
                if path_key(str(intent.get("target_path"))) == path_key(target)
            ]
            if source_member.get("exists") is not True:
                if matching:
                    raise SemanticValidationError(
                        f"journal copied absent canonical member: {member_name}"
                    )
                continue
            if len(matching) != 1:
                raise SemanticValidationError(
                    f"journal lacks one unique backup intent for {member_name}"
                )
            key, intent = matching[0]
            outcome = outcomes.get(key)
            before = intent.get("before")
            if (
                not isinstance(before, Mapping)
                or before.get("exists") is not False
                or not _json_equal(intent.get("intended_after"), backup_member)
                or not isinstance(outcome, Mapping)
                or outcome.get("phase") != "complete"
                or outcome.get("outcome") != "applied"
                or not _json_equal(outcome.get("observed_after"), backup_member)
            ):
                raise SemanticValidationError(
                    f"journal backup completion is not exact for {member_name}"
                )

    if not isinstance(rollback, Mapping) or rollback.get("succeeded") is not True:
        return
    if backup_database is None:
        raise SemanticValidationError(
            "successful rollback journal lacks its verified backup set"
        )
    restored_database = rollback.get("restored_database")
    if not isinstance(restored_database, Mapping):
        raise SemanticValidationError(
            "successful rollback lacks its restored database set"
        )
    restore_intents = [
        (key, intent)
        for key, intent in intents.items()
        if intent.get("action") == "rollback-restore"
    ]
    required_targets: set[str] = set()
    applied_restore_sequences: dict[str, list[int]] = {}
    for member_name, target in canonical_targets.items():
        backup_member = backup_database.get(member_name)
        restored_member = restored_database.get(member_name)
        if not isinstance(backup_member, Mapping) or not isinstance(
            restored_member, Mapping
        ):
            raise SemanticValidationError(
                f"rollback journal coverage lacks {member_name} identity"
            )
        matching = [
            (key, intent)
            for key, intent in restore_intents
            if path_key(str(intent.get("target_path"))) == path_key(target)
        ]
        requires_restore = backup_member.get("exists") is True or any(
            isinstance(intent.get("before"), Mapping)
            and intent["before"].get("exists") is True  # type: ignore[index,union-attr]
            for _, intent in matching
        )
        if not requires_restore:
            if matching:
                raise SemanticValidationError(
                    f"journal contains a superfluous rollback for absent {member_name}"
                )
            continue
        required_targets.add(path_key(target))
        successful_sequences: list[int] = []
        for key, intent in matching:
            if not _json_equal(intent.get("intended_after"), restored_member):
                raise SemanticValidationError(
                    f"rollback intent does not target restored {member_name} identity"
                )
            outcome = outcomes.get(key)
            if (
                isinstance(outcome, Mapping)
                and outcome.get("phase") == "complete"
                and outcome.get("outcome") == "applied"
            ):
                if not _json_equal(
                    outcome.get("observed_after"), restored_member
                ):
                    raise SemanticValidationError(
                        f"rollback completion differs from restored {member_name}"
                    )
                successful_sequences.append(int(outcome["sequence"]))
        if len(successful_sequences) != 1:
            raise SemanticValidationError(
                f"successful rollback requires one applied {member_name} restoration"
            )
        applied_restore_sequences[path_key(target)] = successful_sequences
    verify_completions = [
        outcome
        for key, outcome in outcomes.items()
        if intents[key].get("action") == "rollback-verify"
        and outcome.get("phase") == "complete"
        and outcome.get("outcome") == "applied"
    ]
    if not verify_completions:
        raise SemanticValidationError(
            "successful rollback lacks a final applied verification"
        )
    final_verify_sequence = max(int(item["sequence"]) for item in verify_completions)
    if any(
        int(record.get("sequence", -1)) >= final_verify_sequence
        for record in records
        if record.get("action") == "rollback-restore"
    ):
        raise SemanticValidationError(
            "rollback work occurs at or after the final applied verification"
        )
    if any(
        not any(sequence < final_verify_sequence for sequence in sequences)
        for target, sequences in applied_restore_sequences.items()
        if target in required_targets
    ):
        raise SemanticValidationError(
            "rollback verification does not follow every required restoration"
        )


def _project_file_identity(
    source: FileIdentity,
    target_path: Path,
    *,
    exists: bool | None = None,
) -> FileIdentity:
    target = Path(normalise_windows_path(target_path))
    projected = resolve_final_path(target, allow_missing=True)
    target_exists = source.exists if exists is None else exists
    if not target_exists:
        return FileIdentity(target, projected, False, 0, None, None, _volume_serial_windows(projected))
    return FileIdentity(
        target,
        projected,
        True,
        source.size_bytes,
        source.sha256,
        source.mtime_utc,
        _volume_serial_windows(projected),
    )


def _copy_create_new(
    source: Path,
    target: Path,
    *,
    hooks: PublicationHooks,
) -> FileIdentity:
    destination = Path(normalise_windows_path(target))
    destination.parent.mkdir(parents=True, exist_ok=True)
    resolve_final_path(destination, allow_missing=True)
    try:
        # Reserve the name with create-new semantics before handing it to an
        # injectable copier.  The copier may replace only this owned empty file.
        with destination.open("xb"):
            pass
    except FileExistsError as exc:
        raise ArtifactCollisionError(f"refusing to overwrite artifact: {destination}") from exc
    hooks.copy_file(Path(source), destination)
    shutil.copystat(source, destination, follow_symlinks=False)
    with destination.open("r+b") as handle:
        hooks.fsync_file(handle.fileno())
    hooks.fsync_directory(destination.parent)
    result = fingerprint_file(destination, require_nonempty=Path(source).stat().st_size > 0)
    if result.sha256 != sha256_file(source) or result.size_bytes != Path(source).stat().st_size:
        raise SemanticValidationError(f"copied artifact does not match source: {destination}")
    return result
    # A failed copy intentionally leaves a diagnostic file at an owned
    # create-new path; it can never satisfy verified-backup identity.


def _database_content_matches(
    observed: DatabaseSetIdentity,
    expected: DatabaseSetIdentity,
) -> bool:
    for name in ("main", "wal", "shm"):
        left = getattr(observed, name)
        right = getattr(expected, name)
        if (left.exists, left.size_bytes, left.sha256) != (right.exists, right.size_bytes, right.sha256):
            return False
    return True


def _sqlite_identity_matches(left: SQLiteIdentity, right: SQLiteIdentity) -> bool:
    return left.as_dict() == right.as_dict()


def _create_backup_under_journal(
    canonical: DatabaseSetIdentity,
    backup_directory: Path,
    journal: JournalWriter,
    *,
    hooks: PublicationHooks,
) -> DatabaseSetIdentity:
    directory = Path(normalise_windows_path(backup_directory))
    if directory.exists():
        raise ArtifactCollisionError(f"backup directory already exists: {directory}")
    if not directory.parent.is_dir():
        raise PathSafetyError(f"backup root does not exist: {directory.parent}")
    directory.mkdir()
    hooks.fsync_directory(directory.parent)
    canonical_members = {"main": canonical.main, "wal": canonical.wal, "shm": canonical.shm}
    target_main = directory / canonical.main.path.name
    target_paths = {
        "main": target_main,
        "wal": Path(str(target_main) + "-wal"),
        "shm": Path(str(target_main) + "-shm"),
    }
    for name, source in canonical_members.items():
        if not source.exists:
            continue
        target = target_paths[name]
        before_target = fingerprint_file(target, allow_absent=True)
        if before_target.exists:
            raise ArtifactCollisionError(f"backup member already exists: {target}")
        intended = _project_file_identity(source, target)
        operation_id, intent_sequence = journal.intent(
            "backup", target, before_target.as_dict(), intended.as_dict()
        )
        try:
            hooks.checkpoint(f"backup-{name}-before-copy")
            observed = _copy_create_new(source.path, target, hooks=hooks)
            hooks.checkpoint(f"backup-{name}-after-copy")
            if not _json_equal(observed.as_dict(), intended.as_dict()):
                raise SemanticValidationError(f"backup identity mismatch for {name}")
            journal.complete(
                operation_id,
                intent_sequence,
                "backup",
                target,
                before_target.as_dict(),
                intended.as_dict(),
                observed.as_dict(),
            )
        except Exception as exc:
            journal.error(
                operation_id,
                intent_sequence,
                "backup",
                target,
                before_target.as_dict(),
                intended.as_dict(),
                exc,
            )
            raise
    backup = fingerprint_database_set(target_main, require_checkpointed=True)
    if not _database_content_matches(backup, canonical):
        raise SemanticValidationError("backup database set does not match canonical source")
    return backup


def _stage_replacement(
    source: Path,
    target: Path,
    *,
    hooks: PublicationHooks,
) -> tuple[Path, FileIdentity]:
    stage = Path(target).with_name(f".{Path(target).name}.{uuid.uuid4().hex}.publication-stage")
    identity = _copy_create_new(source, stage, hooks=hooks)
    intended = _project_file_identity(identity, target)
    return stage, intended


def _append_transaction_abort(
    journal: JournalWriter,
    canonical_main: FileIdentity,
) -> None:
    sequence = journal.sequence_offset + len(journal.records)
    operation_id = uuid.uuid4().hex
    journal.append(
        operation_id=operation_id,
        intent_sequence=sequence,
        action="transaction-abort",
        phase="complete",
        target_path=canonical_main.path,
        before=canonical_main.as_dict(),
        intended_after=canonical_main.as_dict(),
        observed_after=canonical_main.as_dict(),
        outcome="aborted",
        error=None,
    )


def _restore_from_backup(
    *,
    canonical_before: DatabaseSetIdentity,
    canonical_before_sqlite: SQLiteIdentity,
    backup: DatabaseSetIdentity,
    canonical_path: Path,
    journal: JournalWriter,
    hooks: PublicationHooks,
    before_mutation: Callable[[], None] | None = None,
    completed_restores: Mapping[str, Mapping[str, object]] | None = None,
    required_restore_targets: set[str] | None = None,
) -> tuple[DatabaseSetIdentity, SQLiteIdentity]:
    current = fingerprint_database_set(canonical_path, require_checkpointed=True)
    canonical_paths = _database_member_paths(Path(canonical_path))
    current_members = (current.main, current.wal, current.shm)
    backup_members = (backup.main, backup.wal, backup.shm)
    for member_name, target, before, source in zip(
        ("main", "wal", "shm"),
        canonical_paths,
        current_members,
        backup_members,
    ):
        target_key = path_key(target)
        completed_identity = (
            completed_restores.get(target_key)
            if completed_restores is not None
            else None
        )
        if completed_identity is not None:
            observed_completed = fingerprint_file(
                target,
                allow_absent=completed_identity.get("exists") is False,
            )
            if not _json_equal(
                observed_completed.as_dict(), completed_identity
            ):
                raise SemanticValidationError(
                    f"completed rollback member changed before retry: {target}"
            )
            continue
        restore_was_started = (
            required_restore_targets is not None
            and target_key in required_restore_targets
        )
        if not source.exists and not before.exists and not restore_was_started:
            continue
        stage: Path | None = None
        if source.exists:
            intended = _project_file_identity(source, target)
        else:
            intended = _project_file_identity(before, target, exists=False)
        operation_id, intent_sequence = journal.intent(
            "rollback-restore", target, before.as_dict(), intended.as_dict()
        )
        try:
            hooks.checkpoint(f"rollback-{member_name}-before-restore")
            if before_mutation is not None:
                before_mutation()
            if _json_equal(before.as_dict(), intended.as_dict()):
                # A prior replace/unlink may have committed before its error
                # record.  Close that already-converged member under a fresh
                # intent without repeating the canonical mutation.
                observed = fingerprint_file(
                    target, allow_absent=not source.exists
                )
            elif source.exists:
                stage, staged_intended = _stage_replacement(
                    source.path, target, hooks=hooks
                )
                if not _json_equal(
                    staged_intended.as_dict(), intended.as_dict()
                ):
                    raise RollbackError(
                        f"rollback stage identity changed for {member_name}"
                    )
                assert stage is not None
                hooks.replace(stage, target)
                hooks.fsync_directory(target.parent)
                observed = fingerprint_file(target)
            else:
                hooks.unlink(target)
                hooks.fsync_directory(target.parent)
                observed = fingerprint_file(target, allow_absent=True)
            if not _json_equal(observed.as_dict(), intended.as_dict()):
                raise RollbackError(f"restored {member_name} identity does not match backup")
            journal.complete(
                operation_id,
                intent_sequence,
                "rollback-restore",
                target,
                before.as_dict(),
                intended.as_dict(),
                observed.as_dict(),
            )
            hooks.checkpoint(f"rollback-{member_name}-completed")
        except BaseException as exc:
            journal.error(
                operation_id,
                intent_sequence,
                "rollback-restore",
                target,
                before.as_dict(),
                intended.as_dict(),
                exc,
            )
            raise
        finally:
            if stage is not None and stage.exists():
                stage.unlink()
    restored = fingerprint_database_set(canonical_path, require_checkpointed=True)
    verify_operation, verify_sequence = journal.intent(
        "rollback-verify",
        Path(canonical_path),
        restored.main.as_dict(),
        canonical_before.main.as_dict(),
    )
    try:
        if not _database_content_matches(restored, backup):
            raise RollbackError("restored database set does not match verified backup")
        restored_sqlite = read_sqlite_identity(canonical_path, restored)
        if not _sqlite_identity_matches(restored_sqlite, canonical_before_sqlite):
            raise RollbackError("restored SQLite identity does not match pre-activation identity")
        journal.complete(
            verify_operation,
            verify_sequence,
            "rollback-verify",
            Path(canonical_path),
            restored.main.as_dict(),
            canonical_before.main.as_dict(),
            restored.main.as_dict(),
        )
        return restored, restored_sqlite
    except BaseException as exc:
        journal.error(
            verify_operation,
            verify_sequence,
            "rollback-verify",
            Path(canonical_path),
            restored.main.as_dict(),
            canonical_before.main.as_dict(),
            exc,
        )
        raise


def activate_database(
    *,
    canonical_database: Path,
    candidate_database: Path,
    backup_directory: Path,
    recovery_journal: Path,
    pre_activation_manifest: FileIdentity,
    generation_id: str,
    post_verify: Callable[[Path], object],
    canonical_handle_free: Callable[[Path], bool],
    manifest_path: Path | None = None,
    expected_canonical_before: DatabaseSetIdentity | None = None,
    finalize: Callable[[ActivationOutcome], None] | None = None,
    terminal_commit_observed: Callable[[], bool] | None = None,
    revalidate_authority: Callable[[], None] | None = None,
    hooks: PublicationHooks | None = None,
) -> ActivationOutcome:
    """Activate one candidate and exactly restore the prior DB set on failure.

    The broad throwable boundary begins immediately after the flushed
    ``main-replace`` intent.  It exists solely to make restoration interruption
    safe; preparation and validation code never catches ``BaseException``.
    """

    active = hooks or PublicationHooks()
    canonical_path = Path(normalise_windows_path(canonical_database))
    candidate_path = Path(normalise_windows_path(candidate_database))
    journal_path = Path(normalise_windows_path(recovery_journal))
    backup_path = Path(normalise_windows_path(backup_directory))
    require_distinct_paths(
        {
            "canonical_database": canonical_path,
            "candidate_database": candidate_path,
            "backup_database": backup_path / canonical_path.name,
        }
    )
    if journal_path.exists():
        raise ArtifactCollisionError(f"recovery journal already exists: {journal_path}")
    if backup_path.exists():
        raise ArtifactCollisionError(f"backup directory already exists: {backup_path}")
    volumes = {
        _volume_serial_windows(canonical_path),
        _volume_serial_windows(candidate_path),
        _volume_serial_windows(backup_path),
        _volume_serial_windows(journal_path),
    }
    if len(volumes) != 1:
        raise PathSafetyError("candidate, canonical, backup, and journal must share one volume")
    candidate = fingerprint_database_set(candidate_path, require_closed=True, require_checkpointed=True)
    candidate_sqlite = read_sqlite_identity(candidate_path, candidate, require_schema4=True)
    canonical_before = fingerprint_database_set(canonical_path, require_closed=True, require_checkpointed=True)
    if expected_canonical_before is not None and not _json_equal(
        canonical_before.as_dict(), expected_canonical_before.as_dict()
    ):
        raise SemanticValidationError("canonical database differs from the final settled sample")
    if not canonical_handle_free(canonical_path):
        raise SemanticValidationError("canonical database handle-free proof failed")
    canonical_before_sqlite = read_sqlite_identity(canonical_path, canonical_before)
    transaction_id = uuid.uuid4().hex
    journal = JournalWriter(
        journal_path,
        transaction_id=transaction_id,
        generation_id=generation_id,
        manifest_path=manifest_path or pre_activation_manifest.path,
        pre_activation_manifest=pre_activation_manifest,
        hooks=active,
    )
    try:
        backup = _create_backup_under_journal(
            canonical_before, backup_path, journal, hooks=active
        )
        active.checkpoint("backup-verified")
        # Reobserve immediately before intent; no earlier proof authorizes the replace.
        revalidated = fingerprint_database_set(canonical_path, require_closed=True, require_checkpointed=True)
        if not _json_equal(revalidated.as_dict(), canonical_before.as_dict()):
            raise SemanticValidationError("canonical database changed before activation")
        if not canonical_handle_free(canonical_path):
            raise SemanticValidationError("canonical handle-free proof expired before activation")
        if revalidate_authority is not None:
            revalidate_authority()
        stage, intended = _stage_replacement(candidate.main.path, canonical_path, hooks=active)
        operation_id, intent_sequence = journal.intent(
            "main-replace", canonical_path, canonical_before.main.as_dict(), intended.as_dict()
        )
    except Exception as exc:
        # No canonical mutation has been attempted.  The caller records this as
        # pre-activation failure and preserves the partial journal/backup.
        try:
            _append_transaction_abort(journal, canonical_before.main)
        except Exception as abort_error:
            raise ActivationError(
                f"pre-activation failure ({_record_error_text(exc)}); journal closure also failed "
                f"({_record_error_text(abort_error)})"
            ) from exc
        raise

    primary: BaseException | None = None
    rollback_error: BaseException | None = None
    canonical_mutated = False
    main_outcome_recorded = False
    terminal_manifest_committed = False
    try:
        # BaseException handling intentionally begins after the durable intent.
        active.checkpoint("main-replace-intent-flushed")
        if revalidate_authority is not None:
            revalidate_authority()
        active.replace(stage, canonical_path)
        canonical_mutated = True
        active.fsync_directory(canonical_path.parent)
        active.checkpoint("main-replace-returned")
        canonical_after_main = fingerprint_file(canonical_path, require_nonempty=True)
        if not _json_equal(canonical_after_main.as_dict(), intended.as_dict()):
            raise ActivationError("activated main file does not match candidate")
        journal.complete(
            operation_id,
            intent_sequence,
            "main-replace",
            canonical_path,
            canonical_before.main.as_dict(),
            intended.as_dict(),
            canonical_after_main.as_dict(),
        )
        main_outcome_recorded = True
        canonical_after = fingerprint_database_set(
            canonical_path, require_closed=True, require_checkpointed=True
        )
        canonical_after_sqlite = read_sqlite_identity(
            canonical_path, canonical_after, require_schema4=True
        )
        if canonical_after.main.sha256 != candidate.main.sha256 or canonical_after.main.size_bytes != candidate.main.size_bytes:
            raise ActivationError("canonical main bytes do not match candidate")
        if not _sqlite_identity_matches(canonical_after_sqlite, candidate_sqlite):
            raise ActivationError("canonical SQLite identity does not match candidate")
        verify_operation, verify_sequence = journal.intent(
            "canonical-verify",
            canonical_path,
            canonical_after.main.as_dict(),
            canonical_after.main.as_dict(),
        )
        try:
            post_verify(canonical_path)
            cleanup_owned_sqlite_sidecars(
                canonical_path,
                hooks=active,
                journal=journal,
                handle_free=canonical_handle_free,
            )
            active.checkpoint("canonical-verification-complete")
            verified = fingerprint_database_set(
                canonical_path, require_closed=True, require_checkpointed=True
            )
            verified_sqlite = read_sqlite_identity(canonical_path, verified, require_schema4=True)
            if (
                verified.main.size_bytes != canonical_after.main.size_bytes
                or verified.main.sha256 != canonical_after.main.sha256
            ):
                raise ActivationError("canonical main bytes changed during post-publication verification")
            if verified.wal.exists and verified.wal.size_bytes != 0:
                raise ActivationError("canonical WAL is not checkpointed after verification")
            if verified.shm.exists:
                raise ActivationError("canonical SHM remains after verification")
            if not _sqlite_identity_matches(verified_sqlite, candidate_sqlite):
                raise ActivationError("post-publication SQLite identity changed")
            journal.complete(
                verify_operation,
                verify_sequence,
                "canonical-verify",
                canonical_path,
                canonical_after.main.as_dict(),
                canonical_after.main.as_dict(),
                verified.main.as_dict(),
            )
            outcome = ActivationOutcome(
                canonical_before=canonical_before,
                canonical_before_sqlite=canonical_before_sqlite,
                canonical=verified,
                sqlite=verified_sqlite,
                candidate=candidate,
                candidate_sqlite=candidate_sqlite,
                backup=backup,
                journal=fingerprint_file(journal_path, require_nonempty=True),
                journal_evidence=journal.evidence(
                    pre_activation_manifest=pre_activation_manifest,
                    status="canonical-verified",
                ),
                rolled_back=False,
            )
            if finalize is not None:
                if revalidate_authority is not None:
                    revalidate_authority()
                finalize(outcome)
                terminal_manifest_committed = True
            return outcome
        except BaseException as exc:
            if terminal_commit_observed is not None and terminal_commit_observed():
                # The exact published receipt already embeds this journal
                # prefix.  Never append an error to sealed terminal evidence.
                raise
            journal.error(
                verify_operation,
                verify_sequence,
                "canonical-verify",
                canonical_path,
                canonical_after.main.as_dict(),
                canonical_after.main.as_dict(),
                exc,
            )
            raise
    except BaseException as exc:
        if terminal_manifest_committed or (
            terminal_commit_observed is not None and terminal_commit_observed()
        ):
            # No throwable arriving after the exact published receipt may
            # mutate its sealed journal or roll canonical bytes backward.
            raise
        primary = exc
        # An interrupt can land inside os.replace after the rename completed but
        # before it returned.  Fresh bytes, not the Python flag, decide whether
        # restoration is mandatory.
        try:
            if journal.terminal_outcome(operation_id, intent_sequence) is not None:
                main_outcome_recorded = True
            observed = fingerprint_file(canonical_path, require_nonempty=True)
            if not main_outcome_recorded:
                if _json_equal(observed.as_dict(), canonical_before.main.as_dict()):
                    journal.complete(
                        operation_id,
                        intent_sequence,
                        "main-replace",
                        canonical_path,
                        canonical_before.main.as_dict(),
                        intended.as_dict(),
                        observed.as_dict(),
                        outcome="not-applied",
                    )
                    main_outcome_recorded = True
                    canonical_mutated = False
                    _append_transaction_abort(journal, observed)
                elif _json_equal(observed.as_dict(), intended.as_dict()):
                    journal.complete(
                        operation_id,
                        intent_sequence,
                        "main-replace",
                        canonical_path,
                        canonical_before.main.as_dict(),
                        intended.as_dict(),
                        observed.as_dict(),
                        outcome="applied",
                    )
                    main_outcome_recorded = True
                    canonical_mutated = True
                else:
                    raise ActivationError("canonical state matches neither side of the main-replace intent")
        except BaseException as observation_error:
            try:
                journal.error(
                    operation_id,
                    intent_sequence,
                    "main-replace",
                    canonical_path,
                    canonical_before.main.as_dict(),
                    intended.as_dict(),
                    observation_error,
                )
            except BaseException:
                pass
            canonical_mutated = True
        if canonical_mutated:
            try:
                _restore_from_backup(
                    canonical_before=canonical_before,
                    canonical_before_sqlite=canonical_before_sqlite,
                    backup=backup,
                    canonical_path=canonical_path,
                    journal=journal,
                    hooks=active,
                )
            except BaseException as restore_exc:
                rollback_error = restore_exc
    finally:
        if stage.exists():
            try:
                stage.unlink()
            except OSError:
                pass
    assert primary is not None
    if isinstance(primary, (KeyboardInterrupt, SystemExit)):
        if rollback_error is not None:
            detail = f"rollback also failed: {_record_error_text(rollback_error)}"
            if hasattr(primary, "add_note"):
                primary.add_note(detail)
            else:
                primary.args = (*primary.args, detail)
        raise primary
    if rollback_error is not None:
        raise RollbackError(
            f"activation failed ({_record_error_text(primary)}); rollback also failed "
            f"({_record_error_text(rollback_error)})"
        ) from primary
    raise ActivationError(
        f"activation failed and prior database was restored: {_record_error_text(primary)}"
    ) from primary


def _journal_evidence_from_chain(
    chain: JournalChain,
    *,
    manifest_path: Path,
    pre_activation_manifest: FileIdentity,
    recovery_result: Mapping[str, object] | None,
) -> dict[str, object]:
    if not chain.records or chain.head_sha256 is None:
        raise SemanticValidationError(
            "terminal journal evidence requires at least one complete record"
        )
    return {
        "journal_format_version": JOURNAL_FORMAT_VERSION,
        "transaction_id": chain.transaction_id,
        "generation_id": chain.generation_id,
        "starting_state": "ready-to-publish",
        "manifest_path": normalise_windows_path(manifest_path),
        "pre_activation_manifest": pre_activation_manifest.as_dict(),
        "recovery_result": None if recovery_result is None else dict(recovery_result),
        "status": chain.derived_status,
        "last_sequence": len(chain.records) - 1,
        "head_sha256": chain.head_sha256,
        "tail_segment_sha256": chain.tail_segment_sha256,
        "segments": list(chain.segments),
    }


def _validate_historical_recovery_hold(
    recovery_hold: Mapping[str, object],
    *,
    authorized_head: object,
    historical_token_sha256: str,
    historical_token_id: str,
    paths: Mapping[str, object] | None = None,
) -> None:
    """Validate an embedded recovery boundary without rereading rotated files."""

    schema = load_manifest_schema()
    hold_schema = _resolve_schema_pointer(schema, "#/$defs/recoveryHold")
    validate_json_schema_subset(
        recovery_hold,
        hold_schema,
        root_schema=schema,
        location="$.recovery_hold",
    )
    _validate_artifact_token(
        recovery_hold.get("recovery_attempt_id"),
        "recovery attempt ID",
    )
    if (
        recovery_hold.get("purpose") != "emergency-recovery"
        or recovery_hold.get("authorized_head_sha256") != authorized_head
        or recovery_hold.get("historical_hold_token_sha256")
        != historical_token_sha256
    ):
        raise SemanticValidationError("historical recovery-hold anchors are invalid")
    hold = recovery_hold.get("hold")
    task = recovery_hold.get("scheduled_task")
    if not isinstance(hold, Mapping) or not isinstance(task, Mapping):
        raise SemanticValidationError("historical recovery hold is incomplete")
    if (
        hold.get("externally_owned") is not True
        or hold.get("publisher_may_release") is not False
        or hold.get("task_acknowledged") is not True
        or hold.get("token_sha256") == historical_token_sha256
        or hold.get("token_id") == historical_token_id
        or hold.get("acknowledgement_sha256")
        != hold_acknowledgement_sha256(hold, task)
    ):
        raise SemanticValidationError("historical recovery hold authority is invalid")
    hold_file = hold.get("hold_file")
    if not isinstance(hold_file, Mapping) or hold.get("token_sha256") != hold_file.get(
        "sha256"
    ):
        raise SemanticValidationError("historical recovery token identity is invalid")
    if paths is not None:
        expected_hold_paths = {
            "hold_file": Path(str(paths["hold_path"])),
            "pause_file": Path(str(paths["queue_state"])) / "pause.flag",
            "queue_state": Path(str(paths["queue_state"])) / "state.json",
        }
        for name, expected_path in expected_hold_paths.items():
            identity = hold.get(name)
            if not isinstance(identity, Mapping) or path_key(
                str(identity.get("path"))
            ) != path_key(expected_path):
                raise SemanticValidationError(
                    f"historical recovery hold names the wrong {name} path"
                )
    if task.get("path") != "\\" or task.get("name") != "Wallpaper Download Queue":
        raise SemanticValidationError("historical recovery task authority is invalid")
    acquired = _utc_instant(hold.get("acquired_at"), "historical hold acquisition")
    acknowledged = _utc_instant(
        hold.get("acknowledged_at"), "historical hold acknowledgement"
    )
    expires = _utc_instant(hold.get("expires_at"), "historical hold expiry")
    task_observed = _utc_instant(
        task.get("observed_at"), "historical task observation"
    )
    task_acknowledged = _utc_instant(
        task.get("acknowledged_at"), "historical task acknowledgement"
    )
    started = _utc_instant(
        recovery_hold.get("recovery_started_at"), "historical recovery start"
    )
    verified = _utc_instant(
        recovery_hold.get("verified_at"), "historical recovery verification"
    )
    if not (
        acquired
        <= task_observed
        <= acknowledged
        == task_acknowledged
        <= started
        <= verified
        < expires
    ):
        raise SemanticValidationError("historical recovery authority times are invalid")
    samples = recovery_hold.get("writer_samples")
    if not isinstance(samples, list) or len(samples) < 2:
        raise SemanticValidationError("historical recovery writer window is incomplete")
    sequences: list[int] = []
    elapsed: list[int] = []
    sample_times: list[datetime] = []
    for sample in samples:
        if (
            not isinstance(sample, Mapping)
            or not isinstance(sample.get("sequence"), int)
            or isinstance(sample.get("sequence"), bool)
            or not isinstance(sample.get("elapsed_milliseconds"), int)
            or isinstance(sample.get("elapsed_milliseconds"), bool)
            or sample.get("downloader_descendant_count") != 0
            or sample.get("index_writer_count") != 0
            or sample.get("process_ids") != []
        ):
            raise SemanticValidationError("historical recovery writer sample is invalid")
        sequences.append(int(sample["sequence"]))
        elapsed.append(int(sample["elapsed_milliseconds"]))
        sample_times.append(
            _utc_instant(sample.get("sampled_at"), "historical recovery writer sample")
        )
    if (
        sequences != list(range(sequences[0], sequences[0] + len(sequences)))
        or any(right <= left for left, right in zip(elapsed, elapsed[1:]))
        or any(right <= left for left, right in zip(sample_times, sample_times[1:]))
        or elapsed[-1] - elapsed[0] < 30_000
        or recovery_hold.get("minimum_window_milliseconds") != 30_000
        or recovery_hold.get("actual_window_milliseconds")
        != elapsed[-1] - elapsed[0]
    ):
        raise SemanticValidationError("historical recovery writer window is invalid")
    wall_window_milliseconds = int(
        (sample_times[-1] - sample_times[0]).total_seconds() * 1000
    )
    if wall_window_milliseconds < 30_000 or abs(
        wall_window_milliseconds - (elapsed[-1] - elapsed[0])
    ) > 2_000:
        raise SemanticValidationError(
            "historical recovery writer wall-clock window is invalid"
        )
    listener = recovery_hold.get("listener_snapshot")
    if (
        not isinstance(listener, Mapping)
        or listener.get("listen_count") != 0
        or listener.get("bindings") != []
    ):
        raise SemanticValidationError("historical recovery listener proof is invalid")
    listener_observed = _utc_instant(
        listener.get("observed_at"), "historical recovery listener observation"
    )
    if not started <= sample_times[0] < sample_times[-1] <= listener_observed <= verified:
        raise SemanticValidationError("historical recovery observation times are invalid")


def _validate_recovery_hold(
    recovery_hold: Mapping[str, object],
    *,
    authorized_head: str | None,
    historical_token_sha256: str,
    historical_token_id: str,
    paths: Mapping[str, object],
    canonical_database: Path,
    canonical_handle_free: Callable[[Path], bool],
    now: datetime,
) -> None:
    _validate_historical_recovery_hold(
        recovery_hold,
        authorized_head=authorized_head,
        historical_token_sha256=historical_token_sha256,
        historical_token_id=historical_token_id,
        paths=paths,
    )
    if recovery_hold.get("purpose") != "emergency-recovery":
        raise SemanticValidationError("recovery hold purpose is invalid")
    _validate_artifact_token(
        recovery_hold.get("recovery_attempt_id"),
        "recovery attempt ID",
    )
    if recovery_hold.get("authorized_head_sha256") != authorized_head:
        raise SemanticValidationError("recovery hold does not authorize the current journal head")
    if recovery_hold.get("historical_hold_token_sha256") != historical_token_sha256:
        raise SemanticValidationError("recovery hold historical-token anchor is invalid")
    hold = recovery_hold.get("hold")
    task = recovery_hold.get("scheduled_task")
    if not isinstance(hold, dict) or not isinstance(task, dict):
        raise SemanticValidationError("recovery hold has no current external hold")
    if (
        hold.get("token_sha256") == historical_token_sha256
        or hold.get("token_id") == historical_token_id
    ):
        raise SemanticValidationError("recovery hold must use a fresh token")
    maximum_age = int(recovery_hold.get("maximum_evidence_age_seconds", 0))
    if maximum_age != 300:
        raise SemanticValidationError("recovery evidence age limit must be 300 seconds")
    _validate_external_hold(
        hold,
        task,
        hold_path=Path(str(paths["hold_path"])),
        queue_state_path=Path(str(paths["queue_state"])),
        now=now,
        maximum_age_seconds=maximum_age,
    )
    started = _utc_instant(recovery_hold.get("recovery_started_at"), "recovery start")
    verified = _utc_instant(recovery_hold.get("verified_at"), "recovery verification")
    hold_acknowledged = _utc_instant(hold.get("acknowledged_at"), "recovery hold acknowledgement")
    hold_expires = _utc_instant(hold.get("expires_at"), "recovery hold expiry")
    if not hold_acknowledged <= started <= verified < hold_expires:
        raise SemanticValidationError("recovery verification predates recovery start")
    _assert_fresh_instant(verified, now, maximum_age, "recovery evidence")
    samples = recovery_hold.get("writer_samples")
    if not isinstance(samples, list) or len(samples) < 2:
        raise SemanticValidationError("recovery writer window is incomplete")
    sequences = [sample.get("sequence") for sample in samples if isinstance(sample, dict)]
    milliseconds = [sample.get("elapsed_milliseconds") for sample in samples if isinstance(sample, dict)]
    if len(sequences) != len(samples) or sequences != list(range(int(sequences[0]), int(sequences[0]) + len(samples))):
        raise SemanticValidationError("recovery writer sequences are not contiguous")
    if any(not isinstance(value, int) or isinstance(value, bool) for value in milliseconds):
        raise SemanticValidationError("recovery elapsed milliseconds must be integers")
    if int(milliseconds[-1]) - int(milliseconds[0]) < 30_000:
        raise SemanticValidationError("recovery writer window is shorter than 30 seconds")
    actual_window = int(recovery_hold.get("actual_window_milliseconds", -1))
    if actual_window < 30_000 or actual_window != int(milliseconds[-1]) - int(milliseconds[0]):
        raise SemanticValidationError("recovery actual writer window is invalid")
    sample_times = [
        _utc_instant(sample.get("sampled_at"), "recovery writer sample")
        for sample in samples
        if isinstance(sample, dict)
    ]
    if len(sample_times) != len(samples) or any(
        right <= left for left, right in zip(sample_times, sample_times[1:])
    ):
        raise SemanticValidationError("recovery writer sample timestamps are not increasing")
    wall_window_milliseconds = int(
        (sample_times[-1] - sample_times[0]).total_seconds() * 1000
    )
    if wall_window_milliseconds < 30_000 or abs(
        wall_window_milliseconds - actual_window
    ) > 2_000:
        raise SemanticValidationError(
            "recovery writer elapsed and wall-clock windows disagree"
        )
    if sample_times[0] < started or sample_times[-1] > verified:
        raise SemanticValidationError("recovery writer samples fall outside their evidence boundary")
    for sample in samples:
        assert isinstance(sample, dict)
        if (
            sample.get("downloader_descendant_count") != 0
            or sample.get("index_writer_count") != 0
            or sample.get("process_ids") != []
        ):
            raise SemanticValidationError("recovery writer observation is not zero")
    listener = recovery_hold.get("listener_snapshot")
    if not isinstance(listener, dict) or listener.get("listen_count") != 0 or listener.get("bindings") != []:
        raise SemanticValidationError("recovery hold does not prove zero listeners")
    listener_observed = _utc_instant(
        listener.get("observed_at"), "recovery listener observation"
    )
    if not sample_times[-1] <= listener_observed <= verified:
        raise SemanticValidationError(
            "recovery listener observation falls outside the recovery boundary"
        )
    _assert_fresh_instant(
        listener_observed,
        now,
        maximum_age,
        "recovery listener evidence",
    )
    if (
        recovery_hold.get("canonical_handle_free") is not True
        or not canonical_handle_free(Path(canonical_database))
    ):
        raise SemanticValidationError("recovery hold does not prove canonical handle freedom")


def _unmatched_intents(records: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    intents: dict[tuple[str, int], Mapping[str, object]] = {}
    finished: set[tuple[str, int]] = set()
    for record in records:
        key = (str(record.get("operation_id")), int(record.get("intent_sequence", -1)))
        if record.get("phase") == "intent":
            intents[key] = record
        elif record.get("action") != "transaction-abort":
            finished.add(key)
    return [intent for key, intent in intents.items() if key not in finished]


def _close_pre_journal_recovery(
    *,
    canonical_path: Path,
    backup_path: Path,
    journal_path: Path,
    result_root: Path,
    manifest_path: Path,
    runtime_evidence: Mapping[str, object],
    canonical_handle_free: Callable[[Path], bool],
    hooks: PublicationHooks,
    parse_error: BaseException,
) -> ValidatedManifest:
    """Close a delayed journal-create/first-flush failure without mutation."""

    manifest_identity = fingerprint_file(manifest_path, require_nonempty=True)
    assert manifest_identity.sha256 is not None
    ready_document = load_json_strict(manifest_path)
    validate_manifest_document(
        ready_document,
        manifest_path=manifest_path,
        check_current=False,
    )
    paths_document = ready_document.get("paths")
    maintenance = ready_document.get("maintenance")
    if not isinstance(paths_document, dict) or not isinstance(maintenance, dict):
        raise SemanticValidationError(
            "pre-journal recovery manifest lacks path/maintenance evidence"
        )
    exact_paths = {
        "canonical_database": canonical_path,
        "backup_directory": backup_path,
        "recovery_journal": journal_path,
        "recovery_result_root": result_root,
        "manifest": manifest_path,
    }
    for name, expected in exact_paths.items():
        if path_key(str(paths_document.get(name))) != path_key(expected):
            raise SemanticValidationError(
                f"pre-journal recovery {name} differs from the ready manifest"
            )
    require_distinct_paths(
        {
            "canonical_database": canonical_path,
            "backup_database": backup_path / canonical_path.name,
            "manifest": manifest_path,
            "recovery_journal": journal_path,
        }
    )

    existing_failure = ready_document.get("pre_activation_failure")
    if (
        ready_document.get("state") == "ready-to-publish"
        and isinstance(existing_failure, Mapping)
        and existing_failure.get("closure_kind")
        == "pre-journal-recovery-close"
    ):
        # An already committed close is a pure idempotent read.  Do not demand
        # another live hold and do not create another archive or output.
        stored_database = existing_failure.get("canonical_database")
        stored_sqlite = existing_failure.get("canonical_sqlite")
        observed_database = fingerprint_database_set(
            canonical_path,
            require_closed=True,
            require_checkpointed=True,
        )
        observed_sqlite = read_sqlite_identity(canonical_path, observed_database)
        if (
            not isinstance(stored_database, Mapping)
            or not isinstance(stored_sqlite, Mapping)
            or not _json_equal(observed_database.as_dict(), stored_database)
            or not _sqlite_identity_matches(
                observed_sqlite, _stored_sqlite_identity(stored_sqlite)
            )
        ):
            raise SemanticValidationError(
                "current canonical state differs from the pre-journal recovery receipt"
            )
        return ValidatedManifest(
            manifest_path,
            manifest_identity.sha256,
            ready_document,
        )

    if (
        ready_document.get("state") != "ready-to-publish"
        or existing_failure is not None
        or ready_document.get("activation") is not None
        or ready_document.get("post_publish") is not None
        or ready_document.get("rollback") is not None
        or not isinstance(ready_document.get("result"), Mapping)
        or ready_document["result"].get("status") != "in-progress"  # type: ignore[index,union-attr]
    ):
        raise SemanticValidationError(
            "pre-journal recovery requires one unclosed ready-to-publish manifest"
        )
    historical_hold = maintenance.get("hold")
    settled_samples = maintenance.get("settled_samples")
    if (
        not isinstance(historical_hold, dict)
        or not isinstance(settled_samples, list)
        or not settled_samples
        or not isinstance(settled_samples[-1], Mapping)
        or not isinstance(settled_samples[-1].get("canonical"), Mapping)
    ):
        raise SemanticValidationError(
            "pre-journal recovery lacks its settled canonical/hold anchor"
        )
    settled_canonical_document = settled_samples[-1]["canonical"]  # type: ignore[index]
    assert isinstance(settled_canonical_document, Mapping)
    recovery_hold = runtime_evidence.get("recovery_hold")
    machine_identity = runtime_evidence.get("machine_identity")
    if not isinstance(recovery_hold, dict) or not isinstance(machine_identity, dict):
        raise SemanticValidationError(
            "fresh recovery hold and machine identity are required"
        )
    fresh_machine = _validate_current_machine_identity(
        machine_identity,
        now=hooks.now(),
    )
    if not _stable_machine_identity_matches(
        fresh_machine, ready_document.get("machine_identity")
    ):
        raise SemanticValidationError(
            "fresh recovery machine identity differs from the transaction"
        )
    volume_paths = (
        canonical_path,
        Path(str(paths_document["candidate_database"])),
        backup_path,
        journal_path,
        result_root,
        manifest_path,
    )
    if len({_volume_serial_windows(path) for path in volume_paths}) != 1:
        raise PathSafetyError(
            "canonical, candidate, backup, journal, result, and manifest must share one volume"
        )

    def revalidate_authority_and_canonical() -> tuple[DatabaseSetIdentity, SQLiteIdentity]:
        _validate_current_machine_identity(machine_identity, now=hooks.now())
        if sha256_file(manifest_path) != manifest_identity.sha256:
            raise SemanticValidationError(
                "manifest changed during protected pre-journal recovery",
                code="manifest-cas-conflict",
            )
        _validate_recovery_hold(
            recovery_hold,
            authorized_head=None,
            historical_token_sha256=str(historical_hold["token_sha256"]),
            historical_token_id=str(historical_hold["token_id"]),
            paths=paths_document,
            canonical_database=canonical_path,
            canonical_handle_free=canonical_handle_free,
            now=hooks.now(),
        )
        observed_database = fingerprint_database_set(
            canonical_path,
            require_closed=True,
            require_checkpointed=True,
        )
        if not _json_equal(
            observed_database.as_dict(), settled_canonical_document
        ):
            raise SemanticValidationError(
                "canonical database differs from the final pre-activation sample"
            )
        return observed_database, read_sqlite_identity(
            canonical_path, observed_database
        )

    canonical, canonical_sqlite = revalidate_authority_and_canonical()
    observed_outputs = [
        fingerprint_file(journal_path, allow_absent=True).as_dict(),
        *[
            fingerprint_file(path, allow_absent=True).as_dict()
            for path in _database_member_paths(backup_path / canonical_path.name)
        ],
    ]
    failed_at = utc_now_text(hooks.now())
    phase = (
        "journal-first-flush"
        if Path(journal_path).exists()
        else "journal-create"
    )
    error_code = "pre-journal-recovery-close"
    error_message = _record_error_text(parse_error)
    replacement = dict(ready_document)
    replacement.update(
        {
            "updated_at": failed_at,
            "machine_identity": fresh_machine,
            "backup": None,
            "pre_activation_failure": {
                "phase": phase,
                "failed_at": failed_at,
                "error_code": error_code,
                "error_message": error_message,
                "canonical_unchanged": True,
                "canonical_database": canonical.as_dict(),
                "canonical_sqlite": canonical_sqlite.as_dict(),
                "closure_kind": "pre-journal-recovery-close",
                "recovery_hold": dict(recovery_hold),
                "observed_outputs": observed_outputs,
                "journal": None,
            },
            "activation": None,
            "post_publish": None,
            "rollback": None,
            "result": {
                "status": "failed",
                "release_eligible": False,
                "listener_restore_required": True,
                "issue_taxonomy": [],
                "terminal_at": failed_at,
                "error_code": error_code,
                "error_message": error_message,
            },
        }
    )
    validate_manifest_document(
        replacement,
        manifest_path=manifest_path,
        check_current=False,
    )
    canonical_again, sqlite_again = revalidate_authority_and_canonical()
    if (
        not _json_equal(canonical_again.as_dict(), canonical.as_dict())
        or not _sqlite_identity_matches(sqlite_again, canonical_sqlite)
    ):
        raise SemanticValidationError(
            "canonical state changed before pre-journal recovery close"
        )
    current_outputs = [
        fingerprint_file(journal_path, allow_absent=True).as_dict(),
        *[
            fingerprint_file(path, allow_absent=True).as_dict()
            for path in _database_member_paths(backup_path / canonical_path.name)
        ],
    ]
    if not _json_equal(current_outputs, observed_outputs):
        raise SemanticValidationError(
            "partial pre-journal outputs changed before reconciliation"
        )
    generation_id = _validate_artifact_token(
        ready_document.get("candidate", {}).get("generation_id")  # type: ignore[union-attr]
        if isinstance(ready_document.get("candidate"), Mapping)
        else None,
        "candidate generation_id",
    )
    attempt_id = _validate_artifact_token(
        recovery_hold.get("recovery_attempt_id"),
        "recovery attempt ID",
    )
    archive_path = manifest_path.with_name(
        f"{manifest_path.stem}.{generation_id}.pre-journal-recovery-source.{attempt_id}.json"
    )
    intended_sha = hashlib.sha256(
        canonical_json_bytes(replacement) + b"\n"
    ).hexdigest()
    commit_interrupt: BaseException | None = None
    try:
        replaced = replace_json_compare_and_swap(
            manifest_path,
            replacement,
            expected_sha256=manifest_identity.sha256,
            archive_path=archive_path,
            hooks=hooks,
        )
    except BaseException as commit_error:
        observed_manifest = fingerprint_file(manifest_path, require_nonempty=True)
        if observed_manifest.sha256 != intended_sha:
            raise
        try:
            hooks.fsync_directory(manifest_path.parent)
        except BaseException:
            _flush_directory(manifest_path.parent)
        replaced = fingerprint_file(manifest_path, require_nonempty=True)
        if replaced.sha256 != intended_sha:
            raise ActivationError(
                "pre-journal recovery manifest changed during commit revalidation"
            ) from commit_error
        if isinstance(commit_error, (KeyboardInterrupt, SystemExit)):
            commit_interrupt = commit_error
    assert replaced.sha256 is not None
    validate_manifest_document(
        replacement,
        manifest_path=manifest_path,
        check_current=False,
    )
    if commit_interrupt is not None:
        raise commit_interrupt
    return ValidatedManifest(manifest_path, replaced.sha256, replacement)


def recover_publication(
    *,
    canonical_database: Path,
    backup_directory: Path,
    recovery_journal: Path,
    recovery_result_root: Path,
    runtime_evidence: Mapping[str, object],
    manifest: Path | None = None,
    canonical_handle_free: Callable[[Path], bool] = database_handles_free,
    continuation_segments: Sequence[Path] = (),
    hooks: PublicationHooks | None = None,
) -> ValidatedManifest:
    """Converge an interrupted transaction backward under a fresh external hold.

    Recovery never resumes forward activation and never appends to a sealed
    segment.  It creates one continuation, an immutable result document, and
    finally one manifest CAS receipt.
    """

    active = hooks or PublicationHooks()
    canonical_path = Path(normalise_windows_path(canonical_database))
    backup_path = Path(normalise_windows_path(backup_directory))
    journal_path = Path(normalise_windows_path(recovery_journal))
    result_root = Path(normalise_windows_path(recovery_result_root))
    explicit_manifest_path = (
        Path(normalise_windows_path(manifest)) if manifest is not None else None
    )
    try:
        chain = parse_journal_chain(
            journal_path, continuation_segments=continuation_segments
        )
    except (PublicationError, OSError) as parse_error:
        if _journal_has_valid_header(journal_path):
            raise
        if continuation_segments:
            raise SemanticValidationError(
                "pre-journal recovery cannot adopt continuation segments"
            ) from parse_error
        if explicit_manifest_path is None:
            raise SemanticValidationError(
                "an explicit manifest is required when no valid journal header exists"
            ) from parse_error
        return _close_pre_journal_recovery(
            canonical_path=canonical_path,
            backup_path=backup_path,
            journal_path=journal_path,
            result_root=result_root,
            manifest_path=explicit_manifest_path,
            runtime_evidence=runtime_evidence,
            canonical_handle_free=canonical_handle_free,
            hooks=active,
            parse_error=parse_error,
        )
    first_header = chain.segments[0]["header"]
    manifest_path = Path(str(first_header["manifest_path"]))
    if explicit_manifest_path is not None and path_key(
        explicit_manifest_path
    ) != path_key(manifest_path):
        raise SemanticValidationError(
            "explicit recovery manifest differs from the journal anchor"
        )
    preactivation_path = Path(str(first_header["pre_activation_manifest_path"]))
    preactivation = fingerprint_file(preactivation_path, require_nonempty=True)
    if preactivation.sha256 != first_header.get("pre_activation_manifest_sha256"):
        raise SemanticValidationError("pre-activation manifest anchor changed")
    ready_document = load_json_strict(preactivation_path)
    if ready_document.get("state") != "ready-to-publish":
        raise SemanticValidationError("recovery anchor is not ready-to-publish")
    paths_document = ready_document.get("paths")
    maintenance = ready_document.get("maintenance")
    if not isinstance(paths_document, dict) or not isinstance(maintenance, dict):
        raise SemanticValidationError("recovery anchor lacks path/maintenance evidence")
    validate_manifest_document(
        ready_document,
        manifest_path=manifest_path,
        check_current=False,
    )
    ready_candidate = ready_document.get("candidate")
    if not isinstance(ready_candidate, Mapping) or ready_candidate.get(
        "generation_id"
    ) != chain.generation_id:
        raise SemanticValidationError("journal generation differs from the ready manifest")
    if path_key(str(paths_document["canonical_database"])) != path_key(canonical_path):
        raise SemanticValidationError("recovery canonical path differs from journal anchor")
    if path_key(str(paths_document["backup_directory"])) != path_key(backup_path):
        raise SemanticValidationError("recovery backup path differs from journal anchor")
    exact_recovery_paths = {
        "manifest": manifest_path,
        "recovery_journal": journal_path,
        "recovery_result_root": result_root,
    }
    for name, expected in exact_recovery_paths.items():
        if path_key(str(paths_document[name])) != path_key(expected):
            raise SemanticValidationError(f"recovery {name} differs from the ready manifest")
    require_distinct_paths(
        {
            "canonical_database": canonical_path,
            "backup_database": backup_path / canonical_path.name,
            "manifest": manifest_path,
            "recovery_journal": journal_path,
        }
    )
    recovery_volumes = {
        _volume_serial_windows(path)
        for path in (
            canonical_path,
            Path(str(paths_document["candidate_database"])),
            backup_path,
            journal_path,
            result_root,
            manifest_path,
            preactivation_path,
            *continuation_segments,
        )
    }
    if len(recovery_volumes) != 1:
        raise PathSafetyError(
            "canonical, backup, journal, result, and manifest recovery artifacts must share one volume"
        )
    if chain.records:
        initial_journal_evidence = _journal_evidence_from_chain(
            chain,
            manifest_path=manifest_path,
            pre_activation_manifest=preactivation,
            recovery_result=None,
        )
        _validate_journal_evidence(initial_journal_evidence, paths=paths_document)
    manifest_expected = fingerprint_file(manifest_path, require_nonempty=True)
    current_manifest_document = load_json_strict(manifest_path)
    current_state = current_manifest_document.get("state")
    terminal_journal: Mapping[str, object] | None = None
    if current_state == "ready-to-publish":
        terminal_failure = current_manifest_document.get("pre_activation_failure")
        if isinstance(terminal_failure, Mapping) and isinstance(
            terminal_failure.get("journal"), Mapping
        ):
            terminal_journal = terminal_failure["journal"]  # type: ignore[assignment]
    elif current_state == "rolled-back":
        terminal_activation = current_manifest_document.get("activation")
        if isinstance(terminal_activation, Mapping) and isinstance(
            terminal_activation.get("journal"), Mapping
        ):
            terminal_journal = terminal_activation["journal"]  # type: ignore[assignment]
    if isinstance(terminal_journal, Mapping) and isinstance(
        terminal_journal.get("recovery_result"), Mapping
    ):
        # A completed recovery is an idempotent read: validate its complete
        # receipt and terminal bytes, then return without requiring a new hold
        # or creating another continuation/result.
        validate_manifest_document(
            current_manifest_document,
            manifest_path=manifest_path,
            check_current=False,
        )
        terminal_result = terminal_journal["recovery_result"]
        assert isinstance(terminal_result, Mapping)
        terminal_result_document = terminal_result.get("document")
        if (
            terminal_journal.get("transaction_id") != chain.transaction_id
            or terminal_journal.get("generation_id") != chain.generation_id
            or not isinstance(terminal_result_document, Mapping)
            or terminal_result_document.get("transaction_id") != chain.transaction_id
            or terminal_result_document.get("generation_id") != chain.generation_id
        ):
            raise SemanticValidationError(
                "terminal recovery receipt differs from the requested transaction"
            )
        terminal_database_document = terminal_result_document.get(
            "terminal_database"
        )
        terminal_sqlite_document = terminal_result_document.get("terminal_sqlite")
        if not isinstance(terminal_database_document, Mapping) or not isinstance(
            terminal_sqlite_document, Mapping
        ):
            raise SemanticValidationError(
                "terminal recovery receipt lacks database/SQLite evidence"
            )
        observed_terminal_database = fingerprint_database_set(
            canonical_path,
            require_closed=True,
            require_checkpointed=True,
        )
        observed_terminal_sqlite = read_sqlite_identity(
            canonical_path,
            observed_terminal_database,
        )
        if not _json_equal(
            observed_terminal_database.as_dict(), terminal_database_document
        ) or not _sqlite_identity_matches(
            observed_terminal_sqlite,
            _stored_sqlite_identity(terminal_sqlite_document),
        ):
            raise SemanticValidationError(
                "current canonical bytes differ from the completed recovery receipt"
            )
        assert manifest_expected.sha256 is not None
        return ValidatedManifest(
            manifest_path,
            manifest_expected.sha256,
            current_manifest_document,
        )
    if manifest_expected.sha256 == preactivation.sha256:
        if not _json_equal(current_manifest_document, ready_document):
            raise SemanticValidationError("current ready manifest differs despite an equal byte hash")
    else:
        validate_manifest_document(
            current_manifest_document,
            manifest_path=manifest_path,
            check_current=False,
        )
        current_candidate = current_manifest_document.get("candidate")
        ready_candidate = ready_document.get("candidate")
        current_paths = current_manifest_document.get("paths")
        current_failure = current_manifest_document.get("pre_activation_failure")
        current_result = current_manifest_document.get("result")
        ready_terminal_receipt = (
            current_manifest_document.get("state") == "ready-to-publish"
            and isinstance(current_failure, Mapping)
            and current_failure.get("canonical_unchanged") is True
            and isinstance(current_result, Mapping)
            and current_result.get("status") == "failed"
            and all(
                _json_equal(
                    current_manifest_document.get(field),
                    ready_document.get(field),
                )
                for field in (
                    "manifest_schema_version",
                    "semantic_contract_version",
                    "created_at",
                    "state_transition",
                    "paths",
                    "candidate",
                    "durable_inputs",
                    "verification",
                    "maintenance",
                    "authorization",
                )
            )
            and _stable_machine_identity_matches(
                current_manifest_document.get("machine_identity"),
                ready_document.get("machine_identity"),
            )
        )
        if (
            current_manifest_document.get("state") != "published"
            and not ready_terminal_receipt
        ) or (
            not isinstance(current_candidate, dict)
            or not isinstance(ready_candidate, dict)
            or current_candidate.get("generation_id") != chain.generation_id
            or current_candidate.get("generation_id") != ready_candidate.get("generation_id")
            or not isinstance(current_paths, dict)
            or not _json_equal(current_paths, paths_document)
        ):
            raise SemanticValidationError(
                "current manifest is not the ready/published transaction anchor",
                code="manifest-cas-conflict",
            )
    recovery_hold = runtime_evidence.get("recovery_hold")
    machine_identity = runtime_evidence.get("machine_identity")
    if not isinstance(recovery_hold, dict) or not isinstance(machine_identity, dict):
        raise SemanticValidationError("fresh recovery hold and machine identity are required")
    historical_hold = maintenance.get("hold")
    if not isinstance(historical_hold, dict):
        raise SemanticValidationError("historical hold evidence is missing")
    fresh_machine = _validate_current_machine_identity(
        machine_identity,
        now=active.now(),
    )
    historical_machine = ready_document.get("machine_identity")
    if not isinstance(historical_machine, dict) or any(
        fresh_machine[key] != historical_machine.get(key)
        for key in (
            "status",
            "machine_id",
            "instance_id",
            "computer_name",
            "qualified_user",
            "verifier_path",
        )
    ):
        raise SemanticValidationError("fresh recovery machine identity differs from the transaction")

    def revalidate_recovery_authority() -> None:
        _validate_current_machine_identity(machine_identity, now=active.now())
        if sha256_file(manifest_path) != manifest_expected.sha256:
            raise SemanticValidationError(
                "manifest changed during protected recovery",
                code="manifest-cas-conflict",
            )
        _validate_recovery_hold(
            recovery_hold,
            authorized_head=chain.head_sha256,
            historical_token_sha256=str(historical_hold["token_sha256"]),
            historical_token_id=str(historical_hold["token_id"]),
            paths=paths_document,
            canonical_database=canonical_path,
            canonical_handle_free=canonical_handle_free,
            now=active.now(),
        )

    revalidate_recovery_authority()
    attempt_id = str(recovery_hold["recovery_attempt_id"])
    prior_segment = chain.segments[-1]
    continuation_path = journal_path.with_name(
        f"{journal_path.stem}.recovery-{attempt_id}.segment.jsonl"
    )
    if continuation_path.exists():
        raise ArtifactCollisionError(f"recovery continuation already exists: {continuation_path}")
    require_descendant(continuation_path, journal_path.parent)
    result_path = result_root / f"{chain.transaction_id}.{attempt_id}.recovery-result.json"
    require_descendant(result_path, result_root)
    if result_path.exists():
        raise ArtifactCollisionError(f"recovery result already exists: {result_path}")
    manifest_archive = preactivation
    if manifest_expected.sha256 != preactivation.sha256:
        source_archive_path = manifest_path.with_name(
            f"{manifest_path.stem}.{chain.generation_id}.recovery-source.{attempt_id}.json"
        )
        manifest_archive = _write_create_new(
            source_archive_path, manifest_path.read_bytes(), active
        )
        if manifest_archive.sha256 != manifest_expected.sha256:
            raise SemanticValidationError("recovery source manifest archive changed")
    revalidate_recovery_authority()
    continuation = JournalWriter(
        continuation_path,
        transaction_id=chain.transaction_id,
        generation_id=chain.generation_id,
        manifest_path=manifest_path,
        pre_activation_manifest=preactivation,
        hooks=active,
        segment_index=len(chain.segments),
        segment_kind="torn-continuation" if prior_segment["torn_final_record"] else "recovery-continuation",
        recovery_hold=recovery_hold,
        previous_segment_sha256=chain.tail_segment_sha256,
        anchor_record_sha256=chain.head_sha256,
        invalid_predecessor_tail_sha256=prior_segment["trailing_bytes_sha256"],
        sequence_offset=len(chain.records),
    )
    mutation_completed = any(
        record.get("action") in {"main-replace", "database-sidecar-remove"}
        and record.get("phase") == "complete"
        and record.get("outcome") == "applied"
        for record in chain.records
    )
    unmatched_intents = _unmatched_intents(chain.records)
    deferred_rollback_verifications: list[Mapping[str, object]] = []
    for intent in unmatched_intents:
        if intent.get("action") == "rollback-verify":
            deferred_rollback_verifications.append(intent)
            continue
        target = Path(str(intent["target_path"]))
        observed = fingerprint_file(target, allow_absent=True)
        before = intent.get("before")
        intended = intent.get("intended_after")
        if (
            intent.get("action") == "rollback-restore"
            and isinstance(intended, dict)
            and _json_equal(observed.as_dict(), intended)
        ):
            # Backward convergence takes precedence when before/intended are
            # byte-identical: treating it as applied prevents a committed
            # restore from being repeated after an ambiguous interruption.
            outcome = "applied"
        elif isinstance(before, dict) and _json_equal(observed.as_dict(), before):
            outcome = "not-applied"
        elif isinstance(intended, dict) and _json_equal(observed.as_dict(), intended):
            outcome = "applied"
            if intent.get("action") in {"main-replace", "database-sidecar-remove"}:
                mutation_completed = True
        else:
            raise SemanticValidationError(
                f"unmatched {intent.get('action')} intent target matches neither recorded state: {target}"
            )
        revalidate_recovery_authority()
        continuation.complete(
            str(intent["operation_id"]),
            int(intent["intent_sequence"]),
            str(intent["action"]),
            target,
            before if isinstance(before, dict) else None,
            intended if isinstance(intended, dict) else None,
            observed.as_dict(),
            outcome=outcome,
        )
    current_main = fingerprint_file(canonical_path, require_nonempty=True)
    for main_intent in reversed(
        [record for record in chain.records if record.get("action") == "main-replace" and record.get("phase") == "intent"]
    ):
        intended_main = main_intent.get("intended_after")
        before_main = main_intent.get("before")
        if isinstance(intended_main, dict) and _json_equal(current_main.as_dict(), intended_main):
            mutation_completed = True
            break
        if isinstance(before_main, dict) and _json_equal(current_main.as_dict(), before_main):
            break
        raise SemanticValidationError("canonical main matches neither side of the recorded replace")
    settled_samples = maintenance.get("settled_samples")
    if not isinstance(settled_samples, list) or not settled_samples:
        raise SemanticValidationError("pre-activation canonical sample is unavailable")
    final_sample = settled_samples[-1]
    if not isinstance(final_sample, dict) or not isinstance(final_sample.get("canonical"), dict):
        raise SemanticValidationError("pre-activation canonical sample is malformed")
    preactivation_database = _stored_database_set(final_sample["canonical"])
    result_status: str
    terminal_database: DatabaseSetIdentity
    terminal_sqlite: SQLiteIdentity
    terminal_basis: str
    target_state: str
    error_code: str | None
    error_message: str | None
    backup: DatabaseSetIdentity | None = None
    if not mutation_completed:
        terminal_database = fingerprint_database_set(canonical_path, require_checkpointed=True)
        if not _database_content_matches(terminal_database, preactivation_database):
            raise SemanticValidationError("canonical set changed despite an unapplied forward transaction")
        terminal_sqlite = read_sqlite_identity(canonical_path, terminal_database)
        # Every recovery attempt closes its own hold-bound continuation with a
        # fresh equal-state abort, even when an earlier segment already did so.
        revalidate_recovery_authority()
        _append_transaction_abort(continuation, terminal_database.main)
        result_status = "aborted-before-mutation"
        terminal_basis = "pre-activation"
        target_state = "ready-to-publish"
        error_code = "publication-aborted"
        error_message = "Interrupted publication made no canonical mutation."
    else:
        backup_main = backup_path / canonical_path.name
        backup = fingerprint_database_set(backup_main, require_checkpointed=True)
        if not _database_content_matches(backup, preactivation_database):
            raise SemanticValidationError(
                "verified backup does not equal the final settled pre-activation database set"
            )
        backup_sqlite = read_sqlite_identity(backup_main, backup)
        for verify_intent in deferred_rollback_verifications:
            verified_database = fingerprint_database_set(
                canonical_path, require_checkpointed=True
            )
            verified_sqlite = read_sqlite_identity(
                canonical_path, verified_database
            )
            verified_main = verified_database.main.as_dict()
            before = verify_intent.get("before")
            intended = verify_intent.get("intended_after")
            if (
                _database_content_matches(verified_database, backup)
                and _sqlite_identity_matches(verified_sqlite, backup_sqlite)
                and isinstance(intended, Mapping)
                and _json_equal(verified_main, intended)
            ):
                verify_outcome = "applied"
            elif isinstance(before, Mapping) and _json_equal(
                verified_main, before
            ):
                verify_outcome = "not-applied"
            else:
                raise SemanticValidationError(
                    "unmatched rollback verification no longer matches its recorded state"
                )
            revalidate_recovery_authority()
            continuation.complete(
                str(verify_intent["operation_id"]),
                int(verify_intent["intent_sequence"]),
                "rollback-verify",
                canonical_path,
                before if isinstance(before, dict) else None,
                intended if isinstance(intended, dict) else None,
                verified_main,
                outcome=verify_outcome,
            )
        backup_records = [
            record for record in (*chain.records, *continuation.records)
            if record.get("action") == "backup" and record.get("phase") == "complete" and record.get("outcome") == "applied"
        ]
        for source_member, member in zip(
            (preactivation_database.main, preactivation_database.wal, preactivation_database.shm),
            (backup.main, backup.wal, backup.shm),
        ):
            if source_member.exists != member.exists:
                raise SemanticValidationError(f"backup member presence differs from its source: {member.path}")
            if not member.exists:
                continue
            matching = [
                record
                for record in backup_records
                if path_key(str(record.get("target_path"))) == path_key(member.path)
                and isinstance(record.get("before"), dict)
                and record["before"].get("exists") is False  # type: ignore[index]
                and isinstance(record.get("intended_after"), dict)
                and record["intended_after"].get("sha256") == member.sha256  # type: ignore[index]
                and record["intended_after"].get("size_bytes") == member.size_bytes  # type: ignore[index]
                and isinstance(record.get("observed_after"), dict)
                and record["observed_after"].get("sha256") == member.sha256  # type: ignore[index]
                and record["observed_after"].get("size_bytes") == member.size_bytes  # type: ignore[index]
            ]
            if len(matching) != 1:
                raise SemanticValidationError(
                    f"backup member lacks one exact verified journal completion: {member.path}"
                )
        current_database = fingerprint_database_set(canonical_path, require_checkpointed=True)
        if chain.derived_status == "restored":
            if not _database_content_matches(current_database, backup):
                raise SemanticValidationError("restored journal does not match the retained backup")
            terminal_database = current_database
            terminal_sqlite = read_sqlite_identity(canonical_path, terminal_database)
            if not _sqlite_identity_matches(terminal_sqlite, backup_sqlite):
                raise SemanticValidationError("restored SQLite identity differs from the backup")
            revalidate_recovery_authority()
            verify_operation, verify_sequence = continuation.intent(
                "rollback-verify",
                canonical_path,
                terminal_database.main.as_dict(),
                terminal_database.main.as_dict(),
            )
            continuation.complete(
                verify_operation,
                verify_sequence,
                "rollback-verify",
                canonical_path,
                terminal_database.main.as_dict(),
                terminal_database.main.as_dict(),
                terminal_database.main.as_dict(),
            )
        else:
            completed_restores: dict[str, Mapping[str, object]] = {}
            required_restore_targets: set[str] = set()
            for record in (*chain.records, *continuation.records):
                if (
                    record.get("action") == "rollback-restore"
                    and record.get("phase") == "intent"
                ):
                    required_restore_targets.add(
                        path_key(str(record["target_path"]))
                    )
                if (
                    record.get("action") == "rollback-restore"
                    and record.get("phase") == "complete"
                    and record.get("outcome") == "applied"
                    and isinstance(record.get("observed_after"), Mapping)
                ):
                    completed_restores[path_key(str(record["target_path"]))] = record[  # type: ignore[assignment,index]
                        "observed_after"
                    ]
            revalidate_recovery_authority()
            terminal_database, terminal_sqlite = _restore_from_backup(
                canonical_before=preactivation_database,
                canonical_before_sqlite=backup_sqlite,
                backup=backup,
                canonical_path=canonical_path,
                journal=continuation,
                hooks=active,
                before_mutation=revalidate_recovery_authority,
                completed_restores=completed_restores,
                required_restore_targets=required_restore_targets,
            )
        result_status = "rolled-back"
        terminal_basis = "backup"
        target_state = "rolled-back"
        error_code = "publication-recovered"
        error_message = "Interrupted publication was restored from its verified backup."
    combined_paths = (*continuation_segments, continuation_path)
    completed_chain = parse_journal_chain(journal_path, continuation_segments=combined_paths)
    expected_terminal = "aborted-before-mutation" if result_status == "aborted-before-mutation" else "restored"
    if completed_chain.derived_status != expected_terminal:
        raise SemanticValidationError(
            f"recovery journal did not reach {expected_terminal}: {completed_chain.derived_status}"
        )
    revalidate_recovery_authority()
    manifest_observed = fingerprint_file(manifest_path, require_nonempty=True)
    completed_at = utc_now_text(active.now())
    manifest_matches = manifest_observed.sha256 == manifest_expected.sha256
    result_document: dict[str, object] = {
        "recovery_result_schema_version": 1,
        "recovery_attempt_id": attempt_id,
        "transaction_id": chain.transaction_id,
        "generation_id": chain.generation_id,
        "completed_at": completed_at,
        "machine_identity": fresh_machine,
        "status": result_status if manifest_matches else "failed",
        "canonical_database": normalise_windows_path(canonical_path),
        "backup_directory": normalise_windows_path(backup_path),
        "journal_head_sha256": completed_chain.head_sha256,
        "journal_tail_segment_sha256": completed_chain.tail_segment_sha256,
        "terminal_database": terminal_database.as_dict(),
        "terminal_sqlite": terminal_sqlite.as_dict(),
        "terminal_match_basis": terminal_basis,
        "terminal_matches_expected": True,
        "recovery_hold": dict(recovery_hold),
        "manifest_path": normalise_windows_path(manifest_path),
        "manifest_cas_expected_sha256": manifest_expected.sha256,
        "manifest_pre_cas_observed_sha256": manifest_observed.sha256,
        "manifest_pre_cas_matches_expected": manifest_matches,
        "manifest_cas_target_state": target_state
        if manifest_matches
        else str(current_manifest_document["state"]),
        "hold_must_remain": True,
        "listener_restore_required": True,
        "error_code": error_code if manifest_matches else "manifest-cas-conflict",
        "error_message": error_message
        if manifest_matches
        else "Manifest changed after recovery began; terminal database state was retained without CAS.",
    }
    schema = load_manifest_schema()
    result_schema = _resolve_schema_pointer(schema, "#/$defs/recoveryResultDocument")
    validate_json_schema_subset(result_document, result_schema, root_schema=schema, location="$.recovery_result")
    revalidate_recovery_authority()
    result_identity = write_json_create_new(result_path, result_document, hooks=active)
    if not manifest_matches:
        raise SemanticValidationError(
            "manifest changed before recovery reconciliation",
            code="manifest-cas-conflict",
        )
    recovery_result = {"file": result_identity.as_dict(), "document": result_document}
    journal_evidence = _journal_evidence_from_chain(
        completed_chain,
        manifest_path=manifest_path,
        pre_activation_manifest=preactivation,
        recovery_result=recovery_result,
    )
    replacement = dict(current_manifest_document)
    replacement["updated_at"] = completed_at
    if result_status == "aborted-before-mutation":
        replacement.update(
            {
                "pre_activation_failure": {
                    "phase": "pre-canonical-mutation-abort",
                    "failed_at": completed_at,
                    "error_code": error_code,
                    "error_message": error_message,
                    "canonical_unchanged": True,
                    "canonical_database": terminal_database.as_dict(),
                    "canonical_sqlite": terminal_sqlite.as_dict(),
                    "closure_kind": "crash-recovery-close",
                    "recovery_hold": None,
                    "observed_outputs": [result_identity.as_dict()],
                    "journal": journal_evidence,
                },
                "result": {
                    "status": "failed",
                    "release_eligible": False,
                    "listener_restore_required": True,
                    "issue_taxonomy": [],
                    "terminal_at": completed_at,
                    "error_code": error_code,
                    "error_message": error_message,
                },
            }
        )
    else:
        assert backup is not None
        backup_sqlite = read_sqlite_identity(backup.main.path, backup)
        prior_published_activation = (
            current_manifest_document.get("activation")
            if current_manifest_document.get("state") == "published"
            else None
        )
        if not isinstance(prior_published_activation, Mapping):
            prior_published_activation = None
        replacement.update(
            {
                "state": "rolled-back",
                "state_transition": {
                    "from_state": str(current_manifest_document["state"]),
                    "entered_at": completed_at,
                    "previous_manifest": manifest_archive.as_dict(),
                },
                "backup": {
                    "created_at": str(first_header["created_at"]),
                    "same_volume": True,
                    "collision_free": True,
                    "database": backup.as_dict(),
                    "source_database_sha256": preactivation_database.main.sha256,
                    "source_sqlite": backup_sqlite.as_dict(),
                },
                "pre_activation_failure": None,
                "activation": {
                    "status": "failed",
                    "started_at": str(
                        prior_published_activation.get("started_at")
                        if prior_published_activation is not None
                        else first_header["created_at"]
                    ),
                    "completed_at": completed_at,
                    "journal": journal_evidence,
                    "canonical_before": preactivation_database.as_dict(),
                    "canonical_after": (
                        prior_published_activation.get("canonical_after")
                        if prior_published_activation is not None
                        else None
                    ),
                    "canonical_before_sqlite": backup_sqlite.as_dict(),
                    "canonical_after_sqlite": (
                        prior_published_activation.get("canonical_after_sqlite")
                        if prior_published_activation is not None
                        else None
                    ),
                    "main_replace_same_volume_atomic": True,
                    "multi_file_atomic_claim": False,
                    "rollback_required": True,
                    "error_code": "publication-interrupted",
                    "error_message": "Publication was interrupted before terminal verification.",
                },
                "post_publish": current_manifest_document.get("post_publish")
                if current_manifest_document.get("state") == "published"
                else None,
                "rollback": {
                    "attempted_at": str(recovery_hold["recovery_started_at"]),
                    "completed_at": completed_at,
                    "trigger": "emergency-recovery",
                    "succeeded": True,
                    "restored_database": terminal_database.as_dict(),
                    "restored_sqlite": terminal_sqlite.as_dict(),
                    "restored_sha256_matches_backup": True,
                    "hold_must_remain": True,
                    "listener_restore_required": True,
                    "error": None,
                },
                "result": {
                    "status": "rolled-back",
                    "release_eligible": False,
                    "listener_restore_required": True,
                    "issue_taxonomy": [],
                    "terminal_at": completed_at,
                    "error_code": error_code,
                    "error_message": error_message,
                },
            }
        )
    validate_manifest_document(
        replacement,
        manifest_path=manifest_path,
        check_current=False,
    )
    revalidate_recovery_authority()
    if sha256_file(manifest_path) != manifest_expected.sha256:
        raise SemanticValidationError("manifest changed before recovery reconciliation", code="manifest-cas-conflict")
    replaced_identity = replace_json_with_precreated_archive(
        manifest_path,
        replacement,
        expected_sha256=str(manifest_expected.sha256),
        archive_identity=manifest_archive,
        hooks=active,
    )
    assert replaced_identity.sha256 is not None
    return ValidatedManifest(manifest_path, replaced_identity.sha256, replacement)


def restore_verified_backup(
    *,
    canonical_database: Path,
    backup_directory: Path,
    recovery_journal: Path,
    canonical_before_sqlite: SQLiteIdentity,
    hooks: PublicationHooks | None = None,
) -> tuple[DatabaseSetIdentity, SQLiteIdentity, JournalChain]:
    """Compatibility inspection helper; mutation requires ``recover_publication``."""

    del canonical_before_sqlite, hooks
    canonical_path = Path(normalise_windows_path(canonical_database))
    backup_main = Path(normalise_windows_path(backup_directory)) / canonical_path.name
    backup = fingerprint_database_set(backup_main, require_checkpointed=True)
    current = fingerprint_database_set(canonical_path, require_checkpointed=True)
    chain = parse_journal_chain(recovery_journal)
    if chain.derived_status == "restored" and _database_content_matches(current, backup):
        return current, read_sqlite_identity(canonical_path, current), chain
    raise SemanticValidationError(
        "recovery mutation requires recover_publication with a fresh hold-bound continuation"
    )
