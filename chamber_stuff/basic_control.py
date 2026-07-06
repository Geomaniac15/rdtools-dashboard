#!/usr/bin/env python3
import argparse
import atexit
import bisect
import csv
import math
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple
from adafruit_extended_bus import ExtendedI2C as I2C

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
# Timing
# -----------------------------
DECISION_INTERVAL_S = 1.0
OUTPUT_REASSERT_INTERVAL_S = 0.25
LOOP_SLEEP_S = 0.05

# -----------------------------
# Mister tuning
# -----------------------------
MISTER_PULSE_ON_S = 0.25
MISTER_COOLDOWN_S = 4.0

# -----------------------------
# Humidity tuning
# -----------------------------
HUMIDITY_SETPOINT_OFFSET_RH = 0.0
HUMIDITY_DEADBAND_RH = 0.0

# -----------------------------
# Heater staging
# -----------------------------
ASSIST_HEATER_RH_THRESHOLD = 65.0

# -----------------------------
# Safety settings
# -----------------------------
MAX_CONSECUTIVE_SENSOR_FAILURES = 3

PLAUSIBLE_TEMP_MIN_C = -20.0
PLAUSIBLE_TEMP_MAX_C = 80.0
PLAUSIBLE_RH_MIN = 0.0
PLAUSIBLE_RH_MAX = 100.0

SAFETY_MIN_TEMP_C: Optional[float] = None
SAFETY_MAX_TEMP_C: Optional[float] = None
SAFETY_MIN_RH: Optional[float] = None
SAFETY_MAX_RH: Optional[float] = None

MAX_SKIPPED_DECISION_SECONDS = 5

# Disk syncing: False is usually better on Pi SD cards for latency/stability.
FSYNC_EVERY_ROW = False

# Diagnostics
SLOW_SENSOR_READ_WARN_S = 1.0
SLOW_LOG_WRITE_WARN_S = 1.0

# Watchdog: if the main loop stops heartbeating for this long,
# force all outputs off from a background thread.
WATCHDOG_TIMEOUT_S = 10.0
WATCHDOG_POLL_S = 0.25


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

        i2c = I2C(3)
        self.sht = adafruit_sht4x.SHT4x(i2c)
        self.sht.mode = adafruit_sht4x.Mode.NOHEAT_HIGHPRECISION

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
        self._set_channel(self.primary_heater_channel, primary_active)
        self._set_channel(self.assist_heater_channel, assist_active)

    def set_mister(self, active: bool) -> None:
        self._set_channel(self.mister_channel, active)

    def all_outputs_off(self) -> None:
        for channel in ALL_MOSFET_CHANNELS:
            try:
                self._set_channel(channel, False)
            except Exception:
                pass

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

        self.temp_c = max(-20.0, min(80.0, self.temp_c))
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


