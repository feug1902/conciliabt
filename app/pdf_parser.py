import csv
import io
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import openpyxl
import pdfplumber
from pypdf import PdfReader

from .database import auto_categorize, cents_to_ve, extract_ref_tail, parse_amount_to_cents

MONTH_MAP_ES = {
    "ENE": 1, "ENERO": 1, "JAN": 1,
    "FEB": 2, "FEBRERO": 2,
    "MAR": 3, "MARZO": 3,
    "ABR": 4, "ABRIL": 4, "APR": 4,
    "MAY": 5, "MAYO": 5,
    "JUN": 6, "JUNIO": 6,
    "JUL": 7, "JULIO": 7,
    "AGO": 8, "AGOSTO": 8, "AUG": 8,
    "SEP": 9, "SEPT": 9, "SET": 9, "SEPTIEMBRE": 9,
    "OCT": 10, "OCTUBRE": 10,
    "NOV": 11, "NOVIEMBRE": 11,
    "DIC": 12, "DICIEMBRE": 12, "DEC": 12,
}

# Regex for monetary tokens (with decimals, typical in statements & ledgers)
AMOUNT_TOKEN_RE = re.compile(
    r"(?<!\d)"
    r"(\(?[+-]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)[.,]\d{2}\)?-?(?:\s*(?:CR|DB|ND|NC))?)"
    r"(?!\d)",
    re.IGNORECASE,
)

DATE_START_RE = re.compile(
    r"^\s*(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2}|\d{1,2}[\s\-/][A-Za-z]{3,9}[\s\-/]\d{2,4})\b"
)

DATE_ANY_RE = re.compile(
    r"\b(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2}|\d{1,2}[\s\-/][A-Za-z]{3,9}[\s\-/]\d{2,4})\b"
)

SKIP_ROW_PATTERNS = re.compile(
    r"(?i)^\s*(total|totales|sub-?total|subtotal|van\b|vienen\b|p[aá]gina|resumen del|estado de cuenta|mayor anal[ií]tico|libro de banco|fecha\s+ref)"
)
OPEN_BAL_RE = re.compile(r"(?i)\b(saldo\s+inicial|saldo\s+anterior|saldo\s+al\s+inicio|vienen\s+saldo)\b")
CLOSE_BAL_RE = re.compile(r"(?i)\b(saldo\s+final|saldo\s+actual|saldo\s+al\s+cierre|saldo\s+disponible|saldo\s+en\s+cuenta)\b")
ACCT_NUM_RE = re.compile(r"\b(0163[\-\s]?\d{4}[\-\s]?\d{2}[\-\s]?\d{10}|\d{20})\b")


def parse_date_str(raw: str, default_year: Optional[int] = None) -> Optional[str]:
    """Normalize Venezuelan date strings (DD/MM/YYYY, DD-MM-YY, YYYY-MM-DD, DD OCT 2026) -> YYYY-MM-DD."""
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw.strftime("%Y-%m-%d")
    s = str(raw).strip()
    if not s:
        return None
    # Strip trailing time if present
    s = re.split(r"\s+\d{1,2}:\d{2}", s)[0].strip()

    # YYYY-MM-DD
    m = re.match(r"^(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})$", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # DD/MM/YYYY or DD/MM/YY
    m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})$", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000 if y < 70 else 1900
        # Handle occasional MM/DD/YYYY if mo > 12 and d <= 12
        if mo > 12 and 1 <= d <= 12:
            d, mo = mo, d
        if 1990 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # DD MMM YYYY
    m = re.match(r"^(\d{1,2})[\s\-/.]([A-Za-zÁÉÍÓÚáéíóú]{3,10})[\s\-/.](\d{2,4})$", s)
    if m:
        d = int(m.group(1))
        mon_txt = m.group(2).upper()[:3]
        mo = MONTH_MAP_ES.get(m.group(2).upper()) or MONTH_MAP_ES.get(mon_txt)
        y = int(m.group(3))
        if y < 100:
            y += 2000
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # DD/MM without year
    if default_year:
        m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})$", s)
        if m:
            d, mo = int(m.group(1)), int(m.group(2))
            if 1 <= mo <= 12 and 1 <= d <= 31:
                return f"{default_year:04d}-{mo:02d}-{d:02d}"

    return None


def detect_number_format(text: str) -> str:
    """Detect whether a document predominantly uses Venezuelan (1.234,56) or US (1,234.56) numbers."""
    ve_hits = len(re.findall(r"\b\d{1,3}(?:\.\d{3})+,\d{2}\b", text)) + len(re.findall(r"\b\d+,\d{2}\b", text))
    us_hits = len(re.findall(r"\b\d{1,3}(?:,\d{3})+\.\d{2}\b", text)) + len(re.findall(r"\b\d+\.\d{2}\b", text))
    if ve_hits >= us_hits and ve_hits > 0:
        return "ve"
    if us_hits > ve_hits:
        return "us"
    return "auto"


