"""
pc_bleak_risicoanalyse_uvgemiddeld.py

Laptop-script voor de wearable. Dit script ontvangt de CSV-regels van de Pico W via
BLE Nordic UART en voert de risicologica uit zoals beschreven in de risicoanalyse:
- UV-index-klassen en cumulatieve UV-dosis in SED
- persoonlijke huidtype-drempel
- Heat Index volgens de Rothfusz-regressie
- zoncorrectie op basis van UV-index
- omgevingsscore, hartslagscore, huidtemperatuurscore en duurfactor
- hittestressscore met weging 0,40 / 0,35 / 0,20 / 0,05
- directe escalatieregels
- rollende gemiddelden: UV over 30 s, BPM over 30 s, huidtemperatuur over 60 s, luchttemperatuur over 60 s
- hysterese: pas verhogen na 60 s, pas verlagen na 5 min, behalve directe escalaties

Start:
    python pc_bleak_risicoanalyse_uvgemiddeld.py

Tijdens de meting:
    help        toon commando's
    s           bevestig dat je zonnebrand hebt aangebracht
    rust        start rust-/herstelmeting voor hartslagherstel
    doorgaan    stop rust-/herstelmeting
    stop        sessie stoppen en CSV/grafieken opslaan
"""

import asyncio
import csv
import os
import queue
import threading
import time
from datetime import datetime, timedelta

from bleak import BleakClient, BleakScanner
import matplotlib.pyplot as plt


DEVICE_NAME = "WearableRisk"

# Nordic UART Service UUIDs
UART_TX_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"  # notify van Pico naar pc
UART_RX_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  # write van pc naar Pico

# -----------------------------
# Gebruikersinstellingen
# -----------------------------
# Deze waarden zijn startwaarden. Tijdens het draaien kunnen ze ook met
# de terminalcommando's 'age ...' en 'skin ...' worden aangepast.
AGE_YEARS = 22
SKIN_TYPE = "II"        # I, II, III, IV, V of VI
BASELINE_SECONDS = 300   # eerste 5 minuten huidtemperatuur-baseline

# In de risicoanalyse wordt de gemeten luchttemperatuur gebruikt.
# Daarom staat de correctie standaard op 0.0 °C.
# Alleen aanpassen als jullie de SHT31 apart hebben gekalibreerd.
SHT_TEMP_OFFSET_C = 0.0

# Rollende gemiddelden uit de risicoanalyse
UV_AVG_SECONDS = 30
BPM_AVG_SECONDS = 30
SKIN_AVG_SECONDS = 60
AIR_AVG_SECONDS = 60

# Hysterese uit de risicoanalyse
RAISE_CONFIRM_SECONDS = 60
LOWER_CONFIRM_SECONDS = 300

# Operationele invulling van niet-gekwantificeerde zinnen uit de risicoanalyse.
# De risicoanalyse zegt "huidtemperatuur stijgt snel" en "hartslag daalt nauwelijks",
# maar geeft daar geen exacte getallen voor. Deze waarden maken die regels uitvoerbaar.
FAST_SKIN_RISE_WINDOW_SECONDS = 60
FAST_SKIN_RISE_MIN_C = 1.0
REST_RECOVERY_CHECK_SECONDS = 60
REST_MIN_EXPECTED_DROP_BPM = 10

SKIN_SED_THRESHOLD = {
    "I": 2.0,
    "II": 2.5,
    "III": 4.0,
    "IV": 6.0,
    "V": 8.5,
    "VI": 10.0,
}

LEVEL_NAMES = ["GROEN", "GEEL", "ORANJE", "ROOD", "KRITIEK"]


# -----------------------------
# Hulpfuncties
# -----------------------------
def parse_float(text):
    """Zet tekst uit de CSV om naar float. Lege velden worden None."""
    text = str(text).strip()
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def clamp(value, low, high):
    """Beperk een waarde tussen een minimum en maximum."""
    return max(low, min(high, value))


def format_duration(seconds):
    """Maak van seconden een leesbare tijd, bijvoorbeeld 0:03:21."""
    return str(timedelta(seconds=int(seconds)))


def bell():
    """Geef een simpele piep in de terminal bij hogere risico's."""
    print("\a", end="")


class RollingAverage:
    """Tijdvenster-gemiddelde voor sensorwaarden.

    We gebruiken dit om schommelende sensoren rustiger te maken zonder meteen
    alle ruwe data kwijt te raken.
    """

    def __init__(self, window_seconds):
        """Maak een nieuw venster van een aantal seconden."""
        self.window_seconds = window_seconds
        self.values = []

    def reset(self):
        """Maak het venster leeg, bijvoorbeeld bij een training-reset."""
        self.values = []

    def add(self, t_s, value):
        """Voeg een waarde toe en geef direct het nieuwe gemiddelde terug."""
        if t_s is None or value is None:
            return self.average(t_s)

        self.values.append((t_s, value))
        cutoff = t_s - self.window_seconds
        self.values = [(t, v) for (t, v) in self.values if t >= cutoff]
        return self.average(t_s)

    def average(self, t_s=None):
        """Bereken het gemiddelde van de waarden die nog in het venster zitten."""
        if not self.values:
            return None
        return sum(v for _, v in self.values) / len(self.values)

    def oldest(self):
        """Geef de oudste opgeslagen waarde terug."""
        if not self.values:
            return None
        return self.values[0]


