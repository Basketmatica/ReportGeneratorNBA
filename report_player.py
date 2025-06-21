import requests
import unicodedata
from bs4 import BeautifulSoup
import undetected_chromedriver as uc
import time
import re
import json
from weasyprint import HTML
import google.generativeai as genai
import os

API_KEY = os.getenv("API_KEY")
# link de la página de basketball-reference
BASE_URL = "https://www.basketball-reference.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 \
                   (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def normalizar(texto):
    """Quita acentos y caracteres especiales, y convierte a minúsculas."""
    texto = unicodedata.normalize("NFD", texto)
    texto = ''.join(c for c in texto if unicodedata.category(c) != 'Mn')
    return texto.lower()

def buscar_jugador(nombre_jugador):
    """Busca la URL del perfil del jugador en Basketball Reference."""
    nombre_normalizado = normalizar(nombre_jugador.strip())
    apellido_inicial = normalizar(nombre_jugador.strip().split()[-1])[0]

    url = f"{BASE_URL}/players/{apellido_inicial}/"
    response = requests.get(url, headers=HEADERS, timeout=10)
    soup = BeautifulSoup(response.text, "html.parser")
    filas = soup.select("table#players tbody tr")

    for fila in filas:
        enlace = fila.select_one("th a")
        if enlace:
            nombre_html = enlace.text.strip()
            if normalizar(nombre_html) == nombre_normalizado:
                return BASE_URL + enlace["href"]

    raise ValueError("Jugador no encontrado. Verifica el nombre e inténtalo de nuevo.")


def obtener_html_con_requests(url):
    '''Obtiene el HTML completo de la página con retries'''
    retries = 3
    for i in range(retries):
        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
            if response.status_code == 200:
                return response.text
        except requests.exceptions.RequestException:
            time.sleep(2)
    return None


def scrapear_datos_personales(html):
    '''Extrae la información personal del jugador desde el HTML'''
    soup = BeautifulSoup(html, "html.parser")
    meta_div = soup.find('div', id='meta')
    personal_info = {}
    if meta_div:
        img_tag = meta_div.find('img')
        if img_tag and 'src' in img_tag.attrs:
            personal_info['Foto'] = img_tag['src']
        name_tag = meta_div.find('h1')
        if name_tag:
            span_name = name_tag.find('span')
            if span_name:
                personal_info['Nombre'] = span_name.get_text(strip=True)
        paragraphs = meta_div.find_all('p')
        for p in paragraphs:
            strong_tag = p.find('strong')
            if strong_tag:
                key = strong_tag.get_text(strip=True).rstrip(':')
                full_text = p.get_text(" ", strip=True)
                key_text = strong_tag.get_text(strip=True)
                value = full_text.replace(key_text, '', 1).strip()
                if value.startswith(':'):
                    value = value[1:].strip()

                # Separar Position y Shoots si están juntos
                if key == "Position" and "Shoots:" in value:
                    parts = value.split("Shoots:")
                    position = parts[0].replace('▪', '').strip(", \n ")
                    shoots = parts[1].strip()
                    personal_info["Position"] = position
                    personal_info["Shoots"] = shoots
                    if personal_info["Shoots"] == "Left":
                        personal_info["Shoots"] = "Izquierda"
                    else:
                        personal_info["Shoots"] = "Derecha"
                else:
                    personal_info[key] = value
        for p in paragraphs:
            p_text = p.get_text(" ", strip=True)
            if 'cm' in p_text and 'kg' in p_text:
                start = p_text.find('(')
                end = p_text.find(')')
                if start != -1 and end != -1:
                    physical = p_text[start+1:end].strip()
                    personal_info['Físico'] = physical
                else:
                    personal_info['Físico'] = p_text
    salary_span = soup.find('span', string=lambda x: x and "$" in x)
    if salary_span:
        salary = salary_span.text
        personal_info['Salario'] = salary
    allowed_keys = {'Foto', 'Nombre', 'Position', 'Shoots', 'Team', 'Born', 'College', 'Draft', 'Físico', 'Salario'}
    personal_info = {k: v for k, v in personal_info.items() if k in allowed_keys and v}
    return personal_info


