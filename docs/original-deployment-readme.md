# TwirlyTunes — complete Raspberry Pi 4 controller

**Version:** prototype integration v1.0 | **Hardware:** Raspberry Pi 4 + INMP441 I²S microphone + **3.5-mm AUX speaker** + ESP32 over USB + L298N motor driver.

This is a supervised engineering prototype. It is **not** an infant monitor or validated infant-care product. Do not mount the moving assembly over a cot or operate it unattended near an infant. Model predictions can be wrong.

## 1. What it does

1. Starts on the Pi (manually, or with a systemd service after you enable it).
2. Opens the I²S microphone using ALSA at **44,100 Hz, stereo, 32-bit**. Selects the **left** channel because INMP441 L/R is connected to ground.
3. Resamples to **8 kHz mono** and scores overlapping **2-second** recordings once every **1 second**, using the supplied MobileNetV3Small TFLite model.
4. Keeps the most recent **10** results; a score **>=0.80** counts as a positive cry. **6 or more positives in 10 full windows** starts a soothing cycle. Isolated non-cry scores are tolerated.
5. **Stops arecord entirely** during the soothing cycle (so the playing lullaby cannot be captured).
6. Sends `CRY\n` to the ESP32 every **0.5 s**, to refresh its independent auto-stop watchdog. Simultaneously plays a WAV on the **Pi 3.5-mm AUX jack** for **20 s**.
7. Sends `STOP\n`, stops `aplay`, and waits **10 s** with the mic still off.
8. Reopens the microphone and starts a **fresh empty** 10-window vote buffer. If real crying continues long enough, another cycle can start.

**The 10 predictions are overlapping, not independent.** This rule reduces some intermittent false triggers but can also miss cries or add ~11 seconds of initial detection delay. Adjust it only after testing on representative sounds.

## 2. Physical connections (do not rewire powered electronics)

### Microphone → Raspberry Pi 4

| INMP441 | Raspberry Pi GPIO/physical pin |
|---|---|
| VDD | 3.3 V / pin 1 |
| GND | GND / pin 6 |
| SCK | GPIO18 / pin 12 (I²S BCLK) |
| WS | GPIO19 / pin 35 (I²S LRCLK) |
| SD | GPIO20 / pin 38 (I²S data input) |
| L/R | GND (left channel) |

Keep the **working `dtoverlay=inmp441`** and `dtparam=audio=on` in `/boot/firmware/config.txt`. **Do not turn on `dtoverlay=max98357a`**: your current speaker uses the Pi's 3.5-mm jack, not a MAX98357A I²S amplifier.

### AUX speaker

Connect the powered speaker's AUX cable to the **Raspberry Pi 4 3.5-mm jack**. If the speaker needs a power adapter or USB power, provide it independently and set its volume low for initial tests.

Detected ALSA device: **Headphones**. Use `plughw:CARD=Headphones,DEV=0`, rather than hard-coding card `3`, as numerical ALSA card IDs may change between reboots.

### ESP32 → L298N

| ESP32 pin | L298N input | Purpose |
|---|---|---|
| GPIO26 | IN1 | Direction |
| GPIO27 | IN2 | Direction |
| GPIO25 | ENA | Motor enable/PWM (remove ENA jumper if GPIO drives ENA) |
| GND | GND | Common logic ground |

The **motor** connects to L298N **OUT1 / OUT2**. The **separately rated motor supply** connects to the L298N motor-power terminals; check its actual terminal labels and board ratings. **Never power the motor from Pi/ESP32 GPIO**. Connect ESP32 USB to the Pi for serial control. Keep an accessible motor power disconnect and avoid spinning an unsecured jhumar.

**Independent failsafe:** The ESP32 firmware must **stop the motor automatically** if no fresh `CRY` command arrives for ~1.5 seconds. Your previously tested short 1.5-second auto-stop sketch supports this; the optional `esp32_watchdog.ino` in this folder provides the same protocol with PWM. **Do not auto-run a motor with firmware that lacks an independent timeout.** A hardware pull-down on ENA and independent power disconnect are recommended for a fail-safe design.

## 3. Copy the ZIP from Windows

In **Windows PowerShell, not inside SSH**:

```powershell
scp "$env:USERPROFILE\Downloads\TwirlyTunes_Final.zip" babydev@baby-pi.local:/home/babydev/
ssh babydev@baby-pi.local
```

On the Pi:

```bash
cd ~
unzip -o TwirlyTunes_Final.zip
cd ~/TwirlyTunes_Final
chmod +x start.sh install_service.sh uninstall_service.sh
```

