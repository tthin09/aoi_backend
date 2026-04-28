"""
AOI Data Collection — v11.0 Final
===================================
Fix so với v10:
  1. Tách TOP/BOT riêng (PROGRAM_1_FAB_G_TOP, PROGRAM_1_FAB_G_BOT...)
  2. Dùng ResultAfter per ComponentGUID để classify PASS/FAIL
     ResultAfter = kết quả sau khi đã qua repair
     PASS = đã sửa xong hoặc chưa bị lỗi
     FAIL = vẫn còn lỗi sau repair (lỗi thật)
    3. PASS: lấy từ TB_AOIDefect (không cần join Detail)
    4. FAIL: join Detail để lấy ROI info
    5. Auto-reconnect khi DB bị timeout

bo_type examples:
  PROGRAM_1_FAB_G_TOP, PROGRAM_1_FAB_G_BOT
  PROGRAM_1_FAB_D_TOP, PROGRAM_1_FAB_D_BOT
  PROGRAM_1_5_BOT
  HPCC2_FABA_REV02

Metadata CSV:
  comp_guid, detail_guid, label, defect,
  bo_type, barcode, pkg_type, uname,
  comp_width, comp_height,
  roi_left, roi_top, roi_right, roi_bottom,
    center_x, center_y,
    path_2d
"""

import os
import pandas as pd
import pyodbc
from sqlalchemy import create_engine

# ============================================================
# CONFIG
# ============================================================
SERVER = "10.10.40.10,1433"
USERNAME = "sa"
PASSWORD = "koh1234"
DB_MAIN = "KY_AOI"

OUTPUT_BASE = "aoi_export"

# PostgreSQL Config
PG_HOST = "192.168.1.139"
PG_PORT = "5432"
PG_USER = "postgres"
PG_PASSWORD = "0000" # TODO: Change to your password
PG_DB = "postgres"

BARCODE_LIST = [
    "IWHT80830275",
]

META_CSV_PATH = f"{OUTPUT_BASE}/aoi_metadata.csv"

SKIP_DBS = set()

# ── Defect codes ──────────────────────────────────────────
TARGET_DEFECTS = {
    "BRIDGING": 30000011,
    "SOLDER_JOINT": 30000008,
    "MISSING": 30000004,
    "OVERHANG": 30000018,
    "POLARITY": 30000015,
    "DIMENSION": 30000003,
    "LIFTED_BODY": 30000010,
    "LIFTED_LEAD": 30000009,
}   
PASS_CODES = [11000000, 12000000]
DEFECT_MAP = {v: k for k, v in TARGET_DEFECTS.items()}

# ── Bo types hợp lệ (sau khi parse TOP/BOT) ──────────────
VALID_BO_TYPES = {
    "PROGRAM_1_FAB_G_TOP",
    "PROGRAM_1_FAB_G_BOT",
    "PROGRAM_1_FAB_D_TOP",
    "PROGRAM_1_FAB_D_BOT",
    "PROGRAM_1_5_BOT",
    "HPCC2_FABA_REV02",
}


# ============================================================
# HELPERS
# ============================================================
def safe_name(s):
    if not s or str(s) == "nan":
        return "UNKNOWN"
    return (
        str(s)
        .strip()
        .replace("/", "_")
        .replace("\\", "_")
        .replace(" ", "_")
        .replace(":", "_")
        .replace("*", "_")
        .replace("?", "_")
        .replace('"', "_")
        .replace("<", "_")
        .replace(">", "_")
        .replace("|", "_")
        .replace("(", "_")
        .replace(")", "_")
        .replace(".", "_")
    )


