use std::{
    io::{BufRead, BufReader},
    process::{Command, Stdio},
    sync::mpsc::Sender,
    thread,
};

use serde_json::{json, Value};

use crate::model::{
    LoadTarget, Message, MirrorSource, MutationIntent, MutationStage, Screen, SearchEntry,
    SettingKey, SettingSetter, SoftwareAction, WorkflowAction,
};

pub fn envy_binary() -> String {
    // UI state-machine tests must never launch the installed CLI or inspect
    // personal machine state. Transport tests launch isolated synthetic commands.
    #[cfg(test)]
    { "/synthetic-envy-unit-test-no-cli".into() }
    #[cfg(not(test))]
    { std::env::var("ENVY_BIN").unwrap_or_else(|_| "envy".to_string()) }
}

/// Lossless entry point for report consumers, including Storage. A valid business
/// failure remains a response with its original payload and exit status. Call
/// `into_success` before treating a response as an applied mutation.
pub fn run_json_response(
    args: &[&str],
) -> Result<crate::transport::JsonResponse, crate::transport::TransportError> {
    crate::transport::run_json(
        Command::new(envy_binary()).args(args),
        &json_command(args),
        crate::transport::Limits::default(),
    )
}

fn json_command(args: &[&str]) -> String {
    if args.first() == Some(&"log") && args.contains(&"--retry") {
        return "log.retry".into();
    }
    let root = args.first().copied().unwrap_or("");
    let subcommand = args.get(1).copied().filter(|arg| !arg.starts_with('-'));
    match (root, subcommand) {
        ("sw" | "software", Some("source")) => {
            format!("software.source.{}", args.get(2).copied().unwrap_or(""))
        }
        ("sw" | "software", Some(subcommand)) => format!("software.{subcommand}"),
        ("storage", Some(subcommand)) => format!("storage {subcommand}"),
        ("mirror" | "config" | "agent" | "host", Some(subcommand)) => {
            format!("{root}.{subcommand}")
        }
        ("history", Some("diff")) => "history.diff".into(),
        _ => root.to_string(),
    }
}

/// Read-only reports retain valid nonzero JSON, including Doctor summaries.
/// Business failure envelopes are still rejected by the compatibility path.
fn run_report_json(args: &[&str]) -> Result<Value, String> {
    run_json_response(args)
        .map_err(|error| error.to_string())
        .and_then(|response| envelope_result(response.payload, "envy report failed"))
}

/// Compatibility path for existing consumers. Structured failures remain
/// available through `run_json_response` instead of being reduced to a string.
pub fn run_json(args: &[&str]) -> Result<Value, String> {
    run_json_response(args)
        .and_then(|response| response.into_success("envy request failed"))
        .map_err(|error| error.to_string())
}

fn load_target(target: &LoadTarget) -> Result<Value, String> {
    match target {
        LoadTarget::Storage(_) | LoadTarget::Screen(Screen::Storage) => {
            Err("Storage requires its lossless report loader".into())
        }
        LoadTarget::Screen(Screen::Home) => run_report_json(&["status", "--json"]),
        LoadTarget::Screen(Screen::Sources) => {
            let response = run_report_json(&["sw", "source", "ls", "--json"])?;
            let response = envelope_result(response, "source catalog failed")?;
            Ok(response.get("data").cloned().unwrap_or(Value::Null))
        }
        LoadTarget::Screen(Screen::Software) => {
            run_report_json(&["sw", "ls", "--details", "--json"])
        }
        LoadTarget::Search(query) => run_report_json(&["sw", "search", query, "--json"]),
        LoadTarget::Screen(Screen::Doctor) => run_report_json(&["doctor", "--json"]),
        LoadTarget::Screen(Screen::History) => run_report_json(&["history", "--json"]),
        LoadTarget::Screen(Screen::Journal) => {
            run_report_json(&["log", "--json", "--limit", "200"])
        }
        LoadTarget::Screen(Screen::Hosts) => run_report_json(&["host", "list", "--json"]),
        LoadTarget::Screen(Screen::Mirror) => run_report_json(&["mirror", "targets", "--json"]),
        LoadTarget::Screen(Screen::Config) => run_report_json(&["config", "show", "--json"]),
        LoadTarget::Screen(Screen::Search) => {
            Ok(json!({"query": "", "results": [], "providers": []}))
        }
    }
}

