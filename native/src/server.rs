use crate::{sessions::SessionManager, supervisor::Execution};
use rmcp::{
    RoleServer, ServerHandler,
    model::{
        CacheScope, CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock,
        ErrorData, Implementation, ListToolsResult, PaginatedRequestParams, ServerCapabilities,
        ServerConfig, Tool,
    },
    service::RequestContext,
};
use serde_json::{Value, json};
use std::sync::{Arc, OnceLock};

#[derive(Clone)]
pub struct ReplServer {
    sessions: Arc<SessionManager>,
}
impl ReplServer {
    pub fn new(sessions: Arc<SessionManager>) -> Self {
        Self { sessions }
    }
}

fn object(value: Value) -> serde_json::Map<String, Value> {
    value.as_object().unwrap().clone()
}
fn tools() -> &'static [Tool] {
    static TOOLS: OnceLock<Vec<Tool>> = OnceLock::new();
    TOOLS.get_or_init(build_tools)
}

fn build_tools() -> Vec<Tool> {
    let output = json!({"type":"object","required":["run_id","success","stdout","stderr","return_value","error","elapsed_ms","truncated","state"],"properties":{"run_id":{"type":"string"},"success":{"type":"boolean"},"stdout":{"type":"string"},"stderr":{"type":"string"},"return_value":{},"error":{"type":["string","null"]},"elapsed_ms":{"type":"number"},"truncated":{"type":"object"},"state":{"enum":["preserved","cleared"]}}});
    let mut execute = Tool::new(
        "execute_python",
        "Execute Python in a persistent trusted local worker with sh() and mcp.call()/mcp.acall() bridge helpers. Top-level await supported. timeout 0.01..3600s, code <=256KiB. reset restarts the worker, clearing variables/cwd/environment. Cancellation stops code and linked MCP calls; external effects may already have occurred.",
        object(
            json!({"type":"object","required":["code"],"additionalProperties":false,"properties":{"session_id":{"type":"string","description":"Get from python_health.server.session_id; required for MCP 2026-07-28 stateless requests"},"code":{"type":"string","maxLength":262144},"expected_generation":{"type":"integer","minimum":0,"description":"Reject STATE_CHANGED before executing code if Python state changed"},"reset":{"type":"boolean","default":false},"timeout":{"type":"number","minimum":0.01,"maximum":3600,"default":120}}}),
        ),
    );
    execute.output_schema = Some(Arc::new(object(output)));
    execute.annotations = Some(
        rmcp::model::ToolAnnotations::new()
            .read_only(false)
            .destructive(true)
            .idempotent(false)
            .open_world(true),
    );
    let mut start = execute.clone();
    start.name = "python_start".into();
    start.description = Some("Start a long Python execution and return run_id immediately. Poll python_run for live output/result, python_cancel to stop. One run at a time; the server owns the task until completion or shutdown. Same code/reset/timeout/session_id semantics as execute_python.".into());
    start.output_schema = Some(Arc::new(object(
        json!({"type":"object","oneOf":[{"required":["run_id","session_id","status"],"properties":{"run_id":{"type":"string"},"session_id":{"type":"string"},"status":{"enum":["starting"]}}},{"required":["error"],"properties":{"error":{"type":"string"}}}]}),
    )));
    let mut tools = vec![
        execute,
        start,
        Tool::new(
            "python_health",
            "Inspect native server/runtime/broker health without starting Python or downstream servers.",
            object(json!({"type":"object","additionalProperties":false,"properties":{}})),
        ),
        Tool::new(
            "python_run",
            "Inspect active or retained execution result (last 64 runs); omit run_id for latest.",
            object(
                json!({"type":"object","additionalProperties":false,"properties":{"session_id":{"type":"string"},"run_id":{"type":"string"}}}),
            ),
        ),
        Tool::new(
            "python_cancel",
            "Cancel active execution and outstanding bridge requests. Cancellation does not roll back external writes.",
            object(
                json!({"type":"object","additionalProperties":false,"properties":{"session_id":{"type":"string"},"run_id":{"type":"string"}}}),
            ),
        ),
    ];
    for tool in &mut tools {
        match tool.name.as_ref() {
            "python_health" => {
                tool.output_schema = Some(Arc::new(object(
                    json!({"type":"object","required":["server","broker"],"properties":{"server":{"type":"object"},"broker":{"type":"object"}}}),
                )));
                tool.annotations = Some(
                    rmcp::model::ToolAnnotations::new()
                        .read_only(true)
                        .destructive(false)
                        .idempotent(true)
                        .open_world(false),
                );
            }
            "python_run" => {
                tool.output_schema = Some(Arc::new(object(
                    json!({"type":"object","oneOf":[{"required":["run_id","status"],"properties":{"run_id":{"type":"string"},"status":{"enum":["starting","running"]}}},{"required":["run_id","success"],"properties":{"run_id":{"type":"string"},"success":{"type":"boolean"}}},{"required":["error"],"not":{"required":["success"]},"properties":{"error":{"type":"string"}}}]}),
                )));
                tool.annotations = Some(
                    rmcp::model::ToolAnnotations::new()
                        .read_only(true)
                        .destructive(false)
                        .idempotent(true)
                        .open_world(false),
                );
            }
            "python_cancel" => {
                tool.output_schema = Some(Arc::new(object(
                    json!({"type":"object","required":["cancel_requested"],"properties":{"cancel_requested":{"type":"boolean"},"run_id":{"type":"string"},"error":{"type":"string"}}}),
                )));
                tool.annotations = Some(
                    rmcp::model::ToolAnnotations::new()
                        .read_only(false)
                        .destructive(true)
                        .idempotent(false)
                        .open_world(true),
                );
            }
            _ => {}
        }
    }
    let definitions = vec![
        (
            "python_session_open",
            "Open an independent lazy Python session for a project. Explicit python or project/.venv/bin/python must be valid; absent .venv uses the reported server default. Interpreter is probed but worker starts on first execution.",
            json!({"type":"object","required":["name","project"],"additionalProperties":false,"properties":{"name":{"type":"string","minLength":1,"maxLength":128},"project":{"type":"string"},"python":{"type":"string"}}}),
        ),
        (
            "python_session_close",
            "Close a session, cancel and reap its active Python execution, and release its artifacts. Shared same-project MCP connections remain available to other sessions.",
            json!({"type":"object","required":["session_id"],"additionalProperties":false,"properties":{"session_id":{"type":"string"}}}),
        ),
        (
            "python_session_list",
            "List sessions, selected environments and generations without starting workers.",
            json!({"type":"object","additionalProperties":false,"properties":{}}),
        ),
        (
            "python_session_inspect",
            "Inspect session metadata. include_namespace returns bounded names and exact builtin type metadata without repr/getattr/property hooks; requires an idle session.",
            json!({"type":"object","additionalProperties":false,"properties":{"session_id":{"type":"string"},"include_namespace":{"type":"boolean","default":false}}}),
        ),
        (
            "python_execute_file",
            "Execute a Python file <=256KiB (including PEP 263 encodings) in the selected session interpreter/project. persistent shares variables; fresh owns a separate child and preserves the persistent namespace. argv is passed as literal strings; timeout/cancel and retained run results apply.",
            json!({"type":"object","required":["session_id","path"],"additionalProperties":false,"properties":{"session_id":{"type":"string"},"path":{"type":"string"},"argv":{"type":"array","maxItems":128,"items":{"type":"string"}},"mode":{"enum":["persistent","fresh"],"default":"persistent"},"timeout":{"type":"number","minimum":0.01,"maximum":3600,"default":120},"expected_generation":{"type":"integer","minimum":0}}}),
        ),
        (
            "artifact_create",
            "Copy a local file into bounded server-owned artifact storage. Artifacts belong to a session, survive resets, expire on session close or oldest-first eviction.",
            json!({"type":"object","required":["session_id","path","format"],"additionalProperties":false,"properties":{"session_id":{"type":"string"},"path":{"type":"string"},"format":{"enum":["json","text","binary"]}}}),
        ),
        (
            "artifact_read",
            "Read a bounded range of a session-owned artifact; use offsets for pagination.",
            json!({"type":"object","required":["session_id","id"],"additionalProperties":false,"properties":{"session_id":{"type":"string"},"id":{"type":"string"},"offset":{"type":"integer","minimum":0,"default":0},"length":{"type":"integer","minimum":0,"maximum":65536,"default":65536},"encoding":{"enum":["text","base64","hex"],"default":"text"}}}),
        ),
        (
            "artifact_save",
            "Save an artifact to an explicit local file, refusing overwrite by default.",
            json!({"type":"object","required":["session_id","id","path"],"additionalProperties":false,"properties":{"session_id":{"type":"string"},"id":{"type":"string"},"path":{"type":"string"},"overwrite":{"type":"boolean","default":false}}}),
        ),
        (
            "artifact_delete",
            "Delete one session-owned artifact.",
            json!({"type":"object","required":["session_id","id"],"additionalProperties":false,"properties":{"session_id":{"type":"string"},"id":{"type":"string"}}}),
        ),
        (
            "artifact_forward",
            "Forward a JSON/text artifact as one named argument to an explicitly configured MCP tool. Preserves the full result; never automatically retries external writes.",
            json!({"type":"object","required":["session_id","id","server","tool","argument"],"additionalProperties":false,"properties":{"session_id":{"type":"string"},"id":{"type":"string"},"server":{"type":"string"},"tool":{"type":"string"},"argument":{"type":"string"},"format":{"enum":["json","text","base64"],"default":"json"},"arguments":{"type":"object"}}}),
        ),
    ];
    for (name, description, input) in definitions {
        let mut tool = Tool::new(name, description, object(input));
        let reference = json!({"type":"object","required":["id","size","format","sha256","lifetime","eviction"],"properties":{"id":{"type":"string"},"size":{"type":"integer","minimum":0},"format":{"enum":["json","text","binary"]},"sha256":{"type":"string"},"lifetime":{"enum":["server"]},"eviction":{"enum":["oldest-first"]}}});
        let metadata = json!({"required":["server_id","session_id","generation","environment","project","name"],"properties":{"server_id":{"type":"string"},"session_id":{"type":"string"},"generation":{"type":"integer","minimum":0},"environment":{"type":"object"},"project":{"type":"string"},"name":{"type":"string"},"namespace":{"type":"object"}}});
        let success = match name {
            "python_session_open" | "python_session_inspect" => metadata,
            "python_session_list" => {
                json!({"required":["server_id","sessions"],"properties":{"server_id":{"type":"string"},"sessions":{"type":"array","items":{"type":"object","required":["session_id","generation","environment","project","name"]}},"limit":{"type":"integer"}}})
            }
            "python_session_close" => {
                json!({"required":["closed","server_id","session_id","generation"],"properties":{"closed":{"const":true},"server_id":{"type":"string"},"session_id":{"type":"string"},"generation":{"type":"integer"}}})
            }
            "artifact_create" => reference.clone(),
            "artifact_read" => {
                json!({"required":["artifact","offset","length","encoding","content","has_more"],"properties":{"artifact":reference,"offset":{"type":"integer","minimum":0},"length":{"type":"integer","minimum":0},"encoding":{"enum":["text","base64","hex"]},"content":{"type":"string"},"has_more":{"type":"boolean"}}})
            }
            "artifact_save" => {
                json!({"required":["artifact","saved","path"],"properties":{"artifact":reference,"saved":{"const":true},"path":{"type":"string"}}})
            }
            "artifact_delete" => {
                json!({"required":["id","deleted"],"properties":{"id":{"type":"string"},"deleted":{"const":true}}})
            }
            "artifact_forward" => {
                json!({"required":["content"],"properties":{"content":{"type":"array"},"structuredContent":{"type":"object"},"isError":{"type":"boolean"},"_meta":{"type":"object"}}})
            }
            _ => json!({}),
        };
        tool.output_schema = Some(Arc::new(object(
            json!({"type":"object","oneOf":[success,{"required":["error"],"properties":{"error":{"type":"string"}}}]}),
        )));
        tool.annotations = Some(
            rmcp::model::ToolAnnotations::new()
                .read_only(matches!(
                    name,
                    "python_session_list" | "python_session_inspect" | "artifact_read"
                ))
                .destructive(!matches!(
                    name,
                    "python_session_list" | "python_session_inspect" | "artifact_read"
                ))
                .idempotent(false)
                .open_world(true),
        );
        if name == "python_execute_file" {
            tool.output_schema = tools[0].output_schema.clone();
        }
        tools.push(tool);
    }
    tools
}

