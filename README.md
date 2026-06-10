# Searibu — Backend API

**Searibu Marine Information API** 

A Flask backend for the Kepulauan Seribu marine information system: TPXO10-atlas-v2 harmonic tidal prediction, Luwes tide-station telemetry, authentication, subscriptions, and IHO S-104 (HDF5) data export.

Capstone Design Project — Geodesy and Geomatics Engineering, FITB, Institut Teknologi Bandung, 2026.

---

## Stack

| Category | Technology |
|----------|-----------|
| Framework | Flask 3 + Flask-CORS |
| Primary database | PostgreSQL (Supabase) via `psycopg2` connection pool |
| Tidal database | SQLite (`tpxo_seribu.db`, TPXO10 harmonic constants) |
| Computation | NumPy (harmonic summation) |
| S-104 export | h5py (HDF5) |
| Server | Gunicorn |
| Deployment | Railway |

---

## Architecture

```
api/
├── app.py                # Entry point: pool init, blueprints, scheduler, tide route
├── pg_db.py              # PostgreSQL layer (ThreadedConnectionPool + execute_* helpers)
├── auth_db.py            # users table operations (SHA-256 hash + salt)
├── auth_routes.py        # /api/auth/{register,login,google}
├── billing_db.py         # subscriptions & payments operations
├── billing_routes.py     # /api/create-payment (DUMMY MODE), /api/subscription
├── profile_routes.py     # /api/profile, /api/profile/role, /api/admin/*
├── luwes_db.py           # water_level_observations & fetch_log operations
├── luwes_service.py      # Luwes API HTTP client + normalisation
├── luwes_scheduler.py    # Daemon thread fetching every 60 seconds
├── luwes_routes.py       # /api/luwes/{level,history,status,fetch,overlay}
├── s104_exporter.py      # IHO S-104 Ed.2.0.0 HDF5 generator + validator
└── s104_routes.py        # /api/s104/{export,export/luwes,json,validate,metadata}

core/
└── tpxo_predictor.py     # TPXO10 harmonic predictor (nodal corrections, equilibrium args)

migrations/
├── 001_initial_schema.sql  # users, observations, fetch_log, subscriptions, payments
├── 002_add_role.sql        # role column (general/researcher)
└── 003_add_is_admin.sql    # is_admin column

scripts/
├── preprocess_tpxo10.py    # Extract TPXO10-atlas-v2 NetCDF → SQLite
├── preprocess_tpxo.py      # TPXO9 version (legacy)
└── migrate_sqlite_to_pg.py # Migrate data SQLite → PostgreSQL
```

---

## Tidal Model

Prediction uses the standard harmonic summation formula:

```
h(t) = Σ_k  f_k · A_k · cos[ ω_k·(t−t₀) + V₀_k + u_k − κ_k ]
```

- **Model:** TPXO10-atlas-v2 (Oregon State University)
- **Constituents:** 15 (`2n2, k1, k2, m2, m4, mf, mm, mn4, ms4, n2, o1, p1, q1, s1, s2`)
- **Nodal corrections:** f (amplitude) and u (phase) — Schureman (1958) / Foreman (1977)
- **Datum:** MSL (Mean Sea Level)
- **Reference epoch:** J1900.0 (JD 2415020.0)

The SQLite database is built once from per-constituent NetCDF files via `scripts/preprocess_tpxo10.py`, then committed to the repo (the source `.nc` files are not committed — see `.gitignore`).

---

## API Endpoints

### General
| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | API info & status |
| GET | `/api/health` | Health check (PostgreSQL + TPXO) |

### Tides
| Method | Path | Parameters |
|--------|------|------------|
| GET | `/api/tide/prediction` | `lon`, `lat`, `start_date`, `end_date`, `interval_hours\|interval_minutes` |

### Luwes (tide telemetry)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/luwes/level` | Latest observation |
| GET | `/api/luwes/history` | History by date range |
| GET | `/api/luwes/status` | Scheduler status + DB statistics |
| POST | `/api/luwes/fetch` | Trigger a manual fetch |
| GET | `/api/luwes/overlay` | Combined Luwes observations + TPXO prediction for one day |

### Authentication
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/auth/register` | Registration (`full_name`, `email`, `password`, `role`) |
| POST | `/api/auth/login` | Email + password login |
| POST | `/api/auth/google` | Sign-in/sign-up via Google OAuth |

### Subscriptions & Billing
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/create-payment` | Activate Pro (**DUMMY MODE** — instant, no gateway) |
| GET | `/api/subscription` | Subscription status (`email` or `user_id`) |
| POST | `/api/subscription/check-access` | Check feature access |

### Profile & Admin
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/profile` | User profile |
| PUT | `/api/profile/role` | Update role (general/researcher) |
| GET | `/api/admin/stats` | Dashboard statistics (admin only) |
| GET | `/api/admin/payments` | Paginated payment history (admin only) |

### IHO S-104
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/s104/export` | Download TPXO prediction HDF5 (`dataDynamicity=2`) |
| GET | `/api/s104/export/luwes` | Download Luwes observation HDF5 (`dataDynamicity=1`, TOL −1.944 m) |
| GET | `/api/s104/json` | JSON preview of water-level data |
| GET | `/api/s104/validate` | Validate HDF5 file structure |
| GET | `/api/s104/metadata` | S-100/S-104 compliance metadata |

