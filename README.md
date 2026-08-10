# 🏀 Generador de Reportes NBA · Basketmática

Escribes el nombre de un jugador NBA y en unos segundos obtienes un **informe de scouting en PDF**: perfil físico, estadísticas de la temporada y de carrera, un análisis de desempeño, un FODA y una proyección de rol, con estética propia de [Basketmática](https://basketmatica.wordpress.com/).

Detrás combina datos reales de baloncesto con un LLM, pero de una forma pensada para que el modelo **no pueda inventarse ni un solo número**.

## Cómo funciona

1. **Datos** — el nombre se busca en [balldontlie](https://www.balldontlie.io/) (estadísticas) y se completa con la API pública de ESPN (foto, biografía). Todo cacheado y con throttling para respetar los límites del free tier.
2. **Análisis** — esos datos, ya en JSON, se le pasan a un LLM que redacta el texto: resumen de desempeño, FODA, proyección y jugadores comparables.
3. **PDF** — Python monta el HTML (tablas incluidas) directamente desde el JSON de datos, y [WeasyPrint](https://weasyprint.org/) lo convierte en PDF.

### La decisión que importa: el LLM no toca las tablas

En la versión original (FastAPI + Render), el modelo generaba el HTML completo del informe, tablas de estadísticas incluidas. Eso tiene dos problemas: el modelo puede *alucinar* una cifra al copiarla, y generar un HTML de varios miles de tokens no cabe cómodamente en los límites de los planes gratuitos de LLM.

La solución fue partir el problema en dos responsabilidades separadas:

- **Los números los pinta Python.** Las tablas (`render_html` en `report_nba.py`) se construyen directamente desde el JSON de estadísticas. El modelo nunca las ve ni las reescribe, así que no hay forma de que las altere.
- **El modelo solo escribe prosa.** Devuelve un JSON compacto (~1K tokens) con el análisis cualitativo — lo único para lo que de verdad hace falta un LLM.

El resultado son informes con datos garantizados correctos y una salida del modelo tan pequeña que corre sin problema en cualquier free tier.

## Una capa de LLM que no depende de un solo proveedor

`llm_client.py` habla con cualquier proveedor con API compatible con OpenAI (chat/completions), configurable por *secrets*, con una cadena de fallback: si el proveedor favorito da rate limit o está caído, se prueba con el siguiente.

| Proveedor | Modelo por defecto | Free tier |
|---|---|---|
| **Groq** (recomendado) | llama-3.3-70b-versatile | 30 req/min, ~1.000 req/día, sin tarjeta |
| OpenRouter | llama-3.3-70b-instruct:free | 20 req/min, 50 req/día |
| Cerebras | gpt-oss-120b | ~1M tokens/día |
| Mistral | mistral-small-latest | — |
| Gemini (endpoint compatible OpenAI) | gemini-2.5-flash | según cuenta |

El orden de intento se configura con `LLM_PROVIDERS = "groq,openrouter,gemini"`; los proveedores sin clave configurada simplemente se saltan.

## Stack

- **[Streamlit](https://streamlit.io/)** — interfaz.
- **[balldontlie](https://www.balldontlie.io/) + ESPN** — datos y fotos de jugadores.
- **LLM (Groq / OpenRouter / Cerebras / Mistral / Gemini)** — análisis en texto.
- **[WeasyPrint](https://weasyprint.org/)** — HTML → PDF.

## Probarlo en local

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Necesita al menos una clave de LLM y, opcionalmente, una de balldontlie. Se configuran en `.streamlit/secrets.toml`:

```toml
BALLDONTLIE_API_KEY = "..."   # opcional, sube el límite de peticiones
GROQ_API_KEY = "..."          # gratis, sin tarjeta — console.groq.com
```
