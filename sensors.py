"""
sensors.py
----------
Optional sensor readers (DHT11 temp/humidity, analog EC/PPM probe).

Disabled by default for this deployment (6 lights + sliding door has
no sensors), but kept as part of the standard project skeleton so a
future revision of this device (e.g. adding room temperature or soil
EC monitoring) only needs to flip config.ENABLE_SENSORS and wire up
the pins already reserved in config.py - no restructuring needed.
"""

import config

_dht_sensor = None


def _init_dht():
    global _dht_sensor
    if _dht_sensor is None:
        import dht
        from machine import Pin
        _dht_sensor = dht.DHT11(Pin(config.DHT11_PIN))
    return _dht_sensor


def read_temp_humidity():
    """Returns (temp_c, humidity_pct) or (None, None) on failure."""
    if not config.ENABLE_SENSORS:
        return None, None
    try:
        d = _init_dht()
        d.measure()
        return d.temperature(), d.humidity()
    except Exception as e:
        print("[sensors] DHT11 read failed:", e)
        return None, None


def read_ec_ppm():
    """Returns a raw ADC-derived PPM estimate, or None if disabled/failed."""
    if not config.ENABLE_SENSORS:
        return None
    try:
        from machine import ADC, Pin
        adc = ADC(Pin(config.EC_PPM_ADC_PIN))
        adc.atten(ADC.ATTN_11DB)  # full 0-3.3V range
        raw = adc.read()
        # Placeholder linear mapping - calibrate for your actual probe.
        ppm = raw * (1000 / 4095)
        return ppm
    except Exception as e:
        print("[sensors] EC/PPM read failed:", e)
        return None


def read_all():
    """Convenience aggregate used for telemetry if sensors are enabled."""
    if not config.ENABLE_SENSORS:
        return {}
    temp, hum = read_temp_humidity()
    ppm = read_ec_ppm()
    return {"temperature_c": temp, "humidity_pct": hum, "ec_ppm": ppm}
