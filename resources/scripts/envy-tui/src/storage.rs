//! Read-only Storage requests and lossless report presentation.

pub mod actions;

use serde_json::Value;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub enum StorageView {
    #[default]
    Inspect,
    Status,
    Validate,
}

impl StorageView {
    pub const ALL: [Self; 3] = [Self::Inspect, Self::Status, Self::Validate];

    pub fn command(self) -> &'static str {
        match self {
            Self::Inspect => "inspect",
            Self::Status => "status",
            Self::Validate => "validate",
        }
    }

    pub fn title(self) -> &'static str {
        match self {
            Self::Inspect => "Inspection",
            Self::Status => "Policy & records",
            Self::Validate => "Rule validation",
        }
    }

    pub fn description(self) -> &'static str {
        match self {
            Self::Inspect => {
                "Report only · bytes are estimates · rows overlap and must not be added"
            }
            Self::Status => {
                "Read configured rules and saved records · may initialize and lock local state"
            }
            Self::Validate => {
                "organize check · writes temporary rule config, then removes it · no cleanup"
            }
        }
    }

    pub fn next(self) -> Self {
        match self {
            Self::Inspect => Self::Status,
            Self::Status => Self::Validate,
            Self::Validate => Self::Inspect,
        }
    }
}

/// Include the complete request in cache and in-flight identities. Project paths
/// remain exactly as supplied; the backend validates their explicit scope.
#[derive(Clone, Debug, Default, PartialEq, Eq, Hash)]
pub struct StorageRequest {
    pub view: StorageView,
    pub projects: Vec<String>,
}

