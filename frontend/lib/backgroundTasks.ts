"use client";

/**
 * Cross-page registry of long-running backend tasks (batch scrape, discovery,
 * match) started by this user in this browser. Pages register a task when
 * they enqueue it; the global BackgroundActivity widget polls the status of
 * every registered task so progress stays visible while navigating between
 * modules. Persisted in localStorage so it survives refreshes.
 */

export type BackgroundTaskKind = "scrape" | "discovery" | "match";

export type BackgroundTask = {
  id: string;
  kind: BackgroundTaskKind;
  label: string;
  startedAt: number;
};

const STORAGE_KEY = "pm_bg_tasks";
const CHANGE_EVENT = "pm-bg-tasks-changed";
// Tasks whose status endpoint keeps failing or that never finish get dropped
// after this long, so a stale entry can't poll forever.
const MAX_AGE_MS = 24 * 60 * 60 * 1000;

export function readBackgroundTasks(): BackgroundTask[] {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(parsed)) return [];
    const cutoff = Date.now() - MAX_AGE_MS;
    return parsed.filter(
      (t): t is BackgroundTask =>
        !!t &&
        typeof t === "object" &&
        typeof (t as BackgroundTask).id === "string" &&
        typeof (t as BackgroundTask).label === "string" &&
        ((t as BackgroundTask).startedAt ?? 0) > cutoff,
    );
  } catch {
    return [];
  }
}

function write(tasks: BackgroundTask[]): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(tasks));
  } catch {
    /* storage unavailable — widget just won't persist */
  }
  window.dispatchEvent(new CustomEvent(CHANGE_EVENT));
}

export function registerBackgroundTask(task: Omit<BackgroundTask, "startedAt">): void {
  const tasks = readBackgroundTasks().filter((t) => t.id !== task.id);
  tasks.push({ ...task, startedAt: Date.now() });
  write(tasks);
}

export function removeBackgroundTask(id: string): void {
  write(readBackgroundTasks().filter((t) => t.id !== id));
}

export function onBackgroundTasksChanged(handler: () => void): () => void {
  window.addEventListener(CHANGE_EVENT, handler);
  window.addEventListener("storage", handler);
  return () => {
    window.removeEventListener(CHANGE_EVENT, handler);
    window.removeEventListener("storage", handler);
  };
}
