use std::sync::mpsc;

use ratatui::{backend::TestBackend, buffer::Buffer, Terminal};
use serde_json::json;

use super::*;
use crate::model::DetailView;

/// 2026-10-29T03:50:44+08:00, a little over three hours after the
/// sample generation was activated.
const NOW: i64 = 1_793_206_124 + 3 * 3600 + 120;

fn test_app() -> App {
    let (tx, rx) = mpsc::channel();
    let mut app = App::new(tx, rx);
    app.fixed_now = Some(NOW);
    app
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

fn settings_app(payload: Value, selected: usize) -> App {
    let mut app = test_app();
    app.screen = Screen::Config;
    app.settings_selected = selected;
    app.pages.insert(
        Screen::Config,
        crate::model::PageState {
            payload: Some(payload),
            ..Default::default()
        },
    );
    app
}

#[test]
fn settings_show_backend_metadata_and_distinguish_same_labels() {
    let mut app = settings_app(
        json!({"fields": [
            {"path": "envy.agents.first.model", "prompt": "Model", "value": "first"},
            {"path": "envy.agents.second.model", "prompt": "Model", "value": "second",
             "group": "AGENTS", "nixType": "string", "required": true,
             "active": null, "conditionState": "unknown", "changeKind": "live",
             "activationHint": "Backend applicability explanation", "choices": ["second", "third"]}
        ]}),
        2,
    );
    let mut terminal = Terminal::new(TestBackend::new(120, 30)).unwrap();
    terminal
        .draw(|frame| configure::render_settings(frame, &app, frame.area()))
        .unwrap();
    let text = buffer_text(terminal.backend().buffer());
    for expected in [
        "envy.agents.second.model",
        "AGENTS",
        "string",
        "required",
        "unknown",
        "live",
        "Backend applicability explanation",
        "Options: second · third",
    ] {
        assert!(text.contains(expected), "Missing {expected}:\n{text}");
    }
    use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
    app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    let chooser = app.setting_chooser.as_ref().unwrap();
    assert_eq!(
        chooser.key,
        crate::model::SettingKey::Config("envy.agents.second.model".into())
    );
    assert!(chooser.title.contains("envy.agents.second.model"));
    app.handle_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
    app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    assert_eq!(app.pending_setting.as_ref().unwrap().value, "third");
}

#[test]
fn settings_keep_selection_above_help_at_narrow_sizes_and_end_of_list() {
    let fields = (0..35)
        .map(|index| {
            json!({
                "path": format!("envy.test.field{index}"),
                "prompt": if index == 34 { "Last visible field".to_string() }
                          else { "Long 中文 label with no short alternative ".repeat(8) },
                "value": format!("v{index}"),
                "active": false, "conditionState": "inactive",
                "activationHint": "Backend says retained"
            })
        })
        .collect::<Vec<_>>();
    let mut app = settings_app(json!({"fields": fields}), 35);
    // The page scroll was calculated using a larger viewport than the table.
    app.scroll = 5;
    for (width, height) in [(48, 18), (80, 24), (120, 30)] {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal
            .draw(|frame| configure::render_settings(frame, &app, frame.area()))
            .unwrap();
        let buffer = terminal.backend().buffer();
        let selected =
            row_containing(buffer, "Last visible field").expect("selected label visible");
        let help = row_containing(buffer, "Selected setting").expect("help panel visible");
        assert!(selected < help, "selected row must remain in the table");
        assert!(buffer_text(buffer).contains("v34"));
    }
    for (width, height) in [(1, 1), (10, 4), (20, 8)] {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal
            .draw(|frame| configure::render_settings(frame, &app, frame.area()))
            .unwrap();
    }
}

#[test]
fn settings_secrets_render_only_status_and_cannot_open_an_editor() {
    let mut app = settings_app(
        json!({"secretFields": [
            {"path": "proxy/url", "prompt": "Proxy credential", "set": true,
             "value": "never-render-this-secret", "choices": ["never-render-this-secret"]},
            {"path": "other/token", "set": false}
        ]}),
        1,
    );
    let mut terminal = Terminal::new(TestBackend::new(120, 24)).unwrap();
    terminal
        .draw(|frame| configure::render_settings(frame, &app, frame.area()))
        .unwrap();
    let text = buffer_text(terminal.backend().buffer());
    assert!(text.contains("Proxy credential [secret]"));
    assert!(text.contains("unset"));
    assert!(text.contains("Secret status only"));
    assert!(!text.contains("never-render-this-secret"));
    use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
    app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    assert!(app.setting_edit.is_none());
    assert!(app.setting_chooser.is_none());
    assert!(app.pending_setting.is_none());
    assert_eq!(app.input_mode, crate::app::InputMode::Normal);
}

#[test]
fn settings_legacy_text_fields_and_host_help_still_work() {
    let mut app = settings_app(
        json!({"fields": [
            {"path": "envy.user.name", "value": "old-name"}
        ]}),
        0,
    );
    let mut terminal = Terminal::new(TestBackend::new(100, 24)).unwrap();
    terminal
        .draw(|frame| configure::render_settings(frame, &app, frame.area()))
        .unwrap();
    assert!(buffer_text(terminal.backend().buffer()).contains("Select the local machine target"));
    app.settings_selected = 1;
    use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
    app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    let edit = app.setting_edit.as_ref().unwrap();
    assert_eq!(edit.buffer, "old-name");
    assert_eq!(
        edit.key,
        crate::model::SettingKey::Config("envy.user.name".into())
    );
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
        "details": (0..40)
            .map(|index| (format!("item{index}"), json!(index)))
            .collect::<serde_json::Map<_, _>>(),
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
        "generation": {"current": {"number": 112, "createdAt": "2026-10-29T00:48:44+08:00"}, "count": 2},
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
    app.pages.insert(
        Screen::Journal,
        crate::model::PageState {
            payload: Some(journal_payload()),
            ..Default::default()
        },
    );
    let (text, buffer) = render(&mut app, 120, 21);
    assert_snapshot(&text, WIDE_HOME);
    assert_terminal_palette(&buffer);
    assert_eq!(cell_at(&buffer, "1 warn").bg, WARN);
    assert_eq!(cell_at(&buffer, "3 changed").fg, WARN);
    let failed = row_containing(&buffer, "sync").unwrap();
    assert_eq!(buffer[(2, failed)].fg, ERROR, "failed operation is marked");
}

#[test]
fn restored_home_names_its_age() {
    let mut app = home_app(status_payload(json!([]), json!([]), 0));
    app.pages.get_mut(&Screen::Home).unwrap().saved_at = Some(NOW - 600);
    let (text, _) = render(&mut app, 120, 16);
    assert!(text
        .lines()
        .next()
        .unwrap()
        .ends_with("saved 10 min ago · mac · master ● 3 changed"));
}

fn journal_payload() -> Value {
    json!({"entries": [
        {"timestamp": "2026-10-29T03:40:00+08:00", "operation": "apply", "result": "ok",
         "durationMs": 34319, "machine": "mac", "detail": {}},
        {"timestamp": "2026-10-29T01:12:05+08:00", "operation": "push", "result": "ok",
         "durationMs": 11431, "machine": "mac", "detail": {"remote": "all", "branch": "master"}},
        {"timestamp": "2026-10-26T22:00:00+08:00", "operation": "sync", "result": "fail",
         "durationMs": 212525, "machine": "mac", "detail": {"error": "merge conflict in flake.lock"}}
    ]})
}

#[test]
fn activity_tables_show_local_times_with_their_age() {
    let mut app = test_app();
    app.screen = Screen::History;
    app.status = "History ready".to_string();
    app.pages.insert(
        Screen::History,
        crate::model::PageState {
            payload: Some(json!({"generations": [
                {"number": 113, "current": true, "createdAt": "2026-10-29T03:40:00+08:00",
                 "path": "/nix/var/nix/profiles/system-113-link",
                 "target": "/nix/store/ypk8n1d1bbnzfy53lzbv5f1frqp36rvx-darwin-system-26.05.56c666e"},
                {"number": 112, "current": false, "createdAt": "2026-10-27T00:48:44+08:00",
                 "path": "/nix/var/nix/profiles/system-112-link",
                 "target": "/nix/store/0123456789abcdfghijklmnpqrsvwxyz-darwin-system-26.05.0246b24"}
            ]})),
            ..Default::default()
        },
    );
    let (text, buffer) = render(&mut app, 120, 8);
    assert_snapshot(&text, GENERATIONS_TABLE);
    let age = cell_at(&buffer, "10 min ago");
    assert_eq!(age.fg, Color::Reset);
    assert!(!age.modifier.contains(Modifier::DIM));
    let target = cell_at(&buffer, "ypk8n1d1");
    assert_eq!(target.fg, Color::Reset);
    assert!(!target.modifier.contains(Modifier::DIM));
    assert!(!cell_at(&buffer, "darwin-system")
        .modifier
        .contains(Modifier::DIM));

    app.history_view = HistoryView::Operations;
    app.pages.insert(
        Screen::Journal,
        crate::model::PageState {
            payload: Some(journal_payload()),
            ..Default::default()
        },
    );
    let (text, buffer) = render(&mut app, 100, 9);
    assert_snapshot(&text, JOURNAL_TABLE);
    assert_eq!(cell_at(&buffer, "fail").fg, ERROR);
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
    assert_eq!(selected.bg, theme::SUBTLE, "selection bar is drawn");
    assert_eq!(selected.fg, OK, "selection keeps the cell color");
}

#[test]
fn software_columns_follow_the_width_and_groups_are_named_once() {
    let mut app = software_app(40);
    app.scroll = 10;
    app.software_selected = 12;
    let (text, _) = render(&mut app, 80, 10);
    assert!(!text.contains("VERSION") && !text.contains("SOURCE"));
    let rows = text
        .lines()
        .filter(|line| line.contains("pkg-"))
        .collect::<Vec<_>>();
    assert!(rows[0].contains("pkg-10") && rows[0].contains("nix.user.package"));
    assert!(rows[1..]
        .iter()
        .all(|row| !row.contains("nix.user.package")));

    let (text, buffer) = render(&mut app, 130, 10);
    assert!(text.contains("VERSION") && text.contains("SOURCE"));
    let source = cell_at(&buffer, "nix:nixpkgs");
    assert_eq!(source.fg, Color::Reset);
    assert!(!source.modifier.contains(Modifier::DIM));
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
            payload: Some(json!({
                "summary": {"ok": 1, "warn": 1, "error": 1, "info": 0},
                "results": [
                    {"status": "ok", "section": "conf", "name": "machine file", "message": "found"},
                    {"status": "warn", "section": "apps", "name": "GitHub CLI auth", "message": "not authenticated"},
                    {"status": "error", "section": "secr", "name": "decrypt", "message": "sops decrypt failed"}
                ]
            })),
            ..Default::default()
        },
    );
    let (text, buffer) = render(&mut app, 100, 9);
    assert_snapshot(&text, DOCTOR_TABLE);
    assert_terminal_palette(&buffer);
    assert_eq!(cell_at(&buffer, "● error").fg, ERROR);
    let nav = row_containing(&buffer, "Health 2").unwrap();
    let badge = (0..buffer.area.width)
        .find(|x| buffer[(*x, nav)].symbol() == "2")
        .unwrap();
    assert_eq!(
        buffer[(badge, nav)].fg,
        ERROR,
        "badge takes the worst status"
    );

    app.doctor_problems_only = true;
    app.pages.get_mut(&Screen::Doctor).unwrap().payload = Some(json!({
        "summary": {"ok": 1, "warn": 0, "error": 0, "info": 0},
        "results": [{"status": "ok", "section": "conf", "name": "machine file", "message": "found"}]
    }));
    let (text, _) = render(&mut app, 100, 12);
    assert!(text.contains("No warnings or errors") && text.contains("Press f to show all"));
    assert!(!text.contains("Health 0"), "no badge without problems");
}

