use std::time::{SystemTime, UNIX_EPOCH};

use ratatui::{
    prelude::*,
    widgets::{Block, Borders, Cell, Clear, Paragraph, Row, Table, TableState, Tabs, Wrap},
};
use serde_json::Value;

use crate::{
    app::{App, InputMode},
    model::{
        journal_entries, mirror_mode, mirror_target_rows, search_entries, text, DetailView,
        HistoryView, NavigationSection, Screen, SoftwareAction, WorkflowLog,
    },
};

const BACKGROUND: Color = Color::Rgb(8, 15, 24);
const PANEL: Color = Color::Rgb(18, 22, 30);
const SELECTED: Color = Color::Rgb(25, 45, 60);

pub fn row_offset(scroll: usize, row_count: usize, area: Rect) -> usize {
    let visible_rows = area.height.saturating_sub(3) as usize;
    scroll.min(row_count.saturating_sub(visible_rows))
}

fn selected_state(selected: usize, offset: usize, empty: bool) -> TableState {
    TableState::default().with_selected((!empty).then(|| selected.saturating_sub(offset)))
}

fn card<'a>(widget: Paragraph<'a>) -> Paragraph<'a> {
    widget.block(
        Block::default()
            .borders(Borders::ALL)
            .border_style(Color::DarkGray),
    )
}

fn render_input_field(
    frame: &mut Frame,
    area: Rect,
    title: &str,
    value: &str,
    active: bool,
    placeholder: &str,
) {
    let block = Block::default()
        .title(format!(" {title} "))
        .borders(Borders::ALL)
        .border_style(if active { Color::Cyan } else { Color::DarkGray });
    let content = if value.is_empty() {
        Line::from(vec![
            Span::styled(" › ", Style::default().fg(Color::Cyan).bold()),
            Span::styled(
                placeholder.to_string(),
                Style::default().fg(Color::DarkGray),
            ),
            Span::styled(
                if active { "█" } else { "" },
                Style::default().fg(Color::Cyan),
            ),
        ])
    } else {
        Line::from(vec![
            Span::styled(" › ", Style::default().fg(Color::Cyan).bold()),
            Span::styled(value.to_string(), Style::default().fg(Color::White)),
            Span::styled(
                if active { "█" } else { "" },
                Style::default().fg(Color::Cyan),
            ),
        ])
    };
    frame.render_widget(Paragraph::new(content).block(block), area);
}

fn render_empty_state(frame: &mut Frame, area: Rect, title: &str, message: &str, action: &str) {
    let panel_height = 7.min(area.height.max(1));
    let rows = Layout::vertical([
        Constraint::Fill(1),
        Constraint::Length(panel_height),
        Constraint::Fill(1),
    ])
    .split(area);
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(""),
            Line::from(Span::styled(
                title.to_string(),
                Style::default().fg(Color::Cyan).bold(),
            )),
            Line::from(""),
            Line::from(message.to_string()),
            Line::from(Span::styled(
                action.to_string(),
                Style::default().fg(Color::DarkGray),
            )),
        ])
        .alignment(Alignment::Center)
        .wrap(Wrap { trim: false })
        .block(
            Block::default()
                .borders(Borders::ALL)
                .border_style(Color::DarkGray),
        ),
        rows[1],
    );
}

fn render_header(frame: &mut Frame, app: &App, area: Rect) {
    let titles = NavigationSection::ALL
        .iter()
        .enumerate()
        .map(|(index, section)| {
            Line::from(vec![
                Span::styled(
                    format!(" {} ", index + 1),
                    Style::default().fg(Color::DarkGray),
                ),
                Span::styled(
                    section.title(),
                    if *section == app.screen.section() {
                        Style::default().fg(Color::Black).bg(Color::Cyan).bold()
                    } else {
                        Style::default().fg(Color::Gray)
                    },
                ),
            ])
        })
        .collect::<Vec<_>>();
    frame.render_widget(
        Tabs::new(titles)
            .select(app.screen.section().index())
            .divider("│")
            .block(
                Block::default()
                    .title(" ENVY ")
                    .borders(Borders::ALL)
                    .border_style(Color::Cyan),
            ),
        area,
    );
}

fn page_keys(screen: Screen) -> &'static str {
    match screen {
        Screen::Sources => "Enter select source  s sync metadata",
        Screen::Software => "Enter toggle  w why  / filter",
        Screen::Search => "Enter add  / query",
        Screen::Doctor => "Enter details",
        Screen::History => "v view  Space mark  d diff",
        Screen::Home => "2 Configure  3 Activity",
        Screen::Config => "Enter edit host / setting",
        Screen::Mirror => "Enter source",
        _ => "r refresh",
    }
}

fn compact_page_keys(screen: Screen) -> &'static str {
    match screen {
        Screen::Sources => "↵ source  s sync",
        Screen::Software => "↵ toggle  w why  / filter",
        Screen::Search => "↵ add  / query",
        Screen::Doctor => "↵ details",
        Screen::History => "v view  Space mark  d diff",
        Screen::Home => "2 Configure  3 Activity",
        Screen::Config => "↵ edit setting",
        Screen::Mirror => "↵ source",
        _ => "r refresh",
    }
}

fn spinner() -> &'static str {
    const FRAMES: [&str; 8] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧"];
    let tick = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        / 120;
    FRAMES[tick as usize % FRAMES.len()]
}

fn render_footer(frame: &mut Frame, app: &App, area: Rect) {
    let mode = match app.input_mode {
        InputMode::Normal => "NORMAL",
        InputMode::Search => "SEARCH",
        InputMode::SoftwareFilter => "FILTER",
        InputMode::SettingValue => "EDIT",
    };
    let activity = if app.mutation_loading {
        format!("  {} verifying", spinner())
    } else if app.mirror_loading {
        format!("  {} mirror", spinner())
    } else if app.mirror_measuring {
        format!("  {} measuring", spinner())
    } else if app.loading() {
        if app.current_has_content() {
            format!("  {} refreshing", spinner())
        } else {
            format!("  {} loading", spinner())
        }
    } else {
        String::new()
    };
    let hints = if area.width >= 118 {
        format!(
            "{}  │  ↑↓ select  Tab section  v view  r refresh  ? help  q quit",
            page_keys(app.screen)
        )
    } else if area.width >= 78 {
        format!(
            "{}  │  Tab section  v view  ? help  q quit",
            compact_page_keys(app.screen)
        )
    } else {
        "? help  q quit".to_string()
    };
    let hint_width = (hints.chars().count() as u16 + 1).min(area.width.saturating_sub(12));
    let columns =
        Layout::horizontal([Constraint::Min(12), Constraint::Length(hint_width)]).split(area);
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(
                format!(" {mode} "),
                Style::default().fg(Color::Black).bg(Color::Green).bold(),
            ),
            Span::styled(
                format!("  {}  ", app.status),
                Style::default().fg(Color::Gray),
            ),
            Span::styled(activity, Style::default().fg(Color::Yellow)),
        ])),
        columns[0],
    );
    frame.render_widget(
        Paragraph::new(Span::styled(hints, Style::default().fg(Color::DarkGray)))
            .alignment(Alignment::Right),
        columns[1],
    );
}

