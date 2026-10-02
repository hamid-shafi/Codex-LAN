"""CODEX: Estimated client shares calibrated against one authoritative provider quota window."""

from __future__ import annotations

import json
import math
import time
from decimal import Decimal, ROUND_HALF_UP

from . import usage_estimator


MAX_INTEGER = 2 ** 63 - 1
WEEK_SECONDS = 7 * 24 * 60 * 60
FRESH_SECONDS = 300
RESET_JITTER_SECONDS = 5
# CODEX: Provider weekly reset boundaries can move after a new window begins; carry only across a bounded adjacent rollover.
RESET_ROLLOVER_DRIFT_SECONDS = 6 * 60 * 60
CAPACITY_CONFIRMATION_RATIO = Decimal("1.25")
REGULAR_LIMIT_ID = "codex"
SPARK_MODEL = "gpt-5.3-codex-spark"
SEPARATE_LIMIT_MODELS = {SPARK_MODEL: "codex_bengalfox"}
ESTIMATE_WARNING = (
    "Per-device attribution is estimated from the provider's model-weighted usage units; the official account meter remains authoritative. "
    "Other account activity, rounded or delayed readings, and unmetered tools can affect the local split. "
    "Cache reclassification across quota resets is conservatively estimated; partial estimates can differ from actual usage. "
    "Limits are checked before new work and do not interrupt active turns."
)
QUOTA_EXHAUSTED_ERROR = "Device estimated weekly account share exhausted"
QUOTA_UNAVAILABLE_ERROR = "Device weekly account share cannot be safely verified right now"


