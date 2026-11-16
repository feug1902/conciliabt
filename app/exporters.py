import io
import sqlite3
from datetime import datetime
from typing import List

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .database import cents_to_float, cents_to_ve, get_settings
from .reconciler import compute_reconciliation_sheet

TARGET_LABELS = {
    "LIBROS_Y_MAYOR": "Tesorería y Contabilidad",
    "SOLO_MAYOR": "Solo Mayor Analítico",
    "SOLO_LIBROS": "Solo Según Libros",
    "RECLAMO_BANCO": "Reclamo Banco del Tesoro",
    "AUDITORIA_SISTEMA": "Soporte / Falla de Sistema",
}

STATUS_LABELS = {
    "PENDIENTE": "Pendiente",
    "CONCILIADO": "Conciliado",
    "CONCILIADO_DIF": "Conciliado c/ Diferencia",
    "EN_TRANSITO": "En Tránsito",
    "AJUSTE_BANCO": "Cargo/Abono Bancario",
    "DUPLICADO": "Duplicado",
    "REVERSO": "Reverso / Anulación",
    "FALLA_SISTEMA": "Falla de Sistema",
    "ERROR_BANCO": "Error Bancario",
}


def _fmt_date_ve(iso_str: str) -> str:
    if not iso_str or len(iso_str) < 10:
        return iso_str or ""
    parts = iso_str[:10].split("-")
    if len(parts) == 3:
        return f"{parts[2]}/{parts[1]}/{parts[0]}"
    return iso_str