def classify_header_cell(cell: str, source: str) -> Optional[str]:
    """
    Map a column header name to semantic role:
      'date', 'ref', 'type', 'desc', 'in', 'out', 'amount', 'balance'
    Respects the difference between BANCO statements and LIBROS/MAYOR accounting reports.
    """
    c = re.sub(r"\s+", " ", (cell or "").strip().lower())
    c_ascii = (
        c.replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
    )
    if not c_ascii or len(c_ascii) > 45:
        return None

    if any(k in c_ascii for k in ("saldo", "balance", "disponible")):
        return "balance"
    if any(k in c_ascii for k in ("fecha", "f. oper", "f. valor", "fec.")):
        return "date"
    if any(k in c_ascii for k in ("referencia", "ref.", "ref", "documento", "nro. doc", "comprobante", "asiento", "cheque/ref", "numero", "nro")):
        if "cuenta" not in c_ascii:
            return "ref"
    if any(k in c_ascii for k in ("descrip", "concepto", "detalle", "beneficiario", "glosa", "observacion", "motivo")):
        return "desc"
    if c_ascii in ("tipo", "t.m.", "tm", "oper", "cod", "codigo", "mov", "nat"):
        return "type"

    if source == "BANCO":
        # Bank statement: Crédito/Abono/Depósito = IN (+), Débito/Cargo/Retiro = OUT (-)
        if any(k in c_ascii for k in ("debito", "cargo", "retiro", "egreso", "salida", "cheque")):
            return "out"
        if any(k in c_ascii for k in ("credito", "abono", "deposito", "ingreso", "entrada")):
            return "in"
    elif source == "MAYOR":
        # General Ledger (Asset account Banco): Debe/Débito/Cargo = IN (+), Haber/Crédito/Abono = OUT (-)
        if any(k in c_ascii for k in ("debe", "debito", "cargo", "ingreso", "entrada")):
            return "in"
        if any(k in c_ascii for k in ("haber", "credito", "abono", "egreso", "salida")):
            return "out"
    else:
        # LIBROS (Tesorería): Debe/Ingreso/Depósito/Abono = IN (+), Haber/Egreso/Retiro/Cargo/Cheque = OUT (-)
        if any(k in c_ascii for k in ("debe", "ingreso", "deposito", "entrada")):
            return "in"
        if any(k in c_ascii for k in ("haber", "egreso", "retiro", "salida", "cheque", "pago")):
            return "out"
        if any(k in c_ascii for k in ("cargo", "debito")):
            return "out"
        if any(k in c_ascii for k in ("abono", "credito")):
            return "in"

    if any(k in c_ascii for k in ("monto", "importe", "valor", "bolivares", "total mov")):
        return "amount"

    return None


def infer_direction_from_text(desc: str, type_str: str, source: str) -> str:
    """Fallback heuristic when neither column role nor running balance indicates direction."""
    t = f"{type_str} {desc}".upper()
    if re.search(r"\b(NC|N/C|ABONO|DEPOSITO|DEPÓSITO|COBRO|COBRANZA|REINTEGRO|REVERSO TRANSF RECHAZADA|INTERESES|APORTE|INGRESO|RECIBID[AO])\b", t):
        return "IN"
    if re.search(r"\b(ND|N/D|CARGO|COMISION|COMISIÓN|IGTF|PAGO|NOMINA|NÓMINA|BOLSA|COMPRA|EGRESO|OP-\d+|CHEQUE|RETIRO|SERVICIO|PROVEEDOR)\b", t):
        return "OUT"
    return "OUT"


def reconcile_directions_with_balances(
    rows: List[dict], opening_balance_cents: Optional[int]
) -> Tuple[List[dict], Optional[int], Optional[int], int]:
    """
    Use running balances (when present) to verify and self-correct movement direction (IN vs OUT)!
    Returns (rows, inferred_open_cents, inferred_close_cents, corrections_count).
    """
    corrections = 0
    prev_bal = opening_balance_cents
    inferred_open = opening_balance_cents
    inferred_close = None

    for idx, r in enumerate(rows):
        bal = r.get("running_balance_cents")
        amt = r.get("amount_cents", 0)
        if bal is not None:
            inferred_close = bal
            if prev_bal is not None and amt > 0:
                delta = bal - prev_bal
                if delta == amt and r["direction"] != "IN":
                    r["direction"] = "IN"
                    r["signed_cents"] = amt
                    corrections += 1
                elif delta == -amt and r["direction"] != "OUT":
                    r["direction"] = "OUT"
                    r["signed_cents"] = -amt
                    corrections += 1
            elif idx == 0 and inferred_open is None and amt > 0:
                # Infer opening balance from first row if direction was explicit
                if r.get("_explicit_dir"):
                    inferred_open = bal - r["signed_cents"]
            prev_bal = bal

    return rows, inferred_open, inferred_close, corrections


