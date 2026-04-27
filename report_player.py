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
from nba_api.stats.endpoints import commonplayerinfo, playercareerstats

# ─── Configuración global ─────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(levelname)s │ %(message)s")
logger = logging.getLogger(__name__)

API_KEY   = os.getenv("API_KEY")
PROXY_URL = os.getenv("PROXY_URL")  # opcional – necesario en servidores cloud

# Stats.nba.com requiere estos headers exactos para no devolver 403/timeout
NBA_HEADERS = {
    "Host": "stats.nba.com",
    "Connection": "keep-alive",
    "Accept": "application/json, text/plain, */*",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "X-NewRelic-ID": "VQECWF5UChAHUlNTBwgBVw==",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
}

NBA_HEADSHOT_URL = "https://cdn.nba.com/headshots/nba/latest/1040x760/{player_id}.png"

# Tiempo máximo por intento (en segundos). Reducido para no bloquear el servicio.
NBA_TIMEOUT = 15
NBA_RETRIES = 3


# ─── Búsqueda de jugador (local, sin red) ────────────────────────────────────

def buscar_jugador(nombre_jugador: str) -> dict:
    """
    Busca un jugador en la base de datos local de nba_api (sin llamada de red).
    Devuelve {'id', 'full_name', 'is_active'}.
    Lanza ValueError si no se encuentra.
    """
    nombre_jugador = nombre_jugador.strip()
    if not nombre_jugador:
        raise ValueError("El nombre del jugador no puede estar vacío.")

    # Búsqueda exacta por nombre completo
    resultados = players_static.find_players_by_full_name(nombre_jugador)

    # Fallback: búsqueda por apellido con filtrado manual
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
            f"Jugador '{nombre_jugador}' no encontrado en la base de datos de la NBA. "
            "Usa el nombre en inglés (ej. 'LeBron James', 'Luka Doncic')."
        )

    activos = [p for p in resultados if p.get("is_active")]
    return activos[0] if activos else resultados[0]


# ─── Llamadas a la API de stats.nba.com ──────────────────────────────────────

def _llamar_endpoint(endpoint_cls, player_id: int, **kwargs) -> list | None:
    """
    Llama a un endpoint de nba_api con reintentos y backoff.
    Usa PROXY_URL si está configurado.
    Devuelve la lista de DataFrames, o None si no se pudo conectar.
    """
    delay = 1.0
    last_error = None

    for intento in range(NBA_RETRIES):
        try:
            time.sleep(delay)
            endpoint = endpoint_cls(
                player_id=player_id,
                headers=NBA_HEADERS,
                timeout=NBA_TIMEOUT,
                proxy=PROXY_URL,   # None → sin proxy; str → usa el proxy indicado
                **kwargs,
            )
            return endpoint.get_data_frames()

        except Exception as exc:
            last_error = exc
            msg = str(exc)

            # Detectar errores permanentes (no reintentar)
            if any(k in msg for k in ("403", "Forbidden", "401", "Unauthorized")):
                logger.error(
                    f"[{endpoint_cls.__name__}] Acceso denegado por stats.nba.com. "
                    "Configura la variable PROXY_URL con un proxy residencial."
                )
                return None

            if intento < NBA_RETRIES - 1:
                logger.warning(
                    f"[{endpoint_cls.__name__}] intento {intento + 1}/{NBA_RETRIES} "
                    f"fallido: {type(exc).__name__}. Reintentando en {delay:.0f}s…"
                )
                delay *= 2
            else:
                logger.warning(
                    f"[{endpoint_cls.__name__}] no accesible tras {NBA_RETRIES} "
                    f"intentos. El informe se generará sin estos datos. "
                    f"({type(exc).__name__}: {str(exc)[:120]})"
                )
                return None

    return None


# ─── Extracción de datos personales ──────────────────────────────────────────

def _fmtv(row, col: str) -> str:
    try:
        val = row[col]
        if val is None or str(val) in ("nan", ""):
            return "–"
        return str(round(float(val), 1))
    except Exception:
        return "–"


def _fmtpct(row, col: str) -> str:
    try:
        return f"{round(float(row[col]) * 100, 1)}%"
    except Exception:
        return "–"


