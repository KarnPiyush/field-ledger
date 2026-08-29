from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import re
import statistics
import subprocess
import sys
import tomllib
import zipfile
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from fieldledger.domain.models import SourceKind
from fieldledger.parsers.docx import DocxParser
from fieldledger.parsers.pdf import PdfParser
from fieldledger.parsers.text import TextParser

ROOT = Path(__file__).resolve().parents[4]
DELIVERY = ROOT / "scripts" / "delivery.py"
DELIVERY_SPEC = importlib.util.spec_from_file_location("fieldledger_delivery", DELIVERY)
assert DELIVERY_SPEC is not None and DELIVERY_SPEC.loader is not None
delivery = importlib.util.module_from_spec(DELIVERY_SPEC)
sys.modules[DELIVERY_SPEC.name] = delivery
DELIVERY_SPEC.loader.exec_module(delivery)

REQUIRED_PATHS = (
    "README.md",
    "TASK.md",
    "PROGRESS.md",
    "VERSION",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "docs/user-flow.md",
    "docs/architecture.md",
    "docs/api-reference.md",
    "docs/mcp.md",
    "docs/operations.md",
    "docs/word-sideload.md",
    "docs/submission/one-page-writeup.md",
    "docs/submission/required-answers.md",
    "docs/submission/use-cases.md",
    "docs/submission/measurement.md",
    "docs/submission/limitations.md",
    "docs/submission/requirement-matrix.md",
    "docs/demo/narration-script.md",
    "demo/fieldledger-superdocs-demo.mp4",
    "demo/fixtures/oracle.json",
    "demo/measurements/workflow-raw.csv",
    "demo/measurements/workflow-summary.json",
)

SCRIPT_STEMS = (
    "demo",
    "doctor",
    "verify",
    "reset",
    "measure-setup",
    "mcp-inspector",
    "package",
)


def run_delivery(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DELIVERY), *arguments],
        cwd=ROOT,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def markdown_links(path: Path) -> list[str]:
    content = path.read_text(encoding="utf-8")
    return re.findall(r"(?<!!)\[[^\]]+\]\(([^)]+)\)", content)


def percentile_nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = max(1, int(percentile * len(ordered) + 0.999999999))
    return ordered[min(rank, len(ordered)) - 1]


def parse_fixture(name: str) -> tuple[object, ...]:
    path = ROOT / "demo" / "fixtures" / name
    content = path.read_bytes()
    if path.suffix == ".pdf":
        return PdfParser().parse(content)
    if path.suffix == ".docx":
        return DocxParser().parse(content)
    kind = SourceKind.MARKDOWN if path.suffix == ".md" else SourceKind.TEXT
    return TextParser(kind).parse(content)


def test_required_delivery_surface_exists() -> None:
    missing = [path for path in REQUIRED_PATHS if not (ROOT / path).is_file()]
    for stem in SCRIPT_STEMS:
        for suffix in ("ps1", "sh"):
            path = ROOT / "scripts" / f"{stem}.{suffix}"
            if not path.is_file():
                missing.append(path.relative_to(ROOT).as_posix())
    assert missing == []


def test_tracked_handoff_matches_the_completed_consolidated_review() -> None:
    progress = (ROOT / "PROGRESS.md").read_text(encoding="utf-8")
    user_flow = (ROOT / "docs" / "user-flow.md").read_text(encoding="utf-8")

    assert "| Consolidated review and final ZIP | Complete |" in progress
    assert "one ordered five-decision command" in user_flow
    assert "Save commands 1 and 2 locally" not in user_flow


def test_operator_wrappers_share_one_help_contract() -> None:
    for stem in SCRIPT_STEMS:
        result = run_delivery(stem, "--help")
        assert result.returncode == 0
        assert f"FieldLedger {stem}" in result.stdout


def test_verifier_builds_shared_declarations_before_workspace_lint() -> None:
    commands = delivery.verify_commands(fast=True)
    lint_index = commands.index(("pnpm", "lint"))

    for package in ("@fieldledger/api-client", "@fieldledger/offline-queue"):
        build = ("pnpm", "--filter", package, "build")
        assert build in commands
        assert commands.index(build) < lint_index


