"""Evidence-bound OutcomeDelta publication and outcome-ledger reads."""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from waystone.adapters.git import (
    build_git_environment,
    git_full_sha,
    git_read_bytes,
)
from waystone.core import WorkflowError
from waystone.features.review_layout import is_uuid7
from waystone.jobs.completion import parse_objective_ref
from waystone.jobs.domain import Role
from waystone.runs.artifacts import (
    ArtifactReference,
    ArtifactReferenceKind,
    ArtifactStore,
    StoredArtifact,
    validate_sha256_digest,
)
from waystone.runs.assurance import (
    AssurancePlan,
    ReviewCycle,
    ReviewerEvidence,
    parse_assurance_plan_bytes,
    parse_evaluation_evidence_bytes,
    parse_review_cycle_bytes,
    parse_reviewer_evidence_bytes,
)
from waystone.runs.effects import (
    EffectKind,
    EffectResultState,
    EffectStateRefusal,
    GitRefEffect,
    ObservationDisposition,
)
from waystone.runs.spec import RunSpec, load_run_spec
from waystone.runs.store import EntityKind, RecordNotFoundError, TransitionReason
from waystone.runs.verify import (
    DecisionOutcome,
    IntegrationDecision,
    VerifierEvidence,
    reload_integration_decision,
    reload_verifier_evidence,
)
from waystone.runs.worker_result import CompletedWorkerResult, parse_worker_result_bytes


OUTCOME_SCHEMA = "waystone-outcome-delta-1"
CLOSEOUT_SCHEMA = "waystone-run-closeout-1"
CLOSEOUT_AUDIT_SCHEMA = "waystone-closeout-audit-1"
OUTCOME_LEDGER_REF = "refs/waystone/outcomes"
OUTCOME_KINDS = frozenset({
    "executable-capability",
    "measured-improvement",
    "validated-decision",
    "simplification",
    "no-objective-delta",
})
LIFECYCLE_STAGES = frozenset({"explore", "evaluate", "promote"})


class OutcomeError(WorkflowError):
    code = "outcome-error"

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(f"{self.code}: {detail}")


class OutcomeSchemaRefusal(OutcomeError):
    code = "outcome-schema-refusal"


class OutcomeBindingRefusal(OutcomeError):
    code = "outcome-binding-refusal"


class OutcomeLedgerRefusal(OutcomeError):
    code = "outcome-ledger-refusal"


class CloseoutIncomplete(OutcomeError):
    code = "closeout-incomplete"

    def __init__(self, detail: str, audit_digest: str | None = None):
        self.audit_digest = audit_digest
        super().__init__(detail)


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False,
) -> dict:
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as error:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                "found an unhashable key", key_node.start_mark,
            ) from error
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                f"found duplicate key {key!r}", key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def digest_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise OutcomeSchemaRefusal(f"{field} must be a mapping")
    return dict(value)


def _exact(row: Mapping[str, Any], fields: set[str], field: str) -> None:
    if set(row) != fields:
        missing = sorted(fields - set(row))
        unknown = sorted(set(row) - fields)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if unknown:
            detail.append("unknown " + ", ".join(unknown))
        raise OutcomeSchemaRefusal(f"{field} fields are not canonical: {'; '.join(detail)}")


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OutcomeSchemaRefusal(f"{field} must be a non-empty string")
    return value


