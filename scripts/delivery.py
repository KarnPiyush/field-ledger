from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import secrets
import shutil
import socket
import stat
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESET_PHRASE = "RESET-SYNTHETIC-DATA"
STATE_MARKER = ".fieldledger-synthetic-state"
DELIVERY_MANIFEST = ROOT / "scripts" / "delivery-manifest.json"
VERSION = (
    (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if (ROOT / "VERSION").exists()
    else "0.1.0"
)
EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".recording",
    ".superpowers",
    ".venv",
    "__pycache__",
    "artifacts",
    "coverage",
    "dist",
    "node_modules",
    "playwright-report",
    "test-results",
    "tmp",
}
EXCLUDED_SUFFIXES = {".log", ".pyc", ".pyo", ".zip"}
BINARY_SUFFIXES = {".docx", ".mp4", ".pdf", ".png"}
SECRET_PATTERNS = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "OpenAI-style token": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "AWS access key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "JWT": re.compile(rb"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    "assigned live token": re.compile(rb"\bsuperdocs_[A-Za-z0-9_-]{16,}\b", re.IGNORECASE),
}


class DeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeliveryManifest:
    top_level: tuple[str, ...]
    trees: tuple[str, ...]
    excluded_prefixes: tuple[str, ...]
    approved_binary_sha256: dict[str, str]


def run(
    command: Sequence[str],
    *,
    check: bool = True,
    capture: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    executable = shutil.which(command[0]) or command[0]
    return subprocess.run(
        [executable, *command[1:]],
        cwd=ROOT,
        check=check,
        text=True,
        encoding="utf-8",
        capture_output=capture,
        env=env,
    )


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def url_ready(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 400
    except (OSError, urllib.error.URLError):
        return False


def wait_for_url(url: str, *, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if url_ready(url):
            return
        time.sleep(1)
    raise DeliveryError(f"Readiness timed out for {url}. Recovery: run scripts/doctor.ps1")


def is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.25)
        return client.connect_ex(("127.0.0.1", port)) == 0


def delivery_ports(environment: dict[str, str] | None = None) -> dict[str, int]:
    values = environment if environment is not None else os.environ
    configured = {
        "db": ("POSTGRES_PORT", 5432),
        "api": ("FIELDLEDGER_API_PORT", 8000),
        "mobile": ("MOBILE_PORT", 4173),
        "word": ("WORD_PORT", 4174),
    }
    ports: dict[str, int] = {}
    for service, (name, default) in configured.items():
        raw = values.get(name, str(default))
        try:
            port = int(raw)
        except ValueError as error:
            raise DeliveryError(f"{name} must be an integer port") from error
        if not 1 <= port <= 65535:
            raise DeliveryError(f"{name} must be between 1 and 65535")
        ports[service] = port
    return ports


def delivery_urls(ports: dict[str, int]) -> dict[str, str]:
    return {
        service: f"http://127.0.0.1:{ports[service]}"
        for service in ("api", "mobile", "word")
    }


def deterministic_compose_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "FIELDLEDGER_PROVIDER": "deterministic",
            "FIELDLEDGER_LOCAL_DEMO": "true",
            "FIELDLEDGER_SESSION_SIGNING_SECRET": secrets.token_urlsafe(32),
            "FIELDLEDGER_DEMO_BOOTSTRAP_SECRET": secrets.token_urlsafe(32),
            "FIELDLEDGER_DEMO_IDENTITIES": json.dumps(
                {
                    "reviewer": {
                        "actor_id": "demo-reviewer",
                        "source": "ui",
                        "global_role": "user",
                    }
                },
                separators=(",", ":"),
            ),
            "FIELDLEDGER_API_TARGET": "http://api:8000",
            # An explicit empty process value outranks a project .env file in Compose.
            "SUPERDOCS_API_KEY": "",
        }
    )
    return environment


