from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from functools import lru_cache
from typing import Any, Dict, List, Optional

import httpx
from nba_api.stats.static import players as players_static

logger = logging.getLogger(__name__)


# ─── Configuración ────────────────────────────────────────────────────────────

BDL_BASE = "https://api.balldontlie.io/nba/v1"
BDL_API_KEY = os.getenv("BALLDONTLIE_API_KEY", "").strip()

NBA_HEADSHOT_URL = (
    "https://cdn.nba.com/headshots/nba/latest/1040x760/{player_id}.png"
)

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# Rate limit del free tier de balldontlie: ~30 req/min. Forzamos un mínimo
# entre llamadas para no chocar con 429.
_MIN_INTERVAL_SEC = 0.6
_last_request_time = 0.0


# ─── Cliente HTTP ─────────────────────────────────────────────────────────────


def _throttle() -> None:
    """Respeta el rate limit del free tier de balldontlie."""
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _MIN_INTERVAL_SEC:
        time.sleep(_MIN_INTERVAL_SEC - elapsed)
    _last_request_time = time.monotonic()


def _bdl_get(path: str, params: Optional[dict] = None, retries: int = 3) -> dict:
    """
    GET autenticado a balldontlie con throttling y reintentos exponenciales.

    Parameters
    ----------
    path : str
        Ruta sin host (p.ej. ``"/players"``).
    params : dict, opcional
        Query string. Soporta listas (se serializan como ``key[]=v1&key[]=v2``).
    retries : int
        Número de intentos antes de rendirse.

    Returns
    -------
    dict
        Respuesta JSON parseada.

    Raises
    ------
    EnvironmentError
        Si la API key no está configurada o es inválida.
    RuntimeError
        Si tras todos los reintentos la petición sigue fallando.
    """
    if not BDL_API_KEY:
        raise EnvironmentError(
            "Variable de entorno 'BALLDONTLIE_API_KEY' no configurada. "
            "Crea una clave gratuita (sin tarjeta) en https://app.balldontlie.io "
            "y añádela en Render → Environment → Add Environment Variable."
        )

    url = f"{BDL_BASE}{path}"
    headers = {"Authorization": BDL_API_KEY}
    delay = 1.5
    last_err: Optional[Exception] = None

    for intento in range(retries):
        _throttle()
        try:
            r = httpx.get(
                url, headers=headers, params=params, timeout=DEFAULT_TIMEOUT
            )
        except httpx.HTTPError as exc:
            last_err = exc
            logger.warning(
                "balldontlie %s → error de red (%s). Reintento %d/%d en %.1fs.",
                path, exc.__class__.__name__, intento + 1, retries, delay,
            )
            time.sleep(delay)
            delay *= 2
            continue

        if r.status_code == 200:
            return r.json()

        if r.status_code in (401, 403):
            raise EnvironmentError(
                "BALLDONTLIE_API_KEY inválida o sin permisos suficientes. "
                "Comprueba la clave en https://app.balldontlie.io."
            )

        if r.status_code == 404:
            # Recurso no existe — no es recuperable.
            raise RuntimeError(f"balldontlie {path} → 404 Not Found.")

        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After") or delay * 4)
            logger.warning("balldontlie %s → 429 rate limit. Esperando %.1fs.", path, wait)
            time.sleep(wait)
            delay *= 2
            continue

        if r.status_code >= 500:
            logger.warning(
                "balldontlie %s → %d. Reintento en %.1fs.", path, r.status_code, delay,
            )
            time.sleep(delay)
            delay *= 2
            continue

        # 4xx no recuperables
        raise RuntimeError(
            f"balldontlie {path} → HTTP {r.status_code}: {r.text[:200]}"
        )

    raise RuntimeError(
        f"balldontlie {path} no respondió tras {retries} reintentos: {last_err}"
    )


# ─── Resolución de nombre → ID NBA (offline) ─────────────────────────────────


