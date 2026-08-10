"""
report_nba.py — Nombre → datos NBA → análisis (LLM, JSON) → HTML (Python) → PDF.

Cambio de arquitectura respecto a la versión FastAPI/Render:

  * El LLM ya NO genera el HTML. Solo escribe el análisis (desempeño, FODA,
    proyección, similares) como JSON compacto (~1K tokens de salida).
  * Las tablas las renderiza Python directamente desde los datos → los números
    del PDF no pasan por el modelo (imposible alucinarlos) y cabemos en los
    límites de tokens de cualquier free tier (Groq, OpenRouter…).
  * Devuelve bytes (para st.download_button), sin ficheros temporales.
  * Identidad visual Basketmática (crema/teja), coherente con el generador ACB.
"""

from __future__ import annotations

import html
import json
import logging
from typing import Any, Dict, List, Optional

from weasyprint import HTML

from llm_client import ProviderConfig, generar_json
from nba_data import obtener_datos_jugador

logger = logging.getLogger(__name__)

# ─── Paleta Basketmática ──────────────────────────────────────────────────────
CREMA = "#FAF6EE"
TINTA = "#1A1A1A"
TINTA_2 = "#6B6B6B"
TEJA = "#C0562F"
VERDE = "#4A7B4E"
ROJO = "#A84438"
MARRON = "#8B7355"
LINEA = "#E3DCCB"

LOGO_URL = "https://basketmatica.wordpress.com/wp-content/uploads/2024/07/logo_basketmatica.png"


# ─── Prompt de análisis (solo JSON) ───────────────────────────────────────────

_SYSTEM = (
    "Eres un analista profesional de baloncesto especializado en estadística "
    "avanzada y scouting. Escribes para Basketmática: registro sobrio y técnico, "
    "sin épica. Respondes SIEMPRE con un único objeto JSON válido, sin Markdown."
)


def _prompt_analisis(player_data: Dict[str, Any]) -> str:
    return f"""A partir de este JSON con datos reales de un jugador NBA, redacta el análisis en ESPAÑOL.

=== DATOS ===
{json.dumps(player_data, ensure_ascii=False)}
=============

Devuelve EXACTAMENTE este esquema JSON (sin campos extra, sin Markdown):
{{
  "resumen_desempeno": "Párrafo de 100-150 palabras. Compara última temporada vs carrera usando per-36 y ratios de eficiencia si existen. Cita números concretos del JSON.",
  "foda": {{
    "fortalezas": ["2-3 puntos, máx. 25 palabras cada uno"],
    "oportunidades": ["2-3 puntos"],
    "debilidades": ["2-3 puntos"],
    "amenazas": ["2-3 puntos"]
  }},
  "proyeccion": "Párrafo de 80-100 palabras sobre rol y sostenibilidad (si es veterano, enfócalo ahí).",
  "similares": [
    {{"nombre": "Jugador comparable", "razon": "justificación técnica de una línea"}},
    {{"nombre": "...", "razon": "..."}}
  ]
}}

Reglas: básate al 100% en los datos del JSON; no inventes lesiones, contratos ni contexto de mercado; si un dato es "–", ignóralo."""


# ─── Render HTML determinista ─────────────────────────────────────────────────


def _e(v: Any) -> str:
    s = str(v if v is not None else "—").strip()
    return html.escape("—" if s in ("", "–") else s)


def _tabla(titulo: str, filas: List[List[str]], cabecera: Optional[List[str]] = None) -> str:
    if not filas:
        return ""
    th = ""
    if cabecera:
        celdas = "".join(
            f'<th style="background-color:{TINTA};color:{CREMA};padding:9px;'
            f'text-align:center;font-size:12.5px;letter-spacing:1px;">{_e(c)}</th>'
            for c in cabecera
        )
        th = f"<tr>{celdas}</tr>"
    trs = ""
    for fila in filas:
        tds = "".join(
            f'<td style="padding:8px;border-bottom:1px solid {LINEA};'
            f'text-align:center;font-size:13px;color:{TINTA};">{_e(c)}</td>'
            for c in fila
        )
        trs += f"<tr>{tds}</tr>"
    return (
        f'<h3 style="color:{TINTA};font-size:15px;margin:18px 0 8px;">{_e(titulo)}</h3>'
        f'<table style="width:100%;border-collapse:collapse;margin-bottom:20px;">{th}{trs}</table>'
    )


