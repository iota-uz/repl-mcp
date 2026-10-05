use crate::{broker::Broker, supervisor::Supervisor};
use rmcp::{
    RoleServer, ServerHandler,
    model::{
        CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock, ErrorData,
        Implementation, ListToolsResult, PaginatedRequestParams, ServerCapabilities, ServerConfig,
        Tool,
    },
    service::RequestContext,
};
use serde_json::{Value, json};
use std::sync::{Arc, OnceLock};

#[derive(Clone)]
pub struct ReplServer {
    supervisor: Arc<Supervisor>,
    broker: Arc<Broker>,
}
impl ReplServer {
    pub fn new(supervisor: Arc<Supervisor>, broker: Arc<Broker>) -> Self {
        Self { supervisor, broker }
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
            json!({"type":"object","required":["code"],"additionalProperties":false,"properties":{"session_id":{"type":"string","description":"Get from python_health.server.session_id; required for MCP 2026-07-28 stateless requests"},"code":{"type":"string","maxLength":262144},"reset":{"type":"boolean","default":false},"timeout":{"type":"number","minimum":0.01,"maximum":3600,"default":120}}}),
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
                json!({"type":"object","additionalProperties":false,"properties":{"run_id":{"type":"string"}}}),
            ),
        ),
        Tool::new(
            "python_cancel",
            "Cancel active execution and outstanding bridge requests. Cancellation does not roll back external writes.",
            object(
                json!({"type":"object","additionalProperties":false,"properties":{"run_id":{"type":"string"}}}),
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
        Ok(ListToolsResult {
            tools: tools().to_vec(),
            ..Default::default()
        })
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
        if request.name == "execute_python" || request.name == "python_start" {
            let explicit = args["session_id"].as_str();
            let latest = context
                .protocol_version()
                .is_some_and(|version| version == rmcp::model::ProtocolVersion::V_2026_07_28);
            if explicit.is_some_and(|id| id != self.supervisor.session_id())
                || (latest && explicit.is_none())
            {
                return Err(ErrorData::invalid_params(
                    "Pass session_id from python_health.server.session_id to reference persistent state",
                    None,
                ));
            }
        }
        let mut value = match request.name.as_ref() {
            "python_start" => self.supervisor.start(
                args["code"].as_str().unwrap().into(),
                args["reset"].as_bool().unwrap_or(false),
                args["timeout"].as_f64().unwrap_or(120.0),
            ),
            "execute_python" => {
                self.supervisor
                    .execute(
                        args["code"].as_str().unwrap().into(),
                        args["reset"].as_bool().unwrap_or(false),
                        args["timeout"].as_f64().unwrap_or(120.0),
                        context.ct,
                    )
                    .await
            }
            "python_health" => {
                json!({"server":self.supervisor.health(),"broker":self.broker.health().await})
            }
            "python_run" => self.supervisor.run(args["run_id"].as_str()),
            "python_cancel" => self.supervisor.cancel(args["run_id"].as_str()),
            _ => unreachable!(),
        };
        if request.name == "execute_python" {
            value["session_id"] = json!(self.supervisor.session_id());
            value["generation"] = json!(self.supervisor.generation());
        }
        let error = value["success"] == false || value.get("error").is_some_and(|e| !e.is_null());
        Ok(envelope(value, error))
    }
}
