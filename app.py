import streamlit as st
import pandas as pd
import numpy as np
from io import BytesIO

st.set_page_config(page_title="Warehouse Planner Lite (Sets)", layout="wide")

# -------------------- Session State Boot --------------------
for k in ["results_ready", "colour_final", "size_final", "pf_assign", "bulk_assign",
          "drive_df", "consolidated_df", "pf_diag", "bulk_diag", "gov_summary"]:
    if k not in st.session_state:
        st.session_state[k] = None

# -------------------- Sidebar controls --------------------
st.sidebar.title("Planner Controls")

# Capacity & demand controls
cap_total = st.sidebar.number_input("Warehouse capacity (pieces)", value=680000, step=10000)
baseline_override = st.sidebar.number_input(
    "Override baseline daily demand (pcs/day, optional)",
    min_value=0, value=0, step=1000,
    help="Leave 0 to auto-compute from the sales file; set e.g. 60000 to force the planner baseline."
)

festival_runner_repeater = st.sidebar.number_input("Festival uplift: Runners/Repeaters ×", value=2.0, step=0.25, format="%.2f")
festival_stranger = st.sidebar.number_input("Festival uplift: Strangers ×", value=1.25, step=0.05, format="%.2f")

# Density thresholds (PF eligibility & staging)
pf_density_threshold = st.sidebar.slider("Pick-Face density threshold (≤ x is PF-friendly)", 1, 10, 5)
bulk_density_threshold = st.sidebar.slider("Bulk density threshold (≤ x is Bulk; > goes CrossDock)", 4, 20, 10)
st.sidebar.caption("Density rules: 1–3 = PF-friendly; 4–10 = Bulk; >10 = CrossDock. Strangers always CrossDock.")

# Roll-up majority threshold
majority_threshold = st.sidebar.slider(
    "Majority threshold for set roll-up (%)", 50, 80, 50,
    help="Minimum share by distinct SKU count to call a clear majority for ABC/RRS at set level."
) / 100.0

# Cover windows (governor will still enforce capacity)
cov_min = st.sidebar.slider("Overall days cover min", 5, 15, 10)
cov_max = st.sidebar.slider("Overall days cover max", 8, 20, 12)

# Racking defaults
pf_levels_max = st.sidebar.number_input("Pick-Face max level (1=bottom; default 3)", value=3, min_value=1, max_value=10)
slot_capacity_default = st.sidebar.number_input("Capacity per slot (pieces)", value=60, step=5)

# PF Min/Max (days) by ABC×RRS (editable)
st.sidebar.subheader("PF Min/Max (days) by ABC×RRS")
def_val = {
    ("A","Runner"): (2,4),
    ("A","Repeater"): (3,5),
    ("A","Stranger"): (0,0),
    ("B","Runner"): (3,6),
    ("B","Repeater"): (3,6),
    ("B","Stranger"): (0,0),
    ("C","Runner"): (1,3),
    ("C","Repeater"): (1,3),
    ("C","Stranger"): (0,2),
}
pf_policy = {}
for key, (dmin, dmax) in def_val.items():
    a, r = key
    col = st.sidebar.columns(2)
    with col[0]:
        mn = st.number_input(f"PF Min {a}/{r}", value=dmin, key=f"pfmin_{a}_{r}")
    with col[1]:
        mx = st.number_input(f"PF Max {a}/{r}", value=dmax, key=f"pfmax_{a}_{r}")
    pf_policy[key] = (mn, mx)

# Hybrid allocation (choose Colour vs Size per article)
hybrid_mode = st.sidebar.checkbox("Hybrid allocation (auto choose Colour vs Size per article)", value=True)
st.sidebar.caption("Hybrid mode avoids double counting by selecting one axis per article/family.")
override_file = st.sidebar.file_uploader("Optional: Axis Overrides CSV (division,department,brand,article,force_axis)", type=["csv"])

# Kanban toggle (optional operational export)
show_kanban = st.sidebar.checkbox("🔁 Show Kanban triggers (PF/Bulk Min Units only)", value=False)

# -------------------- Page header --------------------
st.title("Warehouse Planner Lite — Set-level (Colour/Size)")
st.markdown("""
Upload **Q1 cumulative sales** and **ABC/RRS SKU lists**.  
Optional: upload **Bin Master** for Pick-Face/Bulk assignment (Zone/Floor → Row → Bay → Level/Tier).
""")

# -------------------- Uploaders --------------------
sales_file = st.file_uploader("Q1 Cumulative Sales (Excel/CSV)", type=["xlsx","csv"])
a_file = st.file_uploader("A Class SKUs (Excel/CSV)", type=["xlsx","csv"])
b_file = st.file_uploader("B Class SKUs (Excel/CSV)", type=["xlsx","csv"])
c_file = st.file_uploader("C Class SKUs (Excel/CSV)", type=["xlsx","csv"])

runner_file = st.file_uploader("Runner SKUs (Excel/CSV)", type=["xlsx","csv"])
repeater_file = st.file_uploader("Repeater SKUs (Excel/CSV)", type=["xlsx","csv"])
stranger_file = st.file_uploader("Stranger SKUs (Excel/CSV)", type=["xlsx","csv"])

bin_file = st.file_uploader("Bin Master (Excel or CSV)", type=["xlsx","csv"])

run_btn = st.button("Run Planner")
just_clicked_run = run_btn

# -------------------- Helpers --------------------
def read_xlsx_or_csv(uploaded):
    if uploaded is None:
        return None
    name = uploaded.name.lower()
    if name.endswith(".csv"):
        df = pd.read_csv(uploaded)
    else:
        df = pd.read_excel(uploaded, engine="openpyxl")
    df.columns = [str(c).strip() for c in df.columns]
    return df

def read_sales(uploaded):
    return read_xlsx_or_csv(uploaded)

def norm_size(x):
    if pd.isna(x): return np.nan
    s = str(x).strip().upper().replace(" ", "")
    return (s.replace("FREESIZE","FREE")
             .replace("FREE SIZE","FREE")
             .replace("XXXL","3XL")
             .replace("2XL","XXL"))

def rollup_rrs_majority_by_sku(sku_frame, keys, threshold=0.5):
    """
    Majority by distinct SKU count within the set.
    If no class >= threshold, fallback preference: Repeater > Runner > Stranger > Unknown (tie-break by qty).
    """
    df = sku_frame.copy()
    df["RRS_Class"] = df["RRS_Class"].fillna("Unknown")

    # Count distinct SKUs per RRS within each set
    uni = df.drop_duplicates(subset=keys + ["invarticle_code","RRS_Class"])
    cnt = (uni.groupby(keys + ["RRS_Class"], dropna=False)["invarticle_code"]
              .nunique().rename("sku_cnt").reset_index())
    tot = cnt.groupby(keys, dropna=False)["sku_cnt"].sum().rename("sku_tot")
    cnt = cnt.merge(tot, on=keys, how="left")
    cnt["share"] = cnt["sku_cnt"] / cnt["sku_tot"].replace(0, np.nan)

    # Provisional winner = highest share
    win = (cnt.sort_values(keys + ["share","sku_cnt"],
                           ascending=[True]*len(keys) + [False, False])
              .drop_duplicates(subset=keys, keep="first")[keys + ["RRS_Class","share"]].copy())

    # Build fallback table by preference, tie-break by sales qty
    qty = (df.groupby(keys + ["RRS_Class"], dropna=False)["total_qty"]
             .sum().rename("qty").reset_index())
    pref = {"Repeater":3, "Runner":2, "Stranger":1, "Unknown":0}
    qty["pref"] = qty["RRS_Class"].map(pref).fillna(-1)
    fb = (qty.sort_values(keys + ["pref","qty"],
                          ascending=[True]*len(keys) + [False, False])
             .drop_duplicates(subset=keys, keep="first")[keys + ["RRS_Class"]]
             .rename(columns={"RRS_Class":"RRS_FB"}))

    # Merge fallback; recompute mask AFTER merge so indices align
    win = win.merge(fb, on=keys, how="left")
    mask = win["share"].fillna(0) < float(threshold)
    chosen = np.where(mask, win["RRS_FB"], win["RRS_Class"])

    out = win[keys].copy()
    out["RRS_Class_Rolled"] = pd.Series(chosen).fillna(win["RRS_Class"])
    return out