def _extract_balances_from_text(full_text: str, num_fmt: str) -> Tuple[Optional[int], Optional[int], Optional[str]]:
    open_cents = None
    close_cents = None
    acct_num = None

    m_acct = ACCT_NUM_RE.search(full_text)
    if m_acct:
        acct_num = m_acct.group(1)

    for line in full_text.splitlines():
        clean = line.strip()
        if not clean:
            continue
        if open_cents is None and OPEN_BAL_RE.search(clean):
            toks = AMOUNT_TOKEN_RE.findall(clean)
            if toks:
                val = parse_amount_to_cents(toks[-1], num_fmt)
                if val is not None:
                    open_cents = val
        if CLOSE_BAL_RE.search(clean):
            toks = AMOUNT_TOKEN_RE.findall(clean)
            if toks:
                val = parse_amount_to_cents(toks[-1], num_fmt)
                if val is not None:
                    close_cents = val

    return open_cents, close_cents, acct_num


def _parse_table_rows(
    table: List[List[Optional[str]]],
    source: str,
    num_fmt: str,
    default_year: Optional[int],
) -> Tuple[List[dict], Optional[int], Optional[int]]:
    """Parse a 2D table (from pdfplumber, Excel, or CSV) into movement dicts."""
    rows_out: List[dict] = []
    open_cents: Optional[int] = None
    close_cents: Optional[int] = None

    col_map: Dict[str, int] = {}
    desc_cols: List[int] = []
    header_idx = -1

    # Find header row within first 15 rows
    for idx, row in enumerate(table[:20]):
        cells = [str(c or "").strip() for c in row]
        if not any(cells):
            continue
        joined = " ".join(cells)
        if open_cents is None and OPEN_BAL_RE.search(joined):
            toks = AMOUNT_TOKEN_RE.findall(joined)
            if toks:
                open_cents = parse_amount_to_cents(toks[-1], num_fmt)
        temp_map: Dict[str, int] = {}
        temp_desc: List[int] = []
        for ci, cell in enumerate(cells):
            role = classify_header_cell(cell, source)
            if role == "desc":
                temp_desc.append(ci)
                temp_map.setdefault("desc", ci)
            elif role and role not in temp_map:
                temp_map[role] = ci
        if "date" in temp_map and ("in" in temp_map or "out" in temp_map or "amount" in temp_map):
            col_map = temp_map
            desc_cols = temp_desc or ([temp_map["desc"]] if "desc" in temp_map else [])
            header_idx = idx
            break

    if header_idx < 0:
        return [], open_cents, close_cents

    for row in table[header_idx + 1 :]:
        cells = [re.sub(r"\s+", " ", str(c or "")).strip() for c in row]
        if not any(cells):
            continue
        joined = " ".join(c for c in cells if c)

        # Capture opening/closing balance lines inside the table
        if OPEN_BAL_RE.search(joined):
            bal_raw = cells[col_map["balance"]] if "balance" in col_map and col_map["balance"] < len(cells) else ""
            val = parse_amount_to_cents(bal_raw, num_fmt)
            if val is None:
                toks = AMOUNT_TOKEN_RE.findall(joined)
                if toks:
                    val = parse_amount_to_cents(toks[-1], num_fmt)
            if val is not None and open_cents is None:
                open_cents = val
            continue

        if CLOSE_BAL_RE.search(joined):
            bal_raw = cells[col_map["balance"]] if "balance" in col_map and col_map["balance"] < len(cells) else ""
            val = parse_amount_to_cents(bal_raw, num_fmt)
            if val is None:
                toks = AMOUNT_TOKEN_RE.findall(joined)
                if toks:
                    val = parse_amount_to_cents(toks[-1], num_fmt)
            if val is not None:
                close_cents = val
            continue

        if SKIP_ROW_PATTERNS.search(joined):
            continue

        def get_col(role: str) -> str:
            ci = col_map.get(role)
            if ci is not None and ci < len(cells):
                return cells[ci]
            return ""

        raw_date = get_col("date")
        tx_date = parse_date_str(raw_date, default_year)
        if not tx_date:
            # Multi-line description continuation: append to previous row if no amount
            in_v = parse_amount_to_cents(get_col("in"), num_fmt)
            out_v = parse_amount_to_cents(get_col("out"), num_fmt)
            amt_v = parse_amount_to_cents(get_col("amount"), num_fmt)
            if rows_out and not any(v for v in (in_v, out_v, amt_v)):
                extra = " ".join(cells[ci] for ci in desc_cols if ci < len(cells) and cells[ci]).strip()
                if extra and not classify_header_cell(extra, source):
                    rows_out[-1]["description"] = (rows_out[-1]["description"] + " " + extra).strip()
            continue

        ref = get_col("ref")
        type_str = get_col("type")
        desc = " ".join(cells[ci] for ci in desc_cols if ci < len(cells) and cells[ci]).strip()
        if not desc:
            # Collect leftover text columns
            used = set(col_map.values())
            desc = " ".join(cells[i] for i in range(len(cells)) if i not in used and cells[i]).strip()

        in_val = parse_amount_to_cents(get_col("in"), num_fmt) if "in" in col_map else None
        out_val = parse_amount_to_cents(get_col("out"), num_fmt) if "out" in col_map else None
        amt_val = parse_amount_to_cents(get_col("amount"), num_fmt) if "amount" in col_map else None
        bal_val = parse_amount_to_cents(get_col("balance"), num_fmt) if "balance" in col_map else None

        direction = None
        amount_cents = 0
        explicit_dir = False

        if in_val and abs(in_val) > 0 and (not out_val or abs(out_val) == 0):
            direction = "IN"
            amount_cents = abs(in_val)
            explicit_dir = True
        elif out_val and abs(out_val) > 0 and (not in_val or abs(in_val) == 0):
            direction = "OUT"
            amount_cents = abs(out_val)
            explicit_dir = True
        elif amt_val and abs(amt_val) > 0:
            if amt_val < 0:
                direction = "OUT"
                amount_cents = abs(amt_val)
                explicit_dir = True
            else:
                # Check type column (ND/DB vs NC/CR) or sign prefix '+'
                raw_amt = get_col("amount")
                tu = type_str.upper()
                if "+" in raw_amt or tu in ("NC", "CR", "C", "AB", "DEP", "ING", "DEBE", "D" if source != "BANCO" else "__"):
                    direction = "IN"
                    explicit_dir = True
                elif tu in ("ND", "DB", "DR", "CG", "CH", "RET", "EGR", "HABER", "D" if source == "BANCO" else "__"):
                    direction = "OUT"
                    explicit_dir = True
                else:
                    direction = infer_direction_from_text(desc, type_str, source)
                amount_cents = abs(amt_val)
        else:
            continue

        if amount_cents == 0:
            continue

        signed = amount_cents if direction == "IN" else -amount_cents
        clean_desc = desc or type_str or "Movimiento sin descripción"
        rows_out.append(
            {
                "tx_date": tx_date,
                "reference": ref,
                "ref_tail": extract_ref_tail(ref),
                "description": clean_desc,
                "category": auto_categorize(clean_desc, direction, source),
                "direction": direction,
                "amount_cents": amount_cents,
                "signed_cents": signed,
                "running_balance_cents": bal_val,
                "_explicit_dir": explicit_dir,
            }
        )

    return rows_out, open_cents, close_cents