pub fn spawn_mirror_sources(tx: Sender<Message>, request: u64, target: String) {
    thread::spawn(move || {
        let result = run_json(&["mirror", "sources", &target, "--json"])
            .and_then(|payload| envelope_result(payload, "mirror source lookup failed"));
        let _ = tx.send(Message::MirrorSourcesFinished {
            request,
            target,
            result,
        });
    });
}

pub fn spawn_mirror_measure(tx: Sender<Message>, request: u64, target: String, refresh: bool) {
    thread::spawn(move || {
        let mut args = vec!["mirror", "measure", target.as_str()];
        if refresh {
            args.push("--refresh");
        }
        args.push("--json");
        // `mirror measure` intentionally exits non-zero when every endpoint
        // fails, but still returns a valid JSON report the UI can render.
        let result = run_json_response(&args)
            .map_err(|error| error.to_string())
            .and_then(|response| envelope_result(response.payload, "mirror measurement failed"));
        let _ = tx.send(Message::MirrorMeasureFinished {
            request,
            target,
            result,
        });
    });
}

fn mirror_mutation_args(stage: MutationStage, target: &str, source: &str) -> Vec<String> {
    let mut args = vec![
        "mirror".to_string(),
        "set".to_string(),
        target.to_string(),
        source.to_string(),
    ];
    match stage {
        MutationStage::Preview => args.push("--dry-run".to_string()),
        MutationStage::Apply => args.push("--yes".to_string()),
    }
    args.push("--json".to_string());
    args
}

fn validate_mirror_response(
    stage: MutationStage,
    target: &str,
    source: &MirrorSource,
    payload: Value,
) -> Result<Value, String> {
    crate::transport::validate_envelope(&payload, "mirror.set")
        .map_err(|error| error.to_string())?;
    let payload = envelope_result(payload, "mirror mutation failed")?;
    let data = payload.get("data").unwrap_or(&Value::Null);
    let plan = data.get("plan").unwrap_or(&Value::Null);
    if plan.get("target").and_then(Value::as_str) != Some(target)
        || plan
            .get("source")
            .and_then(|value| value.get("code"))
            .and_then(Value::as_str)
            != Some(source.code.as_str())
        || plan
            .get("source")
            .and_then(|value| value.get("url"))
            .and_then(Value::as_str)
            != Some(source.url.as_str())
    {
        return Err("mirror mutation plan does not match the selected source".to_string());
    }
    let expected = match stage {
        MutationStage::Preview => "dry-run",
        MutationStage::Apply => "applied",
    };
    if data.get("result").and_then(Value::as_str) != Some(expected) {
        return Err("mirror mutation returned an unexpected result".to_string());
    }
    Ok(payload)
}

pub fn spawn_mirror_mutation(
    tx: Sender<Message>,
    request: u64,
    stage: MutationStage,
    target: String,
    source: MirrorSource,
) {
    thread::spawn(move || {
        let args = mirror_mutation_args(stage, &target, &source.code);
        let refs = args.iter().map(String::as_str).collect::<Vec<_>>();
        let result = run_json(&refs)
            .and_then(|payload| validate_mirror_response(stage, &target, &source, payload));
        let _ = tx.send(Message::MirrorMutationFinished {
            request,
            target,
            source,
            stage,
            result,
        });
    });
}

pub fn spawn_request(tx: Sender<Message>, request: u64, target: LoadTarget) {
    if let LoadTarget::Storage(target) = target {
        thread::spawn(move || {
            let result = crate::storage::StorageReport::load(&target);
            let _ = tx.send(Message::StorageFinished {
                request,
                target,
                result,
            });
        });
        return;
    }
    if matches!(target, LoadTarget::Screen(Screen::History | Screen::Journal)) {
        thread::spawn(move || {
            let args = if target == LoadTarget::Screen(Screen::History) {
                vec!["history", "--json"]
            } else { vec!["log", "--json", "--limit", "200"] };
            let result = run_json_response(&args);
            let _ = tx.send(Message::ReportFinished { request, target, result });
        });
        return;
    }
    thread::spawn(move || match load_target(&target) {
        Ok(payload) => {
            let _ = tx.send(Message::Loaded {
                request,
                target,
                payload,
            });
        }
        Err(error) => {
            let _ = tx.send(Message::Failed {
                request,
                target,
                error,
            });
        }
    });
}

