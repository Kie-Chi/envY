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
fn stale_loaded_reply_keeps_the_newer_refresh_and_cached_payload() {
    let mut app = app();
    app.screen = Screen::Doctor;
    app.pages.insert(
        Screen::Doctor,
        PageState {
            payload: Some(json!({"results": [{"name": "cached"}]})),
            loading: true,
            request: Some(9),
            ..PageState::default()
        },
    );
    for (request, name) in [(8, "outdated"), (9, "current")] {
        app.tx
            .send(Message::Loaded {
                request,
                target: LoadTarget::Screen(Screen::Doctor),
                payload: json!({"results": [{"name": name}]}),
            })
            .unwrap();
        app.receive_messages();
        let expected = if request == 8 { "cached" } else { "current" };
        assert_eq!(app.payload().unwrap()["results"][0]["name"], expected);
        assert_eq!(app.loading(), request == 8);
        assert_eq!(
            app.current_state().unwrap().request,
            if request == 8 { Some(9) } else { None }
        );
    }
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
fn activity_subview_v_cycles_health_generations_operations_and_storage() {
    let mut app = app();
    app.screen = Screen::History;
    app.activity_screen = Screen::History;
    app.storage_cache.insert(
        StorageRequest::default(),
        PageState {
            loading: true,
            ..Default::default()
        },
    );
    assert_eq!(app.history_view, HistoryView::Generations);

    app.handle_key(KeyEvent::new(KeyCode::Char('v'), KeyModifiers::NONE));
    assert_eq!(app.history_view, HistoryView::Operations);

    app.handle_key(KeyEvent::new(KeyCode::Char('v'), KeyModifiers::NONE));
    assert_eq!(app.screen, Screen::Storage);

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
    assert_eq!(app.group_chooser.as_ref().unwrap().groups.len(), 1);
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
        .workflow_log.as_ref()
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

fn storage_report(view: StorageView, marker: &str) -> StorageReport {
    use crate::storage::StorageProcess;
    let data = match view {
        StorageView::Inspect => {
            json!({"items": [{"id": marker, "status": "partial", "logicalBytes": null,
            "allocatedBytes": 0, "nativeReclaimableBytes": null, "policyEligibleBytes": null}], "reports": {}})
        }
        StorageView::Status => {
            json!({"mode": "report", "rules": [], "ruleDetails": [], "runs": []})
        }
        StorageView::Validate => json!({"ok": true, "rules": []}),
    };
    let mut report = StorageReport::from_payload(
        view,
        json!({"schemaVersion":1,
        "command":format!("storage {}", view.command()), "ok":true,"data":data,"warnings":[]}),
    );
    report.process = Some(StorageProcess {
        exit_code: Some(0),
        success: true,
        stderr: String::new(),
    });
    report
}

fn cache_storage(app: &mut App, request: StorageRequest) {
    let report = storage_report(request.view, "cached");
    app.prepare_target(&LoadTarget::Storage(request.clone()));
    app.storage_cache.insert(
        request,
        PageState {
            storage_report: Some(report),
            received_at: Some(100),
            ..Default::default()
        },
    );
}

#[test]
fn storage_home_entry_defaults_to_inspection_without_other_views_or_rescan() {
    let mut app = app();
    cache_storage(&mut app, StorageRequest::default());
    let request_id = app.request_id;
    app.handle_key(KeyEvent::new(KeyCode::Char('t'), KeyModifiers::NONE));
    assert_eq!(app.screen, Screen::Storage);
    assert_eq!(
        app.current_target(),
        Some(LoadTarget::Storage(StorageRequest::default()))
    );
    assert_eq!(app.storage_cache.len(), 1);
    assert_eq!(app.request_id, request_id);
    app.load_current(false);
    assert_eq!(
        app.request_id, request_id,
        "cached navigation must not rescan"
    );
    app.handle_key(KeyEvent::new(KeyCode::Char('s'), KeyModifiers::NONE));
    assert_eq!(app.screen, Screen::Search);
    assert_eq!(app.input_mode, InputMode::Search);
    assert_eq!(app.storage_cache.len(), 1);
}

#[test]
fn storage_report_keys_explicitly_select_read_commands_and_preserve_activity_v() {
    let mut app = app();
    app.screen = Screen::Storage;
    app.activity_screen = Screen::Storage;
    for view in StorageView::ALL {
        cache_storage(
            &mut app,
            StorageRequest {
                view,
                projects: vec![],
            },
        );
    }
    app.pages.insert(
        Screen::Doctor,
        PageState {
            payload: Some(json!({})),
            ..Default::default()
        },
    );
    let request_id = app.request_id;
    for (key, view) in [
        (']', StorageView::Status),
        (']', StorageView::Validate),
        (']', StorageView::Inspect),
        ('[', StorageView::Validate),
    ] {
        app.handle_key(KeyEvent::new(KeyCode::Char(key), KeyModifiers::NONE));
        assert_eq!(app.storage_view, view);
        assert_eq!(app.storage_request().argv().unwrap()[1], view.command());
    }
    assert_eq!(app.request_id, request_id);
    app.handle_key(KeyEvent::new(KeyCode::Char('v'), KeyModifiers::NONE));
    assert_eq!(app.screen, Screen::Doctor);
    assert!(app.workflow_confirmation.is_none());
    assert!(app.take_workflow_action().is_none());
}

#[test]
fn storage_project_input_preserves_spaces_unicode_and_shell_characters_as_one_path() {
    let mut app = app();
    app.screen = Screen::Storage;
    let first = "/tmp/a  project/子目录".to_string();
    let second = "/tmp/$(touch never);`echo literal`".to_string();
    for projects in [
        vec![],
        vec![first.clone()],
        vec![first.clone(), second.clone()],
    ] {
        cache_storage(
            &mut app,
            StorageRequest {
                view: StorageView::Inspect,
                projects,
            },
        );
    }
    app.handle_key(KeyEvent::new(KeyCode::Char('p'), KeyModifiers::NONE));
    assert_eq!(app.input_mode, InputMode::StorageProject);
    app.handle_paste(&format!("{first}\n"));
    app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    assert_eq!(app.storage_projects, [first.clone()]);
    app.handle_key(KeyEvent::new(KeyCode::Char('p'), KeyModifiers::NONE));
    app.handle_paste(&second);
    app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    assert_eq!(app.storage_projects, [first.clone(), second.clone()]);
    assert_eq!(
        app.storage_request().argv().unwrap(),
        [
            "storage",
            "inspect",
            "--json",
            "--project=/tmp/a  project/子目录",
            "--project=/tmp/$(touch never);`echo literal`"
        ]
    );
    app.handle_key(KeyEvent::new(KeyCode::Char('p'), KeyModifiers::NONE));
    app.handle_paste(&first);
    app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    assert_eq!(
        app.storage_projects.len(),
        2,
        "duplicate scope is unchanged"
    );
    app.handle_key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE));
    assert_eq!(app.storage_projects, [first]);
    app.handle_key(KeyEvent::new(KeyCode::Char('X'), KeyModifiers::NONE));
    assert!(app.storage_projects.is_empty());
    assert_eq!(
        app.request_id, 0,
        "all combinations were cached; no real inspect is run"
    );
}

