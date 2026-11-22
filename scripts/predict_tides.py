from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from scipy.signal import find_peaks
from utide import reconstruct, solve


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "Raglan_Wharf.txt"
MET_SERVICE_REFERENCE = ROOT / "data" / "metservice_reference.json"
LOCAL_TZ = ZoneInfo("Pacific/Auckland")
LATITUDE = -37.8
GROUNDED_AT = datetime(2026, 11, 22, 11, 12, tzinfo=LOCAL_TZ)
SENTINEL_VALUE = 2.01166e-7
ROW_PATTERN = re.compile(
    r"^(?P<date>\d{2}/\d{2}/\d{4}) @ "
    r"(?P<time>\d{2}:\d{2}:\d{2})(?:\s+(?P<level>[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?))?\s*$"
)


def load_observations(path: Path) -> tuple[list[datetime], list[float], dict[str, int]]:
    times: list[datetime] = []
    levels: list[float] = []
    stats = {"rows": 0, "missing": 0, "sentinel": 0}

    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.reader(source, delimiter="\t")
        next(reader, None)
        for line_number, row in enumerate(reader, start=2):
            stats["rows"] += 1
            text = "\t".join(row)
            match = ROW_PATTERN.match(text)
            if not match:
                raise ValueError(f"Unrecognized data format on line {line_number}: {text!r}")

            if match.group("level") is None:
                stats["missing"] += 1
                continue

            level = float(match.group("level"))
            if not math.isfinite(level):
                stats["missing"] += 1
                continue
            if math.isclose(level, SENTINEL_VALUE, abs_tol=1e-12):
                stats["sentinel"] += 1
                continue

            timestamp = datetime.strptime(
                f'{match.group("date")} {match.group("time")}', "%d/%m/%Y %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
            times.append(timestamp)
            levels.append(level)

    if not times:
        raise ValueError(f"No valid water-level observations in {path}")
    if any(right <= left for left, right in zip(times, times[1:])):
        raise ValueError("Valid observation timestamps must be strictly increasing")

    return times, levels, stats


def to_utide_time(times: list[datetime]) -> np.ndarray:
    return np.array([t.replace(tzinfo=None) for t in times], dtype="datetime64[s]")


def fit_model(times: list[datetime], levels: list[float]):
    return solve(
        to_utide_time(times),
        np.asarray(levels, dtype=float),
        lat=LATITUDE,
        method="ols",
        conf_int="linear",
        constit="auto",
        trend=False,
        phase="Greenwich",
        nodal="linear",
        verbose=False,
    )


def predict(coef, times: list[datetime]) -> np.ndarray:
    return np.asarray(
        reconstruct(to_utide_time(times), coef, verbose=False).h, dtype=float
    )


def evaluate_time_basis(coef) -> dict[str, dict[str, float | bool]]:
    event_utc = GROUNDED_AT.astimezone(timezone.utc)
    candidates = {
        "UTC": event_utc,
        "NZST": event_utc + timedelta(hours=12),
    }
    results = {}
    for basis, model_time in candidates.items():
        levels = predict(coef, [model_time, model_time + timedelta(minutes=1)])
        results[basis] = {
            "level_m": float(levels[0]),
            "falling_tide": bool(levels[1] < levels[0]),
        }
    return results


def score_metservice_times(coef, events: list[dict]) -> dict[str, dict[str, float | int]]:
    scores: dict[str, dict[str, float | int]] = {}
    source_offsets = {"UTC": timedelta(), "NZST": timedelta(hours=12)}
    for basis, offset in source_offsets.items():
        errors = []
        for event in events:
            expected = datetime.fromisoformat(event["time_local"]).replace(tzinfo=LOCAL_TZ)
            model_center = expected.astimezone(timezone.utc) + offset
            sample_times = [
                model_center + timedelta(minutes=minute)
                for minute in range(-120, 121)
            ]
            sample_levels = predict(coef, sample_times)
            extremum_index = int(
                np.argmax(sample_levels) if event["type"] == "high" else np.argmin(sample_levels)
            )
            errors.append(
                abs((sample_times[extremum_index] - model_center).total_seconds()) / 60
            )
        scores[basis] = {
            "events": len(errors),
            "mean_absolute_error_minutes": float(np.mean(errors)),
            "median_absolute_error_minutes": float(np.median(errors)),
        }
    return scores


def select_time_basis(
    time_scores: dict[str, dict[str, float | int]],
    tide_direction: dict[str, dict[str, float | bool]],
) -> tuple[str, str]:
    utc_error = time_scores["UTC"]["mean_absolute_error_minutes"]
    nzst_error = time_scores["NZST"]["mean_absolute_error_minutes"]
    if abs(utc_error - nzst_error) >= 5:
        selected = "UTC" if utc_error < nzst_error else "NZST"
        reason = (
            f"MetService event times match {selected} best "
            f"(mean absolute error {min(utc_error, nzst_error):.1f} minutes "
            f"versus {max(utc_error, nzst_error):.1f} minutes)."
        )
        return selected, reason

    utc_falling = tide_direction["UTC"]["falling_tide"]
    nzst_falling = tide_direction["NZST"]["falling_tide"]
    if utc_falling != nzst_falling:
        selected = "UTC" if utc_falling else "NZST"
        return selected, "Selected to match the reported falling tide at grounding."

    return (
        "UTC",
        "MetService event times and the reported falling tide did not distinguish "
        "UTC from fixed NZST; UTC retained provisionally.",
    )


def find_extrema(times: list[datetime], levels: np.ndarray) -> list[dict]:
    peaks, _ = find_peaks(levels, distance=180, prominence=0.005)
    lows, _ = find_peaks(-levels, distance=180, prominence=0.005)
    events = [
        {"time": times[i].isoformat(), "type": "high", "level_m": round(float(levels[i]), 4)}
        for i in peaks
    ]
    events.extend(
        {"time": times[i].isoformat(), "type": "low", "level_m": round(float(levels[i]), 4)}
        for i in lows
    )
    return sorted(events, key=lambda event: event["time"])


def find_windows(times: list[datetime], levels: np.ndarray, threshold: float) -> list[dict]:
    windows: list[dict] = []
    start = times[0] if levels[0] >= threshold else None

    for i in range(1, len(times)):
        previous, current = levels[i - 1], levels[i]
        if previous < threshold <= current:
            fraction = (threshold - previous) / (current - previous)
            start = times[i - 1] + (times[i] - times[i - 1]) * float(fraction)
        elif previous >= threshold > current and start is not None:
            fraction = (threshold - previous) / (current - previous)
            end = times[i - 1] + (times[i] - times[i - 1]) * float(fraction)
            windows.append({"start": start.isoformat(), "end": end.isoformat()})
            start = None

    if start is not None:
        windows.append({"start": start.isoformat(), "end": times[-1].isoformat()})
    return windows


def build_forecast(as_of: date, output_dir: Path) -> dict:
    times, levels, stats = load_observations(SOURCE)
    first, last = times[0], times[-1]
    holdout_start = last - timedelta(days=45)
    train_times = [t for t in times if t < holdout_start]
    train_levels = [v for t, v in zip(times, levels) if t < holdout_start]
    holdout_times = [t for t in times if t >= holdout_start]
    holdout_levels = np.asarray([v for t, v in zip(times, levels) if t >= holdout_start])

    validation_coef = fit_model(train_times, train_levels)
    validation_prediction = predict(validation_coef, holdout_times)
    residuals = validation_prediction - holdout_levels
    validation = {
        "observations": len(holdout_times),
        "bias_m": round(float(np.mean(residuals)), 4),
        "rmse_m": round(float(np.sqrt(np.mean(residuals**2))), 4),
        "mae_m": round(float(np.mean(np.abs(residuals))), 4),
    }

    basis_results = evaluate_time_basis(validation_coef)
    with MET_SERVICE_REFERENCE.open(encoding="utf-8") as source:
        reference_data = json.load(source)
    metservice_scores = score_metservice_times(validation_coef, reference_data["events"])
    source_time_basis, basis_reason = select_time_basis(metservice_scores, basis_results)

    source_offset = timedelta(hours=12) if source_time_basis == "NZST" else timedelta()
    model_times = [timestamp - source_offset for timestamp in times]
    coef = fit_model(model_times, levels)
    local_start = datetime.combine(as_of, time.min, tzinfo=LOCAL_TZ)
    local_end = datetime.combine(as_of + timedelta(days=365), time.min, tzinfo=LOCAL_TZ)
    start_utc = local_start.astimezone(timezone.utc)
    end_utc = local_end.astimezone(timezone.utc)
    start_epoch = int(start_utc.timestamp())
    end_epoch = int(end_utc.timestamp())
    epoch_seconds = np.arange(start_epoch, end_epoch + 1, 60, dtype=np.int64)
    forecast_times = [
        datetime.fromtimestamp(int(ts), timezone.utc) for ts in epoch_seconds
    ]
    forecast_levels = predict(coef, forecast_times)

    grounding_level = float(predict(coef, [GROUNDED_AT.astimezone(timezone.utc)])[0])
    next_minute = GROUNDED_AT.astimezone(timezone.utc) + timedelta(minutes=1)
    next_level = float(predict(coef, [next_minute])[0])
    falling = next_level < grounding_level

    output_dir.mkdir(parents=True, exist_ok=True)
    chunks: dict[str, list[float]] = defaultdict(list)
    chunk_starts: dict[str, int] = {}
    for epoch, level in zip(epoch_seconds, forecast_levels):
        key = datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m")
        if key not in chunk_starts:
            chunk_starts[key] = int(epoch) * 1000
        chunks[key].append(round(float(level), 4))

    chunk_metadata = []
    for month, values in chunks.items():
        filename = f"forecast-{month}.json"
        with (output_dir / filename).open("w", encoding="utf-8") as target:
            json.dump(
                {"start_ms": chunk_starts[month], "step_ms": 60_000, "levels_m": values},
                target,
                separators=(",", ":"),
            )
        chunk_metadata.append(
            {
                "file": filename,
                "start_ms": chunk_starts[month],
                "count": len(values),
                "end_ms": chunk_starts[month] + (len(values) - 1) * 60_000,
            }
        )

    events = find_extrema(forecast_times, forecast_levels)
    windows = find_windows(forecast_times, forecast_levels, grounding_level)
    with (output_dir / "events.json").open("w", encoding="utf-8") as target:
        json.dump({"events": events, "sailable_windows": windows}, target, separators=(",", ":"))

    constituent_names = [str(name) for name in coef.name]
    manifest = {
        "timezone": "Pacific/Auckland",
        "level_units": "metres (assumed); source vertical datum not specified",
        "source_time_assumption": (
            f"Source timestamps interpreted as {source_time_basis}; "
            "the file does not specify a timezone."
        ),
        "source_period": {
            "start": first.replace(tzinfo=None).isoformat(sep=" "),
            "end": last.replace(tzinfo=None).isoformat(sep=" "),
        },
        "source_quality": {
            **stats,
            "valid_observations": len(times),
            "minimum_m": round(min(levels), 4),
            "maximum_m": round(max(levels), 4),
        },
        "validation": validation,
        "model": {
            "name": "UTide",
            "latitude_degrees": LATITUDE,
            "constituents": constituent_names,
            "source_time_basis": source_time_basis,
            "time_basis_diagnostic": {
                "candidate_predictions": basis_results,
                "metservice_reference": {
                    "source": reference_data["source"],
                    "events": len(reference_data["events"]),
                    "mean_absolute_error_minutes": metservice_scores,
                },
                "selection_reason": basis_reason,
            },
            "calibration": {
                "time_local": GROUNDED_AT.isoformat(),
                "predicted_level_m": round(grounding_level, 4),
                "falling_tide": falling,
            },
        },
        "threshold_m": round(grounding_level, 4),
        "forecast": {
            "start_ms": start_epoch * 1000,
            "end_ms": end_epoch * 1000,
            "resolution_minutes": 1,
            "chunks": chunk_metadata,
        },
    }
    with (output_dir / "manifest.json").open("w", encoding="utf-8") as target:
        json.dump(manifest, target, indent=2)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Fit UTide and generate a static tide forecast.")
    parser.add_argument("--as-of-date", type=date.fromisoformat, default=datetime.now(LOCAL_TZ).date())
    parser.add_argument("--output", type=Path, default=ROOT / "web" / "public" / "data")
    args = parser.parse_args()
    manifest = build_forecast(args.as_of_date, args.output)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