def _parse_pdf_by_word_positions(
    pdf, source: str, num_fmt: str, default_year: Optional[int]
) -> List[dict]:
    """
    Coordinate-aware parser for borderless PDF statements and legacy dot-matrix reports
    (e.g., Banco del Tesoro plain-layout PDFs, SIGESP, Profit Plus, Saint, Galac).
    Detects column X-centers from header words and assigns each row's numbers accurately.
    """
    rows_out: List[dict] = []
    col_x: Dict[str, float] = {}

    for page in pdf.pages:
        words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
        if not words:
            continue

        # Group words into horizontal lines by top coordinate (tolerance 3.5pt)
        words_sorted = sorted(words, key=lambda w: (round(w["top"] / 3.5), w["x0"]))
        lines: List[List[dict]] = []
        cur_line: List[dict] = []
        cur_top: Optional[float] = None
        for w in words_sorted:
            if cur_top is None or abs(w["top"] - cur_top) <= 4.0:
                cur_line.append(w)
                if cur_top is None:
                    cur_top = w["top"]
            else:
                lines.append(sorted(cur_line, key=lambda x: x["x0"]))
                cur_line = [w]
                cur_top = w["top"]
        if cur_line:
            lines.append(sorted(cur_line, key=lambda x: x["x0"]))

        for line in lines:
            line_text = " ".join(w["text"] for w in line).strip()
            if not line_text:
                continue

            # Detect header line and record x-centers of in/out/balance/amount columns
            temp_x: Dict[str, float] = {}
            has_date_hdr = False
            for w in line:
                role = classify_header_cell(w["text"], source)
                x_mid = (w["x0"] + w["x1"]) / 2.0
                if role == "date":
                    has_date_hdr = True
                elif role in ("in", "out", "balance", "amount", "ref"):
                    temp_x[role] = x_mid
            if has_date_hdr and any(k in temp_x for k in ("in", "out", "amount", "balance")):
                col_x = temp_x
                continue

            if OPEN_BAL_RE.search(line_text) or CLOSE_BAL_RE.search(line_text) or SKIP_ROW_PATTERNS.search(line_text):
                continue

            # Check if line begins with a date
            tx_date = parse_date_str(line[0]["text"], default_year)
            date_word_count = 1
            if not tx_date and len(line) >= 3:
                tx_date = parse_date_str(f"{line[0]['text']} {line[1]['text']} {line[2]['text']}", default_year)
                date_word_count = 3
            if not tx_date:
                continue

            rest = line[date_word_count:]
            if not rest:
                continue

            # Separate numeric amount words (typically right-side) from text/reference words
            num_words = []
            text_words = []
            for w in rest:
                if AMOUNT_TOKEN_RE.fullmatch(w["text"].strip()):
                    val = parse_amount_to_cents(w["text"], num_fmt)
                    if val is not None:
                        num_words.append((w, val))
                        continue
                text_words.append(w)

            if not num_words:
                continue

            in_val = None
            out_val = None
            amt_val = None
            bal_val = None
            explicit_dir = False

            numeric_roles = {k: v for k, v in col_x.items() if k in ("in", "out", "amount", "balance")}
            if numeric_roles:
                for w, val in num_words:
                    x_mid = (w["x0"] + w["x1"]) / 2.0
                    # Match closest numeric column header (also check right-edge alignment x1)
                    best_role = min(numeric_roles.keys(), key=lambda r: abs(numeric_roles[r] - x_mid))
                    if best_role == "balance":
                        bal_val = val
                    elif best_role == "in":
                        in_val = (in_val or 0) + abs(val)
                    elif best_role == "out":
                        out_val = (out_val or 0) + abs(val)
                    elif best_role == "amount":
                        amt_val = val
            else:
                # No column header coordinates: last is balance if >=2 numbers, preceding is amount
                if len(num_words) >= 3:
                    # Often Debe, Haber, Saldo (with one of Debe/Haber = 0,00)
                    v1, v2, v3 = num_words[-3][1], num_words[-2][1], num_words[-1][1]
                    bal_val = v3
                    if source == "BANCO":
                        out_val, in_val = abs(v1), abs(v2)
                    else:
                        in_val, out_val = abs(v1), abs(v2)
                elif len(num_words) == 2:
                    amt_val = num_words[0][1]
                    bal_val = num_words[1][1]
                else:
                    amt_val = num_words[0][1]

            # Extract reference and description from text_words
            ref = ""
            desc_parts = []
            for i, w in enumerate(text_words):
                tok = w["text"].strip()
                # Skip a second date (fecha valor) right after main date
                if i == 0 and parse_date_str(tok, default_year):
                    continue
                if not ref and i <= 1 and re.search(r"\d{3,}", tok):
                    ref = tok
                else:
                    desc_parts.append(tok)
            desc = " ".join(desc_parts).strip() or "Movimiento"

            direction = None
            amount_cents = 0
            if in_val and in_val > 0 and (not out_val or out_val == 0):
                direction = "IN"
                amount_cents = abs(in_val)
                explicit_dir = True
            elif out_val and out_val > 0 and (not in_val or in_val == 0):
                direction = "OUT"
                amount_cents = abs(out_val)
                explicit_dir = True
            elif amt_val and abs(amt_val) > 0:
                if amt_val < 0:
                    direction = "OUT"
                    explicit_dir = True
                else:
                    direction = infer_direction_from_text(desc, "", source)
                amount_cents = abs(amt_val)
            else:
                continue

            rows_out.append(
                {
                    "tx_date": tx_date,
                    "reference": ref,
                    "ref_tail": extract_ref_tail(ref),
                    "description": desc,
                    "category": auto_categorize(desc, direction, source),
                    "direction": direction,
                    "amount_cents": amount_cents,
                    "signed_cents": amount_cents if direction == "IN" else -amount_cents,
                    "running_balance_cents": bal_val,
                    "_explicit_dir": explicit_dir,
                }
            )

    return rows_out