const WIDE_HOME: &str = r#"
 envY   1 Home   2 Configure   3 Activity                                                      mac · master ● 3 changed
╭ Machine ─────────────────────────────╮╭ Repository ──────────────────────────╮╭ Health ──────────────────────────────╮
│ mac                                  ││ master → origin/master               ││  40 ok   1 warn   0 error   5 info   │
│ user       chi                       ││ remote     ↑2 ↓0                     ││ generation 112 · 2 kept              │
│ platform   darwin                    ││ worktree   3 changed                 ││ activated  3 h ago                   │
╰──────────────────────────────────────╯╰──────────────────────────────────────╯╰──────────────────────────────────────╯
╭ Software ────────────────────────────╮╭ Recommended ─────────────────────────────────────────────────────────────────╮
│ effective  82                        ││ ▸ envy plan  3 repository path(s) changed                                    │
│ included   91                        ││   envy push  2 commit(s) are ahead of origin/master                          │
│ excluded   9                         ││  t  Storage · local reports and explicit projects                            │
│ groups     8                         ││                                                                              │
╰──────────────────────────────────────╯╰──────────────────────────────────────────────────────────────────────────────╯
╭ Needs attention · 1 ─────────────────────────────────────────────────────────────────────────────────────────────────╮
│ ● GitHub CLI auth  GitHub CLI is not authenticated                                                                   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
╭ Recent operations ───────────────────────────────────────────────────────────────────────────────────────────────────╮
│ ● apply  ok      34.3s  10 min ago                                                                                   │
│ ● push   ok      11.4s  2 h ago     branch=master  remote=all                                                        │
│ ● sync   fail   3m 32s  2 d ago     error=merge conflict in flake.lock                                               │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
 NORMAL   Home ready    t Storage  2 Configure  3 Activity  │  ↑↓ select  Tab section  v view  r refresh  ? help  q quit"#;
