# Baga Radar (Баға Радар)

> Price monitoring dashboard and citizen reporting bot for socially significant food staples (СЗПТ) in Kazakhstan (Korkyt Hackathon, Case 03).

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat&logo=python&logoColor=white)](https://python.org/)
[![Platform](https://img.shields.io/badge/Domain-GovTech-0052CC?style=flat)](https://stat.gov.kz/)
[![Alerts](https://img.shields.io/badge/Telegram-Bot-2CA5E0?style=flat&logo=telegram&logoColor=white)](https://core.telegram.org/bots)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

---

## Overview

A lightweight monitoring tool that aggregates official weekly food prices from the Bureau of National Statistics (BNS ASPiR RK), tracks National Bank currency rates, and provides a Telegram-based appeal intake form for citizens reporting price discrepancies with receipt photos.

### Key Components

| File | Purpose |
| :--- | :--- |
| `baga-radar-gov.html` | Specialist dashboard (prices across 20 regions and 31 staple goods). |
| `obrashenie.html` | Citizen appeal submission page with photo upload. |
| `relay.py` | Local HTTP service, currency cache & Telegram dispatch. |
| `fetch-prices.py` | Ingests and parses official BNS weekly price releases (`Ц-101э`). |
| `build-apk.sh` | Packages the dashboard into a standalone Android APK. |

---

## Quickstart

```bash
# 1. Clone & enter
git clone https://github.com/NURJAKS/Hackathon_Korkyt222.git
cd Hackathon_Korkyt222

# 2. Start relay server
python3 relay.py
```

- **Dashboard**: `http://localhost:8787/`
- **Appeal Form**: `http://localhost:8787/obrashenie.html`

### Update Statistical Data

```bash
# Download latest BNS release and embed into interface
python3 fetch-prices.py --inject baga-radar-gov.html
```

### Telegram Bot Setup (Optional)

Create `.env` based on `.env.example`:
```bash
cp .env.example .env
```
Fill in `TG_TOKEN` (from @BotFather) and `TG_CHAT_ID`. To detect your chat ID automatically:
```bash
python3 relay.py --chat-id --wait
```

---

## License

MIT License
