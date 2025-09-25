{\rtf1\ansi\ansicpg1252\cocoartf2865
\cocoatextscaling0\cocoaplatform0{\fonttbl\f0\fswiss\fcharset0 Helvetica;}
{\colortbl;\red255\green255\blue255;}
{\*\expandedcolortbl;;}
\paperw11900\paperh16840\margl1440\margr1440\vieww11520\viewh8400\viewkind0
\pard\tx720\tx1440\tx2160\tx2880\tx3600\tx4320\tx5040\tx5760\tx6480\tx7200\tx7920\tx8640\pardirnatural\partightenfactor0

\f0\fs24 \cf0 # Warehouse Planner Lite (Streamlit)\
\
A non-tech friendly web app that:\
- Consolidates SKUs into **Colour Sets** and **Size Sets**\
- Rolls up **ABC** and **RRS** classes to set-level\
- Computes **Style Density Proxy**\
- Recommends **Zoning** (PickFace+Bulk / Bulk / CrossDock) from **ABC\'d7RRS + density**\
- Computes **festival-aware Min\'96Max**\
- Applies a **Capacity Governor** (defaults: 6.8 lakh pieces; cover ~10\'9612 days)\
- (Optional) Maps **sets to racking** (Zone \uc0\u8594  Row \u8594  Bay \u8594  Level/Tier) using a Bin Master\
\
## Deploy online (Streamlit Cloud)\
1. Create a GitHub repo (e.g., `warehouse-planner-lite`).\
2. Add files: `app.py`, `requirements.txt`, `README.md`.\
3. Go to https://share.streamlit.io \uc0\u8594  **New app** \u8594  pick your repo \u8594  set main file `app.py` \u8594  **Deploy**.\
4. Open the URL and use the app.\
\
## Use the app\
1. Upload **Q1 Cumulative Sales** Excel.\
2. Upload ABC SKU files (A/B/C) and RRS SKU files (Runner/Repeater/Stranger).\
3. (Optional) Upload **Bin Master** (Excel/CSV) to assign Pick-Face/Bulk sets to racks.\
4. Click **Run Planner** \uc0\u8594  review results \u8594  **Download Excel**.\
\
## Bin Master schema (minimum columns)\
- `bin_code` : unique bin id\
- `zone` : logical zone name\
- `row` : row id\
- `bay` : bay id\
- `level` : shelf/level number\
- `tier` : floor/tier number (if used)\
- `bin_type` : `PF` or `BULK` (or leave blank and app infers by level/tier)\
- `capacity_units` : max pieces this bin can hold\
\
> Optional: `pick_face_flag` (Y/N), `hazmat_flag`, constraints, etc.\
\
Place capacity in **pieces** (or in cartons if you\'92ll feed piece-per-carton in future).\
\
## Tuning\
Use the sidebar to adjust:\
- Capacity ceiling\
- Festival multipliers\
- Density thresholds\
- PF day bands per ABC\'d7RRS\
- Target cover window\
}