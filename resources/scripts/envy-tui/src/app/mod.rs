mod flows;
mod keys;
mod loading;
#[cfg(test)]
mod tests;

use std::{
    collections::{HashMap, VecDeque},
    path::PathBuf,
    sync::mpsc::{Receiver, Sender},
    time::{SystemTime, UNIX_EPOCH},
};

use serde_json::Value;

use crate::{
    model::{
        count_rows, doctor_results, filtered_software_entries, generation_rows, mirror_target_rows,
        result_rows, settings_rows, DetailView, GroupChooser, HistoryView, LoadTarget, Message,
        MirrorChooser, MirrorPreview, MutationPreview, NavigationSection, PageState, Pages,
        PendingSetting, Screen, SettingChooser, SettingEdit, SettingRow, WorkflowAction,
        WorkflowLog,
    },
    snapshot,
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
    pub sources_selected: usize,
    pub search_selected: usize,
    pub doctor_selected: usize,
    /// Health lists only warnings and errors.
    pub doctor_problems_only: bool,
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
    /// Where the last Home payload is kept between sessions; unset in tests.
    pub snapshot_path: Option<PathBuf>,
    /// Current Unix time override, so tests render stable relative ages.
    pub fixed_now: Option<i64>,
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
            sources_selected: 0,
            search_selected: 0,
            doctor_selected: 0,
            doctor_problems_only: false,
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
            snapshot_path: None,
            fixed_now: None,
        }
    }

    pub fn now(&self) -> i64 {
        self.fixed_now.unwrap_or_else(|| {
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|elapsed| elapsed.as_secs() as i64)
                .unwrap_or(0)
        })
    }

    /// Show the Home payload saved by the previous session until a fresh one
    /// arrives; `load_current` still refreshes it.
    pub fn restore_home_snapshot(&mut self) {
        let Some((payload, saved_at)) = self.snapshot_path.as_deref().and_then(snapshot::load)
        else {
            return;
        };
        let state = self.pages.entry(Screen::Home).or_default();
        if state.payload.is_none() {
            state.payload = Some(payload);
            state.saved_at = Some(saved_at);
        }
    }

    /// Age of a page restored from the previous session, such as "3 h ago".
    pub fn saved_age(&self, screen: Screen) -> Option<String> {
        let saved_at = self.pages.get(&screen)?.saved_at?;
        Some(crate::model::relative_age(saved_at, self.now()))
    }

    /// Doctor summary from Health or, before it loads, from Home.
    pub fn doctor_summary(&self) -> Option<&Value> {
        let page = |screen| {
            self.pages
                .get(&screen)
                .and_then(|state: &PageState| state.payload.as_ref())
        };
        page(Screen::Doctor)
            .and_then(|doctor| doctor.get("summary"))
            .or_else(|| page(Screen::Home).and_then(|home| home.pointer("/doctor/summary")))
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
                const VIEWS: [Screen; 5] = [
                    Screen::Software,
                    Screen::Search,
                    Screen::Sources,
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
            Screen::Sources => count_rows(self.payload(), "sources"),
            Screen::Search => result_rows(self.payload()),
            Screen::Doctor => doctor_results(self.payload(), self.doctor_problems_only).len(),
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
            Screen::Sources => self.sources_selected,
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
            Screen::Sources => self.sources_selected = value,
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
}
