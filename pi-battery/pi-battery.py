#!/usr/bin/env python3
"""
INA219 battery monitor — Python rewrite of the provided Bash script.

Features:
- Reads INA219 registers via I2C (smbus2)
- Computes bus voltage, shunt voltage, current, power
- Smoothed values with in-memory moving average (configurable)
- Linear SoC estimate from voltage (same as original)
- Dynamic full-capacity calibration near full voltage with hysteresis + rate limiting
- Writes a Linux power_supply-style key=value block into /dev/pi_battery
- Persists calibration to /var/lib/batmon/calibration_data
- Minimal external deps; efficient (no forking awk/tail/tee)

Notes:
- Keep the same numeric conventions as the Bash version:
  * bus voltage in mV
  * shunt voltage in mV (INA219 LSB 10µV → 0.01 mV)
  * current in A
  * power in W
  * current_now in µA, charge in µAh when writing to BATFILE
- Averages: rolling buffer up to MAX_HISTORY (default 500); if buffer shorter than AVG_WINDOW, fall back to the latest sample (like the original logic).

Author: ChatGPT (Python port)
"""
from __future__ import annotations

import os
import sys
import time
import math
import errno
from collections import deque
from dataclasses import dataclass
from datetime import datetime

try:
    from smbus2 import SMBus
except Exception as e:
    print("ERROR: smbus2 is required (pip install smbus2)", file=sys.stderr)
    raise

# =====================
# Configuration
# =====================
BAT_CELLS = 3
BAT_CELL_CAPACITY_mAh = 2600
BAT_VOLTAGE_HIGH_mV = 4128
BAT_VOLTAGE_LOW_mV = 3100
BAT_FULL_CLAMP = 99
VOLTAGE_HYSTERESIS_mV = 50  # per cell, for full-charge detection

I2C_BUS = 2
I2C_ADDR = 0x41

# INA219 calibration constants (matching the Bash script)
CALIBRATION = 26868
POWER_LSB_W = 0.003048
CURRENT_LSB_mA = 0.1524
SHUNT_RESISTOR_OHM = 0.01

# Files
BATFILE = "/dev/pi_battery"
CALIBRATION_FILE = "/var/lib/batmon/calibration_data"

# Averaging behavior
AVG_WINDOW = 20
MAX_HISTORY = 500

# Loop behavior
SAMPLE_PERIOD_S = 2.0

# Dynamic calibration
CALIBRATION_INTERVAL_S = 3600

# Status thresholds (mV across shunt)
THRESHOLD_DISCHARGE_mV = -3.0
THRESHOLD_CHARGE_mV = 0.2

# =====================
# Derived constants
# =====================
BAT_CAPACITY_mAh = BAT_CELLS * BAT_CELL_CAPACITY_mAh
BAT_VOLTAGE_FULL_mV = BAT_VOLTAGE_HIGH_mV * BAT_CELLS
BAT_VOLTAGE_EMPTY_mV = BAT_VOLTAGE_LOW_mV * BAT_CELLS
BAT_VOLTAGE_HYST_mV = VOLTAGE_HYSTERESIS_mV * BAT_CELLS

# =====================
# INA219 registers
# =====================
REG_CONFIG = 0x00
REG_SHUNTVOLTAGE = 0x01
REG_BUSVOLTAGE = 0x02
REG_POWER = 0x03
REG_CURRENT = 0x04
REG_CALIBRATION = 0x05


@dataclass
class HistAvg:
    maxlen: int = MAX_HISTORY
    win: int = AVG_WINDOW

    def __post_init__(self):
        self.buf = deque(maxlen=self.maxlen)

    def add(self, value: float) -> float:
        self.buf.append(float(value))
        if len(self.buf) >= self.win:
            return sum(self.buf) / len(self.buf)
        # fallback to the latest value if not enough samples yet (like original)
        return float(value)


