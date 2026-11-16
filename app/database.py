import os
import re
import sqlite3
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

# ---------------------------------------------------------------------------
# Database backend selection:
#   - If DATABASE_URL is set (Supabase / PostgreSQL in production) -> psycopg
#   - Otherwise -> local SQLite file (development)
# ---------------------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DATABASE_URL") or ""
USE_POSTGRES = bool(DATABASE_URL.strip())

if not USE_POSTGRES:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH = Path(os.environ.get("CONCILIABT_DB", str(DATA_DIR / "conciliabt.db")))
else:
    DB_PATH = None  # type: ignore

MONTH_NAMES_ES = {
    1: "Enero",
    2: "Febrero",
    3: "Marzo",
    4: "Abril",
    5: "Mayo",
    6: "Junio",
    7: "Julio",
    8: "Agosto",
    9: "Septiembre",
    10: "Octubre",
    11: "Noviembre",
    12: "Diciembre",
}

DEFAULT_SETTINGS = {
    "company_name": "Empresa Venezolana de Servicios y Suministros, C.A.",
    "company_rif": "J-31245890-4",
    "prepared_by": "Analista de Conciliación Bancaria",
    "reviewed_by": "Coordinación de Tesorería",
    "approved_by": "Gerencia de Administración y Contabilidad",
    "date_window_days": "5",
    "cents_tolerance": "100",  # Bs. 1,00 threshold for "céntimos/redondeo" tag
    "acct_bank_default": "1.1.01.02.001 - Banco del Tesoro Cta. Cte.",
    "acct_commissions": "5.2.04.01.001 - Comisiones y Gastos Bancarios",
    "acct_igtf": "5.2.04.01.002 - Impuesto Grandes Transacciones Financieras (IGTF)",
    "acct_interest": "4.2.01.01.001 - Ingresos Financieros / Intereses Bancarios",
    "acct_rounding": "5.2.09.01.001 - Diferencias y Ajustes en Conciliación",
    "acct_transit_pay": "2.1.01.01.001 - Cuentas por Pagar Proveedores / Órdenes de Pago",
    "acct_transit_rec": "1.1.02.01.001 - Cuentas por Cobrar / Ingresos por Conciliar",
    "acct_suspense": "1.1.09.01.099 - Partidas por Aclarar / Diferencias de Sistema",
}


def cents_to_ve(cents: Optional[int], with_symbol: bool = False, show_plus: bool = False) -> str:
    """Format integer cents into Venezuelan notation: 201.300,00 or -201.300,00."""
    if cents is None:
        return "0,00" if not with_symbol else "Bs. 0,00"
    neg = cents < 0
    abs_cents = abs(int(cents))
    whole = abs_cents // 100
    frac = abs_cents % 100
    whole_str = f"{whole:,}".replace(",", ".")
    sign = "-" if neg else ("+" if (show_plus and cents > 0) else "")
    if with_symbol:
        return f"{sign}Bs. {whole_str},{frac:02d}"
    return f"{sign}{whole_str},{frac:02d}"


def cents_to_float(cents: Optional[int]) -> float:
    if cents is None:
        return 0.0
    return round(int(cents) / 100.0, 2)


