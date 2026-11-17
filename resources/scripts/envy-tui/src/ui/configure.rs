//! Configure pages: software, sources, search, mirrors, and settings.

use ratatui::{prelude::*, widgets::{Paragraph, Wrap}};
use serde_json::Value;

use crate::{
    app::{App, InputMode},
    model::{
        filtered_software_entries, mirror_mode, mirror_target_rows, search_entries, text,
        SoftwareEntry,
    },
    theme::{self, ERROR, OK, WARN},
};

use super::table::{
    column, fitted, render_empty_state, render_input_field, render_table, Column, DataTable,
    TableCell,
};
use super::{row_offset, spinner};

pub(super) fn render_settings(frame: &mut Frame, app: &App, area: Rect) {
    let settings = app.settings_rows();
    let selected = app.settings_selected.min(settings.len().saturating_sub(1));
    let Some(row) = settings.get(selected) else { return; };
    let mut context = vec![Line::styled(row.key.label(), theme::faint())];
    let mut hints = Vec::new();
    if !row.effect_hint().is_empty() { hints.push(row.effect_hint()); }
    if row.condition_state == "unknown" && !row.effect_hint().contains("unknown") {
        hints.push("? Applicability unknown");
    } else if row.active == Some(false) && !row.effect_hint().contains("Inactive") {
        hints.push("Inactive");
    }
    if !hints.is_empty() { context.push(Line::styled(hints.join(" · "), theme::muted())); }
    let context = Paragraph::new(context).wrap(Wrap { trim: false });
    let help_height = (context.line_count(area.width.saturating_sub(2).max(1)) as u16 + 2)
        .min(5).min(area.height.saturating_sub(4) / 2);
    let [table_area, help_area] = Layout::vertical([
        Constraint::Min(4), Constraint::Length(help_height),
    ]).areas(area);
    frame.render_widget(context.block(theme::panel().title(" Selected setting ")), help_area);

    let visible = usize::from(table_area.height.saturating_sub(3)).max(1);
    let scroll = app.scroll.min(selected).max(selected.saturating_add(1).saturating_sub(visible));
    let columns = [
        super::table::optional("GROUP", Constraint::Length(14), 90),
        column("SETTING", Constraint::Fill(2)),
        column("VALUE", Constraint::Fill(1)),
    ];
    let offset = row_offset(scroll, settings.len(), table_area);
    let rows = settings.iter().enumerate().map(|(index, row)| {
        let section_starts = index == 0 || index == offset || settings[index - 1].section() != row.section();
        let label = if row.secret {
            TableCell::styled(format!("◆ {}", row.title()), theme::muted())
        } else {
            TableCell::new(row.title())
        };
        let value = if row.value.is_empty() {
            TableCell::styled("—", theme::faint())
        } else if row.secret {
            TableCell::styled(row.value.clone(), if row.value == "set" { Style::new().fg(OK) } else { theme::muted() })
        } else {
            TableCell::new(row.value.clone())
        };
        vec![TableCell::styled(if section_starts { row.section() } else { "" }, theme::faint()), label, value]
    }).collect();
    render_table(frame, table_area, DataTable {
        title: "Settings".into(),
        position: Some((selected + 1, settings.len())),
        columns: &columns,
        rows, selected, scroll,
    });
}

const SOURCE_COLUMNS: [Column; 6] = [
    column("SOURCE", Constraint::Fill(2)),
    super::table::optional("TYPE", Constraint::Length(10), 100),
    column("REGISTERED", Constraint::Length(11)),
    column("SELECTED", Constraint::Length(9)),
    column("READY", Constraint::Length(7)),
    super::table::optional("OWNER", Constraint::Fill(1), 130),
];

/// Booleans read as yes/no; other values are shown as returned.
fn flag_cell(value: Option<&Value>) -> TableCell {
    match value.and_then(Value::as_bool) {
        Some(true) => TableCell::styled("yes", Style::new().fg(OK)),
        Some(false) => TableCell::styled("no", theme::faint()),
        None => TableCell::new(text(value)),
    }
}

pub(super) fn render_sources(frame: &mut Frame, app: &App, area: Rect) {
    let rows = app
        .payload()
        .and_then(|value| value.get("sources"))
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .map(|source| {
            vec![
                TableCell::new(text(source.get("id"))),
                TableCell::new(text(source.get("type"))),
                flag_cell(source.get("registered")),
                flag_cell(source.get("selected")),
                flag_cell(source.get("ready")),
                TableCell::new(text(source.get("owner"))),
            ]
        })
        .collect();
    render_table(
        frame,
        area,
        DataTable {
            title: "Sources".into(),
            position: app.selection_position(),
            columns: &SOURCE_COLUMNS,
            rows,
            selected: app.sources_selected,
            scroll: app.scroll,
        },
    );
}

/// Software columns sized to the given entries; version and source give way
/// on narrow terminals.
fn software_columns(entries: &[SoftwareEntry]) -> [Column; 6] {
    let values = |field: fn(&SoftwareEntry) -> &str| entries.iter().map(field);
    [
        fitted("GROUP", values(|entry| &entry.group), 26, 0),
        fitted("ITEM", values(|entry| &entry.item), 26, 0),
        fitted("VERSION", values(|entry| &entry.version), 14, 100),
        fitted("STATE", values(|entry| &entry.state), 11, 0),
        fitted("SOURCE", values(|entry| &entry.source_id), 18, 120),
        column("REFERENCE", Constraint::Min(20)),
    ]
}

