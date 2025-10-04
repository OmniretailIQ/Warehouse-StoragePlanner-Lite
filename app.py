import streamlit as st
import pandas as pd
import numpy as np
from io import BytesIO

st.set_page_config(page_title="Warehouse Planner Lite (Sets)", layout="wide")

# ====================== Sidebar controls ======================
st.sidebar.title("Planner Controls")

cap_total = st.sidebar.number_input("Warehouse capacity (pieces)", value=680000, step=10000)

festival_runner_repeater = st.sidebar.number_input("Festival uplift: Runners/Repeaters ×", value=2.0, step=0.25, format="%.2f")
festival_stranger = st.sidebar.number_input("Festival uplift: Strangers ×", value=1.25, step=0.05, format="%.2f")

pf_density_threshold = st.sidebar.slider("Pick-Face density threshold (≤ x is PF-friendly)", 1, 10, 3)
bulk_density_threshold = st.sidebar.slider("Bulk density threshold (≤ x is Bulk; > goes CrossDock)", 4, 20, 10)

cov_min = st.sidebar.slider("Overall days cover min", 5, 15, 10)
cov_max = st.sidebar.slider("Overall days cover max", 8, 20, 12)

st.sidebar.caption("Density: 1–3 = PF-friendly; 4–10 = Bulk; >10 = CrossDock. Strangers always CrossDock.")

# Racking defaults per your spec
pf_levels_max = st.sidebar.number_input("Pick-Face max level (1=bottom; default 3)", value=3, min_value=1, max_value=10)
slot_capacity_default = st.sidebar.number_input("Capacity per slot (pieces)", value=60, step=5)

# ABC/RRS→PF bands (defaults)
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

# ====================== Page header ======================
st.title("Warehouse Planner Lite — Set-level (Colour/Size)")
st.markdown("""
Upload **Q1 cumulative sales** and **ABC/RRS SKU lists**.  
Optional: upload **Bin Master** for Pick-Face/Bulk racking assignment (Zone/Floor → Row → Bay → Level/Tier).
""")

# ====================== Uploaders ======================
sales_file = st.file_uploader("Q1 Cumulative Sales (Excel/CSV)", type=["xlsx","csv"])
a_file = st.file_uploader("A Class SKUs (Excel/CSV)", type=["xlsx","csv"])
b_file = st.file_uploader("B Class SKUs (Excel/CSV)", type=["xlsx","csv"])
c_file = st.file_uploader("C Class SKUs (Excel/CSV)", type=["xlsx","csv"])

runner_file = st.file_uploader("Runner SKUs (Excel/CSV)", type=["xlsx","csv"])
repeater_file = st.file_uploader("Repeater SKUs (Excel/CSV)", type=["xlsx","csv"])
stranger_file = st.file_uploader("Stranger SKUs (Excel/CSV)", type=["xlsx","csv"])

bin_file = st.file_uploader("Bin Master (Excel or CSV)", type=["xlsx","csv"])

run_btn = st.button("Run Planner")

# ====================== Helpers ======================
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
    return s.replace("FREESIZE","FREE").replace("FREE SIZE","FREE").replace("XXXL","3XL").replace("2XL","XXL")

