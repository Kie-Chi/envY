use super::*;
use crate::{model::Screen, storage::actions::Operation};
use serde_json::{json, Value};
use std::{os::unix::process::ExitStatusExt, sync::mpsc};

fn fixture() -> Value {
    serde_json::from_str(include_str!("../../storage/fixtures/operations-v1.json")).unwrap()
}
fn app() -> App {
    let (tx, rx) = mpsc::channel();
    let mut app = App::new(tx, rx);
    app.fixed_now = Some(1_700_000_000);
    app.screen = Screen::Storage;
    app
}
fn key(app: &mut App, code: KeyCode) {
    app.handle_key(KeyEvent::new(code, KeyModifiers::NONE));
}
fn response(command: &str, data: Value, ok: bool) -> crate::transport::JsonResponse {
    crate::transport::JsonResponse {
        payload: json!({"schemaVersion":1,"command":command,"ok":ok,"data":data,"warnings":[]}),
        status: std::process::ExitStatus::from_raw(if ok { 0 } else { 256 }),
        stderr: "synthetic bounded stderr".into(),
    }
}
fn loaded() -> App {
    let mut app = app();
    key(&mut app, KeyCode::Char('a'));
    let id = app.storage_actions.pending.as_ref().unwrap().id;
    app.receive_storage_action(
        id,
        Ok(response(
            "storage capabilities",
            fixture()["capabilities"].clone(),
            true,
        )),
    );
    app
}
fn reviewed() -> App {
    let mut app = loaded();
    key(&mut app, KeyCode::Char('3'));
    key(&mut app, KeyCode::Char('6'));
    key(&mut app, KeyCode::Char('p'));
    let id = app.storage_actions.pending.as_ref().unwrap().id;
    app.receive_storage_action(
        id,
        Ok(response(
            "storage maintain",
            fixture()["maintenancePreview"].clone(),
            true,
        )),
    );
    assert_eq!(app.storage_actions.stage, Stage::Review);
    app
}

#[test]
fn evidence_toggle_keeps_preview_and_never_sends_a_request() {
    let mut app = reviewed();
    let request = app.request_id;
    let preview = app.storage_actions.preview.clone();
    key(&mut app, KeyCode::Char('d'));
    assert!(app.storage_actions.details);
    key(&mut app, KeyCode::Esc);
    assert!(!app.storage_actions.details);
    assert_eq!(app.storage_actions.preview, preview);
    assert_eq!(app.storage_actions.stage, Stage::Review);
    assert_eq!(app.request_id, request);
    assert!(app.storage_actions.pending.is_none());
}

#[test]
fn entry_requests_only_capabilities_and_plan_is_explicit_once() {
    let mut app = app();
    assert!(app.storage_actions.pending.is_none());
    assert_eq!(app.storage_view, crate::storage::StorageView::Inspect);
    key(&mut app, KeyCode::Char('a'));
    assert_eq!(
        app.storage_actions.pending.as_ref().unwrap().task,
        Task::Capabilities
    );
    let id = app.storage_actions.pending.as_ref().unwrap().id;
    key(&mut app, KeyCode::Char('s'));
    assert_eq!(app.storage_actions.pending.as_ref().unwrap().id, id);
    app.receive_storage_action(
        id,
        Ok(response(
            "storage capabilities",
            fixture()["capabilities"].clone(),
            true,
        )),
    );
    let before = app.request_id;
    key(&mut app, KeyCode::Char('s'));
    assert_eq!(app.request_id, before + 1);
    assert_eq!(
        app.storage_actions.pending.as_ref().unwrap().task,
        Task::Plan
    );
    key(&mut app, KeyCode::Char('s'));
    assert_eq!(app.request_id, before + 1);
}

#[test]
fn empty_native_selection_has_zero_backend_requests() {
    let mut app = loaded();
    let id = app.request_id;
    key(&mut app, KeyCode::Char('p'));
    key(&mut app, KeyCode::Char('y'));
    key(&mut app, KeyCode::Enter);
    assert_eq!(app.request_id, id);
    assert!(app.storage_actions.pending.is_none());
    assert!(app.storage_actions.notice.contains("at least one"));
    assert_eq!(app.storage_actions.selected, [false; 8]);
    key(&mut app, KeyCode::Char('4'));
    assert!(!app.storage_actions.selected[3]);
}

