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
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from app import settings
from app.db.database import get_connection, log, query, query_one, transaction

API = "https://api.telegram.org"


class TelegramRechazo(RuntimeError):
    """Telegram entendió la petición y dijo que no.

    `permanente` distingue lo que no va a cambiar por reintentar —un mensaje
    con HTML mal formado, una foto que no puede descargar— de lo que sí, como
    un «vas demasiado rápido».
    """

    def __init__(self, mensaje: str, permanente: bool = True,
                 esperar: Optional[float] = None) -> None:
        super().__init__(mensaje)
        self.permanente = permanente
        # Segundos que Telegram pide esperar antes de reintentar (429).
        self.esperar = float(esperar) if esperar else None

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
        "preventa": preventa(),
        "pending": len(eventos_pendientes()),
    }


def preventa() -> Dict[str, Any]:
    """Modo temporal: durante unos días el grupo habla de una sola tienda.

    Sirve para un lanzamiento anunciado, cuando lo que interesa es no perderse
    nada de UNA tienda concreta y el resto del mercado sobra. Mientras está
    encendido, el bot solo publica eventos de las tiendas de la lista, y de
    esas publica también los productos nuevos, que el resto del año son
    demasiados para el grupo.

    Devuelve {} cuando está apagado, que es lo normal. El scraping NO se toca:
    las demás tiendas se siguen recorriendo y el comparador sigue completo, es
    solo el bot el que se calla. Apagarlas de verdad dejaría la web con precios
    de hace días y daría de baja media base al volver.
    """
    cfg = dict(settings.get("preventa", {}) or {})
    if not cfg.get("enabled"):
        return {}

    tiendas = [str(c).strip() for c in (cfg.get("tiendas") or []) if str(c).strip()]
    if not tiendas:
        return {}

    # Fecha de caducidad opcional: «temporal» solo lo es de verdad si se apaga
    # solo. Sin ella hay que acordarse a mano, y nadie se acuerda.
    #
    # Los avisos van por pantalla y no al registro de la base: esto se consulta
    # varias veces en cada publicación, y publicando cada diez minutos serían
    # cientos de líneas al día repitiendo lo mismo.
    hasta = str(cfg.get("hasta") or "").strip()
    if hasta:
        try:
            limite = datetime.fromisoformat(hasta).date()
        except ValueError:
            print(f"[telegram] preventa.hasta = «{hasta}» no es una fecha "
                  f"(AAAA-MM-DD); se ignora y el modo sigue encendido", flush=True)
        else:
            if _ahora_en_chile().date() > limite:
                print(f"[telegram] El modo preventa venció el {limite}; "
                      f"se publica con normalidad", flush=True)
                return {}

    return {
        "tiendas": tiendas,
        "publicar": [t for t in (cfg.get("publicar") or []) if t in PLANTILLAS],
        "hasta": hasta or None,
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

    # En modo preventa se amplía lo que se cuenta (productos nuevos, sobre
    # todo) pero SOLO de las tiendas de la lista. Los dos van juntos a
    # propósito: publicar los productos nuevos de las veinticinco tiendas
    # sería un mensaje cada pocos minutos.
    modo = preventa()
    if modo:
        tipos = sorted(set(tipos) | set(modo["publicar"]))

    if not tipos:
        return []

    limite = limite or int(cfg.get("max_per_run", 8))
    min_pct = float(cfg.get("min_drop_pct", 0) or 0)
    min_monto = float(cfg.get("min_drop_amount", 0) or 0)
    horas = int(cfg.get("max_age_hours", 48) or 48)

    filtro_tienda = ""
    parametros: List[Any] = list(tipos)
    if modo:
        filtro_tienda = f"AND s.code IN ({','.join('?' * len(modo['tiendas']))})"
        parametros += modo["tiendas"]

    sql = f"""
        SELECT e.id, e.type, e.old_value, e.new_value, e.pct_change, e.created_at,
               e.store_product_id,
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
          {filtro_tienda}
          AND e.created_at >= datetime('now', '-{horas} hours')
        ORDER BY e.created_at ASC, e.id ASC
    """
    with get_connection() as conn:
        filas = [dict(f) for f in conn.execute(sql, parametros).fetchall()]

    max_pct = float(cfg.get("max_drop_pct", 70) or 0)
    solo_mejor = bool(cfg.get("back_in_stock_solo_mejor", True))

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

            # Y una bajada del 80 % tampoco: casi siempre es que el precio de
            # antes estaba mal. Pasó de verdad — al corregir un scraper que
            # tomaba el precio de un producto relacionado, sus 18 fichas se
            # arreglaron de golpe y el bot anunció cada corrección como una
            # rebaja del 90 %. Anunciar una oferta falsa es peor que callar
            # una buena: manda a alguien a una tienda a por algo que no existe.
            if max_pct and pct > max_pct:
                log("warn", "telegram",
                    f"No se anuncia «{fila.get('display_name') or fila.get('offer_name')}»: "
                    f"bajada del {pct:.0f}% ({_pesos(antes)} → {_pesos(ahora)}), "
                    f"demasiado grande para ser real")
                continue
            fila["_bajada"] = bajada

        elif fila["type"] == "back_in_stock" and solo_mejor:
            # «Volvió a haber stock» solo es noticia si además conviene
            # comprarlo ahí. Que reaparezca en la tienda más cara del mercado
            # no le sirve a nadie, y hace ruido en el grupo.
            mercado = _posicion_en_el_mercado(fila)
            if mercado is None:
                log("info", "telegram",
                    f"No se anuncia el stock de «{fila.get('display_name') or fila.get('offer_name')}»: "
                    f"no hay otras tiendas con las que compararlo")
                continue
            if not mercado["es_el_mejor"]:
                log("info", "telegram",
                    f"No se anuncia el stock de «{fila.get('display_name') or fila.get('offer_name')}»: "
                    f"{_pesos(fila.get('current_price'))} y el más barato está a "
                    f"{_pesos(mercado['mejor'])}")
                continue
            fila["_mercado"] = mercado

        salida.append(fila)
        if len(salida) >= limite:
            break
    return salida


def _posicion_en_el_mercado(fila: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Dónde queda esta oferta frente a las demás tiendas que lo tienen.

    Devuelve None si no hay con qué comparar: sin grupo, sin precio, o siendo
    la única tienda que lo vende. En esos casos no se puede afirmar nada sobre
    el mercado, y afirmarlo igual sería mentir.
    """
    precio = _numero(fila.get("current_price"))
    if precio is None or not fila.get("product_id"):
        return None

    disponibles = settings.get("stock.available_states", ["in_stock", "preorder"]) or []
    if not disponibles:
        return None

    filas = query(
        f"""SELECT sp.id, sp.price
            FROM store_products sp
            WHERE sp.product_id = ? AND sp.is_active = 1
              AND sp.price IS NOT NULL
              AND sp.stock_status IN ({','.join('?' * len(disponibles))})""",
        (fila["product_id"], *disponibles),
    )
    precios = [float(f["price"]) for f in filas]
    if len(precios) < 2:
        return None

    mas_barato = min(precios)
    return {
        "tiendas": len(precios),
        "mejor": mas_barato,
        # Con holgura de un peso: dos tiendas al mismo precio empatan, y no
        # tiene sentido que gane la que se guardó primero.
        "es_el_mejor": precio <= mas_barato + 1,
    }


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
        # El precio anterior tachado y el porcentaje entre paréntesis: se
        # entiende de un vistazo, sin necesidad de leer una línea de ahorro.
        pct = abs(evento["pct_change"] or 0)
        filas.append(
            f"Precio · {_pesos(evento['new_value'])} "
            f"(<s>{_pesos(evento['old_value'])}</s> / −{pct:.0f} %)"
        )
    else:
        # En «volvió a haber stock» y «nuevo», `new_value` no es un precio:
        # el importe se toma de la oferta tal como está ahora.
        filas.append(f"Precio · {_pesos(evento['current_price'])}")
        # Si se comprobó que es el más barato, decirlo: es la diferencia entre
        # «hay stock» y «hay stock y además es donde más conviene».
        mercado = evento.get("_mercado")
        if mercado:
            filas.append(f"El más barato de {mercado['tiendas']} tiendas")

    if url:
        filas.append(f'<a href="{html.escape(url, quote=True)}">Ver en la tienda</a>')
    return "\n".join(filas)


def componer(eventos: List[Dict[str, Any]]) -> str:
    """Un solo mensaje con todas las ofertas."""
    cabecera = config().get("header", "🔥 <b>Ofertas TCG Chile</b>")
    cuerpo = "\n\n".join(_linea(e) for e in eventos)
    return f"{cabecera}\n\n{cuerpo}"


def _linea_destacado(oportunidad: Dict[str, Any]) -> str:
    """Mismo molde que una bajada, pero comparando tiendas en vez de fechas.

    Aquí el precio tachado no es «lo que costaba antes» sino la mediana de lo
    que piden las demás tiendas: lo que pagarías sin comparar.
    """
    nombre = oportunidad["name"] or "Producto"
    idioma = oportunidad.get("language_name")
    if idioma and f"({idioma})" in nombre:
        nombre = nombre.replace(f"({idioma})", "").strip() + f" · {idioma}"

    url = oportunidad.get("best_url") or ""
    filas = [
        "💡 <b>OPORTUNIDAD</b>",
        f"<b>{html.escape(nombre)}</b>",
        f"Precio · {_pesos(oportunidad['best_price'])} "
        f"(<s>{_pesos(oportunidad['median_price'])}</s> "
        f"/ −{oportunidad['savings_pct']:.0f} %)",
        f"El más barato de {oportunidad['stores_count']} tiendas · "
        f"{html.escape(oportunidad['best_store'] or '')}",
    ]
    if url:
        filas.append(f'<a href="{html.escape(url, quote=True)}">Ver en la tienda</a>')
    return "\n".join(filas)


def destacado() -> Optional[Dict[str, Any]]:
    """Una buena oportunidad al azar, para los días sin bajadas.

    Se elige al azar entre las mejores y no entre la primera, porque si no
    saldría siempre el mismo producto: las diferencias de precio entre tiendas
    cambian mucho más despacio que las ofertas.
    """
    from app.services import queries

    cfg = config()
    minimo = float(cfg.get("destacado_min_pct", 5) or 0)
    dias = int(cfg.get("destacado_no_repetir_dias", 14) or 0)

    candidatos = [
        o for o in queries.opportunities(
            limit=int(cfg.get("destacado_candidatos", 60) or 60), sort="percent"
        )
        if o["savings_pct"] >= minimo
    ]
    if not candidatos:
        return None

    if dias > 0:
        recientes = {
            fila["product_id"] for fila in query(
                f"SELECT product_id FROM telegram_destacados "
                f"WHERE sent_at >= datetime('now', '-{dias} days')"
            )
        }
        frescos = [o for o in candidatos if o["id"] not in recientes]
        # Si ya se publicaron todos, se vuelve a empezar antes que callar.
        candidatos = frescos or candidatos

    return random.choice(candidatos)


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
async def enviar(texto: str, imagen: Optional[str] = None,
                 reintentos: int = 2) -> Dict[str, Any]:
    """Manda un mensaje. Con `imagen`, la foto va arriba y el texto debajo.

    Telegram descarga la foto él mismo desde la URL. Si no puede —enlace roto,
    formato raro, la tienda le niega el acceso— se reintenta como mensaje de
    texto: mejor una oferta sin foto que una oferta que no se publica.

    Y si contesta «vas demasiado rápido», espera lo que él mismo pide y lo
    vuelve a intentar. Mandando una tanda de diez seguidas eso pasa, y sin
    esto se perdería el resto de la tanda.
    """
    try:
        return await _enviar_una_vez(texto, imagen)
    except TelegramRechazo as exc:
        if exc.permanente or reintentos <= 0:
            raise
        await asyncio.sleep(exc.esperar or 5)
        return await enviar(texto, imagen, reintentos - 1)


async def _enviar_una_vez(texto: str, imagen: Optional[str] = None) -> Dict[str, Any]:
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
        codigo = datos.get("error_code")
        # Cuando un grupo pasa a supergrupo, Telegram le cambia el id y devuelve
        # el nuevo aquí mismo. Decirlo ahorra tener que ir a buscarlo.
        nuevo = (datos.get("parameters") or {}).get("migrate_to_chat_id")
        if nuevo:
            detalle += (
                f". El grupo cambió de identificador: pon "
                f"TELEGRAM_CHAT_ID = {nuevo} (antes {chat})"
            )
        # 429 es «vas muy rápido»: reintentar tiene sentido. El resto —HTML mal
        # formado, foto que no puede descargar, grupo que no existe— no cambia
        # por insistir, y quien llama necesita saberlo para no atascarse.
        raise TelegramRechazo(
            f"Telegram rechazó el mensaje: {detalle}",
            permanente=codigo != 429,
            esperar=(datos.get("parameters") or {}).get("retry_after"),
        )
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
        #
        # No se escribe en el registro en cada pasada: corriendo cada 10
        # minutos serían 144 líneas al día diciendo que no pasa nada. Solo se
        # apunta cuando de verdad toca hacer algo.
        return await _publicar_destacado(forzar_envio)

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
            try:
                await enviar(mensaje, evento.get("image_url") if con_imagen else None)
            except TelegramRechazo as exc:
                if not exc.permanente:
                    raise
                # Se descarta y se sigue. Antes la excepción subía y la oferta
                # se quedaba sin marcar: la siguiente pasada cogía la misma
                # —van por antigüedad—, volvía a fallar, y el grupo se quedaba
                # mudo para siempre por culpa de un solo mensaje defectuoso.
                log("warn", "telegram",
                    f"Se descarta «{evento.get('display_name') or evento.get('offer_name')}»: {exc}")
                _marcar_enviados([evento["id"]])
                continue
            _marcar_enviados([evento["id"]])

    log("info", "telegram",
        f"{len(eventos)} ofertas publicadas en {chat_id()} "
        f"({len(mensajes)} mensaje(s))")
    return {"sent": len(eventos), "dry_run": False, "preview": mensajes}


def _ahora_en_chile() -> datetime:
    """Hora local de Chile, con su cambio de horario.

    Se intenta con la base de datos de zonas horarias del sistema; si no está
    —pasa en algunos Windows— se cae a UTC−4, que es el horario de invierno.
    """
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/Santiago"))
    except Exception:  # noqa: BLE001
        return datetime.now(timezone(timedelta(hours=-4)))


def _es_momento_de_relleno() -> bool:
    """¿Toca publicar una oportunidad de relleno?

    El ritmo lo marca «cuánto hace del último mensaje», NO el reloj de pared.
    Antes se exigía además que la ejecución cayera en los primeros minutos de
    la hora, para que los rellenos salieran en punto. Fue un error: los
    horarios de GitHub llegan tarde casi siempre, y una ejecución de las 14:00
    que arranca a las 14:13 no cumplía la condición. El resultado era un grupo
    mudo durante horas.

    Lo que queda es más simple y no depende de la puntualidad de nadie: dentro
    de la franja de día, si el grupo lleva más de una hora callado y no acaba
    de arrancar un scraping, toca hablar.
    """
    cfg = config()
    ahora = _ahora_en_chile()

    franja = cfg.get("destacado_franja") or [9, 22]
    desde, hasta = int(franja[0]), int(franja[1])
    if not (desde <= ahora.hour <= hasta):
        return False

    # Con el catálogo parado, una «oportunidad» sería una comparación de
    # precios viejos presentada como si fuera de hoy. Manda a alguien a una
    # tienda a por un precio que ya no existe, que es peor que callar.
    #
    # Hace falta porque el flujo de scraping se puede pausar —está pausado
    # desde el 20-08-2026— mientras el de publicar sigue vivo para atender las
    # membresías. Sin este freno, el grupo seguiría recibiendo ofertas de un
    # catálogo congelado sin que nada lo delatara.
    caducidad = float(cfg.get("destacado_max_antiguedad_horas", 24) or 0)
    if caducidad > 0:
        fila = query_one("SELECT MAX(started_at) AS ultimo FROM scrape_runs")
        ultimo = fila["ultimo"] if fila else None
        horas = _horas_desde(ultimo)
        if horas is None or horas > caducidad:
            cuanto = f"hace {horas:.0f} h" if horas is not None else "nunca"
            print(f"[telegram] No se publica oportunidad: el catálogo está sin "
                  f"actualizar ({cuanto}; el tope son {caducidad:.0f} h). "
                  f"Los precios que compararía ya no son de fiar.", flush=True)
            return False

    espera = float(cfg.get("destacado_min_horas_entre", 1) or 0)
    if espera > 0:
        transcurridas = _horas_desde_ultimo_envio()
        if transcurridas is not None and transcurridas < espera:
            return False

    # Si hay un scraping en marcha, esperar: dentro de unos minutos puede que
    # haya ofertas de verdad que contar, y sería absurdo soltar un relleno
    # justo antes.
    minutos = int(cfg.get("destacado_esperar_scraping_min", 20) or 0)
    if minutos > 0:
        fila = query_one(
            f"SELECT COUNT(*) AS n FROM scrape_runs "
            f"WHERE started_at >= datetime('now', '-{minutos} minutes')"
        )
        if fila and fila["n"]:
            log("info", "telegram", "Relleno aplazado: hay un scraping en marcha")
            return False
    return True


def _horas_desde(valor: Any) -> Optional[float]:
    """Horas transcurridas desde una fecha de la base, o None si no se entiende.

    Las fechas se guardan como TEXTO en UTC, sin zona, en los dos motores.
    """
    if not valor:
        return None
    try:
        cuando = datetime.fromisoformat(str(valor)).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) - cuando).total_seconds() / 3600


def _horas_desde_ultimo_envio() -> Optional[float]:
    """Cuánto hace del último mensaje, sea una oferta o un destacado."""
    fila = query_one(
        """SELECT MAX(cuando) AS ultimo FROM (
               SELECT MAX(sent_at) AS cuando FROM telegram_sent
               UNION ALL
               SELECT MAX(sent_at) AS cuando FROM telegram_destacados
           ) AS todos"""
    )
    return _horas_desde(fila["ultimo"] if fila else None)


async def _publicar_destacado(forzar_envio: bool) -> Dict[str, Any]:
    """Plan B para los días sin bajadas: una buena oportunidad del comparador.

    Un grupo que pasa tres días mudo se abandona. Y esto no es relleno: es
    justo lo que hace la herramienta —ver dónde está más barato— contado sin
    que nadie tenga que entrar a mirar.
    """
    cfg = config()
    if not cfg.get("destacado_si_no_hay", True):
        log("info", "telegram", "Nada que publicar")
        return {"sent": 0, "reason": "nada nuevo que contar"}

    # El relleno saca la oportunidad del comparador entero, así que en modo
    # preventa contradiría lo que se pidió: el grupo estaría hablando de otras
    # tiendas justo los días en que solo debe hablar de una.
    modo = preventa()
    if modo:
        print(f"[telegram] Sin novedades de {', '.join(modo['tiendas'])} y en modo "
              f"preventa: no se publica oportunidad de relleno", flush=True)
        return {"sent": 0, "reason": "preventa: solo se habla de esas tiendas"}

    if not _es_momento_de_relleno():
        # Por pantalla y no al registro de la base: esto corre cada 10 minutos
        # y guardarlo llenaría `app_log` de líneas diciendo que no pasa nada.
        # En el registro de GitHub, en cambio, es justo lo que hace falta para
        # entender por qué el grupo está callado.
        transcurridas = _horas_desde_ultimo_envio()
        cuanto = f"{transcurridas:.1f} h" if transcurridas is not None else "nunca"
        print(f"[telegram] Sin ofertas y no toca relleno "
              f"(último mensaje: hace {cuanto}; "
              f"franja {cfg.get('destacado_franja')}; "
              f"hora en Chile: {_ahora_en_chile():%H:%M})", flush=True)
        return {"sent": 0, "reason": "no toca relleno"}

    oportunidad = destacado()
    if not oportunidad:
        log("info", "telegram",
            "Nada que publicar, y tampoco hay ninguna oportunidad que supere "
            f"el {cfg.get('destacado_min_pct', 5)}% de diferencia entre tiendas")
        return {"sent": 0, "reason": "nada nuevo que contar"}

    mensaje = _linea_destacado(oportunidad)
    if bool(cfg.get("dry_run", True)) and not forzar_envio:
        log("info", "telegram", f"[simulación] oportunidad destacada:\n{mensaje}")
        return {"sent": 0, "dry_run": True, "preview": [mensaje], "destacado": True}

    await enviar(mensaje, oportunidad.get("image_url") if cfg.get("include_image", True) else None)
    with transaction() as conn:
        conn.execute(
            """INSERT INTO telegram_destacados (product_id) VALUES (?)
               ON CONFLICT(product_id) DO UPDATE SET sent_at = datetime('now')""",
            (oportunidad["id"],),
        )
    log("info", "telegram",
        f"Sin bajadas: se destacó «{oportunidad['name']}» "
        f"({oportunidad['savings_pct']:.0f}% bajo la mediana de "
        f"{oportunidad['stores_count']} tiendas)")
    return {"sent": 1, "destacado": True, "preview": [mensaje]}


def _marcar_enviados(ids: List[int]) -> None:
    with transaction() as conn:
        conn.executemany(
            "INSERT INTO telegram_sent (event_id) VALUES (?) ON CONFLICT DO NOTHING",
            [(i,) for i in ids],
        )
