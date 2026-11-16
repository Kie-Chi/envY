//! Dialogs and overlays drawn above the current page.

use ratatui::{
    prelude::*,
    widgets::{Block, Clear, Paragraph, Wrap},
};
use serde_json::Value;

use crate::{
    app::App,
    model::{SoftwareAction, WorkflowLog},
    theme::{self, ACCENT, ERROR, OK, WARN},
};

use super::choosers::{
    render_group_chooser, render_mirror_chooser, render_mirror_confirm, render_setting_chooser,
    render_setting_confirm, render_setting_edit,
};
use super::detail::render_detail;
use super::spinner;

pub(super) fn inset_rect(area: Rect, horizontal: u16, vertical: u16) -> Rect {
    let horizontal = horizontal.min(area.width / 2);
    let vertical = vertical.min(area.height / 2);
    Rect {
        x: area.x.saturating_add(horizontal),
        y: area.y.saturating_add(vertical),
        width: area.width.saturating_sub(horizontal.saturating_mul(2)),
        height: area.height.saturating_sub(vertical.saturating_mul(2)),
    }
}

pub(super) fn centered_size(width: u16, height: u16, area: Rect) -> Rect {
    let width = width.min(area.width.saturating_sub(4)).max(1);
    let height = height.min(area.height.saturating_sub(2)).max(1);
    Rect {
        x: area.x + area.width.saturating_sub(width) / 2,
        y: area.y + area.height.saturating_sub(height) / 2,
        width,
        height,
    }
}

/// Rounded accent frame shared by dialogs.
pub(super) fn dialog_block(title: &str) -> Block<'static> {
    theme::focus_panel().title(theme::title(title))
}

pub(super) fn popup(
    frame: &mut Frame,
    area: Rect,
    title: &str,
    lines: Vec<Line<'static>>,
    scroll: usize,
) {
    frame.render_widget(Clear, area);
    let block = dialog_block(title);
    let inner = inset_rect(block.inner(area), 2, 1);
    frame.render_widget(block, area);
    frame.render_widget(
        Paragraph::new(lines)
            .wrap(Wrap { trim: false })
            .scroll((scroll.min(u16::MAX as usize) as u16, 0)),
        inner,
    );
}

fn joined_strings(value: Option<&Value>) -> Option<String> {
    value
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join(", ")
        })
        .filter(|value| !value.is_empty())
}

fn render_mutation(frame: &mut Frame, app: &App, area: Rect) {
    if let Some(preview) = &app.pending_mutation {
        let plan = preview
            .payload
            .get("data")
            .and_then(|data| data.get("plan"))
            .unwrap_or(&Value::Null);
        let expected = plan.get("expected").unwrap_or(&Value::Null);
        let effective = expected
            .get("effective")
            .and_then(Value::as_bool)
            .map(|value| if value { "enabled" } else { "disabled" })
            .unwrap_or("unknown");
        let changed = plan
            .get("changed")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let blocked = plan.get("blocked").and_then(Value::as_str);
        let color = match preview.intent.action {
            SoftwareAction::Add => OK,
            SoftwareAction::Remove => WARN,
        };
        let mut lines = vec![
            Line::from(vec![
                Span::styled(
                    format!(" {} ", preview.intent.action.label().to_uppercase()),
                    theme::chip_style(color),
                ),
                Span::styled(format!("  {}", preview.intent.item), theme::strong()),
            ]),
            Line::from(""),
            Line::from(format!("Group       {}", preview.intent.group)),
            Line::from(format!("Operand     {}", preview.intent.operand)),
            Line::from(format!("Result      {effective}")),
            Line::from(format!("Changed     {changed}")),
            Line::from("Verified    dry-run JSON schema v1"),
            Line::from(""),
            Line::from(Span::styled("Policy operations", theme::heading())),
        ];
        let mut operation_count = 0;
        for (label, color, value) in [
            ("Add include", OK, plan.get("includeAdded")),
            ("Remove include", ERROR, plan.get("includeRemoved")),
            ("Add exclusion", WARN, plan.get("excludeAdded")),
            ("Remove exclusion", ACCENT, plan.get("excludeRemoved")),
        ] {
            if let Some(items) = joined_strings(value) {
                operation_count += 1;
                lines.push(Line::from(vec![
                    Span::styled(format!("{label:<18}"), Style::default().fg(color)),
                    Span::styled(items, theme::strong()),
                ]));
            }
        }
        if operation_count == 0 {
            lines.push(Line::from(Span::styled(
                "No policy edits are required.",
                theme::muted(),
            )));
        }
        if let Some(reason) = blocked {
            lines.push(Line::from(""));
            lines.push(Line::from(Span::styled(
                format!("Blocked: {reason}"),
                Style::new().fg(ERROR).bold(),
            )));
        } else {
            lines.push(Line::from(""));
            lines.push(Line::from(vec![
                Span::styled(" ENTER ", theme::chip_style(OK)),
                Span::raw(" Apply    "),
                Span::styled(" ESC ", theme::selection()),
                Span::raw(" Cancel"),
            ]));
        }
        popup(
            frame,
            centered_size(86, 22, area),
            "CONFIRM SOFTWARE CHANGE",
            lines,
            0,
        );
    } else if app.mutation_loading {
        popup(
            frame,
            centered_size(64, 8, area),
            "VERIFYING SOFTWARE CHANGE",
            vec![
                Line::from(""),
                Line::from(Span::styled(
                    format!("{} Preparing dry-run policy plan…", spinner()),
                    Style::new().fg(WARN).bold(),
                )),
            ],
            0,
        );
    }
}

