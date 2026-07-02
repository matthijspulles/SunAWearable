'''kleine driver voor de MLX90614 IR temperatuur sensor.'''


class MLX90614:
    '''leest omgeving en object temperatuur uit'''

    def __init__(self, i2c, address=0x5A):
        '''sla bus en adres op'''
        self.i2c = i2c
        self.addr = address

    def read_temp(self, reg):
        '''lees register en zet om naar graden C'''
        data = self.i2c.readfrom_mem(self.addr, reg, 3)

        # De eerste twee bytes bevatten de temperatuurwaarde
        raw = data[0] | (data[1] << 8)
        temp_c = raw * 0.02 - 273.15
        return temp_c

    def ambient_c(self):
        '''temperatuur van de sensor zelf'''
        return self.read_temp(0x06)

    def object_c(self):
        '''temperatuur waar de sensor op kijkt'''
        return self.read_temp(0x07)
