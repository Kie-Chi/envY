use std::time::{SystemTime, UNIX_EPOCH};

use ratatui::{
    layout::Flex,
    prelude::*,
    widgets::{
        Block, Cell, Clear, Paragraph, Row, Scrollbar, ScrollbarOrientation, ScrollbarState, Table,
        TableState, Wrap,
    },
};
use serde_json::Value;

use crate::{
    app::{App, InputMode},
    model::{
        journal_entries, mirror_mode, mirror_target_rows, search_entries, text, DetailView,
        HistoryView, NavigationSection, Screen, SoftwareAction, WorkflowLog,
    },
    theme::{self, ACCENT, ERROR, NOTE, OK, WARN},
};

const TOP_BAR_HEIGHT: u16 = 1;
const FOOTER_HEIGHT: u16 = 1;
const NAVIGATION_HEIGHT: u16 = 1;
const HIGHLIGHT_SYMBOL: &str = "▸ ";
pub fn row_offset(scroll: usize, row_count: usize, area: Rect) -> usize {
    let visible_rows = area.height.saturating_sub(3) as usize;
    scroll.min(row_count.saturating_sub(visible_rows))
}

fn selected_state(selected: usize, offset: usize, empty: bool) -> TableState {
    TableState::default().with_selected((!empty).then(|| selected.saturating_sub(offset)))
}

/// A table column: header label and width constraint.
struct Column {
    header: &'static str,
    width: Constraint,
}

const fn column(header: &'static str, width: Constraint) -> Column {
    Column { header, width }
}

/// Display text and style of one table cell.
struct TableCell {
    text: String,
    style: Style,
}

impl TableCell {
    fn new(text: impl Into<String>) -> Self {
        Self::styled(text, Style::new())
    }

    fn styled(text: impl Into<String>, style: Style) -> Self {
        Self {
            text: text.into(),
            style,
        }
    }
}

/// Content of a bordered, scrollable table page.
struct DataTable<'a> {
    title: String,
    /// One-based selected row and row count, shown at the top right.
    position: Option<(usize, usize)>,
    columns: &'a [Column],
    rows: Vec<Vec<TableCell>>,
    selected: usize,
    scroll: usize,
}

/// Width of each rendered column. This mirrors ratatui's own table layout so
/// cells can end in a visible ellipsis instead of being clipped silently.
fn column_widths(columns: &[Column], table_width: u16, selection_width: u16) -> Vec<u16> {
    let [_, cells] = Layout::horizontal([Constraint::Length(selection_width), Constraint::Fill(0)])
        .areas(Rect::new(0, 0, table_width, 1));
    Layout::horizontal(columns.iter().map(|column| column.width))
        .flex(Flex::Start)
        .spacing(1)
        .split(cells)
        .iter()
        .map(|cell| cell.width)
        .collect()
}

/// Fit a value into the given number of terminal cells on one line, marking a
/// cut with an ellipsis.
fn ellipsize(value: &str, width: u16) -> String {
    let value = value.replace(['\r', '\n'], " ");
    let width = usize::from(width);
    if Span::raw(value.as_str()).width() <= width {
        return value;
    }
    let mut result = String::new();
    let mut used = 0;
    for character in value.chars() {
        let mut buffer = [0; 4];
        let character_width = Span::raw(&*character.encode_utf8(&mut buffer)).width();
        if used + character_width >= width {
            break;
        }
        used += character_width;
        result.push(character);
    }
    if width > 0 {
        result.push('…');
    }
    result
}

fn render_table(frame: &mut Frame, area: Rect, table: DataTable) {
    let count = table.rows.len();
    let offset = row_offset(table.scroll, count, area);
    let selection_width = if count == 0 {
        0
    } else {
        Span::raw(HIGHLIGHT_SYMBOL).width() as u16
    };
    let widths = column_widths(table.columns, area.width.saturating_sub(2), selection_width);
    let rows = table
        .rows
        .into_iter()
        .skip(offset)
        .map(|cells| {
            Row::new(
                cells
                    .into_iter()
                    .zip(&widths)
                    .map(|(cell, width)| {
                        Cell::from(ellipsize(&cell.text, *width)).style(cell.style)
                    })
                    .collect::<Vec<_>>(),
            )
        })
        .collect::<Vec<_>>();
    let mut block = theme::panel().title(theme::title(&table.title));
    if let Some((selected, total)) = table.position {
        block = block.title_top(
            Line::styled(format!(" {selected} / {total} "), theme::muted()).right_aligned(),
        );
    }
    let widget = Table::new(rows, table.columns.iter().map(|column| column.width))
        .header(Row::new(table.columns.iter().map(|column| column.header)).style(theme::heading()))
        .row_highlight_style(theme::selection())
        .highlight_symbol(Span::styled(HIGHLIGHT_SYMBOL, Style::new().fg(ACCENT)))
        .block(block);
    let mut state = selected_state(table.selected, offset, count == 0);
    frame.render_stateful_widget(widget, area, &mut state);
    render_scrollbar(frame, area, count, offset);
}