def buscar_jugador_nba(nombre_jugador: str) -> Dict[str, Any]:
    """
    Resuelve un nombre de jugador a su entrada en la BD estática de nba_api.

    Esta función NO hace ninguna llamada de red — usa el JSON empaquetado en
    ``nba_api`` que se actualiza cada nueva versión del paquete.
    """
    nombre = (nombre_jugador or "").strip()
    if not nombre:
        raise ValueError("El nombre del jugador no puede estar vacío.")

    # 1) Coincidencia por nombre completo (regex de nba_api).
    resultados = players_static.find_players_by_full_name(nombre)

    # 2) Fallback: búsqueda por apellido + filtrado manual.
    if not resultados:
        partes = nombre.split()
        if partes:
            apellido = partes[-1]
            candidatos = players_static.find_players_by_last_name(apellido)
            n_low = nombre.lower()
            resultados = [
                p for p in candidatos
                if n_low in p["full_name"].lower()
                or p["full_name"].lower() in n_low
            ]

    if not resultados:
        raise ValueError(
            f"Jugador '{nombre}' no encontrado. Verifica la ortografía y "
            "usa el nombre en inglés (p.ej. 'LeBron James')."
        )

    # Preferimos jugadores activos cuando hay homónimos.
    activos = [p for p in resultados if p.get("is_active")]
    elegido = activos[0] if activos else resultados[0]

    return {
        "id": int(elegido["id"]),
        "full_name": str(elegido["full_name"]),
        "is_active": bool(elegido.get("is_active")),
    }


# ─── Búsqueda y bio en balldontlie ───────────────────────────────────────────


@lru_cache(maxsize=256)
def _bdl_buscar_jugador(nombre: str) -> Dict[str, Any]:
    """
    Busca un jugador en balldontlie por nombre y devuelve la mejor coincidencia.
    Cachea para no volver a pedir el mismo jugador en la misma instancia.
    """
    data = _bdl_get("/players", params={"search": nombre, "per_page": 25})
    candidatos = data.get("data", [])
    if not candidatos:
        raise ValueError(
            f"Jugador '{nombre}' no aparece en balldontlie. "
            "Comprueba la ortografía (en inglés)."
        )

    n_low = nombre.lower()
    # Match exacto > contiene apellido > primero que devuelve.
    for c in candidatos:
        full = f"{c.get('first_name', '')} {c.get('last_name', '')}".strip().lower()
        if full == n_low:
            return c
    for c in candidatos:
        full = f"{c.get('first_name', '')} {c.get('last_name', '')}".strip().lower()
        if all(t in full for t in n_low.split()):
            return c
    return candidatos[0]


def _equipo_str(p: dict) -> str:
    """Construye el nombre de equipo a partir de un objeto player de balldontlie."""
    if not isinstance(p, dict):
        return "Sin equipo"
    team = p.get("team")
    if isinstance(team, dict) and team.get("full_name"):
        return team["full_name"]
    if p.get("team_id"):
        # Sin info de equipo embebida; pedimos al endpoint /teams/:id si hace falta.
        return f"Team #{p['team_id']}"
    return "Sin equipo"


def _altura_fmt(s: str) -> str:
    """'6-9' → '6-9 ft (206 cm)'."""
    if not s:
        return "–"
    try:
        pies, pulgadas = (int(x) for x in str(s).split("-"))
        cm = round(pies * 30.48 + pulgadas * 2.54)
        return f"{s} ft ({cm} cm)"
    except (ValueError, TypeError):
        return str(s)


def _peso_fmt(s: str) -> str:
    """'250' → '250 lb (113 kg)'."""
    if not s:
        return "–"
    try:
        kg = round(int(str(s).strip()) * 0.453592)
        return f"{s} lb ({kg} kg)"
    except (ValueError, TypeError):
        return str(s)


def _draft_fmt(p: dict) -> str:
    year = p.get("draft_year")
    if not year:
        return "No draftado"
    rnd = p.get("draft_round") or "?"
    pick = p.get("draft_number") or "?"
    return f"{year}, Ronda {rnd}, Pick #{pick}"


# ─── Estadísticas: temporadas recientes ──────────────────────────────────────


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (ValueError, TypeError):
        return None


def _temporada_actual_nba() -> int:
    """
    Devuelve el año de inicio de la temporada NBA en curso.
    La temporada NBA arranca en octubre, así que de Oct→Dic es `año actual`,
    y de Ene→Sep es `año actual - 1`.
    """
    hoy = datetime.utcnow()
    return hoy.year if hoy.month >= 10 else hoy.year - 1


def _season_average(player_id: int, season: int) -> Optional[Dict[str, Any]]:
    """Media de una temporada concreta. None si el jugador no jugó."""
    try:
        data = _bdl_get(
            "/season_averages",
            params={"season": season, "player_ids[]": player_id},
        )
    except RuntimeError as exc:
        logger.warning("season_averages season=%d player=%d → %s", season, player_id, exc)
        return None
    arr = data.get("data") or []
    return arr[0] if arr else None


