use super::*;
use serde_json::json;

fn envelope(view: StorageView, data: Value) -> StorageReport {
    StorageReport::from_payload(
        view,
        json!({
            "schemaVersion": 1, "command": format!("storage {}", view.command()),
            "ok": true, "data": data, "warnings": []
        }),
    )
}

fn inspection() -> StorageReport {
    envelope(
        StorageView::Inspect,
        json!({
            "readOnly": true, "totalsAdditive": false, "policyEvaluated": false,
            "items": [
                {"id": "parent", "path": "/home/cache", "logicalBytes": 9007199254740993u64,
                 "allocatedBytes": 8, "nativeReclaimableBytes": null, "policyEligibleBytes": null,
                 "status": "complete", "includedIn": null},
                {"id": "child", "path": "/home/cache/child", "logicalBytes": null,
                 "allocatedBytes": null, "observedLogicalBytes": 55, "status": "partial",
                 "includedIn": "parent", "reasons": ["scan_budget_exhausted"]}
            ],
            "reports": {
                "docker": {
                    "status": "complete", "readOnly": true, "totalsAdditive": false,
                    "images": {"count": 1, "logicalBytes": 100, "items": [
                        {"id": "image:one", "logicalBytes": 300, "sharedBytes": 200,
                         "nativeReclaimableBytes": 0, "policyEligibleBytes": 0,
                         "association": {"status": "complete", "referencedBy": ["container-one"],
                                         "observedReferencedBy": ["container-one"]}}
                    ]},
                    "containers": {"count": 1, "associationStatus": "complete", "items": [
                        {"id": "container-one", "imageId": "image:one", "state": "running",
                         "mounts": [{"type": "volume", "name": "amux-home", "rw": true, "destination": "/home"}]}
                    ]},
                    "volumes": {"count": 1, "items": [
                        {"id": "amux-home", "protected": true, "policyEligibleBytes": 0,
                         "association": {"status": "complete", "referencedBy": ["container-one"],
                                         "observedReferencedBy": ["container-one"]}}
                    ]},
                    "buildCache": {"count": 1, "items": [
                        {"id": "cache-one", "shared": true, "inUse": null,
                         "nativeReclaimableBytes": null, "policyEligibleBytes": null}
                    ]},
                    "projects": [{"project": "example", "objects": [
                        {"kind": "buildCache", "id": "cache-one", "action": "report"}],
                        "test": null, "completionEvidence": null}]
                },
                "applications": {"reports": {
                    "codex": {"status": "unknown", "current": {"version": "1.2.3"},
                              "rollbackVersion": "1.2.2", "processStatus": "unknown",
                              "versions": [{"version": "1.2.3", "protected": true,
                                            "protectionReasons": ["current", "inactive_time_unknown"]}]},
                    "vscode": {"updaterRunning": null, "completionStatus": "unknown", "staging": []},
                    "chrome": {"completionStatus": "unknown", "codeSignClone": null}
                }},
                "projects": {"projects": [{"requestedPath": "/explicit project/subdir", "root": "/explicit project/subdir",
                    "gitRoot": "/explicit project", "status": "complete", "files": {"Cargo.lock": {"present": true}},
                    "candidates": [{"relativePath": "target", "present": false, "status": "absent"}],
                    "itemIds": []}], "items": []}
            }
        }),
    )
}

#[test]
fn default_request_is_inspection_and_only_explicit_projects_become_argv() {
    assert_eq!(
        StorageRequest::default().argv().unwrap(),
        ["storage", "inspect", "--json"]
    );
    let projects = vec![
        "/tmp/a project/子目录".into(),
        "/tmp/$(touch never);`echo no`".into(),
        "--yes".into(),
    ];
    let request = StorageRequest {
        projects: projects.clone(),
        ..Default::default()
    };
    assert_eq!(
        request.argv().unwrap(),
        [
            "storage",
            "inspect",
            "--json",
            "--project=/tmp/a project/子目录",
            "--project=/tmp/$(touch never);`echo no`",
            "--project=--yes",
        ]
    );
    for view in [StorageView::Status, StorageView::Validate] {
        let request = StorageRequest {
            view,
            projects: projects.clone(),
        };
        assert_eq!(
            request.argv().unwrap(),
            ["storage", view.command(), "--json"]
        );
    }
    for path in ["", "  ", "bad\0path"] {
        assert!(StorageRequest {
            projects: vec![path.into()],
            ..Default::default()
        }
        .argv()
        .is_err());
    }
    assert_ne!(StorageRequest::default(), request);
    assert_eq!(
        StorageView::Inspect.next().next().next(),
        StorageView::Inspect
    );
}