def export_reconciliation_excel(conn: sqlite3.Connection, period_id: int) -> bytes:
    """Generate a formal 6-sheet .xlsx Venezuelan Bank Reconciliation Workbook (two-phase)."""
    sheet_data = compute_reconciliation_sheet(conn, period_id)
    settings = get_settings(conn)
    p = sheet_data["period"]
    use_mayor = sheet_data["use_mayor"]

    wb = openpyxl.Workbook()

    # Palette & styles
    navy_fill = PatternFill("solid", fgColor="0F2942")
    sub_fill = PatternFill("solid", fgColor="1E4265")
    fase_fill = PatternFill("solid", fgColor="DCE6F1")
    soft_fill = PatternFill("solid", fgColor="F1F5F9")
    ok_fill = PatternFill("solid", fgColor="ECFDF3")
    warn_fill = PatternFill("solid", fgColor="FFFAEB")
    sys_fill = PatternFill("solid", fgColor="EEF2FF")
    err_fill = PatternFill("solid", fgColor="FEF3F2")

    f_title = Font(name="Calibri", size=14, bold=True, color="FFFFFF")
    f_subtitle = Font(name="Calibri", size=10, italic=True, color="E2E8F0")
    f_hdr = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    f_fase = Font(name="Calibri", size=11, bold=True, color="0F2942")
    f_bold = Font(name="Calibri", size=10, bold=True, color="0F172A")
    f_reg = Font(name="Calibri", size=10, color="1E293B")
    f_ok = Font(name="Calibri", size=10, bold=True, color="067647")
    f_err = Font(name="Calibri", size=10, bold=True, color="B42318")

    thin = Side(border_style="thin", color="CBD5E1")
    double_b = Side(border_style="double", color="0F2942")
    top_thick = Side(border_style="medium", color="0F2942")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    total_border = Border(left=thin, right=thin, top=top_thick, bottom=double_b)
    num_fmt = '#,##0.00;[Red]-#,##0.00;"0.00"'

    # ================= SHEET 1: HOJA DE CONCILIACIÓN =================
    ws1 = wb.active
    ws1.title = "Hoja de Conciliacion"
    ws1.views.sheetView[0].showGridLines = False

    ws1.merge_cells("A1:D1")
    ws1["A1"] = f"CONCILIACIÓN BANCARIA — {p['bank_name'].upper()}"
    ws1["A1"].font = f_title
    ws1["A1"].fill = navy_fill
    ws1["A1"].alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws1.row_dimensions[1].height = 28

    ws1.merge_cells("A2:D2")
    ws1["A2"] = f"{settings['company_name']}  |  RIF: {settings['company_rif']}  |  Período: {p['label']}"
    ws1["A2"].font = f_subtitle
    ws1["A2"].fill = sub_fill
    ws1["A2"].alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws1.row_dimensions[2].height = 20

    meta_rows = [
        ("Cuenta Bancaria:", f"{p['account_name']} ({p['account_number']})", "Código Contable:", p["ledger_code"]),
        ("Tipo / Moneda:", f"{p['account_type']} — {p['currency']}", "Fecha de Emisión:", datetime.now().strftime("%d/%m/%Y %H:%M")),
    ]
    r = 4
    for k1, v1, k2, v2 in meta_rows:
        ws1.cell(r, 1, k1).font = f_bold
        ws1.cell(r, 2, v1).font = f_reg
        ws1.cell(r, 3, k2).font = f_bold
        ws1.cell(r, 4, v2).font = f_reg
        r += 1

    r = 7
    headers = [
        "CONCEPTO DE CONCILIACIÓN (PROCEDIMIENTO EN DOS FASES)",
        "BANCO DEL TESORO (Bs.)",
        "SEGÚN LIBROS - TESORERÍA (Bs.)",
        "MAYOR ANALÍTICO - CONTAB. (Bs.)",
    ]
    for ci, h in enumerate(headers, 1):
        c = ws1.cell(r, ci, h)
        c.font = f_hdr
        c.fill = navy_fill
        c.alignment = Alignment(horizontal="center" if ci > 1 else "left", vertical="center", wrap_text=True)
        c.border = box
    ws1.row_dimensions[r].height = 26

    for row in sheet_data["rows"]:
        r += 1
        kind = row["kind"]
        c1 = ws1.cell(r, 1, row["label"])
        c1.border = box
        if kind == "fase":
            ws1.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
            c1.font = f_fase
            c1.fill = fase_fill
            c1.alignment = Alignment(horizontal="left", vertical="center", indent=1)
            for ci in range(2, 5):
                ws1.cell(r, ci).fill = fase_fill
                ws1.cell(r, ci).border = box
            ws1.row_dimensions[r].height = 22
            continue

        is_b = kind in ("saldo", "subtotal", "total")
        c1.font = f_bold if is_b else f_reg
        if kind == "saldo":
            c1.fill = soft_fill
        elif kind == "total":
            c1.fill = ok_fill
        elif kind == "subtotal":
            c1.fill = PatternFill("solid", fgColor="E0F2FE")

        for ci, key in ((2, "banco_cents"), (3, "libros_cents"), (4, "mayor_cents")):
            val = row.get(key)
            cell = ws1.cell(r, ci, cents_to_float(val) if val is not None else None)
            cell.font = f_bold if is_b else f_reg
            cell.number_format = num_fmt
            cell.alignment = Alignment(horizontal="right")
            cell.border = box
            if kind == "saldo":
                cell.fill = soft_fill
            elif kind == "total":
                cell.fill = ok_fill
            elif kind == "subtotal":
                cell.fill = PatternFill("solid", fgColor="E0F2FE")
        ws1.row_dimensions[r].height = 20

    r += 1
    ws1.cell(r, 1, "DIFERENCIA POR CONCILIAR (Vs. Saldo Conciliado Banco)").font = f_bold
    ws1.cell(r, 1).border = box
    is_bal = sheet_data["summary"]["is_balanced"]
    c_b = ws1.cell(r, 2, 0.0)
    c_l = ws1.cell(r, 3, cents_to_float(sheet_data["summary"]["diff_banco_libros_cents"]))
    c_m = ws1.cell(r, 4, cents_to_float(sheet_data["summary"]["diff_banco_mayor_cents"]) if use_mayor else None)
    for cell in (c_b, c_l, c_m):
        cell.number_format = num_fmt
        cell.font = f_ok if is_bal else f_err
        cell.fill = ok_fill if is_bal else err_fill
        cell.alignment = Alignment(horizontal="right")
        cell.border = box

    # Bridge Fase 1: Libros vs Mayor
    if use_mayor:
        br = sheet_data["fase1"]
        r += 3
        ws1.cell(r, 1, "FASE 1 — DIFERENCIAS EXACTAS: SEGÚN LIBROS (TESORERÍA) VS. MAYOR ANALÍTICO (CONTABILIDAD)").font = f_fase
        r += 1
        for ci, h in enumerate(["Fecha / Ref.", "Explicación de la Partida", "Según Libros (Bs.)", "Dif. Libros - Mayor (Bs.)"], 1):
            c = ws1.cell(r, ci, h)
            c.font = f_hdr
            c.fill = sub_fill
            c.border = box
            c.alignment = Alignment(horizontal="center" if ci > 2 else "left")

        r += 1
        ws1.cell(r, 1, "Saldos Cierre").font = f_bold
        ws1.cell(r, 2, f"Libros: Bs. {cents_to_ve(br['close_libros_cents'])}  |  Mayor: Bs. {cents_to_ve(br['close_mayor_cents'])}").font = f_bold
        ws1.cell(r, 3, cents_to_float(br["close_libros_cents"])).number_format = num_fmt
        ws1.cell(r, 4, cents_to_float(br["raw_diff_cents"])).number_format = num_fmt
        for ci in range(1, 5):
            ws1.cell(r, ci).fill = soft_fill
            ws1.cell(r, ci).border = box

        for bi in br["items"]:
            r += 1
            ws1.cell(r, 1, f"{_fmt_date_ve(bi['date'])}  {bi['reference']}").font = f_reg
            ws1.cell(r, 2, bi["description"]).font = f_reg
            c3 = ws1.cell(r, 3, cents_to_float(bi["libros_cents"]))
            c4 = ws1.cell(r, 4, cents_to_float(bi["diff_libros_minus_mayor_cents"]))
            c3.number_format = num_fmt
            c4.number_format = num_fmt
            for ci in range(1, 5):
                ws1.cell(r, ci).border = box

    # Bridge Fase 2: Mayor vs Banco
    br2 = sheet_data["fase2"]
    r += 3
    ws1.cell(r, 1, "FASE 2 — DIFERENCIAS EXACTAS: MAYOR ANALÍTICO (CONTABILIDAD) VS. BANCO DEL TESORO").font = f_fase
    r += 1
    for ci, h in enumerate(["Fecha / Ref.", "Explicación de la Partida", "Mayor Analítico (Bs.)", "Dif. Mayor - Banco (Bs.)"], 1):
        c = ws1.cell(r, ci, h)
        c.font = f_hdr
        c.fill = sub_fill
        c.border = box
        c.alignment = Alignment(horizontal="center" if ci > 2 else "left")

    r += 1
    ws1.cell(r, 1, "Saldos (Mayor conciliado F1 vs Banco)").font = f_bold
    ws1.cell(r, 2, f"Mayor: Bs. {cents_to_ve(br2['close_mayor_cents'])}  |  Banco: Bs. {cents_to_ve(br2['close_banco_cents'])}").font = f_bold
    ws1.cell(r, 3, cents_to_float(br2["close_mayor_cents"])).number_format = num_fmt
    ws1.cell(r, 4, cents_to_float(br2["raw_diff_cents"])).number_format = num_fmt
    for ci in range(1, 5):
        ws1.cell(r, ci).fill = soft_fill
        ws1.cell(r, ci).border = box

    for bi in br2["items"]:
        r += 1
        ws1.cell(r, 1, f"{_fmt_date_ve(bi['date'])}  {bi['reference']}").font = f_reg
        ws1.cell(r, 2, bi["description"]).font = f_reg
        c3 = ws1.cell(r, 3, cents_to_float(bi["mayor_cents"]))
        c4 = ws1.cell(r, 4, cents_to_float(bi["diff_mayor_minus_banco_cents"]))
        c3.number_format = num_fmt
        c4.number_format = num_fmt
        for ci in range(1, 5):
            ws1.cell(r, ci).border = box

    # Signatures
    r += 4
    ws1.cell(r, 1, f"_______________________________\nElaborado por:\n{settings['prepared_by']}").alignment = Alignment(wrap_text=True, horizontal="center")
    ws1.cell(r, 2, f"_______________________________\nRevisado por:\n{settings['reviewed_by']}").alignment = Alignment(wrap_text=True, horizontal="center")
    ws1.cell(r, 4, f"_______________________________\nAprobado por:\n{settings['approved_by']}").alignment = Alignment(wrap_text=True, horizontal="center")
    ws1.row_dimensions[r].height = 42

    for ci, w in enumerate([52, 26, 28, 30], 1):
        ws1.column_dimensions[get_column_letter(ci)].width = w

    # ================= SHEET 2: CRUCES Y DIFERENCIAS =================
    ws2 = wb.create_sheet("Cruces y Diferencias")
    hdr2 = [
        "Grupo #", "Tipo de Cruce", "Fecha", "Ref. Banco", "Ref. Libros / Mayor",
        "Concepto", "Banco del Tesoro (Bs.)", "Según Libros (Bs.)", "Mayor Analítico (Bs.)",
        "Dif. Banco - Libros", "Dif. Libros - Mayor", "Observación / Diagnóstico",
    ]
    for ci, h in enumerate(hdr2, 1):
        c = ws2.cell(1, ci, h)
        c.font = f_hdr
        c.fill = navy_fill
        c.border = box
    ws2.row_dimensions[1].height = 24

    groups = conn.execute("SELECT * FROM match_groups WHERE period_id = ? ORDER BY id ASC", (period_id,)).fetchall()
    r = 1
    for g in groups:
        mems = [dict(x) for x in conn.execute("SELECT * FROM movements WHERE match_group_id = ?", (g["id"],)).fetchall()]
        if not mems:
            continue
        r += 1
        by_s = {"BANCO": [], "LIBROS": [], "MAYOR": []}
        for m in mems:
            by_s[m["source"]].append(m)
        anchor = (by_s["BANCO"] or by_s["LIBROS"] or by_s["MAYOR"])[0]
        sb = sum(x["signed_cents"] for x in by_s["BANCO"]) if by_s["BANCO"] else None
        sl = sum(x["signed_cents"] for x in by_s["LIBROS"]) if by_s["LIBROS"] else None
        sm = sum(x["signed_cents"] for x in by_s["MAYOR"]) if by_s["MAYOR"] else None
        rb = ", ".join(x["reference"] for x in by_s["BANCO"] if x["reference"])
        rlm = " | ".join(x["reference"] for x in (by_s["LIBROS"] + by_s["MAYOR"]) if x["reference"])

        vals = [
            g["id"],
            g["match_type"],
            _fmt_date_ve(anchor["tx_date"]),
            rb,
            rlm,
            anchor["description"],
            cents_to_float(sb) if sb is not None else None,
            cents_to_float(sl) if sl is not None else None,
            cents_to_float(sm) if sm is not None else None,
            f"=IF(AND(ISNUMBER(G{r}),ISNUMBER(H{r})),G{r}-H{r},0)",
            f"=IF(AND(ISNUMBER(H{r}),ISNUMBER(I{r})),H{r}-I{r},0)",
            g["note"],
        ]
        row_fill = warn_fill if g["match_type"] == "CON_DIFERENCIA" else None
        if g["resolution_type"] == "FALLA_SISTEMA":
            row_fill = sys_fill
        for ci, v in enumerate(vals, 1):
            cell = ws2.cell(r, ci, v)
            cell.font = f_reg
            cell.border = box
            if row_fill:
                cell.fill = row_fill
            if 7 <= ci <= 11:
                cell.number_format = num_fmt
                cell.alignment = Alignment(horizontal="right")

    for ci, w in enumerate([10, 18, 12, 16, 22, 38, 18, 18, 18, 18, 18, 48], 1):
        ws2.column_dimensions[get_column_letter(ci)].width = w

    # ================= SHEET 3: PARTIDAS PENDIENTES =================
    ws3 = wb.create_sheet("Partidas Pendientes")
    hdr3 = ["Origen", "Fecha", "Referencia", "Categoría", "Descripción", "Clasificación", "Monto con Signo (Bs.)", "Nota"]
    for ci, h in enumerate(hdr3, 1):
        c = ws3.cell(1, ci, h)
        c.font = f_hdr
        c.fill = navy_fill
        c.border = box
    pend_movs = conn.execute(
        "SELECT * FROM movements WHERE period_id = ? AND match_group_id IS NULL ORDER BY source, tx_date ASC",
        (period_id,),
    ).fetchall()
    r = 1
    for m in pend_movs:
        r += 1
        vals = [
            m["source"],
            _fmt_date_ve(m["tx_date"]),
            m["reference"],
            m["category"],
            m["description"],
            STATUS_LABELS.get(m["status"], m["status"]),
            cents_to_float(m["signed_cents"]),
            m["note"],
        ]
        for ci, v in enumerate(vals, 1):
            c = ws3.cell(r, ci, v)
            c.font = f_reg
            c.border = box
            if ci == 7:
                c.number_format = num_fmt
            if m["status"] == "DUPLICADO":
                c.fill = err_fill
    for ci, w in enumerate([12, 12, 18, 24, 42, 22, 20, 42], 1):
        ws3.column_dimensions[get_column_letter(ci)].width = w

    # ================= SHEET 4: FALLAS DEL SISTEMA =================
    ws4 = wb.create_sheet("Fallas del Sistema")
    hdr4 = [
        "Código", "Fecha Origen", "Módulo Afectado", "Estado", "Arrastrar Mes Sig.",
        "Descripción", "Monto Banco (Bs.)", "Monto Según Libros (Bs.)", "Monto Mayor Analítico (Bs.)",
        "Ajuste Libros (Bs.)", "Ajuste Mayor (Bs.)", "Nota Técnica / Soporte Auditoría",
    ]
    for ci, h in enumerate(hdr4, 1):
        c = ws4.cell(1, ci, h)
        c.font = f_hdr
        c.fill = navy_fill
        c.border = box
    issues = conn.execute("SELECT * FROM system_issues WHERE period_id = ? ORDER BY issue_date ASC", (period_id,)).fetchall()
    r = 1
    for iss in issues:
        r += 1
        vals = [
            iss["code"],
            _fmt_date_ve(iss["issue_date"]),
            iss["affected_module"],
            iss["status"],
            "Sí" if iss["carry_forward"] else "No",
            iss["title"],
            cents_to_float(iss["banco_cents"]) if iss["banco_cents"] is not None else None,
            cents_to_float(iss["libros_cents"]) if iss["libros_cents"] is not None else None,
            cents_to_float(iss["mayor_cents"]) if iss["mayor_cents"] is not None else None,
            cents_to_float(iss["adj_libros_cents"]),
            cents_to_float(iss["adj_mayor_cents"]),
            iss["audit_note"],
        ]
        for ci, v in enumerate(vals, 1):
            c = ws4.cell(r, ci, v)
            c.font = f_reg
            c.fill = sys_fill
            c.border = box
            if 7 <= ci <= 11:
                c.number_format = num_fmt
    for ci, w in enumerate([14, 13, 16, 14, 16, 38, 18, 20, 22, 18, 18, 52], 1):
        ws4.column_dimensions[get_column_letter(ci)].width = w

    # ================= SHEET 5: AJUSTES SUGERIDOS =================
    ws5 = wb.create_sheet("Ajustes Sugeridos")
    hdr5 = ["Fecha", "Aplicar En", "Referencia", "Concepto del Ajuste", "Cuenta DEBE", "Cuenta HABER", "Monto (Bs.)", "Estado", "Detalle"]
    for ci, h in enumerate(hdr5, 1):
        c = ws5.cell(1, ci, h)
        c.font = f_hdr
        c.fill = navy_fill
        c.border = box
    adjs = conn.execute("SELECT * FROM adjustments WHERE period_id = ? ORDER BY adj_date ASC, id ASC", (period_id,)).fetchall()
    r = 1
    for a in adjs:
        r += 1
        vals = [
            _fmt_date_ve(a["adj_date"]),
            TARGET_LABELS.get(a["target"], a["target"]),
            a["reference"],
            a["concept"],
            a["debit_account"],
            a["credit_account"],
            cents_to_float(a["amount_cents"]),
            "Aplicado" if a["applied"] else "Pendiente",
            a["note"],
        ]
        for ci, v in enumerate(vals, 1):
            c = ws5.cell(r, ci, v)
            c.font = f_reg
            c.border = box
            if ci == 7:
                c.number_format = num_fmt
    if r >= 2:
        r += 1
        ws5.cell(r, 6, "TOTAL AJUSTES (Bs.):").font = f_bold
        ct = ws5.cell(r, 7, f"=SUM(G2:G{r-1})")
        ct.font = f_bold
        ct.number_format = num_fmt
        ct.border = total_border
    for ci, w in enumerate([12, 24, 16, 42, 36, 36, 18, 14, 44], 1):
        ws5.column_dimensions[get_column_letter(ci)].width = w

    # ================= SHEET 6: MOVIMIENTOS COMPLETOS =================
    ws6 = wb.create_sheet("Movimientos Completos")
    hdr6 = ["ID", "Fuente", "Fecha", "Referencia", "Categoría", "Descripción", "Entrada / Debe (Bs.)", "Salida / Haber (Bs.)", "Estado", "Grupo #"]
    for ci, h in enumerate(hdr6, 1):
        c = ws6.cell(1, ci, h)
        c.font = f_hdr
        c.fill = navy_fill
        c.border = box
    all_m = conn.execute("SELECT * FROM movements WHERE period_id = ? ORDER BY source, tx_date, id", (period_id,)).fetchall()
    r = 1
    for m in all_m:
        r += 1
        vals = [
            m["id"],
            m["source"],
            _fmt_date_ve(m["tx_date"]),
            m["reference"],
            m["category"],
            m["description"],
            cents_to_float(m["amount_cents"]) if m["direction"] == "IN" else 0.0,
            cents_to_float(m["amount_cents"]) if m["direction"] == "OUT" else 0.0,
            STATUS_LABELS.get(m["status"], m["status"]),
            m["match_group_id"] or "",
        ]
        for ci, v in enumerate(vals, 1):
            c = ws6.cell(r, ci, v)
            c.font = f_reg
            c.border = box
            if ci in (7, 8):
                c.number_format = num_fmt
    for ci, w in enumerate([8, 11, 12, 18, 24, 42, 18, 18, 22, 10], 1):
        ws6.column_dimensions[get_column_letter(ci)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_reconciliation_pdf(conn: sqlite3.Connection, period_id: int) -> bytes:
    """Generate a formal, signable Acta de Conciliación Bancaria (PDF) with ReportLab (two-phase)."""
    sheet_data = compute_reconciliation_sheet(conn, period_id)
    settings = get_settings(conn)
    p = sheet_data["period"]
    use_mayor = sheet_data["use_mayor"]

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.55 * inch,
        title=f"Acta de Conciliación Bancaria - {p['label']}",
    )

    NAVY = colors.HexColor("#0F2942")
    CRIMSON = colors.HexColor("#B42318")
    SLATE = colors.HexColor("#334155")
    LIGHT_BG = colors.HexColor("#F1F5F9")
    FASE_BG = colors.HexColor("#DCE6F1")
    OK_BG = colors.HexColor("#ECFDF3")
    OK_TXT = colors.HexColor("#067647")
    SYS_BG = colors.HexColor("#EEF2FF")
    BORDER = colors.HexColor("#CBD5E1")

    styles = getSampleStyleSheet()
    s_title = ParagraphStyle("TitleBT", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=13, textColor=colors.white, leading=16)
    s_sub = ParagraphStyle("SubBT", parent=styles["Normal"], fontName="Helvetica", fontSize=8.5, textColor=colors.HexColor("#E2E8F0"), leading=11)
    s_h2 = ParagraphStyle("H2BT", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=9.5, textColor=NAVY, spaceBefore=10, spaceAfter=4)
    s_cell = ParagraphStyle("CellBT", parent=styles["Normal"], fontName="Helvetica", fontSize=7.5, textColor=SLATE, leading=9.5)
    s_cell_b = ParagraphStyle("CellBoldBT", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=7.5, textColor=NAVY, leading=9.5)
    s_sig = ParagraphStyle("SigBT", parent=styles["Normal"], fontName="Helvetica", fontSize=7.5, textColor=SLATE, alignment=1, leading=10)

    story: List = []

    # Header banner
    hdr_table = Table(
        [
            [
                Paragraph(f"ACTA DE CONCILIACIÓN BANCARIA — {p['bank_name'].upper()}", s_title),
                Paragraph(f"<b>PERÍODO: {p['label'].upper()}</b><br/>Estado: {p['status']}", s_sub),
            ],
            [
                Paragraph(f"{settings['company_name']}  ·  RIF: {settings['company_rif']}", s_sub),
                Paragraph(f"Emisión: {datetime.now().strftime('%d/%m/%Y %H:%M')}", s_sub),
            ],
        ],
        colWidths=[5.1 * inch, 2.4 * inch],
    )
    hdr_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), NAVY),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, 0), 8),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 8),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LINEBELOW", (0, -1), (-1, -1), 2.5, CRIMSON),
            ]
        )
    )
    story.append(hdr_table)
    story.append(Spacer(1, 6))

    # Account metadata strip
    meta_t = Table(
        [
            [
                Paragraph(f"<b>Cuenta:</b> {p['account_name']}", s_cell),
                Paragraph(f"<b>Nro. Cuenta:</b> {p['account_number']}", s_cell),
                Paragraph(f"<b>Cta. Contable:</b> {p['ledger_code']} ({p['currency']})", s_cell),
            ]
        ],
        colWidths=[2.6 * inch, 2.5 * inch, 2.4 * inch],
    )
    meta_t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
                ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
                ("PADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(meta_t)

    # Section 1: Phased Summary Schedule
    story.append(Paragraph("1. CUADRO RESUMEN DE CONCILIACIÓN EN DOS FASES (EXPRESADO EN BOLÍVARES)", s_h2))

    def f_amt(c) -> str:
        if c is None:
            return "—"
        if c == 0:
            return "—"
        return cents_to_ve(c)

    sum_rows = [["Concepto", "Banco del Tesoro", "Según Libros\n(Tesorería)", "Mayor Analítico\n(Contabilidad)"]]
    for row in sheet_data["rows"]:
        if row["kind"] == "fase":
            sum_rows.append([row["label"], "", "", ""])
        else:
            sum_rows.append([
                row["label"],
                f_amt(row.get("banco_cents")),
                f_amt(row.get("libros_cents")),
                f_amt(row.get("mayor_cents")) if use_mayor else "N/A",
            ])
    sum_rows.append([
        "DIFERENCIA SIN CONCILIAR (Vs. Banco)",
        "0,00",
        cents_to_ve(sheet_data["summary"]["diff_banco_libros_cents"]),
        cents_to_ve(sheet_data["summary"]["diff_banco_mayor_cents"]) if use_mayor else "N/A",
    ])

    t1 = Table(sum_rows, colWidths=[3.3 * inch, 1.4 * inch, 1.4 * inch, 1.4 * inch])
    is_bal = sheet_data["summary"]["is_balanced"]
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("ALIGN", (1, 0), (-1, 0), "CENTER"),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("TEXTCOLOR", (1, -1), (-1, -1), OK_TXT if is_bal else CRIMSON),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ]
    # Style fase/subtotal/total rows by index
    for idx, row in enumerate(sheet_data["rows"], start=1):
        if row["kind"] == "fase":
            style_cmds.append(("BACKGROUND", (0, idx), (-1, idx), FASE_BG))
            style_cmds.append(("FONTNAME", (0, idx), (-1, idx), "Helvetica-Bold"))
            style_cmds.append(("SPAN", (0, idx), (-1, idx)))
        elif row["kind"] == "saldo":
            style_cmds.append(("BACKGROUND", (0, idx), (-1, idx), LIGHT_BG))
            style_cmds.append(("FONTNAME", (0, idx), (-1, idx), "Helvetica-Bold"))
        elif row["kind"] == "subtotal":
            style_cmds.append(("BACKGROUND", (0, idx), (-1, idx), colors.HexColor("#E0F2FE")))
            style_cmds.append(("FONTNAME", (0, idx), (-1, idx), "Helvetica-Bold"))
        elif row["kind"] == "total":
            style_cmds.append(("BACKGROUND", (0, idx), (-1, idx), OK_BG))
            style_cmds.append(("TEXTCOLOR", (0, idx), (-1, idx), OK_TXT))
            style_cmds.append(("FONTNAME", (0, idx), (-1, idx), "Helvetica-Bold"))
            style_cmds.append(("LINEABOVE", (0, idx), (-1, idx), 1.2, NAVY))
    t1.setStyle(TableStyle(style_cmds))
    story.append(t1)

    # Section 2: Bridge Fase 1 (Libros vs Mayor)
    br = sheet_data["fase1"]
    if use_mayor and br["items"]:
        story.append(
            Paragraph(
                f"2. FASE 1 — DIFERENCIAS EXACTAS SEGÚN LIBROS VS. MAYOR ANALÍTICO (Dif. Bruta: Bs. {cents_to_ve(br['raw_diff_cents'])})",
                s_h2,
            )
        )
        b_rows = [["Fecha / Ref.", "Detalle de la Diferencia (Tesorería vs. Contabilidad)", "Impacto Bs."]]
        for bi in br["items"]:
            b_rows.append([
                Paragraph(f"{_fmt_date_ve(bi['date'])}<br/><b>{bi['reference']}</b>", s_cell),
                Paragraph(bi["description"], s_cell),
                cents_to_ve(bi["diff_libros_minus_mayor_cents"], show_plus=True),
            ])
        b_rows.append([
            "",
            Paragraph("<b>TOTAL DIFERENCIA EXPLICADA (Según Libros − Mayor Analítico)</b>", s_cell_b),
            cents_to_ve(br["explained_diff_cents"], show_plus=True),
        ])
        tb = Table(b_rows, colWidths=[1.25 * inch, 4.95 * inch, 1.3 * inch])
        tb.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                    ("ALIGN", (2, 1), (2, -1), "RIGHT"),
                    ("FONTNAME", (2, -1), (2, -1), "Helvetica-Bold"),
                    ("BACKGROUND", (0, -1), (-1, -1), LIGHT_BG),
                    ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("PADDING", (0, 0), (-1, -1), 3.5),
                ]
            )
        )
        story.append(tb)

    # Section 3: Bridge Fase 2 (Mayor vs Banco)
    br2 = sheet_data["fase2"]
    if br2["items"]:
        story.append(
            Paragraph(
                f"3. FASE 2 — DIFERENCIAS EXACTAS MAYOR ANALÍTICO VS. BANCO DEL TESORO (Dif. Bruta: Bs. {cents_to_ve(br2['raw_diff_cents'])})",
                s_h2,
            )
        )
        b_rows = [["Fecha / Ref.", "Detalle de la Diferencia (Contabilidad vs. Banco)", "Impacto Bs."]]
        for bi in br2["items"]:
            b_rows.append([
                Paragraph(f"{_fmt_date_ve(bi['date'])}<br/><b>{bi['reference']}</b>", s_cell),
                Paragraph(bi["description"], s_cell),
                cents_to_ve(bi["diff_mayor_minus_banco_cents"], show_plus=True),
            ])
        b_rows.append([
            "",
            Paragraph("<b>TOTAL DIFERENCIA EXPLICADA (Mayor Analítico − Banco del Tesoro)</b>", s_cell_b),
            cents_to_ve(br2["explained_diff_cents"], show_plus=True),
        ])
        tb = Table(b_rows, colWidths=[1.25 * inch, 4.95 * inch, 1.3 * inch])
        tb.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                    ("ALIGN", (2, 1), (2, -1), "RIGHT"),
                    ("FONTNAME", (2, -1), (2, -1), "Helvetica-Bold"),
                    ("BACKGROUND", (0, -1), (-1, -1), LIGHT_BG),
                    ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("PADDING", (0, 0), (-1, -1), 3.5),
                ]
            )
        )
        story.append(tb)

    # Section 4: Fallas del Sistema
    issues = conn.execute(
        "SELECT * FROM system_issues WHERE period_id = ? AND status != 'RESUELTA' ORDER BY issue_date ASC",
        (period_id,),
    ).fetchall()
    if issues:
        story.append(Paragraph("4. REGISTRO DE FALLAS DEL SISTEMA DOCUMENTADAS (SOPORTE DE AUDITORÍA)", s_h2))
        i_rows = [["Código / Fecha", "Módulo", "Detalle y Nota Técnica", "Ajuste Concil. Bs."]]
        for iss in issues:
            adj_val = iss["adj_libros_cents"] if iss["adj_libros_cents"] != 0 else iss["adj_mayor_cents"]
            i_rows.append([
                Paragraph(f"<b>{iss['code']}</b><br/>{_fmt_date_ve(iss['issue_date'])}", s_cell),
                Paragraph(f"{iss['affected_module']}<br/>({iss['status']})", s_cell),
                Paragraph(f"<b>{iss['title']}</b><br/>{iss['audit_note']}", s_cell),
                cents_to_ve(adj_val, show_plus=True),
            ])
        ti = Table(i_rows, colWidths=[1.1 * inch, 0.9 * inch, 4.3 * inch, 1.2 * inch])
        ti.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("BACKGROUND", (0, 1), (-1, -1), SYS_BG),
                    ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                    ("ALIGN", (3, 1), (3, -1), "RIGHT"),
                    ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("PADDING", (0, 0), (-1, -1), 3.5),
                ]
            )
        )
        story.append(ti)

    # Section 5: Ajustes Contables y de Tesorería Sugeridos
    adjs = conn.execute("SELECT * FROM adjustments WHERE period_id = ? ORDER BY adj_date ASC", (period_id,)).fetchall()
    if adjs:
        story.append(Paragraph("5. AJUSTES CONTABLES Y DE TESORERÍA REQUERIDOS", s_h2))
        a_rows = [["Fecha / Destino", "Concepto", "Cuentas (Debe / Haber)", "Monto Bs."]]
        for a in adjs:
            a_rows.append([
                Paragraph(f"{_fmt_date_ve(a['adj_date'])}<br/><b>{TARGET_LABELS.get(a['target'], a['target'])}</b>", s_cell),
                Paragraph(a["concept"], s_cell),
                Paragraph(f"<b>D:</b> {a['debit_account']}<br/><b>H:</b> {a['credit_account']}", s_cell),
                cents_to_ve(a["amount_cents"]),
            ])
        ta = Table(a_rows, colWidths=[1.35 * inch, 2.55 * inch, 2.5 * inch, 1.1 * inch])
        ta.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 7.2),
                    ("ALIGN", (3, 1), (3, -1), "RIGHT"),
                    ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("PADDING", (0, 0), (-1, -1), 3.5),
                ]
            )
        )
        story.append(ta)

    if p.get("notes"):
        story.append(Paragraph("OBSERVACIONES DEL PERÍODO", s_h2))
        story.append(Paragraph(p["notes"], s_cell))

    # Signatures
    story.append(Spacer(1, 26))
    sig_t = Table(
        [
            [
                Paragraph(f"_____________________________<br/><b>Elaborado por</b><br/>{settings['prepared_by']}", s_sig),
                Paragraph(f"_____________________________<br/><b>Revisado por</b><br/>{settings['reviewed_by']}", s_sig),
                Paragraph(f"_____________________________<br/><b>Aprobado por</b><br/>{settings['approved_by']}", s_sig),
            ]
        ],
        colWidths=[2.5 * inch, 2.5 * inch, 2.5 * inch],
    )
    story.append(KeepTogether([HRFlowable(width="100%", thickness=0.4, color=BORDER), Spacer(1, 18), sig_t]))

    doc.build(story)
    return buf.getvalue()
