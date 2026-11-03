"""Supplier Intelligence: Streamlit entry point.

    streamlit run app.py

Pages live in ui/pages/, shared UI pieces in ui/, orchestration in service/, business logic in checker/.
"""
import os
from pathlib import Path

import streamlit as st

from checker.env import load_dotenv

ASSETS = Path(__file__).parent / "ui" / "assets"

st.set_page_config(page_title="Supplier Intelligence", page_icon=str(ASSETS / "icon.svg"), layout="wide")

load_dotenv()
try:  # Streamlit Cloud: the key comes from the app's Secrets and is never displayed
    if "OPENAI_API_KEY" in st.secrets and not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = st.secrets["OPENAI_API_KEY"]
    if "OPENAI_MODEL" in st.secrets:
        os.environ.setdefault("OPENAI_MODEL", st.secrets["OPENAI_MODEL"])
except Exception:
    pass

from ui import nav  # noqa: E402  (after secrets are in the environment)
from ui.pages import documents, history, overview, processing, review, settings, validation  # noqa: E402
from ui.sidebar import render_sidebar_footer  # noqa: E402
from ui.theme import inject_css  # noqa: E402

inject_css()
st.logo(str(ASSETS / "logo.svg"), size="large", icon_image=str(ASSETS / "icon.svg"))

pages = {
    "overview": st.Page(overview.render, title="Overview", icon=":material/space_dashboard:", default=True),
    "processing": st.Page(processing.render, title="Document Processing", icon=":material/play_circle:",
                          url_path="processing"),
    "documents": st.Page(documents.render, title="All Documents", icon=":material/description:",
                         url_path="documents"),
    "validation": st.Page(validation.render, title="Validation Results", icon=":material/rule:",
                          url_path="validation"),
    "review": st.Page(review.render, title="Human Review", icon=":material/how_to_reg:", url_path="review"),
    "history": st.Page(history.render, title="Processing History", icon=":material/history:", url_path="history"),
    "settings": st.Page(settings.render, title="Settings", icon=":material/settings:", url_path="settings"),
}
nav.register(pages)
current = st.navigation(list(pages.values()), position="sidebar", expanded=True)
render_sidebar_footer()
current.run()