impl StorageRequest {
    pub fn argv(&self) -> Result<Vec<String>, String> {
        let mut args = vec![
            "storage".into(),
            self.view.command().into(),
            "--json".into(),
        ];
        if self.view == StorageView::Inspect {
            for project in &self.projects {
                if project.trim().is_empty() || project.contains('\0') {
                    return Err("Enter a non-empty project directory without NUL characters".into());
                }
                // The equals form also keeps a leading '-' inside the option value.
                args.push(format!("--project={project}"));
            }
        }
        Ok(args)
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StorageProcess {
    pub exit_code: Option<i32>,
    pub success: bool,
    pub stderr: String,
}

#[derive(Clone, Debug)]
pub struct StorageReport {
    pub view: StorageView,
    pub payload: Value,
    /// Absent for restored payloads; never present them as a fresh process result.
    pub process: Option<StorageProcess>,
}

impl StorageReport {
    pub fn from_payload(view: StorageView, payload: Value) -> Self {
        Self {
            view,
            payload,
            process: None,
        }
    }

    pub fn from_response(view: StorageView, response: crate::transport::JsonResponse) -> Self {
        Self {
            view,
            payload: response.payload,
            process: Some(StorageProcess {
                exit_code: response.status.code(),
                success: response.status.success(),
                stderr: response.stderr,
            }),
        }
    }

    /// Preserve valid business failures and partial data. Mutations must use the
    /// backend's success-only path instead of this report loader.
    pub fn load(request: &StorageRequest) -> Result<Self, String> {
        let args = request.argv()?;
        let refs = args.iter().map(String::as_str).collect::<Vec<_>>();
        crate::backend::run_json_response(&refs)
            .map_err(|error| error.to_string())
            .and_then(|response| {
                let expected = format!("storage {}", request.view.command());
                crate::transport::validate_envelope(&response.payload, &expected)
                    .map_err(|error| error.to_string())?;
                Ok(Self::from_response(request.view, response))
            })
    }

    pub fn summary(&self) -> String {
        let rows = self.rows();
        let failed = self.payload.get("ok").and_then(Value::as_bool) != Some(true)
            || self
                .process
                .as_ref()
                .is_some_and(|process| !process.success)
            || rows.iter().any(|row| row.status == "error");
        let warnings = rows.iter().filter(|row| {
            matches!(row.status.as_str(), "warning" | "partial")
                || (row.status == "unknown" && self.value(row).and_then(|v| v.get("status")).is_some())
        }).count();
        if failed {
            let exit = self.process.as_ref().filter(|p| !p.success)
                .and_then(|p| p.exit_code).map(|code| format!(" · exit {code}")).unwrap_or_default();
            format!("! Storage incomplete{exit}")
        }
        else if warnings > 0 { format!("! Storage · {warnings} need attention") }
        else { "✓ Storage ready".into() }
    }

    pub fn data(&self) -> Option<&Value> {
        self.payload.get("data").filter(|value| value.is_object())
    }

    pub fn rows(&self) -> Vec<StorageRow> {
        let mut rows = vec![StorageRow::new(
            StorageRowKind::Report,
            "Complete report",
            "",
            &self.payload,
        )];
        rows[0].status = match self.payload.get("ok").and_then(Value::as_bool) {
            Some(true)
                if !self
                    .process
                    .as_ref()
                    .is_some_and(|process| !process.success) =>
            {
                "ok"
            }
            Some(_) => "partial / failed",
            None => "unknown",
        }
        .into();
        let expected = format!("storage {}", self.view.command());
        let envelope_error = crate::transport::validate_envelope(&self.payload, &expected).err();
        if let Some(error) = &envelope_error {
            rows[0].status = "protocol error".into();
            rows.push(StorageRow::issue("", error.to_string()));
        }
        if let Some(process) = &self.process {
            if !process.success || !process.stderr.is_empty() {
                let mut row = StorageRow::new(
                    StorageRowKind::Diagnostic,
                    "Process result",
                    "",
                    &Value::Null,
                );
                row.status = if process.success { "warning" } else { "error" }.into();
                row.note = format!(
                    "exit: {}; {}",
                    process
                        .exit_code
                        .map(|code| code.to_string())
                        .unwrap_or_else(|| "signal".into()),
                    process.stderr
                );
                rows.push(row);
            }
        }
        if self
            .payload
            .get("error")
            .is_some_and(|value| !value.is_null())
        {
            let error = &self.payload["error"];
            let mut row =
                StorageRow::new(StorageRowKind::Diagnostic, "Backend error", "/error", error);
            row.status = "error".into();
            row.note = error.to_string();
            rows.push(row);
        }
        add_messages(&mut rows, &self.payload, "warnings", "", "warning");
        if envelope_error.is_some() {
            return rows;
        }
        let Some(data) = self.data() else {
            rows.push(StorageRow::issue(
                "/data",
                "Storage response data is unavailable or is not an object",
            ));
            return rows;
        };
        add_messages(&mut rows, data, "errors", "/data", "error");
        add_messages(&mut rows, data, "warnings", "/data", "warning");
        match self.view {
            StorageView::Inspect => self.inspect_rows(data, &mut rows),
            StorageView::Status => self.status_rows(data, &mut rows),
            StorageView::Validate => add_array(
                &mut rows,
                data,
                "rules",
                "/data",
                StorageRowKind::Rule,
                true,
            ),
        }
        rows
    }

    fn inspect_rows(&self, data: &Value, rows: &mut Vec<StorageRow>) {
        add_array(
            rows,
            data,
            "items",
            "/data",
            StorageRowKind::Filesystem,
            true,
        );
        let Some(reports) = data.get("reports").and_then(Value::as_object) else {
            rows.push(StorageRow::issue(
                "/data/reports",
                "Expected an object of Storage provider reports",
            ));
            return;
        };
        for (name, report) in reports {
            let pointer = format!("/data/reports/{}", pointer_key(name));
            add_object(rows, StorageRowKind::Provider, name, &pointer, report);
            match name.as_str() {
                "docker" => {
                    // Provider-level failures may have no native inventory at all.
                    // Preserve their unknown report instead of inventing group data.
                    if report.get("status").and_then(Value::as_str) == Some("unknown")
                        && ["images", "buildCache", "containers", "volumes"]
                            .iter()
                            .all(|key| report.get(key).is_none())
                    {
                        continue;
                    }
                    for group in ["images", "buildCache", "containers", "volumes"] {
                        let group_pointer = format!("{pointer}/{group}");
                        if let Some(value) = report.get(group) {
                            add_object(
                                rows,
                                StorageRowKind::DockerGroup,
                                group,
                                &group_pointer,
                                value,
                            );
                            add_array(
                                rows,
                                value,
                                "items",
                                &group_pointer,
                                StorageRowKind::DockerObject,
                                true,
                            );
                        } else {
                            rows.push(StorageRow::issue(
                                &group_pointer,
                                "Docker object group is unavailable",
                            ));
                        }
                    }
                    add_array(
                        rows,
                        report,
                        "projects",
                        &pointer,
                        StorageRowKind::Project,
                        false,
                    );
                }
                "applications" => {
                    if let Some(applications) = report.get("reports").and_then(Value::as_object) {
                        for (application, value) in applications {
                            add_object(
                                rows,
                                StorageRowKind::Application,
                                application,
                                &format!("{pointer}/reports/{}", pointer_key(application)),
                                value,
                            );
                        }
                    } else if report.get("reports").is_some() {
                        rows.push(StorageRow::issue(
                            &pointer,
                            "Expected application reports to be an object",
                        ));
                    }
                }
                "projects" => add_array(
                    rows,
                    report,
                    "projects",
                    &pointer,
                    StorageRowKind::Project,
                    false,
                ),
                _ => {}
            }
        }
    }

    fn status_rows(&self, data: &Value, rows: &mut Vec<StorageRow>) {
        for key in [
            "mode",
            "excluded",
            "maintenance",
            "archiveConfigured",
            "archive",
            "latestPlan",
        ] {
            if let Some(value) = data.get(key) {
                rows.push(StorageRow::new(
                    StorageRowKind::Record,
                    key,
                    &format!("/data/{key}"),
                    value,
                ));
            }
        }
        add_array(
            rows,
            data,
            "ruleDetails",
            "/data",
            StorageRowKind::Rule,
            true,
        );
        // Keep older or incomplete rule IDs visible even when details are absent.
        if let Some(ids) = data.get("rules").and_then(Value::as_array) {
            for (index, id) in ids.iter().enumerate() {
                let found = data
                    .get("ruleDetails")
                    .and_then(Value::as_array)
                    .is_some_and(|rules| rules.iter().any(|rule| rule.get("id") == Some(id)));
                if !found {
                    rows.push(StorageRow::new(
                        StorageRowKind::Rule,
                        &display(Some(id)),
                        &format!("/data/rules/{index}"),
                        id,
                    ));
                }
            }
        }
        add_array(rows, data, "runs", "/data", StorageRowKind::Record, true);
    }

    pub fn value<'a>(&'a self, row: &StorageRow) -> Option<&'a Value> {
        self.payload.pointer(&row.pointer)
    }

