import json
from functools import lru_cache
from os import getenv
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from fieldledger.security import DemoIdentity


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    database_url: str = "postgresql+asyncpg://fieldledger:fieldledger@localhost:5432/fieldledger"
    provider: str = "deterministic"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    local_demo_enabled: bool = False
    session_signing_secret: str | None = None
    demo_bootstrap_secret: str | None = None
    demo_identities: dict[str, DemoIdentity] = Field(default_factory=dict)
    session_issuer: str = "fieldledger-local"
    session_audience: str = "fieldledger-clients"
    policy_content_hash: str = "sha256:fieldledger-policy-v1"
    superdocs_api_key: str | None = None
    provider_timeout_seconds: float = 15
    watcher_enabled: bool = False
    watcher_inbox: Path = Path("data/inbox")
    watcher_project_id: UUID | None = None

    @classmethod
    def from_environment(cls) -> "Settings":
        provider = getenv("FIELDLEDGER_PROVIDER", cls.model_fields["provider"].default)
        local_demo = getenv("FIELDLEDGER_LOCAL_DEMO", "false").strip().lower()
        return cls(
            database_url=getenv("DATABASE_URL", cls.model_fields["database_url"].default),
            provider=provider,
            log_level=getenv("FIELDLEDGER_LOG_LEVEL", cls.model_fields["log_level"].default),
            api_host=getenv("FIELDLEDGER_API_HOST", cls.model_fields["api_host"].default),
            api_port=int(getenv("FIELDLEDGER_API_PORT", cls.model_fields["api_port"].default)),
            local_demo_enabled=local_demo in {"1", "true", "yes", "on"},
            session_signing_secret=getenv(
                "FIELDLEDGER_SESSION_SIGNING_SECRET",
                cls.model_fields["session_signing_secret"].default,
            ),
            demo_bootstrap_secret=getenv("FIELDLEDGER_DEMO_BOOTSTRAP_SECRET"),
            demo_identities=(
                json.loads(value)
                if (value := getenv("FIELDLEDGER_DEMO_IDENTITIES"))
                else {}
            ),
            session_issuer=getenv(
                "FIELDLEDGER_SESSION_ISSUER", cls.model_fields["session_issuer"].default
            ),
            session_audience=getenv(
                "FIELDLEDGER_SESSION_AUDIENCE",
                cls.model_fields["session_audience"].default,
            ),
            policy_content_hash=getenv(
                "FIELDLEDGER_POLICY_CONTENT_HASH",
                cls.model_fields["policy_content_hash"].default,
            ),
            superdocs_api_key=(
                getenv("SUPERDOCS_API_KEY") if provider == "superdocs" else None
            ),
            provider_timeout_seconds=float(
                getenv(
                    "FIELDLEDGER_PROVIDER_TIMEOUT_SECONDS",
                    cls.model_fields["provider_timeout_seconds"].default,
                )
            ),
            watcher_enabled=getenv("FIELDLEDGER_WATCHER_ENABLED", "false")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"},
            watcher_inbox=Path(
                getenv(
                    "FIELDLEDGER_WATCHER_INBOX",
                    str(cls.model_fields["watcher_inbox"].default),
                )
            ),
            watcher_project_id=(
                UUID(value)
                if (value := getenv("FIELDLEDGER_WATCHER_PROJECT_ID"))
                else None
            ),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings.from_environment()
