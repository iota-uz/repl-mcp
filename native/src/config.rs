//! Explicit broker registry. Client approvals are never inferred from a foreign registry.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};

#[derive(Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ServerConfig {
    #[serde(rename = "type", default)]
    pub transport: Option<String>,
    #[serde(default)]
    pub command: Option<String>,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    #[serde(default)]
    pub cwd: Option<PathBuf>,
    #[serde(default)]
    pub url: Option<String>,
    #[serde(default)]
    pub headers: BTreeMap<String, String>,
    #[serde(default)]
    pub disabled: bool,
    #[serde(rename = "brokerAllowed", default)]
    pub broker_allowed: bool,
    #[serde(rename = "allowedTools", default)]
    pub allowed_tools: Option<Vec<String>>,
    #[serde(default)]
    pub oauth: Option<OAuthConfig>,
}

#[derive(Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct OAuthConfig {
    #[serde(rename = "grantType", default = "authorization_code")]
    pub grant_type: String,
    #[serde(rename = "clientId", default)]
    pub client_id: Option<String>,
    #[serde(rename = "clientSecret", default)]
    pub client_secret: Option<String>,
    #[serde(rename = "credentialFile", default)]
    pub credential_file: Option<PathBuf>,
    #[serde(rename = "redirectPort", default)]
    pub redirect_port: u16,
    #[serde(default)]
    pub scopes: Vec<String>,
}
fn authorization_code() -> String {
    "authorization_code".into()
}

#[derive(Clone)]
pub struct Registry {
    pub servers: BTreeMap<String, ServerConfig>,
    pub config: Option<PathBuf>,
    pub scope: String,
    pub project: PathBuf,
    pub present: bool,
    pub excluded: BTreeMap<String, String>,
}

impl Registry {
    pub fn load_project(
        config: Option<PathBuf>,
        scope: &str,
        project: &Path,
    ) -> Result<Self, String> {
        let project = project
            .canonicalize()
            .map_err(|_| "Broker project must be an existing directory")?;
        if !project.is_dir() {
            return Err("Broker project must be a directory".into());
        }
        let config = config.map(|path| {
            if path.is_absolute() {
                path
            } else {
                project.join(path)
            }
        });
        Self::load_at(
            config,
            scope,
            &project,
            std::env::var_os("HOME").as_deref().map(Path::new),
        )
    }

    fn load_at(
        config: Option<PathBuf>,
        scope: &str,
        cwd: &Path,
        home: Option<&Path>,
    ) -> Result<Self, String> {
        let mut servers = BTreeMap::new();
        let mut excluded = BTreeMap::new();
        let mut present = false;
        if std::env::var("REPL_MCP_NO_BRIDGE").as_deref() == Ok("1") || scope == "none" {
            return Ok(Self {
                servers,
                config,
                scope: scope.to_owned(),
                project: cwd.to_owned(),
                present,
                excluded,
            });
        }
        let scopes: Vec<&str> = if scope == "all" {
            vec!["project", "user", "local"]
        } else {
            scope.split(',').collect()
        };
        if scopes
            .iter()
            .any(|s| !matches!(*s, "project" | "user" | "local"))
        {
            return Err("MCP scopes must be project, user, local, all or none. Plugin registries require an explicit exported --config file.".into());
        }
        if scopes.iter().any(|s| matches!(*s, "user" | "local")) {
            let home = home.ok_or("HOME unavailable for explicitly requested foreign registry")?;
            if let Some(doc) = read_document(&home.join(".claude.json"), false)? {
                present = true;
                if scopes.contains(&"user") {
                    record_excluded(&mut excluded, doc.get("mcpServers"), true);
                    merge(&mut servers, doc.get("mcpServers"), cwd, true)?;
                }
                if scopes.contains(&"local") {
                    let project = doc
                        .get("projects")
                        .and_then(|p| p.get(cwd.to_string_lossy().as_ref()));
                    record_excluded(
                        &mut excluded,
                        project.and_then(|p| p.get("mcpServers")),
                        true,
                    );
                    merge(
                        &mut servers,
                        project.and_then(|p| p.get("mcpServers")),
                        cwd,
                        true,
                    )?;
                }
            }
        }
        if scopes.contains(&"project") {
            let path = config.clone().unwrap_or_else(|| cwd.join(".mcp.json"));
            if let Some(doc) = read_document(&path, config.is_some())? {
                present = true;
                if doc.get("mcpServers").is_none() {
                    return Err(
                        "Project MCP registry requires mcpServers; check the registry format"
                            .into(),
                    );
                }
                record_excluded(&mut excluded, doc.get("mcpServers"), false);
                merge(
                    &mut servers,
                    doc.get("mcpServers"),
                    path.parent().unwrap_or(cwd),
                    false,
                )?;
            }
        }
        Ok(Self {
            servers,
            config,
            scope: scope.to_owned(),
            project: cwd.to_owned(),
            present,
            excluded,
        })
    }

