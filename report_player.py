import re
import json
import time
import os
import logging
from datetime import datetime

from google import genai
from google.genai import types as genai_types
from weasyprint import HTML

from nba_api.stats.static import players as players_static
from nba_api.stats.endpoints import (
    commonplayerinfo,
    playercareerstats,
)

# ─── Configuración ────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(levelname)s │ %(message)s")
logger = logging.getLogger(__name__)

API_KEY = os.getenv("API_KEY")

# Headers oficiales requeridos por la API de stats.nba.com
NBA_HEADERS = {
    "Host": "stats.nba.com",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Referer": "https://www.nba.com/",
    "Connection": "keep-alive",
}

# URL de headshots oficiales de la NBA
NBA_HEADSHOT_URL = "https://cdn.nba.com/headshots/nba/latest/1040x760/{player_id}.png"


# ─── Búsqueda de jugador ──────────────────────────────────────────────────────

def buscar_jugador(nombre_jugador: str) -> dict:
    """
    Busca un jugador en la base de datos estática de la NBA (sin llamada de red).
    Devuelve {'id': ..., 'full_name': ..., 'is_active': ...}.
    Lanza ValueError si no se encuentra.
    """
    nombre_jugador = nombre_jugador.strip()
    if not nombre_jugador:
        raise ValueError("El nombre del jugador no puede estar vacío.")

    # 1. Búsqueda por nombre completo (regex que nba_api usa internamente)
    resultados = players_static.find_players_by_full_name(nombre_jugador)

    # 2. Fallback: búsqueda por apellido y filtrado manual
    if not resultados:
        apellido = nombre_jugador.split()[-1]
        candidatos = players_static.find_players_by_last_name(apellido)
        nombre_lower = nombre_jugador.lower()
        resultados = [
            p for p in candidatos
            if nombre_lower in p["full_name"].lower()
            or p["full_name"].lower() in nombre_lower
        ]

    if not resultados:
        raise ValueError(
            f"Jugador '{nombre_jugador}' no encontrado. "
            "Verifica la ortografía (usa el nombre en inglés, ej. 'LeBron James')."
        )

    # Preferir jugadores activos si hay varios resultados
    activos = [p for p in resultados if p.get("is_active")]
    return activos[0] if activos else resultados[0]


# ─── Llamadas a la API de la NBA ──────────────────────────────────────────────

def _llamar_endpoint(endpoint_cls, player_id: int, **kwargs):
    """Llama a un endpoint de nba_api con reintentos y backoff exponencial."""
    retries = 4
    delay = 1.5
    for intento in range(retries):
        try:
            time.sleep(delay)
            endpoint = endpoint_cls(
                player_id=player_id,
                headers=NBA_HEADERS,
                timeout=30,
                **kwargs,
            )
            return endpoint.get_data_frames()
        except Exception as exc:
            if intento < retries - 1:
                logger.warning(
                    f"[{endpoint_cls.__name__}] intento {intento + 1}/{retries} "
                    f"fallido: {exc}. Reintentando en {delay:.0f}s…"
                )
                delay *= 2
            else:
                raise RuntimeError(
                    f"No se pudo obtener datos de {endpoint_cls.__name__} "
                    f"tras {retries} intentos."
                ) from exc


# ─── Extracción de datos personales ──────────────────────────────────────────

