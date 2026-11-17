mod activity;
mod choosers;
mod configure;
mod detail;
mod home;
mod popups;
mod storage;
mod table;
#[cfg(test)]
mod tests;

use std::time::{SystemTime, UNIX_EPOCH};

use ratatui::{
    prelude::*,
    widgets::{Clear, Paragraph, Wrap},
};
use serde_json::Value;

use crate::{
    app::{App, InputMode},
    model::{HistoryView, NavigationSection, Screen},
    theme::{self, ACCENT, ERROR, OK, WARN},
};

use activity::{render_doctor, render_history};
use configure::{render_mirror, render_search, render_settings, render_software, render_sources};
use home::render_home;
use popups::render_overlays;

const TOP_BAR_HEIGHT: u16 = 1;
const FOOTER_HEIGHT: u16 = 1;
const NAVIGATION_HEIGHT: u16 = 1;
pub fn row_offset(scroll: usize, row_count: usize, area: Rect) -> usize {
    let visible_rows = area.height.saturating_sub(3) as usize;
    scroll.min(row_count.saturating_sub(visible_rows))
}

/// Machine and repository context shown on every page once Home has loaded.
fn top_bar_context(app: &App) -> Line<'static> {
    let Some(payload) = app
        .pages
        .get(&Screen::Home)
        .and_then(|page| page.payload.as_ref())
    else {
        return Line::default();
    };
    let git = payload.get("git").unwrap_or(&Value::Null);
    let mut spans = Vec::new();
    if let Some(age) = app.saved_age(Screen::Home) {
        spans.push(Span::styled(format!("saved {age} · "), theme::muted()));
    }
    if let Some(machine) = payload
        .pointer("/config/device/machineId")
        .or_else(|| payload.get("machine"))
        .and_then(Value::as_str)
    {
        spans.push(Span::styled(machine.to_string(), theme::muted()));
    }
    if let Some(branch) = git.get("branch").and_then(Value::as_str) {
        if !spans.is_empty() {
            spans.push(Span::styled(" · ", theme::muted()));
        }
        spans.push(Span::raw(branch.to_string()));
    }
    let changes = git.get("changes").and_then(Value::as_u64).unwrap_or(0);
    if changes > 0 {
        spans.push(Span::styled(
            format!(" ● {changes} changed"),
            Style::new().fg(WARN),
        ));
    }
    if !spans.is_empty() {
        spans.push(Span::raw(" "));
    }
    Line::from(spans)
}

fn render_top_bar(frame: &mut Frame, app: &App, area: Rect) {
    let current = app.screen.section();
    let mut spans = vec![Span::styled(" envY ", Style::new().fg(ACCENT).bold())];
    for (index, section) in NavigationSection::ALL.iter().enumerate() {
        spans.push(Span::raw(" "));
        if *section == current {
            spans.push(theme::chip(
                format!("{} {}", index + 1, section.title()),
                ACCENT,
            ));
        } else {
            spans.push(Span::styled(format!(" {} ", index + 1), theme::muted()));
            spans.push(Span::raw(format!("{} ", section.title())));
        }
    }
    let tabs = Line::from(spans);
    let context = top_bar_context(app);
    let context_width = if tabs.width() + context.width() + 2 <= usize::from(area.width) {
        context.width() as u16
    } else {
        0
    };
    let [left, right] =
        Layout::horizontal([Constraint::Fill(1), Constraint::Length(context_width)]).areas(area);
    frame.render_widget(Paragraph::new(tabs), left);
    frame.render_widget(Paragraph::new(context), right);
}

fn page_keys(app: &App) -> &'static str {
    match app.screen {
        Screen::Sources => "↵ select  s sync",
        Screen::Software => "↵ toggle  w why  / filter",
        Screen::Search => "↵ add  / search",
        Screen::Doctor => "↵ details  f problems",
        Screen::History if app.history_view == HistoryView::Operations => "↵ receipt",
        Screen::History => "↵ details  Space mark  d diff",
        Screen::Storage => "[ ] view  p project  ↵ details  a actions",
        Screen::Home => "t Storage",
        Screen::Config => "↵ edit",
        Screen::Mirror => "↵ source",
        _ => "↵ details",
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

/// Style "key label" hint pairs: keys in the accent color, labels dimmed.
fn hint_spans(hints: &str) -> Vec<Span<'static>> {
    let mut spans = Vec::new();
    for (index, part) in hints
        .split("  ")
        .filter(|part| !part.is_empty())
        .enumerate()
    {
        if index > 0 {
            spans.push(Span::raw("  "));
        }
        match part.split_once(' ') {
            Some((key, label)) => {
                spans.push(Span::styled(
                    key.to_string(),
                    Style::new().fg(ACCENT).bold(),
                ));
                spans.push(Span::styled(format!(" {label}"), theme::muted()));
            }
            None => spans.push(Span::styled(part.to_string(), theme::muted())),
        }
    }
    spans
}