fn envelope_result(payload: Value, fallback: &str) -> Result<Value, String> {
    if payload.get("ok").and_then(Value::as_bool) == Some(false) {
        Err(crate::transport::failure_message(&payload, fallback))
    } else {
        Ok(payload)
    }
}

pub fn mutation_args(stage: MutationStage, intent: &MutationIntent) -> Vec<String> {
    let mut args = vec![
        "sw".to_string(),
        intent.action.command().to_string(),
        intent.group.clone(),
        intent.operand.clone(),
    ];
    if intent.source_operation {
        args = vec![
            "sw".into(),
            "source".into(),
            intent.action.command().into(),
            intent.source_id.clone().unwrap_or_default(),
        ];
    } else if let Some(source_id) = &intent.source_id {
        if intent.action == SoftwareAction::Add {
            args.extend(["--source-id".into(), source_id.clone()]);
        }
    }
    match stage {
        MutationStage::Preview => args.push("--dry-run".to_string()),
        MutationStage::Apply => args.push("--yes".to_string()),
    }
    args.push("--json".to_string());
    args
}

pub fn validate_mutation_response(
    stage: MutationStage,
    intent: &MutationIntent,
    payload: Value,
) -> Result<Value, String> {
    let expected_command = format!(
        "software.{}{}",
        if intent.source_operation {
            "source."
        } else {
            ""
        },
        intent.action.command()
    );
    crate::transport::validate_envelope(&payload, &expected_command)
        .map_err(|error| error.to_string())?;
    let payload = envelope_result(payload, "software mutation failed")?;
    let data = payload.get("data").unwrap_or(&Value::Null);
    let plan = data.get("plan").unwrap_or(&Value::Null);
    let group = plan
        .get("group")
        .and_then(|group| group.get("id"))
        .and_then(Value::as_str);
    let item = plan.get("item").and_then(Value::as_str);
    let action = plan.get("action").and_then(Value::as_str);
    let effective = plan
        .get("expected")
        .and_then(|expected| expected.get("effective"))
        .and_then(Value::as_bool);
    if let Some(source_id) = &intent.source_id {
        if plan.get("sourceId").and_then(Value::as_str) != Some(source_id.as_str()) {
            return Err("software plan source does not match the request".into());
        }
    }
    if group != Some(intent.group.as_str())
        || item != Some(intent.item.as_str())
        || action != Some(intent.action.command())
        || effective != Some(intent.action == SoftwareAction::Add)
    {
        return Err("software mutation plan does not match the selected item".to_string());
    }
    let result = data.get("result").and_then(Value::as_str);
    let valid_result = match stage {
        MutationStage::Preview => result == Some("dry-run"),
        MutationStage::Apply => matches!(result, Some("applied" | "already-satisfied")),
    };
    if !valid_result {
        return Err("software mutation returned an unexpected result".to_string());
    }
    Ok(payload)
}

pub fn spawn_mutation(
    tx: Sender<Message>,
    request: u64,
    stage: MutationStage,
    intent: MutationIntent,
) {
    thread::spawn(move || {
        let args = mutation_args(stage, &intent);
        let arg_refs = args.iter().map(String::as_str).collect::<Vec<_>>();
        let result = run_json(&arg_refs)
            .and_then(|payload| validate_mutation_response(stage, &intent, payload));
        let _ = tx.send(Message::MutationFinished {
            request,
            stage,
            intent,
            result,
        });
    });
}