    pub fn reload(&self) -> Result<Self, String> {
        Self::load_project(self.config.clone(), &self.scope, &self.project)
    }
}

fn record_excluded(out: &mut BTreeMap<String, String>, entries: Option<&Value>, foreign: bool) {
    if let Some(entries) = entries.and_then(Value::as_object) {
        for (name, entry) in entries {
            if foreign && entry.get("brokerAllowed").and_then(Value::as_bool) != Some(true) {
                out.insert(name.clone(), "not_granted".into());
            } else if entry.get("disabled").and_then(Value::as_bool) == Some(true) {
                out.insert(name.clone(), "disabled".into());
            } else {
                out.remove(name);
            }
        }
    }
}

fn read_document(path: &Path, required: bool) -> Result<Option<Value>, String> {
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NONBLOCK);
    }
    let file = match options.open(path) {
        Ok(file) => file,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound && !required => return Ok(None),
        Err(_) => {
            return Err(
                "Cannot read MCP registry file; check --config and file permissions".into(),
            );
        }
    };
    if !file
        .metadata()
        .map_err(|_| "Cannot inspect MCP registry")?
        .is_file()
    {
        return Err(
            "MCP registry must be a regular file; FIFOs and devices are unsupported".into(),
        );
    }
    use std::io::Read;
    let mut bytes = Vec::new();
    file.take(1024 * 1024 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "Cannot read MCP registry")?;
    if bytes.len() > 1024 * 1024 {
        return Err("MCP registry exceeds the 1 MiB limit".into());
    }
    serde_json::from_slice(&bytes)
        .map(Some)
        .map_err(|_| "MCP registry is invalid JSON; values are omitted from diagnostics".into())
}

