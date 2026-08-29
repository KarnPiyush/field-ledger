from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _migration() -> ModuleType:
    path = Path(__file__).parents[2] / "migrations" / "versions" / "0002_ingestion.py"
    spec = importlib.util.spec_from_file_location("migration_0002_ingestion", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Bind:
    def __init__(self, count: int) -> None:
        self.count = count

    def scalar(self, _statement: object) -> int:
        return self.count


def test_upgrade_refuses_to_fabricate_raw_bytes_for_historical_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration()
    added: list[object] = []
    monkeypatch.setattr(migration.op, "get_bind", lambda: _Bind(1))
    monkeypatch.setattr(migration.op, "add_column", lambda *args: added.append(args))

    with pytest.raises(RuntimeError, match="authoritative raw bytes"):
        migration.upgrade()

    assert added == []


def test_clean_upgrade_adds_non_null_raw_bytes_without_empty_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration()
    added: list[object] = []
    monkeypatch.setattr(migration.op, "get_bind", lambda: _Bind(0))
    monkeypatch.setattr(migration.op, "add_column", lambda *args: added.append(args))
    monkeypatch.setattr(migration.op, "create_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(migration.op, "create_index", lambda *args, **kwargs: None)
    monkeypatch.setattr(migration.op, "f", lambda value: value)

    migration.upgrade()

    assert len(added) == 1
    column = added[0][1]
    assert column.name == "raw_content"
    assert column.nullable is False
    assert column.server_default is None
