"""CODEX: Restore only a vetted quota calibration from a preserved database snapshot."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from pathlib import Path


MAX_INTEGER = 2 ** 63 - 1
WEEK_SECONDS = 7 * 24 * 60 * 60
MAX_ADJACENT_RESET_GAP_SECONDS = 6 * 60 * 60
WEEK_DURATION_TOLERANCE_SECONDS = 5 * 60


def _number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _state(db):
    row = db.execute("SELECT payload FROM quota_state WHERE id=1").fetchone()
    if row is None:
        raise RuntimeError("Quota state is missing")
    value = json.loads(row[0])
    if not isinstance(value, dict):
        raise RuntimeError("Quota state is invalid")
    return value


def _window(state):
    values = tuple(state.get(key) for key in ("starts_at", "ends_at"))
    if not all(_number(value) for value in values) or values[0] >= values[1]:
        raise RuntimeError("Quota window is invalid")
    return values


def restore(database, calibration_database, now=None, replace_existing=False, repair_id=None,
            allow_adjacent_reset=False):
    """CODEX: Copy capacity metadata only across a proven overlapping or explicitly allowed adjacent window."""
    database, calibration_database = Path(database), Path(calibration_database)
    if database.resolve() == calibration_database.resolve():
        raise RuntimeError("Current and calibration databases must differ")
    observed_now = time.time() if now is None else now
    if not _number(observed_now):
        raise RuntimeError("Recovery time is invalid")
    if repair_id is not None and (not isinstance(repair_id, str) or not repair_id or len(repair_id) > 100):
        raise RuntimeError("Repair identifier is invalid")
    with sqlite3.connect(f"file:{calibration_database}?mode=ro", uri=True) as source:
        saved = _state(source)
    capacity = saved.get("capacity_credits_micros")
    capacity_at = saved.get("capacity_observed_at")
    if (isinstance(capacity, bool) or not isinstance(capacity, int)
            or not 0 < capacity <= MAX_INTEGER or not _number(capacity_at)):
        raise RuntimeError("The calibration snapshot has no valid capacity")
    saved_start, saved_end = _window(saved)
    if not saved_start <= capacity_at < saved_end:
        raise RuntimeError("The saved capacity does not belong to its quota window")

    with sqlite3.connect(database, timeout=15, isolation_level=None) as current_db:
        current_db.execute("BEGIN IMMEDIATE")
        try:
            current = _state(current_db)
            current_start, current_end = _window(current)
            existing = current.get("capacity_credits_micros")
            valid_existing = (isinstance(existing, int) and not isinstance(existing, bool)
                              and 0 < existing <= MAX_INTEGER)
            applied = current.get("applied_repairs", [])
            if not isinstance(applied, list) or not all(isinstance(item, str) for item in applied):
                raise RuntimeError("Applied repair history is invalid")
            if repair_id is not None and repair_id in applied:
                current_db.rollback()
                return {"changed": False, "capacity_credits_micros": existing}
            if current.get("limit_id") != saved.get("limit_id") or current.get("limit_id") != "codex":
                raise RuntimeError("Quota limit identifiers do not match regular Codex")
            overlaps = max(current_start, saved_start) < min(current_end, saved_end)
            adjacent_gap = current_start - saved_end
            durations_are_weekly = (
                abs((saved_end - saved_start) - WEEK_SECONDS) <= WEEK_DURATION_TOLERANCE_SECONDS
                and abs((current_end - current_start) - WEEK_SECONDS) <= WEEK_DURATION_TOLERANCE_SECONDS
            )
            adjacent_reset = bool(
                allow_adjacent_reset and not overlaps and 0 <= adjacent_gap <= MAX_ADJACENT_RESET_GAP_SECONDS
                and saved_start < current_start and saved_end < current_end and durations_are_weekly
            )
            if not overlaps and not adjacent_reset:
                raise RuntimeError("Quota windows do not overlap")
            if not current_start <= observed_now < current_end:
                raise RuntimeError("The current quota window has expired")
            current_observed = current.get("observed_at")
            if not _number(current_observed) or current_observed < capacity_at:
                raise RuntimeError("The current provider observation predates the calibration")
            if valid_existing and (not replace_existing or (existing == capacity and repair_id is None)):
                current_db.rollback()
                return {"changed": False, "capacity_credits_micros": existing}
            if replace_existing or adjacent_reset:
                current_percent = current.get("used_percent")
                if not _number(current_percent):
                    raise RuntimeError("The current provider percentage is invalid")
                event_id = current_db.execute(
                    "SELECT COALESCE(MAX(id),0) FROM quota_deltas WHERE created_at<=?",
                    (current_observed,),
                ).fetchone()[0]
                # CODEX: A recovered capacity starts at the latest provider checkpoint so an old interval is never reused.
                current.update(baseline_percent=current_percent, baseline_at=current_observed,
                               baseline_event_id=event_id, unknown_calibration_activity=False,
                               last_calibration_result=("restored_adjacent_window" if adjacent_reset
                                                        else "restored_trusted_checkpoint"))
                for key in ("pending_capacity_credits_micros", "pending_capacity_observed_at",
                            "ignored_capacity_credits_micros", "ignored_capacity_observed_at"):
                    current.pop(key, None)
            current.update(capacity_credits_micros=capacity, capacity_observed_at=capacity_at,
                           carried_forward=True, capacity_recovered_at=observed_now)
            for key in ("calibration_delta_percent", "calibration_delta_credits_micros"):
                if key in saved:
                    current[key] = saved[key]
            if repair_id is not None:
                current["applied_repairs"] = [*applied[-19:], repair_id]
            current_db.execute("UPDATE quota_state SET payload=? WHERE id=1", (json.dumps(current),))
            current_db.commit()
        except BaseException:
            current_db.rollback()
            raise
    return {"changed": True, "capacity_credits_micros": capacity}


def main():
    parser = argparse.ArgumentParser(description="Restore one previously verified Codex quota calibration")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--calibration-database", type=Path, required=True)
    parser.add_argument("--replace-existing", action="store_true",
                        help="replace an existing capacity with the explicitly selected trusted checkpoint")
    parser.add_argument("--repair-id", help="one-time identifier that makes an explicit repair idempotent")
    parser.add_argument("--allow-adjacent-reset", action="store_true",
                        help="carry calibration across a following weekly window shifted by at most six hours")
    args = parser.parse_args()
    result = restore(args.database, args.calibration_database, replace_existing=args.replace_existing,
                     repair_id=args.repair_id, allow_adjacent_reset=args.allow_adjacent_reset)
    action = "Restored" if result["changed"] else "Kept existing"
    print(f"{action} quota capacity {result['capacity_credits_micros']} without replacing chat data.", flush=True)


if __name__ == "__main__":
    main()