def scrapear_estadisticas_individuales(html):
    '''Extrae las estadísticas individuales del jugador desde el HTML'''
    soup = BeautifulSoup(html, "html.parser")
    stats_dict = {}
    # Estadísticas por 36 minutos
    data_row = soup.find('tr', id=re.compile(r'^per_minute_stats\.\d+ Yrs$'))
    if data_row:
        cells = data_row.find_all('td')
        #labels = ['pts_per_minute_36', 'trb_per_minute_36', 'ast_per_minute_36',
         #         'stl_per_minute_36', 'blk_per_minute_36', 'tov_per_minute_36',
          #        'pf_per_minute_36']
        for td in cells:
            stat_name = td.get('data-stat')
            #if stat_name in labels:
            stats_dict[stat_name] = td.get_text(strip=True)
    # Estadísticas avanzadas
    data_row = soup.find('tr', id=re.compile(r'^advanced\.\d+ Yrs$'))
    if data_row:
        cells = data_row.find_all('td')
        #labels = ['ts_pct', 'per', 'obpm', 'dbpm', 'bpm']
        for td in cells:
            stat_name = td.get('data-stat')
            #if stat_name in labels:
            stats_dict[stat_name] = td.get_text(strip=True)
    # Rating ofensivo y defensivo
    data_row = soup.find('tr', id=re.compile(r'^per_poss\.\d+ Yrs$'))
    if data_row:
        cells = data_row.find_all('td')
        #labels = ['off_rtg', 'def_rtg']
        for td in cells:
            stat_name = td.get('data-stat')
            #if stat_name in labels:
            stats_dict[stat_name] = td.get_text(strip=True)
    return stats_dict

def scrapear_jugadores_similares(html):
    '''Extrae los jugadores similares desde el HTML'''
    soup = BeautifulSoup(html, "html.parser")
    table_similarities = soup.find('table', id='sims-thru')
    similar_players = []
    if table_similarities:
        rows = table_similarities.find_all('tr')[3:]  # Omitimos el encabezado
        for row in rows:
            cells = row.find_all('th')
            if len(cells) > 0:
                player_name = cells[0].get_text(strip=True)
                similar_players.append(player_name)
    return similar_players