#[test]
fn preview_review_confirm_cannot_be_skipped_and_double_confirm_executes_once() {
    let mut app = loaded();
    key(&mut app, KeyCode::Char('3'));
    let id = app.request_id;
    key(&mut app, KeyCode::Char('y'));
    key(&mut app, KeyCode::Enter);
    assert_eq!(app.request_id, id);
    key(&mut app, KeyCode::Char('p'));
    let id = app.storage_actions.pending.as_ref().unwrap().id;
    key(&mut app, KeyCode::Enter);
    key(&mut app, KeyCode::Char('y'));
    assert_eq!(app.request_id, id);
    // This preview was for go only, so use a matching producer-shaped response.
    let mut data = fixture()["maintenancePreview"].clone();
    data["request"]["actions"] = json!(["go"]);
    app.receive_storage_action(id, Ok(response("storage maintain", data, true)));
    key(&mut app, KeyCode::Char('y'));
    assert_eq!(app.request_id, id);
    key(&mut app, KeyCode::Enter);
    assert_eq!(app.storage_actions.stage, Stage::Confirm);
    key(&mut app, KeyCode::Enter);
    assert_eq!(app.request_id, id);
    key(&mut app, KeyCode::Char('y'));
    assert_eq!(app.request_id, id + 1);
    assert!(matches!(
        app.storage_actions.pending.as_ref().unwrap().task,
        Task::Execute(..)
    ));
    key(&mut app, KeyCode::Char('y'));
    assert_eq!(app.request_id, id + 1);
    assert!(app.storage_actions.preview.is_none());
}

#[test]
fn cancel_and_selection_change_discard_tokens_without_execute() {
    let mut app = reviewed();
    let id = app.request_id;
    key(&mut app, KeyCode::Enter);
    key(&mut app, KeyCode::Esc);
    key(&mut app, KeyCode::Char('y'));
    assert_eq!(app.request_id, id);
    assert!(app.storage_actions.preview.is_none());
    let mut app = reviewed();
    let id = app.request_id;
    key(&mut app, KeyCode::Char('3'));
    key(&mut app, KeyCode::Enter);
    key(&mut app, KeyCode::Char('y'));
    assert_eq!(app.request_id, id);
    assert!(app.storage_actions.preview.is_none());
}

#[test]
fn settings_host_and_source_context_changes_reject_tokens_and_old_replies() {
    let mut app = reviewed();
    let id = app.request_id;
    app.invalidate_setting_context();
    key(&mut app, KeyCode::Enter);
    key(&mut app, KeyCode::Char('y'));
    assert_eq!(app.request_id, id);
    assert!(app.storage_actions.capabilities.is_none());
    app.receive_storage_action(
        id,
        Ok(response(
            "storage maintain",
            fixture()["maintenancePreview"].clone(),
            true,
        )),
    );
    assert!(app.storage_actions.preview.is_none());
    let mut app = reviewed();
    let id = app.request_id;
    app.pages.entry(Screen::Home).or_default().payload =
        Some(json!({"machine":"different-host","source":{"fingerprint":"new"}}));
    key(&mut app, KeyCode::Enter);
    key(&mut app, KeyCode::Char('y'));
    assert_eq!(app.request_id, id);
    assert!(app.storage_actions.preview.is_none());
}