fn envelope(mut value: Value, error: bool) -> CallToolResponse {
    let mut text = if value.get("success").is_some() {
        let mut text = value["stdout"].as_str().unwrap_or("").to_owned();
        if let Some(stderr) = value["stderr"].as_str().filter(|s| !s.is_empty()) {
            text.push_str(&format!("\nStderr:\n{stderr}"));
        }
        if let Some(returned) = value["return_value"].as_str().filter(|s| !s.is_empty()) {
            text.push_str(&format!("\n{returned}"));
        }
        if let Some(error) = value["error"].as_str() {
            text.push_str(&format!("\n{error}"));
        }
        if value["state"] == "cleared" {
            text.push_str("\nWorker state cleared.");
        }
        if text.is_empty() {
            "Execution completed.".into()
        } else {
            text
        }
    } else {
        serde_json::to_string(&value).unwrap()
    };
    if text.len() > 8192 {
        let mut boundary = 8192;
        while !text.is_char_boundary(boundary) {
            boundary -= 1;
        }
        text.truncate(boundary);
        text.push_str(
            "\n[Text preview truncated; complete bounded output is in structuredContent.]",
        );
        value["text_truncated"] = json!(true);
    }
    let mut result = CallToolResult::success(vec![ContentBlock::text(text)]);
    result.structured_content = Some(value);
    result.is_error = Some(error);
    // Leave room for the JSON-RPC envelope. Presentation is bounded separately
    // so escaping/control characters cannot double the full execution payload.
    if serde_json::to_vec(&result).unwrap().len() > 1_000_000 {
        result.content = vec![ContentBlock::text(
            "Output presentation omitted; inspect structuredContent.",
        )];
    }
    result.into()
}

