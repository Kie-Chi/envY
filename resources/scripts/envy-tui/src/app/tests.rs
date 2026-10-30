use std::sync::mpsc;

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use serde_json::json;

use super::*;
use crate::model::{MirrorSource, MutationIntent, MutationStage, SettingKey, SoftwareAction};

fn app() -> App {
    let (tx, rx) = mpsc::channel();
    App::new(tx, rx)
}

#[test]
fn only_missing_forced_or_restored_pages_need_a_load() {
    let loaded = PageState {
        payload: Some(json!({})),
        ..PageState::default()
    };
    assert!(PageState::default().needs_load(false));
    assert!(!loaded.needs_load(false));
    assert!(loaded.needs_load(true));
    let restored = PageState {
        saved_at: Some(1),
        ..loaded
    };
    assert!(restored.needs_load(false));
    let in_flight = PageState {
        loading: true,
        ..restored
    };
    assert!(!in_flight.needs_load(true));
}

#[test]
fn restored_home_is_shown_until_a_fresh_status_replaces_it() {
    let root = crate::snapshot::tests::temporary_dir("restore");
    let path = root.join("home.json");
    crate::snapshot::save(&path, &json!({"machine": "old"}), 1_000).unwrap();
    let mut app = app();
    app.snapshot_path = Some(path.clone());
    app.fixed_now = Some(1_000 + 3 * 3600);
    app.restore_home_snapshot();
    assert_eq!(app.payload(), Some(&json!({"machine": "old"})));
    assert_eq!(app.saved_age(Screen::Home).as_deref(), Some("3 h ago"));

    // Pretend the refresh started by load_current is in flight.
    let state = app.pages.get_mut(&Screen::Home).unwrap();
    state.loading = true;
    state.request = Some(7);
    app.sync_status();
    assert_eq!(app.status, "Showing home from 3 h ago");
    app.tx
        .send(Message::Loaded {
            request: 7,
            target: LoadTarget::Screen(Screen::Home),
            payload: json!({"machine": "new"}),
        })
        .unwrap();
    app.receive_messages();
    assert_eq!(app.payload(), Some(&json!({"machine": "new"})));
    assert_eq!(app.saved_age(Screen::Home), None);
    assert_eq!(
        crate::snapshot::load(&path),
        Some((json!({"machine": "new"}), 1_000 + 3 * 3600))
    );
    std::fs::remove_dir_all(root).unwrap();
}

fn failed(request: u64, screen: Screen, error: &str) -> Message {
    Message::Failed {
        request,
        target: LoadTarget::Screen(screen),
        error: error.to_string(),
    }
}

