//! Multi-step flows: software, settings, and mirror mutations, details, and workflows.

use serde_json::Value;

use crate::{
    backend::{
        apply_needs_tty, spawn_history_diff, spawn_mirror_measure, spawn_mirror_mutation,
        spawn_mirror_sources, spawn_mutation, spawn_search_groups, spawn_setting,
        spawn_software_why, spawn_workflow,
    },
    model::{
        compatible_groups, doctor_results, generation_number, mirror_target_rows, search_entries,
        DetailView, GroupChooser, MirrorSource, MutationIntent, MutationStage, PendingSetting,
        Screen, SettingChooser, SettingEdit, SoftwareAction, WorkflowAction, WorkflowLog,
    },
};

use super::{App, InputMode};

impl App {
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

    pub(super) fn selected_source(&self) -> Option<Value> {
        self.payload()?
            .get("sources")?
            .as_array()?
            .get(self.sources_selected)
            .cloned()
    }

    pub(super) fn begin_source_mutation(&mut self) {
        let Some(source) = self.selected_source() else {
            return;
        };
        let id = source["id"].as_str().unwrap_or("").to_string();
        let group = match source["type"].as_str() {
            Some("native") if source["parameters"]["system"] != true => "native.system.repository",
            Some("tap") => "homebrew.system.repository",
            _ => {
                self.status =
                    "Use the source CLI for definitions and shared Nix registration".into();
                return;
            }
        };
        self.begin_mutation(MutationIntent {
            source_id: Some(id.clone()),
            source_operation: true,
            action: if source["selected"] == true {
                SoftwareAction::Remove
            } else {
                SoftwareAction::Add
            },
            group: group.into(),
            operand: id.clone(),
            item: id
                .split_once(':')
                .map(|(_, name)| name)
                .unwrap_or("")
                .to_string(),
        });
    }

    pub(super) fn begin_selected_mutation(&mut self) {
        let Some(entry) = self.software_entries().get(self.software_selected).cloned() else {
            self.status = "No software item selected".to_string();
            return;
        };
        self.begin_mutation(MutationIntent {
            source_id: None,
            source_operation: false,
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

    pub(super) fn apply_pending_mutation(&mut self) {
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

    pub(super) fn begin_why(&mut self) {
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

    pub(super) fn begin_search_add(&mut self) {
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

    pub(super) fn open_group_chooser(
        &mut self,
        entry: crate::model::SearchEntry,
        payload: Option<&Value>,
    ) {
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

    pub(super) fn confirm_group(&mut self) {
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
            source_id: chooser.entry.source_id.clone(),
            source_operation: false,
            action: SoftwareAction::Add,
            group: group.id.clone(),
            operand,
            item: chooser.entry.name.clone(),
        });
    }

    pub(super) fn open_doctor_detail(&mut self) {
        let Some(result) = doctor_results(self.payload(), self.doctor_problems_only)
            .get(self.doctor_selected)
            .map(|result| (*result).clone())
        else {
            return;
        };
        self.detail = Some(DetailView::Doctor(result));
        self.detail_scroll = 0;
        self.detail_scroll_max = 0;
    }

    pub(super) fn begin_history_diff(&mut self) {
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

    pub(super) fn invalidate_after_mutation(&mut self) {
        for screen in [
            Screen::Home,
            Screen::Software,
            Screen::Sources,
            Screen::Doctor,
        ] {
            self.pages.remove(&screen);
        }
        self.search_cache.clear();
        self.search_order.clear();
        self.scroll = 0;
    }

    /// Begin editing the selected Settings row: enumerated fields open a
    /// dropdown; freeform fields open a text input pre-filled with the value.
    pub(super) fn begin_setting_edit(&mut self) {
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

    pub(super) fn confirm_setting_choice(&mut self) {
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

    pub(super) fn commit_setting_edit(&mut self) {
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

    pub(super) fn apply_pending_setting(&mut self) {
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
    pub(super) fn refresh_after_setting(&mut self) {
        for screen in [Screen::Home, Screen::Hosts, Screen::Config] {
            self.pages.remove(&screen);
        }
        self.load_current(true);
    }

    pub(super) fn begin_mirror_sources(&mut self) {
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

    pub(super) fn begin_mirror_measure(&mut self, target: String, refresh: bool) {
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

    pub(super) fn cancel_mirror_measure(&mut self) {
        self.mirror_measure_request = None;
        self.mirror_measuring = false;
    }

    pub(super) fn begin_mirror_mutation(&mut self, source: MirrorSource) {
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

    pub(super) fn apply_pending_mirror(&mut self) {
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

    pub(super) fn invalidate_after_mirror(&mut self) {
        for screen in [
            Screen::Home,
            Screen::Software,
            Screen::Sources,
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
    pub(super) fn start_workflow(&mut self, action: WorkflowAction) {
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
    pub(super) fn scroll_workflow_log(&mut self, delta: isize) {
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

    pub(super) fn close_workflow_log(&mut self) {
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

    pub fn doctor_workflow(result: &Value) -> Option<WorkflowAction> {
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
}
