//! Page loading and backend message handling.

use serde_json::Value;

use crate::{
    backend::spawn_request,
    model::{
        mirror_measurements, mirror_mode, mirror_sources, DetailView, LoadTarget, Message,
        MirrorChooser, MirrorPreview, MutationPreview, MutationStage, PageState, Screen,
    },
    snapshot,
};

use super::{App, SEARCH_CACHE_LIMIT, WORKFLOW_LOG_LIMIT};

impl App {
    fn prepare_target(&mut self, target: &LoadTarget) {
        match target {
            LoadTarget::Screen(screen) => {
                self.pages.entry(*screen).or_default();
            }
            LoadTarget::Search(query) => {
                if let Some(index) = self.search_order.iter().position(|item| item == query) {
                    self.search_order.remove(index);
                } else if self.search_order.len() >= SEARCH_CACHE_LIMIT {
                    if let Some(oldest) = self.search_order.pop_front() {
                        self.search_cache.remove(&oldest);
                    }
                }
                self.search_order.push_back(query.clone());
                self.search_cache.entry(query.clone()).or_default();
            }
        }
    }

    pub fn load_current(&mut self, force: bool) {
        let Some(target) = self.current_target() else {
            self.status = "Type a query and press Enter".to_string();
            return;
        };
        self.prepare_target(&target);
        let should_start = self
            .state_for_target(&target)
            .map(|state| state.needs_load(force))
            .unwrap_or(true);
        if !should_start {
            self.sync_status();
            self.ensure_config_dependencies();
            return;
        }
        self.request_id += 1;
        let request = self.request_id;
        let has_content = self
            .state_for_target(&target)
            .map(PageState::has_content)
            .unwrap_or(false);
        if let Some(state) = self.state_for_target_mut(&target) {
            state.loading = true;
            state.error = None;
            state.request = Some(request);
        }
        let saved_age = match &target {
            LoadTarget::Screen(screen) => self.saved_age(*screen),
            LoadTarget::Search(_) => None,
        };
        self.status = match saved_age {
            Some(age) => format!("Showing {} from {age}", target.title().to_lowercase()),
            None if has_content => format!("Refreshing {}", target.title().to_lowercase()),
            None => format!("Loading {}", target.title().to_lowercase()),
        };
        let home = target == LoadTarget::Screen(Screen::Home);
        spawn_request(self.tx.clone(), request, target);
        if home {
            // Home lists recent operations; refresh them alongside it.
            self.ensure_loaded(Screen::Journal, true);
        }
        self.ensure_config_dependencies();
    }

    /// The Settings sub-view edits host and config together. Config is the
    /// active leaf payload; Hosts is loaded alongside it for its selector.
    fn ensure_config_dependencies(&mut self) {
        if self.screen != Screen::Config {
            return;
        }
        self.ensure_loaded(Screen::Hosts, false);
    }

    /// Start a background load for an off-tab screen if it has no cached payload
    /// or `force` is set, without touching the current screen's status or selection.
    fn ensure_loaded(&mut self, screen: Screen, force: bool) {
        let target = LoadTarget::Screen(screen);
        self.prepare_target(&target);
        let should_start = self
            .state_for_target(&target)
            .map(|state| state.needs_load(force))
            .unwrap_or(true);
        if !should_start {
            return;
        }
        self.request_id += 1;
        let request = self.request_id;
        if let Some(state) = self.state_for_target_mut(&target) {
            state.loading = true;
            state.error = None;
            state.request = Some(request);
        }
        spawn_request(self.tx.clone(), request, target);
    }

    pub(super) fn sync_status(&mut self) {
        let Some(target) = self.current_target() else {
            self.status = "Type a query and press Enter".to_string();
            return;
        };
        let Some(state) = self.state_for_target(&target) else {
            self.status = format!("{} not loaded", target.title());
            return;
        };
        let saved_age = state
            .saved_at
            .map(|saved_at| crate::model::relative_age(saved_at, self.now()));
        self.status = if let (true, Some(age)) = (state.loading, &saved_age) {
            format!("Showing {} from {age}", target.title().to_lowercase())
        } else if let (Some(_), Some(age)) = (&state.error, &saved_age) {
            format!("{} refresh failed; showing data from {age}", target.title())
        } else if state.loading && state.has_content() {
            format!("Refreshing {}", target.title().to_lowercase())
        } else if state.loading {
            format!("Loading {}", target.title().to_lowercase())
        } else if state.error.is_some() && state.has_content() {
            format!("{} refresh failed; showing cached data", target.title())
        } else if state.error.is_some() {
            format!("{} load failed", target.title())
        } else if state.has_content() {
            format!("{} cached", target.title())
        } else {
            format!("{} not loaded", target.title())
        };
    }

