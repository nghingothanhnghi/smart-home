"""
oled_display.py
----------------
Thin wrapper around ssd1306.py that renders a simple local status
screen: WiFi state/IP, backend registration state, and a compact
on/off summary of the 6 lights + door. Purely informational - the
OLED never drives control logic, it only reflects it.

If no OLED is physically connected, init() safely disables all draw
calls instead of raising, so the rest of the firmware runs unaffected.
"""

from machine import I2C, Pin

import config


class OledDisplay:
    def __init__(self):
        self.enabled = False
        self.oled = None
        try:
            i2c = I2C(0, scl=Pin(config.I2C_SCL_PIN), sda=Pin(config.I2C_SDA_PIN), freq=400000)
            from ssd1306 import SSD1306_I2C
            self.oled = SSD1306_I2C(
                config.OLED_WIDTH, config.OLED_HEIGHT, i2c, addr=config.OLED_I2C_ADDR
            )
            self.enabled = True
        except Exception as e:
            print("[oled] display not available, continuing without it:", e)

    def show_status(self, wifi_connected, ip_address, registered, actuator_state):
        if not self.enabled:
            return

        try:
            self.oled.fill(0)

            self.oled.text("IoT Relay Ctrl", 0, 0)
            self.oled.hline(0, 10, config.OLED_WIDTH, 1)

            wifi_line = "WiFi: " + (ip_address if wifi_connected else "disconnected")
            self.oled.text(wifi_line, 0, 16)

            backend_line = "Backend: " + ("OK" if registered else "...")
            self.oled.text(backend_line, 0, 26)

            lights = actuator_state.get("lights", {})
            on_count = sum(1 for v in lights.values() if v)
            self.oled.text("Lights: %d/%d ON" % (on_count, len(lights)), 0, 40)

            door_state = "OPEN" if actuator_state.get("door_open") else (
                "CLOSE" if actuator_state.get("door_close") else "IDLE"
            )
            self.oled.text("Door: " + door_state, 0, 50)

            self.oled.show()
        except Exception as e:
            print("[oled] draw failed:", e)

    def show_message(self, line1, line2=""):
        if not self.enabled:
            return
        try:
            self.oled.fill(0)
            self.oled.text(line1, 0, 20)
            if line2:
                self.oled.text(line2, 0, 32)
            self.oled.show()
        except Exception as e:
            print("[oled] draw failed:", e)
