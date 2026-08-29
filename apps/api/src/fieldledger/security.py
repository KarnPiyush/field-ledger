from __future__ import annotations

import base64
import binascii
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fieldledger.persistence.tables import project_memberships


class SecurityError(RuntimeError):
    status = 401
    code = "authentication-failed"


class SessionTokenError(SecurityError):
    pass


class ConfirmationTokenError(SecurityError):
    code = "confirmation-token-invalid"


class AuthorizationError(SecurityError):
    status = 403
    code = "forbidden"


class ActorSource(StrEnum):
    UI = "ui"
    MCP = "mcp"
    OPERATOR = "operator"


class ProjectRole(StrEnum):
    REVIEWER = "reviewer"
    AGENT = "agent"
    OPERATOR = "operator"
    VIEWER = "viewer"


class GlobalRole(StrEnum):
    USER = "user"
    OPERATOR = "operator"


class DemoIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: str
    source: ActorSource = ActorSource.UI
    global_role: GlobalRole = GlobalRole.USER


class Actor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: str
    source: ActorSource
    global_role: GlobalRole


class ConfirmationClaims(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: str
    project_id: UUID
    batch_id: UUID
    dependency_digest: str
    nonce: str
    expires_at: datetime


def token_hash(token: str) -> str:
    return f"sha256:{sha256(token.encode()).hexdigest()}"


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as error:
        raise SessionTokenError("token encoding is invalid") from error


class SecurityManager:
    def __init__(
        self,
        *,
        signing_secret: str | None,
        issuer: str,
        audience: str,
        local_demo_enabled: bool,
        demo_bootstrap_secret: str | None = None,
        demo_identities: dict[str, DemoIdentity] | None = None,
    ) -> None:
        identities = demo_identities or {}
        if local_demo_enabled and not signing_secret:
            raise ValueError("demo auth requires an explicit signing secret")
        if local_demo_enabled and not demo_bootstrap_secret:
            raise ValueError("demo auth requires an explicit bootstrap secret")
        if local_demo_enabled and not identities:
            raise ValueError("demo auth requires configured server identities")
        self._secret = signing_secret.encode() if signing_secret else secrets.token_bytes(32)
        self.issuer = issuer
        self.audience = audience
        self.local_demo_enabled = local_demo_enabled
        self._demo_bootstrap_secret = demo_bootstrap_secret
        self._demo_identities = identities

    @staticmethod
    def actor(
        *, actor_id: str, source: ActorSource, global_role: GlobalRole
    ) -> Actor:
        return Actor(
            actor_id=actor_id,
            source=source,
            global_role=global_role,
        )

    def mint_demo_session(
        self,
        *,
        identity: str,
        bootstrap_secret: str,
        now: datetime,
        lifetime: timedelta = timedelta(hours=8),
    ) -> str:
        if not self.local_demo_enabled:
            raise AuthorizationError("local demo session minting is disabled")
        expected_bootstrap = self._demo_bootstrap_secret or ""
        if not hmac.compare_digest(bootstrap_secret, expected_bootstrap):
            raise AuthorizationError("demo bootstrap authentication failed")
        configured = self._demo_identities.get(identity.strip().casefold())
        if configured is None:
            raise AuthorizationError("demo identity is not configured")
        return self._sign(
            {
                "typ": "session",
                "iss": self.issuer,
                "aud": self.audience,
                "sub": configured.actor_id,
                "source": configured.source.value,
                "global_role": configured.global_role.value,
                "iat": int(now.timestamp()),
                "exp": int((now + lifetime).timestamp()),
            }
        )

    def verify_session(self, token: str, *, now: datetime) -> Actor:
        claims = self._verify(token, now=now, error_type=SessionTokenError)
        if claims.get("typ") != "session":
            raise SessionTokenError("token type is invalid")
        try:
            return Actor(
                actor_id=str(claims["sub"]),
                source=ActorSource(str(claims["source"])),
                global_role=GlobalRole(str(claims["global_role"])),
            )
        except (KeyError, ValueError) as error:
            raise SessionTokenError("session claims are invalid") from error

    def mint_confirmation(
        self,
        *,
        actor: Actor,
        project_id: UUID,
        batch_id: UUID,
        dependency_digest: str,
        nonce: str,
        now: datetime,
        lifetime: timedelta,
    ) -> str:
        if actor.source is not ActorSource.UI:
            raise AuthorizationError("only a UI identity can mint human confirmation")
        return self._sign(
            {
                "typ": "confirmation",
                "iss": self.issuer,
                "aud": self.audience,
                "sub": actor.actor_id,
                "source": actor.source.value,
                "project_id": str(project_id),
                "batch_id": str(batch_id),
                "dependency_digest": dependency_digest,
                "nonce": nonce,
                "iat": int(now.timestamp()),
                "exp": int((now + lifetime).timestamp()),
            }
        )

    def verify_confirmation(
        self,
        token: str,
        *,
        actor_id: str | None,
        project_id: UUID,
        batch_id: UUID,
        dependency_digest: str,
        now: datetime,
    ) -> ConfirmationClaims:
        try:
            claims = self._verify(token, now=now, error_type=ConfirmationTokenError)
        except SessionTokenError as error:
            raise ConfirmationTokenError(str(error)) from error
        if claims.get("typ") != "confirmation":
            raise ConfirmationTokenError("confirmation token type is invalid")
        confirmed_actor_id = str(claims.get("sub") or "")
        if actor_id is not None and confirmed_actor_id != actor_id:
            raise ConfirmationTokenError("confirmation token actor does not match")
        if claims.get("project_id") != str(project_id):
            raise ConfirmationTokenError("confirmation token project does not match")
        if claims.get("batch_id") != str(batch_id):
            raise ConfirmationTokenError("confirmation token batch does not match")
        if claims.get("dependency_digest") != dependency_digest:
            raise ConfirmationTokenError("confirmation token digest does not match")
        if claims.get("source") != ActorSource.UI.value:
            raise ConfirmationTokenError("confirmation token is not human UI authority")
        try:
            expires_at = datetime.fromtimestamp(int(claims["exp"]), tz=UTC)
            return ConfirmationClaims(
                actor_id=confirmed_actor_id,
                project_id=project_id,
                batch_id=batch_id,
                dependency_digest=dependency_digest,
                nonce=str(claims["nonce"]),
                expires_at=expires_at,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ConfirmationTokenError("confirmation token claims are invalid") from error

    def _sign(self, claims: dict[str, Any]) -> str:
        header = _encode(b'{"alg":"HS256","typ":"JWT"}')
        payload = _encode(
            json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
        )
        signature = _encode(hmac.new(self._secret, f"{header}.{payload}".encode(), sha256).digest())
        return f"{header}.{payload}.{signature}"

    def _verify(
        self,
        token: str,
        *,
        now: datetime,
        error_type: type[SecurityError],
    ) -> dict[str, Any]:
        try:
            header, payload, signature = token.split(".")
        except ValueError as error:
            raise error_type("token shape is invalid") from error
        expected = _encode(
            hmac.new(self._secret, f"{header}.{payload}".encode(), sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            raise error_type("token signature is invalid")
        try:
            claims = cast(dict[str, Any], json.loads(_decode(payload)))
        except (json.JSONDecodeError, UnicodeDecodeError, SessionTokenError) as error:
            raise error_type("token payload is invalid") from error
        if not isinstance(claims, dict):
            raise error_type("token payload is invalid")
        if claims.get("iss") != self.issuer:
            raise error_type("token issuer is invalid")
        if claims.get("aud") != self.audience:
            raise error_type("token audience is invalid")
        try:
            expires_at = datetime.fromtimestamp(int(claims["exp"]), tz=UTC)
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise error_type("token expiry is invalid") from error
        if now >= expires_at:
            raise error_type("token is expired")
        return claims


class ProjectAuthorizer:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def require(
        self,
        actor: Actor,
        project_id: UUID,
        *,
        roles: set[ProjectRole] | None = None,
        session: AsyncSession | None = None,
    ) -> ProjectRole:
        if session is None:
            async with self.sessions() as owned:
                return await self.require(actor, project_id, roles=roles, session=owned)
        value = await session.scalar(
            select(project_memberships.c.role).where(
                project_memberships.c.project_id == project_id,
                project_memberships.c.actor_id == actor.actor_id,
            )
        )
        if value is None:
            raise AuthorizationError("project membership is required")
        role = ProjectRole(str(value))
        if roles is not None and role not in roles:
            raise AuthorizationError("project role is not authorized")
        return role

    async def require_actor_id(
        self,
        actor_id: str,
        project_id: UUID,
        *,
        roles: set[ProjectRole] | None = None,
        session: AsyncSession,
    ) -> ProjectRole:
        value = await session.scalar(
            select(project_memberships.c.role).where(
                project_memberships.c.project_id == project_id,
                project_memberships.c.actor_id == actor_id,
            )
        )
        if value is None:
            raise AuthorizationError("project membership is required")
        role = ProjectRole(str(value))
        if roles is not None and role not in roles:
            raise AuthorizationError("project role is not authorized")
        return role
