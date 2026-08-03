"""
Multi-scope MCP server discovery (Claude Code config layout).

The bridge used to see only `<cwd>/.mcp.json`, which made every user-scope
("global") server invisible to REPL code. This module reads the same four
on-disk sources Claude Code itself merges:

  local    ~/.claude.json  -> .projects["<cwd>"].mcpServers
  project  <cwd>/.mcp.json -> .mcpServers            (or --config override)
  user     ~/.claude.json  -> .mcpServers
  plugin   ~/.claude/plugins/installed_plugins.json  -> each enabled plugin's
           <installPath>/.claude-plugin/plugin.json -> .mcpServers

Precedence on a bare-name collision is local > project > user > plugin (the
first three mirror Claude Code; plugins rank last because their host namespace
is flattened here). A shadowed entry stays reachable under its qualified name.

Everything here is read-only and non-raising: a corrupt or missing file
degrades that one source to empty, never the whole discovery. Config *values*
(tokens in `env`/`headers`) must never reach logs or help output — use
`redact()` for anything user-facing.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

Scope = Literal["local", "project", "user", "plugin"]

ALL_SCOPES: tuple[Scope, ...] = ("local", "project", "user", "plugin")

# Lower rank wins a bare-name collision.
_RANK: dict[str, int] = {"local": 0, "project": 1, "user": 2, "plugin": 3}

# Guard: every server we spawn inherits this, and discovery returns nothing when
# it is set in our own environment. Caps bridge recursion at depth 1 even if the
# self-exclusion heuristics below ever miss.
NO_BRIDGE_ENV = "REPL_MCP_NO_BRIDGE"

_MAX_CONFIG_BYTES = 32 * 1024 * 1024
_SELF_NAMES = {"python-repl"}
_SELF_TOKENS = {"repl-mcp", "repl_mcp"}
_ENV_REF = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveredServer:
    """One MCP server entry plus where it came from."""

    name: str
    config: dict           # RAW entry; `${VAR}` expansion happens at connect time
    scope: Scope
    origin: Path
    plugin: Optional[str] = None        # "visual-runtime@visual-mcp"
    plugin_root: Optional[Path] = None  # feeds ${CLAUDE_PLUGIN_ROOT}

    @property
    def qualified_name(self) -> str:
        """Collision-proof name (used when a lower-rank entry is shadowed)."""
        if self.plugin:
            return f"plugin:{self.plugin}:{self.name}"
        return f"{self.scope}:{self.name}"

    @property
    def label(self) -> str:
        """Short human label for help() output."""
        return f"plugin {self.plugin}" if self.plugin else self.scope


@dataclass(frozen=True)
class DiscoveryResult:
    """Merged view of every configured server the bridge may reach."""

    servers: dict[str, DiscoveredServer] = field(default_factory=dict)
    excluded: dict[str, str] = field(default_factory=dict)  # qualified name -> reason
    sources: tuple[Path, ...] = ()

    @property
    def names(self) -> list[str]:
        return sorted(self.servers)


# ---------------------------------------------------------------------------
# file readers (never raise)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Roots:
    claude_json: Path
    config_dir: Path


def _resolve_roots(home: Optional[Path]) -> _Roots:
    """
    Locate `~/.claude.json` and `~/.claude/`.

    `home` is the test seam. `CLAUDE_CONFIG_DIR` (Claude Code's own override)
    is honored, falling back to the real `~/.claude.json` when that directory
    holds no config of its own.
    """
    if home is not None:
        home = Path(home)
        return _Roots(home / ".claude.json", home / ".claude")

    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_dir:
        config_dir = Path(env_dir).expanduser()
        claude_json = config_dir / ".claude.json"
        if not claude_json.is_file():
            fallback = Path.home() / ".claude.json"
            if fallback.is_file():
                claude_json = fallback
        return _Roots(claude_json, config_dir)

    real_home = Path.home()
    return _Roots(real_home / ".claude.json", real_home / ".claude")


def _read_json(path: Path) -> dict:
    """Read a JSON object, degrading to {} on anything unexpected."""
    try:
        if not path.is_file():
            return {}
        if path.stat().st_size > _MAX_CONFIG_BYTES:
            logger.warning("Skipping oversized MCP config: %s", path)
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # unreadable, malformed, permission denied, ...
        logger.debug("Unreadable MCP config %s: %s", path, type(e).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def _servers_of(obj: Mapping[str, Any]) -> dict[str, dict]:
    """Extract a well-formed `mcpServers` map."""
    servers = obj.get("mcpServers")
    if not isinstance(servers, dict):
        return {}
    return {
        str(name): dict(cfg)
        for name, cfg in servers.items()
        if isinstance(cfg, dict)
    }


def _project_keys(cwd: Path) -> list[str]:
    """
    Candidate keys for `~/.claude.json` -> `.projects[...]`.

    The key is path-exact, and macOS hands out both `/tmp` and `/private/tmp`
    for the same directory. Never walk up to parents — that would import a
    parent project's local servers.
    """
    keys: list[str] = []
    env_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_dir:
        keys.append(env_dir)
    keys.append(str(cwd))
    try:
        keys.append(str(cwd.resolve()))
    except OSError:
        pass

    seen: set[str] = set()
    return [k for k in keys if not (k in seen or seen.add(k))]


def _project_entry(claude_json: Mapping[str, Any], cwd: Path) -> dict:
    projects = claude_json.get("projects")
    if not isinstance(projects, dict):
        return {}
    for key in _project_keys(cwd):
        entry = projects.get(key)
        if isinstance(entry, dict):
            return entry
    return {}


def _settings_paths(config_dir: Path, cwd: Path) -> list[Path]:
    return [
        config_dir / "settings.json",
        cwd / ".claude" / "settings.json",
        cwd / ".claude" / "settings.local.json",
    ]


def _denied_names(
    claude_json: Mapping[str, Any], cwd: Path, config_dir: Path
) -> set[str]:
    """
    Servers explicitly turned off via `disabledMcpjsonServers`.

    Deny-only on purpose: honoring the *enable* list would silently drop
    project servers that the bridge connects to today.
    """
    denied: set[str] = set()

    def collect(obj: Mapping[str, Any]) -> None:
        values = obj.get("disabledMcpjsonServers")
        if isinstance(values, list):
            denied.update(str(v) for v in values)

    collect(_project_entry(claude_json, cwd))
    for path in _settings_paths(config_dir, cwd):
        collect(_read_json(path))
    return denied


def _enabled_plugins(config_dir: Path, cwd: Path) -> dict[str, bool]:
    merged: dict[str, bool] = {}
    for path in _settings_paths(config_dir, cwd):
        values = _read_json(path).get("enabledPlugins")
        if isinstance(values, dict):
            merged.update({str(k): bool(v) for k, v in values.items()})
    return merged


def _plugin_candidates(
    config_dir: Path, cwd: Path, sources: list[Path]
) -> list[DiscoveredServer]:
    index_path = config_dir / "plugins" / "installed_plugins.json"
    sources.append(index_path)
    plugins = _read_json(index_path).get("plugins")
    if not isinstance(plugins, dict):
        return []

    enabled = _enabled_plugins(config_dir, cwd)
    project_keys = _project_keys(cwd)
    found: list[DiscoveredServer] = []

    for plugin_id, installs in plugins.items():
        if not enabled.get(str(plugin_id), False):
            continue
        if not isinstance(installs, list):
            continue

        for record in installs:
            if not isinstance(record, dict):
                continue
            # Project-scoped installs belong to one directory only
            if record.get("scope") == "project":
                project_path = record.get("projectPath")
                if not project_path or str(project_path) not in project_keys:
                    continue

            install_path = record.get("installPath")
            if not install_path:
                continue
            root = Path(str(install_path))

            manifest = root / ".claude-plugin" / "plugin.json"
            sources.append(manifest)
            entries = _servers_of(_read_json(manifest))

            # Some plugins also ship a bare .mcp.json (wrapper- or map-shaped);
            # the manifest wins for any name it already declares.
            bare = root / ".mcp.json"
            if bare.is_file():
                sources.append(bare)
                raw = _read_json(bare)
                extra = _servers_of(raw)
                if not extra:
                    extra = {
                        str(name): dict(cfg)
                        for name, cfg in raw.items()
                        if isinstance(cfg, dict) and ("command" in cfg or "url" in cfg)
                    }
                for name, cfg in extra.items():
                    entries.setdefault(name, cfg)

            for name, cfg in entries.items():
                found.append(DiscoveredServer(
                    name=name,
                    config=cfg,
                    scope="plugin",
                    origin=manifest,
                    plugin=str(plugin_id),
                    plugin_root=root,
                ))

    return found


# ---------------------------------------------------------------------------
# self-exclusion
# ---------------------------------------------------------------------------


def is_self_server(name: str, config: Mapping[str, Any]) -> bool:
    """
    True when an entry would spawn *this* REPL server again.

    Without this, the plugin-scope `python-repl` entry makes
    `mcp.call('python-repl', ...)` fork a nested REPL bridge recursively.

    Matching is deliberately narrow — whole argv elements, plus the command's
    basename — so that a path like `uv run --directory /x/repl_mcp other-server`
    is NOT mistaken for us.
    """
    if str(name).strip().lower() in _SELF_NAMES:
        return True

    command = config.get("command")
    if isinstance(command, str):
        basename = os.path.basename(command.strip()).lower()
        if basename in _SELF_TOKENS:
            return True

    args = config.get("args")
    if isinstance(args, list):
        for arg in args:
            if not isinstance(arg, str):
                continue
            token = arg.strip().lower()
            # exact entrypoint (`uv run repl-mcp`, `uvx --from git+... repl-mcp`)
            # or module form (`python -m repl_mcp.repl_mcp_server`)
            if token in _SELF_TOKENS or token.startswith("repl_mcp."):
                return True

    return False


# ---------------------------------------------------------------------------
# expansion / redaction
# ---------------------------------------------------------------------------


def expand_value(
    value: str,
    *,
    overrides: Optional[Mapping[str, str]] = None,
    unresolved: Optional[list[str]] = None,
) -> str:
    """
    Expand `${VAR}` / `${VAR:-default}` anywhere in a string.

    `overrides` wins over `os.environ` — that is how `${CLAUDE_PLUGIN_ROOT}`
    gets the *target* plugin's root. Reading it from the environment would hand
    out our own plugin root, silently starting the wrong binary.
    """

    def repl(m: "re.Match[str]") -> str:
        var, default = m.group(1), m.group(2)
        if overrides and var in overrides:
            return overrides[var]
        if var in os.environ:
            return os.environ[var]
        if default is not None:
            return default
        if unresolved is not None and var not in unresolved:
            unresolved.append(var)
        return ""

    return _ENV_REF.sub(repl, value)


def expand_config(
    config: Mapping[str, Any],
    *,
    overrides: Optional[Mapping[str, str]] = None,
) -> tuple[dict, list[str]]:
    """
    Expand `${VAR}` references in a server entry.

    Returns `(expanded, unresolved)` where `unresolved` lists variables that
    were referenced from `command`/`args`/`url` with no value and no default —
    those must fail the connect rather than exec an empty command.

    Unset variables inside `env`/`headers` are non-fatal (they expand to "",
    matching Claude Code) and are not reported.
    """
    fatal: list[str] = []
    out = dict(config)

    def hard(value: Any) -> Any:
        return expand_value(value, overrides=overrides, unresolved=fatal) \
            if isinstance(value, str) else value

    def soft(value: Any) -> Any:
        return expand_value(value, overrides=overrides) if isinstance(value, str) else value

    if "command" in out:
        out["command"] = hard(out["command"])
    if isinstance(out.get("args"), list):
        out["args"] = [hard(a) for a in out["args"]]
    if "url" in out:
        out["url"] = hard(out["url"])
    if isinstance(out.get("env"), dict):
        out["env"] = {k: soft(v) for k, v in out["env"].items()}
    if isinstance(out.get("headers"), dict):
        out["headers"] = {k: soft(v) for k, v in out["headers"].items()}

    return out, fatal


def spawn_env(config: Mapping[str, Any]) -> dict[str, str]:
    """Environment for a child MCP server: inherited + entry `env` + the recursion guard."""
    env = os.environ.copy()
    entry_env = config.get("env")
    if isinstance(entry_env, dict):
        env.update({str(k): str(v) for k, v in entry_env.items()})
    env[NO_BRIDGE_ENV] = "1"
    return env


def redact(config: Mapping[str, Any]) -> dict:
    """Copy of a server entry with secret-bearing values replaced."""
    out = dict(config)
    for key in ("env", "headers"):
        values = out.get(key)
        if isinstance(values, dict):
            out[key] = {k: "<set>" for k in values}
    return out


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def discover_servers(
    *,
    cwd: Optional[Path] = None,
    home: Optional[Path] = None,
    project_config_path: Optional[Path] = None,
    scopes: Sequence[str] = ALL_SCOPES,
) -> DiscoveryResult:
    """
    Merge every configured MCP server the bridge may reach.

    Args:
        cwd: project directory (defaults to the process cwd)
        home: home directory override (test seam)
        project_config_path: overrides `<cwd>/.mcp.json` (the `--config` flag)
        scopes: subset of ("local", "project", "user", "plugin")

    Returns:
        DiscoveryResult — nothing is connected or spawned here.
    """
    if os.environ.get(NO_BRIDGE_ENV):
        # We are ourselves running as a child of another REPL bridge.
        return DiscoveryResult(
            excluded={"*": f"nested bridge disabled ({NO_BRIDGE_ENV}=1)"}
        )

    cwd = Path(cwd) if cwd is not None else Path.cwd()
    scopes = tuple(s for s in scopes if s in _RANK)
    roots = _resolve_roots(home)

    sources: list[Path] = [roots.claude_json]
    claude_json = _read_json(roots.claude_json)
    denied = _denied_names(claude_json, cwd, roots.config_dir)

    candidates: list[DiscoveredServer] = []

    if "user" in scopes:
        for name, cfg in _servers_of(claude_json).items():
            candidates.append(DiscoveredServer(
                name=name, config=cfg, scope="user", origin=roots.claude_json,
            ))

    if "local" in scopes:
        for name, cfg in _servers_of(_project_entry(claude_json, cwd)).items():
            candidates.append(DiscoveredServer(
                name=name, config=cfg, scope="local", origin=roots.claude_json,
            ))

    if "project" in scopes:
        path = Path(project_config_path) if project_config_path else cwd / ".mcp.json"
        sources.append(path)
        for name, cfg in _servers_of(_read_json(path)).items():
            candidates.append(DiscoveredServer(
                name=name, config=cfg, scope="project", origin=path,
            ))

    if "plugin" in scopes:
        candidates.extend(_plugin_candidates(roots.config_dir, cwd, sources))

    servers: dict[str, DiscoveredServer] = {}
    excluded: dict[str, str] = {}

    for candidate in sorted(candidates, key=lambda c: _RANK[c.scope]):
        if is_self_server(candidate.name, candidate.config):
            excluded[candidate.qualified_name] = "self (would spawn a nested REPL bridge)"
            continue
        if candidate.scope == "project" and candidate.name in denied:
            excluded[candidate.qualified_name] = "disabled via disabledMcpjsonServers"
            continue
        if candidate.name in servers:
            # Shadowed by a higher-precedence scope, but still callable
            servers[candidate.qualified_name] = candidate
            continue
        servers[candidate.name] = candidate

    return DiscoveryResult(servers=servers, excluded=excluded, sources=tuple(sources))


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


SCOPE_NOTE = (
    "Servers are discovered from Claude Code config — local (~/.claude.json -> "
    "projects[cwd]), project (./.mcp.json), user (~/.claude.json -> mcpServers) "
    "and enabled plugins — and connect ON DEMAND: the first mcp.call() for a "
    "server spawns it (~1-3s), later calls are warm.\n"
    "Not reachable here: claude.ai host connectors (Notion/Gmail/Drive/"
    "claude-in-chrome — server-managed, nothing on disk) and remote servers whose "
    "credentials Claude Code holds via OAuth. Call those tools directly."
)


def format_registry(
    result: Optional[DiscoveryResult],
    *,
    connected: Iterable[str] = (),
    failed: Optional[Mapping[str, str]] = None,
    tool_counts: Optional[Mapping[str, int]] = None,
) -> str:
    """
    Render the available-server table for `mcp.help()`.

    Connects nothing: idle servers are listed with their scope so an agent can
    discover `telegram-mcp` exists before paying to start it.
    """
    connected_set = set(connected)
    failed = dict(failed or {})
    counts = dict(tool_counts or {})

    if result is None or not result.servers:
        lines = ["No MCP servers discovered."]
        if result and result.excluded:
            lines.append("")
            lines.append("Excluded:")
            for name, reason in result.excluded.items():
                lines.append(f"  {name}: {reason}")
        lines.extend(["", SCOPE_NOTE])
        return "\n".join(lines)

    lines = ["Available MCP servers (connect on first use):"]
    for name in sorted(result.servers):
        server = result.servers[name]
        if name in connected_set:
            suffix = f"{counts[name]} tools" if name in counts else "connected"
            status = f"✓ {suffix}"
        elif name in failed:
            status = f"✗ {failed[name]}"
        else:
            status = "· not connected yet"
        lines.append(f"  {name} [{server.label}] {status}")

    if result.excluded:
        lines.append("")
        lines.append("Excluded:")
        for name, reason in result.excluded.items():
            lines.append(f"  {name}: {reason}")

    lines.append("")
    lines.append("Use mcp.help('server') to list its tools (connects it)")
    lines.append("Call tools via mcp.call('server', 'tool', **args)")
    lines.append("")
    lines.append(SCOPE_NOTE)
    return "\n".join(lines)