#[test]
fn old_request_identity_cannot_overwrite_new_capabilities_or_inputs() {
    let mut app = loaded();
    key(&mut app, KeyCode::Char('3'));
    key(&mut app, KeyCode::Char('p'));
    let old = app.storage_actions.pending.as_ref().unwrap().id;
    app.storage_actions.invalidate();
    key(&mut app, KeyCode::Char('c'));
    let new = app.storage_actions.pending.as_ref().unwrap().id;
    app.receive_storage_action(
        old,
        Ok(response(
            "storage maintain",
            fixture()["maintenancePreview"].clone(),
            true,
        )),
    );
    assert_eq!(app.storage_actions.pending.as_ref().unwrap().id, new);
    assert!(app.storage_actions.preview.is_none());
    app.receive_storage_action(
        new,
        Ok(response(
            "storage capabilities",
            fixture()["capabilities"].clone(),
            true,
        )),
    );
    key(&mut app, KeyCode::Char('3'));
    key(&mut app, KeyCode::Char('6'));
    key(&mut app, KeyCode::Char('p'));
    let id = app.storage_actions.pending.as_ref().unwrap().id;
    app.storage_actions.selected[2] = false;
    app.receive_storage_action(
        id,
        Ok(response(
            "storage maintain",
            fixture()["maintenancePreview"].clone(),
            true,
        )),
    );
    assert!(app.storage_actions.preview.is_none());
    assert!(app.storage_actions.notice.contains("Old preview ignored"));
}

#[test]
fn expiry_and_drift_require_fresh_preview_and_keep_error_reason() {
    let mut app = reviewed();
    let id = app.request_id;
    key(&mut app, KeyCode::Enter);
    app.fixed_now = Some(1_700_000_301);
    key(&mut app, KeyCode::Char('y'));
    assert_eq!(app.request_id, id);
    assert!(app.storage_actions.notice.contains("expired"));
    let mut app = reviewed();
    key(&mut app, KeyCode::Enter);
    key(&mut app, KeyCode::Char('y'));
    let id = app.storage_actions.pending.as_ref().unwrap().id;
    let mut reply = response("storage maintain", Value::Null, false);
    reply.payload["error"] =
        json!({"code":"stale_preview","message":"policy/source drift; preview again"});
    app.receive_storage_action(id, Ok(reply));
    assert!(app.storage_actions.text().contains("policy/source drift"));
    key(&mut app, KeyCode::Char('y'));
    assert_eq!(app.request_id, id);
}

#[test]
fn cancellation_of_started_execute_keeps_unknown_semantics_and_evidence() {
    let mut app = reviewed();
    key(&mut app, KeyCode::Enter);
    key(&mut app, KeyCode::Char('y'));
    let id = app.storage_actions.pending.as_ref().unwrap().id;
    key(&mut app, KeyCode::Esc);
    assert!(app
        .storage_actions
        .pending
        .as_ref()
        .unwrap()
        .cancel
        .load(std::sync::atomic::Ordering::Relaxed));
    assert!(app.storage_actions.notice.contains("Completed changes remain"));
    key(&mut app, KeyCode::Char('q'));
    key(&mut app, KeyCode::Char('y'));
    assert_eq!(app.request_id, id);
    app.receive_storage_action(
        id,
        Ok(response(
            "storage maintain",
            fixture()["partialResult"].clone(),
            false,
        )),
    );
    assert!(app.storage_actions.text().contains("partial"));
    assert!(app.storage_actions.notice.contains("unknown"));
    assert!(app.storage_actions.preview.is_none());
    key(&mut app, KeyCode::Char('y'));
    assert_eq!(app.request_id, id);
}

#[test]
fn result_refresh_uses_metadata_without_followup_inspect_or_plan() {
    let mut app = reviewed();
    key(&mut app, KeyCode::Enter);
    key(&mut app, KeyCode::Char('y'));
    let id = app.storage_actions.pending.as_ref().unwrap().id;
    app.receive_storage_action(
        id,
        Ok(response(
            "storage maintain",
            fixture()["partialResult"].clone(),
            false,
        )),
    );
    assert_eq!(app.request_id, id);
    assert!(app.storage_cache.is_empty());
    assert!(app.storage_actions.text().contains("refreshedStatus"));
    assert!(app
        .storage_actions
        .text()
        .contains("synthetic bounded stderr"));
    assert_eq!(app.storage_actions.stage, Stage::Result);
}

