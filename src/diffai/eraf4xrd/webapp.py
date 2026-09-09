"""Streamlit web UI for ERAF4XRD (the DiffAI app).

Wraps the pipeline in a browser front-end: upload/select PDFs, configure
and launch a run, watch phase progress, browse and edit the extracted
JSON, compare results, chat about a paper, and (optionally) look up
structures on Materials Project. Run with `streamlit run webapp.py`.
"""

import difflib
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from html import escape as html_escape
from pathlib import Path
from typing import Dict

import cv2
import streamlit as st

from diffai.eraf4xrd.utils import make_safe_stem

# set_page_config() must be the FIRST Streamlit command executed in the script.
st.set_page_config(page_title="DiffAI", layout="wide")

# ---- Feature flags ----
# The XRD Plot Digitizer is hidden for now. Set this to True to restore it
# (the sidebar 'Digitizer' expander and the '📊 XRD Plot Digitizer' panel).
SHOW_XRD_DIGITIZER = False


def _strip_ansi(text: str) -> str:
    """Remove ANSI color codes from a string."""
    # Standard CSI sequences (colors, bold, etc.)
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
    # OSC sequences (title bar, hyperlinks, etc.)
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    # Any remaining lone escape chars
    text = re.sub(r"\x1b[^a-zA-Z\[]*[a-zA-Z]?", "", text)
    # Strip box-drawing characters that were part of the table border
    # Replace box-drawing borders with spaces (preserves column alignment)
    text = re.sub(r"[║│┃▌▐]", " ", text)  # vertical → space
    text = re.sub(
        r"[═─━][═─━]+", lambda m: " " * len(m.group()), text
    )  # horizontal runs → spaces
    text = re.sub(
        r"[╔╗╚╝╠╣╦╩╬┌┐└┘├┤┬┴┼╭╮╰╯┏┓┗┛┣┫┳┻╋═─━╸╺╴╵▄▀█░▒▓]", " ", text
    )  # remaining → space
    return text


try:
    import pdfplumber
except ImportError:
    pdfplumber = None

st.markdown(
    """
<style>
/* Import font */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');

/* Main background */
html, body, [data-testid="stAppViewContainer"] {
    background: linear-gradient(135deg, #eef2ff 0%, #fce7f3 50%, #e0f2fe 100%) !important;
    color: #111827 !important;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif !important;
}
/* ===== SCROLLBAR STYLING (ALWAYS VISIBLE GRAY) ===== */
::-webkit-scrollbar {
    width: 10px !important;
    height: 10px !important;
}
::-webkit-scrollbar-track {
    background: #d1d5db !important;
    border-radius: 6px !important;
}
::-webkit-scrollbar-thumb {
    background: #6b7280 !important;
    border-radius: 6px !important;
    border: 2px solid #d1d5db !important;
}
::-webkit-scrollbar-thumb:hover {
    background: #4b5563 !important;
}
/* Firefox */
* {
    scrollbar-width: thin !important;
    scrollbar-color: #6b7280 #d1d5db !important;
}
/* Force scrollbar on all scrollable containers */
[data-testid="stSidebar"] > div:first-child,
[data-testid="stAppViewContainer"],
.main,
section[data-testid="stSidebar"],
div[data-testid="stExpander"] > div {
    overflow-y: auto !important;
}
[data-testid="stSidebar"] ::-webkit-scrollbar-track {
    background: #c7d2fe !important;
}
[data-testid="stSidebar"] ::-webkit-scrollbar-thumb {
    background: #6366f1 !important;
    border: 2px solid #c7d2fe !important;
}
[data-testid="stSidebar"] ::-webkit-scrollbar-thumb:hover {
    background: #4f46e5 !important;
}
/* ===== FORCE LIGHT COLOR SCHEME (overrides browser dark mode) ===== */
:root, html, body {
    color-scheme: light !important;
}

/* Settings modal / dialog */
[data-testid="stModal"],
[data-testid="stModal"] > div,
[data-testid="stModal"] [data-testid="stMarkdownContainer"],
div[role="dialog"],
div[role="dialog"] > div {
    background: #ffffff !important;
    background-color: #ffffff !important;
    color: #111827 !important;
}
div[role="dialog"] label,
div[role="dialog"] p,
div[role="dialog"] span,
div[role="dialog"] h1,
div[role="dialog"] h2,
div[role="dialog"] h3 {
    color: #111827 !important;
    -webkit-text-fill-color: #111827 !important;
}
div[role="dialog"] select,
div[role="dialog"] [data-baseweb="select"] > div {
    background: #f3f4f6 !important;
    color: #111827 !important;
    border-color: #d1d5db !important;
}

/* Menu dropdown separator lines */
div[data-baseweb="popover"] [role="separator"],
div[data-baseweb="popover"] hr,
div[data-baseweb="popover"] li[role="separator"] {
    border-color: #e5e7eb !important;
    background: #e5e7eb !important;
}

/* Menu "Developer options" and other muted labels */
div[data-baseweb="popover"] [data-testid],
div[data-baseweb="popover"] span[class*="muted"],
div[data-baseweb="popover"] div[class*="separator"] {
    color: #6b7280 !important;
    -webkit-text-fill-color: #6b7280 !important;
    background: transparent !important;
}

/* Main app area */
.main .block-container {
    background: linear-gradient(135deg, #ffffffcc, #eef2ffcc, #fdf2f8cc);
    backdrop-filter: blur(12px);
    border-radius: 16px;
}

/* Header */
[data-testid="stHeader"] {
    background: transparent !important;
    box-shadow: none !important;
    border: none !important;
}

/* ===== SIDEBAR DISTINCT SHADE ===== */
[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #e0e7ff, #ede9fe, #fce7f3) !important;
    border-right: 1px solid #a5b4fc !important;
    box-shadow: 4px 0 16px rgba(99, 102, 241, 0.06) !important;
}
[data-testid="stSidebar"] > div:first-child {
    background: transparent !important;
}

/* Toolbar background */
[data-testid="stToolbar"] {
    background: transparent !important;
}

/* General text */
h1, h2, h3, h4, h5, h6, p, label {
    color: #111827 !important;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif !important;
}

small, .stCaption {
    color: #6b7280 !important;
}

/* Containers */
section[data-testid="stContainer"] {
    background-color: transparent !important;
    border-radius: 10px;
}

/* Buttons */
.stButton > button,
.stDownloadButton > button {
    background: linear-gradient(135deg, #6366f1, #8b5cf6) !important;
    border: none !important;
    border-radius: 10px !important;
    font-weight: 500 !important;
}

.stButton > button *,
.stDownloadButton > button * {
    color: #ffffff !important;
    fill: #ffffff !important;
}

.stButton > button:hover,
.stDownloadButton > button:hover {
    background: linear-gradient(135deg, #4f46e5, #7c3aed) !important;
}

.stButton > button:hover *,
.stDownloadButton > button:hover * {
    color: #ffffff !important;
}

/* File uploader */
div[data-testid="stFileUploader"] {
    background: linear-gradient(135deg, #f8fafc, #eef2ff) !important;
    border: 1px dashed #a5b4fc !important;
    border-radius: 12px !important;
}

section[data-testid="stFileUploaderDropzone"] {
    background: #f8fafc !important;
    border: 1px dashed #cbd5e1 !important;
    border-radius: 12px !important;
    color: #111827 !important;
}

section[data-testid="stFileUploaderDropzone"] button {
    background: #eef2ff !important;
    color: #1e3a8a !important;
    border: 1px solid #c7d2fe !important;
    border-radius: 10px !important;
    box-shadow: none !important;
}

section[data-testid="stFileUploaderDropzone"] button:hover {
    background: #e0e7ff !important;
    color: #1d4ed8 !important;
    border: 1px solid #a5b4fc !important;
}

/* Inputs */
div[data-baseweb="base-input"] {
    background-color: #ffffff !important;
    border-radius: 10px !important;
}

div[data-baseweb="base-input"] > div {
    background-color: #ffffff !important;
    border: 1px solid #cbd5e1 !important;
    border-radius: 10px !important;
}

div[data-baseweb="base-input"] input,
div[data-baseweb="base-input"] input::placeholder {
    color: #111827 !important;
    -webkit-text-fill-color: #111827 !important;
    background-color: #ffffff !important;
}

div[data-testid="stTextArea"] textarea {
    background-color: #ffffff !important;
    color: #111827 !important;
    -webkit-text-fill-color: #111827 !important;
    border: 1px solid #cbd5e1 !important;
    border-radius: 10px !important;
}

/* Selectbox */
div[data-baseweb="select"] {
    background-color: transparent !important;
}

div[data-baseweb="select"] > div {
    background-color: #ffffff !important;
    border: 1px solid #cbd5e1 !important;
    border-radius: 10px !important;
}

div[data-baseweb="select"] span,
div[data-baseweb="select"] input,
div[data-baseweb="select"] div[class*="ValueContainer"] *,
div[data-baseweb="select"] [data-testid="stMarkdownContainer"] *,
div[data-baseweb="select"] > div > div > div {
    color: #111827 !important;
    -webkit-text-fill-color: #111827 !important;
}

div[data-baseweb="select"] > div > div {
    background-color: #ffffff !important;
}

ul[role="listbox"] {
    background-color: #ffffff !important;
    color: #111827 !important;
    border: 1px solid #cbd5e1 !important;
}

input::placeholder,
textarea::placeholder {
    color: #6b7280 !important;
    -webkit-text-fill-color: #6b7280 !important;
}

/* Password eye button only */
div[data-baseweb="base-input"] button {
    background: transparent !important;
    color: #6b7280 !important;
    border: none !important;
    box-shadow: none !important;
}

div[data-baseweb="base-input"] button:hover {
    background: transparent !important;
    color: #111827 !important;
}

/* Info / warning box styling */
div[data-testid="stInfo"] {
    background: #eff6ff !important;
    border: 1px solid #93c5fd !important;
    border-radius: 10px !important;
}

div[data-testid="stInfo"] * {
    color: #1e40af !important;
    font-weight: 600 !important;
    font-size: 15px !important;
    line-height: 1.5 !important;
}

div[data-testid="stInfo"] div[role="alert"] {
    background: #eff6ff !important;
    border: 1px solid #93c5fd !important;
    border-radius: 10px !important;
}

div[data-testid="stInfo"] div[role="alert"] p {
    color: #1e40af !important;
    font-weight: 600 !important;
    font-size: 15px !important;
    line-height: 1.6 !important;
}

/* Metric cards */
div[data-testid="stMetric"] {
    background: rgba(255, 255, 255, 0.45) !important;
    border: 1px solid #e5e7eb !important;
    border-radius: 12px !important;
    padding: 10px !important;
}

div[data-testid="stMetricLabel"] * {
    color: #374151 !important;
    font-weight: 600 !important;
}

div[data-testid="stMetricValue"] *,
div[data-testid="stMetricValue"] div,
div[data-testid="stMetricValue"] p,
div[data-testid="stMetricValue"] span {
    color: #111827 !important;
    -webkit-text-fill-color: #111827 !important;
    font-weight: 700 !important;
}

div[data-testid="stMetricDelta"] * {
    color: #111827 !important;
}

/* Sidebar input / select text fix */
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] span,
[data-testid="stSidebar"] small,
[data-testid="stSidebar"] div {
    color: #111827 !important;
}

[data-testid="stSidebar"] div[data-baseweb="base-input"] input {
    color: #111827 !important;
    -webkit-text-fill-color: #111827 !important;
    opacity: 1 !important;
}

[data-testid="stSidebar"] div[data-baseweb="select"] span,
[data-testid="stSidebar"] div[data-baseweb="select"] input,
[data-testid="stSidebar"] div[data-baseweb="select"] div {
    color: #111827 !important;
    -webkit-text-fill-color: #111827 !important;
    opacity: 1 !important;
}

[data-testid="stSidebar"] input::placeholder,
[data-testid="stSidebar"] textarea::placeholder {
    color: #6b7280 !important;
    -webkit-text-fill-color: #6b7280 !important;
    opacity: 1 !important;
}

[data-testid="stSidebar"] div[data-baseweb="base-input"] > div,
[data-testid="stSidebar"] div[data-baseweb="select"] > div {
    background: #ffffff !important;
    border: 1px solid #cbd5e1 !important;
    border-radius: 10px !important;
}

[data-testid="stSidebar"] .stButton > button * {
    color: #ffffff !important;
    fill: #ffffff !important;
}

[data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3 {
    font-size: 28px !important;
    font-weight: 700 !important;
    color: #111827 !important;
}

/* Expander */
div[data-testid="stExpander"] summary {
    background: #f8fafc !important;
    color: #111827 !important;
    border-radius: 10px !important;
}

div[data-testid="stExpander"] > div {
    background: #ffffff !important;
    color: #111827 !important;
    border-radius: 10px !important;
}

div[data-testid="stExpander"] {
    background: transparent !important;
}

/* ===== SIDEBAR TOGGLE BUTTON ===== */
/* When collapsed: always visible */
[data-testid="collapsedControl"] {
    color: #1e293b !important;
    background: rgba(255, 255, 255, 0.92) !important;
    border: 1px solid #94a3b8 !important;
    border-radius: 8px !important;
    backdrop-filter: blur(6px) !important;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.12) !important;
    opacity: 1 !important;
    visibility: visible !important;
    z-index: 9999 !important;
    transition: none !important;
}
[data-testid="collapsedControl"] svg,
[data-testid="collapsedControl"] svg path {
    stroke: #1e293b !important;
    fill: #1e293b !important;
    color: #1e293b !important;
    opacity: 1 !important;
}

/* When expanded: hide by default, show on sidebar hover */
button[kind="header"] {
    color: #1e293b !important;
    background: rgba(255, 255, 255, 0.92) !important;
    border: 1px solid #94a3b8 !important;
    border-radius: 8px !important;
    backdrop-filter: blur(6px) !important;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.12) !important;
    opacity: 0 !important;
    transition: opacity 0.2s ease !important;
    z-index: 9999 !important;
    position: relative !important;
}
button[kind="header"] svg,
button[kind="header"] svg path {
    stroke: #1e293b !important;
    fill: #1e293b !important;
    color: #1e293b !important;
}

/* Show collapse button when hovering the sidebar */
[data-testid="stSidebar"]:hover ~ [data-testid="stHeader"] button[kind="header"],
[data-testid="stSidebar"]:hover button[kind="header"],
[data-testid="stHeader"]:hover button[kind="header"],
button[kind="header"]:hover {
    opacity: 1 !important;
}
button[kind="header"]:hover {
    background: rgba(255, 255, 255, 1) !important;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.18) !important;
}

/* Prevent Streamlit from hiding header on idle */
[data-testid="stHeader"] {
    visibility: visible !important;
    transition: none !important;
    pointer-events: auto !important;
}

/* Sidebar expander arrows */
[data-testid="stSidebar"] details summary svg,
[data-testid="stSidebar"] details summary svg path {
    stroke: #000000 !important;
    fill: #000000 !important;
    color: #000000 !important;
}

[data-testid="stSidebar"] details summary:hover svg,
[data-testid="stSidebar"] details summary:focus svg,
[data-testid="stSidebar"] details[open] summary svg,
[data-testid="stSidebar"] details summary:hover svg path,
[data-testid="stSidebar"] details summary:focus svg path,
[data-testid="stSidebar"] details[open] summary svg path {
    stroke: #000000 !important;
    fill: #000000 !important;
    color: #000000 !important;
}

[data-testid="stSidebar"] summary * {
    color: #111827 !important;
}

[data-testid="stSidebar"] summary svg * {
    stroke: #000000 !important;
    fill: #000000 !important;
}

/* Top-right 3-dots button only */
[data-testid="stToolbar"] button[aria-label="Main menu"] {
    background: #f8fafc !important;
    border: 1px solid #e5e7eb !important;
    border-radius: 6px !important;
}

[data-testid="stToolbar"] button[aria-label="Main menu"] svg,
[data-testid="stToolbar"] button[aria-label="Main menu"] svg path {
    fill: #000000 !important;
    stroke: none !important;
    color: #000000 !important;
}


/* ===== THREE-DOTS MENU & ALL POPOVERS ===== */
div[data-baseweb="popover"],
div[data-baseweb="popover"] > div,
div[data-baseweb="popover"] > div > div,
div[data-baseweb="popover"] ul,
div[data-baseweb="popover"] [role="menu"],
div[data-baseweb="popover"] [role="listbox"] {
    background: #ffffff !important;
    background-color: #ffffff !important;
    border: 1px solid #e5e7eb !important;
    border-radius: 10px !important;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12) !important;
}
div[data-baseweb="popover"] [role="menuitem"],
div[data-baseweb="popover"] [role="option"],
div[data-baseweb="popover"] li,
div[data-baseweb="popover"] a {
    background: #ffffff !important;
    background-color: #ffffff !important;
    color: #111827 !important;
}
div[data-baseweb="popover"] [role="menuitem"] *,
div[data-baseweb="popover"] [role="option"] *,
div[data-baseweb="popover"] li *,
div[data-baseweb="popover"] a * {
    color: #111827 !important;
    -webkit-text-fill-color: #111827 !important;
    fill: #111827 !important;
}
div[data-baseweb="popover"] [role="menuitem"]:hover,
div[data-baseweb="popover"] [role="option"]:hover,
div[data-baseweb="popover"] li:hover {
    background: #f3f4f6 !important;
    background-color: #f3f4f6 !important;
}
div[data-baseweb="popover"] kbd {
    color: #6b7280 !important;
    -webkit-text-fill-color: #6b7280 !important;
    background: transparent !important;
    border: none !important;
}
div[data-baseweb="popover"] [role="separator"],
div[data-baseweb="popover"] hr {
    border-color: #e5e7eb !important;
    background: #e5e7eb !important;
}

/* Settings dialog buttons and theme editor */
div[role="dialog"] button,
div[role="dialog"] [data-testid] button {
    background: #f3f4f6 !important;
    color: #111827 !important;
    border: 1px solid #d1d5db !important;
}
div[role="dialog"] button *,
div[role="dialog"] [data-testid] button * {
    color: #111827 !important;
    -webkit-text-fill-color: #111827 !important;
}
div[role="dialog"] button:hover {
    background: #e5e7eb !important;
}

/* ===== STICKY PAPER TITLE BAR ===== */
.sticky-paper-bar {
    position: sticky;
    top: 0;
    z-index: 100;
    background: linear-gradient(135deg, rgba(238,242,255,0.95), rgba(253,242,248,0.95));
    backdrop-filter: blur(10px);
    border-bottom: 1px solid #c7d2fe;
    padding: 8px 16px;
    margin: -1rem -1rem 1rem -1rem;
    border-radius: 0 0 10px 10px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.06);
}
.sticky-paper-bar .paper-title {
    font-size: 0.85rem;
    font-weight: 600;
    color: #1e293b;
    white-space: normal;
    word-wrap: break-word;
    max-width: 100%;
}
.sticky-paper-bar .paper-doi {
    font-size: 0.72rem;
    color: #6366f1;
    font-weight: 500;
}


/* Hero section */
.hero-shell {
    position: relative;
    overflow: hidden;
    padding: 1.6rem 2.2rem 1.8rem 2.2rem;
    margin: 0 0 1rem 0;
    min-height: 180px;
    display: flex;
    flex-direction: column;
    justify-content: center;
    border: 1px solid rgba(255,255,255,0.55);
    border-radius: 30px;
    background:
        radial-gradient(circle at top right, rgba(99,102,241,0.18), transparent 34%),
        radial-gradient(circle at bottom left, rgba(236,72,153,0.14), transparent 30%),
        linear-gradient(135deg, rgba(255,255,255,0.84), rgba(238,242,255,0.90), rgba(253,242,248,0.84));
    box-shadow: 0 22px 60px rgba(15, 23, 42, 0.10);
    backdrop-filter: blur(14px);
}
.hero-kicker {
    display: inline-block;
    width: fit-content;
    padding: 0.3rem 0.7rem;
    border-radius: 999px;
    border: 1px solid rgba(99,102,241,0.18);
    background: rgba(255,255,255,0.70);
    color: #4338ca !important;
    font-size: 0.85rem;
    font-weight: 600;
    letter-spacing: 0.02em;
    margin-bottom: 0.6rem;
}
.hero-title {
    font-size: clamp(3rem, 5vw, 4rem);
    line-height: 1.1;
    font-weight: 800;
    letter-spacing: -0.04em;
    color: #4F46E5 !important;
    margin: 0;
}
.hero-subtitle {
    max-width: none;
    margin-top: 0.6rem;
    color: #475569 !important;
    font-size: 1.1rem;
    line-height: 1.6;
}
.hero-highlight {
    color: #4f46e5;
    font-weight: 700;
    letter-spacing: -0.01em;
}
.hero-pills {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-top: 0.8rem;
    font-size: 0.9rem;
}
.hero-pill {
    padding: 0.62rem 1rem;
    border-radius: 999px;
    background: rgba(255,255,255,0.76);
    border: 1px solid rgba(203,213,225,0.95);
    color: #334155 !important;
    font-size: 0.96rem;
    font-weight: 600;
}
.hero-anchor {
    display: inline-flex;
    width: fit-content;
    align-items: center;
    justify-content: center;
    gap: 0.4rem;
    min-height: 38px;
    margin-top: 0.8rem;
    padding: 0.6rem 1rem;
    border-radius: 10px;
    text-decoration: none !important;
    background: linear-gradient(135deg, #2563eb, #7c3aed);
    color: #ffffff !important;
    font-weight: 600;
    font-size: 0.95rem;
    box-shadow: 0 6px 16px rgba(37, 99, 235, 0.14);
}
.hero-anchor:hover {
    filter: brightness(0.98);
}
.workspace-shell {
    padding-top: 0.35rem;
}
.workspace-kicker {
    color: #64748b !important;
    text-transform: uppercase;
    letter-spacing: 0.10em;
    font-size: 0.76rem;
    font-weight: 800;
    margin-bottom: 0.35rem;
}
.workspace-title {
    color: #0f172a !important;
    font-size: 1.65rem;
    font-weight: 800;
    letter-spacing: -0.03em;
    margin-bottom: 0.25rem;
}
.workspace-subtitle {
    color: #64748b !important;
    font-size: 1rem;
    margin-bottom: 1rem;
}

/* Tooltips (eye icon, button hints) */
div[data-baseweb="tooltip"],
div[data-baseweb="tooltip"] > div {
    background: #ffffff !important;
    color: #111827 !important;
    border: 1px solid #e5e7eb !important;
    border-radius: 6px !important;
    box-shadow: 0 4px 12px rgba(0,0,0,0.1) !important;
}
div[data-baseweb="tooltip"] * {
    color: #111827 !important;
    -webkit-text-fill-color: #111827 !important;
}

/* ===== HELP ICON (? tooltip trigger) — forced dark ===== */
[data-testid="stTooltipHoverTarget"],
[data-testid="stTooltipHoverTarget"] svg,
[data-testid="stTooltipHoverTarget"] svg *,
[data-testid="stTooltipHoverTarget"] button,
[data-testid="stTooltipHoverTarget"] button svg,
[data-testid="stTooltipHoverTarget"] button svg * {
    color: #374151 !important;
    fill: #374151 !important;
    stroke: #374151 !important;
    opacity: 1 !important;
    -webkit-text-fill-color: #374151 !important;
}
[data-testid="stTooltipHoverTarget"]:hover,
[data-testid="stTooltipHoverTarget"]:hover svg,
[data-testid="stTooltipHoverTarget"]:hover svg * {
    color: #111827 !important;
    fill: #111827 !important;
    stroke: #111827 !important;
    -webkit-text-fill-color: #111827 !important;
}

/* Fallback: any icon button near widget labels */
div[data-testid="stWidgetLabel"] button {
    color: #374151 !important;
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    opacity: 1 !important;
}
div[data-testid="stWidgetLabel"] button svg,
div[data-testid="stWidgetLabel"] button svg * {
    color: #374151 !important;
    fill: #374151 !important;
    stroke: #374151 !important;
}

/* Force visible text cursor (caret) in inputs */
input, textarea, [contenteditable="true"] {
    caret-color: #000 !important;   /* black cursor */
}

/* ===== CODE BLOCK COPY BUTTON (ALWAYS VISIBLE) ===== */
button[data-testid="stCopyButton"] {
    opacity: 1 !important;
    visibility: visible !important;
    background: rgba(241, 245, 249, 0.9) !important;
    border: 1px solid #cbd5e1 !important;
    border-radius: 6px !important;
    transition: none !important;
}
button[data-testid="stCopyButton"] svg,
button[data-testid="stCopyButton"] svg path {
    color: #111827 !important;
    fill: #111827 !important;
    stroke: #111827 !important;
    opacity: 1 !important;
}
button[data-testid="stCopyButton"]:hover {
    background: #e2e8f0 !important;
    border-color: #94a3b8 !important;
}
button[data-testid="stCopyButton"]:hover svg,
button[data-testid="stCopyButton"]:hover svg path {
    color: #000000 !important;
    fill: #000000 !important;
    stroke: #000000 !important;
}
/* Prevent parent from hiding copy button until hover */
pre:not(:hover) button[data-testid="stCopyButton"],
code:not(:hover) button[data-testid="stCopyButton"],
div:not(:hover) > button[data-testid="stCopyButton"] {
    opacity: 1 !important;
    visibility: visible !important;
}

/* ===== CHECKBOX ===== */
[data-testid="stCheckbox"] label span,
[data-testid="stCheckbox"] label p {
    color: #111827 !important;
    -webkit-text-fill-color: #111827 !important;
}

/* ===== RADIO BUTTON ===== */
[data-testid="stRadio"] label span,
[data-testid="stRadio"] label p,
[data-testid="stRadio"] label div {
    color: #111827 !important;
    -webkit-text-fill-color: #111827 !important;
}

/* ===== SLIDER ===== */
[data-testid="stSlider"] label,
[data-testid="stSlider"] label span,
[data-testid="stSlider"] [data-testid="stTickBarMin"],
[data-testid="stSlider"] [data-testid="stTickBarMax"],
[data-testid="stSlider"] [data-testid="stThumbValue"] {
    color: #111827 !important;
    -webkit-text-fill-color: #111827 !important;
}

/* ===== TOAST ===== */
[data-testid="stToast"],
[data-testid="stToast"] > div {
    background: #ffffff !important;
    color: #111827 !important;
    border: 1px solid #e5e7eb !important;
}
[data-testid="stToast"] * {
    color: #111827 !important;
}

/* ===== TABS ===== */
[data-testid="stTabs"] button[data-baseweb="tab"] {
    color: #374151 !important;
}
[data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"] {
    color: #4f46e5 !important;
}

/* ===== MULTISELECT TAGS ===== */
[data-testid="stMultiSelect"] span[data-baseweb="tag"] {
    background: #eef2ff !important;
    color: #111827 !important;
}
[data-testid="stMultiSelect"] span[data-baseweb="tag"] * {
    color: #111827 !important;
    fill: #374151 !important;
}

/* ===== SUCCESS / WARNING / ERROR BOXES ===== */
div[data-testid="stSuccess"] { background: #f0fdf4 !important; border: 1px solid #86efac !important; border-radius: 10px !important; }
div[data-testid="stSuccess"] * { color: #166534 !important; }
div[data-testid="stWarning"] { background: #fffbeb !important; border: 1px solid #fcd34d !important; border-radius: 10px !important; }
div[data-testid="stWarning"] * { color: #92400e !important; }
div[data-testid="stError"] { background: #fef2f2 !important; border: 1px solid #fca5a5 !important; border-radius: 10px !important; }
div[data-testid="stError"] * { color: #991b1b !important; }

/* ===== NUMBER INPUT +/- BUTTONS ===== */
[data-testid="stNumberInput"] button {
    background: #f1f5f9 !important;
    color: #374151 !important;
    border: 1px solid #cbd5e1 !important;
}
[data-testid="stNumberInput"] button * {
    color: #374151 !important;
    fill: #374151 !important;
}

/* ===== SPINNER ===== */
[data-testid="stSpinner"] > div {
    color: #374151 !important;
}


</style>
""",
    unsafe_allow_html=True,
)


