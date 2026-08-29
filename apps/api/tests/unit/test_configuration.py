import json
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]


def run_json(*command: str) -> Any:
    executable = shutil.which(command[0])
    assert executable is not None, f"required executable is unavailable: {command[0]}"
    completed = subprocess.run(
        (executable, *command[1:]),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_root_manifests_expose_the_required_toolchain_and_commands() -> None:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert package["packageManager"].split("@", maxsplit=1)[1].split(".", maxsplit=1)[0] == "11"
    assert package["engines"]["node"] == ">=22.12.0"
    assert package["scripts"] == {
        "lint": "pnpm -r lint",
        "test": "pnpm -r test",
        "typecheck": "pnpm -r typecheck",
        "build": "pnpm -r build",
        "api:generate": "uv run python scripts/generate_api_client.py",
        "api:check": "uv run python scripts/generate_api_client.py --check",
    }
    assert pyproject["project"]["requires-python"] == ">=3.12,<3.13"


def test_pnpm_resolves_all_real_workspaces() -> None:
    projects = run_json("pnpm", "list", "-r", "--depth", "-1", "--json")

    assert {project["name"] for project in projects} == {
        "fieldledger",
        "@fieldledger/mobile",
        "@fieldledger/word",
        "@fieldledger/offline-queue",
        "@fieldledger/api-client",
    }


def test_each_typescript_workspace_runs_vitest_assertions() -> None:
    workspace_paths = (
        "apps/mobile",
        "extensions/word",
        "packages/offline-queue",
        "packages/api-client",
    )

    for workspace_path in workspace_paths:
        package = json.loads((ROOT / workspace_path / "package.json").read_text(encoding="utf-8"))
        assert package["scripts"]["test"] == "vitest run"
        assert list((ROOT / workspace_path / "src").glob("*.test.ts"))


def test_openapi_snapshot_and_generated_client_have_no_drift() -> None:
    completed = subprocess.run(
        (
            shutil.which("uv") or "uv",
            "run",
            "python",
            "scripts/generate_api_client.py",
            "--check",
        ),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_generated_core_client_operations_have_concrete_types_and_token_support() -> None:
    generated = (ROOT / "packages/api-client/src/generated.ts").read_text(encoding="utf-8")

    assert "private readonly token: string" in generated
    for operation_id in (
        "list_projects_api_v1_projects_get",
        "start_run_api_v1_projects__project_id__runs_post",
        "get_review_api_v1_reviews__batch_id__get",
        "submit_decisions_api_v1_reviews__batch_id__decisions_post",
        "export_document_api_v1_documents__document_id__exports_post",
    ):
        block = generated.split(f'  "{operation_id}": {{', maxsplit=1)[1].split("  };", maxsplit=1)[
            0
        ]
        assert "response: unknown" not in block
        assert "requestBody: unknown" not in block


def test_resolved_compose_contract_has_six_healthy_no_key_services() -> None:
    compose = run_json("docker", "compose", "config", "--format", "json")
    services = compose["services"]

    assert set(services) == {"db", "api", "worker", "watcher", "mobile", "word"}
    assert all(service.get("healthcheck", {}).get("test") for service in services.values())
    assert services["db"]["image"] == (
        "postgres:17-alpine@"
        "sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73"
    )
    assert services["mobile"]["build"]["dockerfile"] == "apps/mobile/Dockerfile"
    assert services["word"]["build"]["dockerfile"] == "extensions/word/Dockerfile"
    for service_name in ("api", "worker", "watcher"):
        environment = services[service_name]["environment"]
        assert environment["FIELDLEDGER_PROVIDER"] == "deterministic"
        assert {
            "FIELDLEDGER_DEMO_BOOTSTRAP_SECRET",
            "FIELDLEDGER_DEMO_IDENTITIES",
            "FIELDLEDGER_LOCAL_DEMO",
            "FIELDLEDGER_POLICY_CONTENT_HASH",
            "FIELDLEDGER_PROVIDER_TIMEOUT_SECONDS",
            "FIELDLEDGER_SESSION_AUDIENCE",
            "FIELDLEDGER_SESSION_ISSUER",
            "FIELDLEDGER_SESSION_SIGNING_SECRET",
            "SUPERDOCS_API_KEY",
        } <= environment.keys()

    watcher_environment = services["watcher"]["environment"]
    assert {
        "FIELDLEDGER_WATCHER_ENABLED",
        "FIELDLEDGER_WATCHER_INBOX",
        "FIELDLEDGER_WATCHER_PROJECT_ID",
    } <= watcher_environment.keys()
    assert "--health" in services["watcher"]["healthcheck"]["test"][-1]


def test_api_image_copies_canonical_demo_fixtures_to_runtime_path() -> None:
    dockerfile = (ROOT / "apps" / "api" / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY demo/fixtures ./demo/fixtures" in dockerfile


def test_default_settings_have_no_known_session_or_demo_secret() -> None:
    from fieldledger.config import Settings

    settings = Settings()

    assert settings.session_signing_secret is None
    assert settings.demo_bootstrap_secret is None
    assert settings.demo_identities == {}