# -----------------------------
# Risicoanalyse
# -----------------------------
class RiskAnalyzer:
    """Voert de risicoanalyse uit op basis van de ontvangen sensorregels.

    De Pico stuurt alleen meetwaarden. Deze klasse houdt de geschiedenis bij,
    maakt gemiddelden en bepaalt daaruit de UV- en hittestressmeldingen.
    """

    def __init__(self, age_years=AGE_YEARS, skin_type=SKIN_TYPE):
        """Zet alle instellingen, timers en buffers klaar voor een nieuwe sessie."""
        self.age_years = int(age_years)
        self.skin_type = skin_type.upper()
        if self.skin_type not in SKIN_SED_THRESHOLD:
            self.skin_type = "III"

        self.training_start_wall = time.time()
        self.training_start_sensor_s = None
        self.last_sample_elapsed_s = None
        self.last_sample_wall = None

        self.uv_dose_sed = 0.0
        self.sunscreen_wall = None

        self.uv_avg = RollingAverage(UV_AVG_SECONDS)
        self.bpm_avg = RollingAverage(BPM_AVG_SECONDS)
        self.skin_avg = RollingAverage(SKIN_AVG_SECONDS)
        self.air_avg = RollingAverage(AIR_AVG_SECONDS)

        self.skin_baseline_values = []
        self.skin_baseline_c = None
        self.skin_trend_history = []

        # Hysterese: start veilig op GROEN. Verhoogde niveaus komen pas door na 60 s,
        # tenzij een directe escalatieregel actief is.
        self.stable_level_index = 0
        self.candidate_level_index = None
        self.candidate_since = None
        self.lower_since = None

        # Rust/herstelregel. De wearable heeft geen bewegingssensor, daarom wordt rust
        # handmatig gestart met het commando 'rust'.
        self.rest_active = False
        self.rest_start_wall = None
        self.rest_start_bpm_avg = None
        self.rest_warning_given = False

    # ---------- instellingen en reset ----------
    def set_skin_type(self, skin_type):
        """Pas het huidtype aan en reset de opgebouwde UV-dosis."""
        skin_type = skin_type.upper()
        if skin_type not in SKIN_SED_THRESHOLD:
            raise ValueError("Gebruik huidtype I, II, III, IV, V of VI.")
        self.skin_type = skin_type
        self.uv_dose_sed = 0.0
        self.sunscreen_wall = None
        print("# Huidtype ingesteld op", self.skin_type, "- UV-dosis is gereset.")

    def set_age(self, age_years):
        """Pas de leeftijd aan voor de berekening van de maximale hartslag."""
        self.age_years = int(age_years)
        print("# Leeftijd ingesteld op", self.age_years)

    def mark_sunscreen(self):
        """Registreer dat de gebruiker zich heeft ingesmeerd."""
        self.sunscreen_wall = time.time()
        print("# Ingesmeerd bevestigd. Insmeer-timer is opnieuw gestart.")

    def reset_uv_dose(self):
        """Zet alleen de cumulatieve UV-dosis terug naar nul."""
        self.uv_dose_sed = 0.0
        print("# UV-dosis gereset.")

    def reset_training(self):
        """Reset de hele training, inclusief gemiddelden en meldingsniveau."""
        self.training_start_wall = time.time()
        self.training_start_sensor_s = None
        self.last_sample_elapsed_s = None
        self.last_sample_wall = None
        self.uv_dose_sed = 0.0
        self.sunscreen_wall = None
        self.uv_avg.reset()
        self.bpm_avg.reset()
        self.skin_avg.reset()
        self.air_avg.reset()
        self.skin_baseline_values = []
        self.skin_baseline_c = None
        self.skin_trend_history = []
        self.stable_level_index = 0
        self.candidate_level_index = None
        self.candidate_since = None
        self.lower_since = None
        self.rest_active = False
        self.rest_start_wall = None
        self.rest_start_bpm_avg = None
        self.rest_warning_given = False
        print("# Training volledig gereset.")

    def start_rest(self):
        """Start een handmatige rustmeting voor hartslagherstel."""
        self.rest_active = True
        self.rest_start_wall = time.time()
        self.rest_start_bpm_avg = self.bpm_avg.average()
        self.rest_warning_given = False
        if self.rest_start_bpm_avg is None:
            print("# Rustmeting gestart. Start-BPM wordt vastgezet zodra BPM beschikbaar is.")
        else:
            print(f"# Rustmeting gestart bij BPMavg {self.rest_start_bpm_avg:.0f}.")

    def stop_rest(self):
        """Stop de handmatige rustmeting."""
        self.rest_active = False
        self.rest_start_wall = None
        self.rest_start_bpm_avg = None
        self.rest_warning_given = False
        print("# Rustmeting gestopt.")

    # ---------- tijd ----------
    def sample_elapsed_seconds(self, data, now_wall):
        """Gebruik bij voorkeur time_s uit de Pico, anders PC-walltime."""
        sensor_time_s = data.get("time_s")

        if sensor_time_s is not None:
            if self.training_start_sensor_s is None:
                self.training_start_sensor_s = sensor_time_s
            elapsed_s = sensor_time_s - self.training_start_sensor_s
            if elapsed_s < 0:
                elapsed_s = 0
            return elapsed_s

        return now_wall - self.training_start_wall

    def dt_minutes(self, elapsed_s, now_wall):
        """Bereken het tijdsverschil met de vorige meting in minuten."""
        if self.last_sample_elapsed_s is not None:
            dt_s = max(0.0, elapsed_s - self.last_sample_elapsed_s)
            self.last_sample_elapsed_s = elapsed_s
            self.last_sample_wall = now_wall
            return dt_s / 60.0

        if self.last_sample_wall is not None:
            dt_s = max(0.0, now_wall - self.last_sample_wall)
            self.last_sample_elapsed_s = elapsed_s
            self.last_sample_wall = now_wall
            return dt_s / 60.0

        self.last_sample_elapsed_s = elapsed_s
        self.last_sample_wall = now_wall
        return 0.0

    # ---------- UV / verbranding ----------
    def uv_class_score(self, uv_index):
        """Vertaal Tabel 1 naar de risiconiveaus uit Tabel 9."""
        if uv_index is None:
            return 0
        if uv_index < 3:
            return 0      # 0-2 laag -> groen
        if uv_index < 6:
            return 25     # 3-5 matig -> geel, zonbescherming aanbevolen
        if uv_index < 8:
            return 45     # 6-7 hoog -> oranje, actieve waarschuwing
        if uv_index < 11:
            return 65     # 8-10 zeer hoog -> rood, sterke waarschuwing
        return 80         # 11+ extreem -> kritiek

    def uv_message(self, uv_index):
        """Geef een korte tekstmelding die hoort bij de actuele UV-index."""
        if uv_index is None or uv_index < 3:
            return None
        if uv_index < 6:
            return "UV-index is verhoogd. Bescherm je huid."
        if uv_index < 8:
            return "Hoge UV-index. Smeer je in en bedek je huid."
        if uv_index < 11:
            return "Zeer hoge UV-index. Er is snel risico op verbranding."
        return "Extreem hoge UV-index. Vermijd langdurige directe zon."

    def reapply_sunscreen_interval_min(self, uv_index, adjusted_heat_c, hr_relative, heavy_sweat_likely):
        """Bepaal na hoeveel minuten opnieuw insmeren nodig is."""
        if uv_index is None or uv_index < 3:
            return None

        high_effort_warm = (
            adjusted_heat_c is not None
            and adjusted_heat_c >= 32
            and hr_relative is not None
            and hr_relative >= 0.75
        )

        if heavy_sweat_likely or high_effort_warm:
            return 60
        if uv_index >= 8:
            # Tabel 3 geeft 60-90 min. Voor dit prototype wordt de conservatieve ondergrens gebruikt.
            return 60
        if uv_index >= 6:
            return 90
        return 120

    # ---------- Heat Index / hittestress ----------
    def heat_index_c(self, temp_c, rh_pct):
        """Bereken de Heat Index in graden Celsius met de Rothfusz-formule."""
        if temp_c is None or rh_pct is None:
            return None

        t = temp_c * 9 / 5 + 32
        rh = rh_pct

        hi_f = (
            -42.379
            + 2.04901523 * t
            + 10.14333127 * rh
            - 0.22475541 * t * rh
            - 0.00683783 * t * t
            - 0.05481717 * rh * rh
            + 0.00122874 * t * t * rh
            + 0.00085282 * t * rh * rh
            - 0.00000199 * t * t * rh * rh
        )
        return (hi_f - 32) / 1.8

    def sun_correction_c(self, uv_index):
        """Tel een zoncorrectie op bij de Heat Index op basis van UV-index."""
        if uv_index is None:
            return 0
        if uv_index < 3:
            return 0
        if uv_index < 6:
            return 2
        if uv_index < 8:
            return 4
        if uv_index < 11:
            return 6
        return 8

    def environment_score(self, adjusted_heat_c):
        """Vertaal de gecorrigeerde warmtebelasting naar een score van 0 tot 100."""
        if adjusted_heat_c is None:
            return 0
        if adjusted_heat_c < 27:
            return 0
        if adjusted_heat_c < 32:
            return 25
        if adjusted_heat_c < 39:
            return 50
        if adjusted_heat_c <= 51:
            return 75
        return 100

    def heart_rate_score(self, bpm_avg):
        """Bereken de hartslagscore ten opzichte van de geschatte maximale hartslag."""
        if bpm_avg is None:
            return 0, None, None

        hr_max = 208 - 0.7 * self.age_years
        relative = bpm_avg / hr_max

        if relative < 0.65:
            score = 0
        elif relative < 0.75:
            score = 25
        elif relative < 0.85:
            score = 50
        elif relative <= 0.92:
            score = 75
        else:
            score = 100

        return score, relative, hr_max

    def skin_temperature_score(self, skin_avg_c, elapsed_s):
        """Bereken score en trend voor huidtemperatuur."""
        if skin_avg_c is None:
            return 0, None, None, False

        if self.skin_baseline_c is None:
            if elapsed_s <= BASELINE_SECONDS:
                self.skin_baseline_values.append(skin_avg_c)
                baseline_now = sum(self.skin_baseline_values) / len(self.skin_baseline_values)
            else:
                if self.skin_baseline_values:
                    self.skin_baseline_c = sum(self.skin_baseline_values) / len(self.skin_baseline_values)
                else:
                    self.skin_baseline_c = skin_avg_c
                baseline_now = self.skin_baseline_c
        else:
            baseline_now = self.skin_baseline_c

        delta = skin_avg_c - baseline_now

        if skin_avg_c >= 38.0:
            score = 100
        elif delta > 3.0:
            score = 75
        elif delta >= 2.0:
            score = 50
        elif delta >= 1.0:
            score = 25
        else:
            score = 0

        self.skin_trend_history.append((elapsed_s, skin_avg_c))
        cutoff = elapsed_s - max(FAST_SKIN_RISE_WINDOW_SECONDS * 3, 180)
        self.skin_trend_history = [(t, v) for (t, v) in self.skin_trend_history if t >= cutoff]

        fast_rise = False
        target_oldest = None
        for t, v in self.skin_trend_history:
            if elapsed_s - t >= FAST_SKIN_RISE_WINDOW_SECONDS:
                target_oldest = (t, v)
                break
        if target_oldest is not None:
            old_t, old_v = target_oldest
            rise = skin_avg_c - old_v
            fast_rise = rise >= FAST_SKIN_RISE_MIN_C

        return score, delta, baseline_now, fast_rise

    def duration_score(self, adjusted_heat_c, elapsed_s):
        """Geef extra score als warme omstandigheden langer aanhouden."""
        if adjusted_heat_c is None or adjusted_heat_c < 32:
            return 0

        minutes = elapsed_s / 60.0
        if minutes < 20:
            return 0
        if minutes < 40:
            return 25
        if minutes < 60:
            return 50
        if minutes <= 90:
            return 75
        return 100

    # ---------- risiconiveaus / hysterese ----------
    def score_to_level(self, score):
        """Zet een numerieke risicoscore om naar een kleur/niveau."""
        if score < 25:
            return 0, "GROEN", "Laag risico"
        if score < 45:
            return 1, "GEEL", "Licht verhoogd risico"
        if score < 65:
            return 2, "ORANJE", "Matig risico"
        if score < 80:
            return 3, "ROOD", "Hoog risico"
        return 4, "KRITIEK", "Zeer hoog risico"

    def apply_hysteresis(self, raw_level_index, direct_escalation, now_wall):
        """Voorkom dat het risiconiveau bij kleine schommelingen steeds wisselt."""
        current = self.stable_level_index

        if raw_level_index > current:
            if direct_escalation:
                self.stable_level_index = raw_level_index
                self.candidate_level_index = None
                self.candidate_since = None
                self.lower_since = None
                return self.stable_level_index

            if self.candidate_level_index != raw_level_index:
                self.candidate_level_index = raw_level_index
                self.candidate_since = now_wall

            if now_wall - self.candidate_since >= RAISE_CONFIRM_SECONDS:
                self.stable_level_index = raw_level_index
                self.candidate_level_index = None
                self.candidate_since = None

            self.lower_since = None
            return self.stable_level_index

        if raw_level_index < current:
            if self.lower_since is None:
                self.lower_since = now_wall

            if now_wall - self.lower_since >= LOWER_CONFIRM_SECONDS:
                self.stable_level_index = raw_level_index
                self.lower_since = None

            self.candidate_level_index = None
            self.candidate_since = None
            return self.stable_level_index

        self.candidate_level_index = None
        self.candidate_since = None
        self.lower_since = None
        return self.stable_level_index

    # ---------- speciale regels ----------
    def heavy_sweat_likely(self, hr_relative, adjusted_heat_c, rv_pct, fast_skin_rise):
        """Benadering van veel zweten volgens de risicoanalyse."""
        return (
            hr_relative is not None
            and hr_relative >= 0.75
            and adjusted_heat_c is not None
            and adjusted_heat_c >= 32
            and rv_pct is not None
            and rv_pct >= 70
            and fast_skin_rise
        )

    def rest_recovery_poor(self, bpm_avg, hr_relative, now_wall):
        """Directe escalatieregel: hartslag daalt nauwelijks tijdens rust.

        Omdat de wearable geen rust/bewegingssensor heeft, wordt rust handmatig gestart
        met het commando 'rust'.
        """
        if not self.rest_active:
            return False

        if self.rest_start_bpm_avg is None and bpm_avg is not None:
            self.rest_start_bpm_avg = bpm_avg
            self.rest_start_wall = now_wall
            return False

        if self.rest_start_wall is None or self.rest_start_bpm_avg is None or bpm_avg is None:
            return False

        if now_wall - self.rest_start_wall < REST_RECOVERY_CHECK_SECONDS:
            return False

        drop = self.rest_start_bpm_avg - bpm_avg
        still_high = hr_relative is not None and hr_relative >= 0.75
        poor = drop < REST_MIN_EXPECTED_DROP_BPM and still_high

        if poor and not self.rest_warning_given:
            self.rest_warning_given = True
            return True

        return False

    # ---------- update ----------
    def update(self, data):
        """Verwerk één sensorregel en geef alle scores en meldingen terug."""
        now_wall = time.time()
        elapsed_s = self.sample_elapsed_seconds(data, now_wall)

        bpm_raw = data["bpm"]
        uv_raw = data["uv_index"]
        skin_raw_c = data["obj_c"]
        air_raw_c = data["sht_c"]
        rv_pct = data["rv_pct"]

        dt_min = self.dt_minutes(elapsed_s, now_wall)

        # Rollende gemiddelden uit de risicoanalyse.
        # UV wordt ook gemiddeld, zodat een onstabiele UV-sensor niet steeds
        # tussen twee meldingsklassen heen en weer springt.
        uv_avg = self.uv_avg.add(elapsed_s, uv_raw)
        bpm_avg = self.bpm_avg.add(elapsed_s, bpm_raw)
        skin_avg_c = self.skin_avg.add(elapsed_s, skin_raw_c)
        air_avg_c = self.air_avg.add(elapsed_s, air_raw_c)

        # UV-dosis: Dnieuw = Doud + 0,015 * UVI * Δt.
        # Hiervoor gebruiken we de gefilterde UV-index, omdat de risicoanalyse
        # meetwaarden filtert en de LTR390 in de praktijk kan schommelen.
        if uv_avg is not None and uv_avg > 0:
            self.uv_dose_sed += 0.015 * uv_avg * dt_min

        sed_threshold = SKIN_SED_THRESHOLD.get(self.skin_type, SKIN_SED_THRESHOLD["III"])
        uv_dose_pct = 100.0 * self.uv_dose_sed / sed_threshold
        uv_dose_score = clamp(uv_dose_pct, 0, 100)
        uv_class_score = self.uv_class_score(uv_avg)
        burn_score = clamp(max(uv_dose_score, uv_class_score), 0, 100)

        heat_index = self.heat_index_c(air_avg_c, rv_pct)
        adjusted_heat = None
        if heat_index is not None:
            adjusted_heat = heat_index + self.sun_correction_c(uv_avg)

        env_score = self.environment_score(adjusted_heat)
        hr_score, hr_relative, hr_max = self.heart_rate_score(bpm_avg)
        skin_score, skin_delta, skin_baseline, fast_skin_rise = self.skin_temperature_score(skin_avg_c, elapsed_s)
        dur_score = self.duration_score(adjusted_heat, elapsed_s)

        # Exacte weging uit de risicoanalyse
        heatstress_score = (
            0.40 * env_score
            + 0.35 * hr_score
            + 0.20 * skin_score
            + 0.05 * dur_score
        )

        messages = []
        direct_escalation = False

        uv_msg = self.uv_message(uv_avg)
        if uv_msg:
            messages.append(uv_msg)

        # Herinnering opnieuw insmeren / geen insmeren bevestigd
        if uv_avg is not None and uv_avg >= 3 and self.sunscreen_wall is None:
            messages.append("Geen insmeren bevestigd. Typ 's' zodra je zonnebrand hebt aangebracht.")

        # Directe escalatieregel: UV-index ≥8 zonder insmeren
        if uv_avg is not None and uv_avg >= 8 and self.sunscreen_wall is None:
            messages.append("UV-index ≥8 zonder bevestigde zonnebrand. Zoek schaduw of smeer direct in.")
            burn_score = max(burn_score, 65)
            direct_escalation = True

        # Hitte-/zweet-inschatting voor opnieuw insmeren
        heavy_sweat = self.heavy_sweat_likely(hr_relative, adjusted_heat, rv_pct, fast_skin_rise)
        interval_min = self.reapply_sunscreen_interval_min(uv_avg, adjusted_heat, hr_relative, heavy_sweat)
        if interval_min is not None and self.sunscreen_wall is not None:
            since_sunscreen_min = (now_wall - self.sunscreen_wall) / 60
            if since_sunscreen_min >= interval_min:
                messages.append("Je bent lang genoeg in de zon geweest. Smeer opnieuw in.")
                burn_score = max(burn_score, 45)

        if heavy_sweat and uv_avg is not None and uv_avg >= 3:
            messages.append("Kans op veel zweten is verhoogd. Controleer je zonnebrandbescherming.")

        # Directe escalatieregels UV-dosis
        if 75 <= uv_dose_pct < 100:
            messages.append("UV-dosis bereikt 75% van je persoonlijke drempel. Smeer opnieuw in of zoek schaduw.")
            burn_score = max(burn_score, 65)
            direct_escalation = True

        if uv_dose_pct >= 100:
            messages.append("Persoonlijke UV-drempel bereikt. Vermijd directe zon.")
            burn_score = max(burn_score, 80)
            direct_escalation = True

        # Meldingen hittestress
        if adjusted_heat is not None and adjusted_heat >= 32:
            messages.append("Hittestressrisico loopt op. Neem rust en drink water.")

        # Directe escalatieregel: HIadj ≥39 °C en HRrel ≥85%
        if adjusted_heat is not None and adjusted_heat >= 39 and hr_relative is not None and hr_relative >= 0.85:
            messages.append("HIadj ≥39 °C en hartslag ≥85%. Stop tijdelijk en zoek schaduw.")
            heatstress_score = max(heatstress_score, 65)
            direct_escalation = True

        # Directe escalatieregel: HIadj ≥32 °C, HRrel ≥85% en Tskin ≥37 °C
        if (
            adjusted_heat is not None
            and adjusted_heat >= 32
            and hr_relative is not None
            and hr_relative >= 0.85
            and skin_avg_c is not None
            and skin_avg_c >= 37.0
        ):
            messages.append("Warme omstandigheden, hoge hartslag en huidtemperatuur ≥37 °C. Neem direct rust.")
            heatstress_score = max(heatstress_score, 65)
            direct_escalation = True

        # Directe escalatieregel: huidtemperatuur stijgt snel terwijl hartslag hoog blijft
        if fast_skin_rise and hr_relative is not None and hr_relative >= 0.85:
            messages.append("Huidtemperatuur stijgt snel terwijl de hartslag hoog blijft. Stop tijdelijk en koel af.")
            heatstress_score = max(heatstress_score, 65)
            direct_escalation = True

        # Directe escalatieregel: hartslag daalt nauwelijks tijdens rust
        poor_recovery = self.rest_recovery_poor(bpm_avg, hr_relative, now_wall)
        if poor_recovery:
            messages.append("Hartslag daalt nauwelijks tijdens rust. Stop en koel af.")
            heatstress_score = max(heatstress_score, 65)
            direct_escalation = True

        if skin_avg_c is not None and skin_avg_c >= 38:
            messages.append("Huidtemperatuur is 38 °C of hoger. Let op: dit is geen kerntemperatuurmeting.")

        total_score = clamp(max(burn_score, heatstress_score), 0, 100)
        raw_level_index, raw_level_name, raw_level_text = self.score_to_level(total_score)

        stable_index = self.apply_hysteresis(raw_level_index, direct_escalation, now_wall)
        stable_level_name = LEVEL_NAMES[stable_index]

        if stable_index >= 4:
            messages.append("Kritiek risico. Stop met sporten en koel af.")
        elif stable_index >= 3 and not any("Stop" in m or "direct" in m for m in messages):
            messages.append("Hoog risico. Stop tijdelijk en zoek schaduw.")
        elif stable_index >= 2 and not messages:
            messages.append("Waarschuwing: matig risico.")
        elif stable_index >= 1 and not messages:
            messages.append("Let op: risico is licht verhoogd.")

        if not messages:
            messages.append("Geen melding nodig.")

        return {
            "time_s": data["time_s"],
            "elapsed_s": elapsed_s,
            "time_min": None if data["time_s"] is None else data["time_s"] / 60.0,
            "elapsed_min": elapsed_s / 60.0,
            "bpm": bpm_raw,
            "bpm_avg_30s": bpm_avg,
            "uv_index": uv_raw,
            "uv_index_avg_30s": uv_avg,
            "uv_index_for_risk": uv_avg,
            "uv_class_score": uv_class_score,
            "lux": data["lux"],
            "obj_c": skin_raw_c,
            "obj_c_avg_60s": skin_avg_c,
            "ir_amb_c": data["ir_amb_c"],
            "sht_c": air_raw_c,
            "sht_c_raw": data.get("sht_c_raw"),
            "sht_c_avg_60s": air_avg_c,
            "rv_pct": rv_pct,
            "heat_index_c": heat_index,
            "adjusted_heat_c": adjusted_heat,
            "env_score": env_score,
            "hr_score": hr_score,
            "hr_relative": hr_relative,
            "hr_max": hr_max,
            "skin_score": skin_score,
            "skin_delta": skin_delta,
            "skin_baseline": skin_baseline,
            "fast_skin_rise": fast_skin_rise,
            "duration_score": dur_score,
            "burn_score": burn_score,
            "uv_dose_score": uv_dose_score,
            "heatstress_score": heatstress_score,
            "total_score": total_score,
            "raw_level_name": raw_level_name,
            "stable_level_name": stable_level_name,
            "stable_level_index": stable_index,
            "direct_escalation": direct_escalation,
            "heavy_sweat_likely": heavy_sweat,
            "rest_active": self.rest_active,
            "poor_rest_recovery": poor_recovery,
            "messages": messages,
            "uv_dose_sed": self.uv_dose_sed,
            "uv_dose_pct": uv_dose_pct,
            "sunscreen_confirmed": self.sunscreen_wall is not None,
        }