class INA219:
    def __init__(self, bus: int, addr: int):
        self.bus_num = bus
        self.addr = addr
        self.bus = SMBus(bus)

    def close(self):
        try:
            self.bus.close()
        except Exception:
            pass

    def _write_u16(self, reg: int, value: int) -> None:
        # INA219 expects MSB first
        msb = (value >> 8) & 0xFF
        lsb = value & 0xFF
        self.bus.write_i2c_block_data(self.addr, reg, [msb, lsb])

    def _read_u16(self, reg: int) -> int:
        # Read two bytes, MSB first
        data = self.bus.read_i2c_block_data(self.addr, reg, 2)
        return (data[0] << 8) | data[1]

    @staticmethod
    def _to_signed_16(val: int) -> int:
        return val - 0x10000 if val & 0x8000 else val

    def configure(self):
        # Write calibration
        self._write_u16(REG_CALIBRATION, CALIBRATION)
        # Config: 16V range, 320mV shunt, 12bit x32 samples, continuous mode
        config = ((0x00 << 13) | (0x03 << 11) | (0x0D << 7) | (0x0D << 3) | 0x07)
        self._write_u16(REG_CONFIG, config)

    def read_all(self):
        bus_raw = self._read_u16(REG_BUSVOLTAGE)
        shunt_raw_u = self._read_u16(REG_SHUNTVOLTAGE)
        current_raw_u = self._read_u16(REG_CURRENT)
        power_raw_u = self._read_u16(REG_POWER)

        shunt_raw = self._to_signed_16(shunt_raw_u)
        current_raw = self._to_signed_16(current_raw_u)
        power_raw = self._to_signed_16(power_raw_u)

        # Bus voltage: [15:3]*4mV
        bus_voltage_mV = ((bus_raw >> 3) & 0x1FFF) * 4
        # Shunt voltage: 10 µV LSB → 0.01 mV
        shunt_voltage_mV = shunt_raw * 0.01
        # Current: CURRENT_LSB is in mA/bit, convert to A
        current_A = (current_raw * CURRENT_LSB_mA) / 1000.0
        # Power: in W via POWER_LSB
        power_W = power_raw * POWER_LSB_W

        return bus_raw, shunt_raw, current_raw, power_raw, bus_voltage_mV, shunt_voltage_mV, current_A, power_W


