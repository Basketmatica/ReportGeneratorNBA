import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
import time
import re
import json
from weasyprint import HTML
import google.generativeai as genai
import os

API_KEY = os.getenv("API_KEY")
# link de la página de basketball-reference
BASE_URL = "https://www.basketball-reference.com"

def buscar_jugador(nombre_jugador):
    '''Busca la URL del perfil del jugador en Basketball Reference'''
    inicial = nombre_jugador.strip().split()[-1][0].lower()
    url = f"{BASE_URL}/players/{inicial}/"
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(url, headers=headers)
    soup = BeautifulSoup(response.text, "html.parser")
    filas = soup.select("table#players tbody tr")
    for fila in filas:
        enlace = fila.select_one("th a")
        if enlace:
            nombre = enlace.text.strip().lower()
            if nombre_jugador.lower() in nombre:
                return BASE_URL + enlace['href']
    return None


def obtener_html_con_selenium(url):
    '''Obtiene el HTML completo de una página utilizando Selenium'''
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-tools")
    options.add_argument("--window-size=1920x1080")
    driver = webdriver.Chrome(options=options)
    driver.get(url)
    time.sleep(3)
    html = driver.page_source
    driver.quit()
    return html


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

4. Fortalezas
Enumera 1 a 3 fortalezas clave en una lista con viñetas (<ul>), usando frases concisas (máx. 20 palabras cada una).

5. Debilidades
Enumera 1 a 3 debilidades detectadas con tono constructivo y objetivo, también en formato de lista.

6. Evaluación del Potencial de Crecimiento
Redacta un párrafo (máx. 100 palabras) sobre las áreas en las que el jugador aún puede mejorar y su posible evolución futura. Ten en cuenta su edad y estadísticas actuales: si ya está en una etapa madura de su carrera, enfócate en su capacidad para mantener el rendimiento o adaptarse a nuevos roles, en lugar de proyectar un crecimiento significativo.

7. Jugadores Similares
Menciona de 1 a 3 jugadores comparables. Justifica brevemente la similitud en estilo, físico, rol o estadísticas.

Indicaciones de redacción:

- Usa solo etiquetas HTML estándar (<section>, <h2>, <p>, <table>, <ul>, etc.).

- Aplica estilos básicos inline si es necesario (tamaño de imagen, espaciado mínimo), pensados para que el HTML sea directamente convertible a PDF sin perder legibilidad.

- No incluyas encabezado ni pie de página externos.

- No añadas información fuera del JSON ni uses referencias externas.

- Mantén un lenguaje técnico, preciso y objetivo, dirigido a un equipo técnico profesional.

- Apoya cada sección con estadísticas del JSON: los datos deben fundamentar el análisis.

"""

    return prompt


def generar_pdf_jugador(nombre_jugador: str, output_path: str):
    url_jugador = buscar_jugador(nombre_jugador)
    if not url_jugador:
        print(f"No se encontró el jugador: {nombre_jugador}")
        return
    html = obtener_html_con_selenium(url_jugador)
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

    model = genai.GenerativeModel("gemini-pro")

    response = model.generate_content(prompt)

    html_content = re.sub(r"^```html\s*|```$", "", response.text.strip(), flags=re.IGNORECASE)
    if html_content.startswith("(```html)") or html_content.startswith("```html"):
        html = html.split('```html', 1)[-1].lstrip(")`\n")
        
    HTML(string=html_content).write_pdf(output_path)

    return True