def test_development_dependencies_include_the_mcp_cli() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert any(
        requirement.startswith("mcp[cli]")
        for requirement in project["dependency-groups"]["dev"]
    )


def test_mcp_inspector_targets_an_explicit_fastmcp_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(delivery, "run", lambda command: commands.append(command))

    delivery.mcp_inspector_command(SimpleNamespace(smoke=False))

    assert commands == [
        (
            "uv",
            "run",
            "mcp",
            "dev",
            "apps/api/src/fieldledger/mcp_inspector.py:mcp",
        )
    ]


def test_mcp_inspector_entrypoint_exposes_fastmcp_object() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from fieldledger.mcp_inspector import mcp; "
            "from mcp.server.fastmcp import FastMCP; "
            "assert isinstance(mcp, FastMCP)",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_web_images_bind_the_declared_compose_ports() -> None:
    expected_commands = {
        ROOT / "apps" / "mobile" / "Dockerfile": (
            'CMD ["pnpm", "--dir", "apps/mobile", "exec", "vite", "preview", '
            '"--host", "0.0.0.0", "--port", "4173"]'
        ),
        ROOT / "extensions" / "word" / "Dockerfile": (
            'CMD ["pnpm", "--dir", "extensions/word", "exec", "vite", "preview", '
            '"--host", "0.0.0.0", "--port", "4174"]'
        ),
    }
    for dockerfile, command in expected_commands.items():
        assert command in dockerfile.read_text(encoding="utf-8")


def test_reset_refuses_to_delete_without_exact_confirmation(tmp_path: Path) -> None:
    state = tmp_path / "synthetic-state"
    state.mkdir()
    marker = state / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    result = run_delivery("reset", "--state-root", str(state), check=False)
    assert result.returncode != 0
    assert "--confirm RESET-SYNTHETIC-DATA" in result.stderr
    assert marker.read_text(encoding="utf-8") == "keep"


def test_reset_refuses_repository_source_even_with_exact_confirmation() -> None:
    with pytest.raises(delivery.DeliveryError, match="synthetic state root"):
        delivery.validated_reset_targets(ROOT / "apps")


def test_reset_requires_a_synthetic_state_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(delivery, "ROOT", tmp_path)
    state = tmp_path / ".data"
    state.mkdir()
    keep = state / "keep.txt"
    keep.write_text("keep", encoding="utf-8")

    with pytest.raises(delivery.DeliveryError, match="marker"):
        delivery.reset_command(
            argparse.Namespace(confirm=delivery.RESET_PHRASE, state_root=state)
        )

    assert keep.read_text(encoding="utf-8") == "keep"


def test_reset_refuses_a_redirected_synthetic_state_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (repository / ".data").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not available on this host")
    monkeypatch.setattr(delivery, "ROOT", repository)

    with pytest.raises(delivery.DeliveryError, match="redirected synthetic state root"):
        delivery.validated_reset_targets(None)


