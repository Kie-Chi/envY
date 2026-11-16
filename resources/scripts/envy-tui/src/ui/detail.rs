//! Detail popups for software items, Doctor results, and generation diffs.

use ratatui::{
    prelude::*,
    widgets::{Clear, Paragraph, Wrap},
};
use serde_json::Value;

use crate::{
    app::App,
    model::{text, DetailView, WorkflowReport, report_data},
    theme::{self, ACCENT, OK, WARN},
};

use super::popups::{centered_size, dialog_block, inset_rect, popup};
use super::spinner;

fn key_footer(scroll: Option<(usize, usize)>) -> Line<'static> {
    let mut spans = vec![theme::chip("ENTER", ACCENT), Span::raw(" close")];
    if let Some((current, maximum)) = scroll {
        spans.extend([
            Span::raw("    "),
            theme::key("↑↓"),
            Span::raw(format!(" scroll  {} / {}", current + 1, maximum + 1)),
        ]);
    }
    Line::from(spans)
}

fn bool_line(label: &'static str, value: Option<&Value>) -> Line<'static> {
    let enabled = value.and_then(Value::as_bool).unwrap_or(false);
    Line::from(vec![
        Span::styled(format!("{label:<18}"), theme::muted()),
        if enabled {
            Span::styled("yes", Style::new().fg(OK))
        } else {
            Span::raw("no")
        },
    ])
}

fn software_detail_lines(payload: &Value) -> Vec<Line<'static>> {
    let matched = payload
        .get("matches")
        .and_then(Value::as_array)
        .and_then(|matches| matches.first())
        .unwrap_or(&Value::Null);
    let reason = if matched.get("machineExclude").and_then(Value::as_bool) == Some(true) {
        "Disabled by this machine's managed exclusion."
    } else if matched.get("externalExclude").and_then(Value::as_bool) == Some(true) {
        "Blocked by policy outside the machine-managed block."
    } else if matched.get("machineInclude").and_then(Value::as_bool) == Some(true) {
        "Included directly by this machine's managed policy."
    } else if matched.get("externalInclude").and_then(Value::as_bool) == Some(true) {
        "Included by shared or feature-owned policy."
    } else if matched.get("effective").and_then(Value::as_bool) == Some(true) {
        "Effective through evaluated policy."
    } else {
        "Not effective in the evaluated machine policy."
    };
    vec![
        Line::from(vec![
            Span::styled("ITEM  ", theme::muted()),
            Span::styled(text(matched.get("item")), theme::strong()),
        ]),
        Line::from(format!("Group       {}", text(matched.get("group")))),
        Line::from(format!("Label       {}", text(matched.get("label")))),
        Line::from(format!("Name        {}", text(matched.get("name")))),
        Line::from(format!("Reference   {}", text(matched.get("ref")))),
        Line::from(""),
        Line::from(Span::styled("Evaluated state", theme::heading())),
        bool_line("Included", matched.get("included")),
        bool_line("Excluded", matched.get("excluded")),
        bool_line("Effective", matched.get("effective")),
        Line::from(""),
        Line::from(Span::styled("Ownership", theme::heading())),
        bool_line("Machine include", matched.get("machineInclude")),
        bool_line("Machine exclude", matched.get("machineExclude")),
        bool_line("External include", matched.get("externalInclude")),
        bool_line("External exclude", matched.get("externalExclude")),
        Line::from(""),
        Line::from(Span::styled("Why", theme::heading())),
        Line::from(reason),
    ]
}

/// A detail value on one line: scalars as text, lists of scalars joined.
fn detail_value(value: &Value) -> String {
    match value {
        Value::Array(items)
            if items
                .iter()
                .all(|item| !item.is_array() && !item.is_object()) =>
        {
            items
                .iter()
                .map(|item| text(Some(item)))
                .collect::<Vec<_>>()
                .join(", ")
        }
        Value::Array(_) | Value::Object(_) => value.to_string(),
        scalar => text(Some(scalar)),
    }
}

/// What a doctor action would do, in words.
fn action_text(action: &Value) -> String {
    let field = |key| action.get(key).and_then(Value::as_str).unwrap_or("—");
    match action.get("kind").and_then(Value::as_str) {
        Some("run") => format!(
            "Run {}",
            action
                .get("argv")
                .and_then(Value::as_array)
                .map(|argv| argv
                    .iter()
                    .map(|part| text(Some(part)))
                    .collect::<Vec<_>>()
                    .join(" "))
                .unwrap_or_default()
        ),
        Some("open-app") => format!("Open {}", field("name")),
        Some("open-settings") => format!("Open System Settings › {}", field("target")),
        _ => action.to_string(),
    }
}

