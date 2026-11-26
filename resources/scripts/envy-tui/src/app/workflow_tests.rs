use std::{os::unix::process::ExitStatusExt, sync::{atomic::{AtomicBool, Ordering}, mpsc, Arc}};

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use serde_json::{json, Value};

use super::*;
use crate::model::{SettingKey, SettingSetter, WorkflowReport};

fn app() -> App {
    let (tx, rx) = mpsc::channel();
    App::new(tx, rx)
}

fn response(payload: Value, code: i32) -> crate::transport::JsonResponse {
    crate::transport::JsonResponse {
        payload,
        status: std::process::ExitStatus::from_raw(code << 8),
        stderr: "build /fixture/target\nverify-generation\n".into(),
    }
}

fn running(app: &mut App, request: u64) -> Arc<AtomicBool> {
    let cancelled = Arc::new(AtomicBool::new(false));
    app.workflow_cancel = Some(cancelled.clone());
    app.workflow_stream_request = Some(request);
    app.workflow_log = Some(WorkflowLog {
        title: "Synthetic JSON workflow".into(), lines: vec![], running: true,
        success: None, scroll: 0, follow: true, json: true, scroll_max: 0, report: None,
    });
    cancelled
}

fn key(app: &mut App, code: KeyCode) {
    app.handle_key(KeyEvent::new(code, KeyModifiers::NONE));
}

#[test]
fn plan_rebuild_rejects_old_progress_and_completion_and_preserves_partial_report() {
    let mut app = app();
    running(&mut app, 10);
    app.close_workflow_log();
    running(&mut app, 11);
    let mut payload: Value = serde_json::from_str(include_str!("../workflow/fixtures/plan-v1.json")).unwrap();
    payload["ok"] = json!(false);
    payload["data"]["result"] = json!("partial");
    payload["warnings"] = json!(["closure unavailable"]);
    payload["errors"] = json!([{"stage":"closure", "message":"fixture failure"}]);
    payload["error"] = json!({"code":"workflow_failed", "message":"partial target retained"});
    app.tx.send(Message::WorkflowOutput { request: 10, line: "old build".into() }).unwrap();
    app.tx.send(Message::JsonWorkflowFinished { request: 10,
        result: Ok(response(json!({"ok": true, "data": {"machine":"old"}}), 0)) }).unwrap();
    app.tx.send(Message::JsonWorkflowFinished { request: 11,
        result: Ok(response(payload.clone(), 7)) }).unwrap();
    app.receive_messages();
    let log = app.workflow_log.as_ref().unwrap();
    assert!(!log.running);
    assert_eq!(log.success, Some(false));
    assert!(!log.lines.iter().any(|line| line == "old build"));
    let report = log.report.as_ref().unwrap();
    assert_eq!(report.response.as_ref().unwrap().payload, payload);
    assert_eq!(report.response.as_ref().unwrap().status.code(), Some(7));
    assert!(report.stderr.contains("verify-generation"));
    assert!(app.pages.is_empty(), "a plan failure must not launch a mutation refresh");
    assert!(app.post_mutation.is_none());
    assert_eq!(app.request_id, 0);
}

#[test]
fn context_change_and_navigation_reject_old_plan_reports() {
    for settings_change in [false, true] {
        let mut app = app();
        let cancelled = running(&mut app, 8);
        app.pages.insert(Screen::Software, PageState { payload: Some(json!({})), ..Default::default() });
        if settings_change { app.invalidate_setting_context(); }
        else { app.set_screen(Screen::Software); }
        assert!(cancelled.load(Ordering::Relaxed));
        app.tx.send(Message::JsonWorkflowFinished { request: 8,
            result: Ok(response(json!({"machine":"old"}), 0)) }).unwrap();
        app.receive_messages();
        assert!(app.workflow_log.is_none());
        assert!(app.workflow_stream_request.is_none());
    }
}

#[test]
fn page_cache_recreation_rejects_old_history_and_journal_receipts() {
    for screen in [Screen::History, Screen::Journal] {
        let mut app = app();
        app.pages.insert(screen, PageState { loading: true, request: Some(2), ..Default::default() });
        for (request, marker) in [(1, "old"), (2, "current"), (1, "late old")] {
            app.tx.send(Message::ReportFinished { request, target: LoadTarget::Screen(screen),
                result: Ok(response(json!({"entries":[], "generations":[], "marker":marker}), 0)) }).unwrap();
        }
        app.receive_messages();
        assert_eq!(app.pages[&screen].payload.as_ref().unwrap()["marker"], "current");
    }
}

