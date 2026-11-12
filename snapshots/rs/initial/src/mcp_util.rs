//! Small helpers shared by both MCP servers. All protocol work is done by rmcp.

use std::sync::Arc;

use rmcp::model::{
    CallToolResponse, CallToolResult, ContentBlock, Implementation, ServerCapabilities, ServerConfig, Tool,
};
use serde_json::{Map, Value};

/// Result of a tool: JSON object text on success, an error message (without the `error: ` prefix).
pub type ToolOut = Result<String, String>;

pub fn ser<T: serde::Serialize>(v: &T) -> ToolOut {
    serde_json::to_string(v).map_err(|e| format!("cannot encode result: {e}"))
}

pub fn tool(name: &'static str, description: &'static str, schema: Value) -> Tool {
    let obj = match schema {
        Value::Object(m) => m,
        _ => unreachable!(),
    };
    Tool::new(name, description, Arc::new(obj))
}

pub fn server_info(name: &str) -> ServerConfig {
    ServerConfig::new(ServerCapabilities::builder().enable_tools().build())
        .with_server_info(Implementation::new(name, env!("CARGO_PKG_VERSION")))
}

pub fn to_response(out: ToolOut) -> CallToolResponse {
    match out {
        Ok(text) => CallToolResult::success(vec![ContentBlock::text(text)]).into(),
        Err(e) => CallToolResult::error(vec![ContentBlock::text(format!("error: {e}"))]).into(),
    }
}

pub fn req_str<'a>(args: &'a Map<String, Value>, key: &str) -> Result<&'a str, String> {
    match args.get(key) {
        Some(Value::String(s)) => Ok(s),
        Some(_) => Err(format!("{key} must be a string")),
        None => Err(format!("missing required argument: {key}")),
    }
}

pub fn opt_str<'a>(args: &'a Map<String, Value>, key: &str) -> Result<Option<&'a str>, String> {
    match args.get(key) {
        Some(Value::String(s)) => Ok(Some(s)),
        Some(Value::Null) | None => Ok(None),
        Some(_) => Err(format!("{key} must be a string")),
    }
}

pub fn opt_int(args: &Map<String, Value>, key: &str) -> Result<Option<i64>, String> {
    match args.get(key) {
        Some(Value::Null) | None => Ok(None),
        Some(Value::Number(n)) => {
            if let Some(i) = n.as_i64() {
                Ok(Some(i))
            } else if let Some(f) = n.as_f64().filter(|f| f.fract() == 0.0 && f.abs() < 9e15) {
                Ok(Some(f as i64))
            } else {
                Err(format!("{key} must be an integer"))
            }
        }
        Some(_) => Err(format!("{key} must be an integer")),
    }
}