#[test]
fn restore_input_preserves_literal_unicode_target_and_uses_only_preview() {
    let mut app = loaded();
    app.storage_actions.operation = Operation::Restore;
    key(&mut app, KeyCode::Char('r'));
    app.handle_paste("saved-run");
    key(&mut app, KeyCode::Enter);
    key(&mut app, KeyCode::Char('t'));
    app.handle_paste("/new/空  格/$(no);`never`\n");
    key(&mut app, KeyCode::Enter);
    key(&mut app, KeyCode::Char('p'));
    let task = &app.storage_actions.pending.as_ref().unwrap().task;
    assert_eq!(
        task.argv().unwrap(),
        [
            "storage",
            "restore",
            "saved-run",
            "--target",
            "/new/空  格/$(no);`never`",
            "--preview",
            "--json"
        ]
    );
    assert!(matches!(task, Task::Preview(_)));
}

#[test]
fn cli_handoff_pages_have_zero_spawns_and_fixed_real_cli_argv() {
    let mut app = loaded();
    let id = app.request_id;
    for (index, name) in ["init", "check", "gc"].into_iter().enumerate() {
        key(&mut app, KeyCode::Char('h'));
        assert_eq!(app.storage_actions.handoff, Some(index));
        let text = app.storage_actions.text();
        assert!(text.contains(&format!("CLI HANDOFF ONLY: {name}")));
        assert!(text.contains("Opening this page executes nothing"));
        if name == "gc" {
            assert!(text.contains("30d"));
            assert!(text.contains("--brew"));
        }
        assert!(text.contains("restic internally --read-data"));
        for code in [
            KeyCode::Char('s'),
            KeyCode::Char('p'),
            KeyCode::Enter,
            KeyCode::Char('y'),
        ] {
            key(&mut app, code);
        }
        assert_eq!(app.request_id, id);
        assert!(app.storage_actions.pending.is_none());
    }
}

#[test]
fn progress_is_bounded_and_old_context_output_is_ignored() {
    let mut app = loaded();
    key(&mut app, KeyCode::Char('s'));
    let id = app.request_id;
    app.storage_action_output(id, "界".repeat(100_000));
    assert!(app.storage_actions.progress.len() <= 256 * 1024);
    let previous = app.storage_actions.progress.clone();
    app.storage_action_output(id + 1, "old reply".into());
    assert_eq!(app.storage_actions.progress, previous);
    app.invalidate_setting_context();
    app.storage_action_output(id, "old context".into());
    assert!(!app.storage_actions.progress.contains("old context"));
}

#[test]
fn legacy_caps_never_spawn_preview_or_execute() {
    let mut app = loaded();
    app.storage_actions.capabilities = Some(json!({"data":{"actions":[],"commands":[]}}));
    app.storage_actions.operation = Operation::Purge;
    let id = app.request_id;
    key(&mut app, KeyCode::Char('p'));
    key(&mut app, KeyCode::Enter);
    key(&mut app, KeyCode::Char('y'));
    assert_eq!(app.request_id, id);
    assert!(app.storage_actions.pending.is_none());
    assert!(app.storage_actions.notice.contains("unavailable"));
}

#[test]
fn saved_plan_completion_never_previews_runs_or_assigns_an_implicit_plan() {
    let mut app = loaded();
    key(&mut app, KeyCode::Char('s'));
    let id = app.request_id;
    app.receive_storage_action(
        id,
        Ok(response(
            "storage plan",
            json!({"id":"explicitly-saved-plan"}),
            true,
        )),
    );
    assert_eq!(app.request_id, id);
    assert!(app.storage_actions.plan_id.is_empty());
    assert!(app.storage_actions.text().contains("explicitly-saved-plan"));
    assert!(app.storage_actions.pending.is_none());
    app.storage_actions.operation = Operation::Run;
    key(&mut app, KeyCode::Char('p'));
    assert_eq!(app.request_id, id);
    key(&mut app, KeyCode::Char('i'));
    app.handle_paste("explicitly-saved-plan");
    key(&mut app, KeyCode::Enter);
    key(&mut app, KeyCode::Char('p'));
    assert_eq!(
        app.storage_actions
            .pending
            .as_ref()
            .unwrap()
            .task
            .argv()
            .unwrap(),
        [
            "storage",
            "run",
            "explicitly-saved-plan",
            "--preview",
            "--json"
        ]
    );
}

