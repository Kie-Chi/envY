//! Keyboard, paste, and mouse input.

use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseEvent, MouseEventKind};

use crate::model::{
    generation_number, DetailView, HistoryView, NavigationSection, Screen, WorkflowAction,
};

use super::{cycle_index, App, InputMode, CHOOSER_PAGE_STEP};

impl App {
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
            KeyCode::Char('s') if self.screen == Screen::Sources => {
                if let Some(source) = self.selected_source() {
                    self.workflow_confirmation = Some(WorkflowAction::SyncSource(
                        source["id"].as_str().unwrap_or("").into(),
                    ));
                }
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
            KeyCode::Enter if self.screen == Screen::Sources => self.begin_source_mutation(),
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
            KeyCode::Char('f') if self.screen == Screen::Doctor => {
                self.doctor_problems_only = !self.doctor_problems_only;
                self.doctor_selected = 0;
                self.scroll = 0;
                let count = self.row_count();
                self.status = if self.doctor_problems_only {
                    format!("Showing {count} warning(s) and error(s)")
                } else {
                    format!("Showing all {count} checks")
                };
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