def parse_amount_to_cents(raw, number_format: str = "auto") -> Optional[int]:
    """
    Parse monetary strings into signed integer cents cleanly without float inaccuracy.
    Supports:
      - Venezuelan/European: '201.300,00', '-1.245,50', '(48.750,80)', '201300,00'
      - Anglo/Legacy: '201,300.00', '201300.00', '-32400'
      - Suffixes: 'CR', 'DB', 'D', 'C', 'ND', 'NC', '-' trailing (e.g. '32.400,00-')
    """
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw * 100
    if isinstance(raw, float):
        d = Decimal(str(raw)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return int(d * 100)

    s = str(raw).strip()
    if not s:
        return None

    # Remove currency prefixes
    s = re.sub(r"(?i)\b(bs\.?s?|bs\.?d?|ves|vef|bol[ií]vares)\b", "", s).strip()
    s = s.replace("\xa0", "").replace(" ", "")

    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    if s.endswith("-"):
        neg = True
        s = s[:-1]
    elif s.startswith("-"):
        neg = True
        s = s[1:]
    elif s.startswith("+"):
        s = s[1:]

    # Strip trailing DR/CR markers if attached, record sign only for DB/ND
    m_suf = re.search(r"(?i)(DB|ND|DR|CR|NC)$", s)
    if m_suf:
        tag = m_suf.group(1).upper()
        if tag in ("DB", "ND", "DR"):
            neg = True
        s = s[: m_suf.start()]

    # Keep only digits, dots, commas
    s = re.sub(r"[^\d.,]", "", s)
    if not s or not re.search(r"\d", s):
        return None

    normalized = s
    if number_format == "ve":
        # Dots are thousands, comma is decimal
        if "," in s:
            parts = s.rsplit(",", 1)
            normalized = parts[0].replace(".", "").replace(",", "") + "." + parts[1]
        else:
            # Check if single dot followed by 1-2 digits vs 3 digits (thousands)
            if re.match(r"^\d{1,3}(\.\d{3})+$", s):
                normalized = s.replace(".", "")
            else:
                normalized = s.replace(",", "")
    elif number_format == "us":
        if "." in s:
            parts = s.rsplit(".", 1)
            normalized = parts[0].replace(",", "").replace(".", "") + "." + parts[1]
        else:
            if re.match(r"^\d{1,3}(,\d{3})+$", s):
                normalized = s.replace(",", "")
            else:
                normalized = s.replace(",", ".")
    else:
        # Auto detect
        has_dot = "." in s
        has_comma = "," in s
        if has_dot and has_comma:
            last_dot = s.rfind(".")
            last_comma = s.rfind(",")
            if last_comma > last_dot:
                # 201.300,00 -> VE
                parts = s.rsplit(",", 1)
                normalized = parts[0].replace(".", "").replace(",", "") + "." + parts[1]
            else:
                # 201,300.00 -> US
                parts = s.rsplit(".", 1)
                normalized = parts[0].replace(",", "").replace(".", "") + "." + parts[1]
        elif has_comma and not has_dot:
            # Multiple commas -> US thousands (1,200,300)
            if s.count(",") > 1:
                normalized = s.replace(",", "")
            else:
                left, right = s.split(",")
                if len(right) in (1, 2):
                    normalized = left + "." + right
                elif len(right) == 3 and len(left) <= 3:
                    # Ambiguous '201,300' — in VE most amounts with comma are decimals,
                    # unless exactly 3 digits after comma -> treat as thousands
                    normalized = left + right
                else:
                    normalized = left + "." + right
        elif has_dot and not has_comma:
            if s.count(".") > 1:
                # 1.201.300 -> VE thousands
                normalized = s.replace(".", "")
            else:
                left, right = s.split(".")
                if len(right) in (1, 2):
                    normalized = left + "." + right
                elif len(right) == 3 and len(left) <= 3:
                    # 201.300 -> VE thousands
                    normalized = left + right
                else:
                    normalized = left + "." + right

    try:
        dec = Decimal(normalized).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        val = int(dec * 100)
        return -val if neg else val
    except InvalidOperation:
        return None


def extract_ref_tail(reference: Optional[str], length: int = 6) -> str:
    """Extract normalized numeric tail for partial reference matching (Banco del Tesoro vs empresa)."""
    if not reference:
        return ""
    # Prefer longest digit run (handles 'ASI-2045 / 445502' or '009812445502' or '701290-D')
    runs = re.findall(r"\d+", str(reference))
    if not runs:
        return re.sub(r"[^A-Z0-9]", "", str(reference).upper())[-length:]
    # Choose the last substantial run (>=4 digits) or longest run
    substantial = [r for r in runs if len(r) >= 4]
    chosen = substantial[-1] if substantial else max(runs, key=len)
    stripped = chosen.lstrip("0") or "0"
    return stripped[-length:] if len(stripped) > length else stripped


def auto_categorize(description: str, direction: str, source: str = "") -> str:
    """Smart Venezuelan accounting concept categorizer."""
    d = (description or "").upper()
    if any(k in d for k in ("IGTF", "GRANDES TRANSACCIONES", "IMP. 2%", "IMP 2%")):
        return "IGTF"
    if any(k in d for k in ("COMISION", "COMISIÓN", "COM.", "MANTENIMIENTO", "AFILIACION", "CHEQUERA", "TARIFA BT")):
        return "Comisión Bancaria"
    if any(k in d for k in ("REVERSO", "ANULACION", "ANULACIÓN", "RECHAZAD", "DEVOLUCION", "DEVOLUCIÓN", "REINTEGRO")):
        return "Reverso / Anulación"
    if any(k in d for k in ("BOLSA DE TRABAJO", "BOLSA TRABAJO", "CESTATICKET", "ALIMENTACION", "ALIMENTACIÓN", "BONO", "BENEFICIO")):
        return "Bolsa de Trabajo / Beneficios"
    if any(k in d for k in ("NOMINA", "NÓMINA", "QUINCENA", "SUELDO", "SALARIO", "VACACIONES", "UTILIDADES", "LIQUIDACION PERSONAL")):
        return "Nómina"
    if any(k in d for k in ("INTERES", "INTERÉS", "RENDIMIENTO")):
        return "Intereses / Rendimientos"
    if any(k in d for k in ("SENIAT", "IVA", "ISLR", "RETENCION", "RETENCIÓN", "IMPUESTO", "ALCALDIA", "IVSS", "BANAVIH", "INCES")):
        return "Impuestos y Parafiscales"
    if any(k in d for k in ("PAGO MOVIL", "PAGO MÓVIL", "C2P", "P2C", "PMOVIL")):
        return "Pago Móvil"
    if any(k in d for k in ("PUNTO DE VENTA", "POS ", "LOTE POS", "TDD", "TDC", "MAESTRO")):
        return "Punto de Venta (POS)"
    if any(k in d for k in ("PAGO DIRECTO", "COMPRA", "INSUMO", "SUMINISTRO", "PAPELERIA", "PAPELERÍA", "EQUIPO", "MATERIAL")):
        return "Pago Directo / Compras"
    if any(k in d for k in ("APORTE", "SITUADO", "ASIGNACION PRESUP", "FONDO")):
        return "Aporte / Fondos"
    if direction == "IN":
        if any(k in d for k in ("COBRO", "COBRANZA", "FACTURA", "FACT.", "CLIENTE", "VENTA", "RECIBO")):
            return "Cobranza / Ingresos"
        if any(k in d for k in ("NC ", "NOTA DE CREDITO", "NOTA CRÉDITO", "ABONO")):
            return "Nota de Crédito / Abono"
        return "Ingreso / Transferencia Recibida"
    else:
        if any(k in d for k in ("PROVEEDOR", "SERVICIO", "TRANSPORTE", "FLETE", "HONORARIO", "ALQUILER", "OP-")):
            return "Pago a Proveedores / Servicios"
        if any(k in d for k in ("ND ", "NOTA DE DEBITO", "NOTA DÉBITO", "CARGO")):
            return "Nota de Débito / Cargo"
        return "Egreso / Transferencia Emitida"


# ---------------------------------------------------------------------------
# PostgreSQL (Supabase) connection wrapper.
# Translates the SQLite-style `?` placeholders used across the app into
# psycopg `%s`, emulates `cursor.lastrowid` via `RETURNING id`, and returns
# plain dict rows so the rest of the codebase works unchanged.
# ---------------------------------------------------------------------------

class _PgCursor:
    def __init__(self, cursor, lastrowid=None, exhausted=False):
        self._cur = cursor
        self._lastrowid = lastrowid
        self._exhausted = exhausted

    @property
    def lastrowid(self):
        return self._lastrowid

    def fetchone(self):
        if self._exhausted:
            return None
        return self._cur.fetchone()

    def fetchall(self):
        if self._exhausted:
            return []
        return self._cur.fetchall()


class _PgConn:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        sql_pg = sql.replace("?", "%s")
        is_insert = sql_pg.lstrip().upper().startswith("INSERT")
        # settings table has no `id` column -> skip RETURNING there
        needs_returning = is_insert and "INTO SETTINGS" not in sql_pg.upper() and "RETURNING" not in sql_pg.upper()
        if needs_returning:
            sql_pg = sql_pg.rstrip().rstrip(";") + " RETURNING id"
        cur = self._conn.cursor()
        cur.execute(sql_pg, params if params else None)
        if needs_returning:
            row = cur.fetchone()
            lastrowid = row["id"] if row else None
            return _PgCursor(cur, lastrowid=lastrowid, exhausted=True)
        return _PgCursor(cur)

    def executescript(self, script):
        cur = self._conn.cursor()
        for stmt in script.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
        return _PgCursor(cur)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def _pg_connect():
    import psycopg
    from psycopg.rows import dict_row

    url = DATABASE_URL.strip()
    # Supabase pooler requires SSL; ensure sslmode=require if not specified
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    # Use the session-mode pooler (port 5432): each client connection gets a
    # dedicated backend session, so transactions and parameterized queries work
    # reliably (transaction mode on 6543 collides on prepared statements).
    # prepare_threshold=0 disables named prepared statements as extra safety.
    conn = psycopg.connect(url, row_factory=dict_row, prepare_threshold=0, connect_timeout=15)
    return _PgConn(conn)


@contextmanager
def get_conn():
    if USE_POSTGRES:
        conn = _pg_connect()
    else:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    bank_name TEXT NOT NULL DEFAULT 'Banco del Tesoro',
    account_number TEXT NOT NULL,
    account_type TEXT NOT NULL DEFAULT 'Corriente',
    ledger_code TEXT NOT NULL DEFAULT '1.1.01.02.001',
    currency TEXT NOT NULL DEFAULT 'Bs.',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    label TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ABIERTO',
    use_mayor INTEGER NOT NULL DEFAULT 1,
    open_banco_cents INTEGER NOT NULL DEFAULT 0,
    close_banco_cents INTEGER NOT NULL DEFAULT 0,
    open_libros_cents INTEGER NOT NULL DEFAULT 0,
    close_libros_cents INTEGER NOT NULL DEFAULT 0,
    open_mayor_cents INTEGER NOT NULL DEFAULT 0,
    close_mayor_cents INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    is_demo INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at TEXT,
    UNIQUE(account_id, year, month)
);

