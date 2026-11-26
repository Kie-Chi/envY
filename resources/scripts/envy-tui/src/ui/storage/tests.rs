use super::*;
use ratatui::{backend::TestBackend, buffer::Buffer, Terminal};
use serde_json::json;

fn text(buffer: &Buffer) -> String {
    (0..buffer.area.height)
        .map(|y| {
            (0..buffer.area.width)
                .map(|x| buffer[(x, y)].symbol())
                .collect::<String>()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn report() -> StorageReport {
    StorageReport::from_payload(
        StorageView::Inspect,
        json!({
            "schemaVersion": 1, "command": "storage inspect", "ok": true, "warnings": [],
            "data": {"items": [
                {"id": "sparse-data", "path": "/explicit/子目录/a path", "status": "complete",
                 "logicalBytes": 9007199254740993u64, "allocatedBytes": 0,
                 "nativeReclaimableBytes": null, "policyEligibleBytes": 0},
                {"id": "unknown-data", "status": "partial", "logicalBytes": null,
                 "allocatedBytes": null, "observedLogicalBytes": 999}
            ], "reports": {}}
        }),
    )
}

fn render(report: Option<&StorageReport>, width: u16, height: u16, error: Option<&str>) -> String {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    terminal
        .draw(|frame| {
            render_storage(
                frame,
                frame.area(),
                StoragePage {
                    view: StorageView::Inspect,
                    report,
                    selected: 1,
                    scroll: 0,
                    loading: false,
                    error,
                    projects: &[],
                    project_input: None,
                    timing: StorageTiming {
                        received_at: Some(100),
                        saved_at: None,
                        now: 280,
                    },
                },
            )
        })
        .unwrap();
    text(terminal.backend().buffer())
}

#[test]
fn storage_table_uses_readable_sizes_and_keeps_unmeasured_values_distinct() {
    let report = report();
    let screen = render(Some(&report), 240, 18, None);
    assert!(screen.contains("Inspection"));
    assert!(screen.contains("Policy & records"));
    assert!(screen.contains("Rule validation"));
    assert!(screen.contains("8.0 PiB"));
    assert!(screen.contains("SIZE"));
    assert!(screen.contains("ON DISK"));
    assert!(!screen.contains("RECLAIMABLE"));
    assert!(screen.contains("CANDIDATE"));
    assert!(screen.contains("—"));
    assert!(screen.contains("0 B"));
    assert!(
        !screen.contains("999"),
        "observed lower bounds do not replace unknown bytes"
    );
    assert!(!screen.contains("9007199254740992"));
    assert!(!screen.contains("rows overlap"));
    assert!(screen.contains("Updated 3 min ago"));
    assert!(!screen.contains("--yes"));
    assert!(!screen.contains("maintain"));
}

#[test]
fn missing_metrics_and_provider_failures_have_different_presentations() {
    let report = StorageReport::from_payload(StorageView::Inspect,
        serde_json::from_str(include_str!("../../storage/fixtures/inspect-darwin.json")).unwrap());
    let screen = render(Some(&report), 180, 36, None);
    assert!(screen.contains("! No access"));
    assert!(screen.contains("! Unavailable"));
    assert!(!screen.contains("RECLAIMABLE"));
    assert!(!screen.contains("CANDIDATE"));
    assert!(!screen.contains("report only"));
    assert!(report.summary().contains("need attention"));
    let codex = report.rows().into_iter().find(|row| row.label == "codex-data").unwrap();
    assert_eq!(codex.measurements.logical, None);
    assert!(report.detail_text(&codex).contains("permission_denied"));
}

#[test]
fn provider_measurements_are_visible_without_fabricating_missing_values() {
    let report = StorageReport::from_payload(StorageView::Inspect, json!({
        "schemaVersion":1, "command":"storage inspect", "ok":true, "warnings":[],
        "data":{"items":[], "reports":{"native":{"status":"complete","logicalBytes":1024,
            "nativeReclaimableBytes":512,"allocatedBytes":null}}}
    }));
    let screen = render(Some(&report), 180, 20, None);
    assert!(screen.contains("1.0 KiB"));
    assert!(screen.contains("512 B"));
    assert!(screen.contains("RECLAIMABLE"));
    assert!(!screen.contains("ON DISK"));
}

#[test]
fn policy_and_rules_do_not_render_capacity_columns() {
    for view in [StorageView::Status, StorageView::Validate] {
        let report = StorageReport::from_payload(view, json!({
            "schemaVersion":1, "command":format!("storage {}", view.command()), "ok":true, "warnings":[],
            "data":{"mode":"manual", "ruleDetails":[], "rules":[], "runs":[]}
        }));
        let mut terminal = Terminal::new(TestBackend::new(180, 24)).unwrap();
        terminal.draw(|frame| render_storage(frame, frame.area(), StoragePage {
            view, report:Some(&report), selected:0, scroll:0, loading:false, error:None,
            timing:StorageTiming::default(), projects:&[], project_input:None,
        })).unwrap();
        let screen = text(terminal.backend().buffer());
        assert!(!screen.contains("SIZE"));
        assert!(!screen.contains("ON DISK"));
        assert!(!screen.contains("unknown"));
    }
}

#[test]
fn cached_and_unknown_receipt_times_remain_explicit() {
    assert_eq!(
        StorageTiming {
            saved_at: Some(100),
            received_at: Some(250),
            now: 280
        }
        .label(),
        "Cached · 3 min ago"
    );
    assert_eq!(
        StorageTiming {
            now: 280,
            ..Default::default()
        }
        .label(),
        "Not refreshed"
    );
}

#[test]
fn transport_error_keeps_previous_report_visible() {
    let report = report();
    let screen = render(
        Some(&report),
        140,
        20,
        Some("Timeout: Storage inspection exceeded deadline"),
    );
    assert!(screen.contains("Timeout"));
    assert!(screen.contains("sparse-data"));
    assert!(screen.contains("unknown-data"));
    let empty = render(None, 120, 20, Some("Protocol: response was not JSON"));
    assert!(empty.contains("Protocol"));
    assert!(empty.contains("Storage unavailable"));
}

#[test]
fn malformed_shape_renders_a_visible_diagnostic() {
    let report = StorageReport::from_payload(
        StorageView::Inspect,
        json!({
            "schemaVersion": 1, "command": "storage inspect", "ok": true, "warnings": [],
            "data": {"items": "bad", "reports": []}
        }),
    );
    let screen = render(Some(&report), 140, 18, None);
    assert!(screen.contains("Invalid report shape"));
    assert!(screen.contains("! Failed"));
}

#[test]
fn detail_keeps_all_four_metrics_full_paths_and_original_null_json() {
    let report = report();
    let row = report
        .rows()
        .into_iter()
        .find(|row| row.label == "sparse-data")
        .unwrap();
    let detail = storage_detail_lines(&report, &row)
        .iter()
        .map(ToString::to_string)
        .collect::<Vec<_>>()
        .join("\n");
    assert!(detail.contains("Logical bytes: 9007199254740993"));
    assert!(detail.contains("Allocated bytes (estimate): 0"));
    assert!(detail.contains("Native reclaimable bytes: unknown"));
    assert!(detail.contains("Policy eligible bytes (estimate): 0"));
    assert!(detail.contains("/explicit/子目录/a path"));
    assert!(detail.contains("\"nativeReclaimableBytes\": null"));
    assert!(detail.contains("Complete backend envelope JSON"));
}

#[test]
fn narrow_and_tiny_terminals_render_without_panics() {
    let report = report();
    for (width, height) in [(80, 16), (38, 8), (10, 4), (1, 1)] {
        let _ = render(Some(&report), width, height, Some("example failure"));
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        let row = &report.rows()[0];
        terminal
            .draw(|frame| {
                let (scroll, maximum) =
                    render_storage_detail(frame, frame.area(), &report, row, usize::MAX);
                assert!(scroll <= maximum);
            })
            .unwrap();
    }
}

#[test]
fn details_use_wrapped_line_counts_and_clamp_scroll() {
    let report = report();
    let row = &report.rows()[1];
    let mut terminal = Terminal::new(TestBackend::new(80, 18)).unwrap();
    terminal
        .draw(|frame| {
            let (scroll, maximum) =
                render_storage_detail(frame, frame.area(), &report, row, usize::MAX);
            assert!(maximum > 0);
            assert_eq!(scroll, maximum);
        })
        .unwrap();
    assert!(text(terminal.backend().buffer()).contains("scroll"));
}

#[test]
fn report_for_another_view_is_not_shown_as_current() {
    let report = report();
    let mut terminal = Terminal::new(TestBackend::new(120, 18)).unwrap();
    terminal
        .draw(|frame| {
            render_storage(
                frame,
                frame.area(),
                StoragePage {
                    view: StorageView::Validate,
                    report: Some(&report),
                    selected: 0,
                    scroll: 0,
                    loading: true,
                    error: None,
                    projects: &[],
                    project_input: None,
                    timing: StorageTiming::default(),
                },
            )
        })
        .unwrap();
    let screen = text(terminal.backend().buffer());
    assert!(screen.contains("Loading selected view…"));
    assert!(!screen.contains("sparse-data"));
}


#[test]
fn actions_render_narrow_unicode_inputs_and_cli_handoff_without_spawning() {
    let mut actions = crate::storage::actions::Actions::default();
    actions.target = "/new/子目录/a  path/$(literal)".into();
    actions.field = Some(crate::storage::actions::Field::Target);
    actions.buffer = actions.target.clone();
    let fixture: serde_json::Value = serde_json::from_str(include_str!("../../storage/fixtures/operations-v1.json")).unwrap();
    actions.capabilities = Some(json!({"data":fixture["capabilities"]}));
    for (width,height) in [(1,1),(20,10),(80,24),(120,40)] {
        let mut terminal = Terminal::new(TestBackend::new(width,height)).unwrap();
        terminal.draw(|frame| render_actions(frame,frame.area(),&mut actions)).unwrap();
        actions.scroll = usize::MAX;
        terminal.draw(|frame| render_actions(frame,frame.area(),&mut actions)).unwrap();
        assert!(actions.scroll <= actions.scroll_max);
        actions.scroll = 0;
    }
    actions.handoff = Some(1);
    let mut terminal = Terminal::new(TestBackend::new(100,30)).unwrap();
    terminal.draw(|frame| render_actions(frame,frame.area(),&mut actions)).unwrap();
    let shown = text(terminal.backend().buffer());
    assert!(shown.contains("CLI HANDOFF ONLY: check"));
    assert!(shown.contains("Native execution is unavailable"));
    assert!(actions.pending.is_none());
}

#[test]
fn action_complete_envelope_tail_remains_reachable_beyond_u16_visual_rows() {
    use std::os::unix::process::ExitStatusExt;
    let mut actions = crate::storage::actions::Actions::default();
    let payload = json!({"data":{"target":format!("{}TAIL", "界".repeat(140_000))}});
    actions.details = true;
    actions.report = Some(crate::model::WorkflowReport::from_result(Ok(crate::transport::JsonResponse {
        payload, status: std::process::ExitStatus::from_raw(256), stderr:"bounded synthetic stderr".into(),
    })));
    actions.scroll = usize::MAX;
    let mut terminal = Terminal::new(TestBackend::new(2,12)).unwrap();
    terminal.draw(|frame| render_actions(frame,frame.area(),&mut actions)).unwrap();
    assert!(actions.scroll_max > u16::MAX as usize);
    let shown = text(terminal.backend().buffer());
    assert!(shown.contains("TA") && shown.contains("IL"));
    assert_eq!(actions.scroll,actions.scroll_max);
}