def parse_free_text(
    text: str,
    source: str,
    number_format: str = "auto",
    default_year: Optional[int] = None,
) -> dict:
    """
    Parse raw lines of text (either fallback from PDF or pasted directly by user from a legacy PDF).
    Also supports tab/semicolon/pipe-separated pasted rows!
    """
    warnings: List[str] = []
    num_fmt = detect_number_format(text) if number_format == "auto" else number_format
    open_cents, close_cents, acct_num = _extract_balances_from_text(text, num_fmt)

    # Check if tab or semicolon separated table with headers
    sample_lines = [ln for ln in text.splitlines() if ln.strip()][:10]
    for delim in ("\t", ";", "|"):
        if sample_lines and sum(1 for ln in sample_lines if ln.count(delim) >= 3) >= 2:
            reader = list(csv.reader(io.StringIO(text), delimiter=delim))
            t_rows, t_open, t_close = _parse_table_rows(reader, source, num_fmt, default_year)
            if t_rows:
                open_c = t_open if t_open is not None else open_cents
                t_rows, open_c, inf_close, corr = reconcile_directions_with_balances(t_rows, open_c)
                close_c = t_close if t_close is not None else (close_cents if close_cents is not None else inf_close)
                if corr:
                    warnings.append(f"Se verificaron y ajustaron {corr} signos (Debe/Haber) usando la columna de Saldo.")
                return _finalize_parse_result(t_rows, open_c, close_c, acct_num, num_fmt, text, warnings, "tabla_delimitada")

    rows_out: List[dict] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or OPEN_BAL_RE.search(line) or CLOSE_BAL_RE.search(line) or SKIP_ROW_PATTERNS.search(line):
            continue
        m_date = DATE_START_RE.match(line)
        if not m_date:
            continue
        tx_date = parse_date_str(m_date.group(1), default_year)
        if not tx_date:
            continue

        remainder = line[m_date.end() :].strip()
        # Strip optional second date (fecha valor)
        m_d2 = DATE_START_RE.match(remainder)
        if m_d2 and parse_date_str(m_d2.group(1), default_year):
            remainder = remainder[m_d2.end() :].strip()

        amt_matches = list(AMOUNT_TOKEN_RE.finditer(remainder))
        if not amt_matches:
            continue

        # Text before the first monetary amount contains reference + description
        prefix = remainder[: amt_matches[0].start()].strip()
        vals = [(m.group(1), parse_amount_to_cents(m.group(1), num_fmt)) for m in amt_matches]
        vals = [(raw_s, v) for raw_s, v in vals if v is not None]
        if not vals:
            continue

        ref = ""
        desc = prefix
        tokens = prefix.split()
        if tokens and re.search(r"\d{3,}", tokens[0]):
            ref = tokens[0]
            desc = " ".join(tokens[1:]).strip()
        elif len(tokens) >= 3 and tokens[1] == "/" and re.search(r"\d{3,}", tokens[2]):
            ref = f"{tokens[0]} / {tokens[2]}"
            desc = " ".join(tokens[3:]).strip()

        desc = desc or "Movimiento"
        bal_val = None
        direction = "OUT"
        amount_cents = 0
        explicit_dir = False

        if len(vals) >= 3:
            # Debe/Cargo, Haber/Abono, Saldo
            v1, v2, v3 = vals[-3][1], vals[-2][1], vals[-1][1]
            bal_val = v3
            if source == "BANCO":
                # Débito (OUT), Crédito (IN), Saldo
                if abs(v1) > 0 and abs(v2) == 0:
                    direction, amount_cents, explicit_dir = "OUT", abs(v1), True
                elif abs(v2) > 0 and abs(v1) == 0:
                    direction, amount_cents, explicit_dir = "IN", abs(v2), True
                else:
                    amount_cents = abs(v1) or abs(v2)
                    direction = "OUT" if abs(v1) > 0 else "IN"
            else:
                # Debe (IN), Haber (OUT), Saldo
                if abs(v1) > 0 and abs(v2) == 0:
                    direction, amount_cents, explicit_dir = "IN", abs(v1), True
                elif abs(v2) > 0 and abs(v1) == 0:
                    direction, amount_cents, explicit_dir = "OUT", abs(v2), True
                else:
                    amount_cents = abs(v1) or abs(v2)
                    direction = "IN" if abs(v1) > 0 else "OUT"
        elif len(vals) == 2:
            raw_a, v1 = vals[0]
            _, v2 = vals[1]
            bal_val = v2
            amount_cents = abs(v1)
            if v1 < 0 or raw_a.strip().startswith("-") or "DB" in raw_a.upper() or "ND" in raw_a.upper():
                direction, explicit_dir = "OUT", True
            elif raw_a.strip().startswith("+") or "CR" in raw_a.upper() or "NC" in raw_a.upper():
                direction, explicit_dir = "IN", True
            else:
                direction = infer_direction_from_text(desc, "", source)
        else:
            raw_a, v1 = vals[0]
            amount_cents = abs(v1)
            if v1 < 0 or raw_a.strip().startswith("-") or "DB" in raw_a.upper() or "ND" in raw_a.upper():
                direction, explicit_dir = "OUT", True
            elif raw_a.strip().startswith("+") or "CR" in raw_a.upper() or "NC" in raw_a.upper():
                direction, explicit_dir = "IN", True
            else:
                direction = infer_direction_from_text(desc, "", source)

        if amount_cents <= 0:
            continue

        rows_out.append(
            {
                "tx_date": tx_date,
                "reference": ref,
                "ref_tail": extract_ref_tail(ref),
                "description": desc,
                "category": auto_categorize(desc, direction, source),
                "direction": direction,
                "amount_cents": amount_cents,
                "signed_cents": amount_cents if direction == "IN" else -amount_cents,
                "running_balance_cents": bal_val,
                "_explicit_dir": explicit_dir,
            }
        )

    rows_out, open_cents, inf_close, corr = reconcile_directions_with_balances(rows_out, open_cents)
    if close_cents is None:
        close_cents = inf_close
    if corr:
        warnings.append(f"Se verificaron {corr} movimientos automáticamente comparando contra el Saldo acumulado.")
    if not rows_out:
        warnings.append("No se detectaron filas con formato Fecha + Descripción + Monto. Revisa el texto o ajusta el formato numérico.")

    return _finalize_parse_result(rows_out, open_cents, close_cents, acct_num, num_fmt, text, warnings, "lineas_texto")


