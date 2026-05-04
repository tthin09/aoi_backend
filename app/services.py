import base64
from datetime import datetime
from typing import List, Optional

import json
import os

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv
import pyodbc
from fastapi import HTTPException
from pydantic import BaseModel, Field

load_dotenv()

# Application config and paths
DB_CONNECT_TIMEOUT_SECONDS = 5
SQLSERVER_CONNECT_TIMEOUT_SECONDS = 30

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
IMAGE_DIR = os.path.join(DATA_DIR, "image")
IMAGE_ALIAS_PATH = os.path.join(IMAGE_DIR, "alias.json")
PART_POSITION_DIR = os.path.join(DATA_DIR, "part_position")
PART_POSITION_JSON_DIR = os.path.join(PART_POSITION_DIR, "json")
ALIAS_PATH = os.path.join(PART_POSITION_DIR, "alias.json")

SQLSERVER_HOST = os.getenv("SQLSERVER_HOST")
SQLSERVER_PORT = os.getenv("SQLSERVER_PORT")
SQLSERVER_USER = os.getenv("SQLSERVER_USER")
SQLSERVER_PASSWORD = os.getenv("SQLSERVER_PASSWORD")
SQLSERVER_DB_MAIN = os.getenv("SQLSERVER_DB_MAIN")

SQLSERVER_DRIVERS = [
    "SQL Server",
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 13 for SQL Server",
    "ODBC Driver 11 for SQL Server",
    "FreeTDS",
]

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

VALID_BO_TYPES = {
    "PROGRAM_1_FAB_G_TOP",
    "PROGRAM_1_FAB_G_BOT",
    "PROGRAM_1_FAB_D_TOP",
    "PROGRAM_1_FAB_D_BOT",
    "PROGRAM_1_5_BOT",
    "HPCC2_FABA_REV02",
}


class AOIRecord(BaseModel):
    pkg_type: Optional[str]
    uname: Optional[str]
    label: Optional[str]
    defect: Optional[str]
    bo_type: Optional[str]
    inspection_time: Optional[datetime]
    position: Optional[dict] = None
    image_2d: Optional[str] = None


class FailedResponse(BaseModel):
    items: List[AOIRecord] = Field(default_factory=list)
    message: Optional[str] = None


class FailedBoard(BaseModel):
    barcode: str
    bo_type: Optional[str]
    inspection_time: Optional[datetime]


def _build_conn_info() -> str:
    missing = [
        name
        for name in ("HOST_NAME", "DB_NAME", "USER", "PASSWORD", "PORT")
        if not os.getenv(name)
    ]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")
    return (
        f"host={os.getenv('HOST_NAME')} "
        f"dbname={os.getenv('DB_NAME')} "
        f"user={os.getenv('USER')} "
        f"password={os.getenv('PASSWORD')} "
        f"port={os.getenv('PORT')}"
    )