def rollup_abc_majority_by_sku(sku_frame, keys, threshold=0.5):
    """
    Majority by distinct SKU count (A/B/C). If no class >= threshold,
    fallback to departmental Pareto on set sales (A to cum<=80%, B to 95%, else C).
    """
    df = sku_frame.copy()
    df["ABC_Class"] = df["ABC_Class"].astype(object)  # allow NaN
    # Count distinct SKUs per ABC within each set
    uni = df.drop_duplicates(subset=keys + ["invarticle_code","ABC_Class"])
    cnt = (uni.groupby(keys + ["ABC_Class"], dropna=False)["invarticle_code"]
              .nunique().rename("sku_cnt").reset_index())
    tot = cnt.groupby(keys, dropna=False)["sku_cnt"].sum().rename("sku_tot")
    cnt = cnt.merge(tot, on=keys, how="left")
    cnt["share"] = cnt["sku_cnt"] / cnt["sku_tot"].replace(0, np.nan)

    # Provisional winner
    win = (cnt.sort_values(keys + ["share","sku_cnt"],
                           ascending=[True]*len(keys) + [False, False])
              .drop_duplicates(subset=keys, keep="first")[keys + ["ABC_Class","share"]].copy()
              .rename(columns={"ABC_Class":"ABC_Prov"}))

    # Departmental Pareto fallback (on set sales)
    sales = df.groupby(keys, dropna=False)["total_qty"].sum().rename("set_qty").reset_index()
    dept_keys = ["department"] if "department" in df.columns else ["division"]
    tmp = sales.sort_values(dept_keys + ["set_qty"], ascending=[True]*len(dept_keys) + [False])
    tmp["tot_dept"] = tmp.groupby(dept_keys, dropna=False)["set_qty"].transform("sum")
    tmp["cum_share"] = tmp.groupby(dept_keys, dropna=False)["set_qty"].cumsum() / tmp["tot_dept"].replace(0, np.nan)
    tmp["ABC_fb"] = np.where(tmp["cum_share"] <= 0.80, "A",
                      np.where(tmp["cum_share"] <= 0.95, "B", "C"))
    fb = tmp[keys + ["ABC_fb"]]

    # Merge fallback; recompute mask AFTER merge so indices align
    win = win.merge(fb, on=keys, how="left")
    mask = win["share"].fillna(0) < float(threshold)
    chosen = np.where(mask, win["ABC_fb"], win["ABC_Prov"])

    out = win[keys].copy()
    out["ABC_Class_Rolled"] = pd.Series(chosen).fillna(win["ABC_Prov"])
    return out

def build_sets(sales_df, abc_map_df, rrs_map_df, majority_threshold):
    keep = ["invarticle_code","article","department","division","section","sze","colour","brand","total_qty","Count of total_qty"]
    for c in keep:
        if c not in sales_df.columns: sales_df[c] = np.nan
    sales = sales_df[keep].copy()
    for c in ["article","department","division","section","colour","brand","sze"]:
        sales[c] = sales[c].astype(str).str.strip().replace({"nan":np.nan,"None":np.nan})
    sales["total_qty"] = pd.to_numeric(sales["total_qty"], errors="coerce").fillna(0.0)
    sales["Count of total_qty"] = pd.to_numeric(sales["Count of total_qty"], errors="coerce").fillna(0).astype(int)
    sales["size_norm"] = sales["sze"].apply(norm_size)

    sku = sales.merge(abc_map_df, on="invarticle_code", how="left").merge(rrs_map_df, on="invarticle_code", how="left")

    # COLOUR SETS aggregation
    ck = ["division","department","brand","article","colour"]
    c_agg = sku.groupby(ck, dropna=False).agg(
        Total_Qty_Q1=("total_qty","sum"),
        SKU_Count=("invarticle_code","nunique"),
        Txn_Count=("Count of total_qty","sum"),
        Distinct_Sizes=("size_norm","nunique"),
    ).reset_index()
    c_agg = c_agg[c_agg["Total_Qty_Q1"]>0].copy()
    c_agg["Style_Density_Proxy"] = (c_agg["SKU_Count"]/c_agg["Distinct_Sizes"].replace(0,1)).round(1)

    # SIZE SETS aggregation
    sk = ["division","department","brand","article","size_norm"]
    s_agg = sku.groupby(sk, dropna=False).agg(
        Total_Qty_Q1=("total_qty","sum"),
        SKU_Count=("invarticle_code","nunique"),
        Txn_Count=("Count of total_qty","sum"),
        Distinct_Colours=("colour","nunique"),
    ).reset_index()
    s_agg = s_agg[s_agg["Total_Qty_Q1"]>0].copy()
    s_agg["Style_Density_Proxy"] = (s_agg["SKU_Count"]/s_agg["Distinct_Colours"].replace(0,1)).round(1)

    # RRS roll-up: majority by SKU
    c_rrs = rollup_rrs_majority_by_sku(sku[ck + ["invarticle_code","RRS_Class","total_qty","Count of total_qty"]],
                                       keys=ck, threshold=majority_threshold)
    s_rrs = rollup_rrs_majority_by_sku(sku[sk + ["invarticle_code","RRS_Class","total_qty","Count of total_qty"]],
                                       keys=sk, threshold=majority_threshold)

    # ABC roll-up: majority by SKU (fallback dept Pareto)
    c_abc = rollup_abc_majority_by_sku(sku[ck + ["invarticle_code","ABC_Class","total_qty"]],
                                       keys=ck, threshold=majority_threshold)
    s_abc = rollup_abc_majority_by_sku(sku[sk + ["invarticle_code","ABC_Class","total_qty"]],
                                       keys=sk, threshold=majority_threshold)

    colour_sets = c_agg.merge(c_rrs, on=ck, how="left").merge(c_abc, on=ck, how="left")
    size_sets   = s_agg.merge(s_rrs, on=sk, how="left").merge(s_abc, on=sk, how="left")

    # IDs
    colour_sets["Set_ID"] = ("COL-"+pd.util.hash_pandas_object(colour_sets[ck].fillna(""), index=False).astype(str).str[-10:])
    size_sets["Set_ID"]   = ("SIZ-"+pd.util.hash_pandas_object(size_sets[sk].fillna(""), index=False).astype(str).str[-10:])
    return colour_sets, size_sets