    /// Only project relationships already supplied by the backend. Unknown
    /// inventories remain unknown, including observed references to missing rows.
    pub fn associations(&self, row: &StorageRow) -> Vec<StorageAssociation> {
        let Some(value) = self.value(row) else {
            return Vec::new();
        };
        let rows = self.rows();
        let find = |kind: &str, id: &str| {
            rows.iter()
                .find(|candidate| {
                    candidate
                        .pointer
                        .starts_with(&format!("/data/reports/docker/{kind}/items/"))
                        && self
                            .value(candidate)
                            .and_then(|value| value.get("id"))
                            .and_then(Value::as_str)
                            == Some(id)
                })
                .map(|candidate| candidate.pointer.clone())
        };
        let mut links = Vec::new();
        if row
            .pointer
            .starts_with("/data/reports/docker/containers/items/")
        {
            let status = self
                .payload
                .pointer("/data/reports/docker/containers/associationStatus")
                .and_then(Value::as_str)
                .unwrap_or("unknown");
            if let Some(id) = value.get("imageId").and_then(Value::as_str) {
                links.push(StorageAssociation::new(
                    "image",
                    id,
                    status,
                    find("images", id),
                ));
            }
            if let Some(mounts) = value.get("mounts").and_then(Value::as_array) {
                for mount in mounts {
                    if mount.get("type").and_then(Value::as_str) == Some("volume") {
                        if let Some(id) = mount.get("name").and_then(Value::as_str) {
                            links.push(StorageAssociation::new(
                                "volume",
                                id,
                                status,
                                find("volumes", id),
                            ));
                        }
                    }
                }
            }
        }
        if let Some(association) = value.get("association") {
            let status = association
                .get("status")
                .and_then(Value::as_str)
                .unwrap_or("unknown");
            let key = if status == "complete" {
                "referencedBy"
            } else {
                "observedReferencedBy"
            };
            if let Some(ids) = association.get(key).and_then(Value::as_array) {
                for id in ids.iter().filter_map(Value::as_str) {
                    links.push(StorageAssociation::new(
                        "container",
                        id,
                        status,
                        find("containers", id),
                    ));
                }
            }
        }
        for key in ["includedIn", "itemId"] {
            if let Some(id) = value.get(key).and_then(Value::as_str) {
                let target = rows
                    .iter()
                    .find(|candidate| {
                        candidate.kind == StorageRowKind::Filesystem
                            && self
                                .value(candidate)
                                .and_then(|value| value.get("id"))
                                .and_then(Value::as_str)
                                == Some(id)
                    })
                    .map(|candidate| candidate.pointer.clone());
                links.push(StorageAssociation::new(key, id, "reported", target));
            }
        }
        links
    }

