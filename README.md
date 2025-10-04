# Warehouse Planner Lite (Streamlit)

A non-tech friendly web app that:
- Consolidates SKUs into **Colour Sets** and **Size Sets**
- Rolls up **ABC** and **RRS** classes to set-level
- Computes **Style Density Proxy**
- Recommends **Zoning** (PickFace+Bulk / Bulk / CrossDock) from **ABC×RRS + density**
- Computes **festival-aware Min–Max**
- Applies a **Capacity Governor** (defaults: 6.8 lakh pieces; cover ~10–12 days)
- (Optional) Maps **sets to racking** (Zone → Row → Bay → Level/Tier) using a Bin Master

---

## Deploy online (Streamlit Cloud)
1. Create a GitHub repo (e.g., `warehouse-planner-lite`).
2. Add files: `app.py`, `requirements.txt`, `README.md`.
3. Go to https://share.streamlit.io → **New app** → pick your repo → set main file `app.py` → **Deploy**.
4. Open the URL and use the app.

---

## Use the app
1. Upload **Q1 Cumulative Sales** Excel.
2. Upload ABC SKU files (A/B/C) and RRS SKU files (Runner/Repeater/Stranger).
3. (Optional) Upload **Bin Master** (Excel/CSV) to assign Pick-Face/Bulk sets to racks.
4. Click **Run Planner** → review results → **Download Excel**.

---

## Bin Master schema (minimum columns)
- `bin_code` : unique bin id
- `zone` : logical zone name
- `row` : row id
- `bay` : bay id
- `level` : shelf/level number
- `tier` : floor/tier number (if used)
- `bin_type` : `PF` or `BULK` (or leave blank and app infers by level/tier)
- `capacity_units` : max pieces this bin can hold

Optional: `pick_face_flag` (Y/N), `hazmat_flag`, constraints, etc.

*Place capacity in pieces (or in cartons if you’ll feed piece-per-carton in future).*

---

## Tuning
Use the sidebar to adjust:
- Capacity ceiling
- Festival multipliers
- Density thresholds
- PF day bands per ABC×RRS
- Target cover window

