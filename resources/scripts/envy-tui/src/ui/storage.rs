//! Standalone Storage rendering; page ownership and key routing live in App.

use ratatui::{
    prelude::*,
    widgets::{Clear, Paragraph, Wrap},
};

use crate::{
    model::relative_age,
    storage::{bytes_text, StorageReport, StorageRow, StorageRowKind, StorageView},
    theme::{self, ACCENT, ERROR, WARN},
};

use super::{
    popups::{centered_size, dialog_block, inset_rect},
    table::{
        column, optional, render_empty_state, render_input_field, render_table, DataTable,
        TableCell,
    },
};

/// Receipt time belongs to PageState. A restored report displays its saved time
/// until a new response arrives; drawing never manufactures a freshness time.
#[derive(Clone, Copy, Debug, Default)]
pub struct StorageTiming {
    pub received_at: Option<i64>,
    pub saved_at: Option<i64>,
    pub now: i64,
}

impl StorageTiming {
    pub fn label(self) -> String {
        if let Some(saved) = self.saved_at {
            format!(
                "Cached · {}",
                relative_age(saved, self.now)
            )
        } else if let Some(received) = self.received_at {
            let age = relative_age(received, self.now);
            if self.now.saturating_sub(received) >= 300 {
                format!("{age} · stale")
            } else {
                format!("Updated {age}")
            }
        } else {
            "Not refreshed".into()
        }
    }
}

pub struct StoragePage<'a> {
    pub view: StorageView,
    pub report: Option<&'a StorageReport>,
    pub selected: usize,
    pub scroll: usize,
    pub loading: bool,
    /// A transport failure may coexist with a previously received report.
    pub error: Option<&'a str>,
    pub timing: StorageTiming,
    pub projects: &'a [String],
    pub project_input: Option<&'a str>,
}

fn row_note(report: &StorageReport, row: &StorageRow) -> String {
    let value = report.value(row).unwrap_or(&serde_json::Value::Null);
    let issues = ["warnings", "errors", "reasons"].into_iter()
        .filter_map(|key| value.get(key).and_then(serde_json::Value::as_array))
        .flatten().map(|value| value.as_str().map(str::to_string).unwrap_or_else(|| value.to_string()))
        .collect::<Vec<_>>().join(" · ");
    let detail = if !issues.is_empty() { issues }
        else if row.path == "unknown" || matches!(row.status.as_str(), "unknown" | "partial" | "error") { row.note.clone() }
        else { String::new() };
    if row.kind == StorageRowKind::Record && !value.is_object() && !value.is_array() {
        return match value { serde_json::Value::Null => "—".into(), _ => row.note.clone() };
    }
    let path = if row.path == "unknown" { "" } else { &row.path };
    match (path.is_empty(), detail.is_empty()) {
        (false, false) => format!("{path} · {detail}"),
        (false, true) => path.into(),
        _ => detail,
    }
}

fn row_status(report: &StorageReport, row: &StorageRow) -> (String, Style) {
    let value = report.value(row);
    let note = row_note(report, row);
    let (label, color) = match row.status.as_str() {
        "error" | "partial / failed" | "protocol error" => ("! Failed", ERROR),
        "warning" | "partial" | "complete" | "ok" | "absent" | "missing" | "skipped" => return (theme::status_label(&row.status), Style::new().fg(theme::status_color(&row.status))),
        "unknown" if note.contains("permission_denied") || note.contains("Permission denied") => ("! No access", WARN),
        "unknown" if value.and_then(|v| v.get("status")).is_none() => ("—", theme::MUTED),
        "unknown" if row.kind == StorageRowKind::Filesystem => ("? Not measured", WARN),
        "unknown" => ("! Unavailable", WARN),

        _ => return (row.status.clone(), theme::muted()),
    };
    (label.into(), Style::new().fg(color))
}

/// Human-readable list values; exact integers remain in row details.
fn capacity(value: Option<u64>) -> String {
    let Some(value) = value else { return "—".into(); };
    const UNITS: [&str; 7] = ["B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"];
    let mut unit = 0;
    let mut divisor = 1_u64;
    while value / divisor >= 1024 && unit + 1 < UNITS.len() {
        divisor *= 1024;
        unit += 1;
    }
    if unit == 0 { format!("{value} B") }
    else { format!("{:.1} {}", value as f64 / divisor as f64, UNITS[unit]) }
}