def obtener_info_personal(player_id: int) -> dict | None:
    """
    Obtiene datos biográficos del jugador desde stats.nba.com.
    Devuelve un dict con los datos, o None si la API no responde.
    """
    dfs = _llamar_endpoint(commonplayerinfo.CommonPlayerInfo, player_id)
    if dfs is None or dfs[0].empty:
        return None

    row = dfs[0].iloc[0]

    # Edad
    edad = ""
    try:
        nac = datetime.strptime(str(row["BIRTHDATE"])[:10], "%Y-%m-%d")
        hoy = datetime.today()
        edad = str(hoy.year - nac.year - ((hoy.month, hoy.day) < (nac.month, nac.day)))
    except Exception:
        pass

    # Altura en sistema métrico
    altura_fmt = str(row.get("HEIGHT", ""))
    try:
        pies, pulgadas = map(int, str(row["HEIGHT"]).split("-"))
        cm = round(pies * 30.48 + pulgadas * 2.54)
        altura_fmt = f"{row['HEIGHT']} ft ({cm} cm)"
    except Exception:
        pass

    # Peso en kg
    peso_fmt = str(row.get("WEIGHT", ""))
    try:
        kg = round(int(row["WEIGHT"]) * 0.453592)
        peso_fmt = f"{row['WEIGHT']} lbs ({kg} kg)"
    except Exception:
        pass

    # Draft
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


def obtener_estadisticas(player_id: int) -> dict | None:
    """
    Obtiene estadísticas de carrera y última temporada.
    Devuelve un dict con los datos, o None si la API no responde.
    """
    stats = {}
    cualquier_dato = False

    # Estadísticas por partido
    dfs = _llamar_endpoint(
        playercareerstats.PlayerCareerStats,
        player_id,
        per_mode_simple="PerGame",
    )
    if dfs is not None:
        cualquier_dato = True
        carrera = dfs[1] if len(dfs) > 1 else None
        temporadas = dfs[0] if len(dfs) > 0 else None

        if carrera is not None and not carrera.empty:
            r = carrera.iloc[0]
            stats["carrera_por_partido"] = {
                "Partidos":           _fmtv(r, "GP"),
                "Minutos":            _fmtv(r, "MIN"),
                "Puntos":             _fmtv(r, "PTS"),
                "Rebotes":            _fmtv(r, "REB"),
                "Reb_ofensivos":      _fmtv(r, "OREB"),
                "Reb_defensivos":     _fmtv(r, "DREB"),
                "Asistencias":        _fmtv(r, "AST"),
                "Robos":              _fmtv(r, "STL"),
                "Tapones":            _fmtv(r, "BLK"),
                "Pérdidas":           _fmtv(r, "TOV"),
                "FG%":                _fmtpct(r, "FG_PCT"),
                "3P%":                _fmtpct(r, "FG3_PCT"),
                "FT%":                _fmtpct(r, "FT_PCT"),
            }

        if temporadas is not None and not temporadas.empty:
            ult = temporadas.iloc[-1]
            stats["ultima_temporada"] = {
                "Temporada":  str(ult.get("SEASON_ID", "")),
                "Equipo":     str(ult.get("TEAM_ABBREVIATION", "")),
                "Partidos":   _fmtv(ult, "GP"),
                "Minutos":    _fmtv(ult, "MIN"),
                "Puntos":     _fmtv(ult, "PTS"),
                "Rebotes":    _fmtv(ult, "REB"),
                "Asistencias":_fmtv(ult, "AST"),
                "Robos":      _fmtv(ult, "STL"),
                "Tapones":    _fmtv(ult, "BLK"),
                "FG%":        _fmtpct(ult, "FG_PCT"),
                "3P%":        _fmtpct(ult, "FG3_PCT"),
                "FT%":        _fmtpct(ult, "FT_PCT"),
            }

    # Estadísticas por 36 minutos
    dfs36 = _llamar_endpoint(
        playercareerstats.PlayerCareerStats,
        player_id,
        per_mode_simple="Per36",
    )
    if dfs36 is not None and len(dfs36) > 1 and not dfs36[1].empty:
        cualquier_dato = True
        r = dfs36[1].iloc[0]
        stats["carrera_por_36_min"] = {
            "Puntos_36":     _fmtv(r, "PTS"),
            "Rebotes_36":    _fmtv(r, "REB"),
            "Asistencias_36":_fmtv(r, "AST"),
            "Robos_36":      _fmtv(r, "STL"),
            "Tapones_36":    _fmtv(r, "BLK"),
            "Pérdidas_36":   _fmtv(r, "TOV"),
            "FG%":           _fmtpct(r, "FG_PCT"),
            "3P%":           _fmtpct(r, "FG3_PCT"),
            "FT%":           _fmtpct(r, "FT_PCT"),
        }

    return stats if cualquier_dato else None


