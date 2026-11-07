# TwirlyTunes — AI-Based Smart Baby Soothing System

**Your Little Soothing Companion**

TwirlyTunes is a supervised academic prototype that uses a fine-tuned MobileNetV3-Small TensorFlow Lite classifier on a Raspberry Pi 4 to detect infant-cry audio. Confirmed crying triggers a timed lullaby and an ESP32-controlled motor for gentle jhoomar movement. **It is not a certified infant monitor or a replacement for adult supervision.**

## Project at a glance

- **Authors:** Chaitanyaa Dave, Kaushiki Choubey, Vedanti Dokare
- **Institution:** K.J. Somaiya School of Engineering, Somaiya Vidyavihar University, Mumbai, India
- **Research paper:** [`docs/AI-Based Smart Baby Soothing System.pdf`](docs/AI-Based%20Smart%20Baby%20Soothing%20System.pdf)
- **DOI:** https://doi.org/10.5281/zenodo.24016070

## Architecture

`INMP441 microphone → Raspberry Pi 4 (log-Mel + TFLite) → majority voting → lullaby (AUX speaker) + ESP32 (USB serial) → L298N → motor`

The configuration specifies 2-second audio windows with a 1-second step, a 0.80 score threshold, and at least 6 positive predictions among the most recent 10 windows. The soothing cycle lasts approximately 20 seconds.

## Reported model evaluation

| Metric | Held-out test result |
|---|---:|
| Test samples | 1,479 |
| Accuracy | 95.06% |
| Precision | 94.42% |
| Recall | 76.89% |
| F1-score | 84.76% |
| ROC-AUC | 0.9802 |

These are **audio-window classification results**, not measured continuous real-world reliability.

## Repository layout

```text
TwirlyTunes/
├── src/          # Raspberry Pi controller, classifier, lullaby generator
├── model/        # TFLite model and deployment metadata
├── config/       # Runtime settings
├── audio/        # Original test lullaby WAV
├── firmware/     # ESP32 Arduino sketch
├── scripts/      # Linux startup and systemd service scripts
├── tests/        # Offline Python logic tests
├── docs/         # Research paper and original deployment notes
├── requirements.txt
└── README.md
```

## Raspberry Pi setup (64-bit Raspberry Pi OS)

From the **repository root**:

```bash
sudo apt update
sudo apt install -y python3-venv alsa-utils
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python src/main.py --check
.venv/bin/python src/main.py --simulate
```

Before attempting live operation, check the ALSA microphone/output names, USB serial port, power wiring and physical clearances. The firmware is in `firmware/esp32_watchdog/esp32_watchdog.ino` (Arduino-ESP32 core 3.x). The sketch uses GPIO 26/27/25 for IN1/IN2/ENA; verify against your actual circuit.

### Supervised validation and operation

```bash
.venv/bin/python src/main.py --dry-run --seconds 60
# Only with verified hardware, supervision and safe physical setup:
.venv/bin/python src/main.py --test-outputs
.venv/bin/python src/main.py --run
```

To install automatic startup **only after** all supervised checks pass:

```bash
chmod +x scripts/*.sh
sudo ./scripts/install_service.sh
# Disable: sudo ./scripts/uninstall_service.sh
```

See [`docs/original-deployment-readme.md`](docs/original-deployment-readme.md) for the detailed original operating notes.

## Safety and limitations

This prototype must not be used as a standalone baby monitor or left running unattended near an infant. Do not mount moving components over a cot. False negatives occur in the evaluation set, and the model's real-world response time and physical reliability are not fully validated.

## Citation and licensing

[1]C. Dave, K. Choubey, and V. Dokare, “AI-Based Smart Baby Soothing System”, Nov. 06, 2026, Zenodo. doi: 10.5281/zenodo.24016070.