impl ServerHandler for ReplServer {
    fn get_info(&self) -> ServerConfig {
        let mut info = ServerConfig::new(ServerCapabilities::builder().enable_tools().build());
        info.server_info = Implementation::new("repl-mcp", env!("CARGO_PKG_VERSION"));
        info.instructions = Some("Trusted local Python execution with full host access. Use python_health for independent diagnostics, python_run/python_cancel for active executions. The mcp helper brokers only explicitly configured servers; mcp.call returns a complete result envelope, mcp.text extracts text.".into());
        info
    }
    fn get_tool(&self, name: &str) -> Option<Tool> {
        tools().iter().find(|tool| tool.name == name).cloned()
    }
    async fn list_tools(
        &self,
        _: Option<PaginatedRequestParams>,
        _: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, ErrorData> {
        Ok(ListToolsResult::with_all_items(tools().to_vec())
            .with_ttl_ms(0)
            .with_cache_scope(CacheScope::Private))
    }
    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, ErrorData> {
        let args = Value::Object(request.arguments.unwrap_or_default());
        let Some(tool) = self.get_tool(&request.name) else {
            return Err(ErrorData::invalid_params("Unknown tool", None));
        };
        if let Err(error) =
            jsonschema::validate(&Value::Object((*tool.input_schema).clone()), &args)
        {
            return Err(ErrorData::invalid_params(
                format!("Invalid arguments at {}", error.instance_path),
                None,
            ));
        }
        if matches!(request.name.as_ref(), "execute_python" | "python_start") {
            let latest = context
                .protocol_version()
                .is_some_and(|v| v == rmcp::model::ProtocolVersion::V_2026_07_28);
            if latest && args["session_id"].as_str().is_none() {
                return Err(ErrorData::invalid_params(
                    "Pass session_id from python_health or python_session_open to reference persistent state",
                    None,
                ));
            }
        }
        if args.get("expected_generation").is_some()
            && args["expected_generation"].as_u64().is_none()
        {
            return Err(ErrorData::invalid_params(
                "expected_generation must be a nonnegative integral u64",
                None,
            ));
        }
        for field in ["offset", "length"] {
            if args.get(field).is_some() && args[field].as_u64().is_none() {
                return Err(ErrorData::invalid_params(
                    format!("{field} must be a nonnegative integral u64"),
                    None,
                ));
            }
        }
        let session = self.sessions.get(args["session_id"].as_str());
        let routed = !matches!(
            request.name.as_ref(),
            "python_session_open"
                | "python_session_close"
                | "python_session_list"
                | "python_health"
        );
        let session = if routed {
            Some(session.map_err(|e| ErrorData::invalid_params(e, None))?)
        } else {
            None
        };
        let result: Result<Value, String> = match request.name.as_ref() {
            "python_session_open" => {
                tokio::select! {
                    biased;
                    _=context.ct.cancelled()=>Err("Session opening cancelled before publication".into()),
                    result=self.sessions.open(
                        args["name"].as_str().unwrap(),
                        args["project"].as_str().unwrap(),
                        args["python"].as_str(),
                    )=>result,
                }
            }
            "python_session_close" => {
                self.sessions
                    .close(args["session_id"].as_str().unwrap())
                    .await
            }
            "python_session_list" => Ok(self.sessions.list()),
            "python_session_inspect" => {
                self.sessions
                    .inspect(
                        args["session_id"].as_str(),
                        args["include_namespace"].as_bool().unwrap_or(false),
                        context.ct,
                    )
                    .await
            }
            "python_health" => {
                let default = self.sessions.default();
                Ok(
                    json!({"server":default.supervisor.health(),"broker":default.supervisor.broker().health().await,"sessions":self.sessions.list()}),
                )
            }
            "execute_python" | "python_start" | "python_execute_file" => {
                let session = session.as_ref().unwrap();
                let mut execution = Execution::code(
                    args["code"].as_str().unwrap_or("").into(),
                    args["reset"].as_bool().unwrap_or(false),
                    args["timeout"].as_f64().unwrap_or(120.0),
                );
                execution.expected_generation = args["expected_generation"].as_u64();
                execution.extra = json!({"artifact_enabled":true});
                if request.name == "python_execute_file" {
                    let path = std::path::Path::new(args["path"].as_str().unwrap());
                    let path = if path.is_absolute() {
                        path.to_owned()
                    } else {
                        session.project.join(path)
                    };
                    execution.extra["source_path"] = json!(path);
                    execution.extra["argv"] = args.get("argv").cloned().unwrap_or(json!([]));
                    execution.fresh = args["mode"] == "fresh";
                }
                Ok(if request.name == "python_start" {
                    session.supervisor.start_request(execution)
                } else {
                    session
                        .supervisor
                        .execute_request(execution, context.ct)
                        .await
                })
            }
            "python_run" => Ok(session
                .as_ref()
                .unwrap()
                .supervisor
                .run(args["run_id"].as_str())),
            "python_cancel" => Ok(session
                .as_ref()
                .unwrap()
                .supervisor
                .cancel(args["run_id"].as_str())),
            "artifact_forward" => {
                let session = session.as_ref().unwrap();
                if context.ct.is_cancelled() || session.lifetime.is_cancelled() {
                    Err("Forwarding cancelled before dispatch".into())
                } else {
                    let forward = async {
                        let payload = self
                            .sessions
                            .artifacts
                            .forward_payload(
                                session.supervisor.session_id(),
                                args["id"].as_str().unwrap(),
                                args["format"].as_str().unwrap_or("json"),
                            )
                            .await?;
                        if context.ct.is_cancelled() || session.lifetime.is_cancelled() {
                            return Err("Forwarding cancelled before dispatch".into());
                        }
                        let mut arguments = args.get("arguments").cloned().unwrap_or(json!({}));
                        arguments[args["argument"].as_str().unwrap()] = payload;
                        session.supervisor.broker().dispatch_with_context(session.supervisor.session_id(),&uuid::Uuid::new_v4().to_string(),"call",json!({"server":args["server"],"tool":args["tool"],"arguments":arguments})).await
                    };
                    tokio::select! {
                        biased;
                        _=context.ct.cancelled()=>Err("Forwarding cancelled; external effects may already have occurred; inspect the broker journal".into()),
                        _=session.lifetime.cancelled()=>Err("Session closed; forwarding cancelled; external effects may already have occurred".into()),
                        result=forward=>result,
                    }
                }
            }

            "artifact_create" | "artifact_read" | "artifact_save" | "artifact_delete" => {
                let session = session.as_ref().unwrap();
                let op = match request.name.as_ref() {
                    "artifact_create" => "artifact.create",
                    "artifact_read" => "artifact.read",
                    "artifact_save" => "artifact.save",
                    _ => "artifact.drop",
                };
                let mut params = args.clone();
                if matches!(op, "artifact.create" | "artifact.save") {
                    let path = std::path::Path::new(args["path"].as_str().unwrap());
                    if !path.is_absolute() {
                        params["path"] = json!(session.project.join(path));
                    }
                }
                self.sessions
                    .artifacts
                    .dispatch(session.supervisor.session_id(), op, params)
                    .await
            }
            _ => unreachable!(),
        };
        let mut value = result.unwrap_or_else(|error| json!({"error":error}));
        if let Some(session) = session {
            session
                .supervisor
                .identify(&mut value, session.supervisor.generation());
        }
        let error = value["isError"] == true
            || value["success"] == false
            || value.get("error").is_some_and(|e| !e.is_null());
        Ok(envelope(value, error))
    }
}