pub fn spawn_software_why(tx: Sender<Message>, request: u64, group: String, item: String) {
    thread::spawn(move || {
        let result = run_report_json(&["sw", "why", &item, "--group", &group, "--json"])
            .and_then(|payload| envelope_result(payload, "software explanation failed"));
        let _ = tx.send(Message::SoftwareWhyFinished { request, result });
    });
}

pub fn spawn_search_groups(tx: Sender<Message>, request: u64, entry: SearchEntry) {
    thread::spawn(move || {
        let software = run_report_json(&["sw", "ls", "--details", "--json"]);
        let _ = tx.send(Message::SearchGroupsFinished {
            request,
            entry,
            software,
        });
    });
}

pub fn spawn_history_diff(tx: Sender<Message>, request: u64, before: u64, after: u64) {
    thread::spawn(move || {
        let before = before.to_string();
        let after = after.to_string();
        let result = run_json_response(&["history", "diff", &before, &after, "--json"]);
        let _ = tx.send(Message::HistoryDiffFinished { request, result });
    });
}

/// Build argv from backend metadata, preserving the original SettingKey path.
pub fn setting_args(key: &SettingKey, setter: &SettingSetter, value: &str) -> Result<Vec<String>, String> {
    let (command, path) = match key {
        SettingKey::Host => ("host", "select"),
        SettingKey::Config(path) => {
            let command = match setter {
                SettingSetter::Config => "config",
                SettingSetter::Agent => "agent",
                SettingSetter::Legacy => {
                    if ["envy.agents.", "envy.agentProfiles.", "envy.llm.endpoints.", "envy.llm.credentials."]
                        .iter().any(|prefix| path.starts_with(prefix)) { "agent" } else { "config" }
                }
                SettingSetter::Disabled | SettingSetter::Unknown(_) =>
                    return Err("Unsupported or disabled backend setter; reload Settings".into()),
            };
            return Ok([command, "set", path, value, "--json", "--yes"]
                .into_iter().map(str::to_string).collect());
        }
    };
    Ok([command, path, value, "--json", "--yes"].into_iter().map(str::to_string).collect())
}

pub fn spawn_setting(tx: Sender<Message>, request: u64, key: SettingKey, setter: SettingSetter, value: String) {
    thread::spawn(move || {
        let result = setting_args(&key, &setter, &value).and_then(|args| {
            let args = args.iter().map(String::as_str).collect::<Vec<_>>();
            run_json(&args).and_then(|payload| envelope_result(payload, "configuration update failed"))
        });
        let _ = tx.send(Message::SettingApplied { request, key, result });
    });
}

/// Remove ANSI escape sequences and collapse carriage-return overwrites so the
/// text is safe to render as a plain line inside the TUI log popup.
pub fn strip_ansi(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    let mut chars = input.chars().peekable();
    while let Some(c) = chars.next() {
        if c != '\u{1b}' {
            out.push(c);
            continue;
        }
        match chars.peek() {
            // CSI sequence: ESC [ … final-byte (0x40..=0x7E).
            Some('[') => {
                chars.next();
                while let Some(&next) = chars.peek() {
                    chars.next();
                    if ('\u{40}'..='\u{7e}').contains(&next) {
                        break;
                    }
                }
            }
            // OSC sequence: ESC ] … terminated by BEL or ST (ESC \).
            Some(']') => {
                chars.next();
                while let Some(&next) = chars.peek() {
                    if next == '\u{7}' {
                        chars.next();
                        break;
                    }
                    if next == '\u{1b}' {
                        chars.next();
                        if chars.peek() == Some(&'\\') {
                            chars.next();
                        }
                        break;
                    }
                    chars.next();
                }
            }
            // Lone ESC or other two-byte escape: drop the following byte.
            _ => {
                chars.next();
            }
        }
    }
    // Progress output overwrites a line with a carriage return; keep only the
    // final segment so the popup shows the latest state rather than fragments.
    if let Some(idx) = out.rfind('\r') {
        out.split_off(idx + 1)
    } else {
        out
    }
}

