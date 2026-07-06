#!/usr/bin/env python3
import argparse
import atexit
import bisect
import csv
import math
import signal
import sys
import time
from dataclasses import dataclass
from typing import List, Tuple

try:
    import board
    import adafruit_sht4x
except ImportError:
    board = None
    adafruit_sht4x = None

try:
    import lib8mosind
except ImportError:
    lib8mosind = None


# -----------------------------
# Hardware mapping
# -----------------------------
PRIMARY_HEATER_CHANNEL = 8
ASSIST_HEATER_CHANNEL = 7
MISTER_CHANNEL = 1
STACK_LEVEL = 0

ALL_MOSFET_CHANNELS = range(1, 9)

# -----------------------------
# Control timing
# -----------------------------
DECISION_INTERVAL_S = 1.0
MISTER_PULSE_ON_S = 1.5
MISTER_COOLDOWN_S = 7.0
LOOP_SLEEP_S = 0.05

# -----------------------------
# Humidity tuning
# -----------------------------
HUMIDITY_SETPOINT_OFFSET_RH = -0.25
HUMIDITY_DEADBAND_RH = 0.0

# -----------------------------
# Heater staging
# -----------------------------
ASSIST_HEATER_RH_THRESHOLD = 55.0


@dataclass(frozen=True)
class ProgramPoint:
    time_s: float
    temp_c: float
    rh_percent: float


class ProgramSchedule:
    def __init__(self, points: List[ProgramPoint]):
        if not points:
            raise ValueError("Program CSV must contain at least one row.")

        points = sorted(points, key=lambda p: p.time_s)

        for i in range(len(points) - 1):
            if points[i + 1].time_s <= points[i].time_s:
                raise ValueError("Program times must be strictly increasing.")

        self.points = points
        self.times = [p.time_s for p in points]
        self.end_time_s = points[-1].time_s

    @classmethod
    def from_csv(cls, path: str) -> "ProgramSchedule":
        points = []
        with open(path, "r", newline="") as f:
            reader = csv.reader(f)
            for row_num, row in enumerate(reader, start=1):
                if not row or all(not cell.strip() for cell in row):
                    continue

                if len(row) != 3:
                    raise ValueError(
                        f"Row {row_num} in {path!r} must have exactly 3 columns: "
                        "time_s,temp_c,rh_percent"
                    )

                try:
                    points.append(
                        ProgramPoint(
                            time_s=float(row[0]),
                            temp_c=float(row[1]),
                            rh_percent=float(row[2]),
                        )
                    )
                except ValueError as exc:
                    raise ValueError(f"Invalid numeric value on row {row_num}: {row}") from exc

        return cls(points)

    def setpoint_at(self, t_s: float) -> Tuple[float, float]:
        if t_s <= self.points[0].time_s:
            return self.points[0].temp_c, self.points[0].rh_percent

        if t_s >= self.points[-1].time_s:
            return self.points[-1].temp_c, self.points[-1].rh_percent

        idx = bisect.bisect_right(self.times, t_s) - 1
        p0 = self.points[idx]
        p1 = self.points[idx + 1]

        frac = (t_s - p0.time_s) / (p1.time_s - p0.time_s)
        temp = p0.temp_c + frac * (p1.temp_c - p0.temp_c)
        rh = p0.rh_percent + frac * (p1.rh_percent - p0.rh_percent)
        return temp, rh


