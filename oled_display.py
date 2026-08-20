"""
oled_display.py
----------------
Thin wrapper around ssd1306.py that renders a simple local status
screen: WiFi state/IP, backend registration state, and a compact
on/off summary of the actuators (pump/fan/light/water_pump/valve).
Purely informational - the OLED never drives control logic, it only
reflects it.

If no OLED is physically connected, init() safely disables all draw
calls instead of raising, so the rest of the firmware runs unaffected.

The new config.py doesn't define OLED_WIDTH/HEIGHT/I2C pins/address,
so sane defaults (128x64, SDA=21/SCL=22, addr 0x3C) are used unless
you add those names to config.py.
"""

from machine import I2C, Pin

import config

WIDTH = getattr(config, "OLED_WIDTH", 128)
HEIGHT = getattr(config, "OLED_HEIGHT", 64)
I2C_SDA_PIN = getattr(config, "I2C_SDA_PIN", 21)
I2C_SCL_PIN = getattr(config, "I2C_SCL_PIN", 22)
I2C_ADDR = getattr(config, "OLED_I2C_ADDR", 0x3C)


class OledDisplay:
    def __init__(self):
        self.enabled = False
        self.oled = None
        try:
            i2c = I2C(0, scl=Pin(I2C_SCL_PIN), sda=Pin(I2C_SDA_PIN), freq=400000)
            from ssd1306 import SSD1306_I2C
            self.oled = SSD1306_I2C(WIDTH, HEIGHT, i2c, addr=I2C_ADDR)
            self.enabled = True
        except Exception as e:
            print("[oled] display not available, continuing without it:", e)

    def show_status(self, wifi_connected, ip_address, registered, actuator_state):
        if not self.enabled:
            return

        try:
            self.oled.fill(0)

            self.oled.text("Hydro Controller", 0, 0)
            self.oled.hline(0, 10, WIDTH, 1)

            wifi_line = "WiFi: " + (ip_address if wifi_connected else "disconnected")
            self.oled.text(wifi_line, 0, 16)

            backend_line = "Backend: " + ("OK" if registered else "...")
            self.oled.text(backend_line, 0, 26)

            on_count = sum(1 for v in actuator_state.values() if v)
            self.oled.text("Actuators: %d/%d ON" % (on_count, len(actuator_state)), 0, 40)

            mode_line = "Mode: " + ("AUTO" if config.AUTO_MODE["enabled"] else "BACKEND")
            self.oled.text(mode_line, 0, 50)

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