fn doctor_detail_lines(result: &Value) -> Vec<Line<'static>> {
    let status = text(result.get("status"));
    let mut lines = vec![
        Line::from(vec![
            theme::chip(status.to_uppercase(), theme::status_color(&status)),
            Span::styled(format!("  {}", text(result.get("name"))), theme::strong()),
        ]),
        Line::from(vec![
            Span::styled("Section  ", theme::muted()),
            Span::raw(text(result.get("section"))),
        ]),
    ];
    let mut section = |title: &str, body: Vec<Line<'static>>| {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(
            title.to_string(),
            theme::heading(),
        )));
        lines.extend(body);
    };
    section("Result", vec![Line::from(text(result.get("message")))]);
    if let Some(hint) = result
        .get("hint")
        .and_then(Value::as_str)
        .filter(|hint| !hint.is_empty())
    {
        section("Hint", vec![Line::from(hint.to_string())]);
    }
    match result.get("details") {
        Some(Value::Object(details)) if !details.is_empty() => {
            let width = details
                .keys()
                .map(|key| key.chars().count())
                .max()
                .unwrap_or(0)
                .min(20);
            section(
                "Details",
                details
                    .iter()
                    .map(|(key, value)| {
                        Line::from(vec![
                            Span::styled(format!("{key:<width$}  "), theme::muted()),
                            Span::raw(detail_value(value)),
                        ])
                    })
                    .collect(),
            );
        }
        Some(Value::Object(_)) | Some(Value::Null) | None => {}
        Some(other) => section("Details", vec![Line::from(detail_value(other))]),
    }
    if let Some(action) = result.get("action").filter(|action| !action.is_null()) {
        let mut body = vec![Line::from(action_text(action))];
        if App::doctor_workflow(result).is_some() {
            body.push(Line::from(Span::styled(
                "Press x to run it; envY returns here afterward.",
                Style::new().fg(WARN),
            )));
        }
        section("Suggested action", body);
    }
    lines
}

fn strip_ansi(value: &str) -> String {
    let mut result = String::new();
    let mut chars = value.chars().peekable();
    while let Some(character) = chars.next() {
        if character == '\u{1b}' && chars.peek() == Some(&'[') {
            chars.next();
            for next in chars.by_ref() {
                if next.is_ascii_alphabetic() {
                    break;
                }
            }
        } else {
            result.push(character);
        }
    }
    result
}

fn json_lines(value: &Value) -> Vec<Line<'static>> {
    serde_json::to_string_pretty(value).unwrap_or_else(|_| value.to_string())
        .lines().map(|line| Line::from(line.to_string())).collect()
}