def _temporadas_recientes(
    player_id: int, draft_year: Optional[int], n_temporadas: int = 6
) -> List[Dict[str, Any]]:
    """
    Recoge las medias de las últimas ``n_temporadas`` temporadas en las que el
    jugador realmente jugó. Para evitar 22 llamadas para LeBron, paramos en
    cuanto recolectamos n_temporadas con datos o llegamos a la temporada del draft.
    """
    actual = _temporada_actual_nba()
    primer_anyo = (draft_year or 1980)
    recolectadas: List[Dict[str, Any]] = []

    for season in range(actual, primer_anyo - 1, -1):
        if len(recolectadas) >= n_temporadas:
            break
        avg = _season_average(player_id, season)
        if avg:
            avg["__season"] = season
            recolectadas.append(avg)

    # Ordenadas de más reciente a más antigua.
    return recolectadas


def _stats_temporada(avg: Dict[str, Any]) -> Dict[str, str]:
    """Formatea una fila de season_averages a strings legibles."""
    def n(v, dec=1):
        f = _safe_float(v)
        return "–" if f is None else f"{round(f, dec)}"

    def pct(v):
        f = _safe_float(v)
        if f is None:
            return "–"
        if f <= 1.0:
            f *= 100
        return f"{round(f, 1)}%"

    # min llega como "MM:SS" o número decimal.
    minutos_raw = avg.get("min")
    if isinstance(minutos_raw, str) and ":" in minutos_raw:
        try:
            mm, ss = minutos_raw.split(":")
            minutos = f"{int(mm) + int(ss) / 60:.1f}"
        except (ValueError, TypeError):
            minutos = minutos_raw
    else:
        minutos = n(minutos_raw)

    return {
        "Partidos": n(avg.get("games_played"), 0),
        "Minutos": minutos,
        "Puntos": n(avg.get("pts")),
        "Rebotes": n(avg.get("reb")),
        "Rebotes_ofensivos": n(avg.get("oreb")),
        "Rebotes_defensivos": n(avg.get("dreb")),
        "Asistencias": n(avg.get("ast")),
        "Robos": n(avg.get("stl")),
        "Tapones": n(avg.get("blk")),
        "Pérdidas": n(avg.get("turnover")),
        "Faltas": n(avg.get("pf")),
        "FG%": pct(avg.get("fg_pct")),
        "3P%": pct(avg.get("fg3_pct")),
        "FT%": pct(avg.get("ft_pct")),
    }


def _media_carrera_ponderada(seasons: List[Dict[str, Any]]) -> Dict[str, str]:
    """
    Promedio ponderado por partidos jugados de las temporadas dadas.
    Aproximación de "carrera reciente"; la cobertura completa de carrera no
    es factible en el free tier (1 llamada por temporada).
    """
    if not seasons:
        return {}

    total_g = 0
    acumulado: Dict[str, float] = {}
    contadores: Dict[str, int] = {}

    cols_pg = [
        "pts", "reb", "oreb", "dreb", "ast", "stl", "blk", "turnover", "pf",
    ]
    cols_pct = ["fg_pct", "fg3_pct", "ft_pct"]

    for s in seasons:
        g = _safe_float(s.get("games_played")) or 0
        if g <= 0:
            continue
        total_g += g
        for c in cols_pg + cols_pct + ["min"]:
            v = s.get(c)
            # min puede venir "MM:SS" — normalizamos
            if c == "min" and isinstance(v, str) and ":" in v:
                try:
                    mm, ss = v.split(":")
                    v = int(mm) + int(ss) / 60
                except (ValueError, TypeError):
                    v = None
            f = _safe_float(v)
            if f is None:
                continue
            acumulado[c] = acumulado.get(c, 0.0) + f * g
            contadores[c] = contadores.get(c, 0) + int(g)

    if total_g == 0:
        return {}

    def fmt(c: str, dec: int = 1) -> str:
        if c not in acumulado or contadores.get(c, 0) == 0:
            return "–"
        return f"{round(acumulado[c] / contadores[c], dec)}"

    def fmt_pct(c: str) -> str:
        if c not in acumulado or contadores.get(c, 0) == 0:
            return "–"
        v = acumulado[c] / contadores[c]
        if v <= 1.0:
            v *= 100
        return f"{round(v, 1)}%"

    return {
        "Partidos": str(int(total_g)),
        "Minutos": fmt("min"),
        "Puntos": fmt("pts"),
        "Rebotes": fmt("reb"),
        "Rebotes_ofensivos": fmt("oreb"),
        "Rebotes_defensivos": fmt("dreb"),
        "Asistencias": fmt("ast"),
        "Robos": fmt("stl"),
        "Tapones": fmt("blk"),
        "Pérdidas": fmt("turnover"),
        "Faltas": fmt("pf"),
        "FG%": fmt_pct("fg_pct"),
        "3P%": fmt_pct("fg3_pct"),
        "FT%": fmt_pct("ft_pct"),
    }