def test_reset_refuses_a_synthetic_state_link_to_repository_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "apps"
    source.mkdir()
    try:
        (repository / ".data").symlink_to(source, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not available on this host")
    monkeypatch.setattr(delivery, "ROOT", repository)

    with pytest.raises(delivery.DeliveryError, match="redirected synthetic state root"):
        delivery.validated_reset_targets(None)


def test_reset_preserves_state_when_compose_volume_deletion_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(delivery, "ROOT", tmp_path)
    state = tmp_path / ".data"
    state.mkdir()
    marker = state / delivery.STATE_MARKER
    marker.write_text("fieldledger synthetic state\n", encoding="utf-8")
    monkeypatch.setattr(delivery, "command_exists", lambda name: name == "docker")
    monkeypatch.setattr(
        delivery,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(["docker"], 1, "", "failed"),
    )

    with pytest.raises(delivery.DeliveryError, match="Docker volume"):
        delivery.reset_command(
            argparse.Namespace(confirm=delivery.RESET_PHRASE, state_root=None)
        )

    assert marker.is_file()


def test_reset_removes_an_empty_readonly_onedrive_state_root_after_rmtree_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(delivery, "ROOT", tmp_path)
    state = tmp_path / ".data"
    state.mkdir()
    (state / delivery.STATE_MARKER).write_text(
        "fieldledger synthetic state\n", encoding="utf-8"
    )
    monkeypatch.setattr(delivery, "command_exists", lambda _name: False)

    def empty_then_deny(path: Path) -> None:
        for child in path.iterdir():
            child.unlink()
        raise PermissionError("OneDrive retained the read-only state root")

    monkeypatch.setattr(delivery.shutil, "rmtree", empty_then_deny)

    delivery.reset_command(
        argparse.Namespace(confirm=delivery.RESET_PHRASE, state_root=None)
    )

    assert not state.exists()


def test_deterministic_environment_blanks_provider_key_and_generates_demo_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPERDOCS_API_KEY", "superdocs_" + "TEST_CANARY_123456789")
    environment = delivery.deterministic_compose_environment()

    assert environment["SUPERDOCS_API_KEY"] == ""
    assert environment["FIELDLEDGER_PROVIDER"] == "deterministic"
    assert environment["FIELDLEDGER_LOCAL_DEMO"] == "true"
    assert len(environment["FIELDLEDGER_SESSION_SIGNING_SECRET"]) >= 32
    assert len(environment["FIELDLEDGER_DEMO_BOOTSTRAP_SECRET"]) >= 32


def test_compose_uses_loopback_and_environment_ports_without_env_key_reinjection(
    tmp_path: Path,
) -> None:
    compose = tmp_path / "compose.yaml"
    compose.write_text((ROOT / "compose.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / ".env").write_text(
        "SUPERDOCS_API_KEY=superdocs_" + "FILE_CANARY_123456789\n", encoding="utf-8"
    )
    environment = delivery.deterministic_compose_environment()
    environment.update(
        {
            "POSTGRES_PORT": "55432",
            "FIELDLEDGER_API_PORT": "58000",
            "MOBILE_PORT": "54173",
            "WORD_PORT": "54174",
        }
    )
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose), "config", "--format", "json"],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    config = json.loads(result.stdout)

    expected_ports = {
        "db": 55432,
        "api": 58000,
        "mobile": 54173,
        "word": 54174,
    }
    for service, port in expected_ports.items():
        published = config["services"][service]["ports"][0]
        assert published["host_ip"] == "127.0.0.1"
        assert int(published["published"]) == port
    for service in ("api", "worker", "watcher"):
        assert config["services"][service]["environment"]["SUPERDOCS_API_KEY"] == ""
    for service in ("mobile", "word"):
        assert config["services"][service]["environment"]["FIELDLEDGER_API_TARGET"] == "http://api:8000"
        assert config["services"][service]["environment"]["FIELDLEDGER_DEMO_BOOTSTRAP_SECRET"]
    assert config["services"]["word"]["build"]["args"]["VITE_FIELDLEDGER_REVIEW_URL"] == (
        "http://127.0.0.1:54173"
    )
    assert "http://localhost:4174/taskpane.html" in config["services"]["word"]["healthcheck"][
        "test"
    ][-1]


def test_demo_migrates_before_starting_application_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(delivery, "ROOT", tmp_path)
    monkeypatch.setattr(delivery, "preflight", lambda: [])
    monkeypatch.setattr(delivery, "deterministic_compose_environment", lambda: {})
    monkeypatch.setattr(delivery, "wait_for_url", lambda _url: None)
    monkeypatch.setattr(
        delivery,
        "run",
        lambda command, **_kwargs: calls.append(tuple(command))
        or subprocess.CompletedProcess(command, 0, "", ""),
    )

    delivery.demo_command(argparse.Namespace())

    migrate_index = next(index for index, command in enumerate(calls) if "alembic" in command)
    full_up_index = next(
        index
        for index, command in enumerate(calls)
        if command[:3] == ("docker", "compose", "up") and not command[-1] == "db"
    )
    assert calls[migrate_index - 1][-1] == "db"
    assert migrate_index < full_up_index


