//! Storage action routing, isolated from read-only Storage navigation.

use super::App;
use crate::storage::actions::{Field, Pending, Stage, Task};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

impl App {
    fn storage_action_context(&self) -> String {
        let home = self
            .pages
            .get(&crate::model::Screen::Home)
            .and_then(|p| p.payload.as_ref());
        serde_json::json!({"epoch": self.storage_actions.epoch,
            "machine": home.and_then(|v| v.get("machine")),
            "config": home.and_then(|v| v.get("config")),
            "source": home.and_then(|v| v.get("source")),
            "git": home.and_then(|v| v.get("git"))})
        .to_string()
    }

    pub(super) fn open_storage_actions(&mut self) {
        self.storage_actions.open = true;
        if self.storage_actions.capabilities.is_none() && self.storage_actions.pending.is_none() {
            self.start_storage_task(Task::Capabilities);
        }
    }

    pub(super) fn start_storage_task(&mut self, task: Task) {
        if self.storage_actions.pending.is_some() {
            return;
        }
        if let Err(error) = task.argv() {
            self.storage_actions.notice = error;
            return;
        }
        self.request_id += 1;
        let request = self.request_id;
        let context = self.storage_action_context();
        let cancel = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        self.storage_actions.preview = None;
        self.storage_actions.stage = Stage::Busy;
        self.storage_actions.field = None;
        self.storage_actions.progress.clear();
        self.storage_actions.scroll = 0;
        self.storage_actions.notice = match &task {
            Task::Capabilities => "Reading capabilities only; no tool probe or scan",
            Task::Plan => {
                "Saving a plan: candidate scan and local state writes; no move or deletion"
            }
            Task::Preview(_) => {
                "Preparing bound preview; receipt is saved locally. No execution confirmation sent"
            }
            Task::Execute(..) => "Executing once; cancelling cannot roll back external effects",
        }
        .into();
        self.storage_actions.pending = Some(Pending {
            id: request,
            context,
            task: task.clone(),
            cancel: cancel.clone(),
        });
        crate::backend::spawn_storage_action(self.tx.clone(), request, task, cancel);
    }

    pub(super) fn storage_preview(&mut self) {
        if self.storage_actions.capabilities_context.as_ref()
            != Some(&self.storage_action_context())
        {
            self.storage_actions.invalidate();
            self.storage_actions.notice =
                "Capabilities context changed; reload capabilities and preview again".into();
            return;
        }
        match self.storage_actions.intent() {
            Ok(intent) => self.start_storage_task(Task::Preview(intent)),
            Err(error) => self.storage_actions.notice = error,
        }
    }

    fn confirm_storage(&mut self) {
        if self.storage_actions.stage != Stage::Confirm || self.storage_actions.pending.is_some() {
            return;
        }
        let Some((intent, token, expires)) = self.storage_actions.preview.take() else {
            return;
        };
        if expires <= self.now() as f64
            || self.storage_actions.preview_context.as_ref() != Some(&self.storage_action_context())
            || self.storage_actions.intent().as_ref() != Ok(&intent)
        {
            self.storage_actions.stage = Stage::Editing;
            self.storage_actions.notice =
                "Preview expired or selection changed; request a fresh preview".into();
            return;
        }
        self.start_storage_task(Task::Execute(intent, token));
    }

    pub(super) fn receive_storage_action(
        &mut self,
        request: u64,
        result: Result<crate::transport::JsonResponse, crate::transport::TransportError>,
    ) {
        let context = self.storage_action_context();
        let Some(pending) = self
            .storage_actions
            .pending
            .as_ref()
            .filter(|p| p.id == request)
        else {
            return;
        };
        if pending.context != context {
            self.storage_actions.invalidate();
            return;
        }
        let pending = self.storage_actions.pending.take().unwrap();
        let cancelled = pending.cancel.load(std::sync::atomic::Ordering::Relaxed);
        // The request identity also captures all IDs, target, and action selectors.
        if let Task::Preview(intent) = &pending.task {
            if self.storage_actions.intent().as_ref() != Ok(intent) {
                self.storage_actions.changed();
                self.storage_actions.notice = "Old preview ignored; input changed".into();
                return;
            }
        }
        let now = self.now();
        self.storage_actions.accept(
            &pending.task,
            crate::model::WorkflowReport::from_result(result),
            now,
        );
        if self.storage_actions.preview.is_some() {
            self.storage_actions.preview_context = Some(context.clone());
        }
        if matches!(pending.task, Task::Capabilities) && self.storage_actions.capabilities.is_some()
        {
            self.storage_actions.capabilities_context = Some(context);
        }
        if cancelled {
            self.storage_actions.preview = None;
            self.storage_actions.stage = Stage::Result;
            self.storage_actions.notice = "Request stopped or completed after cancellation; review returned evidence. External effects may be unknown; no rollback or replay.".into();
        }
        // refreshedStatus is rendered from the result itself. Never trigger a
        // follow-up inspect, implicit plan, or another mutation here.
    }