CREATE TABLE IF NOT EXISTS match_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id INTEGER NOT NULL REFERENCES periods(id) ON DELETE CASCADE,
    match_type TEXT NOT NULL,
    canonical_source TEXT NOT NULL DEFAULT 'BANCO',
    canonical_cents INTEGER NOT NULL DEFAULT 0,
    resolution_type TEXT NOT NULL DEFAULT 'NINGUNA',
    discrepancy_kind TEXT NOT NULL DEFAULT '',
    confidence INTEGER NOT NULL DEFAULT 100,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id INTEGER NOT NULL REFERENCES periods(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('BANCO', 'LIBROS', 'MAYOR')),
    tx_date TEXT NOT NULL,
    reference TEXT NOT NULL DEFAULT '',
    ref_tail TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'General',
    direction TEXT NOT NULL CHECK (direction IN ('IN', 'OUT')),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    signed_cents INTEGER NOT NULL,
    running_balance_cents INTEGER,
    status TEXT NOT NULL DEFAULT 'PENDIENTE',
    match_group_id INTEGER REFERENCES match_groups(id) ON DELETE SET NULL,
    is_carryover INTEGER NOT NULL DEFAULT 0,
    origin_file TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_movements_period_source ON movements(period_id, source);
CREATE INDEX IF NOT EXISTS idx_movements_group ON movements(match_group_id);

