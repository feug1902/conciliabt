# ConciliaBT — Conciliación Bancaria Banco del Tesoro, Tesorería y Contabilidad

App web para conciliar **triple vía**: **Banco del Tesoro (Estado de Cuenta)** ↔ **Según Libros (Tesorería)** ↔ **Mayor Analítico (Contabilidad)**, con detección automática de **diferencias de céntimos, errores de dígito (ej. 201.300 vs 200.300), transposiciones, duplicados, reversos, comisiones/IGTF** y generación de **ajustes contables, Excel y Acta PDF firmable**.

## Cómo arrancar

```bash
cd conciliabt
bash run.sh            # instala dependencias si faltan y levanta en http://0.0.0.0:8000
```

O manual:

```bash
pip install -r requirements.txt
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Abre el navegador en la URL del servidor. La base de datos (SQLite) se crea sola en `data/conciliabt.db`.

## Despliegue en producción (Vercel + Supabase)

La app está lista para publicarse en **Vercel** (web/PWA) con base de datos **Supabase PostgreSQL**:

1. **Base de datos**: crea un proyecto en Supabase y ejecuta el esquema de `app/database.py` (tabla `POSTGRES_DDL`) o usa la migración `create_conciliabt_tables`. Define la variable de entorno **`DATABASE_URL`** con la cadena de conexión del pooler (modo sesión, puerto 5432):
   `postgresql://postgres.<REF>:<PASSWORD>@aws-1-us-east-1.pooler.supabase.com:5432/postgres?sslmode=require`
   Si `DATABASE_URL` no está definida, la app usa SQLite local automáticamente.
2. **Web**: despliega esta carpeta en Vercel (`vercel.json` ya está incluido: función Python en `api/index.py`, frontend en `public/`, rewrites `/api/*`). Comando: `vercel --prod` (o importa la carpeta en el dashboard). Define `DATABASE_URL` en las variables del proyecto.
3. **PIN opcional**: define `APP_PIN` para proteger el acceso (la app pedirá el PIN en el navegador).
4. **Android/PWA**: la app es una PWA instalable (manifest + service worker + íconos en `public/icons/`). En Android Chrome: menú ⋮ → «Añadir a pantalla de inicio».

## Flujo de trabajo diario

1. **Panel General** — Ve los 3 saldos de cierre, el saldo conciliado, el puente Libros↔Mayor y el chequeo de integridad de cada fuente.
2. **Cargar PDFs** — Selecciona la fuente (Banco / Libros / Mayor), arrastra el **PDF digital** (no escaneado), revisa la vista previa (fechas, referencias, montos, saldos detectados y advertencias) y confirma la importación. También acepta Excel/CSV y **texto pegado** si el PDF es escaneado.
3. **Mesa de Conciliación** — Ejecuta **Autoconciliar** (cruce inteligente por referencia parcial, monto, fecha ±5 días y descripción). Marca movimientos en las 3 columnas para **conciliar manualmente** (soporta 1-a-muchos/lotes y reversos). Las filas rosadas son posibles duplicados.
4. **Descuadres y Fallas** — Confirma duplicados, revisa diferencias de monto y el **registro de fallas del sistema** (con soporte de auditoría y arrastre al mes siguiente).
5. **Hoja y Ajustes** — Hoja de conciliación formal, **ajustes contables sugeridos** listos para contabilizar (comisiones, IGTF, intereses, reversos de duplicados, faltantes en Mayor, céntimos) y partidas pendientes.
6. **Exportar** — **Excel** (6 hojas: Hoja de Conciliación, Cruces y Diferencias, Partidas Pendientes, Fallas del Sistema, Ajustes Sugeridos, Movimientos Completos) y **Acta PDF** con firmas.

## Datos de ejemplo

Al arrancar se carga automáticamente el período **Octubre 2026** con 3 PDFs de ejemplo en `ejemplos_pdf/` que incluyen:

- Diferencia **Bs. 1.000,00** entre Según Libros (201.300,00) y Mayor Analítico (200.300,00) en OP-312 → detectada como **falla del sistema**.
- Diferencia de **céntimos (Bs. 0,40)** en Servicios Avila.
- **Registro duplicado** en Según Libros (papelería OP-340) → aparece como posible duplicado para confirmar.
- **Reverso** bancario (Ferretería Central), **comisiones BT** e **intereses** sin registrar.
- Partidas **en tránsito** (OP-369, REC-891) y una partida de Tesorería **faltante en Mayor** (transporte 64.500,00).

Puedes descargar esos PDFs desde la vista «Cargar PDFs» y re-subirlos para probar el extractor. Para restaurar el ejemplo: **Cuentas y Ajustes App → Restablecer datos de ejemplo**.

## Cierre de mes

**Panel General → Cerrar y abrir mes siguiente**: cierra el período, abre el siguiente con los saldos de cierre como iniciales y **arrastra automáticamente** las partidas en tránsito (que se cruzan solas cuando el Banco las hace efectivas) y las fallas del sistema activas.

## Estructura

```
conciliabt/
├── run.sh                  # Arranque rápido
├── requirements.txt
├── app/
│   ├── main.py             # API FastAPI (cuentas, períodos, import, cruce, fallas, ajustes, exports)
│   ├── database.py         # SQLite + utilidades de montos Bs. (céntimos enteros) y categorías
│   ├── pdf_parser.py       # Extractor PDF (tablas + coordenadas + texto) y Excel/CSV
│   ├── reconciler.py       # Cruce inteligente triple vía + hoja de conciliación + ajustes
│   ├── exporters.py        # Excel (.xlsx) y Acta PDF
│   └── seed_and_samples.py # PDFs de ejemplo y datos demo
├── static/                 # Frontend (HTML/CSS/JS vanilla)
├── ejemplos_pdf/           # 3 PDFs de ejemplo Octubre 2026
└── data/conciliabt.db      # Base de datos (se crea sola)
```

## Notas técnicas

- Los montos se guardan como **céntimos enteros** para evitar errores de redondeo; se muestran en formato venezolano `1.234,56`.
- El extractor valida la dirección (Entrada/Salida) contra la columna de saldo del propio PDF y avisa si `Inicial + Movimientos ≠ Final`.
- El cruce usa: últimos dígitos de referencia (Banco del Tesoro trae referencias largas, la empresa cortas), códigos OP-/REC-, similitud de descripción, ventana de fechas y clasificación de la diferencia (céntimos ≤ Bs. 1,00, error de dígito, transposición, coma decimal).
