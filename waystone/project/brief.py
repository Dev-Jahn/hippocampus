"""Typed PROJECT_BRIEF.md facts and owner-evidence-bound adoption."""
from __future__ import annotations

import hashlib
import json
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from waystone.adapters.git import build_git_environment
from waystone.core import WorkflowError, write_bytes_atomic
from waystone.runs.artifacts import ArtifactStore, StoredArtifact, validate_sha256_digest

from . import hold_project_lock, load_config, require_initialized_root


PROJECT_BRIEF_SCHEMA = "waystone-project-brief-1"
PROJECT_BRIEF_STATUSES = frozenset(("provisional", "committed"))
FACT_BINDINGS = frozenset(("binding", "nonbinding"))

_SECTION_PREFIXES = (
    ("Purpose", None),
    ("Commitments", "commitment"),
    ("Prototype scope", "prototype"),
    ("Long-term direction", "long-term"),
    ("Non-goals", "non-goal"),
    ("Working hypotheses", "hypothesis"),
    ("Open questions", "question"),
    ("Revision triggers", "trigger"),
)
_PREFIXES = tuple(prefix for _, prefix in _SECTION_PREFIXES if prefix is not None)
_FACT_ID_RE = re.compile(
    rb"^(commitment|prototype|long-term|non-goal|hypothesis|question|trigger)/[a-z0-9-]+$")
_MARKER_PREFIX_RE = re.compile(
    rb"\[(commitment|prototype|long-term|non-goal|hypothesis|question|trigger)(?=/|\])")
_LIST_RE = re.compile(rb"^[ \t]*[-*+]\s+")
_FACT_LIST_RE = re.compile(rb"^- \[([^]\r\n]+)\][ \t]+([^\r\n]+)(?:\r?\n)?$")
_HEADING_RE = re.compile(rb"^## ([^\r\n]+)(?:\r?\n)?$")
_FRONT_MATTER_RE = re.compile(rb"^([A-Za-z0-9_-]+):[ \t]*([^\r\n]*?)[ \t]*(?:\r?\n)?$")
_FULL_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


class ProjectBriefError(WorkflowError):
    """Base class for typed project-brief refusals."""

    code = "project_brief_error"

    def __init__(self, message: str):
        super().__init__(f"{self.code}: {message}")


class BriefReadError(ProjectBriefError):
    code = "project_brief_unreadable"


class BriefSchemaRefusal(ProjectBriefError):
    code = "project_brief_schema_refusal"


class BriefStatusRefusal(ProjectBriefError):
    code = "project_brief_status_refusal"


class BriefMarkerRefusal(ProjectBriefError):
    code = "project_brief_marker_refusal"


class BriefDuplicateRefusal(ProjectBriefError):
    code = "project_brief_duplicate_refusal"


class BriefAdoptionRefusal(ProjectBriefError):
    code = "project_brief_adoption_refusal"


@dataclass(frozen=True)
class SourceSpan:
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int


@dataclass(frozen=True)
class FrameStatusRef:
    commit: str | None
    path: str
    status: str
    digest: str
    source_span: SourceSpan


@dataclass(frozen=True)
class ProjectFact:
    id: str
    kind: str
    binding: str
    raw_bytes: bytes
    source_span: SourceSpan
    digest: str


@dataclass(frozen=True)
class ProjectFactRef:
    commit: str
    path: str
    fact_id: str
    fact_digest: str
    binding: str

    def __post_init__(self) -> None:
        if _FULL_COMMIT_RE.fullmatch(self.commit) is None:
            raise ValueError("commit must be a full lowercase Git object id")
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("path must be non-empty")
        if _FACT_ID_RE.fullmatch(self.fact_id.encode("ascii", "strict")) is None:
            raise ValueError("fact_id must be a canonical project fact marker")
        validate_sha256_digest(self.fact_digest)
        if self.binding not in FACT_BINDINGS:
            raise ValueError("binding must be binding or nonbinding")

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": "project-fact",
            "commit": self.commit,
            "path": self.path,
            "fact_id": self.fact_id,
            "fact_digest": self.fact_digest,
            "binding": self.binding,
        }


