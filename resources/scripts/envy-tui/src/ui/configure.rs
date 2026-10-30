//! Configure pages: software, sources, search, mirrors, and settings.

use ratatui::{prelude::*, widgets::Paragraph};
use serde_json::Value;

use crate::{
    app::{App, InputMode},
    model::{
        filtered_software_entries, mirror_mode, mirror_target_rows, search_entries, text,
        SoftwareEntry,
    },
    theme::{self, ACCENT, ERROR, OK, WARN},
};

use super::table::{
    column, fitted, render_empty_state, render_input_field, render_table, Column, DataTable,
    TableCell,
};
use super::{row_offset, spinner};

const SETTINGS_COLUMNS: [Column; 3] = [
    column("TYPE", Constraint::Length(8)),
    column("SETTING", Constraint::Length(30)),
    column("VALUE", Constraint::Min(20)),
];

pub(super) fn render_settings(frame: &mut Frame, app: &App, area: Rect) {
    let settings = app.settings_rows();
    if settings.is_empty() {
        frame.render_widget(
            Paragraph::new(vec![
                Line::from(""),
                Line::from(Span::styled(
                    "  Loading host and config values…",
                    theme::muted(),
                )),
            ])
            .block(theme::panel()),
            area,
        );
        return;
    }
    let rows = settings
        .iter()
        .map(|row| {
            let kind = if row.key == crate::model::SettingKey::Host {
                TableCell::styled("host", Style::new().fg(WARN))
            } else if row.freeform() {
                TableCell::styled("text", theme::faint())
            } else {
                TableCell::styled("select", Style::new().fg(ACCENT))
            };
            let value = if row.value.is_empty() {
                TableCell::styled("—", theme::faint())
            } else {
                TableCell::new(row.value.clone())
            };
            vec![kind, TableCell::new(row.label.clone()), value]
        })
        .collect();
    render_table(
        frame,
        area,
        DataTable {
            title: "Settings · Enter edit".to_string(),
            position: Some(app.selection_position().unwrap_or((0, settings.len()))),
            columns: &SETTINGS_COLUMNS,
            rows,
            selected: app.settings_selected,
            scroll: app.scroll,
        },
    );
}

const SOURCE_COLUMNS: [Column; 6] = [
    column("SOURCE", Constraint::Min(26)),
    column("TYPE", Constraint::Length(10)),
    column("REGISTERED", Constraint::Length(12)),
    column("SELECTED", Constraint::Length(10)),
    column("READY", Constraint::Length(8)),
    column("OWNER", Constraint::Min(16)),
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
            title: "Sources · Enter select repository · s sync metadata".to_string(),
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
                "LOCAL FILTER · Enter keep · Esc clear · Ctrl-U reset"
            } else {
                "LOCAL FILTER · / edit · Esc clear"
            },
            &app.software_filter,
            filter_active,
            "type item, group, reference, version, or state",
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
                "No software policy entries",
                "The evaluated manifest contains no software items.",
                "Press r to refresh.",
            )
        } else {
            (
                "No matching software",
                "The local filter did not match any loaded policy entry.",
                "Press / to edit it or Esc to clear it.",
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
    column("SOURCE", Constraint::Length(12)),
    column("KIND", Constraint::Length(12)),
    column("NAME", Constraint::Length(26)),
    column("VERSION", Constraint::Length(12)),
    column("DESCRIPTION", Constraint::Min(24)),
    column("REFERENCE", Constraint::Length(34)),
];

pub(super) fn render_search(frame: &mut Frame, app: &App, area: Rect) {
    let sections = Layout::vertical([Constraint::Length(3), Constraint::Min(1)]).split(area);
    let search_active = app.input_mode == InputMode::Search;
    render_input_field(
        frame,
        sections[0],
        if search_active {
            "REGISTRY SEARCH · Enter submit · Esc cancel · Ctrl-U reset"
        } else {
            "REGISTRY SEARCH · / edit query"
        },
        &app.query,
        search_active,
        "search all configured providers",
    );
    let table_area = sections[1];
    let entries = search_entries(app.payload());
    if app.loading() && !app.current_has_content() {
        render_empty_state(
            frame,
            table_area,
            &format!("{} Searching registries", spinner()),
            "All configured providers remain enabled.",
            "Results will appear here without blocking navigation.",
        );
        return;
    }
    if app.submitted_query.is_empty() {
        render_empty_state(
            frame,
            table_area,
            "Find software across registries",
            "Search Homebrew, npm, PyPI, Cargo, Nix, and other configured providers.",
            "Press /, type a name, then press Enter.",
        );
        return;
    }
    if entries.is_empty() {
        render_empty_state(
            frame,
            table_area,
            "No registry matches",
            &format!(
                "No provider returned a result for ‘{}’.",
                app.submitted_query
            ),
            "Press / to refine the query or r to retry all providers.",
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
                TableCell::new(format!("{} · {}", entry.availability, entry.description)),
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
            "No mirror targets",
            "envY did not return any ecosystem targets for this machine.",
            "Press r to refresh or inspect `envy mirror targets`.",
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
                "Mirror targets · profile {} · Enter choose source",
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
