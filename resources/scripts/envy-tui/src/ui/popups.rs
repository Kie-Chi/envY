//! Dialogs and overlays drawn above the current page.

use ratatui::{
    prelude::*,
    widgets::{Block, Clear, Paragraph, Wrap},
};
use serde_json::Value;

use crate::{
    app::App,
    model::{text, DetailView, SoftwareAction, WorkflowLog},
    theme::{self, ACCENT, ERROR, NOTE, OK, WARN},
};

use super::spinner;
use super::table::render_input_field;

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

/// A detail value on one line: scalars as text, lists of scalars joined.
fn detail_value(value: &Value) -> String {
    match value {
        Value::Array(items)
            if items
                .iter()
                .all(|item| !item.is_array() && !item.is_object()) =>
        {
            items
                .iter()
                .map(|item| text(Some(item)))
                .collect::<Vec<_>>()
                .join(", ")
        }
        Value::Array(_) | Value::Object(_) => value.to_string(),
        scalar => text(Some(scalar)),
    }
}

/// What a doctor action would do, in words.
fn action_text(action: &Value) -> String {
    let field = |key| action.get(key).and_then(Value::as_str).unwrap_or("—");
    match action.get("kind").and_then(Value::as_str) {
        Some("run") => format!(
            "Run {}",
            action
                .get("argv")
                .and_then(Value::as_array)
                .map(|argv| argv
                    .iter()
                    .map(|part| text(Some(part)))
                    .collect::<Vec<_>>()
                    .join(" "))
                .unwrap_or_default()
        ),
        Some("open-app") => format!("Open {}", field("name")),
        Some("open-settings") => format!("Open System Settings › {}", field("target")),
        _ => action.to_string(),
    }
}

fn doctor_detail_lines(result: &Value) -> Vec<Line<'static>> {
    let status = text(result.get("status"));
    let mut lines = vec![
        Line::from(vec![
            theme::chip(status.to_uppercase(), theme::status_color(&status)),
            Span::styled(format!("  {}", text(result.get("name"))), theme::strong()),
        ]),
        Line::from(vec![
            Span::styled("Section  ", theme::muted()),
            Span::raw(text(result.get("section"))),
        ]),
    ];
    let mut section = |title: &str, body: Vec<Line<'static>>| {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(
            title.to_string(),
            theme::heading(),
        )));
        lines.extend(body);
    };
    section("Result", vec![Line::from(text(result.get("message")))]);
    if let Some(hint) = result
        .get("hint")
        .and_then(Value::as_str)
        .filter(|hint| !hint.is_empty())
    {
        section("Hint", vec![Line::from(hint.to_string())]);
    }
    match result.get("details") {
        Some(Value::Object(details)) if !details.is_empty() => {
            let width = details
                .keys()
                .map(|key| key.chars().count())
                .max()
                .unwrap_or(0)
                .min(20);
            section(
                "Details",
                details
                    .iter()
                    .map(|(key, value)| {
                        Line::from(vec![
                            Span::styled(format!("{key:<width$}  "), theme::muted()),
                            Span::raw(detail_value(value)),
                        ])
                    })
                    .collect(),
            );
        }
        Some(Value::Object(_)) | Some(Value::Null) | None => {}
        Some(other) => section("Details", vec![Line::from(detail_value(other))]),
    }
    if let Some(action) = result.get("action").filter(|action| !action.is_null()) {
        let mut body = vec![Line::from(action_text(action))];
        if App::doctor_workflow(result).is_some() {
            body.push(Line::from(Span::styled(
                "Press x to run it; envY returns here afterward.",
                Style::new().fg(WARN),
            )));
        }
        section("Suggested action", body);
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
            Line::from("Health f shows only warnings and errors"),
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

pub(super) fn render_overlays(frame: &mut Frame, app: &mut App) {
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

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn plain(lines: &[Line]) -> String {
        lines
            .iter()
            .map(ToString::to_string)
            .collect::<Vec<_>>()
            .join("\n")
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
    fn doctor_details_hide_empty_sections_and_offer_only_runnable_actions() {
        let mut result = json!({
            "status": "warn", "section": "apps", "name": "GitHub CLI auth",
            "message": "not authenticated", "hint": "",
            "details": {"command": "gh", "paths": ["/a", "/b"], "exit": {"code": 1}},
            "action": {"kind": "run", "argv": ["gh", "auth", "login"]}
        });
        let text = plain(&doctor_detail_lines(&result));
        assert!(!text.contains("Hint"));
        assert!(text.contains("command  gh"));
        assert!(text.contains("paths    /a, /b"));
        assert!(text.contains(r#"exit     {"code":1}"#));
        assert!(text.contains("Run gh auth login"));
        assert!(!text.contains("Press x"), "only allow-listed actions run");

        result["details"] = json!({});
        result["action"] = json!({"kind": "run", "argv": ["envy", "apply"]});
        let text = plain(&doctor_detail_lines(&result));
        assert!(!text.contains("Details"));
        assert!(text.contains("Run envy apply") && text.contains("Press x"));

        result["action"] = Value::Null;
        result["hint"] = json!("run gh auth login");
        let text = plain(&doctor_detail_lines(&result));
        assert!(text.contains("Hint\nrun gh auth login"));
        assert!(!text.contains("Suggested action"));
    }
}