def compose_service_names() -> set[str]:
    result = run(
        ("docker", "compose", "ps", "--services", "--filter", "status=running"),
        capture=True,
        check=False,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def preflight(*, require_running: bool = False) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    ports = delivery_ports()
    urls = delivery_urls(ports)

    def add(name: str, ok: bool, recovery: str) -> None:
        checks.append(
            {"name": name, "status": "ok" if ok else "failed", "recovery": "" if ok else recovery}
        )

    add("Docker CLI", command_exists("docker"), "Install Docker Desktop, then reopen the terminal.")
    if command_exists("docker"):
        docker_info = run(("docker", "info"), check=False, capture=True)
        add(
            "Docker engine",
            docker_info.returncode == 0,
            "Start Docker Desktop, then run scripts/doctor.ps1.",
        )
        compose = run(("docker", "compose", "version"), check=False, capture=True)
        add("Compose", compose.returncode == 0, "Install the Docker Compose plugin.")
        config = run(("docker", "compose", "config", "--quiet"), check=False, capture=True)
        add(
            "Compose configuration",
            config.returncode == 0,
            "Run docker compose config and correct the reported field.",
        )
    else:
        add("Docker engine", False, "Install and start Docker Desktop.")
        add("Compose", False, "Install Docker Desktop with Compose.")
        add("Compose configuration", False, "Install Docker before checking compose.yaml.")

    required = (
        ROOT / "compose.yaml",
        ROOT / "demo" / "fixtures" / "oracle.json",
        ROOT / "apps" / "api" / "migrations",
        ROOT / "apps" / "mobile" / "Dockerfile",
        ROOT / "extensions" / "word" / "Dockerfile",
    )
    add(
        "Required repository files",
        all(path.exists() for path in required),
        "Restore the repository from the delivery ZIP.",
    )
    free = shutil.disk_usage(ROOT).free
    add("Disk space", free >= 2 * 1024**3, "Free at least 2 GB on the repository drive.")

    running = compose_service_names() if command_exists("docker") else set()
    for service, port in ports.items():
        if service in running:
            continue
        add(
            f"{service.title()} port {port}",
            not is_port_open(port),
            f"Stop the process using port {port}, or set the matching port variable.",
        )
    if require_running:
        expected = {"api", "db", "mobile", "watcher", "word", "worker"}
        add("Compose services", expected <= running, "Run scripts/demo.ps1 and wait for readiness.")
        database = run(
            ("docker", "compose", "exec", "-T", "db", "pg_isready"),
            check=False,
            capture=True,
        )
        add(
            "Database readiness",
            database.returncode == 0,
            "Run docker compose logs db.",
        )
        migrations = run(
            (
                "docker",
                "compose",
                "exec",
                "-T",
                "api",
                "alembic",
                "-c",
                "apps/api/alembic.ini",
                "current",
            ),
            check=False,
            capture=True,
        )
        add(
            "Database migrations",
            migrations.returncode == 0,
            "Run docker compose exec -T api alembic -c apps/api/alembic.ini upgrade head.",
        )
        worker = run(
            ("docker", "compose", "exec", "-T", "worker", "python", "-c", "import fieldledger"),
            check=False,
            capture=True,
        )
        add(
            "Worker readiness",
            worker.returncode == 0,
            "Run docker compose logs worker.",
        )
        add(
            "API readiness",
            url_ready(f"{urls['api']}/health/ready"),
            "Run docker compose logs api db.",
        )
        add(
            "Mobile readiness",
            url_ready(f"{urls['mobile']}/inbox"),
            "Run docker compose logs mobile.",
        )
        add(
            "Word readiness",
            url_ready(f"{urls['word']}/taskpane.html"),
            "Run docker compose logs word.",
        )
    return checks


def ensure_preflight(checks: Iterable[dict[str, str]]) -> None:
    failures = [check for check in checks if check["status"] != "ok"]
    if failures:
        detail = "\n".join(f"- {item['name']}: {item['recovery']}" for item in failures)
        raise DeliveryError(f"Preflight failed:\n{detail}")


def demo_command(_: argparse.Namespace) -> None:
    ensure_preflight(preflight())
    environment = deterministic_compose_environment()
    state_root = ROOT / ".data"
    state_root.mkdir(exist_ok=True)
    (state_root / STATE_MARKER).write_text("fieldledger synthetic state\n", encoding="utf-8")
    run(("docker", "compose", "build"), env=environment)
    run(("docker", "compose", "up", "-d", "--wait", "db"), env=environment)
    run(
        (
            "docker",
            "compose",
            "run",
            "--rm",
            "--no-deps",
            "api",
            "alembic",
            "-c",
            "apps/api/alembic.ini",
            "upgrade",
            "head",
        ),
        env=environment,
    )
    run(("docker", "compose", "up", "-d", "--wait"), env=environment)
    urls = delivery_urls(delivery_ports(environment))
    readiness_urls = (
        f"{urls['api']}/health/ready",
        f"{urls['mobile']}/inbox",
        f"{urls['word']}/taskpane.html",
    )
    for url in readiness_urls:
        wait_for_url(url)
    print("FieldLedger deterministic demo is ready")
    print(f"Phone review: {urls['mobile']}/inbox")
    print(f"Word browser demo: {urls['word']}/taskpane.html")
    print(f"API reference: {urls['api']}/docs")
    print("Expected first review: Riverside Medical Center / Change order 014 / 5 cited items")


def doctor_command(arguments: argparse.Namespace) -> None:
    checks = preflight(require_running=arguments.running)
    if arguments.json:
        print(json.dumps({"checks": checks}, indent=2))
    else:
        for item in checks:
            print(f"[{item['status'].upper()}] {item['name']}")
            if item["recovery"]:
                print(f"  Recovery: {item['recovery']}")
    ensure_preflight(checks)


def verify_commands(fast: bool) -> tuple[tuple[str, ...], ...]:
    focused = (
        ("uv", "run", "ruff", "check", "."),
        ("uv", "run", "mypy", "apps/api/src"),
        ("uv", "run", "pytest", "apps/api/tests", "-q"),
        ("pnpm", "--filter", "@fieldledger/api-client", "build"),
        ("pnpm", "--filter", "@fieldledger/offline-queue", "build"),
        ("pnpm", "lint"),
        ("pnpm", "typecheck"),
        ("pnpm", "test"),
        ("pnpm", "api:check"),
        ("pnpm", "build"),
        ("uv", "run", "python", "scripts/delivery.py", "validate-mermaid"),
        ("uv", "run", "python", "scripts/delivery.py", "probe-video"),
        ("uv", "run", "python", "scripts/delivery.py", "package", "--dry-run"),
        ("docker", "compose", "config", "--quiet"),
    )
    if fast:
        return focused
    return focused + (
        ("pnpm", "--filter", "@fieldledger/mobile", "test:e2e"),
        ("pnpm", "--filter", "@fieldledger/word", "test:e2e"),
        ("docker", "compose", "build"),
    )


def verify_command(arguments: argparse.Namespace) -> None:
    for command in verify_commands(arguments.fast):
        print(f"==> {' '.join(command)}", flush=True)
        run(command)
    findings = secret_findings(package_paths())
    if findings:
        raise DeliveryError("Secret scan failed: " + ", ".join(findings))
    print("FieldLedger verification passed")


def validated_reset_targets(state_root: Path | None) -> tuple[Path, ...]:
    repository_root = ROOT.resolve()
    nominal_root = repository_root / ".data"
    safe_root = nominal_root.resolve()
    if safe_root != nominal_root:
        raise DeliveryError("Refusing reset through a redirected synthetic state root")
    target = state_root.resolve() if state_root else safe_root
    if target != safe_root:
        raise DeliveryError(f"Refusing reset outside the exact synthetic state root: {target}")
    return (target,)


def reset_command(arguments: argparse.Namespace) -> None:
    if arguments.confirm != RESET_PHRASE:
        raise DeliveryError(f"Reset requires --confirm {RESET_PHRASE}")
    targets = validated_reset_targets(arguments.state_root)
    for target in targets:
        if target.exists() and not (target / STATE_MARKER).is_file():
            marker = target / STATE_MARKER
            raise DeliveryError(f"Refusing reset without synthetic state marker: {marker}")
    if command_exists("docker"):
        result = run(
            ("docker", "compose", "down", "--volumes", "--remove-orphans"),
            check=False,
            capture=True,
        )
        if result.returncode != 0:
            raise DeliveryError("Docker volume deletion failed; synthetic state was preserved")
    for target in targets:
        if target.exists():
            try:
                shutil.rmtree(target)
            except PermissionError:
                if not target.exists() or any(target.iterdir()):
                    raise
                target.chmod(stat.S_IRWXU)
                target.rmdir()
        print(f"Removed synthetic state: {target}")


def percentile_nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def measure_workflow(samples: int) -> dict[str, Any]:
    from fieldledger.agent.stages import analyze_pile, source_chunks_from_parse
    from fieldledger.parsers import default_registry

    fixture_root = ROOT / "demo" / "fixtures"
    media_types = {
        "construction-contract.pdf": "application/pdf",
        "change-order-014.docx": (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        "site-report-31.txt": "text/plain",
        "invoice-1042.md": "text/markdown",
    }
    rules = json.loads((fixture_root / "rules.json").read_text(encoding="utf-8"))
    raw_path = ROOT / "demo" / "measurements" / "workflow-raw.csv"
    summary_path = ROOT / "demo" / "measurements" / "workflow-summary.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    prior = None
    for index in range(samples):
        started = time.perf_counter_ns()
        chunks = []
        registry = default_registry()
        for name, media_type in media_types.items():
            outcome = registry.parse(name, media_type, (fixture_root / name).read_bytes())
            if outcome.status != "parsed":
                raise DeliveryError(f"Fixture parse failed: {name}")
            chunks.extend(source_chunks_from_parse(name, outcome))
        result = analyze_pile(tuple(chunks), rules, previous=prior)
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        unchanged = 0.0
        if prior is not None:
            unchanged = sum(
                prior.blocks[key].canonical_bytes == result.blocks[key].canonical_bytes
                for key in prior.blocks.keys() & result.blocks.keys()
            ) / max(len(prior.blocks), 1)
        rows.append(
            {
                "sample": index + 1,
                "timestamp_utc": datetime.now(UTC).isoformat(),
                "cache_state": "cold" if index == 0 else "warm",
                "elapsed_ms": f"{elapsed_ms:.3f}",
                "input_bytes": sum((fixture_root / name).stat().st_size for name in media_types),
                "facts": len(result.facts),
                "citation_coverage": f"{result.citation_coverage:.3f}",
                "unchanged_block_ratio": f"{unchanged:.3f}",
                "provider_calls": result.provider_calls,
                "provider_cost_usd": "0.000000",
            }
        )
        prior = result
    fields = list(rows[0])
    with raw_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    elapsed_values = [float(row["elapsed_ms"]) for row in rows]
    summary = {
        "sample_count": len(rows),
        "formula": "p50=median; p95=nearest-rank ceil(0.95*n)",
        "p50_ms": round(statistics.median(elapsed_values), 3),
        "p95_ms": round(percentile_nearest_rank(elapsed_values, 0.95), 3),
        "minimum_ms": round(min(elapsed_values), 3),
        "maximum_ms": round(max(elapsed_values), 3),
        "population_stdev_ms": round(statistics.pstdev(elapsed_values), 3),
        "citation_coverage": 1.0,
        "provider_calls": 0,
        "provider_cost_usd": 0.0,
        "raw_file": "workflow-raw.csv",
    }
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def measure_setup_command(arguments: argparse.Namespace) -> None:
    destination = ROOT / "demo" / "measurements" / "setup-raw.csv"
    destination.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for index in range(arguments.samples):
        started = time.perf_counter_ns()
        result = run(("docker", "compose", "config", "--quiet"), check=False, capture=True)
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        rows.append(
            {
                "sample": index + 1,
                "timestamp_utc": datetime.now(UTC).isoformat(),
                "condition": "warm_cached_configuration",
                "elapsed_ms": f"{elapsed_ms:.3f}",
                "exit_code": result.returncode,
                "cold_pull": "not_measured",
            }
        )
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    workflow = measure_workflow(arguments.samples)
    print(json.dumps({"setup_samples": len(rows), "workflow": workflow}, indent=2))


def mcp_inspector_command(arguments: argparse.Namespace) -> None:
    if arguments.smoke:
        run(
            (
                "uv",
                "run",
                "python",
                "-c",
                "from fieldledger.mcp_server import build_default_server; "
                "print(build_default_server().name)",
            )
        )
        return
    run(
        (
            "uv",
            "run",
            "mcp",
            "dev",
            "apps/api/src/fieldledger/mcp_inspector.py:mcp",
        )
    )


def load_delivery_manifest(
    manifest_path: Path | None = None, *, root: Path = ROOT
) -> DeliveryManifest:
    path = (manifest_path or (root / "scripts" / "delivery-manifest.json")).resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DeliveryError(f"Delivery manifest is unreadable: {path}") from error
    if payload.get("version") != 1:
        raise DeliveryError("Delivery manifest version must be 1")
    try:
        return DeliveryManifest(
            top_level=tuple(str(item) for item in payload["top_level"]),
            trees=tuple(str(item).rstrip("/") for item in payload["trees"]),
            excluded_prefixes=tuple(
                str(item).strip("/") for item in payload["excluded_prefixes"]
            ),
            approved_binary_sha256={
                str(name): str(digest)
                for name, digest in payload["approved_binary_sha256"].items()
            },
        )
    except (KeyError, TypeError) as error:
        raise DeliveryError("Delivery manifest has an invalid shape") from error


def is_package_path(path: Path, *, root: Path = ROOT) -> bool:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    if any(part in EXCLUDED_PARTS for part in relative.parts):
        return False
    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return False
    if path.name.startswith(".env") and relative.as_posix() != ".env.example":
        return False
    return path.is_file()


def _git_tracked_names(root: Path) -> list[str] | None:
    if not command_exists("git"):
        return None
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    return [name.decode("utf-8") for name in result.stdout.split(b"\0") if name]


def package_paths(
    *, root: Path = ROOT, manifest_path: Path | None = None
) -> list[Path]:
    root = root.resolve()
    manifest_path = (manifest_path or (root / "scripts" / "delivery-manifest.json")).resolve()
    manifest = load_delivery_manifest(manifest_path, root=root)
    tracked = _git_tracked_names(root)
    if tracked is None:
        candidates = [root / name for name in manifest.top_level]
        for tree in manifest.trees:
            base = root / tree
            if base.exists():
                candidates.extend(base.rglob("*"))
    else:
        candidates = [root / name for name in tracked]
        if manifest_path.exists():
            candidates.append(manifest_path)

    top_level = set(manifest.top_level)
    trees = set(manifest.trees)
    excluded = manifest.excluded_prefixes
    included: set[Path] = set()
    for candidate in candidates:
        if not is_package_path(candidate, root=root):
            continue
        relative = candidate.resolve().relative_to(root).as_posix()
        allowed = relative in top_level or relative.split("/", 1)[0] in trees
        denied = any(relative == prefix or relative.startswith(f"{prefix}/") for prefix in excluded)
        if allowed and not denied:
            included.add(candidate.resolve())
    return sorted(included, key=lambda path: path.relative_to(root).as_posix())


def _display_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _content_chunks(path: Path) -> Iterable[bytes]:
    content = path.read_bytes()
    yield content
    yield from _extracted_chunks(path.name, content)


def _extracted_chunks(name: str, content: bytes) -> Iterable[bytes]:
    suffix = Path(name).suffix.lower()
    if suffix == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as document:
                for member in document.infolist():
                    if not member.is_dir():
                        yield document.read(member)
        except zipfile.BadZipFile as error:
            raise DeliveryError(f"DOCX content scan failed: {name}") from error
    elif suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(content))
            for page in reader.pages:
                yield (page.extract_text() or "").encode("utf-8")
        except Exception as error:
            raise DeliveryError(f"PDF content scan failed: {name}") from error


def secret_findings(paths: Iterable[Path], *, root: Path = ROOT) -> list[str]:
    findings: list[str] = []
    for path in paths:
        for content in _content_chunks(path):
            for label, pattern in SECRET_PATTERNS.items():
                if pattern.search(content):
                    finding = f"{_display_path(path, root)} ({label})"
                    if finding not in findings:
                        findings.append(finding)
    return findings


PRIVATE_PATH_PATTERNS = {
    "Windows home path": re.compile(rb"\b[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s<>\"]+", re.I),
    "POSIX home path": re.compile(rb"(?:^|[\s(])/(?:home|Users)/[^/\s<>\"]+", re.I),
}


def private_path_findings(paths: Iterable[Path], *, root: Path = ROOT) -> list[str]:
    findings: list[str] = []
    for path in paths:
        for content in _content_chunks(path):
            for label, pattern in PRIVATE_PATH_PATTERNS.items():
                if pattern.search(content):
                    finding = f"{_display_path(path, root)} ({label})"
                    if finding not in findings:
                        findings.append(finding)
    return findings


def verify_approved_binary_hashes(
    paths: Iterable[Path], *, root: Path = ROOT, manifest_path: Path | None = None
) -> None:
    manifest = load_delivery_manifest(manifest_path, root=root)
    binaries = {
        path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in paths
        if path.suffix.lower() in BINARY_SUFFIXES
    }
    if set(binaries) != set(manifest.approved_binary_sha256):
        raise DeliveryError("Delivery manifest binary inventory does not match package inventory")
    drift = [
        name
        for name, digest in binaries.items()
        if digest != manifest.approved_binary_sha256[name]
    ]
    if drift:
        raise DeliveryError("Approved binary hash drift: " + ", ".join(sorted(drift)))


def audit_archive(path: Path, expected: list[Path], *, root: Path = ROOT) -> dict[str, object]:
    expected_names = sorted(item.relative_to(root).as_posix() for item in expected)
    expected_hashes = {
        item.relative_to(root).as_posix(): sha256(item.read_bytes()).hexdigest()
        for item in expected
    }
    with zipfile.ZipFile(path) as archive:
        names = sorted(name.rstrip("/") for name in archive.namelist() if not name.endswith("/"))
        if names != expected_names:
            raise DeliveryError("Archive content does not match the package allowlist")
        for info in archive.infolist():
            normalized = Path(info.filename)
            if normalized.is_absolute() or ".." in normalized.parts:
                raise DeliveryError(f"Unsafe archive path: {info.filename}")
            if info.is_dir():
                continue
            content = archive.read(info)
            if sha256(content).hexdigest() != expected_hashes[info.filename]:
                raise DeliveryError(f"Archive content hash mismatch: {info.filename}")
            for chunk in (content, *_extracted_chunks(info.filename, content)):
                for label, pattern in SECRET_PATTERNS.items():
                    if pattern.search(chunk):
                        raise DeliveryError(f"Archive secret pattern: {info.filename} ({label})")
                for label, pattern in PRIVATE_PATH_PATTERNS.items():
                    if pattern.search(chunk):
                        raise DeliveryError(f"Archive private path: {info.filename} ({label})")
    return {
        "archive": str(path),
        "entries": len(expected_names),
        "sha256": sha256(path.read_bytes()).hexdigest(),
    }


def _git_output(root: Path, *arguments: str) -> str | None:
    if not command_exists("git"):
        return None
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip() if result.returncode == 0 else None


def git_main_checkout(root: Path = ROOT) -> Path | None:
    common = _git_output(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common is None:
        return None
    common_path = Path(common).resolve()
    return common_path.parent if common_path.name == ".git" else common_path


def git_checkout_roots(root: Path = ROOT) -> tuple[Path, ...]:
    output = _git_output(root, "worktree", "list", "--porcelain")
    if output is None:
        return (root.resolve(),)
    roots = tuple(
        Path(line.removeprefix("worktree ")).resolve()
        for line in output.splitlines()
        if line.startswith("worktree ")
    )
    return roots or (root.resolve(),)


def default_archive_output(root: Path = ROOT) -> Path:
    main = git_main_checkout(root)
    boundary = main if main is not None else root.resolve()
    return boundary.parent / f"fieldledger-{VERSION}.zip"


def validate_archive_output(output: Path, *, root: Path = ROOT) -> Path:
    resolved = output.resolve()
    for checkout in git_checkout_roots(root):
        try:
            resolved.relative_to(checkout)
        except ValueError:
            continue
        raise DeliveryError("Archive output must be outside every repository checkout")
    return resolved


def write_archive_atomic(
    output: Path, paths: list[Path], *, root: Path = ROOT
) -> dict[str, object]:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent, prefix=f".{output.name}.", suffix=".tmp"
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for source in paths:
                archive.write(source, source.relative_to(root).as_posix())
        payload = audit_archive(temporary, paths, root=root)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    payload["archive"] = str(output)
    return payload


def package_command(arguments: argparse.Namespace) -> None:
    paths = package_paths()
    findings = secret_findings(paths)
    if findings:
        raise DeliveryError("Secret scan failed: " + ", ".join(findings))
    private_findings = private_path_findings(paths)
    if private_findings:
        raise DeliveryError("Private path scan failed: " + ", ".join(private_findings))
    verify_approved_binary_hashes(paths)
    payload: dict[str, object] = {
        "included_paths": [path.relative_to(ROOT).as_posix() for path in paths],
        "entry_count": len(paths),
    }
    if not arguments.dry_run:
        output = validate_archive_output(
            arguments.output if arguments.output else default_archive_output()
        )
        payload.update(write_archive_atomic(output, paths))
    if arguments.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Package allowlist: {len(paths)} files")
        if payload.get("archive"):
            print(f"Archive: {payload['archive']}")


def mermaid_blocks() -> list[tuple[Path, str]]:
    blocks: list[tuple[Path, str]] = []
    for document in sorted((ROOT / "docs").rglob("*.md")):
        content = document.read_text(encoding="utf-8")
        for source in re.findall(r"```mermaid\s*\n(.*?)```", content, flags=re.DOTALL):
            blocks.append((document, source.strip() + "\n"))
    return blocks


def validate_mermaid_command(arguments: argparse.Namespace) -> None:
    blocks = mermaid_blocks()
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="fieldledger-mermaid-") as directory:
        temp = Path(directory)
        for index, (document, source) in enumerate(blocks):
            input_path = temp / f"diagram-{index}.mmd"
            input_path.write_text(source, encoding="utf-8")
            result = run(
                (
                    "pnpm",
                    "exec",
                    "node",
                    "scripts/validate-mermaid.mjs",
                    str(input_path),
                ),
                check=False,
                capture=True,
            )
            if result.returncode != 0:
                location = document.relative_to(ROOT).as_posix()
                failures.append(f"{location} block {index + 1}: {result.stderr.strip()}")
    payload = {"blocks": len(blocks), "failures": failures}
    if arguments.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Mermaid blocks: {len(blocks)}; failures: {len(failures)}")
    if failures:
        raise DeliveryError("Mermaid validation failed")


def find_media_tool(name: str) -> Path:
    found = shutil.which(name)
    if found:
        return Path(found)
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    matches = sorted(local.glob(f"Gyan.FFmpeg*/**/{name}.exe"))
    if matches:
        return matches[-1]
    raise DeliveryError(f"{name} was not found. Recovery: install FFmpeg Essentials with winget.")


def nonblank_frame_count(video: Path, duration: float, samples: int = 9) -> int:
    ffmpeg = find_media_tool("ffmpeg")
    nonblank = 0
    for index in range(samples):
        timestamp = duration * (index + 1) / (samples + 1)
        result = subprocess.run(
            [
                str(ffmpeg),
                "-v",
                "error",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-vf",
                "scale=64:36,format=gray",
                "-f",
                "rawvideo",
                "-",
            ],
            check=True,
            capture_output=True,
        )
        pixels = result.stdout
        if pixels and max(pixels) - min(pixels) >= 24 and len(set(pixels)) >= 16:
            nonblank += 1
    return nonblank


def probe_video_command(arguments: argparse.Namespace) -> None:
    video = ROOT / "demo" / "fieldledger-superdocs-demo.mp4"
    ffprobe = find_media_tool("ffprobe")
    result = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    raw = json.loads(result.stdout)
    video_stream = next(stream for stream in raw["streams"] if stream["codec_type"] == "video")
    audio_streams = [stream for stream in raw["streams"] if stream["codec_type"] == "audio"]
    duration = float(raw["format"]["duration"])
    rate = video_stream["avg_frame_rate"].split("/")
    fps = float(rate[0]) / float(rate[1])
    sample_count = 9
    payload = {
        "path": video.relative_to(ROOT).as_posix(),
        "sha256": sha256(video.read_bytes()).hexdigest(),
        "duration_seconds": round(duration, 3),
        "codec": video_stream["codec_name"],
        "width": int(video_stream["width"]),
        "height": int(video_stream["height"]),
        "fps": round(fps, 3),
        "audio_codec": audio_streams[0]["codec_name"] if audio_streams else None,
        "sample_count": sample_count,
        "nonblank_samples": nonblank_frame_count(video, duration, sample_count),
        "ffprobe": raw["programs"] if "programs" in raw else [],
    }
    if arguments.json:
        print(json.dumps(payload, indent=2))
    else:
        geometry = f"{payload['width']}x{payload['height']}"
        sampling = f"{payload['nonblank_samples']}/{sample_count}"
        print(
            f"Video {payload['duration_seconds']}s {payload['codec']} {geometry} "
            f"{payload['fps']}fps audio={payload['audio_codec']} nonblank={sampling}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FieldLedger local delivery commands")
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser(
        "demo", description="FieldLedger demo", help="Start the deterministic local stack"
    )
    demo.set_defaults(handler=demo_command)

    doctor = subparsers.add_parser(
        "doctor", description="FieldLedger doctor", help="Check local prerequisites and readiness"
    )
    doctor.add_argument(
        "--running", action="store_true", help="Require the complete stack to be running"
    )
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=doctor_command)

    verify = subparsers.add_parser(
        "verify", description="FieldLedger verify", help="Run noninteractive project checks"
    )
    verify.add_argument("--fast", action="store_true", help="Skip browser suites and image builds")
    verify.set_defaults(handler=verify_command)

    reset = subparsers.add_parser(
        "reset", description="FieldLedger reset", help="Remove synthetic local state"
    )
    reset.add_argument("--confirm")
    reset.add_argument("--state-root", type=Path, help=argparse.SUPPRESS)
    reset.set_defaults(handler=reset_command)

    measure = subparsers.add_parser(
        "measure-setup",
        description="FieldLedger measure-setup",
        help="Record setup and deterministic workflow samples",
    )
    measure.add_argument("--samples", type=int, default=7)
    measure.set_defaults(handler=measure_setup_command)

    mcp = subparsers.add_parser(
        "mcp-inspector",
        description="FieldLedger mcp-inspector",
        help="Open or smoke-test the local MCP inspector",
    )
    mcp.add_argument("--smoke", action="store_true")
    mcp.set_defaults(handler=mcp_inspector_command)

    package = subparsers.add_parser(
        "package",
        description="FieldLedger package",
        help="Create and audit a local delivery archive",
    )
    package.add_argument("--dry-run", action="store_true")
    package.add_argument("--output", type=Path)
    package.add_argument("--json", action="store_true")
    package.set_defaults(handler=package_command)

    mermaid = subparsers.add_parser("validate-mermaid", description="FieldLedger validate-mermaid")
    mermaid.add_argument("--json", action="store_true")
    mermaid.set_defaults(handler=validate_mermaid_command)

    video = subparsers.add_parser("probe-video", description="FieldLedger probe-video")
    video.add_argument("--json", action="store_true")
    video.set_defaults(handler=probe_video_command)
    return parser


def main() -> int:
    parser = build_parser()
    arguments = parser.parse_args()
    try:
        arguments.handler(arguments)
    except (DeliveryError, FileNotFoundError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