def parse_bo_type(job_file_id_share):
    """
    Parse bo_type + side từ JobFileIDShare.

    HPCC1:
      \PROGRAM\INTEL REPAIR\HPCC1 AND HPCC1.5\PROGRAM 1_FAB G\TOP\TOP.KYJOB
      → parts[3] = "PROGRAM 1_FAB G" → bo = "PROGRAM_1_FAB_G"
      → parts[4] = "TOP"             → side = "TOP"
      → bo_type  = "PROGRAM_1_FAB_G_TOP"

    HPCC2:
      \PROGRAM\INTEL REPAIR\HPCC2_FABA_REV02\PROGRAM\HPCC2...\TOP FULL BOARD.KYJOB
      → bo_type = "HPCC2_FABA_REV02" (không tách side)
    """
    if not job_file_id_share or str(job_file_id_share) == "nan":
        return "UNKNOWN"

    parts = str(job_file_id_share).replace("/", "\\").split("\\")
    parts = [p for p in parts if p.strip()]

    if len(parts) < 3:
        return "UNKNOWN"

    # HPCC2 → giữ nguyên, không tách side
    if "HPCC2" in parts[2].upper():
        return "HPCC2_FABA_REV02"

    # HPCC1 → lấy bo_type từ index 3, side từ index 4
    if len(parts) >= 4:
        bo = safe_name(parts[3])  # PROGRAM_1_FAB_G
        side = ""
        if len(parts) >= 5:
            p4 = parts[4].upper()
            if "BOT" in p4:
                side = "_BOT"
            elif "TOP" in p4:
                side = "_TOP"
        return bo + side

    return "UNKNOWN"


def get_conn(db):
    drivers = [
        "SQL Server",
        "ODBC Driver 18 for SQL Server",
        "ODBC Driver 17 for SQL Server",
        "ODBC Driver 13 for SQL Server",
        "ODBC Driver 11 for SQL Server",
        "FreeTDS",
    ]

    last_error = None
    server_host, server_port = SERVER.split(",", 1) if "," in SERVER else (SERVER, "1433")
    for driver in drivers:
        try:
            if driver == "FreeTDS":
                conn_str = (
                    f"DRIVER={{{driver}}};"
                    f"SERVER={server_host};PORT={server_port};DATABASE={db};"
                    f"UID={USERNAME};PWD={PASSWORD};"
                    "TDS_Version=7.0;"
                    "TextSize=2147483647;"
                )
            else:
                conn_str = (
                    f"DRIVER={{{driver}}};"
                    f"SERVER={SERVER};DATABASE={db};"
                    f"UID={USERNAME};PWD={PASSWORD};"
                    "Encrypt=no;"
                    "TrustServerCertificate=yes;"
                )
            return pyodbc.connect(conn_str, timeout=30)
        except pyodbc.Error as e:
            last_error = e

    raise last_error


# ============================================================
# BƯỚC 1: Lấy PCB list
# ============================================================
barcode_list = [str(b).strip() for b in BARCODE_LIST if b and str(b).strip()]
if not barcode_list:
    print("⚠ BARCODE_LIST đang rỗng. Hãy nhập barcode cần truy cứu.")
    exit()

barcode_sql = "','".join(b.replace("'", "''") for b in barcode_list)

print(f"\nBƯỚC 1: Lấy PCB list theo barcode ({len(barcode_list)} items)...")
conn_main = get_conn(DB_MAIN)
df_pcb = pd.read_sql(
    f"""
    SELECT
        PCBGUID, PCBID,
        JobFileIDShare,
        BarCode,
        ResultDBName,
        ImageDBName
    FROM TB_AOIPCB
    WHERE BarCode IN ('{barcode_sql}')
    ORDER BY StartDateTime DESC
""",
    conn_main,
)
conn_main.close()

df_pcb["BoType"] = df_pcb["JobFileIDShare"].apply(parse_bo_type)
df_pcb["BarCode"] = df_pcb["BarCode"].fillna("").apply(safe_name)
df_pcb = df_pcb[df_pcb["BoType"].isin(VALID_BO_TYPES)].copy()

# Giới hạn 2 PCB mỗi bo_type để test nhanh
MAX_PCB_PER_BOTYPE = 20
df_pcb = df_pcb.groupby("BoType").head(MAX_PCB_PER_BOTYPE).reset_index(drop=True)
print(f"  → Sau khi giới hạn {MAX_PCB_PER_BOTYPE} PCB/bo_type:")
print(df_pcb.groupby("BoType").size().to_string())

