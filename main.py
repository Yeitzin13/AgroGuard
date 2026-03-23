from fastapi import FastAPI, HTTPException, Depends, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from datetime import date, datetime
import httpx
import sqlite3
import os
import base64
import io
from PIL import Image
from fpdf import FPDF

# ─────────────────────────────────────────────
# Configuración de la aplicación
# ─────────────────────────────────────────────
app = FastAPI(
    title="AgroGuard API",
    description="Sistema de gestión y consulta de plagas agrícolas",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # En producción, reemplaza con tu dominio
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────
# FASE 3 — Base de datos SQLite
# ─────────────────────────────────────────────
DB_PATH = "agroguard.db"

def get_connection():
    """Abre una conexión a SQLite con row_factory para obtener dicts."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Crea la tabla si no existe y agrega columnas nuevas si faltan."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bitacora_plagas (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre_plaga      TEXT    NOT NULL,
            nombre_cientifico TEXT    NOT NULL,
            familia           TEXT    NOT NULL,
            reino             TEXT    NOT NULL,
            riesgo            TEXT    NOT NULL,
            ficha_tecnica     TEXT    NOT NULL,
            fecha             TEXT    NOT NULL,
            latitud           REAL,
            longitud          REAL
        )
    """)
    # Migración: agregar columnas si la tabla ya existía sin ellas
    for col in ["latitud", "longitud"]:
        try:
            cursor.execute(f"ALTER TABLE bitacora_plagas ADD COLUMN {col} REAL")
        except sqlite3.OperationalError:
            pass  # La columna ya existe
    conn.commit()
    conn.close()

# Inicializa la BD al arrancar
init_db()

# ─────────────────────────────────────────────
# Modelos Pydantic
# ─────────────────────────────────────────────
class RegistroPlaga(BaseModel):
    nombre_plaga: str
    nombre_cientifico: str
    familia: str
    reino: str
    riesgo: str
    ficha_tecnica: str
    fecha: date
    latitud: float | None = None
    longitud: float | None = None

class RespuestaPlaga(BaseModel):
    id: int
    nombre_plaga: str
    nombre_cientifico: str
    familia: str
    reino: str
    riesgo: str
    ficha_tecnica: str
    fecha: str
    latitud: float | None = None
    longitud: float | None = None

class RespuestaGBIF(BaseModel):
    nombre_cientifico: str
    familia: str
    reino: str
    confianza: int | None = None
    foto_url: str | None = None
    wikipedia_url: str | None = None

# ─────────────────────────────────────────────
# FASE 2 — Endpoint externo (iNaturalist → GBIF)
# ─────────────────────────────────────────────
async def traducir_con_inaturalist(nombre: str, client: httpx.AsyncClient) -> dict:
    """
    Paso 1: consulta iNaturalist con el nombre en cualquier idioma.
    Devuelve un dict con el nombre científico, foto y enlace a Wikipedia.
    """
    resultado = {"nombre": nombre, "foto_url": None, "wikipedia_url": None}
    try:
        resp = await client.get(
            "https://api.inaturalist.org/v1/taxa",
            params={"q": nombre, "locale": "es", "per_page": 1, "rank": "species,genus,family,order"},
            timeout=8.0,
        )
        resp.raise_for_status()
        resultados = resp.json().get("results", [])
        if resultados:
            taxon = resultados[0]
            resultado["nombre"]       = taxon.get("name", nombre)
            resultado["wikipedia_url"] = taxon.get("wikipedia_url")
            foto = taxon.get("default_photo")
            if foto:
                resultado["foto_url"] = foto.get("medium_url")
    except Exception:
        pass  # Si iNaturalist falla, usamos el nombre original sin foto ni wiki
    return resultado