fn render_home(frame: &mut Frame, app: &App, area: Rect) {
    let payload = app.payload().unwrap_or(&Value::Null);
    let config = payload.get("config").unwrap_or(&Value::Null);
    let software = payload.get("software").unwrap_or(&Value::Null);
    let doctor = payload.get("doctor").unwrap_or(&Value::Null);
    let git = payload.get("git").unwrap_or(&Value::Null);
    let generation = payload.get("generation").unwrap_or(&Value::Null);
    let values = config.get("values").unwrap_or(&Value::Null);
    let machine = config
        .get("device")
        .and_then(|value| value.get("machineId"));
    let user = values
        .get("envy.user.name")
        .or_else(|| values.get("user.name"));
    let platform = config
        .get("platform")
        .or_else(|| values.get("envy.platform"))
        .or_else(|| config.get("device").and_then(|value| value.get("platform")));
    let summary = doctor.get("summary").unwrap_or(&Value::Null);
    let left = vec![
        Line::from(Span::styled(
            "Machine",
            Style::default().fg(Color::Cyan).bold(),
        )),
        Line::from(format!("  {}", text(machine))),
        Line::from(format!("  user      {}", text(user))),
        Line::from(format!("  platform  {}", text(platform))),
        Line::from(""),
        Line::from(Span::styled(
            "Software",
            Style::default().fg(Color::Cyan).bold(),
        )),
        Line::from(format!("  groups    {}", text(software.get("groups")))),
        Line::from(format!("  included  {}", text(software.get("included")))),
        Line::from(format!("  effective {}", text(software.get("effective")))),
        Line::from(format!("  excluded  {}", text(software.get("excluded")))),
        Line::from(""),
        Line::from(Span::styled(
            "Repository",
            Style::default().fg(Color::Cyan).bold(),
        )),
        Line::from(format!("  branch    {}", text(git.get("branch")))),
        Line::from(format!("  changes   {}", text(git.get("changes")))),
    ];
    let generation_number = generation
        .get("current")
        .and_then(|value| value.get("number"));
    let recommendations = payload
        .get("recommendations")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let right = vec![
        Line::from(Span::styled(
            "Doctor",
            Style::default().fg(Color::Cyan).bold(),
        )),
        Line::from(format!("  ok       {}", text(summary.get("ok")))),
        Line::from(format!("  warnings {}", text(summary.get("warn")))),
        Line::from(format!("  errors   {}", text(summary.get("error")))),
        Line::from(format!("  generation {}", text(generation_number))),
        Line::from(""),
        Line::from(Span::styled(
            "Recommended",
            Style::default().fg(Color::Cyan).bold(),
        )),
        Line::from(format!(
            "  {}",
            recommendations
                .first()
                .and_then(|value| value.get("command"))
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .unwrap_or("No action required")
        )),
        Line::from(format!(
            "  {}",
            recommendations
                .first()
                .and_then(|value| value.get("reason"))
                .and_then(Value::as_str)
                .unwrap_or("The selected machine is healthy")
        )),
        Line::from(""),
        Line::from(Span::styled(
            "Next",
            Style::default().fg(Color::Cyan).bold(),
        )),
        Line::from("  2 Configure  software, mirrors, and system settings"),
        Line::from("  3 Activity   health details and operation history"),
        Line::from(""),
        Line::from("  Setup and repair guidance appears here when attention is needed."),
    ];
    let summary_area = area;
    if summary_area.width >= 88 {
        let columns = Layout::horizontal([Constraint::Percentage(50), Constraint::Percentage(50)])
            .split(summary_area);
        frame.render_widget(card(Paragraph::new(left)), columns[0]);
        frame.render_widget(card(Paragraph::new(right)), columns[1]);
    } else {
        let rows = Layout::vertical([Constraint::Percentage(50), Constraint::Percentage(50)])
            .split(summary_area);
        frame.render_widget(card(Paragraph::new(left)), rows[0]);
        frame.render_widget(card(Paragraph::new(right)), rows[1]);
    }
}

fn render_settings(frame: &mut Frame, app: &App, area: Rect) {
    let settings = app.settings_rows();
    let count = settings.len();
    if count == 0 {
        frame.render_widget(
            card(Paragraph::new(vec![
                Line::from(""),
                Line::from(Span::styled(
                    "  Loading host and config values…",
                    Style::default().fg(Color::DarkGray),
                )),
            ])),
            area,
        );
        return;
    }
    let offset = row_offset(app.scroll, count, area);
    let rows = settings
        .iter()
        .skip(offset)
        .map(|row| {
            let kind = if row.key == crate::model::SettingKey::Host {
                Cell::from("host").style(Style::default().fg(Color::Yellow))
            } else if row.freeform() {
                Cell::from("text").style(Style::default().fg(Color::DarkGray))
            } else {
                Cell::from("select").style(Style::default().fg(Color::Cyan))
            };
            let value = if row.value.is_empty() {
                "—".to_string()
            } else {
                row.value.clone()
            };
            Row::new(vec![kind, Cell::from(row.label.clone()), Cell::from(value)])
        })
        .collect::<Vec<_>>();
    let table = Table::new(
        rows,
        [
            Constraint::Length(8),
            Constraint::Length(30),
            Constraint::Min(20),
        ],
    )
    .header(Row::new(["TYPE", "SETTING", "VALUE"]).style(Style::default().fg(Color::Cyan).bold()))
    .row_highlight_style(Style::default().bg(SELECTED))
    .highlight_symbol("▸ ")
    .block(
        Block::default()
            .title({
                let (selected, count) = app.selection_position().unwrap_or((0, count));
                format!(" Settings — {selected} / {count} · Enter edit ")
            })
            .borders(Borders::ALL)
            .border_style(Color::DarkGray),
    );
    let mut state = selected_state(app.settings_selected, offset, count == 0);
    frame.render_stateful_widget(table, area, &mut state);
}

fn render_sources(frame: &mut Frame, app: &App, area: Rect) {
    let sources = app
        .payload()
        .and_then(|v| v.get("sources"))
        .and_then(Value::as_array);
    let count = sources.map(Vec::len).unwrap_or(0);
    let offset = row_offset(app.scroll, count, area);
    let rows = sources
        .into_iter()
        .flatten()
        .skip(offset)
        .map(|source| {
            Row::new(vec![
                text(source.get("id")),
                text(source.get("type")),
                text(source.get("registered")),
                text(source.get("selected")),
                text(source.get("ready")),
                text(source.get("owner")),
            ])
        })
        .collect::<Vec<_>>();
    let table = Table::new(
        rows,
        [
            Constraint::Min(26),
            Constraint::Length(10),
            Constraint::Length(12),
            Constraint::Length(10),
            Constraint::Length(8),
            Constraint::Min(16),
        ],
    )
    .header(Row::new([
        "SOURCE",
        "TYPE",
        "REGISTERED",
        "SELECTED",
        "READY",
        "OWNER",
    ]))
    .row_highlight_style(Style::default().bg(SELECTED))
    .highlight_symbol("▸ ")
    .block(
        Block::default()
            .title(" Sources · Enter select repository · s sync metadata ")
            .borders(Borders::ALL),
    );
    let mut state = selected_state(app.sources_selected, offset, count == 0);
    frame.render_stateful_widget(table, area, &mut state);
}