/// Draw a scrollbar on the table's right border when its rows overflow.
fn render_scrollbar(frame: &mut Frame, area: Rect, count: usize, offset: usize) {
    // Skip the top border and header row above the rows and the bottom border.
    let track = Rect {
        y: area.y.saturating_add(2),
        height: area.height.saturating_sub(3),
        ..area
    };
    let visible = usize::from(track.height);
    if visible == 0 || count <= visible {
        return;
    }
    let mut state = ScrollbarState::new(count - visible + 1)
        .position(offset)
        .viewport_content_length(visible);
    frame.render_stateful_widget(
        Scrollbar::new(ScrollbarOrientation::VerticalRight)
            .begin_symbol(None)
            .end_symbol(None)
            .track_symbol(Some("│"))
            .track_style(theme::muted())
            .thumb_symbol("┃")
            .thumb_style(Style::new().fg(ACCENT)),
        track,
        &mut state,
    );
}

fn render_input_field(
    frame: &mut Frame,
    area: Rect,
    title: &str,
    value: &str,
    active: bool,
    placeholder: &str,
) {
    let block = if active {
        theme::focus_panel()
    } else {
        theme::panel()
    }
    .title(format!(" {title} "));
    let content = if value.is_empty() {
        Span::styled(placeholder.to_string(), theme::muted())
    } else {
        Span::raw(value.to_string())
    };
    let line = Line::from(vec![
        Span::styled(" › ", Style::new().fg(ACCENT).bold()),
        content,
        Span::styled(if active { "█" } else { "" }, Style::new().fg(ACCENT)),
    ]);
    frame.render_widget(Paragraph::new(line).block(block), area);
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
            Line::from(Span::styled(title.to_string(), theme::heading())),
            Line::from(""),
            Line::from(message.to_string()),
            Line::from(Span::styled(action.to_string(), theme::muted())),
        ])
        .alignment(Alignment::Center)
        .wrap(Wrap { trim: false })
        .block(theme::panel()),
        rows[1],
    );
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
    let (mode, mode_color) = match app.input_mode {
        InputMode::Normal => ("NORMAL", OK),
        InputMode::Search => ("SEARCH", ACCENT),
        InputMode::SoftwareFilter => ("FILTER", ACCENT),
        InputMode::SettingValue => ("EDIT", ACCENT),
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
    let status = Line::from(vec![
        theme::chip(mode, mode_color),
        Span::raw(format!("  {}", app.status)),
        Span::styled(activity, Style::new().fg(WARN)),
    ]);
    // Show as many hints as fit beside the status; the help hint always stays.
    let room = usize::from(area.width).saturating_sub(status.width() + 3);
    let hints = [
        format!(
            "{}  │  ↑↓ select  Tab section  v view  r refresh  ? help  q quit",
            page_keys(app.screen)
        ),
        format!(
            "{}  │  Tab section  v view  ? help  q quit",
            compact_page_keys(app.screen)
        ),
        format!("{}  │  ? help  q quit", compact_page_keys(app.screen)),
    ]
    .into_iter()
    .find(|hints| Span::raw(hints.as_str()).width() <= room)
    .unwrap_or_else(|| "? help  q quit".to_string());
    let hint_width = (Span::raw(hints.as_str()).width() as u16 + 1).min(area.width);
    let [left, right] =
        Layout::horizontal([Constraint::Fill(1), Constraint::Length(hint_width)]).areas(area);
    frame.render_widget(Paragraph::new(status), left);
    frame.render_widget(
        Paragraph::new(Line::from(hint_spans(&hints))).alignment(Alignment::Right),
        right,
    );
}

/// A label/value line in a Home card.
fn field_line(label: &str, value: impl Into<Span<'static>>) -> Line<'static> {
    Line::from(vec![
        Span::styled(format!(" {label:<11}"), theme::muted()),
        value.into(),
    ])
}

fn machine_lines(payload: &Value) -> Vec<Line<'static>> {
    let config = payload.get("config").unwrap_or(&Value::Null);
    let values = config.get("values").unwrap_or(&Value::Null);
    let machine = config
        .pointer("/device/machineId")
        .or_else(|| payload.get("machine"));
    let user = values
        .get("envy.user.name")
        .or_else(|| values.get("user.name"));
    let platform = config
        .get("platform")
        .or_else(|| values.get("envy.platform"))
        .or_else(|| config.pointer("/device/platform"));
    vec![
        Line::from(Span::styled(format!(" {}", text(machine)), theme::strong())),
        field_line("user", text(user)),
        field_line("platform", text(platform)),
    ]
}