fn merge(
    out: &mut BTreeMap<String, ServerConfig>,
    entries: Option<&Value>,
    base: &Path,
    foreign: bool,
) -> Result<(), String> {
    let Some(entries) = entries else {
        return Ok(());
    };
    let entries = entries.as_object().ok_or("mcpServers must be an object")?;
    for (name, entry) in entries {
        if name.len() > 200 || name.is_empty() || name.chars().any(char::is_control) {
            return Err("Invalid MCP server name".into());
        }
        // Foreign entries are inert without a separate broker grant. No tokens or approval settings are read.
        if foreign && entry.get("brokerAllowed").and_then(Value::as_bool) != Some(true) {
            continue;
        }
        let mut cfg: ServerConfig = serde_json::from_value(entry.clone()).map_err(|_| {
            format!("Invalid configuration for server {name}; check documented fields and types")
        })?;
        if cfg.disabled {
            out.remove(name);
            continue;
        }
        let transport = cfg
            .transport
            .as_deref()
            .unwrap_or(if cfg.command.is_some() {
                "stdio"
            } else {
                "http"
            });
        match transport {
            "stdio" => {
                if cfg.command.as_deref().is_none_or(str::is_empty)
                    || cfg.url.is_some()
                    || !cfg.headers.is_empty()
                    || cfg.oauth.is_some()
                {
                    return Err(format!(
                        "Server {name}: stdio requires command and cannot have url/headers/oauth"
                    ));
                }
                cfg.command = Some(expand(cfg.command.as_deref().unwrap())?);
                cfg.args = cfg
                    .args
                    .iter()
                    .map(|v| expand(v))
                    .collect::<Result<_, _>>()?;
                if cfg.cwd.is_none() {
                    cfg.cwd = Some(base.to_owned());
                }
                if is_self(&cfg) {
                    continue;
                }
            }
            "http" | "streamable-http" => {
                if cfg.command.is_some()
                    || !cfg.args.is_empty()
                    || !cfg.env.is_empty()
                    || cfg.cwd.is_some()
                {
                    return Err(format!(
                        "Server {name}: HTTP configuration cannot have command/args/env/cwd"
                    ));
                }
                let url = expand(
                    cfg.url
                        .as_deref()
                        .ok_or_else(|| format!("Server {name}: HTTP requires url"))?,
                )?;
                let parsed = reqwest::Url::parse(&url)
                    .map_err(|_| format!("Server {name}: invalid HTTP URL"))?;
                if !matches!(parsed.scheme(), "https" | "http")
                    || parsed.host_str().is_none()
                    || !parsed.username().is_empty()
                    || parsed.password().is_some()
                {
                    return Err(format!(
                        "Server {name}: URL requires HTTP(S), host, and no embedded credentials"
                    ));
                }
                cfg.url = Some(url);
            }
            _ => {
                return Err(format!(
                    "Server {name}: unsupported transport; use stdio or streamable-http"
                ));
            }
        }
        cfg.transport = Some(transport.to_owned());
        cfg.env = cfg
            .env
            .iter()
            .map(|(k, v)| Ok((k.clone(), expand(v)?)))
            .collect::<Result<_, String>>()?;
        cfg.headers = cfg
            .headers
            .iter()
            .map(|(k, v)| Ok((k.clone(), expand(v)?)))
            .collect::<Result<_, String>>()?;
        if let Some(oauth) = &mut cfg.oauth {
            if cfg
                .headers
                .keys()
                .any(|k| k.eq_ignore_ascii_case("authorization"))
            {
                return Err(format!(
                    "Server {name}: oauth conflicts with authorization header"
                ));
            }
            oauth.client_id = oauth.client_id.as_deref().map(expand).transpose()?;
            oauth.client_secret = oauth.client_secret.as_deref().map(expand).transpose()?;
            if oauth.client_id.as_deref() == Some("") || oauth.client_secret.as_deref() == Some("")
            {
                return Err(format!("Server {name}: OAuth credentials must be nonempty"));
            }
            match oauth.grant_type.as_str() {
                "client_credentials"
                    if oauth.client_id.is_some() && oauth.client_secret.is_some() => {}
                "authorization_code" if oauth.credential_file.is_some() => {}
                _ => {
                    return Err(format!(
                        "Server {name}: OAuth requires authorization_code with credentialFile, or client_credentials with clientId/clientSecret"
                    ));
                }
            }
            if let Some(path) = &oauth.credential_file {
                let expanded = PathBuf::from(expand(&path.to_string_lossy())?);
                oauth.credential_file = Some(if expanded.is_absolute() {
                    expanded
                } else {
                    base.join(expanded)
                });
            }
        }
        for (k, v) in &cfg.headers {
            reqwest::header::HeaderName::from_bytes(k.as_bytes())
                .map_err(|_| format!("Server {name}: invalid header name"))?;
            reqwest::header::HeaderValue::from_str(v)
                .map_err(|_| format!("Server {name}: invalid header value"))?;
            if k.eq_ignore_ascii_case("authorization")
                && (v.trim().is_empty() || v.trim().eq_ignore_ascii_case("Bearer"))
            {
                return Err(format!(
                    "Server {name}: empty authorization header; configure credentials explicitly"
                ));
            }
        }
        if let Some(dir) = &cfg.cwd {
            let expanded = PathBuf::from(expand(&dir.to_string_lossy())?);
            cfg.cwd = Some(if expanded.is_absolute() {
                expanded
            } else {
                base.join(expanded)
            });
        }
        if cfg.cwd.as_ref().is_some_and(|dir| !dir.is_dir()) {
            return Err(format!("Server {name}: cwd must be an existing directory"));
        }
        if cfg
            .allowed_tools
            .as_ref()
            .is_some_and(|t| t.iter().any(|s| s.is_empty()))
        {
            return Err(format!(
                "Server {name}: allowedTools contains an empty name"
            ));
        }
        out.insert(name.clone(), cfg);
    }
    Ok(())
}

fn expand(input: &str) -> Result<String, String> {
    let mut out = String::new();
    let mut rest = input;
    while let Some(start) = rest.find("${") {
        out.push_str(&rest[..start]);
        let tail = &rest[start + 2..];
        let end = tail
            .find('}')
            .ok_or("Unclosed environment placeholder in MCP config")?;
        let key = &tail[..end];
        if key.is_empty() || !key.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') {
            return Err("Invalid environment placeholder in MCP config".into());
        }
        let value = std::env::var(key).map_err(|_| {
            "Required environment variable is missing; config values are omitted".to_owned()
        })?;
        if value.is_empty() {
            return Err(
                "Required environment variable is empty; configure credentials explicitly".into(),
            );
        }
        out.push_str(&value);
        rest = &tail[end + 1..];
    }
    out.push_str(rest);
    Ok(out)
}

