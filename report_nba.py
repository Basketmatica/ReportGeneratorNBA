"""
report_nba.py — Nombre → datos NBA → análisis (LLM, JSON) → HTML (Python) → PDF.

Cambio de arquitectura respecto a la versión FastAPI/Render:

  * El LLM ya NO genera el HTML. Solo escribe el análisis (desempeño, FODA,
    proyección, similares) como JSON compacto (~1K tokens de salida).
  * Las tablas las renderiza Python directamente desde los datos → los números
    del PDF no pasan por el modelo (imposible alucinarlos) y cabemos en los
    límites de tokens de cualquier free tier (Groq, OpenRouter…).
  * Devuelve bytes (para st.download_button), sin ficheros temporales.
  * Identidad visual Basketmática (BG/teja), coherente con el generador ACB.
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
BG = "#F4EFE5"        # --bg
SURFACE = "#FBF8F0"   # --surface
LINE = "#E2D8C4"      # --line
INK = "#221A10"       # --ink
INK_SOFT = "#6E5E46"  # --ink-soft
BRAND = "#583C14"     # --brand (espresso: titulares e identidad)
ACCENT = "#1F8A74"    # --accent (teal: filetes de datos, highlights)
ACCENT_600 = "#176B5A"
SPOT = "#E8772E"      # --spot (uso puntual)
COURT = "#1B140D"     # --court (cabeceras de tabla oscuras)
COURT_INK = "#EFE8DA" # --court-ink (texto sobre court)
    
FODA_FORTALEZAS = ACCENT      # teal: lo que funciona
FODA_OPORTUNIDADES = SPOT     # naranja balón: energía/potencial
FODA_DEBILIDADES = "#A8442F"  # ← único color fuera de tus tokens
FODA_AMENAZAS = INK_SOFT      # marrón apagado: contexto adverso

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

REGLAS DE RIGOR (obligatorias, prevalecen sobre todo lo demás):
1. Compara SOLO pares de valores que estén AMBOS en el JSON. Si el homólogo de carrera de una métrica no existe, NO compares: describe el valor en solitario. Prohibido citar cualquier número que no aparezca literalmente en el JSON (p.ej. per36 de temporada vs per36 de carrera, AST_TO de temporada vs de carrera, ya calculados).
2. Direccionalidad: TOV% y Pérdidas significan mejor cuanto MÁS BAJOS. TS%, eFG%, AST%, %2P, %3P, %TL y AST/BP significan mejor cuanto más altos. Un TOV% bajo (<13) en un exterior con AST% alto es seguridad de balón de élite: FORTALEZA, jamás debilidad.
3. No hay rankings de liga en estos datos: evita calificativos absolutos salvo que un número del JSON lo haga evidente; prefiere describir.
4. Temporadas con PJ < 15 son muestra no significativa: exclúyelas de tendencias y no cites sus porcentajes.
5. Prohibido mencionar defensa, lesiones, contratos, vestuario o minutos futuros si el JSON no contiene un dato que lo respalde. Cada punto del FODA debe citar al menos un número del JSON.
6. En la tendencia de la trayectoria: di meseta, descenso o mejora según los números reales, no la narrativa amable. Un pico anterior seguido de valores menores es meseta o leve descenso, no "mejora".
7. Si existe "_nota_temporada", la temporada en curso aún no ha empezado y "ultima_temporada" es la última completa: no interpretes la temporada nueva como ausencia o falta de participación del jugador."""


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
            f'<th style="background-color:{COURT};color:{COURT_INK};padding:9px;'
            f'text-align:center;font-size:12.5px;letter-spacing:1px;">{_e(c)}</th>'
            for c in cabecera
        )
        th = f"<tr>{celdas}</tr>"
    trs = ""
    for fila in filas:
        tds = "".join(
            f'<td style="padding:8px;border-bottom:1px solid {LINE};'
            f'text-align:center;font-size:13px;color:{INK};">{_e(c)}</td>'
            for c in fila
        )
        trs += f"<tr>{tds}</tr>"
    return (
        f'<h3 style="color:{INK};font-size:15px;margin:18px 0 8px;">{_e(titulo)}</h3>'
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
        ("Fortalezas", FODA_FORTALEZAS, foda.get("fortalezas") or []),
        ("Oportunidades", FODA_OPORTUNIDADES, foda.get("oportunidades") or []),
        ("Debilidades", FODA_DEBILIDADES, foda.get("debilidades") or []),
        ("Amenazas", FODA_AMENAZAS, foda.get("amenazas") or []),
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
        f'<h2 style="font-family:Georgia,serif;color:{INK};border-bottom:2px solid {ACCENT};'
        f'padding-bottom:8px;font-size:19px;margin-top:28px;">{_e(texto)}</h2>'
    )