# -----------------------------
# CSV-regels van Pico parsen
# -----------------------------
def parse_sensor_line(line):
    """Parse één CSV-regel van de Pico naar een dictionary met floats."""
    line = line.strip()

    if not line or line.startswith("#"):
        return None

    if line.lower().startswith("time_s"):
        return None

    parts = line.split(",")

    if len(parts) < 8:
        print("# Onvolledige regel:", line)
        return None

    sht_c_raw = parse_float(parts[6])
    sht_c_corrected = None if sht_c_raw is None else sht_c_raw + SHT_TEMP_OFFSET_C

    return {
        "time_s": parse_float(parts[0]),
        "bpm": parse_float(parts[1]),
        "lux": parse_float(parts[2]),
        "uv_index": parse_float(parts[3]),
        "obj_c": parse_float(parts[4]),
        "ir_amb_c": parse_float(parts[5]),
        "sht_c": sht_c_corrected,
        "sht_c_raw": sht_c_raw,
        "rv_pct": parse_float(parts[7]),
    }


def print_result(result):
    """Print een compacte samenvatting van de laatste risicoanalyse."""
    level = result["stable_level_name"]
    if level in ("ORANJE", "ROOD", "KRITIEK"):
        bell()

    t = format_duration(result["time_s"] or result["elapsed_s"] or 0)
    bpm = "-" if result["bpm_avg_30s"] is None else str(round(result["bpm_avg_30s"]))
    uvi_raw = "-" if result["uv_index"] is None else f"{result['uv_index']:.2f}"
    uvi_avg = "-" if result["uv_index_avg_30s"] is None else f"{result['uv_index_avg_30s']:.2f}"
    temp = "-" if result["sht_c_avg_60s"] is None else f"{result['sht_c_avg_60s']:.1f}"
    rh = "-" if result["rv_pct"] is None else f"{result['rv_pct']:.0f}"
    skin = "-" if result["obj_c_avg_60s"] is None else f"{result['obj_c_avg_60s']:.1f}"
    adj_heat = "-" if result["adjusted_heat_c"] is None else f"{result['adjusted_heat_c']:.1f}"

    print(
        f"[{t}] {level:<7} score {result['total_score']:.0f} "
        f"| BPMavg {bpm} | RH {rh}% | UVI raw/avg {uvi_raw}/{uvi_avg} | huidavg {skin} °C "
        f"| omgevavg {temp} °C | warmtebelasting {adj_heat} °C"
    )


