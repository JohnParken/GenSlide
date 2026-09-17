"""Runtime configuration for the AgentScope API process."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import math
from urllib.parse import urlsplit


def _number(name: str, default: str, *, integer: bool = False) -> int | float:
    value = os.getenv(name, default)
    try:
        parsed = int(value) if integer else float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be a positive number")
    return parsed


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str = "production"
    engine_name: str = "agentscope"
    bff_url: str | None = None
    service_token: str | None = field(default=None, repr=False)
    model_base_url: str | None = None
    model_api_key: str | None = field(default=None, repr=False)
    model_name: str | None = None
    bff_timeout_seconds: float = 10.0
    control_timeout_seconds: float = 5.0
    renew_interval_seconds: float = 10.0
    generation_timeout_seconds: float = 1800.0
    planning_timeout_seconds: float = 180.0
    generation_concurrency: int = 2
    planning_concurrency: int = 2
    cpu_concurrency: int = 2
    transfer_concurrency: int = 2
    max_download_bytes: int = 20 * 1024 * 1024
    max_total_download_bytes: int = 64 * 1024 * 1024
    max_artifact_bytes: int = 20 * 1024 * 1024
    max_material_chars: int = 100_000
    max_memory_bytes: int = 32 * 1024
    max_request_bytes: int = 128 * 1024
    max_sessions: int = 100
    disconnect_poll_seconds: float = 0.25
    sse_heartbeat_seconds: float = 15.0

    def __post_init__(self) -> None:
        if self.environment not in {"development", "test", "production"}:
            raise ValueError("GENSLIDE_ENV must be development, test, or production")
        if self.engine_name != "agentscope":
            raise ValueError("engine_name is fixed by this package")
        if self.bff_url:
            parsed = urlsplit(self.bff_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError("GENSLIDE_BFF_URL must be an absolute HTTP(S) URL without credentials")
            if parsed.query or parsed.fragment:
                raise ValueError("GENSLIDE_BFF_URL must not contain a query or fragment")
        positive = (
            self.bff_timeout_seconds,
            self.control_timeout_seconds,
            self.renew_interval_seconds,
            self.generation_timeout_seconds,
            self.planning_timeout_seconds,
            self.generation_concurrency,
            self.planning_concurrency,
            self.cpu_concurrency,
            self.transfer_concurrency,
            self.max_download_bytes,
            self.max_total_download_bytes,
            self.max_artifact_bytes,
            self.max_material_chars,
            self.max_memory_bytes,
            self.max_request_bytes,
            self.max_sessions,
            self.disconnect_poll_seconds,
            self.sse_heartbeat_seconds,
        )
        if any(not math.isfinite(value) or value <= 0 for value in positive):
            raise ValueError("runtime limits must be positive")
        for variable in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
            configured_workers = os.getenv(variable)
            if configured_workers is not None:
                try:
                    worker_count = int(configured_workers)
                except ValueError as exc:
                    raise ValueError(f"{variable} must be a positive integer") from exc
                if worker_count <= 0 or worker_count > 1:
                    raise ValueError("GenSlide must run with exactly one API worker per Pod")
        if self.environment == "production":
            missing = []
            if not self.bff_url:
                missing.append("GENSLIDE_BFF_URL")
            if not self.service_token or len(self.service_token) < 32:
                missing.append("GENSLIDE_SERVICE_TOKEN (at least 32 characters)")
            if not self.model_base_url:
                missing.append("MODEL_BASE_URL")
            if not self.model_api_key:
                missing.append("MODEL_API_KEY")
            if not self.model_name:
                missing.append("MODEL_NAME")
            if missing:
                raise ValueError("production configuration is incomplete: " + ", ".join(missing))

    @property
    def service_auth_enabled(self) -> bool:
        return self.environment == "production" or bool(self.service_token)

    def require_model_configuration(self) -> None:
        missing = [
            name
            for name, value in (
                ("MODEL_BASE_URL", self.model_base_url),
                ("MODEL_API_KEY", self.model_api_key),
                ("MODEL_NAME", self.model_name),
            )
            if not value
        ]
        if missing:
            raise ValueError("model configuration is incomplete: " + ", ".join(missing))

    @classmethod
    def from_env(cls, *, require_model: bool = False) -> "Settings":
        settings = cls(
            environment=os.getenv("GENSLIDE_ENV", "production").strip().lower(),
            engine_name="agentscope",
            bff_url=os.getenv("GENSLIDE_BFF_URL") or None,
            service_token=os.getenv("GENSLIDE_SERVICE_TOKEN") or None,
            model_base_url=os.getenv("MODEL_BASE_URL") or None,
            model_api_key=os.getenv("MODEL_API_KEY") or None,
            model_name=os.getenv("MODEL_NAME") or None,
            bff_timeout_seconds=float(_number("GENSLIDE_BFF_TIMEOUT_SECONDS", "10")),
            control_timeout_seconds=float(_number("GENSLIDE_CONTROL_TIMEOUT_SECONDS", "5")),
            renew_interval_seconds=float(_number("GENSLIDE_RENEW_INTERVAL_SECONDS", "10")),
            generation_timeout_seconds=float(_number("GENSLIDE_GENERATION_TIMEOUT_SECONDS", "1800")),
            planning_timeout_seconds=float(_number("GENSLIDE_PLANNING_TIMEOUT_SECONDS", "180")),
            generation_concurrency=int(_number("GENSLIDE_GENERATION_CONCURRENCY", "2", integer=True)),
            planning_concurrency=int(_number("GENSLIDE_PLANNING_CONCURRENCY", "2", integer=True)),
            cpu_concurrency=int(_number("GENSLIDE_CPU_CONCURRENCY", "2", integer=True)),
            transfer_concurrency=int(_number("GENSLIDE_TRANSFER_CONCURRENCY", "2", integer=True)),
            max_download_bytes=int(_number("GENSLIDE_MAX_DOWNLOAD_BYTES", str(20 * 1024 * 1024), integer=True)),
            max_total_download_bytes=int(_number("GENSLIDE_MAX_TOTAL_DOWNLOAD_BYTES", str(64 * 1024 * 1024), integer=True)),
            max_artifact_bytes=int(_number("GENSLIDE_MAX_ARTIFACT_BYTES", str(20 * 1024 * 1024), integer=True)),
            max_material_chars=int(_number("GENSLIDE_MAX_MATERIAL_CHARS", "100000", integer=True)),
            max_memory_bytes=int(_number("GENSLIDE_MAX_MEMORY_BYTES", str(32 * 1024), integer=True)),
            max_request_bytes=int(_number("GENSLIDE_MAX_REQUEST_BYTES", str(128 * 1024), integer=True)),
            max_sessions=int(_number("GENSLIDE_MAX_SESSIONS", "100", integer=True)),
            disconnect_poll_seconds=float(_number("GENSLIDE_DISCONNECT_POLL_SECONDS", "0.25")),
            sse_heartbeat_seconds=float(_number("GENSLIDE_SSE_HEARTBEAT_SECONDS", "15")),
        )
        if require_model or settings.environment == "production":
            settings.require_model_configuration()
        return settings
