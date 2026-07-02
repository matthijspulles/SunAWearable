"""
ltr390_uv.py

Driver voor de LTR390 UV- en lichtsensor.
Deze code draait op de Raspberry Pi Pico en wordt aangeroepen vanuit main.py.

De sensor wordt via I2C uitgelezen. In dit project gebruiken we vooral:
- read_uv() voor de ruwe UV-waarde en de omgerekende UV-index
- read_light() voor de ruwe lichtwaarde en lux
"""

import time


class LTR390:
    """Kleine I2C-driver voor de LTR390 sensor."""

    # Standaard I2C-adres van de LTR390.
    ADDR = 0x53

    # Registers uit de datasheet die in deze simpele driver gebruikt worden.
    MAIN_CTRL = 0x00
    MEAS_RATE = 0x04
    GAIN = 0x05
    PART_ID = 0x06

    ALS_DATA = 0x0D
    UVS_DATA = 0x10

    def __init__(self, i2c, addr=ADDR):
        """Sla de I2C-bus op en zet de sensor in een basisinstelling."""
        self.i2c = i2c
        self.addr = addr

        # Handige check bij het opstarten. Als dit niet lukt, zit de sensor
        # meestal niet goed op de bus of is het adres anders.
        part = self.read_u8(self.PART_ID)
        print("LTR390 PART_ID:", hex(part))

        # 16-bit resolutie, 100 ms meettijd.
        self.write_u8(self.MEAS_RATE, (4 << 4) | 2)

        # Gain 3x. Deze waarde wordt ook gebruikt in de omrekening hieronder.
        self.write_u8(self.GAIN, 1)

        # Sensor aan in ALS-modus. Voor UV wordt de modus later tijdelijk aangepast.
        self.write_u8(self.MAIN_CTRL, 0x02)

    def read_u8(self, register):
        """Lees 1 byte uit een register."""
        return self.i2c.readfrom_mem(self.addr, register, 1)[0]

    def write_u8(self, register, value):
        """Schrijf 1 byte naar een register."""
        self.i2c.writeto_mem(self.addr, register, bytes([value]))

    def read_u24(self, register):
        """Lees een 24-bit waarde uit drie opeenvolgende bytes."""
        data = self.i2c.readfrom_mem(self.addr, register, 3)
        return data[0] | (data[1] << 8) | (data[2] << 16)

    def read_uv(self):
        """Lees de UV-waarde en reken deze om naar een UV-index."""
        # UV-modus aanzetten en kort wachten tot de meting klaar is.
        self.write_u8(self.MAIN_CTRL, 0x0A)
        time.sleep_ms(120)

        uv_raw = self.read_u24(self.UVS_DATA)

        # Deze omrekening hoort bij gain 3x en 16-bit resolutie.
        # De factor 2300 is de gevoeligheidsfactor die we in dit prototype gebruiken.
        gain_value = 3
        resolution_bits = 16

        uv_index = uv_raw / (
            (gain_value / 18)
            * ((2 ** resolution_bits) / (2 ** 20))
            * 2300
        )

        return uv_raw, uv_index

    def read_light(self):
        """Lees de zichtbare lichtwaarde en reken deze om naar lux."""
        # ALS staat voor Ambient Light Sensor.
        self.write_u8(self.MAIN_CTRL, 0x02)
        time.sleep_ms(120)

        light_raw = self.read_u24(self.ALS_DATA)

        gain_value = 3
        integration_factor = 0.25

        lux = (light_raw * 0.6) / (gain_value * integration_factor)

        return light_raw, lux