fn render_software(frame: &mut Frame, app: &App, area: Rect) {
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
    let offset = row_offset(app.scroll, entries.len(), table_area);
    let rows = entries
        .iter()
        .skip(offset)
        .map(|entry| {
            let state_style = match entry.state.as_str() {
                "effective" => Style::default().fg(Color::Green),
                "blocked" => Style::default().fg(Color::Red),
                "stale" => Style::default().fg(Color::Yellow),
                _ => Style::default().fg(Color::DarkGray),
            };
            Row::new(vec![
                Cell::from(entry.group.clone()),
                Cell::from(entry.item.clone()),
                Cell::from(entry.version.clone()),
                Cell::from(entry.state.clone()).style(state_style),
                Cell::from(format!("{} · {}", entry.source_id, entry.reference)),
            ])
        })
        .collect::<Vec<_>>();
    let position = app
        .selection_position()
        .map(|(selected, count)| format!("{selected} / {count} selected"))
        .unwrap_or_default();
    let title = if app.software_filter.is_empty() {
        format!(" Software policy — {total} items · {position} ")
    } else {
        format!(
            " Software policy — {} / {total} visible · {position} ",
            entries.len()
        )
    };
    let table = Table::new(
        rows,
        [
            Constraint::Length(28),
            Constraint::Length(22),
            Constraint::Length(12),
            Constraint::Length(12),
            Constraint::Min(20),
        ],
    )
    .header(
        Row::new(["GROUP", "ITEM", "VERSION", "STATE", "REFERENCE"])
            .style(Style::default().fg(Color::Cyan).bold()),
    )
    .row_highlight_style(Style::default().bg(SELECTED))
    .highlight_symbol("▸ ")
    .block(
        Block::default()
            .title(title)
            .borders(Borders::ALL)
            .border_style(Color::DarkGray),
    );
    let mut state = selected_state(app.software_selected, offset, entries.is_empty());
    frame.render_stateful_widget(table, table_area, &mut state);
}

fn render_search(frame: &mut Frame, app: &App, area: Rect) {
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
    let offset = row_offset(app.scroll, entries.len(), table_area);
    let rows = entries
        .iter()
        .skip(offset)
        .map(|entry| {
            Row::new(vec![
                Cell::from(
                    entry
                        .source_id
                        .clone()
                        .unwrap_or_else(|| entry.source.clone()),
                ),
                Cell::from(entry.kind.clone()),
                Cell::from(entry.name.clone()),
                Cell::from(entry.version.clone()),
                Cell::from(format!("{} · {}", entry.availability, entry.description)),
                Cell::from(entry.reference.clone()),
            ])
        })
        .collect::<Vec<_>>();
    let (selected, count) = app.selection_position().unwrap_or((0, entries.len()));
    let title = format!(
        " Results for ‘{}’ — {selected} / {count} selected ",
        app.submitted_query
    );
    let table = Table::new(
        rows,
        [
            Constraint::Length(12),
            Constraint::Length(12),
            Constraint::Length(26),
            Constraint::Length(12),
            Constraint::Min(24),
            Constraint::Length(34),
        ],
    )
    .header(
        Row::new([
            "SOURCE",
            "KIND",
            "NAME",
            "VERSION",
            "DESCRIPTION",
            "REFERENCE",
        ])
        .style(Style::default().fg(Color::Cyan).bold()),
    )
    .row_highlight_style(Style::default().bg(SELECTED))
    .highlight_symbol("▸ ")
    .block(
        Block::default()
            .title(title)
            .borders(Borders::ALL)
            .border_style(Color::DarkGray),
    );
    let mut state = selected_state(app.search_selected, offset, entries.is_empty());
    frame.render_stateful_widget(table, table_area, &mut state);
}

fn status_style(status: &str) -> Style {
    match status {
        "ok" => Style::default().fg(Color::Green),
        "warn" => Style::default().fg(Color::Yellow),
        "error" => Style::default().fg(Color::Red),
        _ => Style::default().fg(Color::Gray),
    }
}