# -----------------------------
# Grafieken en bestanden opslaan
# -----------------------------
def _clean_xy(records, x_key, y_key):
    """Haal x- en y-waarden uit records en sla lege waarden over."""
    xs, ys = [], []
    for r in records:
        x = r.get(x_key)
        y = r.get(y_key)
        if x is not None and y is not None:
            xs.append(x)
            ys.append(y)
    return xs, ys


def save_plots(records, out_dir):
    """Maak grafieken van de meting en sla ze op als PNG-bestanden."""
    if not records:
        print("# Geen data om grafieken te maken.")
        return

    os.makedirs(out_dir, exist_ok=True)

    # 1. RH
    x, y = _clean_xy(records, "elapsed_min", "rv_pct")
    plt.figure(figsize=(10, 5))
    plt.plot(x, y)
    plt.xlabel("Tijd vanaf start/reset (min)")
    plt.ylabel("Relatieve luchtvochtigheid (%)")
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "grafiek_rh.png"), dpi=200)
    plt.close()

    # 2. BPM raw + BPMavg
    x1, y1 = _clean_xy(records, "elapsed_min", "bpm")
    x2, y2 = _clean_xy(records, "elapsed_min", "bpm_avg_30s")
    plt.figure(figsize=(10, 5))
    plt.plot(x1, y1, label="BPM raw")
    plt.plot(x2, y2, label="BPMavg 30 s")
    plt.xlabel("Tijd vanaf start/reset (min)")
    plt.ylabel("Hartslag (BPM)")
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "grafiek_bpm.png"), dpi=200)
    plt.close()

    # 3. UV-index raw + gemiddeld + UV-dosis
    x1, y1 = _clean_xy(records, "elapsed_min", "uv_index")
    x2, y2 = _clean_xy(records, "elapsed_min", "uv_index_avg_30s")
    plt.figure(figsize=(10, 5))
    plt.plot(x1, y1, label="UV-index raw")
    plt.plot(x2, y2, label="UV-index avg 30 s")
    plt.xlabel("Tijd vanaf start/reset (min)")
    plt.ylabel("UV-index")
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "grafiek_uv_index.png"), dpi=200)
    plt.close()

    x, y = _clean_xy(records, "elapsed_min", "uv_dose_pct")
    plt.figure(figsize=(10, 5))
    plt.plot(x, y)
    plt.xlabel("Tijd vanaf start/reset (min)")
    plt.ylabel("UV-dosis (% van persoonlijke drempel)")
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "grafiek_uv_dosis_pct.png"), dpi=200)
    plt.close()

    # 4. Huidtemperatuur + omgevingstemperatuur raw/avg
    x1, y1 = _clean_xy(records, "elapsed_min", "obj_c")
    x2, y2 = _clean_xy(records, "elapsed_min", "obj_c_avg_60s")
    x3, y3 = _clean_xy(records, "elapsed_min", "sht_c")
    x4, y4 = _clean_xy(records, "elapsed_min", "sht_c_avg_60s")
    plt.figure(figsize=(10, 5))
    plt.plot(x1, y1, label="Huidtemperatuur raw (°C)")
    plt.plot(x2, y2, label="Huidtemperatuur avg 60 s (°C)")
    plt.plot(x3, y3, label="Omgevingstemperatuur raw (°C)")
    plt.plot(x4, y4, label="Omgevingstemperatuur avg 60 s (°C)")
    plt.xlabel("Tijd vanaf start/reset (min)")
    plt.ylabel("Temperatuur (°C)")
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "grafiek_temperaturen.png"), dpi=200)
    plt.close()

    # 5. Scores
    for key, label, filename in [
        ("burn_score", "Verbrandingsscore", "grafiek_verbrandingsscore.png"),
        ("heatstress_score", "Hittestressscore", "grafiek_hittestressscore.png"),
        ("total_score", "Totale risicoscore", "grafiek_totale_score.png"),
    ]:
        x, y = _clean_xy(records, "elapsed_min", key)
        plt.figure(figsize=(10, 5))
        plt.plot(x, y)
        plt.xlabel("Tijd vanaf start/reset (min)")
        plt.ylabel(label)
        plt.grid(True)
        plt.tight_layout()
        plt.savefig(os.path.join(out_dir, filename), dpi=200)
        plt.close()

    # 6. Risiconiveau
    level_to_num = {"GROEN": 0, "GEEL": 1, "ORANJE": 2, "ROOD": 3, "KRITIEK": 4}
    times = [r["elapsed_min"] for r in records if r.get("elapsed_min") is not None and r.get("stable_level_name") is not None]
    levels = [r["stable_level_name"] for r in records if r.get("elapsed_min") is not None and r.get("stable_level_name") is not None]
    y_nums = [level_to_num[l] for l in levels]
    plt.figure(figsize=(11, 5))
    plt.step(times, y_nums, where="post")
    plt.yticks([0, 1, 2, 3, 4], ["GROEN", "GEEL", "ORANJE", "ROOD", "KRITIEK"])
    plt.xlabel("Tijd vanaf start/reset (min)")
    plt.ylabel("Risiconiveau")
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "grafiek_risiconiveau.png"), dpi=200)
    plt.close()

    print("# Grafieken opgeslagen in:", out_dir)


