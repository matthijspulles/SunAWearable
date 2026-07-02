import time


class LTR390:
    ADDR = 0x53

    MAIN_CTRL = 0x00
    MEAS_RATE = 0x04
    GAIN = 0x05
    PART_ID = 0x06

    ALS_DATA = 0x0D
    UVS_DATA = 0x10

    def __init__(self, i2c, addr=ADDR):
        self.i2c = i2c
        self.addr = addr

        part = self.read_u8(self.PART_ID)
        print("LTR390 PART_ID:", hex(part))

        # 16-bit resolutie, 100 ms meettijd
        self.write_u8(self.MEAS_RATE, (4 << 4) | 2)

        # Gain 3x
        self.write_u8(self.GAIN, 1)

        # Sensor aan
        self.write_u8(self.MAIN_CTRL, 0x02)

    def read_u8(self, register):
        return self.i2c.readfrom_mem(self.addr, register, 1)[0]

    def write_u8(self, register, value):
        self.i2c.writeto_mem(self.addr, register, bytes([value]))

    def read_u24(self, register):
        data = self.i2c.readfrom_mem(self.addr, register, 3)
        return data[0] | (data[1] << 8) | (data[2] << 16)

    def read_uv(self):
        # UV mode
        self.write_u8(self.MAIN_CTRL, 0x0A)
        time.sleep_ms(120)

        uv_raw = self.read_u24(self.UVS_DATA)

        gain_value = 3
        resolution_bits = 16

        uv_index = uv_raw / (
            (gain_value / 18)
            * ((2 ** resolution_bits) / (2 ** 20))
            * 2300
        )

        return uv_raw, uv_index

    def read_light(self):
        # Licht / ALS mode
        self.write_u8(self.MAIN_CTRL, 0x02)
        time.sleep_ms(120)

        light_raw = self.read_u24(self.ALS_DATA)

        gain_value = 3
        integration_factor = 0.25

        lux = (light_raw * 0.6) / (gain_value * integration_factor)

        return light_raw, lux