#[test]
fn integer_bytes_preserve_precision_nulls_and_observed_lower_bounds() {
    let report = inspection();
    let rows = report.rows();
    let parent = rows.iter().find(|row| row.label == "parent").unwrap();
    let child = rows.iter().find(|row| row.label == "child").unwrap();
    assert_eq!(bytes_text(parent.measurements.logical), "9007199254740993");
    assert_eq!(
        bytes_text(parent.measurements.native_reclaimable),
        "unknown"
    );
    assert_eq!(bytes_text(child.measurements.logical), "unknown");
    assert_eq!(bytes_text(child.measurements.allocated), "unknown");
    assert_eq!(bytes_text(Some(0)), "0");
    assert_eq!(bytes_text(Some(u64::MAX)), "18446744073709551615");
    assert_eq!(
        report.associations(child)[0].pointer.as_deref(),
        Some("/data/items/0")
    );
    assert!(!rows.iter().any(|row| row.label == "Total"));
    let image_group = rows
        .iter()
        .find(|row| row.pointer == "/data/reports/docker/images")
        .unwrap();
    assert_eq!(
        image_group.measurements.logical,
        Some(100),
        "use engine-wide size, not a per-image sum"
    );
}

#[test]
fn malformed_measurements_are_unknown_with_visible_diagnostics() {
    let report = envelope(
        StorageView::Inspect,
        json!({
            "items": [{"id": "bad", "logicalBytes": -1, "allocatedBytes": 1.25,
                       "nativeReclaimableBytes": "100", "policyEligibleBytes": true}], "reports": {}
        }),
    );
    let rows = report.rows();
    let row = rows.iter().find(|row| row.label == "bad").unwrap();
    assert_eq!(row.measurements, StorageMeasurements::default());
    assert_eq!(rows.iter().filter(|row| row.status == "error").count(), 4);
    assert!(report.detail_text(row).contains("1.25"));
}

#[test]
fn partial_business_failure_keeps_data_errors_warnings_and_stderr() {
    let mut report = inspection();
    report.payload["ok"] = json!(false);
    report.payload["error"] = json!({"code": "partial_failure", "message": "inspect errors"});
    report.payload["warnings"] = json!(["scan limited"]);
    report.payload["data"]["warnings"] = json!(["scan limited", "provider limited"]);
    report.payload["data"]["errors"] = json!([{"rule": "r1", "reason": "failed"}]);
    report.process = Some(StorageProcess {
        exit_code: Some(1),
        success: false,
        stderr: "native stderr".into(),
    });
    let original = report.payload.clone();
    let rows = report.rows();
    assert!(rows.iter().any(|row| row.label == "parent"));
    assert!(rows.iter().any(|row| row.note.contains("partial_failure")));
    assert!(rows.iter().any(|row| row.note.contains("r1")));
    assert_eq!(
        rows.iter().filter(|row| row.note == "scan limited").count(),
        1
    );
    assert!(rows.iter().any(|row| row.note == "provider limited"));
    assert!(rows.iter().any(|row| row.note.contains("native stderr")));
    assert_eq!(rows[0].status, "partial / failed");
    assert_eq!(report.payload, original);
    assert!(report.detail_text(&rows[0]).contains("native stderr"));
}

#[test]
fn invalid_envelope_or_data_shapes_have_visible_errors_and_keep_original_json() {
    for payload in [
        json!(null),
        json!([]),
        json!({"data": []}),
        json!({
            "schemaVersion": 2, "command": "storage inspect", "ok": true,
            "warnings": [], "data": {"items": "wrong", "reports": []}
        }),
    ] {
        let report = StorageReport::from_payload(StorageView::Inspect, payload.clone());
        let rows = report.rows();
        assert!(rows.iter().any(|row| row.status == "error"));
        let detail = report.detail_text(&rows[0]);
        assert!(detail.contains("Complete backend envelope JSON"));
        assert_eq!(report.payload, payload);
    }
    let report = envelope(
        StorageView::Inspect,
        json!({"items": [null, 4], "reports": {"docker": {"images": []}}}),
    );
    assert!(
        report
            .rows()
            .iter()
            .filter(|row| row.status == "error")
            .count()
            >= 5
    );
}