def obtener_info_personal(player_id: int) -> dict:
    """Extrae datos biográficos y de equipo del jugador."""
    dfs = _llamar_endpoint(commonplayerinfo.CommonPlayerInfo, player_id)
    row = dfs[0].iloc[0]

    # Edad calculada a partir de la fecha de nacimiento
    edad = ""
    try:
        nacimiento = datetime.strptime(str(row["BIRTHDATE"])[:10], "%Y-%m-%d")
        hoy = datetime.today()
        edad = str(
            hoy.year - nacimiento.year
            - ((hoy.month, hoy.day) < (nacimiento.month, nacimiento.day))
        )
    except Exception:
        pass

    # Conversión de altura: "6-9" → "6-9 (206 cm)"
    altura_imperial = str(row.get("HEIGHT", ""))
    altura_fmt = altura_imperial
    try:
        pies, pulgadas = map(int, altura_imperial.split("-"))
        cm = round(pies * 30.48 + pulgadas * 2.54)
        altura_fmt = f"{altura_imperial} ft ({cm} cm)"
    except Exception:
        pass

    # Conversión de peso: lbs → kg
    peso_lbs = str(row.get("WEIGHT", ""))
    peso_fmt = peso_lbs
    try:
        kg = round(int(peso_lbs) * 0.453592)
        peso_fmt = f"{peso_lbs} lbs ({kg} kg)"
    except Exception:
        pass

    # Información del draft
    draft_year = str(row.get("DRAFT_YEAR", "")).strip()
    if draft_year and draft_year not in ("0", "Undrafted", ""):
        draft = (
            f"{draft_year}, Ronda {row.get('DRAFT_ROUND', '?')}, "
            f"Pick #{row.get('DRAFT_NUMBER', '?')}"
        )
    else:
        draft = "No draftado"

    return {
        "Foto": NBA_HEADSHOT_URL.format(player_id=player_id),
        "Nombre": str(row.get("DISPLAY_FIRST_LAST", "")),
        "Equipo": str(row.get("TEAM_NAME", "Sin equipo")),
        "Ciudad": str(row.get("TEAM_CITY", "")),
        "Posición": str(row.get("POSITION", "")),
        "Altura": altura_fmt,
        "Peso": peso_fmt,
        "Edad": edad,
        "País": str(row.get("COUNTRY", "")),
        "Universidad": str(row.get("SCHOOL", "–")),
        "Draft": draft,
        "Temporadas_NBA": str(row.get("SEASON_EXP", "")),
    }


# ─── Extracción de estadísticas ───────────────────────────────────────────────

def _fmtv(row, col: str) -> str:
    """Formatea un valor numérico de DataFrame de manera segura."""
    try:
        val = row[col]
        if val is None or str(val) in ("nan", ""):
            return "–"
        return str(round(float(val), 1))
    except Exception:
        return "–"


def _fmtpct(row, col: str) -> str:
    """Formatea un porcentaje (0-1 → XX.X%)."""
    try:
        val = float(row[col])
        return f"{round(val * 100, 1)}%"
    except Exception:
        return "–"


def obtener_estadisticas(player_id: int) -> dict:
    """
    Obtiene estadísticas de carrera (por partido, por 36 min) y
    la última temporada del jugador.
    """
    stats = {}

    # ── Estadísticas por partido (carrera completa) ──
    try:
        dfs = _llamar_endpoint(
            playercareerstats.PlayerCareerStats,
            player_id,
            per_mode_simple="PerGame",
        )
        carrera = dfs[1]  # [1] = totales de carrera
        if not carrera.empty:
            r = carrera.iloc[0]
            stats["carrera_por_partido"] = {
                "Partidos": _fmtv(r, "GP"),
                "Minutos": _fmtv(r, "MIN"),
                "Puntos": _fmtv(r, "PTS"),
                "Rebotes": _fmtv(r, "REB"),
                "Rebotes_ofensivos": _fmtv(r, "OREB"),
                "Rebotes_defensivos": _fmtv(r, "DREB"),
                "Asistencias": _fmtv(r, "AST"),
                "Robos": _fmtv(r, "STL"),
                "Tapones": _fmtv(r, "BLK"),
                "Pérdidas": _fmtv(r, "TOV"),
                "Faltas": _fmtv(r, "PF"),
                "FG%": _fmtpct(r, "FG_PCT"),
                "3P%": _fmtpct(r, "FG3_PCT"),
                "FT%": _fmtpct(r, "FT_PCT"),
            }
        # Última temporada
        temporadas = dfs[0]
        if not temporadas.empty:
            ult = temporadas.iloc[-1]
            stats["ultima_temporada"] = {
                "Temporada": str(ult.get("SEASON_ID", "")),
                "Equipo": str(ult.get("TEAM_ABBREVIATION", "")),
                "Partidos": _fmtv(ult, "GP"),
                "Minutos": _fmtv(ult, "MIN"),
                "Puntos": _fmtv(ult, "PTS"),
                "Rebotes": _fmtv(ult, "REB"),
                "Asistencias": _fmtv(ult, "AST"),
                "Robos": _fmtv(ult, "STL"),
                "Tapones": _fmtv(ult, "BLK"),
                "FG%": _fmtpct(ult, "FG_PCT"),
                "3P%": _fmtpct(ult, "FG3_PCT"),
                "FT%": _fmtpct(ult, "FT_PCT"),
            }
    except Exception as exc:
        logger.warning(f"Stats por partido no disponibles: {exc}")

    # ── Estadísticas por 36 minutos (carrera) ──
    try:
        dfs36 = _llamar_endpoint(
            playercareerstats.PlayerCareerStats,
            player_id,
            per_mode_simple="Per36",
        )
        carrera36 = dfs36[1]
        if not carrera36.empty:
            r = carrera36.iloc[0]
            stats["carrera_por_36_min"] = {
                "Puntos_36": _fmtv(r, "PTS"),
                "Rebotes_36": _fmtv(r, "REB"),
                "Asistencias_36": _fmtv(r, "AST"),
                "Robos_36": _fmtv(r, "STL"),
                "Tapones_36": _fmtv(r, "BLK"),
                "Pérdidas_36": _fmtv(r, "TOV"),
                "FG%": _fmtpct(r, "FG_PCT"),
                "3P%": _fmtpct(r, "FG3_PCT"),
                "FT%": _fmtpct(r, "FT_PCT"),
            }
    except Exception as exc:
        logger.warning(f"Stats por 36 min no disponibles: {exc}")

    return stats


