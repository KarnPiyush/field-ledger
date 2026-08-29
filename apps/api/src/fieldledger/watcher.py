from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID

from fieldledger.config import Settings
from fieldledger.services.ingestion_service import (
    FileIngestionOutcome,
    IngestSource,
    PileIngestionResult,
)


class IngestionPort(Protocol):
    async def ingest_pile(
        self, *, project_id: UUID, sources: tuple[IngestSource, ...]
    ) -> PileIngestionResult: ...


class InvalidationPort(Protocol):
    async def invalidate_ingestion(
        self, outcome: FileIngestionOutcome, *, successor_run_id: UUID
    ) -> object: ...


@dataclass(frozen=True)
class _Observation:
    device: int
    inode: int
    size: int
    modified_ns: int
    stable_since: datetime


_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".txt": "text/plain",
    ".md": "text/markdown",
}
_TEMP_SUFFIXES = (".tmp", ".temp", ".part", ".partial", ".crdownload", "~")


class InboxWatcher:
    def __init__(
        self,
        *,
        inbox: Path,
        project_id: UUID,
        ingestion: IngestionPort,
        invalidation: InvalidationPort,
        stability_window: timedelta = timedelta(seconds=2),
    ) -> None:
        if stability_window < timedelta(0):
            raise ValueError("stability_window cannot be negative")
        self.inbox = inbox
        self.project_id = project_id
        self.ingestion = ingestion
        self.invalidation = invalidation
        self.stability_window = stability_window
        self._observed: dict[Path, _Observation] = {}

    async def scan(self, *, now: datetime | None = None) -> PileIngestionResult:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        candidates = tuple(
            path
            for path in sorted(self.inbox.iterdir(), key=lambda item: item.name)
            if path.is_file() and not self._temporary(path.name)
        )
        present = set(candidates)
        stale_observations = {
            path: self._observed[path] for path in set(self._observed) - present
        }

        stable: list[Path] = []
        for path in candidates:
            stat = path.stat()
            signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            observed = self._observed.get(path)
            if observed is None or (
                observed.device,
                observed.inode,
                observed.size,
                observed.modified_ns,
            ) != signature:
                observed = _Observation(
                    stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, timestamp
                )
                self._observed[path] = observed
            if timestamp - observed.stable_since >= self.stability_window:
                stable.append(path)
        if not stable:
            return PileIngestionResult(files=(), run_id=None)

        sources: list[IngestSource] = []
        for path in stable:
            observation = self._observed[path]
            previous_path = next(
                (
                    stale_path
                    for stale_path, stale in stale_observations.items()
                    if (stale.device, stale.inode) == (
                        observation.device,
                        observation.inode,
                    )
                ),
                None,
            )
            sources.append(
                IngestSource(
                    name=path.name,
                    media_type=_MEDIA_TYPES.get(
                        path.suffix.lower(), "application/octet-stream"
                    ),
                    path=path,
                    previous_name=(previous_path.name if previous_path is not None else None),
                )
            )
        for stale in stale_observations:
            self._observed.pop(stale, None)
        result = await self.ingestion.ingest_pile(
            project_id=self.project_id, sources=tuple(sources)
        )
        if result.run_id is not None:
            for outcome in result.files:
                if outcome.status == "parsed" and outcome.previous_version_id is not None:
                    await self.invalidation.invalidate_ingestion(
                        outcome, successor_run_id=result.run_id
                    )
        return result

    @staticmethod
    def _temporary(name: str) -> bool:
        lowered = name.lower()
        return name.startswith((".", "~$")) or lowered.endswith(_TEMP_SUFFIXES)


async def run_watcher(watcher: InboxWatcher, *, poll_interval: float = 1.0) -> None:
    while True:
        await watcher.scan()
        await asyncio.sleep(poll_interval)


def watcher_health_status(settings: Settings) -> str:
    if not settings.watcher_enabled:
        return "disabled"
    if settings.watcher_project_id is None:
        return "misconfigured"
    return "ready"


async def _wait_while_disabled() -> None:
    await asyncio.Event().wait()


def run() -> None:
    from fieldledger.config import get_settings
    from fieldledger.db import session_factory
    from fieldledger.services.ingestion_service import IngestionService
    from fieldledger.services.invalidation_service import InvalidationService
    from fieldledger.services.workflow_service import WorkflowService

    settings = get_settings()
    status = watcher_health_status(settings)
    if "--health" in sys.argv[1:]:
        print(status)
        raise SystemExit(0 if status in {"disabled", "ready"} else 1)
    if status == "disabled":
        print("FieldLedger inbox watcher is disabled")
        asyncio.run(_wait_while_disabled())
        return
    if settings.watcher_project_id is None:
        raise RuntimeError("FIELDLEDGER_WATCHER_PROJECT_ID is required for the watcher")
    settings.watcher_inbox.mkdir(parents=True, exist_ok=True)
    workflow = WorkflowService(session_factory)
    watcher = InboxWatcher(
        inbox=settings.watcher_inbox,
        project_id=settings.watcher_project_id,
        ingestion=IngestionService(session_factory, workflow=workflow),
        invalidation=InvalidationService(session_factory),
    )
    asyncio.run(run_watcher(watcher))


if __name__ == "__main__":
    run()