const NARROW_HEALTHY_HOME: &str = r#"
 envY   1 Home   2 Configure   3 Activity              mac · master ● 3 changed
╭ Machine ─────────────────────────────╮╭ Repository ──────────────────────────╮
│ mac                                  ││ master → origin/master               │
│ user       chi                       ││ remote     ↑2 ↓0                     │
│ platform   darwin                    ││ worktree   3 changed                 │
╰──────────────────────────────────────╯╰──────────────────────────────────────╯
╭ Health ──────────────────────────────╮╭ Software ────────────────────────────╮
│  40 ok   0 warn   0 error   5 info   ││ effective  82                        │
│ generation 112 · 2 kept              ││ included   91                        │
│ activated  3 h ago                   ││ excluded   9                         │
│                                      ││ groups     8                         │
╰──────────────────────────────────────╯╰──────────────────────────────────────╯
╭ Recommended ─────────────────────────────────────────────────────────────────╮
│  HEALTHY   No action required                                                │
│ The selected machine is healthy                                              │
│                                                                              │
│ 2 Configure software, mirrors, settings  3 Activity health and history       │
╰──────────────────────────────────────────────────────────────────────────────╯
 NORMAL   Home ready       t Storage  2 Configure  3 Activity  │  ? help  q quit"#;
const SOFTWARE_TABLE: &str = r#"
 envY   1 Home   2 Configure   3 Activity
 Configure ›  Software   Search   Sources   Mirrors   Settings              v switch view