fn render_footer(frame: &mut Frame, app: &App, area: Rect) {
    if app.storage_actions.open {
        let actions = &app.storage_actions;
        let line = format!("{} / {}  ·  ↑↓ scroll  ·  q back", actions.scroll + 1, actions.scroll_max + 1);
        frame.render_widget(Paragraph::new(Line::from(hint_spans(&line))).alignment(Alignment::Right), area);
        return;
    }
    let mode = match app.input_mode {
        InputMode::Normal => None,
        InputMode::Search => Some("SEARCH"), InputMode::SoftwareFilter => Some("FILTER"),
        InputMode::SettingValue => Some("EDIT"), InputMode::StorageProject => Some("PROJECT"),
    };
    let busy = if app.setting_loading { Some("Saving…") }
        else if app.mutation_loading { Some("Preparing change…") }
        else if app.mirror_loading { Some("Loading sources…") }
        else if app.mirror_measuring { Some("Measuring…") }
        else if app.loading() { Some(if app.current_has_content() { "Refreshing…" } else { "Loading…" }) }
        else { None };
    let display_title = match app.screen {
        Screen::Config => "Settings", Screen::Doctor => "Health",
        Screen::History if app.history_view == HistoryView::Operations => "Operations",
        Screen::History => "Generations", _ => app.screen.title(),
    };
    let steady = [format!("{} ready", app.screen.title()), format!("{} cached", app.screen.title()),
        format!("{display_title} ready"), format!("{display_title} cached")];
    let label = if let Some(busy) = busy { format!("{} {busy}", spinner()) }
        else if steady.contains(&app.status) || app.status == "✓ Storage ready" { String::new() }
        else { app.status.clone() };
    let color = if app.current_error().is_some() || app.overlay_error.is_some() { ERROR }
        else if label.starts_with("! Storage ·") { WARN }
        else if label.starts_with('!') { ERROR }
        else if label.starts_with('✓') { OK } else if busy.is_some() { WARN } else { theme::MUTED };
    // Reserve space for the page action before fitting a long notification.
    let status_budget = (Span::raw(label.as_str()).width() as u16 + if mode.is_some() { 9 } else { 0 }).min(area.width / 3);
    let room = area.width.saturating_sub(status_budget + 2) as usize;
    let primary = match app.input_mode {
        InputMode::Normal => page_keys(app),
        InputMode::Search => "↵ search  Esc cancel",
        InputMode::SettingValue => "↵ review  Esc cancel",
        InputMode::StorageProject => "↵ add  Esc cancel",
        InputMode::SoftwareFilter => "↵ keep  Esc cancel",
    };
    let hints = [
        format!("{primary}  r refresh  ? help  q quit"),
        format!("{primary}  ? help  q quit"),
        format!("{primary}  ? help"),
        "? help  q quit".into(),
    ].into_iter().find(|hint| Span::raw(hint.as_str()).width() <= room).unwrap_or_else(|| "? help".into());
    let hint_width = (Span::raw(hints.as_str()).width() as u16).min(area.width);
    let [left, right] = Layout::horizontal([Constraint::Fill(1), Constraint::Length(hint_width)]).areas(area);
    let mut status = Vec::new();
    if let Some(mode) = mode { status.extend([theme::chip(mode, ACCENT), Span::raw(" ")]); }
    let used = status.iter().map(Span::width).sum::<usize>() as u16;
    status.push(Span::styled(table::ellipsize(&label, left.width.saturating_sub(used + 1)), Style::new().fg(color)));
    frame.render_widget(Paragraph::new(Line::from(status)), left);
    frame.render_widget(Paragraph::new(Line::from(hint_spans(&hints))).alignment(Alignment::Right), right);
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
                (Screen::Storage, _) => 3,
                (Screen::History, HistoryView::Generations) => 1,
                (Screen::History, HistoryView::Operations) | (Screen::Journal, _) => 2,
                _ => 0,
            };
            (
                "Activity",
                ["Health", "Generations", "Operations", "Storage"].as_slice(),
                selected,
            )
        }
    };
    // Health carries its warning and error count.
    let problems = (app.screen.section() == NavigationSection::Activity)
        .then(|| app.doctor_summary())
        .flatten()
        .map(|summary| {
            let count = |status| summary.get(status).and_then(Value::as_u64).unwrap_or(0);
            (count("warn") + count("error"), count("error") > 0)
        })
        .filter(|(count, _)| *count > 0);
    let mut spans = vec![
        Span::styled(format!(" {title} "), theme::heading()),
        Span::styled("›", theme::muted()),
    ];
    for (index, label) in labels.iter().enumerate() {
        let style = if index == selected {
            theme::selection().fg(ACCENT)
        } else {
            Style::new()
        };
        spans.push(Span::raw(" "));
        spans.push(Span::styled(format!(" {label} "), style));
        if let (0, Some((count, errors))) = (index, problems) {
            let color = if errors { ERROR } else { WARN };
            spans.push(Span::styled(format!("{count} "), style.fg(color)));
        }
    }
    let tabs = Line::from(spans);
    let hint = Line::from(hint_spans("v switch view"));
    let hint_width = if tabs.width() + hint.width() + 3 <= usize::from(area.width) {
        hint.width() as u16 + 1
    } else {
        0
    };
    let [left, right] =
        Layout::horizontal([Constraint::Fill(1), Constraint::Length(hint_width)]).areas(area);
    frame.render_widget(Paragraph::new(tabs), left);
    frame.render_widget(Paragraph::new(hint), right);
}