def save_csv(records, out_dir):
    """Sla alle meet- en risicoresultaten op als CSV-bestand."""
    if not records:
        return None

    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "metingen_en_risicoanalyse.csv")

    fieldnames = [
        "time_s", "elapsed_s", "time_min", "elapsed_min",
        "bpm", "bpm_avg_30s", "lux", "uv_index", "uv_index_avg_30s", "uv_index_for_risk", "uv_class_score",
        "obj_c", "obj_c_avg_60s", "ir_amb_c", "sht_c_raw", "sht_c", "sht_c_avg_60s", "rv_pct",
        "heat_index_c", "adjusted_heat_c", "env_score", "hr_score", "hr_relative", "hr_max",
        "skin_score", "skin_delta", "skin_baseline", "fast_skin_rise", "duration_score",
        "burn_score", "uv_dose_score", "heatstress_score", "total_score",
        "raw_level_name", "stable_level_name", "stable_level_index", "direct_escalation",
        "uv_dose_sed", "uv_dose_pct", "sunscreen_confirmed", "heavy_sweat_likely",
        "rest_active", "poor_rest_recovery",
        "message_1", "message_2", "message_3", "message_4", "message_5", "message_6",
    ]

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in records:
            row = dict(r)
            msgs = row.pop("messages", [])
            for i in range(6):
                row[f"message_{i + 1}"] = msgs[i] if len(msgs) > i else ""
            writer.writerow({k: row.get(k, "") for k in fieldnames})

    print("# CSV opgeslagen:", csv_path)
    return csv_path