@app.get(
    "/buscar_externo/{nombre}",
    response_model=RespuestaGBIF,
    summary="Consulta ficha científica (iNaturalist → GBIF)",
    tags=["Fase 2 – API Externa"],
)
async def buscar_externo(nombre: str):
    """
    Flujo de dos pasos:
    1. iNaturalist traduce el nombre común (en cualquier idioma) al nombre científico,
       y además aporta foto real y enlace a Wikipedia.
    2. GBIF recibe ese nombre científico y devuelve la clasificación taxonómica completa.
    """
    async with httpx.AsyncClient(timeout=12.0) as client:

        # ── Paso 1: iNaturalist ──
        inaturalist = await traducir_con_inaturalist(nombre, client)
        nombre_cientifico_traducido = inaturalist["nombre"]

        # ── Paso 2: GBIF ──
        try:
            response = await client.get(
                "https://api.gbif.org/v1/species/match",
                params={"name": nombre_cientifico_traducido, "verbose": False},
            )
            response.raise_for_status()
        except httpx.RequestError as exc:
            raise HTTPException(status_code=503, detail=f"No se pudo contactar GBIF: {exc}")
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=exc.response.status_code, detail=f"Error GBIF: {exc.response.text}")

    data = response.json()

    if data.get("matchType") == "NONE" or "scientificName" not in data:
        raise HTTPException(
            status_code=404,
            detail=f"No se encontró información para '{nombre}' (buscado como '{nombre_cientifico_traducido}').",
        )

    return RespuestaGBIF(
        nombre_cientifico=data.get("scientificName", "Desconocido"),
        familia=data.get("family", "Desconocida"),
        reino=data.get("kingdom", "Desconocido"),
        confianza=data.get("confidence"),
        foto_url=inaturalist["foto_url"],
        wikipedia_url=inaturalist["wikipedia_url"],
    )

# ─────────────────────────────────────────────
# FASE 3 — Endpoints de persistencia
# ─────────────────────────────────────────────
@app.post(
    "/registrar",
    response_model=RespuestaPlaga,
    status_code=201,
    summary="Registra una plaga en la bitácora local",
    tags=["Fase 3 – Persistencia"],
)
def registrar_plaga(plaga: RegistroPlaga):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO bitacora_plagas (nombre_plaga, nombre_cientifico, familia, reino, riesgo, ficha_tecnica, fecha, latitud, longitud)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plaga.nombre_plaga, plaga.nombre_cientifico, plaga.familia,
                plaga.reino, plaga.riesgo, plaga.ficha_tecnica,
                str(plaga.fecha), plaga.latitud, plaga.longitud,
            ),
        )
        conn.commit()
        nuevo_id = cursor.lastrowid
    finally:
        conn.close()

    return RespuestaPlaga(
        id=nuevo_id,
        nombre_plaga=plaga.nombre_plaga,
        nombre_cientifico=plaga.nombre_cientifico,
        familia=plaga.familia,
        reino=plaga.reino,
        riesgo=plaga.riesgo,
        ficha_tecnica=plaga.ficha_tecnica,
        fecha=str(plaga.fecha),
        latitud=plaga.latitud,
        longitud=plaga.longitud,
    )


@app.get(
    "/focos",
    response_model=list[RespuestaPlaga],
    summary="Devuelve todos los registros con coordenadas para el mapa",
    tags=["Geolocalización"],
)
def obtener_focos():
    """Retorna todos los registros que tienen latitud y longitud."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, nombre_plaga, nombre_cientifico, familia, reino,
                   riesgo, ficha_tecnica, fecha, latitud, longitud
            FROM bitacora_plagas
            WHERE latitud IS NOT NULL AND longitud IS NOT NULL
            ORDER BY fecha DESC
        """)
        rows = cursor.fetchall()
    finally:
        conn.close()

    return [
        RespuestaPlaga(
            id=r["id"], nombre_plaga=r["nombre_plaga"],
            nombre_cientifico=r["nombre_cientifico"], familia=r["familia"],
            reino=r["reino"], riesgo=r["riesgo"], ficha_tecnica=r["ficha_tecnica"],
            fecha=r["fecha"], latitud=r["latitud"], longitud=r["longitud"],
        )
        for r in rows
    ]


@app.get(
    "/consultar/{nombre}",
    response_model=list[RespuestaPlaga],
    summary="Consulta registros locales por nombre de plaga",
    tags=["Fase 3 – Persistencia"],
)
def consultar_plaga(nombre: str):
    """
    Busca en la base de datos local todos los registros cuyo
    nombre_plaga contenga el texto indicado (búsqueda parcial).
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT id, nombre_plaga, nombre_cientifico, familia, reino, riesgo, ficha_tecnica, fecha, latitud, longitud
            FROM bitacora_plagas
            WHERE nombre_plaga LIKE ?
            ORDER BY fecha DESC
            """,
            (f"%{nombre}%",),
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No se encontraron registros locales para '{nombre}'.",
        )

    return [
        RespuestaPlaga(
            id=row["id"],
            nombre_plaga=row["nombre_plaga"],
            nombre_cientifico=row["nombre_cientifico"],
            familia=row["familia"],
            reino=row["reino"],
            riesgo=row["riesgo"],
            ficha_tecnica=row["ficha_tecnica"],
            fecha=row["fecha"],
            latitud=row["latitud"],
            longitud=row["longitud"],
        )
        for row in rows
    ]