def _digest(value: object, field: str) -> str:
    try:
        return validate_sha256_digest(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise OutcomeSchemaRefusal(f"{field}: {error}") from error


def _yaml_document(content: bytes, field: str) -> dict[str, Any]:
    if not isinstance(content, bytes):
        raise TypeError(f"{field} content must be bytes")
    try:
        value = yaml.load(content.decode("utf-8"), Loader=_UniqueKeyLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise OutcomeSchemaRefusal(f"{field} must be valid UTF-8 YAML: {error}") from error
    return _mapping(value, field)


@dataclass(frozen=True)
class OutcomeEvidenceRef:
    kind: str
    reference_id: str
    digest: str

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "reference_id": self.reference_id,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class OutcomeDelta:
    run_id: str
    run_spec_digest: str
    lifecycle_stage: str
    objective_ref: Mapping[str, Any]
    kind: str
    summary: str
    result_digest: str
    evidence_refs: tuple[OutcomeEvidenceRef, ...]
    finding_refs: tuple[str, ...]
    recorded_by: Mapping[str, Any]
    rationale: str
    raw_bytes: bytes

    @property
    def digest(self) -> str:
        return digest_bytes(self.raw_bytes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": OUTCOME_SCHEMA,
            "run_id": self.run_id,
            "run_spec_digest": self.run_spec_digest,
            "lifecycle_stage": self.lifecycle_stage,
            "objective_ref": dict(self.objective_ref),
            "kind": self.kind,
            "summary": self.summary,
            "result_digest": self.result_digest,
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
            "finding_refs": list(self.finding_refs),
            "recorded_by": dict(self.recorded_by),
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class RunCloseout:
    run_id: str
    final_run_spec_digest: str
    lifecycle_stage: str
    result_digest: str
    completion_contract_digest: str
    assurance_plan_digest: str
    completion_evidence_refs: tuple[Mapping[str, str], ...]
    outcome_digest: str
    publication_action_id: str
    raw_bytes: bytes

    @property
    def digest(self) -> str:
        return digest_bytes(self.raw_bytes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CLOSEOUT_SCHEMA,
            "run_id": self.run_id,
            "final_run_spec_digest": self.final_run_spec_digest,
            "lifecycle_stage": self.lifecycle_stage,
            "result_digest": self.result_digest,
            "completion_contract_digest": self.completion_contract_digest,
            "assurance_plan_digest": self.assurance_plan_digest,
            "completion_evidence_refs": [dict(item) for item in self.completion_evidence_refs],
            "outcome_digest": self.outcome_digest,
            "publication_action_id": self.publication_action_id,
        }


@dataclass(frozen=True)
class LedgerEntry:
    commit_oid: str
    outcome: OutcomeDelta
    closeout: RunCloseout


@dataclass(frozen=True)
class OutcomePublication:
    run_id: str
    action_id: str
    commit_oid: str
    outcome_digest: str
    closeout_digest: str


@dataclass(frozen=True)
class FinalResultAuthority:
    attempt: object
    result_digest: str
    completion_references: tuple[Mapping[str, str], ...]


def parse_outcome_delta_bytes(content: bytes) -> OutcomeDelta:
    row = _yaml_document(content, "OutcomeDelta")
    _exact(row, {
        "schema", "run_id", "run_spec_digest", "lifecycle_stage", "objective_ref",
        "kind", "summary", "result_digest", "evidence_refs", "finding_refs",
        "recorded_by", "rationale",
    }, "OutcomeDelta")
    if row["schema"] != OUTCOME_SCHEMA:
        raise OutcomeSchemaRefusal(f"schema must be {OUTCOME_SCHEMA}")
    run_id = _nonempty(row["run_id"], "run_id")
    if not is_uuid7(run_id):
        raise OutcomeSchemaRefusal("run_id must be a canonical UUIDv7")
    stage = _nonempty(row["lifecycle_stage"], "lifecycle_stage")
    if stage not in LIFECYCLE_STAGES:
        raise OutcomeSchemaRefusal("lifecycle_stage must be explore, evaluate, or promote")
    try:
        objective = parse_objective_ref(_mapping(row["objective_ref"], "objective_ref")).to_dict()
    except WorkflowError as error:
        raise OutcomeSchemaRefusal(str(error)) from error
    kind = _nonempty(row["kind"], "kind")
    if kind not in OUTCOME_KINDS:
        raise OutcomeSchemaRefusal(f"kind must be one of {sorted(OUTCOME_KINDS)}")
    raw_evidence = row["evidence_refs"]
    if not isinstance(raw_evidence, list):
        raise OutcomeSchemaRefusal("evidence_refs must be a list")
    evidence = []
    seen_references = set()
    for index, value in enumerate(raw_evidence):
        item = _mapping(value, f"evidence_refs[{index}]")
        _exact(item, {"kind", "reference_id", "digest"}, f"evidence_refs[{index}]")
        reference_id = _nonempty(item["reference_id"], f"evidence_refs[{index}].reference_id")
        if reference_id in seen_references:
            raise OutcomeSchemaRefusal("evidence_refs reference_id values must be unique")
        seen_references.add(reference_id)
        evidence.append(OutcomeEvidenceRef(
            _nonempty(item["kind"], f"evidence_refs[{index}].kind"),
            reference_id,
            _digest(item["digest"], f"evidence_refs[{index}].digest"),
        ))
    raw_findings = row["finding_refs"]
    if not isinstance(raw_findings, list):
        raise OutcomeSchemaRefusal("finding_refs must be a list")
    findings = tuple(_nonempty(value, f"finding_refs[{index}]")
                     for index, value in enumerate(raw_findings))
    if len(set(findings)) != len(findings):
        raise OutcomeSchemaRefusal("finding_refs values must be unique")
    recorded = _mapping(row["recorded_by"], "recorded_by")
    _exact(recorded, {"role", "binding_digest", "principal"}, "recorded_by")
    if recorded["role"] != "coordinator":
        raise OutcomeSchemaRefusal("recorded_by.role must be coordinator")
    recorded["binding_digest"] = _digest(
        recorded["binding_digest"], "recorded_by.binding_digest")
    if recorded["principal"] is not None:
        raise OutcomeSchemaRefusal(
            "recorded_by.principal must be null in the single-coordinator trust domain")
    if kind != "no-objective-delta" and not evidence:
        raise OutcomeSchemaRefusal("a positive outcome delta requires evidence_refs")
    evidence_kinds = {item.kind for item in evidence}
    if stage in {"evaluate", "promote"} and kind != "no-objective-delta" \
            and "verifier-evidence" not in evidence_kinds:
        raise OutcomeSchemaRefusal(
            "evaluate/promote outcome claims require independent verifier-evidence")
    if kind != "no-objective-delta" and evidence_kinds == {"worker-proposal"}:
        raise OutcomeSchemaRefusal(
            "worker proposal evidence alone cannot establish objective progress")
    if kind == "measured-improvement" and "measurement-artifact" not in evidence_kinds:
        raise OutcomeSchemaRefusal(
            "measured-improvement requires an actual measurement-artifact")
    return OutcomeDelta(
        run_id=run_id,
        run_spec_digest=_digest(row["run_spec_digest"], "run_spec_digest"),
        lifecycle_stage=stage,
        objective_ref=objective,
        kind=kind,
        summary=_nonempty(row["summary"], "summary"),
        result_digest=_digest(row["result_digest"], "result_digest"),
        evidence_refs=tuple(evidence),
        finding_refs=findings,
        recorded_by=recorded,
        rationale=_nonempty(row["rationale"], "rationale"),
        raw_bytes=content,
    )


def parse_run_closeout_bytes(content: bytes) -> RunCloseout:
    row = _yaml_document(content, "run closeout")
    _exact(row, {
        "schema", "run_id", "final_run_spec_digest", "lifecycle_stage", "result_digest",
        "completion_contract_digest", "assurance_plan_digest", "completion_evidence_refs",
        "outcome_digest", "publication_action_id",
    }, "run closeout")
    if row["schema"] != CLOSEOUT_SCHEMA:
        raise OutcomeSchemaRefusal(f"closeout schema must be {CLOSEOUT_SCHEMA}")
    run_id = _nonempty(row["run_id"], "closeout.run_id")
    if not is_uuid7(run_id):
        raise OutcomeSchemaRefusal("closeout.run_id must be a canonical UUIDv7")
    stage = _nonempty(row["lifecycle_stage"], "closeout.lifecycle_stage")
    if stage not in LIFECYCLE_STAGES:
        raise OutcomeSchemaRefusal("closeout.lifecycle_stage is invalid")
    raw_evidence = row["completion_evidence_refs"]
    if not isinstance(raw_evidence, list):
        raise OutcomeSchemaRefusal("completion_evidence_refs must be a list")
    evidence = []
    seen = set()
    for index, value in enumerate(raw_evidence):
        item = _mapping(value, f"completion_evidence_refs[{index}]")
        _exact(item, {"reference_id", "digest"}, f"completion_evidence_refs[{index}]")
        reference_id = _nonempty(
            item["reference_id"], f"completion_evidence_refs[{index}].reference_id")
        if reference_id in seen:
            raise OutcomeSchemaRefusal("completion evidence reference ids must be unique")
        seen.add(reference_id)
        evidence.append({
            "reference_id": reference_id,
            "digest": _digest(item["digest"], f"completion_evidence_refs[{index}].digest"),
        })
    return RunCloseout(
        run_id=run_id,
        final_run_spec_digest=_digest(
            row["final_run_spec_digest"], "final_run_spec_digest"),
        lifecycle_stage=stage,
        result_digest=_digest(row["result_digest"], "closeout.result_digest"),
        completion_contract_digest=_digest(
            row["completion_contract_digest"], "completion_contract_digest"),
        assurance_plan_digest=_digest(row["assurance_plan_digest"], "assurance_plan_digest"),
        completion_evidence_refs=tuple(evidence),
        outcome_digest=_digest(row["outcome_digest"], "outcome_digest"),
        publication_action_id=_nonempty(
            row["publication_action_id"], "publication_action_id"),
        raw_bytes=content,
    )


def _canonical_yaml(payload: Mapping[str, Any]) -> bytes:
    return yaml.safe_dump(
        dict(payload), allow_unicode=True, sort_keys=False,
        default_flow_style=False,
    ).encode("utf-8")


def _git(root: Path, *args: str, input_bytes: bytes | None = None,
         environment: Mapping[str, str] | None = None) -> bytes:
    operation_environment = dict(environment or {})
    if args and args[0] in {"diff-tree", "rev-list"}:
        operation_environment["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        ["git", "-C", str(root), *args], input=input_bytes, capture_output=True,
        env=build_git_environment(overrides=operation_environment), timeout=15,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
        raise OutcomeLedgerRefusal(f"git {' '.join(args)} failed: {detail or result.returncode}")
    return result.stdout


def _hash_blob(root: Path, content: bytes) -> str:
    return _git(root, "hash-object", "-w", "--stdin", input_bytes=content).decode().strip()


def _prepare_ledger_commit(
    root: Path, parent_oid: str | None, run_id: str, closeout: bytes, outcome: bytes,
) -> str:
    if parent_oid is not None:
        read_outcome_ledger(root, tip_oid=parent_oid)
    paths = {
        f"docs/runs/{run_id}/closeout.yaml": _hash_blob(root, closeout),
        f"docs/runs/{run_id}/outcome.yaml": _hash_blob(root, outcome),
    }
    for path in paths:
        if parent_oid is not None:
            existing = git_read_bytes(root, "ls-tree", parent_oid, "--", path)
            if existing:
                raise OutcomeLedgerRefusal(f"outcome ledger path already exists: {path}")
    with tempfile.TemporaryDirectory(prefix="waystone-outcome-index-") as directory:
        index = str(Path(directory) / "index")
        environment = {"GIT_INDEX_FILE": index}
        if parent_oid is None:
            _git(root, "read-tree", "--empty", environment=environment)
        else:
            _git(root, "read-tree", f"{parent_oid}^{{tree}}", environment=environment)
        for path, oid in paths.items():
            _git(root, "update-index", "--add", "--cacheinfo", "100644", oid, path,
                 environment=environment)
        tree_oid = _git(root, "write-tree", environment=environment).decode().strip()
    command = ["commit-tree", tree_oid]
    if parent_oid is not None:
        command.extend(["-p", parent_oid])
    commit_environment = {
        "GIT_AUTHOR_NAME": "Waystone Engine",
        "GIT_AUTHOR_EMAIL": "engine@waystone.invalid",
        "GIT_COMMITTER_NAME": "Waystone Engine",
        "GIT_COMMITTER_EMAIL": "engine@waystone.invalid",
    }
    return _git(
        root, *command, input_bytes=f"outcome: {run_id}\n".encode(),
        environment=commit_environment,
    ).decode().strip()


def _commit_pair_paths(root: Path, commit_oid: str, parent_oid: str | None) -> tuple[str, str]:
    if parent_oid is None:
        output = _git(
            root, "diff-tree", "--root", "--no-commit-id", "--name-status", "-r", commit_oid)
    else:
        output = _git(
            root, "diff-tree", "--no-commit-id", "--name-status", "-r",
            parent_oid, commit_oid)
    lines = output.decode("utf-8", errors="strict").splitlines()
    if len(lines) != 2 or any(not line.startswith("A\t") for line in lines):
        raise OutcomeLedgerRefusal(
            f"ledger commit {commit_oid} must add exactly one closeout/outcome pair")
    paths = sorted(line.split("\t", 1)[1] for line in lines)
    closeouts = [path for path in paths if path.endswith("/closeout.yaml")]
    outcomes = [path for path in paths if path.endswith("/outcome.yaml")]
    if len(closeouts) != 1 or len(outcomes) != 1:
        raise OutcomeLedgerRefusal(f"ledger commit {commit_oid} does not add a canonical pair")
    closeout_path, outcome_path = closeouts[0], outcomes[0]
    closeout_owner = closeout_path.removeprefix("docs/runs/").removesuffix("/closeout.yaml")
    outcome_owner = outcome_path.removeprefix("docs/runs/").removesuffix("/outcome.yaml")
    if closeout_owner != outcome_owner or not is_uuid7(closeout_owner):
        raise OutcomeLedgerRefusal(f"ledger commit {commit_oid} pair owner is invalid")
    return closeout_path, outcome_path


def read_outcome_ledger(root: Path, *, tip_oid: str | None = None) -> tuple[LedgerEntry, ...]:
    root = Path(root).resolve()
    tip = tip_oid or git_full_sha(root, OUTCOME_LEDGER_REF)
    if tip is None:
        return ()
    history = _git(root, "rev-list", "--first-parent", "--reverse", tip).decode().splitlines()
    entries = []
    previous = None
    for commit_oid in history:
        parents = _git(root, "rev-list", "--parents", "-n", "1", commit_oid).decode().split()
        expected = [commit_oid] if previous is None else [commit_oid, previous]
        if parents != expected:
            raise OutcomeLedgerRefusal(
                f"ledger commit {commit_oid} is not a linear first-parent append")
        closeout_path, outcome_path = _commit_pair_paths(root, commit_oid, previous)
        outcome_bytes = git_read_bytes(root, "show", f"{commit_oid}:{outcome_path}")
        closeout_bytes = git_read_bytes(root, "show", f"{commit_oid}:{closeout_path}")
        outcome = parse_outcome_delta_bytes(outcome_bytes)
        closeout = parse_run_closeout_bytes(closeout_bytes)
        if (closeout.run_id != outcome.run_id
                or closeout.final_run_spec_digest != outcome.run_spec_digest
                or closeout.lifecycle_stage != outcome.lifecycle_stage
                or closeout.result_digest != outcome.result_digest
                or closeout.outcome_digest != outcome.digest
                or closeout.publication_action_id
                != f"{outcome.run_id}:outcome-publication"):
            raise OutcomeLedgerRefusal(
                f"ledger commit {commit_oid} closeout/outcome binding is invalid")
        entries.append(LedgerEntry(commit_oid, outcome, closeout))
        previous = commit_oid
    return tuple(entries)


def _final_attempt(assembly, spec: RunSpec):
    with assembly.store._connection_lock:  # noqa: SLF001 - final-attempt projection
        rows = assembly.store._connection.execute(  # noqa: SLF001
            "SELECT attempt_id, state FROM attempts WHERE run_id = ? AND job_id = ? "
            "ORDER BY rowid",
            (spec.run_id, spec.job_id),
        ).fetchall()
    if not rows:
        raise OutcomeBindingRefusal("run has no final attempt")
    row = rows[-1]
    if row["state"] != "completed":
        raise OutcomeBindingRefusal("final attempt is not completed")
    return assembly.store.get_entity(EntityKind.ATTEMPT, row["attempt_id"])


def _promotion_review_authority(
        assembly, spec: RunSpec, plan: AssurancePlan, result_digest: str,
        ) -> tuple[ReviewerEvidence | None, Mapping[str, str] | None]:
    if not plan.requires("adversarial-review"):
        return None, None
    if spec.promotion_lineage is None:
        raise OutcomeBindingRefusal("promotion review lacks a frozen lineage")
    lineage_id = spec.promotion_lineage.id
    indexed: dict[int, tuple[ReviewCycle, Mapping[str, str]]] = {}
    head = spec.promotion_lineage.review_cycle_head_digest
    inherited = []
    while head is not None:
        try:
            cycle = parse_review_cycle_bytes(assembly.artifact_store.read(head))
        except WorkflowError as error:
            raise OutcomeBindingRefusal(
                "inherited promotion review cycle cannot be reloaded") from error
        inherited.append(cycle)
        head = cycle.supersedes_digest
    for cycle in reversed(inherited):
        indexed[cycle.cycle] = (
            cycle,
            {
                "reference_id": f"review-cycle:{lineage_id}:{cycle.cycle}",
                "digest": cycle.digest,
            },
        )
    prefix = f"review-cycle:{lineage_id}:"
    with assembly.store._connection_lock:  # noqa: SLF001 - durable review projection
        rows = assembly.store._connection.execute(  # noqa: SLF001
            "SELECT reference_id FROM artifacts WHERE reference_id LIKE ?",
            (prefix + "%",),
        ).fetchall()
    for row in rows:
        reference_id = row["reference_id"]
        suffix = reference_id.removeprefix(prefix)
        if not suffix.isdigit() or int(suffix) < 1:
            raise OutcomeBindingRefusal(
                "promotion review cycle reference identity is invalid")
        reference = assembly.store.get_artifact_reference(reference_id)
        try:
            cycle = parse_review_cycle_bytes(
                assembly.artifact_store.read_reference(reference))
        except WorkflowError as error:
            raise OutcomeBindingRefusal(
                "promotion review cycle cannot be reloaded") from error
        prior = indexed.get(cycle.cycle)
        if prior is not None and prior[0].digest != cycle.digest:
            raise OutcomeBindingRefusal("promotion review cycle number is divergent")
        indexed[cycle.cycle] = (
            cycle,
            {"reference_id": reference_id, "digest": reference.digest},
        )
    ordered = tuple(indexed[index] for index in sorted(indexed))
    prior_digest = None
    for index, (cycle, _reference) in enumerate(ordered, start=1):
        if (cycle.promotion_lineage_id != lineage_id
                or cycle.cycle != index
                or cycle.supersedes_digest != prior_digest):
            raise OutcomeBindingRefusal(
                "promotion review cycle chain is divergent or non-contiguous")
        prior_digest = cycle.digest
    if not ordered:
        raise OutcomeBindingRefusal(
            "risk-gated promotion lacks reviewer completion evidence")
    cycle, reference = ordered[-1]
    try:
        reviewer = parse_reviewer_evidence_bytes(
            assembly.artifact_store.read(cycle.review_digest))
    except WorkflowError as error:
        raise OutcomeBindingRefusal(
            "promotion reviewer evidence cannot be reloaded") from error
    candidate = spec.candidate
    expected_reviewer = assembly.profile.binding_for(Role.REVIEWER).binding_digest
    if (not isinstance(candidate, Mapping)
            or cycle.target_result_digest != result_digest
            or cycle.review_digest != reviewer.digest
            or reviewer.promotion_lineage_id != lineage_id
            or reviewer.target_run_spec_digest != spec.run_spec_digest
            or reviewer.candidate_digest != candidate.get("digest")
            or reviewer.target_result_digest != result_digest
            or reviewer.actor["actor_id"] != expected_reviewer):
        raise OutcomeBindingRefusal(
            "promotion reviewer evidence differs from the frozen result lineage")
    return reviewer, reference


def _promotion_apply_references(
        assembly, spec: RunSpec, attempt_id: str,
        ) -> tuple[Mapping[str, str], Mapping[str, str]]:
    candidate = spec.candidate
    if not isinstance(candidate, Mapping):
        raise OutcomeBindingRefusal("promotion apply lacks a frozen candidate")
    action_id = f"{spec.run_id}:target-ref-apply"
    try:
        action = assembly.store.get_entity(EntityKind.ACTION, action_id)
        plan = assembly.effect_executor._load_plan(action_id)  # noqa: SLF001
    except WorkflowError as error:
        raise OutcomeBindingRefusal(
            "promotion target-ref apply plan cannot be reloaded") from error
    root = assembly.context.canonical_root
    if (action.state != "completed"
            or plan.run_id != spec.run_id
            or plan.job_id != spec.job_id
            or plan.attempt_id != attempt_id
            or plan.kind is not EffectKind.GIT_REF
            or Path(str(plan.spec.get("repository"))).resolve() != root
            or plan.spec.get("ref") != spec.result_policy.target_ref
            or plan.spec.get("expected_old_oid") != spec.result_policy.expected_oid
            or plan.spec.get("desired_oid") != candidate.get("target_oid")):
        raise OutcomeBindingRefusal(
            "promotion target-ref apply plan differs from the frozen authority tuple")
    observation = assembly.effect_executor._observe(plan)  # noqa: SLF001
    if (observation.disposition is not ObservationDisposition.DESIRED
            or observation.observed_digest is None):
        raise OutcomeBindingRefusal(
            "promotion target-ref apply is no longer positively observed")
    receipt_error = assembly.effect_executor._observation_receipt_error(  # noqa: SLF001
        plan, observation)
    if receipt_error is not None:
        raise OutcomeBindingRefusal(
            f"promotion target-ref apply observation is invalid: {receipt_error}")
    suffix = observation.observed_digest.split(":", 1)[1]
    observation_id = f"effect-observation:{action_id}:{suffix}"
    try:
        observation_reference = assembly.store.get_artifact_reference(observation_id)
    except RecordNotFoundError as error:
        raise OutcomeBindingRefusal(
            "promotion target-ref apply observation reference is absent") from error
    return (
        {
            "reference_id": f"effect-plan:{action_id}",
            "digest": plan.plan_digest,
        },
        {
            "reference_id": observation_id,
            "digest": observation_reference.digest,
        },
    )


def _promotion_result_authority(
        assembly, spec: RunSpec, attempt,
        ) -> FinalResultAuthority:
    candidate = spec.candidate
    evaluation = spec.evaluation.get("evidence")
    if (not isinstance(candidate, Mapping)
            or not isinstance(evaluation, Mapping)
            or spec.promotion_lineage is None):
        raise OutcomeBindingRefusal(
            "promotion result authority lacks frozen candidate/evaluation lineage")
    try:
        producer_result_digest = validate_sha256_digest(
            candidate.get("producer_result_digest"))  # type: ignore[arg-type]
    except ValueError as error:
        raise OutcomeBindingRefusal(
            "promotion candidate producer_result_digest is invalid") from error
    attempt_id = attempt.entity_id
    if attempt_id == f"{spec.run_id}:attempt:1":
        verifier_action_id = f"{spec.run_id}:typed-independent-verify"
        decision_action_id = f"{spec.run_id}:integration-decision"
    else:
        verifier_action_id = f"{attempt_id}:typed-independent-verify"
        decision_action_id = f"{attempt_id}:integration-decision"
    try:
        verifier = reload_verifier_evidence(
            spec.run_id,
            attempt_id,
            verifier_action_id,
            start=assembly.context.canonical_root,
        )
        decision = reload_integration_decision(
            spec.run_id,
            attempt_id,
            decision_action_id,
            verifier_action_id,
            start=assembly.context.canonical_root,
        )
        plan = parse_assurance_plan_bytes(
            assembly.artifact_store.read(spec.assurance_plan.digest))
    except WorkflowError as error:
        raise OutcomeBindingRefusal(
            f"promotion result authority cannot be reloaded: {error}") from error
    reviewer, review_reference = _promotion_review_authority(
        assembly, spec, plan, producer_result_digest)
    expected_reviewers = () if reviewer is None else (reviewer.digest,)
    run_state = assembly.store.get_run(spec.run_id).state
    expected_outcome = (
        DecisionOutcome.REJECT if run_state == "failed" else DecisionOutcome.ACCEPT)
    evaluation_digest = evaluation.get("digest")
    if (not isinstance(verifier, VerifierEvidence)
            or verifier.run_id != spec.run_id
            or verifier.job_id != spec.job_id
            or verifier.attempt_id != attempt_id
            or verifier.run_spec_digest != spec.run_spec_digest
            or verifier.actor.role is not Role.VERIFIER
            or verifier.verifier_sandbox.filesystem != "read-only"
            or verifier.result.result_oid != candidate.get("target_oid")
            or decision.run_id != spec.run_id
            or decision.job_id != spec.job_id
            or decision.attempt_id != attempt_id
            or decision.actor.role is not Role.COORDINATOR
            or decision.outcome is not expected_outcome
            or decision.result_digest != verifier.result.result_digest
            or decision.verifier_reference_id
            != verifier.artifact_reference.reference_id
            or decision.verifier_artifact_digest
            != verifier.artifact_reference.digest
            or decision.candidate_digest != candidate.get("digest")
            or decision.evaluation_evidence_digest != evaluation_digest
            or decision.reviewer_artifact_digests != expected_reviewers):
        raise OutcomeBindingRefusal(
            "promotion result differs from the approved GitResultTriple authority")
    actor_ids = [verifier.actor.actor_id, decision.actor.actor_id]
    if reviewer is not None:
        actor_ids.append(reviewer.actor["actor_id"])
    if len(actor_ids) != len(set(actor_ids)):
        raise OutcomeBindingRefusal(
            "promotion completion actor identities are not independent")
    references = [{
        "reference_id": verifier.artifact_reference.reference_id,
        "digest": verifier.artifact_reference.digest,
    }]
    if review_reference is not None:
        references.append(review_reference)
    references.append({
        "reference_id": decision.artifact_reference.reference_id,
        "digest": decision.artifact_reference.digest,
    })
    if decision.outcome is DecisionOutcome.ACCEPT:
        references.extend(_promotion_apply_references(
            assembly, spec, attempt_id))
    return FinalResultAuthority(
        attempt,
        decision.result_digest,
        tuple(references),
    )


def _final_result_authority(assembly, spec: RunSpec) -> FinalResultAuthority:
    """Reload the stage-owned final result and its durable completion evidence."""
    attempt = _final_attempt(assembly, spec)
    result_id = f"worker-result:{attempt.entity_id}"
    try:
        result_reference = assembly.store.get_artifact_reference(result_id)
    except RecordNotFoundError:
        result_reference = None
    if spec.lifecycle_stage.value == "promote":
        if result_reference is not None:
            raise OutcomeBindingRefusal(
                "promotion final attempt must not have a worker result")
        return _promotion_result_authority(assembly, spec, attempt)
    if result_reference is None:
        raise OutcomeBindingRefusal("final attempt has no frozen worker result")
    result_content = assembly.artifact_store.read_reference(result_reference)
    try:
        worker_result = parse_worker_result_bytes(
            result_content,
            expected_run_spec_digest=spec.run_spec_digest,
            expected_attempt_id=attempt.entity_id,
        )
    except WorkflowError as error:
        raise OutcomeBindingRefusal(f"final worker result is invalid: {error}") from error
    if not isinstance(worker_result, CompletedWorkerResult):
        raise OutcomeBindingRefusal("final result is not a completed worker result")
    completion_references = [{
        "reference_id": result_reference.reference_id,
        "digest": result_reference.digest,
    }]
    for worker_evidence in worker_result.evidence_refs:
        assembly.artifact_store.read(worker_evidence.digest)
        completion_references.append({
            "reference_id": worker_evidence.reference_id,
            "digest": worker_evidence.digest,
        })
    return FinalResultAuthority(
        attempt,
        result_reference.digest,
        tuple(completion_references),
    )


def _rejected_promotion_decision(
        assembly, spec: RunSpec, attempt) -> IntegrationDecision:
    with assembly.store._connection_lock:  # noqa: SLF001 - terminal evidence pair
        rows = assembly.store._connection.execute(  # noqa: SLF001
            "SELECT reference_id FROM artifacts WHERE run_id = ? "
            "AND entity_kind = ? AND entity_id = ? AND (reference_id LIKE ? "
            "OR reference_id LIKE ?) ORDER BY transition_id",
            (
                spec.run_id,
                EntityKind.ATTEMPT.value,
                attempt.entity_id,
                "verifier-evidence:%",
                "integration-decision:%",
            ),
        ).fetchall()
    verifier_ids = [
        row["reference_id"] for row in rows
        if row["reference_id"].startswith("verifier-evidence:")
    ]
    decision_ids = [
        row["reference_id"] for row in rows
        if row["reference_id"].startswith("integration-decision:")
    ]
    if len(verifier_ids) != 1 or len(decision_ids) != 1:
        raise OutcomeBindingRefusal(
            "failed promotion lacks one exact verifier/decision terminal pair")
    try:
        decision = reload_integration_decision(
            spec.run_id,
            attempt.entity_id,
            decision_ids[0].removeprefix("integration-decision:"),
            verifier_ids[0].removeprefix("verifier-evidence:"),
            start=assembly.context.canonical_root,
        )
    except WorkflowError as error:
        raise OutcomeBindingRefusal(
            f"promotion rejection decision cannot be revalidated: {error}") from error
    if decision.outcome is not DecisionOutcome.REJECT:
        raise OutcomeBindingRefusal(
            "failed promotion closeout requires an exact reject decision")
    return decision


def _reference_owner(assembly, reference_id: str) -> str | None:
    with assembly.store._connection_lock:  # noqa: SLF001 - immutable attribution query
        row = assembly.store._connection.execute(  # noqa: SLF001
            "SELECT run_id FROM artifacts WHERE reference_id = ?", (reference_id,)).fetchone()
    return None if row is None else row["run_id"]


def _validate_verifier_evidence(
    assembly, spec: RunSpec, item: OutcomeEvidenceRef, content: bytes,
    outcome: OutcomeDelta,
) -> tuple[Mapping[str, str], ...]:
    try:
        row = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OutcomeBindingRefusal(f"verifier evidence is not valid JSON: {error}") from error
    if not isinstance(row, dict):
        raise OutcomeBindingRefusal("verifier evidence is not an object")
    if row.get("schema") == "waystone-evaluation-evidence-1":
        evidence = parse_evaluation_evidence_bytes(content)
        frozen = spec.evaluation.get("spec")
        try:
            action = assembly.store.get_entity(
                EntityKind.ACTION, evidence.evaluator_action_id)
        except RecordNotFoundError as error:
            raise OutcomeBindingRefusal(
                "evaluation evidence evaluator action is not frozen") from error
        if (item.reference_id != f"evaluation-evidence:{outcome.run_id}"
                or action.run_id != outcome.run_id
                or spec.candidate is None
                or evidence.candidate_digest != spec.candidate["digest"]
                or not isinstance(frozen, Mapping)
                or evidence.evaluation_spec_digest != frozen.get("digest")
                or evidence.evaluation_generation != frozen.get("generation")):
            raise OutcomeBindingRefusal(
                "evaluation evidence does not bind the frozen evaluator/candidate/spec lineage")
        return evidence.metric_artifacts
    actor = row.get("actor")
    result = row.get("result")
    result_digest = (
        result.get("result_digest") if isinstance(result, dict) else row.get("result_digest"))
    if (not isinstance(actor, dict) or actor.get("role") != "verifier"
            or row.get("run_id") != outcome.run_id
            or row.get("run_spec_digest") != outcome.run_spec_digest
            or result_digest != outcome.result_digest):
        raise OutcomeBindingRefusal(
            "verifier evidence does not bind the outcome run/spec/result lineage")
    return ()


def _validate_outcome_lineage(assembly, spec: RunSpec, outcome: OutcomeDelta):
    if (outcome.run_id != spec.run_id
            or outcome.run_spec_digest != spec.run_spec_digest
            or outcome.lifecycle_stage != spec.lifecycle_stage.value
            or dict(outcome.objective_ref) != spec.objective_ref.to_dict()):
        raise OutcomeBindingRefusal(
            "OutcomeDelta objective/run/spec/stage differs from frozen RunSpec lineage")
    coordinator = assembly.profile.binding_for(Role.COORDINATOR)
    if outcome.recorded_by["binding_digest"] != coordinator.binding_digest:
        raise OutcomeBindingRefusal("recorded_by coordinator binding is stale")
    authority = _final_result_authority(assembly, spec)
    if authority.result_digest != outcome.result_digest:
        if spec.lifecycle_stage.value == "promote":
            raise OutcomeBindingRefusal(
                "OutcomeDelta result_digest differs from the approved GitResultTriple")
        raise OutcomeBindingRefusal("OutcomeDelta result_digest differs from the final result")
    evidence_references = []
    measurement_claims = {
        (item.reference_id, item.digest)
        for item in outcome.evidence_refs if item.kind == "measurement-artifact"
    }
    frozen_measurements: set[tuple[str, str]] = set()
    for item in outcome.evidence_refs:
        if item.kind == "measurement-artifact":
            assembly.artifact_store.read(item.digest)
            continue
        try:
            reference = assembly.store.get_artifact_reference(item.reference_id)
        except RecordNotFoundError as error:
            raise OutcomeBindingRefusal(
                f"outcome evidence reference is not frozen: {item.reference_id}") from error
        if (_reference_owner(assembly, item.reference_id) != spec.run_id
                or reference.kind is not ArtifactReferenceKind.EVIDENCE
                or reference.digest != item.digest):
            raise OutcomeBindingRefusal(
                f"outcome evidence differs from frozen run evidence: {item.reference_id}")
        content = assembly.artifact_store.read_reference(reference)
        if item.kind == "verifier-evidence":
            if not item.reference_id.startswith("verifier-evidence:"):
                raise OutcomeBindingRefusal(
                    "verifier-evidence kind requires a verifier-evidence reference")
            frozen_measurements.update(
                (metric["reference_id"], metric["digest"])
                for metric in _validate_verifier_evidence(
                    assembly, spec, item, content, outcome))
        evidence_references.append(reference)
    if not measurement_claims.issubset(frozen_measurements):
        raise OutcomeBindingRefusal(
            "measurement artifact is not frozen by the cited evaluation evidence")
    completion_refs = list(authority.completion_references)
    completion_refs.extend({
        "reference_id": reference.reference_id,
        "digest": reference.digest,
    } for reference in evidence_references)
    completion_refs.extend({
        "reference_id": reference_id,
        "digest": digest,
    } for reference_id, digest in sorted(measurement_claims))
    deduplicated = {}
    for reference in completion_refs:
        prior = deduplicated.get(reference["reference_id"])
        if prior is not None and prior != reference["digest"]:
            raise OutcomeBindingRefusal(
                "completion evidence reference id names conflicting digests")
        deduplicated[reference["reference_id"]] = reference["digest"]
    return authority.attempt, tuple(
        {"reference_id": reference_id, "digest": digest}
        for reference_id, digest in deduplicated.items())


def _closeout_bytes(
    spec: RunSpec, outcome: OutcomeDelta, action_id: str,
    completion_references: Sequence[Mapping[str, str]],
) -> bytes:
    payload = {
        "schema": CLOSEOUT_SCHEMA,
        "run_id": spec.run_id,
        "final_run_spec_digest": spec.run_spec_digest,
        "lifecycle_stage": spec.lifecycle_stage.value,
        "result_digest": outcome.result_digest,
        "completion_contract_digest": spec.job_input.completion_contract.digest,
        "assurance_plan_digest": spec.assurance_plan.digest,
        "completion_evidence_refs": [
            {"reference_id": item["reference_id"], "digest": item["digest"]}
            for item in completion_references
        ],
        "outcome_digest": outcome.digest,
        "publication_action_id": action_id,
    }
    content = _canonical_yaml(payload)
    parse_run_closeout_bytes(content)
    return content


def _record_incomplete(
    assembly, spec: RunSpec, action_id: str, detail: str,
    outcome_artifact: StoredArtifact, closeout_artifact: StoredArtifact | None = None,
    desired_oid: str | None = None,
) -> str:
    payload = {
        "schema": CLOSEOUT_AUDIT_SCHEMA,
        "reason": "closeout-incomplete",
        "run_id": spec.run_id,
        "publication_action_id": action_id,
        "detail": detail,
        "outcome_digest": outcome_artifact.digest,
        "closeout_digest": (
            None if closeout_artifact is None else closeout_artifact.digest),
        "desired_oid": desired_oid,
        "observed_ref_oid": git_full_sha(assembly.context.canonical_root, OUTCOME_LEDGER_REF),
    }
    artifact = assembly.artifact_store.write(_canonical_yaml(payload))
    reference_id = f"closeout-incomplete:{spec.run_id}:{artifact.digest.split(':', 1)[1]}"
    try:
        existing = assembly.store.get_artifact_reference(reference_id)
    except RecordNotFoundError:
        run = assembly.store.get_run(spec.run_id)
        assembly.store.record_transition(
            EntityKind.RUN,
            spec.run_id,
            expected_version=run.version,
            next_state=run.state,
            reason=TransitionReason.EFFECT_OBSERVED,
            evidence_digest=artifact.digest,
            artifact_references=(ArtifactReference(
                reference_id, ArtifactReferenceKind.EVIDENCE,
                artifact.digest, artifact.size,
            ),),
        )
    else:
        if existing.digest != artifact.digest:
            raise OutcomeBindingRefusal("closeout-incomplete audit identity collision")
    return artifact.digest


def _complete_run(assembly, spec: RunSpec, outcome_digest: str) -> None:
    run = assembly.store.get_run(spec.run_id)
    if run.state == "completed":
        return
    if run.state == "failed":
        return
    if run.state != "closeout-ready":
        raise OutcomeBindingRefusal(
            f"run must be closeout-ready before publication, found {run.state!r}")
    assembly.store.record_transition(
        EntityKind.RUN,
        spec.run_id,
        expected_version=run.version,
        next_state="completed",
        reason=TransitionReason.COMPLETED,
        evidence_digest=outcome_digest,
    )


def publish_outcome(assembly, run_id: str, outcome_content: bytes) -> OutcomePublication:
    """Validate exact coordinator bytes, CAS-publish one pair, then complete the run."""
    if not isinstance(outcome_content, bytes):
        raise TypeError("outcome_content must be bytes")
    outcome_artifact = assembly.artifact_store.write(outcome_content)
    outcome = parse_outcome_delta_bytes(assembly.artifact_store.read(outcome_artifact.digest))
    spec = load_run_spec(run_id, start=assembly.context.canonical_root)
    action_id = f"{run_id}:outcome-publication"
    try:
        if outcome.run_id != run_id:
            raise OutcomeBindingRefusal("CLI run id differs from OutcomeDelta run_id")
        run = assembly.store.get_run(run_id)
        if run.state not in {"closeout-ready", "completed", "failed"}:
            raise OutcomeBindingRefusal(
                f"run must be closeout-ready before close, found {run.state!r}")
        attempt, completion_references = _validate_outcome_lineage(
            assembly, spec, outcome)
        rejection = None
        if run.state == "failed":
            if (spec.lifecycle_stage.value != "promote"
                    or outcome.kind != "no-objective-delta"):
                raise OutcomeBindingRefusal(
                    "only a rejected promotion can close failed with no-objective-delta")
            rejection = _rejected_promotion_decision(
                assembly, spec, attempt)
    except OutcomeBindingRefusal as error:
        audit = _record_incomplete(
            assembly, spec, action_id, str(error), outcome_artifact)
        raise CloseoutIncomplete(str(error), audit) from error
    if rejection is not None:
        completion_references = (*completion_references, {
            "reference_id": rejection.verifier_reference_id,
            "digest": rejection.verifier_artifact_digest,
        }, {
            "reference_id": rejection.artifact_reference.reference_id,
            "digest": rejection.artifact_reference.digest,
        })
        deduplicated = {}
        for reference in completion_references:
            prior = deduplicated.get(reference["reference_id"])
            if prior is not None and prior != reference["digest"]:
                raise OutcomeBindingRefusal(
                    "promotion rejection completion evidence has conflicting digests")
            deduplicated[reference["reference_id"]] = reference["digest"]
        completion_references = tuple({
            "reference_id": reference_id,
            "digest": digest,
        } for reference_id, digest in deduplicated.items())
    closeout_content = _closeout_bytes(
        spec, outcome, action_id, completion_references)
    closeout_artifact = assembly.artifact_store.write(closeout_content)
    root = assembly.context.canonical_root
    current_oid = git_full_sha(root, OUTCOME_LEDGER_REF)
    try:
        action = assembly.store.get_entity(EntityKind.ACTION, action_id)
    except RecordNotFoundError:
        desired_oid = _prepare_ledger_commit(
            root, current_oid, run_id, closeout_content, outcome.raw_bytes)
        plan = assembly.effect_executor.plan_effect(
            run_id,
            spec.job_id,
            attempt.entity_id,
            action_id,
            GitRefEffect(root, OUTCOME_LEDGER_REF, current_oid, desired_oid),
            artifact_references=(
                ArtifactReference(
                    f"run-closeout:{run_id}", ArtifactReferenceKind.INPUT,
                    closeout_artifact.digest, closeout_artifact.size,
                ),
                ArtifactReference(
                    f"outcome:{run_id}", ArtifactReferenceKind.OUTCOME,
                    outcome_artifact.digest, outcome_artifact.size,
                ),
            ),
        )
    else:
        closeout_reference = assembly.store.get_artifact_reference(f"run-closeout:{run_id}")
        outcome_reference = assembly.store.get_artifact_reference(f"outcome:{run_id}")
        if (closeout_reference.digest != closeout_artifact.digest
                or outcome_reference.digest != outcome_artifact.digest):
            raise OutcomeBindingRefusal(
                "existing publication action is bound to different closeout/outcome bytes")
        plan = assembly.effect_executor._load_plan(action_id)  # noqa: SLF001 - reconciliation
        desired_oid = str(plan.spec["desired_oid"])
    try:
        action = assembly.store.get_entity(EntityKind.ACTION, action_id)
        if action.state == "planned":
            claimed = assembly.effect_executor.claim_effect(plan, ttl_seconds=30)
            result = assembly.effect_executor.execute_effect(claimed)
        else:
            result = assembly.effect_executor.reconcile_actions((action_id,))[0]
    except EffectStateRefusal:
        try:
            result = assembly.effect_executor.reconcile_actions((action_id,))[0]
        except WorkflowError as error:
            detail = str(error)
            audit = _record_incomplete(
                assembly, spec, action_id, detail,
                outcome_artifact, closeout_artifact, desired_oid)
            raise CloseoutIncomplete(detail, audit) from error
    except WorkflowError as error:
        detail = str(error)
        audit = _record_incomplete(
            assembly, spec, action_id, detail,
            outcome_artifact, closeout_artifact, desired_oid)
        raise CloseoutIncomplete(detail, audit) from error
    if result.state not in {EffectResultState.COMPLETED, EffectResultState.NOOP}:
        detail = result.reason or f"outcome ledger effect ended in {result.state.value}"
        audit = _record_incomplete(
            assembly, spec, action_id, detail,
            outcome_artifact, closeout_artifact, desired_oid)
        raise CloseoutIncomplete(detail, audit)
    try:
        entries = read_outcome_ledger(root)
        latest = entries[-1]
        if (latest.commit_oid != desired_oid
                or latest.outcome.digest != outcome_artifact.digest
                or latest.closeout.digest != closeout_artifact.digest):
            raise OutcomeLedgerRefusal("observed ledger tip differs from the prepared pair")
    except (IndexError, OutcomeError) as error:
        detail = str(error)
        audit = _record_incomplete(
            assembly, spec, action_id, detail,
            outcome_artifact, closeout_artifact, desired_oid)
        raise CloseoutIncomplete(detail, audit) from error
    _complete_run(assembly, spec, outcome.digest)
    return OutcomePublication(
        run_id, action_id, desired_oid, outcome_artifact.digest, closeout_artifact.digest)


__all__ = [
    "CLOSEOUT_AUDIT_SCHEMA",
    "CLOSEOUT_SCHEMA",
    "CloseoutIncomplete",
    "LedgerEntry",
    "OUTCOME_KINDS",
    "OUTCOME_LEDGER_REF",
    "OUTCOME_SCHEMA",
    "OutcomeBindingRefusal",
    "OutcomeDelta",
    "OutcomeError",
    "OutcomeLedgerRefusal",
    "OutcomePublication",
    "OutcomeSchemaRefusal",
    "RunCloseout",
    "digest_bytes",
    "parse_outcome_delta_bytes",
    "parse_run_closeout_bytes",
    "publish_outcome",
    "read_outcome_ledger",
]