def zoning(abc, rrs, dens, pf_thr, bulk_thr):
    abc = str(abc) if pd.notna(abc) else ""
    rrs = str(rrs).capitalize() if pd.notna(rrs) else ""
    d = dens if pd.notna(dens) else 999
    if rrs == "Stranger":
        return "CrossDock/Staging"
    if abc in ("A","B"):
        if d <= pf_thr: return "PickFace+Bulk"
        elif d <= bulk_thr: return "Bulk"
        else: return "CrossDock/Staging"
    if abc == "C":
        if d <= bulk_thr: return "Bulk"
        else: return "CrossDock/Staging"
    return "Bulk"

def compute_minmax(set_df, set_type, festival_map, pf_policy_map, cov_min_days, cov_max_days, cap_total_pcs):
    df = set_df.copy()
    # Daily demand from Q1 → 13 weeks → 7 days/week (we will normalize to baseline later)
    df["D_day"] = (df["Total_Qty_Q1"] / 13.0) / 7.0
    df["Uplift"] = df["RRS_Class_Rolled"].map(festival_map).fillna(1.0)
    df["D_day_uplift"] = df["D_day"] * df["Uplift"]

    def pf_days(row):
        key = (row.get("ABC_Class_Rolled"), row.get("RRS_Class_Rolled"))
        return pf_policy_map.get(key, (0,0))
    pf_vals = df.apply(pf_days, axis=1, result_type="expand")
    df["PF_Min_days"], df["PF_Max_days"] = pf_vals[0], pf_vals[1]

    # Zoning already determined before calling this in main, but keep safe
    if "Zoning" not in df.columns:
        df["Zoning"] = [zoning(a,r,d, pf_density_threshold, bulk_density_threshold)
                        for a,r,d in zip(df["ABC_Class_Rolled"], df["RRS_Class_Rolled"], df["Style_Density_Proxy"])]

    df.loc[df["Zoning"]!="PickFace+Bulk", ["PF_Min_days","PF_Max_days"]] = (0,0)

    df["PF_Min_units_raw"] = df["D_day_uplift"] * df["PF_Min_days"]
    df["PF_Max_units_raw"] = df["D_day_uplift"] * df["PF_Max_days"]

    df["Target_Total_days"] = np.clip(np.where(df["ABC_Class_Rolled"].isin(["A","B"]), cov_max_days, cov_min_days),
                                      cov_min_days, cov_max_days)
    df["BulkTarget_days"] = (df["Target_Total_days"] - df["PF_Max_days"]).clip(lower=0)

    df["PF_Min_units"] = df["PF_Min_units_raw"].round().astype(int)
    df["PF_Max_units"] = df["PF_Max_units_raw"].round().astype(int)
    df["Bulk_Min_units"] = (df["D_day_uplift"] * (0.6*df["BulkTarget_days"])).round().astype(int)
    df["Bulk_Max_units"] = (df["D_day_uplift"] * (1.0*df["BulkTarget_days"])).round().astype(int)

    def tier(a, r):
        if a=="A" and r=="Runner": return 1
        if a=="A" and r=="Repeater": return 2
        if a=="B": return 3
        if a=="C" and r!="Stranger": return 4
        return 5
    df["Tier"] = [tier(a,r) for a,r in zip(df["ABC_Class_Rolled"], df["RRS_Class_Rolled"])]
    df["Bulk_Final"] = df["Bulk_Max_units"].copy()
    df["Final_Total"] = df["PF_Max_units"] + df["Bulk_Final"]
    df["Final_DaysCover"] = np.where(df["D_day_uplift"]>0, df["Final_Total"]/df["D_day_uplift"], 0.0)
    df["Set_Type"] = set_type
    return df

# Axis overrides & hybrid selection helpers
def load_axis_overrides(uploaded_csv):
    if uploaded_csv is None:
        return pd.DataFrame(columns=["division","department","brand","article","force_axis"])
    odf = pd.read_csv(uploaded_csv)
    odf.columns = [c.strip().lower() for c in odf.columns]
    need = ["division","department","brand","article","force_axis"]
    for c in need:
        if c not in odf.columns: odf[c] = np.nan
    odf["force_axis"] = odf["force_axis"].astype(str).str.strip().str.title()  # "ColourSet"/"SizeSet"
    return odf[need]

SIZE_DEPT_HINTS = {"Leggings","Bottoms","Denim","Chinos","Trousers","Innerwear","Briefs","Bras"}
COLOUR_DEPT_HINTS = {"Kurtis","Dresses","Tops","Tshirts","Shirts","Tees","Sweatshirts"}

def choose_axis_for_article(row, size_span, colour_span, size_density, colour_density, pf_thr, bulk_thr):
    dept = str(row.get("department","")).strip().title()
    if dept in SIZE_DEPT_HINTS: 
        return "SizeSet"
    if dept in COLOUR_DEPT_HINTS: 
        return "ColourSet"
    if (size_span >= 4) and (colour_span <= 3):
        return "SizeSet"
    if (colour_span >= 4) and (size_span <= 3):
        return "ColourSet"
    if size_density < colour_density:
        return "SizeSet"
    if colour_density < size_density:
        return "ColourSet"
    if (size_density <= pf_thr) and (colour_density > pf_thr):
        return "SizeSet"
    if (colour_density <= pf_thr) and (size_density > pf_thr):
        return "ColourSet"
    return "ColourSet"

# Bin Master normalization
def build_bin_master(df):
    d = df.copy()
    d.columns = [c.strip().lower() for c in d.columns]

    def get(colname):
        return d[colname] if colname in d.columns else pd.Series([np.nan]*len(d))

    floor = get("floor").astype(str)
    row = pd.to_numeric(get("row"), errors="coerce")
    bay = pd.to_numeric(get("bay"), errors="coerce")
    level = pd.to_numeric(get("level"), errors="coerce")
    scan = get("loc_code_scan").astype(str)
    hr = get("loc_code_hr").astype(str)

    bin_code = np.where(scan.notna() & (scan.str.strip()!=""), scan, hr)

    def floor_to_tier(x):
        s = str(x).upper()
        if s.startswith("F"):
            rest = s[1:]
            if rest.isdigit():
                return int(rest) + 1  # F00 => 1 (ground)
        return np.nan

    tier = floor.apply(floor_to_tier)
    bin_type = np.where(level <= pf_levels_max, "PF", "BULK")
    capacity_units = pd.Series([int(slot_capacity_default)]*len(d))

    bins = pd.DataFrame({
        "bin_code": bin_code,
        "zone": floor,
        "row": row,
        "bay": bay,
        "level": level,
        "tier": tier,
        "bin_type": bin_type,
        "capacity_units": capacity_units,
    })
    bins = bins.dropna(subset=["level", "bin_code"])
    bins = bins[bins["bin_code"].astype(str).str.strip() != ""]
    bins["row"] = pd.to_numeric(bins["row"], errors="coerce")
    bins["bay"] = pd.to_numeric(bins["bay"], errors="coerce")
    bins["level"] = pd.to_numeric(bins["level"], errors="coerce")
    bins = bins.dropna(subset=["row","bay","level"]).reset_index(drop=True)
    bins["sort_key"] = (bins["zone"].astype(str)+"|"+bins["row"].astype(str)+"|"+
                        bins["bay"].astype(str)+"|"+bins["level"].astype(str)+"|"+bins["bin_code"].astype(str))
    return bins