def _safe_name(value: str) -> str:
    if not value or str(value) == "nan":
        return "UNKNOWN"
    return (
        str(value)
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


def _parse_bo_type(job_file_id_share: str) -> str:
    if not job_file_id_share or str(job_file_id_share) == "nan":
        return "UNKNOWN"

    parts = str(job_file_id_share).replace("/", "\\").split("\\")
    parts = [part for part in parts if part.strip()]

    if len(parts) < 3:
        return "UNKNOWN"

    if "HPCC2" in parts[2].upper():
        return "HPCC2_FABA_REV02"

    if len(parts) >= 4:
        bo = _safe_name(parts[3])
        side = ""
        if len(parts) >= 5:
            p4 = parts[4].upper()
            if "BOT" in p4:
                side = "_BOT"
            elif "TOP" in p4:
                side = "_TOP"
        return bo + side

    return "UNKNOWN"


def _get_recent_failed_barcodes(limit: int = 10) -> list[FailedBoard]:
    conn_main = _get_sqlserver_conn(SQLSERVER_DB_MAIN)
    cursor = conn_main.cursor()
    cursor.execute(
        """
        SELECT TOP 200
            PCBGUID,
            JobFileIDShare,
            BarCode,
            ResultDBName,
            StartDateTime
        FROM TB_AOIPCB
        ORDER BY StartDateTime DESC
        """
    )
    pcb_rows = cursor.fetchall()
    conn_main.close()

    if not pcb_rows:
        return []

    recent_pcbs = []
    for row in pcb_rows:
        bo_type = _parse_bo_type(row.JobFileIDShare)
        if bo_type not in VALID_BO_TYPES:
            continue
        recent_pcbs.append(
            {
                "pcbguid": str(row.PCBGUID),
                "barcode": row.BarCode,
                "bo_type": bo_type,
                "result_db": row.ResultDBName,
                "inspection_time": row.StartDateTime,
            }
        )

    if not recent_pcbs:
        return []

    pcbs_by_db: dict[str, list[str]] = {}
    for pcb in recent_pcbs:
        pcbs_by_db.setdefault(pcb["result_db"], []).append(pcb["pcbguid"])

    pass_codes_str = ",".join(str(code) for code in PASS_CODES)
    fail_codes_str = ",".join(str(code) for code in TARGET_DEFECTS.values())
    fail_pcbs: set[str] = set()

    for db_result, pcbguids in pcbs_by_db.items():
        if not pcbguids:
            continue
        guid_list = "','".join(guid.replace("'", "''") for guid in pcbguids)
        conn = _get_sqlserver_conn(db_result)
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT DISTINCT c.PCBGUID
            FROM TB_AOIDefect c
            JOIN TB_AOIDefectDetail d
              ON d.ComponentGUID = c.ComponentGUID
            WHERE c.PCBGUID IN ('{guid_list}')
            AND c.ResultAfter NOT IN ({pass_codes_str})
            AND d.Defect IN ({fail_codes_str})
            """
        )
        for row in cursor.fetchall():
            fail_pcbs.add(str(row.PCBGUID))
        conn.close()

    results: list[FailedBoard] = []
    seen_barcodes: set[str] = set()
    for pcb in recent_pcbs:
        if pcb["pcbguid"] not in fail_pcbs:
            continue
        barcode = pcb["barcode"]
        if not barcode or barcode in seen_barcodes:
            continue
        seen_barcodes.add(barcode)
        results.append(
            FailedBoard(
                barcode=barcode,
                bo_type=pcb["bo_type"],
                inspection_time=pcb["inspection_time"],
            )
        )
        if len(results) >= limit:
            break

    return results


def _get_sqlserver_conn(db_name: str) -> pyodbc.Connection:
    last_error = None
    server = f"{SQLSERVER_HOST},{SQLSERVER_PORT}"
    server_host, server_port = (
        server.split(",", 1) if "," in server else (server, "1433")
    )
    for driver in SQLSERVER_DRIVERS:
        try:
            if driver == "FreeTDS":
                conn_str = (
                    f"DRIVER={{{driver}}};"
                    f"SERVER={server_host};PORT={server_port};DATABASE={db_name};"
                    f"UID={SQLSERVER_USER};PWD={SQLSERVER_PASSWORD};"
                    "TDS_Version=7.0;"
                    "TextSize=2147483647;"
                )
            else:
                conn_str = (
                    f"DRIVER={{{driver}}};"
                    f"SERVER={server};DATABASE={db_name};"
                    f"UID={SQLSERVER_USER};PWD={SQLSERVER_PASSWORD};"
                    "Encrypt=no;"
                    "TrustServerCertificate=yes;"
                )
            return pyodbc.connect(conn_str, timeout=SQLSERVER_CONNECT_TIMEOUT_SECONDS)
        except pyodbc.Error as exc:
            last_error = exc

    raise last_error


def _fetch_failed_from_sqlserver(barcode: str) -> tuple[list[dict], bool]:
    conn_main = _get_sqlserver_conn(SQLSERVER_DB_MAIN)
    cursor = conn_main.cursor()
    cursor.execute(
        """
        SELECT
            PCBGUID,
            JobFileIDShare,
            BarCode,
            ResultDBName,
            ImageDBName
        FROM TB_AOIPCB
        WHERE BarCode = ?
        ORDER BY StartDateTime DESC
        """,
        (barcode,),
    )
    pcb_rows = cursor.fetchall()
    conn_main.close()

    if not pcb_rows:
        return [], False

    pcbs = []
    for row in pcb_rows:
        bo_type = _parse_bo_type(row.JobFileIDShare)
        if bo_type not in VALID_BO_TYPES:
            continue
        pcbs.append(
            {
                "pcbguid": str(row.PCBGUID),
                "bo_type": bo_type,
                "barcode": row.BarCode,
                "result_db": row.ResultDBName,
                "image_db": row.ImageDBName,
            }
        )

    if not pcbs:
        return [], True

    bo_map = {pcb["pcbguid"]: pcb["bo_type"] for pcb in pcbs}
    bar_map = {pcb["pcbguid"]: pcb["barcode"] for pcb in pcbs}
    img_map = {pcb["pcbguid"]: pcb.get("image_db") for pcb in pcbs}

    pcbs_by_db: dict[str, list[str]] = {}
    for pcb in pcbs:
        pcbs_by_db.setdefault(pcb["result_db"], []).append(pcb["pcbguid"])

    pass_codes_str = ",".join(str(code) for code in PASS_CODES)
    fail_codes_str = ",".join(str(code) for code in TARGET_DEFECTS.values())
    now = datetime.utcnow()
    results: list[dict] = []
    image_conns: dict[str, pyodbc.Connection] = {}
    image_cursors: dict[str, pyodbc.Cursor] = {}

    def get_image_cursor(image_db: str) -> Optional[pyodbc.Cursor]:
        if not image_db:
            return None
        if image_db in image_cursors:
            return image_cursors[image_db]
        try:
            conn = _get_sqlserver_conn(image_db)
            image_conns[image_db] = conn
            image_cursors[image_db] = conn.cursor()
            return image_cursors[image_db]
        except Exception:
            return None

    for db_result, pcbguids in pcbs_by_db.items():
        if not pcbguids:
            continue
        guid_list = "','".join(guid.replace("'", "''") for guid in pcbguids)
        conn = _get_sqlserver_conn(db_result)
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT
                c.ComponentGUID,
                c.PCBGUID,
                c.uname,
                c.PackageType,
                d.Defect
            FROM TB_AOIDefect c
            JOIN TB_AOIDefectDetail d
              ON d.ComponentGUID = c.ComponentGUID
            WHERE c.PCBGUID IN ('{guid_list}')
            AND c.ResultAfter NOT IN ({pass_codes_str})
            AND d.Defect IN ({fail_codes_str})
            """
        )
        for row in cursor.fetchall():
            pcbguid = str(row.PCBGUID)
            image_2d = None
            image_db = img_map.get(pcbguid)
            image_cursor = get_image_cursor(image_db)
            if image_cursor is not None:
                try:
                    image_cursor.execute(
                        """
                        SELECT TOP 1 Image2D FROM TB_Image2D
                        WHERE ComponentGUID = ? AND Image2D IS NOT NULL
                        """,
                        (row.ComponentGUID,),
                    )
                    image_row = image_cursor.fetchone()
                    if image_row and image_row[0]:
                        image_2d = bytes(image_row[0])
                except Exception:
                    image_2d = None
            results.append(
                {
                    "pkg_type": row.PackageType,
                    "uname": row.uname,
                    "label": "FAIL",
                    "defect": DEFECT_MAP.get(row.Defect, "OTHER"),
                    "bo_type": bo_map.get(pcbguid),
                    "inspection_time": now,
                    "barcode": bar_map.get(pcbguid, barcode),
                    "image_2d": image_2d,
                }
            )
        conn.close()

    for cursor in image_cursors.values():
        try:
            cursor.close()
        except Exception:
            pass
    for conn in image_conns.values():
        try:
            conn.close()
        except Exception:
            pass

    return results, True


