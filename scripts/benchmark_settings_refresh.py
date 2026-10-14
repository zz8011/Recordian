#!/usr/bin/env python3
"""Isolated GTK settings/device benchmark; no capture, model/API or user config.

Run under xvfb-run with PYTHONPATH=src. --source accepts a baseline copy of
native_settings.py. --mode real enumerates system device metadata; stress uses
real threads/child processes with synthetic delay, NOT ASR/API performance.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import gi
import psutil

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from recordian import native_settings  # noqa: E402
from recordian.settings_catalog import microphone_choices  # noqa: E402


def pump_until(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("device inventory did not finish")
        GLib.MainContext.default().iteration(False)
        time.sleep(0.001)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--mode", choices=("real", "stress"), default="real")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=7)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 50:
        parser.error("rounds must be between 1 and 50")
    args.output.mkdir(parents=True, exist_ok=True)
    module = native_settings
    source = args.source or Path(native_settings.__file__)
    if args.source:
        spec = importlib.util.spec_from_file_location("settings_baseline", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Baseline code uses identical, unchanged package-local styling/assets.
        module.ROOT = native_settings.ROOT
    before = threading.active_count()
    process = psutil.Process()
    result = {
        "mode": args.mode, "python": sys.version.split()[0],
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "gtk": f"{Gtk.MAJOR_VERSION}.{Gtk.MINOR_VERSION}.{Gtk.MICRO_VERSION}",
        "scope": "GTK/device enumeration only; no recording, model or API requests",
        "samples": [],
    }
    with tempfile.TemporaryDirectory(dir=args.output) as tmp:
        app = SimpleNamespace(settings_preview=True, config_path=Path(tmp) / "hotkey.json")
        current = {"record_backend": "ffmpeg-pulse", "input_device": "default"}
        ui = module.NativeSettingsWindow(app, current, page="daily")
        pump_until(lambda: ui.window.get_window() is not None)
        try:
            for _ in range(args.rounds):
                lock = threading.Lock()
                gate = threading.Event()
                counts = {"calls": 0, "active": 0, "peak_active": 0, "completed": 0}
                durations = []
                children = []
                ticks = []
                peaks = {"workers": 0, "children": 0, "rss_bytes": process.memory_info().rss}
                start_rss = process.memory_info().rss
                start_cpu = time.process_time()

                def query(backend, current_device, lock=lock, counts=counts, gate=gate,
                          durations=durations, children=children):
                    start = time.perf_counter()
                    with lock:
                        counts["calls"] += 1
                        counts["active"] += 1
                        counts["peak_active"] = max(counts["peak_active"], counts["active"])
                    try:
                        if args.mode == "stress":
                            if not gate.wait(5):
                                raise TimeoutError("benchmark gate timed out")
                            with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.15)"],
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as child:
                                children.append(psutil.Process(child.pid))
                                child.wait(timeout=5)
                            return [("default", "跟随系统默认麦克风"), ("synthetic", "Synthetic microphone")]
                        return microphone_choices(backend, current_device)
                    finally:
                        with lock:
                            counts["active"] -= 1
                            counts["completed"] += 1
                            durations.append((time.perf_counter() - start) * 1000)

                def tick(ticks=ticks, peaks=peaks, children=children):
                    ticks.append(time.perf_counter())
                    peaks["workers"] = max(peaks["workers"], sum(
                        t.name == "recordian-device-inventory" for t in threading.enumerate()))
                    rss = process.memory_info().rss
                    alive = 0
                    for child in children:
                        try:
                            rss += child.memory_info().rss
                            alive += 1
                        except psutil.NoSuchProcess:
                            pass
                    peaks["children"] = max(peaks["children"], alive)
                    peaks["rss_bytes"] = max(peaks["rss_bytes"], rss)
                    return True

                module.microphone_choices = query
                timer = GLib.timeout_add(20, tick)
                wall_start = time.perf_counter()
                requests = 32 if args.mode == "stress" else 1
                for _request in range(requests):
                    ui.refresh_microphones()
                dispatch_ms = (time.perf_counter() - wall_start) * 1000
                tick()
                gate.set()
                # Drain callbacks as well as workers; a callback may start the
                # one coalesced follow-up in the candidate.
                pump_until(lambda counts=counts: counts["completed"] > 0 and not any(
                    t.name == "recordian-device-inventory" for t in threading.enumerate())
                    and not getattr(ui, "inventory_running", False))
                GLib.source_remove(timer)
                while GLib.MainContext.default().pending():
                    GLib.MainContext.default().iteration(False)
                sample = {
                    "requests": requests, **counts, "peak_workers": peaks["workers"],
                    "peak_children": peaks["children"] if args.mode == "stress" else None,
                    "peak_sampled_rss_delta_bytes": max(0, peaks["rss_bytes"] - start_rss),
                    "rss_scope": "parent plus synthetic children" if args.mode == "stress" else "parent only",
                    "ui_dispatch_ms": dispatch_ms,
                    "completion_ms": (time.perf_counter() - wall_start) * 1000,
                    "parent_cpu_ms": (time.process_time() - start_cpu) * 1000,
                    "query_median_ms": statistics.median(durations),
                    "heartbeat_max_gap_ms": max((b-a for a, b in zip(ticks, ticks[1:], strict=False)), default=0) * 1000,
                    "device_option_count": len(ui.controls["input_device"][1].get_model()),
                    "selection_preserved": ui.controls["input_device"][1].get_active_id() == "default",
                }
                result["samples"].append(sample)
            # Capture synthetic labels only: never store real device names.
            if args.mode == "stress":
                settle = time.monotonic() + 0.2
                pump_until(lambda: time.monotonic() >= settle)
                window = ui.window.get_window()
                width, height = ui.window.get_size()
                Gdk.pixbuf_get_from_window(window, 0, 0, width, height).savev(
                    str(args.output / "daily-synthetic.png"), "png", [], [])
        finally:
            ui.window.destroy()
    result["remaining_threads_delta"] = threading.active_count() - before
    path = args.output / "results.json"
    path.write_text(json.dumps(result, indent=2) + "\n")
    print(path)


if __name__ == "__main__":
    main()
