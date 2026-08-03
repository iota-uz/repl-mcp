"""
Multi-scope MCP config discovery.

Everything here is hermetic: a fake `home` and `cwd` are passed explicitly, so
the real `~/.claude.json` (222KB of OAuth state on a dev machine) is never
touched and never leaks into assertions.
"""

import json
from pathlib import Path

import pytest

from repl_mcp.mcp_config import (
    ALL_SCOPES,
    NO_BRIDGE_ENV,
    discover_servers,
    expand_config,
    format_registry,
    is_self_server,
    redact,
    spawn_env,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Discovery reads a few env vars — keep the host session out of the tests."""
    for var in (NO_BRIDGE_ENV, "CLAUDE_PROJECT_DIR", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)


def write_json(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def layout(tmp_path):
    """A fake ~/ and project dir, empty until a test fills them in."""
    home = tmp_path / "home"
    cwd = tmp_path / "proj"
    (home / ".claude").mkdir(parents=True)
    cwd.mkdir()
    return home, cwd


def install_plugin(
    home: Path,
    plugin_id: str,
    servers: dict,
    *,
    enabled: bool = True,
    scope: str = "user",
    project_path: str | None = None,
    bare_servers: dict | None = None,
) -> Path:
    """Materialize an installed plugin the way Claude Code lays it out."""
    root = home / ".claude" / "plugins" / "cache" / plugin_id
    write_json(root / ".claude-plugin" / "plugin.json",
               {"name": plugin_id, "mcpServers": servers})
    if bare_servers is not None:
        write_json(root / ".mcp.json", bare_servers)

    index_path = home / ".claude" / "plugins" / "installed_plugins.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {
        "version": 2, "plugins": {}
    }
    record = {"scope": scope, "installPath": str(root), "version": "1.0.0"}
    if project_path is not None:
        record["projectPath"] = project_path
    index["plugins"][plugin_id] = [record]
    write_json(index_path, index)

    settings_path = home / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    settings.setdefault("enabledPlugins", {})[plugin_id] = enabled
    write_json(settings_path, settings)
    return root


class TestScopes:
    def test_user_scope_is_discovered(self, layout):
        home, cwd = layout
        write_json(home / ".claude.json", {
            "mcpServers": {"telegram-mcp": {"command": "uv", "args": ["run", "main.py"]}}
        })

        result = discover_servers(cwd=cwd, home=home)

        assert result.names == ["telegram-mcp"]
        assert result.servers["telegram-mcp"].scope == "user"

    def test_all_four_scopes_merge(self, layout):
        home, cwd = layout
        write_json(home / ".claude.json", {
            "mcpServers": {"u": {"command": "u"}},
            "projects": {str(cwd): {"mcpServers": {"l": {"command": "l"}}}},
        })
        write_json(cwd / ".mcp.json", {"mcpServers": {"p": {"command": "p"}}})
        install_plugin(home, "viz@market", {"g": {"command": "g"}})

        result = discover_servers(cwd=cwd, home=home)

        assert result.names == ["g", "l", "p", "u"]
        assert result.servers["l"].scope == "local"
        assert result.servers["p"].scope == "project"
        assert result.servers["g"].scope == "plugin"
        assert result.servers["g"].plugin == "viz@market"

    def test_precedence_local_over_project_over_user_over_plugin(self, layout):
        home, cwd = layout
        write_json(home / ".claude.json", {
            "mcpServers": {"dup": {"command": "user"}},
            "projects": {str(cwd): {"mcpServers": {"dup": {"command": "local"}}}},
        })
        write_json(cwd / ".mcp.json", {"mcpServers": {"dup": {"command": "project"}}})
        install_plugin(home, "viz@market", {"dup": {"command": "plugin"}})

        result = discover_servers(cwd=cwd, home=home)

        assert result.servers["dup"].config["command"] == "local"
        # Shadowed entries stay reachable under a qualified name
        assert result.servers["project:dup"].config["command"] == "project"
        assert result.servers["user:dup"].config["command"] == "user"
        assert result.servers["plugin:viz@market:dup"].config["command"] == "plugin"

    def test_scope_subset_is_honored(self, layout):
        home, cwd = layout
        write_json(home / ".claude.json", {"mcpServers": {"u": {"command": "u"}}})
        write_json(cwd / ".mcp.json", {"mcpServers": {"p": {"command": "p"}}})

        result = discover_servers(cwd=cwd, home=home, scopes=("project",))

        assert result.names == ["p"]

    def test_project_config_override(self, layout):
        home, cwd = layout
        override = write_json(cwd / ".mcp.dev.json", {"mcpServers": {"dev": {"command": "d"}}})
        write_json(cwd / ".mcp.json", {"mcpServers": {"prod": {"command": "p"}}})

        result = discover_servers(cwd=cwd, home=home, project_config_path=override)

        assert result.names == ["dev"]

    def test_local_scope_matches_resolved_cwd(self, layout):
        """macOS hands out /tmp and /private/tmp for the same directory."""
        home, cwd = layout
        write_json(home / ".claude.json", {
            "projects": {str(cwd.resolve()): {"mcpServers": {"l": {"command": "l"}}}}
        })

        result = discover_servers(cwd=cwd, home=home)

        assert result.names == ["l"]


class TestGates:
    def test_disabled_project_server_is_excluded(self, layout):
        home, cwd = layout
        write_json(home / ".claude.json", {
            "projects": {str(cwd): {"disabledMcpjsonServers": ["p"]}}
        })
        write_json(cwd / ".mcp.json", {"mcpServers": {"p": {"command": "p"}}})

        result = discover_servers(cwd=cwd, home=home)

        assert result.names == []
        assert "disabledMcpjsonServers" in result.excluded["project:p"]

    def test_disabled_list_also_read_from_project_settings(self, layout):
        home, cwd = layout
        write_json(cwd / ".claude" / "settings.local.json",
                   {"disabledMcpjsonServers": ["p"]})
        write_json(cwd / ".mcp.json", {"mcpServers": {"p": {"command": "p"}}})

        assert discover_servers(cwd=cwd, home=home).names == []

    def test_enable_list_is_not_enforced(self, layout):
        """Deny-only: honoring the enable list would drop servers that work today."""
        home, cwd = layout
        write_json(cwd / ".claude" / "settings.local.json",
                   {"enabledMcpjsonServers": ["something-else"]})
        write_json(cwd / ".mcp.json", {"mcpServers": {"p": {"command": "p"}}})

        assert discover_servers(cwd=cwd, home=home).names == ["p"]

    def test_disabled_plugin_is_skipped(self, layout):
        home, cwd = layout
        install_plugin(home, "viz@market", {"g": {"command": "g"}}, enabled=False)

        assert discover_servers(cwd=cwd, home=home).names == []

    def test_project_scoped_plugin_only_in_its_project(self, layout):
        home, cwd = layout
        other = cwd.parent / "other"
        other.mkdir()
        install_plugin(home, "lsp@market", {"g": {"command": "g"}},
                       scope="project", project_path=str(cwd))

        assert discover_servers(cwd=cwd, home=home).names == ["g"]
        assert discover_servers(cwd=other, home=home).names == []

    def test_plugin_manifest_wins_over_bare_mcp_json(self, layout):
        home, cwd = layout
        install_plugin(
            home, "viz@market",
            {"viz": {"command": "manifest"}},
            bare_servers={"viz": {"command": "bare"}, "extra": {"command": "e"}},
        )

        result = discover_servers(cwd=cwd, home=home)

        assert result.servers["viz"].config["command"] == "manifest"
        assert result.servers["extra"].config["command"] == "e"


class TestSelfExclusion:
    @pytest.mark.parametrize("name,config", [
        ("python-repl", {"command": "uvx", "args": ["repl-mcp"]}),
        ("Python-REPL", {"command": "x"}),
        ("anything", {"command": "uv", "args": ["run", "repl-mcp"]}),
        ("anything", {"command": "uvx", "args": [
            "--from", "git+https://github.com/iota-uz/repl-mcp@v2.0.0",
            "repl-mcp", "--transport", "stdio"]}),
        ("anything", {"command": "/usr/local/bin/repl-mcp"}),
        ("anything", {"command": "python", "args": ["-m", "repl_mcp.repl_mcp_server"]}),
    ])
    def test_detects_self(self, name, config):
        assert is_self_server(name, config) is True

    @pytest.mark.parametrize("name,config", [
        # A path that merely contains the repo name is NOT us
        ("other", {"command": "uv", "args": ["run", "--directory",
                                             "/Users/x/toys/repl_mcp", "other-server"]}),
        ("github", {"command": "github-mcp-server", "args": ["stdio"]}),
        ("telegram-mcp", {"command": "uv", "args": ["--directory", "/x/telegram-mcp",
                                                    "run", "main.py"]}),
    ])
    def test_does_not_false_positive(self, name, config):
        assert is_self_server(name, config) is False

    def test_plugin_self_entry_is_excluded_from_discovery(self, layout):
        home, cwd = layout
        install_plugin(home, "python-repl@repl-mcp", {
            "python-repl": {"command": "uvx", "args": [
                "--from", "git+https://github.com/iota-uz/repl-mcp@v2.0.0", "repl-mcp"]},
        })

        result = discover_servers(cwd=cwd, home=home)

        assert result.names == []
        assert "self" in result.excluded["plugin:python-repl@repl-mcp:python-repl"]

    def test_no_bridge_env_disables_discovery(self, layout, monkeypatch):
        home, cwd = layout
        write_json(home / ".claude.json", {"mcpServers": {"u": {"command": "u"}}})
        monkeypatch.setenv(NO_BRIDGE_ENV, "1")

        result = discover_servers(cwd=cwd, home=home)

        assert result.servers == {}
        assert NO_BRIDGE_ENV in result.excluded["*"]

    def test_spawned_children_carry_the_guard(self):
        assert spawn_env({"env": {"A": "1"}})[NO_BRIDGE_ENV] == "1"
        assert spawn_env({"env": {"A": "1"}})["A"] == "1"


class TestResilience:
    def test_malformed_json_degrades_to_empty(self, layout):
        home, cwd = layout
        (home / ".claude.json").write_text("{ not json")
        write_json(cwd / ".mcp.json", {"mcpServers": {"p": {"command": "p"}}})

        # The broken user file must not take the project scope down with it
        assert discover_servers(cwd=cwd, home=home).names == ["p"]

    def test_missing_files_are_fine(self, layout):
        home, cwd = layout
        assert discover_servers(cwd=cwd, home=home).names == []

    def test_non_dict_entries_are_ignored(self, layout):
        home, cwd = layout
        write_json(home / ".claude.json", {"mcpServers": {"bad": "nope",
                                                         "ok": {"command": "c"}}})

        assert discover_servers(cwd=cwd, home=home).names == ["ok"]

    def test_claude_config_dir_env_is_honored(self, layout, monkeypatch):
        home, cwd = layout
        write_json(home / ".claude" / ".claude.json",
                   {"mcpServers": {"u": {"command": "u"}}})
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))

        assert discover_servers(cwd=cwd).names == ["u"]


class TestExpansion:
    def test_expands_command_args_and_url(self, monkeypatch):
        monkeypatch.setenv("TOOL_HOME", "/opt/tool")
        expanded, unresolved = expand_config({
            "command": "${TOOL_HOME}/bin/server",
            "args": ["--root", "${TOOL_HOME}"],
            "url": "https://${TOOL_HOME:-x}/mcp",
        })

        assert expanded["command"] == "/opt/tool/bin/server"
        assert expanded["args"] == ["--root", "/opt/tool"]
        assert unresolved == []

    def test_unset_var_in_command_is_fatal(self, monkeypatch):
        monkeypatch.delenv("NOPE_TOKEN", raising=False)
        _, unresolved = expand_config({"command": "${NOPE_TOKEN}"})
        assert unresolved == ["NOPE_TOKEN"]

    def test_unset_var_in_env_is_not_fatal(self, monkeypatch):
        monkeypatch.delenv("NOPE_TOKEN", raising=False)
        expanded, unresolved = expand_config(
            {"command": "x", "env": {"T": "Bearer ${NOPE_TOKEN}"}}
        )
        assert unresolved == []
        assert expanded["env"]["T"] == "Bearer "

    def test_plugin_root_override_beats_environ(self, monkeypatch):
        """Our own process carries plugin env — it must never leak into a config."""
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/our/own/plugin")
        expanded, _ = expand_config(
            {"command": "node", "args": ["${CLAUDE_PLUGIN_ROOT}/dist/index.js"]},
            overrides={"CLAUDE_PLUGIN_ROOT": "/their/plugin"},
        )
        assert expanded["args"] == ["/their/plugin/dist/index.js"]


class TestRendering:
    def test_registry_never_leaks_secret_values(self, layout):
        home, cwd = layout
        write_json(home / ".claude.json", {"mcpServers": {"gh": {
            "command": "github-mcp-server",
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_CANARY_TOKEN"},
        }}})

        result = discover_servers(cwd=cwd, home=home)
        rendered = format_registry(result, connected=[], failed={})

        assert "ghp_CANARY_TOKEN" not in rendered
        assert "ghp_CANARY_TOKEN" not in json.dumps(redact(result.servers["gh"].config))

    def test_idle_servers_are_listed_with_scope(self, layout):
        home, cwd = layout
        write_json(home / ".claude.json", {"mcpServers": {"telegram-mcp": {"command": "uv"}}})

        rendered = format_registry(discover_servers(cwd=cwd, home=home))

        assert "telegram-mcp" in rendered
        assert "[user]" in rendered
        assert "not connected yet" in rendered

    def test_empty_registry_still_explains_scope(self):
        rendered = format_registry(None)
        assert "No MCP servers discovered" in rendered
        assert "claude.ai" in rendered
