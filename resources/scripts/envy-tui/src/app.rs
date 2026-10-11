use std::{
    collections::{HashMap, VecDeque},
    sync::mpsc::{Receiver, Sender},
};

use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseEvent, MouseEventKind};
use serde_json::Value;

use crate::{
    backend::{
        apply_needs_tty, spawn_history_diff, spawn_mirror_measure, spawn_mirror_mutation,
        spawn_mirror_sources, spawn_mutation, spawn_request, spawn_search_groups, spawn_setting,
        spawn_software_why, spawn_workflow,
    },
    model::{
        compatible_groups, count_rows, filtered_software_entries, generation_number,
        generation_rows, mirror_measurements, mirror_mode, mirror_sources, mirror_target_rows,
        result_rows, search_entries, settings_rows, DetailView, GroupChooser, HistoryView,
        LoadTarget, Message, MirrorChooser, MirrorPreview, MirrorSource, MutationIntent,
        MutationPreview, MutationStage, NavigationSection, PageState, Pages, PendingSetting,
        Screen, SettingChooser, SettingEdit, SettingRow, SoftwareAction, WorkflowAction,
        WorkflowLog,
    },
};

const SEARCH_CACHE_LIMIT: usize = 12;
const CHOOSER_PAGE_STEP: isize = 8;
/// Upper bound on streamed workflow log lines kept in memory; large Nix builds
/// can emit tens of thousands of lines, so we retain only the newest ones.
const WORKFLOW_LOG_LIMIT: usize = 5000;

