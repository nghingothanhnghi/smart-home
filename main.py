"""
main.py
-------
Entry point. Responsibilities ONLY:
  1. Bring up WiFi.
  2. Build the actuator layer (relay/mosfet channels from
     config.TYPE_TO_GPIO).
  3. Log in and register the device + actuators with the FastAPI
     hydro backend.
  4. Run the main loop: keep WiFi alive, poll/execute commands, push
     sensor data + actuator state, run the relay safety sweep, and
     refresh the OLED.

All actual logic lives in the other modules - this file is just the
coordinator, so it stays readable as the project grows.
"""

import time
import gc
import machine

import config
import auth
from wifi import WiFiManager
from actuators import ActuatorManager
from device import Device
from control import ControlLoop
from oled_display import OledDisplay

DEVICE_LABEL = getattr(config, "DEVICE_MODEL", "esp32-hydro-controller")
FIRMWARE_VERSION = getattr(config, "FIRMWARE_VERSION", "unknown")


def boot():
    print("=" * 40)
    print("Booting", DEVICE_LABEL, "fw", FIRMWARE_VERSION)
    print("Device code:", config.DEVICE_CODE)
    print("=" * 40)

    oled = OledDisplay()
    oled.show_message("Booting...", "connecting wifi")

    wifi = WiFiManager()
    actuators = ActuatorManager()  # also forces all relays/mosfets OFF at init (safe state)
    device = Device(actuators)
    control = ControlLoop(device, actuators)

    wifi.connect()
    if wifi.is_connected():
        oled.show_message("WiFi OK", wifi.ip())
        auth.login()
        device.register(wifi.ip())
    else:
        oled.show_message("WiFi FAILED", "retrying in loop")

    return wifi, actuators, device, control, oled


def main_loop(wifi, actuators, device, control, oled):
    last_display_refresh = 0
    display_refresh_interval = 2  # seconds

    while True:
        try:
            # 1. Keep the network alive; re-login + re-register if we
            #    just recovered (a fresh connection likely means a
            #    fresh boot's-worth of state was lost, so don't trust
            #    a stale token/registration).
            was_connected = wifi.is_connected()
            wifi.ensure_connected()
            if wifi.is_connected() and not was_connected:
                print("[main] wifi recovered, re-authenticating + re-registering")
                auth.login()
                device.register(wifi.ip())

            # 2. Talk to the backend (poll/execute commands, push
            #    sensor data + actuator state).
            if wifi.is_connected():
                control.tick()

            # 3. Local maintenance: relay/mosfet safety sweep (force-off
            #    anything that's exceeded its max on-time).
            actuators.tick()

            # 4. Optional local automation - only runs while
            #    config.AUTO_MODE["enabled"] is True, and control.py
            #    already stops pulling backend commands in that mode
            #    so the two never fight over an actuator.
            if config.AUTO_MODE["enabled"]:
                # Local automation rules would go here, e.g. reading
                # sensors.read_all() and driving actuators directly.
                pass

            # 5. Refresh local display.
            now = time.time()
            if now - last_display_refresh >= display_refresh_interval:
                last_display_refresh = now
                oled.show_status(
                    wifi_connected=wifi.is_connected(),
                    ip_address=wifi.ip(),
                    registered=device.registered,
                    actuator_state=actuators.state_snapshot(),
                )

            time.sleep(0.2)
            gc.collect()

        except Exception as e:
            # Never let an unhandled exception kill the control loop of
            # a device driving pumps/valves/lights - log it, force
            # actuators to a safe state, and keep going.
            print("[main] loop error:", e)
            try:
                actuators.all_off()
            except Exception:
                pass
            time.sleep(1)


def run():
    wifi, actuators, device, control, oled = boot()
    try:
        main_loop(wifi, actuators, device, control, oled)
    except KeyboardInterrupt:
        print("[main] stopped by user, switching all actuators off")
        actuators.all_off()
    except Exception as e:
        # Last-resort safety net: force everything off and reboot the
        # board rather than leaving it in an unknown state.
        print("[main] fatal error, forcing safe state and resetting:", e)
        try:
            actuators.all_off()
        except Exception:
            pass
        time.sleep(2)
        machine.reset()


run()
