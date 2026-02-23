"""Runtime configuration helpers for repl_mcp."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional


IdleAction = Literal["stop", "archive", "delete"]
Transport = Literal["stdio", "sse"]


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean environment variable."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    """Parse an integer environment variable with fallback."""
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """Parse a float environment variable with fallback."""
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class RuntimeConfig:
    """Runtime feature and sandbox settings loaded from environment."""

    daytona_api_url: Optional[str] = None
    daytona_api_key: Optional[str] = None
    daytona_target: Optional[str] = None
    daytona_snapshot: Optional[str] = None
    daytona_image: Optional[str] = None
    session_idle_timeout_minutes: int = 30
    session_idle_action: IdleAction = "stop"
    feature_bash_enabled: bool = False
    feature_upload_enabled: bool = True
    exec_timeout_seconds: float = 120.0
    bash_timeout_seconds: float = 30.0
    max_upload_bytes: int = 10 * 1024 * 1024
    session_header_name: str = "x-chat-session-id"
    session_sweep_interval_seconds: int = 60

    @property
    def daytona_enabled(self) -> bool:
        """Enable Daytona mode when both API URL and API key are configured."""
        return bool(self.daytona_api_url and self.daytona_api_key)

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        """Build runtime configuration from environment variables."""
        idle_action_raw = os.getenv("SESSION_IDLE_ACTION", "stop").strip().lower()
        session_idle_action: IdleAction = "stop"
        if idle_action_raw in {"stop", "archive", "delete"}:
            session_idle_action = idle_action_raw  # type: ignore[assignment]

        return cls(
            daytona_api_url=os.getenv("DAYTONA_API_URL"),
            daytona_api_key=os.getenv("DAYTONA_API_KEY"),
            daytona_target=os.getenv("DAYTONA_TARGET"),
            daytona_snapshot=os.getenv("DAYTONA_SNAPSHOT"),
            daytona_image=os.getenv("DAYTONA_IMAGE"),
            session_idle_timeout_minutes=_env_int("SESSION_IDLE_TIMEOUT_MINUTES", 30),
            session_idle_action=session_idle_action,
            feature_bash_enabled=_env_bool("FEATURE_BASH_ENABLED", False),
            feature_upload_enabled=_env_bool("FEATURE_UPLOAD_ENABLED", True),
            exec_timeout_seconds=_env_float("EXEC_TIMEOUT_SECONDS", 120.0),
            bash_timeout_seconds=_env_float("BASH_TIMEOUT_SECONDS", 30.0),
            max_upload_bytes=_env_int("MAX_UPLOAD_BYTES", 10 * 1024 * 1024),
            session_header_name=os.getenv("SESSION_HEADER_NAME", "x-chat-session-id"),
            session_sweep_interval_seconds=_env_int("SESSION_SWEEP_INTERVAL_SECONDS", 60),
        )


@dataclass(frozen=True)
class ServerConfig:
    """Resolved server startup config with CLI > ENV > defaults precedence."""

    transport: Transport = "stdio"
    host: str = "0.0.0.0"
    port: int = 8000
    config_path: Path = Path(".mcp.json")
    autoconnect: bool = True
    debug: bool = False

    @classmethod
    def from_sources(cls, args: argparse.Namespace) -> "ServerConfig":
        """
        Resolve startup config with precedence: CLI > ENV > defaults.

        The parser should use default=None for the supported CLI arguments so we can
        distinguish explicit CLI values from omitted ones.
        """
        transport = args.transport or os.getenv("REPL_MCP_TRANSPORT", "stdio")
        host = args.host or os.getenv("REPL_MCP_HOST", "0.0.0.0")

        if args.port is not None:
            port = args.port
        else:
            port = _env_int("REPL_MCP_PORT", 8000)

        if args.config is not None:
            config_path = args.config
        else:
            config_path = Path(os.getenv("REPL_MCP_CONFIG", ".mcp.json"))

        if args.no_autoconnect:
            autoconnect = False
        else:
            autoconnect = _env_bool("REPL_MCP_AUTOCONNECT", True)

        debug = args.debug or _env_bool("REPL_MCP_DEBUG", False)

        if transport not in {"stdio", "sse"}:
            transport = "stdio"

        return cls(
            transport=transport,  # type: ignore[arg-type]
            host=host,
            port=port,
            config_path=config_path,
            autoconnect=autoconnect,
            debug=debug,
        )