@dataclass(frozen=True)
class ProjectFrame:
    status: str
    declared_status: str
    status_ref: FrameStatusRef
    facts: tuple[ProjectFact, ...]
    schema: str
    path: str
    commit: str | None
    raw_bytes: bytes

    def fact(self, fact_id: str) -> ProjectFact:
        for fact in self.facts:
            if fact.id == fact_id:
                return fact
        raise BriefMarkerRefusal(f"unknown project fact {fact_id!r}")

    def fact_ref(self, fact_id: str) -> ProjectFactRef:
        if self.status == "superseded":
            raise BriefStatusRefusal("a superseded frame cannot source a new run")
        if self.commit is None:
            raise BriefReadError("a committed Git object id is required to create ProjectFactRef")
        fact = self.fact(fact_id)
        return ProjectFactRef(
            commit=self.commit,
            path=self.path,
            fact_id=fact.id,
            fact_digest=fact.digest,
            binding=fact.binding,
        )

    def to_dict(self, fact_id: str | None = None) -> dict[str, Any]:
        selected = self.facts if fact_id is None else (self.fact(fact_id),)
        return {
            "schema": self.schema,
            "status": self.status,
            "declared_status": self.declared_status,
            "status_ref": {
                "commit": self.status_ref.commit,
                "path": self.status_ref.path,
                "status": self.status_ref.status,
                "digest": self.status_ref.digest,
            },
            "facts": [
                {
                    "id": fact.id,
                    "kind": fact.kind,
                    "binding": fact.binding,
                    "digest": fact.digest,
                    "text": fact.raw_bytes.decode("utf-8").rstrip("\r\n"),
                    "source_span": {
                        "start_byte": fact.source_span.start_byte,
                        "end_byte": fact.source_span.end_byte,
                        "start_line": fact.source_span.start_line,
                        "end_line": fact.source_span.end_line,
                    },
                }
                for fact in selected
            ],
        }


@dataclass(frozen=True)
class BriefAdoption:
    frame: ProjectFrame
    owner_evidence: StoredArtifact
    adoption_record: StoredArtifact


def _fact_end(lines: list[bytes], start: int, section_end: int) -> int:
    """Return the exclusive end of one top-level Markdown list item."""
    index = start + 1
    while index < section_end:
        line = lines[index]
        if line.startswith((b"  ", b"\t")):
            index += 1
            continue
        if not line.strip():
            lookahead = index + 1
            if lookahead < section_end and lines[lookahead].startswith((b"  ", b"\t")):
                index += 1
                continue
        break
    return index


def _front_matter(
    lines: list[bytes], offsets: list[int]
) -> tuple[dict[str, bytes], dict[str, SourceSpan], int]:
    if not lines or lines[0].rstrip(b"\r\n") != b"---":
        raise BriefSchemaRefusal("document must start with YAML front matter")
    end = None
    for index in range(1, len(lines)):
        if lines[index].rstrip(b"\r\n") == b"---":
            end = index
            break
    if end is None:
        raise BriefSchemaRefusal("front matter has no closing delimiter")
    values: dict[str, bytes] = {}
    spans: dict[str, SourceSpan] = {}
    for index in range(1, end):
        match = _FRONT_MATTER_RE.fullmatch(lines[index])
        if match is None:
            raise BriefSchemaRefusal(f"invalid front matter at line {index + 1}")
        key = match.group(1).decode("ascii")
        if key in values:
            raise BriefDuplicateRefusal(f"duplicate front matter key {key!r}")
        values[key] = match.group(2)
        value_start = offsets[index] + match.start(2)
        spans[key] = SourceSpan(
            value_start, value_start + len(match.group(2)), index + 1, index + 1)
    if set(values) != {"schema", "status"}:
        missing = sorted({"schema", "status"} - set(values))
        unknown = sorted(set(values) - {"schema", "status"})
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if unknown:
            detail.append("unknown " + ", ".join(unknown))
        raise BriefSchemaRefusal("front matter keys must be exactly schema/status (" + "; ".join(detail) + ")")
    return values, spans, end


