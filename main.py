from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
import tempfile
from report_player import generar_pdf_jugador  # importa tu función

app = FastAPI()

@app.get("/generate-pdf/")
def generate_pdf(player_name: str = Query(...)):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp:
        generar_pdf_jugador(player_name, temp.name)
        temp_path = temp.name

    return FileResponse(temp_path, media_type="application/pdf", filename=f"{player_name} Report.pdf")
