import { stripVTControlCharacters } from "node:util";

interface QueueRpcRow {
  readonly id: string;
  readonly lane: "steer" | "followUp";
  readonly text: string;
  readonly paused?: boolean;
  readonly images?: readonly unknown[];
  readonly dispatchHead?: boolean;
  readonly dispatchBatch?: boolean;
  readonly held?: boolean;
  readonly rowPaused?: boolean;
}

export interface QueueRpcPlan {
  revision: string;
  state: "paused" | "row-paused" | "editing" | "control" | "steer-first" | "ready" | "after-run";
  headId: string | null;
  mode: "one-at-a-time" | "all";
  errorHold: boolean;
  editor: { id: string; owner: string; text: string } | null;
}

function preview(value: string, limit: number): string {
  const text = stripVTControlCharacters(value.slice(0, 16384))
    .replace(/[\s\p{Cc}]+/gu, " ").trim();
  return text.length > limit || value.length > 16384
    ? text.slice(0, limit).replace(/[\uD800-\uDBFF]$/u, "") + "…"
    : text;
}

export const QUEUE_MESSAGE_WIDGET_KEY = "queue-messages";
export const QUEUE_CONTROL_COMMAND = "queue-control";
export const QUEUE_CONTROL_RESULT_WIDGET_KEY = "queue-control-result";
export type QueueRpcAction = "pause" | "row-pause" | "remove" | "move" | "begin-edit" | "save-edit" | "cancel-edit" | "renew-edit";
export interface QueueRpcControl {
  action: QueueRpcAction;
  requestId?: string;
  revision: string;
  id?: string;
  owner?: string;
  paused?: boolean;
  direction?: -1 | 1;
  text?: string;
}

export function parseQueueRpcControl(args: string): QueueRpcControl {
  if (args.length > 400000) throw new Error("Queue control request is too large");
  let value: QueueRpcControl;
  try { value = JSON.parse(Buffer.from(args, "base64url").toString("utf8")); }
  catch { throw new Error("Invalid queue control request"); }
  const actions = ["pause", "row-pause", "remove", "move", "begin-edit", "save-edit", "cancel-edit", "renew-edit"];
  const validString = (input: unknown, limit: number) => typeof input === "string" && input.length > 0 && input.length <= limit;
  if (!value || typeof value !== "object" || !actions.includes(value.action)
      || !validString(value.revision, 128)
      || (value.action !== "pause" && !validString(value.id, 256))
      || (["pause", "row-pause"].includes(value.action) && typeof value.paused !== "boolean")
      || (value.action === "move" && value.direction !== -1 && value.direction !== 1)
      || (value.action.endsWith("edit") && !validString(value.owner, 128))
      || (value.action === "save-edit" && (typeof value.text !== "string" || value.text.length > 65536))) {
    throw new Error("Invalid queue control request");
  }
  return value;
}

export function queueRpcMessages(rows: readonly QueueRpcRow[], paused: boolean, activity?: string, plan?: QueueRpcPlan) {
  const pending = rows.filter(row => row.lane === "followUp");
  if (pending.length === 0) return undefined;
  return {
    version: 1,
    total: pending.length,
    paused,
    activity: activity ? preview(activity, 63) : null,
    ...(plan ? { plan } : {}),
    items: pending.slice(0, 64).map(row => {
      const text = stripVTControlCharacters(row.text.slice(0, 16384))
        .replace(/\r\n?/g, "\n")
        .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/g, "")
        .replace(/[\uD800-\uDFFF]/gu, "\ufffd");
      return {
        id: preview(row.id, 255),
        text: text.slice(0, 4000).replace(/[\uD800-\uDBFF]$/u, ""),
        paused: row.rowPaused ?? row.paused === true,
        imageCount: row.images?.length ?? 0,
        truncated: text.length > 4000 || row.text.length > 16384,
        ...(plan ? { dispatchHead: row.dispatchHead === true, dispatchBatch: row.dispatchBatch === true, held: row.held === true } : {}),
      };
    }),
  };
}

export function queueRpcStatus(rows: readonly QueueRpcRow[], paused: boolean, activity?: string): string | undefined {
  const pending = rows.filter(row => row.lane === "followUp");
  if (pending.length === 0) return undefined;
  const parts = [
    "Queue: " + pending.length + " pending; " + (paused ? "paused" : "active")
      + (activity ? "; control: " + preview(activity, 64) : ""),
    ...pending.slice(0, 8).map(row =>
      preview(row.id, 64) + (row.paused ? " (held)" : "") + ": "
        + (preview(row.text, 400) || "(no text)")),
  ];
  if (pending.length > 8) parts.push("+" + (pending.length - 8) + " more");
  return parts.join(" | ");
}
