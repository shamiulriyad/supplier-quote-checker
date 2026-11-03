"""Page registry so any page can link to another without circular imports."""
import streamlit as st

PAGES: dict[str, "st.Page"] = {}


def register(pages: dict) -> None:
    PAGES.clear()
    PAGES.update(pages)


def go(key: str, **session_values) -> None:
    for k, v in session_values.items():
        st.session_state[k] = v
    st.switch_page(PAGES[key])


def open_document(document_id: str) -> None:
    go("documents", open_document=document_id)