@st.cache_data(show_spinner=False)
def build_sets(sales_df, abc_map_df, rrs_map_df):
    # normalize / guard missing columns
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
    sku["ABC_Prio"] = sku["ABC_Class"].map({"A":3,"B":2,"C":1}).fillna(0).astype(int)

    # Colour Sets
    ck = ["division","department","brand","article","colour"]
    c_agg = sku.groupby(ck, dropna=False).agg(
        Total_Qty_Q1=("total_qty","sum"),
        SKU_Count=("invarticle_code","nunique"),
        Txn_Count=("Count of total_qty","sum"),
        Distinct_Sizes=("size_norm","nunique"),
        ABC_Prio_Max=("ABC_Prio","max"),
    ).reset_index()
    c_agg = c_agg[c_agg["Total_Qty_Q1"] > 0].copy()
    c_agg["Style_Density_Proxy"] = (c_agg["SKU_Count"]/c_agg["Distinct_Sizes"].replace(0,1)).round(1)
    c_agg["ABC_Class_Rolled"] = c_agg["ABC_Prio_Max"].map({3:"A",2:"B",1:"C",0:np.nan})

    c_rrs = sku.loc[:, ck+["RRS_Class","total_qty"]].copy()
    c_rrs["RRS_Class"] = c_rrs["RRS_Class"].fillna("Unknown")
    c_rrs = c_rrs.groupby(ck+["RRS_Class"], as_index=False)["total_qty"].sum().sort_values(
        ck+["total_qty"], ascending=[True,True,True,True,True,False])
    c_rrs_top = c_rrs.drop_duplicates(subset=ck, keep="first").rename(columns={"RRS_Class":"RRS_Class_Rolled"})
    colour_sets = c_agg.merge(c_rrs_top[ck+["RRS_Class_Rolled"]], on=ck, how="left")
    colour_sets["Set_ID"] = ("COL-"+pd.util.hash_pandas_object(colour_sets[ck].fillna(""), index=False).astype(str).str[-10:])

    # Size Sets
    sk = ["division","department","brand","article","size_norm"]
    s_agg = sku.groupby(sk, dropna=False).agg(
        Total_Qty_Q1=("total_qty","sum"),
        SKU_Count=("invarticle_code","nunique"),
        Txn_Count=("Count of total_qty","sum"),
        Distinct_Colours=("colour","nunique"),
        ABC_Prio_Max=("ABC_Prio","max"),
    ).reset_index()
    s_agg = s_agg[s_agg["Total_Qty_Q1"] > 0].copy()
    s_agg["Style_Density_Proxy"] = (s_agg["SKU_Count"]/s_agg["Distinct_Colours"].replace(0,1)).round(1)
    s_agg["ABC_Class_Rolled"] = s_agg["ABC_Prio_Max"].map({3:"A",2:"B",1:"C",0:np.nan})

    s_rrs = sku.loc[:, sk+["RRS_Class","total_qty"]].copy()
    s_rrs["RRS_Class"] = s_rrs["RRS_Class"].fillna("Unknown")
    s_rrs = s_rrs.groupby(sk+["RRS_Class"], as_index=False)["total_qty"].sum().sort_values(
        sk+["total_qty"], ascending=[True,True,True,True,True,False])
    s_rrs_top = s_rrs.drop_duplicates(subset=sk, keep="first").rename(columns={"RRS_Class":"RRS_Class_Rolled"})
    size_sets = s_agg.merge(s_rrs_top[sk+["RRS_Class_Rolled"]], on=sk, how="left")
    size_sets["Set_ID"] = ("SIZ-"+pd.util.hash_pandas_object(size_sets[sk].fillna(""), index=False).astype(str).str[-10:])

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
    # daily demand + festival uplift
    df["D_day"] = (df["Total_Qty_Q1"] / 13.0) / 7.0
    df["Uplift"] = df["RRS_Class_Rolled"].map(festival_map).fillna(1.0)
    df["D_day_uplift"] = df["D_day"] * df["Uplift"]

    # PF bands
    def pf_days(row):
        key = (row.get("ABC_Class_Rolled"), row.get("RRS_Class_Rolled"))
        return pf_policy_map.get(key, (0,0))
    pf_vals = df.apply(pf_days, axis=1, result_type="expand")
    df["PF_Min_days"], df["PF_Max_days"] = pf_vals[0], pf_vals[1]

    # Zoning by density
    df["Zoning"] = [zoning(a,r,d, pf_density_threshold, bulk_density_threshold)
                    for a,r,d in zip(df["ABC_Class_Rolled"], df["RRS_Class_Rolled"], df["Style_Density_Proxy"])]

    # PF only for PF zones
    df.loc[df["Zoning"]!="PickFace+Bulk", ["PF_Min_days","PF_Max_days"]] = (0,0)

    # qty calc
    df["PF_Min_units_raw"] = df["D_day_uplift"] * df["PF_Min_days"]
    df["PF_Max_units_raw"] = df["D_day_uplift"] * df["PF_Max_days"]

    df["Target_Total_days"] = np.clip(np.where(df["ABC_Class_Rolled"].isin(["A","B"]), cov_max_days, cov_min_days),
                                      cov_min_days, cov_max_days)
    df["BulkTarget_days"] = (df["Target_Total_days"] - df["PF_Max_days"]).clip(lower=0)

    df["PF_Min_units"] = df["PF_Min_units_raw"].round().astype(int)
    df["PF_Max_units"] = df["PF_Max_units_raw"].round().astype(int)
    df["Bulk_Min_units"] = (df["D_day_uplift"] * (0.6*df["BulkTarget_days"])).round().astype(int)
    df["Bulk_Max_units"] = (df["D_day_uplift"] * (1.0*df["BulkTarget_days"])).round().astype(int)

    # Capacity governor
    def tier(a, r):
        if a=="A" and r=="Runner": return 1
        if a=="A" and r=="Repeater": return 2
        if a=="B": return 3
        if a=="C" and r!="Stranger": return 4
        return 5
    df["Tier"] = [tier(a,r) for a,r in zip(df["ABC_Class_Rolled"], df["RRS_Class_Rolled"])]
    df["Bulk_Final"] = df["Bulk_Max_units"].copy()

    projected = int(df["PF_Max_units"].sum() + df["Bulk_Final"].sum())
    surplus = max(projected - cap_total_pcs, 0)

    if surplus > 0:
        # reduce Tier >=3 first
        for t in [3,4,5]:
            if surplus <= 0: break
            mask = df["Tier"]==t
            flex = (df.loc[mask,"Bulk_Final"] - df.loc[mask,"Bulk_Min_units"]).clip(lower=0)
            flex_sum = int(flex.sum())
            if flex_sum>0:
                share = surplus * (flex / flex_sum)
                df.loc[mask,"Bulk_Final"] = (df.loc[mask,"Bulk_Final"] - share.clip(upper=flex)).round().astype(int)
                projected = int(df["PF_Max_units"].sum() + df["Bulk_Final"].sum())
                surplus = max(projected - cap_total_pcs, 0)

        # then Tier 2, then Tier 1
        for t in [2,1]:
            if surplus <= 0: break
            mask = df["Tier"]==t
            flex = (df.loc[mask,"Bulk_Final"] - df.loc[mask,"Bulk_Min_units"]).clip(lower=0)
            flex_sum = int(flex.sum())
            if flex_sum>0:
                share = surplus * (flex / flex_sum)
                df.loc[mask,"Bulk_Final"] = (df.loc[mask,"Bulk_Final"] - share.clip(upper=flex)).round().astype(int)
                projected = int(df["PF_Max_units"].sum() + df["Bulk_Final"].sum())
                surplus = max(projected - cap_total_pcs, 0)

    df["Final_Total"] = df["PF_Max_units"] + df["Bulk_Final"]
    df["Final_DaysCover"] = np.where(df["D_day_uplift"]>0, df["Final_Total"]/df["D_day_uplift"], 0.0)
    df["Set_Type"] = set_type
    return df