_ORDEN_PG = [
    ("Puntos", "PTS"), ("Rebotes", "REB"), ("Asistencias", "AST"),
    ("Robos", "STL"), ("Tapones", "BLK"), ("Pérdidas", "TO"),
    ("Minutos", "MIN"), ("FG%", "FG%"), ("3P%", "3P%"), ("FT%", "FT%"),
    ("Partidos", "PJ"),
]

_ORDEN_P36 = [
    ("Puntos", "PTS"), ("Rebotes", "REB"), ("Asistencias", "AST"),
    ("Robos", "STL"), ("Tapones", "BLK"), ("Pérdidas", "TO"),
]


def _fila_stats(stats: Dict[str, Any], orden: List) -> (List[str], List[str]):
    cab = [et for k, et in orden if stats.get(k) not in (None, "", "–")]
    fila = [str(stats.get(k)) for k, et in orden if stats.get(k) not in (None, "", "–")]
    return cab, fila


def _seccion_foda(foda: Dict[str, List[str]]) -> str:
    bloques = [
        ("Fortalezas", VERDE, foda.get("fortalezas") or []),
        ("Oportunidades", MARRON, foda.get("oportunidades") or []),
        ("Debilidades", ROJO, foda.get("debilidades") or []),
        ("Amenazas", TINTA_2, foda.get("amenazas") or []),
    ]
    secciones = ""
    for titulo, color, puntos in bloques:
        lis = "".join(f"<li>{_e(p)}</li>" for p in puntos) or "<li>—</li>"
        secciones += (
            f'<section style="background-color:#F2EDE0;border-left:4px solid {color};'
            f'padding:14px 16px;border-radius:4px;">'
            f'<h3 style="color:{color};margin:0 0 8px;font-size:14px;'
            f'text-transform:uppercase;letter-spacing:1px;">{titulo}</h3>'
            f'<ul style="margin:0;padding-left:18px;font-size:13px;line-height:1.5;">{lis}</ul>'
            f"</section>"
        )
    return (
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;'
        f'margin-bottom:24px;">{secciones}</div>'
    )


def _h2(texto: str) -> str:
    return (
        f'<h2 style="color:{TINTA};border-bottom:2px solid {TEJA};'
        f'padding-bottom:8px;font-size:19px;margin-top:28px;">{_e(texto)}</h2>'
    )