/// Backend facts are presented without deriving package or runtime policy.
/// Missing old receipt fields are explicitly unknown, never empty comparisons.
pub(super) fn workflow_report_lines(report: &WorkflowReport, selected: Option<&Value>) -> Vec<Line<'static>> {
    let mut lines = Vec::new();
    if let Some(error) = &report.error {
        lines.push(Line::from(Span::styled(error.clone(), Style::new().fg(WARN))));
    }
    if let Some(response) = &report.response {
        lines.push(Line::from(format!("Process: {} · backend ok: {}", response.status, text(response.payload.get("ok")))));
        if !response.is_success() {
            lines.push(Line::from(Span::styled(
                crate::transport::failure_message(&response.payload, "Failed or incomplete; inspect retained data"), Style::new().fg(WARN))));
        }
    }
    let payload = report.response.as_ref().map(|response| &response.payload);
    let data = payload.map(report_data);
    if let Some(entry) = selected {
        lines.push(Line::from(Span::styled("Selected receipt / generation", theme::heading())));
        lines.extend(json_lines(entry));
        if entry.get("number").is_some() {
            lines.push(Line::from(format!("Semantic baseline: {}", entry.get("semanticStatus").and_then(Value::as_str).unwrap_or("unknown — backend did not bind a semantic baseline"))));
        } else {
            let retry = crate::model::journal_retry(entry);
            lines.push(Line::from(match retry {
                Ok(_) => "Press x to confirm read-only generation verification. The CLI rechecks host, source and target.".into(),
                Err(reason) => format!("Retry disabled: {reason}"),
            }));
        }
        lines.push(Line::from(""));
    } else if let Some(data) = data {
        for (key, label) in [("machine", "Machine"), ("platform", "Platform"),
            ("current", "Current generation"), ("target", "Target"), ("before", "Before"), ("after", "After"),
            ("source", "Frozen source and provenance"), ("rollbackBoundary", "Rollback boundary")] {
            if let Some(value) = data.get(key) {
                lines.push(Line::from(Span::styled(label, theme::heading())));
                lines.extend(json_lines(value));
                lines.push(Line::from(""));
            }
        }
        for name in ["settings", "software", "sources", "behavior", "closure"] {
            lines.push(Line::from(Span::styled(name.to_uppercase(), theme::heading())));
            match data.get("sections").and_then(|sections| sections.get(name)) {
                Some(section) => lines.extend(json_lines(section)),
                None if name == "closure" => {
                    if let Some(diff) = data.get("closureDiff").and_then(Value::as_str) {
                        lines.push(Line::from("Legacy closure comparison (semantic baseline unknown)"));
                        if diff.is_empty() { lines.push(Line::from("No closure differences reported.")); }
                        lines.extend(strip_ansi(diff).lines().map(|line| Line::from(line.to_string())));
                    } else { lines.push(Line::from("unknown — backend did not provide a closure comparison or baseline")); }
                }
                None => lines.push(Line::from("unknown — no backend semantic comparison was provided")),
            }
            lines.push(Line::from(""));
        }
        lines.push(Line::from(Span::styled("Declared activation actions and manager boundaries", theme::heading())));
        match data.get("activation") {
            Some(activation) => lines.extend(json_lines(activation)),
            None => lines.push(Line::from("unknown — backend did not provide activation declarations")),
        }
    }
    if !report.stderr.is_empty() {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled("stderr / stage progress", theme::heading())));
        lines.extend(report.stderr.split('\n').map(|line| Line::from(crate::backend::strip_ansi(line))));
        lines.push(Line::from(Span::styled("Original stderr (escaped)", theme::heading())));
        lines.extend(json_lines(&Value::String(report.stderr.clone())));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled("Complete original JSON", theme::heading())));
    if let Some(payload) = payload { lines.extend(json_lines(payload)); }
    else if let Some(selected) = selected { lines.extend(json_lines(selected)); }
    else { lines.push(Line::from("No complete JSON response was received.")); }
    lines
}

/// Render a viewport using usize row offsets. Paragraph::scroll is u16, which
/// otherwise makes the tail of a large bounded JSON report unreachable. Wrap
/// at grapheme boundaries so long paths and narrow terminals keep every cell.
pub(super) fn report_window(
    lines: Vec<Line<'static>>, width: u16, height: u16, requested: usize,
) -> (Vec<Line<'static>>, usize, usize) {
    if width == 0 || height == 0 { return (Vec::new(), 0, 0); }
    let width = usize::from(width);
    let rows = lines.iter().map(|line| {
        let (mut rows, mut columns) = (1usize, 0usize);
        for grapheme in line.styled_graphemes(Style::default()) {
            let cells = Span::raw(grapheme.symbol).width();
            if columns > 0 && columns + cells > width { rows += 1; columns = 0; }
            columns += cells;
        }
        rows
    }).collect::<Vec<_>>();
    let maximum = rows.iter().sum::<usize>().saturating_sub(usize::from(height));
    let scroll = requested.min(maximum);
    let end = scroll.saturating_add(usize::from(height));
    let mut visible = Vec::new();
    let mut row = 0usize;
    for (line, count) in lines.iter().zip(rows) {
        if row + count <= scroll { row += count; continue; }
        let mut columns = 0usize;
        let mut current: Vec<Span<'static>> = Vec::new();
        for grapheme in line.styled_graphemes(Style::default()) {
            let cells = Span::raw(grapheme.symbol).width();
            if columns > 0 && columns + cells > width {
                if row >= scroll { visible.push(Line::from(std::mem::take(&mut current))); }
                row += 1;
                if row >= end { return (visible, maximum, scroll); }
                columns = 0;
            }
            if row >= scroll {
                if let Some(last) = current.last_mut().filter(|last| last.style == grapheme.style) {
                    last.content.to_mut().push_str(grapheme.symbol);
                } else { current.push(Span::styled(grapheme.symbol.to_string(), grapheme.style)); }
            }
            columns += cells;
        }
        if row >= scroll { visible.push(Line::from(current)); }
        row += 1;
        if row >= end { break; }
    }
    (visible, maximum, scroll)
}