print(f"  → {len(df_pcb)} PCBs")
print(f"  → BoTypes: {sorted(df_pcb['BoType'].unique().tolist())}")
print(f"  → ResultDBs: {sorted(df_pcb['ResultDBName'].dropna().unique().tolist())}")

if len(df_pcb) == 0:
    print("⚠ Không có PCB!")
    exit()

# ============================================================
# BƯỚC 2: Scan PASS records (dùng ResultBefore)
# ============================================================
print("\nBƯỚC 2: Scan PASS records (dùng ResultBefore)...")
print("  → Chỉ lấy linh kiện có ResultAfter = PASS (sau repair)")

pass_codes_str = ",".join(str(v) for v in PASS_CODES)
all_pass_rows = []

for db_result in sorted(df_pcb["ResultDBName"].dropna().unique()):
    if db_result in SKIP_DBS:
        continue
    pcbs = df_pcb[df_pcb["ResultDBName"] == db_result]
    guids = "','".join(pcbs["PCBGUID"].astype(str).tolist())
    bo_map = pcbs.set_index("PCBGUID")["BoType"].to_dict()
    bar_map = pcbs.set_index("PCBGUID")["BarCode"].to_dict()
    img_map = pcbs.set_index("PCBGUID")["ImageDBName"].to_dict()

    try:
        conn = get_conn(db_result)
        # PASS: lấy từ TB_AOIDefect trực tiếp (KHÔNG join Detail)
        # TB_AOIDefect có tất cả linh kiện kể cả PASS không có lỗi
        # ResultAfter = PASS sau repair
        df_p = pd.read_sql(
            f"""
            SELECT
                c.ComponentGUID,
                c.PCBGUID,
                c.uname,
                c.PackageType,
                c.ResultAfter
            FROM TB_AOIDefect c
            WHERE c.PCBGUID IN ('{guids}')
            AND c.ResultAfter IN ({pass_codes_str})
        """,
            conn,
        )

        # Lấy DetailGUID + ROI info cho PASS riêng
        df_p_detail = pd.read_sql(
            f"""
            SELECT
                d.ComponentGUID,
                d.DetailGUID,
                d.ROILeft,
                d.ROITop,
                d.ROIRight,
                d.ROIBottom,
                d.ROIRight - d.ROILeft AS comp_width,
                d.ROIBottom - d.ROITop AS comp_height,
                ROW_NUMBER() OVER (
                    PARTITION BY d.ComponentGUID
                    ORDER BY d.DetailGUID
                ) AS rn
            FROM TB_AOIDefectDetail d
            WHERE d.ComponentGUID IN (
                SELECT ComponentGUID FROM TB_AOIDefect
                WHERE PCBGUID IN ('{guids}')
                AND ResultAfter IN ({pass_codes_str})
            )
        """,
            conn,
        )
        # Chỉ lấy 1 detail per component
        df_p_detail = df_p_detail[df_p_detail["rn"] == 1].drop(columns=["rn"])
        df_p = df_p.merge(df_p_detail, on="ComponentGUID", how="left")
        conn.close()

        df_p["BoType"] = df_p["PCBGUID"].map(bo_map)
        df_p["BarCode"] = df_p["PCBGUID"].map(bar_map)
        df_p["ImageDBName"] = df_p["PCBGUID"].map(img_map)
        df_p["Label"] = "PASS"
        df_p["DefectName"] = "PASS"

        all_pass_rows.append(df_p)
        print(f"  {db_result}: {len(df_p):6,} PASS")
    except Exception as e:
        print(f"  ⚠ {db_result}: {e}")

# ============================================================
# BƯỚC 3: Scan FAIL records
# ============================================================
print("\nBƯỚC 3: Scan FAIL records...")

fail_codes_str = ",".join(str(v) for v in TARGET_DEFECTS.values())
all_fail_rows = []