---

## IHO S-104 Standard

HDF5 export follows **IHO S-104 Edition 2.0.0** (adopted Dec 2024):

| Attribute | Value | Notes |
|-----------|-------|-------|
| `productSpecification` | `INT.IHO.S-104.2.0` | |
| `horizontalCRS` | 4326 | WGS 84 |
| `verticalDatum` | 3 | meanSeaLevel (MSL) |
| `verticalCoordinateBase` | 2 | verticalDatum |
| `dataCodingFormat` | 2 | regularly-gridded arrays |
| `dataDynamicity` | 2 (TPXO) / 1 (Luwes) | astronomicalPrediction / observation |
| `numGRP` | = numberOfTimes | one group per time step |

Files can be opened with HDFView, ECDIS software, or the `s100py` Python library.

> **Known inconsistency:** `s104_routes.py` (`/api/s104/json` and `/api/s104/metadata`) still report `verticalDatum: 12`, while the actual exporter in `s104_exporter.py` writes `3` (MSL). The exporter value is the correct/authoritative one — consider aligning the two routes for compliance reporting.

---

## Running Locally

### Prerequisites
- Python 3.11
- PostgreSQL (or Supabase access)
- The `data/tpxo_seribu.db` file (generated by the preprocessing script)

### Setup

```bash
# Create a virtual environment
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run the migrations in the Supabase SQL editor (in order):
#   migrations/001_initial_schema.sql
#   migrations/002_add_role.sql
#   migrations/003_add_is_admin.sql

# Run the server
python -m api.app                # http://localhost:5000
```

### Environment Variables

Create a `.env` file (see `.env.example`):

```env
# PostgreSQL (Supabase session pooler)
DATABASE_URL=postgresql://postgres.[ref]:[password]@[host]:5432/postgres?sslmode=require

# TPXO SQLite database (committed to the repo)
DATABASE_PATH=data/tpxo_seribu.db

# Luwes telemetry station IMEI
LUWES_IMEI=869556066101370

# CORS (comma-separated)
CORS_ORIGINS=http://localhost:5173,https://searibu.vercel.app

# Flask
FLASK_DEBUG=False
PORT=5000
```

---

## TPXO10 Preprocessing

Run once, from a local machine that has the TPXO10-atlas-v2 NetCDF files:

```bash
pip install netCDF4 numpy

python scripts/preprocess_tpxo10.py \
    --tpxo-dir  data/tpxo10_atlas_v2 \
    --output-db data/tpxo_seribu.db
```

The `tpxo10_atlas_v2` folder must contain `grid_tpxo10atlas_v2.nc` plus the 15 constituent files `h_<name>_tpxo10_atlas_30_v2.nc`.

> **Bounding box:** lon [106.3, 107.0], lat [−6.0, −5.3].
> Implementation notes: the TPXO10 layout is `(lon, lat)` (requires transpose), hRe/hIm values are in **millimetres** (divide by 1000 → metres), and the phase convention is `κ = atan2(−hIm, hRe)`.

---

## Luwes Scheduler

A single daemon thread (`luwes_scheduler.py`) polls the Luwes API every **60 seconds**, stores new observations, and silently ignores duplicates (by `rec`). Fetch statistics are available at `/api/luwes/status`.

Data source: `http://data3.luwesinovasimandiri.com:8002/last`

---

## Deployment (Railway)

Configured via `railway.toml` / `Procfile`:

```
web: gunicorn api.app:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 120
```

- Builder: nixpacks (`pip install -r requirements.txt`)
- Restart policy: `on_failure`, max 3 retries
- Large files (`.nc`, `.mat`, runtime `.db`) are excluded via `.railwayignore`

> Use **1 worker** so the Luwes scheduler is not duplicated across processes.

---

## Important Notes

- **Billing is in DUMMY MODE** — `POST /api/create-payment` activates the Pro subscription instantly without Midtrans. The earlier Midtrans/Duitku integration has been removed.
- **S-104 feature access** only requires Pro status (not dependent on role). Admins (`is_admin`) get full access.
- **TOL correction** of −1.944 m is applied to shift the Luwes datum to MSL so it is comparable to TPXO.
- **Password hashing** uses SHA-256 + a 32-byte salt (see `auth_db._hash_password`).

---

## License

Capstone Design Project — FITB, Institut Teknologi Bandung, 2026.

### References
- IHO S-100 Universal Hydrographic Data Model, Ed. 5.2.0 (2024)
- IHO S-104 Water Level Information for Surface Navigation, Ed. 2.0.0 (2024)
- Schureman, P. (1958). *Manual of Harmonic Analysis and Prediction of Tides*. USC&GS Special Publication No. 98.
- Foreman, M.G.G. (1977). *Manual for Tidal Heights Analysis and Prediction*. IOS Manuscript Report 77-10.
- Egbert, G.D. & Erofeeva, S.Y. (2002). Efficient Inverse Modeling of Barotropic Ocean Tides. *J. Atmos. Oceanic Technol.*, 19, 183–204.