def test_compose_project_is_scoped_by_the_checkout_directory() -> None:
    compose_source = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert not re.search(r"^name:\s*fieldledger\s*$", compose_source, re.MULTILINE)


def test_compose_database_image_is_pinned_by_digest() -> None:
    compose_source = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert re.search(
        r"image:\s*postgres:17-alpine@sha256:[0-9a-f]{64}", compose_source
    )


def test_delivery_ports_and_urls_follow_environment() -> None:
    ports = delivery.delivery_ports(
        {
            "POSTGRES_PORT": "55432",
            "FIELDLEDGER_API_PORT": "58000",
            "MOBILE_PORT": "54173",
            "WORD_PORT": "54174",
        }
    )

    assert ports == {"db": 55432, "api": 58000, "mobile": 54173, "word": 54174}
    assert delivery.delivery_urls(ports) == {
        "api": "http://127.0.0.1:58000",
        "mobile": "http://127.0.0.1:54173",
        "word": "http://127.0.0.1:54174",
    }


def test_preflight_checks_ports_for_services_missing_from_a_partial_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(delivery, "command_exists", lambda _name: True)
    monkeypatch.setattr(
        delivery,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(["docker"], 0, "", ""),
    )
    monkeypatch.setattr(delivery, "compose_service_names", lambda: {"db"})
    monkeypatch.setattr(delivery, "is_port_open", lambda port: port == 8000)

    checks = delivery.preflight()

    api = next(check for check in checks if check["name"] == "Api port 8000")
    assert api["status"] == "failed"
    assert all(check["name"] != "Db port 5432" for check in checks)


def test_package_dry_run_is_an_allowlist_without_private_or_generated_paths() -> None:
    result = run_delivery("package", "--dry-run", "--json")
    payload = json.loads(result.stdout)
    paths = payload["included_paths"]
    assert "README.md" in paths
    assert "demo/fieldledger-superdocs-demo.mp4" in paths
    forbidden_parts = {
        ".git",
        ".superpowers",
        ".venv",
        "node_modules",
        "dist",
        "coverage",
        "playwright-report",
        "test-results",
        ".data",
        "tmp",
    }
    assert all(not (set(Path(path).parts) & forbidden_parts) for path in paths)
    assert all(not Path(path).name.startswith(".env") or path == ".env.example" for path in paths)
    assert all(not path.lower().endswith(".zip") for path in paths)
    assert all(not path.startswith("docs/reviews/") for path in paths)
    assert all(not path.startswith("docs/superpowers/") for path in paths)


def test_package_uses_tracked_inventory_and_excludes_untracked_canaries(
    tmp_path: Path,
) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "README.md").write_text("tracked\n", encoding="utf-8")
    (tmp_path / "docs" / "kept.txt").write_text("tracked\n", encoding="utf-8")
    manifest = tmp_path / "scripts" / "delivery-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "top_level": ["README.md"],
                "trees": ["docs", "scripts"],
                "excluded_prefixes": [],
                "approved_binary_sha256": {},
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "add", "README.md", "docs/kept.txt", "scripts/delivery-manifest.json"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "docs" / "private-note.txt").write_text("untracked\n", encoding="utf-8")
    (tmp_path / "docs" / "private-screen.png").write_bytes(b"untracked media")

    paths = delivery.package_paths(root=tmp_path, manifest_path=manifest)
    names = [path.relative_to(tmp_path).as_posix() for path in paths]

    assert names == ["README.md", "docs/kept.txt", "scripts/delivery-manifest.json"]


def test_package_manifest_approves_every_binary_and_rejects_hash_drift() -> None:
    paths = delivery.package_paths()
    delivery.verify_approved_binary_hashes(paths)
    approved = delivery.load_delivery_manifest().approved_binary_sha256
    binary_names = {
        path.relative_to(ROOT).as_posix()
        for path in paths
        if path.suffix.lower() in delivery.BINARY_SUFFIXES
    }

    assert binary_names == set(approved)


