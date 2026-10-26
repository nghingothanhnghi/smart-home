"""Copy this file to the billiard ESP32 AS main.py (MicroPython only auto-runs
main.py).  Do NOT copy the hydro main.py to a billiard board, and do not
copy this over the hydro board's main.py.

    mpremote cp main_billiard.py :main.py
"""

import billiard_main  # noqa: F401  (runs the sync loop on import)