#[test]
fn failed_refresh_keeps_the_restored_home_and_names_its_age() {
    let root = crate::snapshot::tests::temporary_dir("refresh-failed");
    let path = root.join("home.json");
    crate::snapshot::save(&path, &json!({"machine": "old"}), 1_000).unwrap();
    let mut app = app();
    app.snapshot_path = Some(path.clone());
    app.fixed_now = Some(1_000 + 3 * 3600);
    app.restore_home_snapshot();
    let state = app.pages.get_mut(&Screen::Home).unwrap();
    state.loading = true;
    state.request = Some(7);
    app.sync_status();

    // A failure for an older request must not end the refresh in flight.
    app.tx.send(failed(6, Screen::Home, "stale")).unwrap();
    app.receive_messages();
    assert!(app.loading());
    assert_eq!(app.status, "Showing home from 3 h ago");

    app.tx
        .send(failed(7, Screen::Home, "nix eval failed"))
        .unwrap();
    app.receive_messages();
    assert!(!app.loading());
    assert_eq!(app.status, "Home refresh failed; showing data from 3 h ago");
    assert_eq!(app.current_error(), Some("nix eval failed"));
    assert_eq!(app.payload(), Some(&json!({"machine": "old"})));
    assert_eq!(app.saved_age(Screen::Home).as_deref(), Some("3 h ago"));
    // The saved copy is still the last good one, and the next visit retries.
    assert_eq!(
        crate::snapshot::load(&path),
        Some((json!({"machine": "old"}), 1_000))
    );
    assert!(app.current_state().unwrap().needs_load(false));
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn failed_refresh_of_a_loaded_page_keeps_its_data() {
    let mut app = app();
    app.screen = Screen::Doctor;
    app.pages.insert(
        Screen::Doctor,
        PageState {
            payload: Some(json!({"summary": {"ok": 1}})),
            loading: true,
            request: Some(3),
            ..PageState::default()
        },
    );
    app.sync_status();
    assert_eq!(app.status, "Refreshing doctor");

    app.tx
        .send(failed(3, Screen::Doctor, "doctor crashed"))
        .unwrap();
    app.receive_messages();

    assert_eq!(app.status, "Doctor refresh failed; showing cached data");
    assert_eq!(app.payload(), Some(&json!({"summary": {"ok": 1}})));
    assert!(!app.current_state().unwrap().needs_load(false));
}

#[test]
fn health_filter_and_details_follow_the_displayed_order() {
    let mut app = app();
    app.screen = Screen::Doctor;
    app.pages.insert(
        Screen::Doctor,
        PageState {
            payload: Some(json!({"results": [
                {"name": "fine", "status": "ok"},
                {"name": "gh", "status": "warn"},
                {"name": "sops", "status": "error"}
            ]})),
            ..PageState::default()
        },
    );
    app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    assert!(matches!(&app.detail, Some(DetailView::Doctor(result)) if result["name"] == "sops"));
    app.detail = None;

    app.handle_key(KeyEvent::new(KeyCode::Char('f'), KeyModifiers::NONE));
    assert_eq!(app.row_count(), 2);
    assert_eq!(app.status, "Showing 2 warning(s) and error(s)");
    app.handle_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
    app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    assert!(matches!(&app.detail, Some(DetailView::Doctor(result)) if result["name"] == "gh"));
    app.detail = None;

    app.handle_key(KeyEvent::new(KeyCode::Char('f'), KeyModifiers::NONE));
    assert_eq!((app.row_count(), app.doctor_selected), (3, 0));
    assert_eq!(app.status, "Showing all 3 checks");
}

#[test]
fn cached_page_does_not_start_another_request() {
    let mut app = app();
    app.pages.insert(
        Screen::Software,
        PageState {
            payload: Some(json!({"groups": []})),
            ..PageState::default()
        },
    );
    app.set_screen(Screen::Software);
    assert_eq!(app.request_id, 0);
    assert_eq!(app.status, "Software cached");
}

#[test]
fn history_operations_subview_reads_journal_payload() {
    let mut app = app();
    app.pages.insert(
        Screen::Journal,
        PageState {
            payload: Some(json!({"entries": [
                {"operation": "apply", "result": "ok"},
                {"operation": "push", "result": "fail"}
            ]})),
            ..PageState::default()
        },
    );
    app.screen = Screen::History;
    app.history_view = HistoryView::Operations;
    assert_eq!(app.row_count(), 2);
    app.visible_rows = 5;
    app.move_selection(1);
    assert_eq!(app.journal_selected, 1);
}

#[test]
fn activity_subview_v_cycles_health_generations_and_operations() {
    let mut app = app();
    app.screen = Screen::History;
    app.activity_screen = Screen::History;
    assert_eq!(app.history_view, HistoryView::Generations);

    app.handle_key(KeyEvent::new(KeyCode::Char('v'), KeyModifiers::NONE));
    assert_eq!(app.history_view, HistoryView::Operations);

    app.handle_key(KeyEvent::new(KeyCode::Char('v'), KeyModifiers::NONE));
    assert_eq!(app.screen, Screen::Doctor);

    app.handle_key(KeyEvent::new(KeyCode::Char('v'), KeyModifiers::NONE));
    assert_eq!(app.screen, Screen::History);
    assert_eq!(app.history_view, HistoryView::Generations);
}

#[test]
fn top_level_navigation_has_three_sections() {
    assert_eq!(
        NavigationSection::ALL,
        [
            NavigationSection::Home,
            NavigationSection::Configure,
            NavigationSection::Activity,
        ]
    );
    assert_eq!(Screen::Home.section(), NavigationSection::Home);
    assert_eq!(Screen::Config.section(), NavigationSection::Configure);
    assert_eq!(Screen::History.section(), NavigationSection::Activity);
}

#[test]
fn section_navigation_preserves_the_last_leaf_view() {
    let mut app = app();
    app.set_screen(Screen::Config);
    app.set_section(NavigationSection::Activity);
    assert_eq!(app.screen, Screen::History);
    app.set_section(NavigationSection::Configure);
    assert_eq!(app.screen, Screen::Config);
}

#[test]
fn home_does_not_preload_configure_settings() {
    let mut app = app();
    app.load_current(false);

    assert!(app.pages.contains_key(&Screen::Home));
    assert!(!app.pages.contains_key(&Screen::Hosts));
    assert!(!app.pages.contains_key(&Screen::Config));
}

#[test]
fn search_is_a_configure_subview() {
    let mut app = app();
    app.handle_key(KeyEvent::new(KeyCode::Char('s'), KeyModifiers::NONE));

    assert_eq!(app.screen, Screen::Search);
    assert_eq!(app.screen.section(), NavigationSection::Configure);
    assert_eq!(app.input_mode, InputMode::Search);
}

#[test]
fn all_table_selections_stay_inside_the_viewport() {
    let mut app = app();
    app.screen = Screen::Doctor;
    app.visible_rows = 2;
    app.pages.insert(
        Screen::Doctor,
        PageState {
            payload: Some(
                json!({"results": (0..5).map(|i| json!({"name": i})).collect::<Vec<_>>() }),
            ),
            ..PageState::default()
        },
    );
    app.move_selection(1);
    app.move_selection(1);
    app.move_selection(1);
    assert_eq!(app.doctor_selected, 3);
    assert_eq!(app.scroll, 2);
}

#[test]
fn table_navigation_wraps_at_both_boundaries() {
    let mut app = app();
    app.screen = Screen::Doctor;
    app.visible_rows = 3;
    app.pages.insert(
        Screen::Doctor,
        PageState {
            payload: Some(
                json!({"results": (0..5).map(|index| json!({"name": index})).collect::<Vec<_>>() }),
            ),
            ..PageState::default()
        },
    );

    app.handle_key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE));
    assert_eq!(app.doctor_selected, 4);
    assert_eq!(app.scroll, 2);

    app.handle_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
    assert_eq!(app.doctor_selected, 0);
    assert_eq!(app.scroll, 0);
}