def assign_bins(sets_df, bins_df, pf_or_bulk="PF"):
    """Greedy assignment of sets to PF or BULK bins by priority and capacity. Returns (assign_df, diagnostics)."""
    df = sets_df.copy()
    if pf_or_bulk == "PF":
        cand = df[df["Zoning"]=="PickFace+Bulk"].copy()
        prio = {"A#Runner":1, "A#Repeater":2, "B#Runner":3, "B#Repeater":4, "C#Runner":5, "C#Repeater":6}
        cand["prio"] = [prio.get(f"{a}#{r}", 9) for a,r in zip(cand["ABC_Class_Rolled"], cand["RRS_Class_Rolled"])]
        cand["Need"] = cand["PF_Max_units"].clip(lower=0)
        bins_avail = bins_df[bins_df["bin_type"]=="PF"].copy()
    else:
        cand = df[df["Zoning"].isin(["PickFace+Bulk","Bulk"])].copy()
        prio = {"A#Runner":1, "A#Repeater":2, "B#Runner":3, "B#Repeater":4, "C#Runner":5, "C#Repeater":6, "C#Stranger":8}
        cand["prio"] = [prio.get(f"{a}#{r}", 9) for a,r in zip(cand["ABC_Class_Rolled"], cand["RRS_Class_Rolled"])]
        cand["Need"] = cand["Bulk_Final"].clip(lower=0)
        bins_avail = bins_df[bins_df["bin_type"]=="BULK"].copy()

    cand = cand[cand["Need"]>0].sort_values(["prio","Style_Density_Proxy","D_day_uplift"], ascending=[True,True,False]).reset_index(drop=True)
    bins_avail = bins_avail.sort_values(["zone","row","bay","level","bin_code"]).copy().reset_index(drop=True)
    bins_avail["available"] = bins_avail["capacity_units"].copy()

    assigns = []
    bin_idx = 0
    avail_col = bins_avail.columns.get_loc("available")

    for _, row in cand.iterrows():
        need = int(row["Need"])
        if need <= 0:
            continue
        while need > 0 and bin_idx < len(bins_avail):
            row_view = bins_avail.iloc[bin_idx]
            cap = int(row_view["available"])
            if cap <= 0:
                bin_idx += 1
                continue
            put = min(need, cap)
            assigns.append({
                "Set_ID": row.get("Set_ID"),
                "Set_Type": row.get("Set_Type"),
                "Division": row.get("division"),
                "Department": row.get("department"),
                "Brand": row.get("brand"),
                "Article": row.get("article"),
                "Colour_or_Size": row.get("colour") if "colour" in row.index else row.get("size_norm"),
                "ABC": row.get("ABC_Class_Rolled"),
                "RRS": row.get("RRS_Class_Rolled"),
                "Zoning": row.get("Zoning"),
                "Assigned_Qty": put,
                "Bin_Code": row_view["bin_code"],
                "Zone": row_view["zone"],
                "Row": row_view["row"],
                "Bay": row_view["bay"],
                "Level": row_view["level"],
                "Tier": row_view["tier"],
                "Bin_Type": row_view["bin_type"],
            })
            need -= put
            bins_avail.iat[bin_idx, avail_col] = cap - put
            if bins_avail.iat[bin_idx, avail_col] <= 0:
                bin_idx += 1
        if bin_idx >= len(bins_avail):
            break

    assigned_units = int(pd.DataFrame(assigns)["Assigned_Qty"].sum()) if assigns else 0
    diag = {
        "bins_available": int(len(bins_avail)),
        "bins_used": int((bins_avail["available"] < bins_avail["capacity_units"]).sum()),
        "total_need_units": int(cand["Need"].sum()),
        "assigned_units": assigned_units
    }
    return pd.DataFrame(assigns), diag

def apply_capacity_governor(drive_df, cap_total_pcs, pf_cap_pcs=None):
    """
    Two-stage governor:
      A) Cap PF_Max_units by PF physical capacity (if provided) by trimming Tier 5→1 toward PF_Min_units.
         If still over PF cap, allow dipping below PF_Min (last resort), Tier 5→1.
      B) Cap Total (PF + Bulk_Final) to warehouse capacity by trimming Bulk_Final Tier 5→1 toward Bulk_Min_units.
         If still over, trim PF below PF_Min (last resort), Tier 5→1.
    """
    df = drive_df.copy()

    # --- Stage A: PF physical capacity cap (optional) ---
    if pf_cap_pcs is not None and np.isfinite(pf_cap_pcs):
        pf_sum = int(df["PF_Max_units"].sum())
        pf_surplus = max(pf_sum - int(pf_cap_pcs), 0)
        if pf_surplus > 0:
            for t in [5,4,3,2,1]:
                if pf_surplus <= 0: break
                mask = (df["Tier"]==t) & (df["PF_Max_units"] > df["PF_Min_units"])
                flex = (df.loc[mask, "PF_Max_units"] - df.loc[mask, "PF_Min_units"]).clip(lower=0)
                flex_sum = int(flex.sum())
                if flex_sum > 0:
                    share = pf_surplus * (flex / flex_sum)
                    df.loc[mask, "PF_Max_units"] = (df.loc[mask, "PF_Max_units"] - share.clip(upper=flex)).round().astype(int)
                    pf_sum = int(df["PF_Max_units"].sum())
                    pf_surplus = max(pf_sum - int(pf_cap_pcs), 0)

            if pf_surplus > 0:
                for t in [5,4,3,2,1]:
                    if pf_surplus <= 0: break
                    mask = (df["Tier"]==t) & (df["PF_Max_units"] > 0)
                    flex = df.loc[mask, "PF_Max_units"].clip(lower=0)
                    flex_sum = int(flex.sum())
                    if flex_sum > 0:
                        share = pf_surplus * (flex / flex_sum)
                        df.loc[mask, "PF_Max_units"] = (df.loc[mask, "PF_Max_units"] - share.clip(upper=flex)).round().astype(int)
                        pf_sum = int(df["PF_Max_units"].sum())
                        pf_surplus = max(pf_sum - int(pf_cap_pcs), 0)

    # --- Stage B: total warehouse cap ---
    projected = int(df["PF_Max_units"].sum() + df["Bulk_Final"].sum())
    surplus = max(projected - int(cap_total_pcs), 0)
    if surplus > 0:
        for t in [5,4,3,2,1]:
            if surplus <= 0: break
            mask = df["Tier"]==t
            flex = (df.loc[mask,"Bulk_Final"] - df.loc[mask,"Bulk_Min_units"]).clip(lower=0)
            flex_sum = int(flex.sum())
            if flex_sum>0:
                share = surplus * (flex / flex_sum)
                df.loc[mask,"Bulk_Final"] = (df.loc[mask,"Bulk_Final"] - share.clip(upper=flex)).round().astype(int)
                projected = int(df["PF_Max_units"].sum() + df["Bulk_Final"].sum())
                surplus = max(projected - int(cap_total_pcs), 0)

    if surplus > 0:
        for t in [5,4,3,2,1]:
            if surplus <= 0: break
            mask = (df["Tier"]==t) & (df["PF_Max_units"] > 0)
            flex = df.loc[mask,"PF_Max_units"].clip(lower=0)
            flex_sum = int(flex.sum())
            if flex_sum>0:
                share = surplus * (flex / flex_sum)
                df.loc[mask,"PF_Max_units"] = (df.loc[mask,"PF_Max_units"] - share.clip(upper=flex)).round().astype(int)
                projected = int(df["PF_Max_units"].sum() + df["Bulk_Final"].sum())
                surplus = max(projected - int(cap_total_pcs), 0)

    df["Final_Total"] = df["PF_Max_units"] + df["Bulk_Final"]
    df["Final_DaysCover"] = np.where(df["D_day_uplift"]>0, df["Final_Total"]/df["D_day_uplift"], 0.0)
    return df

