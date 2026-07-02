"""
sht31.py

Driver voor de SHT31 temperatuur- en luchtvochtigheidssensor.
De sensor hangt aan dezelfde I2C-bus als de andere sensoren.
"""

import time


class SHT31:
    """Leest omgevingstemperatuur en relatieve luchtvochtigheid uit."""

    # Standaardadres van de SHT31. Sommige modules kunnen ook op 0x45 staan.
    ADDR = 0x44

    def __init__(self, i2c, addr=ADDR):
        """Bewaar de I2C-bus en het adres van de sensor."""
        self.i2c = i2c
        self.addr = addr

    def read(self):
        """Start een meting en geef temperatuur en luchtvochtigheid terug."""
        # High repeatability, no clock stretching.
        # Dit commando komt uit de SHT31-datasheet.
        self.i2c.writeto(self.addr, bytes([0x24, 0x00]))
        time.sleep_ms(20)

        data = self.i2c.readfrom(self.addr, 6)

        # Byte 2 en 5 zijn CRC. Die controleren we hier niet, om de code simpel te houden.
        temp_raw = (data[0] << 8) | data[1]
        hum_raw = (data[3] << 8) | data[4]

        # Omrekeningen volgens de datasheet.
        temp_c = -45 + (175 * temp_raw / 65535)
        humidity = 100 * hum_raw / 65535

        return temp_c, humidity