fn render_body(frame: &mut Frame, app: &App, area: Rect) {
    let content_area = if app.screen.section() == NavigationSection::Home {
        area
    } else {
        let [navigation, content] =
            Layout::vertical([Constraint::Length(NAVIGATION_HEIGHT), Constraint::Min(1)])
                .areas(area);
        render_section_navigation(frame, app, navigation);
        content
    };
    if let Some(error) = app
        .current_error()
        .filter(|_| !app.current_has_content() && app.screen != Screen::Storage)
    {
        frame.render_widget(
            Paragraph::new(vec![
                Line::from(Span::styled(
                    "Unable to load data",
                    Style::new().fg(ERROR).bold(),
                )),
                Line::from(""),
                Line::from(error),
                Line::from(""),
                Line::from("Press r to retry or q to quit."),
            ])
            .wrap(Wrap { trim: false })
            .block(
                theme::panel()
                    .title(theme::title("Error"))
                    .border_style(Style::new().fg(ERROR)),
            ),
            content_area,
        );
        return;
    }
    match app.screen {
        Screen::Home => render_home(frame, app, content_area),
        Screen::Storage => storage::render_storage(
            frame,
            content_area,
            storage::StoragePage {
                view: app.storage_view,
                report: app.storage_report(),
                selected: app.storage_selected,
                scroll: app.scroll,
                loading: app.loading(),
                error: app.current_error(),
                projects: &app.storage_projects,
                project_input: (app.input_mode == InputMode::StorageProject)
                    .then_some(app.storage_project_input.as_str()),
                timing: storage::StorageTiming {
                    received_at: app.current_state().and_then(|state| state.received_at),
                    saved_at: app.current_state().and_then(|state| state.saved_at),
                    now: app.now(),
                },
            },
        ),
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
    if app.loading()
        && !app.current_has_content()
        && !matches!(app.screen, Screen::Search | Screen::Storage)
    {
        frame.render_widget(Clear, content_area);
        frame.render_widget(
            Paragraph::new(format!("  {} Loading complete envY data…", spinner()))
                .style(Style::new().fg(WARN))
                .block(theme::panel()),
            content_area,
        );
    }
}

/// Rows left for the current page after the top bar, footer, section
/// navigation, and input bar. App::clamp_scroll then removes the table border
/// and header. Keep this in sync with draw and render_body.
pub fn page_height(app: &App, terminal_height: u16) -> u16 {
    let navigation = if app.screen.section() == NavigationSection::Home {
        0
    } else {
        NAVIGATION_HEIGHT
    };
    terminal_height
        .saturating_sub(TOP_BAR_HEIGHT + FOOTER_HEIGHT + navigation)
        .saturating_sub(app.input_bar_height())
}

pub fn draw(frame: &mut Frame, app: &mut App) {
    let [top_bar, body, footer] = Layout::vertical([
        Constraint::Length(TOP_BAR_HEIGHT),
        Constraint::Min(1),
        Constraint::Length(FOOTER_HEIGHT),
    ])
    .areas(frame.area());
    render_top_bar(frame, app, top_bar);
    if app.storage_actions.open {
        storage::render_actions(frame, body, &mut app.storage_actions);
        render_footer(frame, app, footer);
        return;
    }
    render_body(frame, app, body);
    render_footer(frame, app, footer);
    render_overlays(frame, app);
}
