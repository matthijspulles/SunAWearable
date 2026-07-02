'''kleine driver voor de SHT31 temperatuur en luchtvochtigheid sensor.'''

import time


class SHT31:
    '''leest temperatuur en luchtvochtigheid uit'''

    # standaardadres van de SHT31. Sommige modules kunnen ook op 0x45 staan
    ADDR = 0x44

    def __init__(self, i2c, addr=ADDR):
        '''sla bus en adres op'''
        self.i2c = i2c
        self.addr = addr

    def read(self):
        '''doe 1 meting en geef temp en rv terug'''
        # High repeatability, no clock stretching
        # Dit commando komt uit de SHT31-datasheet
        self.i2c.writeto(self.addr, bytes([0x24, 0x00]))
        time.sleep_ms(20)

        data = self.i2c.readfrom(self.addr, 6)

        temp_raw = (data[0] << 8) | data[1]
        hum_raw = (data[3] << 8) | data[4]

        temp_c = -45 + (175 * temp_raw / 65535)
        humidity = 100 * hum_raw / 65535

        return temp_c, humidity