@app.get(
    "/registros",
    response_model=list[RespuestaPlaga],
    summary="Lista todos los registros de la bitácora",
    tags=["Fase 3 – Persistencia"],
)
def listar_registros():
    """Devuelve todos los registros ordenados por fecha descendente."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT id, nombre_plaga, nombre_cientifico, familia, reino, riesgo, ficha_tecnica, fecha
            FROM bitacora_plagas
            ORDER BY fecha DESC
            """
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    return [
        RespuestaPlaga(
            id=row["id"],
            nombre_plaga=row["nombre_plaga"],
            nombre_cientifico=row["nombre_cientifico"],
            familia=row["familia"],
            reino=row["reino"],
            riesgo=row["riesgo"],
            ficha_tecnica=row["ficha_tecnica"],
            fecha=row["fecha"],
        )
        for row in rows
    ]


@app.put(
    "/actualizar/{id}",
    response_model=RespuestaPlaga,
    summary="Actualiza un registro existente",
    tags=["Fase 3 – Persistencia"],
)
def actualizar_plaga(id: int, plaga: RegistroPlaga):
    """Edita todos los campos de un registro identificado por su ID."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM bitacora_plagas WHERE id = ?", (id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail=f"No existe un registro con id={id}.")

        cursor.execute(
            """
            UPDATE bitacora_plagas
            SET nombre_plaga=?, nombre_cientifico=?, familia=?, reino=?, riesgo=?, ficha_tecnica=?, fecha=?
            WHERE id=?
            """,
            (
                plaga.nombre_plaga,
                plaga.nombre_cientifico,
                plaga.familia,
                plaga.reino,
                plaga.riesgo,
                plaga.ficha_tecnica,
                str(plaga.fecha),
                id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return RespuestaPlaga(
        id=id,
        nombre_plaga=plaga.nombre_plaga,
        nombre_cientifico=plaga.nombre_cientifico,
        familia=plaga.familia,
        reino=plaga.reino,
        riesgo=plaga.riesgo,
        ficha_tecnica=plaga.ficha_tecnica,
        fecha=str(plaga.fecha),
    )


@app.delete(
    "/eliminar/{id}",
    summary="Elimina un registro de la bitácora",
    tags=["Fase 3 – Persistencia"],
)
def eliminar_plaga(id: int):
    """Elimina permanentemente el registro con el ID indicado."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM bitacora_plagas WHERE id = ?", (id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail=f"No existe un registro con id={id}.")
        cursor.execute("DELETE FROM bitacora_plagas WHERE id = ?", (id,))
        conn.commit()
    finally:
        conn.close()

    return {"mensaje": f"Registro #{id} eliminado correctamente."}


# ─────────────────────────────────────────────
# FASE 2B — Identificación por imagen (Gemini + iNaturalist + GBIF)
# ─────────────────────────────────────────────
GEMINI_API_KEY = "AIzaSyDMBGY_odK-11ji9OOWw0P5SzoZAybjoXs"