/// Move within a non-empty selection list, wrapping at either boundary.
/// Detail text deliberately does not use this helper: scrolling prose should
/// remain bounded instead of jumping between its beginning and end.
fn cycle_index(selected: usize, count: usize, delta: isize) -> usize {
    if count == 0 {
        return 0;
    }
    let current = selected.min(count - 1) as isize;
    (current + delta).rem_euclid(count as isize) as usize
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum InputMode {
    Normal,
    Search,
    SoftwareFilter,
    SettingValue,
}

pub struct App {
    pub screen: Screen,
    configure_screen: Screen,
    activity_screen: Screen,
    pub pages: Pages,
    pub search_cache: HashMap<String, PageState>,
    search_order: VecDeque<String>,
    pub status: String,
    pub query: String,
    pub submitted_query: String,
    pub software_filter: String,
    pub input_mode: InputMode,
    pub scroll: usize,
    pub visible_rows: usize,
    pub software_selected: usize,
    pub search_selected: usize,
    pub doctor_selected: usize,
    pub history_selected: usize,
    pub journal_selected: usize,
    pub hosts_selected: usize,
    pub mirror_selected: usize,
    pub settings_selected: usize,
    pub history_view: HistoryView,
    pub history_marked: Option<u64>,
    request_id: u64,
    tx: Sender<Message>,
    rx: Receiver<Message>,
    pub should_quit: bool,
    pub show_help: bool,
    mutation_request: Option<u64>,
    pub mutation_loading: bool,
    pub pending_mutation: Option<MutationPreview>,
    pub overlay_error: Option<String>,
    detail_request: Option<u64>,
    pub detail: Option<DetailView>,
    pub detail_scroll: usize,
    pub detail_scroll_max: usize,
    group_request: Option<u64>,
    pub group_chooser: Option<GroupChooser>,
    pub post_mutation: Option<String>,
    pub workflow_confirmation: Option<WorkflowAction>,
    workflow_action: Option<WorkflowAction>,
    pub workflow_log: Option<WorkflowLog>,
    workflow_stream_request: Option<u64>,
    setting_request: Option<u64>,
    pub setting_loading: bool,
    pub setting_chooser: Option<SettingChooser>,
    pub setting_edit: Option<SettingEdit>,
    pub pending_setting: Option<PendingSetting>,
    pub mirror_chooser: Option<MirrorChooser>,
    pub pending_mirror: Option<MirrorPreview>,
    pub mirror_loading: bool,
    pub mirror_measuring: bool,
    mirror_request: Option<u64>,
    mirror_measure_request: Option<u64>,
}

impl App {
    pub fn new(tx: Sender<Message>, rx: Receiver<Message>) -> Self {
        Self {
            screen: Screen::Home,
            configure_screen: Screen::Software,
            activity_screen: Screen::History,
            pages: HashMap::new(),
            search_cache: HashMap::new(),
            search_order: VecDeque::new(),
            status: "Loading home".to_string(),
            query: String::new(),
            submitted_query: String::new(),
            software_filter: String::new(),
            input_mode: InputMode::Normal,
            scroll: 0,
            visible_rows: 1,
            software_selected: 0,
            search_selected: 0,
            doctor_selected: 0,
            history_selected: 0,
            journal_selected: 0,
            hosts_selected: 0,
            mirror_selected: 0,
            settings_selected: 0,
            history_view: HistoryView::default(),
            history_marked: None,
            request_id: 0,
            tx,
            rx,
            should_quit: false,
            show_help: false,
            mutation_request: None,
            mutation_loading: false,
            pending_mutation: None,
            overlay_error: None,
            detail_request: None,
            detail: None,
            detail_scroll: 0,
            detail_scroll_max: 0,
            group_request: None,
            group_chooser: None,
            post_mutation: None,
            workflow_confirmation: None,
            workflow_action: None,
            workflow_log: None,
            workflow_stream_request: None,
            setting_request: None,
            setting_loading: false,
            setting_chooser: None,
            setting_edit: None,
            pending_setting: None,
            mirror_chooser: None,
            pending_mirror: None,
            mirror_loading: false,
            mirror_measuring: false,
            mirror_request: None,
            mirror_measure_request: None,
        }
    }

    pub fn current_target(&self) -> Option<LoadTarget> {
        match self.screen {
            Screen::Search if self.submitted_query.is_empty() => None,
            Screen::Search => Some(LoadTarget::Search(self.submitted_query.clone())),
            // History merges two time-ordered views; the active sub-view decides
            // which backend command and cached payload we read.
            Screen::History => Some(LoadTarget::Screen(self.history_view.screen())),
            screen => Some(LoadTarget::Screen(screen)),
        }
    }

    pub fn state_for_target(&self, target: &LoadTarget) -> Option<&PageState> {
        match target {
            LoadTarget::Screen(screen) => self.pages.get(screen),
            LoadTarget::Search(query) => self.search_cache.get(query),
        }
    }

    fn state_for_target_mut(&mut self, target: &LoadTarget) -> Option<&mut PageState> {
        match target {
            LoadTarget::Screen(screen) => self.pages.get_mut(screen),
            LoadTarget::Search(query) => self.search_cache.get_mut(query),
        }
    }

    pub fn current_state(&self) -> Option<&PageState> {
        self.current_target()
            .as_ref()
            .and_then(|target| self.state_for_target(target))
    }

    pub fn payload(&self) -> Option<&Value> {
        self.current_state()
            .and_then(|state| state.payload.as_ref())
    }

    pub fn loading(&self) -> bool {
        self.current_state()
            .map(|state| state.loading)
            .unwrap_or(false)
    }

    pub fn current_error(&self) -> Option<&str> {
        self.current_state()
            .and_then(|state| state.error.as_deref())
    }

    pub fn current_has_content(&self) -> bool {
        self.current_state()
            .map(PageState::has_content)
            .unwrap_or(false)
    }

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
            .map(|state| !state.loading && (force || !state.has_content()))
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
        self.status = if has_content {
            format!("Refreshing {}", target.title().to_lowercase())
        } else {
            format!("Loading {}", target.title().to_lowercase())
        };
        spawn_request(self.tx.clone(), request, target);
        self.ensure_config_dependencies();
    }

    /// The Settings sub-view edits host and config together. Config is the
    /// active leaf payload; Hosts is loaded alongside it for its selector.
    fn ensure_config_dependencies(&mut self) {
        if self.screen != Screen::Config {
            return;
        }
        self.ensure_loaded(Screen::Hosts);
    }

    /// Start a background load for an off-tab screen if it has no cached payload,
    /// without touching the current screen's status or selection.
    fn ensure_loaded(&mut self, screen: Screen) {
        let target = LoadTarget::Screen(screen);
        self.prepare_target(&target);
        let should_start = self
            .state_for_target(&target)
            .map(|state| !state.loading && !state.has_content())
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

    fn sync_status(&mut self) {
        let Some(target) = self.current_target() else {
            self.status = "Type a query and press Enter".to_string();
            return;
        };
        let Some(state) = self.state_for_target(&target) else {
            self.status = format!("{} not loaded", target.title());
            return;
        };
        self.status = if state.loading && state.has_content() {
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

    fn set_screen(&mut self, screen: Screen) {
        self.screen = screen;
        match screen.section() {
            NavigationSection::Configure if screen != Screen::Hosts => {
                self.configure_screen = screen;
            }
            NavigationSection::Activity if screen != Screen::Journal => {
                self.activity_screen = screen;
            }
            _ => {}
        }
        self.scroll = 0;
        self.input_mode = InputMode::Normal;
        self.load_current(false);
    }

    fn set_section(&mut self, section: NavigationSection) {
        let screen = match section {
            NavigationSection::Home => Screen::Home,
            NavigationSection::Configure => self.configure_screen,
            NavigationSection::Activity => self.activity_screen,
        };
        self.set_screen(screen);
    }

    fn next_section(&mut self, delta: isize) {
        let count = NavigationSection::ALL.len() as isize;
        let next = (self.screen.section().index() as isize + delta).rem_euclid(count) as usize;
        self.set_section(NavigationSection::ALL[next]);
    }

    fn next_subpage(&mut self) {
        match self.screen.section() {
            NavigationSection::Home => {
                self.status = "Home has no secondary views".to_string();
            }
            NavigationSection::Configure => {
                const VIEWS: [Screen; 4] = [
                    Screen::Software,
                    Screen::Search,
                    Screen::Mirror,
                    Screen::Config,
                ];
                let current = VIEWS
                    .iter()
                    .position(|view| *view == self.configure_screen)
                    .unwrap_or(0);
                let next = (current + 1) % VIEWS.len();
                self.set_screen(VIEWS[next]);
            }
            NavigationSection::Activity => {
                let current = match (self.activity_screen, self.history_view) {
                    (Screen::History, HistoryView::Generations) => 1,
                    (Screen::History, HistoryView::Operations) => 2,
                    _ => 0,
                };
                let next = (current + 1) % 3;
                match next {
                    0 => self.set_screen(Screen::Doctor),
                    1 => {
                        self.history_view = HistoryView::Generations;
                        self.set_screen(Screen::History);
                    }
                    _ => {
                        self.history_view = HistoryView::Operations;
                        self.set_screen(Screen::History);
                    }
                }
            }
        }
    }

    pub fn software_entries(&self) -> Vec<crate::model::SoftwareEntry> {
        filtered_software_entries(self.payload(), &self.software_filter)
    }

    pub fn settings_rows(&self) -> Vec<SettingRow> {
        let hosts = self
            .pages
            .get(&Screen::Hosts)
            .and_then(|state| state.payload.as_ref());
        let config = self
            .pages
            .get(&Screen::Config)
            .and_then(|state| state.payload.as_ref());
        settings_rows(hosts, config)
    }

    pub fn row_count(&self) -> usize {
        match self.screen {
            Screen::Software => self.software_entries().len(),
            Screen::Search | Screen::Doctor => result_rows(self.payload()),
            Screen::History => match self.history_view {
                HistoryView::Generations => generation_rows(self.payload()),
                HistoryView::Operations => crate::model::journal_rows(self.payload()),
            },
            Screen::Journal => crate::model::journal_rows(self.payload()),
            Screen::Hosts => count_rows(self.payload(), "machines"),
            Screen::Mirror => mirror_target_rows(self.payload()).len(),
            Screen::Config => self.settings_rows().len(),
            Screen::Home => 0,
        }
    }

    fn selected(&self) -> usize {
        match self.screen {
            Screen::Software => self.software_selected,
            Screen::Search => self.search_selected,
            Screen::Doctor => self.doctor_selected,
            Screen::History => match self.history_view {
                HistoryView::Generations => self.history_selected,
                HistoryView::Operations => self.journal_selected,
            },
            Screen::Journal => self.journal_selected,
            Screen::Hosts => self.hosts_selected,
            Screen::Mirror => self.mirror_selected,
            Screen::Config => self.settings_selected,
            Screen::Home => 0,
        }
    }

    fn set_selected(&mut self, value: usize) {
        match self.screen {
            Screen::Software => self.software_selected = value,
            Screen::Search => self.search_selected = value,
            Screen::Doctor => self.doctor_selected = value,
            Screen::History => match self.history_view {
                HistoryView::Generations => self.history_selected = value,
                HistoryView::Operations => self.journal_selected = value,
            },
            Screen::Journal => self.journal_selected = value,
            Screen::Hosts => self.hosts_selected = value,
            Screen::Mirror => self.mirror_selected = value,
            Screen::Config => self.settings_selected = value,
            Screen::Home => {}
        }
    }

    fn move_selection(&mut self, delta: isize) {
        let count = self.row_count();
        if count == 0 {
            self.set_selected(0);
            self.scroll = 0;
            return;
        }
        let selected = cycle_index(self.selected(), count, delta);
        self.set_selected(selected);
        if selected < self.scroll {
            self.scroll = selected;
        } else if selected >= self.scroll + self.visible_rows {
            self.scroll = selected.saturating_add(1).saturating_sub(self.visible_rows);
        }
    }

    fn move_selection_to(&mut self, target: usize) {
        let count = self.row_count();
        if count == 0 {
            self.set_selected(0);
            self.scroll = 0;
            return;
        }
        let selected = target.min(count - 1);
        self.set_selected(selected);
        if selected < self.scroll {
            self.scroll = selected;
        } else if selected >= self.scroll + self.visible_rows {
            self.scroll = selected.saturating_add(1).saturating_sub(self.visible_rows);
        }
    }

    fn move_selection_page(&mut self, direction: isize) {
        let distance = self.visible_rows.max(1) as isize;
        self.move_selection(direction.saturating_mul(distance));
    }

    pub fn selection_position(&self) -> Option<(usize, usize)> {
        let count = self.row_count();
        (count > 0).then(|| (self.selected() + 1, count))
    }

    pub fn input_bar_height(&self) -> u16 {
        match self.screen {
            Screen::Search => 3,
            Screen::Software
                if self.input_mode == InputMode::SoftwareFilter
                    || !self.software_filter.is_empty() =>
            {
                3
            }
            _ => 0,
        }
    }

    pub fn clamp_scroll(&mut self, body_height: u16) {
        self.visible_rows = (body_height.saturating_sub(3) as usize).max(1);
        let count = self.row_count();
        let selected = self.selected().min(count.saturating_sub(1));
        self.set_selected(selected);
        if selected < self.scroll {
            self.scroll = selected;
        } else if selected >= self.scroll + self.visible_rows {
            self.scroll = selected.saturating_add(1).saturating_sub(self.visible_rows);
        }
        self.scroll = self.scroll.min(count.saturating_sub(self.visible_rows));
    }

    fn begin_mutation(&mut self, intent: MutationIntent) {
        self.request_id += 1;
        let request = self.request_id;
        self.mutation_request = Some(request);
        self.mutation_loading = true;
        self.overlay_error = None;
        self.group_chooser = None;
        self.status = format!(
            "Preparing {} plan for {}",
            intent.action.label(),
            intent.item
        );
        spawn_mutation(self.tx.clone(), request, MutationStage::Preview, intent);
    }

    fn begin_selected_mutation(&mut self) {
        let Some(entry) = self.software_entries().get(self.software_selected).cloned() else {
            self.status = "No software item selected".to_string();
            return;
        };
        self.begin_mutation(MutationIntent {
            action: if entry.effective {
                SoftwareAction::Remove
            } else {
                SoftwareAction::Add
            },
            group: entry.group,
            operand: entry.item.clone(),
            item: entry.item,
        });
    }

    fn apply_pending_mutation(&mut self) {
        let Some(preview) = self.pending_mutation.as_ref() else {
            return;
        };
        if let Some(reason) = preview
            .payload
            .get("data")
            .and_then(|data| data.get("plan"))
            .and_then(|plan| plan.get("blocked"))
            .and_then(Value::as_str)
        {
            let reason = reason.to_string();
            self.pending_mutation = None;
            self.overlay_error = Some(format!("Policy blocks this change: {reason}"));
            self.status = "Software change blocked".to_string();
            return;
        }
        let intent = preview.intent.clone();
        self.request_id += 1;
        let request = self.request_id;
        self.mutation_request = Some(request);
        self.mutation_loading = true;
        self.status = format!("Applying {} for {}", intent.action.label(), intent.item);
        spawn_mutation(self.tx.clone(), request, MutationStage::Apply, intent);
    }

    fn begin_why(&mut self) {
        let Some(entry) = self.software_entries().get(self.software_selected).cloned() else {
            self.status = "No software item selected".to_string();
            return;
        };
        self.request_id += 1;
        let request = self.request_id;
        self.detail_request = Some(request);
        self.detail = Some(DetailView::Loading {
            title: format!("WHY {}", entry.item),
        });
        self.detail_scroll = 0;
        self.detail_scroll_max = 0;
        spawn_software_why(self.tx.clone(), request, entry.group, entry.item);
    }

    fn begin_search_add(&mut self) {
        let Some(entry) = search_entries(self.payload())
            .get(self.search_selected)
            .cloned()
        else {
            self.status = "No search result selected".to_string();
            return;
        };
        if let Some(payload) = self
            .pages
            .get(&Screen::Software)
            .and_then(|state| state.payload.as_ref())
            .cloned()
        {
            self.open_group_chooser(entry, Some(&payload));
            return;
        }
        self.request_id += 1;
        let request = self.request_id;
        self.group_request = Some(request);
        self.detail = Some(DetailView::Loading {
            title: "FINDING COMPATIBLE GROUPS".to_string(),
        });
        spawn_search_groups(self.tx.clone(), request, entry);
    }

    fn open_group_chooser(&mut self, entry: crate::model::SearchEntry, payload: Option<&Value>) {
        let groups = compatible_groups(&entry, payload);
        self.detail = None;
        if groups.is_empty() {
            self.overlay_error = Some(format!(
                "No managed group accepts {} {} results. Search remains read-only.",
                entry.ecosystem, entry.kind
            ));
        } else {
            self.group_chooser = Some(GroupChooser {
                entry,
                groups,
                selected: 0,
            });
        }
    }

    fn confirm_group(&mut self) {
        let Some(chooser) = self.group_chooser.as_ref() else {
            return;
        };
        let Some(group) = chooser.groups.get(chooser.selected) else {
            return;
        };
        let operand = if chooser.entry.reference == "—" || chooser.entry.reference.is_empty() {
            chooser.entry.name.clone()
        } else {
            chooser.entry.reference.clone()
        };
        self.begin_mutation(MutationIntent {
            action: SoftwareAction::Add,
            group: group.id.clone(),
            operand,
            item: chooser.entry.name.clone(),
        });
    }

    fn open_doctor_detail(&mut self) {
        let Some(result) = self
            .payload()
            .and_then(|payload| payload.get("results"))
            .and_then(Value::as_array)
            .and_then(|results| results.get(self.doctor_selected))
            .cloned()
        else {
            return;
        };
        self.detail = Some(DetailView::Doctor(result));
        self.detail_scroll = 0;
        self.detail_scroll_max = 0;
    }

    fn begin_history_diff(&mut self) {
        let Some(selected) = generation_number(self.payload(), self.history_selected) else {
            self.status = "No generation selected".to_string();
            return;
        };
        let current = self
            .payload()
            .and_then(|payload| payload.get("generations"))
            .and_then(Value::as_array)
            .and_then(|generations| {
                generations.iter().find(|generation| {
                    generation.get("current").and_then(Value::as_bool) == Some(true)
                })
            })
            .and_then(|generation| generation.get("number"))
            .and_then(Value::as_u64);
        let other = self.history_marked.or(current);
        let Some(other) = other else {
            self.overlay_error = Some("Mark another generation with Space first.".to_string());
            return;
        };
        if other == selected {
            self.overlay_error = Some("Select two different generations to compare.".to_string());
            return;
        }
        let (before, after) = if other < selected {
            (other, selected)
        } else {
            (selected, other)
        };
        self.request_id += 1;
        let request = self.request_id;
        self.detail_request = Some(request);
        self.detail = Some(DetailView::Loading {
            title: format!("DIFF {before} → {after}"),
        });
        self.detail_scroll = 0;
        self.detail_scroll_max = 0;
        spawn_history_diff(self.tx.clone(), request, before, after);
    }

    fn invalidate_after_mutation(&mut self) {
        for screen in [Screen::Home, Screen::Software, Screen::Doctor] {
            self.pages.remove(&screen);
        }
        self.search_cache.clear();
        self.search_order.clear();
        self.scroll = 0;
    }

    /// Begin editing the selected Settings row: enumerated fields open a
    /// dropdown; freeform fields open a text input pre-filled with the value.
    fn begin_setting_edit(&mut self) {
        let settings = self.settings_rows();
        let Some(row) = settings.get(self.settings_selected).cloned() else {
            self.status = "No setting selected".to_string();
            return;
        };
        if row.freeform() {
            self.setting_edit = Some(SettingEdit {
                key: row.key,
                title: row.label,
                buffer: row.value.clone(),
                previous: row.value,
            });
            self.input_mode = InputMode::SettingValue;
            self.status = "Edit value; Enter saves, Esc cancels".to_string();
        } else {
            let selected = row
                .choices
                .iter()
                .position(|choice| *choice == row.value)
                .unwrap_or(0);
            self.setting_chooser = Some(SettingChooser {
                key: row.key,
                title: row.label,
                options: row.choices,
                selected,
                current: row.value,
            });
            self.status = "Choose a value; Enter confirms, Esc cancels".to_string();
        }
    }

    fn confirm_setting_choice(&mut self) {
        let Some(chooser) = self.setting_chooser.take() else {
            return;
        };
        let Some(value) = chooser.options.get(chooser.selected).cloned() else {
            return;
        };
        if value == chooser.current {
            self.status = format!("{} unchanged", chooser.key.label());
            return;
        }
        self.pending_setting = Some(PendingSetting {
            key: chooser.key,
            value,
            previous: chooser.current,
        });
    }

    fn commit_setting_edit(&mut self) {
        let Some(edit) = self.setting_edit.take() else {
            return;
        };
        self.input_mode = InputMode::Normal;
        let value = edit.buffer.trim().to_string();
        if value.is_empty() {
            self.status = format!("{} left unchanged (empty)", edit.key.label());
            return;
        }
        if value == edit.previous {
            self.status = format!("{} unchanged", edit.key.label());
            return;
        }
        self.pending_setting = Some(PendingSetting {
            key: edit.key,
            value,
            previous: edit.previous,
        });
    }

    fn apply_pending_setting(&mut self) {
        let Some(pending) = self.pending_setting.take() else {
            return;
        };
        self.request_id += 1;
        let request = self.request_id;
        self.setting_request = Some(request);
        self.setting_loading = true;
        self.status = format!("Applying {}", pending.key.label());
        spawn_setting(self.tx.clone(), request, pending.key, pending.value);
    }

    /// Host/config edits change the selected machine file, so drop and reload the
    /// Home snapshot plus the Configure/Settings payloads that reflect it.
    fn refresh_after_setting(&mut self) {
        for screen in [Screen::Home, Screen::Hosts, Screen::Config] {
            self.pages.remove(&screen);
        }
        self.load_current(true);
    }

    fn begin_mirror_sources(&mut self) {
        let Some(target) = mirror_target_rows(self.payload())
            .get(self.mirror_selected)
            .map(|row| row.key.clone())
        else {
            self.status = "No mirror target selected".to_string();
            return;
        };
        self.request_id += 1;
        let request = self.request_id;
        self.mirror_request = Some(request);
        self.mirror_loading = true;
        self.status = format!("Loading {target} mirror sources");
        spawn_mirror_sources(self.tx.clone(), request, target);
    }

    fn begin_mirror_measure(&mut self, target: String, refresh: bool) {
        if self.mirror_measuring {
            return;
        }
        self.request_id += 1;
        let request = self.request_id;
        self.mirror_measure_request = Some(request);
        self.mirror_measuring = true;
        self.status = if refresh {
            format!("Re-measuring {target} mirror sources")
        } else {
            format!("Measuring {target} mirror sources in background")
        };
        spawn_mirror_measure(self.tx.clone(), request, target, refresh);
    }

    fn cancel_mirror_measure(&mut self) {
        self.mirror_measure_request = None;
        self.mirror_measuring = false;
    }

    fn begin_mirror_mutation(&mut self, source: MirrorSource) {
        let Some(chooser) = self.mirror_chooser.take() else {
            return;
        };
        self.cancel_mirror_measure();
        self.request_id += 1;
        let request = self.request_id;
        self.mirror_request = Some(request);
        self.mirror_loading = true;
        self.status = format!("Preparing {} mirror plan", chooser.target);
        spawn_mirror_mutation(
            self.tx.clone(),
            request,
            MutationStage::Preview,
            chooser.target,
            source,
        );
    }

    fn apply_pending_mirror(&mut self) {
        let Some(preview) = self.pending_mirror.take() else {
            return;
        };
        self.request_id += 1;
        let request = self.request_id;
        self.mirror_request = Some(request);
        self.mirror_loading = true;
        self.status = format!("Writing {} mirror override", preview.target);
        spawn_mirror_mutation(
            self.tx.clone(),
            request,
            MutationStage::Apply,
            preview.target,
            preview.source,
        );
    }

    fn invalidate_after_mirror(&mut self) {
        for screen in [
            Screen::Home,
            Screen::Software,
            Screen::Doctor,
            Screen::Mirror,
            Screen::Hosts,
            Screen::Config,
        ] {
            self.pages.remove(&screen);
        }
        self.search_cache.clear();
        self.search_order.clear();
        self.scroll = 0;
        self.mirror_selected = 0;
    }

    pub fn take_workflow_action(&mut self) -> Option<WorkflowAction> {
        self.workflow_action.take()
    }

    /// Route a workflow to either the in-TUI streamed log popup or the
    /// leave-screen path. GUI launchers and `apply` runs that need an
    /// interactive sudo prompt fall back to leaving the alternate screen.
    fn start_workflow(&mut self, action: WorkflowAction) {
        if !action.is_streamable() || (action == WorkflowAction::Apply && apply_needs_tty()) {
            self.workflow_action = Some(action);
            return;
        }
        self.request_id += 1;
        let request = self.request_id;
        self.workflow_stream_request = Some(request);
        self.workflow_log = Some(WorkflowLog {
            title: action.title(),
            lines: Vec::new(),
            running: true,
            success: None,
            scroll: 0,
            follow: true,
        });
        self.status = format!("Running {}", action.display_command());
        spawn_workflow(self.tx.clone(), request, action);
    }

    /// Scroll the workflow log popup; any manual move unpins auto-follow so the
    /// user can read earlier output while the command keeps streaming.
    fn scroll_workflow_log(&mut self, delta: isize) {
        if let Some(log) = self.workflow_log.as_mut() {
            log.follow = false;
            let max = log.lines.len().saturating_sub(1);
            log.scroll = if delta.is_negative() {
                log.scroll.saturating_sub(delta.unsigned_abs())
            } else {
                log.scroll.saturating_add(delta as usize).min(max)
            };
        }
    }

    fn close_workflow_log(&mut self) {
        self.workflow_log = None;
        self.workflow_stream_request = None;
        self.sync_status();
    }

    pub fn workflow_finished(&mut self, action: &WorkflowAction, success: bool) {
        self.invalidate_after_mutation();
        self.load_current(true);
        self.status = if success {
            format!("{} completed; refreshing data", action.title())
        } else {
            format!("{} failed; inspect the command output", action.title())
        };
    }

    fn doctor_workflow(result: &Value) -> Option<WorkflowAction> {
        let action = result.get("action")?;
        match action.get("kind").and_then(Value::as_str)? {
            "run" => {
                let argv = action.get("argv")?.as_array()?;
                let values = argv.iter().filter_map(Value::as_str).collect::<Vec<_>>();
                (values == ["envy", "apply"]).then_some(WorkflowAction::Apply)
            }
            "open-app" if cfg!(target_os = "macos") => action
                .get("name")
                .and_then(Value::as_str)
                .map(|name| WorkflowAction::OpenApp(name.to_string())),
            "open-settings" if cfg!(target_os = "macos") => action
                .get("target")
                .and_then(Value::as_str)
                .map(|target| WorkflowAction::OpenSettings(target.to_string())),
            _ => None,
        }
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
                    let accepted = self
                        .state_for_target_mut(&target)
                        .filter(|state| state.request == Some(request))
                        .map(|state| {
                            state.payload = Some(payload);
                            state.loading = false;
                            state.error = None;
                            state.request = None;
                        })
                        .is_some();
                    if accepted {
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

    fn handle_input(&mut self, key: KeyEvent) -> bool {
        match self.input_mode {
            InputMode::Normal => false,
            InputMode::Search => {
                match key.code {
                    KeyCode::Esc => {
                        self.input_mode = InputMode::Normal;
                        self.query = self.submitted_query.clone();
                        self.status = "Search cancelled".to_string();
                    }
                    KeyCode::Enter => {
                        self.input_mode = InputMode::Normal;
                        self.scroll = 0;
                        self.search_selected = 0;
                        self.submitted_query = self.query.trim().to_string();
                        self.load_current(false);
                    }
                    KeyCode::Backspace => {
                        self.query.pop();
                    }
                    KeyCode::Char('u') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                        self.query.clear();
                    }
                    KeyCode::Char(character) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                        self.query.push(character);
                    }
                    _ => {}
                }
                true
            }
            InputMode::SoftwareFilter => {
                match key.code {
                    KeyCode::Esc => {
                        self.software_filter.clear();
                        self.software_selected = 0;
                        self.scroll = 0;
                        self.input_mode = InputMode::Normal;
                        self.status = "Software filter cleared".to_string();
                    }
                    KeyCode::Enter => {
                        self.input_mode = InputMode::Normal;
                        self.status = if self.software_filter.is_empty() {
                            "Software filter cleared".to_string()
                        } else {
                            format!("Filtered by ‘{}’", self.software_filter)
                        };
                    }
                    KeyCode::Backspace => {
                        self.software_filter.pop();
                        self.software_selected = 0;
                        self.scroll = 0;
                    }
                    KeyCode::Char('u') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                        self.software_filter.clear();
                        self.software_selected = 0;
                        self.scroll = 0;
                    }
                    KeyCode::Char(character) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                        self.software_filter.push(character);
                        self.software_selected = 0;
                        self.scroll = 0;
                    }
                    _ => {}
                }
                true
            }
            InputMode::SettingValue => {
                match key.code {
                    KeyCode::Esc => {
                        self.setting_edit = None;
                        self.input_mode = InputMode::Normal;
                        self.status = "Setting edit cancelled".to_string();
                    }
                    KeyCode::Enter => {
                        self.commit_setting_edit();
                    }
                    KeyCode::Backspace => {
                        if let Some(edit) = self.setting_edit.as_mut() {
                            edit.buffer.pop();
                        }
                    }
                    KeyCode::Char('u') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                        if let Some(edit) = self.setting_edit.as_mut() {
                            edit.buffer.clear();
                        }
                    }
                    KeyCode::Char(character) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                        if let Some(edit) = self.setting_edit.as_mut() {
                            edit.buffer.push(character);
                        }
                    }
                    _ => {}
                }
                true
            }
        }
    }

    pub fn handle_paste(&mut self, value: &str) {
        let normalized = value.split_whitespace().collect::<Vec<_>>().join(" ");
        if normalized.is_empty() {
            return;
        }
        match self.input_mode {
            InputMode::Search => self.query.push_str(&normalized),
            InputMode::SoftwareFilter => {
                self.software_filter.push_str(&normalized);
                self.software_selected = 0;
                self.scroll = 0;
            }
            InputMode::SettingValue => {
                if let Some(edit) = self.setting_edit.as_mut() {
                    edit.buffer.push_str(&normalized);
                }
            }
            InputMode::Normal => {}
        }
    }

    pub fn handle_mouse(&mut self, mouse: MouseEvent) {
        let direction = match mouse.kind {
            MouseEventKind::ScrollDown => 1,
            MouseEventKind::ScrollUp => -1,
            _ => return,
        };
        if self.workflow_log.is_some() {
            self.scroll_workflow_log(direction * 3);
            return;
        }
        if self.pending_mutation.is_some()
            || self.mutation_loading
            || self.overlay_error.is_some()
            || self.pending_setting.is_some()
            || self.setting_loading
            || self.pending_mirror.is_some()
            || self.mirror_loading
        {
            return;
        }
        if let Some(chooser) = self.mirror_chooser.as_mut() {
            chooser.selected = cycle_index(chooser.selected, chooser.sources.len(), direction);
            return;
        }
        if let Some(chooser) = self.setting_chooser.as_mut() {
            chooser.selected = cycle_index(chooser.selected, chooser.options.len(), direction);
            return;
        }
        if let Some(chooser) = self.group_chooser.as_mut() {
            chooser.selected = cycle_index(chooser.selected, chooser.groups.len(), direction);
            return;
        }
        if self.detail.is_some() {
            if direction > 0 {
                self.detail_scroll = self
                    .detail_scroll
                    .saturating_add(3)
                    .min(self.detail_scroll_max);
            } else {
                self.detail_scroll = self.detail_scroll.saturating_sub(3);
            }
            return;
        }
        self.move_selection(direction * 3);
    }

    pub fn handle_key(&mut self, key: KeyEvent) {
        if key.kind != KeyEventKind::Press {
            return;
        }
        if let Some(log) = self.workflow_log.as_ref() {
            let running = log.running;
            match key.code {
                KeyCode::Down | KeyCode::Char('j') => self.scroll_workflow_log(1),
                KeyCode::Up | KeyCode::Char('k') => self.scroll_workflow_log(-1),
                KeyCode::PageDown => self.scroll_workflow_log(CHOOSER_PAGE_STEP),
                KeyCode::PageUp => self.scroll_workflow_log(-CHOOSER_PAGE_STEP),
                KeyCode::End => {
                    if let Some(log) = self.workflow_log.as_mut() {
                        log.follow = true;
                        log.scroll = log.lines.len().saturating_sub(1);
                    }
                }
                KeyCode::Home => self.scroll_workflow_log(isize::MIN + 1),
                // The popup can only be dismissed once the command has finished;
                // there is no cancel, so a running workflow keeps the log open.
                KeyCode::Esc | KeyCode::Enter if !running => self.close_workflow_log(),
                _ => {}
            }
            return;
        }
        if self.overlay_error.is_some() {
            if matches!(key.code, KeyCode::Esc | KeyCode::Enter) {
                self.overlay_error = None;
                self.sync_status();
            }
            return;
        }
        if self.post_mutation.is_some() {
            match key.code {
                KeyCode::Char('p') => {
                    self.post_mutation = None;
                    self.start_workflow(WorkflowAction::Plan);
                }
                KeyCode::Char('a') => {
                    self.post_mutation = None;
                    self.start_workflow(WorkflowAction::Apply);
                }
                KeyCode::Char('d') => {
                    self.post_mutation = None;
                    self.start_workflow(WorkflowAction::Doctor);
                }
                KeyCode::Esc | KeyCode::Enter => {
                    self.post_mutation = None;
                    self.status = "Software change saved for later application".to_string();
                }
                _ => {}
            }
            return;
        }
        if self.workflow_confirmation.is_some() {
            match key.code {
                KeyCode::Enter | KeyCode::Char('y') => {
                    if let Some(action) = self.workflow_confirmation.take() {
                        self.start_workflow(action);
                    }
                }
                KeyCode::Esc | KeyCode::Char('n') => {
                    self.workflow_confirmation = None;
                    self.status = "Suggested action cancelled".to_string();
                }
                _ => {}
            }
            return;
        }
        if self.pending_setting.is_some() {
            match key.code {
                KeyCode::Enter | KeyCode::Char('y') if !self.setting_loading => {
                    self.apply_pending_setting();
                }
                KeyCode::Esc | KeyCode::Char('n') if !self.setting_loading => {
                    self.pending_setting = None;
                    self.status = "Setting change cancelled".to_string();
                }
                _ => {}
            }
            return;
        }
        if self.pending_mirror.is_some() {
            match key.code {
                KeyCode::Enter | KeyCode::Char('y') if !self.mirror_loading => {
                    self.apply_pending_mirror();
                }
                KeyCode::Esc | KeyCode::Char('n') if !self.mirror_loading => {
                    self.pending_mirror = None;
                    self.status = "Mirror change cancelled".to_string();
                }
                _ => {}
            }
            return;
        }
        if matches!(key.code, KeyCode::Char('r')) {
            if let Some(target) = self
                .mirror_chooser
                .as_ref()
                .map(|chooser| chooser.target.clone())
            {
                self.begin_mirror_measure(target, true);
                return;
            }
        }
        if let Some(chooser) = self.mirror_chooser.as_mut() {
            match key.code {
                KeyCode::Down | KeyCode::Char('j') => {
                    chooser.selected = cycle_index(chooser.selected, chooser.sources.len(), 1);
                }
                KeyCode::Up | KeyCode::Char('k') => {
                    chooser.selected = cycle_index(chooser.selected, chooser.sources.len(), -1);
                }
                KeyCode::PageDown => {
                    chooser.selected =
                        cycle_index(chooser.selected, chooser.sources.len(), CHOOSER_PAGE_STEP);
                }
                KeyCode::PageUp => {
                    chooser.selected =
                        cycle_index(chooser.selected, chooser.sources.len(), -CHOOSER_PAGE_STEP);
                }
                KeyCode::Home | KeyCode::Char('g') => chooser.selected = 0,
                KeyCode::End | KeyCode::Char('G') => {
                    chooser.selected = chooser.sources.len().saturating_sub(1);
                }
                KeyCode::Enter => {
                    if let Some(source) = chooser.sources.get(chooser.selected).cloned() {
                        self.begin_mirror_mutation(source);
                    }
                }
                KeyCode::Esc => {
                    self.mirror_chooser = None;
                    self.cancel_mirror_measure();
                    self.status = "Mirror source selection cancelled".to_string();
                }
                _ => {}
            }
            return;
        }
        if let Some(chooser) = self.setting_chooser.as_mut() {
            match key.code {
                KeyCode::Down | KeyCode::Char('j') => {
                    chooser.selected = cycle_index(chooser.selected, chooser.options.len(), 1);
                }
                KeyCode::Up | KeyCode::Char('k') => {
                    chooser.selected = cycle_index(chooser.selected, chooser.options.len(), -1);
                }
                KeyCode::Enter => self.confirm_setting_choice(),
                KeyCode::Esc => self.setting_chooser = None,
                _ => {}
            }
            return;
        }
        if self.pending_mutation.is_some() {
            match key.code {
                KeyCode::Enter | KeyCode::Char('y') if !self.mutation_loading => {
                    self.apply_pending_mutation();
                }
                KeyCode::Esc | KeyCode::Char('n') if !self.mutation_loading => {
                    self.pending_mutation = None;
                    self.status = "Software change cancelled".to_string();
                }
                _ => {}
            }
            return;
        }
        if let Some(chooser) = self.group_chooser.as_mut() {
            match key.code {
                KeyCode::Down | KeyCode::Char('j') => {
                    chooser.selected = cycle_index(chooser.selected, chooser.groups.len(), 1);
                }
                KeyCode::Up | KeyCode::Char('k') => {
                    chooser.selected = cycle_index(chooser.selected, chooser.groups.len(), -1);
                }
                KeyCode::Enter => self.confirm_group(),
                KeyCode::Esc => self.group_chooser = None,
                _ => {}
            }
            return;
        }
        if self.detail.is_some() {
            match key.code {
                KeyCode::Char('x') => {
                    let action = match self.detail.as_ref() {
                        Some(DetailView::Doctor(result)) => Self::doctor_workflow(result),
                        _ => None,
                    };
                    if let Some(action) = action {
                        self.detail = None;
                        self.workflow_confirmation = Some(action);
                    } else {
                        self.status = "This detail has no safe executable action".to_string();
                    }
                }
                KeyCode::Down | KeyCode::Char('j') => {
                    self.detail_scroll = self
                        .detail_scroll
                        .saturating_add(1)
                        .min(self.detail_scroll_max)
                }
                KeyCode::Up | KeyCode::Char('k') => {
                    self.detail_scroll = self.detail_scroll.saturating_sub(1)
                }
                KeyCode::Esc | KeyCode::Enter => {
                    self.detail = None;
                    self.detail_request = None;
                    self.group_request = None;
                    self.detail_scroll = 0;
                    self.detail_scroll_max = 0;
                }
                _ => {}
            }
            return;
        }
        if self.mutation_loading || self.setting_loading {
            return;
        }
        if self.mirror_loading {
            return;
        }
        if self.show_help {
            if matches!(key.code, KeyCode::Esc | KeyCode::Char('?') | KeyCode::Enter) {
                self.show_help = false;
            }
            return;
        }
        if self.handle_input(key) {
            return;
        }

        match key.code {
            KeyCode::Char('q') => self.should_quit = true,
            KeyCode::Esc if self.screen == Screen::Software && !self.software_filter.is_empty() => {
                self.software_filter.clear();
                self.software_selected = 0;
                self.scroll = 0;
                self.status = "Software filter cleared".to_string();
            }
            KeyCode::Esc if self.screen == Screen::History && self.history_marked.is_some() => {
                self.history_marked = None;
                self.status = "Generation mark cleared".to_string();
            }
            KeyCode::Esc => {
                self.status = "Nothing to dismiss; press q to quit".to_string();
            }
            KeyCode::Char('?') => self.show_help = true,
            KeyCode::Char('r') => self.load_current(true),
            KeyCode::Tab | KeyCode::Right => self.next_section(1),
            KeyCode::BackTab | KeyCode::Left => self.next_section(-1),
            KeyCode::Char('v') => self.next_subpage(),
            KeyCode::Char('/') if self.screen == Screen::Software => {
                self.input_mode = InputMode::SoftwareFilter;
                self.status = "Type to filter locally; Enter keeps it, Esc exits input".to_string();
            }
            KeyCode::Char('/') | KeyCode::Char('s') if self.screen == Screen::Search => {
                self.input_mode = InputMode::Search;
                self.status = "Type a query and press Enter".to_string();
            }
            KeyCode::Char('/') | KeyCode::Char('s') => {
                self.set_screen(Screen::Search);
                self.input_mode = InputMode::Search;
                self.sync_status();
            }
            KeyCode::Char(character) if ('1'..='3').contains(&character) => {
                let index = character as usize - '1' as usize;
                self.set_section(NavigationSection::ALL[index]);
            }
            KeyCode::Enter if self.screen == Screen::Config => {
                self.begin_setting_edit();
            }
            KeyCode::Enter if self.screen == Screen::Mirror => {
                self.begin_mirror_sources();
            }
            KeyCode::Enter | KeyCode::Char(' ') if self.screen == Screen::Software => {
                self.begin_selected_mutation();
            }
            KeyCode::Char('w') | KeyCode::Char('i') if self.screen == Screen::Software => {
                self.begin_why();
            }
            KeyCode::Enter | KeyCode::Char('a') if self.screen == Screen::Search => {
                self.begin_search_add();
            }
            KeyCode::Enter | KeyCode::Char('i') if self.screen == Screen::Doctor => {
                self.open_doctor_detail();
            }
            KeyCode::Char(' ')
                if self.screen == Screen::History
                    && self.history_view == HistoryView::Generations =>
            {
                self.history_marked = generation_number(self.payload(), self.history_selected);
                if let Some(number) = self.history_marked {
                    self.status = format!("Generation {number} marked; select another and press d");
                }
            }
            KeyCode::Enter | KeyCode::Char('d')
                if self.screen == Screen::History
                    && self.history_view == HistoryView::Generations =>
            {
                self.begin_history_diff();
            }
            KeyCode::Home | KeyCode::Char('g') => self.move_selection_to(0),
            KeyCode::End | KeyCode::Char('G') => {
                self.move_selection_to(self.row_count().saturating_sub(1));
            }
            KeyCode::PageDown => self.move_selection_page(1),
            KeyCode::PageUp => self.move_selection_page(-1),
            KeyCode::Down | KeyCode::Char('j') => self.move_selection(1),
            KeyCode::Up | KeyCode::Char('k') => self.move_selection(-1),
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::mpsc;

    use serde_json::json;

    use super::*;
    use crate::model::SettingKey;

    fn app() -> App {
        let (tx, rx) = mpsc::channel();
        App::new(tx, rx)
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
}
