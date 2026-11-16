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
        column, optional, render_empty_state, render_input_field, render_table, Column, DataTable,
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
                "Cached · saved {} · refresh to update",
                relative_age(saved, self.now)
            )
        } else if let Some(received) = self.received_at {
            let age = relative_age(received, self.now);
            if self.now.saturating_sub(received) >= 300 {
                format!("Stale · received {age} · r refresh")
            } else {
                format!("Received {age}")
            }
        } else {
            "Receipt time unknown".into()
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

const COLUMNS: [Column; 8] = [
    optional("TYPE", Constraint::Length(8), 110),
    column("ITEM", Constraint::Min(20)),
    column("STATUS", Constraint::Length(16)),
    optional("LOGICAL B", Constraint::Length(20), 90),
    optional("ALLOCATED B", Constraint::Length(20), 120),
    optional("NATIVE B", Constraint::Length(20), 150),
    optional("POLICY B", Constraint::Length(20), 180),
    optional("PATH / NOTE", Constraint::Min(24), 135),
];

fn status_style(row: &StorageRow) -> Style {
    match row.status.as_str() {
        "error" | "partial / failed" | "protocol error" => Style::new().fg(ERROR),
        "warning" | "partial" | "unknown" | "rejected" => Style::new().fg(WARN),
        _ => theme::muted(),
    }
}

/// The caller binds Enter to `render_storage_detail` for the selected row and
/// chooses when to load another view; this function never executes a command.
pub fn render_storage(frame: &mut Frame, area: Rect, page: StoragePage<'_>) {
    let error_height = if page.error.is_some() { 2 } else { 0 };
    let [header, scope, input, error_area, body] = Layout::vertical([
        Constraint::Length(3),
        Constraint::Length(1),
        Constraint::Length(if page.project_input.is_some() { 3 } else { 0 }),
        Constraint::Length(error_height),
        Constraint::Min(1),
    ])
    .areas(area);
    let mut tabs = Vec::new();
    for view in StorageView::ALL {
        if !tabs.is_empty() {
            tabs.push(Span::raw("  "));
        }
        tabs.push(if view == page.view {
            theme::chip(view.title(), ACCENT)
        } else {
            Span::styled(view.title(), theme::muted())
        });
    }
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(tabs),
            Line::from(Span::styled(page.view.description(), theme::muted())),
            Line::from(Span::styled(
                format!(
                    "{}{}",
                    page.timing.label(),
                    if page.loading { " · loading…" } else { "" }
                ),
                theme::muted(),
            )),
        ]),
        header,
    );
    let projects = if page.projects.is_empty() {
        "none (known hotspots only)".into()
    } else {
        serde_json::to_string(page.projects).unwrap_or_default()
    };
    frame.render_widget(
        Paragraph::new(format!(
            "Explicit projects: {projects} · p add · x remove last · X clear · a actions"
        )),
        scope,
    );
    if let Some(value) = page.project_input {
        render_input_field(
            frame,
            input,
            "PROJECT DIRECTORY · Enter add · Esc cancel · Ctrl-U reset",
            value,
            true,
            "one complete directory; spaces are part of the path",
        );
    }
    if let Some(error) = page.error {
        frame.render_widget(
            Paragraph::new(error)
                .style(Style::new().fg(ERROR))
                .wrap(Wrap { trim: false }),
            error_area,
        );
    }
    let Some(report) = page.report else {
        render_empty_state(
            frame,
            body,
            if page.loading {
                "Loading Storage report…"
            } else if page.error.is_some() {
                "Storage report unavailable"
            } else {
                "No Storage report loaded"
            },
            "Inspection reads known hotspots and explicitly selected project directories.",
            "Refresh to request a report.",
        );
        return;
    };
    if report.view != page.view {
        render_empty_state(
            frame,
            body,
            "Waiting for the selected Storage view",
            "The available report belongs to another view.",
            "Load the selected view to see its report.",
        );
        return;
    }
    let entries = report.rows();
    let selected = page.selected.min(entries.len().saturating_sub(1));
    let rows = entries
        .iter()
        .map(|row| {
            let path_note = if row.path == "unknown" {
                row.note.clone()
            } else if row.note.is_empty() {
                row.path.clone()
            } else {
                format!("{} · {}", row.path, row.note)
            };
            vec![
                TableCell::styled(row.kind.label(), theme::faint()),
                TableCell::new(row.label.clone()),
                TableCell::styled(row.status.clone(), status_style(row)),
                TableCell::new(bytes_text(row.measurements.logical)),
                TableCell::new(bytes_text(row.measurements.allocated)),
                TableCell::new(bytes_text(row.measurements.native_reclaimable)),
                TableCell::new(bytes_text(row.measurements.policy_eligible)),
                TableCell::new(path_note),
            ]
        })
        .collect();
    render_table(
        frame,
        body,
        DataTable {
            title: format!("Storage · {} · Enter details", page.view.title()),
            position: Some((selected + 1, entries.len())),
            columns: &COLUMNS,
            rows,
            selected,
            scroll: page.scroll,
        },
    );
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
        lines.push(Line::from(
            "These values are not additive or a promise of reclaimed space.",
        ));
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
    let [body, footer] = Layout::vertical([Constraint::Min(1), Constraint::Length(1)]).areas(area);
    let lines = actions.text().lines().map(|line| Line::from(line.to_string())).collect();
    let (visible, maximum, scroll) = super::detail::report_window(lines, body.width, body.height, actions.scroll);
    actions.scroll_max = maximum;
    actions.scroll = scroll;
    frame.render_widget(Paragraph::new(visible), body);
    frame.render_widget(Paragraph::new(format!("Storage actions · ↑↓ scroll {} / {} · q back · Esc cancel", scroll + 1, maximum + 1)), footer);
}