# ─── Prompt para Gemini ───────────────────────────────────────────────────────

def generar_prompt_para_llm(player_data: dict) -> str:
    nombre = player_data.get("Datos personales", {}).get("Nombre", "el jugador")
    return f"""Eres un analista profesional de baloncesto especializado en estadística avanzada, scouting y redacción técnica.

A partir del siguiente JSON con información detallada de un jugador de la NBA, genera un informe técnico en HTML listo para convertir a PDF.

=== DATOS DEL JUGADOR ===
{json.dumps(player_data, ensure_ascii=False, indent=2)}
=========================

ESTRUCTURA OBLIGATORIA (no omitas ningún apartado):

1. CABECERA
   - <img> con la URL de 'Foto' del JSON, flotando a la izquierda (max-width: 160px).
   - A la derecha: nombre en <h1> con color #1d428a, posición y equipo en <p>.
   - Línea divisoria <hr> tras la cabecera.

2. DATOS PERSONALES
   - <h2>Reporte Jugador: {nombre}</h2>
   - Tabla de 2 columnas con: Edad, País, Altura, Peso, Posición, Equipo, Universidad, Draft, Temporadas NBA.
   - Cabecera de tabla: fondo #1d428a, texto blanco.

3. ESTADÍSTICAS DESTACADAS
   - Tabla con estadísticas de carrera por partido: PTS, REB, AST, STL, BLK, FG%, 3P%, FT%, MIN.
   - Si hay datos de última temporada, añade una segunda tabla con etiqueta de temporada.

4. RESUMEN DEL DESEMPEÑO
   - Párrafo de 100-150 palabras, analítico y técnico, fundamentado en los datos.

5. ANÁLISIS FODA en cuadrícula 2×2
   Usa este HTML exacto para el grid:
   <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
     <section style="border: 1px solid #1d428a; padding: 10px; border-radius: 4px;">
       <h3 style="color:#1d428a;">💪 Fortalezas</h3><ul>...</ul>
     </section>
     <section style="border: 1px solid #28a745; padding: 10px; border-radius: 4px;">
       <h3 style="color:#28a745;">🚀 Oportunidades</h3><ul>...</ul>
     </section>
     <section style="border: 1px solid #ffc107; padding: 10px; border-radius: 4px;">
       <h3 style="color:#856404;">⚠️ Debilidades</h3><ul>...</ul>
     </section>
     <section style="border: 1px solid #dc3545; padding: 10px; border-radius: 4px;">
       <h3 style="color:#dc3545;">🛡️ Amenazas</h3><ul>...</ul>
     </section>
   </div>
   - De 2 a 3 puntos por sección (máx. 25 palabras por punto). Basa todo en los datos del JSON.

6. POTENCIAL DE CRECIMIENTO
   - Párrafo de 80-100 palabras. Si el jugador es veterano, enfócate en sostenibilidad y adaptación de rol.

7. JUGADORES SIMILARES
   - Lista de 2-3 jugadores comparables con breve justificación (físico, estadísticas, rol, estilo).

REGLAS TÉCNICAS:
- Solo CSS inline. Sin <style>, sin <link>, sin <script>.
- Fuente: font-family: 'Helvetica Neue', Arial, sans-serif.
- Colores: texto #1a1a1a, fondo blanco, acento #1d428a (azul NBA).
- Contenedor principal: <div style="max-width: 800px; margin: 0 auto; padding: 32px; font-family: ...">
- Tablas: border-collapse: collapse; width: 100%; celdas con padding: 6px 10px; border: 1px solid #dee2e6.
- Cabeceras de tabla: background: #1d428a; color: white; font-weight: bold.

LOGO BASKETMÁTICA (esquina inferior derecha):
<img src="https://basketmatica.wordpress.com/wp-content/uploads/2024/07/logo_basketmatica.png"
     alt="Logo Basketmática"
     style="position: fixed; bottom: 20px; right: 20px; width: 80px; opacity: 0.6;" />

RESPUESTA: devuelve ÚNICAMENTE el HTML, sin backticks, sin explicaciones.
Empieza con <!DOCTYPE html> y cierra con </html>.
Idioma del informe: ESPAÑOL.
"""