/// Plan builds can take up to two hours, with separate finite output bounds.
/// The ordinary read-only transport deadline remains 120 seconds.
pub fn spawn_json_workflow(
    tx: Sender<Message>, request: u64, action: WorkflowAction,
    cancelled: std::sync::Arc<std::sync::atomic::AtomicBool>,
) {
    thread::spawn(move || {
        let (program, args) = action.command(envy_binary());
        let expected = json_command(&args.iter().map(String::as_str).collect::<Vec<_>>());
        let result = crate::transport::run_json_controlled(
            Command::new(program).args(args).env("NO_COLOR", "1").env("TERM", "dumb"),
            &expected,
            crate::transport::Limits {
                timeout: std::time::Duration::from_secs(if action == WorkflowAction::Plan { 2 * 60 * 60 } else { 120 }),
                stdout_bytes: 16 * 1024 * 1024,
                stderr_bytes: 4 * 1024 * 1024,
            },
            &cancelled,
            |chunk| {
                // One bounded message per capture batch avoids a channel entry
                // for every newline emitted by a verbose build.
                let _ = tx.send(Message::WorkflowOutput { request, line: chunk.to_string() });
            },
        );
        let result = result.and_then(|response| {
            if matches!(action, WorkflowAction::RetryVerification(_)) {
                if let Err(mut error) = crate::transport::validate_envelope(&response.payload, "log.retry") {
                    error.stderr = response.stderr.clone();
                    error.response = Some(Box::new(response));
                    return Err(error);
                }
            }
            Ok(response)
        });
        let _ = tx.send(Message::JsonWorkflowFinished { request, result });
    });
}

/// Stream a full-configuration workflow (`envy plan`/`apply`/`doctor`) into the
/// TUI. Output is piped (not inherited) so the alternate screen stays intact;
/// stdin is closed so the child never blocks on an interactive prompt.
pub fn spawn_workflow(tx: Sender<Message>, request: u64, action: WorkflowAction) {
    thread::spawn(move || {
        let (program, args) = action.command(envy_binary());
        let mut child = match Command::new(program)
            .args(args)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .env("NO_COLOR", "1")
            .env("TERM", "dumb")
            .spawn()
        {
            Ok(child) => child,
            Err(error) => {
                let _ = tx.send(Message::WorkflowOutput {
                    request,
                    line: format!("could not start workflow: {error}"),
                });
                let _ = tx.send(Message::WorkflowFinished {
                    request,
                    success: false,
                });
                return;
            }
        };

        // Read stderr on a helper thread so it interleaves with stdout instead
        // of buffering until the child exits.
        let stderr_handle = child.stderr.take().map(|stderr| {
            let tx = tx.clone();
            thread::spawn(move || {
                for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                    let _ = tx.send(Message::WorkflowOutput {
                        request,
                        line: strip_ansi(&line),
                    });
                }
            })
        });

        if let Some(stdout) = child.stdout.take() {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                let _ = tx.send(Message::WorkflowOutput {
                    request,
                    line: strip_ansi(&line),
                });
            }
        }

        if let Some(handle) = stderr_handle {
            let _ = handle.join();
        }

        let success = child.wait().map(|status| status.success()).unwrap_or(false);
        let _ = tx.send(Message::WorkflowFinished { request, success });
    });
}

