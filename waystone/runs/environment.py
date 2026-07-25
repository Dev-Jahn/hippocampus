"""Frozen child environments for candidate-bound runner processes."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from waystone.core import WorkflowError
from waystone.runs.artifacts import validate_sha256_digest


_INHERITED_NAMES = frozenset({
    "HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "LANG",
    "NO_PROXY",
    "PATH",
    "SHELL",
    "TERM",
    "TMPDIR",
    "USER",
    "UV_CACHE_DIR",
    "http_proxy",
    "https_proxy",
    "no_proxy",
})
_EXECUTION_DESCRIPTOR_SCHEMA = "waystone-runner-execution-descriptor-1"


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


class RunnerExecutionDescriptorRefusal(WorkflowError):
    """A current runner environment or executable differs from frozen authority."""

    code = "runner_execution_descriptor_refused"

    def __init__(self, axis: str, detail: str):
        self.axis = _nonempty(axis, "execution descriptor refusal axis")
        self.detail = _nonempty(detail, "execution descriptor refusal detail")
        super().__init__(f"{self.code}: {self.axis}: {self.detail}")


@dataclass(frozen=True)
class RunnerEnvironment:
    """One immutable, deterministically digested child environment."""

    values: Mapping[str, str]

    def __post_init__(self) -> None:
        normalized: dict[str, str] = {}
        for name, value in self.values.items():
            if (not isinstance(name, str) or not name or "=" in name or "\0" in name
                    or not isinstance(value, str) or "\0" in value):
                raise ValueError("runner environment entries must be valid strings")
            normalized[name] = value
        object.__setattr__(
            self,
            "values",
            MappingProxyType(dict(sorted(normalized.items()))),
        )

    @property
    def digest(self) -> str:
        content = "\0".join(
            f"{name}={value}" for name, value in self.values.items()
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(content).hexdigest()

    def as_dict(self) -> dict[str, str]:
        return dict(self.values)


def build_runner_environment(
        source: Mapping[str, str] | None = None) -> RunnerEnvironment:
    """Select the complete child environment from an explicit allowlist."""
    ambient = os.environ if source is None else source
    if not isinstance(ambient, Mapping):
        raise TypeError("runner environment source must be a mapping")
    return RunnerEnvironment({
        name: value
        for name, value in ambient.items()
        if name in _INHERITED_NAMES or name.startswith("LC_")
    })


@dataclass(frozen=True)
class RunnerExecutionDescriptor:
    """Secret-free start-time authority for one promotion verifier executable."""

    environment_digest: str
    resolved_executable: str
    executable_content_digest: str
    verifier_backend: str
    verifier_binding_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "environment_digest",
            validate_sha256_digest(self.environment_digest))
        executable = Path(_nonempty(
            self.resolved_executable, "resolved executable"))
        if (not executable.is_absolute()
                or str(executable) != os.path.normpath(str(executable))):
            raise ValueError(
                "resolved executable must be a canonical absolute path")
        object.__setattr__(self, "resolved_executable", str(executable))
        object.__setattr__(
            self, "executable_content_digest",
            validate_sha256_digest(self.executable_content_digest))
        _nonempty(self.verifier_backend, "verifier backend")
        object.__setattr__(
            self, "verifier_binding_digest",
            validate_sha256_digest(self.verifier_binding_digest))

    def canonical_bytes(self) -> bytes:
        return _canonical_json({
            "environment_digest": self.environment_digest,
            "executable_content_digest": self.executable_content_digest,
            "resolved_executable": self.resolved_executable,
            "schema": _EXECUTION_DESCRIPTOR_SCHEMA,
            "verifier_backend": self.verifier_backend,
            "verifier_binding_digest": self.verifier_binding_digest,
        })

    @property
    def digest(self) -> str:
        return _digest(self.canonical_bytes())


def observe_runner_executable(
        executable: str, *, cwd: Path,
        environment: RunnerEnvironment) -> tuple[str, str]:
    """Resolve and hash one executable, refusing unstable or non-executable bytes."""
    value = _nonempty(executable, "runner executable")
    if not isinstance(environment, RunnerEnvironment):
        raise TypeError("environment must be a RunnerEnvironment")
    root = Path(cwd)
    if os.sep in value or (os.altsep is not None and os.altsep in value):
        supplied = Path(value)
        candidate = supplied if supplied.is_absolute() else root / supplied
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise RunnerExecutionDescriptorRefusal(
                "executable path", f"cannot resolve {value!r}: {error}") from error
    else:
        found = shutil.which(value, path=environment.values.get("PATH", ""))
        if found is None:
            raise RunnerExecutionDescriptorRefusal(
                "executable path", f"{value!r} is unavailable on frozen PATH")
        try:
            resolved = Path(found).resolve(strict=True)
        except OSError as error:
            raise RunnerExecutionDescriptorRefusal(
                "executable path", f"cannot resolve {found!r}: {error}") from error
    try:
        before = resolved.stat()
        if not stat.S_ISREG(before.st_mode) or not os.access(resolved, os.X_OK):
            raise OSError("resolved path is not an executable regular file")
        content = resolved.read_bytes()
        after = resolved.stat()
    except OSError as error:
        raise RunnerExecutionDescriptorRefusal(
            "executable content", f"cannot read stable executable bytes: {error}") from error
    before_identity = (
        before.st_dev, before.st_ino, before.st_size,
        before.st_mtime_ns, before.st_mode)
    after_identity = (
        after.st_dev, after.st_ino, after.st_size,
        after.st_mtime_ns, after.st_mode)
    if before_identity != after_identity:
        raise RunnerExecutionDescriptorRefusal(
            "executable content", "executable bytes changed while being hashed")
    return str(resolved), _digest(content)


def freeze_runner_execution_descriptor(
        environment: RunnerEnvironment, *, executable: str, cwd: Path,
        verifier_backend: str,
        verifier_binding_digest: str) -> RunnerExecutionDescriptor:
    """Freeze a secret-free descriptor; raw environment values are never serialized."""
    if not isinstance(environment, RunnerEnvironment):
        raise TypeError("environment must be a RunnerEnvironment")
    resolved, content_digest = observe_runner_executable(
        executable, cwd=Path(cwd), environment=environment)
    return RunnerExecutionDescriptor(
        environment_digest=environment.digest,
        resolved_executable=resolved,
        executable_content_digest=content_digest,
        verifier_backend=verifier_backend,
        verifier_binding_digest=verifier_binding_digest,
    )


def parse_runner_execution_descriptor(
        content: bytes, *, expected_digest: str) -> RunnerExecutionDescriptor:
    """Parse canonical descriptor bytes and require their CAS digest."""
    try:
        expected = validate_sha256_digest(expected_digest)
        decoded = json.loads(content.decode("utf-8"))
        fields = {
            "environment_digest", "executable_content_digest",
            "resolved_executable", "schema", "verifier_backend",
            "verifier_binding_digest",
        }
        if not isinstance(decoded, dict) or set(decoded) != fields:
            raise ValueError("execution descriptor fields are not canonical")
        if decoded["schema"] != _EXECUTION_DESCRIPTOR_SCHEMA:
            raise ValueError("execution descriptor schema is invalid")
        descriptor = RunnerExecutionDescriptor(
            environment_digest=decoded["environment_digest"],
            resolved_executable=decoded["resolved_executable"],
            executable_content_digest=decoded["executable_content_digest"],
            verifier_backend=decoded["verifier_backend"],
            verifier_binding_digest=decoded["verifier_binding_digest"],
        )
        if content != descriptor.canonical_bytes() or descriptor.digest != expected:
            raise ValueError(
                "execution descriptor bytes are not canonical or digest-bound")
        return descriptor
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise RunnerExecutionDescriptorRefusal(
            "descriptor artifact", str(error)) from error


def require_runner_execution_match(
        descriptor: RunnerExecutionDescriptor,
        environment: RunnerEnvironment, *, executable: str, cwd: Path,
        verifier_backend: str,
        verifier_binding_digest: str) -> None:
    """Require current environment, path, and bytes to exactly match start.

    This detects drift through the final pre-spawn observation. It does not
    atomically pin a pathname across the remaining observation-to-exec window.
    """
    if not isinstance(descriptor, RunnerExecutionDescriptor):
        raise TypeError("descriptor must be a RunnerExecutionDescriptor")
    if not isinstance(environment, RunnerEnvironment):
        raise TypeError("environment must be a RunnerEnvironment")
    if descriptor.environment_digest != environment.digest:
        raise RunnerExecutionDescriptorRefusal(
            "environment digest",
            f"expected {descriptor.environment_digest}, observed {environment.digest}",
        )
    if (descriptor.verifier_backend != verifier_backend
            or descriptor.verifier_binding_digest != verifier_binding_digest):
        raise RunnerExecutionDescriptorRefusal(
            "verifier binding",
            "current backend/model binding differs from the start-time descriptor",
        )
    resolved, content_digest = observe_runner_executable(
        executable, cwd=Path(cwd), environment=environment)
    if descriptor.resolved_executable != resolved:
        raise RunnerExecutionDescriptorRefusal(
            "executable path",
            f"expected {descriptor.resolved_executable!r}, observed {resolved!r}",
        )
    if descriptor.executable_content_digest != content_digest:
        raise RunnerExecutionDescriptorRefusal(
            "executable content",
            f"expected {descriptor.executable_content_digest}, observed {content_digest}",
        )


__all__ = [
    "RunnerEnvironment",
    "RunnerExecutionDescriptor",
    "RunnerExecutionDescriptorRefusal",
    "build_runner_environment",
    "freeze_runner_execution_descriptor",
    "observe_runner_executable",
    "parse_runner_execution_descriptor",
    "require_runner_execution_match",
]
