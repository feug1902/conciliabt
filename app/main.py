import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .database import (
    BASE_DIR,
    auto_categorize,
    cents_to_ve,
    extract_ref_tail,
    get_conn,
    get_settings,
    init_db,
    parse_amount_to_cents,
    period_label,
)
from .exporters import export_reconciliation_excel, export_reconciliation_pdf
from .pdf_parser import parse_date_str, parse_free_text, parse_uploaded_file
from .reconciler import (
    classify_discrepancy,
    compute_reconciliation_sheet,
    find_suspected_duplicates,
    run_auto_reconciliation,
    sync_suggested_adjustments,
)
from .seed_and_samples import SAMPLES_DIR, ensure_demo_seeded

STATIC_DIR = BASE_DIR / "public"
STATIC_ASSETS_DIR = STATIC_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_demo_seeded(force=False)
    yield


app = FastAPI(
    title="ConciliaBT — Conciliación Banco del Tesoro, Tesorería y Contabilidad",
    version="1.0.0",
    lifespan=lifespan,
)

# Permissive CORS & host support for live preview environment
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Optional PIN gate: if the APP_PIN environment variable is set, every /api/*
# request must carry the matching X-App-Pin header (except /api/health).
APP_PIN = os.environ.get("APP_PIN", "").strip()


@app.middleware("http")
async def pin_gate(request: Request, call_next):
    if APP_PIN and request.url.path.startswith("/api/") and request.url.path != "/api/health":
        if request.headers.get("x-app-pin", "") != APP_PIN:
            return JSONResponse(status_code=401, content={"detail": "PIN requerido o incorrecto."})
    return await call_next(request)


@app.get("/api/health")
def health_check():
    from .database import USE_POSTGRES

    return {"ok": True, "backend": "postgres" if USE_POSTGRES else "sqlite", "app": "ConciliaBT"}


# -------------------- Pydantic Schemas --------------------

class AccountPayload(BaseModel):
    name: str
    bank_name: str = "Banco del Tesoro"
    account_number: str
    account_type: str = "Corriente"
    ledger_code: str = "1.1.01.02.001"
    currency: str = "Bs."


class PeriodCreatePayload(BaseModel):
    account_id: int
    year: int
    month: int
    use_mayor: bool = True
    open_banco: Optional[str] = "0,00"
    close_banco: Optional[str] = "0,00"
    open_libros: Optional[str] = "0,00"
    close_libros: Optional[str] = "0,00"
    open_mayor: Optional[str] = "0,00"
    close_mayor: Optional[str] = "0,00"
    carry_from_previous: bool = True
    notes: str = ""


class PeriodUpdatePayload(BaseModel):
    status: Optional[str] = None
    use_mayor: Optional[bool] = None
    open_banco_cents: Optional[int] = None
    close_banco_cents: Optional[int] = None
    open_libros_cents: Optional[int] = None
    close_libros_cents: Optional[int] = None
    open_mayor_cents: Optional[int] = None
    close_mayor_cents: Optional[int] = None
    notes: Optional[str] = None


class TextPreviewPayload(BaseModel):
    text: str
    source: str
    number_format: str = "auto"
    default_year: Optional[int] = None


class ImportRowItem(BaseModel):
    tx_date: str
    reference: str = ""
    description: str
    category: Optional[str] = None
    direction: str
    amount_cents: int
    running_balance_cents: Optional[int] = None


class ImportCommitPayload(BaseModel):
    source: str
    origin_file: str = "Importación PDF"
    replace_source: bool = False
    update_balances: bool = True
    opening_balance_cents: Optional[int] = None
    closing_balance_cents: Optional[int] = None
    auto_reconcile: bool = True
    rows: List[ImportRowItem]


class MovementCreatePayload(BaseModel):
    source: str
    tx_date: str
    reference: str = ""
    description: str
    category: Optional[str] = None
    direction: str
    amount_cents: int
    note: str = ""


class MovementUpdatePayload(BaseModel):
    tx_date: Optional[str] = None
    reference: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    direction: Optional[str] = None
    amount_cents: Optional[int] = None
    status: Optional[str] = None
    note: Optional[str] = None


class AutoReconcilePayload(BaseModel):
    reset_existing: bool = False


class ManualMatchPayload(BaseModel):
    movement_ids: List[int]
    match_as_reversal: bool = False
    canonical_source: Optional[str] = None
    resolution_type: str = "AJUSTE"
    note: str = ""


class MatchGroupUpdatePayload(BaseModel):
    canonical_source: Optional[str] = None
    resolution_type: Optional[str] = None
    note: Optional[str] = None


class SystemIssuePayload(BaseModel):
    code: str = ""
    issue_date: str
    title: str
    affected_module: str = "LIBROS"
    banco_cents: Optional[int] = None
    libros_cents: Optional[int] = None
    mayor_cents: Optional[int] = None
    adj_libros_cents: int = 0
    adj_mayor_cents: int = 0
    affects_opening: bool = False
    carry_forward: bool = True
    status: str = "ACTIVA"
    audit_note: str = ""


class AdjustmentCreatePayload(BaseModel):
    adj_date: str
    target: str = "LIBROS_Y_MAYOR"
    reference: str = ""
    concept: str
    debit_account: str
    credit_account: str
    amount_cents: int
    note: str = ""


class AdjustmentUpdatePayload(BaseModel):
    applied: Optional[bool] = None
    target: Optional[str] = None
    concept: Optional[str] = None
    debit_account: Optional[str] = None
    credit_account: Optional[str] = None
    amount_cents: Optional[int] = None
    note: Optional[str] = None


# -------------------- Accounts Endpoints --------------------

@app.get("/api/accounts")
def list_accounts():
    with get_conn() as conn:
        accs = [
            dict(r)
            for r in conn.execute(
                """
                SELECT a.*,
                       (SELECT COUNT(*) FROM periods p WHERE p.account_id = a.id) AS period_count
                FROM accounts a
                WHERE a.active = 1
                ORDER BY a.id ASC
                """
            ).fetchall()
        ]
        periods = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM periods ORDER BY year DESC, month DESC, id DESC"
            ).fetchall()
        ]
    return {"accounts": accs, "periods": periods}


@app.post("/api/accounts")
def create_account(payload: AccountPayload):
    if not payload.name.strip() or not payload.account_number.strip():
        raise HTTPException(400, "Nombre y número de cuenta son obligatorios.")
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO accounts (name, bank_name, account_number, account_type, ledger_code, currency)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                payload.name.strip(),
                payload.bank_name.strip() or "Banco del Tesoro",
                payload.account_number.strip(),
                payload.account_type.strip() or "Corriente",
                payload.ledger_code.strip() or "1.1.01.02.001",
                payload.currency.strip() or "Bs.",
            ),
        )
        acc = dict(conn.execute("SELECT * FROM accounts WHERE id = ?", (cur.lastrowid,)).fetchone())
    return acc


@app.put("/api/accounts/{account_id}")
def update_account(account_id: int, payload: AccountPayload):
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE accounts
            SET name = ?, bank_name = ?, account_number = ?, account_type = ?, ledger_code = ?, currency = ?
            WHERE id = ?
            """,
            (
                payload.name.strip(),
                payload.bank_name.strip(),
                payload.account_number.strip(),
                payload.account_type.strip(),
                payload.ledger_code.strip(),
                payload.currency.strip(),
                account_id,
            ),
        )
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Cuenta no encontrada.")
        return dict(row)


@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    return {"ok": True}


# -------------------- Periods Endpoints --------------------

@app.post("/api/periods")
def create_period(payload: PeriodCreatePayload):
    if not (1 <= payload.month <= 12) or not (2000 <= payload.year <= 2100):
        raise HTTPException(400, "Mes o año inválido.")
    ob = parse_amount_to_cents(payload.open_banco, "ve") or 0
    cb = parse_amount_to_cents(payload.close_banco, "ve") or 0
    ol = parse_amount_to_cents(payload.open_libros, "ve") or 0
    cl = parse_amount_to_cents(payload.close_libros, "ve") or 0
    om = parse_amount_to_cents(payload.open_mayor, "ve") or 0
    cm = parse_amount_to_cents(payload.close_mayor, "ve") or 0

    with get_conn() as conn:
        exists = conn.execute(
            "SELECT id FROM periods WHERE account_id = ? AND year = ? AND month = ?",
            (payload.account_id, payload.year, payload.month),
        ).fetchone()
        if exists:
            raise HTTPException(400, "Ya existe un período para ese mes y año en esta cuenta.")

        # Look up immediately preceding period if carry_from_previous
        prev = conn.execute(
            """
            SELECT * FROM periods
            WHERE account_id = ? AND (year < ? OR (year = ? AND month < ?))
            ORDER BY year DESC, month DESC LIMIT 1
            """,
            (payload.account_id, payload.year, payload.year, payload.month),
        ).fetchone()

        if payload.carry_from_previous and prev and ob == 0 and ol == 0 and om == 0:
            ob = prev["close_banco_cents"]
            cb = ob
            ol = prev["close_libros_cents"]
            cl = ol
            om = prev["close_mayor_cents"]
            cm = om

        cur = conn.execute(
            """
            INSERT INTO periods
                (account_id, year, month, label, status, use_mayor,
                 open_banco_cents, close_banco_cents, open_libros_cents, close_libros_cents,
                 open_mayor_cents, close_mayor_cents, notes)
            VALUES (?, ?, ?, ?, 'ABIERTO', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.account_id,
                payload.year,
                payload.month,
                period_label(payload.year, payload.month),
                1 if payload.use_mayor else 0,
                ob, cb, ol, cl, om, cm,
                payload.notes.strip(),
            ),
        )
        new_pid = cur.lastrowid
        carried = {"transit_items": 0, "system_issues": 0}
        if payload.carry_from_previous and prev:
            carried = _carry_forward_items(conn, prev["id"], new_pid)

        row = dict(conn.execute("SELECT * FROM periods WHERE id = ?", (new_pid,)).fetchone())
    return {"period": row, "carried": carried}


def _carry_forward_items(conn, prev_pid: int, new_pid: int) -> dict:
    """Carry active legacy system issues and in-transit items into the next monthly period."""
    sys_cnt = 0
    tr_cnt = 0

    # Carry forward active system issues (now affecting opening balance difference in next month!)
    issues = conn.execute(
        "SELECT * FROM system_issues WHERE period_id = ? AND carry_forward = 1 AND status != 'RESUELTA'",
        (prev_pid,),
    ).fetchall()
    for iss in issues:
        conn.execute(
            """
            INSERT INTO system_issues
                (period_id, code, issue_date, title, affected_module,
                 banco_cents, libros_cents, mayor_cents, adj_libros_cents, adj_mayor_cents,
                 affects_opening, carry_forward, status, audit_note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?)
            """,
            (
                new_pid,
                iss["code"],
                iss["issue_date"],
                iss["title"],
                iss["affected_module"],
                iss["banco_cents"],
                iss["libros_cents"],
                iss["mayor_cents"],
                iss["adj_libros_cents"],
                iss["adj_mayor_cents"],
                iss["status"],
                iss["audit_note"],
            ),
        )
        sys_cnt += 1

    # Carry forward in-transit items (so when next month's bank statement clears them, they match!)
    # Note: they affected prev month's Libros/Mayor balance already, so mark is_carryover=1
    transit_groups = conn.execute(
        """
        SELECT g.id FROM match_groups g
        WHERE g.period_id = ? AND g.match_type != 'REVERSO'
          AND NOT EXISTS (SELECT 1 FROM movements m WHERE m.match_group_id = g.id AND m.source = 'BANCO')
        """,
        (prev_pid,),
    ).fetchall()
    for tg in transit_groups:
        mems = conn.execute("SELECT * FROM movements WHERE match_group_id = ?", (tg["id"],)).fetchall()
        new_ids = []
        for m in mems:
            c = conn.execute(
                """
                INSERT INTO movements
                    (period_id, source, tx_date, reference, ref_tail, description, category,
                     direction, amount_cents, signed_cents, status, is_carryover, origin_file, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CONCILIADO', 1, 'Arrastre mes anterior', ?)
                """,
                (
                    new_pid, m["source"], m["tx_date"], m["reference"], m["ref_tail"],
                    f"[Tránsito mes ant.] {m['description']}", m["category"],
                    m["direction"], m["amount_cents"], m["signed_cents"],
                    "Partida en tránsito arrastrada del período anterior para cruzar cuando haga efectivo en Banco del Tesoro.",
                ),
            )
            new_ids.append((c.lastrowid, m["source"], m["signed_cents"]))
        if new_ids:
            canon_src = new_ids[0][1]
            canon_c = sum(sc for _, s, sc in new_ids if s == canon_src)
            gc = conn.execute(
                """
                INSERT INTO match_groups (period_id, match_type, canonical_source, canonical_cents, confidence, note)
                VALUES (?, 'EXACTO_DOBLE', ?, ?, 100, 'Partida en tránsito arrastrada del mes anterior (esperando cobro/cargo en Banco del Tesoro)')
                """,
                (new_pid, canon_src, canon_c),
            )
            for mid, _, _ in new_ids:
                conn.execute("UPDATE movements SET match_group_id = ? WHERE id = ?", (gc.lastrowid, mid))
            tr_cnt += 1

    standalone_tr = conn.execute(
        "SELECT * FROM movements WHERE period_id = ? AND match_group_id IS NULL AND status = 'EN_TRANSITO'",
        (prev_pid,),
    ).fetchall()
    for m in standalone_tr:
        conn.execute(
            """
            INSERT INTO movements
                (period_id, source, tx_date, reference, ref_tail, description, category,
                 direction, amount_cents, signed_cents, status, is_carryover, origin_file, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'EN_TRANSITO', 1, 'Arrastre mes anterior', ?)
            """,
            (
                new_pid, m["source"], m["tx_date"], m["reference"], m["ref_tail"],
                f"[Tránsito mes ant.] {m['description']}", m["category"],
                m["direction"], m["amount_cents"], m["signed_cents"],
                "Arrastrada del mes anterior.",
            ),
        )
        tr_cnt += 1

    return {"transit_items": tr_cnt, "system_issues": sys_cnt}


@app.post("/api/periods/{period_id}/rollover")
def rollover_next_month(period_id: int):
    with get_conn() as conn:
        p = conn.execute("SELECT * FROM periods WHERE id = ?", (period_id,)).fetchone()
        if not p:
            raise HTTPException(404, "Período no encontrado.")
        ny = p["year"] + (1 if p["month"] == 12 else 0)
        nm = 1 if p["month"] == 12 else p["month"] + 1
        existing = conn.execute(
            "SELECT * FROM periods WHERE account_id = ? AND year = ? AND month = ?",
            (p["account_id"], ny, nm),
        ).fetchone()
        if existing:
            raise HTTPException(400, f"El período {period_label(ny, nm)} ya existe en esta cuenta.")

        conn.execute(
            "UPDATE periods SET status = 'CERRADO', closed_at = datetime('now') WHERE id = ?",
            (period_id,),
        )
        cur = conn.execute(
            """
            INSERT INTO periods
                (account_id, year, month, label, status, use_mayor,
                 open_banco_cents, close_banco_cents, open_libros_cents, close_libros_cents,
                 open_mayor_cents, close_mayor_cents, notes)
            VALUES (?, ?, ?, ?, 'ABIERTO', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                p["account_id"], ny, nm, period_label(ny, nm), p["use_mayor"],
                p["close_banco_cents"], p["close_banco_cents"],
                p["close_libros_cents"], p["close_libros_cents"],
                p["close_mayor_cents"], p["close_mayor_cents"],
                f"Apertura automática desde cierre de {p['label']} con arrastre de tránsito y fallas del sistema.",
            ),
        )
        new_pid = cur.lastrowid
        carried = _carry_forward_items(conn, period_id, new_pid)
        new_p = dict(conn.execute("SELECT * FROM periods WHERE id = ?", (new_pid,)).fetchone())
    return {"period": new_p, "carried": carried}


@app.get("/api/periods/{period_id}/overview")
def get_period_overview(period_id: int):
    with get_conn() as conn:
        p_check = conn.execute("SELECT id FROM periods WHERE id = ?", (period_id,)).fetchone()
        if not p_check:
            raise HTTPException(404, "Período no encontrado.")

        sheet = compute_reconciliation_sheet(conn, period_id)
        movements = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM movements WHERE period_id = ? ORDER BY tx_date ASC, id ASC",
                (period_id,),
            ).fetchall()
        ]
        groups_raw = [
            dict(g)
            for g in conn.execute(
                "SELECT * FROM match_groups WHERE period_id = ? ORDER BY id ASC",
                (period_id,),
            ).fetchall()
        ]
        movs_by_g: Dict[int, List[dict]] = {}
        for m in movements:
            if m["match_group_id"]:
                movs_by_g.setdefault(m["match_group_id"], []).append(m)

        groups = []
        for g in groups_raw:
            mems = movs_by_g.get(g["id"], [])
            sb = sum(x["signed_cents"] for x in mems if x["source"] == "BANCO")
            sl = sum(x["signed_cents"] for x in mems if x["source"] == "LIBROS")
            sm = sum(x["signed_cents"] for x in mems if x["source"] == "MAYOR")
            has_b = any(x["source"] == "BANCO" for x in mems)
            has_l = any(x["source"] == "LIBROS" for x in mems)
            has_m = any(x["source"] == "MAYOR" for x in mems)
            groups.append(
                {
                    **g,
                    "members": mems,
                    "has_banco": has_b,
                    "has_libros": has_l,
                    "has_mayor": has_m,
                    "sum_banco_cents": sb if has_b else None,
                    "sum_libros_cents": sl if has_l else None,
                    "sum_mayor_cents": sm if has_m else None,
                    "diff_banco_libros_cents": (sb - sl) if (has_b and has_l) else None,
                    "diff_libros_mayor_cents": (sl - sm) if (has_l and has_m) else None,
                    "diff_banco_mayor_cents": (sb - sm) if (has_b and has_m) else None,
                }
            )

        suspected_dups = find_suspected_duplicates(conn, period_id)
        issues = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM system_issues WHERE period_id = ? ORDER BY issue_date ASC, id ASC",
                (period_id,),
            ).fetchall()
        ]
        adjustments = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM adjustments WHERE period_id = ? ORDER BY adj_date ASC, id ASC",
                (period_id,),
            ).fetchall()
        ]

    return {
        "sheet": sheet,
        "movements": movements,
        "groups": groups,
        "suspected_duplicates": suspected_dups,
        "system_issues": issues,
        "adjustments": adjustments,
    }


@app.put("/api/periods/{period_id}")
def update_period(period_id: int, payload: PeriodUpdatePayload):
    with get_conn() as conn:
        p = conn.execute("SELECT * FROM periods WHERE id = ?", (period_id,)).fetchone()
        if not p:
            raise HTTPException(404, "Período no encontrado.")
        updates = {}
        for field in (
            "status", "open_banco_cents", "close_banco_cents",
            "open_libros_cents", "close_libros_cents",
            "open_mayor_cents", "close_mayor_cents", "notes",
        ):
            val = getattr(payload, field)
            if val is not None:
                updates[field] = val
        if payload.use_mayor is not None:
            updates["use_mayor"] = 1 if payload.use_mayor else 0
        if updates:
            cols = ", ".join(f"{k} = ?" for k in updates.keys())
            conn.execute(f"UPDATE periods SET {cols} WHERE id = ?", (*updates.values(), period_id))
            sync_suggested_adjustments(conn, period_id)
        return dict(conn.execute("SELECT * FROM periods WHERE id = ?", (period_id,)).fetchone())


@app.post("/api/periods/{period_id}/recalc-balances")
def recalc_closing_balances(period_id: int):
    """Recalculate closing balances from Opening + Net non-carryover movements for each source."""
    with get_conn() as conn:
        p = conn.execute("SELECT * FROM periods WHERE id = ?", (period_id,)).fetchone()
        if not p:
            raise HTTPException(404, "Período no encontrado.")
        for src in ("BANCO", "LIBROS", "MAYOR"):
            net = conn.execute(
                "SELECT COALESCE(SUM(signed_cents), 0) AS s FROM movements WHERE period_id = ? AND source = ? AND is_carryover = 0",
                (period_id, src),
            ).fetchone()["s"]
            op = p[f"open_{src.lower()}_cents"]
            conn.execute(
                f"UPDATE periods SET close_{src.lower()}_cents = ? WHERE id = ?",
                (op + net, period_id),
            )
        return dict(conn.execute("SELECT * FROM periods WHERE id = ?", (period_id,)).fetchone())


@app.delete("/api/periods/{period_id}")
def delete_period(period_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM periods WHERE id = ?", (period_id,))
    return {"ok": True}


# -------------------- Import & Movements Endpoints --------------------

@app.post("/api/import/preview-file")
async def preview_import_file(
    file: UploadFile = File(...),
    source: str = Form("BANCO"),
    number_format: str = Form("auto"),
    default_year: Optional[int] = Form(None),
):
    src = source.upper()
    if src not in ("BANCO", "LIBROS", "MAYOR"):
        raise HTTPException(400, "Fuente inválida.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "El archivo está vacío.")
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(400, "El archivo excede 25 MB.")
    try:
        result = parse_uploaded_file(data, file.filename or "documento.pdf", src, number_format, default_year)
        result["filename"] = file.filename or "documento.pdf"
        result["source"] = src
        return result
    except ValueError as ve:
        raise HTTPException(400, str(ve))
    except Exception as exc:
        raise HTTPException(400, f"Error al leer el archivo: {exc}")


@app.post("/api/import/preview-text")
def preview_import_text(payload: TextPreviewPayload):
    src = payload.source.upper()
    if src not in ("BANCO", "LIBROS", "MAYOR"):
        raise HTTPException(400, "Fuente inválida.")
    if not payload.text.strip():
        raise HTTPException(400, "Pega el texto copiado del PDF para analizarlo.")
    res = parse_free_text(payload.text, src, payload.number_format, payload.default_year)
    res["filename"] = "Texto pegado desde PDF"
    res["source"] = src
    return res


@app.post("/api/periods/{period_id}/import-commit")
def commit_import(period_id: int, payload: ImportCommitPayload):
    src = payload.source.upper()
    if src not in ("BANCO", "LIBROS", "MAYOR"):
        raise HTTPException(400, "Fuente inválida.")
    with get_conn() as conn:
        p = conn.execute("SELECT * FROM periods WHERE id = ?", (period_id,)).fetchone()
        if not p:
            raise HTTPException(404, "Período no encontrado.")

        if payload.replace_source:
            # Unmatch any groups that contained items from this source
            gids = [
                r["match_group_id"]
                for r in conn.execute(
                    "SELECT DISTINCT match_group_id FROM movements WHERE period_id = ? AND source = ? AND match_group_id IS NOT NULL",
                    (period_id, src),
                ).fetchall()
            ]
            conn.execute("DELETE FROM movements WHERE period_id = ? AND source = ?", (period_id, src))
            for gid in gids:
                rem = conn.execute("SELECT COUNT(*) AS c FROM movements WHERE match_group_id = ?", (gid,)).fetchone()["c"]
                if rem < 2:
                    conn.execute("UPDATE movements SET status = 'PENDIENTE', match_group_id = NULL WHERE match_group_id = ?", (gid,))
                    conn.execute("DELETE FROM match_groups WHERE id = ?", (gid,))

        inserted = 0
        for row in payload.rows:
            amt = abs(int(row.amount_cents))
            if amt == 0:
                continue
            d = "IN" if row.direction.upper() == "IN" else "OUT"
            signed = amt if d == "IN" else -amt
            iso_dt = parse_date_str(row.tx_date, p["year"]) or f"{p['year']:04d}-{p['month']:02d}-01"
            cat = row.category or auto_categorize(row.description, d, src)
            conn.execute(
                """
                INSERT INTO movements
                    (period_id, source, tx_date, reference, ref_tail, description, category,
                     direction, amount_cents, signed_cents, running_balance_cents, status, origin_file)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDIENTE', ?)
                """,
                (
                    period_id, src, iso_dt, row.reference.strip(),
                    extract_ref_tail(row.reference), row.description.strip(),
                    cat, d, amt, signed, row.running_balance_cents, payload.origin_file,
                ),
            )
            inserted += 1

        if src == "MAYOR" and inserted > 0:
            conn.execute("UPDATE periods SET use_mayor = 1 WHERE id = ?", (period_id,))

        if payload.update_balances:
            col_o = f"open_{src.lower()}_cents"
            col_c = f"close_{src.lower()}_cents"
            if payload.opening_balance_cents is not None:
                conn.execute(f"UPDATE periods SET {col_o} = ? WHERE id = ?", (payload.opening_balance_cents, period_id))
            if payload.closing_balance_cents is not None:
                conn.execute(f"UPDATE periods SET {col_c} = ? WHERE id = ?", (payload.closing_balance_cents, period_id))
            elif payload.opening_balance_cents is not None:
                net = conn.execute(
                    "SELECT COALESCE(SUM(signed_cents), 0) AS s FROM movements WHERE period_id = ? AND source = ? AND is_carryover = 0",
                    (period_id, src),
                ).fetchone()["s"]
                conn.execute(f"UPDATE periods SET {col_c} = ? WHERE id = ?", (payload.opening_balance_cents + net, period_id))

        rec_stats = None
        if payload.auto_reconcile:
            rec_stats = run_auto_reconciliation(conn, period_id, reset_existing=False)
        else:
            sync_suggested_adjustments(conn, period_id)

    return {"inserted": inserted, "reconciliation": rec_stats}


@app.post("/api/periods/{period_id}/movements")
def create_movement(period_id: int, payload: MovementCreatePayload):
    src = payload.source.upper()
    if src not in ("BANCO", "LIBROS", "MAYOR"):
        raise HTTPException(400, "Fuente inválida.")
    amt = abs(int(payload.amount_cents))
    if amt <= 0:
        raise HTTPException(400, "El monto debe ser mayor a cero.")
    d = "IN" if payload.direction.upper() == "IN" else "OUT"
    signed = amt if d == "IN" else -amt
    with get_conn() as conn:
        p = conn.execute("SELECT * FROM periods WHERE id = ?", (period_id,)).fetchone()
        if not p:
            raise HTTPException(404, "Período no encontrado.")
        dt = parse_date_str(payload.tx_date, p["year"]) or payload.tx_date
        cat = payload.category or auto_categorize(payload.description, d, src)
        cur = conn.execute(
            """
            INSERT INTO movements
                (period_id, source, tx_date, reference, ref_tail, description, category,
                 direction, amount_cents, signed_cents, status, origin_file, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDIENTE', 'Carga manual', ?)
            """,
            (
                period_id, src, dt, payload.reference.strip(),
                extract_ref_tail(payload.reference), payload.description.strip(),
                cat, d, amt, signed, payload.note.strip(),
            ),
        )
        sync_suggested_adjustments(conn, period_id)
        return dict(conn.execute("SELECT * FROM movements WHERE id = ?", (cur.lastrowid,)).fetchone())


@app.put("/api/movements/{movement_id}")
def update_movement(movement_id: int, payload: MovementUpdatePayload):
    allowed_statuses = {
        "PENDIENTE", "CONCILIADO", "CONCILIADO_DIF", "EN_TRANSITO",
        "AJUSTE_BANCO", "DUPLICADO", "REVERSO", "FALLA_SISTEMA", "ERROR_BANCO",
    }
    with get_conn() as conn:
        m = conn.execute("SELECT * FROM movements WHERE id = ?", (movement_id,)).fetchone()
        if not m:
            raise HTTPException(404, "Movimiento no encontrado.")

        new_dir = (payload.direction or m["direction"]).upper()
        new_amt = abs(int(payload.amount_cents)) if payload.amount_cents is not None else m["amount_cents"]
        new_signed = new_amt if new_dir == "IN" else -new_amt
        new_ref = payload.reference.strip() if payload.reference is not None else m["reference"]
        new_desc = payload.description.strip() if payload.description is not None else m["description"]
        new_dt = payload.tx_date if payload.tx_date is not None else m["tx_date"]
        new_cat = payload.category if payload.category is not None else m["category"]
        new_st = payload.status if (payload.status and payload.status in allowed_statuses) else m["status"]
        new_note = payload.note if payload.note is not None else m["note"]

        conn.execute(
            """
            UPDATE movements
            SET tx_date = ?, reference = ?, ref_tail = ?, description = ?, category = ?,
                direction = ?, amount_cents = ?, signed_cents = ?, status = ?, note = ?
            WHERE id = ?
            """,
            (
                new_dt, new_ref, extract_ref_tail(new_ref), new_desc, new_cat,
                new_dir, new_amt, new_signed, new_st, new_note, movement_id,
            ),
        )

        # If classified as standalone FALLA_SISTEMA, auto-register in system_issues if not there yet
        if new_st == "FALLA_SISTEMA" and m["source"] in ("LIBROS", "MAYOR"):
            ex = conn.execute("SELECT id FROM system_issues WHERE movement_id = ?", (movement_id,)).fetchone()
            if not ex:
                adj_l = -new_signed if m["source"] == "LIBROS" else 0
                adj_m = -new_signed if m["source"] == "MAYOR" else 0
                conn.execute(
                    """
                    INSERT INTO system_issues
                        (period_id, movement_id, code, issue_date, title, affected_module,
                         libros_cents, mayor_cents, adj_libros_cents, adj_mayor_cents,
                         carry_forward, status, audit_note)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'ACTIVA', ?)
                    """,
                    (
                        m["period_id"], movement_id, new_ref or f"MOV-{movement_id}", new_dt,
                        new_desc, m["source"],
                        new_signed if m["source"] == "LIBROS" else None,
                        new_signed if m["source"] == "MAYOR" else None,
                        adj_l, adj_m,
                        new_note or f"Partida errónea del sistema en {m['source']} por Bs. {cents_to_ve(new_amt)}.",
                    ),
                )
        elif m["status"] == "FALLA_SISTEMA" and new_st != "FALLA_SISTEMA":
            conn.execute("DELETE FROM system_issues WHERE movement_id = ?", (movement_id,))

        sync_suggested_adjustments(conn, m["period_id"])
        return dict(conn.execute("SELECT * FROM movements WHERE id = ?", (movement_id,)).fetchone())


@app.delete("/api/movements/{movement_id}")
def delete_movement(movement_id: int):
    with get_conn() as conn:
        m = conn.execute("SELECT * FROM movements WHERE id = ?", (movement_id,)).fetchone()
        if not m:
            raise HTTPException(404, "Movimiento no encontrado.")
        gid = m["match_group_id"]
        pid = m["period_id"]
        conn.execute("DELETE FROM movements WHERE id = ?", (movement_id,))
        if gid:
            rem = conn.execute("SELECT COUNT(*) AS c FROM movements WHERE match_group_id = ?", (gid,)).fetchone()["c"]
            if rem < 2:
                conn.execute("UPDATE movements SET status = 'PENDIENTE', match_group_id = NULL WHERE match_group_id = ?", (gid,))
                conn.execute("DELETE FROM system_issues WHERE match_group_id = ?", (gid,))
                conn.execute("DELETE FROM match_groups WHERE id = ?", (gid,))
        sync_suggested_adjustments(conn, pid)
    return {"ok": True}


@app.delete("/api/periods/{period_id}/sources/{source}")
def clear_period_source(period_id: int, source: str):
    src = source.upper()
    if src not in ("BANCO", "LIBROS", "MAYOR"):
        raise HTTPException(400, "Fuente inválida.")
    with get_conn() as conn:
        gids = [
            r["match_group_id"]
            for r in conn.execute(
                "SELECT DISTINCT match_group_id FROM movements WHERE period_id = ? AND source = ? AND match_group_id IS NOT NULL",
                (period_id, src),
            ).fetchall()
        ]
        conn.execute("DELETE FROM movements WHERE period_id = ? AND source = ?", (period_id, src))
        for gid in gids:
            rem = conn.execute("SELECT COUNT(*) AS c FROM movements WHERE match_group_id = ?", (gid,)).fetchone()["c"]
            if rem < 2:
                conn.execute("UPDATE movements SET status = 'PENDIENTE', match_group_id = NULL WHERE match_group_id = ?", (gid,))
                conn.execute("DELETE FROM system_issues WHERE match_group_id = ?", (gid,))
                conn.execute("DELETE FROM match_groups WHERE id = ?", (gid,))
        sync_suggested_adjustments(conn, period_id)
    return {"ok": True}


# -------------------- Reconciliation Actions --------------------

@app.post("/api/periods/{period_id}/auto-reconcile")
def trigger_auto_reconcile(period_id: int, payload: AutoReconcilePayload):
    with get_conn() as conn:
        p = conn.execute("SELECT id FROM periods WHERE id = ?", (period_id,)).fetchone()
        if not p:
            raise HTTPException(404, "Período no encontrado.")
        stats = run_auto_reconciliation(conn, period_id, reset_existing=payload.reset_existing)
    return {"stats": stats}


@app.post("/api/periods/{period_id}/manual-match")
def create_manual_match(period_id: int, payload: ManualMatchPayload):
    if len(payload.movement_ids) < 2:
        raise HTTPException(400, "Selecciona al menos 2 movimientos para conciliar.")
    with get_conn() as conn:
        placeholders = ",".join("?" for _ in payload.movement_ids)
        rows = [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM movements WHERE period_id = ? AND id IN ({placeholders})",
                (period_id, *payload.movement_ids),
            ).fetchall()
        ]
        if len(rows) < 2:
            raise HTTPException(400, "No se encontraron los movimientos seleccionados.")

        by_s: Dict[str, int] = {}
        counts_s: Dict[str, int] = {}
        for r in rows:
            by_s[r["source"]] = by_s.get(r["source"], 0) + r["signed_cents"]
            counts_s[r["source"]] = counts_s.get(r["source"], 0) + 1

        sources = list(by_s.keys())

        # Reversal within a single source
        if len(sources) == 1 or payload.match_as_reversal:
            if len(sources) > 1:
                raise HTTPException(400, "Un reverso/anulación debe ser entre movimientos de la misma fuente.")
            net = sum(r["signed_cents"] for r in rows)
            if net != 0:
                raise HTTPException(
                    400,
                    f"Para netear como reverso/anulación en {sources[0]}, los montos deben sumar Bs. 0,00 (suma actual: Bs. {cents_to_ve(net, show_plus=True)}).",
                )
            cur = conn.execute(
                """
                INSERT INTO match_groups (period_id, match_type, canonical_source, canonical_cents, confidence, note)
                VALUES (?, 'REVERSO', ?, 0, 100, ?)
                """,
                (period_id, sources[0], payload.note.strip() or f"Reverso/anulación manual en {sources[0]}"),
            )
            gid = cur.lastrowid
            for r in rows:
                conn.execute("UPDATE movements SET status = 'REVERSO', match_group_id = ? WHERE id = ?", (gid, r["id"]))
            sync_suggested_adjustments(conn, period_id)
            return {"group_id": gid, "match_type": "REVERSO"}

        # Multi-source match (supports 1-to-1, 3-way, 1-to-many batch, and match with difference!)
        canon_src = payload.canonical_source if payload.canonical_source in by_s else (
            "BANCO" if "BANCO" in by_s else ("MAYOR" if "MAYOR" in by_s else sources[0])
        )
        canon_cents = by_s[canon_src]
        all_equal = len(set(by_s.values())) == 1
        is_batch = any(c > 1 for c in counts_s.values())

        if all_equal:
            mtype = "LOTE" if is_batch else ("EXACTO_TRIPLE" if len(sources) == 3 else "EXACTO_DOBLE")
            mov_st = "CONCILIADO"
            res_type = "NINGUNA"
            dkind = ""
            default_note = "Conciliación agrupada (lote) manual exacta" if is_batch else "Conciliación manual exacta"
        else:
            mtype = "CON_DIFERENCIA"
            mov_st = "CONCILIADO_DIF"
            res_type = payload.resolution_type if payload.resolution_type in ("AJUSTE", "FALLA_SISTEMA", "ERROR_BANCO") else "AJUSTE"
            other_val = next(v for s, v in by_s.items() if s != canon_src)
            dkind, expl = classify_discrepancy(abs(canon_cents), abs(other_val))
            default_note = f"Conciliación manual con diferencia: {expl}"

        cur = conn.execute(
            """
            INSERT INTO match_groups
                (period_id, match_type, canonical_source, canonical_cents, resolution_type, discrepancy_kind, confidence, note)
            VALUES (?, ?, ?, ?, ?, ?, 100, ?)
            """,
            (period_id, mtype, canon_src, canon_cents, res_type, dkind, payload.note.strip() or default_note),
        )
        gid = cur.lastrowid
        for r in rows:
            conn.execute("UPDATE movements SET status = ?, match_group_id = ? WHERE id = ?", (mov_st, gid, r["id"]))

        if not all_equal and res_type == "FALLA_SISTEMA":
            adj_l = (canon_cents - by_s["LIBROS"]) if "LIBROS" in by_s else 0
            adj_m = (canon_cents - by_s["MAYOR"]) if "MAYOR" in by_s else 0
            mod = "AMBOS" if (adj_l and adj_m) else ("MAYOR" if adj_m else "LIBROS")
            anchor = rows[0]
            conn.execute(
                """
                INSERT INTO system_issues
                    (period_id, match_group_id, code, issue_date, title, affected_module,
                     banco_cents, libros_cents, mayor_cents, adj_libros_cents, adj_mayor_cents,
                     carry_forward, status, audit_note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'ACTIVA', ?)
                """,
                (
                    period_id, gid, anchor["reference"] or f"GRP-{gid}", anchor["tx_date"],
                    anchor["description"], mod,
                    by_s.get("BANCO"), by_s.get("LIBROS"), by_s.get("MAYOR"),
                    adj_l, adj_m, payload.note.strip() or default_note,
                ),
            )

        sync_suggested_adjustments(conn, period_id)
    return {"group_id": gid, "match_type": mtype}


@app.put("/api/match-groups/{group_id}")
def update_match_group(group_id: int, payload: MatchGroupUpdatePayload):
    with get_conn() as conn:
        g = conn.execute("SELECT * FROM match_groups WHERE id = ?", (group_id,)).fetchone()
        if not g:
            raise HTTPException(404, "Grupo no encontrado.")
        mems = [dict(r) for r in conn.execute("SELECT * FROM movements WHERE match_group_id = ?", (group_id,)).fetchall()]
        by_s: Dict[str, int] = {}
        for m in mems:
            by_s[m["source"]] = by_s.get(m["source"], 0) + m["signed_cents"]

        canon_src = payload.canonical_source if (payload.canonical_source and payload.canonical_source in by_s) else g["canonical_source"]
        canon_cents = by_s.get(canon_src, g["canonical_cents"])
        res_type = payload.resolution_type if payload.resolution_type is not None else g["resolution_type"]
        note = payload.note if payload.note is not None else g["note"]

        conn.execute(
            "UPDATE match_groups SET canonical_source = ?, canonical_cents = ?, resolution_type = ?, note = ? WHERE id = ?",
            (canon_src, canon_cents, res_type, note, group_id),
        )

        if res_type == "FALLA_SISTEMA":
            adj_l = (canon_cents - by_s["LIBROS"]) if "LIBROS" in by_s else 0
            adj_m = (canon_cents - by_s["MAYOR"]) if "MAYOR" in by_s else 0
            mod = "AMBOS" if (adj_l and adj_m) else ("MAYOR" if adj_m else "LIBROS")
            ex = conn.execute("SELECT id FROM system_issues WHERE match_group_id = ?", (group_id,)).fetchone()
            if ex:
                conn.execute(
                    """
                    UPDATE system_issues
                    SET affected_module = ?, banco_cents = ?, libros_cents = ?, mayor_cents = ?,
                        adj_libros_cents = ?, adj_mayor_cents = ?, audit_note = ?, status = 'ACTIVA'
                    WHERE id = ?
                    """,
                    (mod, by_s.get("BANCO"), by_s.get("LIBROS"), by_s.get("MAYOR"), adj_l, adj_m, note, ex["id"]),
                )
            elif mems:
                anchor = mems[0]
                conn.execute(
                    """
                    INSERT INTO system_issues
                        (period_id, match_group_id, code, issue_date, title, affected_module,
                         banco_cents, libros_cents, mayor_cents, adj_libros_cents, adj_mayor_cents,
                         carry_forward, status, audit_note)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'ACTIVA', ?)
                    """,
                    (
                        g["period_id"], group_id, anchor["reference"] or f"DIF-{group_id}",
                        anchor["tx_date"], anchor["description"], mod,
                        by_s.get("BANCO"), by_s.get("LIBROS"), by_s.get("MAYOR"),
                        adj_l, adj_m, note,
                    ),
                )
        else:
            conn.execute("DELETE FROM system_issues WHERE match_group_id = ?", (group_id,))

        sync_suggested_adjustments(conn, g["period_id"])
    return {"ok": True}


@app.delete("/api/match-groups/{group_id}")
def delete_match_group(group_id: int):
    with get_conn() as conn:
        g = conn.execute("SELECT * FROM match_groups WHERE id = ?", (group_id,)).fetchone()
        if not g:
            raise HTTPException(404, "Grupo no encontrado.")
        pid = g["period_id"]
        conn.execute("UPDATE movements SET status = 'PENDIENTE', match_group_id = NULL WHERE match_group_id = ?", (group_id,))
        conn.execute("DELETE FROM system_issues WHERE match_group_id = ?", (group_id,))
        conn.execute("DELETE FROM match_groups WHERE id = ?", (group_id,))
        sync_suggested_adjustments(conn, pid)
    return {"ok": True}


# -------------------- System Issues Endpoints --------------------

@app.post("/api/periods/{period_id}/system-issues")
def create_system_issue(period_id: int, payload: SystemIssuePayload):
    with get_conn() as conn:
        p = conn.execute("SELECT id FROM periods WHERE id = ?", (period_id,)).fetchone()
        if not p:
            raise HTTPException(404, "Período no encontrado.")
        cur = conn.execute(
            """
            INSERT INTO system_issues
                (period_id, code, issue_date, title, affected_module,
                 banco_cents, libros_cents, mayor_cents, adj_libros_cents, adj_mayor_cents,
                 affects_opening, carry_forward, status, audit_note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                period_id,
                payload.code.strip() or "SIS-HIST",
                payload.issue_date,
                payload.title.strip(),
                payload.affected_module,
                payload.banco_cents,
                payload.libros_cents,
                payload.mayor_cents,
                payload.adj_libros_cents,
                payload.adj_mayor_cents,
                1 if payload.affects_opening else 0,
                1 if payload.carry_forward else 0,
                payload.status,
                payload.audit_note.strip(),
            ),
        )
        return dict(conn.execute("SELECT * FROM system_issues WHERE id = ?", (cur.lastrowid,)).fetchone())


@app.put("/api/system-issues/{issue_id}")
def update_system_issue(issue_id: int, payload: SystemIssuePayload):
    with get_conn() as conn:
        iss = conn.execute("SELECT * FROM system_issues WHERE id = ?", (issue_id,)).fetchone()
        if not iss:
            raise HTTPException(404, "Registro no encontrado.")
        conn.execute(
            """
            UPDATE system_issues
            SET code = ?, issue_date = ?, title = ?, affected_module = ?,
                banco_cents = ?, libros_cents = ?, mayor_cents = ?,
                adj_libros_cents = ?, adj_mayor_cents = ?,
                affects_opening = ?, carry_forward = ?, status = ?, audit_note = ?
            WHERE id = ?
            """,
            (
                payload.code.strip(), payload.issue_date, payload.title.strip(), payload.affected_module,
                payload.banco_cents, payload.libros_cents, payload.mayor_cents,
                payload.adj_libros_cents, payload.adj_mayor_cents,
                1 if payload.affects_opening else 0, 1 if payload.carry_forward else 0,
                payload.status, payload.audit_note.strip(), issue_id,
            ),
        )
        return dict(conn.execute("SELECT * FROM system_issues WHERE id = ?", (issue_id,)).fetchone())


@app.delete("/api/system-issues/{issue_id}")
def delete_system_issue(issue_id: int):
    with get_conn() as conn:
        iss = conn.execute("SELECT * FROM system_issues WHERE id = ?", (issue_id,)).fetchone()
        if not iss:
            raise HTTPException(404, "Registro no encontrado.")
        if iss["match_group_id"]:
            conn.execute("UPDATE match_groups SET resolution_type = 'AJUSTE' WHERE id = ?", (iss["match_group_id"],))
        if iss["movement_id"]:
            conn.execute("UPDATE movements SET status = 'PENDIENTE' WHERE id = ?", (iss["movement_id"],))
        conn.execute("DELETE FROM system_issues WHERE id = ?", (issue_id,))
        sync_suggested_adjustments(conn, iss["period_id"])
    return {"ok": True}


# -------------------- Adjustments Endpoints --------------------

@app.post("/api/periods/{period_id}/adjustments")
def create_adjustment(period_id: int, payload: AdjustmentCreatePayload):
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO adjustments
                (period_id, source_ref_type, adj_date, target, reference, concept,
                 debit_account, credit_account, amount_cents, applied, is_manual, note)
            VALUES (?, 'MANUAL', ?, ?, ?, ?, ?, ?, ?, 0, 1, ?)
            """,
            (
                period_id, payload.adj_date, payload.target, payload.reference.strip(),
                payload.concept.strip(), payload.debit_account.strip(), payload.credit_account.strip(),
                abs(int(payload.amount_cents)), payload.note.strip(),
            ),
        )
        return dict(conn.execute("SELECT * FROM adjustments WHERE id = ?", (cur.lastrowid,)).fetchone())


@app.put("/api/adjustments/{adj_id}")
def update_adjustment(adj_id: int, payload: AdjustmentUpdatePayload):
    with get_conn() as conn:
        a = conn.execute("SELECT * FROM adjustments WHERE id = ?", (adj_id,)).fetchone()
        if not a:
            raise HTTPException(404, "Ajuste no encontrado.")
        applied = (1 if payload.applied else 0) if payload.applied is not None else a["applied"]
        target = payload.target if payload.target is not None else a["target"]
        concept = payload.concept if payload.concept is not None else a["concept"]
        deb = payload.debit_account if payload.debit_account is not None else a["debit_account"]
        cred = payload.credit_account if payload.credit_account is not None else a["credit_account"]
        amt = abs(int(payload.amount_cents)) if payload.amount_cents is not None else a["amount_cents"]
        note = payload.note if payload.note is not None else a["note"]
        conn.execute(
            """
            UPDATE adjustments
            SET applied = ?, target = ?, concept = ?, debit_account = ?, credit_account = ?, amount_cents = ?, note = ?
            WHERE id = ?
            """,
            (applied, target, concept, deb, cred, amt, note, adj_id),
        )
        return dict(conn.execute("SELECT * FROM adjustments WHERE id = ?", (adj_id,)).fetchone())


@app.delete("/api/adjustments/{adj_id}")
def delete_adjustment(adj_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM adjustments WHERE id = ?", (adj_id,))
    return {"ok": True}


# -------------------- Exports, Settings & Samples --------------------

@app.get("/api/periods/{period_id}/export/excel")
def download_excel(period_id: int):
    with get_conn() as conn:
        p = conn.execute("SELECT label FROM periods WHERE id = ?", (period_id,)).fetchone()
        if not p:
            raise HTTPException(404, "Período no encontrado.")
        content = export_reconciliation_excel(conn, period_id)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", p["label"])
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="Conciliacion_BT_{slug}.xlsx"'},
    )


@app.get("/api/periods/{period_id}/export/pdf")
def download_pdf(period_id: int):
    with get_conn() as conn:
        p = conn.execute("SELECT label FROM periods WHERE id = ?", (period_id,)).fetchone()
        if not p:
            raise HTTPException(404, "Período no encontrado.")
        content = export_reconciliation_pdf(conn, period_id)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", p["label"])
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="Acta_Conciliacion_BT_{slug}.pdf"'},
    )


@app.get("/api/samples")
def list_samples():
    files = sorted(SAMPLES_DIR.glob("*.pdf"))
    return {"files": [{"name": f.name, "size": f.stat().st_size} for f in files]}


@app.get("/api/samples/{filename}")
def get_sample_pdf(filename: str):
    safe = Path(filename).name
    target = SAMPLES_DIR / safe
    if not target.exists() or target.suffix.lower() != ".pdf":
        raise HTTPException(404, "Archivo de ejemplo no encontrado.")
    return FileResponse(str(target), media_type="application/pdf", filename=safe)


@app.get("/api/settings")
def read_settings():
    with get_conn() as conn:
        return get_settings(conn)


@app.put("/api/settings")
def save_settings(payload: Dict[str, str]):
    with get_conn() as conn:
        for k, v in payload.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(k), str(v)),
            )
        return get_settings(conn)


@app.post("/api/demo/reset")
def reset_demo():
    ensure_demo_seeded(force=True)
    return {"ok": True}


# -------------------- Frontend Serving --------------------

@app.get("/")
def serve_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return JSONResponse(content={"ok": True, "app": "ConciliaBT", "docs": "/docs"})


if STATIC_ASSETS_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_ASSETS_DIR)), name="static")
