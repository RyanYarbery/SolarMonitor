# SolarMonitor

SolarMonitor is a lightweight local telemetry, aggregation, retention, and dashboard stack for EG4 off-grid solar systems.

It is designed to run continuously on low-resource hardware while collecting live data from two EG4 6500EX-48 inverters over the PI30 serial protocol. Raw telemetry is stored in SQLite, rolled into multiple historical resolutions, and served through a Flask dashboard.

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
- Raspbian / Debian-based Linux

## Architecture

```text
EG4 inverter pair
       |
       | PI30 serial
       v
  collector.py
       |
       v
  solar_v1.db
       |
       v
  aggregator.py
       |
       +-- minute summaries
       +-- 15-minute summaries
       +-- daily summaries
       +-- per-inverter daily summaries
       +-- monthly summaries
       +-- yearly summaries
       +-- guarded retention
       |
       v
     app.py
       |
       v
 Flask dashboard
```

## Telemetry collection

`collector.py` continuously queries the inverter pair and stores both system-level and inverter-level telemetry.

The current implementation uses:

- `QPGS0` and `QPGS1` for parallel inverter data
- `QPIGS` for directly reported PV charging power
- `QPIGS2` for the second PV input on the directly connected inverter

Each successful sampling cycle writes two inverter records and one combined system record in a short atomic SQLite transaction.

## SQLite storage

The primary database is `solar_v1.db`.

Important tables include:

- `system_samples`
- `inverter_samples`
- `minute_summary`
- `quarter_hour_summary`
- `daily_summary`
- `daily_inverter_summary`
- `monthly_summary`
- `yearly_summary`

SQLite runs in WAL mode so collection, aggregation, and dashboard reads can coexist with minimal blocking.

## Incremental aggregation

`aggregator.py` converts high-frequency telemetry into progressively lower-resolution history.

Rather than rebuilding all history every run, it recalculates only a small overlap near the newest data. This keeps steady-state aggregation practical on a Raspberry Pi 1 while still allowing recent buckets to be corrected.

## Retention

Current retention policy:

- Raw system samples: 7 days
- Raw inverter samples: 7 days
- Minute summaries: 365 days
- 15-minute summaries: retained indefinitely
- Daily summaries: retained indefinitely
- Monthly summaries: retained indefinitely
- Yearly summaries: retained indefinitely

Before raw telemetry is deleted, the retention logic verifies that corresponding daily summary coverage exists.

## Dashboard

`app.py` provides a lightweight Flask dashboard with live system data, per-inverter telemetry, PV quality labels, and historical charts.

History ranges include:

- 1 hour
- 6 hours
- 24 hours
- 7 days
- 2 weeks
- 1 month
- 3 months
- 6 months
- 9 months
- 1 year
- 2 years
- 3 years

The history API automatically selects raw, minute, quarter-hour, or daily data depending on the requested range.

Historical charts also include gap markers so outages are shown as missing data instead of misleading straight lines.

## Performance

The current deployment runs on a Raspberry Pi 1 Model B Rev 2 with 512 MB RAM.

Important optimizations include:

- Indexed timestamp queries
- SQLite WAL mode
- Short collector transactions
- Incremental aggregation
- Pre-aggregated long-term history
- Limited dashboard response sizes
- Automatic raw-data retention

## Services

The deployment uses systemd for long-running components and scheduled aggregation:

```text
solar-collector.service
solar-dashboard.service
solar-aggregator.service
solar-aggregator.timer
```

Machine-specific installation paths are intentionally not embedded in the repository.

## Repository safety

Runtime and private data are excluded from version control, including:

- SQLite telemetry databases
- Python virtual environments
- Local configuration
- Secrets and credentials
- Private keys
- Local safety backups

Raw inverter serial numbers are not hardcoded in the source.

## Status

SolarMonitor is actively evolving. Current work has focused on reliable EG4 serial telemetry, parallel inverter support, low-contention SQLite writes, multi-resolution historical aggregation, guarded retention, and multi-year dashboard history.

Future improvements may include richer diagnostics, better fault interpretation, additional energy analytics, and deployment tooling.

## License

No license has been selected yet.
