"""Atiende el bot al momento, desde tu equipo.

En el despliegue el bot solo escucha cuando corre el cron, cada 4 horas. Eso
va bien para lo que hace —vencimientos y altas, que no corren prisa— pero es
incómodo para probar o para atender algo puntual.

Esto lo mantiene escuchando mientras la ventana esté abierta: responde a las
órdenes en segundos. Ctrl+C para salir.

    python scripts/bot.py                       # sobre la base local
    python scripts/bot.py --url "postgres..."   # sobre Supabase (lo normal)
    python scripts/bot.py --una-vez             # una pasada y salir

Necesita en el entorno:
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ADMIN_ID

No lo dejes corriendo a la vez que el cron: los dos leerían las mismas
novedades de Telegram y se las quitarían el uno al otro.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))


def revisar_entorno() -> None:
    faltan = [
        nombre for nombre in
        ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ADMIN_ID")
        if not (os.environ.get(nombre) or "").strip()
    ]
    if faltan:
        raise SystemExit(
            "\nFaltan variables de entorno: " + ", ".join(faltan) + "\n\n"
            '  $env:TELEGRAM_BOT_TOKEN = "..."\n'
            '  $env:TELEGRAM_CHAT_ID   = "-1004369442500"\n'
            '  $env:TELEGRAM_ADMIN_ID  = "5742570649"\n'
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None, help="URL de Postgres (si no, la base local)")
    parser.add_argument("--una-vez", action="store_true", help="una pasada y salir")
    parser.add_argument("--cada", type=float, default=3.0, help="segundos entre consultas")
    args = parser.parse_args()

    if args.url:
        os.environ["DATABASE_URL"] = args.url
    revisar_entorno()

    from app.db.database import es_postgres, init_db
    from app.services import membership

    init_db()
    print(f"\nBase: {'Supabase' if es_postgres() else 'local (SQLite)'}")
    print(f"Grupo: {membership.chat_id()}   Administrador: {membership.admin_id()}")

    if not membership.config().get("enabled"):
        print("\n  AVISO: membresia.enabled está en false en config/settings.yaml.")
        print("  Las órdenes se atienden igual desde aquí, pero el cron no hará")
        print("  nada hasta que lo pongas en true.\n")

    if args.una_vez:
        print(await membership.recoger_novedades())
        print(await membership.revisar_vencimientos())
        return 0

    print(f"\nEscuchando cada {args.cada:.0f} s. Escríbele por privado. Ctrl+C para salir.\n")
    try:
        while True:
            resultado = await membership.recoger_novedades()
            if resultado.get("novedades"):
                print(f"  {resultado['ordenes']} orden(es), {resultado['altas']} alta(s)")
            await asyncio.sleep(args.cada)
    except KeyboardInterrupt:
        print("\nHasta luego.\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