#[test]
fn history_failure_keeps_partial_receipt_and_native_process_result() {
    let mut app = app();
    app.pages.insert(Screen::History, PageState { loading: true, request: Some(1), ..Default::default() });
    let payload = json!({"schemaVersion":1,"command":"history","ok":false,
        "data":{"generations":[{"number":4,"semanticStatus":"unknown"}]},
        "warnings":["unbound"],"error":{"code":"workflow_failed","message":"partial history"}});
    app.tx.send(Message::ReportFinished { request:1, target:LoadTarget::Screen(Screen::History),
        result:Ok(response(payload.clone(), 3)) }).unwrap();
    app.receive_messages();
    let state = &app.pages[&Screen::History];
    assert_eq!(state.payload.as_ref(), Some(&payload));
    assert!(state.error.as_ref().unwrap().contains("partial history"));
    assert_eq!(state.workflow_report.as_ref().unwrap().response.as_ref().unwrap().status.code(), Some(3));
    assert_eq!(crate::model::generation_number(state.payload.as_ref(), 0), Some(4));
}

#[test]
fn backend_retry_permission_is_required_and_confirmation_can_be_cancelled() {
    let mut app = app();
    let entry = json!({"operationId":"fixture-operation", "operation":"apply",
        "retry":{"allowed":true,"step":"verify-generation","reason":"read-only generation check"}});
    for payload in [json!({"entries":[entry.clone()]}),
        json!({"schemaVersion":1,"command":"log","ok":true,"data":{"entries":[entry.clone()]}})] {
        app.screen = Screen::History;
        app.history_view = HistoryView::Operations;
        app.pages.insert(Screen::Journal, PageState { payload: Some(payload), ..Default::default() });
        key(&mut app, KeyCode::Enter);
        key(&mut app, KeyCode::Char('x'));
        let action = app.workflow_confirmation.as_ref().unwrap();
        let (_, argv) = action.command("synthetic-envy".into());
        assert_eq!(argv, ["log","--retry","fixture-operation","--step","verify-generation","--json"]);
        key(&mut app, KeyCode::Esc);
        assert!(app.workflow_confirmation.is_none());
        assert!(app.detail.is_some(), "cancel retains the operation receipt");
        assert_eq!(app.request_id, 0, "confirmation cancellation must not submit");
        key(&mut app, KeyCode::Esc);
    }
    for entry in [json!({"operation":"apply"}),
        json!({"operationId":"fixture","retry":{"allowed":false,"step":"verify-generation","reason":"conflict"}}),
        json!({"operationId":"fixture","retry":{"allowed":true,"step":"apply","reason":"unsafe"}})] {
        app.detail = Some(DetailView::Report { title:"receipt".into(),
            report:WorkflowReport {response:None,error:None,stderr:String::new()}, selected:Some(entry) });
        key(&mut app, KeyCode::Char('x'));
        assert!(app.workflow_confirmation.is_none());
        assert!(!app.status.is_empty());
        assert_eq!(app.request_id, 0);
    }
}

#[test]
fn retry_rejection_stays_visible_and_running_requests_cannot_submit_twice() {
    let mut app = app();
    running(&mut app, 99);
    app.start_workflow(WorkflowAction::RetryVerification("fixture".into()));
    assert_eq!(app.request_id, 0);
    assert_eq!(app.workflow_stream_request, Some(99));
    key(&mut app, KeyCode::Enter);
    assert_eq!(app.workflow_stream_request, Some(99));
    let rejected = json!({"schemaVersion":1,"command":"log.retry","ok":false,"data":null,
        "warnings":[], "error":{"code":"retry_rejected","message":"Active generation target changed"}});
    app.tx.send(Message::JsonWorkflowFinished { request:99,result:Ok(response(rejected.clone(),1)) }).unwrap();
    app.receive_messages();
    let log = app.workflow_log.as_ref().unwrap();
    assert_eq!(log.success, Some(false));
    assert_eq!(log.report.as_ref().unwrap().response.as_ref().unwrap().payload, rejected);
    assert_eq!(app.request_id, 0, "backend refusal must not automatically resubmit or scan");
}

#[test]
fn retry_cancel_waits_for_transport_receipt_and_rejects_double_submit() {
    let mut app = app();
    let cancelled = running(&mut app, 4);
    key(&mut app, KeyCode::Esc);
    assert!(cancelled.load(Ordering::Relaxed));
    assert_eq!(app.workflow_stream_request, Some(4), "transport completion retains cancellation diagnostics");
    key(&mut app, KeyCode::Esc);
    app.start_workflow(WorkflowAction::RetryVerification("fixture".into()));
    assert_eq!(app.request_id, 0);
    // A command that completed before cancellation still reports its real result.
    app.tx.send(Message::JsonWorkflowFinished { request:4,result:Ok(response(json!({
        "schemaVersion":1,"command":"log.retry","ok":true,"data":{"status":"verified"}}),0)) }).unwrap();
    app.receive_messages();
    assert!(!app.workflow_log.as_ref().unwrap().running);
    assert_eq!(app.workflow_log.as_ref().unwrap().success, Some(true));
}