def test_secret_scan_reads_raw_media_and_extracted_docx_content(tmp_path: Path) -> None:
    media = tmp_path / "screen.png"
    media.write_bytes(b"header superdocs_" + b"MEDIA_CANARY_123456789 trailer")
    document = tmp_path / "document.docx"
    with zipfile.ZipFile(document, "w") as archive:
        archive.writestr(
            "word/document.xml",
            "<w:t>superdocs_" + "DOCUMENT_CANARY_123456789</w:t>",
        )

    findings = delivery.secret_findings([media, document], root=tmp_path)

    assert any("screen.png" in finding for finding in findings)
    assert any("document.docx" in finding for finding in findings)


def test_package_inventory_contains_no_absolute_home_paths() -> None:
    findings = delivery.private_path_findings(delivery.package_paths())
    assert findings == []


def test_default_archive_is_outside_the_main_checkout() -> None:
    main_checkout = delivery.git_main_checkout()
    output = delivery.default_archive_output()
    checkout_boundary = main_checkout or delivery.ROOT.resolve()

    with pytest.raises(ValueError):
        output.relative_to(checkout_boundary)
    with pytest.raises(delivery.DeliveryError, match="outside every repository checkout"):
        delivery.validate_archive_output(checkout_boundary / "fieldledger.zip")


def test_atomic_archive_failure_preserves_an_existing_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "README.md"
    source.write_text("delivery\n", encoding="utf-8")
    output = tmp_path / "fieldledger.zip"
    output.write_bytes(b"previous valid archive")

    def fail_audit(_path: Path, _expected: list[Path], *, root: Path) -> dict[str, object]:
        raise delivery.DeliveryError(f"audit failed for {root}")

    monkeypatch.setattr(delivery, "audit_archive", fail_audit)
    with pytest.raises(delivery.DeliveryError, match="audit failed"):
        delivery.write_archive_atomic(output, [source], root=tmp_path)

    assert output.read_bytes() == b"previous valid archive"


def test_archive_audit_rejects_tampered_entry_content(tmp_path: Path) -> None:
    source = tmp_path / "README.md"
    source.write_text("expected delivery\n", encoding="utf-8")
    archive_path = tmp_path / "tampered.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("README.md", "different delivery\n")

    with pytest.raises(delivery.DeliveryError, match="content hash"):
        delivery.audit_archive(archive_path, [source], root=tmp_path)


def test_package_archive_matches_the_allowlist(tmp_path: Path) -> None:
    archive = tmp_path / "fieldledger-contract.zip"
    result = run_delivery("package", "--output", str(archive), "--json")
    payload = json.loads(result.stdout)
    assert payload["archive"] == str(archive)
    assert payload["entry_count"] == payload["entries"]
    assert len(payload["sha256"]) == 64


def test_delivery_scripts_have_no_remote_publish_path() -> None:
    sources = [DELIVERY]
    sources.extend(
        ROOT / "scripts" / f"{stem}.{suffix}" for stem in SCRIPT_STEMS for suffix in ("ps1", "sh")
    )
    forbidden = re.compile(
        r"\b(git\s+(?:push|remote\s+add)|gh\s+pr|deploy(?:ment)?\b|publish\b|"
        r"curl\b[^\n]*(?:--upload-file|-T\s)|aws\s+s3|gcloud\s+storage|az\s+storage)\b",
        re.IGNORECASE,
    )
    findings = [
        path.relative_to(ROOT).as_posix()
        for path in sources
        if forbidden.search(path.read_text(encoding="utf-8"))
    ]
    assert findings == []


def test_internal_markdown_links_resolve() -> None:
    broken: list[str] = []
    for document in (ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))):
        for raw_link in markdown_links(document):
            target_text = unquote(raw_link.split("#", 1)[0]).strip()
            if not target_text or "://" in target_text or target_text.startswith("mailto:"):
                continue
            target = (document.parent / target_text).resolve()
            if not target.exists():
                broken.append(f"{document.relative_to(ROOT).as_posix()} -> {raw_link}")
    assert broken == []


