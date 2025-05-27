from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
import tempfile
from report_player import generar_pdf_jugador  # importa tu función

app = FastAPI()

@app.get("/generate-pdf/")
def generate_pdf(player_name: str = Query(...)):
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    generar_pdf_jugador(player_name, temp.name)  # llama a tu función con el nombre
    return FileResponse(temp.name, media_type="application/pdf", filename=f"{player_name} Report.pdf")
