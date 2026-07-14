"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { DiscoveryTaskStatus, MatchTaskStatus, ScrapeTaskStatus } from "@/lib/types";
import {
  BackgroundTask,
  onBackgroundTasksChanged,
  readBackgroundTasks,
  registerBackgroundTask,
  removeBackgroundTask,
} from "@/lib/backgroundTasks";

const POLL_MS = 4000;
// Server-side sync: batch scrapes started from other devices/sessions are
// discovered via the active-runs registry, so progress follows the user.
const SERVER_SYNC_MS = 15_000;
// A finished task stays in the list this long so the user sees the ✓, then
// disappears on its own (or immediately via the ✕ button).
const DONE_LINGER_MS = 60_000;
const MAX_POLL_FAILURES = 5;

type TaskView = {
  task: BackgroundTask;
  phase: string;
  pct: number | null; // null → indeterminate
  detail: string;
  ready: boolean;
  failed: boolean;
};

function scrapeView(task: BackgroundTask, s: ScrapeTaskStatus): TaskView {
  const total = s.total ?? 0;
  const isBulkFetch = total === 0 && (s.current_phase ?? "").includes("bulk");
  if (isBulkFetch) {
    const pagesTotal = s.pages_total ?? 0;
    return {
      task,
      phase: "Downloading catalog",
      pct: pagesTotal > 0 ? Math.min(100, (100 * (s.pages_scanned ?? 0)) / pagesTotal) : null,
      detail:
        pagesTotal > 0
          ? `${(s.pages_scanned ?? 0).toLocaleString()} / ${pagesTotal.toLocaleString()} pages`
          : `${(s.product_urls_found ?? 0).toLocaleString()} products found`,
      ready: s.ready,
      failed: s.state === "FAILURE",
    };
  }
  const blocked = s.current_phase === "blocked";
  return {
    task,
    phase: s.ready
      ? s.current_phase === "cancelled"
        ? "Stopped"
        : blocked
          ? "Blocked (captcha)"
          : "Finished"
      : "Scraping",
    pct: total > 0 ? Math.min(100, (100 * (s.current ?? 0)) / total) : null,
    detail: `${(s.current ?? 0).toLocaleString()} / ${total.toLocaleString()} · ${(s.scraped ?? 0).toLocaleString()} scraped`,
    ready: s.ready,
    failed: s.state === "FAILURE" || blocked,
  };
}

function discoveryView(task: BackgroundTask, s: DiscoveryTaskStatus): TaskView {
  return {
    task,
    phase: s.ready ? "Finished" : "Finding URLs",
    pct: null,
    detail: `${(s.product_urls_found ?? 0).toLocaleString()} URLs found · ${(s.new_urls_found ?? s.created ?? 0).toLocaleString()} new`,
    ready: s.ready,
    failed: s.state === "FAILURE",
  };
}

function matchView(task: BackgroundTask, s: MatchTaskStatus): TaskView {
  const total = s.total ?? 0;
  return {
    task,
    phase: s.ready ? "Finished" : "Matching",
    pct: total > 0 ? Math.min(100, (100 * (s.current ?? 0)) / total) : null,
    detail: `${(s.current ?? 0).toLocaleString()} / ${total.toLocaleString()} · ${(s.matched ?? 0).toLocaleString()} matched`,
    ready: s.ready,
    failed: s.state === "FAILURE",
  };
}