class ChamberIO:
    def read_sensor(self) -> Tuple[float, float]:
        raise NotImplementedError

    def set_heaters(self, primary_active: bool, assist_active: bool) -> None:
        raise NotImplementedError

    def set_mister(self, active: bool) -> None:
        raise NotImplementedError

    def all_outputs_off(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class RealChamberIO(ChamberIO):
    def __init__(
        self,
        stack_level: int,
        primary_heater_channel: int,
        assist_heater_channel: int,
        mister_channel: int,
    ):
        if board is None or adafruit_sht4x is None:
            raise RuntimeError(
                "Missing sensor libraries. Install adafruit-blinka and "
                "adafruit-circuitpython-sht4x."
            )
        if lib8mosind is None:
            raise RuntimeError(
                "Missing Sequent library. Install it with:\n"
                "    python3 -m pip install SM8mosind"
            )

        self.stack_level = stack_level
        self.primary_heater_channel = primary_heater_channel
        self.assist_heater_channel = assist_heater_channel
        self.mister_channel = mister_channel

        i2c = board.I2C()
        self.sht = adafruit_sht4x.SHT4x(i2c)
        self.sht.mode = adafruit_sht4x.Mode.NOHEAT_HIGHPRECISION

        self._primary_heater_active = False
        self._assist_heater_active = False
        self._mister_active = False

        self.all_outputs_off()
        atexit.register(self._safe_shutdown_hook)

    def _safe_shutdown_hook(self) -> None:
        try:
            self.all_outputs_off()
        except Exception:
            pass

    def read_sensor(self) -> Tuple[float, float]:
        temperature_c, rh_percent = self.sht.measurements
        return float(temperature_c), float(rh_percent)

    def _set_channel(self, channel: int, active: bool) -> None:
        lib8mosind.set(self.stack_level, channel, 1 if active else 0)

    def set_heaters(self, primary_active: bool, assist_active: bool) -> None:
        # Always reassert the commanded state every second
        self._set_channel(self.primary_heater_channel, primary_active)
        self._set_channel(self.assist_heater_channel, assist_active)
        self._primary_heater_active = primary_active
        self._assist_heater_active = assist_active

    def set_mister(self, active: bool) -> None:
        # Always reassert the commanded state
        self._set_channel(self.mister_channel, active)
        self._mister_active = active

    def all_outputs_off(self) -> None:
        for channel in ALL_MOSFET_CHANNELS:
            try:
                self._set_channel(channel, False)
            except Exception:
                pass

        self._primary_heater_active = False
        self._assist_heater_active = False
        self._mister_active = False

    def close(self) -> None:
        self.all_outputs_off()


class MockChamberIO(ChamberIO):
    def __init__(self, initial_temp_c: float = 25.0, initial_rh_percent: float = 40.0):
        self.temp_c = initial_temp_c
        self.rh_percent = initial_rh_percent
        self.primary_heater_active = False
        self.assist_heater_active = False
        self.mister_active = False

    def read_sensor(self) -> Tuple[float, float]:
        heater_strength = 0.0
        if self.primary_heater_active:
            heater_strength += 0.03
        if self.assist_heater_active:
            heater_strength += 0.03

        if heater_strength > 0:
            self.temp_c += heater_strength
        else:
            self.temp_c -= 0.005

        if self.mister_active:
            self.rh_percent += 0.20
        else:
            self.rh_percent -= 0.03

        self.temp_c = max(0.0, min(80.0, self.temp_c))
        self.rh_percent = max(0.0, min(100.0, self.rh_percent))
        return self.temp_c, self.rh_percent

    def set_heaters(self, primary_active: bool, assist_active: bool) -> None:
        self.primary_heater_active = primary_active
        self.assist_heater_active = assist_active

    def set_mister(self, active: bool) -> None:
        self.mister_active = active

    def all_outputs_off(self) -> None:
        self.primary_heater_active = False
        self.assist_heater_active = False
        self.mister_active = False

    def close(self) -> None:
        self.all_outputs_off()


def humidity_control_target(program_rh_sp: float) -> float:
    return program_rh_sp + HUMIDITY_SETPOINT_OFFSET_RH


def humidity_trigger_threshold(program_rh_sp: float) -> float:
    return humidity_control_target(program_rh_sp) - HUMIDITY_DEADBAND_RH


def assist_heater_allowed(program_rh_sp: float) -> bool:
    return humidity_control_target(program_rh_sp) >= ASSIST_HEATER_RH_THRESHOLD


def format_csv_row(
    elapsed_s: int,
    temp_sp: float,
    temp_meas: float,
    rh_program_sp: float,
    rh_control_sp: float,
    rh_meas: float,
    primary_heater_active: bool,
    assist_heater_active: bool,
    mister_active: bool,
) -> List[object]:
    return [
        elapsed_s,
        f"{temp_sp:.2f}",
        f"{temp_meas:.2f}",
        f"{rh_program_sp:.2f}",
        f"{rh_control_sp:.2f}",
        f"{rh_meas:.2f}",
        1 if primary_heater_active else 0,
        1 if assist_heater_active else 0,
        1 if mister_active else 0,
    ]


def format_status_line(
    elapsed_s: int,
    temp_sp: float,
    temp_meas: float,
    rh_program_sp: float,
    rh_control_sp: float,
    rh_meas: float,
    primary_heater_active: bool,
    assist_heater_active: bool,
    mister_active: bool,
) -> str:
    primary_txt = "ON " if primary_heater_active else "OFF"
    assist_txt = "ON " if assist_heater_active else "OFF"
    mister_txt = "ON " if mister_active else "OFF"

    return (
        f"[t = {elapsed_s:6d} s]  "
        f"TEMP | SP: {temp_sp:6.2f} C | MEAS: {temp_meas:6.2f} C | "
        f"H8_CMD: {primary_txt} | H7_CMD: {assist_txt}   "
        f"RH | PROG: {rh_program_sp:6.2f} % | CTRL: {rh_control_sp:6.2f} % | "
        f"MEAS: {rh_meas:6.2f} % | MISTER_CMD: {mister_txt}"
    )


def print_status_header() -> None:
    print(
        "[time]          TEMPERATURE                                                  HUMIDITY",
        flush=True,
    )
    print(
        "               setpoint      measured      primary      assist              program       control       measured      output",
        flush=True,
    )


def run_program(program_csv: str, log_csv: str, dry_run: bool = False) -> None:
    schedule = ProgramSchedule.from_csv(program_csv)

    if dry_run:
        io = MockChamberIO(
            initial_temp_c=schedule.points[0].temp_c,
            initial_rh_percent=schedule.points[0].rh_percent,
        )
    else:
        io = RealChamberIO(
            stack_level=STACK_LEVEL,
            primary_heater_channel=PRIMARY_HEATER_CHANNEL,
            assist_heater_channel=ASSIST_HEATER_CHANNEL,
            mister_channel=MISTER_CHANNEL,
        )

    stop_requested = False

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    primary_heater_active = False
    assist_heater_active = False
    mister_active = False
    mister_on_until = 0.0
    mister_cooldown_until = 0.0

    try:
        with open(log_csv, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "time_from_start_s",
                "temp_setpoint_c",
                "measured_temp_c",
                "humidity_program_setpoint_rh",
                "humidity_control_target_rh",
                "measured_humidity_rh",
                "primary_heater_active",
                "assist_heater_active",
                "mister_active",
            ])
            f.flush()

            start_monotonic = time.monotonic()
            next_decision_time = 0.0

            print_status_header()

            while True:
                elapsed = time.monotonic() - start_monotonic

                if stop_requested:
                    break

                if elapsed > schedule.end_time_s:
                    break

                if elapsed < next_decision_time:
                    time.sleep(LOOP_SLEEP_S)
                    continue

                decision_second = int(math.floor(elapsed))
                sample_time = float(decision_second)

                # Take exactly one reading for this second
                measured_temp_c, measured_rh = io.read_sensor()

                # End mister pulse if its ON time has elapsed by now
                if mister_active and elapsed >= mister_on_until:
                    io.set_mister(False)
                    mister_active = False

                # Setpoints for this logged/decision second
                temp_sp, rh_program_sp = schedule.setpoint_at(sample_time)
                rh_control_sp = humidity_control_target(rh_program_sp)
                rh_trigger_sp = humidity_trigger_threshold(rh_program_sp)

                # Heater control from this exact reading
                temp_demand = measured_temp_c < temp_sp
                primary_heater_active = temp_demand
                assist_heater_active = temp_demand and assist_heater_allowed(rh_program_sp)
                io.set_heaters(primary_heater_active, assist_heater_active)

                # Mister control from this exact reading
                if (
                    (not mister_active)
                    and (elapsed >= mister_cooldown_until)
                    and (measured_rh < rh_trigger_sp)
                ):
                    io.set_mister(True)
                    mister_active = True
                    mister_on_until = elapsed + MISTER_PULSE_ON_S
                    mister_cooldown_until = mister_on_until + MISTER_COOLDOWN_S

                row = format_csv_row(
                    decision_second,
                    temp_sp,
                    measured_temp_c,
                    rh_program_sp,
                    rh_control_sp,
                    measured_rh,
                    primary_heater_active,
                    assist_heater_active,
                    mister_active,
                )
                writer.writerow(row)
                f.flush()

                print(
                    format_status_line(
                        decision_second,
                        temp_sp,
                        measured_temp_c,
                        rh_program_sp,
                        rh_control_sp,
                        measured_rh,
                        primary_heater_active,
                        assist_heater_active,
                        mister_active,
                    ),
                    flush=True,
                )

                next_decision_time = decision_second + DECISION_INTERVAL_S

    finally:
        io.close()
        print("\nSafety shutdown: all MOSFET channels forced OFF.", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an environmental chamber program from a CSV file."
    )
    parser.add_argument(
        "program_csv",
        help="Input CSV: time_from_start_s,temp_c,rh_percent"
    )
    parser.add_argument(
        "--log-csv",
        default="chamber_log.csv",
        help="Output CSV for 1 Hz logged measurements"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without hardware using a simulated chamber"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        run_program(args.program_csv, args.log_csv, dry_run=args.dry_run)
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)