fn report_popup(frame: &mut Frame, app: &mut App, area: Rect, title: &str, lines: Vec<Line<'static>>) {
    frame.render_widget(Clear, area);
    let block = dialog_block(title);
    let inner = inset_rect(block.inner(area), 2, 1);
    frame.render_widget(block, area);
    let sections = Layout::vertical([Constraint::Min(1), Constraint::Length(1)]).split(inner);
    let (visible, maximum, scroll) = report_window(lines, sections[0].width, sections[0].height, app.detail_scroll);
    app.detail_scroll_max = maximum;
    app.detail_scroll = scroll;
    frame.render_widget(Paragraph::new(visible), sections[0]);
    frame.render_widget(Paragraph::new(key_footer((maximum > 0).then_some((scroll, maximum)))), sections[1]);
}

fn detail_popup(
    frame: &mut Frame,
    app: &mut App,
    area: Rect,
    title: &str,
    lines: Vec<Line<'static>>,
) {
    frame.render_widget(Clear, area);
    let block = dialog_block(title);
    let inner = inset_rect(block.inner(area), 2, 1);
    frame.render_widget(block, area);

    let sections = Layout::vertical([Constraint::Min(1), Constraint::Length(1)]).split(inner);
    let paragraph = Paragraph::new(lines).wrap(Wrap { trim: false });
    let rendered_lines = paragraph.line_count(sections[0].width);
    let maximum = rendered_lines.saturating_sub(sections[0].height as usize);
    app.detail_scroll_max = maximum;
    app.detail_scroll = app.detail_scroll.min(maximum);

    frame.render_widget(
        paragraph.scroll((app.detail_scroll.min(u16::MAX as usize) as u16, 0)),
        sections[0],
    );
    frame.render_widget(
        Paragraph::new(key_footer(
            (maximum > 0).then_some((app.detail_scroll, maximum)),
        ))
        .alignment(Alignment::Left),
        sections[1],
    );
}