def parse_uploaded_file(
    file_bytes: bytes,
    filename: str,
    source: str,
    number_format: str = "auto",
    default_year: Optional[int] = None,
) -> dict:
    """
    Unified file parser for PDF (primary), XLSX, and CSV/TXT.
    Returns structured preview payload for user verification before saving.
    """
    ext = (filename.rsplit(".", 1)[-1].lower() if "." in filename else "").strip()
    warnings: List[str] = []

    if ext == "pdf":
        full_text_pages: List[str] = []
        table_rows: List[dict] = []
        coord_rows: List[dict] = []
        t_open: Optional[int] = None
        t_close: Optional[int] = None

        try:
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                page_count = len(pdf.pages)
                for p in pdf.pages:
                    txt = p.extract_text(layout=False) or ""
                    full_text_pages.append(txt)
                full_text = "\n".join(full_text_pages)
                num_fmt = detect_number_format(full_text) if number_format == "auto" else number_format
                open_cents, close_cents, acct_num = _extract_balances_from_text(full_text, num_fmt)

                # Strategy 1: Structured PDF tables
                all_tables_combined: List[List[Optional[str]]] = []
                for p in pdf.pages:
                    tables = p.extract_tables() or []
                    for tb in tables:
                        pr, po, pc = _parse_table_rows(tb, source, num_fmt, default_year)
                        if pr:
                            table_rows.extend(pr)
                            if t_open is None and po is not None:
                                t_open = po
                            if pc is not None:
                                t_close = pc
                        else:
                            all_tables_combined.extend(tb)

                if not table_rows and all_tables_combined:
                    pr, po, pc = _parse_table_rows(all_tables_combined, source, num_fmt, default_year)
                    table_rows.extend(pr)
                    if t_open is None and po is not None:
                        t_open = po
                    if pc is not None:
                        t_close = pc

                # Strategy 2: Coordinate-aware word position extraction
                coord_rows = _parse_pdf_by_word_positions(pdf, source, num_fmt, default_year)
        except Exception as exc:
            # Fallback to pypdf plain text extraction
            reader = PdfReader(io.BytesIO(file_bytes))
            page_count = len(reader.pages)
            full_text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
            warnings.append(f"Se usó extractor alternativo de PDF ({type(exc).__name__}). Verifica los montos en la vista previa.")
            res = parse_free_text(full_text, source, number_format, default_year)
            res["page_count"] = page_count
            res["warnings"] = warnings + res.get("warnings", [])
            return res

        if not full_text.strip():
            return {
                "rows": [],
                "count": 0,
                "total_in_cents": 0,
                "total_out_cents": 0,
                "net_cents": 0,
                "opening_balance_cents": None,
                "closing_balance_cents": None,
                "detected_account": None,
                "detected_format": "ve",
                "method": "pdf_escaneado",
                "page_count": page_count,
                "raw_text_preview": "",
                "warnings": [
                    "Este PDF parece ser una imagen escaneada sin texto seleccionable. "
                    "Descarga el PDF digital directo del portal del Banco del Tesoro / sistema, "
                    "o usa la carga por texto/manual."
                ],
            }

        # Pick best strategy (prefer structured tables if solid, else coordinate-aware, else regex line parser)
        chosen_method = "tabla_pdf"
        chosen_rows = table_rows
        if len(coord_rows) > len(table_rows) * 1.15:
            chosen_rows = coord_rows
            chosen_method = "coordenadas_pdf"

        if not chosen_rows:
            line_res = parse_free_text(full_text, source, num_fmt, default_year)
            line_res["page_count"] = page_count
            return line_res

        final_open = t_open if t_open is not None else open_cents
        chosen_rows, final_open, inf_close, corr = reconcile_directions_with_balances(chosen_rows, final_open)
        final_close = t_close if t_close is not None else (close_cents if close_cents is not None else inf_close)
        if corr:
            warnings.append(f"Se validaron y ajustaron {corr} signos comparando contra la columna de Saldo del PDF.")

        # Mathematical self-check of extracted PDF: Opening + Net == Closing?
        if final_open is not None and final_close is not None and chosen_rows:
            net = sum(r["signed_cents"] for r in chosen_rows)
            expected_close = final_open + net
            diff = final_close - expected_close
            if diff != 0:
                warnings.append(
                    f"Atención: Saldo Inicial ({cents_to_ve(final_open)}) + Movimientos netos ({cents_to_ve(net, show_plus=True)}) "
                    f"= {cents_to_ve(expected_close)}, pero el Saldo Final detectado en el PDF es {cents_to_ve(final_close)} "
                    f"(diferencia de Bs. {cents_to_ve(diff, show_plus=True)} — puede ser un fallo de suma del propio sistema o una fila omitida)."
                )

        out = _finalize_parse_result(
            chosen_rows, final_open, final_close, acct_num, num_fmt, full_text, warnings, chosen_method
        )
        out["page_count"] = page_count
        return out

    elif ext in ("xlsx", "xlsm"):
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
        ws = wb.active
        grid = []
        raw_lines = []
        for r in ws.iter_rows(values_only=True):
            str_row = []
            for cell in r:
                if isinstance(cell, datetime):
                    str_row.append(cell.strftime("%Y-%m-%d"))
                elif isinstance(cell, (int, float)):
                    str_row.append(f"{cell:.2f}")
                elif cell is None:
                    str_row.append("")
                else:
                    str_row.append(str(cell).strip())
            grid.append(str_row)
            if any(str_row):
                raw_lines.append(" | ".join(c for c in str_row if c))
        raw_text = "\n".join(raw_lines)
        num_fmt = "us" if number_format == "auto" else number_format
        rows_out, open_c, close_c = _parse_table_rows(grid, source, num_fmt, default_year)
        rows_out, open_c, inf_close, corr = reconcile_directions_with_balances(rows_out, open_c)
        if close_c is None:
            close_c = inf_close
        return _finalize_parse_result(rows_out, open_c, close_c, None, num_fmt, raw_text, warnings, "excel_xlsx")

    elif ext in ("csv", "txt", "tsv"):
        try:
            text = file_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = file_bytes.decode("latin-1", errors="replace")
        num_fmt = detect_number_format(text) if number_format == "auto" else number_format
        # Detect delimiter
        first_chunk = text[:4096]
        delim = ";" if first_chunk.count(";") > first_chunk.count(",") else ","
        if first_chunk.count("\t") > max(first_chunk.count(";"), first_chunk.count(",")):
            delim = "\t"
        grid = list(csv.reader(io.StringIO(text), delimiter=delim))
        rows_out, open_c, close_c = _parse_table_rows(grid, source, num_fmt, default_year)
        if rows_out:
            rows_out, open_c, inf_close, _ = reconcile_directions_with_balances(rows_out, open_c)
            if close_c is None:
                close_c = inf_close
            return _finalize_parse_result(rows_out, open_c, close_c, None, num_fmt, text, warnings, "csv")
        return parse_free_text(text, source, number_format, default_year)

    else:
        raise ValueError(f"Formato de archivo '.{ext}' no soportado. Sube un archivo PDF, Excel (.xlsx) o CSV/TXT.")


def _finalize_parse_result(
    rows: List[dict],
    open_cents: Optional[int],
    close_cents: Optional[int],
    acct_num: Optional[str],
    num_fmt: str,
    raw_text: str,
    warnings: List[str],
    method: str,
) -> dict:
    cleaned = []
    total_in = 0
    total_out = 0
    for r in rows:
        item = {k: v for k, v in r.items() if not k.startswith("_")}
        if item["direction"] == "IN":
            total_in += item["amount_cents"]
        else:
            total_out += item["amount_cents"]
        cleaned.append(item)

    return {
        "rows": cleaned,
        "count": len(cleaned),
        "total_in_cents": total_in,
        "total_out_cents": total_out,
        "net_cents": total_in - total_out,
        "opening_balance_cents": open_cents,
        "closing_balance_cents": close_cents,
        "detected_account": acct_num,
        "detected_format": num_fmt,
        "method": method,
        "raw_text_preview": raw_text[:12000],
        "warnings": warnings,
    }