def number(value, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and value >= minimum and (maximum is None or value <= maximum)
    except OverflowError:
        return False


def share_percent(value):
    if value is not None and not number(value, maximum=100):
        raise ValueError("Invalid weekly share percent")
    return value


def scaled_amount(amount, percent):
    result = int((Decimal(amount) * Decimal(str(percent)) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return min(MAX_INTEGER, max(0, result))


def provider_limit_id(model):
    """CODEX: Return the provider bucket that owns an exact model, defaulting unknown models to the strict regular bucket."""
    normalized = model.strip().lower() if isinstance(model, str) else None
    return SEPARATE_LIMIT_MODELS.get(normalized, REGULAR_LIMIT_ID)


class QuotaAccounting:
    """CODEX: Call only while the owning Store holds its lock and transaction."""

    def __init__(self, db):
        self.db = db

    def migrate(self):
        for table, additions in {
            "devices": {"weekly_share_percent": "REAL CHECK(weekly_share_percent >= 0 AND weekly_share_percent <= 100)"},
            "rooms": {"speed": "TEXT NOT NULL DEFAULT 'standard' CHECK(speed IN ('standard','fast'))"},
            "runs": {"model": "TEXT", "effort": "TEXT", "speed": "TEXT"},
        }.items():
            columns = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
            for field, declaration in additions.items():
                if field not in columns:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {field} {declaration}")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS quota_usage (run_id TEXT PRIMARY KEY REFERENCES runs(id), "
            "owner_id TEXT NOT NULL REFERENCES devices(id), credits_micros INTEGER, "
            "fingerprint TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS quota_deltas (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "run_id TEXT NOT NULL REFERENCES runs(id), owner_id TEXT NOT NULL REFERENCES devices(id), "
            "credits_micros INTEGER, created_at REAL NOT NULL)"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS quota_delta_window ON quota_deltas(created_at,owner_id)")
        # CODEX: Bound correlated unknown-usage checks to one run instead of scanning the whole quota window.
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS quota_unknown_delta_run_window "
            "ON quota_deltas(run_id,created_at) WHERE credits_micros IS NULL"
        )
        self.db.execute("CREATE TABLE IF NOT EXISTS quota_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS quota_unknown_acknowledgements ("
            "run_id TEXT PRIMARY KEY REFERENCES runs(id), "
            "owner_id TEXT NOT NULL REFERENCES devices(id), "
            "acknowledged_by TEXT NOT NULL REFERENCES devices(id), "
            "acknowledged_at REAL NOT NULL)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS quota_unknown_ack_owner "
            "ON quota_unknown_acknowledgements(owner_id,acknowledged_at)"
        )

    def record(self, run, usage):
        fingerprint = json.dumps([run["model"], run["effort"], run["speed"], usage["input_tokens"],
                                  usage["output_tokens"], usage["cached_tokens"], usage["total_tokens"]])
        previous = self.db.execute("SELECT * FROM quota_usage WHERE run_id=?", (run["id"],)).fetchone()
        if previous and previous["fingerprint"] == fingerprint:
            return
        now = time.time()
        if provider_limit_id(run["model"]) != REGULAR_LIMIT_ID:
            # CODEX: Keep the generic estimate explicitly unknown without poisoning the separate regular-codex allowance.
            self.db.execute(
                "INSERT INTO quota_usage(run_id,owner_id,credits_micros,fingerprint,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET credits_micros=NULL,fingerprint=excluded.fingerprint,updated_at=excluded.updated_at",
                (run["id"], run["owner_id"], None, fingerprint, now),
            )
            return
        estimate = usage_estimator.estimate_usage(
            model=run["model"], input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
            cached_tokens=usage["cached_tokens"], speed=run["speed"],
        )
        estimate = estimate.get("credits_micros") if isinstance(estimate, dict) else None
        if isinstance(estimate, bool) or not isinstance(estimate, int) or not 0 <= estimate <= MAX_INTEGER:
            estimate = None
        old = previous["credits_micros"] if previous else 0
        delta = None if estimate is None else estimate - (old or 0)
        ambiguous = False
        if previous and old is not None and estimate is not None:
            previous_counts = json.loads(previous["fingerprint"])
            state = self.state()
            boundary = state.get("starts_at")
            if state.get("ends_at") is not None and now >= state["ends_at"]:
                boundary = state["ends_at"]
            if (boundary is not None and now >= boundary and (usage["cached_tokens"] > previous_counts[5] or delta < 0)
                    and (previous["updated_at"] < boundary or self.db.execute(
                        "SELECT 1 FROM quota_deltas WHERE run_id=? AND created_at<? LIMIT 1",
                        (run["id"], boundary),
                    ).fetchone())):
                # CODEX: Old-window cache discounts cannot refund this window's new work.
                gross = usage_estimator.estimate_usage(
                    model=run["model"], input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                    cached_tokens=previous_counts[5], speed=run["speed"],
                ).get("credits_micros")
                delta = max(0, gross - old) if isinstance(gross, int) and 0 <= gross <= MAX_INTEGER else None
                ambiguous = True
        if delta or delta is None:
            self.db.execute("INSERT INTO quota_deltas(run_id,owner_id,credits_micros,created_at) VALUES(?,?,?,?)",
                            (run["id"], run["owner_id"], delta, now))
        if ambiguous and delta is not None:
            self.db.execute("INSERT INTO quota_deltas(run_id,owner_id,credits_micros,created_at) VALUES(?,?,NULL,?)",
                            (run["id"], run["owner_id"], now))
        # CODEX: Keep the corrected cumulative baseline even when its window attribution is uncertain.
        self.db.execute(
            "INSERT INTO quota_usage(run_id,owner_id,credits_micros,fingerprint,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(run_id) DO UPDATE SET credits_micros=excluded.credits_micros, "
            "fingerprint=excluded.fingerprint,updated_at=excluded.updated_at",
            (run["id"], run["owner_id"], estimate, fingerprint, now),
        )

    def state(self):
        row = self.db.execute("SELECT payload FROM quota_state WHERE id=1").fetchone()
        return json.loads(row[0]) if row else {}

    def save(self, state):
        self.db.execute("INSERT INTO quota_state(id,payload) VALUES(1,?) "
                        "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (json.dumps(state),))

    def event_id(self, observed_at):
        return self.db.execute("SELECT COALESCE(MAX(id),0) FROM quota_deltas WHERE created_at<=?", (observed_at,)).fetchone()[0]

    def checkpoint_after_device_deletion(self):
        """CODEX: Start the next calibration interval after the last provider snapshot."""
        state = self.state()
        observed_at = state.get("observed_at")
        used_percent = state.get("used_percent")
        if not number(observed_at) or not number(used_percent, maximum=100):
            return
        state.update(
            baseline_percent=used_percent,
            baseline_event_id=self.event_id(observed_at),
            baseline_at=observed_at,
            unknown_calibration_activity=False,
        )
        self.save(state)

    def observe(self, envelope):
        now, state = time.time(), self.state()
        windows = envelope.get("windows") if isinstance(envelope, dict) else None
        fetched = envelope.get("fetched_at") if isinstance(envelope, dict) else None
        if (not isinstance(windows, list) or envelope.get("available") is not True
                or envelope.get("stale") is True or not number(fetched)
                or fetched > now + 30 or now - fetched > FRESH_SECONDS):
            state["unavailable"] = True
            self.save(state)
            return
        weekly = [item for item in windows if isinstance(item, dict) and item.get("window_minutes") == 10080
                  and number(item.get("used_percent"), maximum=100) and number(item.get("resets_at"))
                  and now < item["resets_at"] <= now + WEEK_SECONDS + 30
                  and isinstance(item.get("limit_id"), str) and 0 < len(item["limit_id"]) <= 80]
        if not weekly:
            state["unavailable"] = True
            self.save(state)
            return
        window = next((item for item in weekly if item["limit_id"] == REGULAR_LIMIT_ID), None)
        if window is None:
            state["unavailable"] = True
            self.save(state)
            return
        end, percent = window["resets_at"], window["used_percent"]
        start = end - WEEK_SECONDS
        same_limit = state.get("limit_id") == window["limit_id"] and number(state.get("ends_at"))
        same = bool(same_limit and abs(state["ends_at"] - end) <= RESET_JITTER_SECONDS)
        normal_reset = bool(same_limit and abs(state["ends_at"] - start) <= RESET_JITTER_SECONDS)
        shifted_reset = bool(
            same_limit and not same and not normal_reset and number(state.get("starts_at"))
            and state["starts_at"] < start < end and state["ends_at"] < end
            and abs(start - state["ends_at"]) <= RESET_ROLLOVER_DRIFT_SECONDS
            and abs((end - state["ends_at"]) - WEEK_SECONDS) <= RESET_ROLLOVER_DRIFT_SECONDS
            and fetched >= max(start, state["ends_at"])
        )
        schedule_correction = bool(
            same_limit and not same and not normal_reset and not shifted_reset and number(state.get("starts_at"))
            and state["starts_at"] <= fetched < state["ends_at"] and start <= fetched < end
        )
        regressive_schedule = bool(
            schedule_correction and end < state["ends_at"] - RESET_JITTER_SECONDS
        )
        if same:
            # CODEX: Provider reset timestamps can jitter by a second without starting a different week.
            start, end = state["starts_at"], state["ends_at"]
        elif normal_reset:
            start, end = state["ends_at"], state["ends_at"] + WEEK_SECONDS
        elif schedule_correction:
            # CODEX: Retain already-counted work while bounding enforcement to the corrected provider reset.
            start = min(state["starts_at"], start)
        if fetched < start or (state.get("observed_at") is not None and fetched < state["observed_at"]):
            return
        if regressive_schedule:
            # CODEX: A fresh RPC can replay an older overlapping window; keep the later reset and restart calibration timing.
            state.update(unavailable=True, carried_forward=bool(state.get("capacity_credits_micros")),
                         baseline_percent=state.get("used_percent"), baseline_event_id=self.event_id(fetched),
                         baseline_at=fetched, unknown_calibration_activity=False,
                         rejected_regressive_reset_at=fetched, rejected_regressive_ends_at=end)
            state.pop("pending_capacity_credits_micros", None)
            state.pop("pending_capacity_observed_at", None)
            self.save(state)
            return
        if fetched == state.get("observed_at"):
            # CODEX: Re-reading a cached provider snapshot must not absorb later client activity.
            if same and percent == state.get("used_percent"):
                state["unavailable"] = False
                self.save(state)
            return
        if not same and not schedule_correction:
            capacity = state.get("capacity_credits_micros") if normal_reset or shifted_reset else None
            state = {"limit_id": window["limit_id"], "starts_at": start, "ends_at": end,
                     "baseline_percent": percent, "baseline_event_id": self.event_id(fetched), "baseline_at": fetched,
                     "capacity_credits_micros": capacity, "capacity_observed_at": state.get("capacity_observed_at") if capacity else None,
                     "carried_forward": bool(capacity),
                     "last_calibration_result": "carried_shifted_reset" if shifted_reset and capacity else None}
        else:
            if schedule_correction:
                state.update(starts_at=start, ends_at=end, schedule_corrected_at=fetched,
                             carried_forward=bool(state.get("capacity_credits_micros")))
        if schedule_correction:
            # CODEX: Never calibrate across alternate provider schedules; each correction starts a clean comparison interval.
            state.update(baseline_percent=percent, baseline_event_id=self.event_id(fetched), baseline_at=fetched,
                         unknown_calibration_activity=False)
            state.pop("pending_capacity_credits_micros", None)
            state.pop("pending_capacity_observed_at", None)
        elif same:
            if percent < state.get("used_percent", percent):
                # CODEX: Provider corrections or manual resets are not client consumption.
                state.update(baseline_percent=percent, baseline_event_id=self.event_id(fetched), baseline_at=fetched)
                state.pop("pending_capacity_credits_micros", None)
                state.pop("pending_capacity_observed_at", None)
            else:
                delta_percent = Decimal(str(percent)) - Decimal(str(state["baseline_percent"]))
                deltas = self.db.execute(
                    "SELECT d.credits_micros FROM quota_deltas d JOIN runs r ON r.id=d.run_id "
                    "WHERE d.id>? AND d.created_at>=? AND d.created_at<? AND d.created_at<=? "
                    "AND (r.model IS NULL OR LOWER(TRIM(r.model))!=?)",
                    (state["baseline_event_id"], start, end, fetched, SPARK_MODEL),
                ).fetchall()
                known_delta = sum(row[0] or 0 for row in deltas)
                incomplete = any(row[0] is None for row in deltas)
                state["unknown_calibration_activity"] = incomplete
                if incomplete:
                    # CODEX: Start a clean interval instead of treating unknown-price work as free.
                    state.update(baseline_percent=percent, baseline_event_id=self.event_id(fetched), baseline_at=fetched)
                if delta_percent >= 2 and not incomplete:
                    # CODEX: Close every settled interval so delayed or external account usage cannot reuse old local work.
                    state.update(baseline_percent=percent, baseline_event_id=self.event_id(fetched), baseline_at=fetched)
                    existing = state.get("capacity_credits_micros")
                    valid_existing = isinstance(existing, int) and not isinstance(existing, bool) and 0 < existing <= MAX_INTEGER
                    capacity = None
                    if known_delta > 0:
                        capacity = int((Decimal(known_delta) * 100 / delta_percent).quantize(Decimal(1), rounding=ROUND_HALF_UP))
                    if capacity is None or not 0 < capacity <= MAX_INTEGER:
                        state["untracked_provider_delta_percent"] = float(
                            Decimal(str(state.get("untracked_provider_delta_percent", 0))) + delta_percent
                        )
                        state["last_calibration_result"] = "provider_only"
                    elif not valid_existing:
                        state.update(capacity_credits_micros=capacity, capacity_observed_at=fetched,
                                     carried_forward=False, last_calibration_result="accepted_initial",
                                     calibration_delta_percent=float(delta_percent),
                                     calibration_delta_credits_micros=known_delta)
                    elif capacity <= existing:
                        # CODEX: A lower candidate can be caused by untracked shared-account work; never charge it to clients.
                        expected = Decimal(known_delta) * 100 / Decimal(existing)
                        unexplained = max(Decimal(0), delta_percent - expected)
                        state.update(last_calibration_result="ignored_lower",
                                     ignored_capacity_credits_micros=capacity,
                                     ignored_capacity_observed_at=fetched,
                                     untracked_provider_delta_percent=float(
                                         Decimal(str(state.get("untracked_provider_delta_percent", 0))) + unexplained
                                     ))
                    else:
                        pending = state.get("pending_capacity_credits_micros")
                        compatible = (isinstance(pending, int) and not isinstance(pending, bool) and pending > 0
                                      and Decimal(max(pending, capacity)) / Decimal(min(pending, capacity))
                                      <= CAPACITY_CONFIRMATION_RATIO)
                        if compatible:
                            # CODEX: Promotion must preserve the provisional high-water mark or usage can jump upward
                            # CODEX: merely because a compatible confirmation interval estimated a smaller capacity.
                            confirmed = max(pending, capacity)
                            state.update(capacity_credits_micros=confirmed, capacity_observed_at=fetched,
                                         carried_forward=False, last_calibration_result="accepted_confirmed",
                                         calibration_delta_percent=float(delta_percent),
                                         calibration_delta_credits_micros=known_delta)
                            state.pop("pending_capacity_credits_micros", None)
                            state.pop("pending_capacity_observed_at", None)
                        else:
                            state.update(pending_capacity_credits_micros=capacity,
                                         pending_capacity_observed_at=fetched,
                                         last_calibration_result="pending_higher")
        state.update(used_percent=percent, observed_at=fetched, observed_event_id=self.event_id(fetched), unavailable=False)
        self.save(state)

    def summary(self):
        state, now = self.state(), time.time()
        window = ({key: state[key] for key in ("limit_id", "starts_at", "ends_at")}
                  if state.get("limit_id") == REGULAR_LIMIT_ID else None)
        expired = window is not None and not window["starts_at"] <= now < window["ends_at"]
        capacity = state.get("capacity_credits_micros")
        valid_capacity = isinstance(capacity, int) and not isinstance(capacity, bool) and 0 < capacity <= MAX_INTEGER
        pending_capacity = state.get("pending_capacity_credits_micros")
        valid_pending_capacity = (isinstance(pending_capacity, int) and not isinstance(pending_capacity, bool)
                                  and 0 < pending_capacity <= MAX_INTEGER)
        # CODEX: A higher same-window candidate is a safer denominator than a known-low carried value; use it immediately
        # CODEX: for attribution while retaining the two-interval rule before promoting it to durable calibration state.
        effective_capacity = max(capacity if valid_capacity else 0,
                                 pending_capacity if valid_pending_capacity else 0) or None
        provisional_higher_capacity = bool(valid_capacity and valid_pending_capacity and pending_capacity > capacity)
        refresh_stale = bool(window and (state.get("unavailable") or now - state.get("observed_at", 0) > FRESH_SECONDS))
        refresh_status = "unknown" if not window else "expired" if expired else "stale" if refresh_stale else "fresh"
        # CODEX: Provider freshness gates calibration and reset discovery, not an in-window local ledger with a durable capacity.
        status = ("unknown" if not window else "stale" if expired or (refresh_stale and not valid_capacity)
                  else "estimated" if valid_capacity else "learning")
        learning_reason = None
        if status == "learning":
            if state.get("unknown_calibration_activity"):
                learning_reason = "unpriced_usage"
            elif state.get("last_calibration_result") == "provider_only":
                learning_reason = "awaiting_usage_reports"
            elif state.get("used_percent", 0) - state.get("baseline_percent", 0) < 2:
                learning_reason = "awaiting_weekly_change"
            else:
                learning_reason = "awaiting_usage_reports"
        calibration = {"status": status, "capacity_credits_micros": capacity,
                       "effective_capacity_credits_micros": effective_capacity,
                       "effective_capacity_source": "current_window_higher_candidate" if provisional_higher_capacity else "calibrated",
                       "provisional_higher_capacity": provisional_higher_capacity,
                       "learning_reason": learning_reason,
                       "observed_at": state.get("observed_at"), "observed_event_id": state.get("observed_event_id"),
                       "capacity_observed_at": state.get("capacity_observed_at"),
                       "carried_forward": state.get("carried_forward", False), "warning": ESTIMATE_WARNING,
                       "unknown_activity": state.get("unknown_calibration_activity", False),
                       "minimum_delta_percent": 2, "provider_used_percent": state.get("used_percent"),
                       "method": "checkpointed_robust", "last_result": state.get("last_calibration_result"),
                       "untracked_provider_delta_percent": state.get("untracked_provider_delta_percent", 0),
                       "refresh_status": refresh_status,
                       "using_last_known": status == "estimated" and refresh_stale}
        return window, calibration

    def _unknown_usage(self, owner_id, start, end):
        rows = self.db.execute(
            "SELECT r.id,r.status,r.model,r.speed,r.finished_at,"
            "q.run_id AS quota_run_id,q.credits_micros AS quota_credits_micros,"
            "u.run_id AS usage_run_id,u.updated_at AS usage_updated_at,"
            "a.run_id AS acknowledgement_run_id,"
            "EXISTS(SELECT 1 FROM quota_deltas x WHERE x.run_id=r.id AND x.credits_micros IS NULL "
            "AND x.created_at>=:start AND x.created_at<:end) AS has_unknown_delta "
            "FROM runs r LEFT JOIN quota_usage q ON q.run_id=r.id "
            "LEFT JOIN usage_ledger u ON u.run_id=r.id "
            "LEFT JOIN quota_unknown_acknowledgements a ON a.run_id=r.id AND a.owner_id=r.owner_id "
            "WHERE r.owner_id=:owner AND (r.model IS NULL OR LOWER(TRIM(r.model))!=:separate_model) AND "
            "((r.created_at>=:start AND r.created_at<:end) OR "
            "(r.finished_at>=:start AND r.finished_at<:end) OR "
            "(u.updated_at>=:start AND u.updated_at<:end) OR "
            "(r.status IN ('queued','running') AND r.created_at<:end)) AND "
            "((q.run_id IS NULL OR q.credits_micros IS NULL) OR "
            "(((r.status IN ('queued','running')) OR (r.finished_at>=:start AND r.finished_at<:end)) "
            "AND (u.updated_at IS NULL OR u.updated_at<:start)) OR "
            "EXISTS(SELECT 1 FROM quota_deltas x WHERE x.run_id=r.id AND x.credits_micros IS NULL "
            "AND x.created_at>=:start AND x.created_at<:end))",
            {"owner": owner_id, "start": start, "end": end, "separate_model": SPARK_MODEL},
        ).fetchall()
        result = {"unknown": len(rows), "legacy": 0, "unreported": 0,
                  "acknowledged": 0, "acknowledgeable": 0, "blocking": 0, "candidates": []}
        for row in rows:
            legacy = row["model"] is None or row["speed"] is None
            live = row["status"] in {"queued", "running"}
            finished_in_window = number(row["finished_at"]) and start <= row["finished_at"] < end
            usage_missing = row["usage_run_id"] is None
            usage_old = row["usage_updated_at"] is None or row["usage_updated_at"] < start
            unreported = not legacy and (usage_missing or ((live or finished_in_window) and usage_old))
            priced = (not legacy and isinstance(usage_estimator.estimate_usage(
                row["model"], 0, 0, 0, row["speed"]
            ).get("credits_micros"), int))
            acknowledgeable = bool(
                unreported and row["status"] in {"completed", "failed", "cancelled", "interrupted"}
                and finished_in_window and priced and not row["has_unknown_delta"]
            )
            acknowledged = acknowledgeable and row["acknowledgement_run_id"] is not None
            result["legacy"] += int(legacy)
            result["unreported"] += int(unreported)
            result["acknowledged"] += int(acknowledged)
            result["acknowledgeable"] += int(acknowledgeable and not acknowledged)
            result["candidates"].append((row["id"], acknowledgeable, acknowledged))
        result["blocking"] = result["unknown"] - result["acknowledged"]
        return result

    def acknowledge_unknown_usage(self, owner_id, acknowledged_by, window):
        if not window:
            return 0
        result = self._unknown_usage(owner_id, window["starts_at"], window["ends_at"])
        run_ids = [run_id for run_id, acknowledgeable, acknowledged in result["candidates"]
                   if acknowledgeable and not acknowledged]
        now = time.time()
        self.db.executemany(
            "INSERT OR IGNORE INTO quota_unknown_acknowledgements"
            "(run_id,owner_id,acknowledged_by,acknowledged_at) VALUES(?,?,?,?)",
            [(run_id, owner_id, acknowledged_by, now) for run_id in run_ids],
        )
        return len(run_ids)

    def device_report(self, device, window, calibration):
        spent, snapshot_spent, all_snapshot_spent = 0, 0, 0
        unknown = {"unknown": 0, "legacy": 0, "unreported": 0, "acknowledged": 0,
                   "acknowledgeable": 0, "blocking": 0, "candidates": []}
        if window:
            start, end = window["starts_at"], window["ends_at"]
            deltas = self.db.execute(
                "SELECT d.id,d.run_id,d.credits_micros,d.created_at FROM quota_deltas d JOIN runs r ON r.id=d.run_id "
                "WHERE d.owner_id=? AND d.created_at>=? AND d.created_at<? "
                "AND (r.model IS NULL OR LOWER(TRIM(r.model))!=?)",
                (device["id"], start, end, SPARK_MODEL),
            ).fetchall()
            by_run, by_run_at_snapshot = {}, {}
            observed_at = calibration.get("observed_at")
            observed_event_id = calibration.get("observed_event_id")
            for row in deltas:
                by_run[row["run_id"]] = by_run.get(row["run_id"], 0) + (row["credits_micros"] or 0)
                at_snapshot = (isinstance(observed_event_id, int) and not isinstance(observed_event_id, bool)
                               and row["id"] <= observed_event_id)
                if observed_event_id is None and number(observed_at):
                    at_snapshot = row["created_at"] <= observed_at
                if at_snapshot:
                    by_run_at_snapshot[row["run_id"]] = (by_run_at_snapshot.get(row["run_id"], 0)
                                                         + (row["credits_micros"] or 0))
            spent = sum(max(0, amount) for amount in by_run.values())
            snapshot_spent = sum(min(max(0, amount), max(0, by_run_at_snapshot.get(run_id, 0)))
                                 for run_id, amount in by_run.items())
            if (isinstance(observed_event_id, int) and not isinstance(observed_event_id, bool)) or number(observed_at):
                cutoff_clause = "d.id<=?" if observed_event_id is not None else "d.created_at<=?"
                cutoff = observed_event_id if observed_event_id is not None else observed_at
                all_snapshot_rows = self.db.execute(
                    "SELECT d.run_id,d.credits_micros FROM quota_deltas d JOIN runs r ON r.id=d.run_id "
                    f"WHERE d.created_at>=? AND d.created_at<? AND {cutoff_clause} "
                    "AND (r.model IS NULL OR LOWER(TRIM(r.model))!=?)",
                    (start, end, cutoff, SPARK_MODEL),
                ).fetchall()
                all_by_run = {}
                for row in all_snapshot_rows:
                    all_by_run[row["run_id"]] = all_by_run.get(row["run_id"], 0) + (row["credits_micros"] or 0)
                all_snapshot_spent = sum(max(0, amount) for amount in all_by_run.values())
            unknown = self._unknown_usage(device["id"], start, end)
        share = device["weekly_share_percent"]
        capacity = calibration.get("effective_capacity_credits_micros") or calibration["capacity_credits_micros"]
        valid_capacity = isinstance(capacity, int) and not isinstance(capacity, bool) and 0 < capacity <= MAX_INTEGER
        allocation = scaled_amount(capacity, share) if share is not None and share != 0 and valid_capacity else None
        allocation = 0 if share == 0 else allocation
        pending_spent = max(0, spent - snapshot_spent)
        pending_account_percent = (None if not valid_capacity else
                                   float(Decimal(pending_spent) * 100 / Decimal(capacity)))
        provider_percent = calibration.get("provider_used_percent")
        valid_provider_percent = number(provider_percent, maximum=100)
        if valid_capacity and all_snapshot_spent > 0 and valid_provider_percent:
            # CODEX: Attribute no more settled local work than the authoritative account meter reports; the remainder
            # CODEX: stays external/unattributed instead of inflating every device through a contaminated denominator.
            local_snapshot_percent = Decimal(all_snapshot_spent) * 100 / Decimal(capacity)
            attributable_snapshot_percent = min(Decimal(str(provider_percent)), local_snapshot_percent)
            snapshot_account_percent = float(
                attributable_snapshot_percent * Decimal(snapshot_spent) / Decimal(all_snapshot_spent)
            )
        elif valid_capacity:
            snapshot_account_percent = 0.0
        else:
            snapshot_account_percent = None
        account_percent = (None if snapshot_account_percent is None or pending_account_percent is None else
                           snapshot_account_percent + pending_account_percent)
        attributed_credits = (None if not valid_capacity or account_percent is None else
                              scaled_amount(capacity, account_percent))
        share_used_percent = (None if share is None or account_percent is None else
                              100.0 if share == 0 else float(Decimal(str(account_percent)) * 100 / Decimal(str(share))))
        remaining = (None if share is None or account_percent is None or not valid_capacity else
                     scaled_amount(capacity, max(0, Decimal(str(share)) - Decimal(str(account_percent)))))
        status = "unknown" if unknown["unknown"] and calibration["status"] == "estimated" else calibration["status"]
        # CODEX: The host's own share is an analysis target; incomplete legacy history must never lock out administration.
        analysis_only = bool(device["is_admin"]) and share is not None
        enforcement_enabled = share is not None and not bool(device["is_admin"])
        strict_blocked, block_reason = False, "not_configured"
        if analysis_only:
            block_reason = "analysis_only"
        elif enforcement_enabled and share == 0:
            strict_blocked, block_reason = True, "disabled"
        elif enforcement_enabled:
            reliable_measurement = bool(
                window is not None
                and calibration["status"] == "estimated"
                and calibration.get("refresh_status") == "fresh"
                and valid_capacity
                and allocation is not None
                and attributed_credits is not None
            )
            if reliable_measurement and attributed_credits >= allocation:
                strict_blocked, block_reason = True, "exhausted"
            else:
                block_reason = "available"
        return {"device_id": device["id"], "allocated_percent": share, "spent_credits_micros": spent,
                "snapshot_spent_credits_micros": snapshot_spent,
                "pending_spent_credits_micros": pending_spent,
                "allocated_credits_micros": allocation,
                "account_used_percent": account_percent,
                "snapshot_account_used_percent": snapshot_account_percent,
                "pending_account_used_percent": pending_account_percent,
                "used_percent": share_used_percent,
                "remaining_credits_micros": remaining,
                "unknown_runs": unknown["unknown"], "blocking_unknown_runs": unknown["blocking"],
                "legacy_unknown_runs": unknown["legacy"], "unreported_runs": unknown["unreported"],
                "acknowledged_unreported_runs": unknown["acknowledged"],
                "acknowledgeable_unreported_runs": unknown["acknowledgeable"],
                "other_unknown_runs": unknown["unknown"] - unknown["legacy"] - unknown["unreported"],
                "limit_active": enforcement_enabled, "target_active": share is not None,
                "analysis_only": analysis_only, "enforcement_enabled": enforcement_enabled,
                "strict_blocked": strict_blocked,
                "block_reason": block_reason, "status": status}

    def check(self, device):
        if device["weekly_share_percent"] is None or device["is_admin"]:
            return
        window, calibration = self.summary()
        report = self.device_report(device, window, calibration)
        if not report["strict_blocked"]:
            return
        if report["block_reason"] in {"disabled", "exhausted"}:
            raise ValueError(QUOTA_EXHAUSTED_ERROR)
        raise ValueError(QUOTA_UNAVAILABLE_ERROR)

    @staticmethod
    def allocated_pool(devices, reports, calibration):
        """CODEX: Summarize only active, positive, enforced shares; unlimited and inactive devices are outside the pool."""
        eligible, zero_share_devices = [], 0
        for device, report in zip(devices, reports):
            if device["status"] != "approved" or device["is_admin"] or not report["enforcement_enabled"]:
                continue
            share = report["allocated_percent"]
            if not number(share, maximum=100):
                continue
            if share == 0:
                zero_share_devices += 1
                continue
            eligible.append(report)
        allocated = sum((Decimal(str(row["allocated_percent"])) for row in eligible), Decimal(0))
        values_available = calibration.get("status") == "estimated" and all(
            number(row["account_used_percent"]) for row in eligible
        )
        account_used = (sum((Decimal(str(row["account_used_percent"])) for row in eligible), Decimal(0))
                        if eligible and values_available else None)
        filled = float(account_used * 100 / allocated) if account_used is not None and allocated > 0 else None
        unknown_runs = sum(row["unknown_runs"] for row in eligible)
        status = "available" if filled is not None else "unavailable" if eligible else (
            "zero_allocation" if zero_share_devices else "not_configured"
        )
        return {
            "status": status,
            "limited_devices": len(eligible),
            "zero_share_devices": zero_share_devices,
            "allocated_percent": float(allocated),
            "account_used_percent": float(account_used) if account_used is not None else None,
            "used_percent": filled,
            "unknown_runs": unknown_runs,
            "lower_bound": unknown_runs > 0,
        }

    def report(self):
        window, calibration = self.summary()
        devices = list(self.db.execute("SELECT * FROM devices ORDER BY created_at,id"))
        reports = [self.device_report(device, window, calibration) for device in devices]
        return {"window": window, "calibration": calibration, "devices": reports,
                "allocated_pool": self.allocated_pool(devices, reports, calibration), "estimated": True,
                "unit": "credit_micros", "warning": ESTIMATE_WARNING}