@app.post(
    "/identificar_imagen",
    response_model=RespuestaGBIF,
    summary="Identifica una plaga desde una imagen usando Gemini Vision",
    tags=["Fase 2B – Imagen"],
)
async def identificar_imagen(imagen: UploadFile = File(...)):
    """
    Flujo de tres pasos:
    1. Comprime la imagen y la envía a Gemini Vision.
    2. iNaturalist confirma y obtiene foto + Wikipedia.
    3. GBIF devuelve la clasificación taxonómica completa.
    """
    # ── Leer y comprimir imagen con Pillow ──
    contenido = await imagen.read()
    try:
        img = Image.open(io.BytesIO(contenido)).convert("RGB")
        # Redimensionar si es muy grande
        img.thumbnail((800, 800), Image.LANCZOS)
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=70)
        contenido_comprimido = buffer.getvalue()
    except Exception:
        contenido_comprimido = contenido  # Si falla Pillow usar original

    imagen_b64 = base64.b64encode(contenido_comprimido).decode("utf-8")

    async with httpx.AsyncClient(timeout=45.0) as client:

        # ── Paso 1: Gemini Vision ──
        gemini_url = f"https://generativelanguage.googleapis.com/v1/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [{
                "parts": [
                    {
                        "text": (
                            "Identify the insect, pest or plant species in this image. "
                            "Reply ONLY with the most likely scientific name (genus and species). "
                            "If uncertain, reply with just the genus or family name. "
                            "No explanations, just the scientific name."
                        )
                    },
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": imagen_b64,
                        }
                    }
                ]
            }],
            "generationConfig": {
                "maxOutputTokens": 50,
                "temperature": 0.1,
            }
        }

        try:
            print(f"[Gemini] Enviando imagen ({len(contenido_comprimido)//1024}KB)...")
            gemini_resp = await client.post(gemini_url, json=payload)
            gemini_resp.raise_for_status()
            print(f"[Gemini] Respuesta recibida: {gemini_resp.status_code}")
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Gemini tardó demasiado. Intenta con una imagen más pequeña.")
        except httpx.RequestError as exc:
            raise HTTPException(status_code=503, detail=f"No se pudo contactar Gemini: {exc}")
        except httpx.HTTPStatusError:
            raise HTTPException(status_code=502, detail=f"Error de Gemini: {gemini_resp.text}")

        gemini_data = gemini_resp.json()
        try:
            nombre_detectado = gemini_data["candidates"][0]["content"]["parts"][0]["text"].strip()
            print(f"[Gemini] Detectó: {nombre_detectado}")
        except (KeyError, IndexError):
            raise HTTPException(status_code=422, detail="Gemini no pudo identificar la especie en la imagen.")

        # ── Paso 2: iNaturalist ──
        inaturalist = await traducir_con_inaturalist(nombre_detectado, client)
        nombre_cientifico_final = inaturalist["nombre"]
        print(f"[iNaturalist] Nombre final: {nombre_cientifico_final}")

        # ── Paso 3: GBIF ──
        try:
            gbif_resp = await client.get(
                "https://api.gbif.org/v1/species/match",
                params={"name": nombre_cientifico_final, "verbose": False},
            )
            gbif_resp.raise_for_status()
        except httpx.RequestError as exc:
            raise HTTPException(status_code=503, detail=f"No se pudo contactar GBIF: {exc}")

    gbif_data = gbif_resp.json()

    if gbif_data.get("matchType") == "NONE" or "scientificName" not in gbif_data:
        raise HTTPException(
            status_code=404,
            detail=f"Gemini detectó '{nombre_detectado}' pero no se encontró en GBIF.",
        )

    return RespuestaGBIF(
        nombre_cientifico=gbif_data.get("scientificName", nombre_detectado),
        familia=gbif_data.get("family", "Desconocida"),
        reino=gbif_data.get("kingdom", "Desconocido"),
        confianza=gbif_data.get("confidence"),
        foto_url=inaturalist["foto_url"],
        wikipedia_url=inaturalist["wikipedia_url"],
    )


# ─────────────────────────────────────────────
# REPORTE PDF
# ─────────────────────────────────────────────
def limpiar_texto(texto: str) -> str:
    """Reemplaza caracteres especiales para compatibilidad con FPDF Helvetica."""
    if not texto:
        return ""
    reemplazos = {
        'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u',
        'Á': 'A', 'É': 'E', 'Í': 'I', 'Ó': 'O', 'Ú': 'U',
        'ñ': 'n', 'Ñ': 'N', 'ü': 'u', 'Ü': 'U',
        '–': '-', '—': '-', '\u2019': "'", '\u201c': '"', '\u201d': '"',
        '°': ' ', '©': '(c)', '®': '(R)', '€': 'EUR',
    }
    for orig, reemplazo in reemplazos.items():
        texto = texto.replace(orig, reemplazo)
    # Eliminar cualquier carácter fuera de latin-1
    return texto.encode('latin-1', errors='replace').decode('latin-1')