def test_packaged_markdown_links_resolve_inside_the_delivery_inventory() -> None:
    inventory = delivery.package_paths()
    names = {path.relative_to(ROOT).as_posix() for path in inventory}
    broken: list[str] = []
    for document in (path for path in inventory if path.suffix.lower() == ".md"):
        for raw_link in markdown_links(document):
            target_text = unquote(raw_link.split("#", 1)[0]).strip()
            if not target_text or "://" in target_text or target_text.startswith("mailto:"):
                continue
            target = (document.parent / target_text).resolve()
            try:
                target_name = target.relative_to(ROOT).as_posix()
            except ValueError:
                target_name = str(target)
            if target_name not in names:
                source = document.relative_to(ROOT).as_posix()
                broken.append(f"{source} -> {raw_link}")
    assert broken == []


def test_mermaid_blocks_parse_with_supported_renderer() -> None:
    result = run_delivery("validate-mermaid", "--json")
    payload = json.loads(result.stdout)
    assert payload["blocks"] >= 4
    assert payload["failures"] == []


def test_fixture_oracle_hashes_citations_and_unsupported_claims() -> None:
    fixture_root = ROOT / "demo" / "fixtures"
    oracle = json.loads((fixture_root / "oracle.json").read_text(encoding="utf-8"))
    assert len(oracle["facts"]) >= 6
    assert oracle["citation_coverage"] == 1.0
    assert {item["reason"] for item in oracle["excluded_claims"]} == {"unsupported"}
    for source in oracle["sources"]:
        data = (fixture_root / source["name"]).read_bytes()
        assert f"sha256:{sha256(data).hexdigest()}" == source["sha256"]
    chunks_by_source = {
        source["name"]: parse_fixture(source["name"])
        for source in oracle["sources"]
        if Path(source["name"]).suffix in {".pdf", ".docx", ".txt", ".md"}
    }
    for fact in oracle["facts"]:
        chunks = chunks_by_source[fact["source"]]
        assert any(fact["quoted_text"] in chunk.content for chunk in chunks)
    published_keys = {fact["fact_key"] for fact in oracle["facts"]}
    assert all(item["fact_key"] not in published_keys for item in oracle["excluded_claims"])


def test_measurement_rows_recompute_documented_summary() -> None:
    raw_path = ROOT / "demo" / "measurements" / "workflow-raw.csv"
    summary_path = ROOT / "demo" / "measurements" / "workflow-summary.json"
    rows = list(csv.DictReader(raw_path.open(encoding="utf-8", newline="")))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert len(rows) >= 5
    elapsed = [float(row["elapsed_ms"]) for row in rows]
    assert summary["sample_count"] == len(rows)
    assert summary["p50_ms"] == pytest.approx(statistics.median(elapsed), abs=0.01)
    assert summary["p95_ms"] == pytest.approx(percentile_nearest_rank(elapsed, 0.95), abs=0.01)
    assert all(row["cache_state"] in {"cold", "warm"} for row in rows)
    assert all(float(row["citation_coverage"]) == 1.0 for row in rows)
    assert all("queue_outcome" not in row for row in rows)


def test_video_probe_and_narration_cover_required_beats() -> None:
    result = run_delivery("probe-video", "--json")
    probe = json.loads(result.stdout)
    assert 180 <= probe["duration_seconds"] <= 300
    assert probe["codec"] == "h264"
    assert probe["width"] >= 1280
    assert probe["height"] >= 720
    assert probe["fps"] >= 24
    assert probe["audio_codec"] == "aac"
    assert probe["nonblank_samples"] == probe["sample_count"] >= 7
    video_bytes = (ROOT / "demo" / "fieldledger-superdocs-demo.mp4").read_bytes()
    assert b"SUPERDOCS_API_KEY" not in video_bytes
    assert b"BEGIN PRIVATE KEY" not in video_bytes
    assert re.search(rb"\bsk-[A-Za-z0-9_-]{20,}\b", video_bytes) is None
    narration = (ROOT / "docs" / "demo" / "narration-script.md").read_text(encoding="utf-8")
    beats = (
        "document pile",
        "citation",
        "offline",
        "mixed decisions",
        "stale",
        "reconnect",
        "apply once",
        "proposal review",
        "export",
        "audit",
        "Word",
        "local-only",
    )
    assert all(beat.casefold() in narration.casefold() for beat in beats)
    timestamps = [
        int(minutes) * 60 + int(seconds)
        for minutes, seconds in re.findall(r"\b(\d):(\d{2})\b", narration)
    ]
    assert timestamps == sorted(timestamps)
    assert timestamps[-1] + 10 <= probe["duration_seconds"]


