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
       |
       v
  solar_v1.db  (system_samples, inverter_samples, events)
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
| `configure.py` | Reads or sets plant facts (array size, battery capacity) |
| `probe_inverter.py` | Dumps raw frames for diagnosis |
| `tests/` | Unit tests, no hardware needed |

## Telemetry collection

`collector.py` holds the serial port open for its lifetime and queries:

- `QPGS0` and `QPGS1` for both parallel inverters (mode, fault code, output, battery, PV voltages and currents)
- `QPIGS` and `QPIGS2` for the directly connected inverter's measured PV charging power on both inputs

Inverter 1's PV power is therefore *measured*; inverter 2's is *estimated* from PV voltage times current. The dashboard labels each.

Every response frame's CRC is verified. A corrupt frame is rejected and the cycle retried rather than stored. Each successful cycle writes two inverter rows and one system row in one short transaction, then updates the live JSON snapshot.

Grid power is not reported by `QPGS` in a trustworthy form, so `grid_watts` is stored as NULL rather than a misleading zero.

### Events

Mode changes and fault code transitions per inverter are written to the `events` table, as are telemetry loss (after six consecutive failed reads) and recovery. The dashboard shows the most recent events and a banner when any inverter reports a fault.

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

Specific yield, capacity factor, and equivalent battery cycles are only computed once the plant facts are set:

```bash
python configure.py                        # show current values
python configure.py installed_pv_kw 11.2
python configure.py battery_capacity_kwh 28.6
```

## Retention

Defaults (override with environment variables, see below):

- Raw system and inverter samples: 7 days
- Minute summaries: 365 days
- 15-minute, daily, monthly, yearly summaries: indefinite

Raw samples are deleted only after the aggregator has verified that every affected local day has both a system and a per-inverter daily summary. The cutoff is a local midnight, so a day is either wholly kept or wholly eligible.

## Dashboard

`app.py` serves the dashboard on port 5000 by default, with:

- Live system and per-inverter cards including mode, fault code, and temperature
- A stale-telemetry banner when the last reading is older than `SOLAR_STALE_AFTER` seconds
- History from 1 hour to 3 years, automatically choosing raw, minute, 15-minute, or daily data, with gap markers for outages
- Recent events

API endpoints: `/api/latest`, `/api/inverters`, `/api/events?limit=N`, `/api/history?hours=N`, and `/healthz` (503 when telemetry is stale).

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
```

Check status:

```bash
systemctl status solar-collector.service solar-dashboard.service solar-aggregator.timer
journalctl -u solar-collector.service -f
```

## Troubleshooting

- **`CRC mismatch on ...` in the collector log.** A frame was corrupted in transit and discarded. Occasional mismatches are normal on a long or noisy serial run. If every frame fails, run `python probe_inverter.py` to inspect raw bytes; if the inverter consistently sends a CRC that does not verify, set `SOLAR_SERIAL_STRICT_CRC=0` and open an issue with the probe output.
- **Dashboard shows "Telemetry is stale".** The collector is not writing. Check its journal.
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
