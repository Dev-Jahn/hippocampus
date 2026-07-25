"""Authority-bound lifecycle completion contracts."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence, TypeAlias

import yaml

from waystone.adapters.git import build_git_environment
from waystone.core import WorkflowError
from waystone.project.brief import read_project_frame_at_commit
from waystone.runs.artifacts import ArtifactStore, StoredArtifact, validate_sha256_digest


COMPLETION_CONTRACT_SCHEMA = "waystone-completion-contract-1"
EVALUATION_SPEC_SCHEMA = "waystone-evaluation-spec-1"
EVALUATION_EVIDENCE_SCHEMA = "waystone-evaluation-evidence-1"
COMPILER_RULE_ID = "stage-completion-v1"

_FULL_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_FACT_ID_RE = re.compile(
    r"^(commitment|prototype|long-term|non-goal|hypothesis|question|trigger)/[a-z0-9-]+$")
_DIGEST_FIELDS = ("digest", "fact_digest", "item_digest")


def canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


class CompletionError(WorkflowError):
    """Base class for typed completion-contract refusals."""

    code = "completion_contract_error"

    def __init__(self, message: str):
        super().__init__(f"{self.code}: {message}")


class AuthorityRefRefusal(CompletionError):
    code = "authority_ref_refusal"


class AuthorityEvidenceRefusal(CompletionError):
    code = "authority_evidence_refusal"


class EvaluationEvidenceRefusal(AuthorityEvidenceRefusal):
    code = "evaluation_evidence_refusal"


class CompletionContractRefusal(CompletionError):
    code = "completion_contract_refusal"


class StageModeRefusal(CompletionError):
    code = "completion_stage_mode_refusal"


class LifecycleStage(str, Enum):
    EXPLORE = "explore"
    EVALUATE = "evaluate"
    PROMOTE = "promote"


class CompletionMode(str, Enum):
    LEARNING = "learning"
    EVALUATION = "evaluation"
    PROMOTION = "promotion"


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AuthorityRefRefusal(f"{field}: must be a mapping")
    return dict(value)


def _allowed(row: Mapping[str, Any], fields: frozenset[str], field: str) -> None:
    unknown = sorted(set(row) - fields)
    missing = sorted(fields - set(row))
    if unknown or missing:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise AuthorityRefRefusal(f"{field}: " + "; ".join(details))


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuthorityRefRefusal(f"{field}: must be a non-empty string")
    return value


def _digest(value: Any, field: str) -> str:
    value = _string(value, field)
    try:
        return validate_sha256_digest(value)
    except ValueError as error:
        raise AuthorityRefRefusal(f"{field}: {error}") from error


def _commit(value: Any, field: str) -> str:
    value = _string(value, field)
    if _FULL_COMMIT_RE.fullmatch(value) is None:
        raise AuthorityRefRefusal(f"{field}: must be a full lowercase Git object id")
    return value


def _path(value: Any, field: str) -> str:
    value = _string(value, field)
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise AuthorityRefRefusal(f"{field}: must be a relative path inside the project")
    return value


def _binding(value: Any, field: str) -> str:
    if value not in ("binding", "nonbinding"):
        raise AuthorityRefRefusal(f"{field}: must be binding or nonbinding")
    return value


def _generation(value: Any, field: str) -> int:
    if type(value) is not int or value < 1:
        raise AuthorityRefRefusal(f"{field}: must be a positive integer")
    return value


@dataclass(frozen=True)
class ProjectFactObjectiveRef:
    commit: str
    path: str
    fact_id: str
    fact_digest: str
    binding: str
    kind: str = "project-fact"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "commit": self.commit,
            "path": self.path,
            "fact_id": self.fact_id,
            "fact_digest": self.fact_digest,
            "binding": self.binding,
        }


@dataclass(frozen=True)
class OwnerRequestObjectiveRef:
    artifact_reference_id: str
    digest: str
    binding: str
    kind: str = "owner-request"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "artifact_reference_id": self.artifact_reference_id,
            "digest": self.digest,
            "binding": self.binding,
        }


@dataclass(frozen=True)
class MilestoneObjectiveRef:
    commit: str
    path: str
    item_id: str
    item_digest: str
    binding: str
    kind: str = "milestone"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "commit": self.commit,
            "path": self.path,
            "item_id": self.item_id,
            "item_digest": self.item_digest,
            "binding": self.binding,
        }


@dataclass(frozen=True)
class AcceptedADRRef:
    commit: str
    path: str
    decision_id: str
    digest: str
    kind: str = "accepted-adr"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "commit": self.commit,
            "path": self.path,
            "decision_id": self.decision_id,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class EvaluationSpecRef:
    commit: str
    path: str
    generation: int
    digest: str
    kind: str = "evaluation-spec"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "commit": self.commit,
            "path": self.path,
            "generation": self.generation,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class EvaluationEvidenceRef:
    reference_id: str
    candidate_digest: str
    generation: int
    digest: str
    kind: str = "evaluation-evidence"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reference_id": self.reference_id,
            "candidate_digest": self.candidate_digest,
            "generation": self.generation,
            "digest": self.digest,
        }


ObjectiveRef: TypeAlias = ProjectFactObjectiveRef | OwnerRequestObjectiveRef | MilestoneObjectiveRef
AuthorityRef: TypeAlias = ObjectiveRef | AcceptedADRRef | EvaluationSpecRef | EvaluationEvidenceRef


def parse_objective_ref(value: Mapping[str, Any], field: str = "objective_ref") -> ObjectiveRef:
    row = _mapping(value, field)
    kind = row.get("kind")
    if kind == "project-fact":
        _allowed(row, frozenset({
            "kind", "commit", "path", "fact_id", "fact_digest", "binding",
        }), field)
        fact_id = _string(row["fact_id"], f"{field}.fact_id")
        if _FACT_ID_RE.fullmatch(fact_id) is None:
            raise AuthorityRefRefusal(f"{field}.fact_id: invalid project fact marker")
        return ProjectFactObjectiveRef(
            _commit(row["commit"], f"{field}.commit"),
            _path(row["path"], f"{field}.path"),
            fact_id,
            _digest(row["fact_digest"], f"{field}.fact_digest"),
            _binding(row["binding"], f"{field}.binding"),
        )
    if kind == "owner-request":
        _allowed(row, frozenset({
            "kind", "artifact_reference_id", "digest", "binding",
        }), field)
        return OwnerRequestObjectiveRef(
            _string(row["artifact_reference_id"], f"{field}.artifact_reference_id"),
            _digest(row["digest"], f"{field}.digest"),
            _binding(row["binding"], f"{field}.binding"),
        )
    if kind == "milestone":
        _allowed(row, frozenset({
            "kind", "commit", "path", "item_id", "item_digest", "binding",
        }), field)
        return MilestoneObjectiveRef(
            _commit(row["commit"], f"{field}.commit"),
            _path(row["path"], f"{field}.path"),
            _string(row["item_id"], f"{field}.item_id"),
            _digest(row["item_digest"], f"{field}.item_digest"),
            _binding(row["binding"], f"{field}.binding"),
        )
    raise AuthorityRefRefusal(
        f"{field}.kind: must be project-fact, owner-request, or milestone")


def parse_authority_ref(value: Mapping[str, Any], field: str = "source") -> AuthorityRef:
    row = _mapping(value, field)
    kind = row.get("kind")
    if kind in ("project-fact", "owner-request", "milestone"):
        return parse_objective_ref(row, field)
    if kind == "accepted-adr":
        _allowed(row, frozenset({"kind", "commit", "path", "decision_id", "digest"}), field)
        return AcceptedADRRef(
            _commit(row["commit"], f"{field}.commit"),
            _path(row["path"], f"{field}.path"),
            _string(row["decision_id"], f"{field}.decision_id"),
            _digest(row["digest"], f"{field}.digest"),
        )
    if kind == "evaluation-spec":
        _allowed(row, frozenset({"kind", "commit", "path", "generation", "digest"}), field)
        return EvaluationSpecRef(
            _commit(row["commit"], f"{field}.commit"),
            _path(row["path"], f"{field}.path"),
            _generation(row["generation"], f"{field}.generation"),
            _digest(row["digest"], f"{field}.digest"),
        )
    if kind == "evaluation-evidence":
        _allowed(row, frozenset({
            "kind", "reference_id", "candidate_digest", "generation", "digest",
        }), field)
        return EvaluationEvidenceRef(
            _string(row["reference_id"], f"{field}.reference_id"),
            _digest(row["candidate_digest"], f"{field}.candidate_digest"),
            _generation(row["generation"], f"{field}.generation"),
            _digest(row["digest"], f"{field}.digest"),
        )
    raise AuthorityRefRefusal(f"{field}.kind: unsupported authority reference")


@dataclass(frozen=True)
class EvidenceRequirement:
    kind: str
    reference_id: str | None = None
    digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {"kind": self.kind}
        if self.reference_id is not None:
            row["reference_id"] = self.reference_id
        if self.digest is not None:
            row["digest"] = self.digest
        return row


@dataclass(frozen=True)
class CompletionCriterion:
    id: str
    mode: CompletionMode
    text: str
    source: AuthorityRef
    binding: str
    evidence: EvidenceRequirement

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mode": self.mode.value,
            "text": self.text,
            "source": self.source.to_dict(),
            "binding": self.binding,
            "evidence": self.evidence.to_dict(),
        }


@dataclass(frozen=True)
class CompletionContract:
    lifecycle_stage: LifecycleStage
    objective_ref: ObjectiveRef
    criteria: tuple[CompletionCriterion, ...]
    compiler_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": COMPLETION_CONTRACT_SCHEMA,
            "lifecycle_stage": self.lifecycle_stage.value,
            "objective_ref": self.objective_ref.to_dict(),
            "criteria": [criterion.to_dict() for criterion in self.criteria],
            "compiled_by": {
                "rule_id": COMPILER_RULE_ID,
                "compiler_digest": self.compiler_digest,
            },
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_dict())


@dataclass(frozen=True)
class PublishedCompletionContract:
    contract: CompletionContract
    artifact: StoredArtifact
    reference_id: str


_COMPILER_DIGEST = digest_bytes(canonical_json({
    "rule_id": COMPILER_RULE_ID,
    "stage_modes": {
        "explore": "learning",
        "evaluate": "evaluation",
        "promote": "promotion",
    },
    "authority_union": (
        "project-fact", "owner-request", "milestone", "accepted-adr",
        "evaluation-spec", "evaluation-evidence",
    ),
}))


def _git_bytes(root: Path, commit: str, path: str, field: str) -> bytes:
    try:
        process = subprocess.run(
            ["git", "show", f"{commit}:{path}"], cwd=root, check=False,
            env=build_git_environment(
                overrides={"GIT_OPTIONAL_LOCKS": "0"}),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise AuthorityEvidenceRefusal(f"{field}: cannot invoke Git: {error}") from error
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", "replace").strip()
        raise AuthorityEvidenceRefusal(f"{field}: cannot read {commit}:{path}: {detail}")
    return process.stdout


class AuthorityResolver:
    """Resolve refs against authoritative Git or CAS bytes, never caller summaries."""

    def __init__(self, root: Path, artifact_store: ArtifactStore | None = None):
        self.root = Path(root).resolve()
        self.artifact_store = artifact_store or ArtifactStore(self.root)

    def validate(self, ref: AuthorityRef) -> None:
        if isinstance(ref, ProjectFactObjectiveRef):
            frame = read_project_frame_at_commit(self.root, ref.commit)
            if frame.path != ref.path:
                raise AuthorityEvidenceRefusal(
                    f"project-fact path mismatch: ref {ref.path}, configured {frame.path}")
            fact = frame.fact(ref.fact_id)
            if fact.digest != ref.fact_digest or fact.binding != ref.binding:
                raise AuthorityEvidenceRefusal(
                    f"project-fact {ref.fact_id} digest/binding does not match committed bytes")
            return
        if isinstance(ref, OwnerRequestObjectiveRef):
            payload = self.artifact_store.read(ref.digest)
            if not payload:
                raise AuthorityEvidenceRefusal("owner-request authority bytes must be non-empty")
            return
        if isinstance(ref, MilestoneObjectiveRef):
            payload = _git_bytes(self.root, ref.commit, ref.path, "milestone")
            try:
                document = yaml.safe_load(payload.decode("utf-8"))
            except (UnicodeDecodeError, yaml.YAMLError) as error:
                raise AuthorityEvidenceRefusal(f"milestone source is unreadable: {error}") from error
            items = document.get("tasks") if isinstance(document, Mapping) else None
            matches = [item for item in (items or [])
                       if isinstance(item, Mapping) and item.get("id") == ref.item_id]
            if len(matches) != 1:
                raise AuthorityEvidenceRefusal(
                    f"milestone item {ref.item_id!r} must resolve exactly once")
            if digest_bytes(canonical_json(dict(matches[0]))) != ref.item_digest:
                raise AuthorityEvidenceRefusal("milestone item digest does not match committed item")
            return
        if isinstance(ref, AcceptedADRRef):
            payload = _git_bytes(self.root, ref.commit, ref.path, "accepted ADR")
            if digest_bytes(payload) != ref.digest:
                raise AuthorityEvidenceRefusal("accepted ADR digest does not match committed bytes")
            text = payload.decode("utf-8", "replace")
            if not re.search(r"(?mi)^-?\s*Status:\s*(?:\*\*)?accepted(?:\*\*)?\s*$", text):
                raise AuthorityEvidenceRefusal("ADR is not recorded with accepted status")
            return
        if isinstance(ref, EvaluationSpecRef):
            payload = _git_bytes(self.root, ref.commit, ref.path, "evaluation spec")
            if digest_bytes(payload) != ref.digest:
                raise AuthorityEvidenceRefusal("evaluation spec digest does not match committed bytes")
            try:
                document = yaml.safe_load(payload.decode("utf-8"))
            except (UnicodeDecodeError, yaml.YAMLError) as error:
                raise AuthorityEvidenceRefusal(f"evaluation spec is unreadable: {error}") from error
            if not isinstance(document, Mapping) or document.get("schema") != EVALUATION_SPEC_SCHEMA:
                raise AuthorityEvidenceRefusal("evaluation spec schema is invalid")
            if document.get("generation") != ref.generation:
                raise AuthorityEvidenceRefusal("evaluation spec generation does not match ref")
            return
        if isinstance(ref, EvaluationEvidenceRef):
            self._validate_evaluation_evidence(ref)
            return
        raise TypeError("unsupported AuthorityRef instance")

    def _validate_evaluation_evidence(self, ref: EvaluationEvidenceRef) -> None:
        # ArtifactStore.read rehashes the actual CAS bytes. No caller-provided pass/generation tuple
        # participates in this decision.
        payload = self.artifact_store.read(ref.digest)
        try:
            document = yaml.safe_load(payload.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as error:
            raise EvaluationEvidenceRefusal(f"CAS evidence is unreadable: {error}") from error
        if not isinstance(document, Mapping):
            raise EvaluationEvidenceRefusal("CAS evidence must be a mapping")
        allowed = {
            "schema", "candidate_digest", "evaluation_spec_digest", "evaluation_generation",
            "evaluator_action_id", "result", "metric_artifacts",
        }
        if set(document) != allowed:
            raise EvaluationEvidenceRefusal("CAS evidence fields do not match schema v1")
        if document.get("schema") != EVALUATION_EVIDENCE_SCHEMA:
            raise EvaluationEvidenceRefusal("CAS evidence schema is invalid")
        if document.get("result") != "pass":
            raise EvaluationEvidenceRefusal("evaluation evidence did not pass")
        if document.get("candidate_digest") != ref.candidate_digest:
            raise EvaluationEvidenceRefusal("evaluation evidence candidate lineage mismatch")
        if document.get("evaluation_generation") != ref.generation:
            raise EvaluationEvidenceRefusal("evaluation evidence generation mismatch")
        try:
            validate_sha256_digest(document.get("evaluation_spec_digest"))
        except ValueError as error:
            raise EvaluationEvidenceRefusal("evaluation spec digest is invalid") from error
        if not isinstance(document.get("evaluator_action_id"), str) or not document["evaluator_action_id"].strip():
            raise EvaluationEvidenceRefusal("evaluator action id is invalid")
        metrics = document.get("metric_artifacts")
        if not isinstance(metrics, list):
            raise EvaluationEvidenceRefusal("metric_artifacts must be a list")
        for index, metric in enumerate(metrics):
            if not isinstance(metric, Mapping) or set(metric) != {"criterion_id", "reference_id", "digest"}:
                raise EvaluationEvidenceRefusal(f"metric_artifacts[{index}] is invalid")
            try:
                _string(metric["criterion_id"], f"metric_artifacts[{index}].criterion_id")
                _string(metric["reference_id"], f"metric_artifacts[{index}].reference_id")
                _digest(metric["digest"], f"metric_artifacts[{index}].digest")
            except AuthorityRefRefusal as error:
                raise EvaluationEvidenceRefusal(str(error)) from error


def _evidence(value: Any, field: str) -> EvidenceRequirement:
    row = _mapping(value, field)
    if not set(row).issubset({"kind", "reference_id", "digest"}) or "kind" not in row:
        raise CompletionContractRefusal(
            f"{field}: fields must be kind with optional reference_id/digest")
    kind = _string(row["kind"], f"{field}.kind")
    reference_id = None
    if "reference_id" in row:
        reference_id = _string(row["reference_id"], f"{field}.reference_id")
    digest = None
    if "digest" in row:
        digest = _digest(row["digest"], f"{field}.digest")
    return EvidenceRequirement(kind, reference_id, digest)


def _criterion(value: Mapping[str, Any], index: int) -> CompletionCriterion:
    row = _mapping(value, f"criteria[{index}]")
    required = {"id", "mode", "text", "source", "binding", "evidence"}
    if set(row) != required:
        raise CompletionContractRefusal(f"criteria[{index}]: fields must be {sorted(required)}")
    try:
        mode = CompletionMode(row["mode"])
    except (TypeError, ValueError) as error:
        raise CompletionContractRefusal(f"criteria[{index}].mode is invalid") from error
    return CompletionCriterion(
        _string(row["id"], f"criteria[{index}].id"),
        mode,
        _string(row["text"], f"criteria[{index}].text"),
        parse_authority_ref(row["source"], f"criteria[{index}].source"),
        _binding(row["binding"], f"criteria[{index}].binding"),
        _evidence(row["evidence"], f"criteria[{index}].evidence"),
    )


def _validate_stage_objective(stage: LifecycleStage, objective: ObjectiveRef) -> None:
    if stage is not LifecycleStage.PROMOTE:
        return
    if isinstance(objective, ProjectFactObjectiveRef):
        prefix = objective.fact_id.partition("/")[0]
        if prefix not in ("commitment", "prototype") or objective.binding != "binding":
            raise StageModeRefusal(
                "promote objective must be a binding commitment/prototype or exact owner request")
        return
    if isinstance(objective, OwnerRequestObjectiveRef) and objective.binding == "binding":
        return
    raise StageModeRefusal(
        "promote objective must be a binding commitment/prototype or exact owner request")


def _validate_stage_criterion(
    stage: LifecycleStage, criterion: CompletionCriterion, resolver: AuthorityResolver
) -> None:
    expected_mode = {
        LifecycleStage.EXPLORE: CompletionMode.LEARNING,
        LifecycleStage.EVALUATE: CompletionMode.EVALUATION,
        LifecycleStage.PROMOTE: CompletionMode.PROMOTION,
    }[stage]
    if criterion.mode is not expected_mode:
        raise StageModeRefusal(
            f"{stage.value} criterion {criterion.id!r} must use mode {expected_mode.value}")
    source = criterion.source
    if stage is LifecycleStage.EXPLORE:
        if not isinstance(source, ProjectFactObjectiveRef):
            raise StageModeRefusal("explore learning criterion must source a project hypothesis/question")
        prefix = source.fact_id.partition("/")[0]
        if prefix not in ("hypothesis", "question") or source.binding != "nonbinding":
            raise StageModeRefusal(
                "explore learning criterion must keep hypothesis/question nonbinding")
        if criterion.binding != "nonbinding":
            raise StageModeRefusal("explore learning criterion binding must be nonbinding")
    elif stage is LifecycleStage.EVALUATE:
        if not isinstance(source, EvaluationSpecRef):
            raise StageModeRefusal("evaluate criterion must source a frozen evaluation spec")
        if criterion.binding != "binding":
            raise StageModeRefusal("evaluate criterion binding must be binding")
    else:
        allowed = False
        if isinstance(source, ProjectFactObjectiveRef):
            allowed = (
                source.fact_id.partition("/")[0] in ("commitment", "prototype", "non-goal")
                and source.binding == "binding"
            )
        elif isinstance(source, OwnerRequestObjectiveRef):
            allowed = source.binding == "binding"
        elif isinstance(source, (AcceptedADRRef, EvaluationEvidenceRef)):
            allowed = True
        if not allowed or criterion.binding != "binding":
            raise StageModeRefusal(
                "promotion criterion requires binding committed fact/owner request, accepted ADR, "
                "or passed same-generation evaluation evidence")
    resolver.validate(source)


def compile_completion_contract(
    root: Path,
    lifecycle_stage: str | LifecycleStage,
    objective_ref: Mapping[str, Any] | ObjectiveRef,
    criteria: Sequence[Mapping[str, Any]],
    *,
    artifact_store: ArtifactStore | None = None,
) -> CompletionContract:
    try:
        stage = LifecycleStage(lifecycle_stage)
    except (TypeError, ValueError) as error:
        raise CompletionContractRefusal("lifecycle_stage must be explore, evaluate, or promote") from error
    objective = (
        parse_objective_ref(objective_ref)
        if isinstance(objective_ref, Mapping)
        else objective_ref
    )
    if not isinstance(objective, (ProjectFactObjectiveRef, OwnerRequestObjectiveRef, MilestoneObjectiveRef)):
        raise CompletionContractRefusal("objective_ref must be an ObjectiveRef variant")
    if not isinstance(criteria, Sequence) or isinstance(criteria, (str, bytes)) or not criteria:
        raise CompletionContractRefusal("criteria must be a non-empty sequence")
    parsed = tuple(_criterion(item, index) for index, item in enumerate(criteria))
    ids = [criterion.id for criterion in parsed]
    if len(ids) != len(set(ids)):
        raise CompletionContractRefusal("criterion ids must be unique")
    resolver = AuthorityResolver(root, artifact_store)
    resolver.validate(objective)
    _validate_stage_objective(stage, objective)
    for criterion in parsed:
        _validate_stage_criterion(stage, criterion, resolver)
    return CompletionContract(stage, objective, parsed, _COMPILER_DIGEST)


def parse_completion_contract_bytes(
    root: Path,
    content: bytes,
    *,
    artifact_store: ArtifactStore | None = None,
) -> CompletionContract:
    """Read canonical contract bytes and re-run authority resolution and stage compilation."""
    if not isinstance(content, bytes):
        raise TypeError("completion contract content must be bytes")
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CompletionContractRefusal(
            f"completion contract must be canonical UTF-8 JSON: {error}") from error
    if not isinstance(payload, Mapping) or canonical_json(payload) != content:
        raise CompletionContractRefusal(
            "completion contract bytes must use sorted-key compact canonical JSON")
    required = {"schema", "lifecycle_stage", "objective_ref", "criteria", "compiled_by"}
    if set(payload) != required:
        raise CompletionContractRefusal(
            f"completion contract fields must be {sorted(required)}")
    if payload["schema"] != COMPLETION_CONTRACT_SCHEMA:
        raise CompletionContractRefusal(f"schema must be {COMPLETION_CONTRACT_SCHEMA}")
    compiled_by = payload["compiled_by"]
    if not isinstance(compiled_by, Mapping) or dict(compiled_by) != {
        "rule_id": COMPILER_RULE_ID,
        "compiler_digest": _COMPILER_DIGEST,
    }:
        raise CompletionContractRefusal("compiled_by does not identify this compiler rule")
    criteria = payload["criteria"]
    if not isinstance(criteria, list):
        raise CompletionContractRefusal("criteria must be a list")
    return compile_completion_contract(
        root,
        payload["lifecycle_stage"],
        payload["objective_ref"],
        criteria,
        artifact_store=artifact_store,
    )


def publish_completion_contract(
    root: Path,
    contract: CompletionContract,
    reference_id: str,
    *,
    artifact_store: ArtifactStore | None = None,
) -> PublishedCompletionContract:
    if not isinstance(contract, CompletionContract):
        raise TypeError("contract must be a CompletionContract")
    if not isinstance(reference_id, str) or not reference_id.strip():
        raise CompletionContractRefusal("reference_id must be non-empty")
    store = artifact_store or ArtifactStore(Path(root))
    artifact = store.write(contract.canonical_bytes())
    return PublishedCompletionContract(contract, artifact, reference_id)


__all__ = [
    "AcceptedADRRef",
    "AuthorityEvidenceRefusal",
    "AuthorityRef",
    "AuthorityRefRefusal",
    "AuthorityResolver",
    "CompletionContract",
    "CompletionContractRefusal",
    "CompletionCriterion",
    "CompletionError",
    "CompletionMode",
    "EvaluationEvidenceRef",
    "EvaluationEvidenceRefusal",
    "EvaluationSpecRef",
    "EvidenceRequirement",
    "LifecycleStage",
    "MilestoneObjectiveRef",
    "ObjectiveRef",
    "OwnerRequestObjectiveRef",
    "ProjectFactObjectiveRef",
    "PublishedCompletionContract",
    "StageModeRefusal",
    "canonical_json",
    "compile_completion_contract",
    "digest_bytes",
    "parse_authority_ref",
    "parse_completion_contract_bytes",
    "parse_objective_ref",
    "publish_completion_contract",
]