# --- Bin Master normalization per your structure ---
def build_bin_master(df):
    """
    Expected input columns (case-insensitive):
      floor, row, bay, level, slot, loc_code_hr, loc_code_scan
    Mapping:
      zone := floor
      tier := F00->1, F01->2, ...
      bin_code := loc_code_scan (fallback to loc_code_hr)
      bin_type := PF if level <= pf_levels_max else BULK
      capacity_units := slot_capacity_default (each row ≈ one slot)
    """
    d = df.copy()
    d.columns = [c.strip().lower() for c in d.columns]

    def get(colname):
        return d[colname] if colname in d.columns else pd.Series([np.nan]*len(d))

    floor = get("floor").astype(str)
    row = pd.to_numeric(get("row"), errors="coerce")
    bay = pd.to_numeric(get("bay"), errors="coerce")
    level = pd.to_numeric(get("level"), errors="coerce")
    slot = get("slot")  # reserved if needed later
    scan = get("loc_code_scan").astype(str)
    hr = get("loc_code_hr").astype(str)

    bin_code = np.where(scan.notna() & (scan.str.strip()!=""), scan, hr)

    def floor_to_tier(x):
        s = str(x).upper()
        if s.startswith("F"):
            rest = s[1:]
            if rest.isdigit():
                return int(rest) + 1  # F00 => 1 (ground), F01 => 2, ...
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

    bins["sort_key"] = bins["zone"].astype(str)+"|"+bins["row"].astype(str)+"|"+bins["bay"].astype(str)+"|"+bins["level"].astype(str)+"|"+bins["bin_code"].astype(str)
    return bins

def assign_bins(sets_df, bins_df, pf_or_bulk="PF"):
    """Greedy assignment of sets to PF or BULK bins by priority and capacity."""
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

    # FIX: reset index after sort and use positional indexing
    bins_avail = bins_avail.sort_values(["zone","row","bay","level","bin_code"]).copy().reset_index(drop=True)
    bins_avail["available"] = bins_avail["capacity_units"].copy()

    assigns = []
    bin_idx = 0
    # cache column index for fast .iat updates
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

    return pd.DataFrame(assigns)