fn render_doctor(frame: &mut Frame, app: &App, area: Rect) {
    let results = app
        .payload()
        .and_then(|payload| payload.get("results"))
        .and_then(Value::as_array);
    let count = results.map(Vec::len).unwrap_or(0);
    if count == 0 && !app.loading() {
        render_empty_state(
            frame,
            area,
            "No doctor results",
            "envY did not return any checks for this machine.",
            "Press r to run Doctor again.",
        );
        return;
    }
    let offset = row_offset(app.scroll, count, area);
    let rows = results
        .map(|results| {
            results
                .iter()
                .skip(offset)
                .map(|result| {
                    let status = text(result.get("status"));
                    Row::new(vec![
                        Cell::from(status.clone()).style(status_style(&status)),
                        Cell::from(text(result.get("section"))),
                        Cell::from(text(result.get("name"))),
                        Cell::from(text(result.get("message"))),
                    ])
                })
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    let table = Table::new(
        rows,
        [
            Constraint::Length(10),
            Constraint::Length(16),
            Constraint::Length(30),
            Constraint::Min(40),
        ],
    )
    .header(
        Row::new(["STATUS", "SECTION", "CHECK", "RESULT"])
            .style(Style::default().fg(Color::Cyan).bold()),
    )
    .row_highlight_style(Style::default().bg(SELECTED))
    .highlight_symbol("▸ ")
    .block(
        Block::default()
            .title({
                let (selected, count) = app.selection_position().unwrap_or((0, count));
                format!(" Doctor — {selected} / {count} selected · Enter details ")
            })
            .borders(Borders::ALL)
            .border_style(Color::DarkGray),
    );
    let mut state = selected_state(app.doctor_selected, offset, count == 0);
    frame.render_stateful_widget(table, area, &mut state);
}

fn render_history(frame: &mut Frame, app: &App, area: Rect) {
    match app.history_view {
        HistoryView::Generations => render_generations(frame, app, area),
        HistoryView::Operations => render_journal(frame, app, area),
    }
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
    let offset = row_offset(app.scroll, count, area);
    let rows = generations
        .map(|generations| {
            generations
                .iter()
                .skip(offset)
                .map(|generation| {
                    let number = generation.get("number").and_then(Value::as_u64);
                    let current = generation.get("current").and_then(Value::as_bool) == Some(true);
                    let state = match (current, number == app.history_marked) {
                        (true, true) => "current · marked",
                        (true, false) => "current",
                        (false, true) => "marked",
                        _ => "",
                    };
                    Row::new(vec![
                        Cell::from(text(generation.get("number"))),
                        Cell::from(state),
                        Cell::from(text(generation.get("createdAt"))),
                        Cell::from(text(generation.get("path"))),
                        Cell::from(text(generation.get("target"))),
                    ])
                })
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    let table = Table::new(
        rows,
        [
            Constraint::Length(12),
            Constraint::Length(18),
            Constraint::Length(28),
            Constraint::Length(38),
            Constraint::Min(40),
        ],
    )
    .header(
        Row::new(["GENERATION", "STATE", "CREATED", "PROFILE", "STORE TARGET"])
            .style(Style::default().fg(Color::Cyan).bold()),
    )
    .row_highlight_style(Style::default().bg(SELECTED))
    .highlight_symbol("▸ ")
    .block(
        Block::default()
            .title({
                let (selected, count) = app.selection_position().unwrap_or((0, count));
                format!(" Generations — {selected} / {count} selected · Space mark · d diff ")
            })
            .borders(Borders::ALL)
            .border_style(Color::DarkGray),
    );
    let mut state = selected_state(app.history_selected, offset, count == 0);
    frame.render_stateful_widget(table, area, &mut state);
}

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
    let offset = row_offset(app.scroll, count, area);
    let rows = entries
        .iter()
        .skip(offset)
        .map(|entry| {
            let result_cell = if entry.result == "ok" {
                Cell::from("ok").style(Style::default().fg(Color::Green))
            } else {
                Cell::from(entry.result.clone()).style(Style::default().fg(Color::Red))
            };
            Row::new(vec![
                Cell::from(entry.timestamp.clone()),
                Cell::from(entry.operation.clone()),
                result_cell,
                Cell::from(entry.duration.clone()),
                Cell::from(entry.machine.clone()),
                Cell::from(entry.detail.clone()),
            ])
        })
        .collect::<Vec<_>>();
    let table = Table::new(
        rows,
        [
            Constraint::Length(26),
            Constraint::Length(15),
            Constraint::Length(8),
            Constraint::Length(10),
            Constraint::Length(24),
            Constraint::Min(20),
        ],
    )
    .header(
        Row::new([
            "TIME",
            "OPERATION",
            "RESULT",
            "DURATION",
            "MACHINE",
            "DETAIL",
        ])
        .style(Style::default().fg(Color::Cyan).bold()),
    )
    .row_highlight_style(Style::default().bg(SELECTED))
    .highlight_symbol("▸ ")
    .block(
        Block::default()
            .title({
                let (selected, count) = app.selection_position().unwrap_or((0, count));
                format!(" Operation journal — {selected} / {count} ")
            })
            .borders(Borders::ALL)
            .border_style(Color::DarkGray),
    );
    let mut state = selected_state(app.journal_selected, offset, count == 0);
    frame.render_stateful_widget(table, area, &mut state);
}

fn render_mirror(frame: &mut Frame, app: &App, area: Rect) {
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
    let offset = row_offset(app.scroll, count, area);
    let rows = entries
        .iter()
        .skip(offset)
        .map(|entry| {
            Row::new(vec![
                Cell::from(entry.key.clone()).style(Style::default().fg(Color::Cyan).bold()),
                Cell::from(entry.value.clone()).style(Style::default().fg(Color::White)),
            ])
        })
        .collect::<Vec<_>>();
    let mode = mirror_mode(app.payload());
    let table = Table::new(rows, [Constraint::Length(18), Constraint::Min(30)])
        .header(Row::new(["TARGET", "EFFECTIVE SOURCE"]).style(Style::default().fg(Color::Cyan).bold()))
        .row_highlight_style(Style::default().bg(SELECTED))
        .highlight_symbol("▸ ")
        .block(
            Block::default()
                .title({
                    let (selected, count) = app.selection_position().unwrap_or((0, count));
                    format!(
                        " Mirror targets — profile {mode} · {selected} / {count} · Enter choose source "
                    )
                })
                .borders(Borders::ALL)
                .border_style(Color::DarkGray),
        );
    let mut state = selected_state(app.mirror_selected, offset, count == 0);
    frame.render_stateful_widget(table, area, &mut state);
}

fn render_section_navigation(frame: &mut Frame, app: &App, area: Rect) {
    let (title, labels, selected) = match app.screen.section() {
        NavigationSection::Home => return,
        NavigationSection::Configure => {
            let selected = match app.screen {
                Screen::Software => 0,
                Screen::Search => 1,
                Screen::Sources => 2,
                Screen::Mirror => 3,
                Screen::Config => 4,
                _ => 0,
            };
            (
                "Configure",
                ["Software", "Search", "Sources", "Mirrors", "Settings"].as_slice(),
                selected,
            )
        }
        NavigationSection::Activity => {
            let selected = match (app.screen, app.history_view) {
                (Screen::History, HistoryView::Generations) => 1,
                (Screen::History, HistoryView::Operations) | (Screen::Journal, _) => 2,
                _ => 0,
            };
            (
                "Activity",
                ["Health", "Generations", "Operations"].as_slice(),
                selected,
            )
        }
    };
    let tabs = labels
        .iter()
        .map(|label| Line::from(Span::styled(*label, Style::default().fg(Color::Gray))))
        .collect::<Vec<_>>();
    frame.render_widget(
        Tabs::new(tabs)
            .select(selected)
            .highlight_style(Style::default().fg(Color::Black).bg(Color::Cyan).bold())
            .divider(" │ ")
            .block(
                Block::default()
                    .title(format!(" {title} · v switch view "))
                    .borders(Borders::BOTTOM)
                    .border_style(Color::DarkGray),
            ),
        area,
    );
}

fn render_body(frame: &mut Frame, app: &App, area: Rect) {
    let content_area = if app.screen.section() == NavigationSection::Home {
        area
    } else {
        let sections = Layout::vertical([Constraint::Length(3), Constraint::Min(1)]).split(area);
        render_section_navigation(frame, app, sections[0]);
        sections[1]
    };
    if let Some(error) = app.current_error().filter(|_| !app.current_has_content()) {
        frame.render_widget(
            Paragraph::new(vec![
                Line::from(Span::styled(
                    "Unable to load data",
                    Style::default().fg(Color::Red).bold(),
                )),
                Line::from(""),
                Line::from(error),
                Line::from(""),
                Line::from("Press r to retry or q to quit."),
            ])
            .wrap(Wrap { trim: false })
            .block(
                Block::default()
                    .title(" Error ")
                    .borders(Borders::ALL)
                    .border_style(Color::Red),
            ),
            content_area,
        );
        return;
    }
    match app.screen {
        Screen::Home => render_home(frame, app, content_area),
        Screen::Sources => render_sources(frame, app, content_area),
        Screen::Software => render_software(frame, app, content_area),
        Screen::Search => render_search(frame, app, content_area),
        Screen::Doctor => render_doctor(frame, app, content_area),
        Screen::History => render_history(frame, app, content_area),
        Screen::Mirror => render_mirror(frame, app, content_area),
        Screen::Config => render_settings(frame, app, content_area),
        // Journal and Hosts remain backend leafs: Journal renders through the
        // History/Operations view and Hosts feeds the Settings host selector.
        Screen::Journal | Screen::Hosts => {}
    }
    if app.loading() && !app.current_has_content() && app.screen != Screen::Search {
        frame.render_widget(Clear, content_area);
        frame.render_widget(
            Paragraph::new(format!("  {} Loading complete envY data…", spinner()))
                .style(Style::default().fg(Color::Yellow))
                .block(Block::default().borders(Borders::ALL)),
            content_area,
        );
    }
}

fn inset_rect(area: Rect, horizontal: u16, vertical: u16) -> Rect {
    let horizontal = horizontal.min(area.width / 2);
    let vertical = vertical.min(area.height / 2);
    Rect {
        x: area.x.saturating_add(horizontal),
        y: area.y.saturating_add(vertical),
        width: area.width.saturating_sub(horizontal.saturating_mul(2)),
        height: area.height.saturating_sub(vertical.saturating_mul(2)),
    }
}

fn centered_size(width: u16, height: u16, area: Rect) -> Rect {
    let width = width.min(area.width.saturating_sub(4)).max(1);
    let height = height.min(area.height.saturating_sub(2)).max(1);
    Rect {
        x: area.x + area.width.saturating_sub(width) / 2,
        y: area.y + area.height.saturating_sub(height) / 2,
        width,
        height,
    }
}

fn popup(frame: &mut Frame, area: Rect, title: &str, lines: Vec<Line<'static>>, scroll: usize) {
    frame.render_widget(Clear, area);
    let block = Block::default()
        .title(format!(" {title} "))
        .borders(Borders::ALL)
        .border_style(Color::Cyan)
        .style(Style::default().bg(PANEL));
    let inner = inset_rect(block.inner(area), 2, 1);
    frame.render_widget(block, area);
    frame.render_widget(
        Paragraph::new(lines)
            .wrap(Wrap { trim: false })
            .scroll((scroll.min(u16::MAX as usize) as u16, 0)),
        inner,
    );
}

fn key_footer(scroll: Option<(usize, usize)>) -> Line<'static> {
    let mut spans = vec![
        Span::styled(
            " ENTER ",
            Style::default().fg(Color::Black).bg(Color::Cyan).bold(),
        ),
        Span::raw(" close"),
    ];
    if let Some((current, maximum)) = scroll {
        spans.extend([
            Span::raw("    "),
            Span::styled(
                " ↑↓ ",
                Style::default().fg(Color::White).bg(Color::DarkGray).bold(),
            ),
            Span::raw(format!(" scroll  {} / {}", current + 1, maximum + 1)),
        ]);
    }
    Line::from(spans)
}

fn bool_line(label: &'static str, value: Option<&Value>) -> Line<'static> {
    let enabled = value.and_then(Value::as_bool).unwrap_or(false);
    Line::from(vec![
        Span::styled(format!("{label:<18}"), Style::default().fg(Color::DarkGray)),
        Span::styled(
            if enabled { "yes" } else { "no" },
            Style::default().fg(if enabled { Color::Green } else { Color::Gray }),
        ),
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
            Span::styled("ITEM  ", Style::default().fg(Color::DarkGray)),
            Span::styled(
                text(matched.get("item")),
                Style::default().fg(Color::White).bold(),
            ),
        ]),
        Line::from(format!("Group       {}", text(matched.get("group")))),
        Line::from(format!("Label       {}", text(matched.get("label")))),
        Line::from(format!("Name        {}", text(matched.get("name")))),
        Line::from(format!("Reference   {}", text(matched.get("ref")))),
        Line::from(""),
        Line::from(Span::styled(
            "Evaluated state",
            Style::default().fg(Color::Cyan).bold(),
        )),
        bool_line("Included", matched.get("included")),
        bool_line("Excluded", matched.get("excluded")),
        bool_line("Effective", matched.get("effective")),
        Line::from(""),
        Line::from(Span::styled(
            "Ownership",
            Style::default().fg(Color::Cyan).bold(),
        )),
        bool_line("Machine include", matched.get("machineInclude")),
        bool_line("Machine exclude", matched.get("machineExclude")),
        bool_line("External include", matched.get("externalInclude")),
        bool_line("External exclude", matched.get("externalExclude")),
        Line::from(""),
        Line::from(Span::styled("Why", Style::default().fg(Color::Cyan).bold())),
        Line::from(reason),
    ]
}

fn json_lines(value: Option<&Value>) -> Vec<Line<'static>> {
    match value {
        None | Some(Value::Null) => vec![Line::from("None")],
        Some(value) => serde_json::to_string_pretty(value)
            .unwrap_or_else(|_| value.to_string())
            .lines()
            .map(|line| Line::from(line.to_string()))
            .collect(),
    }
}

fn doctor_detail_lines(result: &Value) -> Vec<Line<'static>> {
    let status = text(result.get("status"));
    let mut lines = vec![
        Line::from(vec![
            Span::styled(
                format!(" {} ", status.to_uppercase()),
                status_style(&status)
                    .fg(Color::Black)
                    .bg(match status.as_str() {
                        "ok" => Color::Green,
                        "warn" => Color::Yellow,
                        "error" => Color::Red,
                        _ => Color::Gray,
                    })
                    .bold(),
            ),
            Span::styled(
                format!("  {}", text(result.get("name"))),
                Style::default().fg(Color::White).bold(),
            ),
        ]),
        Line::from(format!("Section  {}", text(result.get("section")))),
        Line::from(""),
        Line::from(Span::styled(
            "Result",
            Style::default().fg(Color::Cyan).bold(),
        )),
        Line::from(text(result.get("message"))),
        Line::from(""),
        Line::from(Span::styled(
            "Hint",
            Style::default().fg(Color::Cyan).bold(),
        )),
        Line::from(text(result.get("hint"))),
        Line::from(""),
        Line::from(Span::styled(
            "Details",
            Style::default().fg(Color::Cyan).bold(),
        )),
    ];
    lines.extend(json_lines(result.get("details")));
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        "Suggested action",
        Style::default().fg(Color::Cyan).bold(),
    )));
    lines.extend(json_lines(result.get("action")));
    if result.get("action").is_some_and(|value| !value.is_null()) {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(
            "Press x to run an allow-listed action; envY will return here afterward.",
            Style::default().fg(Color::Yellow),
        )));
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
            Span::styled(
                text(before.get("number")),
                Style::default().fg(Color::Yellow).bold(),
            ),
            Span::raw("  →  "),
            Span::styled(
                text(after.get("number")),
                Style::default().fg(Color::Green).bold(),
            ),
        ]),
        Line::from(""),
        Line::from(Span::styled(
            "Before",
            Style::default().fg(Color::Cyan).bold(),
        )),
        Line::from(format!("Created  {}", text(before.get("createdAt")))),
        Line::from(format!("Profile  {}", text(before.get("path")))),
        Line::from(format!("Target   {}", text(before.get("target")))),
        Line::from(""),
        Line::from(Span::styled(
            "After",
            Style::default().fg(Color::Cyan).bold(),
        )),
        Line::from(format!("Created  {}", text(after.get("createdAt")))),
        Line::from(format!("Profile  {}", text(after.get("path")))),
        Line::from(format!("Target   {}", text(after.get("target")))),
        Line::from(""),
        Line::from(Span::styled(
            "Closure diff",
            Style::default().fg(Color::Cyan).bold(),
        )),
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
    let block = Block::default()
        .title(format!(" {title} "))
        .borders(Borders::ALL)
        .border_style(Color::Cyan)
        .style(Style::default().bg(PANEL));
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