#[test]
fn docker_image_volume_and_container_associations_are_bidirectional() {
    let report = inspection();
    let rows = report.rows();
    let container = rows
        .iter()
        .find(|row| row.label == "container-one")
        .unwrap();
    let links = report.associations(container);
    assert_eq!(links.len(), 2);
    assert_eq!(links[0].relation, "image");
    assert_eq!(
        links[0].pointer.as_deref(),
        Some("/data/reports/docker/images/items/0")
    );
    assert_eq!(links[1].relation, "volume");
    assert_eq!(
        links[1].pointer.as_deref(),
        Some("/data/reports/docker/volumes/items/0")
    );
    for label in ["image:one", "amux-home"] {
        let row = rows.iter().find(|row| row.label == label).unwrap();
        let links = report.associations(row);
        assert_eq!(links.len(), 1);
        assert_eq!(links[0].id, "container-one");
        assert_eq!(links[0].status, "complete");
        assert_eq!(
            links[0].pointer.as_deref(),
            Some(container.pointer.as_str())
        );
    }
    let cache = rows.iter().find(|row| row.label == "cache-one").unwrap();
    assert!(
        report.associations(cache).is_empty(),
        "never invent a container reference for cache metadata"
    );
    let detail = report.detail_text(cache);
    assert!(detail.contains("\"shared\": true"));
    assert!(detail.contains("\"completionEvidence\": null"));
}

#[test]
fn incomplete_associations_preserve_observations_without_claiming_unused() {
    let mut report = inspection();
    let association =
        &mut report.payload["data"]["reports"]["docker"]["images"]["items"][0]["association"];
    *association = json!({"status": "unknown", "referencedBy": null,
        "observedReferencedBy": ["container-one", "missing-container"], "reason": "inventory incomplete"});
    let row = report
        .rows()
        .into_iter()
        .find(|row| row.label == "image:one")
        .unwrap();
    let links = report.associations(&row);
    assert_eq!(links.len(), 2);
    assert!(links.iter().all(|link| link.status == "unknown"));
    assert!(links[0].pointer.is_some());
    assert!(links[1].pointer.is_none());
    let detail = report.detail_text(&row);
    assert!(detail.contains("unknown is not unreferenced"));
    assert!(detail.contains("inventory incomplete"));
}

#[test]
fn every_row_retains_complete_json_and_application_project_evidence() {
    let report = inspection();
    for row in report.rows() {
        let detail = report.detail_text(&row);
        assert!(detail.contains("Selected value JSON"));
        assert!(detail.contains("Complete backend envelope JSON"));
        assert!(detail.contains("inactive_time_unknown"));
        assert!(detail.contains("\"updaterRunning\": null"));
        assert!(detail.contains("/explicit project/subdir"));
    }
    let rows = report.rows();
    for label in ["codex", "vscode", "chrome", "/explicit project/subdir"] {
        assert!(rows.iter().any(|row| row.label == label), "missing {label}");
    }
}

#[test]
fn validation_partial_failure_keeps_each_rule_and_exclusion() {
    let mut report = envelope(
        StorageView::Validate,
        json!({
            "ok": false,
            "rules": [{"rule": "valid", "root": "/a", "ok": true},
                      {"rule": "missing", "root": "/b", "ok": true, "skipped": true},
                      {"rule": "invalid", "root": "/c", "ok": false, "error": "check failed"}],
            "excluded": ["protected"]
        }),
    );
    report.payload["ok"] = json!(false);
    let rows = report.rows();
    assert_eq!(
        rows.iter()
            .filter(|row| row.kind == StorageRowKind::Rule)
            .count(),
        3
    );
    for (label, status) in [
        ("valid", "ok"),
        ("missing", "skipped"),
        ("invalid", "error"),
    ] {
        assert_eq!(
            rows.iter().find(|row| row.label == label).unwrap().status,
            status
        );
    }
    assert!(report.detail_text(&rows[0]).contains("protected"));
}

#[test]
fn status_preserves_rules_archive_state_and_durable_record_details() {
    let report = envelope(
        StorageView::Status,
        json!({
            "mode": "report", "rules": ["r1", "r2"], "ruleDetails": [{"id": "r1", "path": "/a", "action": "quarantine"}],
            "excluded": ["/protected"], "maintenance": ["nix"], "archiveConfigured": true,
            "archive": {"configured": true, "repositoryChecked": false,
                        "passwordFile": {"configured": true, "private": true}},
            "latestPlan": {"id": "plan-id", "createdAt": "2026-01-01", "candidates": [{"path": "/one"}]},
            "runs": [{"id": "run-id", "errors": ["interrupted"], "entries": [{"path": "/two"}]}]
        }),
    );
    let rows = report.rows();
    assert!(rows.iter().any(|row| row.label == "r1"));
    assert!(rows.iter().any(|row| row.label == "r2"));
    let archive = rows.iter().find(|row| row.label == "archive").unwrap();
    assert!(report
        .detail_text(archive)
        .contains("\"repositoryChecked\": false"));
    let run = rows.iter().find(|row| row.label == "run-id").unwrap();
    assert!(report.detail_text(run).contains("interrupted"));
}