st.markdown(
    """
<style>
[data-testid="stJson"] {
    background-color: #f8fafc !important;
    color: #111827 !important;
    border-radius: 10px;
    padding: 10px;
    border: 1px solid #e5e7eb !important;
}

code, pre {
    background-color: #f8fafc !important;
    color: #111827 !important;
    -webkit-text-fill-color: #111827 !important;
}

/* ===== CODE BLOCKS — force dark text in ALL states ===== */
[data-testid="stCode"],
[data-testid="stCode"] > div,
[data-testid="stCode"] pre,
[data-testid="stCode"] code,
[data-testid="stCode"] span,
[data-testid="stCode"] *,
[data-testid="stCode"]:hover,
[data-testid="stCode"]:hover > div,
[data-testid="stCode"]:hover pre,
[data-testid="stCode"]:hover code,
[data-testid="stCode"]:hover span,
[data-testid="stCode"]:hover *,
[data-testid="stCode"]:focus-within,
[data-testid="stCode"]:focus-within pre,
[data-testid="stCode"]:focus-within code,
[data-testid="stCode"]:focus-within span,
[data-testid="stCode"]:focus-within *,
[data-testid="stCode"]:active *,
[data-testid="stCode"] pre:hover,
[data-testid="stCode"] pre:hover *,
[data-testid="stCode"] code:hover,
[data-testid="stCode"] code:hover * {
    color: #111827 !important;
    -webkit-text-fill-color: #111827 !important;
    background-color: #f8fafc !important;
    border-color: #e5e7eb !important;
}
[data-testid="stCode"] {
    border: 1px solid #e5e7eb !important;
    border-radius: 8px !important;
}
.hljs, .hljs span, .hljs code,
code.hljs, code.hljs span,
pre code.hljs, pre code.hljs span,
.hljs:hover, .hljs:hover span {
    color: #111827 !important;
    -webkit-text-fill-color: #111827 !important;
    background-color: #f8fafc !important;
}
</style>
""",
    unsafe_allow_html=True,
)

import streamlit.components.v1 as components  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
PDF_DIR = BASE_DIR / "downloaded_PDFs"
LOG_DIR = BASE_DIR / "logs"
OUT_DIR = BASE_DIR / "outputs"


def _extract_usage_table(text: str):
    """Parse the LLM USAGE REPORT from pipeline output into a list of dicts."""
    lines = text.splitlines()
    rows = []
    in_report = False
    header_found = False
    for line in lines:
        stripped = line.strip()
        if "LLM USAGE REPORT" in stripped:
            in_report = True
            continue
        if not in_report:
            continue
        if stripped.startswith("Phase") and "Requests" in stripped:
            header_found = True
            continue
        if not header_found:
            continue
        if not stripped or all(c in "=─━-" for c in stripped):
            continue
        if "Pipeline wall-clock" in stripped:
            break
        # Parse: phase  requests  input  output  total  cost  time
        # Strip box-drawing chars first
        cleaned = re.sub(
            r"[║│┃═─━╔╗╚╝╠╣╦╩╬┌┐└┘├┤┬┴┼╭╮╰╯┏┓┗┛┣┫┳┻╋▌▐▄▀█░▒▓╸╺╴╵]",
            " ",
            stripped,
        )
        cleaned = cleaned.replace("$", "").strip()
        parts = cleaned.split()
        if len(parts) >= 7:
            rows.append(
                {
                    "Phase": parts[0],
                    "Requests": parts[1],
                    "Input Tok": parts[2],
                    "Output Tok": parts[3],
                    "Total Tok": parts[4],
                    "Cost ($)": parts[5],
                    "Time (s)": parts[6],
                }
            )
        elif len(parts) >= 6:
            rows.append(
                {
                    "Phase": parts[0],
                    "Requests": parts[1],
                    "Input Tok": parts[2],
                    "Output Tok": parts[3],
                    "Total Tok": parts[4],
                    "Cost ($)": parts[5],
                    "Time (s)": "",
                }
            )
    return rows


def save_uploaded_pdfs(uploaded_files):
    """Save uploaded PDF files into the working directory."""
    saved = []
    upload_dir = PDF_DIR / "Uploaded"
    upload_dir.mkdir(parents=True, exist_ok=True)

    for uploaded in uploaded_files:
        safe_name = "".join(
            c if c.isalnum() or c in "._ -" else "_" for c in uploaded.name
        )
        out_path = upload_dir / safe_name
        out_path.write_bytes(uploaded.getbuffer())
        saved.append(out_path)

    return saved


def newest_files(folder: Path, pattern: str, limit: int = 20):
    """Return the most recently modified files matching a pattern."""
    if not folder.exists():
        return []
    return sorted(
        folder.rglob(pattern), key=lambda p: p.stat().st_mtime, reverse=True
    )[:limit]


def resolve_existing_path(path_value):
    """Return the first existing path among the candidates."""
    if not path_value:
        return None

    p = Path(str(path_value))
    candidates = [p]

    if not p.is_absolute():
        candidates.append(BASE_DIR / p)
        if p.parts and p.parts[0] == BASE_DIR.name:
            candidates.append(BASE_DIR.parent / p)
        try:
            candidates.append((BASE_DIR / p).resolve())
        except Exception:
            pass

    seen = set()
    for cand in candidates:
        try:
            key = str(cand.resolve())
        except Exception:
            key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        if cand.exists():
            return cand

    return None