for db_result in sorted(df_pcb["ResultDBName"].dropna().unique()):
    if db_result in SKIP_DBS:
        continue
    pcbs = df_pcb[df_pcb["ResultDBName"] == db_result]
    guids = "','".join(pcbs["PCBGUID"].astype(str).tolist())
    bo_map = pcbs.set_index("PCBGUID")["BoType"].to_dict()
    bar_map = pcbs.set_index("PCBGUID")["BarCode"].to_dict()
    img_map = pcbs.set_index("PCBGUID")["ImageDBName"].to_dict()

    try:
        conn = get_conn(db_result)
        # FAIL: ResultAfter vẫn còn FAIL sau repair
        # Đây là lỗi thật không sửa được
        df_f = pd.read_sql(
            f"""
            SELECT
                c.ComponentGUID,
                c.PCBGUID,
                c.uname,
                c.PackageType,
                c.ResultAfter,
                d.DetailGUID,
                d.Defect,
                d.ROILeft,
                d.ROITop,
                d.ROIRight,
                d.ROIBottom,
                d.ROIRight - d.ROILeft AS comp_width,
                d.ROIBottom - d.ROITop AS comp_height
            FROM TB_AOIDefect c
            JOIN TB_AOIDefectDetail d
              ON d.ComponentGUID = c.ComponentGUID
            WHERE c.PCBGUID IN ('{guids}')
            AND c.ResultAfter NOT IN ({pass_codes_str})
            AND d.Defect IN ({fail_codes_str})
        """,
            conn,
        )
        conn.close()

        df_f["BoType"] = df_f["PCBGUID"].map(bo_map)
        df_f["BarCode"] = df_f["PCBGUID"].map(bar_map)
        df_f["ImageDBName"] = df_f["PCBGUID"].map(img_map)
        df_f["Label"] = "FAIL"
        df_f["DefectName"] = df_f["Defect"].map(DEFECT_MAP).fillna("OTHER")

        all_fail_rows.append(df_f)
        print(f"  {db_result}: {len(df_f):6,} FAIL")
    except Exception as e:
        print(f"  ⚠ {db_result}: {e}")

# ── Concat và thống kê ────────────────────────────────────
df_pass_all = (
    pd.concat(all_pass_rows, ignore_index=True) if all_pass_rows else pd.DataFrame()
)
df_fail_all = (
    pd.concat(all_fail_rows, ignore_index=True) if all_fail_rows else pd.DataFrame()
)

# Bỏ PASS trùng ComponentGUID
df_pass_all = df_pass_all.drop_duplicates("ComponentGUID")

print(f"\n  → PASS: {len(df_pass_all):,} (unique ComponentGUID)")
print(f"  → FAIL: {len(df_fail_all):,}")

# ============================================================
# BƯỚC 4: Auto-build DEFECT_PKG_VALID
# ============================================================
print("\nBƯỚC 4: Auto-build DEFECT_PKG_VALID...")
MIN_PKG_SAMPLES = 20
DEFECT_PKG_VALID = {}

for defect in TARGET_DEFECTS:
    df_d = df_fail_all[df_fail_all["DefectName"] == defect]
    pkg_cnt = df_d.groupby("PackageType").size()
    valid = sorted(pkg_cnt[pkg_cnt >= MIN_PKG_SAMPLES].index.tolist())
    DEFECT_PKG_VALID[defect] = valid if valid else None
    print(f"  {defect:<15}: {valid if valid else 'ALL'}")

# ============================================================
# BƯỚC 5: Filter FAIL
# ============================================================
print("\nBƯỚC 5: Filter FAIL theo pkg_type hợp lệ...")
print(f"  {'Defect':<15} {'Total':>8} {'After filter':>13} {'BoType dist'}")
print("  " + "-" * 70)

selected_fail = []
for defect in TARGET_DEFECTS:
    df_d = df_fail_all[df_fail_all["DefectName"] == defect].copy()
    total = len(df_d)

    valid_pkgs = DEFECT_PKG_VALID.get(defect)
    if valid_pkgs:
        df_filt = df_d[df_d["PackageType"].isin(valid_pkgs)]
        if len(df_filt) >= total * 0.5:
            df_d = df_filt

    selected_fail.append(df_d)
    bo_dist = df_d["BoType"].value_counts().to_dict()
    print(f"  {defect:<15} {total:>8} {len(df_d):>13}  {bo_dist}")

