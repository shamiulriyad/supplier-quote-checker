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
SECRETS = ("OPENAI_API_KEY", "OPENAI_MODEL", "SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_DB_URL",
           "SUPABASE_STORAGE_BUCKET")
try:  # Streamlit Cloud: credentials come from the app's Secrets and are never displayed
    for name in SECRETS:
        if name in st.secrets and name not in os.environ:  # an explicitly set variable (even empty) wins
            os.environ[name] = str(st.secrets[name])
except Exception:
    pass

from ui import nav  # noqa: E402  (after secrets are in the environment)
from ui.pages import documents, history, overview, processing, review, settings, validation  # noqa: E402
from ui.sidebar import render_sidebar_footer  # noqa: E402
from ui.state import get_engine  # noqa: E402
from ui.theme import inject_css  # noqa: E402

inject_css()
st.logo(str(ASSETS / "logo.svg"), size="large", icon_image=str(ASSETS / "icon.svg"))

try:
    get_engine()
except Exception as exc:  # storage misconfigured or unreachable: say so, never fall back silently
    st.error("**Storage is not available, so the app cannot start.**")
    st.write(str(exc))
    st.caption("Check SUPABASE_URL, SUPABASE_KEY and SUPABASE_DB_URL (Streamlit Secrets or .env). "
               "Remove all three to run with local storage instead. See README, Troubleshooting.")
    if st.button("Try again", type="primary"):
        st.rerun()
    st.stop()

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
