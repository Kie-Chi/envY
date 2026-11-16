use super::*;
use std::os::unix::process::ExitStatusExt;

pub(crate) fn fixture() -> Value {
    serde_json::from_str(include_str!("../fixtures/operations-v1.json")).unwrap()
}
pub(crate) fn response(command: &str, data: Value, ok: bool) -> crate::transport::JsonResponse {
    crate::transport::JsonResponse {
        payload: json!({"schemaVersion":1,"command":command,"ok":ok,"data":data,"warnings":[],"error":if ok { Value::Null } else { json!({"code":"partial_failure","message":"synthetic failure"}) }}),
        status: std::process::ExitStatus::from_raw(if ok { 0 } else { 256 }),
        stderr: "synthetic stderr\n".into(),
    }
}
fn ready() -> Actions {
    Actions {
        capabilities: Some(
            response(
                "storage capabilities",
                fixture()["capabilities"].clone(),
                true,
            )
            .payload,
        ),
        ..Actions::default()
    }
}
fn preview_intent(operation: Operation) -> Intent {
    let request = &fixture()[if operation == Operation::Restore {
        "restorePreview"
    } else {
        "maintenancePreview"
    }]["request"];
    Intent {
        operation,
        plan_id: String::new(),
        run_id: request["runId"].as_str().unwrap_or("").into(),
        target: request["absoluteTarget"].as_str().unwrap_or("").into(),
        actions: if operation == Operation::Maintain {
            vec![("go".into(), "--go".into()), ("npm".into(), "--npm".into())]
        } else {
            vec![]
        },
    }
}

#[test]
fn producer_capabilities_keep_all_eight_defaults_false_and_go_requires_metadata_optin() {
    let mut actions = ready();
    assert_eq!(actions.selected, [false; 8]);
    assert_eq!(
        fixture()["capabilities"]["maintenanceSelection"]["frontendDefault"],
        json!([])
    );
    for id in ACTION_IDS {
        assert!(actions.metadata(id).is_some());
    }
    assert!(actions.selectable("go"));
    assert!(!actions.selectable("go-modcache"));
    actions.capabilities.as_mut().unwrap()["data"]["actions"][2]["policyEnabled"] = json!(false);
    assert!(!actions.selectable("go"));
}

#[test]
fn empty_selection_rejected_and_flags_come_from_backend_metadata() {
    let mut actions = ready();
    assert!(actions.intent().is_err());
    for item in actions.capabilities.as_mut().unwrap()["data"]["actions"]
        .as_array_mut()
        .unwrap()
    {
        item["policyEnabled"] = json!(true);
    }
    actions.selected = [true; 8];
    let intent = actions.intent().unwrap();
    assert_eq!(
        intent.argv(None).unwrap(),
        [
            "storage",
            "maintain",
            "--nix",
            "--docker",
            "--go",
            "--go-modcache",
            "--uv",
            "--npm",
            "--pip",
            "--brew",
            "--preview",
            "--json"
        ]
    );
    // Verify metadata mapping is consumed rather than reconstructed from IDs.
    actions.capabilities.as_mut().unwrap()["data"]["actions"][0]["flag"] = json!("--docker");
    assert_eq!(actions.intent().unwrap().argv(None).unwrap()[2], "--docker");
    actions.capabilities.as_mut().unwrap()["data"]["actions"][0]["flag"] = json!("--yes");
    assert!(actions.intent().is_err());
}

#[test]
fn producer_maintenance_and_restore_previews_are_reviewable_without_execution() {
    for (key, op) in [
        ("maintenancePreview", Operation::Maintain),
        ("restorePreview", Operation::Restore),
    ] {
        let mut actions = ready();
        let intent = preview_intent(op);
        actions.accept(
            &Task::Preview(intent.clone()),
            WorkflowReport::from_result(Ok(response(
                &format!("storage {}", op.name()),
                fixture()[key].clone(),
                true,
            ))),
            1_700_000_000,
        );
        assert_eq!(actions.stage, Stage::Review);
        assert_eq!(actions.preview.as_ref().unwrap().0, intent);
        assert!(actions.text().contains("Complete backend envelope"));
        actions.cancel();
        assert_eq!(actions.stage, Stage::Editing);
        assert!(actions.preview.is_none());
    }
}

#[test]
fn run_never_generates_implicit_plan_and_restore_target_is_one_literal_argv() {
    let mut intent = preview_intent(Operation::Restore);
    intent.target = "/new/空 格/$(never);`no`".into();
    let args = intent.argv(None).unwrap();
    assert_eq!(
        args,
        [
            "storage",
            "restore",
            &intent.run_id,
            "--target",
            &intent.target,
            "--preview",
            "--json"
        ]
    );
    let execute = intent.argv(Some("token")).unwrap();
    assert!(!execute.contains(&"--preview".into()));
    assert!(execute.ends_with(&[
        "--preview-token".into(),
        "token".into(),
        "--yes".into(),
        "--json".into()
    ]));
    intent.operation = Operation::Run;
    assert!(intent.argv(None).is_err());
    intent.plan_id = "saved-plan".into();
    assert_eq!(
        intent.argv(None).unwrap(),
        ["storage", "run", "saved-plan", "--preview", "--json"]
    );
    intent.operation = Operation::Restore;
    intent.target = "relative".into();
    assert!(intent.argv(None).is_err());
}

