import re
import sqlite3
from datetime import date, datetime
from typing import Dict, List, Optional, Set, Tuple

from .database import cents_to_ve, get_settings

STOPWORDS_ES = {
    "DE", "LA", "EL", "LOS", "LAS", "DEL", "AL", "POR", "PARA", "CON", "SIN",
    "EN", "UN", "UNA", "QUE", "SE", "SU", "CA", "SA", "BT", "ND", "NC", "MES",
    "PAGO", "COBRO", "TRANSF", "TRANSFERENCIA", "RECIBIDA", "EMITIDA", "DIRECTO",
    "ABONO", "CARGO", "BANCO", "TESORO", "OCTUBRE", "NOVIEMBRE", "DICIEMBRE",
    "ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO", "JULIO", "AGOSTO",
    "SEPTIEMBRE", "OCT", "NOV", "DIC", "ENE", "FEB", "MAR", "ABR", "MAY", "JUN",
    "JUL", "AGO", "SEP", "2025", "2026", "2027",
}


def _parse_iso(d: str) -> date:
    try:
        return datetime.strptime(d[:10], "%Y-%m-%d").date()
    except Exception:
        return date(2026, 1, 1)


def _days_between(d1: str, d2: str) -> int:
    return abs((_parse_iso(d1) - _parse_iso(d2)).days)


def _extract_doc_codes(ref: str, desc: str) -> Set[str]:
    """Extract voucher / payment order codes like OP-312, REC-891, FACT-1084."""
    combined = f"{ref or ''} {desc or ''}".upper()
    codes = set()
    for m in re.finditer(r"\b(OP|REC|FAC|FACT|CHQ|CHE|ORD|NRO|DOC)[\-\s#.]?0*(\d{2,})\b", combined):
        prefix = "FAC" if m.group(1) == "FACT" else m.group(1)
        codes.add(f"{prefix}-{m.group(2)}")
    return codes


def _extract_num_tokens(ref: str) -> List[str]:
    runs = re.findall(r"\d{4,}", str(ref or ""))
    return [r.lstrip("0") or "0" for r in runs]


def ref_similarity(m1: dict, m2: dict) -> int:
    """Return 0..100 reference similarity score (supports Banco del Tesoro long refs vs partial tails)."""
    r1 = re.sub(r"[^A-Z0-9]", "", (m1.get("reference") or "").upper())
    r2 = re.sub(r"[^A-Z0-9]", "", (m2.get("reference") or "").upper())
    if r1 and r2 and len(r1) >= 4 and r1 == r2:
        return 100

    nums1 = _extract_num_tokens(m1.get("reference", ""))
    nums2 = _extract_num_tokens(m2.get("reference", ""))
    for n1 in nums1:
        for n2 in nums2:
            if n1 == n2 and len(n1) >= 4:
                return 96
            short, long_ = (n1, n2) if len(n1) <= len(n2) else (n2, n1)
            if len(short) >= 4 and long_.endswith(short):
                return 94

    t1 = m1.get("ref_tail") or ""
    t2 = m2.get("ref_tail") or ""
    if len(t1) >= 4 and len(t2) >= 4:
        if t1 == t2:
            return 92
        short, long_ = (t1, t2) if len(t1) <= len(t2) else (t2, t1)
        if len(short) >= 4 and long_.endswith(short):
            return 90

    codes1 = _extract_doc_codes(m1.get("reference", ""), m1.get("description", ""))
    codes2 = _extract_doc_codes(m2.get("reference", ""), m2.get("description", ""))
    if codes1 and codes2 and (codes1 & codes2):
        return 88

    return 0


def desc_similarity(d1: str, d2: str) -> float:
    """Jaccard similarity over meaningful description tokens (0.0 .. 1.0)."""
    def tokenize(s: str) -> Set[str]:
        s_up = (
            (s or "")
            .upper()
            .replace("Á", "A")
            .replace("É", "E")
            .replace("Í", "I")
            .replace("Ó", "O")
            .replace("Ú", "U")
        )
        words = re.findall(r"[A-Z0-9]{3,}", s_up)
        return {w for w in words if w not in STOPWORDS_ES and not w.isdigit()}

    w1, w2 = tokenize(d1), tokenize(d2)
    if not w1 or not w2:
        return 0.0
    inter = w1 & w2
    union = w1 | w2
    return len(inter) / len(union)


def classify_discrepancy(a_cents: int, b_cents: int, cents_tolerance: int = 100) -> Tuple[str, str]:
    """
    Classify exact accounting nature of a difference between two positive amounts (in cents):
      - CENTIMOS: rounding/céntimos (e.g. 48.750,80 vs 48.750,40 -> Bs. 0,40)
      - ERROR_DIGITO: single-digit error / legacy glitch (e.g. 201.300,00 vs 200.300,00 -> Bs. 1.000,00)
      - TRANSPOSICION: swapped digits (e.g. 154.000,00 vs 145.000,00 -> divisible by 9, same digits)
      - COMA_DECIMAL: factor of 10 or 100
      - DIFERENCIA_MONTO: other amount mismatch
    """
    aa, bb = abs(a_cents), abs(b_cents)
    diff = abs(aa - bb)
    if diff == 0:
        return ("", "Sin diferencia")
    if diff <= cents_tolerance:
        return ("CENTIMOS", f"Diferencia de céntimos / redondeo (Bs. {cents_to_ve(diff)})")

    sa, sb = str(aa), str(bb)
    if aa == bb * 10 or bb == aa * 10 or aa == bb * 100 or bb == aa * 100:
        return ("COMA_DECIMAL", f"Desplazamiento de coma o cero de más (Bs. {cents_to_ve(diff)})")

    # Single digit substitution (e.g. 20130000 vs 20030000 -> differs at exactly 1 position!)
    if len(sa) == len(sb):
        diffs_pos = sum(1 for x, y in zip(sa, sb) if x != y)
        if diffs_pos == 1:
            return ("ERROR_DIGITO", f"Error en un solo dígito / posible falla de sistema (Bs. {cents_to_ve(diff)})")
        if sorted(sa) == sorted(sb) and diff % 9 == 0:
            return ("TRANSPOSICION", f"Transposición de cifras invertidas (Bs. {cents_to_ve(diff)})")

    # Power-of-10 difference even across carry boundary (e.g. 200.300 vs 199.300 = 1.000,00)
    if re.fullmatch(r"[1-9]0{4,}", str(diff)):
        return ("ERROR_DIGITO", f"Diferencia exacta en cifra redonda / posible falla de sistema (Bs. {cents_to_ve(diff)})")

    return ("DIFERENCIA_MONTO", f"Diferencia de monto (Bs. {cents_to_ve(diff)})")


def _find_ref_discrepancy_partner(
    primary: dict,
    secondary: Optional[dict],
    pool: List[dict],
    used: Set[int],
    date_window: int,
) -> Optional[dict]:
    """Find a movement in pool that clearly represents the SAME transaction (by ref/OP or strong desc+date) with a different amount."""
    best = None
    best_score = 0
    for cand in pool:
        if cand["id"] in used:
            continue
        if cand["direction"] != primary["direction"]:
            continue
        days = _days_between(primary["tx_date"], cand["tx_date"])
        if days > date_window + 7:
            continue
        rsim1 = ref_similarity(primary, cand)
        rsim2 = ref_similarity(secondary, cand) if secondary else 0
        rsim = max(rsim1, rsim2)
        dsim1 = desc_similarity(primary["description"], cand["description"])
        dsim2 = desc_similarity(secondary["description"], cand["description"]) if secondary else 0.0
        dsim = max(dsim1, dsim2)

        score = 0
        if rsim >= 88:
            score = rsim + int(dsim * 20) - days
        elif days <= 2 and dsim >= 0.6:
            score = 75 + int(dsim * 25) - days * 2

        if score > best_score and score >= 80:
            best_score = score
            best = cand
    return best