def build_message_events(records):
    """Compact meldingenoverzicht: alleen nieuwe combinaties opnemen."""
    events = []
    previous_key = None

    for r in records:
        messages = r.get("messages", [])
        filtered_messages = [m for m in messages if m != "Geen melding nodig."]
        if not filtered_messages:
            continue

        key = (r.get("stable_level_name"), tuple(filtered_messages))
        if key == previous_key:
            continue
        previous_key = key

        for msg in filtered_messages:
            events.append({
                "time_s": r.get("time_s"),
                "elapsed_s": r.get("elapsed_s"),
                "time_min": r.get("time_min"),
                "elapsed_min": r.get("elapsed_min"),
                "tijd": format_duration(r.get("time_s") or r.get("elapsed_s") or 0),
                "risiconiveau": r.get("stable_level_name"),
                "score": r.get("total_score"),
                "melding": msg,
            })

    return events


def save_message_log(records, out_dir):
    """Sla een compact meldingenlog op als CSV en TXT."""
    events = build_message_events(records)

    csv_path = os.path.join(out_dir, "meldingen_log.csv")
    txt_path = os.path.join(out_dir, "meldingen_overzicht.txt")

    fieldnames = ["tijd", "time_s", "elapsed_s", "time_min", "elapsed_min", "risiconiveau", "score", "melding"]

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for e in events:
            writer.writerow({
                "tijd": e["tijd"],
                "time_s": "" if e["time_s"] is None else round(e["time_s"], 1),
                "elapsed_s": "" if e["elapsed_s"] is None else round(e["elapsed_s"], 1),
                "time_min": "" if e["time_min"] is None else round(e["time_min"], 2),
                "elapsed_min": "" if e["elapsed_min"] is None else round(e["elapsed_min"], 2),
                "risiconiveau": e["risiconiveau"],
                "score": "" if e["score"] is None else round(e["score"], 1),
                "melding": e["melding"],
            })

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("Meldingen tijdens de training\n")
        f.write("=" * 34 + "\n\n")
        if not events:
            f.write("Er zijn tijdens deze meting geen waarschuwingen of informerende meldingen gegeven.\n")
        else:
            for e in events:
                score_text = "-" if e["score"] is None else str(round(e["score"]))
                f.write(f"{e['tijd']} | {e['risiconiveau']} | score {score_text} | {e['melding']}\n")

    print("# Meldingenlog opgeslagen:", csv_path)
    print("# Meldingenoverzicht opgeslagen:", txt_path)
    return csv_path, txt_path