pub(super) fn render_software(frame: &mut Frame, app: &App, area: Rect) {
    let table_area = if app.input_bar_height() > 0 {
        let sections = Layout::vertical([Constraint::Length(3), Constraint::Min(1)]).split(area);
        let filter_active = app.input_mode == InputMode::SoftwareFilter;
        render_input_field(
            frame,
            sections[0],
            if filter_active {
                "FILTER · ↵ keep · Esc clear"
            } else {
                "FILTER · / edit · Esc clear"
            },
            &app.software_filter,
            filter_active,
            "name, group, or source",
        );
        sections[1]
    } else {
        area
    };
    let entries = app.software_entries();
    let total = crate::model::software_entries(app.payload()).len();
    if entries.is_empty() && !app.loading() {
        let (title, message, action) = if app.software_filter.is_empty() {
            (
                "No software selected", "", "r refresh",
            )
        } else {
            (
                "No matches", "", "/ edit filter · Esc clear",
            )
        };
        render_empty_state(frame, table_area, title, message, action);
        return;
    }
    // Name each group once per run of rows, and again at the top of the view.
    let offset = row_offset(app.scroll, entries.len(), table_area);
    let rows = entries
        .iter()
        .enumerate()
        .map(|(index, entry)| {
            let state_style = match entry.state.as_str() {
                "effective" => Style::new().fg(OK),
                "blocked" => Style::new().fg(ERROR),
                "stale" => Style::new().fg(WARN),
                _ => theme::faint(),
            };
            let group_starts =
                index == 0 || index == offset || entries[index - 1].group != entry.group;
            vec![
                TableCell::new(if group_starts {
                    entry.group.as_str()
                } else {
                    ""
                }),
                TableCell::new(entry.item.clone()),
                TableCell::new(entry.version.clone()),
                TableCell::styled(entry.state.clone(), state_style),
                TableCell::styled(entry.source_id.clone(), theme::faint()),
                TableCell::new(entry.reference.clone()),
            ]
        })
        .collect();
    // Size columns from every entry so filtering does not move them.
    let columns = software_columns(&filtered_software_entries(app.payload(), ""));
    let title = if app.software_filter.is_empty() {
        format!("Software policy · {total} items")
    } else {
        format!("Software policy · {} / {total} visible", entries.len())
    };
    render_table(
        frame,
        table_area,
        DataTable {
            title,
            position: app.selection_position(),
            columns: &columns,
            rows,
            selected: app.software_selected,
            scroll: app.scroll,
        },
    );
}

const SEARCH_COLUMNS: [Column; 6] = [
    super::table::optional("SOURCE", Constraint::Length(18), 90),
    super::table::optional("KIND", Constraint::Length(10), 110),
    column("NAME", Constraint::Fill(2)),
    super::table::optional("VERSION", Constraint::Length(12), 100),
    super::table::optional("DESCRIPTION", Constraint::Fill(3), 70),
    super::table::optional("REFERENCE", Constraint::Fill(2), 160),
];

pub(super) fn render_search(frame: &mut Frame, app: &App, area: Rect) {
    let sections = Layout::vertical([Constraint::Length(3), Constraint::Min(1)]).split(area);
    let search_active = app.input_mode == InputMode::Search;
    render_input_field(
        frame,
        sections[0],
        if search_active {
            "SEARCH · ↵ search · Esc cancel"
        } else {
            "SEARCH · / edit"
        },
        &app.query,
        search_active,
        "package or app name",
    );
    let table_area = sections[1];
    let entries = search_entries(app.payload());
    if app.loading() && !app.current_has_content() {
        render_empty_state(
            frame,
            table_area,
            &format!("{} Searching…", spinner()), "", "",
        );
        return;
    }
    if app.submitted_query.is_empty() {
        render_empty_state(
            frame,
            table_area,
            "Find software", "", "/ type a name · ↵ search",
        );
        return;
    }
    if entries.is_empty() {
        render_empty_state(
            frame,
            table_area,
            "No results", &app.submitted_query, "/ change query · r retry",
        );
        return;
    }
    let rows = entries
        .iter()
        .map(|entry| {
            vec![
                TableCell::new(
                    entry
                        .source_id
                        .clone()
                        .unwrap_or_else(|| entry.source.clone()),
                ),
                TableCell::new(entry.kind.clone()),
                TableCell::new(entry.name.clone()),
                TableCell::new(entry.version.clone()),
                TableCell::new(entry.description.clone()).note(
                    if entry.availability == "managed" { String::new() }
                    else { format!(" · {}", entry.availability) }),
                TableCell::new(entry.reference.clone()),
            ]
        })
        .collect();
    render_table(
        frame,
        table_area,
        DataTable {
            title: format!("Results for ‘{}’", app.submitted_query),
            position: Some(app.selection_position().unwrap_or((0, entries.len()))),
            columns: &SEARCH_COLUMNS,
            rows,
            selected: app.search_selected,
            scroll: app.scroll,
        },
    );
}

const MIRROR_COLUMNS: [Column; 2] = [
    column("TARGET", Constraint::Length(18)),
    column("EFFECTIVE SOURCE", Constraint::Min(30)),
];

pub(super) fn render_mirror(frame: &mut Frame, app: &App, area: Rect) {
    let entries = mirror_target_rows(app.payload());
    let count = entries.len();
    if count == 0 && !app.loading() {
        render_empty_state(
            frame,
            area,
            "No mirror targets", "", "r refresh",
        );
        return;
    }
    let rows = entries
        .iter()
        .map(|entry| {
            vec![
                TableCell::styled(entry.key.clone(), theme::heading()),
                TableCell::new(entry.value.clone()),
            ]
        })
        .collect();
    render_table(
        frame,
        area,
        DataTable {
            title: format!(
                "Mirrors · {}",
                mirror_mode(app.payload())
            ),
            position: Some(app.selection_position().unwrap_or((0, count))),
            columns: &MIRROR_COLUMNS,
            rows,
            selected: app.mirror_selected,
            scroll: app.scroll,
        },
    );
}