class BatteryEstimator:
    def __init__(self):
        self.volt_hist = HistAvg()
        self.shunt_hist = HistAvg()
        self.curr_hist = HistAvg()
        self.power_hist = HistAvg()

        # dynamic calibration state
        self.dynamic_charge_full_uAh = BAT_CAPACITY_mAh * 1000  # µAh
        self.last_calibration_time = 0
        self._load_calibration()

    # -------- calibration persistence --------
    def _load_calibration(self):
        try:
            with open(CALIBRATION_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        if k == "DYNAMIC_CHARGE_FULL":
                            self.dynamic_charge_full_uAh = int(v)
                        elif k == "LAST_CALIBRATION_TIME":
                            self.last_calibration_time = int(v)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"WARN: Failed to load calibration: {e}", file=sys.stderr)

    def _save_calibration(self):
        try:
            os.makedirs(os.path.dirname(CALIBRATION_FILE), exist_ok=True)
            tmp = CALIBRATION_FILE + ".tmp"
            with open(tmp, "w") as f:
                f.write(f"DYNAMIC_CHARGE_FULL={self.dynamic_charge_full_uAh}\n")
                f.write(f"LAST_CALIBRATION_TIME={self.last_calibration_time}\n")
            os.replace(tmp, CALIBRATION_FILE)
        except Exception as e:
            print(f"WARN: Failed to save calibration: {e}", file=sys.stderr)

    # -------- core computations --------
    @staticmethod
    def soc_percent_from_voltage_mV(v_mV: int) -> int:
        if v_mV >= BAT_VOLTAGE_FULL_mV:
            return 100
        if v_mV <= BAT_VOLTAGE_EMPTY_mV:
            return 0
        num = (v_mV - BAT_VOLTAGE_EMPTY_mV) * 100
        den = (BAT_VOLTAGE_FULL_mV - BAT_VOLTAGE_EMPTY_mV)
        # ceil toward up like Bash (int(result) + 1 for non-integers)
        p = num / den
        return int(math.ceil(p))

    def calibrate_if_full(self, voltage_mV: int, charge_now_uAh: int, now_s: int):
        if now_s - self.last_calibration_time < CALIBRATION_INTERVAL_S:
            return
        if voltage_mV >= (BAT_VOLTAGE_FULL_mV - BAT_VOLTAGE_HYST_mV):
            if charge_now_uAh < self.dynamic_charge_full_uAh:
                # smooth update: 19:1 like original
                self.dynamic_charge_full_uAh = (self.dynamic_charge_full_uAh * 19 + charge_now_uAh) // 20
                self.last_calibration_time = now_s
                self._save_calibration()

    @staticmethod
    def status_from_shunt_mV(shunt_mV: float) -> int:
        if shunt_mV < THRESHOLD_DISCHARGE_mV:
            return 2  # discharging
        if shunt_mV > THRESHOLD_CHARGE_mV:
            return 1  # charging
        return 0      # full or small current

    @staticmethod
    def human_time(seconds: int) -> str:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h} h {m:02d} min"

    def step(self, bus_voltage_mV: int, shunt_voltage_mV: float, current_A: float, power_W: float):
        bus_voltage_avg_mV = int(round(self.volt_hist.add(bus_voltage_mV)))
        shunt_voltage_abs_mV = abs(shunt_voltage_mV)
        shunt_voltage_avg_mV = self.shunt_hist.add(shunt_voltage_abs_mV)
        current_abs_A = abs(current_A)
        current_avg_A = self.curr_hist.add(current_abs_A)
        power_avg_W = self.power_hist.add(power_W)

        soc_pct = self.soc_percent_from_voltage_mV(bus_voltage_avg_mV)
        charge_full_uAh = self.dynamic_charge_full_uAh
        charge_now_uAh = (charge_full_uAh * soc_pct) // 100
        current_now_uA = int(current_abs_A * 1_000_000)
        current_now_avg_uA = int(current_avg_A * 1_000_000)

        now_s = int(time.time())
        self.calibrate_if_full(bus_voltage_mV, charge_now_uAh, now_s)

        status_int = self.status_from_shunt_mV(shunt_voltage_mV)

        # --- Mobile-like behavior: clamp near 100% ---
        #if soc_pct >= 94 and status_int in (0, 1):
        #    soc_pct = 100
        #    status_int = 0   # force "Full"
        if soc_pct >= 100 or (soc_pct >= BAT_FULL_CLAMP and status_int in (0, 1)):
            soc_pct = 100
            charge_now_uAh = charge_full_uAh   # >>> TADY přidáno <<<
            current_now_uA = 1000
            status_int = 0
        # --------------------------------------------

        if status_int == 2:  # discharging
            battery_remain_sec = 0 if current_now_avg_uA <= 0 else int((charge_now_uAh / max(current_now_avg_uA, 1)) * 3600)
        elif status_int == 1:  # charging
            battery_remain_sec = 0 if current_now_avg_uA <= 0 else int(((charge_full_uAh - charge_now_uAh) / max(current_now_avg_uA, 1)) * 3600)
        else:
            battery_remain_sec = 0

        return {
            "bus_voltage_mV": bus_voltage_mV,
            "bus_voltage_avg_mV": bus_voltage_avg_mV,
            "shunt_voltage_mV": shunt_voltage_mV,
            "shunt_voltage_avg_mV": shunt_voltage_avg_mV,
            "current_A": current_A,
            "current_avg_A": current_avg_A,
            "power_W": power_W,
            "power_avg_W": power_avg_W,
            "soc_pct": soc_pct,
            "charge_full_uAh": charge_full_uAh,
            "charge_now_uAh": charge_now_uAh,
            "current_now_uA": current_now_uA,
	    "current_now_avg_uA": current_now_avg_uA,
            "status_int": status_int,
            "battery_remain_sec": battery_remain_sec,
        }


STATUS_MAP = {0: "Full", 1: "Charging", 2: "Discharging"}