df_fail_selected = (
    pd.concat(selected_fail, ignore_index=True) if selected_fail else pd.DataFrame()
)

# ── Tổng kết ─────────────────────────────────────────────
df_final = pd.concat([df_pass_all, df_fail_selected], ignore_index=True)
n_pass = (df_final["Label"] == "PASS").sum()
n_fail = (df_final["Label"] == "FAIL").sum()

print(f"\n  ── TỔNG DATASET ──")
print(f"  PASS : {n_pass:,}")
print(f"  FAIL : {n_fail:,}")
print(f"  TOTAL: {len(df_final):,}")
print(f"  Ratio: {n_pass / max(n_fail, 1):.2f}")

print(f"\n  Phân bổ theo bo_type:")
print(df_final.groupby(["BoType", "Label"]).size().unstack(fill_value=0).to_string())

# ============================================================
# BƯỚC 6: Fetch samples
# ============================================================
print(f"\nBƯỚC 6: Fetch {len(df_final):,} samples...")

img_cursors = {}
metadata = []
total = len(df_final)


def get_cursor(img_db, retry=False):
    if not img_db or img_db in SKIP_DBS:
        return None
    if img_db not in img_cursors or img_cursors[img_db] is None or retry:
        try:
            c = get_conn(img_db)
            img_cursors[img_db] = c.cursor()
            tag = "Reconnected" if retry else "Connected"
            print(f"  → {tag}: {img_db}")
        except Exception as e:
            print(f"  ⚠ Fail {img_db}: {e}")
            img_cursors[img_db] = None
    return img_cursors[img_db]


for idx, row in df_final.iterrows():
    comp_guid = str(row["ComponentGUID"])
    detail_guid = str(row["DetailGUID"]) if pd.notna(row.get("DetailGUID")) else None
    img_db = str(row["ImageDBName"]) if pd.notna(row.get("ImageDBName")) else None
    label = row["Label"]
    defect_name = safe_name(row["DefectName"])
    bo_type = safe_name(row["BoType"]) if pd.notna(row["BoType"]) else "UNKNOWN"
    barcode = safe_name(row["BarCode"]) if pd.notna(row["BarCode"]) else "UNKNOWN"
    pkg_type = (
        safe_name(row["PackageType"]) if pd.notna(row["PackageType"]) else "UNKNOWN"
    )
    uname = safe_name(row["uname"]) if pd.notna(row["uname"]) else "UNKNOWN"
    comp_w = int(row["comp_width"]) if pd.notna(row.get("comp_width")) else 0
    comp_h = int(row["comp_height"]) if pd.notna(row.get("comp_height")) else 0
    roi_left = int(row["ROILeft"]) if pd.notna(row.get("ROILeft")) else 0
    roi_top = int(row["ROITop"]) if pd.notna(row.get("ROITop")) else 0
    roi_right = int(row["ROIRight"]) if pd.notna(row.get("ROIRight")) else 0
    roi_bottom = int(row["ROIBottom"]) if pd.notna(row.get("ROIBottom")) else 0
    center_x = (roi_left + roi_right) // 2
    center_y = (roi_top + roi_bottom) // 2

    metadata_row = {
        "comp_guid": comp_guid,
        "detail_guid": detail_guid if detail_guid else comp_guid,
        "label": label,
        "defect": defect_name,
        "bo_type": bo_type,
        "barcode": barcode,
        "pkg_type": pkg_type,
        "uname": uname,
        "comp_width": comp_w,
        "comp_height": comp_h,
        "roi_left": roi_left,
        "roi_top": roi_top,
        "roi_right": roi_right,
        "roi_bottom": roi_bottom,
        "center_x": center_x,
        "center_y": center_y,
        "path_2d": None,
        "image_2d": None,
    }

    if not img_db:
        metadata.append(metadata_row)
        continue

    cursor = get_cursor(img_db)
    if cursor is None:
        metadata.append(metadata_row)
        continue

    # ── 2D image ──────────────────────────────────────────
    try:
        cursor.execute(
            """
            SELECT TOP 1 Image2D FROM TB_Image2D
            WHERE ComponentGUID = ? AND Image2D IS NOT NULL
        """,
            comp_guid,
        )
        res_2d = cursor.fetchone()
        if res_2d and res_2d[0]:
            metadata_row["image_2d"] = bytes(res_2d[0])
    except Exception:
        pass

    # ── Metadata ───────────────────────────────────────────
    metadata.append(metadata_row)

    done = len(metadata)
    if done % 1000 == 0 and done > 0:
        pct = done / total * 100
        print(
            f"  [{done:7,}/{total:,}] {pct:5.1f}% | db={img_db}"
        )

