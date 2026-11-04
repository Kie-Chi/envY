//! Group, setting, and mirror choosers with their confirmation dialogs.

use ratatui::{
    prelude::*,
    widgets::{Clear, Paragraph},
};
use serde_json::Value;

use crate::{
    app::App,
    model::text,
    theme::{self, ACCENT, ERROR, NOTE, OK, WARN},
};

use super::popups::{centered_size, dialog_block, inset_rect, popup};
use super::spinner;
use super::table::render_input_field;

pub(super) fn render_group_chooser(frame: &mut Frame, app: &App, area: Rect) {
    let Some(chooser) = &app.group_chooser else {
        return;
    };
    let mut lines = vec![
        Line::from(vec![
            Span::styled("ADD  ", Style::new().fg(OK).bold()),
            Span::styled(chooser.entry.name.clone(), theme::strong()),
        ]),
        Line::from(format!(
            "{} · {} · {}",
            chooser.entry.ecosystem, chooser.entry.kind, chooser.entry.reference
        )),
        Line::from(format!("Publisher    {}", chooser.entry.publisher)),
        Line::from(format!("Homepage     {}", chooser.entry.homepage)),
        Line::from(format!("Description  {}", chooser.entry.description)),
        Line::from(""),
        Line::from(Span::styled(
            "Choose a compatible managed group",
            theme::heading(),
        )),
    ];
    for (index, group) in chooser.groups.iter().enumerate() {
        lines.push(Line::from(vec![
            Span::styled(
                if index == chooser.selected {
                    "▸ "
                } else {
                    "  "
                },
                Style::new().fg(ACCENT),
            ),
            Span::styled(
                group.id.clone(),
                if index == chooser.selected {
                    theme::selection()
                } else {
                    Style::new()
                },
            ),
            Span::raw(format!("  {} · {}", group.scope, group.label)),
        ]));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(
        "Enter prepares a verified dry-run; Esc cancels.",
    ));
    popup(
        frame,
        centered_size(88, (chooser.groups.len() as u16 + 14).min(27), area),
        "SELECT SOFTWARE GROUP",
        lines,
        0,
    );
}

pub(super) fn render_setting_chooser(frame: &mut Frame, app: &App, area: Rect) {
    let Some(chooser) = &app.setting_chooser else {
        return;
    };
    let mut lines = vec![
        Line::from(vec![
            Span::styled("EDIT  ", theme::heading()),
            Span::styled(chooser.title.clone(), theme::strong()),
        ]),
        Line::from(format!("Current  {}", chooser.current)),
        Line::from(""),
        Line::from(Span::styled("Choose a value", theme::heading())),
    ];
    for (index, option) in chooser.options.iter().enumerate() {
        lines.push(Line::from(vec![
            Span::styled(
                if index == chooser.selected {
                    "▸ "
                } else {
                    "  "
                },
                Style::new().fg(ACCENT),
            ),
            Span::styled(
                option.clone(),
                if index == chooser.selected {
                    theme::selection()
                } else {
                    Style::new()
                },
            ),
        ]));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(
        "Enter confirms (then explicit apply); Esc cancels.",
    ));
    popup(
        frame,
        centered_size(72, (chooser.options.len() as u16 + 10).min(24), area),
        "SELECT VALUE",
        lines,
        0,
    );
}

pub(super) fn render_setting_edit(frame: &mut Frame, app: &App, area: Rect) {
    let Some(edit) = &app.setting_edit else {
        return;
    };
    let region = centered_size(72, 8, area);
    frame.render_widget(Clear, region);
    frame.render_widget(dialog_block(&format!("EDIT {}", edit.title)), region);
    let inner = inset_rect(region, 2, 1);
    let rows = Layout::vertical([Constraint::Length(3), Constraint::Min(1)]).split(inner);
    render_input_field(
        frame,
        rows[0],
        "VALUE · Enter save · Esc cancel · Ctrl-U clear",
        &edit.buffer,
        true,
        "type a new value",
    );
    frame.render_widget(
        Paragraph::new(Line::from(Span::styled(
            format!("Previous  {}", edit.previous),
            theme::muted(),
        ))),
        rows[1],
    );
}

