"""Portable, fail-closed CLI for gallery index publication.

The PowerShell wrapper owns SND-HOST identity, path pinning, queue/task,
listener, writer, and handle observations.  This module consumes those values
without inferring live paths or operating any external resource.  A command
without ``--apply`` is read-only by construction.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
import logging
import os
from pathlib import Path
import sys


# The wrapper already invokes Python with -B.  Keep direct read-only CLI use
# from creating package caches as well.
sys.dont_write_bytecode = True

from dl_engine import gallery_publication as publication  # noqa: E402
from dl_engine import index_library  # noqa: E402


MAX_STDIN_BYTES = 8 * 1024 * 1024
MAX_ERROR_CHARS = 2_000


class CliError(RuntimeError):
    """Expected command failure with a stable code and exit class."""

    def __init__(self, message: str, *, code: str, exit_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


class JsonArgumentParser(argparse.ArgumentParser):
    """Route usage failures through the one-JSON-result contract."""

    def error(self, message: str) -> None:
        raise CliError(message, code="invalid-arguments", exit_code=2)


def _bounded(value: object) -> str:
    text = " ".join(str(value).splitlines()).strip()
    if len(text) <= MAX_ERROR_CHARS:
        return text
    return text[: MAX_ERROR_CHARS - 3] + "..."


def _emit(payload: Mapping[str, object]) -> None:
    sys.stdout.write(
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )


def _error_payload(
    mode: str,
    *,
    code: str,
    message: object,
    applied: bool,
) -> dict[str, object]:
    return {
        "applied": applied,
        "error": {"code": code, "message": _bounded(message)},
        "mode": mode,
        "ok": False,
        "status": "failed",
    }


def _add_runtime_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--runtime-evidence-stdin",
        action="store_true",
        help="Read one strict JSON runtime-evidence object from standard input.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Permit the selected write-capable operation.",
    )
    parser.add_argument(
        "--cutover-authorized",
        action="store_true",
        help="Record the separate external cutover authorization for publish.",
    )


def _add_recovery_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--canonical-database", required=True, type=Path)
    parser.add_argument("--backup-directory", required=True, type=Path)
    parser.add_argument("--recovery-journal", required=True, type=Path)
    parser.add_argument("--recovery-result-root", required=True, type=Path)
    parser.add_argument("--queue-state-path", required=True, type=Path)
    parser.add_argument("--hold-path", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)


def _add_full_paths(parser: argparse.ArgumentParser) -> None:
    _add_recovery_paths(parser)
    parser.add_argument("--candidate-database", required=True, type=Path)
    parser.add_argument("--library-root", required=True, type=Path)
    parser.add_argument("--wallhaven-ledger", required=True, type=Path)
    parser.add_argument("--provider-ledger", required=True, type=Path)
    parser.add_argument("--sibling-database", required=True, type=Path)
    parser.add_argument("--verification-report-root", required=True, type=Path)


def _add_validate_paths(parser: argparse.ArgumentParser) -> None:
    """Add the frozen direct-Validate bundle from the publication contract."""

    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--canonical-database", required=True, type=Path)
    parser.add_argument("--candidate-database", required=True, type=Path)
    parser.add_argument("--library-root", required=True, type=Path)
    parser.add_argument("--wallhaven-ledger", required=True, type=Path)
    parser.add_argument("--provider-ledger", required=True, type=Path)
    parser.add_argument("--verification-report-root", required=True, type=Path)
    parser.add_argument("--backup-directory", required=True, type=Path)
    parser.add_argument("--recovery-journal", required=True, type=Path)
    parser.add_argument("--recovery-result-root", required=True, type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        prog="publish_gallery_index.py",
        description=(
            "Inspect, prepare, validate, publish, or recover one explicitly "
            "addressed gallery-index publication transaction."
        ),
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    for name, description in (
        ("inspect", "Inspect current explicit paths without writing."),
        ("prepare", "Build and verify a unique candidate when --apply is present."),
        ("validate", "Validate the manifest and current bound evidence read-only."),
        ("publish", "Publish only through a contract-complete core operation."),
    ):
        command = subparsers.add_parser(name, help=description, description=description)
        if name == "validate":
            _add_validate_paths(command)
        else:
            _add_full_paths(command)
            _add_runtime_flags(command)

    recover = subparsers.add_parser(
        "recover",
        help="Recover one journal/backup transaction backward only.",
        description="Recover one journal/backup transaction backward only.",
    )
    _add_recovery_paths(recover)
    _add_runtime_flags(recover)
    return parser


def _read_runtime_evidence(requested: bool) -> dict[str, object] | None:
    if not requested:
        return None
    payload = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    if len(payload) > MAX_STDIN_BYTES:
        raise CliError(
            f"runtime evidence exceeds {MAX_STDIN_BYTES} bytes",
            code="runtime-evidence-too-large",
            exit_code=2,
        )
    if not payload.strip():
        raise CliError(
            "--runtime-evidence-stdin requires one JSON object",
            code="runtime-evidence-missing",
            exit_code=2,
        )
    try:
        value = publication.loads_json_strict(payload)
    except publication.PublicationError as exc:
        raise CliError(
            str(exc), code="runtime-evidence-invalid", exit_code=2
        ) from exc
    if not isinstance(value, Mapping):
        raise CliError(
            "runtime evidence must be a JSON object",
            code="runtime-evidence-invalid",
            exit_code=2,
        )
    return {str(key): item for key, item in value.items()}


def _validate_mode_flags(args: argparse.Namespace) -> None:
    mode = str(args.mode)
    apply = bool(getattr(args, "apply", False))
    cutover_authorized = bool(getattr(args, "cutover_authorized", False))
    runtime_evidence_stdin = bool(
        getattr(args, "runtime_evidence_stdin", False)
    )
    if mode in {"inspect", "validate"} and apply:
        raise CliError(
            f"--apply is invalid for read-only {mode}",
            code="invalid-arguments",
            exit_code=2,
        )
    if mode != "publish" and cutover_authorized:
        raise CliError(
            "--cutover-authorized is accepted only by publish",
            code="invalid-arguments",
            exit_code=2,
        )
    if mode == "publish" and apply and not cutover_authorized:
        raise CliError(
            "publish --apply requires --cutover-authorized",
            code="cutover-authorization-required",
            exit_code=2,
        )
    if apply and mode in {"prepare", "publish", "recover"}:
        if not runtime_evidence_stdin:
            raise CliError(
                f"{mode} --apply requires --runtime-evidence-stdin",
                code="runtime-evidence-required",
                exit_code=2,
            )


def _full_paths(args: argparse.Namespace) -> publication.PublicationPaths:
    return publication.PublicationPaths(
        canonical_database=args.canonical_database,
        candidate_database=args.candidate_database,
        library_root=args.library_root,
        wallhaven_ledger=args.wallhaven_ledger,
        provider_ledger=args.provider_ledger,
        verification_report_root=args.verification_report_root,
        manifest=args.manifest,
        backup_directory=args.backup_directory,
        recovery_journal=args.recovery_journal,
        recovery_result_root=args.recovery_result_root,
        queue_state=args.queue_state_path,
        hold_path=args.hold_path,
        sibling_database=args.sibling_database,
    )


def _validate_paths(args: argparse.Namespace) -> publication.PublicationPaths:
    document = publication.load_json_strict(args.manifest)
    manifest_paths = document.get("paths")
    if not isinstance(manifest_paths, Mapping):
        raise publication.SemanticValidationError(
            "manifest core path evidence is missing"
        )
    inherited: dict[str, Path] = {}
    for name in ("sibling_database", "queue_state", "hold_path"):
        value = manifest_paths.get(name)
        if not isinstance(value, str) or not value:
            raise publication.SemanticValidationError(
                f"manifest path evidence is missing: {name}"
            )
        inherited[name] = Path(value)
    return publication.PublicationPaths(
        canonical_database=args.canonical_database,
        candidate_database=args.candidate_database,
        library_root=args.library_root,
        wallhaven_ledger=args.wallhaven_ledger,
        provider_ledger=args.provider_ledger,
        verification_report_root=args.verification_report_root,
        manifest=args.manifest,
        backup_directory=args.backup_directory,
        recovery_journal=args.recovery_journal,
        recovery_result_root=args.recovery_result_root,
        queue_state=inherited["queue_state"],
        hold_path=inherited["hold_path"],
        sibling_database=inherited["sibling_database"],
    )


def _inspection_generation_id(paths: publication.PublicationPaths) -> str:
    if not Path(paths.manifest).is_file():
        return "inspection"
    document = publication.load_json_strict(paths.manifest)
    candidate = document.get("candidate")
    if not isinstance(candidate, Mapping):
        raise publication.SemanticValidationError(
            "manifest candidate evidence is missing"
        )
    generation_id = candidate.get("generation_id")
    if not isinstance(generation_id, str) or not generation_id:
        raise publication.SemanticValidationError(
            "manifest candidate generation_id is missing"
        )
    return generation_id


def _runtime_summary(evidence: Mapping[str, object] | None) -> dict[str, object] | None:
    if evidence is None:
        return None
    identity = evidence.get("machine_identity")
    machine_id = identity.get("machine_id") if isinstance(identity, Mapping) else None
    return {
        "evidence_version": evidence.get("evidence_version"),
        "machine_id": machine_id,
        "mode": evidence.get("mode"),
        "present": True,
    }


def _require_machine_identity(
    evidence: Mapping[str, object] | None,
) -> dict[str, object]:
    if evidence is None:
        raise CliError(
            "verified machine identity was not supplied",
            code="machine-identity-required",
            exit_code=2,
        )
    identity = evidence.get("machine_identity")
    if not isinstance(identity, Mapping):
        raise CliError(
            "runtime evidence is missing machine_identity",
            code="machine-identity-required",
            exit_code=2,
        )
    keys = (
        "status",
        "machine_id",
        "instance_id",
        "computer_name",
        "qualified_user",
        "verified_at",
        "verifier_path",
    )
    missing = [key for key in keys if key not in identity]
    if missing:
        raise CliError(
            f"machine identity is incomplete: {', '.join(missing)}",
            code="machine-identity-required",
            exit_code=2,
        )
    result = {key: identity[key] for key in keys}
    _validate_schema_fragment(result, "machineIdentity", "$.machine_identity")
    return result


def _validate_schema_fragment(
    value: Mapping[str, object],
    definition_name: str,
    location: str,
) -> None:
    schema = publication.load_manifest_schema()
    definitions = schema.get("$defs")
    if not isinstance(definitions, Mapping):
        raise publication.SchemaValidationError("manifest schema has no $defs")
    definition = definitions.get(definition_name)
    if not isinstance(definition, Mapping):
        raise publication.SchemaValidationError(
            f"manifest schema has no {definition_name} definition"
        )
    publication.validate_json_schema_subset(
        dict(value),
        definition,
        root_schema=schema,
        location=location,
    )


def _require_apply_evidence(
    evidence: Mapping[str, object] | None,
    *,
    mode: str,
) -> dict[str, object]:
    if evidence is None:
        raise CliError(
            f"{mode} --apply requires runtime evidence",
            code="runtime-evidence-required",
            exit_code=2,
        )
    if evidence.get("mode") != mode:
        raise CliError(
            f"runtime evidence mode does not match {mode}",
            code="runtime-evidence-mode-mismatch",
            exit_code=2,
        )
    apply_claims = [
        evidence[key]
        for key in ("apply", "apply_requested")
        if key in evidence
    ]
    if not apply_claims or any(value is not True for value in apply_claims):
        raise CliError(
            "runtime evidence does not record this apply request",
            code="runtime-evidence-apply-mismatch",
            exit_code=2,
        )
    runtime = {str(key): value for key, value in evidence.items()}
    runtime["machine_identity"] = _require_machine_identity(evidence)
    return runtime


def _publish_runtime_evidence(
    args: argparse.Namespace,
    evidence: Mapping[str, object] | None,
    *,
    paths: publication.PublicationPaths,
) -> tuple[dict[str, object], str]:
    runtime = _require_apply_evidence(evidence, mode="publish")
    maintenance = runtime.get("maintenance")
    normalized_maintenance = (
        dict(maintenance)
        if isinstance(maintenance, Mapping)
        else _normalize_raw_publish_maintenance(runtime)
    )
    maintenance_inspections = normalized_maintenance.pop(
        "settled_inspections", None
    )
    raw_inspections = runtime.get("settled_inspections")
    if raw_inspections is None:
        raw_inspections = maintenance_inspections
    elif (
        maintenance_inspections is not None
        and publication.canonical_sha256(raw_inspections)
        != publication.canonical_sha256(maintenance_inspections)
    ):
        raise CliError(
            "top-level and maintenance settled inspections disagree",
            code="settled-evidence-ambiguous",
            exit_code=1,
        )
    if raw_inspections is not None:
        if "settled_samples" in normalized_maintenance:
            raise CliError(
                "maintenance evidence supplies both settled_samples and settled_inspections",
                code="settled-evidence-ambiguous",
                exit_code=1,
            )
        normalized_maintenance["settled_samples"] = _normalize_settled_inspections(
            raw_inspections,
        )
    settled_samples = normalized_maintenance.get("settled_samples")
    if not isinstance(settled_samples, list):
        raise CliError(
            "publish maintenance evidence has no settled database samples",
            code="settled-evidence-required",
            exit_code=1,
        )
    _validate_settled_samples_against_manifest(settled_samples, paths=paths)
    _validate_schema_fragment(
        normalized_maintenance, "maintenance", "$.maintenance"
    )
    runtime["maintenance"] = normalized_maintenance
    runtime["apply"] = bool(args.apply)
    runtime["cutover_authorized"] = bool(args.cutover_authorized)
    identity = runtime["machine_identity"]
    assert isinstance(identity, Mapping)
    authorized_by = identity.get("qualified_user")
    if not isinstance(authorized_by, str) or not authorized_by:
        raise CliError(
            "machine identity has no qualified_user authorization subject",
            code="machine-identity-required",
            exit_code=2,
        )
    return runtime, authorized_by


def _required_mapping(
    container: Mapping[str, object],
    name: str,
    *,
    code: str,
) -> Mapping[str, object]:
    value = container.get(name)
    if not isinstance(value, Mapping):
        raise CliError(
            f"runtime evidence is missing {name}",
            code=code,
            exit_code=1,
        )
    return value


def _normalize_raw_hold(runtime: Mapping[str, object]) -> dict[str, object]:
    raw = _required_mapping(runtime, "hold", code="hold-evidence-required")
    document = _required_mapping(
        raw, "hold_document", code="hold-evidence-required"
    )
    hold_file = _required_mapping(raw, "hold_file", code="hold-evidence-required")
    pause_file = _required_mapping(raw, "pause_file", code="hold-evidence-required")
    state_file = _required_mapping(raw, "state_file", code="hold-evidence-required")
    result = {
        "externally_owned": True,
        "owner": document.get("owner"),
        "token_id": document.get("token_id"),
        "token_sha256": hold_file.get("sha256"),
        "hold_file": dict(hold_file),
        "pause_file": dict(pause_file),
        "queue_state": dict(state_file),
        "queue_state_schema_version": raw.get("queue_state_schema_version"),
        "acquired_at": document.get("acquired_at"),
        "expires_at": document.get("expires_at"),
        "acknowledged_at": document.get("acknowledged_at"),
        "acknowledgement_sha256": document.get("acknowledgement_sha256"),
        "task_acknowledged": document.get("task_acknowledged"),
        "publisher_may_release": False,
    }
    _validate_schema_fragment(result, "hold", "$.maintenance.hold")
    return result


def _normalize_raw_task(
    runtime: Mapping[str, object],
    *,
    acknowledged_at: object,
) -> dict[str, object]:
    raw = _required_mapping(
        runtime, "scheduled_task", code="scheduled-task-evidence-required"
    )
    raw_hold = _required_mapping(runtime, "hold", code="hold-evidence-required")
    token = _required_mapping(
        raw_hold, "hold_document", code="hold-evidence-required"
    )
    token_bindings = {
        "path": token.get("task_path"),
        "name": token.get("task_name"),
        "definition_sha256": token.get("task_definition_sha256"),
    }
    if any(raw.get(name) != value for name, value in token_bindings.items()):
        raise CliError(
            "current scheduled task differs from the externally acknowledged token",
            code="scheduled-task-evidence-invalid",
            exit_code=1,
        )
    result = {
        name: raw.get(name)
        for name in (
            "path",
            "name",
            "definition_sha256",
            "state",
            "last_result",
            "instance_id",
        )
    }
    result["observed_at"] = token.get("task_observed_at")
    result["acknowledged_at"] = acknowledged_at
    _validate_schema_fragment(
        result, "scheduledTaskObservation", "$.maintenance.scheduled_task"
    )
    return result


def _normalize_writer_samples(raw_window: Mapping[str, object]) -> list[dict[str, object]]:
    raw_samples = raw_window.get("samples")
    if not isinstance(raw_samples, list) or len(raw_samples) < 2:
        raise CliError(
            "writer_window has fewer than two samples",
            code="writer-evidence-required",
            exit_code=1,
        )
    samples: list[dict[str, object]] = []
    for index, raw in enumerate(raw_samples):
        if not isinstance(raw, Mapping):
            raise CliError(
                f"writer_window.samples[{index}] is not an object",
                code="writer-evidence-invalid",
                exit_code=1,
            )
        samples.append(
            {
                name: raw.get(name)
                for name in (
                    "sequence",
                    "elapsed_seconds",
                    "sampled_at",
                    "downloader_descendant_count",
                    "index_writer_count",
                    "process_ids",
                )
            }
        )
    return samples


def _normalize_raw_publish_maintenance(
    runtime: Mapping[str, object],
) -> dict[str, object]:
    hold = _normalize_raw_hold(runtime)
    task = _normalize_raw_task(
        runtime,
        acknowledged_at=hold["acknowledged_at"],
    )
    observation = _required_mapping(
        runtime, "observation_window", code="settled-evidence-required"
    )
    writer_window = _required_mapping(
        observation, "writer_window", code="writer-evidence-required"
    )
    settled_inspections = observation.get("settled_inspections")
    if settled_inspections is None:
        settled_inspections = runtime.get("settled_inspections")
    if not isinstance(settled_inspections, list) or len(settled_inspections) < 2:
        raise CliError(
            "publish observation window lacks two settled inspections",
            code="settled-evidence-required",
            exit_code=1,
        )
    first = settled_inspections[0]
    last = settled_inspections[-1]
    if not isinstance(first, Mapping) or not isinstance(last, Mapping):
        raise CliError(
            "settled inspection boundary is malformed",
            code="settled-evidence-invalid",
            exit_code=1,
        )
    cutover_listener = runtime.get("cutover_listener")
    if not isinstance(cutover_listener, Mapping):
        raise CliError(
            "publish requires a complete externally acknowledged cutover_listener receipt",
            code="cutover-listener-evidence-required",
            exit_code=1,
        )
    smoke_port = _required_mapping(
        runtime, "smoke_port_snapshot", code="smoke-port-evidence-required"
    )
    if runtime.get("canonical_handle_free") is not True:
        raise CliError(
            "wrapper evidence did not prove the canonical database set handle-free",
            code="canonical-handle-proof-required",
            exit_code=1,
        )
    return {
        "hold": hold,
        "scheduled_task": task,
        "window_started_at": observation.get("window_started_at"),
        "window_ended_at": observation.get("window_ended_at"),
        "minimum_window_seconds": 30,
        "actual_window_seconds": observation.get("actual_settled_window_seconds"),
        "maximum_evidence_age_seconds": 300,
        "writer_samples": _normalize_writer_samples(writer_window),
        "settled_inspections": settled_inspections,
        # These are provisional freshness bounds for the pre-publication
        # receipt.  The core replaces them with the actual under-hold verifier
        # interval before the ready-state compare-and-swap.
        "verification_started_at": runtime.get("captured_at"),
        "verification_completed_at": runtime.get("captured_at"),
        "evidence_checked_at": runtime.get("captured_at"),
        "fingerprints_stable": True,
        "durable_inputs_current": True,
        "canonical_wal_checkpointed": True,
        "canonical_shm_handle_free": True,
        "cutover_listener": dict(cutover_listener),
        "smoke_port": dict(smoke_port),
    }


def _normalize_settled_inspections(
    raw: object,
) -> list[dict[str, object]]:
    if not isinstance(raw, list) or len(raw) < 2:
        raise CliError(
            "settled_inspections must contain at least two observations",
            code="settled-evidence-required",
            exit_code=1,
        )
    samples: list[dict[str, object]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise CliError(
                f"settled_inspections[{index}] is not an object",
                code="settled-evidence-invalid",
                exit_code=1,
            )
        inspection = item.get("inspection")
        if not isinstance(inspection, Mapping):
            raise CliError(
                f"settled_inspections[{index}] has no inspection object",
                code="settled-evidence-invalid",
                exit_code=1,
            )
        nested_result = inspection.get("result")
        if isinstance(nested_result, Mapping):
            inspection = nested_result
        candidate = inspection.get("candidate")
        canonical = inspection.get("canonical")
        durable = inspection.get("durable_inputs")
        if not all(isinstance(value, Mapping) for value in (candidate, canonical, durable)):
            raise CliError(
                f"settled_inspections[{index}] lacks candidate/canonical/input evidence",
                code="settled-evidence-invalid",
                exit_code=1,
            )
        assert isinstance(candidate, Mapping)
        assert isinstance(canonical, Mapping)
        assert isinstance(durable, Mapping)
        candidate_database = candidate.get("database")
        canonical_database = canonical.get("database")
        if not isinstance(candidate_database, Mapping) or not isinstance(
            canonical_database, Mapping
        ):
            raise CliError(
                f"settled_inspections[{index}] has no database-set identity",
                code="settled-evidence-invalid",
                exit_code=1,
            )
        samples.append(
            {
                "sequence": item.get("sequence"),
                "elapsed_seconds": item.get("elapsed_seconds"),
                "sampled_at": item.get("sampled_at"),
                "candidate": dict(candidate_database),
                "canonical": dict(canonical_database),
                "durable_inputs_sha256": durable.get("aggregate_sha256"),
            }
        )
    return samples


def _validate_settled_samples_against_manifest(
    samples: list[dict[str, object]],
    *,
    paths: publication.PublicationPaths,
) -> None:
    publication.validate_settled_samples(samples)
    manifest = publication.validate_manifest_file(
        paths.manifest,
        expected_paths=paths.explicit_mapping(),
        check_current=True,
    )
    candidate = manifest.document.get("candidate")
    durable = manifest.document.get("durable_inputs")
    if not isinstance(candidate, Mapping) or not isinstance(durable, Mapping):
        raise publication.SemanticValidationError(
            "manifest generation-bound evidence is missing"
        )
    candidate_database = candidate.get("database")
    if not isinstance(candidate_database, Mapping):
        raise publication.SemanticValidationError(
            "manifest candidate database evidence is missing"
        )
    expected_candidate = candidate_database.get("aggregate_sha256")
    expected_inputs = durable.get("aggregate_sha256")
    expected_generation = candidate.get("generation_id")
    if expected_generation != durable.get("generation_id"):
        raise publication.SemanticValidationError(
            "manifest generation-bound input evidence disagrees"
        )
    for sample in samples:
        sampled_candidate = sample.get("candidate")
        if not isinstance(sampled_candidate, Mapping):
            raise publication.SemanticValidationError(
                "settled candidate evidence is malformed"
            )
        if sampled_candidate.get("aggregate_sha256") != expected_candidate:
            raise publication.SemanticValidationError(
                "settled candidate identity differs from the manifest"
            )
        if sample.get("durable_inputs_sha256") != expected_inputs:
            raise publication.SemanticValidationError(
                "settled durable inputs differ from the manifest generation"
            )


def _recover_runtime_evidence(
    evidence: Mapping[str, object] | None,
    *,
    queue_state: Path,
    hold_path: Path,
) -> dict[str, object]:
    runtime = _require_apply_evidence(evidence, mode="recover")
    recovery_hold = runtime.get("recovery_hold")
    if not isinstance(recovery_hold, Mapping):
        raise CliError(
            "recover runtime evidence is missing a journal-bound recovery_hold",
            code="recovery-hold-required",
            exit_code=1,
        )
    _validate_schema_fragment(recovery_hold, "recoveryHold", "$.recovery_hold")
    runtime["recovery_hold"] = dict(recovery_hold)
    hold = recovery_hold.get("hold")
    if not isinstance(hold, Mapping):
        raise CliError(
            "recovery_hold has no current external hold",
            code="recovery-hold-required",
            exit_code=1,
        )
    expected_paths = {
        "hold_file": Path(hold_path),
        "pause_file": Path(queue_state) / "pause.flag",
        "queue_state": Path(queue_state) / "state.json",
    }
    for name, expected in expected_paths.items():
        identity = hold.get(name)
        if not isinstance(identity, Mapping) or "path" not in identity:
            raise CliError(
                f"recovery hold is missing {name} path evidence",
                code="recovery-hold-path-mismatch",
                exit_code=1,
            )
        if publication.path_key(str(identity["path"])) != publication.path_key(expected):
            raise CliError(
                f"recovery hold {name} does not match the explicit CLI path",
                code="recovery-hold-path-mismatch",
                exit_code=1,
            )
    return runtime


def _candidate_builder(
    candidate_database: Path,
    library_root: Path,
    wallhaven_ledger: Path,
    provider_ledger: Path,
) -> Mapping[str, object]:
    """Create-new candidate builder passed to the deterministic core."""

    candidate = Path(candidate_database)
    candidate.parent.mkdir(parents=True, exist_ok=True)
    # sqlite3 has no create-exclusive URI mode.  Reserve the exact output name
    # before opening it so a concurrent creator cannot be overwritten.
    with candidate.open("xb") as handle:
        handle.flush()
        os.fsync(handle.fileno())

    connection = None
    primary: BaseException | None = None
    try:
        connection = index_library.connect(candidate)
        stats = index_library.ingest_library(
            connection,
            Path(library_root),
            ledger_path=Path(wallhaven_ledger),
            provider_ledger_path=Path(provider_ledger),
        )
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or int(checkpoint[0]) != 0:
            raise RuntimeError("candidate WAL checkpoint did not complete")
        return dict(stats)
    except BaseException as exc:
        primary = exc
        if connection is not None:
            try:
                connection.rollback()
            except BaseException as cleanup_error:
                if hasattr(exc, "add_note"):
                    exc.add_note(
                        "candidate rollback also failed: "
                        + _bounded(cleanup_error)
                    )
        raise
    finally:
        if connection is not None:
            try:
                connection.close()
            except BaseException as cleanup_error:
                if primary is None:
                    raise
                if hasattr(primary, "add_note"):
                    primary.add_note(
                        "candidate connection close also failed: "
                        + _bounded(cleanup_error)
                    )


def _prepare_result(outcome: publication.PrepareOutcome) -> dict[str, object]:
    return {
        "build_stats": dict(outcome.build_stats),
        "candidate": outcome.candidate.as_dict(),
        "durable_inputs": outcome.durable_inputs.as_dict(),
        "generation_id": outcome.generation_id,
        "manifest": {
            "path": str(outcome.manifest.path),
            "sha256": outcome.manifest.sha256,
            "state": outcome.manifest.document.get("state"),
        },
        "sqlite": outcome.sqlite.as_dict(),
        "verification": dict(outcome.verification),
    }


def _publish_result(outcome: publication.PublishOutcome) -> dict[str, object]:
    activation = outcome.activation
    return {
        "activation": {
            "backup": activation.backup.as_dict(),
            "candidate": activation.candidate.as_dict(),
            "candidate_sqlite": activation.candidate_sqlite.as_dict(),
            "canonical": activation.canonical.as_dict(),
            "canonical_before": activation.canonical_before.as_dict(),
            "canonical_before_sqlite": activation.canonical_before_sqlite.as_dict(),
            "journal": activation.journal.as_dict(),
            "journal_evidence": dict(activation.journal_evidence),
            "rolled_back": activation.rolled_back,
            "sqlite": activation.sqlite.as_dict(),
        },
        "manifest": {
            "path": str(outcome.manifest.path),
            "sha256": outcome.manifest.sha256,
            "state": outcome.manifest.document.get("state"),
        },
        "post_verification": dict(outcome.post_verification),
    }


def _manifest_result(
    manifest: publication.ValidatedManifest,
) -> dict[str, object]:
    return {
        "path": str(manifest.path),
        "sha256": manifest.sha256,
        "state": manifest.document.get("state"),
        "result_status": (
            manifest.document.get("result", {}).get("status")
            if isinstance(manifest.document.get("result"), Mapping)
            else None
        ),
    }


def _database_member_preview(main_path: Path) -> dict[str, object]:
    main = Path(main_path)
    return {
        name: publication.fingerprint_file(path, allow_absent=True).as_dict()
        for name, path in (
            ("main", main),
            ("wal", Path(str(main) + "-wal")),
            ("shm", Path(str(main) + "-shm")),
        )
    }


def _continuation_segments(
    runtime: Mapping[str, object],
) -> tuple[Path, ...]:
    raw = runtime.get("continuation_segments", [])
    if not isinstance(raw, list):
        raise CliError(
            "continuation_segments must be a JSON array",
            code="recovery-continuations-invalid",
            exit_code=2,
        )
    result: list[Path] = []
    for index, value in enumerate(raw):
        if not isinstance(value, str) or not value:
            raise CliError(
                f"continuation_segments[{index}] must be a non-empty path string",
                code="recovery-continuations-invalid",
                exit_code=2,
            )
        result.append(Path(value))
    return tuple(result)


def _recovery_anchor_preview(
    chain: publication.JournalChain,
) -> dict[str, object]:
    first_segment = chain.segments[0]
    header = first_segment.get("header")
    if not isinstance(header, Mapping):
        raise publication.SemanticValidationError(
            "journal has no pre-activation header anchor"
        )
    manifest_path = header.get("pre_activation_manifest_path")
    if not isinstance(manifest_path, str) or not manifest_path:
        raise publication.SemanticValidationError(
            "journal has no pre-activation manifest path"
        )
    manifest_identity = publication.fingerprint_file(
        Path(manifest_path), require_nonempty=True
    )
    if manifest_identity.sha256 != header.get("pre_activation_manifest_sha256"):
        raise publication.SemanticValidationError(
            "pre-activation manifest anchor changed"
        )
    manifest = publication.load_json_strict(Path(manifest_path))
    maintenance = manifest.get("maintenance")
    hold = maintenance.get("hold") if isinstance(maintenance, Mapping) else None
    token_sha256 = hold.get("token_sha256") if isinstance(hold, Mapping) else None
    if not isinstance(token_sha256, str) or not token_sha256:
        raise publication.SemanticValidationError(
            "pre-activation manifest has no historical hold token hash"
        )
    return {
        "historical_hold_token_sha256": token_sha256,
        "pre_activation_manifest_path": manifest_path,
        "pre_activation_manifest_sha256": header.get(
            "pre_activation_manifest_sha256"
        ),
    }


def _dispatch_full(
    args: argparse.Namespace,
    evidence: Mapping[str, object] | None,
) -> dict[str, object]:
    mode = str(args.mode)

    if mode == "validate":
        paths = _validate_paths(args)
        validated = publication.validate_manifest_file(
            paths.manifest,
            expected_paths=paths.explicit_mapping(),
            check_current=True,
        )
        return {
            "applied": False,
            "mode": mode,
            "ok": True,
            "result": {"manifest": _manifest_result(validated)},
            "status": "valid",
        }

    paths = _full_paths(args)

    if mode == "inspect":
        return {
            "applied": False,
            "mode": mode,
            "ok": True,
            "result": publication.inspect_publication(
                paths,
                generation_id=_inspection_generation_id(paths),
            ),
            "status": "inspected",
        }

    if mode == "prepare":
        if not args.apply:
            return {
                "applied": False,
                "mode": mode,
                "ok": True,
                "result": {
                    "inspection": publication.inspect_publication(paths),
                    "runtime_evidence": _runtime_summary(evidence),
                    "transition": "candidate-built -> candidate-verified",
                },
                "status": "would-prepare",
            }
        outcome = publication.prepare_candidate(
            paths,
            machine_identity=_require_machine_identity(evidence),
            builder=_candidate_builder,
            verifier=index_library.verify_library,
        )
        return {
            "applied": True,
            "mode": mode,
            "ok": True,
            "result": _prepare_result(outcome),
            "status": "candidate-verified",
        }

    if mode == "publish":
        if not args.apply:
            validated = publication.validate_manifest_file(
                paths.manifest,
                expected_paths=paths.explicit_mapping(),
                check_current=True,
            )
            return {
                "applied": False,
                "mode": mode,
                "ok": True,
                "result": {
                    "manifest_sha256": validated.sha256,
                    "manifest_state": validated.document.get("state"),
                    "runtime_evidence": _runtime_summary(evidence),
                    "transition": "candidate-verified -> ready-to-publish -> published",
                },
                "status": "would-publish",
            }
        runtime, authorized_by = _publish_runtime_evidence(
            args,
            evidence,
            paths=paths,
        )
        outcome = publication.publish_candidate(
            paths,
            runtime_evidence=runtime,
            verifier=index_library.verify_library,
            canonical_handle_free=publication.database_handles_free,
            authorized_by=authorized_by,
        )
        return {
            "applied": True,
            "mode": mode,
            "ok": True,
            "result": _publish_result(outcome),
            "status": "published",
        }

    raise CliError(
        f"unsupported full-path mode: {mode}",
        code="invalid-arguments",
        exit_code=2,
    )


def _dispatch_recover(
    args: argparse.Namespace,
    evidence: Mapping[str, object] | None,
) -> dict[str, object]:
    if args.apply:
        runtime = _recover_runtime_evidence(
            evidence,
            queue_state=args.queue_state_path,
            hold_path=args.hold_path,
        )
        recovered = publication.recover_publication(
            canonical_database=args.canonical_database,
            backup_directory=args.backup_directory,
            recovery_journal=args.recovery_journal,
            recovery_result_root=args.recovery_result_root,
            manifest=args.manifest,
            runtime_evidence=runtime,
            canonical_handle_free=publication.database_handles_free,
            continuation_segments=_continuation_segments(runtime),
        )
        return {
            "applied": True,
            "mode": "recover",
            "ok": True,
            "result": {"manifest": _manifest_result(recovered)},
            "status": "recovered",
        }

    preview_continuations = (
        _continuation_segments(evidence) if evidence is not None else ()
    )
    try:
        chain = publication.parse_journal_chain(
            args.recovery_journal,
            continuation_segments=preview_continuations,
        )
    except (publication.PublicationError, OSError):
        if publication._journal_has_valid_header(args.recovery_journal):
            # A valid authenticated header commits this transaction to normal
            # continuation recovery.  Later corruption is never relabelled as
            # an unstarted pre-journal failure.
            raise
        validated = publication.validate_manifest_file(
            args.manifest,
            check_current=False,
        )
        if validated.document.get("state") != "ready-to-publish":
            raise
        stored_paths = validated.document.get("paths")
        maintenance = validated.document.get("maintenance")
        historical_hold = (
            maintenance.get("hold") if isinstance(maintenance, Mapping) else None
        )
        if not isinstance(stored_paths, Mapping) or not isinstance(
            historical_hold, Mapping
        ):
            raise publication.SemanticValidationError(
                "pre-journal recovery manifest lacks paths/hold evidence"
            )
        explicit_paths = {
            "canonical_database": args.canonical_database,
            "backup_directory": args.backup_directory,
            "recovery_journal": args.recovery_journal,
            "recovery_result_root": args.recovery_result_root,
            "queue_state": args.queue_state_path,
            "hold_path": args.hold_path,
            "manifest": args.manifest,
        }
        for name, explicit in explicit_paths.items():
            if publication.path_key(str(stored_paths.get(name))) != publication.path_key(
                explicit
            ):
                raise publication.SemanticValidationError(
                    f"pre-journal recovery path differs from manifest: {name}"
                )
        candidate = validated.document.get("candidate")
        generation_id = (
            candidate.get("generation_id") if isinstance(candidate, Mapping) else None
        )
        anchor = {
            "historical_hold_token_sha256": historical_hold.get("token_sha256"),
            "pre_activation_manifest_path": str(args.manifest),
            "pre_activation_manifest_sha256": validated.sha256,
        }
        return {
            "applied": False,
            "mode": "recover",
            "ok": True,
            "result": {
                "anchor": anchor,
                "backup": _database_member_preview(
                    Path(args.backup_directory) / Path(args.canonical_database).name
                ),
                "canonical": _database_member_preview(args.canonical_database),
                "journal": {
                    "derived_status": "pre-journal-recovery-required",
                    "generation_id": generation_id,
                    "head_sha256": None,
                    "tail_segment_sha256": None,
                    "transaction_id": None,
                },
                "runtime_evidence": _runtime_summary(evidence),
                "transition": "pre-journal recovery close",
            },
            "status": "would-recover",
        }
    backup_main = Path(args.backup_directory) / Path(args.canonical_database).name
    anchor = _recovery_anchor_preview(chain)
    preview = {
        "anchor": anchor,
        "backup": _database_member_preview(backup_main),
        "canonical": _database_member_preview(args.canonical_database),
        "journal": {
            "derived_status": chain.derived_status,
            "generation_id": chain.generation_id,
            "head_sha256": chain.head_sha256,
            "tail_segment_sha256": chain.tail_segment_sha256,
            "transaction_id": chain.transaction_id,
        },
        "runtime_evidence": _runtime_summary(evidence),
        "transition": "backward-only recovery",
    }
    return {
        "applied": False,
        "mode": "recover",
        "ok": True,
        "result": preview,
        "status": "would-recover",
    }


def _mode_hint(argv: Sequence[str] | None) -> str:
    values = list(sys.argv[1:] if argv is None else argv)
    return values[0] if values and values[0] in {
        "inspect", "prepare", "validate", "publish", "recover"
    } else "unknown"


def main(argv: Sequence[str] | None = None) -> int:
    # Existing ingest helpers log individual malformed inputs.  The CLI's
    # machine-readable result is authoritative and stderr must stay bounded.
    logging.disable(logging.CRITICAL)
    mode = _mode_hint(argv)
    try:
        args = build_parser().parse_args(argv)
        mode = str(args.mode)
        _validate_mode_flags(args)
        evidence = _read_runtime_evidence(
            bool(getattr(args, "runtime_evidence_stdin", False))
        )
        if mode == "recover":
            payload = _dispatch_recover(args, evidence)
        else:
            payload = _dispatch_full(args, evidence)
        _emit(payload)
        return 0
    except (KeyboardInterrupt, SystemExit):
        raise
    except CliError as exc:
        _emit(
            _error_payload(
                mode,
                code=exc.code,
                message=exc,
                applied=False,
            )
        )
        return exc.exit_code
    except publication.PublicationError as exc:
        _emit(
            _error_payload(
                mode,
                code=getattr(exc, "code", "publication-error"),
                message=exc,
                applied=False,
            )
        )
        return 1
    except (OSError, ValueError) as exc:
        _emit(
            _error_payload(
                mode,
                code="input-or-filesystem-error",
                message=exc,
                applied=False,
            )
        )
        return 2
    except Exception as exc:
        # Do not leak a traceback or an unbounded exception representation into
        # an automation channel.  Unexpected failures remain fail-closed.
        message = _bounded(exc)
        sys.stderr.write(f"gallery publication CLI failed: {message}\n")
        _emit(
            _error_payload(
                mode,
                code="internal-error",
                message=message,
                applied=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
