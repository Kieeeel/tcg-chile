"""Punto de entrada.

    python run.py                 -> arranca la interfaz en http://127.0.0.1:8000
    python run.py --update        -> ejecuta una actualización y termina
    python run.py --rematch       -> reagrupa sin volver a scrapear
    python run.py --port 3000     -> otro puerto
"""
from __future__ import annotations

import argparse
import asyncio
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="TCG Chile — comparador local de precios")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--reload", action="store_true", help="recarga automática (desarrollo)")
    parser.add_argument("--update", action="store_true", help="actualiza las tiendas y termina")
    parser.add_argument("--rematch", action="store_true", help="reagrupa sin scrapear y termina")
    parser.add_argument("--publicar", action="store_true",
                        help="publica en Telegram lo que haya pendiente y termina")
    parser.add_argument("--store", action="append", help="limita --update a estas tiendas")
    parser.add_argument("--vigilar", action="store_true",
                        help="mira los enlaces de config/vigilancia.yaml y termina")
    parser.add_argument("--durante", type=int, default=0, metavar="MINUTOS",
                        help="con --vigilar: sigue mirando en bucle este rato")
    parser.add_argument("--cada", type=int, default=60, metavar="SEGUNDOS",
                        help="con --durante: espera entre vueltas (por defecto 60)")
    args = parser.parse_args()

    from app import settings
    from app.db.database import init_db
    from app.main import seed_catalogs

    init_db()
    seed_catalogs()

    if args.vigilar:
        # Enlaces sueltos, mirados uno a uno. No recorre ninguna tienda ni toca
        # el catálogo: solo compara cada ficha con cómo estaba la última vez.
        from app.db.database import log
        from app.services import watch

        try:
            if args.durante > 0:
                # Un solo turno concedido por GitHub se estira en muchas
                # revisiones. Sin esto, el cron marca cada cuánto se mira; con
                # esto solo marca cada cuánto se releva el turno.
                resultado = asyncio.run(watch.vigilar_durante(args.durante, args.cada))
            else:
                resultado = asyncio.run(watch.revisar())
        except KeyboardInterrupt:
            print("\nVigilancia interrumpida.\n")
            return 0
        except Exception as exc:  # noqa: BLE001
            log("warn", "vigilancia", f"No se pudo completar: {exc}")
            print(f"\nVigilancia: falló: {exc}\n")
            return 1
        print(f"\nVigilancia: {resultado}\n")
        return 0

    if args.publicar:
        # Publicar va por su cuenta, cada hora, para que las ofertas salgan a
        # goteo y no a ráfagas de diez cada vez que termina un scraping.
        # También atiende las membresías, que tampoco dependen de los precios.
        from app.db.database import log
        from app.services import membership, notify

        # Cada tarea por su cuenta: si Telegram rechaza una oferta, las órdenes
        # de administración y los vencimientos tienen que atenderse igual. Con
        # las dos en la misma línea, un fallo al publicar dejaba el grupo de
        # socios sin revisar durante horas sin que se notara.
        salida = {}
        for nombre, tarea in (("publicar", notify.publicar), ("membresia", membership.ejecutar)):
            try:
                salida[nombre] = asyncio.run(tarea())
            except Exception as exc:  # noqa: BLE001
                salida[nombre] = f"falló: {exc}"
                log("warn", nombre, f"No se pudo completar: {exc}")

        print(f"\nTelegram: {salida}\n")
        return 0

    if args.update or args.rematch:
        from app.services import ingest, pipeline

        try:
            ingest.sync_stores_from_config()
            if args.rematch:
                result = asyncio.run(pipeline.rematch_only())
            else:
                result = asyncio.run(
                    pipeline.run_all(trigger="manual", store_codes=args.store)
                )
        except Exception as exc:
            # Si revienta entera no hay resumen que revisar, así que el aviso
            # se manda desde aquí. Corriendo sin nadie delante, un fallo mudo
            # son días de precios viejos sin que nadie se entere.
            import traceback

            traceback.print_exc()
            try:
                from app.services import health

                asyncio.run(health.avisar_fallo(f"{type(exc).__name__}: {exc}"))
            except Exception:  # noqa: BLE001
                pass
            return 1
        print(_summarize(result))
        return 0

    import uvicorn

    host = args.host or settings.get("app.host", "127.0.0.1")
    port = args.port or int(settings.get("app.port", 8000))
    print(f"\n  TCG Chile  ->  http://{host}:{port}\n")
    uvicorn.run("app.main:app", host=host, port=port, reload=args.reload, log_level="info")
    return 0


def _summarize(result: dict) -> str:
    if "stores" in result:
        lines = ["", "Resultado de la actualización:"]
        for store in result.get("stores", []):
            lines.append(
                f"  · {store.get('store_name', store.get('store'))}: "
                f"{store.get('found', 0)} productos "
                f"({store.get('new', 0)} nuevos, {store.get('errors', 0)} errores)"
            )
        matching = result.get("matching") or {}
        lines.append(
            f"  · Matching: {matching.get('products', 0)} productos maestros a partir de "
            f"{matching.get('offers', 0)} ofertas "
            f"({matching.get('reviews', 0)} pendientes de revisión)"
        )
        return "\n".join(lines)
    return str(result)


if __name__ == "__main__":
    sys.exit(main())
