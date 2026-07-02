import time
import math


class MAX30105BPM:
    ADDR = 0x57

    REG_FIFO_WR_PTR = 0x04
    REG_OVF_COUNTER = 0x05
    REG_FIFO_RD_PTR = 0x06
    REG_FIFO_DATA = 0x07
    REG_FIFO_CONFIG = 0x08
    REG_MODE_CONFIG = 0x09
    REG_SPO2_CONFIG = 0x0A
    REG_LED_RED = 0x0C
    REG_LED_IR = 0x0D
    REG_LED_GREEN = 0x0E

    def __init__(self, i2c, address=ADDR):
        self.i2c = i2c
        self.addr = address

        # Sensor draait op 100 Hz
        self.sample_rate = 100

        # Voor berekening maken we hem lichter: 100 Hz / 4 = 25 Hz
        self.downsample = 4
        self.analysis_rate = self.sample_rate // self.downsample

        # 800 samples = ongeveer 8 seconden meetvenster
        # Groter = stabieler, kleiner = sneller
        self.buffer_size = 800

        # Sportbereik
        self.min_bpm = 35
        self.max_bpm = 210

        # Vingerdetectie
        self.finger_limit = 10000

        self.ir_buffer = []
        self.bpm = None
        self.bpm_history = []

        self.last_calc = time.ticks_ms()

        self.setup()

    def w(self, reg, val):
        self.i2c.writeto_mem(self.addr, reg, bytes([val]))

    def r(self, reg, n=1):
        return self.i2c.readfrom_mem(self.addr, reg, n)

    def setup(self):
        # Reset
        self.w(self.REG_MODE_CONFIG, 0x40)
        time.sleep_ms(100)

        for _ in range(100):
            if (self.r(self.REG_MODE_CONFIG)[0] & 0x40) == 0:
                break
            time.sleep_ms(1)

        self.clear_fifo()

        # FIFO:
        # 0x1F = sample average 1, rollover aan
        self.w(self.REG_FIFO_CONFIG, 0x1F)

        # SpO2 config:
        # 0x27 = ADC range 4096 nA, sample rate 100 Hz, pulse width 411 us
        self.w(self.REG_SPO2_CONFIG, 0x27)

        # LED power
        # Voor BPM gebruiken we vooral IR
        self.w(self.REG_LED_RED, 0x18)
        self.w(self.REG_LED_IR, 0x30)
        self.w(self.REG_LED_GREEN, 0x00)

        # Mode 0x03 = red + IR
        self.w(self.REG_MODE_CONFIG, 0x03)

        self.clear_fifo()

    def clear_fifo(self):
        self.w(self.REG_FIFO_WR_PTR, 0)
        self.w(self.REG_OVF_COUNTER, 0)
        self.w(self.REG_FIFO_RD_PTR, 0)

    def samples_available(self):
        write_ptr = self.r(self.REG_FIFO_WR_PTR)[0]
        read_ptr = self.r(self.REG_FIFO_RD_PTR)[0]

        samples = write_ptr - read_ptr

        if samples < 0:
            samples += 32

        return samples

    def read_fifo_sample(self):
        # In red + IR mode:
        # 3 bytes rood + 3 bytes IR
        data = self.r(self.REG_FIFO_DATA, 6)

        red = ((data[0] << 16) | (data[1] << 8) | data[2]) & 0x3FFFF
        ir = ((data[3] << 16) | (data[4] << 8) | data[5]) & 0x3FFFF

        return red, ir

    def update(self):
        count = self.samples_available()

        if count > 32:
            count = 32

        for _ in range(count):
            red, ir = self.read_fifo_sample()
            self.add_sample(ir)

        now = time.ticks_ms()

        # Elke seconde BPM opnieuw berekenen
        if time.ticks_diff(now, self.last_calc) >= 1000:
            self.calculate()
            self.last_calc = now

        return self.bpm

    def add_sample(self, ir):
        if ir < self.finger_limit:
            self.ir_buffer = []
            self.bpm = None
            self.bpm_history = []
            return

        self.ir_buffer.append(ir)

        if len(self.ir_buffer) > self.buffer_size:
            extra = len(self.ir_buffer) - self.buffer_size
            del self.ir_buffer[0:extra]

    def mean(self, values):
        return sum(values) / len(values)

    def median(self, values):
        values = sorted(values)
        n = len(values)

        if n == 0:
            return None

        if n % 2 == 1:
            return values[n // 2]

        return (values[n // 2 - 1] + values[n // 2]) / 2

    def moving_average(self, values, window):
        result = []
        half = window // 2
        n = len(values)

        for i in range(n):
            start = i - half
            end = i + half + 1

            if start < 0:
                start = 0

            if end > n:
                end = n

            result.append(sum(values[start:end]) / (end - start))

        return result

    def downsample_signal(self, values):
        result = []

        for i in range(0, len(values), self.downsample):
            part = values[i:i + self.downsample]

            if len(part) == self.downsample:
                result.append(sum(part) / len(part))

        return result

    def prepare_signal(self, values):
        # Eerst downsamplen zodat de berekening veel sneller wordt
        small = self.downsample_signal(values)

        if len(small) < 50:
            return []

        # DC verwijderen
        avg = self.mean(small)
        signal = [v - avg for v in small]

        # Langzame drift verwijderen
        baseline = self.moving_average(signal, 25)
        signal = [signal[i] - baseline[i] for i in range(len(signal))]

        # Klein beetje gladstrijken
        signal = self.moving_average(signal, 5)

        return signal

    def normalized_autocorr(self, signal, lag):
        n = len(signal) - lag

        if n <= 0:
            return 0

        sum_xy = 0
        sum_x2 = 0
        sum_y2 = 0

        for i in range(n):
            x = signal[i]
            y = signal[i + lag]

            sum_xy += x * y
            sum_x2 += x * x
            sum_y2 += y * y

        if sum_x2 <= 0 or sum_y2 <= 0:
            return 0

        return sum_xy / math.sqrt(sum_x2 * sum_y2)

    def bpm_from_autocorr(self, signal):
        min_lag = int(self.analysis_rate * 60 / self.max_bpm)
        max_lag = int(self.analysis_rate * 60 / self.min_bpm)

        if min_lag < 1:
            min_lag = 1

        candidates = []

        previous = None
        current = None

        # Autocorrelatie berekenen
        corrs = []

        for lag in range(min_lag, max_lag + 1):
            c = self.normalized_autocorr(signal, lag)
            corrs.append((lag, c))

        # Lokale pieken zoeken
        for i in range(1, len(corrs) - 1):
            lag = corrs[i][0]
            c = corrs[i][1]
            c_prev = corrs[i - 1][1]
            c_next = corrs[i + 1][1]

            if c > c_prev and c >= c_next and c > 0.20:
                bpm = 60 * self.analysis_rate / lag

                if self.min_bpm <= bpm <= self.max_bpm:
                    candidates.append((bpm, c))

        if len(candidates) == 0:
            return None

        # Als we al een vorige BPM hebben:
        # kies kandidaat die én goede correlatie heeft én dicht bij vorige BPM ligt
        if self.bpm is not None:
            best_score = -999
            best_bpm = None

            for bpm, corr in candidates:
                diff = abs(bpm - self.bpm)

                # Straf voor enorme sprongen
                score = corr - (diff / 120)

                if score > best_score:
                    best_score = score
                    best_bpm = bpm

            return best_bpm

        # Eerste keer: kies sterkste kandidaat
        best_bpm = candidates[0][0]
        best_corr = candidates[0][1]

        for bpm, corr in candidates:
            if corr > best_corr:
                best_bpm = bpm
                best_corr = corr

        return best_bpm

    def calculate(self):
        if len(self.ir_buffer) < self.buffer_size:
            self.bpm = None
            return

        ir_avg = self.mean(self.ir_buffer)

        if ir_avg < self.finger_limit:
            self.bpm = None
            self.bpm_history = []
            return

        signal = self.prepare_signal(self.ir_buffer)

        if len(signal) < 50:
            return

        new_bpm = self.bpm_from_autocorr(signal)

        if new_bpm is None:
            return

        # Geschiedenis tegen uitschieters
        self.bpm_history.append(new_bpm)

        if len(self.bpm_history) > 3:
            self.bpm_history.pop(0)

        median_bpm = self.median(self.bpm_history)

        if median_bpm is None:
            return

        if self.bpm is None:
            self.bpm = round(median_bpm)
        else:
            # Sneller reageren dan vorige versie
            self.bpm = round(self.bpm * 0.35 + median_bpm * 0.65)
