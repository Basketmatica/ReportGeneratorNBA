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
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()


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

ESTRUCTURA OBLIGATORIA (no omitas ningún apartado):

1. CABECERA (Usa Flexbox, NO float)
   - Contenedor: <div style="display: flex; align-items: center; gap: 24px; border-bottom: 3px solid #1d428a; padding-bottom: 20px; margin-bottom: 30px;">
   - <img> con la URL de 'Foto' del JSON (style="max-width: 140px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);").
   - Textos a la derecha: nombre en <h1 style="color: #1d428a; margin: 0 0 8px 0; font-size: 28px;">. Posición, equipo y dorsal en <p style="margin: 0; font-size: 18px; color: #555;">.

2. DATOS PERSONALES
   - <h2 style="color: #1d428a; border-bottom: 1px solid #eee; padding-bottom: 8px; margin-top: 0;">Perfil Físico y Draft</h2>
   - Tabla de 2 columnas con: Equipo, Posición, Altura, Peso, País, Universidad, Draft, Dorsal, Temporadas en NBA. (Omite "Edad" si es "–").

3. ESTADÍSTICAS DESTACADAS
   - <h2 style="color: #1d428a; border-bottom: 1px solid #eee; padding-bottom: 8px;">Métricas de Rendimiento</h2>
   - Tabla 'Promedios por partido' (última temporada): PTS, REB, AST, STL, BLK, TO, MIN, FG%, 3P%, FT%, Partidos.
   - Si hay 'per36' (última temporada), añade tabla 'Per-36 minutos': PTS, REB, AST, STL, BLK, TO.
   - Si hay 'temporadas_anteriores' (2+), añade tabla 'Trayectoria reciente': Temporada, PTS, REB, AST, FG%, 3P%.
   - Si hay 'carrera_promedio' y 'carrera_totales', añade tablas correspondientes.
   - Si hay 'avanzadas_ultima', añade tabla con: Dobles-dobles, Triples-dobles, AST/TO, STL/TO, Eficiencia anotadora, Eficiencia de tiro. Omite filas vacías.
   - REGLA PARA TABLAS: Las cabeceras de todas las tablas deben tener `style="background-color: #1d428a; color: white; padding: 10px; text-align: center; font-size: 14px;"`. Las celdas de datos `style="padding: 8px; border-bottom: 1px solid #e0e0e0; text-align: center; font-size: 13px; color: #333;"`.

4. RESUMEN DEL DESEMPEÑO
   - <h2 style="color: #1d428a; border-bottom: 1px solid #eee; padding-bottom: 8px;">Análisis de Desempeño</h2>
   - Párrafo de 100-150 palabras, analítico y técnico. Compara última temporada vs carrera. Usa per-36 y ratios de eficiencia si están disponibles para juzgar su impacto real.

5. ANÁLISIS FODA
   - Usa EXACTAMENTE este HTML para el grid (asegura un renderizado perfecto en PDF):
   <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px;">
     <section style="background-color: #f0f8ff; border-left: 4px solid #1d428a; padding: 16px; border-radius: 4px;">
       <h3 style="color:#1d428a; margin-top: 0; font-size: 16px;">Fortalezas</h3><ul style="margin: 0; padding-left: 20px; font-size: 14px;">...</ul>
     </section>
     <section style="background-color: #f4fbf5; border-left: 4px solid #28a745; padding: 16px; border-radius: 4px;">
       <h3 style="color:#28a745; margin-top: 0; font-size: 16px;">Oportunidades</h3><ul style="margin: 0; padding-left: 20px; font-size: 14px;">...</ul>
     </section>
     <section style="background-color: #fff9e6; border-left: 4px solid #ffc107; padding: 16px; border-radius: 4px;">
       <h3 style="color:#b58500; margin-top: 0; font-size: 16px;">Debilidades</h3><ul style="margin: 0; padding-left: 20px; font-size: 14px;">...</ul>
     </section>
     <section style="background-color: #fdf3f4; border-left: 4px solid #dc3545; padding: 16px; border-radius: 4px;">
       <h3 style="color:#dc3545; margin-top: 0; font-size: 16px;">Amenazas</h3><ul style="margin: 0; padding-left: 20px; font-size: 14px;">...</ul>
     </section>
   </div>
   - 2 a 3 puntos por sección (máx. 25 palabras por punto), basados 100% en el JSON.