#[test]
fn provider_names_use_escaped_json_pointers() {
    let report = envelope(
        StorageView::Inspect,
        json!({"items": [], "reports": {"custom/a~b": {"status": "unknown"}}}),
    );
    let row = report
        .rows()
        .into_iter()
        .find(|row| row.label == "custom/a~b")
        .unwrap();
    assert_eq!(row.pointer, "/data/reports/custom~1a~0b");
    assert_eq!(report.value(&row).unwrap()["status"], "unknown");
}

#[test]
fn strict_envelope_rejects_legacy_and_mismatched_commands_before_reading_data() {
    for payload in [
        json!({"items": [{"id": "must-not-render"}]}),
        json!({"schemaVersion": 1, "data": {"items": [{"id": "must-not-render"}], "reports": {}}}),
        json!({"schemaVersion": 1, "command": "storage status", "ok": true, "warnings": [],
               "data": {"items": [{"id": "must-not-render"}], "reports": {}}}),
    ] {
        let report = StorageReport::from_payload(StorageView::Inspect, payload);
        let rows = report.rows();
        assert!(rows.iter().any(|row| row.status == "error"));
        assert!(!rows.iter().any(|row| row.label == "must-not-render"));
        assert_eq!(rows[0].status, "protocol error");
    }
}

#[test]
fn native_nonzero_response_remains_visible_even_when_business_ok_is_true() {
    use std::os::unix::process::ExitStatusExt;
    let report = StorageReport::from_response(
        StorageView::Inspect,
        crate::transport::JsonResponse {
            payload: inspection().payload,
            status: std::process::ExitStatus::from_raw(7 << 8),
            stderr: "native warning".into(),
        },
    );
    let rows = report.rows();
    assert_eq!(rows[0].status, "partial / failed");
    assert!(rows.iter().any(|row| row.note.contains("exit: 7")));
    assert!(rows.iter().any(|row| row.label == "parent"));
    assert!(report.detail_text(&rows[0]).contains("Process exit: 7"));
}

#[test]
fn python_cli_golden_inspection_preserves_absence_permission_unknown_and_containment() {
    let payload: Value =
        serde_json::from_str(include_str!("fixtures/inspect-darwin.json")).unwrap();
    crate::transport::validate_envelope(&payload, "storage inspect").unwrap();
    let report = StorageReport::from_payload(StorageView::Inspect, payload.clone());
    let data = report.data().unwrap();
    assert_eq!(data["home"], "/fixture/home");
    assert_eq!(data["readOnly"], true);
    assert_eq!(data["policyEvaluated"], false);
    assert_eq!(data["totalsAdditive"], false);
    let rows = report.rows();
    assert_eq!(
        rows.iter()
            .filter(|row| row.kind == StorageRowKind::Filesystem)
            .count(),
        data["items"].as_array().unwrap().len()
    );
    let codex = rows.iter().find(|row| row.label == "codex-data").unwrap();
    assert_eq!(codex.status, "unknown");
    assert_eq!(codex.measurements, StorageMeasurements::default());
    assert!(report.detail_text(codex).contains("permission_denied"));
    let cache = rows.iter().find(|row| row.label == "nix-cache").unwrap();
    assert_eq!(cache.status, "absent");
    assert_eq!(cache.measurements.logical, Some(0));
    assert_eq!(cache.measurements.allocated, Some(0));
    assert_eq!(cache.measurements.native_reclaimable, None);
    assert_eq!(cache.measurements.policy_eligible, None);
    let tarballs = rows.iter().find(|row| row.label == "nix-tarballs").unwrap();
    let links = report.associations(tarballs);
    assert_eq!(links.len(), 1);
    assert_eq!(links[0].relation, "includedIn");
    assert_eq!(links[0].id, "nix-cache");
    assert_eq!(links[0].pointer.as_deref(), Some(cache.pointer.as_str()));
    assert!(rows
        .iter()
        .any(|row| row.note.contains("synthetic provider unavailable")));
    assert!(!rows.iter().any(|row| row.label == "Invalid report shape"));
    assert_eq!(report.payload, payload);
}
