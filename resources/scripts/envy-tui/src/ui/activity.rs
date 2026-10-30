//! Activity pages: Doctor results, generations, and the operation journal.

use ratatui::prelude::*;
use serde_json::Value;

use crate::{
    app::App,
    model::{
        doctor_results, journal_entries, parse_timestamp, relative_age, short_timestamp, text,
        HistoryView,
    },
    theme::{self, ERROR, OK, WARN},
};

use super::table::{
    column, optional, render_empty_state, render_table, Column, DataTable, TableCell,
};

const DOCTOR_COLUMNS: [Column; 4] = [
    column("STATUS", Constraint::Length(9)),
    optional("SECTION", Constraint::Length(8), 100),
    column("CHECK", Constraint::Length(30)),
    column("RESULT", Constraint::Min(30)),
];

pub(super) fn render_doctor(frame: &mut Frame, app: &App, area: Rect) {
    let results = doctor_results(app.payload(), app.doctor_problems_only);
    let count = results.len();
    if count == 0 && !app.loading() {
        let (title, message, action) = if app.doctor_problems_only {
            (
                "No warnings or errors",
                "Every check passed or is informational.",
                "Press f to show all checks.",
            )
        } else {
            (
                "No doctor results",
                "envY did not return any checks for this machine.",
                "Press r to run Doctor again.",
            )
        };
        render_empty_state(frame, area, title, message, action);
        return;
    }
    let rows = results
        .into_iter()
        .map(|result| {
            let status = text(result.get("status"));
            vec![
                TableCell::styled(
                    format!("● {status}"),
                    Style::new().fg(theme::status_color(&status)),
                ),
                TableCell::new(text(result.get("section"))),
                TableCell::new(text(result.get("name"))),
                TableCell::new(text(result.get("message"))),
            ]
        })
        .collect();
    render_table(
        frame,
        area,
        DataTable {
            title: if app.doctor_problems_only {
                "Doctor · warnings and errors".to_string()
            } else {
                "Doctor · problems first".to_string()
            },
            position: Some(app.selection_position().unwrap_or((0, count))),
            columns: &DOCTOR_COLUMNS,
            rows,
            selected: app.doctor_selected,
            scroll: app.scroll,
        },
    );
}

pub(super) fn render_history(frame: &mut Frame, app: &App, area: Rect) {
    match app.history_view {
        HistoryView::Generations => render_generations(frame, app, area),
        HistoryView::Operations => render_journal(frame, app, area),
    }
}

const GENERATION_COLUMNS: [Column; 5] = [
    column("GENERATION", Constraint::Length(10)),
    column("STATE", Constraint::Length(16)),
    column("CREATED", Constraint::Length(24)),
    optional("PROFILE", Constraint::Length(38), 150),
    column("STORE TARGET", Constraint::Min(30)),
];

/// Local time of a timestamp with its age as a dimmed note.
fn time_cell(timestamp: &str, now: i64) -> TableCell {
    let cell = TableCell::new(short_timestamp(timestamp));
    match parse_timestamp(timestamp) {
        Some(then) => cell.note(format!("  {}", relative_age(then, now))),
        None => cell,
    }
}

/// A store path's name, with the start of its hash to tell equal names apart.
fn store_path_cell(path: &str) -> TableCell {
    let Some((hash, name)) = path
        .strip_prefix("/nix/store/")
        .and_then(|base| base.split_once('-'))
    else {
        return TableCell::new(path);
    };
    TableCell::new(name).note(format!("  {}", hash.get(..8).unwrap_or(hash)))
}

fn render_generations(frame: &mut Frame, app: &App, area: Rect) {
    let generations = app
        .payload()
        .and_then(|payload| payload.get("generations"))
        .and_then(Value::as_array);
    let count = generations.map(Vec::len).unwrap_or(0);
    if count == 0 && !app.loading() {
        render_empty_state(
            frame,
            area,
            "No generations found",
            "The active platform profile has no generations to compare.",
            "Press r to refresh generation history.",
        );
        return;
    }
    let rows = generations
        .into_iter()
        .flatten()
        .map(|generation| {
            let number = generation.get("number").and_then(Value::as_u64);
            let current = generation.get("current").and_then(Value::as_bool) == Some(true);
            let state = match (current, number == app.history_marked) {
                (true, true) => TableCell::styled("current · marked", Style::new().fg(OK).bold()),
                (true, false) => TableCell::styled("current", Style::new().fg(OK).bold()),
                (false, true) => TableCell::styled("marked", Style::new().fg(WARN)),
                _ => TableCell::new(""),
            };
            vec![
                TableCell::new(text(generation.get("number"))),
                state,
                time_cell(&text(generation.get("createdAt")), app.now()),
                TableCell::styled(text(generation.get("path")), theme::faint()),
                store_path_cell(&text(generation.get("target"))),
            ]
        })
        .collect();
    render_table(
        frame,
        area,
        DataTable {
            title: "Generations · Space mark · d diff".to_string(),
            position: Some(app.selection_position().unwrap_or((0, count))),
            columns: &GENERATION_COLUMNS,
            rows,
            selected: app.history_selected,
            scroll: app.scroll,
        },
    );
}

const JOURNAL_COLUMNS: [Column; 6] = [
    column("TIME", Constraint::Length(24)),
    column("OPERATION", Constraint::Length(14)),
    column("RESULT", Constraint::Length(8)),
    column("DURATION", Constraint::Length(9)),
    optional("MACHINE", Constraint::Length(24), 150),
    column("DETAIL", Constraint::Min(20)),
];

fn render_journal(frame: &mut Frame, app: &App, area: Rect) {
    let entries = journal_entries(app.payload());
    let count = entries.len();
    if count == 0 && !app.loading() {
        render_empty_state(
            frame,
            area,
            "No operations recorded yet",
            "State-changing commands (apply, sync, push, update, rollback, clean) appear here.",
            "Run an operation, then press r to refresh.",
        );
        return;
    }
    let rows = entries
        .iter()
        .map(|entry| {
            let result = if entry.result == "ok" {
                TableCell::styled("ok", Style::new().fg(OK))
            } else {
                TableCell::styled(entry.result.clone(), Style::new().fg(ERROR))
            };
            vec![
                time_cell(&entry.timestamp, app.now()),
                TableCell::new(entry.operation.clone()),
                result,
                TableCell::new(entry.duration.clone()),
                TableCell::styled(entry.machine.clone(), theme::faint()),
                TableCell::new(entry.detail.clone()),
            ]
        })
        .collect();
    render_table(
        frame,
        area,
        DataTable {
            title: "Operation journal".to_string(),
            position: Some(app.selection_position().unwrap_or((0, count))),
            columns: &JOURNAL_COLUMNS,
            rows,
            selected: app.journal_selected,
            scroll: app.scroll,
        },
    );
}
