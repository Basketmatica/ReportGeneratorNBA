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

# ESPN API "oculta" — sin auth, sin clave. Akamai-fronted, cloud-friendly.
ESPN_WEB_BASE = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba"
ESPN_SEARCH_URL = "https://site.web.api.espn.com/apis/common/v3/search"
ESPN_HEADSHOT_URL = "https://a.espncdn.com/i/headshots/nba/players/full/{espn_id}.png"

# UA de navegador estándar — ESPN responde 403 a UAs vacíos o de bots conocidos.
ESPN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

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

    Importante: el parámetro `search` de balldontlie busca un substring literal
    en `first_name` O `last_name` por separado. Por eso `search="LeBron James"`
    devuelve vacío (no aparece como substring en ninguno de los dos campos).
    Estrategia robusta: intentamos varias búsquedas (apellido, nombre completo,
    nombre de pila) y combinamos candidatos antes de elegir la mejor coincidencia.

    Cachea para no volver a pedir el mismo jugador en la misma instancia.
    """
    nombre = (nombre or "").strip()
    if not nombre:
        raise ValueError("Nombre vacío.")

    n_low = nombre.lower()
    tokens = nombre.split()

    # Lista ordenada de intentos (params para _bdl_get).
    intentos: list[dict] = []
    if len(tokens) >= 2:
        # 1) Filtro por last_name (más selectivo, suele ser case-insensitive).
        intentos.append({"last_name": tokens[-1], "per_page": 25})
        # 2) Search por el apellido (substring).
        intentos.append({"search": tokens[-1], "per_page": 25})
        # 3) Search por el nombre de pila (por si el apellido es raro o compuesto).
        intentos.append({"search": tokens[0], "per_page": 25})
    else:
        # Un solo token: probamos search y last_name.
        intentos.append({"search": nombre, "per_page": 25})
        intentos.append({"last_name": nombre, "per_page": 25})

    candidatos: list[dict] = []
    vistos: set[int] = set()

    for params in intentos:
        try:
            data = _bdl_get("/players", params=params)
        except Exception as exc:
            log.debug("Búsqueda balldontlie falló con %s: %s", params, exc)
            continue
        for c in data.get("data") or []:
            cid = c.get("id")
            if cid is None or cid in vistos:
                continue
            vistos.add(cid)
            candidatos.append(c)
        # Si ya tenemos coincidencia exacta, no hace falta seguir consultando.
        for c in candidatos:
            full = f"{c.get('first_name', '')} {c.get('last_name', '')}".strip().lower()
            if full == n_low:
                return c

    if not candidatos:
        raise ValueError(
            f"Jugador '{nombre}' no aparece en balldontlie. "
            "Comprueba la ortografía (en inglés)."
        )

    # Ranking final: exacto > contiene todos los tokens > primero.
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


# ─── Estadísticas: ESPN (sin clave, gratis) ──────────────────────────────────


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


def _espn_get(url: str, params: Optional[dict] = None, retries: int = 2) -> Optional[dict]:
    """
    GET genérico contra ESPN con reintentos suaves. Devuelve None ante cualquier
    fallo (caller decide si degrada o aborta) — ESPN no es nuestra fuente
    crítica, así que NO lanzamos excepciones a la cara del usuario.
    """
    last_err: Optional[str] = None
    for intento in range(retries + 1):
        try:
            with httpx.Client(
                timeout=DEFAULT_TIMEOUT, headers=ESPN_HEADERS, follow_redirects=True
            ) as cli:
                r = cli.get(url, params=params or {})
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    last_err = "respuesta no JSON"
                    return None
            if r.status_code in (429, 502, 503, 504) and intento < retries:
                time.sleep(0.6 * (intento + 1))
                continue
            last_err = f"HTTP {r.status_code}"
            break
        except httpx.RequestError as exc:
            last_err = f"red: {exc}"
            if intento < retries:
                time.sleep(0.6 * (intento + 1))
                continue
            break
    logger.warning("ESPN %s falló: %s", url, last_err)
    return None


@lru_cache(maxsize=256)
def _espn_buscar_atleta_id(nombre: str) -> Optional[str]:
    """
    Busca un atleta NBA en ESPN por nombre y devuelve su ID (string) o None.
    Cacheada para no repetir búsquedas en la misma instancia.
    """
    nombre = (nombre or "").strip()
    if not nombre:
        return None

    data = _espn_get(
        ESPN_SEARCH_URL,
        params={
            "query": nombre,
            "limit": 25,
            "type": "player",
            "sport": "basketball",
            "league": "nba",
        },
    )
    if not data:
        return None

    # ESPN devuelve resultados en data["results"][i]["contents"][j] o similar.
    # La estructura ha cambiado con el tiempo; hacemos un parser tolerante.
    candidatos: List[dict] = []
    if isinstance(data, dict):
        # Forma A: {"results": [{"type":"player", "contents": [...]}, ...]}
        for grupo in data.get("results") or []:
            if not isinstance(grupo, dict):
                continue
            for c in grupo.get("contents") or []:
                if isinstance(c, dict):
                    candidatos.append(c)
        # Forma B: {"items": [...]}
        for c in data.get("items") or []:
            if isinstance(c, dict):
                candidatos.append(c)

    if not candidatos:
        return None

    n_low = nombre.lower()

    def es_jugador_nba(c: dict) -> bool:
        # Filtrado tolerante: aceptamos si menciona basketball/nba en algún campo.
        sport = str(c.get("sport") or "").lower()
        league = str(c.get("league") or "").lower()
        leagues = c.get("leagues") or []
        if isinstance(leagues, list) and any(
            "nba" in str(l).lower() for l in leagues
        ):
            return True
        if "basketball" in sport or "nba" in league:
            return True
        # Si no se puede determinar, no descartamos.
        return True

    def display_name(c: dict) -> str:
        for k in ("displayName", "fullName", "name", "nameAbbr"):
            v = c.get(k)
            if v:
                return str(v)
        return ""

    def athlete_id(c: dict) -> Optional[str]:
        for k in ("id", "uid", "athleteId"):
            v = c.get(k)
            if v:
                return str(v).split(":")[-1]  # uid puede ser "s:40~l:46~a:1966"
        return None

    # 1) Match exacto.
    for c in candidatos:
        if not es_jugador_nba(c):
            continue
        if display_name(c).lower() == n_low:
            aid = athlete_id(c)
            if aid:
                return aid
    # 2) Contiene todos los tokens.
    for c in candidatos:
        if not es_jugador_nba(c):
            continue
        nm = display_name(c).lower()
        if all(t in nm for t in n_low.split()):
            aid = athlete_id(c)
            if aid:
                return aid
    # 3) Primer atleta razonable.
    for c in candidatos:
        if es_jugador_nba(c):
            aid = athlete_id(c)
            if aid:
                return aid
    return None


def _espn_stats_jugador(espn_id: str) -> Optional[dict]:
    """
    Recupera stats históricas (varias temporadas) desde el endpoint /stats de
    ESPN. Devuelve el JSON crudo o None.
    """
    return _espn_get(f"{ESPN_WEB_BASE}/athletes/{espn_id}/stats")


# Mapa nombre-ESPN → clave normalizada interna.
# ESPN usa nombres como "avgPoints", "avgRebounds"… que son consistentes en NBA.
# ─────────────────────────────────────────────────────────────────────────────
# Mapeos de nombres ESPN → claves normalizadas en español
# ─────────────────────────────────────────────────────────────────────────────

# Categoría "averages" (per-game): nombres de stat → clave normalizada.
_ESPN_STAT_KEYS: Dict[str, str] = {
    "gamesPlayed": "Partidos",
    "games": "Partidos",
    "avgMinutes": "Minutos",
    "minutes": "Minutos",
    "avgPoints": "Puntos",
    "points": "Puntos",
    "avgRebounds": "Rebotes",
    "rebounds": "Rebotes",
    "avgOffensiveRebounds": "Rebotes_ofensivos",
    "offensiveRebounds": "Rebotes_ofensivos",
    "avgDefensiveRebounds": "Rebotes_defensivos",
    "defensiveRebounds": "Rebotes_defensivos",
    "avgAssists": "Asistencias",
    "assists": "Asistencias",
    "avgSteals": "Robos",
    "steals": "Robos",
    "avgBlocks": "Tapones",
    "blocks": "Tapones",
    "avgTurnovers": "Pérdidas",
    "turnovers": "Pérdidas",
    "avgFouls": "Faltas",
    "fouls": "Faltas",
    "fieldGoalPct": "FG%",
    "threePointFieldGoalPct": "3P%",
    "freeThrowPct": "FT%",
}

# Categoría "totals" (totales acumulados de carrera): incluye los compuestos
# "X-Y" (anotados-intentados) que tienen su propio sentido y queremos preservar.
_ESPN_TOTALS_KEYS: Dict[str, str] = {
    "fieldGoalsMade-fieldGoalsAttempted": "FG_anotados_intentados",
    "fieldGoalPct": "FG%",
    "threePointFieldGoalsMade-threePointFieldGoalsAttempted": "3P_anotados_intentados",
    "threePointFieldGoalPct": "3P%",
    "freeThrowsMade-freeThrowsAttempted": "FT_anotados_intentados",
    "freeThrowPct": "FT%",
    "offensiveRebounds": "Rebotes_ofensivos",
    "defensiveRebounds": "Rebotes_defensivos",
    "totalRebounds": "Rebotes",
    "assists": "Asistencias",
    "blocks": "Tapones",
    "steals": "Robos",
    "fouls": "Faltas",
    "turnovers": "Pérdidas",
    "points": "Puntos",
}

# Categoría "miscellaneous" (estadísticas avanzadas).
_ESPN_MISC_KEYS: Dict[str, str] = {
    "doubleDouble": "Dobles_dobles",
    "tripleDouble": "Triples_dobles",
    "disqualifications": "Descalificaciones",
    "ejections": "Expulsiones",
    "technicalFouls": "Faltas_tecnicas",
    "flagrantFouls": "Faltas_flagrantes",
    "assistTurnoverRatio": "AST_TO_ratio",
    "stealTurnoverRatio": "STL_TO_ratio",
    "scoringEfficiency": "Eficiencia_anotadora",
    "shootingEfficiency": "Eficiencia_tiro",
}

# Conjunto de claves que requieren sufijo "%".
_PCT_KEYS: set = {"FG%", "3P%", "FT%"}

# Campos que tiene sentido normalizar a per-36-min.
_PER36_FIELDS = [
    "Puntos", "Rebotes", "Rebotes_ofensivos", "Rebotes_defensivos",
    "Asistencias", "Robos", "Tapones", "Pérdidas", "Faltas",
]

_EXPECTED_PG = [
    "Partidos", "Minutos", "Puntos", "Rebotes",
    "Rebotes_ofensivos", "Rebotes_defensivos",
    "Asistencias", "Robos", "Tapones",
    "Pérdidas", "Faltas", "FG%", "3P%", "FT%",
]


# ─────────────────────────────────────────────────────────────────────────────
# Parsers genéricos
# ─────────────────────────────────────────────────────────────────────────────


def _parsear_fila_posicional(
    names: List[str],
    stats: List[Any],
    key_map: Dict[str, str],
    pct_keys: Optional[set] = None,
) -> Dict[str, str]:
    """
    Genérico: hace ``zip(names, stats)`` y mapea via ``key_map`` a un dict
    normalizado. ESPN devuelve las stats como array PLANO posicional, así que
    el orden importa.

    Si una clave normalizada está en ``pct_keys`` y el valor no termina en
    ``%``, se le añade el sufijo.
    """
    pct_keys = pct_keys or set()
    out: Dict[str, str] = {}
    for name, value in zip(names or [], stats or []):
        target = key_map.get(name)
        if not target:
            continue
        v = "" if value is None else str(value).strip()
        if not v or v in {"-", "--"}:
            out[target] = "–"
            continue
        if target in pct_keys and not v.endswith("%"):
            v = f"{v}%"
        out[target] = v
    return out


def _categoria_por_nombre(raw: dict, nombre: str) -> Optional[dict]:
    """Localiza una categoría dentro de la respuesta /stats por su `name`."""
    if not isinstance(raw, dict):
        return None
    for c in raw.get("categories") or []:
        if isinstance(c, dict) and str(c.get("name") or "").lower() == nombre.lower():
            return c
    return None


def _per36(stats: Dict[str, str]) -> Dict[str, str]:
    """
    Calcula promedios per-36-minutos a partir de un dict de stats per-game.
    Devuelve dict vacío si los minutos no son válidos.

    Per-36 es útil para comparar jugadores con minutajes diferentes —
    es la convención estándar en análisis NBA.
    """
    minutos = _safe_float(str(stats.get("Minutos", "")).rstrip("%"))
    if not minutos or minutos <= 0:
        return {}
    factor = 36.0 / minutos
    out: Dict[str, str] = {}
    for k in _PER36_FIELDS:
        v = _safe_float(str(stats.get(k, "")).rstrip("%"))
        if v is None:
            continue
        out[k] = f"{round(v * factor, 1)}"
    return out


def _seasons_de_categoria(
    cat: dict, key_map: Dict[str, str], pct_keys: set
) -> List[Dict[str, Any]]:
    """
    Parsea ``cat["statistics"]`` a lista de seasons normalizadas, ordenadas
    DESC por año (más reciente primero). Filtra entradas vacías.
    """
    names = cat.get("names") or []
    if not names:
        return []
    rows = cat.get("statistics") or []
    seasons: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        season_obj = row.get("season") or {}
        if isinstance(season_obj, dict):
            year = season_obj.get("year") or season_obj.get("displayYear")
            display = season_obj.get("displayName") or (
                f"{int(year) - 1}-{int(year) % 100:02d}" if year else "–"
            )
        else:
            year = None
            display = str(season_obj) if season_obj else "–"

        stats_arr = row.get("stats") or []
        fila = _parsear_fila_posicional(names, stats_arr, key_map, pct_keys)
        fila["Temporada"] = display
        try:
            fila["__year"] = int(year) if year is not None else 0
        except (TypeError, ValueError):
            fila["__year"] = 0
        seasons.append(fila)

    # Filtrar entradas sin datos relevantes (todo "–" / vacío / 0).
    def has_data(s: Dict[str, Any]) -> bool:
        for k, v in s.items():
            if k in {"Temporada", "__year"}:
                continue
            if v not in (None, "", "–", "0"):
                return True
        return False

    seasons = [s for s in seasons if has_data(s)]
    seasons.sort(key=lambda s: s.get("__year", 0), reverse=True)
    return seasons


# ─────────────────────────────────────────────────────────────────────────────
# Extractor maestro: una sola llamada, todo procesado
# ─────────────────────────────────────────────────────────────────────────────


def _extraer_stats_completas_espn(
    espn_id: str, n_temporadas: int = 6
) -> Dict[str, Any]:
    """
    Hace UNA sola llamada a ``/stats`` y devuelve un diccionario completo:

    .. code-block:: python

        {
            "temporadas":       [...],   # últimas N per-game (más reciente 1ª)
            "carrera_promedio": {...},   # per-game de TODA la carrera + per-36
            "carrera_totales":  {...},   # contadores de carrera (PTS totales…)
            "avanzadas_ultima": {...},   # misc de la temporada más reciente
            "avanzadas_carrera":{...},   # misc acumulada de carrera
        }

    Cada bloque puede faltar de forma independiente — robusto a categorías
    que ESPN omita o cambie. Los errores se loguean pero no se propagan.
    """
    raw = _espn_stats_jugador(espn_id)
    if not raw or not isinstance(raw, dict):
        return {}

    out: Dict[str, Any] = {}

    # ── Categoría "averages": per-game por temporada + media de carrera ──
    try:
        cat_avg = _categoria_por_nombre(raw, "averages")
        if cat_avg:
            names_avg = cat_avg.get("names") or []
            seasons = _seasons_de_categoria(cat_avg, _ESPN_STAT_KEYS, _PCT_KEYS)

            # Asegurar todas las claves esperadas en cada season.
            for s in seasons:
                for k in _EXPECTED_PG:
                    s.setdefault(k, "–")
                p36 = _per36(s)
                if p36:
                    s["per36"] = p36

            # Total real de temporadas en la NBA (antes de cortar a n_temporadas).
            out["temporadas_totales"] = len(seasons)
            out["temporadas"] = seasons[:n_temporadas]

            # Media de carrera real (no aproximada) que ESPN ya pre-calcula.
            career_pg = _parsear_fila_posicional(
                names_avg, cat_avg.get("totals") or [], _ESPN_STAT_KEYS, _PCT_KEYS
            )
            if career_pg:
                for k in _EXPECTED_PG:
                    career_pg.setdefault(k, "–")
                p36 = _per36(career_pg)
                if p36:
                    career_pg["per36"] = p36
                out["carrera_promedio"] = career_pg
    except Exception as exc:
        logger.warning("Error parseando 'averages' de ESPN: %s", exc)

    # ── Categoría "totals": contadores acumulados de carrera ──
    try:
        cat_tot = _categoria_por_nombre(raw, "totals")
        if cat_tot:
            names_tot = cat_tot.get("names") or []
            career_tot = _parsear_fila_posicional(
                names_tot, cat_tot.get("totals") or [], _ESPN_TOTALS_KEYS, _PCT_KEYS
            )
            if career_tot:
                out["carrera_totales"] = career_tot
    except Exception as exc:
        logger.warning("Error parseando 'totals' de ESPN: %s", exc)

    # ── Categoría "miscellaneous": estadísticas avanzadas ──
    try:
        cat_misc = _categoria_por_nombre(raw, "miscellaneous")
        if cat_misc:
            names_misc = cat_misc.get("names") or []
            misc_seasons = _seasons_de_categoria(cat_misc, _ESPN_MISC_KEYS, set())
            if misc_seasons:
                # Sólo exponemos la más reciente — el resto sería ruido.
                misc_ultima = dict(misc_seasons[0])
                misc_ultima.pop("__year", None)
                out["avanzadas_ultima"] = misc_ultima
            misc_carrera = _parsear_fila_posicional(
                names_misc, cat_misc.get("totals") or [], _ESPN_MISC_KEYS, set()
            )
            if misc_carrera:
                out["avanzadas_carrera"] = misc_carrera
    except Exception as exc:
        logger.warning("Error parseando 'miscellaneous' de ESPN: %s", exc)

    return out


# ─── API pública ──────────────────────────────────────────────────────────────


def obtener_datos_jugador(nombre_jugador: str) -> Dict[str, Any]:
    """
    Pipeline completo de extracción de datos de un jugador.

    Returns
    -------
    dict
        Estructura normalizada con las claves:

        - ``"Datos personales"``: dict con bio (nombre, equipo, posición,
          altura, peso, draft, universidad, país, temporadas en NBA, foto).
        - ``"Estadísticas"``: dict con TODAS estas sub-claves (cada una puede
          faltar de forma independiente si ESPN no la devuelve):

            * ``ultima_temporada``: per-game de la temporada más reciente,
              incluye ``per36`` con los promedios normalizados a 36 minutos.
            * ``temporadas_anteriores``: lista de las N-1 temporadas previas
              (más reciente → más antigua), también con ``per36`` cada una.
            * ``carrera_promedio``: per-game de TODA la carrera (calculado
              por ESPN, no aproximación) + ``per36``.
            * ``carrera_totales``: contadores totales de carrera (PTS, REB,
              AST totales, FG anotados-intentados, etc.).
            * ``avanzadas_ultima``: estadísticas avanzadas de la última
               temporada (DD2, TD3, AST/TO, eficiencias, técnicas…).
            * ``avanzadas_carrera``: estadísticas avanzadas acumuladas de
               carrera.
            * ``_nota``: nota de pie sobre la fuente de datos.

        Estadísticas puede ir vacío si ESPN no responde — el informe se
        genera igualmente sólo con la bio.

    Raises
    ------
    ValueError
        Jugador no encontrado en NBA o en balldontlie.
    EnvironmentError
        BALLDONTLIE_API_KEY no configurada.
    RuntimeError
        Errores transitorios de red / API tras agotar reintentos en BDL.
        (ESPN nunca lanza excepciones — degrada silenciosamente.)
    """
    # 1) Resolución NBA (offline).
    nba = buscar_jugador_nba(nombre_jugador)
    nba_id = nba["id"]
    full_name = nba["full_name"]
    logger.info("✓ NBA ID resuelto: %s (ID: %d)", full_name, nba_id)

    # 2) Bio en balldontlie (free tier — funciona en cloud).
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

    # 3) Estadísticas completas vía ESPN (gratis, sin clave). Una sola llamada
    #    HTTP da: per-game por temporada, medias de carrera reales, totales
    #    de carrera y métricas avanzadas. Degradación graceful en cada bloque.
    estadisticas: Dict[str, Any] = {}
    espn_id: Optional[str] = None
    try:
        espn_id = _espn_buscar_atleta_id(full_name)
    except Exception as exc:
        logger.warning("Búsqueda ESPN lanzó excepción inesperada: %s", exc)
        espn_id = None

    if espn_id:
        logger.info("✓ ESPN athlete ID: %s", espn_id)
        try:
            stats = _extraer_stats_completas_espn(espn_id)
        except Exception as exc:
            logger.warning("Extracción de stats ESPN falló: %s", exc)
            stats = {}

        seasons = stats.get("temporadas") or []
        total_temps = stats.get("temporadas_totales") or len(seasons)
        if seasons:
            logger.info(
                "✓ Recolectadas %d temporadas (de %d totales) desde ESPN.",
                len(seasons), total_temps,
            )
            ultima = dict(seasons[0])
            ultima.pop("__year", None)
            estadisticas["ultima_temporada"] = ultima

            anteriores = []
            for s in seasons[1:]:
                fila = dict(s)
                fila.pop("__year", None)
                anteriores.append(fila)
            estadisticas["temporadas_anteriores"] = anteriores

            bio["Temporadas_NBA"] = str(total_temps)
        else:
            logger.warning("ESPN devolvió 0 temporadas per-game para %s.", full_name)
            bio["Temporadas_NBA"] = "–"

        if stats.get("carrera_promedio"):
            estadisticas["carrera_promedio"] = stats["carrera_promedio"]
            logger.info("✓ Promedios de carrera obtenidos.")
        if stats.get("carrera_totales"):
            estadisticas["carrera_totales"] = stats["carrera_totales"]
            logger.info("✓ Totales de carrera obtenidos.")
        if stats.get("avanzadas_ultima"):
            estadisticas["avanzadas_ultima"] = stats["avanzadas_ultima"]
        if stats.get("avanzadas_carrera"):
            estadisticas["avanzadas_carrera"] = stats["avanzadas_carrera"]
            logger.info("✓ Estadísticas avanzadas (misc) obtenidas.")

        if estadisticas:
            estadisticas["_nota"] = (
                "Fuente: ESPN. Per-36 calculado a partir de los promedios "
                "per-game. Las estadísticas avanzadas incluyen dobles-dobles, "
                "triples-dobles, ratios de juego y eficiencias."
            )
    else:
        logger.warning(
            "No se pudo obtener ID ESPN para '%s'. "
            "El informe se generará sin estadísticas detalladas.",
            full_name,
        )
        bio["Temporadas_NBA"] = "–"

    # Sustituir foto por la de ESPN si tenemos su ID y la NBA falla en el
    # futuro; cdn.nba.com sigue siendo la primera opción.
    if espn_id and not bio["Foto"]:
        bio["Foto"] = ESPN_HEADSHOT_URL.format(espn_id=espn_id)

    return {
        "Datos personales": bio,
        "Estadísticas": estadisticas,
    }