    pub(super) fn storage_action_output(&mut self, request: u64, chunk: String) {
        if self
            .storage_actions
            .pending
            .as_ref()
            .is_some_and(|p| p.id == request && p.context == self.storage_action_context())
        {
            const LIMIT: usize = 256 * 1024;
            let progress = &mut self.storage_actions.progress;
            let remaining = LIMIT.saturating_sub(progress.len());
            let mut end = chunk.len().min(remaining);
            while !chunk.is_char_boundary(end) {
                end -= 1;
            }
            progress.push_str(&chunk[..end]);
        }
    }

    pub(super) fn storage_actions_key(&mut self, key: KeyEvent) {
        if let Some(field) = self.storage_actions.field {
            match key.code {
                KeyCode::Esc => {
                    self.storage_actions.field = None;
                    self.storage_actions.buffer.clear();
                }
                KeyCode::Enter => {
                    let value = std::mem::take(&mut self.storage_actions.buffer);
                    match field {
                        Field::Plan => self.storage_actions.plan_id = value,
                        Field::Run => self.storage_actions.run_id = value,
                        Field::Target => self.storage_actions.target = value,
                    }
                    self.storage_actions.field = None;
                    self.storage_actions.changed();
                }
                KeyCode::Backspace => {
                    self.storage_actions.buffer.pop();
                }
                KeyCode::Char('u') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                    self.storage_actions.buffer.clear()
                }
                KeyCode::Char(ch) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                    self.storage_actions.buffer.push(ch)
                }
                _ => {}
            }
            return;
        }
        // Scrolling and cancellation remain available while a child runs.
        match key.code {
            KeyCode::Down | KeyCode::Char('j') => {
                self.storage_actions.scroll = self
                    .storage_actions
                    .scroll
                    .saturating_add(1)
                    .min(self.storage_actions.scroll_max)
            }
            KeyCode::Up | KeyCode::Char('k') => {
                self.storage_actions.scroll = self.storage_actions.scroll.saturating_sub(1)
            }
            KeyCode::PageDown => {
                self.storage_actions.scroll = self
                    .storage_actions
                    .scroll
                    .saturating_add(12)
                    .min(self.storage_actions.scroll_max)
            }
            KeyCode::PageUp => {
                self.storage_actions.scroll = self.storage_actions.scroll.saturating_sub(12)
            }
            KeyCode::Home => self.storage_actions.scroll = 0,
            KeyCode::End => self.storage_actions.scroll = self.storage_actions.scroll_max,
            KeyCode::Esc => {
                if self.storage_actions.handoff.is_some() {
                    self.storage_actions.handoff = None;
                    self.storage_actions.scroll = 0;
                } else {
                    self.storage_actions.cancel();
                }
            }
            KeyCode::Char('q') if self.storage_actions.pending.is_none() => {
                self.storage_actions.cancel();
                self.storage_actions.open = false;
            }
            _ if self.storage_actions.pending.is_some() => {}
            KeyCode::Char('h') if self.storage_actions.handoff.is_some() => {
                self.storage_actions.handoff = self.storage_actions.handoff.map(|n| (n + 1) % 3);
                self.storage_actions.scroll = 0;
            }
            _ if self.storage_actions.handoff.is_some() => {}
            KeyCode::Tab => {
                self.storage_actions.operation = self.storage_actions.operation.next();
                self.storage_actions.changed();
            }
            KeyCode::Char(ch @ '1'..='8') => {
                let index = (ch as u8 - b'1') as usize;
                let id = crate::storage::actions::ACTION_IDS[index];
                if self.storage_actions.selectable(id) {
                    self.storage_actions.selected[index] = !self.storage_actions.selected[index];
                    self.storage_actions.changed();
                } else {
                    self.storage_actions.notice =
                        format!("{id}: backend policy opt-in required or metadata unavailable");
                }
            }
            KeyCode::Char('i' | 'r' | 't') => {
                let (field, buffer) = match key.code {
                    KeyCode::Char('i') => (Field::Plan, self.storage_actions.plan_id.clone()),
                    KeyCode::Char('r') => (Field::Run, self.storage_actions.run_id.clone()),
                    _ => (Field::Target, self.storage_actions.target.clone()),
                };
                self.storage_actions.changed();
                self.storage_actions.field = Some(field);
                self.storage_actions.buffer = buffer;
            }
            KeyCode::Char('s') => self.start_storage_task(Task::Plan),
            KeyCode::Char('p') => self.storage_preview(),
            KeyCode::Char('c') => {
                self.storage_actions.capabilities = None;
                self.storage_actions.selected = [false; 8];
                self.start_storage_task(Task::Capabilities);
            }
            KeyCode::Char('h') => {
                let next = self
                    .storage_actions
                    .handoff
                    .map(|n| (n + 1) % 3)
                    .unwrap_or(0);
                self.storage_actions.changed();
                self.storage_actions.handoff = Some(next);
            }
            KeyCode::Enter if self.storage_actions.stage == Stage::Review => {
                if self
                    .storage_actions
                    .preview
                    .as_ref()
                    .is_some_and(|(_, _, expiry)| *expiry > self.now() as f64)
                {
                    self.storage_actions.stage = Stage::Confirm;
                } else {
                    self.storage_actions.changed();
                    self.storage_actions.notice = "Preview expired; request a fresh preview".into();
                }
            }
            KeyCode::Char('y') => self.confirm_storage(),
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests;
