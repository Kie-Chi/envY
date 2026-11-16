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
        self.preview = None;
        self.preview_context = None;
        self.stage = Stage::Editing;
        self.scroll = 0;
        self.handoff = None;
        self.notice = "Selection changed; a fresh preview is required".into();
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
        self.notice = "Context invalidated; token discarded. Any started operation has unknown external effects; inspect before retrying.".into();
    }
    pub fn cancel(&mut self) {
        self.preview = None;
        self.field = None;
        if let Some(pending) = &self.pending {
            pending.cancel.store(true, Ordering::Relaxed);
            self.notice = "Stop requested. Started external effects are unknown and are not rolled back. No automatic replay.".into();
        } else {
            let result = self.stage == Stage::Result;
            self.stage = Stage::Editing;
            self.notice = if result {
                "Closed review; previous process evidence remains below"
            } else {
                "Cancelled; no execute request sent"
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
                "Capabilities loaded; native maintenance starts with no selections".into();
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
                self.notice = "Review the complete preview. Enter opens explicit confirmation; it does not execute.".into();
            } else {
                self.notice = "Preview not ready, expired, failed or mismatched. Review evidence and request a fresh preview.".into();
            }
        } else if matches!(task, Task::Execute(..)) {
            self.notice = if report.error.is_some() || report.response.as_ref()
                .and_then(|r| r.payload.pointer("/data/state")).and_then(Value::as_str) == Some("unknown") {
                "Execution outcome may be unknown. Review process evidence and backend status before a new operation; no rollback or replay."
            } else {
                "Execution returned. Review state/errors/unknown and refreshedStatus below; no automatic replay or inspection."
            }.into();
        } else if matches!(task, Task::Plan) {
            self.notice = "Plan request returned; read its saved ID and enter it explicitly before run preview.".into();
        } else {
            self.notice =
                "Capabilities unavailable; review the backend error and reload explicitly".into();
        }
        self.report = Some(report);
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

#[cfg(test)]
mod tests;