# ─── Generación del prompt para Gemini ───────────────────────────────────────

def generar_prompt_para_llm(
    nombre_jugador: str,
    player_id: int,
    datos_personales: dict | None,
    estadisticas: dict | None,
) -> str:
    """
    Genera el prompt para Gemini.
    Si no hay datos de la API, le pide que use su conocimiento de entrenamiento.
    """
    foto_url = NBA_HEADSHOT_URL.format(player_id=player_id)
    tiene_datos = datos_personales is not None or estadisticas is not None

    if tiene_datos:
        player_data = {}
        if datos_personales:
            player_data["Datos personales"] = datos_personales
        if estadisticas:
            player_data["Estadísticas"] = estadisticas

        nombre_display = (datos_personales or {}).get("Nombre", nombre_jugador)
        datos_json = json.dumps(player_data, ensure_ascii=False, indent=2)
        seccion_datos = f"""A partir del siguiente JSON con información actualizada del jugador:

{datos_json}

Basa CADA afirmación del informe en estos datos. No inventes estadísticas."""

    else:
        nombre_display = nombre_jugador
        seccion_datos = f"""NOTA IMPORTANTE: La API de estadísticas no está disponible en este momento.
Genera el informe usando tu conocimiento de entrenamiento sobre {nombre_jugador}.
Indica claramente al inicio del informe (en una nota discreta en cursiva) que las estadísticas
son aproximadas y pueden no reflejar la temporada más reciente.
Basa el análisis en estadísticas conocidas históricamente del jugador."""

    return f"""Eres un analista profesional de baloncesto especializado en estadística avanzada, scouting y redacción técnica.

{seccion_datos}

Genera un informe técnico completo sobre {nombre_display} en formato HTML optimizado para PDF.

ESTRUCTURA OBLIGATORIA:

1. CABECERA
   - <img src="{foto_url}" alt="Foto {nombre_display}" style="max-width:160px; float:left; margin-right:20px; border-radius:4px;">
   - A la derecha de la imagen: <h1 style="color:#1d428a;">{nombre_display}</h1>, posición y equipo en <p>.
   - <div style="clear:both;"></div> y <hr style="border-color:#1d428a;"> tras la cabecera.

2. DATOS PERSONALES
   - <h2>Reporte Jugador: {nombre_display}</h2>
   - Tabla de 2 columnas: Edad, País, Altura, Peso, Posición, Equipo, Universidad, Draft, Temporadas NBA.
   - Cabecera de tabla: background:#1d428a; color:white.

3. ESTADÍSTICAS DESTACADAS
   - Tabla con estadísticas de carrera por partido: PTS, REB, AST, STL, BLK, FG%, 3P%, FT%, MIN.
   - Si hay datos de última temporada disponibles, añade una segunda tabla comparativa con la temporada.

4. RESUMEN DEL DESEMPEÑO (100-150 palabras)
   - Análisis técnico y objetivo del rendimiento, impacto y aportación al equipo.

5. ANÁLISIS FODA en cuadrícula 2×2
   <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px; margin:16px 0;">
     <section style="border:2px solid #1d428a; padding:12px; border-radius:6px;">
       <h3 style="color:#1d428a; margin-top:0;">💪 Fortalezas</h3><ul>...</ul>
     </section>
     <section style="border:2px solid #28a745; padding:12px; border-radius:6px;">
       <h3 style="color:#28a745; margin-top:0;">🚀 Oportunidades</h3><ul>...</ul>
     </section>
     <section style="border:2px solid #e6a817; padding:12px; border-radius:6px;">
       <h3 style="color:#856404; margin-top:0;">⚠️ Debilidades</h3><ul>...</ul>
     </section>
     <section style="border:2px solid #dc3545; padding:12px; border-radius:6px;">
       <h3 style="color:#dc3545; margin-top:0;">🛡️ Amenazas</h3><ul>...</ul>
     </section>
   </div>
   - 2-3 puntos por sección. Máx. 25 palabras por punto. Fundamentados en datos reales.

6. POTENCIAL DE CRECIMIENTO (80-100 palabras)
   - Si es veterano: enfócate en sostenibilidad y adaptación de rol.

7. JUGADORES SIMILARES
   - 2-3 jugadores comparables. Justificación breve: físico, rol, estadísticas o estilo.

REGLAS TÉCNICAS (solo CSS inline, sin <style> ni <link>):
- Contenedor: <div style="max-width:800px; margin:0 auto; padding:32px; font-family:'Helvetica Neue',Arial,sans-serif; color:#1a1a1a;">
- Tablas: border-collapse:collapse; width:100%. Celdas: padding:7px 12px; border:1px solid #dee2e6.
- Filas alternas de tablas de stats: background:#f8f9fa en filas pares.
- Acento de color: #1d428a (azul NBA).

LOGO:
<img src="https://basketmatica.wordpress.com/wp-content/uploads/2024/07/logo_basketmatica.png"
     alt="Logo Basketmática"
     style="position:fixed; bottom:20px; right:20px; width:80px; opacity:0.55;" />

RESPUESTA: devuelve ÚNICAMENTE el HTML. Sin backticks, sin explicaciones.
Empieza con <!DOCTYPE html>. Idioma: ESPAÑOL.
"""