export function BackgroundActivity() {
  const [tasks, setTasks] = useState<BackgroundTask[]>([]);
  const [views, setViews] = useState<Record<string, TaskView>>({});
  const [open, setOpen] = useState(false);
  const failureCounts = useRef<Record<string, number>>({});
  const doneSince = useRef<Record<string, number>>({});

  useEffect(() => {
    setTasks(readBackgroundTasks());
    return onBackgroundTasksChanged(() => setTasks(readBackgroundTasks()));
  }, []);

  useEffect(() => {
    let cancelled = false;
    const sync = async () => {
      try {
        const active = await api.get<{ task_id: string; competitor_name: string; kind?: string }[]>(
          "/competitors/active-scrape-tasks",
        );
        if (cancelled || !Array.isArray(active)) return;
        const known = new Set(readBackgroundTasks().map((t) => t.id));
        const labels: Record<string, string> = {
          scrape: "Scrape all",
          match: "Match all",
          discovery: "Find all URLs",
        };
        for (const run of active) {
          const kind = (run.kind ?? "scrape") as "scrape" | "match" | "discovery";
          if (!known.has(run.task_id)) {
            registerBackgroundTask({
              id: run.task_id,
              kind,
              label: `${labels[kind] ?? "Task"} — ${run.competitor_name}`,
            });
          }
        }
      } catch {
        /* offline or auth issue — local tasks still work */
      }
    };
    void sync();
    const timer = window.setInterval(() => void sync(), SERVER_SYNC_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  const poll = useCallback(async () => {
    const active = readBackgroundTasks();
    if (active.length === 0) return;
    const results = await Promise.allSettled(
      active.map(async (task): Promise<TaskView> => {
        if (task.kind === "scrape") {
          return scrapeView(task, await api.get<ScrapeTaskStatus>(`/competitors/scrape-tasks/${task.id}`));
        }
        if (task.kind === "match") {
          return matchView(task, await api.get<MatchTaskStatus>(`/competitors/match-tasks/${task.id}`));
        }
        return discoveryView(task, await api.get<DiscoveryTaskStatus>(`/competitors/discovery-tasks/${task.id}`));
      }),
    );
    const nextViews: Record<string, TaskView> = {};
    const now = Date.now();
    results.forEach((res, i) => {
      const task = active[i];
      if (res.status === "rejected") {
        const failures = (failureCounts.current[task.id] ?? 0) + 1;
        failureCounts.current[task.id] = failures;
        if (failures >= MAX_POLL_FAILURES) removeBackgroundTask(task.id);
        return;
      }
      failureCounts.current[task.id] = 0;
      const view = res.value;
      if (view.ready) {
        const since = doneSince.current[task.id] ?? now;
        doneSince.current[task.id] = since;
        if (now - since > DONE_LINGER_MS) {
          removeBackgroundTask(task.id);
          return;
        }
      }
      nextViews[task.id] = view;
    });
    setViews(nextViews);
  }, []);

  useEffect(() => {
    if (tasks.length === 0) return;
    void poll();
    const timer = window.setInterval(() => void poll(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [tasks.length, poll]);

  if (tasks.length === 0) return null;

  const runningCount = tasks.filter((t) => !(views[t.id]?.ready ?? false)).length;

  return (
    <div className="bg-activity">
      {open ? (
        <div className="bg-activity-panel">
          <div className="bg-activity-panel-title">
            {runningCount > 0 ? "In progress" : "Background tasks"}
          </div>
          {tasks.map((task) => {
            const view = views[task.id];
            return (
              <div key={task.id} className="bg-activity-item">
                <div className="bg-activity-item-head">
                  <span className="bg-activity-item-label" title={task.label}>
                    {task.label}
                  </span>
                  <span
                    className={
                      view?.failed
                        ? "bg-activity-status bg-activity-status-failed"
                        : view?.ready
                          ? "bg-activity-status bg-activity-status-done"
                          : "bg-activity-status"
                    }
                  >
                    {view ? (view.failed ? "Failed" : view.ready ? "Done ✓" : view.phase) : "Starting…"}
                  </span>
                  {view?.ready || view?.failed ? (
                    <button
                      type="button"
                      className="bg-activity-dismiss"
                      onClick={() => removeBackgroundTask(task.id)}
                      title="Dismiss"
                    >
                      ✕
                    </button>
                  ) : null}
                </div>
                {view ? <div className="bg-activity-detail">{view.detail}</div> : null}
                <div className="stat-bar bg-activity-bar">
                  <div
                    className={
                      view && view.pct === null && !view.ready
                        ? "stat-bar-fill stat-bar-indeterminate"
                        : "stat-bar-fill"
                    }
                    style={{
                      width: view
                        ? view.ready
                          ? "100%"
                          : view.pct === null
                            ? "40%"
                            : `${view.pct}%`
                        : "5%",
                    }}
                  />
                </div>
              </div>
            );
          })}
        </div>
      ) : null}
      <button type="button" className="bg-activity-pill" onClick={() => setOpen((v) => !v)}>
        <span className={runningCount > 0 ? "bg-activity-dot bg-activity-dot-live" : "bg-activity-dot"} />
        Background activity
        <span className="bg-activity-count">{tasks.length}</span>
      </button>
    </div>
  );
}
