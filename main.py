import os
import tempfile
import logging

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from report_player import generar_pdf_jugador

# ─── Configuración ────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(levelname)s │ %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="NBA Report Generator",
    description=(
        "Genera informes PDF profesionales de jugadores de la NBA "
        "usando datos oficiales de la NBA y análisis con IA."
    ),
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def root():
    return {
        "status": "ok",
        "mensaje": "NBA Report Generator activo. Usa /generate-pdf/?player_name=LeBron+James",
        "docs": "/docs",
    }


@app.get("/health")
def health_check():
    """Comprueba que el servicio está en línea."""
    return {"status": "ok"}


@app.get("/generate-pdf/")
def generate_pdf(
    player_name: str = Query(
        ...,
        description="Nombre del jugador en inglés (ej. 'LeBron James', 'Stephen Curry')",
        min_length=2,
        max_length=80,
    )
):
    """
    Genera y devuelve un informe PDF del jugador indicado.

    - **player_name**: nombre del jugador en inglés.
    - Devuelve un fichero PDF listo para descargar.
    - Código 404 si el jugador no existe en la base de datos de la NBA.
    - Código 503 si hay un problema temporal con la API de la NBA.
    - Código 500 para otros errores internos.
    """
    logger.info(f"Solicitud recibida: player_name='{player_name}'")

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp_path = tmp.name

        generar_pdf_jugador(player_name, tmp_path)

        safe_name = player_name.replace("/", "-").replace("\\", "-")
        return FileResponse(
            tmp_path,
            media_type="application/pdf",
            filename=f"{safe_name} Report.pdf",
        )

    except ValueError as exc:
        # Jugador no encontrado en la base de datos de la NBA
        logger.warning(f"Jugador no encontrado: {exc}")
        raise HTTPException(status_code=404, detail=str(exc))

    except EnvironmentError as exc:
        # API_KEY no configurada
        logger.error(f"Error de configuración: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))

    except RuntimeError as exc:
        # Error al llamar a la API de la NBA (timeout, rate limit, etc.)
        logger.error(f"Error de API de NBA: {exc}")
        raise HTTPException(
            status_code=503,
            detail=(
                "No se pudo obtener datos de la API de la NBA. "
                "Inténtalo de nuevo en unos segundos."
            ),
        )

    except Exception as exc:
        logger.exception(f"Error inesperado al generar informe para '{player_name}'")
        raise HTTPException(
            status_code=500,
            detail="Error interno al generar el informe. Consulta los logs para más detalles.",
        )