def write_batfile(payload: dict) -> None:
    lines = []
    voltage_min_design_mV = BAT_VOLTAGE_EMPTY_mV
    lines.append(f"voltage_min_design={voltage_min_design_mV * 1000}")  # to µV
    lines.append(f"voltage_now={payload['bus_voltage_mV'] * 1000}")     # to µV
    lines.append(f"current_now={payload['current_now_uA']}")
    lines.append(f"charge_full_design={BAT_CAPACITY_mAh * 1000}")       # to µAh
    lines.append(f"charge_full={payload['charge_full_uAh']}")
    lines.append(f"charge_now={payload['charge_now_uAh']}")
    lines.append(f"capacity={payload['soc_pct']}")
    charging = 1 if payload['status_int'] in (0, 1) else 0
    lines.append(f"charging={charging}")

    data = "\n".join(lines) + "\n"

    try:
        with open(BATFILE, "w") as f:
            f.write(data)
    except OSError as e:
        if e.errno == errno.ENOENT:
            # optional: create device file or warn
            print(f"WARN: BATFILE {BATFILE} not found", file=sys.stderr)
        else:
            print(f"WARN: Failed to write BATFILE: {e}", file=sys.stderr)


def main():
    # Optional debug based on env
    DEBUG = os.environ.get("DEBUG", "0") == "1"

    ina = INA219(I2C_BUS, I2C_ADDR)
    est = BatteryEstimator()

    try:
        ina.configure()
        time.sleep(1.0)

        while True:
            (
                bus_raw,
                shunt_raw,
                current_raw,
                power_raw,
                bus_voltage_mV,
                shunt_voltage_mV,
                current_A,
                power_W,
            ) = ina.read_all()

            payload = est.step(
                bus_voltage_mV=bus_voltage_mV,
                shunt_voltage_mV=shunt_voltage_mV,
                current_A=current_A,
                power_W=power_W,
            )

            write_batfile(payload)

            if DEBUG:
                t=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"--- [{t}] -------------------------\n")
                # mirror the Bash diagnostic prints, condensed
                print("Battery values\n---------------------------------------------------")
                print(f"bus_raw:             {bus_raw}")
                print(f"bus_voltage:         {bus_voltage_mV} mV")
                print(f"bus_voltage_avg:     {payload['bus_voltage_avg_mV']} mV\n")

                print(f"shunt_raw:           {shunt_raw}")
                print(f"shunt_voltage:       {shunt_voltage_mV:.3f} mV")
                print(f"shunt_voltage_avg:   {payload['shunt_voltage_avg_mV']:.3f} mV\n")

                print(f"current_raw:         {current_raw}")
                print(f"current:             {current_A:.6f} A")
                print(f"current_avg:         {payload['current_avg_A']:.6f} A\n")

                print(f"power:               {power_W:.3f} W")
                print(f"power_avg:           {payload['power_avg_W']:.3f} W\n")

                print("Battery info\n---------------------------------------------------")
                print(f"Design capacity:     {BAT_CAPACITY_mAh} mAh ({BAT_CELL_CAPACITY_mAh} mAh * {BAT_CELLS})")
                print(f"Last max. capacity:  {payload['charge_full_uAh'] // 1000} mAh")
                print(f"Remaining capacity:  {payload['charge_now_uAh'] // 1000} mAh\n")
                print(f"Voltage:             {bus_voltage_mV} mV (min. design: {BAT_VOLTAGE_EMPTY_mV} mV)")
                print(f"Current:             {payload['current_avg_A']:.6f} A")
                print(f"Power:               {power_W:.3f} W\n")

                status_text = STATUS_MAP.get(payload['status_int'], 'n/a')
                print(f"Status:              {status_text}")
                print(f"Charge:              {payload['soc_pct']} %")
                if payload['status_int'] == 0:
                    print("Remaining time:      Fully charged\n")
                else:
                    print(f"Remaining time:      {BatteryEstimator.human_time(payload['battery_remain_sec'])}\n")

                print(f"Data written to {BATFILE}\n---------------------------------------------------\n")

            time.sleep(SAMPLE_PERIOD_S)

    except KeyboardInterrupt:
        pass
    finally:
        ina.close()


if __name__ == "__main__":
    # Ensure numeric formatting independent of locale
    os.environ["LC_ALL"] = "C"
    # Be robust when run under systemd
    os.environ.setdefault("TERM", "dumb")
    main()
