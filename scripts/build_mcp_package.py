#!/usr/bin/env python3
"""Build a deterministic, digest-addressed Hermes MCP package."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import pathlib
import re
import stat
import tarfile
from typing import Any

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){2}(?:[+.-][A-Za-z0-9.-]+)?$")
ENV_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
HOST_PATTERN = re.compile(
    r"^(?:\*\.)?[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$"
)
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
PACKAGE_ROOT_TOKEN = "${PACKAGE_ROOT}"
RUNTIME_COMMAND_PATTERN = re.compile(
    r"^/opt/hermes/\.venv/bin/[a-z][a-z0-9-]{0,63}$"
)


class PackageBuildError(ValueError):
    """The package source contract is invalid."""


def _mapping(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise PackageBuildError(f"{label} fields are invalid")
    return value


def _string_list(value: Any, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
        or len(set(value)) != len(value)
    ):
        raise PackageBuildError(f"{label} must be a unique non-empty string list")
    return value


def _safe_relative_path(value: Any, label: str) -> pathlib.PurePosixPath:
    if not isinstance(value, str):
        raise PackageBuildError(f"{label} must be a string")
    path = pathlib.PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PackageBuildError(f"{label} is unsafe")
    return path


def _read_source(
    source_root: pathlib.Path,
    relative: pathlib.PurePosixPath,
) -> bytes:
    path = source_root
    metadata: os.stat_result | None = None
    for index, part in enumerate(relative.parts):
        path /= part
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise PackageBuildError(
                f"package source is unavailable: {relative}"
            ) from exc
        if index < len(relative.parts) - 1 and (
            not stat.S_ISDIR(metadata.st_mode) or path.is_symlink()
        ):
            raise PackageBuildError(
                f"package source parent is unsafe: {relative}"
            )
    assert metadata is not None
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise PackageBuildError(f"package source must be a regular file: {relative}")
    if metadata.st_size > MAX_FILE_BYTES:
        raise PackageBuildError(f"package source is too large: {relative}")
    return path.read_bytes()


def _parse_mode(value: Any) -> int:
    if not isinstance(value, str) or re.fullmatch(r"0[67][0-7]{2}", value) is None:
        raise PackageBuildError("package file mode is invalid")
    mode = int(value, 8)
    if mode & 0o022:
        raise PackageBuildError("package files must not be group or world writable")
    return mode


def _package_command_target(command: str) -> pathlib.PurePosixPath | None:
    prefix = f"{PACKAGE_ROOT_TOKEN}/"
    if not command.startswith(prefix):
        return None
    return _safe_relative_path(command.removeprefix(prefix), "package command")


def build_package(
    source_manifest: pathlib.Path,
    output: pathlib.Path,
    *,
    source_root: pathlib.Path,
) -> dict[str, str]:
    try:
        source = json.loads(source_manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackageBuildError("package source manifest is unreadable") from exc
    source = _mapping(
        source,
        {
            "schema_version",
            "kind",
            "name",
            "version",
            "platform",
            "server",
            "tools",
            "required_env",
            "egress_hosts",
            "build",
        },
        "package source manifest",
    )
    if (
        source["schema_version"] != 1
        or source["kind"] != "hermes-mcp-package-source"
        or not isinstance(source["name"], str)
        or NAME_PATTERN.fullmatch(source["name"]) is None
        or not isinstance(source["version"], str)
        or VERSION_PATTERN.fullmatch(source["version"]) is None
        or source["platform"] != "linux-amd64"
    ):
        raise PackageBuildError("package identity is invalid")

    server = _mapping(source["server"], {"command", "args"}, "package server")
    if (
        not isinstance(server["command"], str)
        or not isinstance(server["args"], list)
        or not all(isinstance(argument, str) for argument in server["args"])
        or (
            server["command"] != "/opt/hermes/.venv/bin/python"
            and not server["command"].startswith(f"{PACKAGE_ROOT_TOKEN}/")
            and RUNTIME_COMMAND_PATTERN.fullmatch(server["command"]) is None
        )
        or (
            server["command"] == "/opt/hermes/.venv/bin/python"
            and not any(PACKAGE_ROOT_TOKEN in value for value in server["args"])
        )
    ):
        raise PackageBuildError("package server command is invalid")

    tools = _string_list(source["tools"], "package tools")
    required_env = _string_list(source["required_env"], "package required_env")
    if any(ENV_PATTERN.fullmatch(name) is None for name in required_env):
        raise PackageBuildError("package required_env contains an invalid name")
    egress_hosts = _string_list(source["egress_hosts"], "package egress_hosts")
    if any(HOST_PATTERN.fullmatch(host) is None for host in egress_hosts):
        raise PackageBuildError("package egress_hosts contains an invalid host")

    build = _mapping(source["build"], {"files"}, "package build")
    files = build["files"]
    if not isinstance(files, list):
        raise PackageBuildError("package build files must be a list")

    payload: dict[str, tuple[bytes, int]] = {}
    total_bytes = 0
    for raw_file in files:
        file_spec = _mapping(
            raw_file,
            {"source", "target", "mode"},
            "package file",
        )
        source_path = _safe_relative_path(file_spec["source"], "package source")
        target_path = _safe_relative_path(file_spec["target"], "package target")
        target = target_path.as_posix()
        if target in payload:
            raise PackageBuildError(f"duplicate package target: {target}")
        content = _read_source(source_root, source_path)
        total_bytes += len(content)
        if total_bytes > MAX_PACKAGE_BYTES:
            raise PackageBuildError("package payload is too large")
        payload[target] = (content, _parse_mode(file_spec["mode"]))

    command_target = _package_command_target(server["command"])
    if command_target is not None:
        command_entry = payload.get(command_target.as_posix())
        if command_entry is None or command_entry[1] & 0o111 == 0:
            raise PackageBuildError(
                "package command must reference an executable payload file"
            )

    runtime_manifest = {
        "schema_version": 1,
        "kind": "hermes-mcp-package",
        "name": source["name"],
        "version": source["version"],
        "platform": source["platform"],
        "server": server,
        "tools": tools,
        "required_env": required_env,
        "egress_hosts": egress_hosts,
        "payload": {
            target: {
                "bytes": len(content),
                "mode": f"{mode:04o}",
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for target, (content, mode) in sorted(payload.items())
        },
    }
    manifest_bytes = (
        json.dumps(runtime_manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as zipped:
                with tarfile.open(
                    fileobj=zipped,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                ) as archive:
                    entries = {"manifest.json": (manifest_bytes, 0o644)}
                    entries.update(
                        {
                            f"payload/{target}": value
                            for target, value in payload.items()
                        }
                    )
                    for name, (content, mode) in sorted(entries.items()):
                        info = tarfile.TarInfo(name)
                        info.size = len(content)
                        info.mode = mode
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.mtime = 0
                        archive.addfile(info, io.BytesIO(content))
            raw.flush()
            os.fsync(raw.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()

    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return {
        "name": source["name"],
        "version": source["version"],
        "package_sha256": digest,
        "path": str(output),
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="build_mcp_package.py")
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--source-root", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        result = build_package(
            args.manifest,
            args.output,
            source_root=args.source_root,
        )
    except PackageBuildError as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
