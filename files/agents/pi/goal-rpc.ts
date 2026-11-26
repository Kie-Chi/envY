import { stripVTControlCharacters } from "node:util";
import type { ExtensionContext } from "@earendil-works/pi-coding-agent";
import type { GoalRecord } from "./goal-record.ts";
import type { GoalSettings } from "./goal-settings.ts";
import { deriveGoalDashboardModel } from "./widgets/goal-dashboard-model.ts";
import type { AuditorWidgetProgress } from "./widgets/goal-widget.ts";
import type { AuditVerdict } from "./widgets/auditor-dashboard-model.ts";

export const GOAL_TASK_WIDGET_KEY = "goal-tasks";

interface GoalRpcOptions {
  settings: GoalSettings;
  otherOpenGoals: number;
  auditProgress: AuditorWidgetProgress | null;
  auditResult: { verdict: AuditVerdict } | null;
  stalled: boolean;
}

function label(value: string, limit = 100): string {
  const text = stripVTControlCharacters(value).replace(/\s+|\p{Cc}/gu, " ").trim();
  const chars = Array.from(text);
  return chars.length > limit ? chars.slice(0, limit - 1).join("") + "…" : text;
}

export function goalRpcStatus(goal: GoalRecord | null, options: GoalRpcOptions): string | undefined {
  if (!goal) {
    return options.otherOpenGoals > 0 && !options.settings.hideUnfocusedBanner
      ? `Goal: unfocused (${options.otherOpenGoals} open)` : undefined;
  }
  const model = deriveGoalDashboardModel(goal, {
    focused: true,
    otherOpenGoals: options.otherOpenGoals,
    tasksDisabled: options.settings.disableTasks === true,
  })!;
  const status = options.auditProgress && options.auditProgress.phase !== "done"
    ? "Auditing" : options.stalled && goal.status === "active" ? "Stalled" : model.status.label;
  const parts = [`Goal: ${label(model.title, 80)}`, status];
  const current = model.currentTask;
  if (current) {
    const roots = model.taskTree.filter(task => task.depth === 0);
    const index = model.taskTree.findIndex(task => task.id === current.id);
    const stage = model.taskTree.slice(0, index + 1).findLast(task => task.depth === 0);
    if (stage) {
      const prefix = current.inferred ? "Next phase" : "Phase";
      parts.push(`${prefix} ${roots.findIndex(task => task.id === stage.id) + 1}/${roots.length}: ${label(stage.title)}`);
      if (stage.id !== current.id) parts.push(`Task: ${label(current.title)}`);
    }
  }
  if (model.taskProgress) parts.push(`${model.taskProgress.completed}/${model.taskProgress.total} done`);
  if (model.status.reason) parts.push(`Reason: ${label(model.status.reason, 120)}`);
  if (options.auditResult) parts.push(`Audit: ${options.auditResult.verdict}`);
  return parts.join(" | ");
}

export function goalRpcTasks(goal: GoalRecord | null, options: GoalRpcOptions) {
  if (!goal || options.settings.disableTasks) return undefined;
  const model = deriveGoalDashboardModel(goal, { focused: true, otherOpenGoals: options.otherOpenGoals })!;
  if (model.taskTree.length === 0) return undefined;
  return {
    version: 1,
    goalId: model.goalId,
    title: label(model.title, 200),
    status: model.status.code,
    completed: model.taskProgress?.completed ?? 0,
    total: model.taskProgress?.total ?? 0,
    currentTaskId: model.currentTask?.id ?? null,
    currentTaskInferred: model.currentTask?.inferred === true,
    tasks: model.taskTree.map(task => ({
      id: task.id, title: label(task.title, 1000), status: task.status, depth: task.depth,
    })),
  };
}

export function createGoalRpcDisplay() {
  let previous: string | undefined;
  let previousTasks: string | undefined | null = null;
  return {
    reset(ctx: ExtensionContext): void {
      previous = undefined;
      previousTasks = null;
      if (ctx.hasUI && ctx.mode === "rpc") ctx.ui.setWidget(GOAL_TASK_WIDGET_KEY, undefined);
    },
    update(ctx: ExtensionContext, goal: GoalRecord | null, options: GoalRpcOptions): void {
      const text = goalRpcStatus(goal, options);
      const tasks = goalRpcTasks(goal, options);
      const snapshot = tasks ? JSON.stringify(tasks) : undefined;
      if (snapshot !== previousTasks) {
        previousTasks = snapshot;
        ctx.ui.setWidget(GOAL_TASK_WIDGET_KEY, snapshot ? [snapshot] : undefined);
      }
      const key = JSON.stringify([goal?.id, text]);
      if (key === previous) return;
      previous = key;
      ctx.ui.setStatus("goal", text);
      // A durable work-log checkpoint complements the compact mobile status.
      // Usage and elapsed time stay out of the key so streaming never floods it.
      if (goal && text) ctx.ui.notify(text, "info");
    },
  };
}