CREATE TABLE IF NOT EXISTS system_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id INTEGER NOT NULL REFERENCES periods(id) ON DELETE CASCADE,
    match_group_id INTEGER REFERENCES match_groups(id) ON DELETE SET NULL,
    movement_id INTEGER REFERENCES movements(id) ON DELETE SET NULL,
    code TEXT NOT NULL DEFAULT '',
    issue_date TEXT NOT NULL,
    title TEXT NOT NULL,
    affected_module TEXT NOT NULL DEFAULT 'LIBROS',
    banco_cents INTEGER,
    libros_cents INTEGER,
    mayor_cents INTEGER,
    adj_libros_cents INTEGER NOT NULL DEFAULT 0,
    adj_mayor_cents INTEGER NOT NULL DEFAULT 0,
    affects_opening INTEGER NOT NULL DEFAULT 0,
    carry_forward INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'ACTIVA',
    audit_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id INTEGER NOT NULL REFERENCES periods(id) ON DELETE CASCADE,
    source_ref_type TEXT NOT NULL DEFAULT 'MANUAL',
    source_ref_id INTEGER,
    adj_date TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT 'LIBROS_Y_MAYOR',
    reference TEXT NOT NULL DEFAULT '',
    concept TEXT NOT NULL,
    debit_account TEXT NOT NULL,
    credit_account TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    applied INTEGER NOT NULL DEFAULT 0,
    is_manual INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

POSTGRES_DDL = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    bank_name TEXT NOT NULL DEFAULT 'Banco del Tesoro',
    account_number TEXT NOT NULL,
    account_type TEXT NOT NULL DEFAULT 'Corriente',
    ledger_code TEXT NOT NULL DEFAULT '1.1.01.02.001',
    currency TEXT NOT NULL DEFAULT 'Bs.',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (NOW()::text)
);