@app.get(
    "/reporte_pdf",
    summary="Genera un reporte PDF de la bitácora",
    tags=["Reportes"],
)
def generar_reporte_pdf():
    """
    Genera y descarga un reporte PDF con todos los registros
    de la bitácora local de AgroGuard.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, nombre_plaga, nombre_cientifico, familia, reino, riesgo, ficha_tecnica, fecha
            FROM bitacora_plagas
            ORDER BY fecha DESC
        """)
        registros = cursor.fetchall()
    finally:
        conn.close()

    # ── Crear PDF ──
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # ── Encabezado — fondo verde bosque ──
    pdf.set_fill_color(15, 31, 19)        # --bg #0f1f13
    pdf.rect(0, 0, 210, 42, 'F')

    # Línea superior decorativa (verde lima)
    pdf.set_fill_color(184, 244, 88)      # --neon
    pdf.rect(0, 0, 210, 1.5, 'F')

    # Título
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 24)
    pdf.set_xy(12, 7)
    pdf.cell(0, 10, "AGROGUARD", ln=False)

    # Subtítulo
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(184, 244, 88)      # --neon
    pdf.set_xy(12, 20)
    pdf.cell(0, 5, "SISTEMA DE GESTION DE PLAGAS AGRICOLAS  |  v1.0", ln=True)

    # Fecha
    pdf.set_text_color(170, 200, 170)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_xy(12, 29)
    pdf.cell(0, 5, f"Reporte generado: {datetime.now().strftime('%d/%m/%Y %H:%M')}", ln=True)

    # ── Separador ──
    pdf.set_fill_color(184, 244, 88)
    pdf.rect(12, 39, 186, 0.5, 'F')

    # ── Subtítulo sección ──
    pdf.set_text_color(15, 31, 19)
    pdf.set_xy(12, 47)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 7, "Bitacora de Plagas Registradas", ln=True)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(80, 100, 80)
    pdf.cell(0, 5, f"Total de registros: {len(registros)}", ln=True)
    pdf.ln(3)

    if not registros:
        pdf.set_text_color(140, 160, 140)
        pdf.set_font("Helvetica", "I", 11)
        pdf.cell(0, 10, "No hay registros en la bitacora.", ln=True)
    else:
        for r in registros:
            riesgo = r["riesgo"]
            nombre_plaga      = limpiar_texto(r["nombre_plaga"])
            nombre_cientifico = limpiar_texto(r["nombre_cientifico"])
            familia           = limpiar_texto(r["familia"])
            reino             = limpiar_texto(r["reino"])
            ficha             = limpiar_texto(r["ficha_tecnica"])
            if len(ficha) > 180:
                ficha = ficha[:177] + "..."

            # Colores por riesgo — paleta Eco Premium
            if riesgo == "Alto":
                color_borde = (242, 107, 107)    # --red suavizado
                color_badge = (180, 50, 50)
            elif riesgo == "Medio":
                color_borde = (245, 184, 48)     # --amber
                color_badge = (180, 130, 20)
            else:
                color_borde = (93, 180, 120)     # verde suave
                color_badge = (40, 120, 70)

            y_start = pdf.get_y()

            # Fondo tarjeta — blanco muy suave
            pdf.set_fill_color(248, 252, 248)
            pdf.rect(10, y_start, 190, 44, 'F')

            # Borde izquierdo de color
            pdf.set_fill_color(*color_borde)
            pdf.rect(10, y_start, 3, 44, 'F')

            # Línea inferior sutil
            pdf.set_fill_color(220, 235, 220)
            pdf.rect(10, y_start + 44, 190, 0.4, 'F')

            # Nombre plaga
            pdf.set_xy(16, y_start + 4)
            pdf.set_font("Helvetica", "B", 11)
            pdf.set_text_color(15, 31, 19)
            pdf.cell(100, 6, nombre_plaga, ln=False)

            # Badge riesgo
            pdf.set_fill_color(*color_badge)
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("Helvetica", "B", 7)
            pdf.set_xy(158, y_start + 4)
            pdf.cell(38, 5, f" {riesgo.upper()} ", ln=False, fill=True)

            # Nombre científico
            pdf.set_xy(16, y_start + 12)
            pdf.set_font("Helvetica", "I", 9)
            pdf.set_text_color(60, 100, 70)
            pdf.cell(0, 5, nombre_cientifico, ln=True)

            # Familia y Reino
            pdf.set_xy(16, y_start + 19)
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(100, 130, 100)
            pdf.cell(95, 4, f"Familia: {familia}   Reino: {reino}", ln=False)
            pdf.cell(0, 4, f"Fecha: {r['fecha']}  |  ID: #{r['id']}", ln=True)

            # Ficha técnica
            pdf.set_xy(16, y_start + 25)
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(50, 70, 55)
            pdf.multi_cell(181, 4, f"Ficha: {ficha}")

            pdf.set_y(y_start + 47)
            pdf.ln(1)

    # ── Pie de página ──
    pdf.set_y(-18)
    pdf.set_fill_color(15, 31, 19)
    pdf.rect(0, pdf.get_y() - 2, 210, 25, 'F')
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(184, 244, 88)
    pdf.cell(0, 5, "AGROGUARD - Reporte generado automaticamente", align="C")

    # ── Exportar ──
    pdf_bytes = pdf.output()

    return Response(
        content=bytes(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=AgroGuard_Reporte_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
        },
    )


# ─────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────
@app.get("/", tags=["General"])
def root():
    return {"sistema": "AgroGuard", "estado": "activo", "version": "1.0.0"}