# Đóng connections
for db, cur in img_cursors.items():
    if cur:
        try:
            cur.close()
        except:
            pass

# ============================================================
# BƯỚC 7: Lưu metadata vào PostgreSQL
# ============================================================
print("\nBƯỚC 7: Lưu metadata vào PostgreSQL...")
df_meta = pd.DataFrame(metadata)

if not df_meta.empty:
    try:
        os.makedirs(OUTPUT_BASE, exist_ok=True)
        df_meta_csv = df_meta.copy()
        if "image_2d" in df_meta_csv.columns:
            df_meta_csv["image_2d_len"] = df_meta_csv["image_2d"].apply(
                lambda v: len(v) if isinstance(v, (bytes, bytearray, memoryview)) else 0
            )
            df_meta_csv = df_meta_csv.drop(columns=["image_2d"])
        df_meta_csv.to_csv(META_CSV_PATH, index=False)
        print(f"  ✓ Đã lưu {len(df_meta_csv)} records vào file {META_CSV_PATH}")

        engine = create_engine(f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DB}")
        df_meta_to_save = df_meta[
            [
                "comp_guid",
                "detail_guid",
                "label",
                "defect",
                "bo_type",
                "barcode",
                "pkg_type",
                "uname",
                "image_2d",
            ]
        ].copy()
        df_meta_to_save["inspection_time"] = pd.Timestamp.utcnow()
        df_meta_to_save.to_sql("aoi_metadata_1", engine, if_exists="append", index=False)
        print(f"  ✓ Đã lưu {len(df_meta)} records vào bảng aoi_metadata_1 trong database {PG_DB}")
    except Exception as e:
        print(f"  ⚠ Lỗi khi lưu vào PostgreSQL: {e}")
else:
    print("  ⚠ Không có dữ liệu metadata để lưu.")

# ── Báo cáo cuối ─────────────────────────────────────────
print("\n" + "=" * 65)
print("BÁO CÁO DATASET")
print("=" * 65)
n_p = len(df_meta[df_meta["label"] == "PASS"])
n_f = len(df_meta[df_meta["label"] == "FAIL"])
print(f"Tổng  : {len(df_meta):,}")
print(f"PASS  : {n_p:,}")
print(f"FAIL  : {n_f:,}")
print(f"Ratio : {n_p / max(n_f, 1):.2f}")

print("\nFAIL theo defect:")
for d, cnt in df_meta[df_meta["label"] == "FAIL"]["defect"].value_counts().items():
    print(f"  {d:<20}: {cnt:6,}")

print("\nPASS theo bo_type × pkg_type:")
pass_dist = (
    df_meta[df_meta["label"] == "PASS"]
    .groupby(["bo_type", "pkg_type"])
    .size()
    .reset_index(name="count")
)
for _, r in pass_dist.iterrows():
    bar = "█" * (r["count"] // 200)
    print(f"  {r['bo_type']:<30} {r['pkg_type']:<22}: {r['count']:6,} {bar}")

print("\nPhân bổ theo bo_type:")
print(df_meta.groupby(["bo_type", "label"]).size().unstack(fill_value=0).to_string())

print("\n" + "=" * 65)
print("HOÀN THÀNH!")
print(f"  DB  : PostgreSQL (Table: aoi_metadata_1)")
print("=" * 65)