def parse_project_brief(
    content: bytes,
    *,
    path: str = "PROJECT_BRIEF.md",
    commit: str | None = None,
    superseded: bool = False,
) -> ProjectFrame:
    """Parse exact UTF-8 bytes into fact-level authority without normalizing content."""
    if not isinstance(content, bytes):
        raise TypeError("project brief content must be bytes")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BriefReadError(f"project brief is not UTF-8: {error}") from error
    if commit is not None and _FULL_COMMIT_RE.fullmatch(commit) is None:
        raise BriefReadError("commit must be a full lowercase Git object id")
    if not isinstance(path, str) or not path:
        raise BriefReadError("project brief path must be non-empty")

    lines = content.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    values, front_spans, front_end = _front_matter(lines, offsets)
    try:
        schema = values["schema"].decode("utf-8")
        declared_status = values["status"].decode("utf-8")
    except UnicodeDecodeError as error:
        raise BriefSchemaRefusal(f"front matter value is not UTF-8: {error}") from error
    if schema != PROJECT_BRIEF_SCHEMA:
        raise BriefSchemaRefusal(f"schema must be {PROJECT_BRIEF_SCHEMA!r}")
    if declared_status not in PROJECT_BRIEF_STATUSES:
        raise BriefStatusRefusal("status must be provisional or committed")

    headings: list[tuple[int, str]] = []
    seen_headings: set[str] = set()
    expected_names = [name for name, _ in _SECTION_PREFIXES]
    for index in range(front_end + 1, len(lines)):
        match = _HEADING_RE.fullmatch(lines[index])
        if match is None:
            continue
        name = match.group(1).decode("utf-8")
        if name not in expected_names:
            raise BriefSchemaRefusal(f"unknown level-two section {name!r} at line {index + 1}")
        if name in seen_headings:
            raise BriefDuplicateRefusal(f"duplicate section {name!r}")
        seen_headings.add(name)
        headings.append((index, name))
    actual_names = [name for _, name in headings]
    if actual_names != expected_names:
        raise BriefSchemaRefusal(
            "canonical sections must appear once and in order: " + ", ".join(expected_names))

    facts: list[ProjectFact] = []
    fact_ids: set[str] = set()
    allowed_markers: set[tuple[int, int]] = set()
    prefixes_by_section = dict(_SECTION_PREFIXES)
    for heading_position, (heading_index, section_name) in enumerate(headings):
        prefix = prefixes_by_section[section_name]
        section_end = (
            headings[heading_position + 1][0]
            if heading_position + 1 < len(headings)
            else len(lines)
        )
        index = heading_index + 1
        while index < section_end:
            line = lines[index]
            if not _LIST_RE.match(line):
                index += 1
                continue
            if prefix is None:
                index += 1
                continue
            match = _FACT_LIST_RE.fullmatch(line)
            if match is None:
                raise BriefMarkerRefusal(
                    f"fact list item at line {index + 1} must use '- [<fact-id>] <text>'")
            if not match.group(2).strip():
                raise BriefMarkerRefusal(f"fact text must be non-empty at line {index + 1}")
            marker_bytes = match.group(1)
            if _FACT_ID_RE.fullmatch(marker_bytes) is None:
                raise BriefMarkerRefusal(f"malformed fact marker at line {index + 1}")
            marker = marker_bytes.decode("ascii")
            actual_prefix = marker.partition("/")[0]
            if actual_prefix != prefix:
                raise BriefMarkerRefusal(
                    f"fact marker {marker!r} does not belong in section {section_name!r}")
            if marker in fact_ids:
                raise BriefDuplicateRefusal(f"duplicate fact marker {marker!r}")
            fact_ids.add(marker)
            marker_start = line.find(b"[")
            allowed_markers.add((index, marker_start))
            end_index = _fact_end(lines, index, section_end)
            raw = b"".join(lines[index:end_index])
            span = SourceSpan(
                offsets[index], offsets[index] + len(raw), index + 1, end_index)
            binding = (
                "binding"
                if declared_status == "committed" and prefix in {"commitment", "prototype", "non-goal"}
                else "nonbinding"
            )
            facts.append(ProjectFact(marker, prefix, binding, raw, span, _digest(raw)))
            index = end_index

    # Scan the entire document after parsing list positions. A marker-looking token is never
    # ignored merely because it appears in narrative, the wrong section, or malformed syntax.
    for index, line in enumerate(lines):
        for candidate in _MARKER_PREFIX_RE.finditer(line):
            if (index, candidate.start()) not in allowed_markers:
                raise BriefMarkerRefusal(
                    f"fact-marker syntax appears outside a canonical fact position at line {index + 1}")
            closing = line.find(b"]", candidate.start())
            if closing < 0:
                raise BriefMarkerRefusal(f"unterminated fact marker at line {index + 1}")

    status_span = front_spans["status"]
    effective_status = "superseded" if superseded else declared_status
    status_ref = FrameStatusRef(
        commit=commit,
        path=path,
        status=effective_status,
        digest=_digest(content[status_span.start_byte:status_span.end_byte]),
        source_span=status_span,
    )
    return ProjectFrame(
        status=effective_status,
        declared_status=declared_status,
        status_ref=status_ref,
        facts=tuple(facts),
        schema=schema,
        path=path,
        commit=commit,
        raw_bytes=content,
    )


def configured_brief_path(root: Path) -> tuple[str, Path]:
    configured = load_config(Path(root))["brief"]
    return configured, Path(root) / configured


