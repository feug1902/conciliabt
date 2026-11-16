from pathlib import Path
from typing import List, Tuple

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .database import BASE_DIR, auto_categorize, cents_to_ve, extract_ref_tail, get_conn, period_label
from .reconciler import run_auto_reconciliation

SAMPLES_DIR = BASE_DIR / "ejemplos_pdf"
try:
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    # Read-only filesystem (e.g. Vercel serverless): sample PDFs are optional
    pass

# Exact movements (date_ddmmyyyy, ref, desc, dir, amount_cents)
BANCO_MOVS: List[Tuple[str, str, str, str, int]] = [
    ("03/10/2026", "004589201411", "NC TRANSF RECIBIDA SUMINISTROS ORINOCO", "IN", 42000000),
    ("06/10/2026", "009812445502", "ND PAGO DIRECTO PROVEEDOR TECNOSISTEMAS", "OUT", 20030000),
    ("10/10/2026", "009812510833", "ND LOTE ASIGNACION BOLSA DE TRABAJO OCT", "OUT", 14560000),
    ("14/10/2026", "009812599120", "ND NOMINA 1RA QUINCENA OCTUBRE BT", "OUT", 31000000),
    ("16/10/2026", "009812634789", "ND TRANSF TERCEROS SERVICIOS AVILA CA", "OUT", 4875080),
    ("19/10/2026", "004589774310", "NC ABONO APORTE OPERATIVO MENSUAL", "IN", 52500000),
    ("21/10/2026", "009812701290", "ND PAGO DIRECTO PAPELERIA CARACAS", "OUT", 3240000),
    ("23/10/2026", "009812755001", "ND TRANSF OTROS BANCOS FERRETERIA CENTRAL", "OUT", 1890000),
    ("23/10/2026", "009812755002", "NC REVERSO TRANSF RECHAZADA FERRETERIA CENTRAL", "IN", 1890000),
    ("26/10/2026", "009812812344", "ND PAGO SERVICIO TRANSPORTE LOGISTICA MIRANDA", "OUT", 6450000),
    ("28/10/2026", "000010289901", "ND COMISION MANTENIMIENTO Y TRANSF BT", "OUT", 124550),
    ("28/10/2026", "000010289902", "ND COMISION EMISION LOTE NOMINA BT", "OUT", 62000),
    ("29/10/2026", "000010294410", "NC LIQUIDACION INTERESES MES OCTUBRE", "IN", 315000),
]

LIBROS_MOVS: List[Tuple[str, str, str, str, int]] = [
    ("03/10/2026", "201411", "Cobro Factura #1084 Suministros Orinoco", "IN", 42000000),
    ("06/10/2026", "445502", "Pago directo compra insumos Tecnosistemas OP-312", "OUT", 20130000),  # 201.300,00 vs 200.300,00!
    ("10/10/2026", "510833", "Asignacion Bolsa de Trabajo personal Octubre", "OUT", 14560000),
    ("14/10/2026", "599120", "Nomina 1ra Quincena Octubre 2026", "OUT", 31000000),
    ("16/10/2026", "634789", "Pago directo mantenimiento Servicios Avila OP-328", "OUT", 4875040),  # dif centimos Bs. 0,40
    ("19/10/2026", "774310", "Recepcion Aporte Operativo mensual Octubre", "IN", 52500000),
    ("21/10/2026", "701290", "Compra directa papeleria y oficina OP-340", "OUT", 3240000),
    ("22/10/2026", "701290-D", "Compra directa papeleria y oficina OP-340", "OUT", 3240000),  # Duplicado en Libros!
    ("26/10/2026", "812344", "Pago transporte Logistica Miranda OP-355", "OUT", 6450000),  # Falta en Mayor!
    ("30/10/2026", "OP-369", "Pago proveedor Inversiones El Avila OP-369", "OUT", 5500000),  # En transito
    ("31/10/2026", "REC-891", "Cobro cierre de mes Comercializadora Sur REC-891", "IN", 2800000),  # En transito
]

