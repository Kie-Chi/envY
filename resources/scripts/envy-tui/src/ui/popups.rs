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
            Line::from(if changed { "Policy change" } else { "Already selected" }),
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
    if app.screen == crate::model::Screen::Config {
        let settings = app.settings_rows();
        if let Some(row) = settings.get(app.settings_selected) {
            let mut lines = vec![
                Line::styled(row.title(), theme::heading()),
                Line::styled(row.key.label(), theme::faint()),
                Line::from(""),
                Line::from(row.label.clone()),
                Line::from(format!("{} · {}{}", row.group, row.nix_type,
                    if row.required == Some(true) { " · required" } else { "" })),
                Line::from(format!("Applicability: {}", if row.condition_state.is_empty() { "—" } else { &row.condition_state })),
            ];
            if !row.change_kind.is_empty() { lines.push(Line::from(format!("Update: {}", row.change_kind))); }
            if !row.effect_hint().is_empty() { lines.push(Line::from(row.effect_hint().to_string())); }
            if !row.choices.is_empty() { lines.push(Line::from(format!("Options: {}", row.choices.join(" · ")))); }
            lines.extend([Line::from(""), Line::from("↵ edit   ↑↓ select   r refresh   ? / Esc close")]);
            popup(frame, centered_size(86, 20, area), "SETTING HELP", lines, 0);
        }
        return;
    }
    use crate::model::{HistoryView, Screen};
    let (title, keys): (&str, &[&str]) = match app.screen {
        Screen::Storage => ("STORAGE", &[
            "[ / ]       Inspection / Policy & records / Rule validation",
            "↵           selected item and full report",
            "p           add project directory",
            "x / X       remove last project / clear projects",
            "a           maintenance, plans and recovery",
            "r           refresh report",
            "", "Size: logical bytes · On disk: allocated estimate",
            "— unmeasured · rows may overlap",
            "Policy: may initialize and lock local state",
            "Validation: temporary organize config",
            "Quarantine occupies storage until purged.",
        ]),
        Screen::Software => ("SOFTWARE", &["↵ / Space   review toggle", "w / i       explain selection", "/           filter locally", "Esc         clear filter"]),
        Screen::Search => ("SEARCH", &["/           edit query", "↵           search / add selected result", "Ctrl-U      clear query"]),
        Screen::Sources => ("SOURCES", &["↵           select repository", "s           sync metadata"]),
        Screen::Mirror => ("MIRRORS", &["↵           choose source", "r           refresh measurements in chooser"]),
        Screen::Doctor => ("HEALTH", &["↵           details", "f           problems / all checks", "x           review suggested action"]),
        Screen::History if app.history_view == HistoryView::Operations => ("OPERATIONS", &["↵           receipt", "x           review available verification step"]),
        Screen::History => ("GENERATIONS", &["↵           details", "Space       mark generation", "d           compare with marked generation"]),
        _ => ("HOME", &["t           Storage", "2           Configure", "3           Activity"]),
    };
    let mut lines = keys.iter().map(|key| Line::from(key.to_string())).collect::<Vec<_>>();
    lines.extend([
        Line::from(""), Line::styled("Navigation", theme::heading()),
        Line::from("1..3 / Tab  section      v view"),
        Line::from("↑↓ select · PgUp/PgDn page · g/G ends"),
        Line::from(if app.screen == Screen::Sources { "r refresh   q quit" } else { "r refresh   s search   q quit" }),
        Line::from("? / Esc close help"),
    ]);
    popup(frame, centered_size(76, (lines.len() as u16 + 4).min(28), area), title, lines, 0);
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
                Line::from("↵ / Esc close"),
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

                Line::from("↵ / y run   Esc / n cancel"),
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
                    Span::raw(" saved"),
                ]),
                Line::from(""),
                Line::from("Apply to activate this change."),
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
                Line::from("↵ / Esc later"),
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
