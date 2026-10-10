use std::{env, path::Path, process::Command, sync::mpsc::Sender, thread};

use serde_json::{json, Value};

use crate::model::{
    LoadTarget, Message, MirrorSource, MutationIntent, MutationStage, Screen, SearchEntry,
    SettingKey, SoftwareAction,
};

pub fn envy_binary() -> String {
    env::var("ENVY_BIN").unwrap_or_else(|_| "envy".to_string())
}

pub fn run_json(args: &[&str]) -> Result<Value, String> {
    let binary = envy_binary();
    let command = if Path::new(&binary).is_file() {
        binary
    } else {
        "envy".to_string()
    };
    let output = Command::new(command)
        .args(args)
        .output()
        .map_err(|error| format!("could not start envy: {error}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    match serde_json::from_str::<Value>(&stdout) {
        Ok(value) => Ok(value),
        Err(error) => {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
            if stderr.is_empty() {
                Err(format!("envy returned invalid JSON: {error}"))
            } else {
                Err(stderr)
            }
        }
    }
}

fn load_target(target: &LoadTarget) -> Result<Value, String> {
    match target {
        LoadTarget::Screen(Screen::Home) => run_json(&["status", "--json"]),
        LoadTarget::Screen(Screen::Software) => run_json(&["sw", "ls", "--details", "--json"]),
        LoadTarget::Search(query) => run_json(&["sw", "search", query, "--json"]),
        LoadTarget::Screen(Screen::Doctor) => run_json(&["doctor", "--json"]),
        LoadTarget::Screen(Screen::History) => run_json(&["history", "--json"]),
        LoadTarget::Screen(Screen::Journal) => run_json(&["log", "--json", "--limit", "200"]),
        LoadTarget::Screen(Screen::Hosts) => run_json(&["host", "list", "--json"]),
        LoadTarget::Screen(Screen::Mirror) => run_json(&["mirror", "targets", "--json"]),
        LoadTarget::Screen(Screen::Config) => run_json(&["config", "show", "--json"]),
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
        let result = run_json(&args).and_then(|payload| {
            let payload = envelope_result(payload, "mirror measurement failed")?;
            if payload.get("command").and_then(Value::as_str) != Some("mirror.measure") {
                Err("mirror measurement command does not match the request".to_string())
            } else {
                Ok(payload)
            }
        });
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
    let payload = envelope_result(payload, "mirror mutation failed")?;
    if payload.get("schemaVersion").and_then(Value::as_u64) != Some(1) {
        return Err("unsupported mirror mutation schema".to_string());
    }
    if payload.get("command").and_then(Value::as_str) != Some("mirror.set") {
        return Err("mirror mutation command does not match the request".to_string());
    }
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
        let message = payload
            .get("error")
            .and_then(|error| error.get("message"))
            .and_then(Value::as_str)
            .unwrap_or(fallback);
        Err(message.to_string())
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
    let payload = envelope_result(payload, "software mutation failed")?;
    if payload.get("schemaVersion").and_then(Value::as_u64) != Some(1) {
        return Err("unsupported software mutation schema".to_string());
    }
    let expected_command = format!("software.{}", intent.action.command());
    if payload.get("command").and_then(Value::as_str) != Some(expected_command.as_str()) {
        return Err("software mutation command does not match the request".to_string());
    }
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
        let result = run_json(&["sw", "why", &item, "--group", &group, "--json"])
            .and_then(|payload| envelope_result(payload, "software explanation failed"));
        let _ = tx.send(Message::SoftwareWhyFinished { request, result });
    });
}

pub fn spawn_search_groups(tx: Sender<Message>, request: u64, entry: SearchEntry) {
    thread::spawn(move || {
        let software = run_json(&["sw", "ls", "--details", "--json"]);
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
        let result = run_json(&["history", "diff", &before, &after, "--json"])
            .and_then(|payload| envelope_result(payload, "generation diff failed"));
        let _ = tx.send(Message::HistoryDiffFinished { request, result });
    });
}

/// Apply a Configure/Settings value through the unified, non-interactive CLI:
/// `host select … --json --yes` or `config set … --json --yes`.
pub fn spawn_setting(tx: Sender<Message>, request: u64, key: SettingKey, value: String) {
    thread::spawn(move || {
        let result = match &key {
            SettingKey::Host => run_json(&["host", "select", &value, "--json", "--yes"])
                .and_then(|payload| envelope_result(payload, "host select failed")),
            SettingKey::Config(path) => {
                let command = if path.starts_with("envy.agents.")
                    || path.starts_with("envy.llm.endpoints.")
                    || path.starts_with("envy.llm.credentials.")
                {
                    "agent"
                } else {
                    "config"
                };
                let args = [command, "set", path.as_str(), value.as_str(), "--json", "--yes"];
                run_json(&args)
                    .and_then(|payload| envelope_result(payload, "configuration update failed"))
            }
        };
        let _ = tx.send(Message::SettingApplied {
            request,
            key,
            result,
        });
    });
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn intent(operand: &str) -> MutationIntent {
        MutationIntent {
            action: SoftwareAction::Add,
            group: "homebrew.system.formula".to_string(),
            operand: operand.to_string(),
            item: "git".to_string(),
        }
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
}