#[test]
fn storage_project_input_cancel_and_invalid_paths_do_not_change_scope() {
    let mut app = app();
    app.screen = Screen::Storage;
    app.begin_storage_project();
    app.handle_paste("/one\n/two");
    app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    assert_eq!(app.input_mode, InputMode::StorageProject);
    assert!(app.storage_projects.is_empty());
    app.handle_key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL));
    assert!(app.storage_project_input.is_empty());
    app.handle_key(KeyEvent::new(KeyCode::Char('s'), KeyModifiers::NONE));
    assert_eq!(app.storage_project_input, "s");
    assert_eq!(
        app.screen,
        Screen::Storage,
        "input consumes the global search key"
    );
    app.handle_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
    assert_eq!(app.input_mode, InputMode::Normal);
    app.storage_view = StorageView::Status;
    app.begin_storage_project();
    assert_eq!(app.input_mode, InputMode::Normal);
    assert!(app.storage_projects.is_empty());
}

#[test]
fn storage_cache_is_bounded_and_evicted_responses_cannot_recreate_entries() {
    let mut app = app();
    for index in 0..STORAGE_CACHE_LIMIT + 2 {
        let target = StorageRequest {
            projects: vec![format!("/project/{index}")],
            ..Default::default()
        };
        app.prepare_target(&LoadTarget::Storage(target.clone()));
        app.storage_cache.get_mut(&target).unwrap().request = Some(index as u64 + 1);
    }
    assert_eq!(app.storage_cache.len(), STORAGE_CACHE_LIMIT);
    assert_eq!(app.storage_order.len(), STORAGE_CACHE_LIMIT);
    let evicted = StorageRequest {
        projects: vec!["/project/0".into()],
        ..Default::default()
    };
    app.tx
        .send(Message::StorageFinished {
            request: 1,
            target: evicted.clone(),
            result: Ok(storage_report(StorageView::Inspect, "evicted")),
        })
        .unwrap();
    app.receive_messages();
    assert!(!app.storage_cache.contains_key(&evicted));
    let recent = app.storage_order.front().unwrap().clone();
    app.prepare_target(&LoadTarget::Storage(recent.clone()));
    assert_eq!(app.storage_order.back(), Some(&recent));
    assert_eq!(app.storage_cache.len(), STORAGE_CACHE_LIMIT);
}

