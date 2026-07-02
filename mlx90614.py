import time

class MLX90614:
    def __init__(self, i2c, address=0x5A):
        self.i2c = i2c
        self.addr = address

    def read_temp(self, reg):
        data = self.i2c.readfrom_mem(self.addr, reg, 3)
        raw = data[0] | (data[1] << 8)
        temp_c = raw * 0.02 - 273.15
        return temp_c

    def ambient_c(self):
        return self.read_temp(0x06)

    def object_c(self):
        return self.read_temp(0x07)
