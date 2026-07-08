from __future__ import annotations

import re


def normalize_html_input(raw_html: str | bytes | None) -> str:
    if isinstance(raw_html, bytes):
        html_text = raw_html.decode("utf-8", errors="replace")
    else:
        html_text = str(raw_html or "")

    html_text = html_text.replace("\r\n", "\n").replace("\r", "\n")
    html_text = html_text.replace("\x00", "")
    html_text = re.sub(r"\n{3,}", "\n\n", html_text)
    return html_text