# ─── Derivadas y saneado (todo aritmética sobre datos reales) ─────────────────

def _f(v: Any) -> Optional[float]:
    """'26.6' | '51.3%' | '34.0' → float. None si no es numérico."""
    s = str(v if v is not None else "").strip().replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


_P36_CAMPOS = ["Puntos", "Rebotes", "Asistencias", "Robos", "Tapones", "Pérdidas"]


def _per36_de(stats: Dict[str, Any]) -> Dict[str, str]:
    minutos = _f(stats.get("Minutos"))
    if not minutos or minutos <= 0:
        return {}
    factor = 36.0 / minutos
    out: Dict[str, str] = {}
    for campo in _P36_CAMPOS:
        v = _f(stats.get(campo))
        if v is not None:
            out[campo] = f"{v * factor:.1f}"
    return out


def _ast_to_de(stats: Dict[str, Any]) -> Optional[str]:
    ast, to = _f(stats.get("Asistencias")), _f(stats.get("Pérdidas"))
    if ast is None or not to:
        return None
    return f"{ast / to:.1f}"


# Claves candidatas para convertidos/intentados (según cómo los exponga nba_data).
_KEYS_FGM = ("FGM", "TC_conv", "Tiros_convertidos")
_KEYS_FGA = ("FGA", "TC_int", "Tiros_intentados")
_KEYS_3PA = ("3PA", "FG3A", "T3_int", "Triples_intentados")
_KEYS_FTA = ("FTA", "TL_int", "Libres_intentados")
_KEYS_FTM = ("FTM", "TL_conv", "Libres_convertidos")
_KEYS_3PM = ("3PM", "FG3M", "T3_conv", "Triples_convertidos")


def _get_any(stats: Dict[str, Any], keys) -> Optional[float]:
    for k in keys:
        if k in stats:
            return _f(stats.get(k))
    return None


def _eficiencias_tiro(stats: Dict[str, Any]) -> Dict[str, str]:
    """
    TS%, eFG%, 3PAr y FTr con las fórmulas estándar, SOLO si los intentos
    están en los datos. Si nba_data no expone FGA/3PA/FTA, devuelve {} y la
    tabla simplemente no aparece (nunca se estima ni inventa).
      TS%  = PTS / (2 * (FGA + 0.44 * FTA))
      eFG% = (FGM + 0.5 * 3PM) / FGA
    """
    pts = _f(stats.get("Puntos"))
    fga = _get_any(stats, _KEYS_FGA)
    fta = _get_any(stats, _KEYS_FTA)
    fgm = _get_any(stats, _KEYS_FGM)
    tpm = _get_any(stats, _KEYS_3PM)
    tpa = _get_any(stats, _KEYS_3PA)
    out: Dict[str, str] = {}
    if pts is not None and fga and fta is not None and (fga + 0.44 * fta) > 0:
        out["TS% (calculado)"] = f"{100 * pts / (2 * (fga + 0.44 * fta)):.1f}%"
    if fgm is not None and tpm is not None and fga:
        out["eFG% (calculado)"] = f"{100 * (fgm + 0.5 * tpm) / fga:.1f}%"
    if tpa is not None and fga:
        out["3PAr (calculado)"] = f"{100 * tpa / fga:.1f}%"
    if fta is not None and fga:
        out["FTr (calculado)"] = f"{100 * fta / fga:.1f}%"
    if fga is not None:
        out["Tiros de campo int./partido"] = f"{fga:.1f}"
    if tpa is not None:
        out["Triples int./partido"] = f"{tpa:.1f}"
    return out


# Whitelist ESPN: acumulados con valor de scouting y ratios interpretables.
# Fuera: disciplinarias (no discriminan) y eficiencias sin definición pública.
_AVANZADAS_NBA_OK = {"Dobles_dobles", "Triples_dobles", "AST_TO_ratio"}