/// Ask the CLI whether `envy apply` would require an interactive TTY (Darwin
/// with no cached sudo password). On any failure we assume it does, so callers
/// fall back to the proven leave-screen path rather than risk a hung apply.
pub fn apply_needs_tty() -> bool {
    match run_json(&["_apply-needs-tty", "--json"]) {
        Ok(payload) => payload
            .get("data")
            .and_then(|data| data.get("needsTty"))
            .or_else(|| payload.get("needsTty"))
            .and_then(Value::as_bool)
            .unwrap_or(true),
        Err(_) => true,
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn intent(operand: &str) -> MutationIntent {
        MutationIntent {
            source_id: None,
            source_operation: false,
            action: SoftwareAction::Add,
            group: "homebrew.system.formula".to_string(),
            operand: operand.to_string(),
            item: "git".to_string(),
        }
    }

    #[test]
    fn setter_metadata_is_authoritative_and_legacy_profiles_keep_the_agent_route() {
        let profile = SettingKey::Config("envy.agentProfiles.fixture.model".into());
        let ordinary = SettingKey::Config("envy.some.future.setting".into());
        for (key, setter, command) in [
            (&profile, SettingSetter::Agent, "agent"),
            (&profile, SettingSetter::Config, "config"),
            (&ordinary, SettingSetter::Agent, "agent"),
            (&profile, SettingSetter::Legacy, "agent"),
            (&ordinary, SettingSetter::Legacy, "config"),
        ] {
            let args = setting_args(key, &setter, "literal; $(never-execute)").unwrap();
            assert_eq!(args[0], command);
            assert_eq!(args[2], key.label());
            assert_eq!(args[3], "literal; $(never-execute)");
        }
        for setter in [SettingSetter::Disabled, SettingSetter::Unknown("other.set".into())] {
            assert!(setting_args(&ordinary, &setter, "value").is_err());
        }
        let (_, args) = WorkflowAction::RetryVerification("fixture".into()).command("envy".into());
        assert_eq!(json_command(&args.iter().map(String::as_str).collect::<Vec<_>>()), "log.retry");
        assert_eq!(WorkflowAction::Plan.command("envy".into()).1, ["plan", "--json"]);
    }

    #[test]
    fn wire_commands_are_canonical_and_storage_uses_spaces() {
        for (args, expected) in [
            (vec!["sw", "source", "ls", "--json"], "software.source.ls"),
            (
                vec!["software", "add", "group", "item", "--json"],
                "software.add",
            ),
            (vec!["storage", "inspect", "--json"], "storage inspect"),
            (vec!["config", "show", "--json"], "config.show"),
            (vec!["history", "diff", "1", "2", "--json"], "history.diff"),
            (vec!["doctor", "--json"], "doctor"),
        ] {
            assert_eq!(json_command(&args), expected);
        }
    }

    #[test]
    fn failed_partial_payload_never_becomes_a_mutation_preview_or_commit() {
        for stage in [MutationStage::Preview, MutationStage::Apply] {
            let result = match stage {
                MutationStage::Preview => "dry-run",
                MutationStage::Apply => "applied",
            };
            let payload = json!({
                "schemaVersion": 1, "command": "software.add", "ok": false,
                "data": {"result": result, "plan": {
                    "action": "add", "group": {"id": "homebrew.system.formula"},
                    "item": "git", "expected": {"effective": true}
                }, "errors": ["partial change"]},
                "error": {"code": "partial_failure", "message": "inspect results"}
            });
            let error = validate_mutation_response(stage, &intent("git"), payload).unwrap_err();
            assert!(error.contains("partial_failure"));
            assert!(error.contains("inspect results"));
        }
    }

    #[test]
    fn mutation_requires_an_explicit_success_envelope() {
        let legacy = json!({"schemaVersion": 1, "data": {"result": "dry-run", "plan": {
            "action": "add", "group": {"id": "homebrew.system.formula"},
            "item": "git", "expected": {"effective": true}
        }}});
        assert!(
            validate_mutation_response(MutationStage::Preview, &intent("git"), legacy).is_err()
        );
    }

    #[test]
    fn failed_mirror_payload_never_becomes_an_applied_change() {
        let source = MirrorSource {
            code: "synthetic".into(),
            url: "https://synthetic.invalid".into(),
            label: "Synthetic".into(),
            provider: "synthetic".into(),
            ok: None,
            http_status: None,
            throughput_bps: None,
            detail: None,
            stale: false,
            measurement_stale: false,
            fetched_at: None,
            current: false,
            selection_origin: None,
        };
        let payload = json!({"schemaVersion":1,"command":"mirror.set","ok":false,
            "data":{"result":"applied"},
            "error":{"code":"partial_failure","message":"failed"}});
        let error =
            validate_mirror_response(MutationStage::Apply, "target", &source, payload).unwrap_err();
        assert!(error.contains("partial_failure"));
    }

    #[test]
    fn canonical_search_operand_is_kept_separate_from_verified_item() {
        assert_eq!(
            mutation_args(MutationStage::Preview, &intent("homebrew:formula/git")),
            [
                "sw",
                "add",
                "homebrew.system.formula",
                "homebrew:formula/git",
                "--dry-run",
                "--json"
            ]
        );
    }

    #[test]
    fn normalized_plan_item_is_validated() {
        let payload = json!({
            "schemaVersion": 1,
            "command": "software.add",
            "ok": true,
            "data": {"result": "dry-run", "plan": {
                "action": "add", "group": {"id": "homebrew.system.formula"},
                "item": "git", "expected": {"effective": true}
            }}
        });
        assert!(validate_mutation_response(
            MutationStage::Preview,
            &intent("homebrew:formula/git"),
            payload
        )
        .is_ok());
    }

    #[test]
    fn strip_ansi_removes_color_codes_and_keeps_text() {
        assert_eq!(strip_ansi("\u{1b}[32mok\u{1b}[0m done"), "ok done");
    }

    #[test]
    fn strip_ansi_collapses_carriage_return_overwrites() {
        assert_eq!(
            strip_ansi("building... 40%\rbuilding... 90%"),
            "building... 90%"
        );
    }

    #[test]
    fn strip_ansi_drops_osc_hyperlink_sequences() {
        assert_eq!(
            strip_ansi("\u{1b}]8;;https://example.com\u{7}link\u{1b}]8;;\u{7}"),
            "link"
        );
    }
    #[test]
    fn search_mutation_keeps_concrete_source_and_checks_response() {
        let mut request = intent("homebrew:formula/git");
        request.source_id = Some("homebrew:homebrew/core".into());
        let args = mutation_args(MutationStage::Preview, &request);
        assert!(args
            .windows(2)
            .any(|pair| pair == ["--source-id", "homebrew:homebrew/core"]));
        let wrong_source = json!({"schemaVersion":1,"command":"software.add","ok":true,
            "data":{"result":"dry-run","plan":{"action":"add","group":{"id":request.group},
                "item":"git","sourceId":"homebrew:other/tap","expected":{"effective":true}}}});
        assert!(
            validate_mutation_response(MutationStage::Preview, &request, wrong_source).is_err()
        );
    }

    #[test]
    fn source_mutation_calls_source_cli_with_preview_flags() {
        let mut request = intent("vendor");
        request.group = "native.system.repository".into();
        request.item = "vendor".into();
        request.source_id = Some("native:vendor".into());
        request.source_operation = true;
        assert_eq!(
            mutation_args(MutationStage::Preview, &request),
            vec![
                "sw",
                "source",
                "add",
                "native:vendor",
                "--dry-run",
                "--json"
            ]
        );
    }
}

/// Storage mutations always use the token protocol; never route to the legacy
/// success-only loader. Valid failures retain the complete response and stderr.
pub fn spawn_storage_action(
    tx: Sender<Message>, request: u64, task: crate::storage::actions::Task,
    cancelled: std::sync::Arc<std::sync::atomic::AtomicBool>,
) {
    thread::spawn(move || {
        let result = match task.argv() {
            Err(message) => Err(crate::transport::TransportError {
                kind: crate::transport::ErrorKind::Protocol, message,
                response: None, stderr: String::new(),
            }),
            Ok(args) => crate::transport::run_json_controlled(
                Command::new(envy_binary()).args(args).env("NO_COLOR", "1").env("TERM", "dumb"),
                &task.command(),
                crate::transport::Limits {
                    timeout: std::time::Duration::from_secs(if task.mutating() { 2 * 60 * 60 } else { 120 }),
                    stdout_bytes: 16 * 1024 * 1024, stderr_bytes: 4 * 1024 * 1024,
                },
                &cancelled,
                |chunk| { let _ = tx.send(Message::StorageActionOutput { request, chunk: chunk.into() }); },
            ).and_then(|response| {
                if let Err(mut error) = crate::transport::validate_envelope(&response.payload, &task.command()) {
                    error.stderr = response.stderr.clone();
                    error.response = Some(Box::new(response));
                    return Err(error);
                }
                Ok(response)
            }),
        };
        let _ = tx.send(Message::StorageActionFinished { request, result });
    });
}
