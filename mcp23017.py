"""
mcp23017.py
-----------
MicroPython driver for the MCP23017 16-bit I2C GPIO expander.

Gives each of the 16 expander pins (port A: 0-7, port B: 8-15) a
`machine.Pin`-like object (`.value()`, `.on()`, `.off()`) so callers -
notably gpio_manager.py - can treat an expander pin exactly like a
native ESP32 GPIO pin without knowing the difference.

Hardware notes:
  - No PWM. The MCP23017 only does digital in/out - there is no PWM
    peripheral behind it. gpio_manager.py enforces this (raises if you
    ask for a PWM-capable pin on an expander pin); this driver
    deliberately doesn't fake a duty() method, so a misconfigured
    "mosfet" actuator on an expander pin fails loudly at boot instead
    of silently behaving like a relay.
  - Multiple chips on one bus: the A0-A2 address pins let up to 8
    units share one I2C bus (0x20-0x27). Pass the resolved 7-bit
    address as `address=`.
  - Every register write goes through an in-memory shadow of
    IODIR/GPPU/OLAT/etc, so setting a pin's mode or level is always a
    single register write, never a read-modify-write over I2C (which
    would double the bus traffic and could race a hardware interrupt
    changing an input pin between the read and the write).

Register map assumes IOCON.BANK = 0 (the power-on default) with
IOCON.SEQOP = 0 (sequential addressing across the A/B pair for a given
register), which __init__ sets explicitly rather than trusting the
chip's reset state.
"""

# Register addresses (IOCON.BANK = 0, port A; port B is always +1)
_IODIRA = 0x00
_IPOLA = 0x02
_GPINTENA = 0x04
_DEFVALA = 0x06
_INTCONA = 0x08
_IOCON = 0x0A
_GPPUA = 0x0C
_INTFA = 0x0E
_INTCAPA = 0x10
_GPIOA = 0x12
_OLATA = 0x14

_IOCON_MIRROR = 0x40  # OR both INTA/INTB together into one physical interrupt line

IN = 0
OUT = 1


