//! The Home page cards.

use ratatui::{prelude::*, widgets::Paragraph};
use serde_json::Value;

use crate::{
    app::App,
    model::{
        journal_entries, parse_timestamp, relative_age, short_timestamp, text, JournalEntry, Screen,
    },
    theme::{self, ACCENT, ERROR, OK, WARN},
};

use super::hint_spans;
use super::table::ellipsize;

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

fn health_lines(doctor: &Value, generation: &Value, now: i64) -> Vec<Line<'static>> {
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
    let activated = generation
        .pointer("/current/createdAt")
        .and_then(Value::as_str)
        .and_then(parse_timestamp)
        .map(|then| Span::raw(relative_age(then, now)))
        .unwrap_or_else(|| Span::styled("—", theme::muted()));
    vec![
        Line::from(counts),
        field_line(
            "generation",
            format!("{}{kept}", text(generation.pointer("/current/number"))),
        ),
        field_line("activated", activated),
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

/// The newest operations from the journal, one per line.
fn activity_lines(entries: &[JournalEntry], width: u16, now: i64) -> Vec<Line<'static>> {
    let operation_width = entries
        .iter()
        .map(|entry| entry.operation.chars().count())
        .max()
        .unwrap_or(0)
        .min(14);
    entries
        .iter()
        .map(|entry| {
            let color = if entry.result == "ok" { OK } else { ERROR };
            let age = parse_timestamp(&entry.timestamp)
                .map(|then| relative_age(then, now))
                .unwrap_or_else(|| short_timestamp(&entry.timestamp));
            let operation = format!(
                "{:<operation_width$}",
                ellipsize(&entry.operation, operation_width as u16)
            );
            let facts = format!("  {:<4}  {:>7}  {age:<10}  ", entry.result, entry.duration);
            let used =
                3 + Span::raw(operation.as_str()).width() + Span::raw(facts.as_str()).width();
            let room = width.saturating_sub(used.min(usize::from(u16::MAX)) as u16);
            let detail = if entry.detail == "—" {
                ""
            } else {
                entry.detail.as_str()
            };
            Line::from(vec![
                Span::styled(" ● ", Style::new().fg(color)),
                Span::raw(operation),
                Span::styled(facts, theme::muted()),
                Span::styled(ellipsize(detail, room), theme::faint()),
            ])
        })
        .collect()
}

fn home_card(title: &str, lines: Vec<Line<'static>>) -> Paragraph<'static> {
    Paragraph::new(lines).block(theme::panel().title(theme::title(title)))
}

pub(super) fn render_home(frame: &mut Frame, app: &App, area: Rect) {
    let payload = app.payload().unwrap_or(&Value::Null);
    let doctor = payload.get("doctor").unwrap_or(&Value::Null);
    let machine = home_card("Machine", machine_lines(payload));
    let repository = home_card(
        "Repository",
        repository_lines(payload.get("git").unwrap_or(&Value::Null)),
    );
    let health = home_card(
        "Health",
        health_lines(
            doctor,
            payload.get("generation").unwrap_or(&Value::Null),
            app.now(),
        ),
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
    let activity_area = if !attention.is_empty() && room > 0 {
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
        let [card, rest] = Layout::vertical([Constraint::Length(height), Constraint::Fill(1)])
            .areas(attention_area);
        frame.render_widget(home_card(&title, attention), card);
        rest
    } else {
        attention_area
    };
    render_recent_activity(frame, app, activity_area);
}

const RECENT_ACTIVITY_LIMIT: usize = 5;

/// The newest journal entries in whatever room Home has left.
fn render_recent_activity(frame: &mut Frame, app: &App, area: Rect) {
    let journal = app
        .pages
        .get(&Screen::Journal)
        .and_then(|page| page.payload.as_ref());
    let mut entries = journal_entries(journal);
    let room = usize::from(area.height.saturating_sub(2)).min(RECENT_ACTIVITY_LIMIT);
    if entries.is_empty() || room == 0 {
        return;
    }
    entries.truncate(room);
    let lines = activity_lines(&entries, area.width.saturating_sub(2), app.now());
    let [card, _] = Layout::vertical([
        Constraint::Length(lines.len() as u16 + 2),
        Constraint::Fill(1),
    ])
    .areas(area);
    frame.render_widget(home_card("Recent operations", lines), card);
}