def build_consolidated(drive_df, pf_assign_df, bulk_assign_df):
    base_cols = [
        "Set_ID","Set_Type","division","department","brand","article",
        "colour","size_norm",
        "ABC_Class_Rolled","RRS_Class_Rolled","Style_Density_Proxy","Zoning",
        "D_day","D_day_uplift",
        "PF_Min_days","PF_Max_days","PF_Min_units","PF_Max_units",
        "Bulk_Min_units","Bulk_Max_units","Bulk_Final",
        "Final_Total","Final_DaysCover","Tier"
    ]
    base = drive_df.copy()
    for c in base_cols:
        if c not in base.columns:
            base[c] = np.nan
    base = base[base_cols].copy()
    base["Set_Key"] = np.where(base["Set_Type"].eq("ColourSet"), base["colour"], base["size_norm"])
    base.rename(columns={"colour":"Colour", "size_norm":"Size"}, inplace=True)

    pf = pf_assign_df.copy()
    if not pf.empty:
        pf["PF_Bin"] = pf["Zone"].astype(str)+"|R"+pf["Row"].astype(str)+"|B"+pf["Bay"].astype(str)+"|L"+pf["Level"].astype(str)
        pf_g = pf.groupby("Set_ID", as_index=False).agg(
            PF_Assigned_Qty=("Assigned_Qty","sum"),
            PF_Bins=("PF_Bin", lambda s: "; ".join(map(str, s)))
        )
    else:
        pf_g = base[["Set_ID"]].drop_duplicates().assign(PF_Assigned_Qty=0, PF_Bins="")

    bk = bulk_assign_df.copy()
    if not bk.empty:
        bk["Bulk_Bin"] = bk["Zone"].astype(str)+"|R"+bk["Row"].astype(str)+"|B"+bk["Bay"].astype(str)+"|L"+bk["Level"].astype(str)
        bk_g = bk.groupby("Set_ID", as_index=False).agg(
            Bulk_Assigned_Qty=("Assigned_Qty","sum"),
            Bulk_Bins=("Bulk_Bin", lambda s: "; ".join(map(str, s)))
        )
    else:
        bk_g = base[["Set_ID"]].drop_duplicates().assign(Bulk_Assigned_Qty=0, Bulk_Bins="")

    cons = base.merge(pf_g, on="Set_ID", how="left").merge(bk_g, on="Set_ID", how="left") \
               .fillna({"PF_Assigned_Qty":0, "Bulk_Assigned_Qty":0, "PF_Bins":"", "Bulk_Bins":""})
    cons["PF_Assigned_Qty"] = cons["PF_Assigned_Qty"].astype(int)
    cons["Bulk_Assigned_Qty"] = cons["Bulk_Assigned_Qty"].astype(int)
    cons["Total_Assigned_Qty"] = cons["PF_Assigned_Qty"] + cons["Bulk_Assigned_Qty"]
    cons["Set_Label"] = cons["Set_Type"].str.replace("Set","",regex=False) + ":" + cons["Set_Key"].astype(str)

    ordered = [
        "Set_ID","Set_Type","Set_Label","division","department","brand","article","Colour","Size",
        "ABC_Class_Rolled","RRS_Class_Rolled","Style_Density_Proxy","Zoning","Tier",
        "D_day","D_day_uplift",
        "PF_Min_days","PF_Max_days","PF_Min_units","PF_Max_units",
        "Bulk_Min_units","Bulk_Max_units","Bulk_Final",
        "Final_Total","Final_DaysCover",
        "PF_Assigned_Qty","Bulk_Assigned_Qty","Total_Assigned_Qty",
        "PF_Bins","Bulk_Bins"
    ]
    for c in ordered:
        if c not in cons.columns:
            cons[c] = np.nan
    return cons[ordered]

def build_kanban_triggers(drive_df, pf_assign_df, bulk_assign_df):
    """Compact, ops-ready table of Kanban cards for PF and Bulk."""
    base_cols = [
        "Set_ID","Set_Type","division","department","brand","article",
        "colour","size_norm","ABC_Class_Rolled","RRS_Class_Rolled","Tier",
        "Zoning","D_day_uplift","PF_Min_units","PF_Max_units",
        "Bulk_Min_units","Bulk_Final"
    ]
    df = drive_df[base_cols].copy()
    df["Set_Key"] = np.where(df["Set_Type"].eq("ColourSet"), df["colour"], df["size_norm"])
    df["Set_Label"] = df["Set_Type"].str.replace("Set","",regex=False) + ":" + df["Set_Key"].astype(str)
    df.rename(columns={"colour":"Colour","size_norm":"Size"}, inplace=True)

    pf_bins = pd.DataFrame(columns=["Set_ID","PF_Bins"])
    if pf_assign_df is not None and not pf_assign_df.empty:
        t = pf_assign_df.copy()
        t["PF_Bin"] = t["Zone"].astype(str)+"|R"+t["Row"].astype(str)+"|B"+t["Bay"].astype(str)+"|L"+t["Level"].astype(str)
        pf_bins = t.groupby("Set_ID", as_index=False).agg(PF_Bins=("PF_Bin", lambda s: "; ".join(map(str, s))))
    bk_bins = pd.DataFrame(columns=["Set_ID","Bulk_Bins"])
    if bulk_assign_df is not None and not bulk_assign_df.empty:
        t = bulk_assign_df.copy()
        t["Bulk_Bin"] = t["Zone"].astype(str)+"|R"+t["Row"].astype(str)+"|B"+t["Bay"].astype(str)+"|L"+t["Level"].astype(str)
        bk_bins = t.groupby("Set_ID", as_index=False).agg(Bulk_Bins=("Bulk_Bin", lambda s: "; ".join(map(str, s))))

    pf_df = df[(df["Zoning"].eq("PickFace+Bulk")) & (df["PF_Min_units"]>0)].copy()
    pf_df["Area"] = "PF"
    pf_df["Min_Qty"] = pf_df["PF_Min_units"].astype(int)
    pf_df["Target_Qty"] = pf_df["PF_Max_units"].astype(int)
    pf_df = pf_df.merge(pf_bins, on="Set_ID", how="left")
    pf_df["Bins"] = pf_df["PF_Bins"].fillna("")
    pf_df.drop(columns=["PF_Bins"], inplace=True)

    bk_df = df[df["Zoning"].isin(["PickFace+Bulk","Bulk"]) & (df["Bulk_Min_units"]>0)].copy()
    bk_df["Area"] = "BULK"
    bk_df["Min_Qty"] = bk_df["Bulk_Min_units"].astype(int)
    bk_df["Target_Qty"] = bk_df["Bulk_Final"].astype(int)
    bk_df = bk_df.merge(bk_bins, on="Set_ID", how="left")
    bk_df["Bins"] = bk_df["Bulk_Bins"].fillna("")
    bk_df.drop(columns=["Bulk_Bins"], inplace=True)

    out = pd.concat([pf_df, bk_df], ignore_index=True)
    out["Implied_Min_Days"] = np.where(out["D_day_uplift"]>0, out["Min_Qty"]/out["D_day_uplift"], 0.0)
    out["Implied_Target_Days"] = np.where(out["D_day_uplift"]>0, out["Target_Qty"]/out["D_day_uplift"], 0.0)

    cols = [
        "Set_ID","Set_Type","Set_Label","division","department","brand","article","Colour","Size",
        "ABC_Class_Rolled","RRS_Class_Rolled","Tier","Area",
        "Min_Qty","Target_Qty","Implied_Min_Days","Implied_Target_Days","D_day_uplift","Bins"
    ]
    return out[cols].sort_values(["Area","Tier","department","brand","article","Set_Label"]).reset_index(drop=True)