def test_video_recorder_asserts_authoritative_reconnect_and_audit_delta() -> None:
    recorder = (ROOT / "demo" / "record-demo.mjs").read_text(encoding="utf-8")
    required_proof = (
        "context.setOffline(true)",
        "context.setOffline(false)",
        "decisionPostCount",
        'getByText("5 decisions · Change order 014", { exact: true })',
        "auditCountBefore",
        "auditCountAfter",
        "review.decisions_applied",
        'level: 2, name: "review.decisions_applied"',
        '.filter({ hasText: "review.decisions_applied" })',
        'getByText("Applied", { exact: true })',
        'getByText("not-recorded", { exact: true })',
    )
    assert all(marker in recorder for marker in required_proof)

    wrapper = (ROOT / "demo" / "record-demo.ps1").read_text(encoding="utf-8")
    assert "http://127.0.0.1:4173" in wrapper
    assert "http://127.0.0.1:4174" in wrapper
    assert "Start-Process" not in wrapper
    assert "vite\", \"preview" not in wrapper
    assert "-t 230" in wrapper
    for second in (10, 30, 55, 90, 125, 150, 180, 210):
        assert f"frame-{second:03}.png" in wrapper
    assert "contact-sheet.png" in wrapper


def test_documented_api_and_mcp_names_match_executable_contracts() -> None:
    openapi = json.loads((ROOT / "apps" / "api" / "openapi.json").read_text(encoding="utf-8"))
    api_reference = (ROOT / "docs" / "api-reference.md").read_text(encoding="utf-8")
    for path in openapi["paths"]:
        assert path in api_reference
    mcp_source = (ROOT / "apps" / "api" / "src" / "fieldledger" / "mcp_server.py").read_text(
        encoding="utf-8"
    )
    mcp_docs = (ROOT / "docs" / "mcp.md").read_text(encoding="utf-8")
    tools = re.findall(
        r"@mcp\.tool(?:\(name=\"([^\"]+)\"\))?\s*\n\s*async def ([a-z_]+)", mcp_source
    )
    names = [public or function for public, function in tools]
    assert names
    assert all(f"`{name}`" in mcp_docs for name in names)

    apply_statuses = set(
        openapi["components"]["schemas"]["ApplyResult"]["properties"]["status"]["enum"]
    )
    outcomes_section = api_reference.split("Outcomes are:", 1)[1].split("##", 1)[0]
    documented_statuses = set(re.findall(r"^- `([^`]+)`", outcomes_section, re.MULTILINE))
    assert documented_statuses <= apply_statuses
    assert "reviewed dependency digest" not in api_reference
    assert "required `Idempotency-Key` header" in api_reference
    assert "body contains only ordered item decisions and a UI confirmation token" in api_reference
    inspect_row = next(
        line for line in api_reference.splitlines() if "/api/v1/runs/{run_id}" in line
    )
    assert "attempts" not in inspect_row.casefold()
    assert "artifacts" not in inspect_row.casefold()


def test_requirement_matrix_proof_paths_are_packaged() -> None:
    matrix = (ROOT / "docs" / "submission" / "requirement-matrix.md").read_text(
        encoding="utf-8"
    )
    package_names = {path.relative_to(ROOT).as_posix() for path in delivery.package_paths()}
    proof_paths = re.findall(r"`([^`]+\.(?:png|mp4))`", matrix)

    assert proof_paths
    assert all(path in package_names for path in proof_paths)


def test_posix_commands_use_the_shell_explicitly() -> None:
    documents = [ROOT / "README.md", ROOT / "docs" / "operations.md"]
    direct_invocations = []
    for document in documents:
        content = document.read_text(encoding="utf-8")
        direct_invocations.extend(
            match.group(0)
            for match in re.finditer(r"(?<!sh )\./scripts/[a-z-]+\.sh", content)
        )

    assert direct_invocations == []
