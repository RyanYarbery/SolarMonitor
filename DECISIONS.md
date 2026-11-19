# Decisions

Why things are the way they are. Read this before changing anything that looks deliberate. Reverse an entry only with a newer word from Ryan, a new fact that breaks its premise, a safety problem or a broken build, and record the reversal here.

## 2026-11-19: Battery SOC comes from the BMS via the inverters; no software charge counter

**Who decided:** Ryan, from two choices offered in session. He picked "Restore BMS link + detector" after his first pick, "Software charge counter", was tested and failed (below).

**Problem:** Ryan reported "The monitor isn't tracking battery level properly."

**What the data showed** (raw samples 2026-11-12 to 2026-11-19, pulled from the Pi):
- Until 2026-11-14T03:34Z (19:34 PT on Nov 13), the inverters reported the BMS's SOC. It fell smoothly while the voltage was flat and didn't jump with load.
- At that moment it snapped onto a straight line in voltage, SOC ≈ 10 × (V − 45.2), and has followed it since. It jumps 8 to 15 points with every load or charge step, flickers to a false 100% while charging, and read 0% at 45 V this morning. The switch came during AC-input on/off events (a generator start) and two manual collector restarts, so the BMS comms cable or the battery-type setting most likely changed then.
- An amp-hour counter on the inverters' current readings (whole amps per unit, idle draw left out) was replayed against the 42 hours of real BMS SOC. Untuned, it was 27 points off after one night. With the best constant correction (−2.5 A) it was still 13 to 16 points off, and the bank went 7 days without a full charge to reset it.

**Decided:**
- Ryan restores the BMS link at the inverters: the comms cable and the battery type setting.
- The collector detects which source the SOC is coming from (`SocSourceDetector` in `collector.py`), raises a `soc_estimated` alert when it becomes a voltage estimate, and the dashboard labels the % "Voltage estimate".
- `system_samples.battery_soc` keeps whatever the inverters report. No software SOC replaces it.
- `battery_capacity_kwh` is set to 20.48 (4 × EG4 LifePower4, 48 V 100 Ah; Ryan's answer).

**Replaces:** nothing in code. Before this, the inverters' SOC was shown without any check of where it came from.

**Revisit if:** the inverters can't talk to these batteries' BMS at all; a current sensor (shunt) with sub-amp resolution is added; or the BMS is read directly over RS485 (`battery_samples` is the table reserved for that).

## 2026-11-19: Alert trackers restore their state from `alert_state` on startup

**Who decided:** decided in session, Ryan to confirm (it was included in the option he picked).

**Why:** the Pi loses power when the bank reaches the inverters' low-voltage cutoff. A fresh `LowSocTracker` started with `low=False` on every reboot, so "Battery low" fired again after each one and the earlier alert was never cleared. On 2026-11-19 that left two open alerts and no recovery. `SocSourceDetector` is seeded the same way. `GridTracker` is seeded with `present=True` while the `grid_present` alert is active, so AC that went away during a restart is reported as off. On 2026-11-19 "Utility power on" had stayed active from 05:12 with 0 V at the input. It is not seeded when the alert is inactive, so a first start stays silent as before.

**Revisit if:** the Pi gets power that survives the inverters shutting down; then a reboot no longer coincides with low battery.
