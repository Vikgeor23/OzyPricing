"""Adaptive concurrency gate for batch scraping."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from app.config import get_settings

logger = logging.getLogger(__name__)


class AdaptiveConcurrencyController:
    """
    Limits in-flight scrapes with a dynamic cap.

    Safety: timeout spikes and rate-limit hits shrink the limit over rolling
    windows, as before. Growth is throughput-driven: the controller measures
    completions/min in phases and hill-climbs — an up-step of X% concurrency
    is kept only when it gains at least ``gain_ratio*X%`` throughput; at a
    plateau it periodically probes downward and stays lower while the loss is
    within ``loss_ratio*X%``. It thus settles at the knee of the site's real
    capacity curve; the configured max is only a hard ceiling.
    """

    def __init__(
        self,
        *,
        initial: int | None = None,
        min_limit: int | None = None,
        max_limit: int | None = None,
    ) -> None:
        settings = get_settings()
        self._min = min_limit if min_limit is not None else settings.scrape_concurrency_min
        self._max = max_limit if max_limit is not None else settings.scrape_concurrency_max
        start = initial if initial is not None else settings.scrape_concurrency
        self._limit = max(self._min, min(self._max, start))
        self._in_use = 0
        self._lock = asyncio.Lock()
        self._cond = asyncio.Condition(self._lock)
        self._outcomes: deque[bool] = deque(maxlen=200)
        self._rate_limited_outcomes: deque[bool] = deque(maxlen=200)
        # Throughput hill-climbing state. A "phase" is one measurement window
        # at a fixed limit; _probe_dir says whether the current phase tests a
        # step up (+1), a step down (-1), or re-measures the baseline (0).
        self._phase_start = time.monotonic()
        self._phase_count = 0
        self._baseline_ppm: float | None = None
        self._baseline_limit = self._limit
        self._probe_dir = 0
        self._next_probe_dir = 1
        self._hold_remaining = 0
        self._last_ppm: float | None = None

    @property
    def current_limit(self) -> int:
        return self._limit

    @property
    def last_throughput_ppm(self) -> float | None:
        """Most recently measured phase throughput (completions/min)."""
        return self._last_ppm

    def acquire(self) -> "_AdaptiveSlot":
        return _AdaptiveSlot(self)

    def record_outcome(self, *, timed_out: bool, rate_limited: bool = False) -> bool:
        """Record scrape outcome; return True if concurrency limit changed."""
        self._outcomes.append(timed_out)
        self._rate_limited_outcomes.append(rate_limited)
        self._phase_count += 1
        return self._maybe_adjust()

    def timeout_rate_pct(self) -> float:
        if not self._outcomes:
            return 0.0
        return round(100.0 * sum(1 for t in self._outcomes if t) / len(self._outcomes), 1)

    async def record_outcome_async(self, *, timed_out: bool, rate_limited: bool = False) -> None:
        changed = self.record_outcome(timed_out=timed_out, rate_limited=rate_limited)
        if changed:
            async with self._cond:
                self._cond.notify_all()

    def _maybe_adjust(self) -> bool:
        settings = get_settings()
        old = self._limit
        new_limit = old
        reason: str | None = None

        if self._rate_limited_outcomes and self._rate_limited_outcomes[-1]:
            new_limit = max(self._min, old - 4)
            reason = "rate_limited"

        if reason is None and len(self._outcomes) >= settings.scrape_adaptive_window_high:
            recent = list(self._outcomes)[-settings.scrape_adaptive_window_high :]
            timeout_rate = sum(1 for t in recent if t) / len(recent)
            if timeout_rate > settings.scrape_adaptive_timeout_rate_high:
                # Multiplicative cut, then judge a fresh window at the new
                # level — stale timeouts must not compound into more cuts.
                new_limit = max(self._min, old - max(2, old // 4))
                reason = f"timeout_rate_{timeout_rate:.0%}_over_last_{len(recent)}"
                self._outcomes.clear()
                self._rate_limited_outcomes.clear()

        if reason is not None:
            # Safety cut: whatever the probe was testing is invalid now.
            self._settle(next_dir=-1, hold=settings.scrape_adaptive_tpm_hold_windows)
        elif settings.scrape_adaptive_throughput_enabled:
            decision = self._throughput_adjust(settings)
            if decision is not None:
                new_limit, reason = decision
        elif len(self._outcomes) >= settings.scrape_adaptive_window_low:
            # Legacy behavior: grow while timeouts stay low.
            recent = list(self._outcomes)[-settings.scrape_adaptive_window_low :]
            recent_rate_limited = list(self._rate_limited_outcomes)[-settings.scrape_adaptive_window_low :]
            timeout_rate = sum(1 for t in recent if t) / len(recent)
            if timeout_rate < settings.scrape_adaptive_timeout_rate_low and not any(recent_rate_limited):
                new_limit = min(self._max, old + 1)
                reason = f"timeout_rate_{timeout_rate:.0%}_over_last_{len(recent)}"

        if new_limit != old and reason:
            self._limit = new_limit
            logger.info(
                "adaptive_concurrency_change old=%s new=%s reason=%s",
                old,
                new_limit,
                reason,
            )
            return True
        return False

    @staticmethod
    def _step(limit: int) -> int:
        return max(2, limit // 8)

    def _reset_phase(self) -> None:
        self._phase_start = time.monotonic()
        self._phase_count = 0

    def _settle(self, *, next_dir: int, hold: int) -> None:
        self._probe_dir = 0
        self._next_probe_dir = next_dir
        self._hold_remaining = hold
        self._baseline_ppm = None
        self._reset_phase()

    def _recent_timeouts_low(self, settings) -> bool:
        window = min(settings.scrape_adaptive_window_low, len(self._outcomes))
        if window == 0:
            return True
        recent = list(self._outcomes)[-window:]
        recent_rate_limited = list(self._rate_limited_outcomes)[-window:]
        timeout_rate = sum(1 for t in recent if t) / len(recent)
        return timeout_rate < settings.scrape_adaptive_timeout_rate_low and not any(recent_rate_limited)

    def _throughput_adjust(self, settings) -> tuple[int, str] | None:
        elapsed = time.monotonic() - self._phase_start
        if self._phase_count < settings.scrape_adaptive_tpm_window or elapsed < settings.scrape_adaptive_tpm_min_secs:
            return None
        ppm = self._phase_count / (elapsed / 60.0)
        self._last_ppm = round(ppm, 1)
        old = self._limit
        hold = settings.scrape_adaptive_tpm_hold_windows

        if self._probe_dir == 0:
            # Baseline phase finished; start the next probe unless holding.
            self._baseline_ppm = ppm
            self._baseline_limit = old
            self._reset_phase()
            if self._hold_remaining > 0:
                self._hold_remaining -= 1
                return None
            direction = self._next_probe_dir
            if direction > 0 and not self._recent_timeouts_low(settings):
                return None
            target = max(self._min, min(self._max, old + direction * self._step(old)))
            if target == old:
                self._next_probe_dir = -direction
                return None
            self._probe_dir = direction
            return target, f"tpm_probe_{'up' if direction > 0 else 'down'}_baseline_{ppm:.0f}ppm"

        # A probe phase finished — judge it against the baseline. Thresholds
        # scale with the step actually taken (old is the probed limit).
        baseline_ppm = self._baseline_ppm or 0.001
        gain_pct = 100.0 * (ppm - baseline_ppm) / baseline_ppm
        step_pct = 100.0 * abs(old - self._baseline_limit) / max(self._baseline_limit, 1)
        direction = self._probe_dir
        self._reset_phase()
        if direction > 0:
            if gain_pct >= settings.scrape_adaptive_tpm_gain_ratio * step_pct:
                self._baseline_ppm = ppm
                self._baseline_limit = old
                target = min(self._max, old + self._step(old))
                if target == old:
                    self._settle(next_dir=-1, hold=hold)
                    return None
                return target, f"tpm_gain_{gain_pct:.0f}%_climbing"
            # Extra browsers didn't pay for themselves — back to baseline.
            self._settle(next_dir=-1, hold=hold)
            return self._baseline_limit, f"tpm_gain_{gain_pct:.0f}%_reverting"
        if gain_pct >= -settings.scrape_adaptive_tpm_loss_ratio * step_pct:
            # Same throughput with fewer slots — keep descending.
            self._baseline_ppm = ppm
            self._baseline_limit = old
            target = max(self._min, old - self._step(old))
            if target == old:
                self._settle(next_dir=1, hold=hold)
                return None
            return target, f"tpm_loss_{max(-gain_pct, 0):.0f}%_descending"
        self._settle(next_dir=1, hold=hold)
        return self._baseline_limit, f"tpm_loss_{-gain_pct:.0f}%_reverting"

    async def _acquire_slot(self) -> None:
        async with self._cond:
            while self._in_use >= self._limit:
                await self._cond.wait()
            self._in_use += 1

    async def _release_slot(self) -> None:
        async with self._cond:
            self._in_use = max(0, self._in_use - 1)
            self._cond.notify_all()


class _AdaptiveSlot:
    def __init__(self, controller: AdaptiveConcurrencyController) -> None:
        self._controller = controller

    async def __aenter__(self) -> _AdaptiveSlot:
        await self._controller._acquire_slot()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._controller._release_slot()