MAYOR_MOVS: List[Tuple[str, str, str, str, int]] = [
    ("03/10/2026", "COMP-1001/201411", "Ingreso cobranza Fact 1084 Suministros Orinoco", "IN", 42000000),
    ("06/10/2026", "ASI-2045/445502", "Pago directo compra insumos Tecnosistemas OP-312", "OUT", 20030000),  # 200.300,00!
    ("10/10/2026", "ASI-2058/510833", "Asignacion Bolsa de Trabajo Octubre 2026", "OUT", 14560000),
    ("14/10/2026", "ASI-2071/599120", "Nomina personal 1ra Quincena Octubre", "OUT", 31000000),
    ("16/10/2026", "ASI-2080/634789", "Servicio mantenimiento Servicios Avila OP-328", "OUT", 4875040),
    ("19/10/2026", "COMP-1042/774310", "Abono aporte operativo mensual Octubre", "IN", 52500000),
    ("21/10/2026", "ASI-2095/701290", "Compra directa papeleria y oficina OP-340", "OUT", 3240000),
    ("30/10/2026", "ASI-2120/OP-369", "Pago proveedor Inversiones El Avila OP-369", "OUT", 5500000),
    ("31/10/2026", "COMP-1078/REC-891", "Cobro cierre Comercializadora Sur REC-891", "IN", 2800000),
]

OPEN_BAL_CENTS = 85000000  # Bs. 850.000,00


def _build_statement_pdf(
    out_path: Path,
    title: str,
    subtitle: str,
    col_headers: List[str],
    movs: List[Tuple[str, str, str, str, int]],
    is_bank: bool,
    accent_hex: str,
):
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=letter,
        leftMargin=0.45 * inch,
        rightMargin=0.45 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
    )
    styles = getSampleStyleSheet()
    st_t = ParagraphStyle("T", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=12, textColor=colors.HexColor(accent_hex))
    st_s = ParagraphStyle("S", parent=styles["Normal"], fontName="Helvetica", fontSize=8.5, textColor=colors.HexColor("#334155"), leading=12)

    story = [
        Paragraph(title, st_t),
        Spacer(1, 3),
        Paragraph(subtitle, st_s),
        Spacer(1, 8),
    ]

    bal = OPEN_BAL_CENTS
    table_data = [
        col_headers,
        ["01/10/2026", "", "SALDO INICIAL AL 01/10/2026", "", "", cents_to_ve(bal)],
    ]

    tot_c4 = 0
    tot_c5 = 0
    for dt, ref, desc, direction, amt in movs:
        bal += amt if direction == "IN" else -amt
        if is_bank:
            # Col 4 = Débito (OUT), Col 5 = Crédito (IN)
            c4 = cents_to_ve(amt) if direction == "OUT" else ""
            c5 = cents_to_ve(amt) if direction == "IN" else ""
            if direction == "OUT":
                tot_c4 += amt
            else:
                tot_c5 += amt
        else:
            # Col 4 = Debe (IN), Col 5 = Haber (OUT)
            c4 = cents_to_ve(amt) if direction == "IN" else ""
            c5 = cents_to_ve(amt) if direction == "OUT" else ""
            if direction == "IN":
                tot_c4 += amt
            else:
                tot_c5 += amt
        table_data.append([dt, ref, desc, c4, c5, cents_to_ve(bal)])

    table_data.append(["", "", "TOTALES DEL PERIODO", cents_to_ve(tot_c4), cents_to_ve(tot_c5), ""])
    table_data.append(["31/10/2026", "", "SALDO FINAL AL 31/10/2026", "", "", cents_to_ve(bal)])

    tbl = Table(
        table_data,
        colWidths=[0.82 * inch, 1.18 * inch, 2.55 * inch, 0.98 * inch, 0.98 * inch, 1.09 * inch],
    )
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(accent_hex)),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.2),
                ("ALIGN", (3, 1), (5, -1), "RIGHT"),
                ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#F1F5F9")),
                ("BACKGROUND", (0, -2), (-1, -1), colors.HexColor("#F1F5F9")),
                ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
                ("FONTNAME", (0, -2), (-1, -1), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5E1")),
                ("TOPPADDING", (0, 0), (-1, -1), 3.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
            ]
        )
    )
    story.append(tbl)
    doc.build(story)
    return out_path


