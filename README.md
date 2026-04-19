# SneakPeek — Smart CCTV AI Engine

Local AI engine for the SneakPeek IoT security system.
Runs on your laptop, connected to an ESP32 via USB serial.

## Project structure

```
sneakpeek/
├── main.py                  # Start monitoring
├── config.json              # Your threat profile + settings
├── setup/
│   └── enroll.py            # Run once to register known faces
├── engine/
│   ├── sensor_context.py    # Live sensor state (PIR, MQ-2, LDR)
│   ├── cooldown.py          # Per-threat cooldown timers
│   ├── receiver.py          # USB serial reader
│   ├── detector.py          # YOLOv8 person detection
│   ├── recognizer.py        # InsightFace face recognition
│   ├── pose.py              # MediaPipe pose + aggression
│   └── scorer.py            # Final threat scoring
├── alert/
│   └── aws_sender.py        # AWS alert sender (stub for now)
├── data/
│   └── known_faces/         # Add person folders here
└── tests/
    └── simulate.py          # Test without ESP32 hardware
```

## Sensor roles

| Sensor | Role | Acts alone? |
|--------|------|-------------|
| PIR motion | Presence gate — decides if a real person is there | Yes — filters short blips |
| MQ-2 smoke | Fire/gas alert — fires immediately | Yes — bypasses AI entirely |
| LDR light | Night multiplier — shifts threat weights | Always running, never events |
| Camera | Identity + behaviour — who and what | Only after motion confirmed |

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Enroll known faces
Add photos to `data/known_faces/` in named subfolders:
```
data/known_faces/
    Alice/
        alice_front.jpg
        alice_side.jpg
    Bob/
        bob.jpg
```
Then run:
```bash
python setup/enroll.py
```

### 3. Configure
Edit `config.json`:
- Set `serial.port` to your ESP32 COM port (e.g. `"COM3"`)
- Enable/disable threats in `threat_profile`
- Set `aws.alert_email` to your email address
- Adjust `thresholds` and `cooldown_seconds` to your preference

### 4. Test without hardware
```bash
python tests/simulate.py --image path/to/test.jpg
python tests/simulate.py --image test.jpg --night
python tests/simulate.py --image test.jpg --smoke
```

### 5. Run
```bash
python main.py
python main.py --debug    # verbose logging
```

## How the pipeline works

```
ESP32 serial packet
       │
       ▼
SensorContext.update()        ← PIR / smoke / LDR values live here
       │
       ▼
on_frame() callback
  ├── Gate 1: sustained motion? (PIR's own decision — 2s minimum)
  ├── Gate 2: smoke? → immediate alert, skip AI
  ├── Gate 3: all threats on cooldown? → skip
  ├── YOLOv8  → people in frame?
  ├── InsightFace → known or unknown?
  ├── MediaPipe → contact? aggression?
  ├── Scorer  → threat score + LDR night multiplier
  └── AWS sender → alert email (stub until Phase 2)
```

## Threat scoring weights (configurable in config.json)

```
score = (unknown_ratio × 0.35)
      + (crowd_factor  × 0.20)
      + (contact_iou   × 0.25)
      + (aggression    × 0.20)

if night:  score × 1.80
```

Default alert threshold: 0.50

## Next steps (Phase 2 — ESP32 + AWS)

- Flash ESP32 firmware (serial protocol + sensor code)
- Wire PIR, MQ-2, LDR to ESP32 GPIO pins
- Replace `alert/aws_sender.py` stub with real boto3 calls
- Deploy Lambda + API Gateway + S3 + SES on AWS