#[test]
fn search_result_only_offers_compatible_manifest_groups() {
    let mut app = app();
    let entry = crate::model::SearchEntry {
        source_id: None,
        availability: "installable".into(),
        source: "homebrew".into(),
        ecosystem: "homebrew".into(),
        kind: "formula".into(),
        name: "git".into(),
        version: "—".into(),
        description: "—".into(),
        homepage: "—".into(),
        publisher: "—".into(),
        reference: "homebrew:formula/git".into(),
        managed_group: None,
    };
    let payload = json!({"groups": [
        {"id": "homebrew.system.formula", "label": "Formulae", "ecosystem": "homebrew", "scope": "system", "kind": "formula"},
        {"id": "homebrew.system.cask", "label": "Casks", "ecosystem": "homebrew", "scope": "system", "kind": "cask"}
    ]});
    app.open_group_chooser(entry, Some(&payload));
    assert_eq!(app.group_chooser.unwrap().groups.len(), 1);
}

#[test]
fn successful_apply_invalidates_dependent_caches() {
    let mut app = app();
    app.pages.insert(Screen::Software, PageState::default());
    app.pages.insert(Screen::Doctor, PageState::default());
    app.search_cache.insert("git".into(), PageState::default());
    app.invalidate_after_mutation();
    assert!(!app.pages.contains_key(&Screen::Software));
    assert!(!app.pages.contains_key(&Screen::Doctor));
    assert!(app.search_cache.is_empty());
}

#[test]
fn saved_policy_offers_a_follow_up_workflow() {
    let mut app = app();
    app.post_mutation = Some("firefox".into());

    app.handle_key(KeyEvent::new(KeyCode::Char('p'), KeyModifiers::NONE));

    assert!(app.post_mutation.is_none());
    // `plan` streams into the in-TUI log popup rather than leaving the
    // alternate screen, so no leave-screen workflow action is queued.
    assert!(app.take_workflow_action().is_none());
    let log = app
        .workflow_log
        .expect("plan should open a streamed log popup");
    assert!(log.running);
    assert_eq!(log.title, WorkflowAction::Plan.title());
}

#[test]
fn blocked_preview_cannot_reach_apply_stage() {
    let mut app = app();
    app.pending_mutation = Some(MutationPreview {
        intent: MutationIntent {
            source_id: None,
            source_operation: false,
            action: SoftwareAction::Add,
            group: "nix.user.package".into(),
            operand: "git".into(),
            item: "git".into(),
        },
        payload: json!({"data": {"plan": {"blocked": "shared exclusion"}}}),
    });

    app.apply_pending_mutation();

    assert!(app.pending_mutation.is_none());
    assert!(!app.mutation_loading);
    assert_eq!(
        app.overlay_error.as_deref(),
        Some("Policy blocks this change: shared exclusion")
    );
}

