"""Publicación de ofertas en un grupo o canal de Telegram.

Es la única salida del proyecto hacia un servicio ajeno a las tiendas, y va
apagada de fábrica. Se enciende en `config/settings.yaml`; el token NO se
guarda ahí, sino en la variable de entorno TELEGRAM_BOT_TOKEN, para que
subir el repositorio no filtre una credencial.

No hace falta ninguna librería: la API de Telegram son peticiones HTTP y
`httpx` ya es dependencia del scraping.
"""
from __future__ import annotations

import asyncio
import html
import os
import re
from typing import Any, Dict, List, Optional

import httpx

from app import settings
from app.db.database import get_connection, log, transaction

API = "https://api.telegram.org"

# Qué sabe anunciar, y con qué cara.
PLANTILLAS = {
    "price_drop": "📉",
    "back_in_stock": "📦",
    "new_product": "🆕",
}


def config() -> Dict[str, Any]:
    return dict(settings.get("telegram", {}) or {})


def token() -> Optional[str]:
    """El token vive en el entorno, nunca en un archivo del repositorio."""
    return (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip() or None


def chat_id() -> Optional[str]:
    """Grupo o canal donde publicar.

    Manda la variable de entorno TELEGRAM_CHAT_ID si está; si no, lo que diga
    `config/settings.yaml`. No es una credencial —sin el token nadie puede
    publicar ahí— pero con el repositorio público conviene poder dejarlo fuera.
    """
    del_entorno = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if del_entorno:
        return del_entorno
    return (config().get("chat_id") or "").strip() or None


def estado() -> Dict[str, Any]:
    cfg = config()
    return {
        "enabled": bool(cfg.get("enabled")),
        "chat_id": chat_id(),
        "has_token": token() is not None,
        "dry_run": bool(cfg.get("dry_run", True)),
        "publish": list(cfg.get("publish") or []),
        "pending": len(eventos_pendientes()),
    }


# ---------------------------------------------------------------------------
# Qué se publica
# ---------------------------------------------------------------------------
def eventos_pendientes(limite: Optional[int] = None) -> List[Dict[str, Any]]:
    """Eventos que cumplen los filtros y todavía no se han anunciado.

    Se piden de más antiguo a más nuevo para que el grupo lea la historia en
    el orden en que ocurrió.
    """
    cfg = config()
    tipos = [t for t in (cfg.get("publish") or ["price_drop"]) if t in PLANTILLAS]
    if not tipos:
        return []

    limite = limite or int(cfg.get("max_per_run", 8))
    min_pct = float(cfg.get("min_drop_pct", 0) or 0)
    min_monto = float(cfg.get("min_drop_amount", 0) or 0)
    horas = int(cfg.get("max_age_hours", 48) or 48)

    sql = f"""
        SELECT e.id, e.type, e.old_value, e.new_value, e.pct_change, e.created_at,
               sp.name AS offer_name, sp.url, sp.language, sp.price AS current_price,
               p.id AS product_id, p.display_name, p.game,
               -- La foto de la tienda manda: es la de ese producto concreto.
               -- La del producto maestro es la de otra tienda cualquiera.
               COALESCE(sp.image_url, p.image_url) AS image_url,
               s.name AS store_name
        FROM events e
        JOIN store_products sp ON sp.id = e.store_product_id AND sp.is_active = 1
        LEFT JOIN products p ON p.id = sp.product_id
        LEFT JOIN stores s ON s.id = e.store_id
        LEFT JOIN telegram_sent t ON t.event_id = e.id
        WHERE t.event_id IS NULL
          AND e.type IN ({','.join('?' * len(tipos))})
          AND e.created_at >= datetime('now', '-{horas} hours')
        ORDER BY e.created_at ASC, e.id ASC
    """
    with get_connection() as conn:
        filas = [dict(f) for f in conn.execute(sql, tipos).fetchall()]

    salida: List[Dict[str, Any]] = []
    for fila in filas:
        if fila["type"] == "price_drop":
            # Una bajada de $200 en un producto de $80.000 no es noticia.
            antes, ahora = _numero(fila["old_value"]), _numero(fila["new_value"])
            if antes is None or ahora is None:
                continue
            bajada = antes - ahora
            pct = abs(fila["pct_change"] or 0)
            if bajada < min_monto or pct < min_pct:
                continue
            fila["_bajada"] = bajada
        salida.append(fila)
        if len(salida) >= limite:
            break
    return salida


# ---------------------------------------------------------------------------
# Cómo se ve
# ---------------------------------------------------------------------------
def _numero(valor: Any) -> Optional[float]:
    """`old_value` y `new_value` se guardan como TEXTO en `events`.

    La columna es genérica —también lleva nombres en `name_change`—, así que
    aquí se convierte con cuidado en vez de dar por hecho que es un número.
    """
    if valor is None or valor == "":
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _pesos(valor: Any) -> str:
    numero = _numero(valor)
    if numero is None:
        return "—"
    return "$" + f"{int(round(numero)):,}".replace(",", ".")


# El nombre del producto ya termina en el idioma entre paréntesis
# —«… Elite Trainer Box (Inglés)»—. En el mensaje queda mejor separado con
# un punto medio, igual que el resto de los campos.
_IDIOMA_AL_FINAL = re.compile(r"\s*\(([^()]{3,20})\)\s*$")

TITULARES = {
    "price_drop": "BAJÓ DE PRECIO",
    "back_in_stock": "VOLVIÓ A HABER STOCK",
    "new_product": "NUEVO EN LA TIENDA",
}


def _titulo(evento: Dict[str, Any]) -> str:
    nombre = evento["display_name"] or evento["offer_name"] or "Producto"
    coincidencia = _IDIOMA_AL_FINAL.search(nombre)
    if coincidencia:
        nombre = f"{nombre[:coincidencia.start()]} · {coincidencia.group(1)}"
    return html.escape(nombre)


def _linea(evento: Dict[str, Any]) -> str:
    icono = PLANTILLAS.get(evento["type"], "•")
    titular = TITULARES.get(evento["type"], "OFERTA")
    url = evento["url"] or ""

    filas = [f"{icono} <b>{titular}</b>", f"<b>{_titulo(evento)}</b>"]

    if evento["type"] == "price_drop":
        # El precio anterior tachado: se entiende de un vistazo, sin leer.
        filas.append(
            f"Precio · {_pesos(evento['new_value'])} "
            f"<s>{_pesos(evento['old_value'])}</s>"
        )
        filas.append(f"Ahorro · {_pesos(evento.get('_bajada') or 0)}")
    else:
        # En «volvió a haber stock» y «nuevo», `new_value` no es un precio:
        # el importe se toma de la oferta tal como está ahora.
        filas.append(f"Precio · {_pesos(evento['current_price'])}")

    if url:
        filas.append(f'<a href="{html.escape(url, quote=True)}">Ver en la tienda</a>')
    return "\n".join(filas)


def componer(eventos: List[Dict[str, Any]]) -> str:
    """Un solo mensaje con todas las ofertas."""
    cabecera = config().get("header", "🔥 <b>Ofertas TCG Chile</b>")
    cuerpo = "\n\n".join(_linea(e) for e in eventos)
    return f"{cabecera}\n\n{cuerpo}"


def componer_sueltos(eventos: List[Dict[str, Any]]) -> List[str]:
    """Un mensaje por oferta, para que cada una se pueda reenviar y comentar.

    Sin cabecera repetida: doce mensajes seguidos encabezados por «Ofertas TCG
    Chile» se leen como spam. El icono de cada línea ya distingue una bajada de
    precio de una vuelta a stock.
    """
    return [_linea(e) for e in eventos]


# ---------------------------------------------------------------------------
# Envío
# ---------------------------------------------------------------------------
async def enviar(texto: str, imagen: Optional[str] = None) -> Dict[str, Any]:
    """Manda un mensaje. Con `imagen`, la foto va arriba y el texto debajo.

    Telegram descarga la foto él mismo desde la URL. Si no puede —enlace roto,
    formato raro, la tienda le niega el acceso— se reintenta como mensaje de
    texto: mejor una oferta sin foto que una oferta que no se publica.
    """
    cfg = config()
    clave = token()
    chat = chat_id()
    if not clave:
        raise RuntimeError("Falta la variable de entorno TELEGRAM_BOT_TOKEN")
    if not chat:
        raise RuntimeError("Falta el grupo: TELEGRAM_CHAT_ID o telegram.chat_id")

    # El pie de una foto admite 1024 caracteres; un mensaje suelto, 4096.
    con_foto = bool(imagen) and len(texto) <= 1024

    async with httpx.AsyncClient(timeout=30) as cliente:
        if con_foto:
            respuesta = await cliente.post(
                f"{API}/bot{clave}/sendPhoto",
                json={
                    "chat_id": chat,
                    "photo": imagen,
                    "caption": texto,
                    "parse_mode": "HTML",
                },
            )
            datos = respuesta.json()
            if datos.get("ok"):
                return datos
            log("warn", "telegram",
                f"No se pudo enviar con foto ({datos.get('description')}); "
                f"se manda solo el texto")

        respuesta = await cliente.post(
            f"{API}/bot{clave}/sendMessage",
            json={
                "chat_id": chat,
                "text": texto,
                "parse_mode": "HTML",
                "disable_web_page_preview": bool(cfg.get("hide_preview", True)),
            },
        )
    datos = respuesta.json()
    if not datos.get("ok"):
        detalle = datos.get("description")
        # Cuando un grupo pasa a supergrupo, Telegram le cambia el id y devuelve
        # el nuevo aquí mismo. Decirlo ahorra tener que ir a buscarlo.
        nuevo = (datos.get("parameters") or {}).get("migrate_to_chat_id")
        if nuevo:
            detalle += (
                f". El grupo cambió de identificador: pon "
                f"TELEGRAM_CHAT_ID = {nuevo} (antes {chat})"
            )
        raise RuntimeError(f"Telegram rechazó el mensaje: {detalle}")
    return datos


async def publicar(forzar_envio: bool = False) -> Dict[str, Any]:
    """Publica las ofertas pendientes.

    En modo `dry_run` no se envía nada: el mensaje se escribe en el registro
    para poder revisar el formato antes de conectarlo a un grupo de verdad.
    Los eventos NO se marcan como enviados en ese modo, para poder repetir.
    """
    cfg = config()
    if not cfg.get("enabled") and not forzar_envio:
        log("info", "telegram", "Desactivado (telegram.enabled = false)")
        return {"sent": 0, "reason": "desactivado"}

    eventos = eventos_pendientes()
    if not eventos:
        # Antes esto no dejaba rastro, y desde fuera no había forma de saber
        # si el aviso estaba apagado, mal configurado, o simplemente callado
        # porque no había nada que contar.
        log("info", "telegram",
            f"Nada que publicar — no hay {' ni '.join(cfg.get('publish') or ['eventos'])} "
            f"de las últimas {cfg.get('max_age_hours', 48)} h que superen los filtros "
            f"(bajada mínima {cfg.get('min_drop_pct', 0)}% o ${cfg.get('min_drop_amount', 0):,.0f})"
            .replace(",", "."))
        return {"sent": 0, "reason": "nada nuevo que contar"}

    sueltos = bool(cfg.get("one_message_per_offer", False))
    mensajes = componer_sueltos(eventos) if sueltos else [componer(eventos)]

    simulacion = bool(cfg.get("dry_run", True)) and not forzar_envio
    if simulacion:
        log("info", "telegram",
            f"[simulación] se habrían publicado {len(eventos)} ofertas en "
            f"{len(mensajes)} mensaje(s):\n\n" + "\n\n———\n\n".join(mensajes))
        return {"sent": 0, "dry_run": True, "preview": mensajes, "events": len(eventos)}

    if not sueltos:
        await enviar(mensajes[0])
        _marcar_enviados([e["id"] for e in eventos])
    else:
        # Uno a uno, marcando cada oferta en cuanto sale. Si el envío se corta
        # a la mitad, las que ya salieron no se repiten en la próxima pasada.
        pausa = float(cfg.get("delay_between_messages", 1.5))
        con_imagen = bool(cfg.get("include_image", True))
        for indice, (evento, mensaje) in enumerate(zip(eventos, mensajes)):
            if indice:
                # Telegram corta a un grupo que recibe más de ~20 mensajes por
                # minuto; esta pausa mantiene el ritmo por debajo.
                await asyncio.sleep(pausa)
            await enviar(mensaje, evento.get("image_url") if con_imagen else None)
            _marcar_enviados([evento["id"]])

    log("info", "telegram",
        f"{len(eventos)} ofertas publicadas en {chat_id()} "
        f"({len(mensajes)} mensaje(s))")
    return {"sent": len(eventos), "dry_run": False, "preview": mensajes}


def _marcar_enviados(ids: List[int]) -> None:
    with transaction() as conn:
        conn.executemany(
            "INSERT INTO telegram_sent (event_id) VALUES (?) ON CONFLICT DO NOTHING",
            [(i,) for i in ids],
        )