pub(super) fn render_detail(frame: &mut Frame, app: &mut App, area: Rect, detail: &DetailView) {
    match detail {
        DetailView::Loading { title } => popup(
            frame,
            centered_size(64, 8, area),
            title,
            vec![
                Line::from(""),
                Line::from(Span::styled(
                    format!("{} Loading complete details…", spinner()),
                    Style::new().fg(WARN).bold(),
                )),
            ],
            0,
        ),
        DetailView::Software(payload) => detail_popup(
            frame,
            app,
            centered_size(82, 25, area),
            "SOFTWARE WHY",
            software_detail_lines(payload),
        ),
        DetailView::Doctor(result) => detail_popup(
            frame,
            app,
            centered_size(90, 28, area),
            "DOCTOR DETAILS",
            doctor_detail_lines(result),
        ),
        DetailView::Storage { report, row } => {
            let (scroll, maximum) =
                super::storage::render_storage_detail(frame, area, report, row, app.detail_scroll);
            app.detail_scroll = scroll;
            app.detail_scroll_max = maximum;
        }
        DetailView::Report { title, report, selected } => report_popup(
            frame,
            app,
            centered_size(100, 32, area),
            title,
            workflow_report_lines(report, selected.as_ref()),
        ),
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn plain(lines: &[Line]) -> String {
        lines
            .iter()
            .map(ToString::to_string)
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[test]
    fn ansi_is_removed_from_closure_diff() {
        assert_eq!(
            strip_ansi("envy: \u{1b}[31;1m16 KiB\u{1b}[0m"),
            "envy: 16 KiB"
        );
    }

    #[test]
    fn doctor_details_hide_empty_sections_and_offer_only_runnable_actions() {
        let mut result = json!({
            "status": "warn", "section": "apps", "name": "GitHub CLI auth",
            "message": "not authenticated", "hint": "",
            "details": {"command": "gh", "paths": ["/a", "/b"], "exit": {"code": 1}},
            "action": {"kind": "run", "argv": ["gh", "auth", "login"]}
        });
        let text = plain(&doctor_detail_lines(&result));
        assert!(!text.contains("Hint"));
        assert!(text.contains("command  gh"));
        assert!(text.contains("paths    /a, /b"));
        assert!(text.contains(r#"exit     {"code":1}"#));
        assert!(text.contains("Run gh auth login"));
        assert!(!text.contains("Press x"), "only allow-listed actions run");

        result["details"] = json!({});
        result["action"] = json!({"kind": "run", "argv": ["envy", "apply"]});
        let text = plain(&doctor_detail_lines(&result));
        assert!(!text.contains("Details"));
        assert!(text.contains("Run envy apply") && text.contains("Press x"));

        result["action"] = Value::Null;
        result["hint"] = json!("run gh auth login");
        let text = plain(&doctor_detail_lines(&result));
        assert!(text.contains("Hint\nrun gh auth login"));
        assert!(!text.contains("Suggested action"));
    }

    fn report(payload: Value) -> WorkflowReport {
        use std::os::unix::process::ExitStatusExt;
        WorkflowReport::from_result(Ok(crate::transport::JsonResponse {
            payload, status: std::process::ExitStatus::from_raw(0), stderr: "stage: freeze/build/compare".into(),
        }))
    }

    #[test]
    fn plan_producer_report_keeps_unknown_semantics_actions_provenance_and_complete_json() {
        let payload: Value = serde_json::from_str(include_str!("../workflow/fixtures/plan-v1.json")).unwrap();
        let text = plain(&workflow_report_lines(&report(payload.clone()), None));
        for evidence in ["No verified semantic baseline", "fixture-output", "frozenPath", "gitRevision",
            "dirtyFingerprint", "lockFingerprint", "writable-merge", "Runtime inspection was not requested",
            "rollbackBoundary", "stage: freeze/build/compare"] {
            assert!(text.contains(evidence), "missing {evidence}");
        }
        let raw = text.split_once("Complete original JSON\n").unwrap().1;
        assert_eq!(serde_json::from_str::<Value>(raw).unwrap(),payload);
        assert!(!text.contains("No closure differences reported."));
    }

    #[test]
    fn old_history_payload_without_baseline_cannot_be_presented_as_empty_diff() {
        let payload = json!({"before":{"number":1,"target":"/old"},"after":{"number":2,"target":"/new"},"closureDiff":null});
        let text = plain(&workflow_report_lines(&report(payload),None));
        assert!(text.contains("unknown — no backend semantic comparison"));
        assert!(text.contains("unknown — backend did not provide a closure comparison"));
        assert!(!text.contains("No closure differences"));
        let old_generation = json!({"number":1,"target":"/old"});
        let text = plain(&workflow_report_lines(&report(json!({"generations":[old_generation.clone()]})),Some(&old_generation)));
        assert!(text.contains("Semantic baseline: unknown"));
    }

    #[test]
    fn interrupted_receipt_keeps_stage_evidence_and_backend_disabled_retry_reason() {
        let entry = json!({"operationId":"fixture","result":"interrupted","terminal":null,
            "stages":[{"name":"activation","status":"started"}],"source":{"frozenPath":"/nix/store/frozen"},
            "retry":{"allowed":false,"step":"verify-generation","reason":"No verified generation target is recorded"}});
        let text = plain(&workflow_report_lines(&report(json!({"entries":[entry.clone()]})),Some(&entry)));
        assert!(text.contains("interrupted") && text.contains("activation") && text.contains("/nix/store/frozen"));
        assert!(text.contains("Retry disabled: No verified generation target is recorded"));
        assert!(!text.contains("Press x"));
    }


    #[test]
    fn complete_json_tail_is_reachable_beyond_u16_visual_rows() {
        let long = format!("{}END", "界".repeat(70_000));
        let (visible, maximum, scroll) = report_window(vec![Line::from(long)],2,4,usize::MAX);
        assert!(maximum > usize::from(u16::MAX));
        assert_eq!(scroll,maximum);
        assert!(visible.iter().map(ToString::to_string).collect::<String>().ends_with("END"));
        let (visible, maximum, _) = report_window(vec![Line::from("e\u{301}界")],1,4,0);
        assert_eq!(maximum,0);
        assert_eq!(visible.iter().map(ToString::to_string).collect::<String>(),"e\u{301}界");
    }

    #[test]
    fn carriage_return_progress_keeps_prior_stages_and_original_stderr() {
        let mut report = report(json!({"sections":{}}));
        report.stderr = "freeze complete\nbuild 10%\rbuild 100%\ncompare complete\n".into();
        let text = plain(&workflow_report_lines(&report,None));
        assert!(text.contains("freeze complete\nbuild 100%\ncompare complete"));
        assert!(text.contains("build 10%\\rbuild 100%"));
    }

}
