"""
sensors.py
----------
Optional sensor readers (DHT11 temp/humidity, analog EC/PPM probe).
Readings feed control.py's POST /sensor/data push.

The new config.py doesn't define ENABLE_SENSORS / DHT11_PIN /
EC_PPM_ADC_PIN, so this module falls back to disabled-by-default with
placeholder pins - add those three names to config.py to wire real
sensors in without touching this file.
"""

import config

ENABLE_SENSORS = getattr(config, "ENABLE_SENSORS", False)
DHT11_PIN = getattr(config, "DHT11_PIN", 4)
EC_PPM_ADC_PIN = getattr(config, "EC_PPM_ADC_PIN", 35)

_dht_sensor = None


def _init_dht():
    global _dht_sensor
    if _dht_sensor is None:
        import dht
        from machine import Pin
        _dht_sensor = dht.DHT11(Pin(DHT11_PIN))
    return _dht_sensor


def read_temp_humidity():
    """Returns (temp_c, humidity_pct) or (None, None) on failure."""
    if not ENABLE_SENSORS:
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
    if not ENABLE_SENSORS:
        return None
    try:
        from machine import ADC, Pin
        adc = ADC(Pin(EC_PPM_ADC_PIN))
        adc.atten(ADC.ATTN_11DB)  # full 0-3.3V range
        raw = adc.read()
        # Placeholder linear mapping - calibrate for your actual probe.
        ppm = raw * (1000 / 4095)
        return ppm
    except Exception as e:
        print("[sensors] EC/PPM read failed:", e)
        return None


def read_all():
    """Convenience aggregate used for the /sensor/data push if sensors are enabled."""
    if not ENABLE_SENSORS:
        return {}
    temp, hum = read_temp_humidity()
    ppm = read_ec_ppm()
    return {"temperature_c": temp, "humidity_pct": hum, "ec_ppm": ppm}