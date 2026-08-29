from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fieldledger.config import Settings
from fieldledger.services.ingestion_service import FileIngestionOutcome, PileIngestionResult
from fieldledger.watcher import InboxWatcher, watcher_health_status


class RecordingIngestion:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.paths: list[tuple[Path | None, ...]] = []

    async def ingest_pile(self, *, project_id, sources):  # type: ignore[no-untyped-def]
        del project_id
        names = tuple(source.name for source in sources)
        self.calls.append(names)
        self.paths.append(tuple(source.path for source in sources))
        outcomes = tuple(
            FileIngestionOutcome(
                source_name=name,
                status="failed" if name == "broken.pdf" else "parsed",
                content_hash=f"sha256:{name}",
            )
            for name in names
        )
        return PileIngestionResult(files=outcomes, run_id=uuid4())


class RecordingInvalidation:
    def __init__(self) -> None:
        self.calls: list[tuple[FileIngestionOutcome, object]] = []

    async def invalidate_ingestion(
        self, outcome, *, successor_run_id
    ):  # type: ignore[no-untyped-def]
        self.calls.append((outcome, successor_run_id))


class ChangedIngestion:
    def __init__(self) -> None:
        self.run_id = uuid4()
        self.previous_version_id = uuid4()
        self.document_version_id = uuid4()

    async def ingest_pile(self, *, project_id, sources):  # type: ignore[no-untyped-def]
        outcome = FileIngestionOutcome(
            project_id=project_id,
            source_name=sources[0].name,
            status="parsed",
            content_hash="sha256:changed",
            previous_version_id=self.previous_version_id,
            document_version_id=self.document_version_id,
        )
        return PileIngestionResult(files=(outcome,), run_id=self.run_id)


async def test_watcher_waits_for_stability_ignores_partials_and_survives_bad_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "contract.txt").write_text("Document type: Contract")
    (tmp_path / "broken.pdf").write_bytes(b"broken")
    (tmp_path / "upload.part").write_bytes(b"partial")
    ingestion = RecordingIngestion()
    invalidation = RecordingInvalidation()
    watcher = InboxWatcher(
        inbox=tmp_path,
        project_id=uuid4(),
        ingestion=ingestion,
        invalidation=invalidation,
        stability_window=timedelta(seconds=2),
    )
    started = datetime(2026, 8, 23, tzinfo=UTC)

    assert (await watcher.scan(now=started)).files == ()
    result = await watcher.scan(now=started + timedelta(seconds=2))

    assert set(ingestion.calls[0]) == {"contract.txt", "broken.pdf"}
    assert all(path is not None for path in ingestion.paths[0])
    assert {item.status for item in result.files} == {"parsed", "failed"}
    assert all("upload.part" not in call for call in ingestion.calls)


async def test_watcher_rescan_and_rename_delegate_duplicate_authority_to_ingestion(
    tmp_path: Path,
) -> None:
    source = tmp_path / "site.txt"
    source.write_text("Installed quantity: 420 m")
    ingestion = RecordingIngestion()
    invalidation = RecordingInvalidation()
    watcher = InboxWatcher(
        inbox=tmp_path,
        project_id=uuid4(),
        ingestion=ingestion,
        invalidation=invalidation,
        stability_window=timedelta(0),
    )
    now = datetime(2026, 8, 23, tzinfo=UTC)

    await watcher.scan(now=now)
    await watcher.scan(now=now)
    source.rename(tmp_path / "renamed.txt")
    await watcher.scan(now=now)

    assert ingestion.calls == [("site.txt",), ("site.txt",), ("renamed.txt",)]


async def test_watcher_never_buffers_files_before_ingestion(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "site.txt"
    source.write_text("Installed quantity: 420 m")
    ingestion = RecordingIngestion()
    watcher = InboxWatcher(
        inbox=tmp_path,
        project_id=uuid4(),
        ingestion=ingestion,
        invalidation=RecordingInvalidation(),
        stability_window=timedelta(0),
    )

    def forbidden_read_bytes(_path: Path) -> bytes:
        raise AssertionError("watcher must stream through ingestion")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)
    result = await watcher.scan(now=datetime(2026, 8, 23, tzinfo=UTC))

    assert result.files[0].status == "parsed"
    assert ingestion.paths == [(source,)]


async def test_watcher_targets_changed_invalidation_at_successor_run(tmp_path: Path) -> None:
    (tmp_path / "site.txt").write_text("Installed quantity: 421 m")
    ingestion = ChangedIngestion()
    invalidation = RecordingInvalidation()
    watcher = InboxWatcher(
        inbox=tmp_path,
        project_id=uuid4(),
        ingestion=ingestion,
        invalidation=invalidation,
        stability_window=timedelta(0),
    )

    result = await watcher.scan(now=datetime(2026, 8, 23, tzinfo=UTC))

    assert result.run_id == ingestion.run_id
    assert len(invalidation.calls) == 1
    outcome, target_run_id = invalidation.calls[0]
    assert outcome.document_version_id == ingestion.document_version_id
    assert outcome.previous_version_id == ingestion.previous_version_id
    assert target_run_id == ingestion.run_id


def test_watcher_health_reports_disabled_misconfigured_and_ready(tmp_path: Path) -> None:
    project_id = uuid4()

    assert watcher_health_status(Settings()) == "disabled"
    assert watcher_health_status(Settings(watcher_enabled=True)) == "misconfigured"
    assert watcher_health_status(
        Settings(
            watcher_enabled=True,
            watcher_inbox=tmp_path,
            watcher_project_id=project_id,
        )
    ) == "ready"