    pub fn detail_text(&self, row: &StorageRow) -> String {
        let value = self.value(row).unwrap_or(&Value::Null);
        let mut detail = format!(
            "{} · {}\nStatus: {}\nPath: {}\n{}\n\n{}\n",
            row.kind.label(),
            row.label,
            row.status,
            row.path,
            row.note,
            self.view.description()
        );
        if let Some(process) = &self.process {
            detail.push_str(&format!(
                "\nProcess exit: {}\nStderr:\n{}\n",
                process
                    .exit_code
                    .map(|code| code.to_string())
                    .unwrap_or_else(|| "signal".into()),
                process.stderr
            ));
        }
        let associations = self.associations(row);
        if !associations.is_empty() {
            detail.push_str("\nReported associations (unknown is not unreferenced):\n");
            for association in associations {
                detail.push_str(&format!(
                    "{}: {} · {} · {}\n",
                    association.relation,
                    association.id,
                    association.status,
                    association
                        .pointer
                        .as_deref()
                        .unwrap_or("target unavailable")
                ));
            }
        }
        detail.push_str("\nSelected value JSON:\n");
        detail.push_str(&serde_json::to_string_pretty(value).unwrap_or_else(|_| value.to_string()));
        detail.push_str("\n\nComplete backend envelope JSON:\n");
        detail.push_str(
            &serde_json::to_string_pretty(&self.payload)
                .unwrap_or_else(|_| self.payload.to_string()),
        );
        detail
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StorageRowKind {
    Report,
    Diagnostic,
    Filesystem,
    Provider,
    DockerGroup,
    DockerObject,
    Application,
    Project,
    Rule,
    Record,
}

impl StorageRowKind {
    pub fn label(self) -> &'static str {
        match self {
            Self::Report => "report",
            Self::Diagnostic => "notice",
            Self::Filesystem => "path",
            Self::Provider => "provider",
            Self::DockerGroup => "Docker",
            Self::DockerObject => "object",
            Self::Application => "app",
            Self::Project => "project",
            Self::Rule => "rule",
            Self::Record => "record",
        }
    }
}

/// Integer bytes only: missing, null, negative, fractional, and overflowing
/// values are unknown. Observed lower bounds never replace unknown estimates.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct StorageMeasurements {
    pub logical: Option<u64>,
    pub allocated: Option<u64>,
    pub native_reclaimable: Option<u64>,
    pub policy_eligible: Option<u64>,
}

impl StorageMeasurements {
    pub fn from_value(value: &Value) -> Self {
        let bytes = |key| value.get(key).and_then(Value::as_u64);
        Self {
            logical: bytes("logicalBytes"),
            allocated: bytes("allocatedBytes"),
            native_reclaimable: bytes("nativeReclaimableBytes"),
            policy_eligible: bytes("policyEligibleBytes"),
        }
    }
}

