"""
report_nba.py — Datos de balldontlie + ESPN → vista del informe → PDF.

Solo contiene lo propio de la NBA: la configuración de la competición, la
vista construida desde nba_data y los ratios calculados. El prompt, la
normalización del texto de la IA y el render son comunes (informe_comun.py,
idéntico en el generador ACB).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from informe_comun import Competicion, generar_pdf
from llm_client import ProviderConfig
from nba_data import obtener_datos_jugador

logger = logging.getLogger(__name__)

COMPETICION = Competicion(
    nombre="NBA",
    fuente="balldontlie · ESPN",
    ambito_similares="la NBA",
    minutos_normalizacion=36,
    pj_minimo=15,
    metricas_clave="per-36, %TC, %3P y %TL, TS% y eFG% calculados y AST/TO",
    contexto_metricas=(
        "- No hay rankings de liga ni valoración (PIR) en estos datos.\n"
        '- "%TC" es el porcentaje de tiros de campo (2 y 3 puntos juntos).\n'
        '- "avanzadas_oficiales" son datos OFICIALES de ESPN de la temporada de referencia: '
        "dobles-dobles y triples-dobles (partidos totales) y AST/TO.\n"
        '- "trayectoria" son sus últimas temporadas NBA con el club de cada año; si cambió de '
        "equipo a mitad de temporada, la fila es el total y \"Club\" lista los equipos."
    ),
    col_promedios=[
        ("Partidos", "PJ"), ("Minutos", "MIN"), ("Puntos", "PTS"),
        ("%TC", "%TC"), ("%3P", "%3P"), ("%TL", "%TL"),
        ("Rebotes", "REB"), ("Asistencias", "AST"),
        ("Recuperaciones", "REC"), ("Tapones", "TAP"), ("Pérdidas", "PÉR"),
    ],
    col_normalizado=[
        ("Puntos", "PTS"), ("Rebotes", "REB"), ("Asistencias", "AST"),
        ("Recuperaciones", "REC"), ("Tapones", "TAP"), ("Pérdidas", "PÉR"),
    ],
    col_trayectoria=[
        ("Temporada", "Temp"), ("Club", "Club"), ("Partidos", "PJ"), ("Minutos", "MIN"),
        ("Puntos", "PTS"), ("%TC", "%TC"), ("%3P", "%3P"), ("%TL", "%TL"),
        ("Rebotes", "REB"), ("Asistencias", "AST"), ("Pérdidas", "PÉR"),
    ],
    titulo_avanzadas="Estadísticas complementarias (oficiales ESPN)",
    leyenda_avanzadas=(
        "Dobles-dobles y triples-dobles: partidos de la temporada con dos o tres categorías "
        "en dobles cifras · AST/TO: asistencias por pérdida."
    ),
    leyenda_ratios=(
        "Calculados por Basketmática a partir de los promedios por partido de ESPN: "
        "TS% = PTS ÷ (2 × (TC intentados + 0,44 × TL intentados)) · eFG% = (TC anotados + "
        "0,5 × triples anotados) ÷ TC intentados · 3PAr y FTr: triples y tiros libres "
        "intentados por tiro de campo intentado. Los intentos por partido son datos oficiales."
    ),
)

_POSICIONES = {
    "G": "Base / escolta", "F": "Alero / ala-pívot", "C": "Pívot",
    "G-F": "Escolta / alero", "F-G": "Alero / escolta",
    "F-C": "Ala-pívot / pívot", "C-F": "Pívot / ala-pívot",
}

# Claves de nba_data → vocabulario común con el generador ACB.
_CLAVES_ES = {"FG%": "%TC", "3P%": "%3P", "FT%": "%TL", "Robos": "Recuperaciones"}


def _f(v: Any) -> Optional[float]:
    """'26.6' | '51.3%' → float. None si no es numérico."""
    try:
        return float(str(v if v is not None else "").replace("%", "").strip())
    except ValueError:
        return None


def _claves_es(stats: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {_CLAVES_ES.get(k, k): v for k, v in (stats or {}).items()}


def _temporada_corta(t: str) -> str:
    """'2024-25' → '24-25', como la trayectoria de acb.com."""
    return t[2:] if re.match(r"^\d{4}-\d{2}$", t or "") else t


def _ratios(stats: Dict[str, Any], ast_to_oficial: bool) -> Dict[str, str]:
    """
    TS%, eFG%, 3PAr y FTr con las fórmulas estándar, SOLO si los intentos están
    en los datos (nunca se estiman). AST/TO solo si ESPN no lo da oficial.
    """
    pts, fgm, fga = _f(stats.get("Puntos")), _f(stats.get("TC_conv")), _f(stats.get("TC_int"))
    tpm, tpa, fta = _f(stats.get("T3_conv")), _f(stats.get("T3_int")), _f(stats.get("TL_int"))
    ast, to = _f(stats.get("Asistencias")), _f(stats.get("Pérdidas"))
    out: Dict[str, str] = {}
    if pts is not None and fga and fta is not None:
        out["TS% (calculado)"] = f"{100 * pts / (2 * (fga + 0.44 * fta)):.1f}%"
    if fgm is not None and tpm is not None and fga:
        out["eFG% (calculado)"] = f"{100 * (fgm + 0.5 * tpm) / fga:.1f}%"
    if tpa is not None and fga:
        out["3PAr (calculado)"] = f"{100 * tpa / fga:.1f}%"
    if fta is not None and fga:
        out["FTr (calculado)"] = f"{100 * fta / fga:.1f}%"
    if ast is not None and to and not ast_to_oficial:
        out["AST/TO (calculado)"] = f"{ast / to:.1f}"
    for clave, etiqueta in (
        ("TC_int", "Tiros de campo intentados/partido"),
        ("T3_int", "Triples intentados/partido"),
        ("TL_int", "Tiros libres intentados/partido"),
    ):
        if stats.get(clave) not in (None, "", "–"):
            out[etiqueta] = str(stats[clave])
    return out


def _metros(altura: str) -> str:
    m = re.search(r"\((\d+) cm\)", altura or "")
    return f"{int(m.group(1)) / 100:.2f} m" if m else altura


def _kilos(peso: str) -> str:
    m = re.search(r"\((\d+) kg\)", peso or "")
    return f"{m.group(1)} kg" if m else peso


def construir_vista(player_data: Dict[str, Any]) -> Dict[str, Any]:
    bio = player_data.get("Datos personales", {})
    est = player_data.get("Estadísticas", {})
    norm = COMPETICION.clave_normalizado
    ult = est.get("ultima_temporada") or {}
    carrera = est.get("carrera_promedio") or {}

    misc = est.get("avanzadas_ultima") or {}
    if misc.get("Temporada") != ult.get("Temporada"):
        misc = {}
    avanzadas = {
        "Dobles-dobles": misc.get("Dobles_dobles"),
        "Triples-dobles": misc.get("Triples_dobles"),
        "AST/TO": misc.get("AST_TO_ratio"),
    }

    trayectoria = [
        {**_claves_es(s), "Temporada": _temporada_corta(s.get("Temporada", ""))}
        for s in ([ult] if ult else []) + list(est.get("temporadas_anteriores") or [])
    ]

    return {
        "jugador": {
            "Nombre": bio.get("Nombre"),
            "Posición": _POSICIONES.get(str(bio.get("Posición", "")).upper(), bio.get("Posición")),
            "Equipo": bio.get("Equipo"),
            "Dorsal": bio.get("Dorsal"),
            "Fecha nacimiento": bio.get("Fecha nacimiento"),
            "Altura": _metros(bio.get("Altura", "")),
            "Peso": _kilos(bio.get("Peso", "")),
            "País": bio.get("País"),
            "Universidad": bio.get("Universidad"),
            "Draft": bio.get("Draft"),
            "Temporadas en la NBA": bio.get("Temporadas_NBA"),
        },
        "alias": [],
        "foto": bio.get("Foto"),
        "temporada": {
            "etiqueta": ult.get("Temporada", ""),
            "promedios": _claves_es(ult),
            norm: _claves_es(ult.get("per36")),
            "ratios": _ratios(ult, ast_to_oficial=bool(avanzadas["AST/TO"])),
        },
        "carrera": {"promedios": _claves_es(carrera), norm: _claves_es(carrera.get("per36"))},
        "avanzadas_oficiales": avanzadas,
        "trayectoria": trayectoria,
        "nota_temporada": est.get("_nota_temporada"),
    }


def generar_pdf_jugador_nba(
    nombre_jugador: str,
    proveedores: List[ProviderConfig],
    player_data: Optional[Dict[str, Any]] = None,
) -> bytes:
    """
    Pipeline completo. Devuelve el PDF como bytes.

    Raises
    ------
    ValueError        Jugador no encontrado.
    EnvironmentError  Ningún proveedor de LLM o BALLDONTLIE_API_KEY configurados.
    RuntimeError      Errores transitorios de red / API.
    """
    logger.info("=== Informe NBA para: '%s' ===", nombre_jugador)
    if player_data is None:
        player_data = obtener_datos_jugador(nombre_jugador)
    return generar_pdf(construir_vista(player_data), COMPETICION, proveedores)