CREATE TABLE IF NOT EXISTS periods (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    label TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ABIERTO',
    use_mayor INTEGER NOT NULL DEFAULT 1,
    open_banco_cents INTEGER NOT NULL DEFAULT 0,
    close_banco_cents INTEGER NOT NULL DEFAULT 0,
    open_libros_cents INTEGER NOT NULL DEFAULT 0,
    close_libros_cents INTEGER NOT NULL DEFAULT 0,
    open_mayor_cents INTEGER NOT NULL DEFAULT 0,
    close_mayor_cents INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    is_demo INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (NOW()::text),
    closed_at TEXT,
    UNIQUE(account_id, year, month)
);

CREATE TABLE IF NOT EXISTS match_groups (
    id SERIAL PRIMARY KEY,
    period_id INTEGER NOT NULL REFERENCES periods(id) ON DELETE CASCADE,
    match_type TEXT NOT NULL,
    canonical_source TEXT NOT NULL DEFAULT 'BANCO',
    canonical_cents INTEGER NOT NULL DEFAULT 0,
    resolution_type TEXT NOT NULL DEFAULT 'NINGUNA',
    discrepancy_kind TEXT NOT NULL DEFAULT '',
    confidence INTEGER NOT NULL DEFAULT 100,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (NOW()::text)
);

CREATE TABLE IF NOT EXISTS movements (
    id SERIAL PRIMARY KEY,
    period_id INTEGER NOT NULL REFERENCES periods(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('BANCO', 'LIBROS', 'MAYOR')),
    tx_date TEXT NOT NULL,
    reference TEXT NOT NULL DEFAULT '',
    ref_tail TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'General',
    direction TEXT NOT NULL CHECK (direction IN ('IN', 'OUT')),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    signed_cents INTEGER NOT NULL,
    running_balance_cents INTEGER,
    status TEXT NOT NULL DEFAULT 'PENDIENTE',
    match_group_id INTEGER REFERENCES match_groups(id) ON DELETE SET NULL,
    is_carryover INTEGER NOT NULL DEFAULT 0,
    origin_file TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (NOW()::text)
);

CREATE INDEX IF NOT EXISTS idx_movements_period_source ON movements(period_id, source);
CREATE INDEX IF NOT EXISTS idx_movements_group ON movements(match_group_id);

CREATE TABLE IF NOT EXISTS system_issues (
    id SERIAL PRIMARY KEY,
    period_id INTEGER NOT NULL REFERENCES periods(id) ON DELETE CASCADE,
    match_group_id INTEGER REFERENCES match_groups(id) ON DELETE SET NULL,
    movement_id INTEGER REFERENCES movements(id) ON DELETE SET NULL,
    code TEXT NOT NULL DEFAULT '',
    issue_date TEXT NOT NULL,
    title TEXT NOT NULL,
    affected_module TEXT NOT NULL DEFAULT 'LIBROS',
    banco_cents INTEGER,
    libros_cents INTEGER,
    mayor_cents INTEGER,
    adj_libros_cents INTEGER NOT NULL DEFAULT 0,
    adj_mayor_cents INTEGER NOT NULL DEFAULT 0,
    affects_opening INTEGER NOT NULL DEFAULT 0,
    carry_forward INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'ACTIVA',
    audit_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (NOW()::text)
);

CREATE TABLE IF NOT EXISTS adjustments (
    id SERIAL PRIMARY KEY,
    period_id INTEGER NOT NULL REFERENCES periods(id) ON DELETE CASCADE,
    source_ref_type TEXT NOT NULL DEFAULT 'MANUAL',
    source_ref_id INTEGER,
    adj_date TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT 'LIBROS_Y_MAYOR',
    reference TEXT NOT NULL DEFAULT '',
    concept TEXT NOT NULL,
    debit_account TEXT NOT NULL,
    credit_account TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    applied INTEGER NOT NULL DEFAULT 0,
    is_manual INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (NOW()::text)
);
"""


def init_db():
    with get_conn() as conn:
        if USE_POSTGRES:
            conn.executescript(POSTGRES_DDL)
            for k, v in DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING",
                    (k, v),
                )
        else:
            conn.executescript(SQLITE_DDL)
            for k, v in DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (k, v),
                )


def get_settings(conn) -> dict:
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    out = dict(DEFAULT_SETTINGS)
    for r in rows:
        out[r["key"]] = r["value"]
    return out


def period_label(year: int, month: int) -> str:
    return f"{MONTH_NAMES_ES.get(month, str(month))} {year}"
