#!/usr/bin/env python3
"""TwirlyTunes Raspberry Pi cry-classifier inference and guarded optional actions.

Safe by default: --live only prints probabilities. Motor and speaker are OFF
unless the respective --enable-* arguments are explicitly supplied.

IMPORTANT: Exact training preprocessing is not proven by deployment_config.json:
confirm the dB reference, mel conventions, resize method, and color mapping
against the training notebook BEFORE interpreting model scores as validated.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
CONFIG = BASE / "model" / "deployment_config.json"
MODEL = BASE / "model" / "BabySoother_FineTuned_MobileNetV3Small.tflite"


def load_config() -> dict:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    for key in ("sample_rate", "window_seconds", "n_mels", "n_fft", "hop_length", "fmin", "fmax", "threshold", "input_shape"):
        if key not in cfg:
            raise ValueError(f"Missing required field in deployment_config.json: {key}")
    if cfg["audio_channels"] != 1 or cfg["normalization"] != "per_example_minmax_0_to_1":
        raise ValueError("This script expects mono and per-example min-max [0,1] normalization.")
    return cfg


def as_mono(samples: np.ndarray, mode: str = "left") -> np.ndarray:
    """Channel selection; INMP441 L/R=GND is wired to the left channel."""
    if samples.ndim == 1:
        return samples.astype(np.float32, copy=False)
    if samples.shape[1] == 1:
        return samples[:, 0].astype(np.float32, copy=False)
    if mode == "left":
        selected = 0
    elif mode == "right":
        selected = 1
    else:
        rms = np.sqrt(np.mean(np.square(samples.astype(np.float64)), axis=0))
        selected = int(np.argmax(rms))
    return samples[:, selected].astype(np.float32, copy=False)


def resample_mono(samples: np.ndarray, old_rate: int, new_rate: int) -> np.ndarray:
    from scipy.signal import resample_poly
    if old_rate == new_rate:
        return samples.astype(np.float32, copy=False)
    ratio = Fraction(new_rate, old_rate)
    return resample_poly(samples, ratio.numerator, ratio.denominator).astype(np.float32)


def preprocess_8k(audio: np.ndarray, cfg: dict, args: argparse.Namespace) -> np.ndarray:
    """Convert one 8 kHz, 2s mono sample into float32 [1,224,224,3].

    Assumptions not specified in supplied JSON:
      librosa Slaney mel filters (htk=False, norm='slaney'), centered Hann STFT,
      power (not magnitude), librosa.power_to_db(ref=np.max, top_db=80),
      bilinear float-image resizing, and grayscale replicated into 3 channels.
    These MUST be checked against training code.
    """
    import librosa
    from PIL import Image

    rate = int(cfg["sample_rate"])
    target_samples = int(round(float(cfg["window_seconds"]) * rate))
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if len(audio) < target_samples:
        audio = np.pad(audio, (0, target_samples - len(audio)))
    else:
        audio = audio[:target_samples]

    mel = librosa.feature.melspectrogram(
        y=audio, sr=rate, n_fft=int(cfg["n_fft"]),
        hop_length=int(cfg["hop_length"]), win_length=int(cfg["n_fft"]),
        window="hann", center=args.center_stft, pad_mode="constant",
        n_mels=int(cfg["n_mels"]), fmin=float(cfg["fmin"]),
        fmax=float(cfg["fmax"]), power=2.0,
        htk=args.htk_mel, norm=None if args.no_mel_norm else "slaney",
    )
    if args.db_reference == "max":
        db = librosa.power_to_db(mel, ref=np.max, top_db=args.top_db)
    else:
        db = librosa.power_to_db(mel, ref=1.0, top_db=args.top_db)
    lo = float(db.min())
    hi = float(db.max())
    normed = (db - lo) / (hi - lo) if hi - lo > 1e-8 else np.zeros_like(db)

    # Float ('F') image to preserve values in [0,1] without uint8 rounding.
    # Resize method is a configurable assumption; training may instead use
    # tf.image.resize or cv2.resize. They are not always pixel-identical.
    resampling = {"bilinear": Image.Resampling.BILINEAR,
                  "nearest": Image.Resampling.NEAREST,
                  "bicubic": Image.Resampling.BICUBIC}[args.resize]
    frame = Image.fromarray(normed.astype(np.float32), mode="F")
    frame = frame.resize((224, 224), resample=resampling)
    gray = np.asarray(frame, dtype=np.float32)
    rgb = np.repeat(gray[:, :, None], 3, axis=2)
    # The TFLite model has its own Rescaling(2,-1), per training notes.
    # DO NOT multiply by 2 or subtract 1 here.
    batch = rgb[None, ...]
    required = tuple(int(x) for x in cfg["input_shape"])
    if batch.shape != required:
        raise ValueError(f"Preprocessing produced {batch.shape}; config expects {required}")
    return batch


def make_interpreter():
    try:
        from ai_edge_litert.interpreter import Interpreter
    except ImportError:
        try:
            from tflite_runtime.interpreter import Interpreter
        except ImportError as exc:
            raise RuntimeError(
                "No TFLite interpreter installed. Install requirements.txt in the Pi virtual environment."
            ) from exc
    interpreter = Interpreter(model_path=str(MODEL), num_threads=2)
    interpreter.allocate_tensors()
    return interpreter


class Classifier:
    def __init__(self, cfg: dict, args: argparse.Namespace):
        self.cfg = cfg
        self.args = args
        self.interpreter = make_interpreter()
        inputs = self.interpreter.get_input_details()
        outputs = self.interpreter.get_output_details()
        if len(inputs) != 1 or len(outputs) != 1:
            raise ValueError(f"Expected one input and one output, got {len(inputs)} / {len(outputs)}")
        self.input = inputs[0]
        self.output = outputs[0]
        wanted = tuple(cfg["input_shape"])
        got = tuple(int(v) for v in self.input["shape"])
        if got != wanted:
            raise ValueError(f"Model input shape {got} does not match config {wanted}")
        if self.input["dtype"] != np.float32:
            raise ValueError(f"Expected float32 model input; got {self.input['dtype']}. Check quantization/export.")

    def score(self, audio_8k: np.ndarray) -> float:
        image = preprocess_8k(audio_8k, self.cfg, self.args)
        self.interpreter.set_tensor(self.input["index"], image)
        self.interpreter.invoke()
        tensor = self.interpreter.get_tensor(self.output["index"])
        value = float(np.asarray(tensor).reshape(-1)[0])
        if self.output["dtype"] in (np.uint8, np.int8):
            scale, zero = self.output["quantization"]
            value = (value - zero) * scale
        if not math.isfinite(value):
            raise RuntimeError("Inference returned a non-finite output")
        return value


class Actions:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.serial = None
        self.player = None
        if args.enable_motor:
            if not args.serial_port:
                raise ValueError("--enable-motor requires --serial-port (e.g. /dev/ttyUSB0)")
            import serial
            self.serial = serial.Serial(args.serial_port, baudrate=args.baud,
                                        timeout=1, write_timeout=1)
            # Some ESP32 boards reset when opening USB serial.
            time.sleep(2)
            self.send("STOP")
        if args.enable_speaker:
            if not args.lullaby or not Path(args.lullaby).is_file():
                raise ValueError("--enable-speaker requires --lullaby PATH_TO_EXISTING_WAV")
            if not args.speaker_device:
                raise ValueError("--enable-speaker requires --speaker-device from 'aplay -l'")

    def send(self, command: str):
        if self.serial is not None:
            self.serial.write((command + "\n").encode("ascii"))
            self.serial.flush()

    def start(self):
        print("ACTION: CRY detected")
        if self.serial is not None:
            self.send("CRY")
        else:
            print("  Motor not enabled — would send CRY")
        if self.args.enable_speaker:
            self.player = subprocess.Popen([
                "aplay", "-q", "-D", self.args.speaker_device, self.args.lullaby
            ])
        else:
            print("  Speaker not enabled — no playback")

    def stop(self):
        if self.serial is not None:
            self.send("STOP")
        if self.player is not None:
            self.player.terminate()
            try:
                self.player.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.player.kill()
                self.player.wait(timeout=2)
            self.player = None
        print("ACTION: STOP")

    def close(self):
        try:
            self.stop()
        finally:
            if self.serial is not None:
                self.serial.close()


class StateMachine:
    def __init__(self, threshold: float, min_run: float = 6.0,
                 max_run: float = 30.0, cooldown: float = 5.0):
        self.threshold = threshold
        self.min_run = min_run
        self.max_run = max_run
        self.cooldown = cooldown
        self.active = False
        self.above = 0
        self.below = 0
        self.started = 0.0
        self.cooled_until = 0.0
        self.rearm_needed = False

    def update(self, p: float, now: float) -> str | None:
        if p >= self.threshold:
            self.above += 1
            self.below = 0
        else:
            self.below += 1
            self.above = 0
            if self.below >= 3:
                self.rearm_needed = False
        if self.active:
            elapsed = now - self.started
            if elapsed >= self.max_run:
                self.active = False
                self.cooled_until = now + self.cooldown
                self.rearm_needed = True
                self.above = 0
                return "STOP (max runtime)"
            if self.below >= 3 and elapsed >= self.min_run:
                self.active = False
                self.cooled_until = now + self.cooldown
                self.above = 0
                return "STOP"
        elif not self.rearm_needed and now >= self.cooled_until and self.above >= 2:
            self.active = True
            self.started = now
            self.above = 0
            self.below = 0
            return "CRY"
        return None


def score_wav(args, cfg):
    import soundfile as sf
    path = Path(args.wav)
    if not path.exists():
        raise FileNotFoundError(path)
    arr, sample_rate = sf.read(str(path), always_2d=True, dtype="float32")
    mono = as_mono(arr, args.channel)
    mono = resample_mono(mono, int(sample_rate), int(cfg["sample_rate"]))
    n = int(cfg["window_seconds"] * cfg["sample_rate"])
    step = int(args.step_seconds * cfg["sample_rate"])
    if len(mono) < n:
        print("Short audio: zero-padding to 2 seconds (interpret scores carefully).")
        mono = np.pad(mono, (0, n - len(mono)))
    if args.export_input:
        input_tensor = preprocess_8k(mono[:n], cfg, args)
        np.save(args.export_input, input_tensor)
        print(f"Saved model-input tensor: {args.export_input}  shape={input_tensor.shape}")
    clf = Classifier(cfg, args)
    print(f"File: {path} | original rate: {sample_rate} | windows: 2 s | threshold: {args.threshold:.2f}")
    for index, start in enumerate(range(0, len(mono) - n + 1, step), 1):
        p = clf.score(mono[start:start+n])
        print(f"Window {index:03d}  t={start / cfg['sample_rate']:6.2f}s   cry_score={p:6.3f}   "
              f"{'CRY' if p >= args.threshold else 'NON-CRY'}")


def read_exact(pipe, needed: int) -> bytes:
    parts = []
    remaining = needed
    while remaining:
        part = pipe.read(remaining)
        if not part:
            raise EOFError("Microphone audio stream ended. Check arecord error output and 'arecord -l'.")
        parts.append(part)
        remaining -= len(part)
    return b"".join(parts)


def score_live(args, cfg):
    source_rate = args.capture_rate
    channels = 2
    frames_per_step = int(round(args.step_seconds * source_rate))
    if (float(cfg["window_seconds"]) / args.step_seconds).is_integer() is False:
        raise ValueError("Choose --step-seconds dividing the 2-second window (for example 0.5 or 1.0).")
    chunks_per_window = int(round(float(cfg["window_seconds"]) / args.step_seconds))
    bytes_per_step = frames_per_step * channels * 4
    cmd = ["arecord", "-q", "-D", args.capture_device, "-f", "S32_LE",
           "-r", str(source_rate), "-c", str(channels), "-t", "raw"]
    print("Audio capture:", " ".join(cmd), flush=True)
    print("Safety default: no physical actions unless explicitly enabled.", flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0)
    actions = None
    logfh = None
    try:
        clf = Classifier(cfg, args)
        actions = Actions(args)
        machine = StateMachine(args.threshold, args.min_run, args.max_run, args.cooldown)
        if args.csv_log:
            logfh = open(args.csv_log, "a", encoding="utf-8", newline="")
            writer = csv.writer(logfh)
            if logfh.tell() == 0:
                writer.writerow(["utc_time", "cry_score", "decision", "rms", "event"])
        window = deque(maxlen=chunks_per_window)
        started_at = time.monotonic()
        while True:
            if args.duration and time.monotonic() - started_at >= args.duration:
                break
            raw = read_exact(proc.stdout, bytes_per_step)
            pcm = np.frombuffer(raw, dtype="<i4").reshape((-1, channels))
            audio = as_mono(pcm.astype(np.float32) / 2147483648.0, args.channel)
            audio = resample_mono(audio, source_rate, cfg["sample_rate"])
            window.append(audio)
            if len(window) < chunks_per_window:
                continue
            combined = np.concatenate(tuple(window))
            expected = int(cfg["sample_rate"] * cfg["window_seconds"])
            if combined.size < expected:
                combined = np.pad(combined, (0, expected - combined.size))
            combined = combined[-expected:]
            rms = float(np.sqrt(np.mean(combined.astype(np.float64)**2)))
            score = clf.score(combined)
            now = time.monotonic()
            event = machine.update(score, now)
            label = "CRY" if score >= args.threshold else "NON-CRY"
            if event == "CRY":
                actions.start()
            elif event and event.startswith("STOP"):
                actions.stop()
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            print(f"{stamp}  cry_score={score:.3f}  {label:7}  rms={rms:.5f}"
                  f"  soothing={'ON' if machine.active else 'OFF'}" + (f"  {event}" if event else ""), flush=True)
            if logfh:
                writer.writerow([stamp, f"{score:.5f}", label, f"{rms:.7f}", event or ""])
                logfh.flush()
    finally:
        if actions is not None:
            actions.close()
        if logfh:
            logfh.close()
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def parse_args():
    parser = argparse.ArgumentParser(description="TwirlyTunes 2s Mel TFLite cry detector (safe dry-run by default)")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--self-test", action="store_true", help="Test Mel preprocessing without loading model")
    src.add_argument("--inspect", action="store_true", help="Print model I/O shape and dtype")
    src.add_argument("--wav", help="Predict every 2s window from a WAV file, no physical actions")
    src.add_argument("--live", action="store_true", help="Capture from INMP441 using arecord")
    parser.add_argument("--channel", choices=["left", "right", "auto"], default="left")
    parser.add_argument("--capture-device", default="plughw:CARD=INMP441,DEV=0")
    parser.add_argument("--capture-rate", type=int, default=44100)
    parser.add_argument("--step-seconds", type=float, default=1.0)
    parser.add_argument("--duration", type=float, default=0, help="Stop after N seconds (0 = until Ctrl+C)")
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--min-run", type=float, default=6.0)
    parser.add_argument("--max-run", type=float, default=30.0)
    parser.add_argument("--cooldown", type=float, default=5.0)
    parser.add_argument("--csv-log", help="Optional CSV score log path")
    parser.add_argument("--export-input", help="With --wav, save the first preprocessed model-input tensor as .npy")
    parser.add_argument("--enable-motor", action="store_true", help="REAL motor control (only after ESP32/L298N is tested)")
    parser.add_argument("--serial-port", default="", help="ESP32 port, typically /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--enable-speaker", action="store_true", help="REAL playback (only after I2S duplex resolved)")
    parser.add_argument("--lullaby", help="Existing WAV file to play")
    parser.add_argument("--speaker-device", help="Playback ALSA device (verify with aplay -l)")
    # Training details not supplied in deployment JSON: explicit assumptions.
    parser.add_argument("--db-reference", choices=["max", "one"], default="max")
    parser.add_argument("--top-db", type=float, default=80.0)
    parser.add_argument("--resize", choices=["bilinear", "nearest", "bicubic"], default="bilinear")
    parser.add_argument("--htk-mel", action="store_true", help="Use HTK mel scale instead of Slaney")
    parser.add_argument("--no-mel-norm", action="store_true", help="Disable Slaney area normalization")
    parser.add_argument("--no-center-stft", action="store_false", dest="center_stft")
    parser.set_defaults(center_stft=True)
    a = parser.parse_args()
    if a.step_seconds <= 0 or a.step_seconds > 2:
        parser.error("--step-seconds must be in (0, 2]")
    if a.duration < 0:
        parser.error("--duration must be >= 0")
    if a.max_run <= 0 or a.min_run <= 0 or a.max_run < a.min_run:
        parser.error("Require 0 < min-run <= max-run")
    if (a.enable_motor or a.enable_speaker) and not a.live:
        parser.error("Physical outputs can be enabled only in --live mode")
    if a.export_input and not a.wav:
        parser.error("--export-input only works with --wav")
    return a


def main():
    args = parse_args()
    cfg = load_config()
    args.threshold = float(cfg["threshold"] if args.threshold is None else args.threshold)
    if not (0.0 < args.threshold < 1.0):
        raise ValueError("Threshold must be strictly between 0 and 1")
    if args.self_test:
        t = np.arange(int(cfg["sample_rate"] * cfg["window_seconds"])) / cfg["sample_rate"]
        signal = (0.15 * np.sin(2 * np.pi * 330 * t)).astype(np.float32)
        batch = preprocess_8k(signal, cfg, args)
        print(f"PASS: preprocessing input -> {batch.shape}, {batch.dtype}, "
              f"range=({batch.min():.4f},{batch.max():.4f})")
        print("NOTE: unknown training defaults must still be verified before claims of parity.")
    elif args.inspect:
        clf = Classifier(cfg, args)
        print("Model:", MODEL)
        print("Input:", clf.input["shape"], clf.input["dtype"], clf.input["name"])
        print("Output:", clf.output["shape"], clf.output["dtype"], clf.output["name"])
    elif args.wav:
        score_wav(args, cfg)
    elif args.live:
        score_live(args, cfg)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
