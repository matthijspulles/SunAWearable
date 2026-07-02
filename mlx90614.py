"""
mlx90614.py

Eenvoudige driver voor de MLX90614 infraroodtemperatuursensor.
Deze sensor meet contactloos de temperatuur van een object, in ons geval de huid.
"""


class MLX90614:
    """Leest omgevingstemperatuur en objecttemperatuur via I2C."""

    def __init__(self, i2c, address=0x5A):
        """Bewaar de I2C-bus en het standaardadres van de sensor."""
        self.i2c = i2c
        self.addr = address

    def read_temp(self, reg):
        """Lees een temperatuurregister en zet de ruwe waarde om naar graden Celsius."""
        data = self.i2c.readfrom_mem(self.addr, reg, 3)

        # De eerste twee bytes bevatten de temperatuurwaarde.
        # Volgens de datasheet is de resolutie 0,02 K per stap.
        raw = data[0] | (data[1] << 8)
        temp_c = raw * 0.02 - 273.15
        return temp_c

    def ambient_c(self):
        """Geef de temperatuur van de sensor/omgeving terug in graden Celsius."""
        return self.read_temp(0x06)

    def object_c(self):
        """Geef de gemeten objecttemperatuur terug, dus de huidzijde in dit project."""
        return self.read_temp(0x07)