def compute_uplifted_daily_baseline(sales_df, rrs_map_df):
    """Compute baseline uplifted daily demand directly from the raw sales file (prevents overcount)."""
    s = sales_df.copy()
    qty = pd.to_numeric(s.get("total_qty", 0), errors="coerce").fillna(0.0)
    if "invarticle_code" not in s.columns:
        s.columns = [str(c) for c in s.columns]
    sku = s[["invarticle_code"]].copy()
    sku["total_qty"] = qty
    sku = sku.groupby("invarticle_code", as_index=False)["total_qty"].sum()

    rrs = (rrs_map_df.drop_duplicates(subset=["invarticle_code"]).copy()
           if rrs_map_df is not None and not rrs_map_df.empty else
           pd.DataFrame(columns=["invarticle_code","RRS_Class"]))
    uplift_map = {"Runner": festival_runner_repeater, "Repeater": festival_runner_repeater, "Stranger": festival_stranger}
    sku = sku.merge(rrs, on="invarticle_code", how="left")
    sku["uplift"] = sku["RRS_Class"].map(uplift_map).fillna(1.0)

    # Use 91 days for Q1 baseline
    daily = (sku["total_qty"] / 91.0) * sku["uplift"]
    return float(daily.sum())

# -------------------- Run / Display (Session-State) --------------------
if just_clicked_run:
    sales_df = read_sales(sales_file)
    if sales_df is None:
        st.error("Please upload the Q1 Cumulative Sales file."); st.stop()

    # ABC / RRS mapping
    def build_map(uploaded, label_col, label_val):
        if uploaded is None: return None
        t = read_xlsx_or_csv(uploaded)
        if t is None or t.empty: return None
        lc = [c.lower() for c in t.columns]
        cand = None
        for c in ["invarticle_code","sku","sku code","skucode","item","item code","articlecode","style code","style"]:
            if c in lc:
                cand = t.columns[lc.index(c)]
                break
        if cand is None: cand = t.columns[0]
        out = t[[cand]].rename(columns={cand:"invarticle_code"})
        out[label_col] = label_val
        return out.drop_duplicates()

    abc_frames = [
        build_map(a_file, "ABC_Class", "A"),
        build_map(b_file, "ABC_Class", "B"),
        build_map(c_file, "ABC_Class", "C"),
    ]
    abc_frames = [x for x in abc_frames if x is not None]
    abc_map_df = pd.concat(abc_frames, ignore_index=True).drop_duplicates() if abc_frames else pd.DataFrame(columns=["invarticle_code","ABC_Class"])

    rrs_frames = [
        build_map(runner_file, "RRS_Class", "Runner"),
        build_map(repeater_file, "RRS_Class", "Repeater"),
        build_map(stranger_file, "RRS_Class", "Stranger"),
    ]
    rrs_frames = [x for x in rrs_frames if x is not None]
    rrs_map_df = pd.concat(rrs_frames, ignore_index=True).drop_duplicates() if rrs_frames else pd.DataFrame(columns=["invarticle_code","RRS_Class"])

    # Optional Bin Master
    bins_df = None
    if bin_file is not None:
        raw = read_xlsx_or_csv(bin_file)
        if raw is not None and not raw.empty:
            bins_df = build_bin_master(raw)

    with st.spinner("Building sets (majority roll-ups), zoning & min–max..."):
        colour_sets, size_sets = build_sets(sales_df, abc_map_df, rrs_map_df, majority_threshold)

        # Zoning using majority-rolled classes
        colour_sets["Zoning"] = [zoning(a,r,d, pf_density_threshold, bulk_density_threshold)
                                 for a,r,d in zip(colour_sets["ABC_Class_Rolled"], colour_sets["RRS_Class_Rolled"], colour_sets["Style_Density_Proxy"])]
        size_sets["Zoning"] = [zoning(a,r,d, pf_density_threshold, bulk_density_threshold)
                               for a,r,d in zip(size_sets["ABC_Class_Rolled"], size_sets["RRS_Class_Rolled"], size_sets["Style_Density_Proxy"])]

        festival_map = {"Runner": festival_runner_repeater, "Repeater": festival_runner_repeater, "Stranger": festival_stranger}
        pf_policy_map = pf_policy

        colour_final = compute_minmax(colour_sets.assign(Set_Type="ColourSet"),
                                      "ColourSet", festival_map, pf_policy_map, cov_min, cov_max, cap_total)
        size_final   = compute_minmax(size_sets.assign(Set_Type="SizeSet"),
                                      "SizeSet", festival_map, pf_policy_map, cov_min, cov_max, cap_total)

        # Hybrid allocation decision (per article)
        art_keys = ["division","department","brand","article"]
        if hybrid_mode:
            c_metrics = colour_final.groupby(art_keys, dropna=False).agg(
                colour_span=("colour","nunique"),
                colour_sku=("SKU_Count","sum"),
                colour_qty=("Total_Qty_Q1","sum"),
                colour_density=("Style_Density_Proxy","mean")
            ).reset_index()
            s_metrics = size_final.groupby(art_keys, dropna=False).agg(
                size_span=("size_norm","nunique"),
                size_sku=("SKU_Count","sum"),
                size_qty=("Total_Qty_Q1","sum"),
                size_density=("Style_Density_Proxy","mean")
            ).reset_index()
            axis_df = pd.merge(c_metrics, s_metrics, on=art_keys, how="outer")
            for col in ["colour_span","size_span","colour_density","size_density"]:
                axis_df[col] = pd.to_numeric(axis_df[col], errors="coerce").fillna(0)

            odf = load_axis_overrides(override_file)
            if not odf.empty:
                axis_df = axis_df.merge(odf, on=art_keys, how="left")
            else:
                axis_df["force_axis"] = np.nan

            decisions = []
            for _, r in axis_df.iterrows():
                if pd.notna(r.get("force_axis")) and r["force_axis"] in ("ColourSet","SizeSet"):
                    decisions.append(r["force_axis"])
                else:
                    decisions.append(
                        choose_axis_for_article(
                            r,
                            int(r.get("size_span",0)), int(r.get("colour_span",0)),
                            float(r.get("size_density",999)), float(r.get("colour_density",999)),
                            pf_density_threshold, bulk_density_threshold
                        )
                    )
            axis_df["chosen_axis"] = decisions

            colour_with_axis = colour_final.merge(axis_df[art_keys+["chosen_axis"]], on=art_keys, how="left")
            size_with_axis   = size_final.merge(axis_df[art_keys+["chosen_axis"]], on=art_keys, how="left")
            drive_colour = colour_with_axis[colour_with_axis["chosen_axis"].eq("ColourSet")].copy()
            drive_size   = size_with_axis[size_with_axis["chosen_axis"].eq("SizeSet")].copy()
            drive_df = pd.concat([drive_colour, drive_size], ignore_index=True)
        else:
            alloc_axis = st.sidebar.radio("Allocation axis (non-hybrid)", options=["ColourSets","SizeSets"], index=0)
            drive_df = colour_final.copy() if alloc_axis=="ColourSets" else size_final.copy()

    # Debug expander (pre-normalization)
    with st.expander("Debug: classification & zoning distribution (pre-normalization)", expanded=False):
        st.write("ABC rolled counts:", drive_df["ABC_Class_Rolled"].value_counts(dropna=False).to_dict())
        st.write("RRS rolled counts:", drive_df["RRS_Class_Rolled"].value_counts(dropna=False).to_dict())
        st.write("Zoning counts:", drive_df["Zoning"].value_counts(dropna=False).to_dict())
        st.write("PF-eligible (PickFace+Bulk):", int((drive_df["Zoning"]=="PickFace+Bulk").sum()))
        st.write("Density quantiles:", drive_df["Style_Density_Proxy"].quantile([0.25,0.5,0.75,0.9,0.95]).to_dict())

    # PF capacity from Bin Master (for governor)
    pf_cap = None
    if bins_df is not None and isinstance(bins_df, pd.DataFrame) and not bins_df.empty:
        pf_cap = int(bins_df.loc[bins_df["bin_type"]=="PF","capacity_units"].sum())

    # ---- Normalize demand so sum(D_day_uplift) matches the baseline (prevents over-count) ----
    if baseline_override and baseline_override > 0:
        baseline_daily_uplift = float(baseline_override)
    else:
        baseline_daily_uplift = compute_uplifted_daily_baseline(sales_df, rrs_map_df)

    sum_drive_daily_uplift = float(drive_df["D_day_uplift"].sum())
    scale = 1.0
    if sum_drive_daily_uplift > 0 and baseline_daily_uplift > 0:
        scale = baseline_daily_uplift / sum_drive_daily_uplift

    if scale != 1.0:
        for col in ["D_day", "D_day_uplift",
                    "PF_Min_units_raw", "PF_Max_units_raw",
                    "PF_Min_units", "PF_Max_units",
                    "Bulk_Min_units", "Bulk_Max_units",
                    "Bulk_Final", "Final_Total"]:
            if col in drive_df.columns:
                drive_df[col] = (drive_df[col].astype(float) * scale)

        drive_df["Final_DaysCover"] = np.where(
            drive_df["D_day_uplift"]>0,
            drive_df["Final_Total"]/drive_df["D_day_uplift"],
            0.0
        )

    # ---- Apply capacity governors (PF cap, then Total cap) ----
    drive_df = apply_capacity_governor(drive_df, cap_total, pf_cap_pcs=pf_cap)

    # -------- Governed Summary (store in session) --------
    daily_demand_total = float(drive_df["D_day_uplift"].sum())
    pf_need = int(drive_df["PF_Max_units"].sum())
    bulk_need = int(drive_df["Bulk_Final"].sum())
    total_need = pf_need + bulk_need
    gov_cover_days = (total_need / daily_demand_total) if daily_demand_total > 0 else 0.0
    st.session_state["gov_summary"] = {
        "pf_cap": int(pf_cap) if pf_cap is not None else None,
        "pf_need": pf_need,
        "pf_cap_util": (pf_need / pf_cap) if pf_cap else None,
        "total_cap": int(cap_total),
        "total_need": int(total_need),
        "bulk_need": int(bulk_need),
        "daily_demand": daily_demand_total,
        "cover_days": float(gov_cover_days),
    }

    # Racking assignment (governed) + diagnostics
    pf_assign = pd.DataFrame()
    bulk_assign = pd.DataFrame()
    pf_diag = {"bins_available":0,"bins_used":0,"total_need_units":0,"assigned_units":0}
    bulk_diag = {"bins_available":0,"bins_used":0,"total_need_units":0,"assigned_units":0}
    if bins_df is not None and isinstance(bins_df, pd.DataFrame) and not bins_df.empty:
        with st.spinner("Assigning sets to PF/Bulk bins..."):
            pf_assign, pf_diag = assign_bins(drive_df, bins_df, pf_or_bulk="PF")
            bulk_assign, bulk_diag = assign_bins(drive_df, bins_df, pf_or_bulk="BULK")

    # Reason codes
    drive_flags = drive_df.copy()
    drive_flags["PF_Eligible"] = drive_flags["Zoning"].eq("PickFace+Bulk")
    drive_flags["PF_Need"] = drive_flags["PF_Max_units"].fillna(0).astype(int)
    drive_flags["Bulk_Need"] = drive_flags["Bulk_Final"].fillna(0).astype(int)
    pf_flags = drive_flags[["Set_ID","PF_Eligible","PF_Need"]].drop_duplicates()
    bk_flags = drive_flags[["Set_ID","Bulk_Need"]].drop_duplicates()

    def reason_pf(row):
        if not bool(row.get("PF_Eligible", False)):
            return "Not PF-eligible (zoning)"
        if int(row.get("PF_Min_units",0))==0 and int(row.get("PF_Max_units",0))==0:
            return "No PF need"
        if int(row.get("PF_Assigned_Qty",0))==0:
            return "No PF bins available / capacity exhausted"
        return ""

    def reason_bulk(row):
        if int(row.get("Bulk_Min_units",0))==0 and int(row.get("Bulk_Final",0))==0:
            return "No Bulk need (or squeezed by capacity governor)"
        if int(row.get("Bulk_Assigned_Qty",0))==0:
            return "No Bulk bins available / capacity exhausted"
        return ""

    consolidated_df = build_consolidated(drive_df, pf_assign, bulk_assign)
    consolidated_df = consolidated_df.merge(pf_flags, on="Set_ID", how="left").merge(bk_flags, on="Set_ID", how="left")
    consolidated_df["PF_Reason"] = consolidated_df.apply(reason_pf, axis=1)
    consolidated_df["Bulk_Reason"] = consolidated_df.apply(reason_bulk, axis=1)

    # Save to session
    st.session_state["colour_final"] = colour_final
    st.session_state["size_final"] = size_final
    st.session_state["pf_assign"] = pf_assign
    st.session_state["bulk_assign"] = bulk_assign
    st.session_state["drive_df"] = drive_df
    st.session_state["consolidated_df"] = consolidated_df
    st.session_state["pf_diag"] = pf_diag
    st.session_state["bulk_diag"] = bulk_diag
    st.session_state["results_ready"] = True

