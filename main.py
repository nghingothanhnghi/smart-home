"""
main.py
-------
Entry point (uasyncio version). Responsibilities ONLY:
  1. Build hardware layers (actuators start OFF) + load cached schedule.
  2. Start the async tasks:
       - scheduler.run        : executes the cached schedule when offline
       - scheduler.wifi_task  : non-blocking WiFi reconnect w/ backoff
       - scheduler.probe_task : non-blocking "is the backend reachable?"
       - scheduler.ntp_task   : clock sync (needed for the schedule)
       - io_loop              : backend polling (only when reachable),
                                relay safety sweep, OLED
  3. Any unhandled error forces actuators off (same policy as before).
"""

import time
import gc
import machine
import uasyncio as asyncio

import config
import auth
from wifi import WiFiManager
from actuators import ActuatorManager
from device import Device
from control import ControlLoop
from oled_display import OledDisplay
from scheduler import Scheduler
import flow_sensor

DEVICE_LABEL = getattr(config, "DEVICE_MODEL", "esp32-hydro-controller")
FIRMWARE_VERSION = getattr(config, "FIRMWARE_VERSION", "unknown")
WDT_TIMEOUT_MS = getattr(config, "WDT_TIMEOUT_MS", None)   # None = disabled (dev)


def boot():
    print("=" * 40)
    print("Booting", DEVICE_LABEL, "fw", FIRMWARE_VERSION)
    print("Device code:", config.DEVICE_CODE)
    print("=" * 40)

    gc.collect()
    gc.threshold(gc.mem_free() // 4 + gc.mem_alloc())   # collect before heap fragments

    oled = OledDisplay()
    oled.show_message("Booting...", "offline-capable")

    wifi = WiFiManager()
    actuators = ActuatorManager()          # forces all relays/mosfets OFF (safe state)
    try:
        flow_sensor.init()
    except Exception as e:
        print("[main] flow_sensor init failed, continuing without flow sensing:", e)

    device = Device(actuators)
    control = ControlLoop(device, actuators)
    sched = Scheduler(actuators)           # loads schedule.json from flash
    control.on_status = sched.on_status    # cache schedule on every good poll
    return wifi, actuators, device, control, oled, sched


async def io_loop(wifi, actuators, device, control, oled, sched):
    last_display = time.ticks_ms()
    last_reg_try = time.ticks_add(time.ticks_ms(), -60000)
    was_connected = False
    need_register = True

    while True:
        try:
            connected = wifi.is_connected()
            if connected and not was_connected:
                need_register = True       # fresh link: re-auth + re-register
            was_connected = connected

            # Backend work happens ONLY if the async probe says it's
            # reachable, so blocking urequests calls are rare + short
            # while the backend is down.
            if connected and sched.reachable:
                if need_register or not device.registered:
                    if time.ticks_diff(time.ticks_ms(), last_reg_try) > config.SEND_INTERVAL * 1000:
                        last_reg_try = time.ticks_ms()
                        auth.login()
                        device.register(wifi.ip())
                        need_register = not device.registered
                if not need_register:
                    control.tick()

            tripped = actuators.tick()     # safety sweep + door pulses
            if tripped:
                sched.latch(tripped)       # don't instantly re-energize

            if time.ticks_diff(time.ticks_ms(), last_display) >= 2000:
                last_display = time.ticks_ms()
                oled.show_status(
                    wifi_connected=connected,
                    ip_address=wifi.ip(),
                    registered=device.registered,
                    actuator_state=actuators.state_snapshot(),
                )

            gc.collect()
            await asyncio.sleep_ms(200)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[main] loop error:", e)
            try:
                actuators.all_off()
            except Exception:
                pass
            await asyncio.sleep(1)


async def supervise(name, make_coro, actuators):
    """Restart a task if it crashes, forcing actuators to a safe state first."""
    while True:
        try:
            await make_coro()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[main] task '%s' crashed: %s" % (name, e))
            try:
                actuators.all_off()
            except Exception:
                pass
            await asyncio.sleep(1)


async def main_async(wifi, actuators, device, control, oled, sched):
    wdt = machine.WDT(timeout=WDT_TIMEOUT_MS) if WDT_TIMEOUT_MS else None
    tasks = [
        asyncio.create_task(supervise("scheduler", lambda: sched.run(wdt), actuators)),
        asyncio.create_task(supervise("wifi", lambda: sched.wifi_task(wifi), actuators)),
        asyncio.create_task(supervise("probe", lambda: sched.probe_task(wifi), actuators)),
        asyncio.create_task(supervise("ntp", lambda: sched.ntp_task(wifi), actuators)),
        asyncio.create_task(supervise("io", lambda: io_loop(wifi, actuators, device, control, oled, sched), actuators)),
    ]
    await asyncio.gather(*tasks)


def run():
    wifi, actuators, device, control, oled, sched = boot()
    try:
        asyncio.run(main_async(wifi, actuators, device, control, oled, sched))
    except KeyboardInterrupt:
        print("[main] stopped by user, switching all actuators off")
        actuators.all_off()
    except Exception as e:
        print("[main] fatal error, forcing safe state and resetting:", e)
        try:
            actuators.all_off()
        except Exception:
            pass
        time.sleep(2)
        machine.reset()


run()