def _upsert_group_system_issue(
    conn,
    period_id: int,
    group_id: int,
    b_row: Optional[dict],
    l_row: Optional[dict],
    m_row: Optional[dict],
    explanation: str,
):
    """Automatically log a structured Falla del Sistema entry when Libros and Mayor disagree due to digit/system error."""
    existing = conn.execute(
        "SELECT id FROM system_issues WHERE period_id = ? AND match_group_id = ?",
        (period_id, group_id),
    ).fetchone()

    canon = (
        b_row["signed_cents"]
        if b_row
        else (m_row["signed_cents"] if m_row else (l_row["signed_cents"] if l_row else 0))
    )
    b_c = b_row["signed_cents"] if b_row else None
    l_c = l_row["signed_cents"] if l_row else None
    m_c = m_row["signed_cents"] if m_row else None
    adj_l = (canon - l_c) if l_c is not None else 0
    adj_m = (canon - m_c) if m_c is not None else 0

    if adj_l != 0 and adj_m != 0:
        mod = "AMBOS"
    elif adj_m != 0:
        mod = "MAYOR"
    else:
        mod = "LIBROS"

    anchor = l_row or m_row or b_row
    codes = list(_extract_doc_codes(anchor.get("reference", ""), anchor.get("description", "")))
    code = codes[0] if codes else (anchor.get("reference") or f"DIF-{group_id}")
    title = anchor.get("description") or "Diferencia entre Según Libros y Mayor Analítico"
    audit_note = (
        f"{explanation}. "
        + (f"Según Libros registra Bs. {cents_to_ve(abs(l_c))} " if l_c is not None else "")
        + (f"y Mayor Analítico Bs. {cents_to_ve(abs(m_c))}" if m_c is not None else "")
        + (f" (Estado de Cuenta Banco del Tesoro: Bs. {cents_to_ve(abs(b_c))})." if b_c is not None else ".")
        + " Partida documentada para control de conciliación y seguimiento con soporte de sistemas/contabilidad."
    )

    if existing:
        conn.execute(
            """
            UPDATE system_issues
            SET banco_cents = ?, libros_cents = ?, mayor_cents = ?,
                adj_libros_cents = ?, adj_mayor_cents = ?, affected_module = ?, audit_note = ?
            WHERE id = ?
            """,
            (b_c, l_c, m_c, adj_l, adj_m, mod, audit_note, existing["id"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO system_issues
                (period_id, match_group_id, code, issue_date, title, affected_module,
                 banco_cents, libros_cents, mayor_cents, adj_libros_cents, adj_mayor_cents,
                 affects_opening, carry_forward, status, audit_note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, 'ACTIVA', ?)
            """,
            (period_id, group_id, code, anchor["tx_date"], title, mod, b_c, l_c, m_c, adj_l, adj_m, audit_note),
        )


def run_auto_reconciliation(conn, period_id: int, reset_existing: bool = False) -> dict:
    """
    Two-phase reconciliation following the company's formal procedure:

      FASE 1: Según Libros (Tesorería)  <->  Mayor Analítico (Contabilidad)
              -> finds exact internal differences: missing postings, amount
                 differences (céntimos, digit errors, transpositions),
                 duplicates and system failures.

      FASE 2: Mayor Analítico (Contabilidad)  <->  Banco del Tesoro (Estado de Cuenta)
              -> once Libros and Mayor agree, finds bank differences: transit
                 items, unbooked bank charges (comisiones, IGTF), NC/ND,
                 amount differences and bank errors.

      FASE 2b: leftover Banco <-> Libros pairs (both missing in Mayor).
    """
    settings = get_settings(conn)
    date_window = max(0, int(settings.get("date_window_days", "5") or 5))
    cents_tol = max(1, int(settings.get("cents_tolerance", "100") or 100))

    if reset_existing:
        # Preserve manual system issues without group; clear auto groups & reset movement statuses
        conn.execute(
            """
            UPDATE movements
            SET status = 'PENDIENTE', match_group_id = NULL
            WHERE period_id = ? AND status NOT IN ('FALLA_SISTEMA')
            """,
            (period_id,),
        )
        conn.execute("DELETE FROM system_issues WHERE period_id = ? AND match_group_id IS NOT NULL", (period_id,))
        conn.execute("DELETE FROM match_groups WHERE period_id = ?", (period_id,))

    rows = [
        dict(r)
        for r in conn.execute(
            """
            SELECT * FROM movements
            WHERE period_id = ? AND status = 'PENDIENTE' AND match_group_id IS NULL
            ORDER BY tx_date ASC, id ASC
            """,
            (period_id,),
        ).fetchall()
    ]

    used: Set[int] = set()
    group_of: Dict[int, int] = {}
    group_sources: Dict[int, Set[str]] = {}
    stats = {
        "exact_triple": 0,
        "exact_double": 0,
        "with_discrepancy": 0,
        "reversals": 0,
        "bank_charges_classified": 0,
        "suspected_duplicates": 0,
        "fase1_exact": 0,
        "fase1_diffs": 0,
        "fase2_exact": 0,
        "fase2_diffs": 0,
    }

    def create_group(
        members: List[dict],
        match_type: str,
        canonical_source: str,
        resolution_type: str = "NINGUNA",
        discrepancy_kind: str = "",
        confidence: int = 100,
        note: str = "",
        mov_status: str = "CONCILIADO",
    ) -> int:
        by_src: Dict[str, int] = {}
        for m in members:
            by_src[m["source"]] = by_src.get(m["source"], 0) + m["signed_cents"]
        canon_cents = by_src.get(canonical_source, 0)
        cur = conn.execute(
            """
            INSERT INTO match_groups
                (period_id, match_type, canonical_source, canonical_cents, resolution_type, discrepancy_kind, confidence, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (period_id, match_type, canonical_source, canon_cents, resolution_type, discrepancy_kind, confidence, note),
        )
        gid = cur.lastrowid
        group_sources[gid] = {m["source"] for m in members}
        for m in members:
            used.add(m["id"])
            group_of[m["id"]] = gid
            conn.execute(
                "UPDATE movements SET status = ?, match_group_id = ? WHERE id = ?",
                (mov_status, gid, m["id"]),
            )
        return gid

    def add_to_group(gid: int, movement: dict, new_canonical_source: str, new_match_type: str, mov_status: str = "CONCILIADO"):
        """Add a movement (e.g. Banco) to an existing group (e.g. Fase 1 Libros+Mayor) and refresh canonical data."""
        used.add(movement["id"])
        group_of[movement["id"]] = gid
        group_sources.setdefault(gid, set()).add(movement["source"])
        conn.execute(
            "UPDATE movements SET status = ?, match_group_id = ? WHERE id = ?",
            (mov_status, gid, movement["id"]),
        )
        mems = [
            dict(r)
            for r in conn.execute("SELECT * FROM movements WHERE match_group_id = ?", (gid,)).fetchall()
        ]
        by_src: Dict[str, int] = {}
        for m in mems:
            by_src[m["source"]] = by_src.get(m["source"], 0) + m["signed_cents"]
        canon_cents = by_src.get(new_canonical_source, 0)
        conn.execute(
            "UPDATE match_groups SET canonical_source = ?, canonical_cents = ?, match_type = ? WHERE id = ?",
            (new_canonical_source, canon_cents, new_match_type, gid),
        )
        # Keep any linked system-issue record in sync with the bank amount
        conn.execute(
            "UPDATE system_issues SET banco_cents = ? WHERE match_group_id = ?",
            (by_src.get("BANCO"), gid),
        )

    # ------------------------------------------------------------------
    # Step 0: Detect internal REVERSOS / ANULACIONES within the same source
    # ------------------------------------------------------------------
    rev_kw = re.compile(r"(?i)\b(REVERSO|ANULAC|RECHAZAD|REINTEGRO|DEVOLUC|EXTORNO|CONTRAASIENTO)\b")
    for src in ("BANCO", "LIBROS", "MAYOR"):
        src_rows = [r for r in rows if r["source"] == src and r["id"] not in used]
        outs = [r for r in src_rows if r["direction"] == "OUT"]
        ins = [r for r in src_rows if r["direction"] == "IN"]
        for o in outs:
            if o["id"] in used:
                continue
            candidates = []
            for i in ins:
                if i["id"] in used:
                    continue
                if o["amount_cents"] != i["amount_cents"]:
                    continue
                days = _days_between(o["tx_date"], i["tx_date"])
                if days > 5:
                    continue
                has_rev = bool(rev_kw.search(o["description"]) or rev_kw.search(i["description"]))
                dsim = desc_similarity(o["description"], i["description"])
                rsim = ref_similarity(o, i)
                if has_rev and (dsim >= 0.25 or rsim >= 85 or days <= 2):
                    candidates.append((days, -dsim, i))
            if candidates:
                candidates.sort(key=lambda x: (x[0], x[1]))
                partner = candidates[0][2]
                create_group(
                    [o, partner],
                    match_type="REVERSO",
                    canonical_source=src,
                    confidence=98,
                    note=f"Cargo y abono reversados en {src} (efecto neto Bs. 0,00)",
                    mov_status="REVERSO",
                )
                stats["reversals"] += 1

    # ------------------------------------------------------------------
    # Step 0.5: Match incoming BANCO movements against carried-over
    # in-transit groups from the prior month (Libros+Mayor awaiting bank).
    # ------------------------------------------------------------------
    carry_groups = conn.execute(
        """
        SELECT g.* FROM match_groups g
        WHERE g.period_id = ? AND g.match_type != 'REVERSO'
          AND NOT EXISTS (SELECT 1 FROM movements m WHERE m.match_group_id = g.id AND m.source = 'BANCO')
          AND EXISTS (SELECT 1 FROM movements m WHERE m.match_group_id = g.id AND m.is_carryover = 1)
        """,
        (period_id,),
    ).fetchall()
    for cg in carry_groups:
        cmems = [dict(x) for x in conn.execute("SELECT * FROM movements WHERE match_group_id = ?", (cg["id"],)).fetchall()]
        if not cmems:
            continue
        target_signed = cg["canonical_cents"]
        for b in [r for r in rows if r["source"] == "BANCO" and r["id"] not in used]:
            if b["signed_cents"] != target_signed:
                continue
            rsim = max(ref_similarity(b, cm) for cm in cmems)
            dsim = max(desc_similarity(b["description"], cm["description"]) for cm in cmems)
            if rsim >= 85 or dsim >= 0.3 or _days_between(b["tx_date"], cmems[0]["tx_date"]) <= 15:
                used.add(b["id"])
                group_of[b["id"]] = cg["id"]
                conn.execute(
                    "UPDATE movements SET status = 'CONCILIADO', match_group_id = ? WHERE id = ?",
                    (cg["id"], b["id"]),
                )
                new_type = "EXACTO_TRIPLE" if len({cm["source"] for cm in cmems} | {"BANCO"}) == 3 else "EXACTO_DOBLE"
                conn.execute(
                    "UPDATE match_groups SET match_type = ?, canonical_source = 'BANCO', note = ? WHERE id = ?",
                    (new_type, "Partida en tránsito del mes anterior cobrada/debitada en Banco del Tesoro", cg["id"]),
                )
                stats["exact_triple"] += 1
                stats["fase2_exact"] += 1
                break

    def _fase2_used() -> Set[int]:
        """Used ids that must stay excluded in Fase 2: everything except Mayor rows
        already grouped in Fase 1 whose group has no Banco member yet (triple completion)."""
        return {
            uid
            for uid in used
            if not (group_of.get(uid) and "BANCO" not in group_sources.get(group_of[uid], set()))
        }

    def best_exact_partner(anchor: dict, pool: List[dict], allow_grouped: bool = False) -> Tuple[Optional[dict], int]:
        best = None
        best_score = -1
        excluded = _fase2_used() if allow_grouped else used
        for cand in pool:
            if cand["id"] in excluded:
                continue
            if cand["direction"] != anchor["direction"]:
                continue
            if cand["amount_cents"] != anchor["amount_cents"]:
                continue
            days = _days_between(anchor["tx_date"], cand["tx_date"])
            rsim = ref_similarity(anchor, cand)
            dsim = desc_similarity(anchor["description"], cand["description"])
            # Allow slightly wider date window when reference matches strongly
            max_days = date_window + 7 if rsim >= 88 else date_window
            if days > max_days:
                continue
            score = rsim + int(dsim * 25) - days * 2
            if rsim >= 88:
                score += 40
            elif days <= date_window:
                score += 55
            if score > best_score:
                best_score = score
                best = cand
        return (best, min(100, max(65, best_score))) if best and best_score >= 45 else (None, 0)

    banco_rows = [r for r in rows if r["source"] == "BANCO"]
    libros_rows = [r for r in rows if r["source"] == "LIBROS"]
    mayor_rows = [r for r in rows if r["source"] == "MAYOR"]

    # ==================================================================
    # FASE 1: Según Libros (Tesorería)  <->  Mayor Analítico (Contabilidad)
    # ==================================================================
    for l_row in libros_rows:
        if l_row["id"] in used:
            continue
        m_exact, m_sc = best_exact_partner(l_row, mayor_rows)
        if m_exact:
            create_group(
                [l_row, m_exact],
                match_type="EXACTO_DOBLE",
                canonical_source="MAYOR",
                confidence=m_sc,
                note="Fase 1: conciliado entre Según Libros (Tesorería) y Mayor Analítico (Contabilidad)",
                mov_status="CONCILIADO",
            )
            stats["fase1_exact"] += 1
            stats["exact_double"] += 1
            continue

        m_diff = _find_ref_discrepancy_partner(l_row, None, mayor_rows, used, date_window)
        if m_diff:
            kind, explanation = classify_discrepancy(l_row["amount_cents"], m_diff["amount_cents"], cents_tol)
            res_type = "AJUSTE" if kind == "CENTIMOS" else "FALLA_SISTEMA"
            gid = create_group(
                [l_row, m_diff],
                match_type="CON_DIFERENCIA",
                canonical_source="MAYOR",
                resolution_type=res_type,
                discrepancy_kind=kind,
                confidence=92,
                note=(
                    f"Fase 1: {explanation} — Según Libros: Bs. {cents_to_ve(l_row['amount_cents'])} "
                    f"vs. Mayor Analítico: Bs. {cents_to_ve(m_diff['amount_cents'])}"
                ),
                mov_status="CONCILIADO_DIF",
            )
            if res_type == "FALLA_SISTEMA":
                _upsert_group_system_issue(conn, period_id, gid, None, l_row, m_diff, explanation)
            stats["fase1_diffs"] += 1
            stats["with_discrepancy"] += 1

    # ==================================================================
    # FASE 2: Mayor Analítico (Contabilidad)  <->  Banco del Tesoro
    # ==================================================================
    for b in banco_rows:
        if b["id"] in used:
            continue
        m_exact, m_sc = best_exact_partner(b, mayor_rows, allow_grouped=True)
        if m_exact:
            gid = group_of.get(m_exact["id"])
            if gid:
                # Mayor row already reconciled with Libros in Fase 1 -> complete the triple
                add_to_group(gid, b, new_canonical_source="BANCO", new_match_type="EXACTO_TRIPLE", mov_status="CONCILIADO")
                conn.execute(
                    "UPDATE match_groups SET note = ? WHERE id = ?",
                    ("Fase 1 y 2: conciliado en Según Libros, Mayor Analítico y Banco del Tesoro", gid),
                )
                stats["exact_triple"] += 1
            else:
                create_group(
                    [b, m_exact],
                    match_type="EXACTO_DOBLE",
                    canonical_source="BANCO",
                    confidence=m_sc,
                    note="Fase 2: conciliado entre Mayor Analítico y Banco del Tesoro (falta en Según Libros)",
                    mov_status="CONCILIADO",
                )
                stats["exact_double"] += 1
            stats["fase2_exact"] += 1
            continue

        m_diff = _find_ref_discrepancy_partner(b, None, mayor_rows, _fase2_used(), date_window)
        if m_diff:
            kind, explanation = classify_discrepancy(b["amount_cents"], m_diff["amount_cents"], cents_tol)
            gid = group_of.get(m_diff["id"])
            note = (
                f"Fase 2: {explanation} — Banco del Tesoro: Bs. {cents_to_ve(b['amount_cents'])} "
                f"vs. Mayor Analítico: Bs. {cents_to_ve(m_diff['amount_cents'])}"
            )
            if gid:
                add_to_group(gid, b, new_canonical_source="BANCO", new_match_type="CON_DIFERENCIA", mov_status="CONCILIADO_DIF")
                grow = conn.execute("SELECT resolution_type FROM match_groups WHERE id = ?", (gid,)).fetchone()
                if grow and grow["resolution_type"] != "FALLA_SISTEMA":
                    conn.execute(
                        "UPDATE match_groups SET resolution_type = 'AJUSTE', discrepancy_kind = ?, note = ? WHERE id = ?",
                        (kind, note, gid),
                    )
                else:
                    conn.execute("UPDATE match_groups SET note = ? WHERE id = ?", (note, gid))
            else:
                create_group(
                    [b, m_diff],
                    match_type="CON_DIFERENCIA",
                    canonical_source="BANCO",
                    resolution_type="AJUSTE",
                    discrepancy_kind=kind,
                    confidence=90,
                    note=note,
                    mov_status="CONCILIADO_DIF",
                )
            stats["fase2_diffs"] += 1
            stats["with_discrepancy"] += 1

    # ==================================================================
    # FASE 2b: leftover Banco <-> Libros pairs (both missing in Mayor)
    # ==================================================================
    for b in banco_rows:
        if b["id"] in used:
            continue
        l_exact, l_sc = best_exact_partner(b, libros_rows)
        if l_exact:
            create_group(
                [b, l_exact],
                match_type="EXACTO_DOBLE",
                canonical_source="BANCO",
                confidence=l_sc,
                note="Fase 2: en Banco del Tesoro y Según Libros, pendiente de pase a Mayor Analítico",
                mov_status="CONCILIADO",
            )
            stats["exact_double"] += 1
            continue

        l_diff = _find_ref_discrepancy_partner(b, None, libros_rows, used, date_window)
        if l_diff:
            kind, explanation = classify_discrepancy(b["amount_cents"], l_diff["amount_cents"], cents_tol)
            create_group(
                [b, l_diff],
                match_type="CON_DIFERENCIA",
                canonical_source="BANCO",
                resolution_type="AJUSTE",
                discrepancy_kind=kind,
                confidence=90,
                note=(
                    f"Fase 2: {explanation} — Banco del Tesoro: Bs. {cents_to_ve(b['amount_cents'])} "
                    f"vs. Según Libros: Bs. {cents_to_ve(l_diff['amount_cents'])} (pendiente en Mayor Analítico)"
                ),
                mov_status="CONCILIADO_DIF",
            )
            stats["with_discrepancy"] += 1

    # ------------------------------------------------------------------
    # Auto-classify clear unmatched Bank charges/credits (Comisiones, IGTF, Intereses)
    # ------------------------------------------------------------------
    auto_bank_cats = {"Comisión Bancaria", "IGTF", "Intereses / Rendimientos"}
    for b in banco_rows:
        if b["id"] in used:
            continue
        if b["category"] in auto_bank_cats:
            conn.execute(
                "UPDATE movements SET status = 'AJUSTE_BANCO', note = ? WHERE id = ?",
                (f"{b['category']} en Banco del Tesoro pendiente de registrar en empresa", b["id"]),
            )
            used.add(b["id"])
            stats["bank_charges_classified"] += 1

    # ------------------------------------------------------------------
    # Count suspected duplicates & refresh suggested accounting adjustments
    # ------------------------------------------------------------------
    dups = find_suspected_duplicates(conn, period_id)
    stats["suspected_duplicates"] = len(dups)
    sync_suggested_adjustments(conn, period_id)
    return stats


def find_suspected_duplicates(conn, period_id: int) -> List[dict]:
    """
    Find pending movements that look like duplicates of another movement in the SAME source
    (same direction, exact same amount, date within 5 days, and similar ref or description).
    """
    all_movs = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM movements WHERE period_id = ? AND status != 'REVERSO' ORDER BY tx_date ASC, id ASC",
            (period_id,),
        ).fetchall()
    ]
    alerts: List[dict] = []
    seen_suspects: Set[int] = set()

    for src in ("BANCO", "LIBROS", "MAYOR"):
        src_list = [m for m in all_movs if m["source"] == src]
        for i, m in enumerate(src_list):
            if m["status"] != "PENDIENTE" or m["id"] in seen_suspects:
                continue
            for j, other in enumerate(src_list):
                if i == j or other["status"] == "DUPLICADO":
                    continue
                # Prefer marking the already-reconciled or earlier row as the "original"
                if other["status"] == "PENDIENTE" and j > i:
                    continue
                if m["direction"] != other["direction"] or m["amount_cents"] != other["amount_cents"]:
                    continue
                days = _days_between(m["tx_date"], other["tx_date"])
                if days > 7:
                    continue
                rsim = ref_similarity(m, other)
                dsim = desc_similarity(m["description"], other["description"])
                if rsim >= 85 or dsim >= 0.45 or days <= 2:
                    seen_suspects.add(m["id"])
                    reason_parts = [f"mismo monto (Bs. {cents_to_ve(m['amount_cents'])})"]
                    if rsim >= 85:
                        reason_parts.append(f"referencia similar ({m['reference']} vs. {other['reference']})")
                    if days == 0:
                        reason_parts.append("misma fecha")
                    else:
                        reason_parts.append(f"{days} día(s) de diferencia")
                    alerts.append(
                        {
                            "source": src,
                            "suspect": m,
                            "original": other,
                            "days_apart": days,
                            "reason": "Posible duplicado: " + ", ".join(reason_parts),
                        }
                    )
                    break

    return alerts


def sync_suggested_adjustments(conn, period_id: int):
    """
    Auto-generate clear, ready-to-book accounting & treasury journal entries from current reconciliation findings.
    Preserves manually created adjustments and remembers 'applied' state for unchanged entries.
    """
    period = conn.execute(
        """
        SELECT p.*, a.ledger_code, a.name AS account_name, a.account_number
        FROM periods p JOIN accounts a ON a.id = p.account_id
        WHERE p.id = ?
        """,
        (period_id,),
    ).fetchone()
    if not period:
        return

    settings = get_settings(conn)
    acct_bank = f"{period['ledger_code']} - {period['account_name']}"
    acct_comm = settings["acct_commissions"]
    acct_igtf = settings["acct_igtf"]
    acct_int = settings["acct_interest"]
    acct_round = settings["acct_rounding"]
    acct_pay = settings["acct_transit_pay"]
    acct_rec = settings["acct_transit_rec"]
    acct_susp = settings["acct_suspense"]

    # Remember applied flags by (source_ref_type, source_ref_id, target)
    prev_applied = {
        (r["source_ref_type"], r["source_ref_id"], r["target"]): r["applied"]
        for r in conn.execute(
            "SELECT source_ref_type, source_ref_id, target, applied FROM adjustments WHERE period_id = ? AND is_manual = 0",
            (period_id,),
        ).fetchall()
    }
    conn.execute("DELETE FROM adjustments WHERE period_id = ? AND is_manual = 0", (period_id,))

    def add_adj(ref_type: str, ref_id: int, adj_date: str, target: str, reference: str, concept: str, debit: str, credit: str, amount_cents: int, note: str = ""):
        if amount_cents <= 0:
            return
        applied = prev_applied.get((ref_type, ref_id, target), 0)
        conn.execute(
            """
            INSERT INTO adjustments
                (period_id, source_ref_type, source_ref_id, adj_date, target, reference, concept,
                 debit_account, credit_account, amount_cents, applied, is_manual, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (period_id, ref_type, ref_id, adj_date, target, reference, concept, debit, credit, abs(amount_cents), applied, note),
        )

    # 1. Unbooked Bank charges/credits (status = AJUSTE_BANCO)
    bank_adjs = conn.execute(
        "SELECT * FROM movements WHERE period_id = ? AND source = 'BANCO' AND status = 'AJUSTE_BANCO' ORDER BY tx_date ASC",
        (period_id,),
    ).fetchall()
    for b in bank_adjs:
        if b["direction"] == "OUT":
            exp_acct = acct_igtf if b["category"] == "IGTF" else acct_comm
            add_adj(
                "MOV_BANCO",
                b["id"],
                b["tx_date"],
                "LIBROS_Y_MAYOR",
                b["reference"],
                f"Registrar {b['category'].lower()}: {b['description']}",
                debit=exp_acct,
                credit=acct_bank,
                amount_cents=b["amount_cents"],
                note="Cargo debitado en Estado de Cuenta del Banco del Tesoro no registrado en empresa.",
            )
        else:
            inc_acct = acct_int if b["category"] == "Intereses / Rendimientos" else acct_rec
            add_adj(
                "MOV_BANCO",
                b["id"],
                b["tx_date"],
                "LIBROS_Y_MAYOR",
                b["reference"],
                f"Registrar abono/NC bancaria: {b['description']}",
                debit=acct_bank,
                credit=inc_acct,
                amount_cents=b["amount_cents"],
                note="Abono acreditado en Estado de Cuenta del Banco del Tesoro no registrado en empresa.",
            )

    # 2. Confirmed duplicates (LIBROS, MAYOR, BANCO)
    dups = conn.execute(
        "SELECT * FROM movements WHERE period_id = ? AND status = 'DUPLICADO' ORDER BY tx_date ASC",
        (period_id,),
    ).fetchall()
    for d in dups:
        if d["source"] == "BANCO":
            target = "RECLAMO_BANCO"
            debit = acct_susp if d["direction"] == "OUT" else acct_bank
            credit = acct_bank if d["direction"] == "OUT" else acct_susp
            note = "Levantar reclamo ante Banco del Tesoro por operación duplicada."
        elif d["source"] == "LIBROS":
            target = "SOLO_LIBROS"
            debit = acct_bank if d["direction"] == "OUT" else acct_rec
            credit = acct_pay if d["direction"] == "OUT" else acct_bank
            note = "Anular/reversar registro duplicado únicamente en Según Libros (Tesorería)."
        else:
            target = "SOLO_MAYOR"
            debit = acct_bank if d["direction"] == "OUT" else acct_rec
            credit = acct_pay if d["direction"] == "OUT" else acct_bank
            note = "Asiento de reverso únicamente en Mayor Analítico (Contabilidad)."
        add_adj(
            "MOV_DUP",
            d["id"],
            d["tx_date"],
            target,
            d["reference"],
            f"Reversar registro duplicado en {d['source']}: {d['description']}",
            debit=debit,
            credit=credit,
            amount_cents=d["amount_cents"],
            note=note,
        )

    # 3. Groups: missing in Mayor, missing in Libros, or matched with amount discrepancy
    groups = conn.execute(
        "SELECT * FROM match_groups WHERE period_id = ? AND match_type != 'REVERSO' ORDER BY id ASC",
        (period_id,),
    ).fetchall()
    for g in groups:
        mems = [
            dict(r)
            for r in conn.execute("SELECT * FROM movements WHERE match_group_id = ?", (g["id"],)).fetchall()
        ]
        if not mems:
            continue
        by_src: Dict[str, List[dict]] = {"BANCO": [], "LIBROS": [], "MAYOR": []}
        for m in mems:
            by_src[m["source"]].append(m)

        anchor = (by_src["BANCO"] or by_src["LIBROS"] or by_src["MAYOR"])[0]
        canon = g["canonical_cents"]

        # Missing in MAYOR while in BANCO + LIBROS
        if period["use_mayor"] and by_src["BANCO"] and by_src["LIBROS"] and not by_src["MAYOR"]:
            ref_txt = (by_src["LIBROS"][0]["reference"] or anchor["reference"])
            desc_txt = by_src["LIBROS"][0]["description"]
            if canon < 0:
                add_adj(
                    "GRP_MAYOR",
                    g["id"],
                    anchor["tx_date"],
                    "SOLO_MAYOR",
                    ref_txt,
                    f"Contabilizar egreso faltante en Mayor Analítico: {desc_txt}",
                    debit=acct_pay,
                    credit=acct_bank,
                    amount_cents=abs(canon),
                    note="Movimiento ya conciliado entre Banco del Tesoro y Según Libros, pendiente de pase a Contabilidad.",
                )
            elif canon > 0:
                add_adj(
                    "GRP_MAYOR",
                    g["id"],
                    anchor["tx_date"],
                    "SOLO_MAYOR",
                    ref_txt,
                    f"Contabilizar ingreso faltante en Mayor Analítico: {desc_txt}",
                    debit=acct_bank,
                    credit=acct_rec,
                    amount_cents=abs(canon),
                    note="Ingreso ya conciliado entre Banco del Tesoro y Según Libros, pendiente de pase a Contabilidad.",
                )

        # Missing in LIBROS while in BANCO + MAYOR
        if by_src["BANCO"] and by_src["MAYOR"] and not by_src["LIBROS"]:
            ref_txt = by_src["MAYOR"][0]["reference"] or anchor["reference"]
            desc_txt = by_src["MAYOR"][0]["description"]
            add_adj(
                "GRP_LIBROS",
                g["id"],
                anchor["tx_date"],
                "SOLO_LIBROS",
                ref_txt,
                f"Registrar movimiento faltante en Según Libros (Tesorería): {desc_txt}",
                debit=acct_bank if canon > 0 else acct_pay,
                credit=acct_rec if canon > 0 else acct_bank,
                amount_cents=abs(canon),
                note="Ya aparece en Banco del Tesoro y Mayor Analítico, pero no fue cargado en el módulo de Tesorería.",
            )

        # Amount differences inside group
        sum_l = sum(x["signed_cents"] for x in by_src["LIBROS"]) if by_src["LIBROS"] else None
        sum_m = sum(x["signed_cents"] for x in by_src["MAYOR"]) if by_src["MAYOR"] else None
        adj_l = (canon - sum_l) if sum_l is not None else 0
        adj_m = (canon - sum_m) if (period["use_mayor"] and sum_m is not None) else 0

        if adj_l != 0 and adj_l == adj_m:
            # Same adjustment needed in both Libros & Mayor (e.g. céntimos Bs. -0,40)
            add_adj(
                "GRP_DIF_BOTH",
                g["id"],
                anchor["tx_date"],
                "LIBROS_Y_MAYOR",
                anchor["reference"],
                f"Ajuste por diferencia de monto ({anchor['description']})",
                debit=acct_bank if adj_l > 0 else acct_round,
                credit=acct_round if adj_l > 0 else acct_bank,
                amount_cents=abs(adj_l),
                note=g["note"],
            )
        else:
            if adj_l != 0:
                target = "AUDITORIA_SISTEMA" if g["resolution_type"] == "FALLA_SISTEMA" else "SOLO_LIBROS"
                l_ref = by_src["LIBROS"][0]["reference"] or anchor["reference"]
                add_adj(
                    "GRP_DIF_L",
                    g["id"],
                    anchor["tx_date"],
                    target,
                    l_ref,
                    f"Ajuste/soporte diferencia en Según Libros: {by_src['LIBROS'][0]['description']}",
                    debit=acct_bank if adj_l > 0 else acct_susp,
                    credit=acct_susp if adj_l > 0 else acct_bank,
                    amount_cents=abs(adj_l),
                    note=g["note"],
                )
            if adj_m != 0:
                target = "AUDITORIA_SISTEMA" if g["resolution_type"] == "FALLA_SISTEMA" else "SOLO_MAYOR"
                m_ref = by_src["MAYOR"][0]["reference"] or anchor["reference"]
                add_adj(
                    "GRP_DIF_M",
                    g["id"],
                    anchor["tx_date"],
                    target,
                    m_ref,
                    f"Ajuste/soporte diferencia en Mayor Analítico: {by_src['MAYOR'][0]['description']}",
                    debit=acct_bank if adj_m > 0 else acct_susp,
                    credit=acct_susp if adj_m > 0 else acct_bank,
                    amount_cents=abs(adj_m),
                    note=g["note"],
                )

    # 4. Bank errors to claim
    errs = conn.execute(
        "SELECT * FROM movements WHERE period_id = ? AND source = 'BANCO' AND status = 'ERROR_BANCO' ORDER BY tx_date ASC",
        (period_id,),
    ).fetchall()
    for e in errs:
        add_adj(
            "MOV_ERR_B",
            e["id"],
            e["tx_date"],
            "RECLAMO_BANCO",
            e["reference"],
            f"Reclamo bancario ante Banco del Tesoro: {e['description']}",
            debit=acct_susp if e["direction"] == "OUT" else acct_bank,
            credit=acct_bank if e["direction"] == "OUT" else acct_susp,
            amount_cents=e["amount_cents"],
            note="Partida observada en Estado de Cuenta para gestión de reclamo bancario.",
        )


def compute_reconciliation_sheet(conn, period_id: int) -> dict:
    """
    Build the complete TWO-PHASE Formal Bank Reconciliation Statement,
    following the company's procedure:

      FASE 1: Según Libros (Tesorería) <-> Mayor Analítico (Contabilidad)
              -> exact internal differences between the two company records.

      FASE 2: Mayor Analítico (Contabilidad) <-> Banco del Tesoro (Estado de Cuenta)
              -> bank differences once Libros and Mayor agree.

    Returns the phased schedule rows, both phase bridges (exact differences),
    source integrity checks and the summary.
    """
    period = conn.execute(
        """
        SELECT p.*, a.name AS account_name, a.bank_name, a.account_number,
               a.account_type, a.ledger_code, a.currency
        FROM periods p JOIN accounts a ON a.id = p.account_id
        WHERE p.id = ?
        """,
        (period_id,),
    ).fetchone()
    if not period:
        raise ValueError("Período no encontrado")
    p = dict(period)
    use_mayor = bool(p["use_mayor"])

    all_movs = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM movements WHERE period_id = ? ORDER BY tx_date ASC, id ASC",
            (period_id,),
        ).fetchall()
    ]
    groups = [
        dict(g)
        for g in conn.execute(
            "SELECT * FROM match_groups WHERE period_id = ? ORDER BY id ASC",
            (period_id,),
        ).fetchall()
    ]
    issues = [
        dict(i)
        for i in conn.execute(
            "SELECT * FROM system_issues WHERE period_id = ? ORDER BY issue_date ASC, id ASC",
            (period_id,),
        ).fetchall()
    ]
    suspected_dups = find_suspected_duplicates(conn, period_id)
    suspect_ids = {d["suspect"]["id"] for d in suspected_dups}

    # Check source internal balance integrity: open + in - out vs declared close
    integrity = {}
    for src, ok, ck in (
        ("BANCO", "open_banco_cents", "close_banco_cents"),
        ("LIBROS", "open_libros_cents", "close_libros_cents"),
        ("MAYOR", "open_mayor_cents", "close_mayor_cents"),
    ):
        sm = [m for m in all_movs if m["source"] == src]
        sm_cur = [m for m in sm if not m.get("is_carryover")]
        t_in = sum(m["amount_cents"] for m in sm_cur if m["direction"] == "IN")
        t_out = sum(m["amount_cents"] for m in sm_cur if m["direction"] == "OUT")
        op = p[ok]
        cl = p[ck]
        calc_close = op + t_in - t_out
        reconciled_cnt = sum(1 for m in sm if m["status"] != "PENDIENTE")
        integrity[src] = {
            "count": len(sm),
            "reconciled_count": reconciled_cnt,
            "pending_count": len(sm) - reconciled_cnt,
            "open_cents": op,
            "total_in_cents": t_in,
            "total_out_cents": t_out,
            "net_cents": t_in - t_out,
            "calculated_close_cents": calc_close,
            "declared_close_cents": cl,
            "internal_diff_cents": cl - calc_close,
        }

    # ---------------- Buckets ----------------
    # FASE 1: Libros <-> Mayor
    p1_missing_libros: List[dict] = []   # in Mayor, not in Libros  -> Libros col
    p1_missing_mayor: List[dict] = []    # in Libros, not in Mayor  -> Mayor col
    p1_amt_adj_libros: List[dict] = []   # Libros vs Mayor amount diff -> Libros col (bring Libros to Mayor)
    p1_dup_libros: List[dict] = []       # duplicate reversals in Libros
    p1_dup_mayor: List[dict] = []        # duplicate reversals in Mayor
    p1_sys_libros: List[dict] = []       # system failures in Libros
    p1_sys_mayor: List[dict] = []        # system failures in Mayor

    # FASE 2: Mayor <-> Banco
    p2_dep_transit: List[dict] = []      # in Mayor, not in Banco (IN)  -> Banco col
    p2_pay_transit: List[dict] = []      # in Mayor, not in Banco (OUT) -> Banco col
    p2_bank_errors: List[dict] = []      # bank errors / duplicates    -> Banco col
    p2_nc_unbooked: List[dict] = []      # in Banco, not in Mayor (IN)  -> Mayor+Libros cols
    p2_nd_unbooked: List[dict] = []      # in Banco, not in Mayor (OUT) -> Mayor+Libros cols
    p2_amt_adj_mayor: List[dict] = []    # Mayor vs Banco amount diff  -> Mayor+Libros cols

    bridge1: List[dict] = []  # exact Libros vs Mayor differences
    bridge2: List[dict] = []  # exact Mayor vs Banco differences

    movs_by_group: Dict[int, List[dict]] = {}
    ungrouped: List[dict] = []
    for m in all_movs:
        gid = m["match_group_id"]
        if gid:
            movs_by_group.setdefault(gid, []).append(m)
        else:
            ungrouped.append(m)

    active_issue_groups = {
        i["match_group_id"] for i in issues if i["match_group_id"] and i["status"] != "RESUELTA"
    }

    for g in groups:
        if g["match_type"] == "REVERSO":
            continue
        mems = movs_by_group.get(g["id"], [])
        if not mems:
            continue
        by_s: Dict[str, List[dict]] = {"BANCO": [], "LIBROS": [], "MAYOR": []}
        for m in mems:
            by_s[m["source"]].append(m)

        sum_l = sum(x["signed_cents"] for x in by_s["LIBROS"]) if by_s["LIBROS"] else None
        sum_m = sum(x["signed_cents"] for x in by_s["MAYOR"]) if by_s["MAYOR"] else None
        sum_b = sum(x["signed_cents"] for x in by_s["BANCO"]) if by_s["BANCO"] else None

        anchor = (by_s["BANCO"] or by_s["LIBROS"] or by_s["MAYOR"])[0]
        label = anchor["description"]
        ref = (by_s["LIBROS"][0]["reference"] if by_s["LIBROS"] else anchor["reference"]) or ""
        dt = anchor["tx_date"]
        is_sys = g["resolution_type"] == "FALLA_SISTEMA" or g["id"] in active_issue_groups

        # ---------- FASE 1: Libros <-> Mayor ----------
        if use_mayor:
            if sum_l is not None and sum_m is not None:
                if sum_l != sum_m:
                    entry = {
                        "date": dt,
                        "reference": by_s["LIBROS"][0]["reference"] or ref,
                        "description": f"{by_s['LIBROS'][0]['description']} (Libros: Bs. {cents_to_ve(abs(sum_l))} vs. Mayor: Bs. {cents_to_ve(abs(sum_m))})",
                        "effect_cents": sum_m - sum_l,
                        "group_id": g["id"],
                    }
                    (p1_sys_libros if is_sys else p1_amt_adj_libros).append(entry)
                    bridge1.append({
                        "kind": "FALLA_SISTEMA" if is_sys else "DIFERENCIA_MONTO",
                        "date": dt,
                        "reference": by_s["LIBROS"][0]["reference"] or ref,
                        "description": f"Diferencia de monto ({by_s['LIBROS'][0]['description']}): Libros Bs. {cents_to_ve(abs(sum_l))} vs. Mayor Bs. {cents_to_ve(abs(sum_m))}",
                        "libros_cents": sum_l,
                        "mayor_cents": sum_m,
                        "diff_libros_minus_mayor_cents": sum_l - sum_m,
                    })
            elif sum_l is not None:
                p1_missing_mayor.append({"date": dt, "reference": ref, "description": label, "effect_cents": sum_l, "group_id": g["id"]})
                bridge1.append({
                    "kind": "FALTA_EN_MAYOR",
                    "date": dt,
                    "reference": ref,
                    "description": f"En Según Libros pero falta en Mayor Analítico: {label}",
                    "libros_cents": sum_l,
                    "mayor_cents": 0,
                    "diff_libros_minus_mayor_cents": sum_l,
                })
            elif sum_m is not None:
                p1_missing_libros.append({"date": dt, "reference": ref, "description": label, "effect_cents": sum_m, "group_id": g["id"]})
                bridge1.append({
                    "kind": "FALTA_EN_LIBROS",
                    "date": dt,
                    "reference": ref,
                    "description": f"En Mayor Analítico pero no en Según Libros: {label}",
                    "libros_cents": 0,
                    "mayor_cents": sum_m,
                    "diff_libros_minus_mayor_cents": -sum_m,
                })

        # ---------- FASE 2: Mayor <-> Banco ----------
        # mayor_eff = Mayor amount AFTER phase 1 (Libros-only items are posted to Mayor)
        mayor_eff = sum_m if sum_m is not None else sum_l
        if mayor_eff is not None and sum_b is not None:
            if mayor_eff != sum_b:
                p2_amt_adj_mayor.append({
                    "date": dt,
                    "reference": ref,
                    "description": f"{label} (Mayor: Bs. {cents_to_ve(abs(mayor_eff))} vs. Banco: Bs. {cents_to_ve(abs(sum_b))})",
                    "effect_cents": sum_b - mayor_eff,
                    "group_id": g["id"],
                })
                bridge2.append({
                    "kind": "DIFERENCIA_MONTO",
                    "date": dt,
                    "reference": ref,
                    "description": f"Diferencia de monto ({label}): Mayor Bs. {cents_to_ve(abs(mayor_eff))} vs. Banco Bs. {cents_to_ve(abs(sum_b))}",
                    "mayor_cents": mayor_eff,
                    "banco_cents": sum_b,
                    "diff_mayor_minus_banco_cents": mayor_eff - sum_b,
                })
        elif mayor_eff is not None:
            item = {"date": dt, "reference": ref, "description": label, "effect_cents": mayor_eff, "group_id": g["id"]}
            (p2_dep_transit if mayor_eff >= 0 else p2_pay_transit).append(item)
            bridge2.append({
                "kind": "EN_TRANSITO",
                "date": dt,
                "reference": ref,
                "description": f"En Mayor Analítico, pendiente en Banco del Tesoro: {label}",
                "mayor_cents": mayor_eff,
                "banco_cents": 0,
                "diff_mayor_minus_banco_cents": mayor_eff,
            })
        elif sum_b is not None:
            item = {"date": dt, "reference": ref, "description": label, "effect_cents": sum_b, "group_id": g["id"]}
            (p2_nc_unbooked if sum_b >= 0 else p2_nd_unbooked).append(item)
            bridge2.append({
                "kind": "NO_REGISTRADO_EN_MAYOR",
                "date": dt,
                "reference": ref,
                "description": f"En Banco del Tesoro, no registrado en Mayor Analítico: {label}",
                "mayor_cents": 0,
                "banco_cents": sum_b,
                "diff_mayor_minus_banco_cents": -sum_b,
            })

    # ---------------- Ungrouped standalone movements ----------------
    unreviewed_suspects: List[dict] = []
    for m in ungrouped:
        if m["status"] == "REVERSO":
            continue
        src = m["source"]
        sc = m["signed_cents"]
        st = m["status"]
        base = {
            "date": m["tx_date"],
            "reference": m["reference"],
            "description": m["description"],
            "movement_id": m["id"],
            "status": st,
            "provisional": st == "PENDIENTE",
        }

        if src == "BANCO":
            if st in ("ERROR_BANCO", "DUPLICADO"):
                p2_bank_errors.append({**base, "effect_cents": -sc})
                bridge2.append({
                    "kind": "ERROR_BANCO",
                    "date": m["tx_date"],
                    "reference": m["reference"],
                    "description": f"Error/duplicado bancario por reclamar: {m['description']}",
                    "mayor_cents": 0,
                    "banco_cents": sc,
                    "diff_mayor_minus_banco_cents": -sc,
                })
            elif m["id"] in suspect_ids and st == "PENDIENTE":
                unreviewed_suspects.append(m)
                bridge2.append({
                    "kind": "POSIBLE_DUPLICADO_BANCO",
                    "date": m["tx_date"],
                    "reference": m["reference"],
                    "description": f"Posible duplicado sin confirmar en Banco: {m['description']}",
                    "mayor_cents": 0,
                    "banco_cents": sc,
                    "diff_mayor_minus_banco_cents": -sc,
                })
            else:
                # AJUSTE_BANCO or regular PENDIENTE bank movement -> unbooked in Mayor (and Libros)
                (p2_nc_unbooked if sc >= 0 else p2_nd_unbooked).append({**base, "effect_cents": sc})
                bridge2.append({
                    "kind": "NO_REGISTRADO_EN_MAYOR",
                    "date": m["tx_date"],
                    "reference": m["reference"],
                    "description": f"En Banco del Tesoro, no registrado en Mayor Analítico: {m['description']}",
                    "mayor_cents": 0,
                    "banco_cents": sc,
                    "diff_mayor_minus_banco_cents": -sc,
                })

        elif src == "LIBROS":
            if st == "DUPLICADO":
                p1_dup_libros.append({**base, "effect_cents": -sc})
                bridge1.append({
                    "kind": "DUPLICADO_LIBROS",
                    "date": m["tx_date"],
                    "reference": m["reference"],
                    "description": f"Registro duplicado en Según Libros: {m['description']}",
                    "libros_cents": sc,
                    "mayor_cents": 0,
                    "diff_libros_minus_mayor_cents": sc,
                })
            elif st == "FALLA_SISTEMA":
                p1_sys_libros.append({**base, "effect_cents": -sc})
                bridge1.append({
                    "kind": "FALLA_SISTEMA",
                    "date": m["tx_date"],
                    "reference": m["reference"],
                    "description": f"Falla de sistema en Según Libros: {m['description']}",
                    "libros_cents": sc,
                    "mayor_cents": 0,
                    "diff_libros_minus_mayor_cents": sc,
                })
            elif m["id"] in suspect_ids and st == "PENDIENTE":
                # Keep unconfirmed duplicate visible as unexplained difference until user confirms!
                unreviewed_suspects.append(m)
                bridge1.append({
                    "kind": "POSIBLE_DUPLICADO_LIBROS",
                    "date": m["tx_date"],
                    "reference": m["reference"],
                    "description": f"Posible duplicado sin confirmar en Según Libros: {m['description']}",
                    "libros_cents": sc,
                    "mayor_cents": 0,
                    "diff_libros_minus_mayor_cents": sc,
                })
            else:
                # In Libros, missing in Mayor (Fase 1) and in transit vs Banco (Fase 2)
                p1_missing_mayor.append({**base, "effect_cents": sc})
                bridge1.append({
                    "kind": "FALTA_EN_MAYOR",
                    "date": m["tx_date"],
                    "reference": m["reference"],
                    "description": f"En Según Libros pero falta en Mayor Analítico: {m['description']}",
                    "libros_cents": sc,
                    "mayor_cents": 0,
                    "diff_libros_minus_mayor_cents": sc,
                })
                (p2_dep_transit if sc >= 0 else p2_pay_transit).append({**base, "effect_cents": sc})
                bridge2.append({
                    "kind": "EN_TRANSITO",
                    "date": m["tx_date"],
                    "reference": m["reference"],
                    "description": f"En Según Libros/Mayor, pendiente en Banco del Tesoro: {m['description']}",
                    "mayor_cents": sc,
                    "banco_cents": 0,
                    "diff_mayor_minus_banco_cents": sc,
                })

        elif src == "MAYOR" and use_mayor:
            if st == "DUPLICADO":
                p1_dup_mayor.append({**base, "effect_cents": -sc})
                bridge1.append({
                    "kind": "DUPLICADO_MAYOR",
                    "date": m["tx_date"],
                    "reference": m["reference"],
                    "description": f"Asiento duplicado en Mayor Analítico: {m['description']}",
                    "libros_cents": 0,
                    "mayor_cents": sc,
                    "diff_libros_minus_mayor_cents": -sc,
                })
            elif st == "FALLA_SISTEMA":
                p1_sys_mayor.append({**base, "effect_cents": -sc})
                bridge1.append({
                    "kind": "FALLA_SISTEMA",
                    "date": m["tx_date"],
                    "reference": m["reference"],
                    "description": f"Falla de sistema en Mayor Analítico: {m['description']}",
                    "libros_cents": 0,
                    "mayor_cents": sc,
                    "diff_libros_minus_mayor_cents": -sc,
                })
            elif m["id"] in suspect_ids and st == "PENDIENTE":
                unreviewed_suspects.append(m)
                bridge1.append({
                    "kind": "POSIBLE_DUPLICADO_MAYOR",
                    "date": m["tx_date"],
                    "reference": m["reference"],
                    "description": f"Posible duplicado sin confirmar en Mayor: {m['description']}",
                    "libros_cents": 0,
                    "mayor_cents": sc,
                    "diff_libros_minus_mayor_cents": -sc,
                })
            else:
                # In Mayor, missing in Libros (Fase 1) and in transit vs Banco (Fase 2)
                p1_missing_libros.append({**base, "effect_cents": sc})
                bridge1.append({
                    "kind": "FALTA_EN_LIBROS",
                    "date": m["tx_date"],
                    "reference": m["reference"],
                    "description": f"En Mayor Analítico pero falta en Según Libros: {m['description']}",
                    "libros_cents": 0,
                    "mayor_cents": sc,
                    "diff_libros_minus_mayor_cents": -sc,
                })
                (p2_dep_transit if sc >= 0 else p2_pay_transit).append({**base, "effect_cents": sc})
                bridge2.append({
                    "kind": "EN_TRANSITO",
                    "date": m["tx_date"],
                    "reference": m["reference"],
                    "description": f"En Mayor Analítico, pendiente en Banco del Tesoro: {m['description']}",
                    "mayor_cents": sc,
                    "banco_cents": 0,
                    "diff_mayor_minus_banco_cents": sc,
                })

    # ---------------- Standalone / historical carryover system issues ----------------
    for iss in issues:
        if iss["status"] == "RESUELTA":
            continue
        if iss["match_group_id"] is not None or iss["movement_id"] is not None:
            continue
        tag = "Histórica arrastrada" if iss["affects_opening"] else "Documentada"
        if iss["adj_libros_cents"] != 0:
            p1_sys_libros.append({
                "date": iss["issue_date"],
                "reference": iss["code"],
                "description": f"[{tag}] {iss['title']}",
                "effect_cents": iss["adj_libros_cents"],
                "issue_id": iss["id"],
            })
        if use_mayor and iss["adj_mayor_cents"] != 0:
            p1_sys_mayor.append({
                "date": iss["issue_date"],
                "reference": iss["code"],
                "description": f"[{tag}] {iss['title']}",
                "effect_cents": iss["adj_mayor_cents"],
                "issue_id": iss["id"],
            })
        if use_mayor and (iss["adj_libros_cents"] != iss["adj_mayor_cents"]):
            net_lm = iss["adj_mayor_cents"] - iss["adj_libros_cents"]
            bridge1.append({
                "kind": "FALLA_HISTORICA",
                "date": iss["issue_date"],
                "reference": iss["code"],
                "description": f"Falla histórica del sistema ({iss['code']}): {iss['title']}",
                "libros_cents": iss["libros_cents"] or 0,
                "mayor_cents": iss["mayor_cents"] or 0,
                "diff_libros_minus_mayor_cents": net_lm,
            })

    def S(lst: List[dict]) -> int:
        return sum(x["effect_cents"] for x in lst)

    close_b = p["close_banco_cents"]
    close_l = p["close_libros_cents"]
    close_m = p["close_mayor_cents"]

    # FASE 1 totals
    f1_libros_total = S(p1_missing_libros) + S(p1_amt_adj_libros) + S(p1_dup_libros) + S(p1_sys_libros)
    f1_mayor_total = S(p1_missing_mayor) + S(p1_dup_mayor) + S(p1_sys_mayor)
    conc_libros_f1 = close_l + f1_libros_total
    conc_mayor_f1 = close_m + f1_mayor_total

    # FASE 2 totals (Mayor adjustments are mirrored to Libros: after Fase 1 both records agree)
    f2_banco_total = S(p2_dep_transit) + S(p2_pay_transit) + S(p2_bank_errors)
    f2_mayor_total = S(p2_nc_unbooked) + S(p2_nd_unbooked) + S(p2_amt_adj_mayor)
    f2_libros_total = f2_mayor_total
    conc_banco = close_b + f2_banco_total
    conc_mayor_f2 = conc_mayor_f1 + f2_mayor_total
    conc_libros_f2 = conc_libros_f1 + f2_libros_total

    # Bridges
    raw1 = (close_l - close_m) if use_mayor else 0
    exp1 = sum(x["diff_libros_minus_mayor_cents"] for x in bridge1) if use_mayor else 0
    unexp1 = raw1 - exp1

    raw2 = (conc_mayor_f1 - close_b) if use_mayor else (close_l - close_b)
    exp2 = sum(x["diff_mayor_minus_banco_cents"] for x in bridge2)
    unexp2 = raw2 - exp2

    is_balanced = (
        conc_libros_f2 == conc_banco
        and (not use_mayor or conc_mayor_f2 == conc_banco)
        and not unreviewed_suspects
    )

    # ---------------- Formal phased schedule rows ----------------
    def line(label, phase, banco=0, libros=0, mayor=0, items=None):
        return {
            "label": label,
            "phase": phase,
            "kind": "linea",
            "banco_cents": banco,
            "libros_cents": libros,
            "mayor_cents": mayor if use_mayor else None,
            "items": items or [],
        }

    rows = [
        {
            "label": "Saldo final según reporte",
            "phase": 0,
            "kind": "saldo",
            "banco_cents": close_b,
            "libros_cents": close_l,
            "mayor_cents": close_m if use_mayor else None,
            "items": [],
        },
        {"label": "FASE 1 · Según Libros (Tesorería) ↔ Mayor Analítico (Contabilidad)", "phase": 1, "kind": "fase", "banco_cents": None, "libros_cents": None, "mayor_cents": None, "items": []},
        line("(+) Partidas en Mayor pendientes en Según Libros", 1, libros=S(p1_missing_libros), items=p1_missing_libros),
        line("(+) Partidas en Según Libros pendientes en Mayor Analítico", 1, mayor=S(p1_missing_mayor), items=p1_missing_mayor),
        line("(+/-) Ajustes por diferencia de monto (Libros ↔ Mayor)", 1, libros=S(p1_amt_adj_libros), items=p1_amt_adj_libros),
        line("(+/-) Reverso de registros duplicados en Según Libros", 1, libros=S(p1_dup_libros), items=p1_dup_libros),
        line("(+/-) Reverso de asientos duplicados en Mayor Analítico", 1, mayor=S(p1_dup_mayor), items=p1_dup_mayor),
        line("(+/-) Fallas del sistema identificadas (Libros / Mayor)", 1, libros=S(p1_sys_libros), mayor=S(p1_sys_mayor), items=p1_sys_libros + p1_sys_mayor),
        {
            "label": "SALDO CONCILIADO FASE 1 (Libros ↔ Mayor)",
            "phase": 1,
            "kind": "subtotal",
            "banco_cents": None,
            "libros_cents": conc_libros_f1,
            "mayor_cents": conc_mayor_f1 if use_mayor else None,
            "items": [],
        },
        {"label": "FASE 2 · Mayor Analítico (Contabilidad) ↔ Banco del Tesoro (Estado de Cuenta)", "phase": 2, "kind": "fase", "banco_cents": None, "libros_cents": None, "mayor_cents": None, "items": []},
        line("(+) Depósitos y abonos en tránsito", 2, banco=S(p2_dep_transit), items=p2_dep_transit),
        line("(-) Pagos, cheques y transferencias en tránsito", 2, banco=S(p2_pay_transit), items=p2_pay_transit),
        line("(+/-) Errores o cargos duplicados del banco por reclamar", 2, banco=S(p2_bank_errors), items=p2_bank_errors),
        line("(+) Notas de crédito y abonos bancarios no registrados", 2, libros=S(p2_nc_unbooked), mayor=S(p2_nc_unbooked), items=p2_nc_unbooked),
        line("(-) Comisiones, IGTF y notas de débito no registradas", 2, libros=S(p2_nd_unbooked), mayor=S(p2_nd_unbooked), items=p2_nd_unbooked),
        line("(+/-) Ajustes por diferencia de monto (Mayor ↔ Banco)", 2, libros=S(p2_amt_adj_mayor), mayor=S(p2_amt_adj_mayor), items=p2_amt_adj_mayor),
        {
            "label": "SALDO CONCILIADO FASE 2 (Mayor ↔ Banco)",
            "phase": 2,
            "kind": "total",
            "banco_cents": conc_banco,
            "libros_cents": conc_libros_f2,
            "mayor_cents": conc_mayor_f2 if use_mayor else None,
            "items": [],
        },
    ]

    fase1 = {
        "close_libros_cents": close_l,
        "close_mayor_cents": close_m,
        "raw_diff_cents": raw1,
        "explained_diff_cents": exp1,
        "unexplained_diff_cents": unexp1,
        "conciliado_libros_cents": conc_libros_f1,
        "conciliado_mayor_cents": conc_mayor_f1,
        "items": bridge1,
    }
    fase2 = {
        "close_mayor_cents": conc_mayor_f1,
        "close_banco_cents": close_b,
        "raw_diff_cents": raw2,
        "explained_diff_cents": exp2,
        "unexplained_diff_cents": unexp2,
        "conciliado_mayor_cents": conc_mayor_f2,
        "conciliado_banco_cents": conc_banco,
        "items": bridge2,
    }

    return {
        "period": p,
        "use_mayor": use_mayor,
        "integrity": integrity,
        "rows": rows,
        "fase1": fase1,
        "fase2": fase2,
        "banco": {
            "open_cents": p["open_banco_cents"],
            "close_cents": close_b,
            "conciliated_cents": conc_banco,
        },
        "libros": {
            "open_cents": p["open_libros_cents"],
            "close_cents": close_l,
            "conciliated_cents": conc_libros_f2,
        },
        "mayor": {
            "open_cents": p["open_mayor_cents"],
            "close_cents": close_m,
            "conciliated_cents": conc_mayor_f2,
        },
        "summary": {
            "is_balanced": is_balanced,
            "diff_banco_libros_cents": conc_banco - conc_libros_f2,
            "diff_banco_mayor_cents": (conc_banco - conc_mayor_f2) if use_mayor else 0,
            "diff_libros_mayor_conciliated_cents": (conc_libros_f1 - conc_mayor_f1) if use_mayor else 0,
            "unreviewed_duplicates_count": len(unreviewed_suspects),
            "unreviewed_duplicates": unreviewed_suspects,
        },
        # Backwards-compatible aliases
        "libros_vs_mayor_bridge": fase1,
        "mayor_vs_banco_bridge": fase2,
    }