6. POTENCIAL DE CRECIMIENTO Y JUGADORES SIMILARES
   - <h2 style="color: #1d428a; border-bottom: 1px solid #eee; padding-bottom: 8px;">Proyección</h2>
   - Párrafo de 80-100 palabras de potencial (enfocado en rol/sostenibilidad si es veterano).
   - <h2 style="color: #1d428a; border-bottom: 1px solid #eee; padding-bottom: 8px;">Jugadores similares</h2>
   - Seguido de una lista `<ul style="font-size: 14px; line-height: 1.6;">` con 2-3 jugadores comparables y su justificación técnica.

REGLAS TÉCNICAS ESTRICTAS:
- Solo CSS inline. Sin <style>, sin <link>, sin <script>.
- Fuente base: font-family: 'Helvetica Neue', Arial, sans-serif; line-height: 1.5; color: #222;
- Contenedor principal: <div style="max-width: 850px; margin: 0 auto; padding: 40px; background: white; position: relative;">
- Tablas: width: 100%; border-collapse: collapse; margin-bottom: 24px;
- Si un dato está como "–", escribe "—" (no inventes).

LOGO BASKETMÁTICA:
<img src="https://basketmatica.wordpress.com/wp-content/uploads/2024/07/logo_basketmatica.png" 
     alt="Logo Basketmática" 
     style="position: absolute; top: 40px; right: 40px; width: 90px; opacity: 0.8;" />

RESPUESTA: devuelve ÚNICAMENTE el HTML, sin bloques de código (backticks) de Markdown, sin introducciones ni conclusiones. Empieza con <!DOCTYPE html> y cierra con </html>. Idioma: ESPAÑOL.
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
            # Margen amplio para informes completos (tablas + FODA + análisis).
            # Gemini 2.5 Flash soporta hasta 65 536 tokens de salida.
            max_output_tokens=32768,
        ),
    )

    # Detectar truncamiento u otros finish_reason no normales antes de seguir,
    # para no generar PDFs incompletos silenciosamente.
    candidate = (response.candidates or [None])[0]
    finish = getattr(getattr(candidate, "finish_reason", None), "name", None) \
        or str(getattr(candidate, "finish_reason", "") or "")
    if finish and finish.upper() not in {"STOP", "FINISH_REASON_STOP", ""}:
        if finish.upper() in {"MAX_TOKENS", "FINISH_REASON_MAX_TOKENS"}:
            raise RuntimeError(
                f"Gemini cortó la respuesta por límite de tokens (finish_reason={finish}). "
                "Sube max_output_tokens o reduce la complejidad del prompt."
            )
        if finish.upper() in {"SAFETY", "FINISH_REASON_SAFETY"}:
            raise RuntimeError(
                f"Gemini bloqueó la respuesta por filtro de seguridad "
                f"(finish_reason={finish})."
            )
        logger.warning("Gemini terminó con finish_reason=%s (no STOP).", finish)

    html_content = _limpiar_html_gemini(response.text or "")

    if not html_content.lstrip().startswith("<"):
        raise RuntimeError(
            "Gemini no devolvió HTML válido. Posible filtro de seguridad o "
            "respuesta vacía. Inicio recibido: " + repr(html_content[:120])
        )

    # Sanity check final: el HTML completo debería cerrar con </html>.
    if "</html>" not in html_content.lower():
        logger.warning(
            "El HTML de Gemini no contiene </html>. Probablemente truncado. "
            "Tamaño: %d caracteres.",
            len(html_content),
        )

    # 4) Conversión HTML → PDF.
    logger.info("Generando PDF → %s", output_path)
    HTML(string=html_content).write_pdf(output_path)
    logger.info("✓ PDF generado correctamente.")
    return True
