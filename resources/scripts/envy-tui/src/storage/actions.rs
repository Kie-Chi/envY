//! Explicit storage operations. Policy and filesystem eligibility belong to Python.

use crate::model::WorkflowReport;
use serde_json::{json, Value};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};

pub const ACTION_IDS: [&str; 8] = [
    "nix",
    "docker",
    "go",
    "go-modcache",
    "uv",
    "npm",
    "pip",
    "brew",
];

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Operation {
    Run,
    Purge,
    #[default]
    Maintain,
    Restore,
}
impl Operation {
    pub fn name(self) -> &'static str {
        match self {
            Self::Run => "run",
            Self::Purge => "purge",
            Self::Maintain => "maintain",
            Self::Restore => "restore",
        }
    }
    pub fn next(self) -> Self {
        match self {
            Self::Maintain => Self::Run,
            Self::Run => Self::Purge,
            Self::Purge => Self::Restore,
            Self::Restore => Self::Maintain,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Intent {
    pub operation: Operation,
    pub plan_id: String,
    pub run_id: String,
    pub target: String,
    pub actions: Vec<(String, String)>,
}
impl Intent {
    pub fn request(&self) -> Value {
        let mut value =
            json!({"actions": self.actions.iter().map(|(id, _)| id).collect::<Vec<_>>()});
        match self.operation {
            Operation::Run => value["planId"] = json!(self.plan_id),
            Operation::Restore => {
                value["runId"] = json!(self.run_id);
                value["absoluteTarget"] = json!(self.target);
            }
            _ => {}
        }
        value
    }
    pub fn matches_request(&self, request: &Value) -> bool {
        if self.operation != Operation::Restore {
            return *request == self.request();
        }
        // The backend may return a canonical absolute target. Keep the original
        // input in the request identity and execution argv; never resolve paths
        // or inspect eligibility here. Review displays the backend's target.
        request.get("actions") == Some(&json!([]))
            && request.get("runId").and_then(Value::as_str) == Some(self.run_id.as_str())
            && request
                .get("absoluteTarget")
                .and_then(Value::as_str)
                .is_some_and(|target| target.starts_with('/'))
    }
    pub fn argv(&self, token: Option<&str>) -> Result<Vec<String>, String> {
        let mut args = vec!["storage".into(), self.operation.name().into()];
        let valid_id =
            |id: &str| !id.trim().is_empty() && !id.starts_with('-') && !id.contains('\0');
        match self.operation {
            Operation::Run => {
                if !valid_id(&self.plan_id) {
                    return Err("Enter an explicitly saved plan ID".into());
                }
                args.push(self.plan_id.clone());
            }
            Operation::Restore => {
                if !valid_id(&self.run_id)
                    || !self.target.starts_with('/')
                    || self.target.contains('\0')
                {
                    return Err(
                        "Enter a run ID and new absolute target; backend validates eligibility"
                            .into(),
                    );
                }
                args.extend([self.run_id.clone(), "--target".into(), self.target.clone()]);
            }
            Operation::Maintain => {
                if self.actions.is_empty() {
                    return Err("Select at least one native maintenance action".into());
                }
                for (id, flag) in &self.actions {
                    // Only fixed maintenance selectors may enter argv. Availability,
                    // opt-in, and the advertised selector come from backend metadata.
                    if !ACTION_IDS.contains(&id.as_str())
                        || !ACTION_IDS.iter().any(|id| flag == &format!("--{id}"))
                    {
                        return Err("Unsupported backend maintenance selector".into());
                    }
                    args.push(flag.clone());
                }
            }
            Operation::Purge => {}
        }
        match token {
            Some(token) if !token.is_empty() && !token.contains('\0') => {
                args.extend(["--preview-token".into(), token.into(), "--yes".into()])
            }
            Some(_) => return Err("Missing preview token; preview again".into()),
            None => args.push("--preview".into()),
        }
        args.push("--json".into());
        Ok(args)
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Task {
    Capabilities,
    Plan,
    Preview(Intent),
    Execute(Intent, String),
}
impl Task {
    pub fn argv(&self) -> Result<Vec<String>, String> {
        match self {
            Self::Capabilities => Ok(vec![
                "storage".into(),
                "capabilities".into(),
                "--json".into(),
            ]),
            Self::Plan => Ok(vec!["storage".into(), "plan".into(), "--json".into()]),
            Self::Preview(intent) => intent.argv(None),
            Self::Execute(intent, token) => intent.argv(Some(token)),
        }
    }
    pub fn command(&self) -> String {
        match self {
            Self::Capabilities => "storage capabilities".into(),
            Self::Plan => "storage plan".into(),
            Self::Preview(intent) | Self::Execute(intent, _) => {
                format!("storage {}", intent.operation.name())
            }
        }
    }
    pub fn mutating(&self) -> bool {
        !matches!(self, Self::Capabilities)
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Stage {
    #[default]
    Editing,
    Busy,
    Review,
    Confirm,
    Result,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Field {
    Plan,
    Run,
    Target,
}

/// Evidence from a reviewed preview, separate from the single-use execution
/// token. This receipt has no token field and cannot construct an execute task.
#[derive(Clone, Debug)]
pub struct ReviewedReceipt {
    operation: Operation,
    operation_id: String,
    request: Value,
    preview: Value,
    policy_fingerprint: String,
    source_fingerprint: String,
    expires_at: f64,
}

impl ReviewedReceipt {
    fn from_preview(operation: Operation, data: &Value) -> Self {
        Self {
            operation,
            operation_id: data["operationId"].as_str().unwrap().into(),
            request: data["request"].clone(),
            preview: data["preview"].clone(),
            policy_fingerprint: data["policyFingerprint"].as_str().unwrap().into(),
            source_fingerprint: data["sourceFingerprint"].as_str().unwrap().into(),
            expires_at: data["expiresAt"].as_f64().unwrap(),
        }
    }

    pub fn evidence(&self) -> Value {
        json!({
            "operation": self.operation.name(),
            "operationId": self.operation_id,
            "request": self.request,
            "preview": self.preview,
            "policyFingerprint": self.policy_fingerprint,
            "sourceFingerprint": self.source_fingerprint,
            "expiresAt": self.expires_at,
        })
    }
}

pub struct Pending {
    pub id: u64,
    pub context: String,
    pub task: Task,
    pub cancel: Arc<AtomicBool>,
}
#[derive(Default)]
pub struct Actions {
    pub open: bool,
    pub operation: Operation,
    pub selected: [bool; 8],
    pub plan_id: String,
    pub run_id: String,
    pub target: String,
    pub field: Option<Field>,
    pub buffer: String,
    pub capabilities: Option<Value>,
    pub stage: Stage,
    pub pending: Option<Pending>,
    pub preview: Option<(Intent, String, f64)>,
    pub reviewed_receipt: Option<ReviewedReceipt>,
    pub report: Option<WorkflowReport>,
    pub notice: String,
    pub progress: String,
    pub scroll: usize,
    pub scroll_max: usize,
    pub handoff: Option<usize>,
    pub details: bool,
    pub epoch: u64,
    pub preview_context: Option<String>,
    pub capabilities_context: Option<String>,
}
impl Actions {
    pub fn data(&self) -> Option<&Value> {
        self.capabilities.as_ref()?.get("data")
    }
    pub fn metadata(&self, id: &str) -> Option<&Value> {
        self.data()?
            .get("actions")?
            .as_array()?
            .iter()
            .find(|item| item.get("id").and_then(Value::as_str) == Some(id))
    }
    pub fn supported(&self) -> bool {
        let Some(data) = self.data() else {
            return false;
        };
        let protocol = data.get("operations");
        if protocol.is_some_and(|p| {
            p.get("contractVersion").and_then(Value::as_u64) == Some(1)
                && p.get("previewFlag").and_then(Value::as_str) == Some("--preview")
                && p.get("tokenFlag").and_then(Value::as_str) == Some("--preview-token")
                && p.get("confirmFlag").and_then(Value::as_str) == Some("--yes")
        }) {
            return true;
        }
        data.get("commands")
            .and_then(Value::as_array)
            .and_then(|commands| {
                commands.iter().find(|item| {
                    item.get("id").and_then(Value::as_str) == Some(self.operation.name())
                })
            })
            .and_then(|item| item.get("flags"))
            .and_then(Value::as_array)
            .is_some_and(|flags| {
                ["--preview", "--preview-token", "--yes"]
                    .iter()
                    .all(|flag| {
                        flags
                            .iter()
                            .any(|item| item.get("flag").and_then(Value::as_str) == Some(flag))
                    })
            })
    }
    pub fn selectable(&self, id: &str) -> bool {
        self.metadata(id).is_some_and(|m| {
            let policy = m.get("policyEnabled").and_then(Value::as_bool);
            let opt_in = m.get("requiresPolicyOptIn").and_then(Value::as_bool);
            matches!(
                (policy, opt_in),
                (Some(true), Some(_)) | (Some(false), Some(false))
            )
        })
    }
    pub fn intent(&self) -> Result<Intent, String> {
        if !self.supported() {
            return Err("Native preview protocol unavailable; no legacy execution fallback".into());
        }
        let mut actions = Vec::new();
        if self.operation == Operation::Maintain {
            for (_, id) in ACTION_IDS
                .iter()
                .enumerate()
                .filter(|(index, _)| self.selected[*index])
            {
                if !self.selectable(id) {
                    return Err(format!(
                        "{id}: backend policy opt-in required or metadata unavailable"
                    ));
                }
                let flag = self
                    .metadata(id)
                    .and_then(|m| m.get("flag"))
                    .and_then(Value::as_str)
                    .ok_or("Backend action flag unavailable")?;
                actions.push((id.to_string(), flag.to_string()));
            }
        }
        let intent = Intent {
            operation: self.operation,
            plan_id: self.plan_id.clone(),
            run_id: self.run_id.clone(),
            target: self.target.clone(),
            actions,
        };
        intent.argv(None)?;
        Ok(intent)
    }
    pub fn changed(&mut self) {
        self.details = false;
        self.preview = None;
        self.preview_context = None;
        self.stage = Stage::Editing;
        self.scroll = 0;
        self.handoff = None;
        self.notice = "Selection changed · preview again.".into();
    }
    pub fn invalidate(&mut self) {
        self.epoch += 1;
        if let Some(pending) = self.pending.take() {
            pending.cancel.store(true, Ordering::Relaxed);
        }
        self.capabilities = None;
        self.capabilities_context = None;
        self.preview_context = None;
        self.preview = None;
        self.selected = [false; 8];
        self.plan_id.clear();
        self.run_id.clear();
        self.target.clear();
        self.buffer.clear();
        self.handoff = None;
        self.stage = Stage::Result;
        self.field = None;
        self.notice = "Context changed · preview cleared. Started operations have unknown external effects; check before retrying.".into();
    }
    pub fn cancel(&mut self) {
        self.preview = None;
        self.field = None;
        if let Some(pending) = &self.pending {
            pending.cancel.store(true, Ordering::Relaxed);
            self.notice = "Stop requested · effects unknown. Completed changes remain.".into();
        } else {
            let result = self.stage == Stage::Result;
            self.stage = Stage::Editing;
            self.notice = if result {
                "Review closed · result retained in details."
            } else {
                "Cancelled · no execute request sent."
            }
            .into();
        }
    }
    pub fn accept(&mut self, task: &Task, report: WorkflowReport, now: i64) {
        self.stage = Stage::Result;
        self.preview = None;
        self.scroll = 0;
        if matches!(task, Task::Capabilities) && report.is_success() {
            self.capabilities = report.response.as_ref().map(|r| r.payload.clone());
            self.stage = Stage::Editing;
            self.notice =
                "Select caches to maintain.".into();
        } else if let Task::Preview(intent) = task {
            let data = report.response.as_ref().map(|r| &r.payload["data"]);
            let valid = data.filter(|d| {
                report.is_success()
                    && d["contractVersion"].as_u64() == Some(1)
                    && d["kind"] == "preview"
                    && d["operation"] == intent.operation.name()
                    && d["state"] == "ready"
                    && intent.matches_request(&d["request"])
                    && d["operationId"].as_str().is_some_and(|s| !s.is_empty())
                    && d["policyFingerprint"].is_string()
                    && d["sourceFingerprint"].is_string()
                    && d["preview"].is_object()
                    && d["result"].is_null()
                    && d["errors"].as_array().is_some_and(Vec::is_empty)
                    && d["unknown"].as_array().is_some_and(Vec::is_empty)
                    && d["previewToken"].as_str().is_some_and(|s| !s.is_empty())
                    && d["expiresAt"]
                        .as_f64()
                        .is_some_and(|expires| expires > now as f64)
            });
            if let Some(data) = valid {
                self.reviewed_receipt = Some(ReviewedReceipt::from_preview(intent.operation, data));
                self.preview = Some((
                    intent.clone(),
                    data["previewToken"].as_str().unwrap().into(),
                    data["expiresAt"].as_f64().unwrap(),
                ));
                self.stage = Stage::Review;
                self.notice = "Review targets and effects below.".into();
            } else {
                self.notice = "Preview unavailable · check details and preview again.".into();
            }
        } else if matches!(task, Task::Execute(..)) {
            self.notice = if report.error.is_some() || report.response.as_ref()
                .and_then(|r| r.payload.pointer("/data/state")).and_then(Value::as_str) == Some("unknown") {
                "Outcome unknown · check details before retrying."
            } else {
                "Results received · check failures below."
            }.into();
        } else if matches!(task, Task::Plan) {
            self.notice = if report.is_success() { "Plan saved · enter its ID for Run plan." } else { "Plan not saved · check errors." }.into();
        } else {
            self.notice =
                "Capabilities unavailable · check errors, then c to reload.".into();
        }
        self.report = Some(report);
    }
    /// A compact task view. Complete receipts remain available via details.
    pub fn summary_text(&self) -> String {
        let mut lines = if self.stage == Stage::Review { Vec::new() } else { vec![self.notice.clone(), String::new()] };
        match self.operation {
            Operation::Maintain if matches!(self.stage, Stage::Editing | Stage::Busy) => {
                for (index, id) in ACTION_IDS.iter().enumerate() {
                    let state = if self.selectable(id) { "" }
                        else if self.metadata(id).and_then(|m| m["requiresPolicyOptIn"].as_bool()) == Some(true) { " · opt-in required" }
                        else { " · unavailable" };
                    lines.push(format!("{}  [{}] {id}{state}", index + 1, if self.selected[index] { "x" } else { " " }));
                }
                for (index, id) in ACTION_IDS.iter().enumerate().filter(|(index, _)| self.selected[*index]) {
                    if let Some(effect) = self.metadata(id).and_then(|m| m["sideEffects"].as_str()) {
                        lines.push(format!("\n{} · {effect}", ACTION_IDS[index]));
                    }
                }
            }
            Operation::Run => lines.push(format!("Plan  {}", if self.plan_id.is_empty() { "i to enter a saved plan ID" } else { &self.plan_id })),
            Operation::Restore => {
                lines.push(format!("Run     {}", if self.run_id.is_empty() { "r to enter run ID" } else { &self.run_id }));
                lines.push(format!("Target  {}", if self.target.is_empty() { "t to enter a new absolute path" } else { &self.target }));
            }
            Operation::Purge => lines.push("Delete expired quarantine items after preview.".into()),
            _ => {}
        }
        if let Some(field) = self.field {
            lines.push(format!("\n{field:?}  {}\n↵ keep   Esc cancel   Ctrl-U clear", self.buffer));
        }
        if !self.supported() { lines.push("Preview unavailable · reload capabilities with c.".into()); }
        if let Some(report) = &self.report {
            if let Some(error) = &report.error { lines.push(format!("\n! {error}")); }
            if let Some(response) = &report.response {
                if !response.status.success() { lines.push(format!("! {}", response.status)); }
                if matches!(self.stage, Stage::Review | Stage::Confirm | Stage::Result) {
                    let data = &response.payload["data"];
                    let state = data["state"].as_str().unwrap_or("Report");
                    if state != "ready" {
                        let state = match state {
                            "complete" if report.is_success() => "✓ Complete",
                            "partial" => "! Partially completed",
                            "unknown" => "? Outcome unknown",
                            other => other,
                        };
                        lines.push(format!("\n{state}"));
                    }
                    let content = if data["kind"] == "preview" { &data["preview"] } else { data.get("result").filter(|v| !v.is_null()).unwrap_or(data) };
                    compact_evidence(content, &mut lines);
                    for key in ["errors", "unknown"] {
                        if let Some(values) = data[key].as_array().filter(|values| !values.is_empty()) {
                            let issue = format!("! {key}: {}", values.iter().map(short_value).collect::<Vec<_>>().join(" · "));
                            if !lines.contains(&issue) { lines.push(issue); }
                        }
                    }
                    if let Some(reason) = data.pointer("/refreshedStatus/reason").and_then(Value::as_str) {
                        if data["kind"] != "preview" { lines.push(format!("Status: {reason}")); }
                    }
                }
            }
            if !report.stderr.is_empty() { lines.push(format!("\n! {} · d for full output", report.stderr.lines().next().unwrap_or(""))); }
        }
        if !self.progress.is_empty() {
            lines.push(format!("\n↻ {}", crate::backend::strip_ansi(&self.progress).lines().last().unwrap_or("")));
        }
        if self.stage == Stage::Confirm {
            lines.push("\ny execute   Esc cancel · changes may be irreversible".into());
        }
        lines.join("\n")
    }

    pub fn text(&self) -> String {
        if let Some(index) = self.handoff {
            let id = ["init", "check", "gc"][index % 3];
            let metadata = self
                .data()
                .and_then(|d| d["commands"].as_array())
                .and_then(|items| items.iter().find(|item| item["id"] == id));
            return format!("CLI HANDOFF ONLY: {id}\nNative execution is unavailable for this command. Opening this page executes nothing.\nh next handoff · Esc back · q close · ↑↓ scroll\n\nPress q to close actions, then q to exit TUI; invoke the fixed argv explicitly in your terminal.\ncheck uses envy storage check --json (restic internally --read-data). gc has no JSON and deletes old generations; it differs from maintain --nix.\n\nBackend-provided fixed argv, safe target, parameters, side effects and exit behavior:\n{}\n", metadata.map(|m| serde_json::to_string_pretty(&m["handoff"]).unwrap_or_default()).unwrap_or_else(|| "Backend handoff metadata unavailable; target unknown".into()));
        }
        let mut text = format!("STORAGE ACTIONS · {} · {:?}\nTab operation · 1–8 toggle maintenance · i plan ID · r run ID · t restore target\ns save plan (scan + local disk write) · p preview (saves receipt) · Enter review → confirm · y execute only in confirm\nc reload capabilities · h CLI handoff · Esc cancel/back · q close actions · ↑↓/PgUp/PgDn/Home/End scroll\n\n{}\n\nPlan ID: {}\nRun ID: {}\nNew absolute target: {}\n", self.operation.name(), self.stage, self.notice, self.plan_id, self.run_id, self.target);
        if !self.supported() {
            text.push_str("Native operations unavailable: backend preview/token protocol required. No legacy fallback.\n");
        }
        if let Some(field) = self.field {
            text.push_str(&format!(
                "\nEditing {field:?}: {}\nEnter save · Esc cancel · Ctrl-U clear\n",
                self.buffer
            ));
        }
        text.push_str(
            "\nMaintenance selection (all default false; backend policy decides availability):\n",
        );
        for (index, id) in ACTION_IDS.iter().enumerate() {
            let metadata = self.metadata(id);
            text.push_str(&format!(
                "{} [{}] {} · policyEnabled={} · requiresPolicyOptIn={} · {}\n",
                index + 1,
                if self.selected[index] { "x" } else { " " },
                id,
                metadata
                    .map(|m| m["policyEnabled"].to_string())
                    .unwrap_or_else(|| "unknown".into()),
                metadata
                    .map(|m| m["requiresPolicyOptIn"].to_string())
                    .unwrap_or_else(|| "unknown".into()),
                metadata
                    .and_then(|m| m["sideEffects"].as_str())
                    .unwrap_or("metadata unavailable")
            ));
        }
        if self.stage == Stage::Confirm {
            text.push_str("\nEXPLICIT CONFIRMATION: y executes the reviewed request once; Esc cancels. Backend revalidates token and targets.\n");
        }
        if !self.progress.is_empty() {
            text.push_str(&format!(
                "\nBounded stderr progress:\n{}\n",
                crate::backend::strip_ansi(&self.progress)
            ));
        }
        if let Some(receipt) = &self.reviewed_receipt {
            text.push_str(&format!(
                "\nLast reviewed receipt (evidence only; no execution token; cannot replay):\n{}\n",
                serde_json::to_string_pretty(&receipt.evidence()).unwrap_or_default()
            ));
        }
        if let Some(report) = &self.report {
            text.push_str(&format!(
                "\nTransport error: {}\nProcess status: {}\nComplete stderr:\n{}\n",
                report.error.as_deref().unwrap_or("none"),
                report
                    .response
                    .as_ref()
                    .map(|r| r.status.to_string())
                    .unwrap_or_else(|| "unknown".into()),
                report.stderr
            ));
            if let Some(response) = &report.response {
                text.push_str(&format!("\nComplete backend envelope (including errors, unknown and refreshedStatus):\n{}\n", serde_json::to_string_pretty(&response.payload).unwrap_or_default()));
            }
        }
        text
    }
}

fn short_value(value: &Value) -> String {
    value.as_str().map(str::to_string).unwrap_or_else(|| value.to_string())
}

/// Omit transport identities from the summary; targets and effects stay visible.
fn compact_evidence(value: &Value, lines: &mut Vec<String>) {
    if let Some(object) = value.as_object() {
        for (key, value) in object {
            if matches!(key.as_str(), "binding" | "targetBinding" | "policy" | "manifestDigest" | "signature" | "sha256" | "commands" | "output" | "execute" | "available") || value.is_null() { continue; }
            if key == "items" {
                if let Some(items) = value.as_array() {
                    lines.push(format!("{} item(s)", items.len()));
                    for item in items {
                        let id = item["action"].as_str().or_else(|| item["id"].as_str()).or_else(|| item["rule"].as_str()).unwrap_or("");
                        let path = item["path"].as_str().or_else(|| item.pointer("/target/path").and_then(Value::as_str)).or_else(|| item["target"].as_str()).unwrap_or("");
                        let exits = item["output"].as_array().into_iter().flatten().filter_map(|entry| entry["returncode"].as_i64()).collect::<Vec<_>>();
                        let failed = exits.iter().any(|code| *code != 0) || item["available"] == false;
                        let marker = if failed { "!" } else { "›" };
                        lines.push(format!("\n{marker} {id}  {path}"));
                        if let Some(target) = item["target"].as_object() {
                            let target = target.iter().filter(|(key, _)| key.as_str() != "path").map(|(key, value)| (key.clone(), value.clone())).collect();
                            compact_evidence(&Value::Object(target), lines);
                        } else if !item["target"].is_null() && !item["target"].is_string() {
                            compact_evidence(&item["target"], lines);
                        }
                        if let Some(status) = item["status"].as_str() { lines.push(status.into()); }
                        if let Some(note) = item["note"].as_str() { lines.push(note.replace(" Read-only preview; no cleanup command was run.", "")); }
                        for code in exits.iter().filter(|code| **code != 0) { lines.push(format!("! Exit {code}")); }
                        if item["available"] == false { lines.push("! Unavailable".into()); }
                        for key in ["error", "errors", "unknown", "reason", "reasons"] {
                            if let Some(value) = item.get(key).filter(|v| !v.is_null() && !v.as_array().is_some_and(Vec::is_empty)) {
                                lines.push(format!("! {key}: {}", short_value(value)));
                            }
                        }
                        if id.is_empty() && path.is_empty() { compact_evidence(item, lines); }
                    }
                }
            } else if value.is_object() {
                lines.push(format!("{key}:"));
                compact_evidence(value, lines);
            } else if value.as_array().is_some_and(Vec::is_empty) { continue; }
            else if matches!(key.as_str(), "errors" | "unknown") {
                let items = value.as_array().map(|items| items.iter().map(short_value).collect::<Vec<_>>().join(" · ")).unwrap_or_else(|| short_value(value));
                lines.push(format!("! {key}: {items}"));
            } else if key == "note" {
                lines.push(short_value(value).replace(" Read-only preview; no cleanup command was run.", ""));
            } else {
                lines.push(format!("{key}: {}", short_value(value)));
            }
        }
    } else { lines.push(short_value(value)); }
}

#[cfg(test)]
mod tests;