fn repository_lines(git: &Value) -> Vec<Line<'static>> {
    let upstream = git.get("upstream").and_then(Value::as_str);
    let ahead = git.get("ahead").and_then(Value::as_u64).unwrap_or(0);
    let behind = git.get("behind").and_then(Value::as_u64).unwrap_or(0);
    let remote = match upstream {
        None => Span::styled("no upstream", theme::muted()),
        Some(_) if ahead == 0 && behind == 0 => Span::styled("up to date", Style::new().fg(OK)),
        Some(_) => Span::styled(format!("↑{ahead} ↓{behind}"), Style::new().fg(WARN)),
    };
    let worktree = match git.get("changes").and_then(Value::as_u64) {
        Some(0) => Span::styled("clean", Style::new().fg(OK)),
        Some(changes) => Span::styled(format!("{changes} changed"), Style::new().fg(WARN)),
        None => Span::styled(text(git.get("changes")), theme::muted()),
    };
    vec![
        Line::from(vec![
            Span::styled(format!(" {}", text(git.get("branch"))), theme::strong()),
            Span::styled(
                upstream
                    .map(|upstream| format!(" → {upstream}"))
                    .unwrap_or_default(),
                theme::muted(),
            ),
        ]),
        field_line("remote", remote),
        field_line("worktree", worktree),
    ]
}

fn health_lines(doctor: &Value, generation: &Value) -> Vec<Line<'static>> {
    let summary = doctor.get("summary").unwrap_or(&Value::Null);
    let mut counts = vec![Span::raw(" ")];
    for status in ["ok", "warn", "error", "info"] {
        counts.push(match summary.get(status).and_then(Value::as_u64) {
            Some(count) if count > 0 && status != "info" => {
                theme::chip(format!("{count} {status}"), theme::status_color(status))
            }
            _ => Span::styled(
                format!(" {} {status} ", text(summary.get(status))),
                theme::muted(),
            ),
        });
        counts.push(Span::raw(" "));
    }
    let kept = generation
        .get("count")
        .and_then(Value::as_u64)
        .map(|count| format!(" · {count} kept"))
        .unwrap_or_default();
    vec![
        Line::from(counts),
        Line::from(""),
        field_line(
            "generation",
            format!("{}{kept}", text(generation.pointer("/current/number"))),
        ),
    ]
}

fn software_lines(software: &Value) -> Vec<Line<'static>> {
    vec![
        field_line(
            "effective",
            Span::styled(text(software.get("effective")), Style::new().fg(OK).bold()),
        ),
        field_line("included", text(software.get("included"))),
        field_line("excluded", text(software.get("excluded"))),
        field_line("groups", text(software.get("groups"))),
    ]
}

fn recommendation_lines(payload: &Value) -> Vec<Line<'static>> {
    let actions = payload
        .get("recommendations")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|recommendation| {
            let command = recommendation
                .get("command")
                .and_then(Value::as_str)
                .filter(|command| !command.is_empty())?;
            let reason = recommendation
                .get("reason")
                .and_then(Value::as_str)
                .unwrap_or("");
            Some((command.to_string(), reason.to_string()))
        })
        .collect::<Vec<_>>();
    if actions.is_empty() {
        return vec![
            Line::from(vec![
                Span::raw(" "),
                theme::chip("HEALTHY", OK),
                Span::raw("  No action required"),
            ]),
            Line::from(Span::styled(
                " The selected machine is healthy",
                theme::muted(),
            )),
            Line::from(""),
            Line::from(
                [Span::raw(" ")]
                    .into_iter()
                    .chain(hint_spans(
                        "2 Configure software, mirrors, settings  3 Activity health and history",
                    ))
                    .collect::<Vec<_>>(),
            ),
        ];
    }
    let width = actions
        .iter()
        .map(|(command, _)| command.chars().count())
        .max()
        .unwrap_or(0);
    actions
        .into_iter()
        .enumerate()
        .map(|(index, (command, reason))| {
            let (marker, style) = if index == 0 {
                ("▸", Style::new().fg(ACCENT).bold())
            } else {
                (" ", Style::new())
            };
            Line::from(vec![
                Span::styled(format!(" {marker} "), Style::new().fg(ACCENT)),
                Span::styled(format!("{command:<width$}"), style),
                Span::styled(format!("  {reason}"), theme::muted()),
            ])
        })
        .collect()
}