def render_html(player_data: Dict[str, Any], analisis: Dict[str, Any]) -> str:
    bio = player_data.get("Datos personales", {})
    est = player_data.get("Estadísticas", {})

    # Cabecera
    foto = bio.get("Foto") or ""
    img = (
        f'<img src="{html.escape(foto)}" alt="" '
        f'style="max-width:150px;border-radius:8px;"/>' if foto else ""
    )
    cabecera = f"""
    <div style="display:flex;align-items:center;gap:24px;border-bottom:3px solid {TEJA};
                padding-bottom:20px;margin-bottom:26px;">
      {img}
      <div>
        <h1 style="color:{TINTA};margin:0 0 8px;font-size:28px;letter-spacing:.5px;">{_e(bio.get("Nombre"))}</h1>
        <p style="margin:0;font-size:16px;color:{TINTA_2};">
          {_e(bio.get("Posición"))} · {_e(bio.get("Equipo"))} · Dorsal {_e(bio.get("Dorsal"))}
        </p>
        <p style="margin:6px 0 0;font-size:11px;color:{TEJA};text-transform:uppercase;letter-spacing:2px;">
          Informe de scouting · NBA
        </p>
      </div>
    </div>"""

    # Perfil
    campos_bio = [
        "Equipo", "Posición", "Altura", "Peso", "País",
        "Universidad", "Draft", "Dorsal", "Temporadas_NBA",
    ]
    filas_bio = [
        [c.replace("_", " "), str(bio.get(c))]
        for c in campos_bio
        if bio.get(c) not in (None, "", "–")
    ]
    perfil = _h2("Perfil físico y draft") + _tabla("", filas_bio)

    # Estadísticas
    stats_html = _h2("Métricas de rendimiento")
    ult = est.get("ultima_temporada") or {}
    if ult:
        cab, fila = _fila_stats(ult, _ORDEN_PG)
        stats_html += _tabla(
            f"Promedios por partido — {_e(ult.get('Temporada', 'última temporada'))}",
            [fila], cab,
        )
        p36 = ult.get("per36") or {}
        if p36:
            cab, fila = _fila_stats(p36, _ORDEN_P36)
            stats_html += _tabla("Per-36 minutos (calculado)", [fila], cab)

    anteriores = est.get("temporadas_anteriores") or []
    if len(anteriores) >= 2:
        filas = [
            [s.get("Temporada", "—"), s.get("Puntos", "—"), s.get("Rebotes", "—"),
             s.get("Asistencias", "—"), s.get("FG%", "—"), s.get("3P%", "—")]
            for s in anteriores[:5]
        ]
        stats_html += _tabla(
            "Trayectoria reciente", filas,
            ["Temporada", "PTS", "REB", "AST", "FG%", "3P%"],
        )

    carrera = est.get("carrera_promedio") or {}
    if carrera:
        cab, fila = _fila_stats(carrera, _ORDEN_PG)
        stats_html += _tabla("Promedios de carrera", [fila], cab)

    avanz = est.get("avanzadas_ultima") or {}
    if avanz:
        filas = [
            [k.replace("_", " "), str(v)]
            for k, v in avanz.items()
            if not k.startswith(("_", "Temporada")) and v not in ("", "–", None)
        ]
        stats_html += _tabla("Estadísticas avanzadas (última temporada)", filas)

    # Análisis del LLM
    analisis_html = (
        _h2("Análisis de desempeño")
        + f'<p style="font-size:13.5px;line-height:1.6;">{_e(analisis.get("resumen_desempeno"))}</p>'
        + _h2("Análisis FODA")
        + _seccion_foda(analisis.get("foda") or {})
        + _h2("Proyección")
        + f'<p style="font-size:13.5px;line-height:1.6;">{_e(analisis.get("proyeccion"))}</p>'
        + _h2("Perfiles similares")
        + "<ul style='font-size:13.5px;line-height:1.7;'>"
        + "".join(
            f"<li><strong>{_e(s.get('nombre'))}</strong>: {_e(s.get('razon'))}</li>"
            for s in (analisis.get("similares") or [])
            if isinstance(s, dict)
        )
        + "</ul>"
    )

    modelo = _e(analisis.get("_modelo", ""))
    pie = (
        f'<p style="margin-top:32px;padding-top:12px;border-top:1px solid {TEJA};'
        f'font-size:10.5px;color:{TINTA_2};text-transform:uppercase;letter-spacing:2px;">'
        f"Basketmática · basketmatica.com · Datos: balldontlie / ESPN · Análisis: {modelo}</p>"
    )

    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"></head>
<body style="background-color:{CREMA};margin:0;
             font-family:'Helvetica Neue',Arial,sans-serif;line-height:1.55;color:{TINTA};">
  <div style="max-width:850px;margin:0 auto;padding:40px;background-color:{CREMA};position:relative;">
    <img src="{LOGO_URL}" alt="Basketmática"
         style="position:absolute;top:40px;right:40px;width:90px;opacity:.85;"/>
    {cabecera}
    {perfil}
    {stats_html}
    {analisis_html}
    {pie}
  </div>
</body></html>"""


# ─── Pipeline principal ───────────────────────────────────────────────────────


def generar_pdf_jugador_nba(
    nombre_jugador: str,
    proveedores: List[ProviderConfig],
    player_data: Optional[Dict[str, Any]] = None,
) -> bytes:
    """Pipeline completo. Devuelve el PDF como bytes."""
    logger.info("=== Informe NBA para: '%s' ===", nombre_jugador)

    if player_data is None:
        player_data = obtener_datos_jugador(nombre_jugador)

    analisis = generar_json(
        _prompt_analisis(player_data),
        proveedores,
        system=_SYSTEM,
        max_tokens=2500,
    )

    html_doc = render_html(player_data, analisis)
    pdf: bytes = HTML(string=html_doc).write_pdf()
    logger.info("✓ PDF generado (%d KB).", len(pdf) // 1024)
    return pdf