def _save_failed_rows_to_postgres(rows: list[dict]) -> None:
    if not rows:
        return
    try:
        conn_info = _build_conn_info()
        insert_query = (
            "INSERT INTO aoi_metadata_1 "
            "(pkg_type, uname, label, defect, bo_type, inspection_time, barcode, image_2d) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
        )
        values = [
            (
                row.get("pkg_type"),
                row.get("uname"),
                row.get("label"),
                row.get("defect"),
                row.get("bo_type"),
                row.get("inspection_time"),
                row.get("barcode"),
                _coerce_image_bytes(row.get("image_2d")),
            )
            for row in rows
        ]
        with psycopg.connect(
            conn_info, connect_timeout=DB_CONNECT_TIMEOUT_SECONDS
        ) as conn:
            with conn.cursor() as cur:
                cur.executemany(insert_query, values)
    except Exception as exc:
        print(f"Failed to save fallback rows: {exc}")


def _coerce_image_bytes(value: Optional[object]) -> Optional[bytes]:
    if value is None:
        return None
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        try:
            return base64.b64decode(value)
        except Exception:
            return None
    return None


def _resolve_part_position_csv(board_type: str) -> Optional[str]:
    if os.path.isfile(ALIAS_PATH):
        with open(ALIAS_PATH, "r", encoding="utf-8") as handle:
            alias_map = json.load(handle)
        candidates = alias_map.get(board_type, [board_type])
    else:
        candidates = [board_type]

    for candidate in candidates:
        name = candidate if candidate.lower().endswith(".csv") else f"{candidate}.csv"
        csv_path = os.path.join(PART_POSITION_DIR, name)
        if os.path.isfile(csv_path):
            return csv_path

    return None