pub(super) fn render_setting_confirm(frame: &mut Frame, app: &App, area: Rect) {
    let Some(pending) = &app.pending_setting else {
        return;
    };
    let previous = if pending.previous.is_empty() {
        "—".to_string()
    } else {
        pending.previous.clone()
    };
    popup(
        frame,
        centered_size(72, 13, area),
        "CONFIRM SETTING CHANGE",
        vec![
            Line::from(""),
            Line::from(Span::styled(
                pending.key.label(),
                Style::new().fg(WARN).bold(),
            )),
            Line::from(""),
            Line::from(format!("From  {previous}")),
            Line::from(vec![
                Span::raw("To    "),
                Span::styled(pending.value.clone(), Style::new().fg(OK).bold()),
            ]),
            Line::from(""),
            Line::from("This writes the selected machine file. Enter/y confirms; Esc/n cancels."),
        ],
        0,
    );
}

/// Return the inclusive-start/exclusive-end source range that keeps the chosen
/// source visible in a fixed-height mirror chooser. The chooser uses two lines
/// plus a spacer per source, so it cannot rely on the main page's one-row table
/// scroll state.
fn mirror_chooser_source_window(selected: usize, count: usize, visible: usize) -> (usize, usize) {
    if count == 0 {
        return (0, 0);
    }
    let visible = visible.max(1).min(count);
    let selected = selected.min(count - 1);
    let start = selected
        .saturating_sub(visible / 2)
        .min(count.saturating_sub(visible));
    (start, start + visible)
}

fn format_throughput(throughput_bps: u64) -> String {
    format!("{:.2} MB/s", throughput_bps as f64 / (1024.0 * 1024.0))
}

fn mirror_measurement_state(source: &crate::model::MirrorSource) -> String {
    let http = source
        .http_status
        .map(|status| format!(" · HTTP {status}"))
        .unwrap_or_default();
    let throughput = source
        .throughput_bps
        .map(|value| format!(" · {}", format_throughput(value)))
        .unwrap_or_default();
    match source.ok {
        Some(true) if source.measurement_stale => {
            format!("measurement cache · stale · ok{http}{throughput}")
        }
        Some(false) if source.measurement_stale => {
            format!("measurement cache · stale · failed{http}")
        }
        Some(true) => format!("measured · ok{http}{throughput}"),
        Some(false) => format!("measured · failed{http}"),
        None if source.stale => "source cache · stale".to_string(),
        None if source.fetched_at.is_some() => "source cache".to_string(),
        None if source.provider == "chsrc" => "chsrc · unmeasured".to_string(),
        None => "catalog · unmeasured".to_string(),
    }
}

fn mirror_measurement_style(source: &crate::model::MirrorSource) -> Style {
    match source.ok {
        Some(true) if source.measurement_stale => Style::new().fg(WARN),
        Some(true) => Style::new().fg(OK),
        Some(false) => Style::new().fg(ERROR),
        None if source.stale => Style::new().fg(WARN),
        _ => theme::muted(),
    }
}