/// Doctor warnings and errors, cut to the card's inner width.
fn attention_lines(doctor: &Value, width: u16) -> Vec<Line<'static>> {
    doctor
        .get("results")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|result| {
            matches!(
                result.get("status").and_then(Value::as_str),
                Some("warn" | "error")
            )
        })
        .map(|result| {
            let status = text(result.get("status"));
            let name = text(result.get("name"));
            let used = Span::raw(name.as_str()).width() + 5;
            let message = ellipsize(
                &text(result.get("message")),
                width.saturating_sub(used.min(u16::MAX as usize) as u16),
            );
            Line::from(vec![
                Span::styled(" ● ", Style::new().fg(theme::status_color(&status))),
                Span::styled(name, theme::strong()),
                Span::styled(format!("  {message}"), theme::muted()),
            ])
        })
        .collect()
}

fn home_card(title: &str, lines: Vec<Line<'static>>) -> Paragraph<'static> {
    Paragraph::new(lines).block(theme::panel().title(theme::title(title)))
}

fn render_home(frame: &mut Frame, app: &App, area: Rect) {
    let payload = app.payload().unwrap_or(&Value::Null);
    let doctor = payload.get("doctor").unwrap_or(&Value::Null);
    let machine = home_card("Machine", machine_lines(payload));
    let repository = home_card(
        "Repository",
        repository_lines(payload.get("git").unwrap_or(&Value::Null)),
    );
    let health = home_card(
        "Health",
        health_lines(doctor, payload.get("generation").unwrap_or(&Value::Null)),
    );
    let software = home_card(
        "Software",
        software_lines(payload.get("software").unwrap_or(&Value::Null)),
    );
    let next_lines = recommendation_lines(payload);
    let next_rows = next_lines.len() as u16;
    let next = home_card("Recommended", next_lines);
    let attention_area = if area.width >= 110 {
        // Keep Recommended at least as tall as the Software card beside it.
        let next_height = next_rows.max(4) + 2;
        let [top, middle, rest] = Layout::vertical([
            Constraint::Length(5),
            Constraint::Length(next_height),
            Constraint::Fill(1),
        ])
        .areas(area);
        let [left, center, right] = Layout::horizontal([Constraint::Ratio(1, 3); 3]).areas(top);
        frame.render_widget(machine, left);
        frame.render_widget(repository, center);
        frame.render_widget(health, right);
        let [left, right] =
            Layout::horizontal([Constraint::Ratio(1, 3), Constraint::Ratio(2, 3)]).areas(middle);
        frame.render_widget(software, left);
        frame.render_widget(next, right);
        rest
    } else {
        let [top, middle, bottom, rest] = Layout::vertical([
            Constraint::Length(5),
            Constraint::Length(6),
            Constraint::Length(next_rows + 2),
            Constraint::Fill(1),
        ])
        .areas(area);
        let [left, right] = Layout::horizontal([Constraint::Ratio(1, 2); 2]).areas(top);
        frame.render_widget(machine, left);
        frame.render_widget(repository, right);
        let [left, right] = Layout::horizontal([Constraint::Ratio(1, 2); 2]).areas(middle);
        frame.render_widget(health, left);
        frame.render_widget(software, right);
        frame.render_widget(next, bottom);
        rest
    };
    let mut attention = attention_lines(doctor, attention_area.width.saturating_sub(2));
    let room = usize::from(attention_area.height.saturating_sub(2));
    if !attention.is_empty() && room > 0 {
        let title = format!("Needs attention · {}", attention.len());
        if attention.len() > room {
            let hidden = attention.len() - room + 1;
            attention.truncate(room - 1);
            attention.push(Line::from(Span::styled(
                format!("   … {hidden} more in 3 Activity"),
                theme::muted(),
            )));
        }
        let height = attention.len() as u16 + 2;
        let [card, _] = Layout::vertical([Constraint::Length(height), Constraint::Fill(1)])
            .areas(attention_area);
        frame.render_widget(home_card(&title, attention), card);
    }
}

const SETTINGS_COLUMNS: [Column; 3] = [
    column("TYPE", Constraint::Length(8)),
    column("SETTING", Constraint::Length(30)),
    column("VALUE", Constraint::Min(20)),
];

fn render_settings(frame: &mut Frame, app: &App, area: Rect) {
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

fn render_sources(frame: &mut Frame, app: &App, area: Rect) {
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

const SOFTWARE_COLUMNS: [Column; 5] = [
    column("GROUP", Constraint::Length(28)),
    column("ITEM", Constraint::Length(22)),
    column("VERSION", Constraint::Length(12)),
    column("STATE", Constraint::Length(12)),
    column("REFERENCE", Constraint::Min(20)),
];

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
    let rows = entries
        .iter()
        .map(|entry| {
            let state_style = match entry.state.as_str() {
                "effective" => Style::new().fg(OK),
                "blocked" => Style::new().fg(ERROR),
                "stale" => Style::new().fg(WARN),
                _ => theme::faint(),
            };
            vec![
                TableCell::new(entry.group.clone()),
                TableCell::new(entry.item.clone()),
                TableCell::new(entry.version.clone()),
                TableCell::styled(entry.state.clone(), state_style),
                TableCell::new(format!("{} · {}", entry.source_id, entry.reference)),
            ]
        })
        .collect();
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
            columns: &SOFTWARE_COLUMNS,
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

const DOCTOR_COLUMNS: [Column; 4] = [
    column("STATUS", Constraint::Length(9)),
    column("SECTION", Constraint::Length(8)),
    column("CHECK", Constraint::Length(30)),
    column("RESULT", Constraint::Min(40)),
];

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
    let rows = results
        .into_iter()
        .flatten()
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
            title: "Doctor · Enter details".to_string(),
            position: Some(app.selection_position().unwrap_or((0, count))),
            columns: &DOCTOR_COLUMNS,
            rows,
            selected: app.doctor_selected,
            scroll: app.scroll,
        },
    );
}