The `start.sh` launcher **reuses your existing working** `/home/babydev/TwirlyTunes_Pi_Deployment/.venv` if available. If not, create a fresh venv:

```bash
sudo apt install -y python3-venv python3-pip alsa-utils
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## 4. Verify all devices independently

```bash
arecord -l
aplay -l
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
ls -l /dev/serial/by-id/ 2>/dev/null
```

Expected ALSA names: **INMP441** in capture and **Headphones** in playback. Check `/dev/ttyUSB0` for the ESP32. If serial changes across reboots, use a stable `/dev/serial/by-id/...` path in `settings.json` when available. Do **not** keep Arduino Serial Monitor connected to the same ESP32 port while the Pi program uses it.

For initial safe volume (on your earlier Pi card it was 3; verify with `aplay -l` before using this card number):

```bash
amixer -c 3 sset PCM 65%
```

To test sound (low volume first):

```bash
speaker-test -D plughw:CARD=Headphones,DEV=0 -c 2 -t sine -f 440 -l 1
```

## 5. Test TwirlyTunes in order

Always work in `~/TwirlyTunes_Final`.

**A. Configuration, no motors/music:**

```bash
./start.sh --check
./start.sh --simulate
./start.sh --dry-run --seconds 90
```

`--dry-run` uses the live microphone and real model but **does not** activate motor or audio. When a pattern is detected, it simulates the 20-second soothing pause and 10-second cooldown.

**B. Test music and motor without cry detection:**

Secure the bare motor, keep the mobile detached, verify a separate motor supply and watchdog. Then:

```bash
./start.sh --test-outputs --seconds 4
```

Should play the included quiet **original** 24-second WAV through AUX and run the motor for only ~4 seconds. It then sends STOP.

**C. Test full integration manually:**

```bash
./start.sh --run --seconds 120
```

Play realistic recorded crying (or use appropriately sourced cry audio) near your mic, then ordinary talking/music. After **6+/10** positives, look for `CRY PATTERN DETECTED`, `SOOTHING START`, and `SOOTHING END`. The mic is stopped during music, and a fresh vote cycle starts after cooldown. Never deliberately distress a baby for testing. Press **Ctrl+C** to stop early.

**D. Keep the program running continuously in a manual terminal:**

```bash
./start.sh --run
```

Press **Ctrl+C** to stop motor and speaker, close microphone, and exit.

## 6. Enable automatic startup at boot (after C passes)

Run these commands **once** from `~/TwirlyTunes_Final`:

```bash
sudo ./install_service.sh
sudo systemctl status twirlytunes.service --no-pager
sudo journalctl -u twirlytunes.service -n 50 --no-pager
```

The installer creates `/etc/systemd/system/twirlytunes.service`, enables it, and starts it immediately. The process runs under **babydev**, not root, with supplementary `audio` and `dialout` groups. Network connectivity is **not needed** for detection once installed.

Now reboot for a real startup test:

```bash
sudo reboot
```

Reconnect and check:

```bash
sudo systemctl is-enabled twirlytunes.service
sudo systemctl is-active twirlytunes.service
sudo journalctl -u twirlytunes.service -n 50 --no-pager
```

The service starts when the Pi boots after power is supplied, as long as **all hardware and its own power supplies** are ready. Booting the Pi is not identical to safely switching a motor's separate supply; use an appropriate physical power design and supervised tests.

**Control the service:**

```bash
sudo systemctl stop twirlytunes.service       # stop now
sudo systemctl start twirlytunes.service      # restart
sudo systemctl disable --now twirlytunes.service # turn off boot start
sudo journalctl -u twirlytunes.service -f     # live events
```

To remove service registration entirely (does not delete your files):

```bash
sudo ./uninstall_service.sh
```

**Do not run `./start.sh --run` concurrently with the active service**: both would compete for ALSA capture and the ESP32 port. Stop the service first.

## 7. Adjust operation safely

Edit:

```bash
nano ~/TwirlyTunes_Final/settings.json
```

Important fields:

| Setting | Default | Meaning |
|---|---:|---|
| `cry_probability_threshold` | `0.80` | Probability required for each positive vote. |
| `vote_window` | `10` | Number of consecutive scored windows considered. |
| `minimum_cry_votes` | `6` | Require >50% positive among ten. |
| `step_seconds` | `1.0` | 2-second sound window advanced each second. |
| `soothing_seconds` | `20.0` | Play motor + AUX music for up to 20 seconds. |
| `cooldown_seconds` | `10.0` | No microphone recording after music for 10 seconds. |
| `esp32_serial_port` | `/dev/ttyUSB0` | Change if a different port is detected. |
| `motor_keepalive_seconds` | `0.50` | CRY sent every 0.5 sec while motor should run. |
| `aux_output_device` | `plughw:CARD=Headphones,DEV=0` | AUX speaker device. |
| `lullaby_file` | `lullaby.wav` | Replace with a gentle WAV of your choosing. |
| `minimum_rms` | `0.0` | Optional silence rejection (off until calibrated). |
| `csv_logging` | `false` | Log every prediction (can fill the SD card). |

If the service is running, restart after changing settings:

```bash
sudo systemctl restart twirlytunes.service
```

A model probability **is not a calibrated probability of a baby's needs**; it is a model score. Your reported held-out recall at 0.80 is **77.65%** before voting. A 6/10 vote can decrease sensitivity while reducing some isolated false alarms. Evaluate on new, separate recordings before making accuracy or timing claims.

## 8. Troubleshooting / important edge cases

| Observation | Check/action |
|---|---|
| `arecord` not found | `sudo apt install alsa-utils` |
| `Unknown PCM INMP441` | `arecord -l`, confirm the working `dtoverlay=inmp441`; reboot after changing overlays. |
| Speaker silent | `aplay -l`, powered AUX speaker, low-to-moderate volume, and `Headphones` card; check `dtparam=audio=on`. |
| ESP32 port missing | Move USB cable to Pi, use a **data cable**, inspect `ls /dev/ttyUSB* /dev/ttyACM*`; set port in JSON. |
| ESP32 won't acknowledge STOP | Close Arduino Serial Monitor; check baud 115200, `CRY`/`STOP` firmware, ESP32 reset on serial open. |
| Motor stops after 1.5 seconds | The Pi should refresh `CRY` every 0.5 seconds while playing; check serial and program logs. |
| Motor runs at wrong speed | Bench-test the driver and power first; adjust PWM firmware cautiously; secure motor before adding mobile. |
| Repeated trigger from music | Microphone is intentionally *stopped during playback*. If it retriggers after cooldown, extend cooldown or test room acoustics/threshold. |
| Real cries missed | Training recall was not perfect; fine-tune votes/threshold based on held-out real recordings. |
| Service inactive after reboot | `sudo journalctl -u twirlytunes.service -n 100 --no-pager`; check `sound.target`, ALSA, ESP32 permissions. |
| Hardware loses power mid-action | ESP32 watchdog must stop the motor within ~1.5 sec without CRY. Physical stop/disconnect still required. |
| Inference is slower than 1 second | Logs warn `Inference exceeds hop`; use faster inference/model, consider a longer step or optimized streaming. |

## 9. Known limitation: training preprocessing equivalence

The supplied `deployment_config.json` confirms 8 kHz mono, 2-second windows, Mel 64, FFT 512, hop 128, fmin 20, fmax 4000, min-max normalization, (1,224,224,3) float32, and 0.80 threshold. But it **does not specify** exactly whether the training notebook used HTK/Slaney Mel filters, dB reference `np.max` vs 1.0, power-to-dB cap, 224x224 interpolation, or grayscale/color mapping. The packaged `twirly.py` has explicit initial assumptions and preserves the model's internal `Rescaling(2,-1)` layer by **not** applying rescaling again. You should compare a reference training-input tensor (`.npy`) against the Pi's exported tensor before trusting the model's scores as equivalent to its training/test accuracy.

## 10. Project files

```
TwirlyTunes_Final/
    main.py                  # primary integrated Pi controller
    twirly.py                # earlier tested TFLite audio preprocessing/inference
    settings.json            # parameters and hardware paths
    deployment_config.json   # model preprocessing information
    BabySoother_FineTuned_MobileNetV3Small.tflite
    lullaby.wav              # original soft sample; replace with your own WAV
    generate_lullaby.py      # regenerates original WAV
    esp32_watchdog.ino       # optional independent motor timeout firmware
    start.sh                 # reuses existing working Pi Python environment
    install_service.sh       # enables auto-start AFTER supervised tests
    uninstall_service.sh     # removes auto-start
    test_logic.py            # offline voting and safety tests
    requirements.txt         # only needed if creating a fresh venv
    README.md
```

**Safety:** Prototype only, bench test supervised. Cry detection is not a medical diagnosis and may fail. Always supervise an infant personally. No validated sound pressure, electrical rating, battery life, mounting design, or motor safety assessment is supplied.
