# Generador de Reportes NBA · Basketmática (versión Streamlit)

Port a Streamlit del generador original (FastAPI + Render), con dos cambios de fondo:

**1. El LLM ya no genera HTML.** Las tablas del PDF las monta Python directamente desde los datos (`render_html` en `report_nba.py`): los números no pasan por el modelo, así que no puede alucinarlos ni truncar tablas. El modelo solo escribe el análisis (desempeño, FODA, proyección, similares) como JSON de ~1K tokens. Esto hace viable cualquier free tier y elimina los fallos de "HTML incompleto".

**2. Capa de LLM intercambiable** (`llm_client.py`). Cualquier proveedor con API compatible OpenAI, configurable por secrets, con cadena de fallback:

| Proveedor | Modelo por defecto | Free tier | Clave |
|---|---|---|---|
| **Groq** (recomendado) | llama-3.3-70b-versatile | 30 req/min, ~1.000 req/día, sin tarjeta | `GROQ_API_KEY` |
| OpenRouter | llama-3.3-70b-instruct:free | 20 req/min, 50 req/día (1.000 con $10 únicos) | `OPENROUTER_API_KEY` |
| Cerebras | gpt-oss-120b | ~1M tokens/día | `CEREBRAS_API_KEY` |
| Gemini (compat OpenAI) | gemini-2.5-flash | solo cuentas antiguas con free tier | `API_KEY` |

Orden de intento: `LLM_PROVIDERS = "groq,openrouter,gemini"` (se saltan los que no tengan clave).

## Ficheros

```
streamlit_app.py   UI Streamlit (tema Basketmática)
nba_data.py        Datos (balldontlie + ESPN) — con los fixes de la revisión aplicados
llm_client.py      Cliente LLM multi-proveedor (OpenAI-compatible) con fallback
report_nba.py      Prompt de análisis (JSON) + render HTML determinista + WeasyPrint
packages.txt       Dependencias de sistema de WeasyPrint para Streamlit Cloud
```

Fixes aplicados sobre `nba_data.py` respecto al repo original: `log.debug` → `logger.warning` (NameError latente), eliminado `_temporada_actual_nba` y el import de `datetime` (código muerto + `utcnow()` deprecado), eliminado `es_jugador_nba` (filtro no-op), y el fallback de foto a ESPN ahora verifica con un HEAD que la URL de cdn.nba.com responde 200. Los fixes de `main.py` (fd leak, CORS, slowapi) ya no aplican: no hay FastAPI.

## Deploy en Streamlit Community Cloud

1. Repo a GitHub con `requirements.txt` y `packages.txt` en la raíz.
2. share.streamlit.io → New app → main file `streamlit_app.py`.
3. Settings → Secrets:
   ```toml
   BALLDONTLIE_API_KEY = "..."
   GROQ_API_KEY = "..."
   ```
4. Deploy.

## Local

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # rellena claves
streamlit run streamlit_app.py
```

## Nota sobre el Astro

Al morir el endpoint de Render, la página `generador-de-reportes-de-la-nba.astro` deja de necesitar el formulario + fetch: sustitúyela por un iframe embebiendo la app (`{URL_STREAMLIT}/?embed=true`), igual que la página del generador ACB. Cuando ambos estén desplegados puedes dar de baja el servicio de Render.
