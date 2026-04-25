"""
engine/receiver.py
------------------
Reads from ESP32 DevKit over USB serial.

Your DevKit sends:
  1. Sensor JSON (every 500ms, newline terminated):
     {"type":"sensor","motion":1,"smoke_ppm":87.3,"ldr":2940}

  2. Frame header JSON (when motion/smoke triggers capture, newline terminated):
     {"type":"frame","size":22016}
     <raw 22016 bytes of JPEG — NOT newline terminated>

  3. Error/boot JSON (logged, ignored):
     {"type":"boot","msg":"..."}
     {"type":"error","msg":"..."}

The receiver runs in a daemon thread.
On sensor packets  → calls state.update_from_serial(packet)
On frame packets   → calls on_frame(jpeg_bytes)
"""

import json
import time
import threading
import logging
import serial
import serial.tools.list_ports

logger = logging.getLogger(__name__)


def list_ports():
    return [p.device for p in serial.tools.list_ports.comports()]


class SerialReceiver:
    def __init__(self, state, on_frame):
        from engine.config_manager import cfg
        self._cfg      = cfg
        self._state    = state
        self._on_frame = on_frame
        self._running  = False
        self._thread   = None
        self._ser      = None

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop, daemon=True, name="serial-rx"
        )
        self._thread.start()

    def stop(self):
        self._running = False
        try:
            if self._ser and self._ser.is_open:
                self._ser.close()
        except Exception:
            pass

    # ── Connection loop ────────────────────────────────────────
    def _loop(self):
        while self._running:
            sc   = self._cfg.get("serial", {})
            port = sc.get("port", "COM6")
            baud = int(sc.get("baud_rate", 115200))

            available = list_ports()
            logger.info(f"[serial] Available ports: {available}")
            logger.info(f"[serial] Listening on {port} @ {baud} baud")

            # ── Check if port is in use before connecting ──────
            # Common cause: Arduino Serial Monitor still open.
            try:
                test = serial.Serial(port, baud, timeout=1)
                test.close()
            except serial.SerialException as e:
                if "Access is denied" in str(e) or "PermissionError" in str(e):
                    logger.warning(
                        f"[serial] {port} is busy — "
                        "CLOSE Arduino Serial Monitor then retry. Retrying in 5s..."
                    )
                else:
                    logger.warning(f"[serial] Cannot open {port}: {e} — retry in 5s")
                time.sleep(5)
                continue

            try:
                self._ser = serial.Serial(port, baud, timeout=5)
                logger.info(f"[serial] Connected to {port}")
                self._read_loop()
            except serial.SerialException as e:
                logger.warning(f"[serial] Connection dropped: {e} — retry in 3s")
                time.sleep(3)
            finally:
                try:
                    if self._ser and self._ser.is_open:
                        self._ser.close()
                except Exception:
                    pass

    # ── Read loop ──────────────────────────────────────────────
    def _read_loop(self):
        while self._running and self._ser and self._ser.is_open:
            try:
                # Read one line (terminated by \n from Serial.println())
                raw = self._ser.readline()
                if not raw:
                    continue

                line = raw.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue

                # Try to parse as JSON
                try:
                    packet = json.loads(line)
                except json.JSONDecodeError:
                    # Non-JSON: Arduino boot text, WiFi messages etc.
                    logger.debug(f"[serial] raw: {line[:100]}")
                    continue

                ptype = packet.get("type", "")

                if ptype == "sensor":
                    self._handle_sensor(packet)

                elif ptype == "frame":
                    size = int(packet.get("size", 0))
                    if size > 0:
                        self._handle_frame(size)
                    else:
                        logger.warning("[serial] frame size=0 — CAM capture failed")

                elif ptype == "error":
                    logger.warning(f"[serial] DevKit error: {packet.get('msg','')}")

                elif ptype == "boot":
                    logger.info(f"[serial] DevKit boot: {packet.get('msg','')}")

            except serial.SerialException as e:
                logger.error(f"[serial] Serial read error: {e}")
                break
            except Exception as e:
                logger.error(f"[serial] Unexpected: {e}")
                break

    def _handle_sensor(self, packet: dict):
        self._state.update_from_serial(packet)
        logger.debug(
            f"[serial] sensor motion={packet.get('motion')} "
            f"smoke={packet.get('smoke_ppm','?')} "
            f"ldr={packet.get('ldr','?')}"
        )

    def _handle_frame(self, size: int):
        logger.info(f"[serial] Receiving frame: {size} bytes")
        jpeg = self._read_exact(size)
        if jpeg is None:
            logger.warning("[serial] Frame incomplete — discarded")
            return
        logger.info(f"[serial] Frame complete: {len(jpeg)} bytes")
        try:
            self._on_frame(jpeg)
        except Exception as e:
            logger.error(f"[serial] on_frame error: {e}")

    def _read_exact(self, n: int):
        buf      = b""
        deadline = time.time() + 15.0   # 15s for large frames at 115200

        while len(buf) < n:
            if time.time() > deadline:
                logger.warning(
                    f"[serial] Timeout: got {len(buf)}/{n} bytes"
                )
                return None
            if not self._ser or not self._ser.is_open:
                return None
            chunk = self._ser.read(min(4096, n - len(buf)))
            if chunk:
                buf += chunk

        return buf