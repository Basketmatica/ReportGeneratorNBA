from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import FileResponse
import tempfile
from report_player import generar_pdf_jugador  # importa tu función
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/generate-pdf/")
def generate_pdf(player_name: str = Query(...)):
    try:
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        generar_pdf_jugador(player_name, temp.name)  # esta función lanzará un ValueError si no encuentra al jugador
        return FileResponse(temp.name, media_type="application/pdf", filename=f"{player_name} Report.pdf")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Error interno al generar el informe.")
