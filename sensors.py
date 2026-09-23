"""
sensors.py
----------
Optional sensor readers (DHT11 temp/humidity, analog EC/PPM probe).
Readings feed control.py's POST /sensor/data push.

The new config.py doesn't define ENABLE_SENSORS / DHT11_PIN /
EC_PPM_ADC_PIN, so this module falls back to disabled-by-default with
placeholder pins - add those three names to config.py to wire real
sensors in without touching this file.

Sensor readers for:
- DHT11 temperature / humidity
- Analog EC/PPM probe
- YL-83 rain sensor (analog AO + digital DO)

Readings feed control.py's POST /sensor/data push.

The module falls back to disabled-by-default if ENABLE_SENSORS
is not defined in config.py.

Default sensor pins:
- DHT11       : GPIO4
- EC/PPM ADC  : GPIO35
- Rain AO     : GPIO32
- Rain DO     : GPIO33
"""

import config

ENABLE_SENSORS = getattr(config, "ENABLE_SENSORS", False)

# DHT11
DHT11_PIN = getattr(config, "DHT11_PIN", 4)

# EC / PPM analog probe
EC_PPM_ADC_PIN = getattr(config, "EC_PPM_ADC_PIN", 35)

# YL-83 rain sensor
# AO = analog rain/wetness level
# DO = digital rain detection
RAIN_SENSOR_ADC_PIN = getattr(config, "RAIN_SENSOR_ADC_PIN", 32)
RAIN_SENSOR_DIGITAL_PIN = getattr(config, "RAIN_SENSOR_DIGITAL_PIN", 33)

# Most YL-83 comparator modules output LOW when the rain threshold
# is reached. This can be changed from config.py if necessary.
RAIN_DIGITAL_ACTIVE_LOW = getattr(
    config,
    "RAIN_DIGITAL_ACTIVE_LOW",
    True,
)

# ---------------------------------------------------------------------------
# Internal sensor instances
# ---------------------------------------------------------------------------
_dht_sensor = None
_rain_adc = None
_rain_digital = None

# ---------------------------------------------------------------------------
# DHT11
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# EC / PPM
# ---------------------------------------------------------------------------
def read_ec_ppm():
    """Returns a raw ADC-derived PPM estimate, or None if disabled/failed."""
    if not ENABLE_SENSORS:
        return None
    try:
        from machine import ADC, Pin

        adc = ADC(Pin(EC_PPM_ADC_PIN))
        adc.atten(ADC.ATTN_11DB)  # full 0-3.3V range

        raw = adc.read()

        # Placeholder linear mapping.
        # Calibrate this for your actual EC/PPM probe.
        ppm = raw * (1000 / 4095)
        return ppm
    except Exception as e:
        print("[sensors] EC/PPM read failed:", e)
        return None

# ---------------------------------------------------------------------------
# YL-83 Rain Sensor
# ---------------------------------------------------------------------------

def _init_rain_sensor():
    global _rain_adc
    global _rain_digital

    if _rain_adc is None or _rain_digital is None:
        from machine import ADC, Pin

        # AO - analog rain/wetness reading
        _rain_adc = ADC(Pin(RAIN_SENSOR_ADC_PIN))
        _rain_adc.atten(ADC.ATTN_11DB)

        # DO - digital rain threshold detection
        _rain_digital = Pin(
            RAIN_SENSOR_DIGITAL_PIN,
            Pin.IN,
        )

    return _rain_adc, _rain_digital


def read_rain():
    """
    Read the YL-83 rain sensor.

    Returns:
        {
            "rain_detected": bool,
            "rain_raw": int,
            "rain_level_pct": float
        }

    rain_raw:
        Raw ESP32 ADC value from 0-4095.

    rain_level_pct:
        Estimated wetness/rain level from 0-100%.

        0%   = dry
        100% = very wet

    Note:
        The AO behavior can vary slightly between YL-83 modules.
        The current calculation assumes lower ADC values mean
        more water/wetness.
    """

    if not ENABLE_SENSORS:
        return None

    try:
        adc, digital = _init_rain_sensor()

        # ---------------------------------------------------------------
        # Analog reading
        # ---------------------------------------------------------------

        raw = adc.read()

        # YL-83 commonly produces a lower analog value when the
        # sensor becomes wetter.
        rain_level_pct = 100 - ((raw / 4095) * 100)

        # Keep within 0-100 range.
        rain_level_pct = max(
            0,
            min(100, rain_level_pct),
        )

        # ---------------------------------------------------------------
        # Digital reading
        # ---------------------------------------------------------------

        digital_value = digital.value()

        if RAIN_DIGITAL_ACTIVE_LOW:
            rain_detected = digital_value == 0
        else:
            rain_detected = digital_value == 1

        return {
            "rain_detected": rain_detected,
            "rain_raw": raw,
            "rain_level_pct": round(rain_level_pct, 1),
        }

    except Exception as e:
        print("[sensors] Rain sensor read failed:", e)
        return None

# ---------------------------------------------------------------------------
# Read all sensors
# ---------------------------------------------------------------------------
def read_all():
    """Convenience aggregate used for the /sensor/data push if sensors are enabled."""
    if not ENABLE_SENSORS:
        return {}
    temp, hum = read_temp_humidity()
    ppm = read_ec_ppm()
    rain = read_rain()
    
    data = {
        "temperature_c": temp,
        "humidity_pct": hum,
        "ec_ppm": ppm,
    }

    if rain is not None:
        data.update(rain)

    return data