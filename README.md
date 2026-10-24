# SolarMonitor

SolarMonitor is a lightweight local telemetry, aggregation, retention, and dashboard stack for EG4 off-grid solar systems.

It runs continuously on low-resource hardware, collecting live data from two EG4 6500EX-48 inverters over the PI30 serial protocol. Raw telemetry is stored in SQLite, rolled into multiple historical resolutions, and served through a Flask dashboard.

## Goals

- Local-first monitoring with no cloud dependency
- Reliable telemetry from parallel EG4 inverters
- Multi-year history without unbounded database growth
- Low SQLite contention between collection and aggregation
- A lightweight browser dashboard suitable for old Raspberry Pi hardware
- Clear separation between raw measurements, derived summaries, and presentation

## Current hardware target

- 2 x EG4 6500EX-48 inverters in parallel
- EG4 LifePower battery bank
- Raspberry Pi 1 Model B Rev 2
- USB FTDI serial adapter
- Raspbian / Debian-based Linux, Python 3.9 or newer

## Architecture

```text
EG4 inverter pair
       |
       | PI30 serial (eg4.py: framing, CRC, parsers)
       v
  collector.py ---> /run/solar-monitor/latest.json (live snapshot)
       |          --> ntfy (faults, low battery, telemetry loss)
       v
  solar_v1.db  (system_samples, inverter_samples, events, weather_samples)
       ^
       |  weather.py (hourly timer, Open-Meteo irradiance)
       |
       v
  aggregator.py (systemd timer, every 5 min)
       +-- minute summaries
       +-- 15-minute summaries
       +-- daily summaries (system and per inverter)
       +-- monthly / yearly summaries
       +-- guarded retention
       |
       v
     app.py  ->  Flask dashboard + JSON API
```

| File | Purpose |
|---|---|
| `settings.py` | All operational settings, each overridable by environment variable |
| `timeutil.py` | Timestamp conventions (UTC storage, local calendar days) |
| `eg4.py` | PI30 framing with CRC verification, pure parsers, persistent serial link |
| `collector.py` | Sampling loop, event detection, live snapshot |
| `aggregator.py` | Incremental roll-ups and retention |
| `app.py` | Dashboard and API |
| `init_db.py` | Creates a schema v3 database |
| `migrate_v3.py` | Upgrades a v1/v2 database in place (with backup) |
| `configure.py` | Reads or sets plant facts (array size, battery capacity, site coordinates) |
| `alerts.py` | ntfy notifications with cooldowns shared between processes |
| `weather.py` | Hourly irradiance from Open-Meteo for performance ratios |
| `backup.py` | Verified, compressed, rotated SQLite backups |
| `sdnotify.py` | systemd readiness and watchdog pings |
| `probe_inverter.py` | Dumps raw frames for diagnosis |
| `tests/` | Unit tests, no hardware needed |

## Telemetry collection

`collector.py` holds the serial port open for its lifetime and queries:

- `QPGS0` and `QPGS1` for both parallel inverters (mode, fault code, output, battery, PV voltages and currents)
- `QPIGS` and `QPIGS2` for the directly connected inverter's measured PV charging power on both inputs

Inverter 1's PV power is therefore *measured*; inverter 2's is *estimated* from PV voltage times current. The dashboard labels each. Both PV strings on each inverter are stored separately (voltage, current, watts) so a shaded or degraded string shows up in history.

Every response frame's CRC is verified. A corrupt frame is rejected and the cycle retried rather than stored. Each successful cycle writes two inverter rows and one system row in one short transaction, then updates the live JSON snapshot.

Grid power is not reported by `QPGS` in a trustworthy form, so `grid_watts` is stored as NULL rather than a misleading zero.

### Events and alerts

Mode changes and fault code transitions per inverter are written to the `events` table, as are telemetry loss (after six consecutive failed reads), recovery, and battery SOC dropping below `low_soc_alert_percent` (clearing with 5% hysteresis). The dashboard shows the most recent events and a banner when any inverter reports a fault.