#[test]
fn mirror_source_response_opens_target_chooser() {
    let mut app = app();
    app.mirror_request = Some(7);
    app.mirror_loading = true;
    // Keep this unit test free of a real chsrc subprocess; the dedicated
    // measurement test below covers the asynchronous merge.
    app.mirror_measuring = true;
    app.tx
        .send(Message::MirrorSourcesFinished {
            request: 7,
            target: "npm".into(),
            result: Ok(json!({
                "schemaVersion": 1,
                "command": "mirror.sources",
                "ok": true,
                "data": {
                    "target": "npm",
                    "label": "NPM",
                    "sources": [
                        {
                            "code": "huawei",
                            "label": "Huawei Cloud",
                            "url": "https://mirror.invalid/npm/",
                            "provider": "catalog"
                        },
                        {
                            "code": "npmmirror",
                            "label": "npmmirror",
                            "url": "https://registry.npmmirror.com",
                            "provider": "chsrc",
                            "current": true,
                            "selectionOrigin": "profile"
                        }
                    ]
                }
            })),
        })
        .unwrap();

    app.receive_messages();

    assert!(!app.mirror_loading);
    assert_eq!(app.mirror_chooser.as_ref().unwrap().target, "npm");
    assert_eq!(
        app.mirror_chooser.as_ref().unwrap().sources[0].code,
        "huawei"
    );
    assert_eq!(app.mirror_chooser.as_ref().unwrap().selected, 1);
}

#[test]
fn mirror_measurement_updates_chooser_without_hiding_current_selection() {
    let mut app = app();
    app.mirror_chooser = Some(MirrorChooser {
        target: "npm".into(),
        label: "NPM".into(),
        profile: "china".into(),
        sources: vec![MirrorSource {
            code: "npmmirror".into(),
            label: "npmmirror".into(),
            url: "https://registry.npmmirror.com".into(),
            provider: "catalog".into(),
            ok: None,
            http_status: None,
            throughput_bps: None,
            detail: None,
            stale: false,
            measurement_stale: false,
            fetched_at: None,
            current: true,
            selection_origin: Some("profile".into()),
        }],
        selected: 0,
    });
    app.mirror_measure_request = Some(9);
    app.mirror_measuring = true;
    app.tx
        .send(Message::MirrorMeasureFinished {
            request: 9,
            target: "npm".into(),
            result: Ok(json!({
                "schemaVersion": 1,
                "command": "mirror.measure",
                "ok": true,
                "data": {"results": [{
                    "code": "npmmirror",
                    "label": "npmmirror",
                    "url": "https://registry.npmmirror.com",
                    "provider": "chsrc",
                    "ok": true,
                    "httpStatus": 200,
                    "throughputBps": 1024,
                    "current": true,
                    "selectionOrigin": "profile"
                }]}
            })),
        })
        .unwrap();

    app.receive_messages();

    let source = &app.mirror_chooser.as_ref().unwrap().sources[0];
    assert!(!app.mirror_measuring);
    assert!(source.current);
    assert_eq!(source.selection_origin.as_deref(), Some("profile"));
    assert_eq!(source.http_status, Some(200));
    assert_eq!(source.throughput_bps, Some(1024));
}

#[test]
fn mirror_preview_response_waits_for_explicit_apply() {
    let mut app = app();
    app.mirror_request = Some(8);
    app.mirror_loading = true;
    let source = MirrorSource {
        code: "huawei".into(),
        label: "Huawei Cloud".into(),
        url: "https://mirror.invalid/npm/".into(),
        provider: "catalog".into(),
        ok: None,
        http_status: None,
        throughput_bps: None,
        detail: None,
        stale: false,
        measurement_stale: false,
        fetched_at: None,
        current: false,
        selection_origin: None,
    };
    app.tx
        .send(Message::MirrorMutationFinished {
            request: 8,
            target: "npm".into(),
            source,
            stage: MutationStage::Preview,
            result: Ok(json!({
                "schemaVersion": 1,
                "command": "mirror.set",
                "ok": true,
                "data": {"result": "dry-run", "plan": {"target": "npm"}}
            })),
        })
        .unwrap();

    app.receive_messages();

    assert!(!app.mirror_loading);
    assert!(app.pending_mirror.is_some());
}

#[test]
fn escape_clears_an_in_progress_software_filter() {
    let mut app = app();
    app.screen = Screen::Software;
    app.input_mode = InputMode::SoftwareFilter;
    app.software_filter = "git".into();

    app.handle_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));

    assert_eq!(app.input_mode, InputMode::Normal);
    assert!(app.software_filter.is_empty());
    assert!(!app.should_quit);
}