def extract_pdf_text_for_chat(
    pdf_path_value: str, max_chars: int = 30000
) -> str:
    """Extract a PDF's text for the chat/QA feature."""
    if pdfplumber is None:
        return ""
    resolved = resolve_existing_path(pdf_path_value)
    if resolved is None or not resolved.exists():
        return ""

    parts = []
    total = 0

    try:
        with pdfplumber.open(str(resolved)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                try:
                    txt = page.extract_text() or ""
                except Exception:
                    txt = ""

                txt = txt.strip()
                if not txt:
                    continue

                block = f"[Page {i}]\n{txt}\n"
                parts.append(block)
                total += len(block)

                if total >= max_chars:
                    break
    except Exception:
        return ""

    joined = "\n".join(parts)
    return joined[:max_chars]


def derive_safe_base(selected_json: Path, parsed_json):
    """Derive the safe filename stem for a PDF."""
    pdf_value = ""
    if isinstance(parsed_json, dict):
        pdf_value = str(
            parsed_json.get("pdf", "")
            or parsed_json.get("pdf_resolved", "")
            or ""
        ).strip()

    if pdf_value:
        return make_safe_stem(Path(pdf_value).stem)

    name = selected_json.name

    suffixes = [
        "__phase1_raw.json",
        "__phase2_enriched.json",
        "__phase2_clean.json",
        "__phase3_validated_FINAL.json",
        "__phase3_validation_log.json",
    ]
    for suffix in suffixes:
        if name.endswith(suffix):
            return name[: -len(suffix)]

    return make_safe_stem(selected_json.stem)


def load_json_if_exists(path: Path):
    """Load a JSON file if it exists, else return None."""
    try:
        if path.exists():
            return json.loads(
                path.read_text(encoding="utf-8", errors="replace")
            )
    except Exception:
        return None
    return None


def get_xrd_images_for_json(selected_json: Path, parsed_json):
    """Find the XRD figure crops for a result JSON."""
    image_records = []
    seen_paths = set()

    current_pdf = ""
    if isinstance(parsed_json, dict):
        current_pdf = str(parsed_json.get("pdf", "") or "").strip()

    def same_pdf(a: str, b: str) -> bool:
        """True if two paths point to the same PDF."""
        a = str(a or "").strip()
        b = str(b or "").strip()
        if not a or not b:
            return False
        try:
            return Path(a).name.lower() == Path(b).name.lower()
        except Exception:
            return a.lower() == b.lower()

    def add_record(path_value, caption=""):
        """Add one paper record to the harvested results."""
        resolved = resolve_existing_path(path_value)
        if resolved is None:
            return
        key = str(resolved.resolve())
        if key in seen_paths:
            return
        seen_paths.add(key)
        image_records.append(
            {
                "path": resolved,
                "caption": caption or resolved.name,
            }
        )

    def harvest_records(data):
        """Collect all result records from the run's output folder."""
        if not isinstance(data, dict):
            return

        for rec in data.get("xrd_figures", []) or []:
            if not isinstance(rec, dict):
                continue

            fig_no = rec.get("matched_fig_num") or rec.get("figure_number")
            page_no = rec.get("page")
            caption_text = str(
                rec.get("matched_caption_text") or rec.get("caption") or ""
            ).strip()

            label_parts = []
            if fig_no not in (None, ""):
                label_parts.append(f"Figure {fig_no}")
            if page_no not in (None, ""):
                label_parts.append(f"page {page_no}")
            if caption_text:
                label_parts.append(caption_text)

            add_record(rec.get("xrd_path"), " • ".join(label_parts))

    # 1) Try current JSON first
    harvest_records(parsed_json)

    # 2) Try direct sibling lookup from base name
    safe_base = derive_safe_base(selected_json, parsed_json)
    sibling_jsons = [
        selected_json.with_name(f"{safe_base}__phase1_raw.json"),
        selected_json.with_name(f"{safe_base}__phase2_enriched.json"),
    ]
    for sibling in sibling_jsons:
        sibling_data = load_json_if_exists(sibling)
        if sibling_data is not None:
            harvest_records(sibling_data)

    # 3) Robust fallback: scan all phase1/phase2 JSONs and match by PDF path/name
    if not image_records:
        for candidate_json in sorted(
            OUT_DIR.glob("*__phase1_raw.json")
        ) + sorted(OUT_DIR.glob("*__phase2_enriched.json")):
            candidate_data = load_json_if_exists(candidate_json)
            if not isinstance(candidate_data, dict):
                continue
            candidate_pdf = str(candidate_data.get("pdf", "") or "").strip()
            if current_pdf and same_pdf(candidate_pdf, current_pdf):
                harvest_records(candidate_data)

    # 4) Try direct XRD_ONLY folder by base
    xrd_dir = OUT_DIR / f"{safe_base}__XRD_ONLY"
    if xrd_dir.exists():
        for img_path in sorted(xrd_dir.glob("*.png")):
            add_record(img_path, img_path.name)

    # 5) Robust fallback: scan all XRD_ONLY folders and match them through phase1 raw JSON
    if not image_records:
        for candidate_dir in sorted(OUT_DIR.glob("*__XRD_ONLY")):
            if not candidate_dir.is_dir():
                continue

            base = candidate_dir.name.replace("__XRD_ONLY", "")
            phase1_json = OUT_DIR / f"{base}__phase1_raw.json"
            phase1_data = load_json_if_exists(phase1_json)
            if not isinstance(phase1_data, dict):
                continue

            candidate_pdf = str(phase1_data.get("pdf", "") or "").strip()
            if current_pdf and same_pdf(candidate_pdf, current_pdf):
                for img_path in sorted(candidate_dir.glob("*.png")):
                    add_record(img_path, img_path.name)

    return image_records


ARXIV_ID_RE = re.compile(
    r"\b(?:arxiv\s*:\s*)?(\d{4}\.\d{4,5}(?:v\d+)?)\b", re.IGNORECASE
)
ARXIV_OLD_ID_RE = re.compile(
    r"\b([a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)\b", re.IGNORECASE
)


def normalize_title(text: str) -> str:
    """Lowercase + strip punctuation from a title for matching."""
    text = (text or "").lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def title_similarity(a: str, b: str) -> float:
    """Fuzzy ratio (0..1) between two normalized titles."""
    na = normalize_title(a)
    nb = normalize_title(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def normalize_author_tokens(author_value: str):
    """Split an author string into lowercase surnames."""
    raw = re.split(r"[;,]|\band\b", author_value or "", flags=re.IGNORECASE)
    out = []
    for item in raw:
        item = re.sub(r"\s+", " ", item).strip()
        if not item:
            continue
        parts = item.split()
        if parts:
            out.append(parts[-1].lower())
    return out


def author_overlap_score(a: str, b: str) -> float:
    """Fraction of shared surname tokens between two lists."""
    a_set = set(normalize_author_tokens(a))
    b_set = set(normalize_author_tokens(b))
    if not a_set or not b_set:
        return 0.0
    return len(a_set & b_set) / max(len(a_set), len(b_set))


def detect_source_type_from_pdf_value(pdf_value: str) -> str:
    """Guess the source (arXiv/journal) from a PDF value."""
    lower = (pdf_value or "").lower()
    if "arxiv" in lower:
        return "arxiv"
    if "springer" in lower:
        return "springer"
    if "elsevier" in lower or "sciencedirect" in lower:
        return "elsevier"
    if "crossref" in lower or "unpaywall" in lower:
        return "crossref"
    return "unknown"


def extract_arxiv_id(text_value: str) -> str:
    """Find an arXiv id in text."""
    blob = text_value or ""
    m = ARXIV_ID_RE.search(blob)
    if m:
        return m.group(1)
    m = ARXIV_OLD_ID_RE.search(blob)
    if m:
        return m.group(1)
    return ""


def arxiv_id_to_doi(arxiv_id: str) -> str:
    """Map an arXiv id to its DOI."""
    clean_id = re.sub(
        r"^arxiv\s*:\s*", "", (arxiv_id or "").strip(), flags=re.IGNORECASE
    )
    clean_id = re.sub(r"v\d+$", "", clean_id, flags=re.IGNORECASE)
    if not clean_id:
        return ""
    return f"10.48550/arXiv.{clean_id}"


def collect_reference_candidates(parsed_json, selected_json: Path):
    """Gather candidate references (title/DOI/arXiv)."""
    safe_base = derive_safe_base(selected_json, parsed_json)
    candidate_dicts = []

    if isinstance(parsed_json, dict):
        candidate_dicts.append(parsed_json)

    sibling_jsons = [
        selected_json.with_name(f"{safe_base}__phase1_raw.json"),
        selected_json.with_name(f"{safe_base}__phase2_enriched.json"),
        selected_json.with_name(f"{safe_base}__phase2_clean.json"),
        selected_json.with_name(f"{safe_base}__phase3_validated_FINAL.json"),
        selected_json.with_name(f"{safe_base}__phase3_validation_log.json"),
    ]
    for sibling in sibling_jsons:
        sibling_data = load_json_if_exists(sibling)
        if isinstance(sibling_data, dict):
            candidate_dicts.append(sibling_data)

    merged = {
        "title": "",
        "doi": "",
        "author": "",
        "pdf": "",
        "pdf_name": selected_json.name,
        "source_type": "",
        "arxiv_id": "",
        "doi_source": "",
        "published_doi": "",
        "match_confidence": 0.0,
    }

    for data in candidate_dicts:
        pdf_value = str(data.get("pdf", "") or "").strip()
        title_value = str(data.get("title", "") or "").strip()
        doi_value = str(data.get("doi", "") or "").strip()
        author_value = str(data.get("author", "") or "").strip()
        source_type = str(data.get("source_type", "") or "").strip().lower()
        arxiv_id = str(data.get("arxiv_id", "") or "").strip()
        doi_source = str(data.get("doi_source", "") or "").strip()
        published_doi = str(data.get("published_doi", "") or "").strip()

        if not title_value:
            for block_key in ["global", "methods", "global_xrd_text"]:
                global_block = data.get(block_key, {}) or {}
                if isinstance(global_block, dict):
                    title_value = str(
                        global_block.get("title", "") or title_value
                    ).strip()
                    if not doi_value:
                        doi_value = str(
                            global_block.get("doi", "") or ""
                        ).strip()
                    if not author_value:
                        author_value = str(
                            global_block.get("author", "") or ""
                        ).strip()

        if not merged["pdf"] and pdf_value:
            merged["pdf"] = pdf_value
            merged["pdf_name"] = Path(pdf_value).name
        if not merged["title"] and title_value:
            merged["title"] = title_value
        if not merged["doi"] and doi_value:
            merged["doi"] = doi_value
        if not merged["author"] and author_value:
            merged["author"] = author_value
        if not merged["source_type"] and source_type:
            merged["source_type"] = source_type
        if not merged["arxiv_id"] and arxiv_id:
            merged["arxiv_id"] = arxiv_id
        if not merged["doi_source"] and doi_source:
            merged["doi_source"] = doi_source
        if not merged["published_doi"] and published_doi:
            merged["published_doi"] = published_doi

    if not merged["source_type"]:
        merged["source_type"] = detect_source_type_from_pdf_value(
            merged["pdf"]
        )
    if not merged["arxiv_id"]:
        merged["arxiv_id"] = extract_arxiv_id(
            f"{merged['pdf']}\n{merged['title']}"
        )
    if (
        merged["arxiv_id"]
        and merged["source_type"] == "arxiv"
        and not merged["doi"]
    ):
        merged["doi"] = arxiv_id_to_doi(merged["arxiv_id"])
        merged["doi_source"] = "arxiv"

    return merged


def http_get_json(url: str):
    """GET a URL and return the parsed JSON."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def lookup_arxiv_metadata(
    title_value: str, author_value: str, arxiv_id_hint: str = ""
):
    """Look up a paper's metadata from the arXiv API."""
    title_value = (title_value or "").strip()
    if not title_value:
        return None

    try:
        import arxiv
    except Exception:
        return None

    best = None
    best_score = 0.0

    query_parts = []
    if arxiv_id_hint:
        query_parts.append(f"id:{arxiv_id_hint}")
    query_parts.append(f'ti:"{title_value}"')
    query = " OR ".join(query_parts)

    try:
        search = arxiv.Search(
            query=query, max_results=5, sort_by=arxiv.SortCriterion.Relevance
        )
        client = arxiv.Client(page_size=5, delay_seconds=0)
        results = list(client.results(search))
    except Exception:
        return None

    for result in results:
        result_title = getattr(result, "title", "") or ""
        result_authors = "; ".join(
            getattr(a, "name", "")
            for a in (getattr(result, "authors", []) or [])
        )
        entry_id = getattr(result, "entry_id", "") or ""
        short_id = ""
        if hasattr(result, "get_short_id"):
            try:
                short_id = result.get_short_id()
            except Exception:
                short_id = ""
        if not short_id and entry_id:
            short_id = entry_id.rstrip("/").split("/")[-1]

        title_score = title_similarity(title_value, result_title)
        author_score = author_overlap_score(author_value, result_authors)
        id_score = (
            1.0
            if arxiv_id_hint
            and short_id
            and short_id.lower().startswith(
                arxiv_id_hint.lower().replace("arxiv:", "")
            )
            else 0.0
        )
        total_score = (
            0.70 * title_score + 0.20 * author_score + 0.10 * id_score
        )

        if total_score > best_score:
            published_doi = getattr(result, "doi", "") or ""
            best_score = total_score
            best = {
                "title": result_title or title_value,
                "doi": arxiv_id_to_doi(short_id),
                "author": result_authors or author_value,
                "arxiv_id": short_id,
                "published_doi": published_doi,
                "doi_source": "arxiv",
                "source_type": "arxiv",
                "match_confidence": round(total_score, 4),
            }

    if best_score >= 0.75:
        return best
    return None


def lookup_crossref_metadata(title_value: str, author_value: str):
    """Look up a paper's metadata from CrossRef."""
    title_value = (title_value or "").strip()
    if not title_value:
        return None

    query_text = title_value
    if author_value:
        query_text = f"{title_value} {author_value}"

    params = {
        "query.bibliographic": query_text,
        "rows": 5,
    }
    mailto = os.environ.get("UNPAYWALL_EMAIL", "").strip()
    if mailto and "@" in mailto:
        params["mailto"] = mailto

    url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)

    try:
        obj = http_get_json(url)
    except Exception:
        return None

    items = obj.get("message", {}).get("items", []) or []
    best = None
    best_score = 0.0

    for item in items:
        item_titles = item.get("title", []) or []
        item_title = str(item_titles[0] if item_titles else "").strip()
        author_list = item.get("author", []) or []
        item_authors = "; ".join(
            " ".join(
                filter(
                    None,
                    [
                        str(a.get("given", "")).strip(),
                        str(a.get("family", "")).strip(),
                    ],
                )
            ).strip()
            for a in author_list
            if isinstance(a, dict)
        )
        title_score = title_similarity(title_value, item_title)
        author_score = author_overlap_score(author_value, item_authors)
        total_score = 0.80 * title_score + 0.20 * author_score

        if total_score > best_score:
            best_score = total_score
            best = {
                "title": item_title or title_value,
                "doi": str(item.get("DOI", "") or "").strip(),
                "author": item_authors or author_value,
                "doi_source": "crossref",
                "source_type": "crossref",
                "match_confidence": round(total_score, 4),
            }

    if best and best.get("doi") and best_score >= 0.80:
        return best
    return None


def resolve_reference_metadata(parsed_json, selected_json: Path):
    """Resolve the best reference metadata for a paper."""
    ref = collect_reference_candidates(parsed_json, selected_json)

    if ref["source_type"] == "arxiv":
        arxiv_match = lookup_arxiv_metadata(
            ref["title"], ref["author"], ref["arxiv_id"]
        )
        if arxiv_match is not None:
            old_doi = ref.get("doi", "")
            if (
                old_doi
                and old_doi != arxiv_match["doi"]
                and not ref.get("published_doi")
            ):
                ref["published_doi"] = old_doi
            ref.update(arxiv_match)
            ref["pdf"] = ref.get("pdf", "")
            ref["pdf_name"] = (
                Path(ref["pdf"]).name if ref.get("pdf") else selected_json.name
            )
            return ref

    if not ref.get("doi"):
        crossref_match = lookup_crossref_metadata(ref["title"], ref["author"])
        if crossref_match is not None:
            ref.update(crossref_match)
            ref["pdf"] = ref.get("pdf", "")
            ref["pdf_name"] = (
                Path(ref["pdf"]).name if ref.get("pdf") else selected_json.name
            )
            return ref

    ref["pdf_name"] = (
        Path(ref["pdf"]).name if ref.get("pdf") else selected_json.name
    )
    return ref


def extract_paper_reference(parsed_json, selected_json: Path):
    """Extract the citation reference for a paper."""
    return resolve_reference_metadata(parsed_json, selected_json)


def show_reference_and_usage_notice(parsed_json, selected_json: Path):
    """Render the reference + usage notice."""
    ref = extract_paper_reference(parsed_json, selected_json)

    with st.container(border=True):
        st.markdown("### Paper reference")

        if ref["title"]:
            st.write(f"**Title:** {ref['title']}")
        else:
            st.write("**Title:** Not available in this JSON")

        if ref["doi"]:
            st.write(f"**DOI:** {ref['doi']}")
        else:
            st.write("**DOI:** Not available")

        if ref.get("published_doi"):
            st.write(f"**Published DOI:** {ref['published_doi']}")

        if ref.get("author"):
            st.write(f"**Author(s):** {ref['author']}")

        if ref.get("source_type"):
            st.write(f"**Source type:** {ref['source_type']}")

        if ref.get("arxiv_id"):
            st.write(f"**arXiv ID:** {ref['arxiv_id']}")

        if ref.get("doi_source"):
            st.write(f"**DOI source:** {ref['doi_source']}")

        if ref.get("match_confidence"):
            st.write(
                f"**Metadata match confidence:** {ref['match_confidence']:.2f}"
            )

        if ref["pdf"]:
            st.write(f"**Source file:** {ref['pdf']}")
        else:
            st.write(f"**Source file:** {ref['pdf_name']}")

        st.markdown(
            """
            <div style="
                background: #fff7ed;
                border: 1px solid #fdba74;
                border-left: 6px solid #ea580c;
                border-radius: 10px;
                padding: 14px 16px;
                margin-top: 10px;
                color: #c2410c;
                font-weight: 700;
                font-size: 18px;
                line-height: 1.6;
            ">
                Publication content shown or extracted by this tool remains subject to the copyright,
                license, and reuse terms of the original source. Users should verify publisher and
                paper-specific permissions before redistribution, reproduction, or commercial use.
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_paper_summary_card(parsed_json, selected_json: Path):
    """Render the paper summary card in the UI."""
    ref = extract_paper_reference(parsed_json, selected_json)
    figures = (
        parsed_json.get("figures", parsed_json.get("xrd_figures", [])) or []
    )
    methods = parsed_json.get("methods", parsed_json.get("global", {})) or {}

    material_values = []
    for fig in figures:
        if isinstance(fig, dict):
            material = str(fig.get("material", "") or "").strip()
            if material and material not in material_values:
                material_values.append(material)

    st.subheader("Paper summary")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Figures", str(len(figures)))
    c2.metric(
        "Method fields", str(len(methods) if isinstance(methods, dict) else 0)
    )
    c3.metric("Source", ref.get("source_type", "unknown") or "unknown")
    c4.metric("DOI", "Yes" if ref.get("doi") else "No")

    if material_values:
        st.write(f"**Materials:** {', '.join(material_values[:8])}")


def summarize_json_for_compare(json_path: Path):
    """Summarize a result JSON for comparison."""
    try:
        data = json.loads(
            json_path.read_text(encoding="utf-8", errors="replace")
        )
    except Exception:
        return {
            "file": json_path.name,
            "title": "Unreadable JSON",
            "doi": "",
            "source_type": "",
            "figures": 0,
            "materials": "",
            "methods": "",
        }

    ref = extract_paper_reference(data, json_path)
    figures = data.get("figures", data.get("xrd_figures", [])) or []
    methods = data.get("methods", data.get("global", {})) or {}
    material_values = []
    for fig in figures:
        if isinstance(fig, dict):
            material = str(fig.get("material", "") or "").strip()
            if material and material not in material_values:
                material_values.append(material)

    method_keys = []
    if isinstance(methods, dict):
        method_keys = [str(k) for k in methods.keys()]

    return {
        "file": json_path.name,
        "title": ref.get("title", ""),
        "doi": ref.get("doi", ""),
        "source_type": ref.get("source_type", ""),
        "figures": len(figures),
        "materials": ", ".join(material_values[:5]),
        "methods": ", ".join(method_keys[:8]),
    }


def _http_post_json_app(
    url: str, payload: Dict, headers: Dict[str, str]
) -> Dict:
    """POST JSON and return the parsed response dict."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


def ask_llm_about_paper(
    question: str, parsed_json, selected_json: Path
) -> str:
    """Answer a user question about a paper via the LLM."""
    provider = (
        str(st.session_state.get("provider", "gpt") or "gpt").strip().lower()
    )
    model = str(st.session_state.get("model", "gpt-5.2") or "gpt-5.2").strip()
    ref = extract_paper_reference(parsed_json, selected_json)

    pdf_path_value = ""
    if isinstance(parsed_json, dict):
        pdf_path_value = str(parsed_json.get("pdf", "") or "").strip()
    if not pdf_path_value:
        pdf_path_value = str(ref.get("pdf", "") or "").strip()

    pdf_text = extract_pdf_text_for_chat(pdf_path_value, max_chars=30000)

    context = {
        "reference": {
            "title": ref.get("title", ""),
            "doi": ref.get("doi", ""),
            "author": ref.get("author", ""),
            "source_type": ref.get("source_type", ""),
            "arxiv_id": ref.get("arxiv_id", ""),
            "pdf": pdf_path_value,
        },
        "json_content": parsed_json,
        "pdf_text": pdf_text,
    }

    context_text = json.dumps(context, ensure_ascii=False, indent=2)
    if len(context_text) > 50000:
        context_text = context_text[:50000]

    system_prompt = (
        "You answer questions about one paper using the provided JSON, metadata, and extracted PDF text. "
        "Use both the JSON and PDF text together. "
        "If the user asks whether the JSON is correct, compare the JSON against the PDF text and say clearly what matches, what does not match, and what is uncertain. "
        "If the PDF text is missing or insufficient, say so explicitly. "
        "Do not use outside knowledge."
    )
    user_prompt = f"QUESTION:\n{question}\n\nCONTEXT:\n{context_text}"

    if provider == "gpt":
        from openai import OpenAI

        api_key = str(
            st.session_state.get("openai_api_key", "")
            or os.environ.get("OPENAI_API_KEY", "")
        ).strip()
        if not api_key:
            raise RuntimeError("Missing OPENAI_API_KEY")
        client = OpenAI(api_key=api_key)
        resp = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": system_prompt},
                        {"type": "input_text", "text": user_prompt},
                    ],
                }
            ],
            temperature=0.0,
        )
        return (getattr(resp, "output_text", "") or "").strip()

    if provider == "grok":
        from openai import OpenAI

        api_key = str(
            st.session_state.get("xai_api_key", "")
            or os.environ.get("XAI_API_KEY", "")
        ).strip()
        if not api_key:
            raise RuntimeError("Missing XAI_API_KEY")
        client = OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
        resp = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": system_prompt},
                        {"type": "input_text", "text": user_prompt},
                    ],
                }
            ],
            temperature=0.0,
        )
        return (getattr(resp, "output_text", "") or "").strip()

    if provider == "gemini":
        api_key = str(
            st.session_state.get("gemini_api_key", "")
            or os.environ.get("GEMINI_API_KEY", "")
        ).strip()
        if not api_key:
            raise RuntimeError("Missing GEMINI_API_KEY")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        payload = {
            "generationConfig": {
                "temperature": 0.0,
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": system_prompt},
                        {"text": user_prompt},
                    ],
                }
            ],
        }
        obj = _http_post_json_app(url, payload, headers={})
        txt = ""
        for cand in obj.get("candidates", []):
            content = cand.get("content", {})
            for part in content.get("parts", []):
                if "text" in part:
                    txt += part["text"]
        return txt.strip()

    if provider == "claude":
        api_key = str(
            st.session_state.get("anthropic_api_key", "")
            or os.environ.get("ANTHROPIC_API_KEY", "")
        ).strip()
        if not api_key:
            raise RuntimeError("Missing ANTHROPIC_API_KEY")
        url = "https://api.anthropic.com/v1/messages"
        payload = {
            "model": model,
            "max_tokens": 3000,
            "temperature": 0.0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": system_prompt},
                        {"type": "text", "text": user_prompt},
                    ],
                }
            ],
        }
        obj = _http_post_json_app(
            url,
            payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        txt = ""
        for block in obj.get("content", []):
            if block.get("type") == "text":
                txt += block.get("text", "")
        return txt.strip()

    raise ValueError(f"Unsupported provider for chat: {provider}")


def render_paper_ai_chat(
    parsed_json, selected_json: Path, key_prefix: str = "paper_ai"
):
    """Render the paper AI-chat panel in the UI."""
    with st.container(border=True):
        st.subheader("ChatBot: Ask AI about this paper")
        st.caption(
            "Grounded only in the selected JSON, metadata, and extracted document text."
        )
        safe_name = re.sub(r"[^A-Za-z0-9_\-]", "_", selected_json.name)
        key_base = f"{key_prefix}_{safe_name}"
        q_key = f"{key_base}_question"
        a_key = f"{key_base}_answer"
        btn_key = f"{key_base}_ask_btn"
        question = st.text_area(
            "Question",
            key=q_key,
            height=210,
            placeholder="Ask anything about the paper, validation, methods, XRD metadata, or whether the JSON matches the document.",
        )
        if st.button("Ask AI", key=btn_key, width="stretch"):
            if not question.strip():
                st.warning("Enter a question first.")
            else:
                try:
                    with st.spinner("Generating answer..."):
                        st.session_state[a_key] = ask_llm_about_paper(
                            question.strip(), parsed_json, selected_json
                        )
                except Exception as e:
                    st.session_state[a_key] = f"Error: {e}"
        if st.session_state.get(a_key):
            st.markdown("#### Answer")
            st.write(st.session_state[a_key])


def bool_env(name: str, default: bool) -> bool:
    """Read an env var as a bool (with a default)."""
    val = os.environ.get(name)
    if val is None:
        return default
    return str(val).strip().lower() in {"1", "true", "yes", "on"}


def int_env(name: str, default: int) -> int:
    """Read an env var as an int (with a default)."""
    val = os.environ.get(name)
    if val is None or str(val).strip() == "":
        return default
    try:
        return int(val)
    except ValueError:
        return default


def concise_status_from_logs(lines):
    """Summarize the pipeline log into a short status."""
    downloaded = []
    already_exists = []
    phase1_started = False
    phase2_started = False
    cleaner_started = False
    verifier_started = False
    pipeline_done = False

    for line in lines:
        lower = line.lower()

        if (
            "[ok] downloaded" in lower
            or "downloaded oa pdf" in lower
            or "downloaded arxiv pdf" in lower
        ):
            downloaded.append(line)

        if "already exists" in lower:
            already_exists.append(line)

        if "starting phase 1" in lower:
            phase1_started = True
        if "starting phase 2" in lower:
            phase2_started = True
        if ("starting json clean" in lower) or (
            "json clean agent" in lower and "starting" in lower
        ):
            cleaner_started = True
        if ("starting json verify" in lower) or (
            "json verify agent" in lower and "starting" in lower
        ):
            verifier_started = True
        if "pipeline completed" in lower:
            pipeline_done = True

    summary = []

    if downloaded:
        summary.append("Downloaded files:")
        for item in downloaded[-8:]:
            summary.append(f"- {item}")
    elif already_exists:
        summary.append("Download phase:")
        summary.append("- Files already exist.")
        summary.append("- See the detailed log below for file names.")
    else:
        summary.append("No completed downloads yet.")

    summary.append("")
    summary.append("Framework progress:")
    summary.append(
        f"- Step I: {'started' if phase1_started else 'not started'}"
    )
    summary.append(
        f"- Step II: {'started' if phase2_started else 'not started'}"
    )
    summary.append(
        f"- JSON cleaner: {'started' if cleaner_started else 'not started'}"
    )
    summary.append(
        f"- JSON verifier: {'started' if verifier_started else 'not started'}"
    )
    summary.append(
        f"- Pipeline: {'completed' if pipeline_done else 'running'}"
    )

    return "\n".join(summary)


def detect_phase_status(lines):
    """Detect each phase's status from the run outputs."""
    info = {
        "download": "Idle",
        "phase0": "Idle",
        "phase1": "Idle",
        "phase2": "Idle",
        "cleaner": "Idle",
        "verifier": "Idle",
    }

    for line in lines:
        lower = line.lower()

        if (
            "starting arxiv download phase" in lower
            or "starting springer download phase" in lower
            or "starting elsevier download phase" in lower
            or "starting crossref + unpaywall download phase" in lower
        ):
            info["download"] = "Running"

        if (
            "[ok] downloaded" in lower
            or "downloaded oa pdf" in lower
            or "downloaded arxiv pdf" in lower
            or "already exists" in lower
        ):
            info["download"] = "Running"

        if "starting phase 0" in lower:
            info["phase0"] = "Running"
        if "phase 0 skipped" in lower:
            info["phase0"] = "Skipped"

        if "starting phase 1" in lower:
            info["phase1"] = "Running"
        if "phase 1 skipped" in lower:
            info["phase1"] = "Skipped"

        if "starting phase 2" in lower:
            info["phase2"] = "Running"
        if "phase 2 skipped" in lower:
            info["phase2"] = "Skipped"

        if "starting json clean agent" in lower:
            info["cleaner"] = "Running"
        if "json clean agent skipped" in lower:
            info["cleaner"] = "Skipped"

        if "starting json verify agent" in lower:
            info["verifier"] = "Running"
        if "json verify agent skipped" in lower:
            info["verifier"] = "Skipped"

        if "pipeline completed" in lower:
            for key, value in info.items():
                if value == "Running":
                    info[key] = "Done"

    return info


def contains_error_text(text: str, return_code=None) -> bool:
    """True if the text contains error markers."""
    if not text:
        return False

    if return_code == 0:
        return False

    lowered = text.lower()
    error_tokens = [
        "traceback",
        "runtimeerror",
        "exception",
        "pipeline failed",
    ]
    return any(tok in lowered for tok in error_tokens)


def show_json_editor(data, height=650):
    """Render the JSON editor panel in the UI."""
    payload = json.dumps(data, ensure_ascii=False)
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <link href="https://cdn.jsdelivr.net/npm/jsoneditor@10.4.1/dist/jsoneditor.min.css" rel="stylesheet" type="text/css">
        <script src="https://cdn.jsdelivr.net/npm/jsoneditor@10.4.1/dist/jsoneditor.min.js"></script>
        <style>
            html, body {{
                margin: 0;
                padding: 0;
                background: white;
                font-family: Arial, sans-serif;
            }}
            #jsoneditor {{
                height: {height - 10}px;
                border: 1px solid #d9d9d9;
                border-radius: 8px;
            }}
        </style>
    </head>
    <body>
        <div id="jsoneditor"></div>
        <script>
            const container = document.getElementById("jsoneditor");
            const options = {{
                mode: "tree",
                modes: ["tree", "view", "code"],
                navigationBar: true,
                statusBar: true,
                search: true,
                mainMenuBar: true,
                enableSort: false,
                enableTransform: false
            }};
            const editor = new JSONEditor(container, options);
            editor.set({payload});
        </script>
    </body>
    </html>
    """
    components.html(html, height=height, scrolling=True)


PHASE_TERMS = {
    "fcc",
    "bcc",
    "hcp",
    "cubic",
    "tetragonal",
    "orthorhombic",
    "monoclinic",
    "triclinic",
    "trigonal",
    "rhombohedral",
    "hexagonal",
}
SPACE_GROUP_RE = re.compile(
    r"\b([PIFRCA]\s*[0-9]{1,3}(?:/[A-Za-z0-9\-]+)?)\b", re.IGNORECASE
)
FORMULA_TOKEN_RE = re.compile(r"\b(?:[A-Z][a-z]?\d*){1,8}\b")
FORMULA_KEY_HINTS = {
    "formula",
    "composition",
    "material",
    "materials",
    "compound",
    "phase",
    "reported_materials",
    "materials_name",
    "reduced_formula",
}
ELEMENT_NAME_TO_SYMBOL = {
    "hydrogen": "H",
    "helium": "He",
    "lithium": "Li",
    "beryllium": "Be",
    "boron": "B",
    "carbon": "C",
    "nitrogen": "N",
    "oxygen": "O",
    "fluorine": "F",
    "neon": "Ne",
    "sodium": "Na",
    "magnesium": "Mg",
    "aluminum": "Al",
    "aluminium": "Al",
    "silicon": "Si",
    "phosphorus": "P",
    "sulfur": "S",
    "sulphur": "S",
    "chlorine": "Cl",
    "argon": "Ar",
    "potassium": "K",
    "calcium": "Ca",
    "scandium": "Sc",
    "titanium": "Ti",
    "vanadium": "V",
    "chromium": "Cr",
    "manganese": "Mn",
    "iron": "Fe",
    "cobalt": "Co",
    "nickel": "Ni",
    "copper": "Cu",
    "zinc": "Zn",
    "gallium": "Ga",
    "germanium": "Ge",
    "arsenic": "As",
    "selenium": "Se",
    "bromine": "Br",
    "krypton": "Kr",
    "rubidium": "Rb",
    "strontium": "Sr",
    "yttrium": "Y",
    "zirconium": "Zr",
    "niobium": "Nb",
    "molybdenum": "Mo",
    "technetium": "Tc",
    "ruthenium": "Ru",
    "rhodium": "Rh",
    "palladium": "Pd",
    "silver": "Ag",
    "cadmium": "Cd",
    "indium": "In",
    "tin": "Sn",
    "antimony": "Sb",
    "tellurium": "Te",
    "iodine": "I",
    "xenon": "Xe",
    "cesium": "Cs",
    "caesium": "Cs",
    "barium": "Ba",
    "lanthanum": "La",
    "cerium": "Ce",
    "praseodymium": "Pr",
    "neodymium": "Nd",
    "samarium": "Sm",
    "europium": "Eu",
    "gadolinium": "Gd",
    "terbium": "Tb",
    "dysprosium": "Dy",
    "holmium": "Ho",
    "erbium": "Er",
    "thulium": "Tm",
    "ytterbium": "Yb",
    "lutetium": "Lu",
    "hafnium": "Hf",
    "tantalum": "Ta",
    "tungsten": "W",
    "rhenium": "Re",
    "osmium": "Os",
    "iridium": "Ir",
    "platinum": "Pt",
    "gold": "Au",
    "mercury": "Hg",
    "thallium": "Tl",
    "lead": "Pb",
    "bismuth": "Bi",
    "thorium": "Th",
    "uranium": "U",
}


def normalize_formula_like(value: str) -> str:
    """Normalize a chemical-formula-like string."""
    text = str(value).strip()
    if not text:
        return ""

    low = text.lower()
    if low in ELEMENT_NAME_TO_SYMBOL:
        return ELEMENT_NAME_TO_SYMBOL[low]

    text = text.replace(" ", "")
    if re.fullmatch(r"(?:[A-Z][a-z]?\d*){1,8}", text):
        return text

    found = FORMULA_TOKEN_RE.findall(text)
    if found:
        found = sorted(found, key=len, reverse=True)
        return found[0]

    return ""


def walk_json(node, path="root"):
    """Recursively walk a nested JSON structure."""
    if isinstance(node, dict):
        for k, v in node.items():
            child_path = f"{path}.{k}"
            yield from walk_json(v, child_path)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            child_path = f"{path}[{i}]"
            yield from walk_json(item, child_path)
    else:
        yield path, node


def extract_mp_hints_from_json(data):
    """Pull Materials Project search hints from a JSON."""
    formulas = []
    phases = []
    space_groups = []
    raw_strings = []

    for path, value in walk_json(data):
        if isinstance(value, (str, int, float)):
            text = str(value).strip()
            if not text:
                continue
            raw_strings.append((path.lower(), text))

    seen_formula = set()
    seen_phase = set()
    seen_sg = set()

    for path, text in raw_strings:
        last_key = path.split(".")[-1].lower()

        if any(hint in last_key for hint in FORMULA_KEY_HINTS):
            formula_candidate = normalize_formula_like(text)
            if formula_candidate and formula_candidate not in seen_formula:
                seen_formula.add(formula_candidate)
                formulas.append(formula_candidate)

        sg_matches = SPACE_GROUP_RE.findall(text)
        for match in sg_matches:
            sg = " ".join(str(match).split())
            if sg not in seen_sg:
                seen_sg.add(sg)
                space_groups.append(sg)

        lower = text.lower()
        for term in PHASE_TERMS:
            if re.search(rf"\b{re.escape(term)}\b", lower):
                if term not in seen_phase:
                    seen_phase.add(term)
                    phases.append(term)

    if not formulas:
        for _, text in raw_strings:
            formula_candidate = normalize_formula_like(text)
            if formula_candidate and formula_candidate not in seen_formula:
                seen_formula.add(formula_candidate)
                formulas.append(formula_candidate)
            if len(formulas) >= 8:
                break

    return {
        "formulas": formulas[:8],
        "phases": phases[:8],
        "space_groups": space_groups[:8],
    }


def score_mp_doc(doc, formula_hint="", phase_hint="", space_group_hint=""):
    """Score a Materials Project candidate against the paper."""
    score = 0

    formula_hint_clean = (formula_hint or "").strip()
    phase_hint_clean = (phase_hint or "").strip().lower()
    sg_hint_clean = " ".join((space_group_hint or "").split()).lower()

    doc_formula = str(getattr(doc, "formula_pretty", "") or "").strip()
    if formula_hint_clean and doc_formula == formula_hint_clean:
        score += 100
    elif formula_hint_clean and normalize_formula_like(
        doc_formula
    ) == normalize_formula_like(formula_hint_clean):
        score += 90

    symmetry = getattr(doc, "symmetry", None)
    if symmetry is not None:
        doc_sg_symbol = str(getattr(symmetry, "symbol", "") or "").strip()
        doc_crystal_system = (
            str(getattr(symmetry, "crystal_system", "") or "").strip().lower()
        )

        if (
            sg_hint_clean
            and " ".join(doc_sg_symbol.split()).lower() == sg_hint_clean
        ):
            score += 40

        if phase_hint_clean:
            phase_map = {
                "fcc": "cubic",
                "bcc": "cubic",
                "hcp": "hexagonal",
            }
            expected_system = phase_map.get(phase_hint_clean, phase_hint_clean)
            if doc_crystal_system == expected_system:
                score += 20

    if bool(getattr(doc, "is_stable", False)):
        score += 10

    eah = getattr(doc, "energy_above_hull", None)
    if isinstance(eah, (int, float)):
        score += max(0, 10 - int(min(eah * 1000.0, 10)))

    return score


def structure_to_cif_text(structure, conventional_cell=True):
    """Render a pymatgen Structure to CIF text."""
    from pymatgen.io.cif import CifWriter

    structure_to_write = structure
    if conventional_cell:
        try:
            from pymatgen.symmetry.analyzer import SpacegroupAnalyzer

            structure_to_write = SpacegroupAnalyzer(
                structure
            ).get_conventional_standard_structure()
        except Exception:
            structure_to_write = structure

    return str(CifWriter(structure_to_write))


def search_materials_project(
    formula_hint,
    phase_hint,
    space_group_hint,
    api_key,
    conventional_cell=True,
    max_results=5,
):
    """Query Materials Project for matching structures."""
    try:
        from mp_api.client import MPRester
    except Exception as e:
        return [], f"Could not import mp-api. Install it first. Details: {e}"

    formula_query = normalize_formula_like(formula_hint)
    if not formula_query:
        return (
            [],
            "Could not determine a valid formula/query. Enter something like Cu, Mo, Cu2O, MoS2.",
        )

    try:
        with MPRester(api_key) as mpr:
            docs = mpr.materials.summary.search(
                formula=formula_query,
                fields=[
                    "material_id",
                    "formula_pretty",
                    "symmetry",
                    "energy_above_hull",
                    "is_stable",
                    "database_IDs",
                ],
            )

            ranked = sorted(
                docs,
                key=lambda d: score_mp_doc(
                    d, formula_query, phase_hint, space_group_hint
                ),
                reverse=True,
            )[:max_results]

            results = []
            for doc in ranked:
                mpid = str(getattr(doc, "material_id", ""))
                if not mpid:
                    continue

                structure = mpr.get_structure_by_material_id(mpid)
                cif_text = structure_to_cif_text(
                    structure, conventional_cell=conventional_cell
                )

                symmetry = getattr(doc, "symmetry", None)
                sg_symbol = str(getattr(symmetry, "symbol", "") or "")
                sg_number = getattr(symmetry, "number", None)
                crystal_system = str(
                    getattr(symmetry, "crystal_system", "") or ""
                )

                database_ids = getattr(doc, "database_IDs", None)
                icsd_ids = []
                if database_ids is not None:
                    try:
                        icsd_ids = list(database_ids.get("icsd", []))
                    except Exception:
                        icsd_ids = []

                results.append(
                    {
                        "material_id": mpid,
                        "formula_pretty": str(
                            getattr(doc, "formula_pretty", "") or ""
                        ),
                        "space_group_symbol": sg_symbol,
                        "space_group_number": sg_number,
                        "crystal_system": crystal_system,
                        "energy_above_hull": getattr(
                            doc, "energy_above_hull", None
                        ),
                        "is_stable": bool(getattr(doc, "is_stable", False)),
                        "icsd_ids": icsd_ids,
                        "cif_text": cif_text,
                    }
                )

            return results, None

    except Exception as e:
        return [], f"Materials Project search failed: {e}"


def init_state():
    """Initialize the Streamlit session state."""
    defaults = {
        "elements_input": os.environ.get("ELEMENTS", "Cu OR Copper"),
        "technique_input": os.environ.get(
            "TECHNIQUE",
            '"x-ray diffraction" OR "x ray diffraction" OR XRD OR PXRD OR '
            '"powder x-ray diffraction" OR "powder diffraction"',
        ),
        "run_full": bool_env("RUN_FULL", True),
        "run_download": bool_env("RUN_DOWNLOAD", True),
        "run_phase0": bool_env("RUN_PHASE0_FILTER", True),
        "run_phase1": bool_env("RUN_PHASE1", False),
        "run_phase2": bool_env("RUN_PHASE2", False),
        "run_clean": bool_env("RUN_JSON_CLEAN_AGENT", False),
        "run_verify": bool_env("RUN_JSON_VERIFY_AGENT", False),
        "use_arxiv": bool_env("USE_ARXIV", True),
        "use_springer": bool_env("USE_SPRINGER", False),
        "use_elsevier": bool_env("USE_ELSEVIER", False),
        "use_crossref": bool_env("USE_CROSSREF", False),
        "target_downloads": int_env("TARGET_DOWNLOADS", 2),
        "provider": os.environ.get("PROVIDER", "gpt"),
        "model": os.environ.get("MODEL", "gpt-5.2"),
        "phase0_provider": os.environ.get(
            "PHASE0_PROVIDER", os.environ.get("PROVIDER", "gpt")
        ),
        "phase0_model": os.environ.get(
            "PHASE0_MODEL", os.environ.get("MODEL", "gpt-5.2")
        ),
        "openai_api_key": os.environ.get("OPENAI_API_KEY", ""),
        "gemini_api_key": os.environ.get("GEMINI_API_KEY", ""),
        "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
        "xai_api_key": os.environ.get("XAI_API_KEY", ""),
        "springer_api_key": os.environ.get("SPRINGER_API_KEY", ""),
        "elsevier_api_key": os.environ.get("ELSEVIER_API_KEY", ""),
        "unpaywall_email": os.environ.get("UNPAYWALL_EMAIL", ""),
        "mp_api_key": os.environ.get("MP_API_KEY", ""),
        "phase1_provider": os.environ.get(
            "PHASE1_PROVIDER", os.environ.get("PROVIDER", "gpt")
        ),
        "phase1_model": os.environ.get(
            "PHASE1_MODEL", os.environ.get("MODEL", "gpt-5.2")
        ),
        "phase2_provider": os.environ.get(
            "PHASE2_PROVIDER", os.environ.get("PROVIDER", "gpt")
        ),
        "phase2_model": os.environ.get(
            "PHASE2_MODEL", os.environ.get("MODEL", "gpt-5.2")
        ),
        "verify_provider": os.environ.get(
            "VERIFY_PROVIDER", os.environ.get("PROVIDER", "gpt")
        ),
        "verify_model": os.environ.get(
            "VERIFY_MODEL", os.environ.get("MODEL", "gpt-5.2")
        ),
        "run_digitizer": False,
        "digitizer_algorithm": "topmost",
        "digitizer_model": os.environ.get("DIGITIZER_MODEL", "gpt-4o"),
        "digitizer_selected_figures": [],
    }

    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

    st.session_state["openai_api_key"] = st.session_state.get(
        "openai_api_key"
    ) or os.environ.get("OPENAI_API_KEY", "")
    st.session_state["gemini_api_key"] = st.session_state.get(
        "gemini_api_key"
    ) or os.environ.get("GEMINI_API_KEY", "")
    st.session_state["anthropic_api_key"] = st.session_state.get(
        "anthropic_api_key"
    ) or os.environ.get("ANTHROPIC_API_KEY", "")
    st.session_state["xai_api_key"] = st.session_state.get(
        "xai_api_key"
    ) or os.environ.get("XAI_API_KEY", "")
    st.session_state["springer_api_key"] = st.session_state.get(
        "springer_api_key"
    ) or os.environ.get("SPRINGER_API_KEY", "")
    st.session_state["elsevier_api_key"] = st.session_state.get(
        "elsevier_api_key"
    ) or os.environ.get("ELSEVIER_API_KEY", "")
    st.session_state["unpaywall_email"] = st.session_state.get(
        "unpaywall_email"
    ) or os.environ.get("UNPAYWALL_EMAIL", "")
    st.session_state["mp_api_key"] = st.session_state.get(
        "mp_api_key"
    ) or os.environ.get("MP_API_KEY", "")

    if "is_running" not in st.session_state:
        st.session_state.is_running = False
    if "process_pid" not in st.session_state:
        st.session_state.process_pid = None
    if "last_output" not in st.session_state:
        st.session_state.last_output = ""
    if "last_status_summary" not in st.session_state:
        st.session_state.last_status_summary = ""
    if "last_return_code" not in st.session_state:
        st.session_state.last_return_code = None
    if "last_run_message" not in st.session_state:
        st.session_state.last_run_message = None
    if "last_run_message_type" not in st.session_state:
        st.session_state.last_run_message_type = "info"
    if "last_elapsed_seconds" not in st.session_state:
        st.session_state.last_elapsed_seconds = None
    if "last_phase_status" not in st.session_state:
        st.session_state.last_phase_status = {}
    if "run_started_at" not in st.session_state:
        st.session_state.run_started_at = None


def reset_form():
    """Reset the input form to its defaults."""
    keys_to_reset = [
        "elements_input",
        "technique_input",
        "run_download",
        "run_phase0",
        "run_phase1",
        "run_phase2",
        "run_clean",
        "run_verify",
        "use_arxiv",
        "use_springer",
        "use_elsevier",
        "use_crossref",
        "target_downloads",
        "provider",
        "model",
        "openai_api_key",
        "gemini_api_key",
        "anthropic_api_key",
        "xai_api_key",
        "springer_api_key",
        "elsevier_api_key",
        "unpaywall_email",
        "mp_api_key",
        "phase1_provider",
        "phase1_model",
        "phase2_provider",
        "phase2_model",
        "verify_provider",
        "verify_model",
    ]
    for key in keys_to_reset:
        if key in st.session_state:
            del st.session_state[key]
    init_state()


def apply_preset(name: str):
    """Apply a preset configuration to the form."""
    if name == "Cu XRD full":
        st.session_state.elements_input = "Cu OR Copper"
        st.session_state.technique_input = (
            '"x-ray diffraction" OR "x ray diffraction" OR XRD OR PXRD OR '
            '"powder x-ray diffraction" OR "powder diffraction"'
        )
        st.session_state.run_download = True
        st.session_state.run_phase0 = True
        st.session_state.run_phase1 = True
        st.session_state.run_phase2 = True
        st.session_state.run_clean = True
        st.session_state.run_verify = True
        st.session_state.run_full = True
        st.session_state.use_arxiv = True
        st.session_state.use_springer = True
        st.session_state.use_elsevier = False
        st.session_state.use_crossref = False
        st.session_state.target_downloads = 2

    elif name == "Mo XRD full":
        st.session_state.elements_input = "Mo OR Molybdenum"
        st.session_state.technique_input = (
            '"x-ray diffraction" OR "x ray diffraction" OR XRD OR PXRD OR '
            '"powder x-ray diffraction" OR "powder diffraction"'
        )
        st.session_state.run_download = True
        st.session_state.run_phase0 = True
        st.session_state.run_phase1 = True
        st.session_state.run_phase2 = True
        st.session_state.run_clean = True
        st.session_state.run_verify = True
        st.session_state.run_full = True
        st.session_state.use_arxiv = True
        st.session_state.use_springer = True
        st.session_state.use_elsevier = False
        st.session_state.use_crossref = False
        st.session_state.target_downloads = 2

    elif name == "Uploaded PDFs only":
        st.session_state.run_download = False
        st.session_state.run_phase0 = False
        st.session_state.run_phase1 = True
        st.session_state.run_phase2 = True
        st.session_state.run_clean = True
        st.session_state.run_verify = True
        st.session_state.run_full = False
        st.session_state.use_arxiv = False
        st.session_state.use_springer = False
        st.session_state.use_elsevier = False
        st.session_state.use_crossref = False


def run_pipeline(env_overrides: dict):
    """Launch the pipeline as a subprocess from the UI."""
    env = os.environ.copy()
    env.update(
        {
            k: str(v)
            for k, v in env_overrides.items()
            if v is not None and str(v) != ""
        }
    )
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    cmd = [sys.executable, "-m", "diffai.eraf4xrd.app"]
    # Run from the launch dir, NOT the installed package dir -- otherwise
    # outputs land under src/diffai/eraf4xrd/ (deep path + pollutes install).
    process = subprocess.Popen(
        cmd,
        cwd=os.getcwd(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )

    st.session_state.process_pid = process.pid

    top_info_placeholder = st.empty()
    progress_placeholder = st.empty()
    phase_placeholder = st.empty()
    live_status_placeholder = st.empty()
    live_details_placeholder = st.empty()

    lines = []
    started_at = time.time()

    if process.stdout is not None:
        for line in process.stdout:
            lines.append(line.rstrip())

            current_summary = concise_status_from_logs(lines)
            phase_status = detect_phase_status(lines)

            elapsed = time.time() - started_at
            top_info_placeholder.info(
                f"Running framework... Elapsed: {elapsed:.1f} sec"
            )

            progress_value = min(0.95, max(0.05, len(lines) / 200.0))
            if "pipeline completed" in current_summary.lower():
                progress_value = 1.0
            progress_placeholder.progress(progress_value)

            with phase_placeholder.container():
                st.subheader("Step status")
                c1, c2, c3, c4, c5, c6 = st.columns(6)
                c1.metric("Download", phase_status.get("download", "Idle"))
                c2.metric("Screen", phase_status.get("phase0", "Idle"))
                c3.metric("Step I", phase_status.get("phase1", "Idle"))
                c4.metric("Step II", phase_status.get("phase2", "Idle"))
                c5.metric("Cleaner", phase_status.get("cleaner", "Idle"))
                c6.metric("Verifier", phase_status.get("verifier", "Idle"))

            with live_status_placeholder.container():
                st.subheader("Live run summary")
                st.code(current_summary, language="text")

    process.wait()
    elapsed = time.time() - started_at
    st.session_state.process_pid = None

    full_output = "\n".join(lines)
    final_summary = concise_status_from_logs(lines)
    final_phase_status = detect_phase_status(lines)

    top_info_placeholder.success(f"Run finished in {elapsed:.1f} sec")
    progress_placeholder.progress(1.0)

    with phase_placeholder.container():
        st.subheader("Step status")
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("Download", final_phase_status.get("download", "Idle"))
        c2.metric("Screen", final_phase_status.get("phase0", "Idle"))
        c3.metric("Step I", final_phase_status.get("phase1", "Idle"))
        c4.metric("Step II", final_phase_status.get("phase2", "Idle"))
        c5.metric("Cleaner", final_phase_status.get("cleaner", "Idle"))
        c6.metric("Verifier", final_phase_status.get("verifier", "Idle"))

    with live_status_placeholder.container():
        st.subheader("Live run summary")
        st.code(final_summary, language="text")

    with live_details_placeholder.container():
        with st.expander("Live run details", expanded=False):
            st.code(
                full_output if full_output else "No output captured.",
                language="text",
            )

    return (
        process.returncode,
        full_output,
        final_summary,
        elapsed,
        final_phase_status,
    )


def stop_running_process():
    """Stop the running pipeline process (cross-platform)."""
    pid = st.session_state.get("process_pid")
    if not pid:
        return False, "No running process found."

    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            os.kill(pid, signal.SIGKILL)

        st.session_state.process_pid = None
        st.session_state.is_running = False
        return True, f"Stopped process {pid}."
    except Exception as e:
        return False, f"Failed to stop process {pid}: {e}"


def phase_model_selector(
    label_prefix, provider_key, model_key, model_options, disabled=False
):
    """Render the per-phase provider/model selector."""
    provider_options = list(model_options.keys())

    current_provider = st.session_state.get(provider_key, "gpt")
    if current_provider not in provider_options:
        current_provider = provider_options[0]
        st.session_state[provider_key] = current_provider

    st.selectbox(
        f"{label_prefix} provider",
        provider_options,
        key=provider_key,
        disabled=disabled,
    )

    valid_models = model_options[st.session_state[provider_key]]
    current_model = st.session_state.get(model_key, valid_models[0])
    if current_model not in valid_models:
        current_model = valid_models[0]
        st.session_state[model_key] = current_model

    st.selectbox(
        f"{label_prefix} model",
        valid_models,
        key=model_key,
        disabled=disabled,
    )


if "current_page" not in st.session_state:
    st.session_state.current_page = "home"


def go_to_eraf4xrd():
    """Switch the UI to the ERAF4XRD page."""
    st.session_state.current_page = "eraf4xrd"


def go_home():
    """Switch the UI to the home page."""
    st.session_state.current_page = "home"


# =========================================================================
# HOME / LANDING PAGE
# =========================================================================
if st.session_state.current_page == "home":
    st.markdown(
        """
    <style>
    .diffai-home {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        min-height: 70vh;
        text-align: center;
        padding: 3rem 1rem;
    }
    .diffai-badge {
        display: inline-block;
        padding: 0.35rem 1rem;
        border-radius: 999px;
        background: rgba(99, 102, 241, 0.08);
        border: 1px solid rgba(99, 102, 241, 0.20);
        color: #4338ca;
        font-size: 0.85rem;
        font-weight: 600;
        letter-spacing: 0.04em;
        margin-bottom: 1.5rem;
    }
    .diffai-title {
        font-size: clamp(3.5rem, 8vw, 6rem);
        font-weight: 800;
        letter-spacing: -0.04em;
        line-height: 1.05;
        color: #0f172a;
        margin: 0 0 0.75rem 0;
    }
    .diffai-title span {
        background: linear-gradient(135deg, #6366f1, #8b5cf6, #ec4899);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
    }
    .diffai-subtitle {
        font-size: 1.2rem;
        color: #64748b;
        max-width: 540px;
        margin: 0 auto 3rem auto;
        line-height: 1.7;
    }
    .diffai-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(260px, 320px));
        gap: 1.5rem;
        justify-content: center;
        width: 100%;
        max-width: 1000px;
    }
    .tool-card {
        background: rgba(255, 255, 255, 0.75);
        border: 2px solid #e2e8f0;
        border-radius: 20px;
        padding: 2rem 1.8rem;
        text-align: left;
        transition: box-shadow 0.25s, border-color 0.25s, transform 0.15s, background 0.25s;
        cursor: default;
    }
    .tool-card:hover {
        box-shadow: 0 12px 32px rgba(99, 102, 241, 0.12);
        border-color: #a5b4fc;
        transform: translateY(-2px);
    }
    .tool-card.active {
        cursor: pointer;
    }
    .tool-card.active:hover {
        box-shadow: 0 12px 32px rgba(99, 102, 241, 0.18);
        border-color: #818cf8;
    }
    .tool-card.active:active {
        transform: scale(0.98);
        box-shadow: 0 4px 12px rgba(99, 102, 241, 0.20);
    }
    .tool-card.selected {
        border-color: #6366f1;
        background: rgba(99, 102, 241, 0.06);
        box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.15), 0 8px 24px rgba(99, 102, 241, 0.10);
    }
    .tool-icon {
        font-size: 2.2rem;
        margin-bottom: 0.8rem;
    }
    .tool-name {
        font-size: 1.25rem;
        font-weight: 700;
        color: #0f172a;
        margin-bottom: 0.4rem;
    }
    .tool-desc {
        font-size: 0.92rem;
        color: #64748b;
        line-height: 1.55;
        margin-bottom: 1rem;
    }
    .tool-status {
        display: inline-block;
        padding: 0.25rem 0.65rem;
        border-radius: 999px;
        font-size: 0.78rem;
        font-weight: 600;
    }
    .tool-status.ready {
        background: #dcfce7;
        color: #166534;
    }
    .tool-status.coming {
        background: #f1f5f9;
        color: #64748b;
    }
    .diffai-footer {
        margin-top: 3rem;
        font-size: 0.82rem;
        color: #94a3b8;
    }
    </style>

    <div class="diffai-home">
        <div class="diffai-badge">ACMML &middot; University of Rochester</div>
        <h1 class="diffai-title"><span>DiffAI</span></h1>
        <p class="diffai-subtitle">
            AI-powered tools for scientific diffraction data extraction, analysis, and validation.
        </p>
        <div class="diffai-grid">
            <div class="tool-card active" id="eraf4xrd-card" onclick="this.classList.toggle('selected'); document.getElementById('xrd-open-hint').style.display = this.classList.contains('selected') ? 'block' : 'none';">
                <div class="tool-icon">&#x1F4CA;</div>
                <div class="tool-name">XRD<span style="font-variant: small-caps;">reader</span></div>
                <div class="tool-desc">
                    Extract XRD data from scientific papers. Downloads PDFs, screens for XRD content,
                    detects figures, extracts metadata, and digitizes diffraction curves.
                </div>
                <span class="tool-status ready">Ready</span>
            </div>
            <div class="tool-card">
                <div class="tool-icon">&#x1F52C;</div>
                <div class="tool-name">More tools</div>
                <div class="tool-desc">
                    Additional diffraction analysis tools are in development. Check back soon.
                </div>
                <span class="tool-status coming">Coming soon</span>
            </div>
        </div>
        <div id="xrd-open-hint" style="display:none; margin-top:1.5rem; text-align:center; animation: fadeIn 0.3s ease;">
            <p style="color:#4338ca; font-weight:600; font-size:0.95rem; margin-bottom:0.5rem;">
                &#x2193; Click the button below to open ERAF4XRD &#x2193;
            </p>
        </div>
        <div class="diffai-footer">
            Advanced Computational Mechanics and Materials Laboratory
        </div>
    </div>
    <style>
    @keyframes fadeIn { from { opacity: 0; transform: translateY(-8px); } to { opacity: 1; transform: translateY(0); } }
    </style>
    """,
        unsafe_allow_html=True,
    )

    st.button(
        "Open ERAF4XRD",
        on_click=go_to_eraf4xrd,
        type="primary",
        use_container_width=False,
    )
    st.stop()

# =========================================================================
# ERAF4XRD PAGE (everything below is the existing ERAF4XRD UI)
# =========================================================================

init_state()

st.markdown(
    """