# ─── Limpieza HTML de Gemini ──────────────────────────────────────────────────

def _limpiar_html_gemini(texto: str) -> str:
    """Elimina envoltorios de markdown que Gemini a veces añade."""
    texto = texto.strip()
    # Quitar ```html ... ``` o ``` ... ```
    texto = re.sub(r"^```(?:html)?\s*\n?", "", texto, flags=re.IGNORECASE)
    texto = re.sub(r"\n?```\s*$", "", texto, flags=re.IGNORECASE)
    return texto.strip()


# ─── Pipeline principal ───────────────────────────────────────────────────────

def generar_pdf_jugador(nombre_jugador: str, output_path: str) -> bool:
    """
    Pipeline completo:
      buscar jugador → datos personales → estadísticas → prompt → Gemini → PDF

    Parámetros:
        nombre_jugador: nombre en inglés (ej. "LeBron James", "Stephen Curry").
        output_path:    ruta donde se guardará el PDF resultante.

    Retorna True si todo fue exitoso.
    Lanza ValueError (jugador no encontrado) o RuntimeError (errores de API).
    """
    logger.info(f"=== Iniciando informe para: '{nombre_jugador}' ===")

    # 1. Buscar jugador (sin llamada de red, base de datos local)
    jugador = buscar_jugador(nombre_jugador)
    player_id = jugador["id"]
    logger.info(f"✓ Jugador encontrado: {jugador['full_name']} (ID: {player_id})")

    # 2. Obtener datos personales
    logger.info("Obteniendo información personal…")
    datos_personales = obtener_info_personal(player_id)

    # 3. Obtener estadísticas
    logger.info("Obteniendo estadísticas de carrera…")
    estadisticas = obtener_estadisticas(player_id)

    player_data = {
        "Datos personales": datos_personales,
        "Estadísticas": estadisticas,
    }

    # 4. Generar HTML con Gemini
    logger.info("Generando informe con Gemini…")
    if not API_KEY:
        raise EnvironmentError(
            "Variable de entorno 'API_KEY' no configurada. "
            "Añade tu clave de API de Google Gemini."
        )

    client = genai.Client(api_key=API_KEY)

    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=generar_prompt_para_llm(player_data),
        config=genai_types.GenerateContentConfig(
            temperature=0.35,
            max_output_tokens=8192,
        ),
    )

    html_content = _limpiar_html_gemini(response.text)

    if not html_content.lstrip().startswith("<"):
        raise ValueError(
            "La respuesta de Gemini no contiene HTML válido. "
            f"Inicio: {html_content[:120]!r}"
        )

    # 5. Convertir a PDF
    logger.info(f"Generando PDF → {output_path}")
    HTML(string=html_content).write_pdf(output_path)
    logger.info("✓ PDF generado correctamente.")
    return True