╭ Software policy · 6 items ────────────────────────────────────────────────────── 1 / 6 ╮
│  GROUP              ITEM    STATE       REFERENCE                                      │
│▸ nix.user.package   pkg-0   effective   nix:a-very-long-attribute-path-for-package-num…┃
│                     pkg-1   excluded    nix:a-very-long-attribute-path-for-package-num…┃
│                     pkg-2   effective   nix:a-very-long-attribute-path-for-package-num…┃
│                     pkg-3   effective   nix:a-very-long-attribute-path-for-package-num…│
╰────────────────────────────────────────────────────────────────────────────────────────╯
 NORMAL   Software ready                      ↵ toggle  w why  / filter  │  ? help  q quit"#;
const GENERATIONS_TABLE: &str = r#"
 envY   1 Home   2 Configure   3 Activity
 Activity ›  Health   Generations   Operations   Storage                                                  v switch view
╭ Generations · Enter details · Space mark · d diff ──────────────────────────────────────────────────────────── 1 / 2 ╮
│  GENERATION STATE            CREATED                  STORE TARGET                                                   │
│▸ 113        current          10-29 03:40  10 min ago  darwin-system-26.05.56c666e  ypk8n1d1                          │
│  112                         10-27 00:48  2 d ago     darwin-system-26.05.0246b24  01234567                          │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
 NORMAL   History ready                Enter details  v view  Space mark  d diff  │  Tab section  v view  ? help  q quit"#;