# ====================== Run ======================
if run_btn:
    sales_df = read_sales(sales_file)
    if sales_df is None:
        st.error("Please upload the Q1 Cumulative Sales file."); st.stop()

    # ABC maps (auto-detect SKU column)
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
        if cand is None:
            cand = t.columns[0]
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

    with st.spinner("Building sets and computing zoning & min–max..."):
        colour_sets, size_sets = build_sets(sales_df, abc_map_df, rrs_map_df)

        # Zoning (ABC×RRS + density thresholds)
        colour_sets["Zoning"] = [zoning(a,r,d, pf_density_threshold, bulk_density_threshold)
                                 for a,r,d in zip(colour_sets["ABC_Class_Rolled"], colour_sets["RRS_Class_Rolled"], colour_sets["Style_Density_Proxy"])]
        size_sets["Zoning"] = [zoning(a,r,d, pf_density_threshold, bulk_density_threshold)
                               for a,r,d in zip(size_sets["ABC_Class_Rolled"], size_sets["RRS_Class_Rolled"], size_sets["Style_Density_Proxy"])]

        # Festival map & PF policy
        festival_map = {"Runner": festival_runner_repeater, "Repeater": festival_runner_repeater, "Stranger": festival_stranger}
        pf_policy_map = pf_policy

        colour_final = compute_minmax(colour_sets.assign(Set_Type="ColourSet"),
                                      "ColourSet", festival_map, pf_policy_map, cov_min, cov_max, cap_total)
        size_final = compute_minmax(size_sets.assign(Set_Type="SizeSet"),
                                    "SizeSet", festival_map, pf_policy_map, cov_min, cov_max, cap_total)

    st.success("Planner run complete. Explore below and download outputs.")

    # ====================== Display (preview) ======================
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

    # ====================== Racking Assignment (if Bin Master uploaded) ======================
    pf_assign = pd.DataFrame()
    bulk_assign = pd.DataFrame()
    if bins_df is not None:
        st.subheader("Racking Assignment (using Bin Master)")
        pool = pd.concat([colour_final, size_final], ignore_index=True)
        pf_assign = assign_bins(pool, bins_df, pf_or_bulk="PF")
        bulk_assign = assign_bins(pool, bins_df, pf_or_bulk="BULK")

        if not pf_assign.empty:
            st.markdown("**Pick-Face Assignments (preview)**")
            st.dataframe(pf_assign.head(500))
            st.caption(f"PF assignments rows: {len(pf_assign):,}")
            st.download_button(
                "Download PF Assignments (CSV, full)",
                data=pf_assign.to_csv(index=False).encode("utf-8"),
                file_name="PF_Assignments_full.csv",
                mime="text/csv",
                use_container_width=True
            )
        else:
            st.info("No PF assignments (either no PF bins or no PF-eligible sets).")

        if not bulk_assign.empty:
            st.markdown("**Bulk Assignments (preview)**")
            st.dataframe(bulk_assign.head(500))
            st.caption(f"Bulk assignments rows: {len(bulk_assign):,}")
            st.download_button(
                "Download Bulk Assignments (CSV, full)",
                data=bulk_assign.to_csv(index=False).encode("utf-8"),
                file_name="Bulk_Assignments_full.csv",
                mime="text/csv",
                use_container_width=True
            )
        else:
            st.info("No Bulk assignments (either no Bulk bins or no Bulk need).")

    # ====================== Excel Download (always full) ======================
    def to_excel(col_df, size_df, pf_df=None, bulk_df=None):
        output = BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            pd.DataFrame({"Notes":[
                "Warehouse Planner Lite Output",
                "Tabs: ColourSets, SizeSets, PF_Assignments, Bulk_Assignments (if Bin Master uploaded).",
                "Zone=floor; Tier=F00->1; PF levels <= user setting (default 1–3); capacity per slot configurable.",
                "Contains ABC/RRS roll-ups, Style Density, Zoning, festival-aware Min–Max, capacity-governed totals."
            ]}).to_excel(writer, sheet_name="README", index=False)
            col_df.to_excel(writer, sheet_name="ColourSets", index=False)
            size_df.to_excel(writer, sheet_name="SizeSets", index=False)
            if pf_df is not None and not pf_df.empty:
                pf_df.to_excel(writer, sheet_name="PF_Assignments", index=False)
            if bulk_df is not None and not bulk_df.empty:
                bulk_df.to_excel(writer, sheet_name="Bulk_Assignments", index=False)
        return output.getvalue()

    xls_bytes = to_excel(colour_final, size_final, pf_assign, bulk_assign)
    st.download_button(
        "Download Planner Output (Excel, full)",
        data=xls_bytes,
        file_name="WarehousePlannerLite_Output.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True
    )

else:
    st.info("Upload files (and optional Bin Master) then click **Run Planner**.")


