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
    parser.add_argument("--store", action="append", help="limita --update a estas tiendas")
    args = parser.parse_args()

    from app import settings
    from app.db.database import init_db
    from app.main import seed_catalogs

    init_db()
    seed_catalogs()

    if args.update or args.rematch:
        from app.services import ingest, pipeline

        ingest.sync_stores_from_config()
        if args.rematch:
            result = asyncio.run(pipeline.rematch_only())
        else:
            result = asyncio.run(pipeline.run_all(trigger="manual", store_codes=args.store))
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
