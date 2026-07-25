"""Deterministic WorkBrief and OutcomeDelta assembly from semantic YAML drafts."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

import yaml

from waystone.adapters.git import GitReadError, git_full_sha, git_read_bytes
from waystone.core import WorkflowError
from waystone.features.review_layout import new_run_id
from waystone.jobs import completion
from waystone.jobs.domain import Role
from waystone.jobs.work_brief import parse_work_brief_bytes
from waystone.project.brief import read_project_frame_at_commit
from waystone.runs.assurance import (
    parse_evaluation_evidence_bytes,
    parse_evaluation_spec_bytes,
)
from waystone.runs.outcome import (
    OUTCOME_SCHEMA,
    OutcomeBindingRefusal,
    _final_result_authority,
    parse_outcome_delta_bytes,
)
from waystone.runs.spec import load_run_spec
from waystone.runs.store import RecordNotFoundError


class RunScaffoldRefusal(WorkflowError):
    """A semantic draft cannot be bound to current protocol authority."""

    code = "run_scaffold_refusal"

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(f"{self.code}: {detail}")


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


_BRIEF_FIELDS = {
    "lifecycle_stage", "objective_fact_id", "desired_delta", "why_now",
    "current_state", "decisions", "constraints", "non_goals", "known_failures",
    "evidence_expected", "references", "open_questions",
}
_OUTCOME_FIELDS = {"kind", "summary", "evidence_refs", "finding_refs", "rationale"}


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise RunScaffoldRefusal(f"{field} must be a mapping with string keys")
    return dict(value)


def _exact(row: Mapping[str, Any], fields: set[str], field: str) -> None:
    if set(row) == fields:
        return
    missing = sorted(fields - set(row))
    unknown = sorted(set(row) - fields)
    detail = []
    if missing:
        detail.append("missing " + ", ".join(missing))
    if unknown:
        detail.append("unknown " + ", ".join(unknown))
    raise RunScaffoldRefusal(f"{field} fields are not exact: {'; '.join(detail)}")


def _document(content: bytes, field: str) -> dict[str, Any]:
    if not isinstance(content, bytes):
        raise TypeError(f"{field} content must be bytes")
    try:
        decoded = content.decode("utf-8")
        value = yaml.load(decoded, Loader=_UniqueKeyLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise RunScaffoldRefusal(f"{field} must be valid UTF-8 YAML: {error}") from error
    return _mapping(value, field)


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RunScaffoldRefusal(f"{field} must be a non-empty string")
    return value


def _strings(value: object, field: str) -> list[str]:
    if not isinstance(value, list):
        raise RunScaffoldRefusal(f"{field} must be a list")
    return [_string(item, f"{field}[{index}]") for index, item in enumerate(value)]


def _semantic_item(text: str) -> dict[str, object]:
    return {
        "text": text,
        "provenance": "coordinator-summary",
        "sources": [],
    }


def _semantic_items(value: object, field: str) -> list[dict[str, object]]:
    return [_semantic_item(text) for text in _strings(value, field)]


def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _head(assembly) -> str:
    head = git_full_sha(assembly.context.active_worktree_root)
    if head is None:
        raise RunScaffoldRefusal("active worktree HEAD cannot be resolved")
    return head


def _git_file(assembly, head: str, value: object, field: str) -> tuple[str, bytes]:
    path = _string(value, field)
    parsed = Path(path)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise RunScaffoldRefusal(f"{field} must be a relative project path")
    try:
        content = git_read_bytes(
            assembly.context.active_worktree_root, "show", f"{head}:{path}")
    except GitReadError as error:
        raise RunScaffoldRefusal(f"{field} is not readable at current HEAD: {error}") from error
    return path, content


def _run_rows(assembly) -> tuple[tuple[str, str], ...]:
    with assembly.store._connection_lock:  # noqa: SLF001 - deterministic run projection
        rows = assembly.store._connection.execute(  # noqa: SLF001
            "SELECT run_id, state FROM runs ORDER BY rowid DESC").fetchall()
    return tuple((row["run_id"], row["state"]) for row in rows)


def _same_project_fact(
    left: Mapping[str, object], right: Mapping[str, object],
) -> bool:
    if left.get("kind") != "project-fact" or right.get("kind") != "project-fact":
        return dict(left) == dict(right)
    fields = {"kind", "path", "fact_id", "fact_digest", "binding"}
    return {field: left.get(field) for field in fields} == {
        field: right.get(field) for field in fields}


def _current_candidate_source(assembly, task_id: str):
    for run_id, state in _run_rows(assembly):
        if state not in {"closeout-ready", "completed"}:
            continue
        spec = load_run_spec(run_id, start=assembly.context.canonical_root)
        if (spec.job_input.task_id != task_id
                or spec.lifecycle_stage.value != "explore"):
            continue
        reference_id = f"candidate:{run_id}"
        try:
            reference = assembly.store.get_artifact_reference(reference_id)
        except RecordNotFoundError as error:
            raise RunScaffoldRefusal(
                f"current explore run {run_id} has no frozen candidate") from error
        return {
            "kind": "evidence",
            "reference_id": reference.reference_id,
            "digest": reference.digest,
        }
    raise RunScaffoldRefusal(
        f"no closeout-ready explore candidate exists for task {task_id!r}")


def _current_evaluation_sources(assembly, task_id: str, objective: Mapping[str, object]):
    for run_id, state in _run_rows(assembly):
        if state not in {"closeout-ready", "completed"}:
            continue
        spec = load_run_spec(run_id, start=assembly.context.canonical_root)
        if (spec.job_input.task_id != task_id
                or spec.lifecycle_stage.value != "evaluate"
                or not _same_project_fact(spec.objective_ref.to_dict(), objective)
                or spec.candidate is None):
            continue
        evidence_id = f"evaluation-evidence:{run_id}"
        try:
            evidence_ref = assembly.store.get_artifact_reference(evidence_id)
            candidate_id = str(spec.candidate["reference_id"])
            candidate_ref = assembly.store.get_artifact_reference(candidate_id)
            evidence = parse_evaluation_evidence_bytes(
                assembly.artifact_store.read_reference(evidence_ref))
        except (KeyError, RecordNotFoundError, WorkflowError) as error:
            raise RunScaffoldRefusal(
                f"current evaluate run {run_id} has incomplete frozen lineage: {error}") from error
        if evidence.result != "pass":
            continue
        return (
            {
                "kind": "evidence",
                "reference_id": candidate_ref.reference_id,
                "digest": candidate_ref.digest,
            },
            {
                "kind": "evaluation-evidence",
                "reference_id": evidence_ref.reference_id,
                "candidate_digest": evidence.candidate_digest,
                "generation": evidence.evaluation_generation,
                "digest": evidence_ref.digest,
            },
            spec.objective_ref.to_dict(),
        )
    raise RunScaffoldRefusal(
        f"no passed evaluation exists for task {task_id!r} and objective")


def _evaluation_spec_source(
    assembly, head: str, value: object, objective: Mapping[str, object],
) -> dict[str, object]:
    path, content = _git_file(assembly, head, value, "evaluation_spec_path")
    try:
        spec = parse_evaluation_spec_bytes(content)
    except WorkflowError as error:
        raise RunScaffoldRefusal(f"evaluation_spec_path is invalid: {error}") from error
    if not _same_project_fact(spec.objective_ref, objective):
        raise RunScaffoldRefusal(
            "evaluation spec objective differs from the current ProjectFactRef")
    return {
        "kind": "evaluation-spec",
        "commit": head,
        "path": path,
        "generation": spec.generation,
        "digest": spec.digest,
    }


def _expectations(
    value: object, field: str, source: Mapping[str, object],
) -> list[dict[str, object]]:
    if not isinstance(value, list) or not value:
        raise RunScaffoldRefusal(f"{field} must be a non-empty list")
    result = []
    seen = set()
    for index, item in enumerate(value):
        row = _mapping(item, f"{field}[{index}]")
        _exact(row, {"criterion_id", "kind", "text"}, f"{field}[{index}]")
        criterion_id = _string(row["criterion_id"], f"{field}[{index}].criterion_id")
        if criterion_id in seen:
            raise RunScaffoldRefusal(f"{field} criterion ids must be unique")
        seen.add(criterion_id)
        result.append({
            "criterion_id": criterion_id,
            "kind": _string(row["kind"], f"{field}[{index}].kind"),
            "text": _string(row["text"], f"{field}[{index}].text"),
            "source": dict(source),
        })
    return result


def _references(assembly, head: str, value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise RunScaffoldRefusal("references must be a list")
    result = []
    for index, item in enumerate(value):
        row = _mapping(item, f"references[{index}]")
        _exact(row, {"path", "anchor", "purpose"}, f"references[{index}]")
        path, content = _git_file(assembly, head, row["path"], f"references[{index}].path")
        result.append({
            "path": path,
            "anchor": _string(row["anchor"], f"references[{index}].anchor"),
            "digest": _sha256(content),
            "purpose": _string(row["purpose"], f"references[{index}].purpose"),
        })
    return result


def _promotion_sources(assembly, head: str, value: object) -> list[dict[str, str]]:
    row = _mapping(value, "promotion_records")
    fields = {"regression_contract", "supported_scope", "accepted_risks"}
    _exact(row, fields, "promotion_records")
    sources = []
    for name in sorted(fields):
        _path, content = _git_file(assembly, head, row[name], f"promotion_records.{name}")
        artifact = assembly.artifact_store.write(content)
        sources.append({
            "kind": "evidence",
            "reference_id": name.replace("_", "-") + ":scaffold",
            "digest": artifact.digest,
        })
    return sources


def scaffold_work_brief(assembly, task_id: str, content: bytes) -> bytes:
    """Bind a semantic draft to current Git/store authority and emit canonical WorkBrief bytes."""
    task_id = _string(task_id, "task_id")
    draft = _document(content, "WorkBrief semantic draft")
    stage = draft.get("lifecycle_stage")
    fields = set(_BRIEF_FIELDS)
    if stage == "evaluate":
        fields.add("evaluation_spec_path")
    elif stage == "promote":
        fields.add("promotion_records")
    _exact(draft, fields, "WorkBrief semantic draft")
    if stage not in {"explore", "evaluate", "promote"}:
        raise RunScaffoldRefusal("lifecycle_stage must be explore, evaluate, or promote")

    head = _head(assembly)
    frame = read_project_frame_at_commit(assembly.context.active_worktree_root, head)
    fact_id = _string(draft["objective_fact_id"], "objective_fact_id")
    try:
        objective = frame.fact_ref(fact_id).to_dict()
    except WorkflowError as error:
        raise RunScaffoldRefusal(
            f"objective_fact_id is not a current project fact: {error}") from error

    current_state = _semantic_items(draft["current_state"], "current_state")
    expectation_source = objective
    if stage == "evaluate":
        candidate = _current_candidate_source(assembly, task_id)
        evaluation_spec = _evaluation_spec_source(
            assembly, head, draft["evaluation_spec_path"], objective)
        current_state.append({
            "text": "The harness selected the current frozen candidate lineage for evaluation.",
            "provenance": "harness-observation",
            "source": candidate,
        })
        expectation_source = evaluation_spec
    elif stage == "promote":
        current_objective = objective
        candidate, evaluation, objective = _current_evaluation_sources(
            assembly, task_id, current_objective)
        sources = [candidate, evaluation, *_promotion_sources(
            assembly, head, draft["promotion_records"])]
        current_state.append({
            "text": "The harness selected the current passed candidate/evaluation lineage.",
            "provenance": "harness-observation",
            "sources": sources,
        })
        expectation_source = evaluation

    decisions = _mapping(draft["decisions"], "decisions")
    _exact(
        decisions, {"fixed", "worker_may_choose", "requires_escalation"}, "decisions")
    payload = {
        "schema": "waystone-work-brief-1",
        "brief_id": new_run_id(),
        "task_id": task_id,
        "revision": 1,
        "supersedes_digest": None,
        "resolves_context_request_digest": None,
        "lifecycle_stage": stage,
        "objective": {
            "ref": objective,
            "desired_delta": _string(draft["desired_delta"], "desired_delta"),
            "why_now": _semantic_item(_string(draft["why_now"], "why_now")),
        },
        "current_state": current_state,
        "decisions": {
            "fixed": _semantic_items(decisions["fixed"], "decisions.fixed"),
            "worker_may_choose": _semantic_items(
                decisions["worker_may_choose"], "decisions.worker_may_choose"),
            "requires_escalation": _semantic_items(
                decisions["requires_escalation"], "decisions.requires_escalation"),
        },
        "constraints": _semantic_items(draft["constraints"], "constraints"),
        "non_goals": _semantic_items(draft["non_goals"], "non_goals"),
        "known_failures": _semantic_items(draft["known_failures"], "known_failures"),
        "evidence_expected": _expectations(
            draft["evidence_expected"], "evidence_expected", expectation_source),
        "references": _references(assembly, head, draft["references"]),
        "open_questions": _semantic_items(draft["open_questions"], "open_questions"),
    }
    canonical = completion.canonical_json(payload)
    parse_work_brief_bytes(canonical, artifact_store=assembly.artifact_store)
    return canonical


def _outcome_evidence(assembly, run_id: str, value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise RunScaffoldRefusal("evidence_refs must be a list")
    result = []
    seen = set()
    for index, item in enumerate(value):
        row = _mapping(item, f"evidence_refs[{index}]")
        _exact(row, {"kind", "reference_id"}, f"evidence_refs[{index}]")
        reference_id = _string(row["reference_id"], f"evidence_refs[{index}].reference_id")
        if reference_id in seen:
            raise RunScaffoldRefusal("evidence_refs reference ids must be unique")
        seen.add(reference_id)
        try:
            reference = assembly.store.get_artifact_reference(reference_id)
        except RecordNotFoundError as error:
            raise RunScaffoldRefusal(
                f"evidence reference is not frozen: {reference_id}") from error
        with assembly.store._connection_lock:  # noqa: SLF001 - immutable reference ownership
            owner = assembly.store._connection.execute(  # noqa: SLF001
                "SELECT run_id FROM artifacts WHERE reference_id = ?", (reference_id,),
            ).fetchone()
        if owner is None or owner["run_id"] != run_id:
            raise RunScaffoldRefusal(
                f"evidence reference is not owned by run {run_id}: {reference_id}")
        result.append({
            "kind": _string(row["kind"], f"evidence_refs[{index}].kind"),
            "reference_id": reference_id,
            "digest": reference.digest,
        })
    return result


def scaffold_outcome_delta(assembly, run_id: str, content: bytes) -> bytes:
    """Bind outcome meaning to the frozen run/result/profile lineage and emit YAML bytes."""
    run_id = _string(run_id, "run_id")
    draft = _document(content, "OutcomeDelta semantic draft")
    _exact(draft, _OUTCOME_FIELDS, "OutcomeDelta semantic draft")
    spec = load_run_spec(run_id, start=assembly.context.canonical_root)
    try:
        result = _final_result_authority(assembly, spec)
    except OutcomeBindingRefusal as error:
        raise RunScaffoldRefusal(error.detail) from error
    payload = {
        "schema": OUTCOME_SCHEMA,
        "run_id": run_id,
        "run_spec_digest": spec.run_spec_digest,
        "lifecycle_stage": spec.lifecycle_stage.value,
        "objective_ref": spec.objective_ref.to_dict(),
        "kind": _string(draft["kind"], "kind"),
        "summary": _string(draft["summary"], "summary"),
        "result_digest": result.result_digest,
        "evidence_refs": _outcome_evidence(assembly, run_id, draft["evidence_refs"]),
        "finding_refs": _strings(draft["finding_refs"], "finding_refs"),
        "recorded_by": {
            "role": "coordinator",
            "binding_digest": assembly.profile.binding_for(Role.COORDINATOR).binding_digest,
            "principal": None,
        },
        "rationale": _string(draft["rationale"], "rationale"),
    }
    output = yaml.safe_dump(
        payload, allow_unicode=True, sort_keys=False, default_flow_style=False,
    ).encode("utf-8")
    parse_outcome_delta_bytes(output)
    return output


__all__ = [
    "RunScaffoldRefusal",
    "scaffold_outcome_delta",
    "scaffold_work_brief",
]