fn render_help(frame: &mut Frame, app: &App, area: Rect) {
    if app.screen == crate::model::Screen::Storage {
        popup(
            frame,
            centered_size(92, 26, area),
            "STORAGE · READ REPORTS",
            vec![
                Line::from("[ / ]     Inspection / Policy & records / Rule validation"),
                Line::from("p         add one complete project directory (Inspection only)"),
                Line::from("x / X     remove last project / clear all explicit projects"),
                Line::from("Enter     full details and original backend JSON"),
                Line::from("a         explicit actions: save plan / preview / review / confirm"),
                Line::from("          native maintenance starts empty; init/check/gc are CLI handoffs"),
                Line::from("↑↓ / j k  select rows or scroll details; PgUp/PgDn, Home/End"),
                Line::from(
                    "r         request fresh data; cached reports never rescan on navigation",
                ),
                Line::from("s         search software; v cycles Activity; Tab changes section"),
                Line::from(""),
                Line::from("Inspection: known hotspots and explicit project directories only."),
                Line::from("Status: may initialize and lock local metadata."),
                Line::from("Validation: writes and removes temporary organize rule configuration."),
                Line::from(
                    "Logical / allocated / native / policy bytes are independent estimates.",
                ),
                Line::from("Rows overlap; unknown is not zero. Quarantine still occupies storage."),
                Line::from("Docker associations and application/project evidence remain in JSON."),
                Line::from(""),
                Line::from("? / Esc close help; Esc cancels input; q quits"),
            ],
            0,
        );
        return;
    }
    popup(
        frame,
        centered_size(80, 34, area),
        "KEYBOARD",
        vec![
            Line::from("1..3        jump to Home / Configure / Activity"),
            Line::from("Tab / ←→    switch top-level sections"),
            Line::from("v           cycle the current section's sub-view"),
            Line::from("↑↓ / j k    select rows; detail dialogs scroll"),
            Line::from("PgUp/PgDn   move by one viewport; g/G first/last"),
            Line::from("s           open Configure / Search from anywhere"),
            Line::from("r           refresh the current data view"),
            Line::from("Esc         cancel or clear context; q quits"),
            Line::from(""),
            Line::from(Span::styled("Home", theme::heading())),
            Line::from("Status and setup / repair guidance; t opens Storage reports"),
            Line::from(""),
            Line::from(Span::styled("Configure", theme::heading())),
            Line::from("Software / Search / Mirrors / Settings are secondary views"),
            Line::from("Settings Enter edits host or a config value"),
            Line::from("Enter/Space  preview availability toggle"),
            Line::from("w / i        explain selected software policy"),
            Line::from("/            local text filter (no backend request)"),
            Line::from("Ctrl-U       clear the current filter or query"),
            Line::from("Enter / a    choose group, then preview add"),
            Line::from("Mirror Enter  choose a target, then a chsrc/catalog source"),
            Line::from(""),
            Line::from(Span::styled("Activity", theme::heading())),
            Line::from("Health / Generations / Operations / Storage are secondary views"),
            Line::from("Health Enter opens details; x runs an allow-listed safe action"),
            Line::from("Health f shows only warnings and errors"),
            Line::from("v cycles Activity views; Space marks generations, d compares"),
            Line::from(""),
            Line::from("Every mutation: dry-run → contract validation → explicit confirmation."),
            Line::from("Press ? or Esc to close."),
        ],
        0,
    );
}