fn render_history(frame: &mut Frame, app: &App, area: Rect) {
    match app.history_view {
        HistoryView::Generations => render_generations(frame, app, area),
        HistoryView::Operations => render_journal(frame, app, area),
    }
}

const GENERATION_COLUMNS: [Column; 5] = [
    column("GENERATION", Constraint::Length(12)),
    column("STATE", Constraint::Length(18)),
    column("CREATED", Constraint::Length(28)),
    column("PROFILE", Constraint::Length(38)),
    column("STORE TARGET", Constraint::Min(40)),
];

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
                TableCell::new(text(generation.get("createdAt"))),
                TableCell::new(text(generation.get("path"))),
                TableCell::new(text(generation.get("target"))),
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
    column("TIME", Constraint::Length(26)),
    column("OPERATION", Constraint::Length(15)),
    column("RESULT", Constraint::Length(8)),
    column("DURATION", Constraint::Length(10)),
    column("MACHINE", Constraint::Length(24)),
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
                TableCell::new(entry.timestamp.clone()),
                TableCell::new(entry.operation.clone()),
                result,
                TableCell::new(entry.duration.clone()),
                TableCell::new(entry.machine.clone()),
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

const MIRROR_COLUMNS: [Column; 2] = [
    column("TARGET", Constraint::Length(18)),
    column("EFFECTIVE SOURCE", Constraint::Min(30)),
];

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
    let mut spans = vec![
        Span::styled(format!(" {title} "), theme::heading()),
        Span::styled("›", theme::muted()),
    ];
    for (index, label) in labels.iter().enumerate() {
        spans.push(Span::raw(" "));
        spans.push(if index == selected {
            Span::styled(format!(" {label} "), theme::selection().fg(ACCENT))
        } else {
            Span::raw(format!(" {label} "))
        });
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
    if let Some(error) = app.current_error().filter(|_| !app.current_has_content()) {
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
                .style(Style::new().fg(WARN))
                .block(theme::panel()),
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

/// Rounded accent frame shared by dialogs.
fn dialog_block(title: &str) -> Block<'static> {
    theme::focus_panel().title(theme::title(title))
}

fn popup(frame: &mut Frame, area: Rect, title: &str, lines: Vec<Line<'static>>, scroll: usize) {
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

fn key_footer(scroll: Option<(usize, usize)>) -> Line<'static> {
    let mut spans = vec![theme::chip("ENTER", ACCENT), Span::raw(" close")];
    if let Some((current, maximum)) = scroll {
        spans.extend([
            Span::raw("    "),
            theme::key("↑↓"),
            Span::raw(format!(" scroll  {} / {}", current + 1, maximum + 1)),
        ]);
    }
    Line::from(spans)
}

fn bool_line(label: &'static str, value: Option<&Value>) -> Line<'static> {
    let enabled = value.and_then(Value::as_bool).unwrap_or(false);
    Line::from(vec![
        Span::styled(format!("{label:<18}"), theme::muted()),
        if enabled {
            Span::styled("yes", Style::new().fg(OK))
        } else {
            Span::raw("no")
        },
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
            Span::styled("ITEM  ", theme::muted()),
            Span::styled(text(matched.get("item")), theme::strong()),
        ]),
        Line::from(format!("Group       {}", text(matched.get("group")))),
        Line::from(format!("Label       {}", text(matched.get("label")))),
        Line::from(format!("Name        {}", text(matched.get("name")))),
        Line::from(format!("Reference   {}", text(matched.get("ref")))),
        Line::from(""),
        Line::from(Span::styled("Evaluated state", theme::heading())),
        bool_line("Included", matched.get("included")),
        bool_line("Excluded", matched.get("excluded")),
        bool_line("Effective", matched.get("effective")),
        Line::from(""),
        Line::from(Span::styled("Ownership", theme::heading())),
        bool_line("Machine include", matched.get("machineInclude")),
        bool_line("Machine exclude", matched.get("machineExclude")),
        bool_line("External include", matched.get("externalInclude")),
        bool_line("External exclude", matched.get("externalExclude")),
        Line::from(""),
        Line::from(Span::styled("Why", theme::heading())),
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
            theme::chip(status.to_uppercase(), theme::status_color(&status)),
            Span::styled(format!("  {}", text(result.get("name"))), theme::strong()),
        ]),
        Line::from(format!("Section  {}", text(result.get("section")))),
        Line::from(""),
        Line::from(Span::styled("Result", theme::heading())),
        Line::from(text(result.get("message"))),
        Line::from(""),
        Line::from(Span::styled("Hint", theme::heading())),
        Line::from(text(result.get("hint"))),
        Line::from(""),
        Line::from(Span::styled("Details", theme::heading())),
    ];
    lines.extend(json_lines(result.get("details")));
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        "Suggested action",
        theme::heading(),
    )));
    lines.extend(json_lines(result.get("action")));
    if result.get("action").is_some_and(|value| !value.is_null()) {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(
            "Press x to run an allow-listed action; envY will return here afterward.",
            Style::new().fg(WARN),
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
            Span::styled(text(before.get("number")), Style::new().fg(WARN).bold()),
            Span::raw("  →  "),
            Span::styled(text(after.get("number")), Style::new().fg(OK).bold()),
        ]),
        Line::from(""),
        Line::from(Span::styled("Before", theme::heading())),
        Line::from(format!("Created  {}", text(before.get("createdAt")))),
        Line::from(format!("Profile  {}", text(before.get("path")))),
        Line::from(format!("Target   {}", text(before.get("target")))),
        Line::from(""),
        Line::from(Span::styled("After", theme::heading())),
        Line::from(format!("Created  {}", text(after.get("createdAt")))),
        Line::from(format!("Profile  {}", text(after.get("path")))),
        Line::from(format!("Target   {}", text(after.get("target")))),
        Line::from(""),
        Line::from(Span::styled("Closure diff", theme::heading())),
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
    let block = dialog_block(title);
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
                    Style::new().fg(WARN).bold(),
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