def preparar_datos(player_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Enriquecimiento + saneado previo a prompt y render (misma vista para ambos):
      * per-36 y AST/TO de CARRERA calculados (evita que el modelo se invente
        el homólogo de carrera al comparar).
      * AST/TO de la última temporada calculado.
      * Eficiencias de tiro calculadas si hay intentos en los datos.
      * Avanzadas ESPN filtradas por whitelist.
    """
    data = json.loads(json.dumps(player_data))  # deep copy
    est = data.get("Estadísticas", {})

    ult = est.get("ultima_temporada") or {}
    if ult:
        if not ult.get("per36"):
            p36 = _per36_de(ult)
            if p36:
                ult["per36"] = p36
        at = _ast_to_de(ult)
        if at:
            ult["AST_TO (calculado)"] = at
        ef = _eficiencias_tiro(ult)
        if ef:
            ult["eficiencia_tiro_calculada"] = ef

    carrera = est.get("carrera_promedio") or {}
    if carrera:
        p36c = _per36_de(carrera)
        if p36c:
            carrera["per36"] = p36c
        atc = _ast_to_de(carrera)
        if atc:
            carrera["AST_TO (calculado)"] = atc

    avanz = est.get("avanzadas_ultima") or {}
    if avanz:
        limpio = {k: v for k, v in avanz.items() if k in _AVANZADAS_NBA_OK}
        if limpio:
            est["avanzadas_ultima"] = limpio
        else:
            est.pop("avanzadas_ultima", None)

    return data

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
    <div style="display:flex;align-items:center;gap:24px;border-bottom:3px solid {ACCENT};
                padding-bottom:20px;margin-bottom:26px;">
      {img}
      <div>
        <h1 style="font-family:Georgia,serif;color:{INK};margin:0 0 8px;font-size:28px;letter-spacing:.5px;">{_e(bio.get("Nombre"))}</h1>
        <p style="margin:0;font-size:16px;color:{INK_SOFT};">
          {_e(bio.get("Posición"))} · {_e(bio.get("Equipo"))} · Dorsal {_e(bio.get("Dorsal"))}
        </p>
        <p style="margin:6px 0 0;font-size:11px;color:{ACCENT};text-transform:uppercase;letter-spacing:2px;">
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
        filas_ef = []
        ef = ult.get("eficiencia_tiro_calculada") or {}
        for k, v in ef.items():
            filas_ef.append([k, v])
        if ult.get("AST_TO (calculado)"):
            filas_ef.append(["AST/TO (calculado)", ult["AST_TO (calculado)"]])
        if filas_ef:
            stats_html += _tabla(
                "Eficiencia y volumen de tiro", filas_ef, ["Métrica", "Valor"]
            )

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
        p36c = carrera.get("per36") or {}
        if p36c:
            cab, fila = _fila_stats(p36c, _ORDEN_P36)
            stats_html += _tabla("Per-36 de carrera (calculado)", [fila], cab)

    avanz = est.get("avanzadas_ultima") or {}
    if avanz:
        filas = [
            [k.replace("_", " "), str(v)]
            for k, v in avanz.items()
            if not k.startswith(("_", "Temporada")) and v not in ("", "–", None)
        ]
        stats_html += _tabla("Acumulados destacados (última temporada)", filas)

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
        f'<p style="margin-top:32px;padding-top:12px;border-top:1px solid {ACCENT};'
        f'font-size:10.5px;color:{INK_SOFT};text-transform:uppercase;letter-spacing:2px;">'
        f"Basketmática · basketmatica.com · Datos: balldontlie / ESPN · Análisis: {modelo}</p>"
    )

    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"></head>
<body style="background-color:{BG};margin:0;
             font-family:Georgia,'Times New Roman',serif;line-height:1.55;color:{INK};">
  <div style="max-width:850px;margin:0 auto;padding:40px;background-color:{BG};position:relative;">
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

    player_data = preparar_datos(player_data)

    analisis = generar_json(
        _prompt_analisis(player_data),
        proveedores,
        system=_SYSTEM,
        max_tokens=3500,
    )

    html_doc = render_html(player_data, analisis)
    pdf: bytes = HTML(string=html_doc).write_pdf()
    logger.info("✓ PDF generado (%d KB).", len(pdf) // 1024)
    return pdf
