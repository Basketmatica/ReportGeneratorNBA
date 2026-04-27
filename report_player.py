from __future__ import annotations

import json
import logging
import os
import re

from google import genai
from google.genai import types as genai_types
from weasyprint import HTML

from nba_data import obtener_datos_jugador

logger = logging.getLogger(__name__)


# ─── Configuración Gemini ─────────────────────────────────────────────────────

API_KEY = os.getenv("API_KEY", "").strip()

# `gemini-2.0-flash` se desconecta el 1 de junio de 2026. Por defecto usamos
# `gemini-2.5-flash`, que está en el free tier y es estrictamente superior.
# El usuario puede sobreescribir con la env var GEMINI_MODEL si quiere.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-pro").strip()


# ─── Prompt ───────────────────────────────────────────────────────────────────


def generar_prompt_para_llm(player_data: dict) -> str:
    """Construye el prompt en español que pedirá a Gemini un informe HTML completo."""
    nombre = (
        player_data.get("Datos personales", {}).get("Nombre") or "el jugador"
    )
    return f"""Eres un analista profesional de baloncesto especializado en estadística avanzada, scouting y redacción técnica.

A partir del siguiente JSON con información detallada de un jugador de la NBA, genera un informe técnico en HTML listo para convertir a PDF.

=== DATOS DEL JUGADOR ===
{json.dumps(player_data, ensure_ascii=False, indent=2)}
=========================

ESTRUCTURA OBLIGATORIA (no omitas ningún apartado, aunque los datos sean limitados):

1. CABECERA
   - <img> con la URL de 'Foto' del JSON, flotando a la izquierda (max-width: 160px).
   - A la derecha: nombre en <h1> color #1d428a, posición y equipo en <p>.
   - Línea divisoria <hr> tras la cabecera.

2. DATOS PERSONALES
   - <h2>Reporte Jugador: {nombre}</h2>
   - Tabla de 2 columnas con: Equipo, Posición, Altura, Peso, País, Universidad,
     Draft, Dorsal, Temporadas en NBA. Si "Edad" es "–" no la incluyas.
   - Cabecera de tabla: fondo #1d428a, texto blanco.

3. ESTADÍSTICAS DESTACADAS
   - Tabla 'Promedios por partido' con la última temporada disponible: PTS,
     REB (OR/DR si están), AST, STL, BLK, TO, MIN, FG%, 3P%, FT%, Partidos.
     Indica el etiquetado de temporada en la cabecera de la tabla.
   - Si la última temporada incluye 'per36', añade una segunda tabla
     'Per-36 minutos (última temporada)' con: PTS, REB, AST, STL, BLK, TO.
     Es útil para contextualizar producción independientemente del minutaje.
   - Si hay 'temporadas_anteriores' con 2+ temporadas, añade una tabla
     'Trayectoria reciente' comparativa (una fila por temporada) con
     PTS/REB/AST/FG%/3P% al menos.
   - Si hay 'carrera_promedio', añade una tabla 'Promedios de carrera'
     con los mismos campos que la última temporada. Si hay 'per36' dentro,
     incluye una columna o tabla complementaria con el per-36 de carrera.
   - Si hay 'carrera_totales', añade una pequeña tabla 'Totales de carrera'
     con: Partidos (ya viene de carrera_promedio), PTS totales, REB totales,
     AST totales, STL totales, BLK totales, FG anotados-intentados,
     3P anotados-intentados, FT anotados-intentados.
   - Si hay 'avanzadas_ultima' o 'avanzadas_carrera', añade una tabla
     'Estadísticas avanzadas' con dos columnas (Última temporada / Carrera)
     mostrando: Dobles-dobles, Triples-dobles, AST/TO ratio, STL/TO ratio,
     Eficiencia anotadora, Eficiencia de tiro, Faltas técnicas, Faltas
     flagrantes. Omite filas sin datos.
   - Si el JSON contiene la nota '_nota', muéstrala como pie de tabla en
     cursiva y tamaño pequeño tras todas las tablas estadísticas.

4. RESUMEN DEL DESEMPEÑO
   - Párrafo de 100-150 palabras, analítico y técnico, fundamentado en los
     datos. Compara última temporada vs promedios de carrera (mejora,
     mantenimiento, declive). Si hay per-36, úsalo para juzgar la producción
     ajustada por minutaje. Si hay AST/TO o eficiencias, comenta lo que
     dicen sobre control de balón y selección de tiro.
   - No inventes cifras que no estén en el JSON.

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
   - De 2 a 3 puntos por sección (máx. 25 palabras por punto). Basa todo en
     los datos del JSON. Aprovecha métricas avanzadas (DD2/TD3/AST-TO/
     eficiencias) cuando estén disponibles, no te limites a las clásicas.

6. POTENCIAL DE CRECIMIENTO
   - Párrafo de 80-100 palabras. Si el jugador es veterano (carrera larga
     según 'carrera_totales'/'Temporadas_NBA'), enfócate en sostenibilidad,
     adaptación de rol y mantenimiento de eficiencia. Si la última temporada
     muestra caída en minutos o producción respecto a 'carrera_promedio',
     menciónalo.

7. JUGADORES SIMILARES
   - Lista de 2-3 jugadores comparables con breve justificación
     (físico, estadísticas, rol, estilo).

REGLAS TÉCNICAS:
- Solo CSS inline. Sin <style>, sin <link>, sin <script>.
- Fuente: font-family: 'Helvetica Neue', Arial, sans-serif.
- Colores: texto #1a1a1a, fondo blanco, acento #1d428a (azul NBA).
- Contenedor principal: <div style="max-width: 800px; margin: 0 auto; padding: 32px; font-family: ...">
- Tablas: border-collapse: collapse; width: 100%; celdas con padding: 6px 10px;
  border: 1px solid #dee2e6.
- Cabeceras de tabla: background: #1d428a; color: white; font-weight: bold.
- Si un dato está como "–" en el JSON, escribe simplemente "—" (no inventes).

LOGO BASKETMÁTICA (esquina inferior derecha):
<img src="https://basketmatica.wordpress.com/wp-content/uploads/2024/07/logo_basketmatica.png"
     alt="Logo Basketmática"
     style="position: fixed; bottom: 20px; right: 20px; width: 80px; opacity: 0.6;" />

RESPUESTA: devuelve ÚNICAMENTE el HTML, sin backticks, sin explicaciones.
Empieza con <!DOCTYPE html> y cierra con </html>.
Idioma del informe: ESPAÑOL.
"""