    pub fn receive_messages(&mut self) {
        while let Ok(message) = self.rx.try_recv() {
            match message {
                Message::Loaded {
                    request,
                    target,
                    payload,
                } => {
                    let is_current = self.current_target().as_ref() == Some(&target);
                    let home_doctor = (target == LoadTarget::Screen(Screen::Home))
                        .then(|| payload.get("doctor").cloned())
                        .flatten();
                    let saved = (target == LoadTarget::Screen(Screen::Home))
                        .then(|| {
                            self.snapshot_path
                                .clone()
                                .map(|path| (path, payload.clone()))
                        })
                        .flatten();
                    let accepted = self
                        .state_for_target_mut(&target)
                        .filter(|state| state.request == Some(request))
                        .map(|state| {
                            state.payload = Some(payload);
                            state.loading = false;
                            state.error = None;
                            state.request = None;
                            state.saved_at = None;
                        })
                        .is_some();
                    if accepted {
                        if let Some((path, payload)) = saved {
                            // Best effort: a missing cache only slows the next start.
                            let _ = snapshot::save(&path, &payload, self.now());
                        }
                        if let Some(doctor) = home_doctor {
                            let state = self.pages.entry(Screen::Doctor).or_default();
                            if !state.loading {
                                state.payload = Some(doctor);
                                state.error = None;
                            }
                        }
                        let count = self.row_count();
                        self.set_selected(self.selected().min(count.saturating_sub(1)));
                    }
                    if accepted && is_current {
                        self.status = format!("{} ready", target.title());
                    }
                }
                Message::Failed {
                    request,
                    target,
                    error,
                } => {
                    let is_current = self.current_target().as_ref() == Some(&target);
                    let accepted = self
                        .state_for_target_mut(&target)
                        .filter(|state| state.request == Some(request))
                        .map(|state| {
                            state.loading = false;
                            state.error = Some(error);
                            state.request = None;
                        })
                        .is_some();
                    if accepted && is_current {
                        self.sync_status();
                    }
                }
                Message::MutationFinished {
                    request,
                    stage,
                    intent,
                    result,
                } if self.mutation_request == Some(request) => {
                    self.mutation_request = None;
                    self.mutation_loading = false;
                    match (stage, result) {
                        (MutationStage::Preview, Ok(payload)) => {
                            self.status =
                                format!("{} plan ready for {}", intent.action.label(), intent.item);
                            self.pending_mutation = Some(MutationPreview { intent, payload });
                        }
                        (MutationStage::Apply, Ok(_)) => {
                            let description =
                                format!("{} applied for {}", intent.action.label(), intent.item);
                            self.pending_mutation = None;
                            self.invalidate_after_mutation();
                            self.load_current(true);
                            self.post_mutation = Some(intent.item.clone());
                            self.status = format!("{description}; refreshing data");
                        }
                        (_, Err(error)) => {
                            self.pending_mutation = None;
                            self.overlay_error = Some(error);
                            self.status = format!("{} failed", intent.action.label());
                        }
                    }
                }
                Message::MutationFinished { .. } => {}
                Message::SoftwareWhyFinished { request, result }
                    if self.detail_request == Some(request) =>
                {
                    self.detail_request = None;
                    match result {
                        Ok(payload) => self.detail = Some(DetailView::Software(payload)),
                        Err(error) => {
                            self.detail = None;
                            self.overlay_error = Some(error);
                        }
                    }
                }
                Message::SearchGroupsFinished {
                    request,
                    entry,
                    software,
                } if self.group_request == Some(request) => {
                    self.group_request = None;
                    match software {
                        Ok(payload) => {
                            self.pages.entry(Screen::Software).or_default().payload =
                                Some(payload.clone());
                            self.open_group_chooser(entry, Some(&payload));
                        }
                        Err(error) => {
                            self.detail = None;
                            self.overlay_error = Some(error);
                        }
                    }
                }
                Message::HistoryDiffFinished { request, result }
                    if self.detail_request == Some(request) =>
                {
                    self.detail_request = None;
                    match result {
                        Ok(payload) => self.detail = Some(DetailView::HistoryDiff(payload)),
                        Err(error) => {
                            self.detail = None;
                            self.overlay_error = Some(error);
                        }
                    }
                }
                Message::SettingApplied {
                    request,
                    key,
                    result,
                } if self.setting_request == Some(request) => {
                    self.setting_request = None;
                    self.setting_loading = false;
                    match result {
                        Ok(_) => {
                            self.refresh_after_setting();
                            self.status = format!("{} updated; refreshing", key.label());
                        }
                        Err(error) => {
                            self.overlay_error = Some(error);
                            self.status = format!("{} change failed", key.label());
                        }
                    }
                }
                Message::SettingApplied { .. } => {}
                Message::MirrorSourcesFinished {
                    request,
                    target,
                    result,
                } if self.mirror_request == Some(request) => {
                    self.mirror_request = None;
                    self.mirror_loading = false;
                    match result {
                        Ok(payload) => {
                            let data = payload.get("data").unwrap_or(&Value::Null);
                            let label = data
                                .get("label")
                                .and_then(Value::as_str)
                                .unwrap_or(&target)
                                .to_string();
                            let profile = mirror_mode(Some(&payload));
                            let sources = mirror_sources(Some(&payload));
                            if sources.is_empty() {
                                self.overlay_error =
                                    Some(format!("No mirror sources are available for {target}"));
                            } else {
                                let selected = sources
                                    .iter()
                                    .position(|source| source.current)
                                    .unwrap_or(0);
                                self.mirror_chooser = Some(MirrorChooser {
                                    target: target.clone(),
                                    label,
                                    profile,
                                    sources,
                                    selected,
                                });
                                self.begin_mirror_measure(target.clone(), false);
                            }
                        }
                        Err(error) => {
                            self.overlay_error = Some(error);
                            self.status = format!("{target} mirror lookup failed");
                        }
                    }
                }
                Message::MirrorSourcesFinished { .. } => {}
                Message::MirrorMeasureFinished {
                    request,
                    target,
                    result,
                } if self.mirror_measure_request == Some(request) => {
                    self.mirror_measure_request = None;
                    self.mirror_measuring = false;
                    match result {
                        Ok(payload) => {
                            let sources = mirror_measurements(Some(&payload));
                            if let Some(chooser) = self
                                .mirror_chooser
                                .as_mut()
                                .filter(|chooser| chooser.target == target)
                            {
                                if !sources.is_empty() {
                                    let selected_code = chooser
                                        .sources
                                        .get(chooser.selected)
                                        .map(|source| source.code.clone());
                                    chooser.sources = sources;
                                    chooser.selected = selected_code
                                        .as_deref()
                                        .and_then(|code| {
                                            chooser
                                                .sources
                                                .iter()
                                                .position(|source| source.code == code)
                                        })
                                        .unwrap_or(0);
                                    self.status =
                                        format!("{} mirror measurements updated", chooser.target);
                                }
                            }
                        }
                        Err(_) => {
                            // A failed measurement should not hide the usable
                            // candidate list or turn a network condition into a
                            // blocking TUI error.
                            self.status = format!(
                                "{target} measurement unavailable; source selection remains usable"
                            );
                        }
                    }
                }
                Message::MirrorMeasureFinished { .. } => {}
                Message::MirrorMutationFinished {
                    request,
                    target,
                    source,
                    stage,
                    result,
                } if self.mirror_request == Some(request) => {
                    self.mirror_request = None;
                    self.mirror_loading = false;
                    match (stage, result) {
                        (MutationStage::Preview, Ok(payload)) => {
                            self.pending_mirror = Some(MirrorPreview {
                                target: target.clone(),
                                source,
                                payload,
                            });
                            self.status = format!("{target} mirror plan ready");
                        }
                        (MutationStage::Apply, Ok(_)) => {
                            self.pending_mirror = None;
                            self.invalidate_after_mirror();
                            self.load_current(true);
                            self.status =
                                format!("{target} mirror override saved; plan/apply when ready");
                        }
                        (_, Err(error)) => {
                            self.pending_mirror = None;
                            self.overlay_error = Some(error);
                            self.status = format!("{target} mirror change failed");
                        }
                    }
                }
                Message::MirrorMutationFinished { .. } => {}
                Message::WorkflowOutput { request, line }
                    if self.workflow_stream_request == Some(request) =>
                {
                    if let Some(log) = self.workflow_log.as_mut() {
                        log.lines.push(line);
                        if log.lines.len() > WORKFLOW_LOG_LIMIT {
                            let overflow = log.lines.len() - WORKFLOW_LOG_LIMIT;
                            log.lines.drain(0..overflow);
                        }
                        if log.follow {
                            log.scroll = log.lines.len().saturating_sub(1);
                        }
                    }
                }
                Message::WorkflowOutput { .. } => {}
                Message::WorkflowFinished { request, success }
                    if self.workflow_stream_request == Some(request) =>
                {
                    self.workflow_stream_request = None;
                    if let Some(log) = self.workflow_log.as_mut() {
                        log.running = false;
                        log.success = Some(success);
                        if log.follow {
                            log.scroll = log.lines.len().saturating_sub(1);
                        }
                    }
                    // Refresh cached views to reflect the applied configuration;
                    // the popup stays open so the user can read the full log.
                    self.invalidate_after_mutation();
                    self.load_current(true);
                }
                Message::WorkflowFinished { .. } => {}
                _ => {}
            }
        }
    }
}
