from __future__ import annotations

import hashlib
import importlib.util
import json
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts/build_mcp_package.py"
SPEC = importlib.util.spec_from_file_location("build_mcp_package", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
build_mcp_package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_mcp_package)


def write_manifest(root: Path, *, source: str = "connector.py") -> Path:
    manifest = {
        "schema_version": 1,
        "kind": "hermes-mcp-package-source",
        "name": "example",
        "version": "1.2.3",
        "platform": "linux-amd64",
        "server": {
            "command": "/opt/hermes/.venv/bin/python",
            "args": ["-s", "${PACKAGE_ROOT}/lib/connector.py"],
        },
        "tools": ["read_item"],
        "required_env": ["EXAMPLE_TOKEN"],
        "egress_hosts": ["api.example.com"],
        "build": {
            "files": [
                {
                    "source": source,
                    "target": "lib/connector.py",
                    "mode": "0644",
                }
            ]
        },
    }
    path = root / "package.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_accepts_an_executable_command_from_the_package(tmp_path: Path) -> None:
    executable = tmp_path / "connector"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    manifest = json.loads(write_manifest(tmp_path).read_text(encoding="utf-8"))
    manifest["server"] = {
        "command": "${PACKAGE_ROOT}/bin/connector",
        "args": [],
    }
    manifest["build"]["files"][0] = {
        "source": "connector",
        "target": "bin/connector",
        "mode": "0755",
    }
    manifest_path = tmp_path / "package.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    build_mcp_package.build_package(
        manifest_path,
        tmp_path / "package.tar.gz",
        source_root=tmp_path,
    )


def test_rejects_a_non_executable_package_command(tmp_path: Path) -> None:
    executable = tmp_path / "connector"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    manifest = json.loads(write_manifest(tmp_path).read_text(encoding="utf-8"))
    manifest["server"] = {
        "command": "${PACKAGE_ROOT}/bin/connector",
        "args": [],
    }
    manifest["build"]["files"][0] = {
        "source": "connector",
        "target": "bin/connector",
        "mode": "0644",
    }
    manifest_path = tmp_path / "package.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(
        build_mcp_package.PackageBuildError,
        match="executable payload",
    ):
        build_mcp_package.build_package(
            manifest_path,
            tmp_path / "package.tar.gz",
            source_root=tmp_path,
        )


def test_build_is_deterministic_and_hashes_the_payload(tmp_path: Path) -> None:
    (tmp_path / "connector.py").write_text("print('ready')\n", encoding="utf-8")
    manifest = write_manifest(tmp_path)
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"

    result = build_mcp_package.build_package(
        manifest,
        first,
        source_root=tmp_path,
    )
    build_mcp_package.build_package(
        manifest,
        second,
        source_root=tmp_path,
    )

    assert first.read_bytes() == second.read_bytes()
    assert result["package_sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()
    with tarfile.open(first, "r:gz") as archive:
        assert archive.getnames() == ["manifest.json", "payload/lib/connector.py"]
        package_manifest = json.load(archive.extractfile("manifest.json"))
        content = archive.extractfile("payload/lib/connector.py").read()
    assert package_manifest["kind"] == "hermes-mcp-package"
    assert package_manifest["payload"]["lib/connector.py"] == {
        "bytes": len(content),
        "mode": "0644",
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    assert "actual-secret-value" not in first.read_bytes().decode("latin-1")


@pytest.mark.parametrize("source", ["../connector.py", "/tmp/connector.py"])
def test_rejects_source_path_escape(tmp_path: Path, source: str) -> None:
    (tmp_path / "connector.py").write_text("print('ready')\n", encoding="utf-8")
    manifest = write_manifest(tmp_path, source=source)

    with pytest.raises(build_mcp_package.PackageBuildError, match="unsafe"):
        build_mcp_package.build_package(
            manifest,
            tmp_path / "package.tar.gz",
            source_root=tmp_path,
        )


def test_rejects_symlinked_source(tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("print('secret')\n", encoding="utf-8")
    (tmp_path / "connector.py").symlink_to(outside)
    manifest = write_manifest(tmp_path)

    with pytest.raises(
        build_mcp_package.PackageBuildError,
        match="regular file",
    ):
        build_mcp_package.build_package(
            manifest,
            tmp_path / "package.tar.gz",
            source_root=tmp_path,
        )


def test_rejects_a_symlinked_source_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "connector.py").write_text("print('secret')\n", encoding="utf-8")
    (tmp_path / "linked").symlink_to(outside)
    manifest = write_manifest(tmp_path, source="linked/connector.py")

    with pytest.raises(
        build_mcp_package.PackageBuildError,
        match="parent is unsafe",
    ):
        build_mcp_package.build_package(
            manifest,
            tmp_path / "package.tar.gz",
            source_root=tmp_path,
        )


def test_zoom_package_contains_its_connector(tmp_path: Path) -> None:
    output = tmp_path / "zoom.tar.gz"
    build_mcp_package.build_package(
        REPO_ROOT / "mcp-packages/zoom/package.json",
        output,
        source_root=REPO_ROOT,
    )

    with tarfile.open(output, "r:gz") as archive:
        names = archive.getnames()
        package_manifest = json.load(archive.extractfile("manifest.json"))

    assert names == [
        "manifest.json",
        "payload/bin/hermes-zoom-mcp",
        "payload/lib/zoom_mcp.py",
    ]
    assert package_manifest["name"] == "zoom"
    assert package_manifest["server"] == {
        "command": "${PACKAGE_ROOT}/bin/hermes-zoom-mcp",
        "args": [],
    }
    assert package_manifest["payload"]["bin/hermes-zoom-mcp"]["mode"] == "0755"
    assert package_manifest["payload"]["lib/zoom_mcp.py"]["mode"] == "0644"
    assert package_manifest["tools"] == [
        "meetings_list",
        "meetings_get",
        "recordings_list",
        "recordings_get",
        "recordings_transcript",
    ]