fn render_detail(frame: &mut Frame, app: &mut App, area: Rect, detail: &DetailView) {
    match detail {
        DetailView::Loading { title } => popup(
            frame,
            centered_size(64, 8, area),
            title,
            vec![
                Line::from(""),
                Line::from(Span::styled(
                    format!("{} Loading complete details…", spinner()),
                    Style::default().fg(Color::Yellow).bold(),
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
            SoftwareAction::Add => Color::Green,
            SoftwareAction::Remove => Color::Yellow,
        };
        let mut lines = vec![
            Line::from(vec![
                Span::styled(
                    format!(" {} ", preview.intent.action.label().to_uppercase()),
                    Style::default().fg(Color::Black).bg(color).bold(),
                ),
                Span::styled(
                    format!("  {}", preview.intent.item),
                    Style::default().fg(Color::White).bold(),
                ),
            ]),
            Line::from(""),
            Line::from(format!("Group       {}", preview.intent.group)),
            Line::from(format!("Operand     {}", preview.intent.operand)),
            Line::from(format!("Result      {effective}")),
            Line::from(format!("Changed     {changed}")),
            Line::from("Verified    dry-run JSON schema v1"),
            Line::from(""),
            Line::from(Span::styled(
                "Policy operations",
                Style::default().fg(Color::Cyan).bold(),
            )),
        ];
        let mut operation_count = 0;
        for (label, color, value) in [
            ("Add include", Color::Green, plan.get("includeAdded")),
            ("Remove include", Color::Red, plan.get("includeRemoved")),
            ("Add exclusion", Color::Yellow, plan.get("excludeAdded")),
            ("Remove exclusion", Color::Cyan, plan.get("excludeRemoved")),
        ] {
            if let Some(items) = joined_strings(value) {
                operation_count += 1;
                lines.push(Line::from(vec![
                    Span::styled(format!("{label:<18}"), Style::default().fg(color)),
                    Span::styled(items, Style::default().fg(Color::White).bold()),
                ]));
            }
        }
        if operation_count == 0 {
            lines.push(Line::from(Span::styled(
                "No policy edits are required.",
                Style::default().fg(Color::DarkGray),
            )));
        }
        if let Some(reason) = blocked {
            lines.push(Line::from(""));
            lines.push(Line::from(Span::styled(
                format!("Blocked: {reason}"),
                Style::default().fg(Color::Red).bold(),
            )));
        } else {
            lines.push(Line::from(""));
            lines.push(Line::from(vec![
                Span::styled(
                    " ENTER ",
                    Style::default().fg(Color::Black).bg(Color::Green).bold(),
                ),
                Span::raw(" Apply    "),
                Span::styled(
                    " ESC ",
                    Style::default().fg(Color::White).bg(Color::DarkGray).bold(),
                ),
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
                    Style::default().fg(Color::Yellow).bold(),
                )),
            ],
            0,
        );
    }
}

fn render_group_chooser(frame: &mut Frame, app: &App, area: Rect) {
    let Some(chooser) = &app.group_chooser else {
        return;
    };
    let mut lines = vec![
        Line::from(vec![
            Span::styled("ADD  ", Style::default().fg(Color::Green).bold()),
            Span::styled(
                chooser.entry.name.clone(),
                Style::default().fg(Color::White).bold(),
            ),
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
            Style::default().fg(Color::Cyan).bold(),
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
                Style::default().fg(Color::Cyan),
            ),
            Span::styled(
                group.id.clone(),
                if index == chooser.selected {
                    Style::default().fg(Color::White).bg(SELECTED).bold()
                } else {
                    Style::default().fg(Color::Gray)
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

fn render_setting_chooser(frame: &mut Frame, app: &App, area: Rect) {
    let Some(chooser) = &app.setting_chooser else {
        return;
    };
    let mut lines = vec![
        Line::from(vec![
            Span::styled("EDIT  ", Style::default().fg(Color::Cyan).bold()),
            Span::styled(
                chooser.title.clone(),
                Style::default().fg(Color::White).bold(),
            ),
        ]),
        Line::from(format!("Current  {}", chooser.current)),
        Line::from(""),
        Line::from(Span::styled(
            "Choose a value",
            Style::default().fg(Color::Cyan).bold(),
        )),
    ];
    for (index, option) in chooser.options.iter().enumerate() {
        lines.push(Line::from(vec![
            Span::styled(
                if index == chooser.selected {
                    "▸ "
                } else {
                    "  "
                },
                Style::default().fg(Color::Cyan),
            ),
            Span::styled(
                option.clone(),
                if index == chooser.selected {
                    Style::default().fg(Color::White).bg(SELECTED).bold()
                } else {
                    Style::default().fg(Color::Gray)
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

fn render_setting_edit(frame: &mut Frame, app: &App, area: Rect) {
    let Some(edit) = &app.setting_edit else {
        return;
    };
    let region = centered_size(72, 8, area);
    frame.render_widget(Clear, region);
    frame.render_widget(
        Block::default()
            .title(format!(" EDIT {} ", edit.title))
            .borders(Borders::ALL)
            .border_style(Color::Cyan)
            .style(Style::default().bg(PANEL)),
        region,
    );
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
            Style::default().fg(Color::DarkGray),
        ))),
        rows[1],
    );
}

fn render_setting_confirm(frame: &mut Frame, app: &App, area: Rect) {
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
                Style::default().fg(Color::Yellow).bold(),
            )),
            Line::from(""),
            Line::from(format!("From  {previous}")),
            Line::from(vec![
                Span::raw("To    "),
                Span::styled(
                    pending.value.clone(),
                    Style::default().fg(Color::Green).bold(),
                ),
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
        Some(true) if source.measurement_stale => Style::default().fg(Color::Yellow),
        Some(true) => Style::default().fg(Color::Green),
        Some(false) => Style::default().fg(Color::Red),
        None if source.stale => Style::default().fg(Color::Yellow),
        _ => Style::default().fg(Color::DarkGray),
    }
}

fn render_mirror_chooser(frame: &mut Frame, app: &App, area: Rect) {
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
            Span::styled("MIRROR  ", Style::default().fg(Color::Cyan).bold()),
            Span::styled(
                chooser.label.clone(),
                Style::default().fg(Color::White).bold(),
            ),
        ]),
        Line::from(vec![
            Span::styled("Target       ", Style::default().fg(Color::DarkGray)),
            Span::styled(
                chooser.target.clone(),
                Style::default().fg(Color::White).bold(),
            ),
        ]),
        Line::from(vec![
            Span::styled("Profile      ", Style::default().fg(Color::DarkGray)),
            Span::styled(
                chooser.profile.clone(),
                Style::default().fg(Color::Yellow).bold(),
            ),
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
            Style::default().fg(Color::Cyan).bold(),
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
                Style::default().fg(Color::Cyan),
            ),
            Span::styled(
                format!("{:<16}", source.code),
                if index == chooser.selected {
                    Style::default().fg(Color::White).bg(SELECTED).bold()
                } else {
                    Style::default().fg(Color::Cyan).bold()
                },
            ),
            Span::styled(source.label.clone(), Style::default().fg(Color::White)),
            Span::raw("  "),
            Span::styled(state, state_style.bold()),
            current
                .map(|value| {
                    Span::styled(
                        format!("  {value}"),
                        Style::default().fg(Color::Magenta).bold(),
                    )
                })
                .unwrap_or_else(|| Span::raw("")),
        ]));
        let diagnostic = source
            .detail
            .as_deref()
            .map(|detail| format!(" · {detail}"))
            .unwrap_or_default();
        let diagnostic_style = if source.ok == Some(false) {
            Style::default().fg(Color::Red)
        } else {
            Style::default().fg(Color::DarkGray)
        };
        lines.push(Line::from(vec![
            Span::styled("      ", Style::default().fg(Color::DarkGray)),
            Span::styled(source.url.clone(), Style::default().fg(Color::Gray)),
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

fn render_mirror_confirm(frame: &mut Frame, app: &App, area: Rect) {
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
            Span::styled("TARGET  ", Style::default().fg(Color::Cyan).bold()),
            Span::styled(
                pending.target.clone(),
                Style::default().fg(Color::White).bold(),
            ),
        ]),
        Line::from(format!(
            "Source  {} ({})",
            pending.source.label, pending.source.code
        )),
        Line::from(format!("URL     {}", pending.source.url)),
        Line::from(format!("Provider {}", pending.source.provider)),
        Line::from(""),
        Line::from(Span::styled(
            "Generated assignments",
            Style::default().fg(Color::Cyan).bold(),
        )),
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
            Style::default().fg(Color::DarkGray),
        )));
    } else {
        lines.extend(assignments.into_iter().map(|assignment| {
            Line::from(Span::styled(assignment, Style::default().fg(Color::White)))
        }));
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

fn render_help(frame: &mut Frame, area: Rect) {
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
            Line::from(Span::styled(
                "Home",
                Style::default().fg(Color::Cyan).bold(),
            )),
            Line::from("Status, recommendations, and setup / repair guidance"),
            Line::from(""),
            Line::from(Span::styled(
                "Configure",
                Style::default().fg(Color::Cyan).bold(),
            )),
            Line::from("Software / Search / Mirrors / Settings are secondary views"),
            Line::from("Settings Enter edits host or a config value"),
            Line::from("Enter/Space  preview availability toggle"),
            Line::from("w / i        explain selected software policy"),
            Line::from("/            local text filter (no backend request)"),
            Line::from("Ctrl-U       clear the current filter or query"),
            Line::from("Enter / a    choose group, then preview add"),
            Line::from("Mirror Enter  choose a target, then a chsrc/catalog source"),
            Line::from(""),
            Line::from(Span::styled(
                "Activity",
                Style::default().fg(Color::Cyan).bold(),
            )),
            Line::from("Health / Generations / Operations are secondary views"),
            Line::from("Health Enter opens details; x runs an allow-listed safe action"),
            Line::from("v cycles Activity views; Space marks generations, d compares"),
            Line::from(""),
            Line::from("Every mutation: dry-run → contract validation → explicit confirmation."),
            Line::from("Press ? or Esc to close."),
        ],
        0,
    );
}

fn render_workflow_log(frame: &mut Frame, area: Rect, log: &WorkflowLog) {
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
    let hint = if log.running {
        "↑↓ scroll · End follow"
    } else {
        "↑↓ scroll · Enter/Esc close"
    };
    let title = format!("{} · {} · {}", log.title.to_uppercase(), status, hint);

    let mut lines: Vec<Line<'static>> = log
        .lines
        .iter()
        .map(|line| Line::from(Span::styled(line.clone(), Style::default().fg(Color::Gray))))
        .collect();
    if lines.is_empty() {
        lines.push(Line::from(Span::styled(
            "Waiting for output…",
            Style::default().fg(Color::DarkGray),
        )));
    }

    let scroll = if log.follow {
        lines.len().saturating_sub(viewport)
    } else {
        log.scroll.min(lines.len().saturating_sub(1))
    };
    popup(frame, region, &title, lines, scroll);
}

fn render_overlays(frame: &mut Frame, app: &mut App) {
    let area = frame.area();
    if let Some(log) = &app.workflow_log {
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
                Line::from(Span::styled(error.clone(), Style::default().fg(Color::Red))),
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
                Line::from(Span::styled(
                    action.title(),
                    Style::default().fg(Color::Yellow).bold(),
                )),
                Line::from(""),
                Line::from(format!("Command  {}", action.display_command())),
                Line::from(""),
                Line::from("The TUI will pause and show the complete command output."),
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
                    Span::styled(item.clone(), Style::default().fg(Color::Green).bold()),
                    Span::raw(" was saved to the selected machine policy."),
                ]),
                Line::from(""),
                Line::from("Choose the next step; the policy remains pending until applied."),
                Line::from(""),
                Line::from(vec![
                    Span::styled(
                        " p ",
                        Style::default().fg(Color::Black).bg(Color::Cyan).bold(),
                    ),
                    Span::raw(" Preview     "),
                    Span::styled(
                        " a ",
                        Style::default().fg(Color::Black).bg(Color::Green).bold(),
                    ),
                    Span::raw(" Apply     "),
                    Span::styled(
                        " d ",
                        Style::default().fg(Color::Black).bg(Color::Yellow).bold(),
                    ),
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
                    Style::default().fg(Color::Yellow).bold(),
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
        render_help(frame, area);
    }
}

pub fn draw(frame: &mut Frame, app: &mut App) {
    let layout = Layout::vertical([
        Constraint::Length(3),
        Constraint::Min(4),
        Constraint::Length(1),
    ])
    .split(frame.area());
    frame.render_widget(
        Block::default().style(Style::default().bg(BACKGROUND)),
        frame.area(),
    );
    render_header(frame, app, layout[0]);
    render_body(frame, app, layout[1]);
    render_footer(frame, app, layout[2]);
    render_overlays(frame, app);
}

#[cfg(test)]
mod tests {
    use std::sync::mpsc;

    use ratatui::{backend::TestBackend, buffer::Buffer, Terminal};
    use serde_json::json;

    use super::*;

    fn test_app() -> App {
        let (tx, rx) = mpsc::channel();
        App::new(tx, rx)
    }

    fn row_containing(buffer: &Buffer, needle: &str) -> Option<u16> {
        (0..buffer.area.height).find(|y| {
            let row = (0..buffer.area.width)
                .map(|x| buffer[(x, *y)].symbol())
                .collect::<String>();
            row.contains(needle)
        })
    }

    fn buffer_text(buffer: &Buffer) -> String {
        (0..buffer.area.height)
            .map(|y| {
                (0..buffer.area.width)
                    .map(|x| buffer[(x, y)].symbol())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[test]
    fn scroll_is_clamped_to_last_full_viewport() {
        assert_eq!(row_offset(200, 30, Rect::new(0, 0, 100, 13)), 20);
    }

    #[test]
    fn popup_is_fixed_and_centered_on_tall_terminal() {
        assert_eq!(
            centered_size(94, 18, Rect::new(0, 0, 150, 60)),
            Rect::new(28, 21, 94, 18)
        );
    }

    #[test]
    fn mirror_chooser_window_keeps_a_selected_late_source_visible() {
        assert_eq!(mirror_chooser_source_window(0, 12, 8), (0, 8));
        assert_eq!(mirror_chooser_source_window(7, 12, 8), (3, 11));
        assert_eq!(mirror_chooser_source_window(11, 12, 8), (4, 12));
    }

    #[test]
    fn configure_subview_keeps_three_top_level_sections_visible() {
        let mut app = test_app();
        app.screen = Screen::Mirror;
        let mut terminal = Terminal::new(TestBackend::new(120, 20)).unwrap();
        terminal.draw(|frame| draw(frame, &mut app)).unwrap();

        let text = buffer_text(terminal.backend().buffer());
        assert!(text.contains("Home"));
        assert!(text.contains("Configure"));
        assert!(text.contains("Activity"));
        assert!(text.contains("Software"));
        assert!(text.contains("Mirrors"));
        assert!(text.contains("Settings"));
    }

    #[test]
    fn mirror_throughput_is_displayed_in_megabytes_per_second() {
        assert_eq!(format_throughput(0), "0.00 MB/s");
        assert_eq!(format_throughput(1024 * 1024), "1.00 MB/s");
        assert_eq!(format_throughput(4_850_000), "4.63 MB/s");
    }

    #[test]
    fn ansi_is_removed_from_closure_diff() {
        assert_eq!(
            strip_ansi("envy: \u{1b}[31;1m16 KiB\u{1b}[0m"),
            "envy: 16 KiB"
        );
    }

    #[test]
    fn history_operations_subview_renders_journal() {
        let mut app = test_app();
        app.screen = Screen::History;
        app.history_view = HistoryView::Operations;
        app.pages.insert(
            Screen::Journal,
            crate::model::PageState {
                payload: Some(json!({"entries": [
                    {"timestamp": "2026-07-27T14:52:03+08:00", "operation": "apply",
                     "result": "ok", "durationMs": 12300, "machine": "mac", "detail": {}}
                ]})),
                ..Default::default()
            },
        );
        let mut terminal = Terminal::new(TestBackend::new(120, 20)).unwrap();
        terminal.draw(|frame| draw(frame, &mut app)).unwrap();
        let text = buffer_text(terminal.backend().buffer());
        assert!(text.contains("Operations"));
        assert!(text.contains("apply"));
        assert!(text.contains("12.3s"));
    }

    #[test]
    fn configure_settings_renders_editable_values() {
        let mut app = test_app();
        app.screen = Screen::Config;
        app.pages.insert(
            Screen::Home,
            crate::model::PageState {
                payload: Some(json!({"config": {"device": {"machineId": "mac"}}})),
                ..Default::default()
            },
        );
        app.pages.insert(
            Screen::Hosts,
            crate::model::PageState {
                payload: Some(json!({"machines": [
                    {"platform": "darwin", "machineId": "mac", "current": true, "file": "a"}
                ]})),
                ..Default::default()
            },
        );
        app.pages.insert(
            Screen::Config,
            crate::model::PageState {
                payload: Some(json!({
                    "device": {"machineId": "mac"},
                    "fields": [{"path": "envy.mirrors.mode", "value": "china", "choices": ["upstream", "china"]}]
                })),
                ..Default::default()
            },
        );
        let mut terminal = Terminal::new(TestBackend::new(120, 24)).unwrap();
        terminal.draw(|frame| draw(frame, &mut app)).unwrap();
        let text = buffer_text(terminal.backend().buffer());
        assert!(text.contains("Settings"));
        assert!(text.contains("envy.mirrors.mode"));
    }

    #[test]
    fn software_detail_that_fits_cannot_scroll_into_blank_space() {
        let mut app = test_app();
        app.detail = Some(DetailView::Software(json!({
            "matches": [{
                "group": "nix.system.font",
                "label": "Darwin fonts",
                "item": "MapleMono-NF-CN",
                "name": "MapleMono-NF-CN",
                "included": true,
                "excluded": false,
                "effective": true,
                "externalInclude": true
            }]
        })));
        app.detail_scroll = usize::MAX;
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).unwrap();

        terminal.draw(|frame| draw(frame, &mut app)).unwrap();

        assert_eq!(app.detail_scroll_max, 0);
        assert_eq!(app.detail_scroll, 0);
    }

    #[test]
    fn detail_footer_stays_fixed_at_the_last_valid_scroll_offset() {
        let mut app = test_app();
        app.detail = Some(DetailView::Doctor(json!({
            "section": "test",
            "name": "long details",
            "status": "warn",
            "message": "inspect the complete result",
            "hint": "scroll to the end",
            "details": {"items": (0..40).collect::<Vec<_>>()},
            "action": null
        })));
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).unwrap();
        terminal.draw(|frame| draw(frame, &mut app)).unwrap();
        let footer_row = row_containing(terminal.backend().buffer(), "ENTER").unwrap();
        assert!(buffer_text(terminal.backend().buffer()).contains("scroll  1 /"));
        assert!(app.detail_scroll_max > 0);

        app.detail_scroll = usize::MAX;
        terminal.draw(|frame| draw(frame, &mut app)).unwrap();

        assert_eq!(app.detail_scroll, app.detail_scroll_max);
        assert_eq!(
            row_containing(terminal.backend().buffer(), "ENTER"),
            Some(footer_row)
        );
        assert!(buffer_text(terminal.backend().buffer()).contains(&format!(
            "scroll  {} / {}",
            app.detail_scroll_max + 1,
            app.detail_scroll_max + 1
        )));
    }

    #[test]
    fn narrow_search_keeps_input_and_essential_shortcuts_visible() {
        let mut app = test_app();
        app.screen = Screen::Search;
        app.input_mode = InputMode::Search;
        app.query = "git".to_string();
        let mut terminal = Terminal::new(TestBackend::new(60, 20)).unwrap();

        terminal.draw(|frame| draw(frame, &mut app)).unwrap();

        let rendered = buffer_text(terminal.backend().buffer());
        assert!(rendered.contains("git█"));
        assert!(rendered.contains("Enter submit"));
        assert!(rendered.contains("? help  q quit"));

        app.input_mode = InputMode::Normal;
        terminal.draw(|frame| draw(frame, &mut app)).unwrap();
        let rendered = buffer_text(terminal.backend().buffer());
        assert!(rendered.contains("REGISTRY SEARCH · / edit query"));
        assert!(!rendered.contains("Enter submit"));
    }
}