# -------------------- Display from session --------------------
if st.session_state["results_ready"]:
    colour_final = st.session_state["colour_final"]
    size_final = st.session_state["size_final"]
    pf_assign = st.session_state["pf_assign"]
    bulk_assign = st.session_state["bulk_assign"]
    drive_df = st.session_state["drive_df"]
    consolidated_df = st.session_state["consolidated_df"]
    pf_diag = st.session_state["pf_diag"] or {}
    bulk_diag = st.session_state["bulk_diag"] or {}
    gs = st.session_state.get("gov_summary", {}) or {}

    st.success("Planner run complete. Explore below and download outputs.")
    st.caption("Hybrid allocation (if enabled) selects Colour or Size per article; racking & capacity use the chosen axis only.")

    # -------- Governed Summary panel --------
    st.subheader("Governed Summary")
    colA, colB, colC, colD = st.columns(4)
    with colA:
        pf_need_val = gs.get("pf_need", 0)
        pf_cap_val = gs.get("pf_cap")
        st.metric("PF need (pcs)", f"{pf_need_val:,}",
                  delta=(f"cap {pf_cap_val:,}" if pf_cap_val is not None else None))
    with colB:
        util = gs.get("pf_cap_util")
        st.metric("PF cap utilisation",
                  f"{util*100:.1f}%" if util is not None else "—")
    with colC:
        st.metric("Total governed (pcs)",
                  f"{gs.get('total_need', 0):,}",
                  delta=f"cap {gs.get('total_cap', 0):,}")
    with colD:
        st.metric("Implied cover (days)",
                  f"{gs.get('cover_days', 0.0):.1f}")
    st.caption(f"Uplifted daily demand used: {gs.get('daily_demand', 0):,.0f} pcs/day")

    if util is not None and util > 1.0:
        st.warning("PF need exceeds PF physical capacity. Targets were trimmed, but please review PF thresholds or slot capacity.")
    if gs.get("total_need", 0) > gs.get("total_cap", 0):
        st.warning("Governed total still above capacity. Review cover bands/density thresholds.")

    # ColourSets
    st.subheader("Colour Sets — Zoning & Min–Max (preview)")
    st.dataframe(colour_final.head(500))
    st.caption(f"Colour Sets total rows: {len(colour_final):,}")
    st.download_button(
        "Download Colour Sets (CSV, full)",
        data=colour_final.to_csv(index=False).encode("utf-8"),
        file_name="ColourSets_full.csv",
        mime="text/csv",
        use_container_width=True
    )

    # SizeSets
    st.subheader("Size Sets — Zoning & Min–Max (preview)")
    st.dataframe(size_final.head(500))
    st.caption(f"Size Sets total rows: {len(size_final):,}")
    st.download_button(
        "Download Size Sets (CSV, full)",
        data=size_final.to_csv(index=False).encode("utf-8"),
        file_name="SizeSets_full.csv",
        mime="text/csv",
        use_container_width=True
    )

    # Bin Utilization Summary
    st.subheader("Bin Utilization Summary")
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Pick-Face (PF) bins**")
        st.write({
            "PF bins available": pf_diag.get("bins_available", 0),
            "PF bins used (≥1 unit)": pf_diag.get("bins_used", 0),
            "PF total need (units)": pf_diag.get("total_need_units", 0),
            "PF assigned (units)": pf_diag.get("assigned_units", 0),
            "PF unassigned (units)": max(pf_diag.get("total_need_units", 0) - pf_diag.get("assigned_units", 0), 0)
        })
    with col2:
        st.markdown("**Bulk bins**")
        st.write({
            "Bulk bins available": bulk_diag.get("bins_available", 0),
            "Bulk bins used (≥1 unit)": bulk_diag.get("bins_used", 0),
            "Bulk total need (units)": bulk_diag.get("total_need_units", 0),
            "Bulk assigned (units)": bulk_diag.get("assigned_units", 0),
            "Bulk unassigned (units)": max(bulk_diag.get("total_need_units", 0) - bulk_diag.get("assigned_units", 0), 0)
        })

    # Racking assignment previews
    if pf_assign is not None and not pf_assign.empty:
        st.subheader("Pick-Face Assignments (preview)")
        st.dataframe(pf_assign.head(500))
        st.caption(f"PF assignments rows: {len(pf_assign):,}")
        st.download_button(
            "Download PF Assignments (CSV, full)",
            data=pf_assign.to_csv(index=False).encode("utf-8"),
            file_name="PF_Assignments_full.csv",
            mime="text/csv",
            use_container_width=True
        )
    if bulk_assign is not None and not bulk_assign.empty:
        st.subheader("Bulk Assignments (preview)")
        st.dataframe(bulk_assign.head(500))
        st.caption(f"Bulk assignments rows: {len(bulk_assign):,}")
        st.download_button(
            "Download Bulk Assignments (CSV, full)",
            data=bulk_assign.to_csv(index=False).encode("utf-8"),
            file_name="Bulk_Assignments_full.csv",
            mime="text/csv",
            use_container_width=True
        )

    # Consolidated Plan
    st.subheader("Consolidated Storage Plan (Min–Max + PF/Bulk bins) — preview")
    st.dataframe(consolidated_df.head(500))
    st.caption(f"Consolidated rows: {len(consolidated_df):,}")
    st.download_button(
        "Download Consolidated Storage Plan (CSV, full)",
        data=consolidated_df.to_csv(index=False).encode("utf-8"),
        file_name="Consolidated_Storage_Plan.csv",
        mime="text/csv",
        use_container_width=True
    )

    # --- Kanban triggers (optional) ---
    kanban_df = None
    if show_kanban:
        st.subheader("🔁 Kanban Triggers — PF/Bulk (operational)")
        kanban_df = build_kanban_triggers(drive_df, pf_assign, bulk_assign)
        st.dataframe(kanban_df.head(500))
        st.caption(f"Kanban rows: {len(kanban_df):,}  •  One row per trigger (PF or BULK) with target replenishment level.")
        st.download_button(
            "Download Kanban Triggers (CSV)",
            data=kanban_df.to_csv(index=False).encode("utf-8"),
            file_name="Kanban_Triggers.csv",
            mime="text/csv",
            use_container_width=True
        )

    # Excel export (full)
    def to_excel(col_df, size_df, pf_df=None, bulk_df=None, consolidated=None, kanban=None):
        output = BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            notes = [
                "Warehouse Planner Lite Output",
                "Tabs: ColourSets, SizeSets, PF_Assignments, Bulk_Assignments, Consolidated, (optional) Kanban.",
                "Zone=floor; Tier=F00->1; PF levels <= user setting (default 1–3); capacity per slot configurable.",
                "Hybrid mode selects Colour or Size per article; only the chosen axis feeds capacity & racking.",
                "Reason codes indicate why PF/Bulk bins may be blank in Consolidated."
            ]
            gs_loc = st.session_state.get("gov_summary", {}) or {}
            if gs_loc:
                notes.append(f"Governed PF need: {gs_loc.get('pf_need',0):,}  | PF cap: {gs_loc.get('pf_cap',0) if gs_loc.get('pf_cap') else '—'}")
                notes.append(f"Governed Total: {gs_loc.get('total_need',0):,}  | Total cap: {gs_loc.get('total_cap',0):,}  | Cover days: {gs_loc.get('cover_days',0):.1f}")
            pd.DataFrame({"Notes":notes}).to_excel(writer, sheet_name="README", index=False)

            col_df.to_excel(writer, sheet_name="ColourSets", index=False)
            size_df.to_excel(writer, sheet_name="SizeSets", index=False)
            if pf_df is not None and not pf_df.empty:
                pf_df.to_excel(writer, sheet_name="PF_Assignments", index=False)
            if bulk_df is not None and not bulk_df.empty:
                bulk_df.to_excel(writer, sheet_name="Bulk_Assignments", index=False)
            if consolidated is not None and not consolidated.empty:
                consolidated.to_excel(writer, sheet_name="Consolidated", index=False)
            if kanban is not None and isinstance(kanban, pd.DataFrame) and not kanban.empty:
                kanban.to_excel(writer, sheet_name="Kanban", index=False)
        return output.getvalue()

    xls_bytes = to_excel(colour_final, size_final, pf_assign, bulk_assign, consolidated_df, kanban_df)
    st.download_button(
        "Download Planner Output (Excel, full)",
        data=xls_bytes,
        file_name="WarehousePlannerLite_Output.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True
    )

    if st.button("Clear Results"):
        for k in ["results_ready","colour_final","size_final","pf_assign","bulk_assign","drive_df",
                  "consolidated_df","pf_diag","bulk_diag","gov_summary"]:
            st.session_state[k] = None
        st.experimental_rerun()
else:
    st.info("Upload files (and optional Bin Master) then click **Run Planner**.")