# -----------------------------
# Consolecommando's
# -----------------------------
def start_input_thread(cmd_queue):
    """Start een aparte thread zodat consolecommando's de BLE-loop niet blokkeren."""
    def worker():
        while True:
            try:
                cmd = input().strip()
                cmd_queue.put(cmd)
            except EOFError:
                return
            except Exception:
                return

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()


class SessionController:
    """Verwerkt commando's die tijdens de meting in de terminal worden getypt."""

    def __init__(self):
        """Houd bij of de gebruiker de sessie wil stoppen."""
        self.stop_requested = False

    def handle_command(self, cmd, analyzer):
        """Voer één gebruikerscommando uit, zoals 's', 'rust' of 'stop'."""
        if not cmd:
            return

        lower = cmd.lower()

        if lower in ("help", "?"):
            print()
            print("Commando's:")
            print("  s              = insmeren bevestigd")
            print("  rust           = rust/herstelmeting starten")
            print("  doorgaan       = rust/herstelmeting stoppen")
            print("  resetuv        = UV-dosis resetten")
            print("  reset          = hele training resetten")
            print("  age 18         = leeftijd instellen")
            print("  skin III       = huidtype instellen: I, II, III, IV, V of VI")
            print("  stop           = sessie stoppen en grafieken opslaan")
            print()
            return

        if lower in ("s", "smeer", "sunscreen", "insmeren"):
            analyzer.mark_sunscreen()
            return

        if lower in ("rust", "rest"):
            analyzer.start_rest()
            return

        if lower in ("doorgaan", "resume", "start", "sport"):
            analyzer.stop_rest()
            return

        if lower == "resetuv":
            analyzer.reset_uv_dose()
            return

        if lower == "reset":
            analyzer.reset_training()
            return

        if lower.startswith("age "):
            try:
                analyzer.set_age(int(lower.split()[1]))
            except Exception as e:
                print("# Leeftijd niet aangepast:", e)
            return

        if lower.startswith("skin "):
            try:
                analyzer.set_skin_type(cmd.split()[1])
            except Exception as e:
                print("# Huidtype niet aangepast:", e)
            return

        if lower == "stop":
            self.stop_requested = True
            print("# Stop aangevraagd. Verbinding wordt netjes afgesloten...")
            return

        print("# Onbekend commando. Typ 'help' voor opties.")