_part_position_cache: dict[str, tuple[float, dict]] = {}


def _resolve_part_position_json_path(board_type: str) -> Optional[str]:
    if os.path.isfile(ALIAS_PATH):
        with open(ALIAS_PATH, "r", encoding="utf-8") as handle:
            alias_map = json.load(handle)
        candidates = alias_map.get(board_type, [board_type])
    else:
        candidates = [board_type]

    for candidate in candidates:
        name = candidate if candidate.lower().endswith(".json") else f"{candidate}.json"
        json_path = os.path.join(PART_POSITION_JSON_DIR, name)
        if os.path.isfile(json_path):
            return json_path

    return None


def _load_part_position_json(json_path: str) -> dict:
    mtime = os.path.getmtime(json_path)
    cached = _part_position_cache.get(json_path)
    if cached and cached[0] == mtime:
        return cached[1]

    with open(json_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    _part_position_cache[json_path] = (mtime, data)
    return data


def _attach_positions(rows: list[dict]) -> list[dict]:
    for row in rows:
        board_type = row.get("bo_type")
        uname = row.get("uname")
        row.setdefault("image_2d", None)
        row["position"] = None
        if "image_2d" in row:
            image_value = row["image_2d"]
            if isinstance(image_value, memoryview):
                image_value = image_value.tobytes()
            if isinstance(image_value, (bytes, bytearray)):
                row["image_2d"] = base64.b64encode(image_value).decode("ascii")
        if not board_type or not uname:
            continue

        json_path = _resolve_part_position_json_path(board_type)
        if not json_path:
            continue

        positions_map = _load_part_position_json(json_path)
        positions = positions_map.get(uname)
        if not positions:
            continue

        pos = positions[0] if isinstance(positions, list) else positions
        if not isinstance(pos, dict):
            continue
        row["position"] = {
            "x": pos.get("x"),
            "y": pos.get("y"),
            "rotation": pos.get("rotation"),
        }

    return rows


def _dedupe_rows(rows: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for row in rows:
        key = (
            row.get("pkg_type"),
            row.get("uname"),
            row.get("bo_type"),
            row.get("inspection_time"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _resolve_image_path(board_type: str) -> Optional[str]:
    if os.path.isfile(IMAGE_ALIAS_PATH):
        with open(IMAGE_ALIAS_PATH, "r", encoding="utf-8") as handle:
            alias_map = json.load(handle)
        candidates = alias_map.get(board_type, [board_type])
    else:
        candidates = [board_type]

    newest_path = None
    newest_mtime = None
    for candidate in candidates:
        name = candidate if candidate.lower().endswith(".jpg") else f"{candidate}.jpg"
        image_path = os.path.join(IMAGE_DIR, name)
        if not os.path.isfile(image_path):
            continue
        mtime = os.path.getmtime(image_path)
        if newest_mtime is None or mtime > newest_mtime:
            newest_mtime = mtime
            newest_path = image_path

    return newest_path


def _get_bo_type_by_barcode(barcode: str) -> str:
    conn_info = _build_conn_info()
    query = "SELECT bo_type FROM aoi_metadata_1 WHERE barcode = %s LIMIT 1"
    with psycopg.connect(conn_info, connect_timeout=DB_CONNECT_TIMEOUT_SECONDS) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, (barcode,))
            row = cur.fetchone()
    if row is None or not row.get("bo_type"):
        raise HTTPException(status_code=404, detail=f"Barcode not found: {barcode}")
    return row["bo_type"]
