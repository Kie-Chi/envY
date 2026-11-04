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
    assert!(cell_at(&buffer, "10 min ago")
        .modifier
        .contains(Modifier::DIM));
    assert!(cell_at(&buffer, "ypk8n1d1")
        .modifier
        .contains(Modifier::DIM));
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
    assert_eq!(selected.bg, theme::MUTED, "selection bar is drawn");
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
    assert!(cell_at(&buffer, "nix:nixpkgs")
        .modifier
        .contains(Modifier::DIM));
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
│ excluded   9                         ││                                                                              │
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
 NORMAL   Home ready                  2 Configure  3 Activity  │  ? help  q quit"#;
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
 Activity ›  Health   Generations   Operations                                                            v switch view
╭ Generations · Space mark · d diff ──────────────────────────────────────────────────────────────────────────── 1 / 2 ╮
│  GENERATION STATE            CREATED                  STORE TARGET                                                   │
│▸ 113        current          10-29 03:40  10 min ago  darwin-system-26.05.56c666e  ypk8n1d1                          │
│  112                         10-27 00:48  2 d ago     darwin-system-26.05.0246b24  01234567                          │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
 NORMAL   History ready         v view  Space mark  d diff  │  ↑↓ select  Tab section  v view  r refresh  ? help  q quit"#;
const JOURNAL_TABLE: &str = r#"
 envY   1 Home   2 Configure   3 Activity
 Activity ›  Health   Generations   Operations                                        v switch view
╭ Operation journal ──────────────────────────────────────────────────────────────────────── 1 / 3 ╮
│  TIME                     OPERATION      RESULT   DURATION  DETAIL                               │
│▸ 10-29 03:40  10 min ago  apply          ok       34.3s     —                                    │
│  10-29 01:12  2 h ago     push           ok       11.4s     branch=master  remote=all            │
│  10-26 22:00  2 d ago     sync           fail     3m 32s    error=merge conflict in flake.lock   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
 NORMAL   History ready           v view  Space mark  d diff  │  Tab section  v view  ? help  q quit"#;
const DOCTOR_TABLE: &str = r#"
 envY   1 Home   2 Configure   3 Activity
 Activity ›  Health 2   Generations   Operations                                      v switch view
╭ Doctor · problems first ────────────────────────────────────────────────────────────────── 1 / 3 ╮
│  STATUS    SECTION  CHECK                          RESULT                                        │
│▸ ● error   secr     decrypt                        sops decrypt failed                           │
│  ● warn    apps     GitHub CLI auth                not authenticated                             │
│  ● ok      conf     machine file                   found                                         │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
 NORMAL   Doctor ready                   ↵ details  f filter  │  Tab section  v view  ? help  q quit"#;