class MCP23017:
    def __init__(self, i2c, address=0x20, int_mirror=False, reset_on_init=True):
        self.i2c = i2c
        self.address = address

        # Shadow registers - single source of truth for every writable
        # register, so every public method below is one clean I2C
        # write, never a read-then-modify-then-write round trip.
        self._iodir = 0xFFFF  # power-on default: every pin is an input
        self._gppu = 0x0000
        self._olat = 0x0000
        self._ipol = 0x0000
        self._gpinten = 0x0000
        self._defval = 0x0000
        self._intcon = 0x0000

        self._pins = [None] * 16  # lazily-built MCP23017Pin cache, one per pin

        if reset_on_init:
            self._write8(_IOCON, _IOCON_MIRROR if int_mirror else 0x00)
            self._write16(_IODIRA, self._iodir)
            self._write16(_GPPUA, self._gppu)
            self._write16(_OLATA, self._olat)
            self._write16(_IPOLA, self._ipol)
            self._write16(_GPINTENA, self._gpinten)
            self._write16(_DEFVALA, self._defval)
            self._write16(_INTCONA, self._intcon)

    # ---- low level ----
    def _write8(self, reg, val):
        self.i2c.writeto_mem(self.address, reg, bytes([val & 0xFF]))

    def _write16(self, reg_a, val16):
        """reg_a is the port-A register; port-B is reg_a + 1 (sequential-mode auto-increment)."""
        self.i2c.writeto_mem(self.address, reg_a,
                              bytes([val16 & 0xFF, (val16 >> 8) & 0xFF]))

    def _read16(self, reg_a):
        raw = self.i2c.readfrom_mem(self.address, reg_a, 2)
        return raw[0] | (raw[1] << 8)

    @staticmethod
    def _check_pin(pin_no):
        if not 0 <= pin_no <= 15:
            raise ValueError("MCP23017 pin must be 0-15, got %r" % (pin_no,))

    # ---- per-pin configuration ----
    def set_mode(self, pin_no, mode, pull=None):
        """mode: IN or OUT. pull: 'up' enables the internal ~100k pull-up (inputs only)."""
        self._check_pin(pin_no)
        bit = 1 << pin_no

        if mode == OUT:
            self._iodir &= ~bit
        elif mode == IN:
            self._iodir |= bit
        else:
            raise ValueError("mode must be mcp23017.IN or mcp23017.OUT")
        self._write16(_IODIRA, self._iodir)

        if pull == "up":
            self._gppu |= bit
        else:
            self._gppu &= ~bit
        self._write16(_GPPUA, self._gppu)

    def value(self, pin_no, val=None):
        """
        Get (val=None) or set the logic level of one pin -
        digitalRead/digitalWrite style. A get on a pin currently
        configured as OUTPUT is answered from the OLAT shadow register
        rather than an I2C read of GPIOA/B: on real MCP23017 silicon
        GPIO mirrors OLAT for output pins anyway, so this returns the
        identical value while skipping a bus round trip - only INPUT
        pins actually need a live read.
        """
        self._check_pin(pin_no)
        bit = 1 << pin_no

        if val is None:
            if self._iodir & bit:  # configured as input - need the live pin state
                return 1 if (self._read16(_GPIOA) & bit) else 0
            return 1 if (self._olat & bit) else 0  # output - shadow register already IS the driven state

        if val:
            self._olat |= bit
        else:
            self._olat &= ~bit
        self._write16(_OLATA, self._olat)
        return None

    def pin(self, pin_no, mode=OUT, pull=None):
        """
        Returns a cached MCP23017Pin exposing the machine.Pin-shaped
        API (.value()/.on()/.off()) that gpio_manager.py hands to
        relay.py. Calling this again for a pin_no already claimed just
        reconfigures its mode/pull and returns the same wrapper,
        rather than creating a second object bound to the same pin.
        """
        self._check_pin(pin_no)
        self.set_mode(pin_no, mode, pull)
        if self._pins[pin_no] is None:
            self._pins[pin_no] = MCP23017Pin(self, pin_no)
        return self._pins[pin_no]

    # ---- whole-port convenience ----
    def read_gpio(self):
        """Raw 16-bit snapshot of both ports (bit N = pin N)."""
        return self._read16(_GPIOA)

    def write_gpio(self, val16):
        """Set every OUTPUT-configured pin to match val16 in one I2C write; bits for INPUT pins are ignored by the hardware."""
        self._olat = val16 & 0xFFFF
        self._write16(_OLATA, self._olat)

    # ---- interrupt-on-change (optional - polling read_gpio()/value() works fine without this) ----
    def enable_interrupt(self, pin_no, compare_default=None):
        """
        Makes pin_no assert INTA/INTB on change. If compare_default is
        given (0 or 1), it's interrupt-on-compare against that fixed
        level instead of interrupt-on-any-change - useful for e.g. a
        door limit switch you only care about hitting one way.
        """
        self._check_pin(pin_no)
        bit = 1 << pin_no

        if compare_default is None:
            self._intcon &= ~bit
        else:
            self._intcon |= bit
            if compare_default:
                self._defval |= bit
            else:
                self._defval &= ~bit
            self._write16(_DEFVALA, self._defval)
        self._write16(_INTCONA, self._intcon)

        self._gpinten |= bit
        self._write16(_GPINTENA, self._gpinten)

    def disable_interrupt(self, pin_no):
        self._check_pin(pin_no)
        self._gpinten &= ~(1 << pin_no)
        self._write16(_GPINTENA, self._gpinten)

    def interrupt_flags(self):
        """16-bit mask of which pin(s) triggered the pending interrupt."""
        return self._read16(_INTFA)

    def clear_interrupts(self):
        """Reading INTCAP latches-and-clears the interrupt on real silicon; returns the captured levels."""
        return self._read16(_INTCAPA)


class MCP23017Pin:
    """
    machine.Pin-alike bound to one pin of one MCP23017. Implements only
    what relay.py/gpio_manager.py actually use - value get/set plus
    on()/off() - so it's a drop-in for those call sites without
    pretending to be a full machine.Pin (no irq(), no PWM/duty() - see
    the module docstring for why PWM specifically can't exist here).
    """

    def __init__(self, expander, pin_no):
        self._expander = expander
        self._pin_no = pin_no

    def value(self, val=None):
        return self._expander.value(self._pin_no, val)

    def on(self):
        self._expander.value(self._pin_no, 1)

    def off(self):
        self._expander.value(self._pin_no, 0)

    def mode(self, mode, pull=None):
        self._expander.set_mode(self._pin_no, mode, pull)

    def __repr__(self):
        return "MCP23017Pin(addr=0x%02X, pin=%d)" % (self._expander.address, self._pin_no)
