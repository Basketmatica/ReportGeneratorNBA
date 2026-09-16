# Generador de Reportes NBA · Basketmática

Aplicación que genera scouting reports en PDF de jugadores de la NBA, a partir
de datos de balldontlie y de las estadísticas oficiales de ESPN, más un
análisis redactado por un modelo de lenguaje.

Es la herramienta que alimenta la sección de reportes de [Basketmática](https://basketmatica.com).
Tiene un generador hermano para la Liga Endesa (`ReportGeneratorACB`) que
comparte el mismo núcleo: mismo informe, mismas reglas y mismo tratamiento del
texto de la IA.

## Cómo funciona

1. **Resolución del jugador** — el nombre introducido se resuelve contra la
   base de datos estática de `nba_api` (sin red) y se completa en balldontlie.
2. **Extracción de datos** — bio (balldontlie + ESPN) y estadísticas de ESPN:
   promedios por temporada con su club, promedios de carrera, intentos de tiro
   por partido y dobles-dobles, triples-dobles y AST/TO oficiales. Las
   temporadas con traspaso se agrupan en una fila con todos los clubes. Si la
   temporada más reciente no tiene partidos del jugador, se usa la última con
   partidos.
3. **Vista del informe** — los datos se reducen exactamente a lo que muestran
   las tablas del PDF, en formato español (coma decimal, metros y kilos). Esa
   misma vista es la que recibe el LLM: no puede citar ninguna cifra que el
   lector no encuentre en una tabla.
4. **Análisis con IA** — el LLM redacta el análisis (resumen de desempeño,
   FODA, proyección, jugadores de perfil similar) como JSON estructurado. El
   modelo nunca genera las tablas de cifras. Después, Python normaliza el
   texto (coma decimal, terminología de baloncesto en España) y descarta al
   propio jugador si aparece entre los similares.
5. **Render y PDF** — Python compone el HTML final con los tokens de diseño
   de la marca y WeasyPrint lo convierte a PDF.

Las métricas calculadas por Basketmática (per-36, estándar en la NBA, y ratios
de tiro como TS%, eFG%, 3PAr y FTr) salen de minutos, promedios e intentos
oficiales y siempre aparecen etiquetadas como calculadas, tanto en las tablas
como en el texto de la IA.

## Stack técnico

| Capa | Tecnología |
|---|---|
| Interfaz | [Streamlit](https://streamlit.io) |
| Datos | [balldontlie](https://www.balldontlie.io) + API pública de ESPN con `httpx`; `nba_api` para resolver nombres |
| Análisis con IA | Cliente propio compatible con la API de OpenAI, con fallback en cadena entre proveedores (Groq, OpenRouter, Cerebras, Mistral, Gemini) |
| Generación de PDF | [WeasyPrint](https://weasyprint.org) |

## Estructura del repositorio

```
streamlit_app.py   Interfaz de usuario
nba_data.py        Datos de la fuente: resolución del jugador, bio y estadísticas ESPN
report_nba.py      Lo propio de la competición: configuración, vista y ratios calculados
informe_comun.py   Núcleo común: prompt, normalización del texto de la IA, render y PDF
llm_client.py      Cliente LLM multi-proveedor con fallback
```

`informe_comun.py` y `llm_client.py` son **idénticos** en los generadores ACB y
NBA. Tras cambiar uno, cópialo al otro repositorio y comprueba:

```bash
diff informe_comun.py ../ReportGeneratorACB/informe_comun.py
diff llm_client.py ../ReportGeneratorACB/llm_client.py
```

## Probarlo en local

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Necesita la clave de balldontlie y al menos una de LLM en `.streamlit/secrets.toml`:

```toml
BALLDONTLIE_API_KEY = "..."           # app.balldontlie.io
GROQ_API_KEY = "..."                  # console.groq.com
GROQ_MODEL = "openai/gpt-oss-120b"
# Fallback recomendado (mismo modelo, límites más altos):
# CEREBRAS_API_KEY = "..."
# LLM_PROVIDERS = "groq,cerebras,gemini"
```

## Por qué este enfoque

Pedir al LLM que redacte solo texto (nunca cifras) mantiene la respuesta
pequeña, viable en los free tier de cualquier proveedor, y garantiza que
ningún número de las tablas del PDF pase por el modelo: todos vienen
directamente de balldontlie y ESPN o de un cálculo etiquetado sobre esos datos.