#[test]
fn storage_old_request_and_other_project_replies_never_overwrite_current_report() {
    let mut app = app();
    app.screen = Screen::Storage;
    app.fixed_now = Some(1000);
    let target = StorageRequest::default();
    cache_storage(&mut app, target.clone());
    app.storage_cache.get_mut(&target).unwrap().request = Some(20);
    app.storage_cache.get_mut(&target).unwrap().loading = true;
    let other = StorageRequest {
        projects: vec!["/different".into()],
        ..Default::default()
    };
    cache_storage(&mut app, other.clone());
    app.storage_cache.get_mut(&other).unwrap().request = Some(19);
    app.tx
        .send(Message::StorageFinished {
            request: 19,
            target: target.clone(),
            result: Ok(storage_report(StorageView::Inspect, "stale")),
        })
        .unwrap();
    app.tx
        .send(Message::StorageFinished {
            request: 19,
            target: other.clone(),
            result: Ok(storage_report(StorageView::Inspect, "offscreen")),
        })
        .unwrap();
    app.receive_messages();
    assert_eq!(
        app.storage_report().unwrap().payload["data"]["items"][0]["id"],
        "cached"
    );
    assert_eq!(app.current_state().unwrap().request, Some(20));
    assert_eq!(app.current_state().unwrap().received_at, Some(100));
    assert_eq!(app.storage_cache[&other].received_at, Some(1000));
    app.tx
        .send(Message::StorageFinished {
            request: 20,
            target,
            result: Ok(storage_report(StorageView::Inspect, "new")),
        })
        .unwrap();
    app.receive_messages();
    assert_eq!(
        app.storage_report().unwrap().payload["data"]["items"][0]["id"],
        "new"
    );
    assert_eq!(app.current_state().unwrap().received_at, Some(1000));
}

#[test]
fn storage_failed_refresh_preserves_data_receipt_time_and_requires_explicit_retry() {
    let mut app = app();
    app.screen = Screen::Storage;
    let target = StorageRequest::default();
    cache_storage(&mut app, target.clone());
    app.storage_cache.get_mut(&target).unwrap().request = Some(20);
    app.tx
        .send(Message::StorageFinished {
            request: 20,
            target: target.clone(),
            result: Err("Timeout: inspect".into()),
        })
        .unwrap();
    app.receive_messages();
    assert_eq!(app.current_state().unwrap().received_at, Some(100));
    assert_eq!(app.current_error(), Some("Timeout: inspect"));
    assert!(app.current_has_content());
    assert!(app.status.contains("refresh failed"));
    app.set_screen(Screen::Storage);
    assert_eq!(app.request_id, 0);
    assert!(app.current_error().is_some());
    app.storage_cache.get_mut(&target).unwrap().storage_report = None;
    app.load_current(false);
    assert_eq!(
        app.request_id, 0,
        "navigation does not retry a failed scan without r"
    );
}

#[test]
fn storage_partial_nonzero_reports_and_details_never_become_ready_or_mutations() {
    let mut app = app();
    app.screen = Screen::Storage;
    let target = StorageRequest::default();
    cache_storage(&mut app, target.clone());
    app.storage_cache.get_mut(&target).unwrap().request = Some(20);
    let mut report = storage_report(StorageView::Inspect, "partial item");
    report.payload["ok"] = json!(false);
    report.payload["warnings"] = json!(["warning evidence"]);
    report.payload["data"]["errors"] = json!(["partial error"]);
    report.payload["error"] = json!({"code":"partial_failure","message":"inspect errors"});
    report.process.as_mut().unwrap().exit_code = Some(7);
    report.process.as_mut().unwrap().success = false;
    app.tx
        .send(Message::StorageFinished {
            request: 20,
            target,
            result: Ok(report),
        })
        .unwrap();
    app.receive_messages();
    assert!(app.status.contains("partial / failed"));
    assert!(app.status.contains("exit 7"));
    assert!(!app.status.contains("ready"));
    app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    match app.detail.as_ref().unwrap() {
        DetailView::Storage { report, row } => {
            let detail = report.detail_text(row);
            for evidence in [
                "partial item",
                "warning evidence",
                "partial error",
                "partial_failure",
                "Process exit: 7",
            ] {
                assert!(detail.contains(evidence));
            }
        }
        _ => panic!("expected Storage detail"),
    }
    app.detail_scroll_max = 100;
    app.handle_key(KeyEvent::new(KeyCode::End, KeyModifiers::NONE));
    assert_eq!(app.detail_scroll, 100);
    app.handle_key(KeyEvent::new(KeyCode::Home, KeyModifiers::NONE));
    assert_eq!(app.detail_scroll, 0);
    app.handle_key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE));
    assert!(app.workflow_confirmation.is_none());
    assert!(app.take_workflow_action().is_none());
    assert!(app.pending_mutation.is_none());
}