#[test]
fn source_change_while_request_runs_rejects_reply_and_resets_input_context() {
    let mut app = loaded();
    app.storage_actions.plan_id = "prior-host-plan".into();
    key(&mut app, KeyCode::Char('3'));
    key(&mut app, KeyCode::Char('6'));
    key(&mut app, KeyCode::Char('p'));
    let id = app.request_id;
    app.pages.entry(Screen::Home).or_default().payload =
        Some(json!({"source":{"revision":"changed"}}));
    app.receive_storage_action(
        id,
        Ok(response(
            "storage maintain",
            fixture()["maintenancePreview"].clone(),
            true,
        )),
    );
    assert!(app.storage_actions.pending.is_none());
    assert!(app.storage_actions.preview.is_none());
    assert!(app.storage_actions.plan_id.is_empty());
    assert!(app.storage_actions.capabilities.is_none());
    assert!(app.storage_actions.notice.contains("Context changed"));
}

fn assert_reviewed_receipt_survives_response_less_failure(kind: crate::transport::ErrorKind) {
    let mut app = reviewed();
    let original = fixture()["maintenancePreview"].clone();
    let receipt = app
        .storage_actions
        .reviewed_receipt
        .as_ref()
        .unwrap()
        .evidence();
    assert_eq!(receipt["operationId"], original["operationId"]);
    for field in [
        "request",
        "preview",
        "policyFingerprint",
        "sourceFingerprint",
    ] {
        assert_eq!(receipt[field], original[field]);
    }
    assert!(receipt.get("previewToken").is_none());

    key(&mut app, KeyCode::Enter);
    key(&mut app, KeyCode::Char('y'));
    let execution = app.request_id;
    assert!(matches!(
        app.storage_actions.pending.as_ref().unwrap().task,
        Task::Execute(..)
    ));
    assert!(app.storage_actions.preview.is_none());
    if kind == crate::transport::ErrorKind::Cancelled {
        key(&mut app, KeyCode::Esc);
        assert!(app
            .storage_actions
            .pending
            .as_ref()
            .unwrap()
            .cancel
            .load(std::sync::atomic::Ordering::Relaxed));
    }
    app.receive_storage_action(
        execution,
        Err(crate::transport::TransportError {
            kind,
            message: "synthetic operation stopped without a JSON response".into(),
            response: None,
            stderr: "synthetic execution progress before the response was lost".into(),
        }),
    );

    assert_eq!(app.storage_actions.stage, Stage::Result);
    assert!(app.storage_actions.pending.is_none());
    assert!(app.storage_actions.preview.is_none());
    assert!(app
        .storage_actions
        .report
        .as_ref()
        .unwrap()
        .response
        .is_none());
    assert_eq!(
        app.storage_actions
            .reviewed_receipt
            .as_ref()
            .unwrap()
            .evidence(),
        receipt
    );
    let shown = app.storage_actions.text();
    assert!(shown.contains(original["operationId"].as_str().unwrap()));
    assert!(
        shown.contains("Last reviewed receipt (evidence only; no execution token; cannot replay)")
    );
    assert!(shown.contains("synthetic operation stopped without a JSON response"));
    assert!(shown.contains(&format!("{kind:?}")));
    assert!(shown.contains("synthetic execution progress before the response was lost"));
    assert!(shown.contains("Process status: unknown"));
    assert!(shown.contains("unknown"));
    assert!(!shown.contains("previewToken"));
    assert!(!shown.contains(original["previewToken"].as_str().unwrap()));

    for code in [
        KeyCode::Char('y'),
        KeyCode::Enter,
        KeyCode::Char('y'),
        KeyCode::Char('y'),
    ] {
        key(&mut app, code);
    }
    assert_eq!(app.request_id, execution);
    assert!(app.storage_actions.pending.is_none());
    assert!(app.storage_actions.preview.is_none());
}

#[test]
fn reviewed_execute_cancelled_without_response_keeps_receipt_and_never_replays() {
    assert_reviewed_receipt_survives_response_less_failure(crate::transport::ErrorKind::Cancelled);
}

#[test]
fn reviewed_execute_timeout_without_response_keeps_receipt_and_never_replays() {
    assert_reviewed_receipt_survives_response_less_failure(crate::transport::ErrorKind::Timeout);
}