fn render_group_chooser(frame: &mut Frame, app: &App, area: Rect) {
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

fn render_setting_chooser(frame: &mut Frame, app: &App, area: Rect) {
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

fn render_setting_edit(frame: &mut Frame, app: &App, area: Rect) {
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
            Line::from(Span::styled("Home", theme::heading())),
            Line::from("Status, recommendations, and setup / repair guidance"),
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
        .map(|line| Line::from(Span::styled(line.clone(), Style::new())))
        .collect();
    if lines.is_empty() {
        lines.push(Line::from(Span::styled(
            "Waiting for output…",
            theme::muted(),
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
        render_help(frame, area);
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
    render_body(frame, app, body);
    render_footer(frame, app, footer);
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

    /// Render the whole screen and return it with trailing spaces trimmed.
    fn render(app: &mut App, width: u16, height: u16) -> (String, Buffer) {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal.draw(|frame| draw(frame, app)).unwrap();
        let buffer = terminal.backend().buffer().clone();
        let text = buffer_text(&buffer)
            .lines()
            .map(str::trim_end)
            .collect::<Vec<_>>()
            .join("\n");
        (text, buffer)
    }

    fn assert_snapshot(actual: &str, expected: &str) {
        let expected = expected.strip_prefix('\n').unwrap_or(expected);
        assert!(
            actual == expected,
            "snapshot mismatch\n--- actual ---\n{actual}\n--- expected ---\n{expected}"
        );
    }

    /// Every cell must use the terminal's default or 16-color palette.
    fn assert_terminal_palette(buffer: &Buffer) {
        for cell in &buffer.content {
            for color in [cell.fg, cell.bg] {
                assert!(
                    !matches!(color, Color::Rgb(..) | Color::Indexed(_)),
                    "fixed color {color:?} at symbol {:?}",
                    cell.symbol()
                );
            }
        }
    }

    fn cell_at<'a>(buffer: &'a Buffer, needle: &str) -> &'a ratatui::buffer::Cell {
        let y = row_containing(buffer, needle).expect("needle is rendered");
        let row = (0..buffer.area.width)
            .map(|x| buffer[(x, y)].symbol())
            .collect::<String>();
        let x = row[..row.find(needle).unwrap()].chars().count() as u16;
        &buffer[(x, y)]
    }

    fn home_app(payload: Value) -> App {
        let mut app = test_app();
        app.status = "Home ready".to_string();
        app.pages.insert(
            Screen::Home,
            crate::model::PageState {
                payload: Some(payload),
                ..Default::default()
            },
        );
        app
    }

    fn status_payload(recommendations: Value, results: Value, warn: u64) -> Value {
        json!({
            "machine": "mac",
            "config": {
                "platform": "darwin",
                "device": {"machineId": "mac"},
                "values": {"envy.user.name": "chi"}
            },
            "git": {"branch": "master", "upstream": "origin/master", "ahead": 2, "behind": 0, "changes": 3},
            "generation": {"current": {"number": 112}, "count": 2},
            "software": {"available": 95, "groups": 8, "included": 91, "effective": 82, "excluded": 9},
            "doctor": {"summary": {"ok": 40, "warn": warn, "error": 0, "info": 5}, "results": results},
            "recommendations": recommendations
        })
    }

    #[test]
    fn wide_home_shows_every_recommendation_and_attention_item() {
        let mut app = home_app(status_payload(
            json!([
                {"label": "Plan", "command": "envy plan", "reason": "3 repository path(s) changed"},
                {"label": "Push", "command": "envy push", "reason": "2 commit(s) are ahead of origin/master"}
            ]),
            json!([
                {"status": "ok", "name": "git", "message": "found"},
                {"status": "warn", "name": "GitHub CLI auth", "message": "GitHub CLI is not authenticated"}
            ]),
            1,
        ));
        let (text, buffer) = render(&mut app, 120, 16);
        assert_snapshot(&text, WIDE_HOME);
        assert_terminal_palette(&buffer);
        assert_eq!(cell_at(&buffer, "1 warn").bg, WARN);
        assert_eq!(cell_at(&buffer, "3 changed").fg, WARN);
    }

    #[test]
    fn narrow_healthy_home_stacks_cards_and_reports_no_action() {
        let mut app = home_app(status_payload(json!([]), json!([]), 0));
        let (text, buffer) = render(&mut app, 80, 19);
        assert_snapshot(&text, NARROW_HEALTHY_HOME);
        assert_terminal_palette(&buffer);
        assert_eq!(cell_at(&buffer, "HEALTHY").bg, OK);
    }

    fn software_app(count: usize) -> App {
        let items = (0..count)
            .map(|index| {
                json!({
                    "id": format!("pkg-{index}"),
                    "sourceId": "nix:nixpkgs",
                    "version": "1.0",
                    "effective": index != 1,
                    "ref": format!("nix:a-very-long-attribute-path-for-package-number-{index}")
                })
            })
            .collect::<Vec<_>>();
        let mut app = test_app();
        app.screen = Screen::Software;
        app.status = "Software ready".to_string();
        app.pages.insert(
            Screen::Software,
            crate::model::PageState {
                payload: Some(json!({"groups": [
                    {"id": "nix.user.package", "label": "Nix user packages", "items": items}
                ]})),
                ..Default::default()
            },
        );
        app
    }

    #[test]
    fn software_table_ellipsizes_long_cells_and_shows_a_scrollbar() {
        let mut app = software_app(6);
        let (text, buffer) = render(&mut app, 90, 10);
        assert_snapshot(&text, SOFTWARE_TABLE);
        assert_terminal_palette(&buffer);
        let selected = cell_at(&buffer, "effective");
        assert_eq!(selected.bg, theme::MUTED, "selection bar is drawn");
        assert_eq!(selected.fg, OK, "selection keeps the cell color");
    }

    #[test]
    fn page_height_matches_the_rendered_table_rows() {
        for (height, input) in [(12, false), (30, false), (16, true)] {
            let mut app = software_app(40);
            if input {
                app.software_filter = "pkg".to_string();
            }
            app.clamp_scroll(page_height(&app, height));
            let (text, _) = render(&mut app, 90, height);
            let rows = text.lines().filter(|line| line.contains("pkg-")).count();
            assert_eq!(app.visible_rows, rows, "terminal height {height}");
        }
        let mut app = test_app();
        assert_eq!(page_height(&app, 40), 38, "Home has no section navigation");
        app.screen = Screen::Search;
        assert_eq!(page_height(&app, 40), 34);
    }

    #[test]
    fn doctor_table_marks_each_status() {
        let mut app = test_app();
        app.screen = Screen::Doctor;
        app.status = "Doctor ready".to_string();
        app.pages.insert(
            Screen::Doctor,
            crate::model::PageState {
                payload: Some(json!({"results": [
                    {"status": "ok", "section": "conf", "name": "machine file", "message": "found"},
                    {"status": "warn", "section": "apps", "name": "GitHub CLI auth", "message": "not authenticated"},
                    {"status": "error", "section": "secr", "name": "decrypt", "message": "sops decrypt failed"}
                ]})),
                ..Default::default()
            },
        );
        let (text, buffer) = render(&mut app, 100, 9);
        assert_snapshot(&text, DOCTOR_TABLE);
        assert_terminal_palette(&buffer);
        assert_eq!(cell_at(&buffer, "● error").fg, ERROR);
    }

    #[test]
    fn ellipsize_counts_terminal_cells() {
        assert_eq!(ellipsize("short", 8), "short");
        assert_eq!(ellipsize("abcdefgh", 5), "abcd…");
        assert_eq!(ellipsize("中文字符", 5), "中文…");
        assert_eq!(ellipsize("line\nbreak", 20), "line break");
        assert_eq!(ellipsize("abc", 0), "");
    }

    const WIDE_HOME: &str = r#"
 envY   1 Home   2 Configure   3 Activity                                                      mac · master ● 3 changed
╭ Machine ─────────────────────────────╮╭ Repository ──────────────────────────╮╭ Health ──────────────────────────────╮
│ mac                                  ││ master → origin/master               ││  40 ok   1 warn   0 error   5 info   │
│ user       chi                       ││ remote     ↑2 ↓0                     ││                                      │
│ platform   darwin                    ││ worktree   3 changed                 ││ generation 112 · 2 kept              │
╰──────────────────────────────────────╯╰──────────────────────────────────────╯╰──────────────────────────────────────╯
╭ Software ────────────────────────────╮╭ Recommended ─────────────────────────────────────────────────────────────────╮
│ effective  82                        ││ ▸ envy plan  3 repository path(s) changed                                    │
│ included   91                        ││   envy push  2 commit(s) are ahead of origin/master                          │
│ excluded   9                         ││                                                                              │
│ groups     8                         ││                                                                              │
╰──────────────────────────────────────╯╰──────────────────────────────────────────────────────────────────────────────╯
╭ Needs attention · 1 ─────────────────────────────────────────────────────────────────────────────────────────────────╮
│ ● GitHub CLI auth  GitHub CLI is not authenticated                                                                   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
 NORMAL   Home ready               2 Configure  3 Activity  │  ↑↓ select  Tab section  v view  r refresh  ? help  q quit"#;
    const NARROW_HEALTHY_HOME: &str = r#"
 envY   1 Home   2 Configure   3 Activity              mac · master ● 3 changed
╭ Machine ─────────────────────────────╮╭ Repository ──────────────────────────╮
│ mac                                  ││ master → origin/master               │
│ user       chi                       ││ remote     ↑2 ↓0                     │
│ platform   darwin                    ││ worktree   3 changed                 │
╰──────────────────────────────────────╯╰──────────────────────────────────────╯
╭ Health ──────────────────────────────╮╭ Software ────────────────────────────╮
│  40 ok   0 warn   0 error   5 info   ││ effective  82                        │
│                                      ││ included   91                        │
│ generation 112 · 2 kept              ││ excluded   9                         │
│                                      ││ groups     8                         │
╰──────────────────────────────────────╯╰──────────────────────────────────────╯
╭ Recommended ─────────────────────────────────────────────────────────────────╮
│  HEALTHY   No action required                                                │
│ The selected machine is healthy                                              │
│                                                                              │
│ 2 Configure software, mirrors, settings  3 Activity health and history       │
╰──────────────────────────────────────────────────────────────────────────────╯
 NORMAL   Home ready                  2 Configure  3 Activity  │  ? help  q quit"#;
    const SOFTWARE_TABLE: &str = r#"
 envY   1 Home   2 Configure   3 Activity
 Configure ›  Software   Search   Sources   Mirrors   Settings              v switch view
╭ Software policy · 6 items ────────────────────────────────────────────────────── 1 / 6 ╮
│  GROUP               ITEM                VERSION      STATE        REFERENCE           │
│▸ nix.user.package    pkg-0               1.0          effective    nix:nixpkgs · nix:a…┃
│  nix.user.package    pkg-1               1.0          excluded     nix:nixpkgs · nix:a…┃
│  nix.user.package    pkg-2               1.0          effective    nix:nixpkgs · nix:a…┃
│  nix.user.package    pkg-3               1.0          effective    nix:nixpkgs · nix:a…│
╰────────────────────────────────────────────────────────────────────────────────────────╯
 NORMAL   Software ready                      ↵ toggle  w why  / filter  │  ? help  q quit"#;
    const DOCTOR_TABLE: &str = r#"
 envY   1 Home   2 Configure   3 Activity
 Activity ›  Health   Generations   Operations                                        v switch view
╭ Doctor · Enter details ─────────────────────────────────────────────────────────────────── 1 / 3 ╮
│  STATUS    SECTION  CHECK                          RESULT                                        │
│▸ ● ok      conf     machine file                   found                                         │
│  ● warn    apps     GitHub CLI auth                not authenticated                             │
│  ● error   secr     decrypt                        sops decrypt failed                           │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
 NORMAL   Doctor ready   Enter details  │  ↑↓ select  Tab section  v view  r refresh  ? help  q quit"#;
}
