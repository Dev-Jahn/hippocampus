"""Focused review finding commands; public CLI wiring belongs to the later cut-over task."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Mapping

import yaml

from waystone.core import WorkflowError
from waystone.features import review_layout
from waystone.project import load_config, load_tasks, require_initialized_root, hold_project_lock
from waystone.project.context import (
    CanonicalRootIsLinkedWorktree,
    ProjectContext,
    resolve_project_context,
)
from waystone.project import tasks_cli
from waystone.reviews import findings


_HEADING_RE = re.compile(
    r"(?m)^#{2,4}\s*(?P<id>[A-Za-z][A-Za-z0-9_-]*-[A-Za-z][A-Za-z0-9_-]*-\d+)\s*[—-]\s*(?P<title>.+?)\s*$")
_TABLE_RE = re.compile(
    r"(?m)^\|\s*(?P<id>[A-Za-z][A-Za-z0-9_-]*-[A-Za-z][A-Za-z0-9_-]*-\d+)\s*[—-]\s*(?P<title>[^|]+?)\s*\|\s*(?P<impact>blocker|major|minor)\s*\|(?P<body>[^\n]*)$")
_SEVERITY_RE = re.compile(r"(?im)^\s*(?:severity|impact)\s*:\s*(blocker|major|minor)\s*$")


class ReviewGroupError(WorkflowError):
    """A review-group command could not complete without hiding a failed path."""


class MaterializationRefused(ReviewGroupError):
    code = "finding-materialization-refused"


class ReviewContextRequired(ReviewGroupError):
    code = "review-context-required"


def _canonical_review_root(context: ProjectContext) -> Path:
    if not isinstance(context, ProjectContext):
        raise ReviewContextRequired(
            "review mutation requires canonical ProjectContext proof")
    if not context.is_canonical_checkout:
        raise CanonicalRootIsLinkedWorktree(
            "review mutation cannot use a linked worktree as project authority")
    return context.canonical_root


def _reviews_dir(root: Path) -> Path:
    config = load_config(root)
    configured = Path(config.get("reviews_dir", "docs/reviews"))
    if configured.is_absolute() or ".." in configured.parts:
        raise ReviewGroupError("reviews_dir must be a relative path inside the project")
    return root / configured


def _parse_structured(raw: bytes) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    try:
        document = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return None, []
    if not isinstance(document, Mapping) or not isinstance(document.get("findings"), list):
        return None, []
    rows = [dict(item) for item in document["findings"] if isinstance(item, Mapping)]
    if len(rows) != len(document["findings"]):
        raise ReviewGroupError("structured review feedback findings must be mappings")
    return dict(document), rows


def _parse_markdown(raw: bytes) -> list[dict[str, Any]]:
    text = raw.decode("utf-8")
    matches = list(_HEADING_RE.finditer(text))
    rows: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.start():end].strip()
        severity = _SEVERITY_RE.search(block)
        rows.append({
            "source_finding_id": match.group("id"),
            "claim": block,
            "evidence": [block],
            "impact": severity.group(1).lower() if severity else "minor",
        })
    if rows:
        return rows
    for match in _TABLE_RE.finditer(text):
        rows.append({
            "source_finding_id": match.group("id"),
            "claim": match.group(0),
            "evidence": [match.group("body").strip() or match.group(0)],
            "impact": match.group("impact"),
        })
    return rows


def _feedback_claim_rows(raw: bytes) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    document, rows = _parse_structured(raw)
    if document is not None:
        return document, rows
    return {}, _parse_markdown(raw)


def _target(target: Mapping[str, Any] | None, feedback_digest: str) -> dict[str, Any]:
    row = dict(target or {})
    row.setdefault("review_artifact_digest", feedback_digest)
    for field in ("run_spec_digest", "result_digest", "review_artifact_digest"):
        if field not in row:
            raise ReviewGroupError(
                f"review ingest: target.{field} is required; provide structured feedback metadata "
                "or the target argument")
    return row


def ingest_feedback(
        context: ProjectContext, run_id: str, feedback_file: Path, *,
        target: Mapping[str, Any] | None = None,
        binding_digest: str | None = None,
        principal: str | None = None,
) -> tuple[findings.Artifact, ...]:
    """Preserve feedback and create one immutable claim for each structured finding."""
    root = _canonical_review_root(context)
    require_initialized_root(root)
    run_id = review_layout.require_uuid7(run_id)
    source = Path(feedback_file)
    try:
        raw = source.read_bytes()
    except OSError as error:
        raise ReviewGroupError(f"review ingest: feedback unavailable {source}: {error}") from error
    reviews_dir = _reviews_dir(root)
    canonical_feedback = review_layout.bind_markdown_run_id(raw, run_id)
    feedback_digest = findings.artifact_digest(canonical_feedback)
    metadata, rows = _feedback_claim_rows(raw)
    target_row = _target(target or metadata.get("target"), feedback_digest)
    binding_digest = binding_digest or metadata.get("binding_digest")
    if not isinstance(binding_digest, str):
        raise ReviewGroupError(
            "review ingest: reviewer binding digest is required in metadata or binding_digest")
    reported_by = metadata.get("reported_by") or {
        "role": "reviewer", "binding_digest": binding_digest, "principal": principal,
    }
    claims: list[findings.Artifact] = []
    for row in rows:
        finding_id = row.get("finding_id") or review_layout.new_run_id()
        claim = {
            "schema": findings.CLAIM_SCHEMA,
            "finding_id": finding_id,
            "review_run_id": run_id,
            "target": target_row,
            "source_finding_id": row.get("source_finding_id") or row.get("id"),
            "claim": row.get("claim") or row.get("title"),
            "evidence": row.get("evidence") or [row.get("claim") or row.get("title")],
            "reviewer_assessment": {
                "impact": row.get("impact") or row.get("severity") or "minor",
                "suggested_remediation": row.get("suggested_remediation"),
            },
            "reported_by": reported_by,
        }
        findings.validate_claim(claim)
        claims.append(findings._artifact(claim))

    feedback_path = review_layout.canonical_artifact_path(
        reviews_dir, run_id, review_layout.FEEDBACK)
    if feedback_path.exists() and not feedback_path.is_symlink():
        raise ReviewGroupError(f"review ingest: feedback already exists: {feedback_path}")
    for claim in claims:
        claim_path = review_layout.canonical_finding_path(
            reviews_dir, run_id, claim.payload["finding_id"], review_layout.FINDING_CLAIM)
        if claim_path.exists() or claim_path.is_symlink():
            raise ReviewGroupError(f"review ingest: claim already exists: {claim_path}")
    review_layout.publish_markdown(reviews_dir, run_id, review_layout.FEEDBACK, raw)
    published: list[findings.Artifact] = []
    try:
        for claim in claims:
            published.append(findings.write_claim(reviews_dir, claim.payload))
    except Exception:
        # The feedback and any successfully published claims remain immutable evidence. The caller
        # receives the failure instead of a false success or destructive rollback.
        raise
    return tuple(published)


def ingest(
        context: ProjectContext,
        run_id: str,
        feedback_file: Path,
        **kwargs,
) -> tuple[findings.Artifact, ...]:
    return ingest_feedback(context, run_id, feedback_file, **kwargs)


def validate_file(
        context: ProjectContext,
        run_id: str,
        finding_id: str,
        source_file: Path,
) -> findings.Artifact:
    root = _canonical_review_root(context)
    require_initialized_root(root)
    raw = Path(source_file).read_bytes()
    payload = findings.parse_artifact(raw, findings.VALIDATION_SCHEMA)
    return findings.append_validation(
        _reviews_dir(root), run_id, finding_id, payload, root=root)


def disposition_file(
        context: ProjectContext,
        run_id: str,
        finding_id: str,
        source_file: Path,
) -> findings.Artifact:
    root = _canonical_review_root(context)
    require_initialized_root(root)
    raw = Path(source_file).read_bytes()
    payload = findings.parse_artifact(raw, findings.DISPOSITION_SCHEMA)
    return findings.append_disposition(
        _reviews_dir(root), run_id, finding_id, payload, root=root)


def resolve_finding_run(root: Path, finding_id: str) -> str:
    """Resolve a finding UUID only when exactly one canonical run owns it."""
    reviews_dir = _reviews_dir(Path(root).resolve())
    runs = reviews_dir / "runs"
    matches = []
    if runs.is_dir() and not runs.is_symlink():
        for run_directory in sorted(runs.iterdir()):
            if not run_directory.is_dir() or run_directory.is_symlink():
                continue
            if (run_directory / "findings" / finding_id).is_dir():
                matches.append(run_directory.name)
    if len(matches) != 1:
        raise ReviewGroupError(
            f"finding {finding_id!r} must resolve to exactly one canonical run (found {len(matches)})")
    return review_layout.require_uuid7(matches[0])


def _task_id(finding_id: str) -> str:
    return "fix/review-finding-" + finding_id.replace("-", "")[:32]


def materialize(context: ProjectContext, run_id: str, finding_id: str) -> str:
    """Materialize only explicitly selected remediation dispositions into tasks.yaml."""
    if not isinstance(context, ProjectContext):
        raise MaterializationRefused(
            "finding materialization requires canonical ProjectContext proof")
    if not context.is_canonical_checkout:
        raise CanonicalRootIsLinkedWorktree(
            "review mutation cannot use a linked worktree as project authority")
    root = context.canonical_root
    require_initialized_root(root)
    reviews_dir = _reviews_dir(root)
    projection = findings.load_finding(reviews_dir, run_id, finding_id)
    claim = projection["claim"]
    validation = projection["validation"]
    disposition = projection["disposition"]
    if disposition is None:
        raise MaterializationRefused("finding has no disposition")
    if validation is None or disposition.payload["confirmed_validation_digest"] != validation.digest:
        raise findings.StaleDisposition(
            "latest validation changed after this disposition; record a new disposition revision")
    findings.validate_validation_authority(root, validation.payload)
    findings.validate_disposition_authority(root, disposition.payload)
    row = disposition.payload
    if row["disposition"] not in ("fix-now", "fix-before-promotion"):
        raise MaterializationRefused(
            f"disposition {row['disposition']} does not select executable work")
    task_id = row.get("materialized_task_id") or _task_id(finding_id)
    existing = {task.get("id"): task for task in (load_tasks(root).get("tasks") or [])
                if isinstance(task, Mapping)}
    if task_id not in existing:
        fields = {
            "id": task_id,
            "title": f"Remediate confirmed review finding {claim.payload.get('source_finding_id') or finding_id}",
            "status": "pending",
            "severity": row["impact"],
            "origin": f"review-finding-{finding_id}",
            "notes": f"finding_id={finding_id}; disposition={disposition.digest}",
        }
        with hold_project_lock(root):
            if tasks_cli.cmd_add(root, fields) != 0:
                raise MaterializationRefused(f"task registry rejected materialization {task_id}")
    if row.get("materialized_task_id") != task_id:
        revision = dict(row)
        revision["revision"] = row["revision"] + 1
        revision["supersedes_digest"] = disposition.digest
        revision["materialized_task_id"] = task_id
        findings.append_disposition(
            reviews_dir, run_id, finding_id, revision, root=root)
    return task_id


def _review_context(value: str | None) -> ProjectContext:
    context = resolve_project_context(Path.cwd() if value is None else Path(value))
    if not context.is_canonical_checkout:
        raise CanonicalRootIsLinkedWorktree(
            "review mutation cannot use a linked worktree as project authority")
    return context


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="waystone review")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest_parser = sub.add_parser("ingest")
    ingest_parser.add_argument("run_id")
    ingest_parser.add_argument("--file", type=Path, required=True)
    ingest_parser.add_argument("--root")
    ingest_parser.add_argument("--binding-digest")
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("finding_id")
    validate_parser.add_argument("--run-id")
    validate_parser.add_argument("--file", type=Path, required=True)
    validate_parser.add_argument("--root")
    disposition_parser = sub.add_parser("disposition")
    disposition_parser.add_argument("finding_id")
    disposition_parser.add_argument("--run-id")
    disposition_parser.add_argument("--file", type=Path, required=True)
    disposition_parser.add_argument("--root")
    materialize_parser = sub.add_parser("materialize")
    materialize_parser.add_argument("finding_id")
    materialize_parser.add_argument("--run-id")
    materialize_parser.add_argument("--root")
    args = parser.parse_args(argv)
    try:
        context = _review_context(getattr(args, "root", None))
        root = context.canonical_root
        if args.command == "ingest":
            result = ingest_feedback(
                context, args.run_id, args.file, binding_digest=args.binding_digest)
            print(f"review ingest: preserved feedback and recorded {len(result)} claim(s)")
        elif args.command == "validate":
            run_id = args.run_id or resolve_finding_run(root, args.finding_id)
            result = validate_file(context, run_id, args.finding_id, args.file)
            print(f"review validate: recorded {result.payload['revision']:04d}.yaml")
        elif args.command == "disposition":
            run_id = args.run_id or resolve_finding_run(root, args.finding_id)
            result = disposition_file(context, run_id, args.finding_id, args.file)
            print(f"review disposition: recorded {result.payload['revision']:04d}.yaml")
        else:
            run_id = args.run_id or resolve_finding_run(root, args.finding_id)
            task_id = materialize(context, run_id, args.finding_id)
            print(f"review materialize: {task_id}")
        return 0
    except (FindingError, ReviewGroupError, WorkflowError, OSError, yaml.YAMLError) as error:
        print(f"waystone review: {error}", file=sys.stderr)
        return 1


# Re-export the domain error for the CLI catch without making callers import two modules.
FindingError = findings.FindingError


def attach_review(
        context: ProjectContext,
        promotion_run_id: str,
        review_run_id: str,
):
    """Attach one ingested reviewer result to its exact frozen promotion lineage."""
    from waystone.jobs.domain import Role
    from waystone.jobs.profile import assemble_run
    from waystone.runs.assurance import ReviewerEvidence
    from waystone.runs.engine import StagedRunEngine
    from waystone.runs.spec import load_run_spec

    root = _canonical_review_root(context)
    require_initialized_root(root)
    reviews_dir = _reviews_dir(root)
    feedback = review_layout.read_canonical_artifact(
        reviews_dir,
        review_layout.canonical_artifact_path(
            reviews_dir, review_run_id, review_layout.FEEDBACK),
    )
    raw = feedback["bytes"]
    body = raw.split(b"\n", 1)[1] if raw.startswith(b"<!-- waystone-review-artifact:") else raw
    metadata, _rows = _parse_structured(body)
    if metadata is None:
        raise ReviewGroupError(
            "review attach requires structured ingested feedback metadata")
    with assemble_run(context) as assembly:
        spec = load_run_spec(promotion_run_id, start=assembly.context.canonical_root)
        if (spec.lifecycle_stage.value != "promote"
                or spec.promotion_lineage is None
                or not isinstance(spec.candidate, Mapping)):
            raise ReviewGroupError("review attach target is not a frozen promotion run")
        target = metadata.get("target")
        if not isinstance(target, Mapping):
            raise ReviewGroupError("review attach feedback lacks target metadata")
        feedback_digest = findings.artifact_digest(raw)
        expected_result = spec.candidate["producer_result_digest"]
        if (target.get("run_spec_digest") != spec.run_spec_digest
                or target.get("result_digest") != expected_result
                or target.get("review_artifact_digest", feedback_digest) != feedback_digest):
            raise ReviewGroupError(
                "review attach feedback names a different RunSpec, candidate result, or artifact")
        expected_binding = assembly.profile.binding_for(Role.REVIEWER).binding_digest
        reported_by = metadata.get("reported_by")
        if reported_by is None:
            reported_by = {
                "role": "reviewer",
                "binding_digest": metadata.get("binding_digest"),
                "principal": None,
            }
        if (not isinstance(reported_by, Mapping)
                or reported_by.get("role") != "reviewer"
                or reported_by.get("binding_digest") != expected_binding):
            raise ReviewGroupError(
                "review attach actor differs from the frozen reviewer binding")

        finding_digests = []
        findings_root = review_layout.canonical_run_directory(
            reviews_dir, review_run_id) / "findings"
        if findings_root.is_dir() and not findings_root.is_symlink():
            for finding_dir in sorted(findings_root.iterdir()):
                if not finding_dir.is_dir() or finding_dir.is_symlink():
                    raise ReviewGroupError("review attach finding directory is unsafe")
                claim = findings.read_claim(
                    reviews_dir, review_run_id, finding_dir.name)
                claim_target = claim.payload["target"]
                claim_actor = claim.payload["reported_by"]
                if (claim_target["run_spec_digest"] != spec.run_spec_digest
                        or claim_target["result_digest"] != expected_result
                        or claim_target["review_artifact_digest"] != feedback_digest
                        or claim_actor["binding_digest"] != expected_binding):
                    raise ReviewGroupError(
                        "review attach claim differs from feedback target or reviewer binding")
                finding_digests.append(claim.digest)
        reviewer = ReviewerEvidence(
            promotion_lineage_id=spec.promotion_lineage.id,
            target_run_spec_digest=spec.run_spec_digest,
            candidate_digest=spec.candidate["digest"],
            target_result_digest=expected_result,
            review_artifact_digest=feedback_digest,
            actor={"actor_id": expected_binding, "role": "reviewer"},
            finding_digests=tuple(finding_digests),
        )
        artifact = assembly.artifact_store.write(reviewer.canonical_bytes())
        if artifact.digest != reviewer.digest:
            raise ReviewGroupError("review attach manifest digest changed in canonical CAS")
        return StagedRunEngine(assembly).append_review_cycle(
            promotion_run_id,
            target_result_digest=expected_result,
            review_digest=artifact.digest,
        )


_finding_main = main


def main(argv: list[str] | None = None) -> int:
    if not argv or argv[0] != "attach":
        return _finding_main(argv)
    parser = argparse.ArgumentParser(prog="waystone review")
    sub = parser.add_subparsers(dest="command", required=True)
    attach_parser = sub.add_parser("attach"); attach_parser.add_argument("promotion_run_id"); attach_parser.add_argument("review_run_id"); attach_parser.add_argument("--root")
    args = parser.parse_args(argv)
    try:
        context = _review_context(args.root)
        cycle = attach_review(context, args.promotion_run_id, args.review_run_id)
        print(f"review attach: recorded promotion cycle {cycle.cycle}")
        return 0
    except (FindingError, ReviewGroupError, WorkflowError, OSError, yaml.YAMLError) as error:
        print(f"waystone review: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