def generar_prompt_para_llm(player_data: dict) -> str:
    prompt = f"""Eres un analista profesional de baloncesto especializado en estadística avanzada, scouting y redacción técnica. A partir del siguiente objeto JSON que contiene información detallada de un jugador:

{json.dumps(player_data, ensure_ascii=False, indent=2)}

Genera un informe técnico en formato HTML imprimible, estructurado semánticamente y optimizado para su posterior conversión directa a PDF. El contenido debe seguir un formato profesional, claro y autónomo, sin depender de hojas de estilo externas ni scripts.

Requisitos de contenido (no omitas ningún apartado):

1. Imagen del Jugador  
Muestra la imagen en la parte superior izquierda con una etiqueta <img> y un atributo alt descriptivo.

2. Datos Personales  
Usa una tabla <table> simple para mostrar: nombre, altura, peso, posición, mano dominante, edad, nacionalidad, salario y equipo. Asegúrate de que sea legible y bien alineada, sin estar al mismo nivel de altura que la imagen. Además, antes de la tabla, incluye un header <h2> con el título "Reporte Jugador: " seguido del nombre del jugador.

3. Resumen del Desempeño General  
Redacta un análisis breve (máx. 150 palabras), evaluando su rendimiento actual, impacto y posible aportación al equipo.

4. Análisis FODA  
Incluye un análisis estructurado con las siguientes sub-secciones:

  <section>
    <h3>Fortalezas</h3>
    <ul>
      <li>Enumera de 1 a 3 fortalezas clave, con frases concisas de máximo 20 palabras cada una, fundamentadas en datos del JSON.</li>
    </ul>
  </section>

  <section>
    <h3>Oportunidades</h3>
    <ul>
      <li>Identifica de 1 a 3 oportunidades externas que puedan potenciar su rendimiento o carrera (ej. rol en el equipo, entrenador, estilo de juego del equipo, calendario), fundamentadas en datos del JSON.</li>
    </ul>
  </section>

  <section>
    <h3>Debilidades</h3>
    <ul>
      <li>Enumera de 1 a 3 debilidades o áreas de mejora, expresadas con tono constructivo y máximo 20 palabras cada una, fundamentadas en datos del JSON.</li>
    </ul>
  </section>

  <section>
    <h3>Amenazas</h3>
    <ul>
      <li>Enumera de 1 a 3 amenazas externas que podrían limitar su impacto (competencia en el equipo, historial de lesiones, edad, contrato, etc.), fundamentadas en datos del JSON.</li>
    </ul>
  </section>

5. Evaluación del Potencial de Crecimiento  
Redacta un párrafo (máx. 100 palabras) sobre las áreas en las que el jugador aún puede mejorar y su posible evolución futura. Ten en cuenta su edad y estadísticas actuales: si ya está en una etapa madura de su carrera, enfócate en su capacidad para mantener el rendimiento o adaptarse a nuevos roles, en lugar de proyectar un crecimiento significativo.

6. Jugadores Similares  
Menciona de 1 a 3 jugadores comparables. Justifica brevemente la similitud en estilo, físico, rol o estadísticas.

Indicaciones de redacción:

- Usa solo etiquetas HTML estándar (<section>, <h2>, <h3>, <p>, <table>, <ul>, <li>, etc.).
- Aplica estilos básicos inline si es necesario (tamaño de imagen, espaciado mínimo), pensados para que el HTML sea directamente convertible a PDF sin perder legibilidad.
- No incluyas encabezado ni pie de página externos.
- No añadas información fuera del JSON ni uses referencias externas.
- Mantén un lenguaje técnico, preciso y objetivo, dirigido a un equipo técnico profesional.
- Apoya cada sección con estadísticas del JSON: los datos deben fundamentar el análisis.
- Añade el siguiente logo en la esquina inferior derecha del documento, con opacidad media, para identificar el informe como autoría de Basketmática:

<img src="https://basketmatica.wordpress.com/wp-content/uploads/2024/07/logo_basketmatica.png"
     alt="Logo Basketmática"
     style="position: absolute; bottom: 20px; right: 20px; width: 80px; opacity: 0.6;" />

"""

    return prompt


def generar_pdf_jugador(nombre_jugador: str, output_path: str):
    url_jugador = buscar_jugador(nombre_jugador)
    if not url_jugador:
        print(f"No se encontró el jugador: {nombre_jugador}")
        return
    html = obtener_html_con_requests(url_jugador)
    datos_personales = scrapear_datos_personales(html)
    estadisticas = scrapear_estadisticas_individuales(html)
    jugadores_similares = scrapear_jugadores_similares(html)
    player_data = {
        "Datos personales": datos_personales,
        "Estadísticas individuales": estadisticas,
        "Jugadores similares": jugadores_similares
    }

    prompt = generar_prompt_para_llm(player_data)
    genai.configure(api_key=API_KEY)

    model = genai.GenerativeModel("gemini-2.0-flash")

    response = model.generate_content(prompt)

    html_content = re.sub(r"^```html\s*|```$", "", response.text.strip(), flags=re.IGNORECASE)
    if html_content.startswith("(```html)") or html_content.startswith("```html"):
        html = html.split('```html', 1)[-1].lstrip(")`\n")
        
    HTML(string=html_content).write_pdf(output_path)

    return True