#[test]
fn storage_strict_envelope_failure_retains_previous_valid_report() {
    let mut app = app();
    app.screen = Screen::Storage;
    let target = StorageRequest::default();
    cache_storage(&mut app, target.clone());
    for (index, payload) in [
        json!({"data":{"items":[]}}),
        json!({"schemaVersion":1,"command":"storage status","ok":true,"data":{},"warnings":[]}),
        json!({"schemaVersion":1,"command":"storage inspect","ok":"true","data":{}}),
    ]
    .into_iter()
    .enumerate()
    {
        app.storage_cache.get_mut(&target).unwrap().request = Some(index as u64);
        app.tx
            .send(Message::StorageFinished {
                request: index as u64,
                target: target.clone(),
                result: Ok(StorageReport::from_payload(StorageView::Inspect, payload)),
            })
            .unwrap();
        app.receive_messages();
        assert!(app.current_error().unwrap().contains("Protocol"));
        assert_eq!(
            app.storage_report().unwrap().payload["data"]["items"][0]["id"],
            "cached"
        );
        assert_eq!(app.current_state().unwrap().received_at, Some(100));
    }
}

#[test]
fn successful_host_or_config_edits_discard_storage_reports_and_in_flight_replies() {
    for key in [SettingKey::Host, SettingKey::Config("storage.mode".into())] {
        let mut app = app();
        app.request_id = 1000;
        for (index, view) in StorageView::ALL.into_iter().enumerate() {
            let target = StorageRequest {
                view,
                projects: vec![],
            };
            cache_storage(&mut app, target.clone());
            let state = app.storage_cache.get_mut(&target).unwrap();
            state.loading = true;
            state.request = Some(700 + index as u64);
        }
        app.screen = Screen::Storage;
        app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(matches!(app.detail, Some(DetailView::Storage { .. })));
        // An empty Search page has no request, keeping this regression isolated.
        app.screen = Screen::Search;
        app.query.clear();
        app.setting_request = Some(999);
        app.setting_loading = true;
        app.tx
            .send(Message::SettingApplied {
                request: 999,
                key,
                result: Ok(json!({})),
            })
            .unwrap();
        app.receive_messages();
        assert!(app.storage_cache.is_empty());
        assert!(app.storage_order.is_empty());
        assert!(app.detail.is_none());
        assert_eq!(app.request_id, 1000, "no off-tab scan after a setting edit");
        for (index, view) in StorageView::ALL.into_iter().enumerate() {
            app.tx
                .send(Message::StorageFinished {
                    request: 700 + index as u64,
                    target: StorageRequest {
                        view,
                        projects: vec![],
                    },
                    result: Ok(storage_report(view, "old host")),
                })
                .unwrap();
        }
        app.receive_messages();
        assert!(
            app.storage_cache.is_empty(),
            "old host replies cannot revive cache"
        );
        let target = StorageRequest::default();
        app.prepare_target(&LoadTarget::Storage(target.clone()));
        let state = app.storage_cache.get_mut(&target).unwrap();
        state.request = Some(1001);
        state.loading = true;
        app.tx
            .send(Message::StorageFinished {
                request: 700,
                target: target.clone(),
                result: Ok(storage_report(StorageView::Inspect, "old host")),
            })
            .unwrap();
        app.receive_messages();
        assert!(app.storage_cache[&target].loading);
        assert!(!app.storage_cache[&target].has_content());
        app.tx
            .send(Message::StorageFinished {
                request: 1001,
                target: target.clone(),
                result: Ok(storage_report(StorageView::Inspect, "new host")),
            })
            .unwrap();
        app.receive_messages();
        assert!(!app.storage_cache[&target].loading);
        assert_eq!(
            app.storage_cache[&target]
                .storage_report
                .as_ref()
                .unwrap()
                .payload["data"]["items"][0]["id"],
            "new host"
        );
    }
}