fn is_self(cfg: &ServerConfig) -> bool {
    let executable = cfg
        .command
        .as_deref()
        .and_then(|c| Path::new(c).file_name())
        .and_then(|n| n.to_str())
        .unwrap_or("");
    matches!(executable, "repl-mcp" | "repl-mcp.exe")
        || cfg.args.windows(2).any(|pair| {
            pair[0] == "-m" && matches!(pair[1].as_str(), "repl_mcp.repl_mcp_server" | "repl_mcp")
        })
        || (matches!(executable, "uvx" | "uv") && cfg.args.iter().any(|a| a == "repl-mcp"))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn reload_is_bound_to_project_and_stdio_cwd_does_not_follow_host_launch_directory() {
        let base =
            std::env::temp_dir().join(format!("repl-project-registry-{}", uuid::Uuid::new_v4()));
        let project_a = base.join("a");
        let project_b = base.join("b");
        std::fs::create_dir_all(&project_a).unwrap();
        std::fs::create_dir_all(&project_b).unwrap();
        std::fs::write(
            project_a.join(".mcp.json"),
            r#"{"mcpServers":{"peer":{"command":"echo","env":{"TOKEN":"a"}}}}"#,
        )
        .unwrap();
        std::fs::write(
            project_b.join(".mcp.json"),
            r#"{"mcpServers":{"other":{"command":"echo"}}}"#,
        )
        .unwrap();
        let a = Registry::load_project(None, "project", &project_a).unwrap();
        let b = Registry::load_project(None, "project", &project_b).unwrap();
        assert!(a.servers.contains_key("peer") && !b.servers.contains_key("peer"));
        assert_eq!(
            a.servers["peer"].cwd.as_ref().unwrap(),
            &project_a.canonicalize().unwrap()
        );
        assert!(a.reload().unwrap().servers.contains_key("peer"));
        std::fs::remove_dir_all(base).unwrap();
    }
    #[test]
    fn excluded_entries_report_grants_without_parsing_foreign_credentials() {
        let mut excluded = BTreeMap::new();
        record_excluded(
            &mut excluded,
            Some(
                &serde_json::json!({"foreign":{"headers":{"Authorization":"do-not-read"}},"off":{"disabled":true,"brokerAllowed":true}}),
            ),
            true,
        );
        assert_eq!(excluded["foreign"], "not_granted");
        assert_eq!(excluded["off"], "disabled");
        assert!(
            !serde_json::to_string(&excluded)
                .unwrap()
                .contains("do-not-read")
        );
    }
    #[test]
    fn rejects_missing_env_without_values() {
        assert!(
            expand("${REPL_MCP_TEST_NONEXISTENT_349839}")
                .unwrap_err()
                .contains("missing")
        );
    }
    #[test]
    fn validates_transport_and_redacts_config() {
        let mut out = BTreeMap::new();
        let doc = serde_json::json!({"x":{"type":"sse","url":"https://secret.invalid"}});
        let error = merge(&mut out, Some(&doc), Path::new("."), false).unwrap_err();
        assert!(!error.contains("secret.invalid"));
        assert!(
            merge(
                &mut out,
                Some(&serde_json::json!({"x":{"command":"echo","typo":"secret"}})),
                Path::new("."),
                false
            )
            .is_err()
        );
    }
    #[test]
    fn foreign_requires_independent_grant() {
        let mut out = BTreeMap::new();
        merge(
            &mut out,
            Some(&serde_json::json!({"x":{"command":"echo"}})),
            Path::new("."),
            true,
        )
        .unwrap();
        assert!(out.is_empty());
        merge(&mut out, Some(&serde_json::json!({"x":{"command":"echo","brokerAllowed":true,"allowedTools":["read"]}})), Path::new("."), true).unwrap();
        assert_eq!(out.len(), 1);
    }
    #[test]
    fn fixture_paths_are_not_recursive_servers() {
        let mut out = BTreeMap::new();
        merge(&mut out,Some(&serde_json::json!({"fixture":{"command":"python","args":["/projects/repl_mcp/tests/fixture.py"]},"self":{"command":"python","args":["-m","repl_mcp.repl_mcp_server"]}})),Path::new("."),false).unwrap();
        assert!(out.contains_key("fixture"));
        assert!(!out.contains_key("self"));
    }
}
