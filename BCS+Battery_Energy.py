import re
import struct
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import filedialog, messagebox

import os
import sys

# -------------------------------------------------
# REGEX
# -------------------------------------------------
RE_110 = re.compile(
    r"\)\s+([\d.]+)\s+(Rx|Tx)\s+(?:0x)?0*110\s+\d+\s+(.+)",
    re.IGNORECASE
)

RE_109 = re.compile(
    r"\)\s+([\d.]+)\s+(Rx|Tx)\s+(?:0x)?0*109\s+\d+\s+(.+)",
    re.IGNORECASE
)

RE_LINE = re.compile(
    r"\)\s*([\d.]+)\s+(Rx|Tx)\s+([0-9A-Fa-f]+)\s+(\d+)\s+(.+)"
)

TARGET_ID = 0x248


# -------------------------------------------------
# FILE PICKER
# -------------------------------------------------
def select_trc_files():
    root = tk.Tk()
    root.withdraw()
    return filedialog.askopenfilenames(
        title="Select TRC Files",
        filetypes=[("TRC files", "*.trc")]
    )


# -------------------------------------------------
# START TIME
# -------------------------------------------------
def extract_start_time(trc_file):
    with open(trc_file, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if "start time" in line.lower():
                raw = line.split(":", 1)[1].strip()

                parts = raw.split(".")
                if len(parts) > 2:
                    raw = parts[0] + "." + parts[1].ljust(6, "0")

                for fmt in ("%m/%d/%Y %H:%M:%S.%f", "%d-%m-%Y %H:%M:%S.%f"):
                    try:
                        return datetime.strptime(raw, fmt)
                    except:
                        continue
    return None


def extract_date_from_file(trc_file):
    dt = extract_start_time(trc_file)
    if not dt:
        return "Unknown Date"
    return dt.strftime("%d %B %Y")


# -------------------------------------------------
# PARSERS
# -------------------------------------------------
def parse_signals_script1(trc_file):
    start_time = extract_start_time(trc_file)
    data = []
    first_offset = None

    with open(trc_file, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:

            m = RE_110.search(line)
            if m:
                offset = float(m.group(1))
                if first_offset is None:
                    first_offset = offset

                ts = start_time + timedelta(milliseconds=offset) if start_time else timedelta(milliseconds=(offset - first_offset))

                d = m.group(3).split()
                if len(d) >= 8:
                    b4, b5, b6, b7 = [int(x, 16) for x in d[4:8]]
                    raw = struct.unpack("<i", bytes([b4, b5, b6, b7]))[0]
                    I = raw * 1e-5
                    data.append((ts, "I", I))

            m = RE_109.search(line)
            if m:
                offset = float(m.group(1))
                if first_offset is None:
                    first_offset = offset

                ts = start_time + timedelta(milliseconds=offset) if start_time else timedelta(milliseconds=(offset - first_offset))

                d = m.group(3).split()
                if len(d) >= 8:
                    try:
                        b6, b7 = int(d[6], 16), int(d[7], 16)
                        raw = (b7 << 8) | b6
                        V = raw * 0.1
                    except:
                        continue

                    if V > 0:
                        data.append((ts, "V", V))

    return data


def parse_signals_script2(trc_file):
    start_time = extract_start_time(trc_file)
    data = []
    first_offset = None

    with open(trc_file, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:

            m = RE_LINE.search(line)
            if not m:
                continue

            offset = float(m.group(1))
            can_id = int(m.group(3), 16)

            if can_id != TARGET_ID:
                continue

            if first_offset is None:
                first_offset = offset

            ts = (
                start_time + timedelta(milliseconds=offset)
                if start_time
                else timedelta(milliseconds=(offset - first_offset))
            )

            d = m.group(5).split()
            if len(d) < 8:
                continue

            try:
                power_kw = int(d[2], 16) * 0.1
                current = int(d[6], 16) * 0.1
                voltage = int(d[7], 16) * 4

                if voltage <= 0:
                    continue

                data.append((ts, current, voltage, power_kw))

            except:
                continue

    return data


# -------------------------------------------------
# CALCULATIONS
# -------------------------------------------------
def summarize_current(signal_list):
    DEFAULT_DT = 0.3
    pos_as = 0
    neg_as = 0

    curr = [(ts, v) for ts, t, v in signal_list if t == "I"]
    curr = sorted(curr, key=lambda x: x[0])

    for i in range(1, len(curr)):
        t0, I0 = curr[i-1]
        t1, _ = curr[i]

        dt = (t1 - t0).total_seconds()
        if dt <= 0 or dt > 0.5:
            dt = DEFAULT_DT

        if I0 >= 0:
            pos_as += I0 * dt
        else:
            neg_as += I0 * dt

    return pos_as/3600, neg_as/3600


def integrate_energy_directional(current_list, voltage_list, use_positive):
    total_j = 0
    v_idx = 0
    last_v = voltage_list[0][1] if voltage_list else 0
    DEFAULT_DT = 0.3

    for i in range(1, len(current_list)):
        t0, I0 = current_list[i-1]
        t1, _ = current_list[i]

        dt = (t1 - t0).total_seconds()
        if dt <= 0:
            continue
        if dt > 0.5:
            dt = DEFAULT_DT

        while v_idx < len(voltage_list) and voltage_list[v_idx][0] <= t1:
            last_v = voltage_list[v_idx][1]
            v_idx += 1

        if use_positive and I0 > 0:
            total_j += last_v * I0 * dt
        elif not use_positive and I0 < 0:
            total_j += last_v * (-I0) * dt

    return total_j / 3600


def integrate_energy_vi(signal_list):
    total_j = 0
    EXPECTED_DT = 0.1

    for i in range(1, len(signal_list)):
        t0, I0, V0, _ = signal_list[i-1]
        t1, _, _, _ = signal_list[i]

        dt = (t1 - t0).total_seconds()
        if dt <= 0:
            continue
        if abs(dt - EXPECTED_DT) > 0.05:
            dt = EXPECTED_DT

        total_j += V0 * I0 * dt

    return total_j / 3600


def integrate_energy_power(signal_list):
    total_wh = 0
    EXPECTED_DT = 0.1

    for i in range(1, len(signal_list)):
        t0, _, _, p_kw = signal_list[i-1]
        t1, _, _, _ = signal_list[i]

        dt = (t1 - t0).total_seconds()
        if dt <= 0:
            continue
        if abs(dt - EXPECTED_DT) > 0.05:
            dt = EXPECTED_DT

        total_wh += (p_kw * dt) / 3.6

    return total_wh


def calculate_duration(signal_list):
    if len(signal_list) < 2:
        return 0
    return (signal_list[-1][0] - signal_list[0][0]).total_seconds()


def calculate_active_duration(signal_list, threshold=0.5):
    total = 0
    EXPECTED_DT = 0.1

    for i in range(1, len(signal_list)):
        t0, I0, _, _ = signal_list[i-1]
        t1, _, _, _ = signal_list[i]

        dt = (t1 - t0).total_seconds()
        if dt <= 0 or abs(dt - EXPECTED_DT) > 0.05:
            dt = EXPECTED_DT

        if I0 > threshold:
            total += dt

    return total


def format_duration(sec):
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# -------------------------------------------------
# MAIN
# -------------------------------------------------
if __name__ == "__main__":

    files = select_trc_files()
    if not files:
        messagebox.showerror("Error", "No files selected")
        sys.exit()

    total_pos = total_neg = 0
    total_charge_e = total_discharge_e = 0
    total_vi = total_p = 0
    total_dur = total_act = 0
    count = 0

    for f in files:
        sig1 = parse_signals_script1(f)
        sig2 = parse_signals_script2(f)

        if not sig1 and not sig2:
            continue

        count += 1

        if sig1:
            current = [(ts, v) for ts, t, v in sig1 if t == "I"]
            voltage = [(ts, v) for ts, t, v in sig1 if t == "V"]

            pos, neg = summarize_current(sig1)

            total_pos += pos
            total_neg += neg
            total_charge_e += integrate_energy_directional(current, voltage, True)
            total_discharge_e += integrate_energy_directional(current, voltage, False)

        if sig2:
            total_vi += integrate_energy_vi(sig2)
            total_p += integrate_energy_power(sig2)
            total_dur += calculate_duration(sig2)
            total_act += calculate_active_duration(sig2)

    result = (
        f"Trc Files Submitted: {count}\n\n"
        f"Charge Ah: {total_pos:.4f}\n"
        f"Discharge Ah: {total_neg:.4f}\n"
        f"Battery Charge Energy (Wh): {total_charge_e:.4f}\n"
        f"Battery Discharge Energy (Wh): {total_discharge_e:.4f}\n\n"
        f"------------------------------\n\n"
        f"BCS Energy (Wh) (V×I): {total_vi:.4f}\n"
        f"BCS Energy (Wh)(Power): {total_p:.4f}\n"
        f"BCS Total Duration: {format_duration(total_dur)}\n"
        f"BCS Active Time: {format_duration(total_act)}"
    )

    date_str = extract_date_from_file(files[0]) if files else "Unknown Date"

    win = tk.Tk()
    win.title("Energy Summary")
    win.configure(bg="#0f0f0f")
    win.resizable(False, False)

    outer = tk.Frame(win, bg="#0f0f0f")
    outer.pack(padx=25, pady=25)

    box = tk.Frame(outer, bg="#141414", highlightbackground="#ff2e2e", highlightthickness=3)
    box.pack()

    header = tk.Label(
        box,
        text=f"⚡ Energy Data — {date_str}",
        font=("Segoe UI", 14, "bold"),
        fg="#ffffff",
        bg="#141414",
        pady=12
    )
    header.pack()

    divider = tk.Frame(box, bg="#ff2e2e", height=2)
    divider.pack(fill="x", padx=15, pady=(0, 10))

    content = tk.Label(
        box,
        text=result,
        font=("Consolas", 11),
        fg="#00ffaa",
        bg="#141414",
        justify="left",
        anchor="w",
        padx=20,
        pady=10
    )
    content.pack()

    btn = tk.Button(
        outer,
        text="Close",
        command=win.destroy,
        bg="#ff2e2e",
        fg="white",
        font=("Segoe UI", 10, "bold"),
        relief="flat",
        activebackground="#cc0000",
        activeforeground="white",
        padx=12,
        pady=6
    )
    btn.pack(pady=(12, 0))

    win.mainloop()