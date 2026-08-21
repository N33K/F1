# F1 Energy Decoder — Project Context

A web app that explains F1's 2026 megajoule energy regulations to fans, built
from scratch as a learning project (HTML/CSS/JS + Python/Flask), deployed
live on Render.

---

## Project structure

```
f1-energy-decoder/
├── backend/
│   ├── app.py              Flask server — API + static file serving
│   ├── parser.py           Reads FIA PDF, extracts MJ table
│   ├── fetcher.py          Finds + downloads PDF from FIA site
│   ├── energy_model.py     Physics simulation (MGU-K recovery/deployment)
│   └── telemetry.py        Loads/caches FastF1 telemetry for the model
│
├── data/
│   ├── circuits.json       Single source of truth: 24 circuits, aliases,
│   │                       svg_file, (needs) downforce_level, aero_profiles
│   ├── overrides.json      Real MJ limits parsed from FIA PDFs, per circuit
│   ├── telemetry/          Cached FastF1 CSVs, e.g. miami_race.csv
│   ├── pu_info_*.pdf       Downloaded FIA Power Unit Information docs
│   └── CIRCUITS_JSON_ADDITIONS.md   Fields the energy model needs added
│
├── frontend/
│   ├── index.html          Desktop + mobile layout (burger menu, drawer)
│   ├── style.css           Dark F1-style theme; mobile styles in @media block
│   └── app.js              API calls, SVG animation, mobile player sync
│
└── f1-circuits-svg/        Downloaded circuit outline SVGs (from
                            julesr0y/f1-circuits-svg, plain files not a clone)
```

---

## How the pieces connect

```
FIA website (Power Unit Information PDFs, published per race weekend)
        │
        ▼
   fetcher.py  ──finds & downloads──▶  pu_info_{circuit}.pdf
        │
        ▼
   parser.py  ──extracts MJ table──▶  overrides.json
        │
        ▼
   app.py  ──merges with circuits.json──▶  /api/circuits, /api/circuit/<id>
        │
        ▼
   frontend (app.js)  ──renders──▶  animated circuit, metric cards, session table
```

```
FastF1 (run locally only)
        │
        ▼
   telemetry.py export  ──▶  data/telemetry/{circuit}_{session}.csv  (committed)
        │
        ▼
   app.py /api/simulate/<id>  ──loads cached CSV, real MJ limit from
        │                        overrides.json──▶
        ▼
   energy_model.py  ──▶  corner/straight extraction, physics sim──▶  summary
```

---

## Key architectural decisions made along the way

- **`circuits.json` is the single source of truth.** Circuit names, aliases,
  lap times, SVG filenames — everything lives here once. `parser.py` and
  `fetcher.py` both load it rather than keeping their own copies (this was a
  real bug we caught and fixed: `fetcher.py` originally had its own
  hardcoded `FIA_EVENT_NAMES` dict duplicating `circuits.json`).

- **Official FIA PDFs, not RSS/news scraping.** The original plan was to
  scrape FIA/F1 RSS feeds for energy announcements using regex. We pivoted
  hard once the user found the actual "Power Unit Information" PDFs the FIA
  publishes before every race — these have a clean structured table
  (`Article C5.2.10`) with exact MJ values per session. Far more reliable
  than parsing news prose.

- **`app.py` merges, never trusts one source blindly.** `merge_circuit_data()`
  combines `circuits.json` (baseline) with `overrides.json` (fetched PDF
  data). Circuits without fetched data show `limits: null` rather than
  crashing — the "always works" pattern used throughout.

