from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _migration() -> ModuleType:
    path = Path(__file__).parents[2] / "migrations" / "versions" / "0007_secure_review_lifecycle.py"
    spec = importlib.util.spec_from_file_location("migration_0007_secure_review_lifecycle", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Bind:
    def scalar(self, _statement: object) -> int:
        return 1


def test_upgrade_refuses_existing_projects_without_explicit_membership_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration()
    created: list[str] = []
    monkeypatch.setattr(migration.op, "get_bind", lambda: _Bind())
    monkeypatch.setattr(
        migration.op,
        "create_table",
        lambda name, *_args, **_kwargs: created.append(name),
    )
    monkeypatch.setattr(migration.op, "create_index", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(migration.op, "add_column", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="project membership"):
        migration.upgrade()

    assert created == []
