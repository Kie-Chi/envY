//! Detail popups for software items, Doctor results, and generation diffs.

use ratatui::{
    prelude::*,
    widgets::{Clear, Paragraph, Wrap},
};
use serde_json::Value;

use crate::{
    app::App,
    model::{text, DetailView},
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

fn history_diff_lines(payload: &Value) -> Vec<Line<'static>> {
    let before = payload.get("before").unwrap_or(&Value::Null);
    let after = payload.get("after").unwrap_or(&Value::Null);
    let diff = payload
        .get("closureDiff")
        .and_then(Value::as_str)
        .map(strip_ansi)
        .unwrap_or_else(|| "No closure changes reported.".to_string());
    let mut lines = vec![
        Line::from(vec![
            Span::styled(text(before.get("number")), Style::new().fg(WARN).bold()),
            Span::raw("  →  "),
            Span::styled(text(after.get("number")), Style::new().fg(OK).bold()),
        ]),
        Line::from(""),
        Line::from(Span::styled("Before", theme::heading())),
        Line::from(format!("Created  {}", text(before.get("createdAt")))),
        Line::from(format!("Profile  {}", text(before.get("path")))),
        Line::from(format!("Target   {}", text(before.get("target")))),
        Line::from(""),
        Line::from(Span::styled("After", theme::heading())),
        Line::from(format!("Created  {}", text(after.get("createdAt")))),
        Line::from(format!("Profile  {}", text(after.get("path")))),
        Line::from(format!("Target   {}", text(after.get("target")))),
        Line::from(""),
        Line::from(Span::styled("Closure diff", theme::heading())),
    ];
    lines.extend(diff.lines().map(|line| Line::from(line.to_string())));
    lines
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
        DetailView::HistoryDiff(payload) => detail_popup(
            frame,
            app,
            centered_size(100, 28, area),
            "GENERATION DIFF",
            history_diff_lines(payload),
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
}
