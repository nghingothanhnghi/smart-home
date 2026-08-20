"""
main.py
-------
Entry point. Responsibilities ONLY:
  1. Bring up WiFi.
  2. Build the actuator layer (relays for 6 lights + sliding door).
  3. Register the device with the FastAPI backend.
  4. Run the main loop: keep WiFi alive, poll/execute commands,
     push state, run the relay safety sweep, and refresh the OLED.

All actual logic lives in the other modules - this file is just the
coordinator, so it stays readable as the project grows.
"""

import time
import machine

import config
from wifi import WiFiManager
from actuators import ActuatorManager
from device import Device
from control import ControlLoop
from oled_display import OledDisplay

try:
    import sensors
except Exception:
    sensors = None


def boot():
    print("=" * 40)
    print("Booting", config.DEVICE_MODEL, "fw", config.FIRMWARE_VERSION)
    print("=" * 40)

    oled = OledDisplay()
    oled.show_message("Booting...", "connecting wifi")

    wifi = WiFiManager()
    actuators = ActuatorManager()  # also forces all relays OFF at init (safe state)
    device = Device(actuators)
    control = ControlLoop(device, actuators)

    wifi.connect()
    if wifi.is_connected():
        oled.show_message("WiFi OK", wifi.ip())
        device.register(wifi.ip())
    else:
        oled.show_message("WiFi FAILED", "retrying in loop")

    return wifi, actuators, device, control, oled


def main_loop(wifi, actuators, device, control, oled):
    last_display_refresh = 0
    display_refresh_interval = 2  # seconds

    while True:
        try:
            # 1. Keep the network alive; re-register if we just recovered.
            was_connected = wifi.is_connected()
            wifi.ensure_connected()
            if wifi.is_connected() and not was_connected:
                print("[main] wifi recovered, re-registering device")
                device.register(wifi.ip())

            # 2. Talk to the backend (poll/execute/ack/state/heartbeat).
            if wifi.is_connected():
                control.tick()

            # 3. Local maintenance: door pulse auto-stop + relay safety sweep.
            actuators.tick()

            # 4. Optional local automation, only if explicitly enabled.
            if config.ENABLE_LOCAL_AUTOMATION and sensors is not None:
                readings = sensors.read_all()
                # Local automation rules would go here, operating only on
                # actuators the backend hasn't already claimed control of.
                _ = readings

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

        except Exception as e:
            # Never let an unhandled exception kill the control loop of a
            # device driving 220V loads - log it, force relays to a safe
            # state, and keep going.
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
        print("[main] stopped by user, switching all relays off")
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