const JOURNAL_TABLE: &str = r#"
 envY   1 Home   2 Configure   3 Activity
 Activity ›  Health   Generations   Operations   Storage                              v switch view
╭ Operation journal · Enter receipt ──────────────────────────────────────────────────────── 1 / 3 ╮
│  TIME                     OPERATION      RESULT   DURATION  DETAIL                               │
│▸ 10-29 03:40  10 min ago  apply          ok       34.3s     —                                    │
│  10-29 01:12  2 h ago     push           ok       11.4s     branch=master  remote=all            │
│  10-26 22:00  2 d ago     sync           fail     3m 32s    error=merge conflict in flake.lock   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
 NORMAL   History ready                 Enter details  v view  Space mark  d diff  │  ? help  q quit"#;
const DOCTOR_TABLE: &str = r#"
 envY   1 Home   2 Configure   3 Activity
 Activity ›  Health 2   Generations   Operations   Storage                            v switch view
╭ Doctor · problems first ────────────────────────────────────────────────────────────────── 1 / 3 ╮
│  STATUS    SECTION  CHECK                          RESULT                                        │
│▸ ● error   secr     decrypt                        sops decrypt failed                           │
│  ● warn    apps     GitHub CLI auth                not authenticated                             │
│  ● ok      conf     machine file                   found                                         │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
 NORMAL   Doctor ready                   ↵ details  f filter  │  Tab section  v view  ? help  q quit"#;

fn storage_app() -> App {
    let mut app = test_app();
    app.screen = Screen::Storage;
    let payload: Value =
        serde_json::from_str(include_str!("../storage/fixtures/inspect-darwin.json")).unwrap();
    let report =
        crate::storage::StorageReport::from_payload(crate::storage::StorageView::Inspect, payload);
    app.status = report.summary();
    app.storage_cache.insert(
        app.storage_request(),
        crate::model::PageState {
            storage_report: Some(report),
            received_at: Some(NOW - 600),
            ..Default::default()
        },
    );
    app
}

#[test]
fn storage_entry_report_age_and_scope_are_visible_in_the_integrated_page() {
    let mut app = storage_app();
    app.storage_projects = vec!["/explicit/a  project/子目录".into()];
    let state = app
        .storage_cache
        .remove(&crate::storage::StorageRequest::default())
        .unwrap();
    app.storage_cache.insert(app.storage_request(), state);
    let (screen, buffer) = render(&mut app, 180, 28);
    for text in [
        "Activity",
        "Storage",
        "Inspection",
        "Policy & records",
        "Rule validation",
        "Stale",
        "received 10 min ago",
        "/explicit/a  project/",
        "codex-data",
        "unknown",
    ] {
        assert!(screen.contains(text), "missing {text}");
    }
    assert_terminal_palette(&buffer);
    assert_eq!(app.current_state().unwrap().received_at, Some(NOW - 600));
    assert!(!screen.contains("--yes"));
    assert!(!screen.contains("plan/run"));
}

#[test]
fn storage_project_input_and_context_help_keep_read_effects_and_keys_visible() {
    let mut app = storage_app();
    app.handle_key(crossterm::event::KeyEvent::new(
        crossterm::event::KeyCode::Char('p'),
        crossterm::event::KeyModifiers::NONE,
    ));
    app.handle_paste("/tmp/a  path/中文");
    let (screen, _) = render(&mut app, 120, 24);
    assert!(screen.contains("PROJECT DIRECTORY"));
    assert!(screen.contains("/tmp/a  path/"));
    assert!(screen.contains('中') && screen.contains('文'));
    assert_eq!(app.storage_project_input, "/tmp/a  path/中文");
    assert!(screen.contains("Enter add"));
    app.handle_key(crossterm::event::KeyEvent::new(
        crossterm::event::KeyCode::Esc,
        crossterm::event::KeyModifiers::NONE,
    ));
    app.show_help = true;
    let (help, _) = render(&mut app, 110, 32);
    for text in [
        "STORAGE",
        "remove last project",
        "initialize and lock",
        "temporary organize",
        "Quarantine still occupies",
        "cached reports never rescan",
    ] {
        assert!(help.contains(text), "missing help: {text}");
    }
}

