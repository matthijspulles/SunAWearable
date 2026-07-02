"""
main.py

Hoofdprogramma voor de Raspberry Pi Pico 2 W.

Dit bestand doet drie dingen:
1. I2C-bus starten en de sensoren initialiseren.
2. De BLE Nordic UART Service aanbieden onder de naam "WearableRisk".
3. Elke seconde een CSV-regel met meetwaarden naar USB en BLE sturen.

Bestanden die ook op de Pico moeten staan:
- max30105_bpm.py
- ltr390_uv.py
- mlx90614.py
- sht31.py

Let op: de bestandsnamen moeten op de Pico precies zo heten, anders werken de imports niet.
"""

from machine import Pin, I2C
import time
import bluetooth
import struct

try:
    from micropython import const
except ImportError:
    # Maakt testen buiten MicroPython iets makkelijker.
    const = lambda x: x

from max30105_bpm import MAX30105BPM
from ltr390_uv import LTR390
from mlx90614 import MLX90614
from sht31 import SHT31


# =============================
# BLE Nordic UART Service
# =============================

# BLE event-codes van MicroPython.
_IRQ_CENTRAL_CONNECT = const(1)
_IRQ_CENTRAL_DISCONNECT = const(2)
_IRQ_GATTS_WRITE = const(3)

# UUID's van de Nordic UART Service. Bleak op de laptop luistert naar dezelfde UUID's.
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
    """Maak het BLE-advertisingpakket waarmee de laptop de Pico kan vinden."""
    payload = bytearray()

    def append(adv_type, value):
        payload.extend(struct.pack("BB", len(value) + 1, adv_type))
        payload.extend(value)

    append(0x01, b"\x06")          # discoverable
    append(0x09, name.encode())    # complete local name
    return payload


class BLEUART:
    """Kleine wrapper rond BLE, zodat main.py alleen send_line() hoeft te gebruiken."""

    def __init__(self, name="WearableRisk"):
        """Start BLE, registreer de UART-service en begin met adverteren."""
        self.ble = bluetooth.BLE()
        self.ble.active(True)

        # Een grotere MTU is handig, maar niet op elke firmware beschikbaar.
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
        """Verwerk BLE-events zoals verbinden, verbreken en ontvangen commando's."""
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
        """Maak de Pico zichtbaar als BLE-apparaat."""
        self.ble.gap_advertise(500000, adv_data=self.payload)
        print("# BLE advertising als WearableRisk")

    def send_line(self, line):
        """Stuur een tekstregel naar alle verbonden BLE-clients."""
        if not line.endswith("\n"):
            line += "\n"

        data = line.encode()

        # In stukjes van 20 bytes voor betrouwbare BLE-notifications.
        # Dit voorkomt problemen met clients die geen grotere pakketjes accepteren.
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
    """Zet een sensorwaarde netjes om naar tekst voor de CSV-regel."""
    if value is None:
        return ""
    try:
        return str(round(value, decimals))
    except Exception:
        return ""


def safe_read(label, func):
    """Lees een sensor veilig uit. Wordt niet overal gebruikt, maar is handig bij testen."""
    try:
        return func()
    except Exception as e:
        print("#", label, "fout:", e)
        return None


# =============================
# Sensoren starten
# =============================

# I2C op Pico: SDA = GP0, SCL = GP1.
# Alle sensoren zitten op dezelfde I2C-bus.
i2c = I2C(0, sda=Pin(0), scl=Pin(1), freq=100000)
led = Pin("LED", Pin.OUT)

# De scan is vooral handig om te controleren of alle sensoren worden gezien.
print("# I2C scan:", [hex(x) for x in i2c.scan()])

max_sensor = MAX30105BPM(i2c)
ltr = LTR390(i2c)
ir_temp = MLX90614(i2c)
sht = SHT31(i2c)

ble = BLEUART("WearableRisk")

print("# Start met meten via BLE")
print("# BPM gebruikt max_sensor.update()")
print("# Eerste 8 seconden kan BPM leeg zijn, omdat de buffer dan nog gevuld wordt.")
print("# CSV:")
print("time_s,bpm,lux,uv_index,obj_c,ir_amb_c,sht_c,rv_pct")

start_time = time.ticks_ms()
last_send = time.ticks_ms()

# Laatste geldige BPM blijft bewaard. Daardoor valt de BPM niet meteen weg bij één mislukte update.
bpm_value = None


# =============================
# Hoofdloop
# =============================

while True:
    # De MAX30105 moet zo vaak mogelijk worden bijgewerkt, anders mist de BPM-code samples.
    try:
        bpm = max_sensor.update()

        if bpm is not None:
            bpm_value = bpm

    except Exception:
        # Niet bpm_value meteen wissen; anders krijg je steeds lege BPM bij één korte fout.
        # Ook niet te veel printen, want dat vertraagt de loop.
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

        # Omgevingslicht en UV. Eerst licht en daarna UV, omdat de LTR390 per meting van modus wisselt.
        try:
            light_raw, lux = ltr.read_light()
            uv_raw, uv_index = ltr.read_uv()
        except Exception as e:
            print("# LTR390 fout:", e)

        # Infraroodtemperatuur: object is de huid, ambient is de sensortemperatuur.
        try:
            obj_c = ir_temp.object_c()
            ir_amb_c = ir_temp.ambient_c()
        except Exception as e:
            print("# MLX90614 fout:", e)

        # Omgevingstemperatuur en relatieve luchtvochtigheid.
        try:
            sht_c, rv_pct = sht.read()
        except Exception as e:
            print("# SHT31 fout:", e)

        # CSV-volgorde moet gelijk blijven aan het Bleak-script op de laptop.
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

        # LED toggle is een simpele alive-indicator.
        led.toggle()
        last_send = now

    # Kleine pauze zodat de Pico niet helemaal voluit draait.
    time.sleep_ms(5)