def generate_sample_pdfs() -> dict:
    """Best-effort generation of the 3 example PDFs (skipped on read-only filesystems)."""
    try:
        p_banco = _build_statement_pdf(
            SAMPLES_DIR / "01_Estado_Cuenta_Banco_del_Tesoro_Oct2026.pdf",
            "BANCO DEL TESORO, C.A. BANCO UNIVERSAL — ESTADO DE CUENTA",
            "Cliente: Empresa Venezolana de Servicios y Suministros, C.A.  |  RIF: J-31245890-4<br/>"
            "Cuenta Corriente Nro.: 0163-0900-12-0010458921  |  Moneda: Bolívares (Bs.)  |  Período: 01/10/2026 al 31/10/2026",
            ["Fecha", "Referencia", "Descripción", "Débito", "Crédito", "Saldo"],
            BANCO_MOVS,
            is_bank=True,
            accent_hex="#0F2942",
        )
        p_libros = _build_statement_pdf(
            SAMPLES_DIR / "02_Segun_Libros_Tesoreria_Oct2026.pdf",
            "SISTEMA INTEGRADO DE TESORERÍA — MOVIMIENTO SEGÚN LIBROS",
            "Banco: 0163 - Banco del Tesoro  |  Cuenta: 0163-0900-12-0010458921  |  Expresado en Bs.<br/>"
            "Desde: 01/10/2026  Hasta: 31/10/2026  |  Módulo: Control Bancario (Según Libros)",
            ["Fecha", "Documento", "Concepto", "Debe", "Haber", "Saldo"],
            LIBROS_MOVS,
            is_bank=False,
            accent_hex="#1E3A5F",
        )
        p_mayor = _build_statement_pdf(
            SAMPLES_DIR / "03_Mayor_Analitico_Contabilidad_Oct2026.pdf",
            "MÓDULO DE CONTABILIDAD — MAYOR ANALÍTICO DE CUENTA",
            "Cuenta Contable: 1.1.01.02.001 - Banco del Tesoro Cta. Cte. 8921  |  Moneda: Bs.<br/>"
            "Período Contable: Octubre 2026 (01/10/2026 al 31/10/2026)",
            ["Fecha", "Comprobante", "Descripción", "Debe", "Haber", "Saldo"],
            MAYOR_MOVS,
            is_bank=False,
            accent_hex="#334155",
        )
        return {"BANCO": p_banco, "LIBROS": p_libros, "MAYOR": p_mayor}
    except OSError:
        return {}


def _iso_from_ddmmyyyy(s: str) -> str:
    d, m, y = s.split("/")
    return f"{y}-{m}-{d}"


def ensure_demo_seeded(force: bool = False):
    """Populate the initial demo period (if DB is empty or force=True). Filesystem-independent."""
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"]
        if count > 0 and not force:
            return

        if force:
            conn.execute("DELETE FROM accounts WHERE account_number = '0163-0900-12-0010458921'")

        cur = conn.execute(
            """
            INSERT INTO accounts (name, bank_name, account_number, account_type, ledger_code, currency)
            VALUES (?, 'Banco del Tesoro', ?, 'Corriente', '1.1.01.02.001', 'Bs.')
            """,
            ("Cuenta Operativa Principal BT (Demo)", "0163-0900-12-0010458921"),
        )
        account_id = cur.lastrowid

        pcur = conn.execute(
            """
            INSERT INTO periods (account_id, year, month, label, status, use_mayor, is_demo, notes)
            VALUES (?, 2026, 10, ?, 'EN_REVISION', 1, 1, ?)
            """,
            (
                account_id,
                period_label(2026, 10),
                "Período de demostración: incluye diferencia de Bs. 1.000,00 entre Según Libros (201.300,00) y Mayor Analítico (200.300,00) en OP-312, diferencia de céntimos (0,40), registro duplicado, reverso bancario, comisiones BT y partidas en tránsito. Procedimiento en dos fases: Fase 1 Libros↔Mayor, Fase 2 Mayor↔Banco.",
            ),
        )
        pid = pcur.lastrowid

        sources = {"BANCO": BANCO_MOVS, "LIBROS": LIBROS_MOVS, "MAYOR": MAYOR_MOVS}
        for src, movs in sources.items():
            bal = OPEN_BAL_CENTS
            for dt, ref, desc, direction, amt in movs:
                signed = amt if direction == "IN" else -amt
                bal += signed
                conn.execute(
                    """
                    INSERT INTO movements
                        (period_id, source, tx_date, reference, ref_tail, description, category,
                         direction, amount_cents, signed_cents, running_balance_cents, status, origin_file)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDIENTE', ?)
                    """,
                    (
                        pid,
                        src,
                        _iso_from_ddmmyyyy(dt),
                        ref,
                        extract_ref_tail(ref),
                        desc,
                        auto_categorize(desc, direction, src),
                        direction,
                        amt,
                        signed,
                        bal,
                        f"Ejemplo {src} Oct-2026",
                    ),
                )
            col_o = f"open_{src.lower()}_cents"
            col_c = f"close_{src.lower()}_cents"
            conn.execute(
                f"UPDATE periods SET {col_o} = ?, {col_c} = ? WHERE id = ?",
                (OPEN_BAL_CENTS, bal, pid),
            )

        run_auto_reconciliation(conn, pid, reset_existing=False)

    # Best effort: also drop the example PDFs for download (local dev only)
    generate_sample_pdfs()