#[test]
fn legacy_capabilities_cannot_fallback_to_destructive_commands() {
    let mut actions = ready();
    actions.capabilities.as_mut().unwrap()["data"]["operations"] = Value::Null;
    for item in actions.capabilities.as_mut().unwrap()["data"]["commands"]
        .as_array_mut()
        .unwrap()
    {
        item["flags"] = json!([{"flag":"--yes"}]);
    }
    actions.selected[2] = true;
    for op in [
        Operation::Run,
        Operation::Purge,
        Operation::Maintain,
        Operation::Restore,
    ] {
        actions.operation = op;
        assert!(!actions.supported());
        assert!(actions.intent().is_err());
    }
}

#[test]
fn failed_expired_mismatched_previews_are_visible_without_tokens() {
    for kind in ["failed", "expired", "mismatch", "unknown", "errors"] {
        let mut data = fixture()["maintenancePreview"].clone();
        let mut ok = true;
        match kind {
            "failed" => ok = false,
            "expired" => data["expiresAt"] = json!(1),
            "mismatch" => data["request"]["actions"] = json!(["docker"]),
            "unknown" => data["unknown"] = json!([{"reason":"probe unknown"}]),
            _ => data["errors"] = json!([{"reason":"drift"}]),
        }
        let mut actions = ready();
        actions.accept(
            &Task::Preview(preview_intent(Operation::Maintain)),
            WorkflowReport::from_result(Ok(response("storage maintain", data.clone(), ok))),
            1_700_000_000,
        );
        assert_eq!(actions.stage, Stage::Result);
        assert!(actions.preview.is_none());
        assert_eq!(
            actions
                .report
                .as_ref()
                .unwrap()
                .response
                .as_ref()
                .unwrap()
                .payload["data"],
            data
        );
    }
}

#[test]
fn producer_partial_and_unknown_results_keep_payload_status_stderr_and_refresh_error() {
    for state in ["partial", "unknown", "complete"] {
        let mut data = fixture()["partialResult"].clone();
        data["state"] = json!(state);
        data["refreshedStatus"] =
            json!({"status":"unavailable","reason":"synthetic refresh failed"});
        let mut actions = ready();
        actions.accept(
            &Task::Execute(preview_intent(Operation::Maintain), "used".into()),
            WorkflowReport::from_result(Ok(response("storage maintain", data.clone(), false))),
            1_700_000_000,
        );
        let text = actions.text();
        assert!(text.contains(state));
        assert!(text.contains("synthetic refresh failed"));
        assert!(text.contains("synthetic stderr"));
        assert!(text.contains("exit status: 1"));
        assert!(actions.preview.is_none());
        assert_eq!(
            actions.report.unwrap().response.unwrap().payload["data"],
            data
        );
    }
}

#[test]
fn input_and_context_changes_destroy_preview_and_policy_metadata() {
    let mut actions = ready();
    actions.accept(
        &Task::Preview(preview_intent(Operation::Maintain)),
        WorkflowReport::from_result(Ok(response(
            "storage maintain",
            fixture()["maintenancePreview"].clone(),
            true,
        ))),
        1_700_000_000,
    );
    assert!(actions.preview.is_some());
    actions.changed();
    assert!(actions.preview.is_none());
    actions.invalidate();
    assert!(actions.capabilities.is_none());
    assert_eq!(actions.selected, [false; 8]);
    assert!(actions.text().contains("unknown external effects"));
}

#[test]
fn backend_canonical_restore_target_is_reviewed_without_rust_filesystem_resolution() {
    let mut intent = preview_intent(Operation::Restore);
    intent.target = "/fixture/home/unused/../recovered".into();
    let mut actions = ready();
    actions.accept(
        &Task::Preview(intent.clone()),
        WorkflowReport::from_result(Ok(response(
            "storage restore",
            fixture()["restorePreview"].clone(),
            true,
        ))),
        1_700_000_000,
    );
    assert_eq!(actions.stage, Stage::Review);
    let execution = intent.argv(Some("bound-token")).unwrap();
    assert_eq!(execution[4], "/fixture/home/unused/../recovered");
    assert!(actions.text().contains("/fixture/home/recovered"));
}

#[test]
fn storage_operations_keep_existing_space_envelope_names() {
    for operation in [
        Operation::Run,
        Operation::Purge,
        Operation::Maintain,
        Operation::Restore,
    ] {
        let intent = Intent {
            operation,
            plan_id: "saved".into(),
            run_id: "run".into(),
            target: "/new".into(),
            actions: vec![],
        };
        for task in [
            Task::Preview(intent.clone()),
            Task::Execute(intent, "token".into()),
        ] {
            assert_eq!(task.command(), format!("storage {}", operation.name()));
        }
    }
}

#[test]
fn transport_failure_after_confirmation_marks_unknown_and_keeps_stderr() {
    let mut actions = ready();
    actions.accept(
        &Task::Execute(preview_intent(Operation::Maintain), "claimed".into()),
        WorkflowReport::from_result(Err(crate::transport::TransportError {
            kind: crate::transport::ErrorKind::Timeout,
            message: "synthetic deadline after claim".into(),
            response: None,
            stderr: "synthetic prior stage evidence".into(),
        })),
        1_700_000_000,
    );
    assert!(actions.notice.contains("unknown"));
    assert!(actions.text().contains("synthetic prior stage evidence"));
    assert!(actions.text().contains("deadline after claim"));
    assert!(actions.preview.is_none());
}
