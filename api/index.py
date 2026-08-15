"""Punto de entrada para Vercel.

Vercel ejecuta funciones sin estado y de vida corta: aquí la aplicación solo
LEE de Supabase y sirve la interfaz. El scraping y la publicación en Telegram
los hace GitHub Actions cada 4 horas, que sí puede estar minutos trabajando.

Por eso se apaga el planificador: si arrancara aquí, cada invocación de la
función intentaría montar su propio temporizador que moriría con ella.
"""
from __future__ import annotations

import os

os.environ.setdefault("TCG_DISABLE_SCHEDULER", "1")

from app.main import app  # noqa: E402  (después de fijar el entorno)

# Vercel busca una variable llamada `app` o `handler`.
handler = app