def _regular_input_bytes(path: Path, label: str) -> bytes:
    try:
        info = path.lstat()
    except OSError as error:
        raise BriefReadError(f"cannot inspect {label} {path}: {error}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise BriefReadError(f"{label} must be a regular non-symlink file: {path}")
    try:
        return path.read_bytes()
    except OSError as error:
        raise BriefReadError(f"cannot read {label} {path}: {error}") from error


def read_project_frame(root: Path) -> ProjectFrame:
    root = Path(root).resolve()
    require_initialized_root(root)
    relative, source = configured_brief_path(root)
    return parse_project_brief(_regular_input_bytes(source, "project brief"), path=relative)


def read_project_frame_at_commit(
    root: Path, commit: str, *, current_commit: str | None = None
) -> ProjectFrame:
    root = Path(root).resolve()
    require_initialized_root(root)
    if _FULL_COMMIT_RE.fullmatch(commit) is None:
        raise BriefReadError("commit must be a full lowercase Git object id")
    relative, _ = configured_brief_path(root)
    try:
        process = subprocess.run(
            ["git", "show", f"{commit}:{relative}"],
            cwd=root,
            check=False,
            env=build_git_environment(
                overrides={"GIT_OPTIONAL_LOCKS": "0"}),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise BriefReadError(f"cannot read project brief from Git: {error}") from error
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", "replace").strip()
        raise BriefReadError(f"cannot read {relative} at {commit}: {detail}")
    superseded = False
    if current_commit is not None and commit != current_commit:
        if _FULL_COMMIT_RE.fullmatch(current_commit) is None:
            raise BriefReadError("current_commit must be a full lowercase Git object id")
        ancestry = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, current_commit],
            cwd=root,
            check=False,
            env=build_git_environment(
                overrides={"GIT_OPTIONAL_LOCKS": "0"}),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if ancestry.returncode == 0:
            superseded = True
        elif ancestry.returncode == 1:
            raise BriefReadError(
                f"{commit} is not an ancestor of current brief commit {current_commit}")
        else:
            detail = ancestry.stderr.decode("utf-8", "replace").strip()
            raise BriefReadError(f"cannot derive superseded status from Git ancestry: {detail}")
    return parse_project_brief(
        process.stdout, path=relative, commit=commit, superseded=superseded)


def check_project_brief(root: Path) -> ProjectFrame:
    return read_project_frame(root)


def show_project_brief(root: Path, fact_id: str | None = None) -> dict[str, Any]:
    return read_project_frame(root).to_dict(fact_id)


def adopt_project_brief(
    root: Path,
    owner_evidence_bytes: bytes,
    *,
    declared_evidence_digest: str | None = None,
) -> BriefAdoption:
    """Preserve exact owner evidence and bind it before changing provisional status."""
    if not isinstance(owner_evidence_bytes, bytes) or not owner_evidence_bytes:
        raise BriefAdoptionRefusal("owner adoption evidence must contain exact non-empty bytes")
    root = Path(root).resolve()
    require_initialized_root(root)
    with hold_project_lock(root):
        frame = read_project_frame(root)
        if frame.status != "provisional":
            raise BriefAdoptionRefusal("only a provisional current brief can be adopted")
        store = ArtifactStore(root)
        evidence = store.write(owner_evidence_bytes)
        if declared_evidence_digest is not None:
            try:
                expected = validate_sha256_digest(declared_evidence_digest)
            except ValueError as error:
                raise BriefAdoptionRefusal(str(error)) from error
            if evidence.digest != expected:
                raise BriefAdoptionRefusal(
                    f"owner evidence digest mismatch: declared {expected}, observed {evidence.digest}")

        status_span = frame.status_ref.source_span
        committed_bytes = (
            frame.raw_bytes[:status_span.start_byte]
            + b"committed"
            + frame.raw_bytes[status_span.end_byte:]
        )
        committed_frame = parse_project_brief(committed_bytes, path=frame.path)
        record_bytes = _canonical_json({
            "schema": "waystone-brief-adoption-1",
            "brief_path": frame.path,
            "before_digest": _digest(frame.raw_bytes),
            "after_digest": _digest(committed_bytes),
            "owner_evidence": {
                "reference_id": "brief-owner-evidence:" + evidence.digest.removeprefix("sha256:"),
                "digest": evidence.digest,
                "size": evidence.size,
            },
        })
        adoption_record = store.write(record_bytes)
        _, brief_path = configured_brief_path(root)
        write_bytes_atomic(brief_path, committed_bytes)
        return BriefAdoption(committed_frame, evidence, adoption_record)


def adopt_project_brief_from_file(
    root: Path, evidence_path: Path, *, declared_evidence_digest: str | None = None
) -> BriefAdoption:
    evidence = _regular_input_bytes(Path(evidence_path), "owner adoption evidence")
    return adopt_project_brief(
        root, evidence, declared_evidence_digest=declared_evidence_digest)


__all__ = [
    "BriefAdoption",
    "BriefAdoptionRefusal",
    "BriefDuplicateRefusal",
    "BriefMarkerRefusal",
    "BriefReadError",
    "BriefSchemaRefusal",
    "BriefStatusRefusal",
    "FrameStatusRef",
    "ProjectFact",
    "ProjectFactRef",
    "ProjectFrame",
    "PROJECT_BRIEF_SCHEMA",
    "SourceSpan",
    "adopt_project_brief",
    "adopt_project_brief_from_file",
    "check_project_brief",
    "configured_brief_path",
    "parse_project_brief",
    "read_project_frame",
    "read_project_frame_at_commit",
    "show_project_brief",
]