class LoopWatchdog:
    def __init__(self, io: ChamberIO, timeout_s: float, poll_s: float):
        self.io = io
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self._lock = threading.Lock()
        self._last_beat = time.monotonic()
        self._stop_event = threading.Event()
        self.tripped = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=1.0)

    def beat(self) -> None:
        with self._lock:
            self._last_beat = time.monotonic()

    def _age(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_beat

    def _run(self) -> None:
        while not self._stop_event.wait(self.poll_s):
            age = self._age()
            if age > self.timeout_s:
                if not self.tripped.is_set():
                    self.tripped.set()
                    print(
                        f"[ERROR] Watchdog tripped after {age:.2f}s without main-loop heartbeat; forcing all outputs OFF",
                        file=sys.stderr,
                        flush=True,
                    )
                try:
                    self.io.all_outputs_off()
                except Exception as exc:
                    print(f"[ERROR] Watchdog OFF command failed: {exc}", file=sys.stderr, flush=True)


def fsync_file(f) -> None:
    f.flush()
    if FSYNC_EVERY_ROW:
        os.fsync(f.fileno())


def ensure_output_path(path: str, overwrite: bool) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    if os.path.exists(path) and not overwrite:
        raise FileExistsError(
            f"Refusing to overwrite existing file: {path}\n"
            f"Use --overwrite if you really want to replace it."
        )


def derive_events_path(log_csv: str) -> str:
    base, ext = os.path.splitext(log_csv)
    if not ext:
        ext = ".csv"
    return f"{base}_events{ext}"


def humidity_control_target(program_rh_sp: float) -> float:
    return program_rh_sp + HUMIDITY_SETPOINT_OFFSET_RH


def humidity_trigger_threshold(program_rh_sp: float) -> float:
    return humidity_control_target(program_rh_sp) - HUMIDITY_DEADBAND_RH


def assist_heater_allowed(program_rh_sp: float) -> bool:
    return humidity_control_target(program_rh_sp) >= ASSIST_HEATER_RH_THRESHOLD


def is_plausible_reading(temp_c: float, rh: float) -> bool:
    return (
        PLAUSIBLE_TEMP_MIN_C <= temp_c <= PLAUSIBLE_TEMP_MAX_C
        and PLAUSIBLE_RH_MIN <= rh <= PLAUSIBLE_RH_MAX
    )


def safety_limit_breached(temp_c: float, rh: float) -> Optional[str]:
    if SAFETY_MIN_TEMP_C is not None and temp_c < SAFETY_MIN_TEMP_C:
        return f"Temperature below hard safety limit: {temp_c:.2f} C < {SAFETY_MIN_TEMP_C:.2f} C"
    if SAFETY_MAX_TEMP_C is not None and temp_c > SAFETY_MAX_TEMP_C:
        return f"Temperature above hard safety limit: {temp_c:.2f} C > {SAFETY_MAX_TEMP_C:.2f} C"
    if SAFETY_MIN_RH is not None and rh < SAFETY_MIN_RH:
        return f"Humidity below hard safety limit: {rh:.2f} % < {SAFETY_MIN_RH:.2f} %"
    if SAFETY_MAX_RH is not None and rh > SAFETY_MAX_RH:
        return f"Humidity above hard safety limit: {rh:.2f} % > {SAFETY_MAX_RH:.2f} %"
    return None


def fmt_value(value: Optional[float]) -> str:
    return "" if value is None else f"{value:.2f}"


def format_csv_row(
    elapsed_s: int,
    temp_sp: float,
    temp_meas: Optional[float],
    rh_program_sp: float,
    rh_control_sp: float,
    rh_meas: Optional[float],
    primary_heater_active: bool,
    assist_heater_active: bool,
    mister_active: bool,
) -> List[object]:
    return [
        elapsed_s,
        f"{temp_sp:.2f}",
        fmt_value(temp_meas),
        f"{rh_program_sp:.2f}",
        f"{rh_control_sp:.2f}",
        fmt_value(rh_meas),
        1 if primary_heater_active else 0,
        1 if assist_heater_active else 0,
        1 if mister_active else 0,
    ]


def format_status_line(
    elapsed_s: int,
    temp_sp: float,
    temp_meas: Optional[float],
    rh_program_sp: float,
    rh_control_sp: float,
    rh_meas: Optional[float],
    primary_heater_active: bool,
    assist_heater_active: bool,
    mister_active: bool,
) -> str:
    temp_meas_txt = " ERR " if temp_meas is None else f"{temp_meas:5.2f}"
    rh_meas_txt = " ERR " if rh_meas is None else f"{rh_meas:4.2f}"

    return (
        f"[T = {elapsed_s:4d}] "
        f"St = {temp_sp:5.2f}|Mt = {temp_meas_txt} "
        f"HEAT {1 if primary_heater_active else 0}/{1 if assist_heater_active else 0} "
        f"|| Sh = {rh_control_sp:4.2f}| Mh = {rh_meas_txt} "
        f"MIST {1 if mister_active else 0}"
    )


def print_status_header() -> None:
    print("[T] St|Mt HEAT H8/H7 || Sh|Mh MIST", flush=True)


def run_program(
    program_csv: str,
    log_csv: str,
    events_csv: str,
    dry_run: bool = False,
    overwrite: bool = False,
) -> None:
    ensure_output_path(log_csv, overwrite=overwrite)
    ensure_output_path(events_csv, overwrite=overwrite)

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

    watchdog = LoopWatchdog(io, timeout_s=WATCHDOG_TIMEOUT_S, poll_s=WATCHDOG_POLL_S)
    watchdog.start()

    stop_requested = False

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, request_stop)
    if hasattr(signal, "SIGQUIT"):
        signal.signal(signal.SIGQUIT, request_stop)

    primary_heater_cmd = False
    assist_heater_cmd = False

    mister_pulse_active = False
    mister_on_until = 0.0
    mister_cooldown_until = 0.0
    mister_cmd = False

    consecutive_sensor_failures = 0
    last_decision_second = -1
    next_output_reassert_time = 0.0
    watchdog_event_logged = False

    def emit_event(
        writer: csv.writer,
        events_file,
        elapsed_s: float,
        level: str,
        message: str,
    ) -> None:
        row = [f"{elapsed_s:.2f}", level, message]
        writer.writerow(row)
        fsync_file(events_file)
        print(f"[{level}] t={elapsed_s:.2f}s {message}", file=sys.stderr, flush=True)

    try:
        with open(log_csv, "w", newline="") as log_f, open(events_csv, "w", newline="") as events_f:
            log_writer = csv.writer(log_f)
            events_writer = csv.writer(events_f)

            log_writer.writerow([
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
            fsync_file(log_f)

            events_writer.writerow(["time_from_start_s", "level", "message"])
            fsync_file(events_f)

            start_monotonic = time.monotonic()
            print_status_header()

            while True:
                watchdog.beat()
                elapsed = time.monotonic() - start_monotonic

                if watchdog.tripped.is_set():
                    if not watchdog_event_logged:
                        emit_event(events_writer, events_f, elapsed, "ERROR", "Watchdog tripped; outputs forced OFF")
                        watchdog_event_logged = True
                    break

                if mister_pulse_active and elapsed >= mister_on_until:
                    mister_pulse_active = False
                mister_cmd = mister_pulse_active

                if elapsed >= next_output_reassert_time:
                    io.set_heaters(primary_heater_cmd, assist_heater_cmd)
                    io.set_mister(mister_cmd)
                    next_output_reassert_time = elapsed + OUTPUT_REASSERT_INTERVAL_S

                if stop_requested:
                    emit_event(events_writer, events_f, elapsed, "INFO", "Stop requested by signal")
                    break

                if elapsed > schedule.end_time_s:
                    emit_event(events_writer, events_f, elapsed, "INFO", "Program complete")
                    break

                current_second = int(math.floor(elapsed))

                if current_second <= last_decision_second:
                    time.sleep(LOOP_SLEEP_S)
                    continue

                if last_decision_second >= 0 and current_second - last_decision_second > MAX_SKIPPED_DECISION_SECONDS:
                    emit_event(
                        events_writer,
                        events_f,
                        elapsed,
                        "WARN",
                        f"Control loop delayed; skipped from {last_decision_second}s to {current_second}s"
                    )

                sample_time = float(current_second)
                temp_sp, rh_program_sp = schedule.setpoint_at(sample_time)
                rh_control_sp = humidity_control_target(rh_program_sp)
                rh_trigger_sp = humidity_trigger_threshold(rh_program_sp)

                measured_temp_c: Optional[float] = None
                measured_rh: Optional[float] = None

                try:
                    t_read_start = time.monotonic()
                    measured_temp_c, measured_rh = io.read_sensor()
                    t_read_end = time.monotonic()
                    read_duration = t_read_end - t_read_start

                    if read_duration > SLOW_SENSOR_READ_WARN_S:
                        emit_event(
                            events_writer,
                            events_f,
                            elapsed,
                            "WARN",
                            f"Slow sensor read: {read_duration:.2f}s"
                        )

                    if stop_requested:
                        emit_event(events_writer, events_f, elapsed, "INFO", "Stop requested after sensor read")
                        break

                    if watchdog.tripped.is_set():
                        if not watchdog_event_logged:
                            emit_event(events_writer, events_f, elapsed, "ERROR", "Watchdog tripped after sensor read; outputs forced OFF")
                            watchdog_event_logged = True
                        break

                    if not is_plausible_reading(measured_temp_c, measured_rh):
                        raise ValueError(
                            f"Implausible sensor reading: T={measured_temp_c:.2f} C, RH={measured_rh:.2f} %"
                        )
                except Exception as exc:
                    consecutive_sensor_failures += 1

                    primary_heater_cmd = False
                    assist_heater_cmd = False
                    mister_pulse_active = False
                    mister_cmd = False
                    io.set_heaters(False, False)
                    io.set_mister(False)

                    emit_event(
                        events_writer,
                        events_f,
                        elapsed,
                        "WARN" if consecutive_sensor_failures < MAX_CONSECUTIVE_SENSOR_FAILURES else "ERROR",
                        f"Sensor read failed ({consecutive_sensor_failures}/{MAX_CONSECUTIVE_SENSOR_FAILURES}): {exc}"
                    )

                    if not stop_requested and not watchdog.tripped.is_set():
                        log_writer.writerow(
                            format_csv_row(
                                current_second,
                                temp_sp,
                                None,
                                rh_program_sp,
                                rh_control_sp,
                                None,
                                False,
                                False,
                                False,
                            )
                        )
                        t_log_start = time.monotonic()
                        fsync_file(log_f)
                        t_log_end = time.monotonic()

                        log_duration = t_log_end - t_log_start
                        if log_duration > SLOW_LOG_WRITE_WARN_S:
                            emit_event(
                                events_writer,
                                events_f,
                                elapsed,
                                "WARN",
                                f"Slow log write/fsync: {log_duration:.2f}s"
                            )

                        print(
                            format_status_line(
                                current_second,
                                temp_sp,
                                None,
                                rh_program_sp,
                                rh_control_sp,
                                None,
                                False,
                                False,
                                False,
                            ),
                            flush=True,
                        )

                    last_decision_second = current_second

                    if consecutive_sensor_failures >= MAX_CONSECUTIVE_SENSOR_FAILURES:
                        emit_event(events_writer, events_f, elapsed, "ERROR", "Too many consecutive sensor failures; aborting")
                        break

                    time.sleep(LOOP_SLEEP_S)
                    continue

                if consecutive_sensor_failures > 0:
                    emit_event(events_writer, events_f, elapsed, "INFO", "Sensor recovered")
                consecutive_sensor_failures = 0

                limit_message = safety_limit_breached(measured_temp_c, measured_rh)
                if limit_message is not None:
                    primary_heater_cmd = False
                    assist_heater_cmd = False
                    mister_pulse_active = False
                    mister_cmd = False
                    io.set_heaters(False, False)
                    io.set_mister(False)

                    emit_event(events_writer, events_f, elapsed, "ERROR", limit_message)

                    if not stop_requested and not watchdog.tripped.is_set():
                        log_writer.writerow(
                            format_csv_row(
                                current_second,
                                temp_sp,
                                measured_temp_c,
                                rh_program_sp,
                                rh_control_sp,
                                measured_rh,
                                False,
                                False,
                                False,
                            )
                        )
                        t_log_start = time.monotonic()
                        fsync_file(log_f)
                        t_log_end = time.monotonic()

                        log_duration = t_log_end - t_log_start
                        if log_duration > SLOW_LOG_WRITE_WARN_S:
                            emit_event(
                                events_writer,
                                events_f,
                                elapsed,
                                "WARN",
                                f"Slow log write/fsync: {log_duration:.2f}s"
                            )

                        print(
                            format_status_line(
                                current_second,
                                temp_sp,
                                measured_temp_c,
                                rh_program_sp,
                                rh_control_sp,
                                measured_rh,
                                False,
                                False,
                                False,
                            ),
                            flush=True,
                        )

                    last_decision_second = current_second
                    break

                temp_demand = measured_temp_c < temp_sp
                primary_heater_cmd = temp_demand
                assist_heater_cmd = temp_demand and assist_heater_allowed(rh_program_sp)

                if (
                    (not mister_pulse_active)
                    and (elapsed >= mister_cooldown_until)
                    and (measured_rh < rh_trigger_sp)
                ):
                    mister_pulse_active = True
                    mister_on_until = elapsed + MISTER_PULSE_ON_S
                    mister_cooldown_until = mister_on_until + MISTER_COOLDOWN_S
                    emit_event(
                        events_writer,
                        events_f,
                        elapsed,
                        "INFO",
                        f"Mister pulse started; ON until {mister_on_until:.2f}s, cooldown until {mister_cooldown_until:.2f}s"
                    )

                mister_cmd = mister_pulse_active

                if stop_requested:
                    emit_event(events_writer, events_f, elapsed, "INFO", "Stop requested before output apply")
                    break

                if watchdog.tripped.is_set():
                    if not watchdog_event_logged:
                        emit_event(events_writer, events_f, elapsed, "ERROR", "Watchdog tripped before output apply; outputs forced OFF")
                        watchdog_event_logged = True
                    break

                io.set_heaters(primary_heater_cmd, assist_heater_cmd)
                io.set_mister(mister_cmd)

                if stop_requested:
                    emit_event(events_writer, events_f, elapsed, "INFO", "Stop requested before main log write")
                    break

                if watchdog.tripped.is_set():
                    if not watchdog_event_logged:
                        emit_event(events_writer, events_f, elapsed, "ERROR", "Watchdog tripped before main log write; outputs forced OFF")
                        watchdog_event_logged = True
                    break

                log_writer.writerow(
                    format_csv_row(
                        current_second,
                        temp_sp,
                        measured_temp_c,
                        rh_program_sp,
                        rh_control_sp,
                        measured_rh,
                        primary_heater_cmd,
                        assist_heater_cmd,
                        mister_cmd,
                    )
                )
                t_log_start = time.monotonic()
                fsync_file(log_f)
                t_log_end = time.monotonic()

                log_duration = t_log_end - t_log_start
                if log_duration > SLOW_LOG_WRITE_WARN_S:
                    emit_event(
                        events_writer,
                        events_f,
                        elapsed,
                        "WARN",
                        f"Slow log write/fsync: {log_duration:.2f}s"
                    )

                print(
                    format_status_line(
                        current_second,
                        temp_sp,
                        measured_temp_c,
                        rh_program_sp,
                        rh_control_sp,
                        measured_rh,
                        primary_heater_cmd,
                        assist_heater_cmd,
                        mister_cmd,
                    ),
                    flush=True,
                )

                last_decision_second = current_second
                time.sleep(LOOP_SLEEP_S)

    finally:
        try:
            watchdog.stop()
        except Exception:
            pass

        try:
            io.all_outputs_off()
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
        help="Main 1 Hz log CSV"
    )
    parser.add_argument(
        "--events-csv",
        default=None,
        help="Events/warnings CSV (default: derived from --log-csv)"
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow overwriting existing log files"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without hardware using a simulated chamber"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    events_csv = args.events_csv or derive_events_path(args.log_csv)

    try:
        run_program(
            program_csv=args.program_csv,
            log_csv=args.log_csv,
            events_csv=events_csv,
            dry_run=args.dry_run,
            overwrite=args.overwrite,
        )
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)