# -----------------------------
# BLE ontvangen met Bleak
# -----------------------------
async def find_wearable():
    """Zoek via Bleak naar het BLE-apparaat met naam WearableRisk."""
    print("# Scannen naar BLE-apparaat:", DEVICE_NAME)
    devices = await BleakScanner.discover(timeout=8)

    for device in devices:
        if device.name == DEVICE_NAME:
            print("# Gevonden:", device.name, device.address)
            return device

    print("# Niet gevonden. Gevonden apparaten:")
    for device in devices:
        print("  -", device.name, device.address)

    return None


async def run_session():
    """Verbind met de Pico, ontvang BLE-data en voer de risicoanalyse uit."""
    analyzer = RiskAnalyzer()
    controller = SessionController()
    cmd_queue = queue.Queue()
    start_input_thread(cmd_queue)

    records = []

    print("# Typ 'help' voor commando's.")
    print("# Typ 'stop' als jullie training klaar is en je grafieken wilt opslaan.")
    print("# Zorg dat de Pico W aan staat en main.py draait.")

    while True:
        device = await find_wearable()

        if device is None:
            print("# Nieuwe poging over 5 seconden...")
            await asyncio.sleep(5)
            continue

        buffer = bytearray()

        def on_notify(sender, data):
            """Callback die door Bleak wordt aangeroepen bij nieuwe BLE-data."""
            nonlocal buffer, records
            buffer.extend(data)

            while b"\n" in buffer:
                raw_line, _, rest = buffer.partition(b"\n")
                buffer = bytearray(rest)

                try:
                    line = raw_line.decode().strip()
                except UnicodeDecodeError:
                    continue

                sensor_data = parse_sensor_line(line)
                if sensor_data is None:
                    continue

                result = analyzer.update(sensor_data)
                records.append(result)
                print_result(result)

        try:
            async with BleakClient(device) as client:
                print("# Verbonden met WearableRisk")
                await client.start_notify(UART_TX_UUID, on_notify)

                try:
                    await client.write_gatt_char(UART_RX_UUID, b"pc_connected\n", response=False)
                except Exception:
                    pass

                while client.is_connected and not controller.stop_requested:
                    try:
                        while True:
                            cmd = cmd_queue.get_nowait()
                            controller.handle_command(cmd, analyzer)
                    except queue.Empty:
                        pass

                    await asyncio.sleep(0.2)

                if controller.stop_requested:
                    try:
                        await client.stop_notify(UART_TX_UUID)
                    except Exception:
                        pass
                    return records

        except Exception as e:
            print("# BLE-verbinding fout:", e)
            print("# Opnieuw verbinden over 5 seconden...")
            await asyncio.sleep(5)


def main():
    """Start de sessie en sla na afloop CSV, meldingen en grafieken op."""
    session_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(os.getcwd(), f"training_risicoanalyse_{session_stamp}")

    records = []
    try:
        records = asyncio.run(run_session())
    except KeyboardInterrupt:
        print("\n# Gestopt met Ctrl+C")

    if records:
        save_csv(records, out_dir)
        save_message_log(records, out_dir)
        save_plots(records, out_dir)
    else:
        print("# Geen records opgeslagen.")


if __name__ == "__main__":
    main()
