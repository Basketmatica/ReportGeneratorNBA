from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from fastapi import Request

# ─── Logging (UNA sola configuración global) ─────────────────────────────────

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("nba-report")

# Importamos DESPUÉS de configurar logging para que los módulos hereden el formato.
from report_player import GEMINI_MODEL, generar_pdf_jugador  # noqa: E402


# ─── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="NBA Report Generator",
    description=(
        "Genera informes PDF profesionales de jugadores de la NBA usando "
        "datos de balldontlie + cdn.nba.com (compatible con Render) y "
        "análisis con Google Gemini."
    ),
    version="3.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://basketmatica.com",
        "https://www.basketmatica.com",
        "http://localhost:4321",  # dev de Astro
    ],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ─── Endpoints ────────────────────────────────────────────────────────────────


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {
        "status": "ok",
        "mensaje": (
            "NBA Report Generator activo. "
            "Usa /generate-pdf/?player_name=LeBron+James"
        ),
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health")
def health_check() -> dict:
    """
    Health check con información de configuración.

    Devuelve ``status: ok`` solo si todas las claves necesarias están presentes.
    Útil para validar el deployment antes de empezar a usarlo.
    """
    api_key = bool(os.getenv("API_KEY", "").strip())
    bdl_key = bool(os.getenv("BALLDONTLIE_API_KEY", "").strip())
    ok = api_key and bdl_key
    return {
        "status": "ok" if ok else "missing_config",
        "google_gemini_api_key": "configured" if api_key else "MISSING (env API_KEY)",
        "balldontlie_api_key": (
            "configured" if bdl_key else "MISSING (env BALLDONTLIE_API_KEY)"
        ),
        "gemini_model": GEMINI_MODEL,
    }


def _safe_filename(name: str) -> str:
    """Sanitiza un nombre para usarlo en una cabecera Content-Disposition."""
    cleaned = "".join(c if c.isalnum() or c in " -_." else "_" for c in name)
    return cleaned.strip() or "Player"

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

@app.get("/generate-pdf/")
def generate_pdf(
    request: Request,
    background_tasks: BackgroundTasks,
    player_name: str = Query(
        ...,
        description=(
            "Nombre del jugador en inglés "
            "(p.ej. 'LeBron James', 'Stephen Curry')."
        ),
        min_length=2,
        max_length=80,
    ),
):
    """
    Genera y devuelve el informe PDF del jugador indicado.

    Códigos de respuesta:
      - **200**: PDF devuelto.
      - **404**: jugador no encontrado.
      - **500**: error de configuración (faltan env vars) o error inesperado.
      - **503**: error transitorio con la API externa (balldontlie / Gemini).
    """
    logger.info("Solicitud recibida: player_name='%s'", player_name)

    # Crear el fichero temporal y programar su borrado para DESPUÉS de la
    # respuesta. FileResponse mantendrá el fichero abierto hasta que termine
    # de transmitirlo, y BackgroundTasks corre tras el envío.
    fd, tmp_name = tempfile.mkstemp(suffix=".pdf", prefix="nba_report_")
    os.close(fd)
    tmp_path = Path(tmp_name)

    def _cleanup(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
            logger.debug("PDF temporal eliminado: %s", path)
        except OSError as exc:
            logger.warning("No se pudo eliminar %s: %s", path, exc)

    try:
        generar_pdf_jugador(player_name, str(tmp_path))

        background_tasks.add_task(_cleanup, tmp_path)

        return FileResponse(
            path=str(tmp_path),
            media_type="application/pdf",
            filename=f"{_safe_filename(player_name)} Report.pdf",
        )

    except ValueError as exc:
        # Jugador no existe.
        _cleanup(tmp_path)
        logger.warning("Jugador no encontrado: %s", exc)
        raise HTTPException(status_code=404, detail=str(exc))

    except EnvironmentError as exc:
        # Falta API_KEY o BALLDONTLIE_API_KEY.
        _cleanup(tmp_path)
        logger.error("Error de configuración: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    except RuntimeError as exc:
        # Errores transitorios de red / API.
        _cleanup(tmp_path)
        logger.error("Error transitorio de API: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=(
                "No se pudo obtener datos de la API externa. "
                "Inténtalo de nuevo en unos segundos."
            ),
        )

    except Exception:
        _cleanup(tmp_path)
        logger.exception("Error inesperado al generar informe para '%s'", player_name)
        raise HTTPException(
            status_code=500,
            detail=(
                "Error interno al generar el informe. "
                "Consulta los logs para más detalles."
            ),
        )


# ─── Arranque local / Render ──────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    # Render inyecta el puerto en $PORT. En local cae a 8000.
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")
    logger.info("Arrancando en http://%s:%d  (modelo Gemini: %s)", host, port, GEMINI_MODEL)
    uvicorn.run(app, host=host, port=port)
