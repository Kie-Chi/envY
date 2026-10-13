use std::collections::HashMap;

use serde_json::Value;

/// The persistent navigation level. Leaf screens keep their own backend data
/// routes, while this enum defines the small, stable top-level information
/// architecture shown in the header.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NavigationSection {
    Home,
    Configure,
    Activity,
}

impl NavigationSection {
    pub const ALL: [Self; 3] = [Self::Home, Self::Configure, Self::Activity];

    pub fn title(self) -> &'static str {
        match self {
            Self::Home => "Home",
            Self::Configure => "Configure",
            Self::Activity => "Activity",
        }
    }

    pub fn index(self) -> usize {
        match self {
            Self::Home => 0,
            Self::Configure => 1,
            Self::Activity => 2,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Screen {
    Home,
    Software,
    Sources,
    Search,
    Doctor,
    History,
    Journal,
    Hosts,
    Mirror,
    Config,
}

impl Screen {
    /// Map a leaf/data view to the stable top-level section that contains it.
    /// Hosts and Config intentionally remain leaf screens so existing backend
    /// requests and cache identities stay unchanged.
    pub fn section(self) -> NavigationSection {
        match self {
            Self::Home => NavigationSection::Home,
            Self::Software
            | Self::Sources
            | Self::Search
            | Self::Hosts
            | Self::Mirror
            | Self::Config => NavigationSection::Configure,
            Self::Doctor | Self::History | Self::Journal => NavigationSection::Activity,
        }
    }

    pub fn title(self) -> &'static str {
        match self {
            Self::Home => "Home",
            Self::Software => "Software",
            Self::Sources => "Sources",
            Self::Search => "Search",
            Self::Doctor => "Doctor",
            Self::History => "History",
            Self::Journal => "Journal",
            Self::Hosts => "Hosts",
            Self::Mirror => "Mirror",
            Self::Config => "Config",
        }
    }
}

/// The two time-ordered Activity data views rendered through the History leaf.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub enum HistoryView {
    #[default]
    Generations,
    Operations,
}