# ─── Limpieza del HTML generado por Gemini ───────────────────────────────────

def _limpiar_html_gemini(texto: str) -> str:
    texto = texto.strip()
    texto = re.sub(r"^```(?:html)?\s*\n?", "", texto, flags=re.IGNORECASE)
    texto = re.sub(r"\n?```\s*$", "", texto, flags=re.IGNORECASE)
    return texto.strip()


# ─── Pipeline principal ───────────────────────────────────────────────────────

def generar_pdf_jugador(nombre_jugador: str, output_path: str) -> bool:
    """
    Pipeline completo:
      buscar jugador (local) → datos NBA API → Gemini → PDF

    Si stats.nba.com no responde, Gemini genera el informe con su conocimiento
    de entrenamiento sobre el jugador.

    Parámetros:
        nombre_jugador: nombre en inglés (ej. "LeBron James").
        output_path:    ruta de salida del PDF.

    Lanza:
        ValueError   → jugador no encontrado.
        EnvironmentError → API_KEY no configurada.
        RuntimeError → error irrecuperable al generar el PDF.
    """
    logger.info(f"=== Informe para: '{nombre_jugador}' ===")

    # 1. Búsqueda del jugador (local, sin red)
    jugador = buscar_jugador(nombre_jugador)
    player_id = jugador["id"]
    logger.info(f"✓ Jugador: {jugador['full_name']} (ID: {player_id})")

    if PROXY_URL:
        logger.info(f"  Usando proxy: {PROXY_URL.split('@')[-1]}")  # ocultar credenciales
    else:
        logger.info(
            "  PROXY_URL no configurada. Si el servidor está en la nube y "
            "stats.nba.com da timeout, configura PROXY_URL en las variables "
            "de entorno de Render."
        )

    # 2. Datos personales (puede fallar en servidores cloud)
    logger.info("Obteniendo información personal…")
    datos_personales = obtener_info_personal(player_id)
    if datos_personales:
        logger.info(f"  ✓ Datos personales obtenidos: {datos_personales.get('Nombre')}")
    else:
        logger.warning("  ✗ Datos personales no disponibles (API NBA inaccesible).")

    # 3. Estadísticas (puede fallar en servidores cloud)
    logger.info("Obteniendo estadísticas…")
    estadisticas = obtener_estadisticas(player_id)
    if estadisticas:
        logger.info(f"  ✓ Estadísticas obtenidas ({len(estadisticas)} secciones).")
    else:
        logger.warning("  ✗ Estadísticas no disponibles (API NBA inaccesible).")

    if not datos_personales and not estadisticas:
        logger.warning(
            "La API de la NBA no está accesible desde este servidor. "
            "Generando informe con conocimiento de entrenamiento de Gemini. "
            "Para obtener datos en tiempo real, configura PROXY_URL."
        )

    # 4. Generar HTML con Gemini
    logger.info("Generando informe con Gemini…")
    if not API_KEY:
        raise EnvironmentError(
            "Variable de entorno 'API_KEY' no configurada. "
            "Añade tu clave de API de Google Gemini."
        )

    client = genai.Client(api_key=API_KEY)
    prompt = generar_prompt_para_llm(
        nombre_jugador=nombre_jugador,
        player_id=player_id,
        datos_personales=datos_personales,
        estadisticas=estadisticas,
    )

    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            temperature=0.35,
            max_output_tokens=8192,
        ),
    )

    html_content = _limpiar_html_gemini(response.text)

    if not html_content.lstrip().startswith("<"):
        raise RuntimeError(
            f"Gemini no devolvió HTML válido. Inicio: {html_content[:120]!r}"
        )

    # 5. Convertir a PDF
    logger.info(f"Generando PDF → {output_path}")
    HTML(string=html_content).write_pdf(output_path)
    logger.info("✓ PDF generado correctamente.")
    return True