pub(super) fn render_mirror_chooser(frame: &mut Frame, app: &App, area: Rect) {
    let Some(chooser) = &app.mirror_chooser else {
        return;
    };
    let requested_height = chooser
        .sources
        .len()
        .saturating_mul(3)
        .saturating_add(13)
        .min(u16::MAX as usize) as u16;
    let region = centered_size(110, requested_height.min(30), area);
    // `popup()` removes a one-cell block border plus one vertical inset on each
    // side. Nine remaining lines belong to the static header/footer.
    // Each source gets a summary line, a dim URL/diagnostic line, and a
    // one-line spacer. Keeping this accounting explicit prevents the footer
    // from being pushed out when the chooser has many discovered sources.
    let source_capacity = (region.height.saturating_sub(4).saturating_sub(9) as usize / 3).max(1);
    let (source_start, source_end) =
        mirror_chooser_source_window(chooser.selected, chooser.sources.len(), source_capacity);
    let mut lines = vec![
        Line::from(vec![
            Span::styled("MIRROR  ", theme::heading()),
            Span::styled(chooser.label.clone(), theme::strong()),
        ]),
        Line::from(vec![
            Span::styled("Target       ", theme::muted()),
            Span::styled(chooser.target.clone(), theme::strong()),
        ]),
        Line::from(vec![
            Span::styled("Profile      ", theme::muted()),
            Span::styled(chooser.profile.clone(), Style::new().fg(WARN).bold()),
        ]),
        Line::from(if app.mirror_measuring {
            format!(
                "Measurement  {} running in background (r forces refresh)",
                spinner()
            )
        } else {
            "Measurement  cached result or last completed run (r re-measures)".to_string()
        }),
        Line::from(""),
        Line::from(Span::styled(
            "Choose a source discovered by chsrc or the catalog fallback",
            theme::heading(),
        )),
        Line::from(format!(
            "Sources      {}–{} / {} · selected {} / {}",
            source_start.saturating_add(1),
            source_end,
            chooser.sources.len(),
            chooser.selected.saturating_add(1),
            chooser.sources.len(),
        )),
    ];
    for (index, source) in chooser
        .sources
        .iter()
        .enumerate()
        .skip(source_start)
        .take(source_end.saturating_sub(source_start))
    {
        let state = mirror_measurement_state(source);
        let state_style = mirror_measurement_style(source);
        let current = if source.current {
            Some(format!(
                "CURRENT {}",
                source.selection_origin.as_deref().unwrap_or("selection")
            ))
        } else {
            None
        };
        lines.push(Line::from(vec![
            Span::styled(
                if index == chooser.selected {
                    "▸ "
                } else {
                    "  "
                },
                Style::new().fg(ACCENT),
            ),
            Span::styled(
                format!("{:<16}", source.code),
                if index == chooser.selected {
                    theme::selection()
                } else {
                    theme::heading()
                },
            ),
            Span::styled(source.label.clone(), Style::new()),
            Span::raw("  "),
            Span::styled(state, state_style.bold()),
            current
                .map(|value| Span::styled(format!("  {value}"), Style::new().fg(NOTE).bold()))
                .unwrap_or_else(|| Span::raw("")),
        ]));
        let diagnostic = source
            .detail
            .as_deref()
            .map(|detail| format!(" · {detail}"))
            .unwrap_or_default();
        let diagnostic_style = if source.ok == Some(false) {
            Style::new().fg(ERROR)
        } else {
            theme::muted()
        };
        lines.push(Line::from(vec![
            Span::styled("      ", theme::muted()),
            Span::styled(source.url.clone(), Style::new()),
            Span::styled(diagnostic, diagnostic_style),
        ]));
        if index + 1 < source_end {
            lines.push(Line::from(""));
        }
    }
    lines.push(Line::from(""));
    lines.push(Line::from(
        "↑↓/j k select (loops) · PgUp/PgDn page · g/G ends · Enter preview · Esc cancel.",
    ));
    popup(frame, region, "SELECT MIRROR SOURCE", lines, 0);
}

pub(super) fn render_mirror_confirm(frame: &mut Frame, app: &App, area: Rect) {
    let Some(pending) = &app.pending_mirror else {
        return;
    };
    let plan = pending
        .payload
        .get("data")
        .and_then(|data| data.get("plan"))
        .unwrap_or(&Value::Null);
    let mut lines = vec![
        Line::from(vec![
            Span::styled("TARGET  ", theme::heading()),
            Span::styled(pending.target.clone(), theme::strong()),
        ]),
        Line::from(format!(
            "Source  {} ({})",
            pending.source.label, pending.source.code
        )),
        Line::from(format!("URL     {}", pending.source.url)),
        Line::from(format!("Provider {}", pending.source.provider)),
        Line::from(""),
        Line::from(Span::styled("Generated assignments", theme::heading())),
    ];
    let mut assignments = plan
        .get("after")
        .and_then(Value::as_object)
        .map(|values| {
            values
                .iter()
                .map(|(key, value)| format!("{key} = {}", text(Some(value))))
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    assignments.sort();
    if assignments.is_empty() {
        lines.push(Line::from(Span::styled(
            "No change; this source is already selected.",
            theme::muted(),
        )));
    } else {
        lines.extend(
            assignments
                .into_iter()
                .map(|assignment| Line::from(Span::styled(assignment, Style::new()))),
        );
    }
    lines.push(Line::from(""));
    lines.push(Line::from(
        "This writes the selected machine file's ENVY mirror block.",
    ));
    lines.push(Line::from("Enter/y confirms; Esc/n cancels."));
    popup(
        frame,
        centered_size(104, 20, area),
        "CONFIRM MIRROR OVERRIDE",
        lines,
        0,
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mirror_chooser_window_keeps_a_selected_late_source_visible() {
        assert_eq!(mirror_chooser_source_window(0, 12, 8), (0, 8));
        assert_eq!(mirror_chooser_source_window(7, 12, 8), (3, 11));
        assert_eq!(mirror_chooser_source_window(11, 12, 8), (4, 12));
    }

    #[test]
    fn mirror_throughput_is_displayed_in_megabytes_per_second() {
        assert_eq!(format_throughput(0), "0.00 MB/s");
        assert_eq!(format_throughput(1024 * 1024), "1.00 MB/s");
        assert_eq!(format_throughput(4_850_000), "4.63 MB/s");
    }
}
