"""
streamlit_app.py — Generador de Reportes NBA · Basketmática (versión Streamlit)

Secrets necesarios (Settings → Secrets en Streamlit Cloud):

    BALLDONTLIE_API_KEY = "..."          # datos bio (gratis, app.balldontlie.io)
    GROQ_API_KEY = "..."                 # análisis LLM (gratis, console.groq.com)
    # Fallbacks opcionales:
    # OPENROUTER_API_KEY = "..."
    # API_KEY = "..."                    # Gemini (si tu cuenta antigua conserva free tier)
    # LLM_PROVIDERS = "groq,openrouter,gemini"
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata

import streamlit as st

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("nba-report")

from llm_client import cargar_proveedores  # noqa: E402
from nba_data import obtener_datos_jugador  # noqa: E402
from report_nba import generar_pdf_jugador_nba  # noqa: E402

st.set_page_config(
    page_title="Generador de Reportes NBA · Basketmática",
    page_icon="🏀",
    layout="centered",
)

CREMA, TINTA, TINTA_2, TEJA = "#FAF6EE", "#1A1A1A", "#6B6B6B", "#C0562F"

st.markdown(
    f"""
    <style>
      .stApp {{ background-color: {CREMA}; }}
      h1, h2, h3, p, label, span {{ color: {TINTA}; }}
      .bm-kicker {{
        color: {TEJA}; text-transform: uppercase; letter-spacing: 2.5px;
        font-size: 0.78rem; font-weight: 600; margin-bottom: 0.2rem;
      }}
      .bm-sub {{ color: {TINTA_2}; font-size: 0.95rem; }}
      div.stButton > button, div.stDownloadButton > button {{
        background-color: {TEJA}; color: {CREMA}; border: none;
        font-weight: 700; width: 100%; padding: 0.6rem;
      }}
      div.stButton > button:hover, div.stDownloadButton > button:hover {{
        background-color: #A34724; color: {CREMA};
      }}
      .bm-nota {{
        border-left: 3px solid {TEJA}; padding: 0.6rem 1rem;
        background: #F2EDE0; font-size: 0.85rem; color: {TINTA_2};
      }}
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(ttl=60 * 60 * 6, show_spinner=False)
def datos_jugador(nombre_normalizado: str):
    return obtener_datos_jugador(nombre_normalizado)


def _safe_filename(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^A-Za-z0-9 _.-]", "_", s).strip()
    return s or "Player"


st.markdown('<p class="bm-kicker">Basketmática · Herramientas</p>', unsafe_allow_html=True)
st.title("Generador de Reportes NBA")
st.markdown(
    '<p class="bm-sub">Introduce el nombre de un jugador de la NBA (en inglés) '
    "y obtén un scouting report en PDF: perfil, estadísticas, per-36, FODA y "
    "proyección.</p>",
    unsafe_allow_html=True,
)

with st.form("form-reporte"):
    nombre = st.text_input(
        "Nombre del jugador",
        placeholder="Ej. LeBron James, Nikola Jokić, Santi Aldama…",
        max_chars=80,
    )
    enviar = st.form_submit_button("Generar informe PDF")

if enviar:
    nombre = (nombre or "").strip()
    if len(nombre) < 2:
        st.error("Escribe un nombre válido (mínimo 2 caracteres).")
        st.stop()

    # balldontlie: la lee nba_data vía env; en Streamlit la inyectamos desde secrets.
    bdl = str(st.secrets.get("BALLDONTLIE_API_KEY", os.getenv("BALLDONTLIE_API_KEY", ""))).strip()
    if bdl:
        os.environ["BALLDONTLIE_API_KEY"] = bdl

    proveedores = cargar_proveedores({**os.environ, **st.secrets})

    try:
        with st.spinner("Buscando al jugador y descargando estadísticas…"):
            data = datos_jugador(nombre.lower())

        dp = data.get("Datos personales", {})
        st.success(f"Encontrado: **{dp.get('Nombre', nombre)}** · {dp.get('Equipo', '—')}")

        with st.spinner("Redactando el análisis y montando el PDF… (~10-25 s)"):
            pdf = generar_pdf_jugador_nba(nombre, proveedores, player_data=data)

        st.download_button(
            label="⬇️ Descargar informe PDF",
            data=pdf,
            file_name=f"{_safe_filename(dp.get('Nombre', nombre))}_Report.pdf",
            mime="application/pdf",
        )

        with st.expander("Ver datos extraídos (JSON)"):
            st.json(data)

    except ValueError as exc:
        st.error(str(exc))
    except EnvironmentError as exc:
        st.error(str(exc))
    except RuntimeError as exc:
        st.error(f"Error transitorio: {exc}. Inténtalo de nuevo en unos segundos.")
    except Exception:
        logger.exception("Error inesperado generando informe para '%s'", nombre)
        st.error("Error interno al generar el informe. Inténtalo de nuevo más tarde.")

st.markdown(
    '<div class="bm-nota"><strong>Nota:</strong> las tablas del PDF se montan '
    "directamente desde los datos (balldontlie + ESPN); el modelo de IA solo "
    "redacta el texto analítico a partir de esos datos.</div>",
    unsafe_allow_html=True,
)