# ─── API pública ──────────────────────────────────────────────────────────────


def obtener_datos_jugador(nombre_jugador: str) -> Dict[str, Any]:
    """
    Pipeline completo de extracción de datos de un jugador.

    Returns
    -------
    dict
        Estructura normalizada con las claves:

        - ``"Datos personales"``: dict con bio (nombre, equipo, posición,
          altura, peso, edad, draft, universidad, país, temporadas, foto).
        - ``"Estadísticas"``: dict con sub-claves ``carrera_reciente`` (media
          ponderada de las últimas ~6 temporadas), ``ultima_temporada`` y
          ``temporadas_anteriores`` (lista de las anteriores).

    Raises
    ------
    ValueError
        Jugador no encontrado en NBA o en balldontlie.
    EnvironmentError
        BALLDONTLIE_API_KEY no configurada.
    RuntimeError
        Errores transitorios de red / API tras agotar reintentos.
    """
    # 1) Resolución NBA (offline).
    nba = buscar_jugador_nba(nombre_jugador)
    nba_id = nba["id"]
    full_name = nba["full_name"]
    logger.info("✓ NBA ID resuelto: %s (ID: %d)", full_name, nba_id)

    # 2) Bio en balldontlie.
    bdl = _bdl_buscar_jugador(full_name)
    logger.info("✓ Encontrado en balldontlie: bdl_id=%s", bdl.get("id"))

    bio: Dict[str, Any] = {
        "Foto": NBA_HEADSHOT_URL.format(player_id=nba_id),
        "Nombre": full_name,
        "Equipo": _equipo_str(bdl) if nba["is_active"] else "Retirado / Sin equipo",
        "Posición": str(bdl.get("position") or "–"),
        "Altura": _altura_fmt(bdl.get("height", "")),
        "Peso": _peso_fmt(bdl.get("weight", "")),
        "Edad": "–",  # balldontlie no expone fecha de nacimiento para NBA
        "País": str(bdl.get("country") or "–"),
        "Universidad": str(bdl.get("college") or "–"),
        "Draft": _draft_fmt(bdl),
        "Dorsal": str(bdl.get("jersey_number") or "–"),
    }

    # 3) Estadísticas: últimas N temporadas + media ponderada como "carrera".
    bdl_player_id = bdl.get("id")
    estadisticas: Dict[str, Any] = {}
    if bdl_player_id is not None:
        seasons = _temporadas_recientes(int(bdl_player_id), bdl.get("draft_year"))
        logger.info("✓ Recolectadas %d temporadas recientes.", len(seasons))

        if seasons:
            ultima = seasons[0]
            stats_ultima = _stats_temporada(ultima)
            stats_ultima["Temporada"] = (
                f"{ultima['__season']}-{(ultima['__season'] + 1) % 100:02d}"
            )
            estadisticas["ultima_temporada"] = stats_ultima

            estadisticas["temporadas_anteriores"] = []
            for s in seasons[1:]:
                fila = _stats_temporada(s)
                fila["Temporada"] = f"{s['__season']}-{(s['__season'] + 1) % 100:02d}"
                estadisticas["temporadas_anteriores"].append(fila)

            estadisticas["carrera_reciente"] = _media_carrera_ponderada(seasons)
            estadisticas["_nota"] = (
                f"Promedios ponderados de las últimas {len(seasons)} temporadas. "
                "Cobertura limitada a ese rango por el free tier de balldontlie."
            )

        bio["Temporadas_NBA"] = (
            str(len(seasons)) if seasons else "–"
        )
    else:
        bio["Temporadas_NBA"] = "–"

    return {
        "Datos personales": bio,
        "Estadísticas": estadisticas,
    }