/// Keep every report row addressable; only its presentation varies by view.
pub fn render_storage(frame: &mut Frame, area: Rect, page: StoragePage<'_>) {
    let error_height = if page.error.is_some() { 2 } else { 0 };
    let [header, scope, input, error_area, body] = Layout::vertical([
        Constraint::Length(1), Constraint::Length(1),
        Constraint::Length(if page.project_input.is_some() { 3 } else { 0 }),
        Constraint::Length(error_height), Constraint::Min(1),
    ]).areas(area);
    let mut tabs = Vec::new();
    for view in StorageView::ALL {
        if !tabs.is_empty() { tabs.push(Span::raw("  ")); }
        tabs.push(if view == page.view { theme::chip(view.title(), ACCENT) }
                  else { Span::styled(view.title(), theme::muted()) });
    }
    frame.render_widget(Paragraph::new(Line::from(tabs)), header);
    let projects = if page.projects.is_empty() { "Projects: none".into() }
        else { format!("Projects: {} · {}", page.projects.len(), page.projects.last().unwrap()) };
    frame.render_widget(Paragraph::new(Line::from(vec![
        Span::styled(format!("{}{}", page.timing.label(), if page.loading { " · refreshing…" } else { "" }), theme::muted()),
        Span::styled(if page.view == StorageView::Inspect { format!("   {projects}") } else { String::new() }, theme::faint()),
    ])), scope);
    if let Some(value) = page.project_input {
        render_input_field(frame, input, "PROJECT · ↵ add · Esc cancel", value, true, "directory path");
    }
    if let Some(error) = page.error {
        frame.render_widget(Paragraph::new(error).style(Style::new().fg(ERROR)).wrap(Wrap { trim: false }), error_area);
    }
    let Some(report) = page.report else {
        render_empty_state(frame, body,
            if page.loading { "Loading Storage…" } else if page.error.is_some() { "Storage unavailable" } else { "No Storage report" },
            "", "r refresh");
        return;
    };
    if report.view != page.view {
        render_empty_state(frame, body, "Loading selected view…", "", "");
        return;
    }
    let entries = report.rows();
    let selected = page.selected.min(entries.len().saturating_sub(1));
    let inspect = page.view == StorageView::Inspect;
    let allocated = inspect && entries.iter().any(|row| row.measurements.allocated.is_some());
    let native = inspect && entries.iter().any(|row| row.measurements.native_reclaimable.is_some());
    let policy = inspect && entries.iter().any(|row| row.measurements.policy_eligible.is_some());
    let mut columns = vec![
        optional("TYPE", Constraint::Length(9), 120),
        column("ITEM", Constraint::Fill(2)),
        column("STATE", Constraint::Length(16)),
    ];
    if inspect { columns.push(column("SIZE", Constraint::Length(12))); }
    if allocated { columns.push(optional("ON DISK", Constraint::Length(12), 110)); }
    if native { columns.push(optional("RECLAIMABLE", Constraint::Length(12), 145)); }
    if policy { columns.push(optional("CANDIDATE", Constraint::Length(12), 175)); }
    columns.push(optional("DETAIL", Constraint::Fill(3), if inspect { 90 } else { 0 }));
    let rows = entries.iter().map(|row| {
        let (status, style) = row_status(report, row);
        let mut cells = vec![
            TableCell::styled(row.kind.label(), theme::faint()),
            TableCell::new(row.label.clone()), TableCell::styled(status, style),
        ];
        let physical = matches!(row.kind, StorageRowKind::Filesystem | StorageRowKind::DockerGroup | StorageRowKind::DockerObject)
            || [row.measurements.logical, row.measurements.allocated, row.measurements.native_reclaimable, row.measurements.policy_eligible].iter().any(Option::is_some);
        let metric = |value, relevant| TableCell::styled(if relevant { capacity(value) } else { String::new() }, theme::muted());
        if inspect { cells.push(metric(row.measurements.logical, physical)); }
        if allocated { cells.push(metric(row.measurements.allocated, physical)); }
        if native { cells.push(metric(row.measurements.native_reclaimable, row.measurements.native_reclaimable.is_some() || matches!(row.kind, StorageRowKind::DockerGroup | StorageRowKind::DockerObject))); }
        if policy { cells.push(metric(row.measurements.policy_eligible, physical)); }
        cells.push(TableCell::styled(row_note(report, row), theme::faint()));
        cells
    }).collect();
    render_table(frame, body, DataTable {
        title: "Storage".into(), position: Some((selected + 1, entries.len())),
        columns: &columns, rows, selected, scroll: page.scroll,
    });
}