- **Energy model is pure computation, no file I/O.** `energy_model.py` takes
  data in, returns data out — same pattern as `parser.py`. `app.py` owns all
  file loading. This was an explicit fix during integration (the original
  model file had its own `load_circuits()` duplicating the app's).

- **Telemetry is cache-first, network-optional.** `telemetry.py` reads
  committed CSVs by default and never touches FastF1 on the deployed server
  (`allow_fastf1=False`). Render's free tier has an ephemeral filesystem and
  512MB RAM, so live FastF1 downloads aren't viable there — CSVs are
  generated locally once and committed.

- **Numpy/pandas types never leak into JSON responses.** `summarize_results()`
  explicitly casts to native `float()` before returning, since `np.float64`
  breaks Flask's `jsonify`. Found via a smoke test before it could bite in
  production.

---

## Real bugs hit and fixed (useful history for debugging similar issues)

1. **Function signature drift** — `extract_circuit()` changed to take an
   `aliases` parameter, but callers weren't updated in the same pass.
   Recurring lesson: when a function signature changes, grep every call site.

2. **Missing `svg_file` in API response** — `circuits.json` had the field,
   but `merge_circuit_data()` built its response dict manually and forgot to
   include it. Any manually-constructed dict from a larger object needs to be
   checked against the source for completeness.

3. **Static files 404 on Flask** — needed an explicit wildcard route
   (`@app.route("/<path:filename>")`) to serve `style.css`/`app.js`, since
   Flask only serves `index.html` by default. Route ordering doesn't matter
   in Flask (specificity does), but a *placement* mistake in the file
   (putting it before other routes were defined) caused confusing failures.

4. **Route naming mismatch** — `/api/circuit/<id>` (singular, correct) vs
   accidentally calling `/api/circuits/<id>` (plural) from the frontend.
   Small naming inconsistencies between backend routes and frontend fetch
   calls are a recurring source of 404s.

5. **SVG path clipping** — stripping `width`/`height` attributes from
   injected SVGs without setting a `viewBox` first left the browser with no
   coordinate space to scale against.

6. **git embedded repository** — cloning `f1-circuits-svg` with `git clone`
   left a nested `.git` folder, so the parent repo treated it as a submodule
   and silently excluded its contents. Fixed by re-downloading as a plain
   zip instead of cloning.

7. **Spain/Madrid FIA naming swap** — in the 2026 calendar, Barcelona is now
   called the "Barcelona-Catalunya Grand Prix" and "Spanish Grand Prix" was
   reassigned to Madrid. `circuits.json`'s `name` field (used to build the
   FIA documents URL) had the old assignment, causing Barcelona's fetch to
   404 against the wrong URL.

8. **`normalize_session()` mismatch** — `telemetry.py`'s docstring example
   told the user to export with session code `"R"`, but the default
   `load_telemetry()` call used `"race"`, and `cache_path()` didn't
   normalize between the two — so export and check built different
   filenames (`miami_R.csv` vs `miami_race.csv`) and never found each other.
   Fixed by adding a `normalize_session()` step used consistently in both
   the CLI and `cache_path()`.

---

## What still needs doing

- **Real `aero_profiles` values.** `CIRCUITS_JSON_ADDITIONS.md` has
  placeholder CdA/ClA numbers. The energy model's validated accuracy
  (4-12% error) depends on the real fitted values — get them from whoever
  built the original model before trusting simulation output.

- **`downforce_level` field** — needs adding to all 24 circuits in
  `circuits.json` (mapping provided in `CIRCUITS_JSON_ADDITIONS.md`).

- **Wire the simulate route into `app.py`** — the `/api/simulate/<id>` route,
  a `get_recharge_limit_mj()` helper, and a `from flask import request` import
  need to be added to `app.py`. `requirements.txt` needs `numpy` and `pandas`
  added (deliberately *not* `fastf1` — that's local-only tooling).

- **Generate telemetry CSVs for more circuits** — only Miami has been
  exported so far. Each one needs `python backend/telemetry.py export
  <circuit_id>` run locally, then committed.

- **Frontend integration for the simulation results** — `/api/simulate/<id>`
  exists as a backend endpoint but nothing in `app.js` calls it yet or
  displays the results.

- User mentioned wanting to add more features and address some other
  outstanding issues in a future session — nothing specific captured yet.

---

## Deployment

- **Host:** Render (free tier) — chosen because it's the only major platform
  with a real permanent free tier for web services in 2026 (Railway and
  Fly.io both dropped theirs). Trade-off: spins down after 15 min idle,
  30-60s cold start on first request after.
- **Live URL:** `https://f1-vh1i.onrender.com`
- Requires `PORT` env var read from `os.environ` (Render sets this
  automatically) and `debug=False`, `host='0.0.0.0'` in the final
  `app.run()` call.
- Git branch used for the mobile-responsive work, kept separate from
  `master` to avoid breaking the working version during iteration.