#[test]
fn escape_does_not_accidentally_quit_normal_mode() {
    let mut app = app();

    app.handle_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));

    assert!(!app.should_quit);
    assert_eq!(app.status, "Nothing to dismiss; press q to quit");
}

#[test]
fn viewport_and_boundary_navigation_keep_selection_visible() {
    let mut app = app();
    app.screen = Screen::Doctor;
    app.visible_rows = 3;
    app.pages.insert(
        Screen::Doctor,
        PageState {
            payload: Some(json!({
                "results": (0..10).map(|index| json!({"name": index})).collect::<Vec<_>>()
            })),
            ..PageState::default()
        },
    );

    app.handle_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
    assert_eq!(app.doctor_selected, 3);
    assert_eq!(app.scroll, 1);

    app.handle_key(KeyEvent::new(KeyCode::End, KeyModifiers::NONE));
    assert_eq!(app.doctor_selected, 9);
    assert_eq!(app.scroll, 7);

    app.handle_key(KeyEvent::new(KeyCode::Home, KeyModifiers::NONE));
    assert_eq!(app.doctor_selected, 0);
    assert_eq!(app.scroll, 0);
}

#[test]
fn bracketed_paste_is_normalized_into_the_active_input() {
    let mut app = app();
    app.input_mode = InputMode::Search;

    app.handle_paste("  visual\n studio\tcode  ");

    assert_eq!(app.query, "visual studio code");
}

fn settings_app() -> App {
    let mut app = app();
    app.pages.insert(
        Screen::Hosts,
        PageState {
            payload: Some(json!({"machines": [
                {"platform": "darwin", "machineId": "mac", "current": true, "file": "a"},
                {"platform": "darwin", "machineId": "air", "current": false, "file": "b"}
            ]})),
            ..PageState::default()
        },
    );
    app.pages.insert(
        Screen::Config,
        PageState {
            payload: Some(json!({
                "device": {"machineId": "mac"},
                "fields": [
                    {"path": "envy.mirrors.mode", "value": "china", "choices": ["upstream", "china"]},
                    {"path": "envy.user.name", "value": "chi", "choices": []}
                ]
            })),
            ..PageState::default()
        },
    );
    app.screen = Screen::Config;
    app
}

#[test]
fn settings_enumerated_value_opens_a_dropdown_and_confirms() {
    let mut app = settings_app();
    // Row 0 is host; move to the enumerated mirror field (row 1).
    app.visible_rows = 10;
    app.move_selection(1);
    app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    let chooser = app.setting_chooser.as_ref().expect("dropdown opens");
    assert_eq!(
        chooser.key,
        SettingKey::Config("envy.mirrors.mode".to_string())
    );
    assert_eq!(chooser.selected, 1); // "china" is currently selected
                                     // Pick "upstream" and confirm.
    app.handle_key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE));
    app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    let pending = app.pending_setting.as_ref().expect("awaiting confirmation");
    assert_eq!(pending.value, "upstream");
}

#[test]
fn settings_freeform_value_uses_text_input() {
    let mut app = settings_app();
    app.visible_rows = 10;
    app.move_selection(2); // envy.user.name (freeform)
    app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    assert_eq!(app.input_mode, InputMode::SettingValue);
    assert_eq!(app.setting_edit.as_ref().unwrap().buffer, "chi");
}

#[test]
fn host_row_dropdown_lists_machines() {
    let mut app = settings_app();
    app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    let chooser = app.setting_chooser.as_ref().expect("host dropdown");
    assert_eq!(chooser.key, SettingKey::Host);
    assert_eq!(chooser.options, vec!["mac".to_string(), "air".to_string()]);
}

#[test]
fn chooser_navigation_wraps_at_both_boundaries() {
    let mut app = settings_app();
    app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));

    app.handle_key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE));
    assert_eq!(app.setting_chooser.as_ref().unwrap().selected, 1);

    app.handle_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
    assert_eq!(app.setting_chooser.as_ref().unwrap().selected, 0);
}
#[test]
fn source_sync_shortcut_preserves_source_screen_and_requests_confirmation() {
    let mut app = app();
    app.screen = Screen::Sources;
    app.pages.insert(
        Screen::Sources,
        PageState {
            payload: Some(json!({"sources":[{"id":"native:vendor"}]})),
            ..PageState::default()
        },
    );
    app.handle_key(KeyEvent::new(KeyCode::Char('s'), KeyModifiers::NONE));
    assert_eq!(app.screen, Screen::Sources);
    assert_eq!(
        app.workflow_confirmation,
        Some(WorkflowAction::SyncSource("native:vendor".into()))
    );
}