# ─── Limpieza HTML de Gemini ──────────────────────────────────────────────────

_FENCE_OPEN_RE = re.compile(r"^```(?:html)?\s*\n?", re.IGNORECASE)
_FENCE_CLOSE_RE = re.compile(r"\n?```\s*$", re.IGNORECASE)


def _limpiar_html_gemini(texto: str) -> str:
    """Elimina envoltorios de markdown que Gemini a veces añade."""
    if not texto:
        return ""
    texto = texto.strip()
    texto = _FENCE_OPEN_RE.sub("", texto)
    texto = _FENCE_CLOSE_RE.sub("", texto)
    return texto.strip()


# ─── Pipeline principal ───────────────────────────────────────────────────────


def generar_pdf_jugador(nombre_jugador: str, output_path: str) -> bool:
    """
    Pipeline completo: nombre → datos → prompt → Gemini → HTML → PDF.

    Parameters
    ----------
    nombre_jugador : str
        Nombre del jugador en inglés (p.ej. "LeBron James").
    output_path : str
        Ruta de fichero donde se escribirá el PDF.

    Returns
    -------
    bool
        ``True`` si el PDF se generó correctamente.

    Raises
    ------
    ValueError
        Jugador no encontrado.
    EnvironmentError
        Variable de entorno API_KEY o BALLDONTLIE_API_KEY no configurada.
    RuntimeError
        Errores transitorios de red / API tras agotar reintentos.
    """
    logger.info("=== Iniciando informe para: '%s' ===", nombre_jugador)

    # 1) Datos del jugador (bio + stats).
    player_data = obtener_datos_jugador(nombre_jugador)

    # 2) Validación de la API key de Gemini.
    if not API_KEY:
        raise EnvironmentError(
            "Variable de entorno 'API_KEY' no configurada. "
            "Añade tu clave gratuita de Google Gemini "
            "(https://aistudio.google.com/apikey) en Render → Environment."
        )

    # 3) Llamada a Gemini.
    logger.info("Generando informe con Gemini (%s)…", GEMINI_MODEL)
    client = genai.Client(api_key=API_KEY)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=generar_prompt_para_llm(player_data),
        config=genai_types.GenerateContentConfig(
            temperature=0.35,
            max_output_tokens=8192,
        ),
    )

    html_content = _limpiar_html_gemini(response.text or "")

    if not html_content.lstrip().startswith("<"):
        raise RuntimeError(
            "Gemini no devolvió HTML válido. Posible filtro de seguridad o "
            "respuesta vacía. Inicio recibido: " + repr(html_content[:120])
        )

    # 4) Conversión HTML → PDF.
    logger.info("Generando PDF → %s", output_path)
    HTML(string=html_content).write_pdf(output_path)
    logger.info("✓ PDF generado correctamente.")
    return True