pub fn storage_detail_lines(report: &StorageReport, row: &StorageRow) -> Vec<Line<'static>> {
    let mut lines = vec![
        Line::from(Span::styled(row.label.clone(), theme::heading())),
        Line::from(""),
    ];
    if row.kind == StorageRowKind::Filesystem
        || row.kind == StorageRowKind::DockerGroup
        || row.kind == StorageRowKind::DockerObject
    {
        for (label, bytes) in [
            ("Logical bytes", row.measurements.logical),
            ("Allocated bytes (estimate)", row.measurements.allocated),
            (
                "Native reclaimable bytes",
                row.measurements.native_reclaimable,
            ),
            (
                "Policy eligible bytes (estimate)",
                row.measurements.policy_eligible,
            ),
        ] {
            lines.push(Line::from(format!("{label}: {}", bytes_text(bytes))));
        }
        lines.push(Line::styled("Overlapping estimates · do not add together.", theme::muted()));
        lines.push(Line::from(""));
    }
    lines.extend(
        report
            .detail_text(row)
            .lines()
            .map(|line| Line::from(line.to_string())),
    );
    lines
}

/// Returns the clamped scroll offset and maximum for the caller's detail state.
/// Complete JSON is wrapped, including long paths and nested evidence.
pub fn render_storage_detail(
    frame: &mut Frame,
    area: Rect,
    report: &StorageReport,
    row: &StorageRow,
    scroll: usize,
) -> (usize, usize) {
    let area = centered_size(110, 34, area);
    frame.render_widget(Clear, area);
    let block = dialog_block("STORAGE DETAILS");
    let inner = inset_rect(block.inner(area), 2, 1);
    frame.render_widget(block, area);
    let [body, footer] = Layout::vertical([Constraint::Min(1), Constraint::Length(1)]).areas(inner);
    let paragraph = Paragraph::new(storage_detail_lines(report, row)).wrap(Wrap { trim: false });
    let maximum = paragraph
        .line_count(body.width.max(1))
        .saturating_sub(body.height as usize);
    let scroll = scroll.min(maximum).min(u16::MAX as usize);
    frame.render_widget(paragraph.scroll((scroll as u16, 0)), body);
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            theme::key("Enter / Esc"),
            Span::raw(" close  "),
            theme::key("↑↓"),
            Span::raw(format!(" scroll  {} / {}", scroll + 1, maximum + 1)),
        ])),
        footer,
    );
    (scroll, maximum)
}

#[cfg(test)]
mod tests;

/// Reuse the visual-row window so Unicode, unbroken paths and >65535 rows
/// remain reachable in operation evidence and complete backend envelopes.
pub fn render_actions(frame: &mut Frame, area: Rect, actions: &mut crate::storage::actions::Actions) {
    use crate::storage::actions::{Operation, Stage};
    let [header, body, footer] = Layout::vertical([
        Constraint::Length(1), Constraint::Min(1), Constraint::Length(1),
    ]).areas(area);
    let mut tabs = Vec::new();
    for (operation, title) in [(Operation::Maintain, "Maintenance"), (Operation::Run, "Run plan"), (Operation::Purge, "Purge"), (Operation::Restore, "Restore")] {
        tabs.push(if operation == actions.operation { theme::chip(title, ACCENT) } else { Span::styled(format!(" {title} "), theme::muted()) });
    }
    frame.render_widget(Paragraph::new(Line::from(tabs)), header);
    let detailed = actions.details || actions.handoff.is_some();
    let content = if detailed { actions.text() } else { actions.summary_text() };
    let lines = content.lines().map(|line| Line::styled(line.to_string(),
        if line.starts_with('!') { Style::new().fg(ERROR) } else { Style::default() })).collect();
    let title = if actions.handoff.is_some() { "Run in terminal" } else if actions.details { "Details" }
        else { match actions.stage { Stage::Review => "Preview", Stage::Confirm => "Confirm", Stage::Busy => "Working", Stage::Result => "Result", _ => "Storage actions" } };
    let block = theme::panel().title(theme::title(title));
    let inner = if body.width >= 4 && body.height >= 4 {
        let inner = block.inner(body);
        frame.render_widget(block, body);
        inner
    } else { body };
    let (visible, maximum, scroll) = super::detail::report_window(lines, inner.width, inner.height, actions.scroll);
    actions.scroll_max = maximum;
    actions.scroll = scroll;
    frame.render_widget(Paragraph::new(visible), inner);
    let keys = if actions.handoff.is_some() { "h next · Esc back · q close" }
        else if actions.field.is_some() { "↵ keep · Esc cancel · Ctrl-U clear" }
        else if actions.details { "d summary · Esc back · ↑↓ scroll" }
        else { match actions.stage {
            Stage::Review => "↵ confirm · d details · Esc cancel",
            Stage::Confirm => "y execute · Esc cancel · d details",
            Stage::Busy => "Esc stop · d details",
            _ => "Tab operation · p preview · s save plan · d details · h terminal",
        } };
    frame.render_widget(Paragraph::new(Line::from(super::hint_spans(keys))), footer);
}