impl HistoryView {
    /// The backing screen whose command and cached payload feed this view.
    pub fn screen(self) -> Screen {
        match self {
            Self::Generations => Screen::History,
            Self::Operations => Screen::Journal,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum LoadTarget {
    Screen(Screen),
    Search(String),
}

impl LoadTarget {
    pub fn title(&self) -> &'static str {
        match self {
            Self::Screen(screen) => screen.title(),
            Self::Search(_) => "Search",
        }
    }
}

#[derive(Debug, Default)]
pub struct PageState {
    pub payload: Option<Value>,
    pub loading: bool,
    pub error: Option<String>,
    pub request: Option<u64>,
}

impl PageState {
    pub fn has_content(&self) -> bool {
        self.payload.is_some()
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MutationStage {
    Preview,
    Apply,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SoftwareAction {
    Add,
    Remove,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum WorkflowAction {
    Plan,
    Apply,
    Doctor,
    SyncSource(String),
    OpenApp(String),
    OpenSettings(String),
}

impl WorkflowAction {
    pub fn title(&self) -> String {
        match self {
            Self::Plan => "Preview configuration".to_string(),
            Self::Apply => "Apply configuration".to_string(),
            Self::Doctor => "Verify with doctor".to_string(),
            Self::SyncSource(id) => format!("Sync source {id}"),
            Self::OpenApp(name) => format!("Open {name}"),
            Self::OpenSettings(_) => "Open System Settings".to_string(),
        }
    }

    pub fn command(&self, envy: String) -> (String, Vec<String>) {
        match self {
            Self::Plan => (envy, vec!["plan".to_string()]),
            Self::Apply => (envy, vec!["apply".to_string()]),
            Self::Doctor => (envy, vec!["doctor".to_string()]),
            Self::SyncSource(id) => (
                envy,
                vec![
                    "sw".into(),
                    "source".into(),
                    "sync".into(),
                    id.clone(),
                    "--yes".into(),
                ],
            ),
            Self::OpenApp(name) => ("open".to_string(), vec!["-a".to_string(), name.clone()]),
            Self::OpenSettings(target) => (
                "open".to_string(),
                vec![match target.as_str() {
                    "privacy-full-disk-access" => {
                        "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
                            .to_string()
                    }
                    _ => "x-apple.systempreferences:com.apple.preference.security".to_string(),
                }],
            ),
        }
    }

    pub fn display_command(&self) -> String {
        match self {
            Self::Plan => "envy plan".to_string(),
            Self::Apply => "envy apply".to_string(),
            Self::Doctor => "envy doctor".to_string(),
            Self::SyncSource(id) => format!("envy sw source sync {id:?} --yes"),
            Self::OpenApp(name) => format!("open -a {name:?}"),
            Self::OpenSettings(_) => "open macOS Privacy & Security settings".to_string(),
        }
    }

    /// Workflows that produce streamable terminal output can render inside a
    /// TUI log popup. GUI launchers (`open …`) have no output worth streaming
    /// and keep the existing leave-screen path.
    pub fn is_streamable(&self) -> bool {
        matches!(
            self,
            Self::Plan | Self::Apply | Self::Doctor | Self::SyncSource(_)
        )
    }
}

impl SoftwareAction {
    pub fn command(self) -> &'static str {
        match self {
            Self::Add => "add",
            Self::Remove => "rm",
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Add => "enable",
            Self::Remove => "disable",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MutationIntent {
    pub source_id: Option<String>,
    pub source_operation: bool,
    pub action: SoftwareAction,
    pub group: String,
    /// Operand passed to envY. Search uses a canonical registry reference.
    pub operand: String,
    /// Stable item ID that the verified plan must resolve to.
    pub item: String,
}

#[derive(Clone, Debug)]
pub struct MutationPreview {
    pub intent: MutationIntent,
    pub payload: Value,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SoftwareEntry {
    pub source_id: String,
    pub group: String,
    pub label: String,
    pub item: String,
    pub version: String,
    pub state: String,
    pub reference: String,
    pub effective: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SoftwareGroup {
    pub id: String,
    pub label: String,
    pub ecosystem: String,
    pub scope: String,
    pub kind: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SearchEntry {
    pub source_id: Option<String>,
    pub availability: String,
    pub source: String,
    pub ecosystem: String,
    pub kind: String,
    pub name: String,
    pub version: String,
    pub description: String,
    pub homepage: String,
    pub publisher: String,
    pub reference: String,
    pub managed_group: Option<String>,
}

#[derive(Clone, Debug)]
pub struct GroupChooser {
    pub entry: SearchEntry,
    pub groups: Vec<SoftwareGroup>,
    pub selected: usize,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MirrorSource {
    pub code: String,
    pub label: String,
    pub url: String,
    pub provider: String,
    pub ok: Option<bool>,
    pub http_status: Option<u16>,
    pub throughput_bps: Option<u64>,
    pub detail: Option<String>,
    pub stale: bool,
    pub measurement_stale: bool,
    pub fetched_at: Option<u64>,
    pub current: bool,
    pub selection_origin: Option<String>,
}

#[derive(Clone, Debug)]
pub struct MirrorChooser {
    pub target: String,
    pub label: String,
    pub profile: String,
    pub sources: Vec<MirrorSource>,
    pub selected: usize,
}

#[derive(Clone, Debug)]
pub struct MirrorPreview {
    pub target: String,
    pub source: MirrorSource,
    pub payload: Value,
}

/// A Configure/Settings value: either the active host or one managed config field.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SettingKey {
    Host,
    Config(String),
}

impl SettingKey {
    pub fn label(&self) -> String {
        match self {
            Self::Host => "host".to_string(),
            Self::Config(path) => path.clone(),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SettingRow {
    pub key: SettingKey,
    pub label: String,
    pub value: String,
    /// Non-empty = enumerated (dropdown); empty = freeform (text input).
    pub choices: Vec<String>,
}

impl SettingRow {
    pub fn freeform(&self) -> bool {
        self.choices.is_empty()
    }
}

/// A dropdown over an enumerated setting or the known host list.
#[derive(Clone, Debug)]
pub struct SettingChooser {
    pub key: SettingKey,
    pub title: String,
    pub options: Vec<String>,
    pub selected: usize,
    pub current: String,
}

/// A freeform text edit in progress for one setting.
#[derive(Clone, Debug)]
pub struct SettingEdit {
    pub key: SettingKey,
    pub title: String,
    pub buffer: String,
    pub previous: String,
}

/// A chosen value awaiting explicit confirmation before it is written.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PendingSetting {
    pub key: SettingKey,
    pub value: String,
    pub previous: String,
}

#[derive(Clone, Debug)]
pub enum DetailView {
    Loading { title: String },
    Software(Value),
    Doctor(Value),
    HistoryDiff(Value),
}

/// Live output buffer for a streamed workflow (`envy plan`/`apply`/`doctor`)
/// rendered inside a TUI popup instead of leaving the alternate screen.
#[derive(Clone, Debug)]
pub struct WorkflowLog {
    pub title: String,
    pub lines: Vec<String>,
    pub running: bool,
    pub success: Option<bool>,
    pub scroll: usize,
    /// While true the popup pins to the newest line; a manual scroll unpins it.
    pub follow: bool,
}

#[derive(Debug)]
pub enum Message {
    Loaded {
        request: u64,
        target: LoadTarget,
        payload: Value,
    },
    Failed {
        request: u64,
        target: LoadTarget,
        error: String,
    },
    MutationFinished {
        request: u64,
        stage: MutationStage,
        intent: MutationIntent,
        result: Result<Value, String>,
    },
    SoftwareWhyFinished {
        request: u64,
        result: Result<Value, String>,
    },
    SearchGroupsFinished {
        request: u64,
        entry: SearchEntry,
        software: Result<Value, String>,
    },
    HistoryDiffFinished {
        request: u64,
        result: Result<Value, String>,
    },
    SettingApplied {
        request: u64,
        key: SettingKey,
        result: Result<Value, String>,
    },
    MirrorSourcesFinished {
        request: u64,
        target: String,
        result: Result<Value, String>,
    },
    MirrorMeasureFinished {
        request: u64,
        target: String,
        result: Result<Value, String>,
    },
    MirrorMutationFinished {
        request: u64,
        target: String,
        source: MirrorSource,
        stage: MutationStage,
        result: Result<Value, String>,
    },
    WorkflowOutput {
        request: u64,
        line: String,
    },
    WorkflowFinished {
        request: u64,
        success: bool,
    },
}

pub fn text(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(value)) => value.clone(),
        Some(Value::Bool(value)) => value.to_string(),
        Some(Value::Number(value)) => value.to_string(),
        Some(Value::Null) | None => "—".to_string(),
        Some(value) => value.to_string(),
    }
}

pub fn software_groups(payload: Option<&Value>) -> Vec<SoftwareGroup> {
    payload
        .and_then(|value| value.get("groups"))
        .and_then(Value::as_array)
        .map(|groups| {
            groups
                .iter()
                .map(|group| SoftwareGroup {
                    id: text(group.get("id")),
                    label: text(group.get("label")),
                    ecosystem: text(group.get("ecosystem")),
                    scope: text(group.get("scope")),
                    kind: text(group.get("kind")),
                })
                .collect()
        })
        .unwrap_or_default()
}

pub fn software_entries(payload: Option<&Value>) -> Vec<SoftwareEntry> {
    let mut entries = Vec::new();
    let Some(groups) = payload
        .and_then(|value| value.get("groups"))
        .and_then(Value::as_array)
    else {
        return entries;
    };
    for group in groups {
        let group_id = text(group.get("id"));
        let group_label = text(group.get("label"));
        let Some(items) = group.get("items").and_then(Value::as_array) else {
            continue;
        };
        for item in items {
            let effective = item
                .get("effective")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let state = if effective {
                "effective"
            } else if item
                .get("externalExclude")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                "blocked"
            } else if item.get("stale").and_then(Value::as_bool).unwrap_or(false) {
                "stale"
            } else {
                "excluded"
            };
            entries.push(SoftwareEntry {
                source_id: text(item.get("sourceId")),
                group: group_id.clone(),
                label: group_label.clone(),
                item: text(item.get("id")),
                version: text(
                    item.get("version")
                        .filter(|v| !v.is_null())
                        .or_else(|| item.get("resolvedVersion")),
                ),
                state: state.to_string(),
                reference: text(item.get("ref")),
                effective,
            });
        }
    }
    entries
}

pub fn filtered_software_entries(payload: Option<&Value>, filter: &str) -> Vec<SoftwareEntry> {
    let needle = filter.trim().to_lowercase();
    software_entries(payload)
        .into_iter()
        .filter(|entry| {
            needle.is_empty()
                || [
                    &entry.group,
                    &entry.label,
                    &entry.item,
                    &entry.version,
                    &entry.state,
                    &entry.reference,
                ]
                .iter()
                .any(|value| value.to_lowercase().contains(&needle))
        })
        .collect()
}

pub fn search_entries(payload: Option<&Value>) -> Vec<SearchEntry> {
    payload
        .and_then(|value| value.get("results"))
        .and_then(Value::as_array)
        .map(|results| {
            results
                .iter()
                .map(|result| SearchEntry {
                    source_id: result
                        .get("sourceId")
                        .and_then(Value::as_str)
                        .map(str::to_string),
                    availability: result
                        .get("availability")
                        .and_then(Value::as_str)
                        .unwrap_or("installable")
                        .to_string(),
                    source: text(result.get("source")),
                    ecosystem: text(result.get("ecosystem")),
                    kind: text(result.get("kind")),
                    name: text(result.get("name")),
                    version: text(result.get("version")),
                    description: text(result.get("description")),
                    homepage: text(result.get("homepage")),
                    publisher: text(result.get("publisher")),
                    reference: text(result.get("ref")),
                    managed_group: result
                        .get("managed_group")
                        .or_else(|| result.get("managedGroup"))
                        .and_then(Value::as_str)
                        .map(str::to_string),
                })
                .collect()
        })
        .unwrap_or_default()
}

pub fn compatible_groups(entry: &SearchEntry, payload: Option<&Value>) -> Vec<SoftwareGroup> {
    let groups = software_groups(payload);
    if entry.availability == "discovery-only" || entry.availability == "unsupported" {
        return Vec::new();
    }
    if let Some(managed) = &entry.managed_group {
        if let Some(group) = groups.iter().find(|group| &group.id == managed) {
            return vec![group.clone()];
        }
    }
    groups
        .into_iter()
        .filter(|group| group.ecosystem == entry.ecosystem && group.kind == entry.kind)
        .collect()
}

pub fn result_rows(payload: Option<&Value>) -> usize {
    payload
        .and_then(|value| value.get("results"))
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0)
}

pub fn generation_rows(payload: Option<&Value>) -> usize {
    payload
        .and_then(|value| value.get("generations"))
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0)
}

pub fn generation_number(payload: Option<&Value>, index: usize) -> Option<u64> {
    payload
        .and_then(|value| value.get("generations"))
        .and_then(Value::as_array)
        .and_then(|generations| generations.get(index))
        .and_then(|generation| generation.get("number"))
        .and_then(Value::as_u64)
}

pub type Pages = HashMap<Screen, PageState>;

// ==========================================
// READ-ONLY SCREEN VIEW MODELS
// ==========================================

fn format_detail(value: Option<&Value>) -> String {
    match value {
        Some(Value::Object(map)) if !map.is_empty() => map
            .iter()
            .map(|(key, val)| format!("{key}={}", text(Some(val))))
            .collect::<Vec<_>>()
            .join("  "),
        _ => "—".to_string(),
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct JournalEntry {
    pub timestamp: String,
    pub operation: String,
    pub result: String,
    pub duration: String,
    pub machine: String,
    pub detail: String,
}

fn duration_text(duration_ms: Option<&Value>) -> String {
    match duration_ms.and_then(Value::as_u64) {
        Some(ms) => format!("{:.1}s", ms as f64 / 1000.0),
        None => "—".to_string(),
    }
}

pub fn journal_entries(payload: Option<&Value>) -> Vec<JournalEntry> {
    payload
        .and_then(|value| value.get("entries"))
        .and_then(Value::as_array)
        .map(|entries| {
            entries
                .iter()
                .map(|entry| JournalEntry {
                    timestamp: text(entry.get("timestamp")),
                    operation: text(entry.get("operation")),
                    result: text(entry.get("result")),
                    duration: duration_text(entry.get("durationMs")),
                    machine: text(entry.get("machine")),
                    detail: format_detail(entry.get("detail")),
                })
                .collect()
        })
        .unwrap_or_default()
}

pub fn journal_rows(payload: Option<&Value>) -> usize {
    payload
        .and_then(|value| value.get("entries"))
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0)
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HostRow {
    pub platform: String,
    pub machine: String,
    pub current: bool,
    pub file: String,
}

pub fn host_rows(payload: Option<&Value>) -> Vec<HostRow> {
    payload
        .and_then(|value| value.get("machines"))
        .and_then(Value::as_array)
        .map(|machines| {
            machines
                .iter()
                .map(|machine| HostRow {
                    platform: text(machine.get("platform")),
                    machine: text(machine.get("machineId")),
                    current: machine.get("current").and_then(Value::as_bool) == Some(true),
                    file: text(machine.get("file")),
                })
                .collect()
        })
        .unwrap_or_default()
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct KeyValueRow {
    pub key: String,
    pub value: String,
}

pub fn mirror_rows(payload: Option<&Value>) -> Vec<KeyValueRow> {
    payload
        .and_then(|value| value.get("settings"))
        .and_then(Value::as_object)
        .map(|settings| {
            settings
                .iter()
                .map(|(key, value)| KeyValueRow {
                    key: key.clone(),
                    value: text(Some(value)),
                })
                .collect()
        })
        .unwrap_or_default()
}

pub fn mirror_target_rows(payload: Option<&Value>) -> Vec<KeyValueRow> {
    payload
        .and_then(|value| value.get("data"))
        .and_then(|value| value.get("targets"))
        .and_then(Value::as_array)
        .map(|targets| {
            targets
                .iter()
                .map(|target| KeyValueRow {
                    key: text(target.get("id")),
                    value: match (
                        target.get("selectedSource").and_then(Value::as_str),
                        target.get("selectionOrigin").and_then(Value::as_str),
                    ) {
                        (Some(source), Some(origin)) if !source.is_empty() => {
                            format!("{} · ● {} ({origin})", text(target.get("label")), source)
                        }
                        _ => text(target.get("label")),
                    },
                })
                .collect()
        })
        .unwrap_or_default()
}

pub fn mirror_mode(payload: Option<&Value>) -> String {
    payload
        .and_then(|value| value.get("data"))
        .and_then(|value| value.get("mode"))
        .and_then(Value::as_str)
        .unwrap_or("unknown")
        .to_string()
}

fn mirror_source_values(values: Option<&Vec<Value>>) -> Vec<MirrorSource> {
    values
        .map(|sources| {
            sources
                .iter()
                .filter_map(|source| {
                    Some(MirrorSource {
                        code: source.get("code")?.as_str()?.to_string(),
                        label: source
                            .get("label")
                            .and_then(Value::as_str)
                            .unwrap_or("—")
                            .to_string(),
                        url: source
                            .get("url")
                            .and_then(Value::as_str)
                            .unwrap_or("—")
                            .to_string(),
                        provider: source
                            .get("provider")
                            .and_then(Value::as_str)
                            .unwrap_or("catalog")
                            .to_string(),
                        ok: source.get("ok").and_then(Value::as_bool),
                        http_status: source
                            .get("httpStatus")
                            .and_then(Value::as_u64)
                            .and_then(|value| u16::try_from(value).ok()),
                        throughput_bps: source.get("throughputBps").and_then(Value::as_u64),
                        detail: source
                            .get("detail")
                            .and_then(Value::as_str)
                            .filter(|value| !value.trim().is_empty())
                            .map(str::to_string),
                        stale: source
                            .get("stale")
                            .and_then(Value::as_bool)
                            .unwrap_or(false),
                        measurement_stale: source
                            .get("measurementStale")
                            .and_then(Value::as_bool)
                            .unwrap_or(false),
                        fetched_at: source.get("fetchedAt").and_then(Value::as_u64),
                        current: source
                            .get("current")
                            .and_then(Value::as_bool)
                            .unwrap_or(false),
                        selection_origin: source
                            .get("selectionOrigin")
                            .and_then(Value::as_str)
                            .map(str::to_string),
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

pub fn mirror_sources(payload: Option<&Value>) -> Vec<MirrorSource> {
    mirror_source_values(
        payload
            .and_then(|value| value.get("data"))
            .and_then(|value| value.get("sources"))
            .and_then(Value::as_array),
    )
}

pub fn mirror_measurements(payload: Option<&Value>) -> Vec<MirrorSource> {
    mirror_source_values(
        payload
            .and_then(|value| value.get("data"))
            .and_then(|value| value.get("results"))
            .and_then(Value::as_array),
    )
}

/// Build the Configure/Settings list from the cached host list and
/// config payloads. Row 0 is the active host; the rest are managed config fields
/// (`fields[]` from `config show --json`), where a non-empty `choices` marks an
/// enumerated field that should render as a dropdown.
pub fn settings_rows(hosts: Option<&Value>, config: Option<&Value>) -> Vec<SettingRow> {
    let mut rows = Vec::new();
    let current_host = config
        .and_then(|value| value.get("device"))
        .and_then(|device| device.get("machineId"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let machine_ids = host_rows(hosts)
        .into_iter()
        .map(|host| host.machine)
        .filter(|machine| machine != "—" && !machine.is_empty())
        .collect::<Vec<_>>();
    rows.push(SettingRow {
        key: SettingKey::Host,
        label: "host".to_string(),
        value: current_host,
        choices: machine_ids,
    });
    if let Some(fields) = config
        .and_then(|value| value.get("fields"))
        .and_then(Value::as_array)
    {
        for field in fields {
            let path = field
                .get("path")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            if path.is_empty() {
                continue;
            }
            let value = field
                .get("value")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let choices = field
                .get("choices")
                .and_then(Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .filter_map(Value::as_str)
                        .map(str::to_string)
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            rows.push(SettingRow {
                key: SettingKey::Config(path.clone()),
                label: path,
                value,
                choices,
            });
        }
    }
    rows
}

pub fn count_rows(payload: Option<&Value>, key: &str) -> usize {
    payload
        .and_then(|value| value.get(key))
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn local_filter_matches_identity_reference_and_state() {
        let payload = json!({"groups": [{
            "id": "nix.user.package",
            "label": "Nix packages",
            "items": [{"id": "ripgrep", "ref": "nix:rg", "effective": true}]
        }]});
        assert_eq!(filtered_software_entries(Some(&payload), "RIP").len(), 1);
        assert_eq!(filtered_software_entries(Some(&payload), "nix:rg").len(), 1);
        assert_eq!(
            filtered_software_entries(Some(&payload), "blocked").len(),
            0
        );
    }

    #[test]
    fn snake_case_managed_group_is_supported() {
        let payload = json!({"results": [{
            "source": "homebrew", "ecosystem": "homebrew", "kind": "formula",
            "name": "git", "ref": "homebrew:formula/git",
            "managed_group": "homebrew.system.formula"
        }]});
        assert_eq!(
            search_entries(Some(&payload))[0].managed_group.as_deref(),
            Some("homebrew.system.formula")
        );
    }

    #[test]
    fn journal_entries_parse_and_format() {
        let payload = json!({"entries": [{
            "timestamp": "2026-07-27T14:52:03+08:00",
            "operation": "push",
            "result": "fail",
            "durationMs": 18420,
            "machine": "mac",
            "detail": {"remote": "origin", "branch": "master"}
        }]});
        let entries = journal_entries(Some(&payload));
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].operation, "push");
        assert_eq!(entries[0].duration, "18.4s");
        assert!(entries[0].detail.contains("remote=origin"));
        assert_eq!(journal_rows(Some(&payload)), 1);
    }

    #[test]
    fn host_rows_mark_current_machine() {
        let payload = json!({"machines": [
            {"platform": "darwin", "machineId": "mac", "current": true, "file": "hosts/darwin/mac.nix"},
            {"platform": "linux", "machineId": "box", "current": false, "file": "hosts/linux/box.nix"}
        ]});
        let rows = host_rows(Some(&payload));
        assert_eq!(rows.len(), 2);
        assert!(rows[0].current);
        assert!(!rows[1].current);
    }

    #[test]
    fn mirror_rows_flatten_settings() {
        let payload = json!({"settings": {"mode": "china", "npm.registry": "https://x"}});
        let rows = mirror_rows(Some(&payload));
        assert_eq!(rows.len(), 2);
        assert!(rows
            .iter()
            .any(|row| row.key == "mode" && row.value == "china"));
    }

    #[test]
    fn mirror_targets_and_measurements_expose_profile_and_current_source() {
        let targets = json!({"data": {
            "mode": "china",
            "targets": [{
                "id": "npm", "label": "NPM", "selectedSource": "huawei",
                "selectionOrigin": "override"
            }]
        }});
        let measurements = json!({"data": {"results": [{
            "code": "huawei", "label": "Huawei Cloud", "url": "https://mirror.invalid/npm/",
            "provider": "chsrc", "ok": true, "throughputBps": 2048,
            "current": true, "selectionOrigin": "override"
        }]}});

        assert_eq!(mirror_mode(Some(&targets)), "china");
        assert_eq!(
            mirror_target_rows(Some(&targets))[0].value,
            "NPM · ● huawei (override)"
        );
        let mut sources = mirror_measurements(Some(&measurements));
        let source = sources.pop().unwrap();
        assert!(source.current);
        assert_eq!(source.selection_origin.as_deref(), Some("override"));
        assert_eq!(source.throughput_bps, Some(2048));
    }

    #[test]
    fn settings_rows_expose_host_and_config_editability() {
        let hosts = json!({"machines": [
            {"platform": "darwin", "machineId": "mac", "current": true, "file": "hosts/darwin/mac.nix"},
            {"platform": "linux", "machineId": "box", "current": false, "file": "hosts/linux/box.nix"}
        ]});
        let config = json!({
            "device": {"machineId": "mac"},
            "fields": [
                {"path": "envy.mirrors.mode", "value": "china", "choices": ["upstream", "china"]},
                {"path": "envy.user.name", "value": "chi", "choices": []}
            ]
        });
        let rows = settings_rows(Some(&hosts), Some(&config));
        assert_eq!(rows[0].key, SettingKey::Host);
        assert_eq!(rows[0].value, "mac");
        assert_eq!(rows[0].choices, vec!["mac".to_string(), "box".to_string()]);
        let mirror = rows
            .iter()
            .find(|row| row.key == SettingKey::Config("envy.mirrors.mode".to_string()))
            .unwrap();
        assert!(!mirror.freeform());
        assert_eq!(mirror.value, "china");
        let name = rows
            .iter()
            .find(|row| row.key == SettingKey::Config("envy.user.name".to_string()))
            .unwrap();
        assert!(name.freeform());
    }
    #[test]
    fn discovery_only_search_has_no_install_groups() {
        let payload = serde_json::json!({"results":[{"source":"cargo","sourceId":"cargo:crates.io",
            "ecosystem":"cargo","kind":"tool","name":"tool","availability":"discovery-only"}]});
        let entry = search_entries(Some(&payload)).remove(0);
        assert_eq!(entry.source_id.as_deref(), Some("cargo:crates.io"));
        assert!(compatible_groups(&entry, Some(&serde_json::json!({"groups":[]}))).is_empty());
    }
}