#[test]
fn storage_resize_selection_and_full_json_detail_stay_scrollable() {
    let mut app = storage_app();
    app.storage_selected = app.row_count() - 1;
    for (width, height) in [(180, 28), (80, 15), (38, 10)] {
        let table_height = page_height(&app, height);
        app.clamp_scroll(table_height);
        let (_, buffer) = render(&mut app, width, height);
        assert!(app.storage_selected >= app.scroll);
        assert!(app.storage_selected < app.scroll + app.visible_rows);
        assert_terminal_palette(&buffer);
    }
    app.handle_key(crossterm::event::KeyEvent::new(
        crossterm::event::KeyCode::Enter,
        crossterm::event::KeyModifiers::NONE,
    ));
    let (screen, _) = render(&mut app, 100, 22);
    assert!(screen.contains("STORAGE DETAILS"));
    assert!(app.detail_scroll_max > 0);
    app.detail_scroll = usize::MAX;
    let (_, _) = render(&mut app, 80, 18);
    assert_eq!(app.detail_scroll, app.detail_scroll_max);
}


fn workflow_report(payload: Value) -> crate::model::WorkflowReport {
    use std::os::unix::process::ExitStatusExt;
    crate::model::WorkflowReport::from_result(Ok(crate::transport::JsonResponse {
        payload, status: std::process::ExitStatus::from_raw(0), stderr: "freeze\nbuild\ncompare\n".into(),
    }))
}

#[test]
fn workflow_details_wrap_long_paths_and_keep_full_json_reachable_at_narrow_sizes() {
    let mut payload: Value = serde_json::from_str(include_str!("../workflow/fixtures/plan-v1.json")).unwrap();
    payload["data"]["source"]["frozenPath"] = json!(format!("/nix/store/{}-fixture-source", "long-path-".repeat(25)));
    payload["z"] = json!("END");
    for (width, height) in [(28,12),(50,18),(100,30)] {
        let mut app = test_app();
        app.detail = Some(DetailView::Report { title:"PLAN".into(),report:workflow_report(payload.clone()),selected:None });
        render(&mut app,width,height);
        let maximum = app.detail_scroll_max;
        assert!(maximum > 100);
        app.detail_scroll = usize::MAX;
        let (text,_) = render(&mut app,width,height);
        assert_eq!(app.detail_scroll,maximum);
        assert!(text.contains(r#""z": "END""#), "{width}x{height}: {text}");
        assert!(text.contains("ENTER"));
    }
    for (width,height) in [(1,1),(2,2),(8,5)] {
        let mut app = test_app();
        app.detail = Some(DetailView::Report {title:"PLAN".into(),report:workflow_report(payload.clone()),selected:None});
        render(&mut app,width,height);
    }
}

#[test]
fn completed_plan_report_scroll_uses_wrapped_lines_instead_of_progress_line_count() {
    let mut payload: Value = serde_json::from_str(include_str!("../workflow/fixtures/plan-v1.json")).unwrap();
    payload["z"] = json!("END");
    let mut app = test_app();
    app.workflow_log = Some(crate::model::WorkflowLog {title:"PLAN".into(),lines:vec![],running:false,
        success:Some(true),scroll:usize::MAX,follow:false,json:true,scroll_max:0,report:Some(workflow_report(payload))});
    let (text,_) = render(&mut app,50,18);
    let log = app.workflow_log.as_ref().unwrap();
    assert!(log.scroll_max > 100);
    assert_eq!(log.scroll,log.scroll_max);
    assert!(text.contains(r#""z": "END""#));
}