Set `SOLAR_NTFY_URL` to a topic such as `https://ntfy.sh/your-unguessable-topic` and the same conditions are pushed to your phone through the free [ntfy](https://ntfy.sh) app. Each alert key has a cooldown (default 30 minutes) tracked in the `alert_state` table, and a recovery message is sent when the condition clears. The aggregator, which runs from a timer, additionally alerts when the newest sample is older than `SOLAR_STALE_ALERT_AFTER` seconds, since a dead collector cannot report itself.

### Watchdog

The collector unit uses `Type=notify` with `WatchdogSec=90`. The collector pings systemd every cycle, so a hung serial read or deadlock gets the service restarted rather than silently going stale.

## Timestamps

All timestamps are stored as UTC ISO-8601 text with an explicit offset, for example `2026-10-23T18:30:00+00:00`. Daily, monthly, and yearly summaries are keyed on local calendar dates so a "solar day" is a local day, including the 23- and 25-hour days around DST changes.

Databases created before schema v3 used naive local time, which produced duplicate keys during the autumn DST change and crashed the collector. `migrate_v3.py` converts them.

## SQLite storage

The primary database is `solar_v1.db`. Important tables:

- `system_samples`, `inverter_samples`, `events`
- `minute_summary`, `quarter_hour_summary`
- `daily_summary`, `daily_inverter_summary`
- `monthly_summary`, `yearly_summary`
- `system_config` (schema version and plant facts)

SQLite runs in WAL mode so collection, aggregation, and dashboard reads coexist with minimal blocking.

## Incremental aggregation

`aggregator.py` recalculates only a small overlap near the newest data on each run. Energy is integrated trapezoidally from raw samples; pairs more than 60 seconds apart are skipped so outages are not back-filled.

Monthly and yearly summaries are rebuilt only for the periods touched by the daily overlap, so the job stays constant-time as history grows.

Specific yield, capacity factor, equivalent battery cycles, and the battery runtime estimate are only computed once the plant facts are set:

```bash
python configure.py                        # show current values
python configure.py installed_pv_kw 11.2
python configure.py battery_capacity_kwh 28.6
python configure.py low_soc_alert_percent 25
```

### Weather normalisation

With site coordinates set, `weather.py` fetches hourly global horizontal irradiance from Open-Meteo (free, no key) and the aggregator stores each completed day's insolation and a performance ratio: actual kWh divided by nameplate kW times kWh/m². A clear day and a cloudy day produce different kWh but a similar ratio; a falling ratio on clear days means soiling, shading, or a failing string.

```bash
python configure.py latitude 36.15
python configure.py longitude -95.99
```

The ratio is left blank for any day with fewer than 23 hours of weather data so a partial fetch cannot produce a misleading number.

## Backups

`backup.py` takes a consistent copy with SQLite's online backup API while the collector keeps writing, verifies it with `PRAGMA quick_check`, gzips it, and keeps the newest `SOLAR_BACKUP_KEEP`. The `solar-backup.timer` runs it nightly at 03:30. Point `SOLAR_BACKUP_DIR` at a USB drive or network mount, not the SD card the database lives on, and optionally set `SOLAR_BACKUP_COMMAND` to ship each file off-site:

```bash
SOLAR_BACKUP_COMMAND="rclone copy {path} remote:solar-backups"
```

Restore by gunzipping the file over `solar_v1.db` with the services stopped.

## Retention

Defaults (override with environment variables, see below):

- Raw system and inverter samples: 7 days
- Minute summaries: 365 days
- 15-minute, daily, monthly, yearly summaries: indefinite

Raw samples are deleted only after the aggregator has verified that every affected local day has both a system and a per-inverter daily summary. The cutoff is a local midnight, so a day is either wholly kept or wholly eligible.

## Dashboard

`app.py` serves the dashboard on port 5000 by default, with:

- Live system and per-inverter cards including mode, fault code, temperature, and both PV strings
- Energy today (completed quarter-hours plus the raw tail, cheap on a Pi 1) and yesterday, lowest SOC, yesterday's performance ratio
- Battery runtime: hours to empty while discharging or to full while charging, from `battery_capacity_kwh`
- A daily table at `/days` with per-inverter energy, peaks, production hours, SOC range, round-trip efficiency, cycles, yield, insolation, and performance ratio, downloadable as CSV
- A stale-telemetry banner when the last reading is older than `SOLAR_STALE_AFTER` seconds
- History from 1 hour to 3 years, automatically choosing raw, minute, 15-minute, or daily data, with gap markers for outages
- Recent events

API endpoints: `/api/latest`, `/api/summary`, `/api/inverters`, `/api/events?limit=N`, `/api/history?hours=N`, `/api/days?days=N`, `/api/days.csv?days=N`, and `/healthz` (503 when telemetry is stale).

The dashboard has no authentication. Keep it on a trusted network.

## Configuration

Operational settings are environment variables, read by `settings.py`. The systemd units load them from `/etc/solar-monitor.env`; a template is in `systemd/solar-monitor.env.example`.

| Variable | Default | Meaning |
|---|---|---|
| `SOLAR_DB_PATH` | `solar_v1.db` | SQLite database |
| `SOLAR_LIVE_PATH` | `/run/solar-monitor/latest.json` | Live snapshot |
| `SOLAR_SERIAL_PORT` | `/dev/ttyUSB0` | Serial device |
| `SOLAR_SERIAL_BAUD` | `2400` | |
| `SOLAR_SERIAL_COMMAND_GAP` | `0.2` | Seconds between commands |
| `SOLAR_SERIAL_STRICT_CRC` | `1` | Reject frames with bad CRC |
| `SOLAR_SAMPLE_INTERVAL` | `10` | Seconds between cycles |
| `SOLAR_STALE_AFTER` | `60` | Seconds before a reading is stale |
| `SOLAR_RAW_RETENTION_DAYS` | `7` | 0 keeps forever |
| `SOLAR_MINUTE_RETENTION_DAYS` | `365` | 0 keeps forever |
| `SOLAR_DASHBOARD_HOST` / `PORT` | `0.0.0.0` / `5000` | |
| `SOLAR_NTFY_URL` | empty | ntfy topic URL; empty disables alerts |
| `SOLAR_NTFY_TOKEN` | empty | Bearer token for a protected topic |
| `SOLAR_ALERT_COOLDOWN` | `1800` | Seconds between repeats of the same alert |
| `SOLAR_STALE_ALERT_AFTER` | `600` | Aggregator alerts when newest sample is older |
| `SOLAR_BACKUP_DIR` | `backups` | Put this on a different device |
| `SOLAR_BACKUP_KEEP` | `14` | Backups to retain |
| `SOLAR_BACKUP_COMMAND` | empty | Shell command run after a backup, `{path}` substituted |

## Installation

```bash
git clone https://github.com/RyanYarbery/SolarMonitor.git /opt/solar-monitor
cd /opt/solar-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

New installation:

```bash
python init_db.py
python configure.py installed_pv_kw <kW>
python configure.py battery_capacity_kwh <kWh>
```

Upgrading an existing installation (stop the services first):

```bash
sudo systemctl stop solar-collector.service solar-aggregator.timer solar-dashboard.service
python migrate_v3.py        # writes solar_v1.db.before-v3 as a backup
```

Install the units. They assume `/opt/solar-monitor` and a `solar` service account; adjust if needed. The collector unit declares `RuntimeDirectory=solar-monitor`, which creates the live snapshot directory.

```bash
sudo cp systemd/solar-monitor.env.example /etc/solar-monitor.env   # edit as needed
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now solar-collector.service
sudo systemctl enable --now solar-dashboard.service
sudo systemctl enable --now solar-aggregator.timer
sudo systemctl enable --now solar-weather.timer
sudo systemctl enable --now solar-backup.timer
```

Check status:

```bash
systemctl status solar-collector.service solar-dashboard.service solar-aggregator.timer
journalctl -u solar-collector.service -f
```

## Troubleshooting

- **`CRC mismatch on ...` in the collector log.** A frame was corrupted in transit and discarded. Occasional mismatches are normal on a long or noisy serial run. If every frame fails, run `python probe_inverter.py` to inspect raw bytes; if the inverter consistently sends a CRC that does not verify, set `SOLAR_SERIAL_STRICT_CRC=0` and open an issue with the probe output.
- **Dashboard shows "Telemetry is stale".** The collector is not writing. Check its journal. If `SOLAR_NTFY_URL` is set you will also have received a push notification.
- **Collector restarts every 90 seconds under systemd.** The watchdog is firing because a cycle took too long. Look for a hung serial read in the journal; the FTDI adapter may need re-plugging. Raise `WatchdogSec` only if cycles are legitimately slow.
- **Performance ratio stays blank.** Needs `installed_pv_kw`, `latitude`, `longitude`, the weather timer enabled, and a full day of hourly rows.
- **`Retention aborted: missing ... daily summaries`.** The aggregator refused to delete raw data for a day it has not summarised. Run `python aggregator.py` again; if it persists, the day's raw data is incomplete and the message names it.

## Development

```bash
pip install ruff
python -m unittest discover -s tests -t .
ruff format . && ruff check .
```

The tests run with no hardware; `tests/test_eg4.py` holds recorded-style frames for the parsers.

## License

No license has been selected yet.