fn render_workflow_log(frame: &mut Frame, area: Rect, log: &mut WorkflowLog) {
    let region = centered_size(area.width, area.height, area);
    // Mirror popup()'s inset (1-cell border + 1-row vertical inset per side) so
    // the follow offset keeps the newest line pinned to the bottom.
    let viewport = region.height.saturating_sub(4).max(1) as usize;

    let status = match (log.running, log.success) {
        (true, _) => format!("{} running", spinner()),
        (false, Some(true)) => "done ✓".to_string(),
        (false, Some(false)) => "failed ✗".to_string(),
        (false, None) => "finished".to_string(),
    };
    let hint = if log.running && log.json {
        "↑↓ scroll · End follow · Esc cancel"
    } else if log.running {
        "↑↓ scroll · End follow"
    } else {
        "↑↓ scroll · Enter/Esc close"
    };
    let title = format!("{} · {} · {}", log.title.to_uppercase(), status, hint);

    let mut lines: Vec<Line<'static>> = log
        .lines
        .iter()
        .map(|line| Line::from(Span::styled(line.clone(), Style::new())))
        .collect();
    if let Some(report) = &log.report {
        lines = super::detail::workflow_report_lines(report, None);
    }
    if lines.is_empty() {
        lines.push(Line::from(Span::styled("Waiting for stderr stage progress…", theme::muted())));
    }
    let block = dialog_block(&title);
    let inner = inset_rect(block.inner(region), 2, 1);
    let requested = if log.follow { usize::MAX } else { log.scroll };
    let (visible, maximum, scroll) = super::detail::report_window(lines, inner.width, viewport as u16, requested);
    log.scroll_max = maximum;
    log.scroll = scroll;
    frame.render_widget(Clear, region);
    frame.render_widget(block, region);
    frame.render_widget(Paragraph::new(visible), inner);
}

pub(super) fn render_overlays(frame: &mut Frame, app: &mut App) {
    let area = frame.area();
    if let Some(log) = &mut app.workflow_log {
        render_workflow_log(frame, area, log);
        return;
    }
    if let Some(error) = &app.overlay_error {
        popup(
            frame,
            centered_size(78, 12, area),
            "REQUEST FAILED",
            vec![
                Line::from(""),
                Line::from(Span::styled(error.clone(), Style::new().fg(ERROR))),
                Line::from(""),
                Line::from("Enter or Esc closes this message; cached data remains available."),
            ],
            0,
        );
    } else if let Some(action) = &app.workflow_confirmation {
        popup(
            frame,
            centered_size(72, 12, area),
            "CONFIRM SUGGESTED ACTION",
            vec![
                Line::from(""),
                Line::from(Span::styled(action.title(), Style::new().fg(WARN).bold())),
                Line::from(""),
                Line::from(format!("Command  {}", action.display_command())),
                Line::from(""),
                Line::from(if action.is_json() { "The backend revalidates this fixed read-only step; its complete result stays visible." } else { "The TUI will pause and show the complete command output." }),
                Line::from(""),
                Line::from("Enter/y confirms; Esc/n cancels."),
            ],
            0,
        );
    } else if let Some(item) = &app.post_mutation {
        popup(
            frame,
            centered_size(78, 14, area),
            "SOFTWARE POLICY SAVED",
            vec![
                Line::from(""),
                Line::from(vec![
                    Span::styled(item.clone(), Style::new().fg(OK).bold()),
                    Span::raw(" was saved to the selected machine policy."),
                ]),
                Line::from(""),
                Line::from("Choose the next step; the policy remains pending until applied."),
                Line::from(""),
                Line::from(vec![
                    Span::styled(" p ", theme::chip_style(ACCENT)),
                    Span::raw(" Preview     "),
                    Span::styled(" a ", theme::chip_style(OK)),
                    Span::raw(" Apply     "),
                    Span::styled(" d ", theme::chip_style(WARN)),
                    Span::raw(" Doctor"),
                ]),
                Line::from(""),
                Line::from("Enter or Esc keeps the change pending and returns to envY."),
            ],
            0,
        );
    } else if app.pending_mirror.is_some() {
        render_mirror_confirm(frame, app, area);
    } else if app.mirror_chooser.is_some() {
        render_mirror_chooser(frame, app, area);
    } else if app.mirror_loading {
        popup(
            frame,
            centered_size(70, 8, area),
            "LOADING MIRROR SOURCES",
            vec![
                Line::from(""),
                Line::from(Span::styled(
                    format!("{} Querying envY cache and chsrc…", spinner()),
                    Style::new().fg(WARN).bold(),
                )),
            ],
            0,
        );
    } else if app.pending_setting.is_some() {
        render_setting_confirm(frame, app, area);
    } else if app.setting_chooser.is_some() {
        render_setting_chooser(frame, app, area);
    } else if app.setting_edit.is_some() {
        render_setting_edit(frame, app, area);
    } else if app.pending_mutation.is_some() || app.mutation_loading {
        render_mutation(frame, app, area);
    } else if app.group_chooser.is_some() {
        render_group_chooser(frame, app, area);
    } else if let Some(detail) = app.detail.clone() {
        render_detail(frame, app, area, &detail);
    } else if app.show_help {
        render_help(frame, app, area);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn popup_is_fixed_and_centered_on_tall_terminal() {
        assert_eq!(
            centered_size(94, 18, Rect::new(0, 0, 150, 60)),
            Rect::new(28, 21, 94, 18)
        );
    }
}