pub fn bytes_text(value: Option<u64>) -> String {
    value
        .map(|value| value.to_string())
        .unwrap_or_else(|| "unknown".into())
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StorageRow {
    pub kind: StorageRowKind,
    pub label: String,
    pub status: String,
    pub path: String,
    pub note: String,
    pub measurements: StorageMeasurements,
    pub pointer: String,
}

impl StorageRow {
    fn new(kind: StorageRowKind, fallback: &str, pointer: &str, value: &Value) -> Self {
        let first = |keys: &[&str]| {
            keys.iter()
                .find_map(|key| value.get(key).filter(|value| !value.is_null()))
        };
        let label = first(&["id", "rule", "project", "root", "requestedPath"])
            .map(|value| display(Some(value)))
            .unwrap_or_else(|| fallback.into());
        let status = first(&["status", "associationStatus"])
            .map(|value| display(Some(value)))
            .unwrap_or_else(|| {
                match (
                    value.get("skipped").and_then(Value::as_bool),
                    value.get("ok").and_then(Value::as_bool),
                ) {
                    (Some(true), _) => "skipped",
                    (_, Some(true)) => "ok",
                    (_, Some(false)) => "error",
                    _ => "unknown",
                }
                .into()
            });
        Self {
            kind,
            label,
            status,
            path: display(first(&["path", "root", "requestedPath"])),
            note: first(&["note", "reason", "error", "reasons"])
                .map(|value| display(Some(value)))
                .unwrap_or_else(|| {
                    if !value.is_object() {
                        display(Some(value))
                    } else {
                        String::new()
                    }
                }),
            measurements: StorageMeasurements::from_value(value),
            pointer: pointer.into(),
        }
    }

    fn issue(pointer: &str, message: impl Into<String>) -> Self {
        let mut row = Self::new(
            StorageRowKind::Diagnostic,
            "Invalid report shape",
            pointer,
            &Value::Null,
        );
        row.status = "error".into();
        row.note = format!("{pointer}: {}", message.into());
        row
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StorageAssociation {
    pub relation: String,
    pub id: String,
    pub status: String,
    pub pointer: Option<String>,
}

impl StorageAssociation {
    fn new(relation: &str, id: &str, status: &str, pointer: Option<String>) -> Self {
        Self {
            relation: relation.into(),
            id: id.into(),
            status: status.into(),
            pointer,
        }
    }
}

fn display(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => "unknown".into(),
        Some(Value::String(value)) => value.clone(),
        Some(value) => value.to_string(),
    }
}

fn pointer_key(key: &str) -> String {
    key.replace('~', "~0").replace('/', "~1")
}

fn add_object(
    rows: &mut Vec<StorageRow>,
    kind: StorageRowKind,
    label: &str,
    pointer: &str,
    value: &Value,
) {
    rows.push(StorageRow::new(kind, label, pointer, value));
    if !value.is_object() {
        rows.push(StorageRow::issue(pointer, "Expected a report object"));
        return;
    }
    for key in [
        "logicalBytes",
        "allocatedBytes",
        "nativeReclaimableBytes",
        "policyEligibleBytes",
    ] {
        if value
            .get(key)
            .is_some_and(|value| !value.is_null() && value.as_u64().is_none())
        {
            rows.push(StorageRow::issue(
                &format!("{pointer}/{key}"),
                "Expected non-negative integer bytes or null; shown as unknown",
            ));
        }
    }
}

fn add_array(
    rows: &mut Vec<StorageRow>,
    parent: &Value,
    key: &str,
    base: &str,
    kind: StorageRowKind,
    required: bool,
) {
    match parent.get(key) {
        Some(Value::Array(values)) => {
            for (index, value) in values.iter().enumerate() {
                add_object(
                    rows,
                    kind,
                    &format!("{key} {}", index + 1),
                    &format!("{base}/{key}/{index}"),
                    value,
                );
            }
        }
        None if !required => {}
        _ => rows.push(StorageRow::issue(
            &format!("{base}/{key}"),
            "Expected a list of report entries",
        )),
    }
}

fn add_messages(rows: &mut Vec<StorageRow>, parent: &Value, key: &str, base: &str, status: &str) {
    let Some(value) = parent.get(key) else { return };
    let Some(values) = value.as_array() else {
        rows.push(StorageRow::issue(
            &format!("{base}/{key}"),
            "Expected a list of diagnostics",
        ));
        return;
    };
    for (index, value) in values.iter().enumerate() {
        // The CLI often repeats data warnings in the envelope. Keep one visible
        // row while retaining both original arrays in every complete detail.
        let note = display(Some(value));
        if rows.iter().any(|row| {
            row.kind == StorageRowKind::Diagnostic && row.status == status && row.note == note
        }) {
            continue;
        }
        let mut row = StorageRow::new(
            StorageRowKind::Diagnostic,
            key,
            &format!("{base}/{key}/{index}"),
            value,
        );
        row.status = status.into();
        row.note = note;
        rows.push(row);
    }
}

#[cfg(test)]
mod tests;
