from __future__ import annotations

import csv
import json
import os
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pyserial is listed in requirements.txt
    serial = None
    list_ports = None

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
APP_TITLE = "A1528B eSRD range profile V02 2026.06.25 15:08"
DEFAULT_BAUD = "921600"
DEFAULT_PORT = "COM5"
DEFAULT_WINDOW = 300
MAX_POINTS = 20000
POLL_MS = 40


class UartReader(threading.Thread):
    def __init__(self, port: str, baud: int, out_queue: queue.Queue, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.port = port
        self.baud = baud
        self.out_queue = out_queue
        self.stop_event = stop_event
        self.serial_obj = None

    def run(self) -> None:
        if serial is None:
            self.out_queue.put({"type": "error", "message": "pyserial is not installed. Run: pip install -r requirements.txt"})
            return

        try:
            self.serial_obj = serial.Serial(self.port, self.baud, timeout=0.1)
            self.out_queue.put({"type": "status", "message": f"Opened {self.port} @ {self.baud}"})
            while not self.stop_event.is_set():
                raw = self.serial_obj.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    self.out_queue.put({"type": "line", "line": line, "time": time.time()})
        except Exception as exc:
            self.out_queue.put({"type": "error", "message": str(exc)})
        finally:
            try:
                if self.serial_obj and self.serial_obj.is_open:
                    self.serial_obj.close()
            except Exception:
                pass
            self.out_queue.put({"type": "closed"})


class RecorderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1180x720")
        self.root.minsize(920, 560)

        self.msg_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.reader: UartReader | None = None
        self.csv_file = None
        self.raw_file = None
        self.csv_writer = None
        self.csv_path: Path | None = None
        self.raw_path: Path | None = None

        self.records: list[dict] = []
        self.valid_count = 0
        self.error_count = 0
        self.latest_fn = "-"
        self.running = False

        self.port_var = tk.StringVar(value=DEFAULT_PORT)
        self.baud_var = tk.StringVar(value=DEFAULT_BAUD)
        self.window_var = tk.IntVar(value=DEFAULT_WINDOW)
        self.status_var = tk.StringVar(value="Ready.")
        self.file_var = tk.StringVar(value="No file yet")
        self.count_var = tk.StringVar(value="0")
        self.error_var = tk.StringVar(value="0")
        self.latest_fn_var = tk.StringVar(value="-")

        self._build_ui()
        self.refresh_ports(select_default=True)
        self.root.after(POLL_MS, self.process_queue)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=0)
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(1, weight=1)

        header = ttk.Frame(self.root, padding=(10, 8))
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=APP_TITLE, font=("Arial", 15, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="UART JSON recorder", foreground="#667085").grid(row=0, column=1, sticky="e")

        side = ttk.Frame(self.root, padding=10)
        side.grid(row=1, column=0, sticky="nsw")
        side.columnconfigure(0, weight=1)

        self._labeled_combo(side, 0, "COM port", self.port_var)
        ttk.Button(side, text="Refresh", command=self.refresh_ports).grid(row=2, column=0, sticky="ew", pady=(4, 10))

        self._labeled_entry(side, 3, "Baud rate", self.baud_var)
        tk.Button(side, text="Start", command=self.start_recording, bg="#16803c", fg="white", activebackground="#126b32", activeforeground="white", font=("Arial", 10, "bold"), relief="raised").grid(row=5, column=0, sticky="ew", pady=(12, 4))
        ttk.Button(side, text="Stop", command=self.stop_recording).grid(row=6, column=0, sticky="ew", pady=(0, 12))

        ttk.Label(side, text="Window size / frames", font=("Arial", 9, "bold")).grid(row=7, column=0, sticky="w")
        scale_row = ttk.Frame(side)
        scale_row.grid(row=8, column=0, sticky="ew")
        scale_row.columnconfigure(0, weight=1)
        self.window_label = ttk.Label(scale_row, text=str(DEFAULT_WINDOW), width=5, anchor="e")
        self.window_label.grid(row=0, column=1, padx=(8, 0))
        self.window_scale = ttk.Scale(scale_row, from_=50, to=2000, orient="horizontal", command=self.on_window_change)
        self.window_scale.grid(row=0, column=0, sticky="ew")
        self.window_scale.set(DEFAULT_WINDOW)

        stats = ttk.LabelFrame(side, text="Live Status", padding=8)
        stats.grid(row=9, column=0, sticky="ew", pady=(14, 0))
        stats.columnconfigure(1, weight=1)
        self._stat(stats, 0, "Valid records", self.count_var)
        self._stat(stats, 1, "Latest fn", self.latest_fn_var)
        self._stat(stats, 2, "JSON errors", self.error_var)

        file_box = ttk.LabelFrame(side, text="Save file", padding=8)
        file_box.grid(row=10, column=0, sticky="ew", pady=(12, 0))
        file_box.columnconfigure(0, weight=1)
        tk.Label(file_box, textvariable=self.file_var, fg="#16803c", wraplength=250, justify="left").grid(row=0, column=0, sticky="ew")
        ttk.Button(file_box, text="Open ./data folder", command=self.open_data_folder).grid(row=1, column=0, sticky="ew", pady=(8, 0))

        workspace = ttk.Frame(self.root, padding=(0, 10, 10, 10))
        workspace.grid(row=1, column=1, sticky="nsew")
        workspace.rowconfigure(0, weight=1)
        workspace.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(workspace, bg="white", highlightthickness=1, highlightbackground="#d7dde5")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda _event: self.draw_plot())

        status = ttk.Label(workspace, textvariable=self.status_var, anchor="w")
        status.grid(row=1, column=0, sticky="ew", pady=(8, 0))

    def _labeled_combo(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label, font=("Arial", 9, "bold")).grid(row=row, column=0, sticky="w")
        self.port_combo = ttk.Combobox(parent, textvariable=variable, width=28)
        self.port_combo.grid(row=row + 1, column=0, sticky="ew", pady=(2, 0))

    def _labeled_entry(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label, font=("Arial", 9, "bold")).grid(row=row, column=0, sticky="w")
        ttk.Entry(parent, textvariable=variable, width=28).grid(row=row + 1, column=0, sticky="ew", pady=(2, 0))

    def _stat(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Label(parent, textvariable=variable, font=("Arial", 9, "bold")).grid(row=row, column=1, sticky="e", pady=2)

    def refresh_ports(self, select_default: bool = False) -> None:
        ports = []
        if list_ports is not None:
            ports = [port.device for port in list_ports.comports()]
        if DEFAULT_PORT not in ports:
            ports.insert(0, DEFAULT_PORT)
        self.port_combo["values"] = ports
        if select_default or not self.port_var.get():
            self.port_var.set(DEFAULT_PORT)
        self.status_var.set(f"Ports refreshed: {', '.join(ports) if ports else 'none'}")

    def on_window_change(self, value: str) -> None:
        window = int(float(value))
        self.window_var.set(window)
        if hasattr(self, "window_label"):
            self.window_label.config(text=str(window))
        self.draw_plot()

    def open_data_folder(self) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        try:
            os.startfile(DATA_DIR)
            self.status_var.set(f"Opened data folder: {DATA_DIR}")
        except OSError as exc:
            messagebox.showerror("Open data folder failed", str(exc))

    def start_recording(self) -> None:
        if self.running:
            return
        port = self.port_var.get().strip() or DEFAULT_PORT
        try:
            baud = int(self.baud_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid baud rate", "Baud rate must be an integer.")
            return

        DATA_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.csv_path = DATA_DIR / f"jb_GEP_dataset_{stamp}.csv"
        self.raw_path = DATA_DIR / f"jb_GEP_dataset_{stamp}_raw.log"

        try:
            self.csv_file = self.csv_path.open("w", newline="", encoding="utf-8")
            self.raw_file = self.raw_path.open("w", encoding="utf-8")
            self.csv_writer = csv.DictWriter(
                self.csv_file,
                fieldnames=["pc_time_iso", "pc_time_ms", "fn", "r0", "r1", "r2", "raw_json"],
            )
            self.csv_writer.writeheader()
            self.csv_file.flush()
        except OSError as exc:
            self.close_files()
            messagebox.showerror("File error", str(exc))
            return

        self.records.clear()
        self.valid_count = 0
        self.error_count = 0
        self.latest_fn = "-"
        self.count_var.set("0")
        self.error_var.set("0")
        self.latest_fn_var.set("-")
        self.file_var.set(f"./data/{self.csv_path.name}")

        self.stop_event.clear()
        self.reader = UartReader(port, baud, self.msg_queue, self.stop_event)
        self.reader.start()
        self.running = True
        self.status_var.set(f"Recording {port} @ {baud}")
        self.draw_plot()

    def stop_recording(self) -> None:
        if self.reader:
            self.stop_event.set()
        self.running = False
        self.close_files()
        self.status_var.set("Stopped. Files closed safely.")

    def close_files(self) -> None:
        for handle in (self.csv_file, self.raw_file):
            try:
                if handle:
                    handle.flush()
                    handle.close()
            except OSError:
                pass
        self.csv_file = None
        self.raw_file = None
        self.csv_writer = None

    def process_queue(self) -> None:
        try:
            while True:
                item = self.msg_queue.get_nowait()
                kind = item.get("type")
                if kind == "line":
                    self.handle_line(item["line"], item["time"])
                elif kind == "error":
                    self.status_var.set(f"ERROR: {item.get('message', '')}")
                    self.running = False
                    self.close_files()
                elif kind == "status":
                    self.status_var.set(item.get("message", ""))
                elif kind == "closed" and not self.running:
                    self.status_var.set("UART closed.")
        except queue.Empty:
            pass
        self.draw_plot()
        self.root.after(POLL_MS, self.process_queue)

    def handle_line(self, line: str, pc_time: float) -> None:
        if self.raw_file:
            self.raw_file.write(line + "\n")

        try:
            obj = json.loads(line)
            fn = obj.get("fn")
            record = {
                "fn": self._to_number(fn),
                "r0": self._to_number(obj.get("r0")),
                "r1": self._to_number(obj.get("r1")),
                "r2": self._to_number(obj.get("r2")),
                "pc_time": pc_time,
                "raw_json": line,
            }
            if record["fn"] is None:
                raise ValueError("missing fn")
        except Exception:
            self.error_count += 1
            self.error_var.set(str(self.error_count))
            return

        self.records.append(record)
        if len(self.records) > MAX_POINTS:
            del self.records[: len(self.records) - MAX_POINTS]

        self.valid_count += 1
        self.latest_fn = str(record["fn"])
        self.count_var.set(str(self.valid_count))
        self.latest_fn_var.set(self.latest_fn)
        self.write_csv_row(record)

    @staticmethod
    def _to_number(value):
        if value is None or value == "":
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number.is_integer():
            return int(number)
        return number

    def write_csv_row(self, record: dict) -> None:
        if not self.csv_writer or not self.csv_file:
            return
        dt = datetime.fromtimestamp(record["pc_time"])
        self.csv_writer.writerow({
            "pc_time_iso": dt.isoformat(timespec="milliseconds"),
            "pc_time_ms": int(record["pc_time"] * 1000),
            "fn": record.get("fn"),
            "r0": self._blank_none(record.get("r0")),
            "r1": self._blank_none(record.get("r1")),
            "r2": self._blank_none(record.get("r2")),
            "raw_json": record.get("raw_json", ""),
        })
        self.csv_file.flush()
        if self.raw_file:
            self.raw_file.flush()

    @staticmethod
    def _blank_none(value):
        return "" if value is None else value

    def draw_plot(self) -> None:
        canvas = self.canvas
        width = max(320, canvas.winfo_width())
        height = max(240, canvas.winfo_height())
        canvas.delete("all")
        margin_l, margin_t, margin_r, margin_b = 62, 32, 22, 52
        x0, y0 = margin_l, margin_t
        x1, y1 = width - margin_r, height - margin_b
        canvas.create_rectangle(x0, y0, x1, y1, outline="#d7dde5", fill="white")

        points = self.records[-max(10, self.window_var.get()):]
        if not points:
            canvas.create_text(width / 2, height / 2, text="Waiting for UART JSON records", fill="#667085", font=("Arial", 16))
            return

        fns = [p["fn"] for p in points if p.get("fn") is not None]
        values = []
        for key in ("r0", "r1", "r2"):
            values.extend([p[key] for p in points if p.get(key) is not None])
        if not fns or not values:
            canvas.create_text(width / 2, height / 2, text="Records received, no range value yet", fill="#667085", font=("Arial", 16))
            return

        fn_min, fn_max = min(fns), max(fns)
        y_min, y_max = min(values), max(values)
        if fn_min == fn_max:
            fn_max = fn_min + 1
        if y_min == y_max:
            y_min -= 0.5
            y_max += 0.5
        pad = max(0.05, (y_max - y_min) * 0.12)
        y_min -= pad
        y_max += pad

        def sx(fn):
            return x0 + (float(fn) - fn_min) / (fn_max - fn_min) * (x1 - x0)

        def sy(value):
            return y1 - (float(value) - y_min) / (y_max - y_min) * (y1 - y0)

        for i in range(5):
            y_val = y_min + (y_max - y_min) * i / 4
            y = sy(y_val)
            canvas.create_line(x0, y, x1, y, fill="#edf0f4")
            canvas.create_text(x0 - 8, y, text=f"{y_val:.2f}", fill="#667085", anchor="e", font=("Arial", 9))

        for i in range(6):
            fn = fn_min + (fn_max - fn_min) * i / 5
            x = sx(fn)
            canvas.create_line(x, y0, x, y1, fill="#edf0f4")
            canvas.create_text(x, y1 + 18, text=str(int(round(fn))), fill="#667085", font=("Arial", 9))

        self._draw_series(points, "r0", "#ff0000", sx, sy)
        self._draw_series(points, "r1", "#00a000", sx, sy)
        self._draw_series(points, "r2", "#0000ff", sx, sy)

        canvas.create_text((x0 + x1) / 2, 14, text="range profile curve - fn", fill="#17202a", font=("Arial", 11, "bold"))
        canvas.create_text((x0 + x1) / 2, height - 16, text="frame number fn", fill="#667085", font=("Arial", 10))
        canvas.create_text(18, (y0 + y1) / 2, text="range", fill="#667085", font=("Arial", 10), angle=90)
        canvas.create_text(x1 - 8, y0 + 10, text="r0", fill="#ff0000", anchor="e", font=("Arial", 10, "bold"))
        canvas.create_text(x1 - 8, y0 + 28, text="r1", fill="#00a000", anchor="e", font=("Arial", 10, "bold"))
        canvas.create_text(x1 - 8, y0 + 46, text="r2", fill="#0000ff", anchor="e", font=("Arial", 10, "bold"))

    def _draw_series(self, points, key, color, sx, sy) -> None:
        xy = []
        for rec in points:
            if rec.get(key) is None or rec.get("fn") is None:
                continue
            xy.extend([sx(rec["fn"]), sy(rec[key])])
        if len(xy) >= 4:
            self.canvas.create_line(*xy, fill=color, width=2)
        for i in range(0, len(xy), 2):
            self.canvas.create_oval(xy[i] - 2, xy[i + 1] - 2, xy[i] + 2, xy[i + 1] + 2, fill=color, outline=color)

    def on_close(self) -> None:
        self.stop_recording()
        self.root.after(100, self.root.destroy)


if __name__ == "__main__":
    tk_root = tk.Tk()
    app = RecorderApp(tk_root)
    tk_root.mainloop()



