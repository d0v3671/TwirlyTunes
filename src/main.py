#!/usr/bin/env python3
"""TwirlyTunes: Raspberry Pi cry-voting controller for INMP441 + AUX + ESP32.

REAL ACTIONS REQUIRE --run (or use the service after supervised validation).

Audio path: I2S INMP441 -> ALSA 44.1 kHz stereo S32_LE -> left mono ->
8 kHz -> same 64-bin Mel preprocessing as existing twirly.py -> float32
(1,224,224,3) -> TFLite -> sigmoid score.

Important: the original training notebook must still be checked for exact
Mel convention, dB reference, resize method, and channel construction.
The deployment JSON alone cannot confirm those details.

Decision: 6+ positives (>=0.80) among 10 *overlapping* 2s windows with 1s hop.
Soothing: close arecord (no recording during music); repeatedly send CRY to
ESP32 while aplay runs; STOP at timeout; cooldown; reopen arecord and collect
an entirely NEW set of 10 votes.

HARDWARE SAFETY: ESP32 firmware MUST have its own auto-stop watchdog; Pi
cannot guarantee stopping a motor if its power or USB connection fails.
This is a supervised prototype, not a baby monitor or certified infant device.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import select
import shutil
import signal
import subprocess
import sys
import time
import wave
from collections import deque
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from twirly import Classifier, as_mono, load_config, resample_mono

BASE = Path(__file__).resolve().parent.parent
LOG = logging.getLogger("twirlytunes")
STOP_REQUESTED = False


class GracefulExit(Exception):
    """Shutdown requested by SIGTERM/SIGINT."""


def signal_handler(signum, _frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True
    raise GracefulExit(f"Received signal {signum}")


def check_stop(deadline: float | None = None):
    if STOP_REQUESTED or (deadline is not None and time.monotonic() >= deadline):
        raise GracefulExit("Stop requested or test duration completed")


def nap(seconds: float, deadline: float | None = None):
    """Sleep in short slices so shutdown interrupts playback and cooldown."""
    until = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < until:
        check_stop(deadline)
        time.sleep(max(0.0, min(0.1, until - time.monotonic())))


def load_settings():
    settings = json.loads((BASE / "config" / "settings.json").read_text(encoding="utf-8"))
    required = [
        "microphone_device", "capture_rate", "channel", "step_seconds",
        "cry_probability_threshold", "vote_window", "minimum_cry_votes",
        "minimum_rms", "soothing_seconds", "cooldown_seconds",
        "esp32_serial_port", "esp32_baud", "motor_keepalive_seconds",
        "aux_output_device", "lullaby_file", "log_every_n_windows", "csv_logging"
    ]
    missing = [key for key in required if key not in settings]
    if missing:
        raise ValueError(f"settings.json missing required fields: {missing}")
    cfg = load_config()
    if cfg["sample_rate"] != 8000 or cfg["window_seconds"] != 2 or cfg["input_shape"] != [1, 224, 224, 3]:
        raise ValueError("Unexpected TFLite training configuration; inspect before running")
    if settings["channel"] not in ("left", "right", "auto"):
        raise ValueError("channel must be left, right or auto")
    if settings["capture_rate"] <= 0 or settings["esp32_baud"] <= 0:
        raise ValueError("Invalid sample rate or baud")
    hop = float(settings["step_seconds"])
    if not (hop > 0 and hop <= 2 and (2 / hop).is_integer()):
        raise ValueError("step_seconds must divide 2.0 exactly (e.g. 0.5 or 1.0)")
    if not (0 < float(settings["cry_probability_threshold"]) < 1):
        raise ValueError("cry_probability_threshold must be between 0 and 1")
    window = settings["vote_window"]
    minimum = settings["minimum_cry_votes"]
    if not (isinstance(window, int) and isinstance(minimum, int) and 1 <= minimum <= window <= 60):
        raise ValueError("Invalid vote_window / minimum_cry_votes")
    if not (0 <= settings["minimum_rms"] < 1):
        raise ValueError("minimum_rms must be [0,1)")
    if not (0.5 <= settings["soothing_seconds"] <= 120):
        raise ValueError("soothing_seconds must be between 0.5 and 120")
    if not (0 <= settings["cooldown_seconds"] <= 120):
        raise ValueError("cooldown_seconds must be between 0 and 120")
    # Tested ESP32 sketch auto-stops after ~1.5s; refresh much sooner.
    if not (0.2 <= settings["motor_keepalive_seconds"] <= 0.7):
        raise ValueError("Keepalive must be 0.2 to 0.7 seconds for short ESP32 watchdog")
    if not (1 <= settings["log_every_n_windows"] <= 100):
        raise ValueError("log_every_n_windows must be 1 to 100")
    if not settings["microphone_device"] or not settings["aux_output_device"]:
        raise ValueError("ALSA device name cannot be blank")
    return settings, cfg


class Votes:
    """Ring buffer; only a FULL set of 10 predictions can trigger an action."""
    def __init__(self, size, minimum, threshold):
        self.size = size
        self.minimum = minimum
        self.threshold = threshold
        self.items = deque(maxlen=size)

    def add(self, probability):
        if not (math.isfinite(probability) and 0.0 <= probability <= 1.0):
            raise ValueError(f"Invalid model probability {probability}")
        self.items.append(float(probability) >= self.threshold)
        return len(self.items) == self.size and sum(self.items) >= self.minimum

    @property
    def positives(self):
        return sum(self.items)

    @property
    def count(self):
        return len(self.items)


class Microphone:
    """ALSA arecord pipe, with a read timeout and safe subprocess cleanup."""
    def __init__(self, settings):
        self.settings = settings
        self.proc = None
        self.source_rate = int(settings["capture_rate"])
        self.channels = 2
        self.frames = int(round(self.source_rate * settings["step_seconds"]))
        self.bytes_per_step = self.frames * self.channels * 4

    def __enter__(self):
        cmd = ["arecord", "-q", "-D", self.settings["microphone_device"],
               "-f", "S32_LE", "-r", str(self.source_rate), "-c", "2", "-t", "raw"]
        LOG.info("Starting mic: %s", " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        return self

    def read_step(self, deadline=None):
        raw = bytearray()
        idle_deadline = time.monotonic() + 5.0
        fd = self.proc.stdout.fileno()
        while len(raw) < self.bytes_per_step:
            check_stop(deadline)
            remaining = max(0.0, idle_deadline - time.monotonic())
            if remaining <= 0:
                raise RuntimeError("INMP441 audio stalled for 5 seconds; restarting is safer")
            ready, _, _ = select.select([fd], [], [], min(remaining, 0.5))
            if not ready:
                continue
            part = os.read(fd, min(self.bytes_per_step - len(raw), 262144))
            if not part:
                rc = self.proc.poll()
                detail = self.proc.stderr.read(4096).decode(errors="replace") if rc is not None else ""
                raise RuntimeError(f"arecord stopped (exit={rc}): {detail.strip()}")
            raw.extend(part)
            idle_deadline = time.monotonic() + 5.0
        data = np.frombuffer(raw, dtype="<i4").reshape((-1, self.channels))
        audio = as_mono(data.astype(np.float32) / 2147483648.0, self.settings["channel"])
        resampled = resample_mono(audio, self.source_rate, 8000)
        expected = int(round(float(self.settings["step_seconds"]) * 8000))
        if len(resampled) != expected:
            # resample_poly sometimes differs by one sample for non-integer ratios.
            if len(resampled) < expected:
                resampled = np.pad(resampled, (0, expected - len(resampled)))
            else:
                resampled = resampled[:expected]
        return resampled

    def __exit__(self, *_args):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=2)
            for stream in (self.proc.stdout, self.proc.stderr):
                if stream:
                    stream.close()
            self.proc = None
        LOG.info("Microphone stopped (closed during music to avoid feedback)")


class Motor:
    """Short CRY keepalives / STOP, compatible with tested ESP32 Serial Monitor firmware."""
    def __init__(self, settings):
        import serial
        port = settings["esp32_serial_port"]
        LOG.info("Opening ESP32 %s at %d baud", port, settings["esp32_baud"])
        self.ser = serial.Serial(port=port, baudrate=int(settings["esp32_baud"]),
                                 timeout=0.15, write_timeout=0.5, exclusive=True)
        # Opening USB serial may reboot the ESP32: wait before STOP.
        time.sleep(2.0)
        self.ser.reset_input_buffer()
        self.send("STOP", drain_ack=False)
        # Both example firmwares reply with MOTOR STOPPED or ACK STOP.
        ack = []
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            line = self.ser.readline().decode("utf-8", errors="replace").strip()
            if line:
                ack.append(line)
                if "STOP" in line.upper():
                    break
        if not any("STOP" in msg.upper() for msg in ack):
            self.ser.close()
            raise RuntimeError(f"ESP32 did not acknowledge STOP (received {ack!r}). Check firmware/port.")
        LOG.info("ESP32 STOP handshake passed: %s", ack[-1])

    def send(self, command, drain_ack=True):
        if command not in ("CRY", "STOP"):
            raise ValueError("Motor protocol only supports CRY or STOP")
        if not self.ser.is_open:
            raise RuntimeError("ESP32 serial port is closed")
        payload = (command + "\n").encode("ascii")
        count = self.ser.write(payload)
        if count != len(payload):
            raise RuntimeError("Short serial write to ESP32")
        self.ser.flush()
        # Drain acknowledgements so an hours-long session cannot fill buffers.
        if drain_ack:
            available = self.ser.in_waiting
            if available:
                self.ser.read(min(available, 4096))

    def stop(self):
        try:
            self.send("STOP")
            LOG.info("MOTOR STOP")
        except Exception as exc:
            # The independent ESP32 watchdog must stop the motor if USB fails.
            LOG.error("Cannot send STOP: %s; ESP32 auto-stop watchdog is essential", exc)

    def close(self):
        self.stop()
        if self.ser.is_open:
            self.ser.close()


class Player:
    """ALSA 3.5mm analog jack. Automatically replays a short WAV until timeout."""
    def __init__(self, settings):
        self.device = settings["aux_output_device"]
        filepath = Path(settings["lullaby_file"])
        self.file = filepath if filepath.is_absolute() else BASE / filepath
        if not self.file.is_file():
            raise FileNotFoundError(f"Lullaby file missing: {self.file}")
        with wave.open(str(self.file), "rb") as wav:
            if wav.getnframes() <= 0 or wav.getnchannels() not in (1, 2):
                raise ValueError("Lullaby WAV must contain audio and have one or two channels")
            LOG.info("Lullaby: %s (%0.1f sec)", self.file, wav.getnframes() / wav.getframerate())
        if shutil.which("aplay") is None:
            raise RuntimeError("aplay not installed; sudo apt install alsa-utils")
        self.proc = None

    def play(self):
        if self.proc is not None and self.proc.poll() is None:
            raise RuntimeError("Player already running")
        cmd = ["aplay", "-q", "-D", self.device, str(self.file)]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        LOG.info("LULLABY PLAY (3.5 mm AUX)")

    def is_playing(self):
        return self.proc is not None and self.proc.poll() is None

    def exit_status(self):
        return None if self.proc is None else self.proc.poll()

    def stop(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=2)
            if self.proc.stderr:
                self.proc.stderr.close()
            self.proc = None
            LOG.info("LULLABY STOP")


class Controller:
    def __init__(self, settings, model_config, real_actions: bool):
        self.settings = settings
        self.model_config = model_config
        self.real_actions = real_actions
        self.classifier = None
        self.motor = None
        self.player = None
        self.csv_file = None
        self.writer = None
        self.total_windows = 0

    def open(self):
        # Model first, then audio playback, then ESP32. Missing hardware fails
        # before we open arecord or attempt motor movement.
        args = SimpleNamespace(center_stft=True, htk_mel=False, no_mel_norm=False,
                               db_reference="max", top_db=80.0, resize="bilinear")
        self.classifier = Classifier(self.model_config, args)
        LOG.info("TFLite model loaded; input=(1,224,224,3) float32, output=(1,1)")
        if self.real_actions:
            self.player = Player(self.settings)
            self.motor = Motor(self.settings)
        if self.settings["csv_logging"]:
            logdir = BASE / "logs"
            logdir.mkdir(exist_ok=True)
            filename = logdir / ("scores-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".csv")
            self.csv_file = filename.open("w", newline="", encoding="utf-8")
            self.writer = csv.writer(self.csv_file)
            self.writer.writerow(["local_time", "probability", "votes", "windows_seen", "rms"])
            LOG.info("CSV predictions log: %s", filename)

    def close(self):
        try:
            if self.player:
                self.player.stop()
        finally:
            try:
                if self.motor:
                    self.motor.close()
            finally:
                if self.csv_file:
                    self.csv_file.close()

    def listen_until_trigger(self, deadline=None):
        """Return True on positive vote, or False if duration has elapsed."""
        s = self.settings
        cfg = self.model_config
        votes = Votes(s["vote_window"], s["minimum_cry_votes"], s["cry_probability_threshold"])
        chunks = deque(maxlen=int(round(cfg["window_seconds"] / s["step_seconds"])))
        expected = int(cfg["window_seconds"] * cfg["sample_rate"])
        with Microphone(s) as mic:
            while True:
                check_stop(deadline)
                audio = mic.read_step(deadline)
                chunks.append(audio)
                if len(chunks) < chunks.maxlen:
                    continue
                data = np.concatenate(tuple(chunks))[-expected:]
                rms = float(np.sqrt(np.mean(data.astype(np.float64) ** 2)))
                # Optional quiet gate. Keep disabled until empirically calibrated.
                t0 = time.monotonic()
                p = 0.0 if rms < s["minimum_rms"] else self.classifier.score(data)
                latency = time.monotonic() - t0
                triggered = votes.add(p)
                self.total_windows += 1
                if self.writer:
                    self.writer.writerow([datetime.now().isoformat(timespec="seconds"), f"{p:.6f}",
                                          votes.positives, votes.count, f"{rms:.8f}"])
                    self.csv_file.flush()
                if self.total_windows % s["log_every_n_windows"] == 0 or triggered:
                    LOG.info("cry=%.3f threshold=%.2f votes=%d/%d (need %d) rms=%.5f inference=%.2fs",
                             p, s["cry_probability_threshold"], votes.positives, votes.count,
                             votes.minimum, rms, latency)
                if latency > s["step_seconds"]:
                    LOG.warning("Inference %.2fs exceeds hop %.2fs; readings may lag real time", latency, s["step_seconds"])
                if triggered:
                    LOG.warning("CRY PATTERN DETECTED (%d/%d), stopping mic before soothing",
                                votes.positives, votes.size)
                    return True

    def soothe(self, seconds, deadline=None):
        """Trigger sound and motor for a fixed time; both stop in finally."""
        LOG.warning("SOOTHING START for %.1f sec (microphone is OFF)", seconds)
        start = time.monotonic()
        finish = start + seconds
        if not self.real_actions:
            LOG.info("DRY RUN: would play %s and refresh ESP32 CRY commands", self.settings["lullaby_file"])
            nap(seconds, deadline)
            LOG.warning("SOOTHING END (dry run)")
            return
        try:
            self.motor.send("CRY")
            self.player.play()
            next_heartbeat = start + self.settings["motor_keepalive_seconds"]
            while time.monotonic() < finish:
                check_stop(deadline)
                now = time.monotonic()
                if now >= next_heartbeat:
                    self.motor.send("CRY")  # keep motor moving; ESP32 firmware has 1.5s independent failsafe
                    next_heartbeat = now + self.settings["motor_keepalive_seconds"]
                status = self.player.exit_status()
                if status is not None:
                    if status != 0:
                        raise RuntimeError(f"aplay failed with exit {status}. Check {self.settings['aux_output_device']} and WAV")
                    # Reuse the WAV if shorter than selected soothing time.
                    self.player.stop()
                    if time.monotonic() < finish - 0.2:
                        self.player.play()
                time.sleep(min(0.05, max(0.0, finish - time.monotonic())))
        finally:
            # Independent ESP32 watchdog is still required if Pi dies mid-action.
            self.motor.stop()
            self.player.stop()
            LOG.warning("SOOTHING END (MOTOR STOP, AUDIO STOP)")

    def loop(self, deadline=None):
        while True:
            check_stop(deadline)
            LOG.info("Listening: filling fresh %d-window vote buffer", self.settings["vote_window"])
            trigger = self.listen_until_trigger(deadline)
            if trigger:
                self.soothe(self.settings["soothing_seconds"], deadline)
                LOG.info("Cooldown: %.1fs with microphone OFF", self.settings["cooldown_seconds"])
                nap(self.settings["cooldown_seconds"], deadline)
                # Next iteration reopens arecord and starts with empty votes.


def check_environment(settings, model_config):
    model = BASE / "model" / "BabySoother_FineTuned_MobileNetV3Small.tflite"
    print(f"Model: {model} ({model.stat().st_size:,} bytes)")
    print(f"Training audio: {model_config['sample_rate']} Hz mono, {model_config['window_seconds']}s")
    print(f"Voting: {settings['minimum_cry_votes']} of {settings['vote_window']} at >= {settings['cry_probability_threshold']}")
    print(f"Soothing {settings['soothing_seconds']}s; cooldown {settings['cooldown_seconds']}s")
    for cmd in ("arecord", "aplay"):
        print(f"{cmd}: {shutil.which(cmd) or 'MISSING'}")
    print(f"Mic ALSA: {settings['microphone_device']}")
    print(f"AUX ALSA: {settings['aux_output_device']}")
    print(f"Serial: {settings['esp32_serial_port']} ({'present' if Path(settings['esp32_serial_port']).exists() else 'MISSING'})")
    path = Path(settings["lullaby_file"])
    if not path.is_absolute():
        path = BASE / path
    print(f"Lullaby: {path} ({'present' if path.exists() else 'MISSING'})")
    print("Note: ALSA card names and USB serial paths must be checked on the Pi.")
    print("Training preprocessing parity is still unconfirmed without the original notebook.")


def simulate():
    v = Votes(size=10, minimum=6, threshold=0.8)
    for i, score in enumerate([.92,.93,.14,.95,.12,.91,.87,.16,.82,.81], 1):
        fire = v.add(score)
        print(f"Window {i:2}: score={score:.2f}  {v.positives}/{v.count} cry votes  trigger={fire}")
    assert v.count == 10 and v.positives == 7
    assert fire
    print("PASS: interspersed non-cry windows do not prevent 6+/10 trigger")


def main():
    parser = argparse.ArgumentParser(description="TwirlyTunes: cry majority voting + timed motor/AUX, safe boot service")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true", help="Only print device/file configuration")
    modes.add_argument("--simulate", action="store_true", help="Test voting logic without model/hardware")
    modes.add_argument("--dry-run", action="store_true", help="Live scores and timing, NO MOTOR OR SPEAKER")
    modes.add_argument("--test-outputs", action="store_true", help="Test motor and AUX without AI, requires connected devices")
    modes.add_argument("--run", action="store_true", help="REAL cry-triggered motor and AUX speaker actions")
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="Maximum runtime for manual tests; 0 = until Ctrl+C/systemd stops")
    a = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout)
    settings, model_config = load_settings()
    if a.check:
        check_environment(settings, model_config)
        return
    if a.simulate:
        simulate()
        return
    if a.seconds < 0:
        parser.error("--seconds must be >= 0")
    # --test-outputs uses 4 seconds by default rather than the full normal soothing period.
    deadline = time.monotonic() + a.seconds if a.seconds else None
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    controller = Controller(settings, model_config, real_actions=(a.run or a.test_outputs))
    try:
        if a.test_outputs:
            # Do not require loading expensive AI model for a hardware-only test.
            controller.player = Player(settings)
            controller.motor = Motor(settings)
            controller.soothe(min(a.seconds if a.seconds else 4.0, 10.0), deadline=None)
        else:
            controller.open()
            LOG.warning("MODE: %s", "REAL OUTPUTS" if a.run else "DRY RUN (no physical outputs)")
            controller.loop(deadline)
    except GracefulExit as exc:
        LOG.warning("Graceful shutdown: %s", exc)
    finally:
        controller.close()
        LOG.info("Exiting: speaker stopped, motor STOP requested")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        LOG.exception("FATAL: %s", exc)
        sys.exit(1)