#[test]
fn profile_setter_survives_row_editor_and_confirmation_without_path_reclassification() {
    for choices in [json!([]), json!(["old","new"])] {
        let mut app = app();
        app.screen = Screen::Config;
        app.settings_selected = 1;
        let path = "envy.agentProfiles.fixture.model";
        app.pages.insert(Screen::Config, PageState { payload:Some(json!({"data":{"fields":[{
            "path":path,"prompt":"Model","value":"old","setter":"agent.set","group":"AGENT PROFILES","choices":choices
        }]}})), ..Default::default() });
        app.begin_setting_edit();
        if let Some(edit) = app.setting_edit.as_mut() {
            edit.buffer = "new".into();
            app.commit_setting_edit();
        } else {
            app.setting_chooser.as_mut().unwrap().selected = 1;
            app.confirm_setting_choice();
        }
        let pending = app.pending_setting.as_ref().unwrap();
        assert_eq!(pending.key, SettingKey::Config(path.into()));
        assert_eq!(pending.setter, SettingSetter::Agent);
        assert_eq!(crate::backend::setting_args(&pending.key,&pending.setter,&pending.value).unwrap(),
            ["agent","set",path,"new","--json","--yes"]);
    }
}

#[test]
fn setting_partial_failure_invalidates_all_machine_requests_without_automatic_reads() {
    let mut app = app();
    app.screen = Screen::Config;
    app.setting_request = Some(90);
    app.setting_loading = true;
    let cancelled = running(&mut app, 91);
    for screen in [Screen::Config, Screen::Software, Screen::Home, Screen::History, Screen::Journal] {
        app.pages.insert(screen, PageState {payload:Some(json!({"old":true})),request:Some(2),loading:true,..Default::default()});
    }
    let storage = StorageRequest::default();
    app.storage_cache.insert(storage.clone(),PageState {request:Some(3),loading:true,..Default::default()});
    app.search_cache.insert("old".into(),PageState {request:Some(4),loading:true,..Default::default()});
    app.tx.send(Message::SettingApplied {request:90,key:SettingKey::Config("envy.agents.fixture.model".into()),
        result:Err("RuntimeRefreshConflict: declarations saved; partial runtime refresh".into())}).unwrap();
    app.tx.send(Message::Loaded {request:2,target:LoadTarget::Screen(Screen::Config),payload:json!({"old":true})}).unwrap();
    app.tx.send(Message::StorageFinished {request:3,target:storage,result:Err("late old scan".into())}).unwrap();
    app.receive_messages();
    assert!(app.pages.is_empty());
    assert!(app.storage_cache.is_empty());
    assert!(app.search_cache.is_empty());
    assert!(cancelled.load(Ordering::Relaxed));
    assert_eq!(app.request_id,0,"failure must not trigger Storage, Plan, or any read/mutation");
    assert!(app.overlay_error.as_ref().unwrap().contains("declarations saved"));
    // A later explicit safe Settings reload must not erase the write error.
    app.pages.insert(Screen::Config,PageState {request:Some(101),loading:true,..Default::default()});
    app.tx.send(Message::Loaded {request:101,target:LoadTarget::Screen(Screen::Config),payload:json!({"fields":[]})}).unwrap();
    app.receive_messages();
    assert!(app.overlay_error.as_ref().unwrap().contains("partial runtime"));
}


#[test]
fn empty_failed_history_and_journal_reports_still_open_complete_details() {
    for (view,screen,command) in [(HistoryView::Generations,Screen::History,"history"),
        (HistoryView::Operations,Screen::Journal,"log")] {
        let mut app = app();
        app.screen = Screen::History;
        app.history_view = view;
        let payload = json!({"schemaVersion":1,"command":command,"ok":false,"data":null,
            "warnings":["receipt unavailable"],"error":{"code":"workflow_failed","message":"fixture failure"}});
        let report = WorkflowReport::from_result(Ok(response(payload.clone(),1)));
        app.pages.insert(screen,PageState {payload:Some(payload.clone()),workflow_report:Some(report),..Default::default()});
        key(&mut app,KeyCode::Enter);
        let Some(DetailView::Report {report,selected,..}) = app.detail.as_ref() else {panic!("empty report must remain inspectable");};
        assert!(selected.is_none());
        assert_eq!(report.response.as_ref().unwrap().payload,payload);
        assert!(!report.is_success());
        assert_eq!(app.request_id,0);
    }
}
