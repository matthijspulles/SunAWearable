# main.py - Pico W BLE + jouw werkende BPM update()
#
# Zet dit bestand op de Pico W als main.py.
#
# Bestanden die ook op de Pico moeten staan:
# - max30105_bpm.py
# - ltr390_uv.py
# - mlx90614.py
# - sht31.py
#
# Let op: als je bestand mlx90614(4).py heet, hernoem het op de Pico naar mlx90614.py.

from machine import Pin, I2C
import time
import bluetooth
import struct

try:
    from micropython import const
except ImportError:
    const = lambda x: x

from max30105_bpm import MAX30105BPM
from ltr390_uv import LTR390
from mlx90614 import MLX90614
from sht31 import SHT31


# =============================
# BLE Nordic UART Service
# =============================

_IRQ_CENTRAL_CONNECT = const(1)
_IRQ_CENTRAL_DISCONNECT = const(2)
_IRQ_GATTS_WRITE = const(3)

_UART_UUID = bluetooth.UUID("6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
_UART_TX = (
    bluetooth.UUID("6E400003-B5A3-F393-E0A9-E50E24DCCA9E"),
    bluetooth.FLAG_READ | bluetooth.FLAG_NOTIFY,
)
_UART_RX = (
    bluetooth.UUID("6E400002-B5A3-F393-E0A9-E50E24DCCA9E"),
    bluetooth.FLAG_WRITE,
)
_UART_SERVICE = (_UART_UUID, (_UART_TX, _UART_RX))


def advertising_payload(name):
    payload = bytearray()

    def append(adv_type, value):
        payload.extend(struct.pack("BB", len(value) + 1, adv_type))
        payload.extend(value)

    append(0x01, b"\x06")          # discoverable
    append(0x09, name.encode())    # complete local name
    return payload


class BLEUART:
    def __init__(self, name="WearableRisk"):
        self.ble = bluetooth.BLE()
        self.ble.active(True)

        try:
            self.ble.config(mtu=100)
        except Exception:
            pass

        self.ble.irq(self._irq)
        ((self.tx_handle, self.rx_handle),) = self.ble.gatts_register_services((_UART_SERVICE,))
        self.connections = set()
        self.payload = advertising_payload(name)
        self.advertise()

    def _irq(self, event, data):
        if event == _IRQ_CENTRAL_CONNECT:
            conn_handle, addr_type, addr = data
            self.connections.add(conn_handle)
            print("# BLE verbonden")

        elif event == _IRQ_CENTRAL_DISCONNECT:
            conn_handle, addr_type, addr = data
            self.connections.discard(conn_handle)
            print("# BLE losgekoppeld")
            self.advertise()

        elif event == _IRQ_GATTS_WRITE:
            conn_handle, attr_handle = data
            if attr_handle == self.rx_handle:
                try:
                    msg = self.ble.gatts_read(self.rx_handle).decode().strip()
                    print("# PC commando:", msg)
                except Exception:
                    pass

    def advertise(self):
        self.ble.gap_advertise(500000, adv_data=self.payload)
        print("# BLE advertising als WearableRisk")

    def send_line(self, line):
        if not line.endswith("\n"):
            line += "\n"

        data = line.encode()

        # In stukjes van 20 bytes voor betrouwbare BLE-notifications.
        for conn_handle in self.connections:
            for i in range(0, len(data), 20):
                try:
                    self.ble.gatts_notify(conn_handle, self.tx_handle, data[i:i + 20])
                    time.sleep_ms(5)
                except Exception as e:
                    print("# BLE notify fout:", e)
                    break


# =============================
# Helpers
# =============================

def value_to_text(value, decimals=2):
    if value is None:
        return ""
    try:
        return str(round(value, decimals))
    except Exception:
        return ""


def safe_read(label, func):
    try:
        return func()
    except Exception as e:
        print("#", label, "fout:", e)
        return None


# =============================
# Sensoren starten
# =============================

# I2C op Pico: SDA = GP0, SCL = GP1
i2c = I2C(0, sda=Pin(0), scl=Pin(1), freq=100000)
led = Pin("LED", Pin.OUT)

print("# I2C scan:", [hex(x) for x in i2c.scan()])

max_sensor = MAX30105BPM(i2c)
ltr = LTR390(i2c)
ir_temp = MLX90614(i2c)
sht = SHT31(i2c)

ble = BLEUART("WearableRisk")

print("# Start met meten via BLE")
print("# BPM gebruikt jouw werkende max_sensor.update()")
print("# Eerste 8 seconden kan BPM leeg zijn.")
print("# CSV:")
print("time_s,bpm,lux,uv_index,obj_c,ir_amb_c,sht_c,rv_pct")

start_time = time.ticks_ms()
last_send = time.ticks_ms()

bpm_value = None


# =============================
# Hoofdloop
# =============================

while True:
    # Belangrijk: dit moet zo vaak mogelijk blijven draaien,
    # precies zoals in jouw werkende testcode.
    try:
        bpm = max_sensor.update()

        if bpm is not None:
            bpm_value = bpm

    except Exception as e:
        # Niet bpm_value meteen wissen; anders krijg je steeds lege BPM bij één korte fout.
        # Print beperkt, zodat de loop snel blijft.
        pass

    now = time.ticks_ms()

    # Elke seconde één CSV-regel via USB printen én via BLE sturen.
    if time.ticks_diff(now, last_send) >= 1000:
        seconds = time.ticks_diff(now, start_time) // 1000

        lux = None
        uv_index = None
        obj_c = None
        ir_amb_c = None
        sht_c = None
        rv_pct = None

        try:
            light_raw, lux = ltr.read_light()
            uv_raw, uv_index = ltr.read_uv()
        except Exception as e:
            print("# LTR390 fout:", e)

        try:
            obj_c = ir_temp.object_c()
            ir_amb_c = ir_temp.ambient_c()
        except Exception as e:
            print("# MLX90614 fout:", e)

        try:
            sht_c, rv_pct = sht.read()
        except Exception as e:
            print("# SHT31 fout:", e)

        line = "{},{},{},{},{},{},{},{}".format(
            seconds,
            value_to_text(bpm_value, 0),
            value_to_text(lux, 1),
            value_to_text(uv_index, 2),
            value_to_text(obj_c, 1),
            value_to_text(ir_amb_c, 1),
            value_to_text(sht_c, 1),
            value_to_text(rv_pct, 1),
        )

        print(line)
        ble.send_line(line)

        led.toggle()
        last_send = now

    time.sleep_ms(5)