<div class="hero-shell">
    <div class="hero-kicker">X-ray diffraction data, simplified</div>
    <div class="hero-title">XRD<span style="font-variant: small-caps;">reader</span></div>
    <div class="hero-subtitle">
        <span class="hero-highlight">From scientific papers to structured data</span><br>
        Extract, organize, and analyze XRD data.
    </div>
    <div class="hero-pills">
        <div class="hero-pill">Downloads papers </div>
        <div class="hero-pill">Extracts XRD metadata and figures</div>
        <div class="hero-pill">Analyze & validate</div>
    </div>
    <a class="hero-anchor" href="#main-workspace">Get started ↓</a>
</div>
""",
    unsafe_allow_html=True,
)

st.markdown(
    """
<div id="main-workspace" class="workspace-shell">
    <div class="workspace-kicker">Workspace</div>
    <div class="workspace-title">Run framework and inspect outputs</div>
</div>
""",
    unsafe_allow_html=True,
)

if st.session_state.last_run_message:
    if st.session_state.last_run_message_type == "success":
        st.success(st.session_state.last_run_message)
    elif st.session_state.last_run_message_type == "error":
        st.error(st.session_state.last_run_message)
    elif st.session_state.last_run_message_type == "warning":
        st.warning(st.session_state.last_run_message)
    else:
        st.info(st.session_state.last_run_message)

model_options = {
    "gpt": [
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.2",
        "gpt-4.1",
        "gpt-4o",
        "gpt-4o-mini",
    ],
    "gemini": [
        "gemini-3.1-pro-preview",
        "gemini-3-flash",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
    ],
    "claude": [
        "claude-opus-4-7-20250415",
        "claude-sonnet-4-6-20250220",
        "claude-sonnet-4-5-20250929",
        "claude-sonnet-4",
        "claude-3-7-sonnet-latest",
    ],
    "grok": [
        "grok-4.20",
        "grok-4.20-non-reasoning",
        "grok-4-fast",
        "grok-3",
        "grok-3-mini",
    ],
}

sidebar_disabled = st.session_state.is_running

with st.sidebar:
    st.button("Back to DiffAI", on_click=go_home, use_container_width=True)
    st.markdown("---")
    st.header("Framework settings")

    st.subheader("Quick presets")
    preset_col1, preset_col2 = st.columns(2)
    if preset_col1.button(
        "Cu XRD", disabled=sidebar_disabled, width="stretch"
    ):
        apply_preset("Cu XRD full")
        st.rerun()
    if preset_col2.button(
        "Mo XRD", disabled=sidebar_disabled, width="stretch"
    ):
        apply_preset("Mo XRD full")
        st.rerun()

    if st.button(
        "Uploaded documents only", disabled=sidebar_disabled, width="stretch"
    ):
        apply_preset("Uploaded PDFs only")
        st.rerun()

    st.divider()

    with st.expander("Search parameters", expanded=True):
        if not st.session_state.get("elements_input", "").strip():
            st.session_state.elements_input = os.environ.get(
                "ELEMENTS", "Cu OR Copper"
            )
        if not st.session_state.get("technique_input", "").strip():
            st.session_state.technique_input = os.environ.get(
                "TECHNIQUE",
                '"x-ray diffraction" OR "x ray diffraction" OR XRD OR PXRD OR '
                '"powder x-ray diffraction" OR "powder diffraction"',
            )

        st.text_input(
            "ELEMENTS",
            key="elements_input",
            help="Example: Cu OR Copper",
            disabled=sidebar_disabled,
        )

        st.text_area(
            "TECHNIQUE",
            key="technique_input",
            height=80,
            disabled=sidebar_disabled,
        )

        search_keywords = f"(({st.session_state.elements_input}) AND ({st.session_state.technique_input}))"
        _sk_escaped = (
            search_keywords.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        st.markdown(
            f'<div style="background:#f8fafc; color:#111827; -webkit-text-fill-color:#111827; '
            f"padding:10px 12px; border-radius:8px; border:1px solid #e5e7eb; "
            f"font-family:monospace; font-size:0.85rem; white-space:pre-wrap; word-break:break-all; "
            f'user-select:all; cursor:text;">{_sk_escaped}</div>',
            unsafe_allow_html=True,
        )

    st.subheader("Run switches")

    # Step status pills
    phase_status_map = st.session_state.get("last_phase_status", {})

    def _pill(status):
        """Render a small colored status pill (HTML)."""
        if status in ("Done",):
            return "🟢"
        elif status in ("Running",):
            return "🟡"
        elif status in ("Skipped",):
            return "⚪"
        elif status in ("Idle", "--"):
            return "⚫"
        return "⚫"

    _ps = phase_status_map
    st.markdown(
        f"**Status:** "
        f"{_pill(_ps.get('download', '--'))} DL  "
        f"{_pill(_ps.get('phase0', '--'))} S0  "
        f"{_pill(_ps.get('phase1', '--'))} SI  "
        f"{_pill(_ps.get('phase2', '--'))} SII  "
        f"{_pill(_ps.get('cleaner', '--'))} CL  "
        f"{_pill(_ps.get('verifier', '--'))} SIII"
    )
    st.caption("🟢 Done  🟡 Running  ⚪ Skipped  ⚫ Idle")

    st.toggle(
        "Run the whole framework",
        key="run_full",
        disabled=sidebar_disabled,
        help="Runs every step end-to-end on your documents (download → screen → extract → clean → verify). "
        "Untick to choose steps individually.",
    )
    if not st.session_state.get("run_full", True):
        st.caption("Choose the steps to run:")
        st.checkbox(
            "Download documents", key="run_download", disabled=sidebar_disabled
        )
        st.checkbox(
            "Step 0: Screen documents",
            key="run_phase0",
            disabled=sidebar_disabled,
        )
        st.checkbox(
            "Step I: Raw JSON from documents",
            key="run_phase1",
            disabled=sidebar_disabled,
        )
        st.checkbox(
            "Step II: Enrich JSON", key="run_phase2", disabled=sidebar_disabled
        )
        st.checkbox("Clean JSON", key="run_clean", disabled=sidebar_disabled)
        st.checkbox(
            "Step III: Verify JSON",
            key="run_verify",
            disabled=sidebar_disabled,
        )
    else:
        st.caption("All steps will run · a *document* is a PDF file.")

    # ---- Existing documents vs. fresh download ----
    # If the folder already holds PDFs (uploaded, or fetched on a previous run),
    # ask whether to reuse them or download new ones — so uploads aren't ignored.
    existing_pdfs = list(PDF_DIR.rglob("*.pdf")) if PDF_DIR.exists() else []
    if existing_pdfs:
        st.session_state.setdefault(
            "doc_source_choice", "Use existing documents in the folder"
        )
        st.radio(
            f"Found {len(existing_pdfs)} document(s) already in the folder — "
            "download new ones or use these?",
            ["Use existing documents in the folder", "Download new documents"],
            key="doc_source_choice",
            disabled=sidebar_disabled,
        )
        st.session_state.use_existing_docs = (
            st.session_state.doc_source_choice.startswith("Use existing")
        )
    else:
        st.session_state.use_existing_docs = False

    with st.expander("Sources & downloads", expanded=False):
        src_col1, src_col2 = st.columns(2)
        with src_col1:
            st.checkbox("ArXiv", key="use_arxiv", disabled=sidebar_disabled)
            st.checkbox(
                "Springer", key="use_springer", disabled=sidebar_disabled
            )
        with src_col2:
            st.checkbox(
                "Elsevier", key="use_elsevier", disabled=sidebar_disabled
            )
            st.checkbox(
                "CrossRef", key="use_crossref", disabled=sidebar_disabled
            )

        st.number_input(
            "TARGET DOWNLOADS",
            min_value=1,
            max_value=500,
            step=1,
            key="target_downloads",
            disabled=sidebar_disabled,
        )

    with st.expander("LLM provider & model", expanded=False):
        provider_options = ["gpt", "gemini", "claude", "grok"]
        if st.session_state.provider not in provider_options:
            st.session_state.provider = provider_options[0]
        st.selectbox(
            "LLM PROVIDER",
            provider_options,
            key="provider",
            disabled=sidebar_disabled,
        )

        valid_models = model_options[st.session_state.provider]
        if st.session_state.model not in valid_models:
            st.session_state.model = valid_models[0]

        st.selectbox(
            "LLM MODEL",
            valid_models,
            key="model",
            disabled=sidebar_disabled,
        )

    with st.expander("LLM per step", expanded=False):
        phase_model_selector(
            "Step 0: Screener",
            "phase0_provider",
            "phase0_model",
            model_options,
            disabled=sidebar_disabled,
        )
        phase_model_selector(
            "Step I",
            "phase1_provider",
            "phase1_model",
            model_options,
            disabled=sidebar_disabled,
        )
        phase_model_selector(
            "Step II",
            "phase2_provider",
            "phase2_model",
            model_options,
            disabled=sidebar_disabled,
        )
        phase_model_selector(
            "Step III: Verifier",
            "verify_provider",
            "verify_model",
            model_options,
            disabled=sidebar_disabled,
        )

    if SHOW_XRD_DIGITIZER:
        with st.expander("Digitizer", expanded=False):
            st.session_state.run_digitizer = st.checkbox(
                "Run Digitizer",
                value=st.session_state.run_digitizer,
                disabled=sidebar_disabled,
            )
            st.session_state.digitizer_algorithm = st.radio(
                "Algorithm",
                options=["topmost", "tracking"],
                index=(
                    0
                    if st.session_state.digitizer_algorithm == "topmost"
                    else 1
                ),
                disabled=sidebar_disabled,
                help="**Topmost**: Best for sharp XRD peaks. **Tracking**: Best for smooth/overlapping curves.",
            )

    with st.expander("API keys", expanded=False):
        st.text_input(
            "OPENAI",
            type="password",
            key="openai_api_key",
            disabled=sidebar_disabled,
        )
        st.text_input(
            "GEMINI",
            type="password",
            key="gemini_api_key",
            disabled=sidebar_disabled,
        )
        st.text_input(
            "ANTHROPIC",
            type="password",
            key="anthropic_api_key",
            disabled=sidebar_disabled,
        )
        st.text_input(
            "X", type="password", key="xai_api_key", disabled=sidebar_disabled
        )
        st.text_input(
            "SPRINGER",
            type="password",
            key="springer_api_key",
            disabled=sidebar_disabled,
        )
        st.text_input(
            "ELSEVIER",
            type="password",
            key="elsevier_api_key",
            disabled=sidebar_disabled,
        )
        st.text_input(
            "UNPAYWALL email",
            key="unpaywall_email",
            disabled=sidebar_disabled,
            help="Any real email address -- required by the Unpaywall API "
            "when downloading from CrossRef.",
        )
        st.text_input(
            "Materials Project",
            type="password",
            key="mp_api_key",
            disabled=sidebar_disabled,
        )

tab_run, tab_browse, tab_results = st.tabs(
    ["Run framework", "Browse files", "JSON outputs"]
)

with tab_run:
    with st.container(border=True):
        st.subheader("Upload documents")
        uploaded_files = st.file_uploader(
            "Upload your local documents (PDF)",
            type=["pdf"],
            accept_multiple_files=True,
        )

        if st.button(
            "Save uploaded documents", disabled=st.session_state.is_running
        ):
            if uploaded_files:
                saved = save_uploaded_pdfs(uploaded_files)
                st.toast(
                    f"Saved {len(saved)} document(s) to downloaded_pdfs/Uploaded"
                )
            else:
                st.info("No documents selected.")

    st.markdown("")
    with st.container(border=True):
        st.subheader("Run framework")

    is_running_now = st.session_state.get("is_running", False)

    run_clicked = st.button(
        "▶  Run ERAF4XRD",
        type="primary",
        disabled=is_running_now,
        use_container_width=True,
    )

    stop_col, reset_col = st.columns(2)
    with stop_col:
        stop_clicked = st.button(
            "Stop",
            disabled=not is_running_now,
            use_container_width=True,
        )
    with reset_col:
        reset_clicked = st.button(
            "Reset",
            disabled=is_running_now,
            use_container_width=True,
        )

    if stop_clicked:
        ok, msg = stop_running_process()
        st.session_state.last_run_message = msg
        st.session_state.last_run_message_type = "warning" if ok else "error"
        st.rerun()

    if reset_clicked:
        reset_form()
        st.session_state.last_output = ""
        st.session_state.last_status_summary = ""
        st.session_state.last_return_code = None
        st.session_state.last_run_message = None
        st.session_state.last_run_message_type = "info"
        st.session_state.last_elapsed_seconds = None
        st.session_state.last_phase_status = {}
        st.session_state.run_started_at = None
        st.rerun()

    if st.session_state.is_running:
        st.warning(
            "The framework is currently running. Press 'Stop' to terminate."
        )

    if run_clicked:
        st.session_state.last_run_message = None
        st.session_state.last_run_message_type = "info"
        st.session_state.last_output = ""
        st.session_state.last_status_summary = ""
        st.session_state.last_return_code = None
        st.session_state.last_elapsed_seconds = None
        st.session_state.last_phase_status = {}
        st.session_state.run_started_at = time.time()
        st.session_state.is_running = True
        st.rerun()

    if st.session_state.is_running and st.session_state.process_pid is None:
        try:
            env_overrides = {
                "ELEMENTS": st.session_state.elements_input,
                "TECHNIQUE": st.session_state.technique_input,
                "SEARCH_KEYWORDS": search_keywords,
                "RUN_DOWNLOAD": str(
                    (
                        st.session_state.get("run_full", True)
                        or st.session_state.run_download
                    )
                    and not st.session_state.get("use_existing_docs", False)
                ),
                "RUN_PHASE0_FILTER": str(
                    st.session_state.get("run_full", True)
                    or st.session_state.run_phase0
                ),
                "RUN_PHASE1": str(
                    st.session_state.get("run_full", True)
                    or st.session_state.run_phase1
                ),
                "RUN_PHASE2": str(
                    st.session_state.get("run_full", True)
                    or st.session_state.run_phase2
                ),
                "RUN_JSON_CLEAN_AGENT": str(
                    st.session_state.get("run_full", True)
                    or st.session_state.run_clean
                ),
                "RUN_JSON_VERIFY_AGENT": str(
                    st.session_state.get("run_full", True)
                    or st.session_state.run_verify
                ),
                "RUN_DIGITIZER": str(st.session_state.run_digitizer),
                "DIGITIZER_ALGORITHM": st.session_state.digitizer_algorithm,
                "DIGITIZER_MODEL": st.session_state.digitizer_model,
                "USE_ARXIV": str(int(st.session_state.use_arxiv)),
                "USE_SPRINGER": str(int(st.session_state.use_springer)),
                "USE_ELSEVIER": str(int(st.session_state.use_elsevier)),
                "USE_CROSSREF": str(int(st.session_state.use_crossref)),
                "TARGET_DOWNLOADS": str(
                    int(st.session_state.target_downloads)
                ),
                "PROVIDER": st.session_state.provider,
                "MODEL": st.session_state.model,
                "OPENAI_MODEL": st.session_state.model,
                "PHASE0_PROVIDER": st.session_state.phase0_provider,
                "PHASE0_MODEL": st.session_state.phase0_model,
                "PHASE1_PROVIDER": st.session_state.phase1_provider,
                "PHASE1_MODEL": st.session_state.phase1_model,
                "PHASE2_PROVIDER": st.session_state.phase2_provider,
                "PHASE2_MODEL": st.session_state.phase2_model,
                "VERIFY_PROVIDER": st.session_state.verify_provider,
                "VERIFY_MODEL": st.session_state.verify_model,
                "OPENAI_API_KEY": st.session_state.openai_api_key,
                "GEMINI_API_KEY": st.session_state.gemini_api_key,
                "ANTHROPIC_API_KEY": st.session_state.anthropic_api_key,
                "XAI_API_KEY": st.session_state.xai_api_key,
                "SPRINGER_API_KEY": st.session_state.springer_api_key,
                "ELSEVIER_API_KEY": st.session_state.elsevier_api_key,
                "UNPAYWALL_EMAIL": st.session_state.unpaywall_email,
                "MP_API_KEY": st.session_state.mp_api_key,
            }

            code, output, summary, elapsed, phase_status = run_pipeline(
                env_overrides
            )
            st.session_state.last_return_code = code
            st.session_state.last_output = output
            st.session_state.last_status_summary = summary
            st.session_state.last_elapsed_seconds = elapsed
            st.session_state.last_phase_status = phase_status

            if code == 0:
                st.session_state.last_run_message = (
                    f"Framework completed in {elapsed:.1f} sec."
                )
                st.session_state.last_run_message_type = "success"
            else:
                st.session_state.last_run_message = f"Framework failed with exit code {code} after {elapsed:.1f} sec."
                st.session_state.last_run_message_type = "error"
        finally:
            st.session_state.is_running = False
            st.session_state.process_pid = None
            st.rerun()

    st.divider()

    with st.container(border=True):
        st.subheader("Results")

        if st.session_state.last_return_code == 0:
            json_count = len(list(OUT_DIR.rglob("*.json")))
            result_cols = st.columns(3)
            result_cols[0].metric("Last run", "Success")
            result_cols[1].metric("JSON outputs", str(json_count))
            if st.session_state.last_elapsed_seconds is not None:
                result_cols[2].metric(
                    "Elapsed",
                    f"{st.session_state.last_elapsed_seconds:.1f} sec",
                )
            else:
                result_cols[2].metric("Elapsed", "--")
        elif st.session_state.last_return_code is not None:
            result_cols = st.columns(3)
            result_cols[0].metric("Last run", "Failed")
            result_cols[1].metric("JSON outputs", "--")
            if st.session_state.last_elapsed_seconds is not None:
                result_cols[2].metric(
                    "Elapsed",
                    f"{st.session_state.last_elapsed_seconds:.1f} sec",
                )
            else:
                result_cols[2].metric("Elapsed", "--")
        else:
            result_cols = st.columns(3)
            result_cols[0].metric("Last run", "--")
            result_cols[1].metric("JSON outputs", "--")
            result_cols[2].metric("Elapsed", "--")

        if contains_error_text(
            st.session_state.last_output, st.session_state.last_return_code
        ):
            _err_count = st.session_state.last_output.lower().count(
                "traceback"
            )
            _fail_count = st.session_state.last_output.lower().count("[fail]")
            st.error(
                f"Errors detected: {_err_count} traceback(s), {_fail_count} failure(s). "
                f"Check details below."
            )

        st.subheader("Last step status")
        c1, c2, c3, c4, c5 = st.columns(5)
        phase_status = st.session_state.last_phase_status or {}
        c1.metric("Download", phase_status.get("download", "--"))
        c2.metric("Step I", phase_status.get("phase1", "--"))
        c3.metric("Step II", phase_status.get("phase2", "--"))
        c4.metric("Cleaner", phase_status.get("cleaner", "--"))
        c5.metric("Verifier", phase_status.get("verifier", "--"))

        st.subheader("Last run summary")
        _summary_text = st.session_state.last_status_summary or "--"
        _summary_escaped = (
            _summary_text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        st.markdown(
            f'<div style="background:#f8fafc; color:#111827; -webkit-text-fill-color:#111827; '
            f"padding:10px 12px; border-radius:8px; border:1px solid #e5e7eb; "
            f"font-family:monospace; font-size:0.85rem; white-space:pre-wrap; "
            f'user-select:all; cursor:text;">{_summary_escaped}</div>',
            unsafe_allow_html=True,
        )

        if st.session_state.last_output:
            with st.expander("Last run details", expanded=False):
                # Usage table as styled HTML (st.dataframe ignores parent CSS)
                _usage_rows = _extract_usage_table(
                    st.session_state.last_output
                )
                if _usage_rows:
                    st.caption("LLM Usage Report")
                    _hdr = "".join(
                        f'<th style="padding:8px 12px;text-align:right;font-weight:600;'
                        f'border-bottom:2px solid #cbd5e1;color:#374151;background:#f1f5f9;">{k}</th>'
                        for k in _usage_rows[0].keys()
                    )
                    _body = ""
                    for row in _usage_rows:
                        is_total = str(row.get("Phase", "")).upper() == "TOTAL"
                        _rw = (
                            "font-weight:700;background:#f8fafc;"
                            if is_total
                            else ""
                        )
                        _cells = "".join(
                            f'<td style="padding:7px 12px;text-align:right;'
                            f'border-bottom:1px solid #e5e7eb;color:#111827;{_rw}">{v}</td>'
                            for v in row.values()
                        )
                        _bdr = (
                            "border-top:2px solid #94a3b8;" if is_total else ""
                        )
                        _body += f'<tr style="{_bdr}{_rw}">{_cells}</tr>'
                    st.markdown(
                        f'<div style="overflow-x:auto;border-radius:8px;border:1px solid #e5e7eb;">'
                        f'<table style="width:100%;border-collapse:collapse;font-family:monospace;'
                        f'font-size:0.84rem;background:#ffffff;color:#111827;">'
                        f"<thead><tr>{_hdr}</tr></thead><tbody>{_body}</tbody></table></div>",
                        unsafe_allow_html=True,
                    )

                # Log text (without the usage table section)
                _clean_output = _strip_ansi(st.session_state.last_output)
                # Remove the usage report block from the log text
                _clean_output = re.sub(
                    r"={10,}.*?LLM USAGE REPORT.*?Pipeline wall-clock time:.*?\n",
                    "",
                    _clean_output,
                    flags=re.DOTALL,
                )
                _escaped = html_escape(_clean_output.strip())
                if _escaped:
                    st.caption("Run log")
                    st.markdown(
                        f'<div style="max-height:300px; overflow-y:auto; background:#ffffff; '
                        f"color:#111827; padding:12px; border-radius:8px; font-family:monospace; "
                        f"font-size:0.82rem; white-space:pre; overflow-x:auto; "
                        f'border:1px solid #e5e7eb;">'
                        f"<style>.run-log-box * {{ color: #111827 !important; font-family: monospace !important; "
                        f"font-size: 0.82rem !important; background: transparent !important; }}</style>"
                        f'<div class="run-log-box">{_escaped}</div></div>',
                        unsafe_allow_html=True,
                    )
        else:
            with st.expander("Last run details", expanded=False):
                st.code("--", language="text")


with tab_browse:
    browse_left, browse_right = st.columns(2)
    with browse_left:
        with st.container(border=True):
            st.subheader("Recent documents")
            pdfs = newest_files(PDF_DIR, "*.pdf", limit=25)
            if pdfs:
                for p in pdfs[:8]:
                    rel_path = str(p.relative_to(BASE_DIR))
                    st.markdown(
                        f'<div style="padding:6px 0;border-bottom:1px solid #e5e7eb;">'
                        f'<div style="font-weight:600;font-size:0.9rem;">{p.name}</div>'
                        f'<div style="font-size:0.8rem;color:#6b7280;word-break:break-all;">{rel_path}</div>'
                        f"</div>",
                        unsafe_allow_html=True,
                    )
            else:
                st.info("No documents downloaded yet.")

        with st.container(border=True):
            st.subheader("Quick library stats")
            pdf_count2 = (
                len(list(PDF_DIR.rglob("*.pdf"))) if PDF_DIR.exists() else 0
            )
            json_count2 = (
                len(
                    [
                        f
                        for f in OUT_DIR.rglob("*.json")
                        if "_agent_cache_" not in f.name
                    ]
                )
                if OUT_DIR.exists()
                else 0
            )
            log_count2 = (
                len(list(LOG_DIR.rglob("*.txt"))) if LOG_DIR.exists() else 0
            )
            s1b, s2b, s3b = st.columns(3)
            s1b.metric("PDFs", str(pdf_count2))
            s2b.metric("JSONs", str(json_count2))
            s3b.metric("Logs", str(log_count2))

    with browse_right:
        with st.container(border=True):
            st.subheader("Recent logs")
            logs2 = newest_files(LOG_DIR, "*.txt", limit=10)
            if logs2:
                selected_log2 = st.selectbox(
                    "Choose a log",
                    logs2,
                    format_func=lambda p: p.name,
                    key="browse_log_select",
                )
                _lt = html_escape(
                    _strip_ansi(
                        selected_log2.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    )
                )
                st.markdown(
                    f'<div style="max-height:400px; overflow-y:auto; background:#ffffff; '
                    f"color:#111827; padding:12px; border-radius:8px; font-family:monospace; "
                    f"font-size:0.82rem; white-space:pre; overflow-x:auto; "
                    f'border:1px solid #e5e7eb;">'
                    f"{_lt}</div>",
                    unsafe_allow_html=True,
                )
            else:
                st.info("No logs yet.")

with tab_results:
    json_files = [
        f
        for f in newest_files(OUT_DIR, "*.json", limit=200)
        if not any(part.startswith("_agent_cache") for part in f.parts)
        and any(
            tag in f.name
            for tag in [
                "__phase1_raw",
                "__phase2_enriched",
                "__phase2_clean",
                "__phase3",
            ]
        )
    ]

    if json_files:
        compare_selection = st.multiselect(
            "Compare multiple JSON outputs",
            json_files,
            default=[],
            format_func=lambda p: str(p.relative_to(BASE_DIR)),
        )

        if compare_selection:
            compare_rows = [
                summarize_json_for_compare(p) for p in compare_selection
            ]
            st.caption("Quick comparison")
            if compare_rows:
                _hdr = "".join(
                    f'<th style="padding:8px 12px;text-align:left;font-weight:600;'
                    f'border-bottom:2px solid #cbd5e1;color:#374151;background:#f1f5f9;">{k}</th>'
                    for k in compare_rows[0].keys()
                )
                _body = ""
                for row in compare_rows:
                    _cells = "".join(
                        f'<td style="padding:7px 12px;text-align:left;'
                        f'border-bottom:1px solid #e5e7eb;color:#111827;">{v}</td>'
                        for v in row.values()
                    )
                    _body += f"<tr>{_cells}</tr>"
                st.markdown(
                    f'<div style="overflow-x:auto;border-radius:8px;border:1px solid #e5e7eb;">'
                    f'<table style="width:100%;border-collapse:collapse;font-family:monospace;'
                    f'font-size:0.84rem;background:#ffffff;color:#111827;">'
                    f"<thead><tr>{_hdr}</tr></thead><tbody>{_body}</tbody></table></div>",
                    unsafe_allow_html=True,
                )

            # Side-by-side JSON diff (when exactly 2 selected)
            if len(compare_selection) == 2:
                with st.expander("🔍 Side-by-side diff", expanded=False):
                    try:
                        json_a = json.loads(
                            compare_selection[0].read_text(
                                encoding="utf-8", errors="replace"
                            )
                        )
                        json_b = json.loads(
                            compare_selection[1].read_text(
                                encoding="utf-8", errors="replace"
                            )
                        )
                        text_a = json.dumps(
                            json_a, indent=2, ensure_ascii=False
                        ).splitlines()
                        text_b = json.dumps(
                            json_b, indent=2, ensure_ascii=False
                        ).splitlines()

                        differ = difflib.HtmlDiff(wrapcolumn=80)
                        diff_html = differ.make_table(
                            text_a,
                            text_b,
                            fromdesc=compare_selection[0].name,
                            todesc=compare_selection[1].name,
                            context=True,
                            numlines=3,
                        )

                        # Style the diff table for readability
                        styled_diff = f"""
                        <div style="overflow-x:auto; max-height:600px; overflow-y:auto;
                                    border:1px solid #cbd5e1; border-radius:10px; padding:4px;
                                    background:#ffffff;">
                        <style>
                            .diff td {{ font-size: 0.75rem; font-family: monospace; padding: 2px 6px; }}
                            .diff_header {{ background: #e0e7ff; color: #1e3a8a; font-weight: 600; }}
                            .diff_next {{ background: #f8fafc; }}
                            .diff_add {{ background: #dcfce7; }}
                            .diff_chg {{ background: #fef9c3; }}
                            .diff_sub {{ background: #fee2e2; }}
                            .diff th {{ font-size: 0.72rem; background: #f1f5f9; color: #334155;
                                         padding: 4px 8px; text-align: left; }}
                        </style>
                        {diff_html}
                        </div>
                        """
                        components.html(
                            styled_diff, height=620, scrolling=True
                        )
                    except Exception as diff_err:
                        st.warning(f"Could not generate diff: {diff_err}")

        @st.cache_data(ttl=30)
        def _json_label(p_str):
            p = Path(p_str)
            label = str(p.relative_to(BASE_DIR))
            try:
                _d = json.loads(
                    p.read_text(encoding="utf-8", errors="replace")
                )
                for _f in _d.get("figures", []):
                    if isinstance(_f, dict) and _f.get("digitized"):
                        return f"📊 {label}"
            except Exception:
                pass
            return label

        # --- Two-level selector: paper → variant ---
        _phase_tags = [
            "__phase3_validated_FINAL",
            "__phase3_validation_log",
            "__phase2_clean",
            "__phase2_enriched",
            "__phase1_raw",
        ]

        def _paper_stem(p):
            """Strip phase tag to get the base paper name."""
            name = p.stem
            for tag in _phase_tags:
                if tag in name:
                    return name.split(tag)[0]
            return name

        def _variant_label(p):
            """Short label for the phase variant."""
            name = p.name
            if "__phase3_validated_FINAL" in name:
                return "Final (Step III)"
            if "__phase3_validation_log" in name:
                return "Validation log"
            if "__phase2_clean" in name:
                return "Clean (Step II)"
            if "__phase2_enriched" in name:
                return "Enriched (Step II)"
            if "__phase1_raw" in name:
                return "Raw (Step I)"
            return name

        # Group files by paper
        from collections import OrderedDict

        _paper_groups = OrderedDict()
        for f in json_files:
            stem = _paper_stem(f)
            _paper_groups.setdefault(stem, []).append(f)

        _paper_names = list(_paper_groups.keys())

        sel_col1, sel_col2 = st.columns([2, 1])
        with sel_col1:
            selected_paper = st.selectbox(
                "Select paper",
                _paper_names,
                format_func=lambda s: s.replace("_", " ")[:80],
            )
        with sel_col2:
            variants = _paper_groups[selected_paper]
            # Sort: Final first, then clean, enriched, raw
            variants.sort(
                key=lambda p: next(
                    (i for i, tag in enumerate(_phase_tags) if tag in p.name),
                    99,
                )
            )
            selected_json = st.selectbox(
                "Select version",
                variants,
                format_func=_variant_label,
            )

        selected_json_text = selected_json.read_text(
            encoding="utf-8", errors="replace"
        )

        viewer_col1, viewer_col2, viewer_col3 = st.columns([2.2, 1, 1])
        with viewer_col1:
            st.caption(f"Interactive JSON viewer: {selected_json.name}")
        with viewer_col2:
            st.caption(f"Size: {selected_json.stat().st_size/1024:.1f} KB")
        with viewer_col3:
            with open(selected_json, "rb") as fh:
                st.download_button(
                    "Download JSON",
                    data=fh,
                    file_name=selected_json.name,
                    mime="application/json",
                    width="stretch",
                )

        parsed_json = None
        try:
            parsed_json = json.loads(selected_json_text)
            show_json_editor(parsed_json, height=700)
        except Exception:
            st.warning(
                "Could not load the interactive JSON viewer. Showing plain JSON instead."
            )
            st.code(selected_json_text, language="json")

        if parsed_json is not None:
            # Sticky paper title bar
            _ref = extract_paper_reference(parsed_json, selected_json)
            _title = _ref.get("title", "") or selected_json.name
            _doi = _ref.get("doi", "")
            _doi_html = (
                f'<span class="paper-doi">{_doi}</span>' if _doi else ""
            )
            st.markdown(
                f'<div class="sticky-paper-bar">'
                f'<div class="paper-title">📄 {_title}</div>'
                f"{_doi_html}"
                f"</div>",
                unsafe_allow_html=True,
            )

            st.divider()
            show_reference_and_usage_notice(parsed_json, selected_json)

            st.divider()
            render_paper_summary_card(parsed_json, selected_json)

            st.divider()
            st.subheader("XRD plots from this file")

            xrd_images = get_xrd_images_for_json(selected_json, parsed_json)
            if xrd_images:
                cols = st.columns(3)
                for i, rec in enumerate(xrd_images):
                    with cols[i % 3]:
                        st.image(
                            str(rec["path"]),
                            caption=rec["caption"],
                            width="stretch",
                        )
            else:
                st.info(
                    "No XRD images were found for this JSON. "
                    "If you selected a clean or verified JSON, the image paths may only exist in the phase1/phase2 raw files."
                )

            st.divider()
            render_paper_ai_chat(
                parsed_json, selected_json, key_prefix="left_json_panel"
            )

            if SHOW_XRD_DIGITIZER:
                # ---- Digitizer section ----
                st.divider()
                st.subheader("📊 XRD Plot Digitizer")

                # Discover XRD figures from Step I output.
                # The digitizer module is optional in this build; degrade gracefully if absent.
                try:
                    from diffai.eraf4xrd.digitizer import (
                        build_digitized_output,
                        digitize_figure,
                        discover_xrd_figures,
                        find_validated_json_for_figure,
                    )
                except ModuleNotFoundError:
                    st.info(
                        "📊 The XRD Plot Digitizer is not included in this build."
                    )

                    def discover_xrd_figures(*a, **k):
                        return []

                    def _digitizer_unavailable(*a, **k):
                        st.error(
                            "The digitizer module is not available in this build."
                        )
                        return None

                    digitize_figure = build_digitized_output = (
                        find_validated_json_for_figure
                    ) = _digitizer_unavailable
                discovered = discover_xrd_figures(str(OUT_DIR))

                # Build list of digitizable images: auto-discovered + manual upload
                digitize_options = {}

                if discovered:
                    for d in discovered:
                        label = f"{d['pdf_name']} — Fig {d['figure_number']} (p{d['page']})"
                        digitize_options[label] = {
                            "crop_path": d["crop_path"],
                            "figure_number": d.get("figure_number"),
                            "pdf_name": d.get("pdf_name", ""),
                            "source": "auto",
                        }

                # Manual upload option
                uploaded_plots = st.file_uploader(
                    "Or upload XRD plot image(s) manually",
                    type=["png", "jpg", "jpeg"],
                    key="digitizer_upload",
                    accept_multiple_files=True,
                )
                if uploaded_plots:
                    upload_dir = Path("outputs/digitized/uploads")
                    upload_dir.mkdir(parents=True, exist_ok=True)
                    for uploaded_plot in uploaded_plots:
                        upload_path = upload_dir / uploaded_plot.name
                        upload_path.write_bytes(uploaded_plot.getbuffer())
                        digitize_options[
                            f"📎 Uploaded: {uploaded_plot.name}"
                        ] = {
                            "crop_path": str(upload_path),
                            "figure_number": None,
                            "pdf_name": uploaded_plot.name,
                            "source": "upload",
                        }

                if not digitize_options:
                    st.info(
                        "No XRD figures found. Run Steps 0–I first, or upload an image above."
                    )
                else:
                    col_sel, col_algo = st.columns([2, 1])

                    with col_sel:
                        selected_fig_label = st.selectbox(
                            "Select figure to digitize",
                            options=list(digitize_options.keys()),
                            key="digitizer_fig_select",
                        )

                    with col_algo:
                        digi_algorithm = st.radio(
                            "Algorithm",
                            options=["topmost", "tracking"],
                            index=0,
                            key="digitizer_algo_radio",
                            help="**Topmost**: Best for sharp XRD peaks. **Tracking**: Best for smooth/overlapping curves.",
                            horizontal=True,
                        )

                    # Selected figure info
                    fig_info = digitize_options[selected_fig_label]
                    crop_path = fig_info["crop_path"]
                    stem = Path(crop_path).stem
                    output_dir = str(OUT_DIR / "digitized")
                    state_key = f"digitization_result_{stem}"
                    cache_json_path = str(
                        Path(output_dir) / f"{stem}__digi_cache.json"
                    )

                    # Restore from disk cache if session lost it
                    if (
                        state_key not in st.session_state
                        and Path(cache_json_path).exists()
                    ):
                        try:
                            cached = json.loads(
                                Path(cache_json_path).read_text(
                                    encoding="utf-8"
                                )
                            )
                            st.session_state[state_key] = cached
                        except Exception:
                            pass

                    # Show raw image only if this figure has not been digitized yet
                    if (
                        Path(crop_path).exists()
                        and state_key not in st.session_state
                    ):
                        st.image(
                            str(crop_path),
                            caption=f"Selected: {selected_fig_label}",
                            width=700,
                        )

                    # Digitize button
                    digi_model = os.environ.get(
                        "DIGITIZER_MODEL", os.environ.get("MODEL", "gpt-4o")
                    )
                    palette_path = str(BASE_DIR / "palette.json")
                    if not Path(palette_path).exists():
                        st.warning(
                            "⚠️ palette.json not found in project directory. Digitization will fail. Place palette.json next to app.py."
                        )
                        if not (
                            st.session_state.get("openai_api_key", "")
                            or os.environ.get("OPENAI_API_KEY", "")
                        ).strip():
                            st.warning(
                                "⚠️ No OpenAI API key set. Digitizer requires it for vision calls."
                            )

                    if st.button(
                        "🔬 Digitize Selected Figure",
                        key="btn_digitize",
                        type="primary",
                        width="stretch",
                    ):
                        if selected_fig_label:
                            fig_info = digitize_options[selected_fig_label]
                            crop_path = fig_info["crop_path"]
                            stem = Path(crop_path).stem
                            output_dir = str(OUT_DIR / "digitized")
                            state_key = f"digitization_result_{stem}"

                            # Clean stale files from previous runs
                            _out_p = Path(output_dir)
                            if _out_p.exists():
                                for _old in _out_p.glob(f"{stem}_*"):
                                    try:
                                        _old.unlink()
                                    except Exception:
                                        pass
                                _old_cache = (
                                    _out_p / f"{stem}__digi_cache.json"
                                )
                                if _old_cache.exists():
                                    try:
                                        _old_cache.unlink()
                                    except Exception:
                                        pass
                            st.session_state.pop(state_key, None)

                            with st.spinner(
                                f"Digitizing with '{digi_algorithm}' algorithm..."
                            ):
                                try:
                                    result = digitize_figure(
                                        image_path=crop_path,
                                        palette_path=palette_path,
                                        algorithm=digi_algorithm,
                                        output_dir=output_dir,
                                        model=digi_model,
                                    )

                                    n_curves = len(result.get("results", []))
                                    # Count LLM calls from digitizer output files
                                    _digi_files = result.get(
                                        "output_files", {}
                                    )
                                    _n_rounds = len(
                                        list(
                                            Path(output_dir).glob(
                                                f"{stem}_round*_residual.png"
                                            )
                                        )
                                    )
                                    st.toast(
                                        f"✅ Digitized {n_curves} curve(s) using '{digi_algorithm}' "
                                        f"({_n_rounds} round(s))"
                                    )

                                    # Build merged JSON with validated metadata
                                    validated_path = (
                                        find_validated_json_for_figure(
                                            str(OUT_DIR),
                                            fig_info.get("pdf_name", ""),
                                        )
                                    )
                                    out_json_path = str(
                                        Path(output_dir)
                                        / f"{stem}__digitized.json"
                                    )
                                    build_digitized_output(
                                        validated_json_path=validated_path,
                                        digitized_results=result,
                                        figure_number=fig_info.get(
                                            "figure_number"
                                        ),
                                        output_path=out_json_path,
                                    )

                                    # ---- Build all three images ----
                                    import numpy as np

                                    crop_img_bgr = cv2.imread(crop_path)
                                    h_img, w_img = (
                                        crop_img_bgr.shape[:2]
                                        if crop_img_bgr is not None
                                        else (400, 600)
                                    )

                                    # (1) Original image
                                    raw_path = str(crop_path)

                                    # (2) Transparent points-only image (pixel-based, preserves peaks)
                                    overlay_from_digitizer = result.get(
                                        "output_files", {}
                                    ).get("overlay", "")
                                    transparent_path = str(
                                        Path(output_dir)
                                        / f"{stem}_points_only.png"
                                    )
                                    transparent = np.zeros(
                                        (h_img, w_img, 4), dtype=np.uint8
                                    )
                                    if (
                                        overlay_from_digitizer
                                        and Path(
                                            overlay_from_digitizer
                                        ).exists()
                                        and crop_img_bgr is not None
                                    ):
                                        overlay_bgr = cv2.imread(
                                            overlay_from_digitizer
                                        )
                                        if overlay_bgr is not None:
                                            if (
                                                overlay_bgr.shape[:2]
                                                != crop_img_bgr.shape[:2]
                                            ):
                                                overlay_bgr = cv2.resize(
                                                    overlay_bgr,
                                                    (w_img, h_img),
                                                    interpolation=cv2.INTER_AREA,
                                                )
                                            diff = cv2.absdiff(
                                                overlay_bgr, crop_img_bgr
                                            )
                                            diff_gray = cv2.cvtColor(
                                                diff, cv2.COLOR_BGR2GRAY
                                            )
                                            _, mask = cv2.threshold(
                                                diff_gray,
                                                25,
                                                255,
                                                cv2.THRESH_BINARY,
                                            )
                                            transparent[:, :, :3] = overlay_bgr
                                            transparent[:, :, 3] = mask
                                    else:
                                        for curve in result.get("results", []):
                                            rgb = curve.get(
                                                "target_rgb", [255, 0, 0]
                                            )
                                            color_bgra = (
                                                int(rgb[2]),
                                                int(rgb[1]),
                                                int(rgb[0]),
                                                255,
                                            )
                                            for col, row in curve.get(
                                                "picked", []
                                            ):
                                                col = int(round(col))
                                                row = int(round(row))
                                                if (
                                                    0 <= col < w_img
                                                    and 0 <= row < h_img
                                                ):
                                                    cv2.circle(
                                                        transparent,
                                                        (col, row),
                                                        4,
                                                        color_bgra,
                                                        -1,
                                                    )
                                    cv2.imwrite(transparent_path, transparent)

                                    # (2b) Matplotlib plot with axes (separate file)
                                    import matplotlib

                                    matplotlib.use("Agg")
                                    import matplotlib.pyplot as plt

                                    mpl_path = str(
                                        Path(output_dir)
                                        / f"{stem}_mpl_axes.png"
                                    )
                                    fig_pts, ax_pts = plt.subplots(
                                        figsize=(8, 5)
                                    )
                                    for curve in result.get("results", []):
                                        rgb = curve.get(
                                            "target_rgb", [0, 0, 0]
                                        )
                                        r, g, b = rgb[0], rgb[1], rgb[2]
                                        if r < 30 and g < 30 and b < 30:
                                            mpl_color = (0.15, 0.15, 0.15)
                                        else:
                                            mpl_color = (
                                                r / 255.0,
                                                g / 255.0,
                                                b / 255.0,
                                            )
                                        x_vals = curve.get("x_vals", [])
                                        y_vals = curve.get("y_vals", [])
                                        label = curve.get("label", "?")
                                        if len(x_vals) > 0 and len(y_vals) > 0:
                                            ax_pts.scatter(
                                                x_vals,
                                                y_vals,
                                                color=mpl_color,
                                                s=6,
                                                zorder=5,
                                                label=label,
                                            )
                                    ax_pts.set_xlabel("2θ (deg)")
                                    ax_pts.set_ylabel("Intensity (a.u.)")
                                    ax_pts.legend(fontsize=8)
                                    ax_pts.set_title("Digitized XRD Curves")
                                    fig_pts.tight_layout()
                                    fig_pts.savefig(
                                        mpl_path, dpi=150, bbox_inches="tight"
                                    )
                                    plt.close(fig_pts)

                                    # (3) Use digitizer's own overlay if available (CORRECT alignment)
                                    overlay_from_digitizer = result.get(
                                        "output_files", {}
                                    ).get("overlay", "")

                                    if (
                                        overlay_from_digitizer
                                        and Path(
                                            overlay_from_digitizer
                                        ).exists()
                                    ):
                                        composited_path = (
                                            overlay_from_digitizer
                                        )
                                    else:
                                        # fallback to manual overlay
                                        composited_path = ""
                                        if crop_img_bgr is not None:
                                            crop_bgra = cv2.cvtColor(
                                                crop_img_bgr,
                                                cv2.COLOR_BGR2BGRA,
                                            )
                                            alpha = (
                                                transparent[:, :, 3:4] / 255.0
                                            )

                                            blended = (
                                                crop_bgra[:, :, :3]
                                                * (1 - alpha)
                                                + transparent[:, :, :3] * alpha
                                            ).astype(np.uint8)

                                            composited_bgra = np.dstack(
                                                [
                                                    blended,
                                                    np.full(
                                                        (h_img, w_img),
                                                        255,
                                                        dtype=np.uint8,
                                                    ),
                                                ]
                                            )

                                            composited_path = str(
                                                Path(output_dir)
                                                / f"{stem}_composited.png"
                                            )
                                            cv2.imwrite(
                                                composited_path,
                                                composited_bgra,
                                            )

                                    # Save display paths so changing radio options does not rerun digitization
                                    _cache_data = {
                                        "raw_path": raw_path,
                                        "transparent_path": transparent_path,
                                        "mpl_path": mpl_path,
                                        "composited_path": composited_path,
                                        "result": {
                                            "results": [
                                                {
                                                    "label": c.get(
                                                        "label", ""
                                                    ),
                                                    "target_rgb": list(
                                                        c.get(
                                                            "target_rgb",
                                                            [0, 0, 0],
                                                        )
                                                    ),
                                                    "x_vals": [
                                                        float(x)
                                                        for x in c.get(
                                                            "x_vals", []
                                                        )
                                                    ],
                                                    "y_vals": [
                                                        float(y)
                                                        for y in c.get(
                                                            "y_vals", []
                                                        )
                                                    ],
                                                    "picked": [
                                                        [int(p[0]), int(p[1])]
                                                        for p in c.get(
                                                            "picked", []
                                                        )
                                                    ],
                                                }
                                                for c in result.get(
                                                    "results", []
                                                )
                                            ],
                                        },
                                        "out_json_path": out_json_path,
                                    }
                                    st.session_state[state_key] = _cache_data

                                    # Persist to disk so it survives page refresh
                                    try:
                                        Path(output_dir).mkdir(
                                            parents=True, exist_ok=True
                                        )
                                        Path(cache_json_path).write_text(
                                            json.dumps(
                                                _cache_data,
                                                indent=2,
                                                ensure_ascii=False,
                                            ),
                                            encoding="utf-8",
                                        )
                                    except Exception:
                                        pass

                                    # # ---- 2. Update validated JSON with digitized data ----
                                    # validated_path = find_validated_json_for_figure(
                                    #     str(OUT_DIR), fig_info.get("pdf_name", "")
                                    # )
                                    # stem_out = Path(crop_path).stem
                                    # out_json_path = str(Path(output_dir) / f"{stem_out}__digitized.json")
                                    # build_digitized_output(
                                    #     validated_json_path=validated_path,
                                    #     digitized_results=result,
                                    #     figure_number=fig_info.get("figure_number"),
                                    #     output_path=out_json_path,
                                    # )

                                    # Also patch the validated JSON itself with digitized curves
                                    if (
                                        validated_path
                                        and Path(validated_path).exists()
                                    ):
                                        try:
                                            val_data = json.loads(
                                                Path(validated_path).read_text(
                                                    encoding="utf-8"
                                                )
                                            )
                                            fig_num = fig_info.get(
                                                "figure_number"
                                            )

                                            for fig_entry in val_data.get(
                                                "figures", []
                                            ):
                                                if (
                                                    fig_entry.get(
                                                        "figure_number"
                                                    )
                                                    == fig_num
                                                ):
                                                    fig_entry["digitized"] = {
                                                        "algorithm": digi_algorithm,
                                                        "n_curves": len(
                                                            result.get(
                                                                "results", []
                                                            )
                                                        ),
                                                        "curves": [],
                                                    }

                                                    for curve in result.get(
                                                        "results", []
                                                    ):
                                                        fig_entry["digitized"][
                                                            "curves"
                                                        ].append(
                                                            {
                                                                "label": curve.get(
                                                                    "label", ""
                                                                ),
                                                                "target_rgb": [
                                                                    int(x)
                                                                    for x in curve.get(
                                                                        "target_rgb",
                                                                        [
                                                                            0,
                                                                            0,
                                                                            0,
                                                                        ],
                                                                    )
                                                                ],
                                                                "n_points": len(
                                                                    curve.get(
                                                                        "x_vals",
                                                                        [],
                                                                    )
                                                                ),
                                                                "x_vals": [
                                                                    float(x)
                                                                    for x in curve.get(
                                                                        "x_vals",
                                                                        [],
                                                                    )
                                                                ],
                                                                "y_vals": [
                                                                    float(y)
                                                                    for y in curve.get(
                                                                        "y_vals",
                                                                        [],
                                                                    )
                                                                ],
                                                            }
                                                        )
                                                    break

                                            Path(validated_path).write_text(
                                                json.dumps(
                                                    val_data,
                                                    indent=2,
                                                    ensure_ascii=False,
                                                ),
                                                encoding="utf-8",
                                            )
                                            st.toast(
                                                f"📝 Updated validated JSON: {Path(validated_path).name}"
                                            )

                                        except Exception as patch_err:
                                            st.warning(
                                                f"Could not patch validated JSON: {patch_err}"
                                            )

                                    st.rerun()

                                except Exception as e:
                                    st.error(f"Digitization failed: {e}")
                                    import traceback

                                    st.code(traceback.format_exc())

                        # # Rerun so JSON viewer at top refreshes with patched data
                        # st.rerun()

                    # ---- Persistent display selector ----
                    # This must stay OUTSIDE the st.button block.
                    if state_key in st.session_state:
                        saved = st.session_state[state_key]

                        view_mode = st.radio(
                            "View mode",
                            options=[
                                "Original image",
                                "Digitized points",
                                "Overlay on reference",
                                "Plot with axes",
                            ],
                            index=2,
                            key=f"digi_view_mode_{stem}",
                            horizontal=True,
                        )

                        if view_mode == "Original image":
                            st.image(
                                saved["raw_path"],
                                caption="Original XRD plot",
                                width=700,
                            )

                        elif view_mode == "Digitized points":
                            if (
                                saved.get("transparent_path")
                                and Path(saved["transparent_path"]).exists()
                            ):
                                st.image(
                                    saved["transparent_path"],
                                    caption="Digitized points only",
                                    width=700,
                                )
                            else:
                                st.warning(
                                    "Digitized points image was not found."
                                )

                        elif view_mode == "Overlay on reference":
                            if (
                                saved.get("composited_path")
                                and Path(saved["composited_path"]).exists()
                            ):
                                st.image(
                                    saved["composited_path"],
                                    caption="Digitized points on reference image",
                                    width=700,
                                )
                            else:
                                st.warning(
                                    "Combined reference image was not found."
                                )
                        elif view_mode == "Plot with axes":
                            if (
                                saved.get("mpl_path")
                                and Path(saved["mpl_path"]).exists()
                            ):
                                st.image(
                                    saved["mpl_path"],
                                    caption="Digitized curves with 2θ / Intensity axes",
                                    width=700,
                                )
                            else:
                                st.warning(
                                    "Matplotlib plot was not found. Re-digitize to generate it."
                                )

                        # Download buttons (persistent — survives rerun)
                        _dl_cols = st.columns(4)
                        with _dl_cols[0]:
                            _dj_path = saved.get("out_json_path", "")
                            if _dj_path and Path(_dj_path).exists():
                                st.download_button(
                                    "📥 JSON",
                                    data=Path(_dj_path).read_text(
                                        encoding="utf-8"
                                    ),
                                    file_name=Path(_dj_path).name,
                                    mime="application/json",
                                    key=f"dl_digi_json_{stem}",
                                )
                        with _dl_cols[1]:
                            _csv_path = str(
                                Path(output_dir) / f"{stem}_digitized.csv"
                            )
                            if Path(_csv_path).exists():
                                st.download_button(
                                    "📥 CSV",
                                    data=Path(_csv_path).read_bytes(),
                                    file_name=Path(_csv_path).name,
                                    mime="text/csv",
                                    key=f"dl_digi_csv_{stem}",
                                )
                        with _dl_cols[2]:
                            if (
                                saved.get("out_json_path")
                                and Path(saved["out_json_path"]).exists()
                            ):
                                st.download_button(
                                    "📥 Full JSON",
                                    data=Path(
                                        saved["out_json_path"]
                                    ).read_text(encoding="utf-8"),
                                    file_name=Path(
                                        saved["out_json_path"]
                                    ).name,
                                    mime="application/json",
                                    key=f"dl_digi_full_{stem}",
                                )
                        with _dl_cols[3]:
                            if (
                                saved.get("transparent_path")
                                and Path(saved["transparent_path"]).exists()
                            ):
                                st.download_button(
                                    "📥 Points PNG",
                                    data=Path(
                                        saved["transparent_path"]
                                    ).read_bytes(),
                                    file_name=Path(
                                        saved["transparent_path"]
                                    ).name,
                                    mime="image/png",
                                    key=f"dl_digi_png_{stem}",
                                )

                        # Axis info
                        _sr = saved.get("result", {})
                        if (
                            any(k in saved for k in ["x_min", "x_max"])
                            or "results" in _sr
                        ):
                            _digi_json_p = saved.get("out_json_path", "")
                            if _digi_json_p and Path(_digi_json_p).exists():
                                try:
                                    _dj = json.loads(
                                        Path(_digi_json_p).read_text(
                                            encoding="utf-8"
                                        )
                                    )
                                    _dig = _dj.get("digitized", {})
                                    _xa = _dig.get("x_axis", {})
                                    _ya = _dig.get("y_axis", {})
                                    _nc = len(_dig.get("curves", []))
                                    _algo = _dig.get("algorithm", "?")
                                    ax1, ax2, ax3, ax4 = st.columns(4)
                                    ax1.metric("Algorithm", _algo)
                                    ax2.metric("Curves", str(_nc))
                                    ax3.metric(
                                        "2θ range",
                                        f"{_xa.get('min', '?')}–{_xa.get('max', '?')}°",
                                    )
                                    ax4.metric(
                                        "Y range",
                                        f"{_ya.get('min', '?')}–{_ya.get('max', '?')}",
                                    )
                                except Exception:
                                    pass
                        # Quick preview of digitized data
                        with st.expander(
                            "📋 Digitized curves data", expanded=False
                        ):
                            saved_result = saved.get("result", {})
                            for curve in saved_result.get("results", []):
                                rgb = curve.get("target_rgb", [0, 0, 0])
                                color_hex = (
                                    f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
                                )
                                n_pts = len(curve.get("x_vals", []))
                                st.markdown(
                                    f"<span style='color:{color_hex};font-size:20px;'>●</span> "
                                    f"**{curve.get('label', '?')}** — {n_pts} points",
                                    unsafe_allow_html=True,
                                )

                            # Show the digitized JSON file inline
                            digi_json_path = saved.get("out_json_path", "")
                            if (
                                digi_json_path
                                and Path(digi_json_path).exists()
                            ):
                                try:
                                    digi_data = json.loads(
                                        Path(digi_json_path).read_text(
                                            encoding="utf-8"
                                        )
                                    )
                                    show_json_editor(digi_data, height=400)
                                except Exception:
                                    st.code(
                                        Path(digi_json_path).read_text(
                                            encoding="utf-8"
                                        ),
                                        language="json",
                                    )

            st.divider()
            st.subheader("Find matching CIF from Materials Project")

            mp_hints = extract_mp_hints_from_json(parsed_json)
            # Use figure-level material first, then fall back to main_material, then formula hints
            figure_materials = []

            for fig in (
                parsed_json.get("figures", parsed_json.get("xrd_figures", []))
                or []
            ):
                if isinstance(fig, dict):
                    mat = str(fig.get("material", "") or "").strip()
                    if mat and mat not in figure_materials:
                        figure_materials.append(mat)

            main_mat = str(parsed_json.get("main_material", "") or "").strip()

            raw_formula_default = ""

            if figure_materials:
                raw_formula_default = figure_materials[0]
            elif main_mat:
                raw_formula_default = main_mat
            elif mp_hints["formulas"]:
                raw_formula_default = mp_hints["formulas"][0]

            # Clean descriptive material text into MP-safe formula/query
            formula_default = normalize_formula_like(raw_formula_default)

            phase_default = mp_hints["phases"][0] if mp_hints["phases"] else ""
            sg_default = (
                mp_hints["space_groups"][0] if mp_hints["space_groups"] else ""
            )

            caption_parts = []

            if raw_formula_default:
                caption_parts.append(f"raw material: {raw_formula_default}")

            if formula_default:
                caption_parts.append(f"cleaned formula: {formula_default}")

            if mp_hints["formulas"]:
                caption_parts.append(
                    f"formula hints: {', '.join(mp_hints['formulas'])}"
                )

            if mp_hints["phases"]:
                caption_parts.append(
                    f"phases: {', '.join(mp_hints['phases'])}"
                )

            if mp_hints["space_groups"]:
                caption_parts.append(
                    f"space groups: {', '.join(mp_hints['space_groups'])}"
                )

            if caption_parts:
                st.caption("Hints from JSON | " + " | ".join(caption_parts))

            mp_key = f"mp_formula_{selected_json.name}"
            phase_key = f"mp_phase_{selected_json.name}"
            sg_key = f"mp_sg_{selected_json.name}"
            conv_key = f"mp_conv_{selected_json.name}"
            nres_key = f"mp_nres_{selected_json.name}"

            # Initialize once (DO NOT overwrite user edits)
            # Auto-fill once, or refill if currently empty
            if not st.session_state.get(mp_key, "").strip():
                st.session_state[mp_key] = formula_default

            if not st.session_state.get(phase_key, "").strip():
                st.session_state[phase_key] = phase_default

            if not st.session_state.get(sg_key, "").strip():
                st.session_state[sg_key] = sg_default

            formula_query = st.text_input("Formula / query", key=mp_key)
            phase_hint = st.text_input("Step hint (optional)", key=phase_key)
            space_group_hint = st.text_input(
                "Space group hint (optional)", key=sg_key
            )

            search_col1, search_col2 = st.columns([1, 1])
            with search_col1:
                conventional_cell = st.checkbox(
                    "Export conventional cell CIF", value=True, key=conv_key
                )
            with search_col2:
                max_results = st.number_input(
                    "Max results",
                    min_value=1,
                    max_value=10,
                    value=5,
                    step=1,
                    key=nres_key,
                )

            if st.button(
                "Find matching MP CIF",
                width="stretch",
                key=f"find_mp_{selected_json.name}",
            ):
                if not st.session_state.mp_api_key.strip():
                    st.error("Add MP_API_KEY in the sidebar first.")
                else:
                    with st.spinner("Searching Materials Project..."):
                        mp_results, mp_error = search_materials_project(
                            formula_hint=formula_query,
                            phase_hint=phase_hint,
                            space_group_hint=space_group_hint,
                            api_key=st.session_state.mp_api_key.strip(),
                            conventional_cell=conventional_cell,
                            max_results=int(max_results),
                        )

                    if mp_error:
                        st.error(mp_error)
                    elif not mp_results:
                        st.warning("No Materials Project matches found.")
                    else:
                        st.success(f"Found {len(mp_results)} match(es).")

                        for i, item in enumerate(mp_results, start=1):
                            title = f"{i}. {item['formula_pretty']} | {item['material_id']}"
                            with st.expander(title, expanded=(i == 1)):
                                info_cols = st.columns(3)
                                info_cols[0].metric(
                                    "Stable",
                                    "Yes" if item["is_stable"] else "No",
                                )
                                info_cols[1].metric(
                                    "E above hull (eV/atom)",
                                    (
                                        "N/A"
                                        if item["energy_above_hull"] is None
                                        else f"{item['energy_above_hull']:.4f}"
                                    ),
                                )
                                info_cols[2].metric(
                                    "Crystal system",
                                    (
                                        item["crystal_system"]
                                        if item["crystal_system"]
                                        else "N/A"
                                    ),
                                )

                                sg_symbol = item["space_group_symbol"] or "N/A"
                                sg_number = item["space_group_number"]
                                sg_text = (
                                    f"{sg_symbol} ({sg_number})"
                                    if sg_number
                                    else sg_symbol
                                )
                                st.write(f"Space group: {sg_text}")

                                if item["icsd_ids"]:
                                    st.write(
                                        f"ICSD-linked IDs in MP: {', '.join(map(str, item['icsd_ids']))}"
                                    )
                                else:
                                    st.write(
                                        "ICSD-linked IDs in MP: none shown"
                                    )

                                cif_filename = f"{item['formula_pretty']}_{item['material_id']}.cif".replace(
                                    " ", "_"
                                )
                                st.download_button(
                                    "Download CIF",
                                    data=item["cif_text"],
                                    file_name=cif_filename,
                                    mime="chemical/x-cif",
                                    key=f"download_cif_{selected_json.name}_{item['material_id']}",
                                )

                                with st.expander(
                                    "Preview CIF text", expanded=False
                                ):
                                    st.code(item["cif_text"], language="text")
        # ---- Export all results as ZIP ----
        st.divider()
        if st.button(
            "📦 Export all results as ZIP",
            width="stretch",
            key="export_all_zip",
        ):
            import io
            import zipfile

            zip_buffer = io.BytesIO()
            file_count = 0

            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                # All JSON outputs
                for jf in json_files:
                    arcname = f"json/{jf.name}"
                    zf.write(str(jf), arcname)
                    file_count += 1

                # Digitized outputs
                digi_dir = OUT_DIR / "digitized"
                if digi_dir.exists():
                    for df in digi_dir.rglob("*"):
                        if df.is_file():
                            arcname = f"digitized/{df.relative_to(digi_dir)}"
                            zf.write(str(df), arcname)
                            file_count += 1

                # XRD images
                for xrd_dir in OUT_DIR.glob("*__XRD_ONLY"):
                    if xrd_dir.is_dir():
                        for img in xrd_dir.glob("*.png"):
                            arcname = f"xrd_images/{xrd_dir.name}/{img.name}"
                            zf.write(str(img), arcname)
                            file_count += 1

                # Usage report
                usage_path = OUT_DIR / "usage_report.json"
                if usage_path.exists():
                    zf.write(str(usage_path), "usage_report.json")
                    file_count += 1

            zip_buffer.seek(0)
            st.download_button(
                f"📥 Download ZIP ({file_count} files)",
                data=zip_buffer.getvalue(),
                file_name="eraf4xrd_results.zip",
                mime="application/zip",
                key="dl_all_zip",
            )
            st.toast(f"✅ Packaged {file_count} files into ZIP")
    else:
        st.write("No JSON outputs found yet.")
