"""Membresías de pago del grupo de Telegram.

El reparto de tareas es deliberado: **el dinero y la decisión de dejar entrar
son tuyos; el bot solo lleva la cuenta y hace cumplir el plazo.**

    alguien paga  ->  tú apruebas su solicitud en Telegram (un toque)
                  ->  el bot lo ve en la siguiente pasada y le pone fecha
                  ->  avisa cuando quedan pocos días
                  ->  expulsa al vencer

Así nada urgente depende del bot. Lo único que tiene que ser inmediato —dejar
entrar a quien acaba de transferir— lo haces tú en la aplicación de siempre.
Que el bot tarde hasta 4 horas en anotar la fecha da igual: ya está dentro.

Las órdenes se dan por chat privado con el bot, nunca en el grupo, y solo las
obedece de TELEGRAM_ADMIN_ID.

Requisitos en Telegram:
  · El bot tiene que ser administrador del grupo con permiso para expulsar.
  · El enlace de invitación debe exigir aprobación, o cualquiera que reciba
    el enlace entra sin pagar y sin pasar por aquí.
"""
from __future__ import annotations

import html
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from app import settings
from app.db.database import get_connection, log, query, transaction
from app.services.notify import API, chat_id, token

# Dónde se recuerda por qué actualización de Telegram íbamos. Sin esto, cada
# pasada del cron reprocesaría las mismas altas y órdenes.
CLAVE_OFFSET = "membresia.ultimo_update_id"

# Telegram NO envía los avisos de entradas y salidas salvo que se pidan
# explícitamente. Es el fallo típico de este montaje: sin esta lista el bot
# no se entera de que alguien entró.
TIPOS = ["message", "chat_member", "chat_join_request"]

AYUDA = """<b>Órdenes de administración</b>

/socios — quién está y cuándo vence
/alta &lt;id&gt; [días o fecha]
/renovar &lt;id&gt; [días o fecha]
/baja &lt;id&gt; — expulsar ahora
/ayuda — esto

El plazo admite las dos formas:
<code>/alta 123456789 31</code> — 31 días desde hoy
<code>/alta 123456789 30/09/2026</code> — hasta esa fecha

Con fecha, ese día entero cuenta: sale de madrugada.
Con días, si renueva antes de vencer, se suman a lo que le quedaba."""


def config() -> Dict[str, Any]:
    return dict(settings.get("membresia", {}) or {})


def admin_id() -> Optional[int]:
    """Quién puede dar órdenes. Uno solo, y viene del entorno."""
    valor = (os.environ.get("TELEGRAM_ADMIN_ID") or "").strip()
    return int(valor) if valor.lstrip("-").isdigit() else None


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _texto_fecha(momento: datetime) -> str:
    """Mismo formato que usa el resto de la base: texto UTC, ancho fijo."""
    return momento.strftime("%Y-%m-%d %H:%M:%S")


def _leer_fecha(valor: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(valor)).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _dias_por_defecto() -> int:
    return int(config().get("dias", 31) or 31)


def fecha_local(vence_at: Any) -> str:
    """La fecha de vencimiento como la lee una persona en Chile.

    Por dentro todo se guarda en UTC, y un vencimiento de las 03:59 UTC es en
    realidad la noche del día anterior en Chile. Mostrar el texto crudo hacía
    que al poner «30/09» el mensaje contestara «hasta el 2026-10-01».
    """
    momento = _leer_fecha(vence_at)
    if momento is None:
        return str(vence_at)[:10]
    return momento.astimezone(_zona_chile()).strftime("%d/%m/%Y")


_FORMATOS_FECHA = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%m-%y", "%d/%m/%y")

# Chile va cuatro horas por detrás de UTC en invierno y tres en verano.
HORAS_CHILE = 4


def _zona_chile():
    """La zona horaria de Chile, con su cambio de hora si el sistema lo sabe."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/Santiago")
    except Exception:  # noqa: BLE001
        return timezone(timedelta(hours=-HORAS_CHILE))


def parsear_plazo(texto: str) -> Optional[datetime]:
    """Convierte «31» o «2026-09-30» o «30/09/2026» en un vencimiento.

    Un número son días a partir de ahora; una fecha es el último día de
    acceso, incluido: quien pone el 30 de septiembre sigue dentro todo ese
    día y sale de madrugada.

    El final del día se construye EN la zona de Chile y luego se pasa a UTC.
    Sumar un desfase fijo no vale: entre septiembre y abril Chile está a tres
    horas y no a cuatro, y el vencimiento se colaba una hora en el día
    siguiente, con lo que la fecha mostrada salía cambiada.
    """
    texto = texto.strip()
    if texto.isdigit():
        return _ahora() + timedelta(days=int(texto))

    for formato in _FORMATOS_FECHA:
        try:
            dia = datetime.strptime(texto, formato)
        except ValueError:
            continue
        fin = dia.replace(hour=23, minute=59, second=59, tzinfo=_zona_chile())
        return fin.astimezone(timezone.utc)
    return None


def _dias_hasta(vence: datetime, ahora: datetime) -> int:
    """Días que faltan, redondeando hacia arriba.

    `timedelta.days` trunca: a falta de 2 días y 23 horas diría «2», y el
    aviso quedaría corto. Para un plazo que se cuenta en días, lo que la
    persona entiende por «faltan 3» es esto.
    """
    segundos = (vence - ahora).total_seconds()
    if segundos <= 0:
        return 0
    return int(-(-segundos // 86400))


# ---------------------------------------------------------------------------
# Llamadas a Telegram
# ---------------------------------------------------------------------------
async def _llamar(metodo: str, datos: Dict[str, Any]) -> Dict[str, Any]:
    clave = token()
    if not clave:
        raise RuntimeError("Falta la variable de entorno TELEGRAM_BOT_TOKEN")
    async with httpx.AsyncClient(timeout=30) as cliente:
        respuesta = await cliente.post(f"{API}/bot{clave}/{metodo}", json=datos)
    return respuesta.json()


async def _privado(user_id: int, texto: str) -> bool:
    """Mensaje privado. Falla si esa persona nunca le habló al bot.

    Telegram no deja que un bot escriba primero: si alguien no ha pulsado
    «Iniciar» en el chat con el bot, no hay forma de avisarle en privado. Por
    eso esto devuelve si lo consiguió, y quien llama decide qué hacer.
    """
    datos = await _llamar("sendMessage", {
        "chat_id": user_id, "text": texto, "parse_mode": "HTML",
    })
    return bool(datos.get("ok"))


async def expulsar(user_id: int) -> bool:
    """Echa del grupo sin vetar para siempre.

    `banChatMember` sacaría a la persona y le impediría volver; el
    `unbanChatMember` de después levanta el veto en el acto. La combinación
    es la forma de decir «sal, pero puedes volver cuando renueves».
    """
    grupo = chat_id()
    fuera = await _llamar("banChatMember", {"chat_id": grupo, "user_id": user_id})
    if not fuera.get("ok"):
        log("warn", "membresia",
            f"No se pudo expulsar a {user_id}: {fuera.get('description')}")
        return False
    await _llamar("unbanChatMember", {
        "chat_id": grupo, "user_id": user_id, "only_if_banned": True,
    })
    return True


# ---------------------------------------------------------------------------
# La tabla
# ---------------------------------------------------------------------------
def socio(user_id: int) -> Optional[Dict[str, Any]]:
    filas = query("SELECT * FROM miembros WHERE user_id = ?", (user_id,))
    return dict(filas[0]) if filas else None


def socios(estado: Optional[str] = "activo") -> List[Dict[str, Any]]:
    if estado:
        filas = query(
            "SELECT * FROM miembros WHERE estado = ? ORDER BY vence_at", (estado,)
        )
    else:
        filas = query("SELECT * FROM miembros ORDER BY estado, vence_at")
    return [dict(f) for f in filas]


def dar_alta(user_id: int, dias: Optional[int] = None, *,
             hasta: Optional[datetime] = None,
             nombre: Optional[str] = None, usuario: Optional[str] = None,
             nota: Optional[str] = None) -> Dict[str, Any]:
    """Alta nueva o renovación.

    Con `hasta` la fecha manda tal cual, sin sumas: es lo que quieres cuando
    dices «este entra hasta el 30 de septiembre».

    Con `dias`, en cambio, se SUMAN a lo que quedaba: a quien renueva con una
    semana por delante no se le regalan ni se le quitan esos días.
    """
    existente = socio(user_id)
    ahora = _ahora()

    if hasta is not None:
        nuevo_vence = _texto_fecha(hasta)
        dias = max(0, _dias_hasta(hasta, ahora))
    else:
        dias = dias or _dias_por_defecto()
        desde = ahora
        if existente:
            vence = _leer_fecha(existente["vence_at"])
            if vence and vence > ahora and existente["estado"] == "activo":
                desde = vence
        nuevo_vence = _texto_fecha(desde + timedelta(days=dias))
    with transaction() as conn:
        conn.execute(
            """INSERT INTO miembros (user_id, nombre, usuario, vence_at, estado,
                                     ultimo_aviso, nota, updated_at)
               VALUES (?, ?, ?, ?, 'activo', NULL, ?, datetime('now'))
               ON CONFLICT(user_id) DO UPDATE SET
                   nombre = COALESCE(excluded.nombre, miembros.nombre),
                   usuario = COALESCE(excluded.usuario, miembros.usuario),
                   vence_at = excluded.vence_at,
                   estado = 'activo',
                   ultimo_aviso = NULL,
                   nota = COALESCE(excluded.nota, miembros.nota),
                   updated_at = datetime('now')""",
            (user_id, nombre, usuario, nuevo_vence, nota),
        )
    return {"user_id": user_id, "vence_at": nuevo_vence, "dias": dias,
            "renovacion": existente is not None}


def marcar(user_id: int, estado: str) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE miembros SET estado = ?, updated_at = datetime('now') "
            "WHERE user_id = ?",
            (estado, user_id),
        )


# ---------------------------------------------------------------------------
# Recoger lo que pasó desde la última pasada
# ---------------------------------------------------------------------------
async def recoger_novedades() -> Dict[str, Any]:
    """Lee las novedades de Telegram: quién entró, quién salió, qué órdenes hay.

    Telegram guarda las novedades 24 horas. Con el cron cada 4 horas sobra,
    pero si el flujo estuviera caído un día entero se perderían altas; por eso
    `revisar_vencimientos` no se fía solo de esto.
    """
    desde = settings.get(CLAVE_OFFSET, 0) or 0
    datos = await _llamar("getUpdates", {
        "offset": desde + 1 if desde else 0,
        "timeout": 0,
        "allowed_updates": TIPOS,
    })
    if not datos.get("ok"):
        log("warn", "membresia", f"getUpdates falló: {datos.get('description')}")
        return {"altas": 0, "ordenes": 0}

    novedades = datos.get("result") or []
    altas = ordenes = 0
    ultimo = desde

    for novedad in novedades:
        ultimo = max(ultimo, int(novedad.get("update_id", 0)))

        # Alguien entró o salió del grupo
        cambio = novedad.get("chat_member")
        if cambio and str(cambio.get("chat", {}).get("id")) == str(chat_id()):
            if await _procesar_cambio(cambio):
                altas += 1
            continue

        # Orden por privado del administrador
        mensaje = novedad.get("message") or {}
        texto = (mensaje.get("text") or "").strip()
        if not texto.startswith("/"):
            continue
        if mensaje.get("chat", {}).get("type") != "private":
            continue  # en el grupo no se obedecen órdenes, a propósito
        if mensaje.get("from", {}).get("id") != admin_id():
            continue
        await _obedecer(texto)
        ordenes += 1

    if ultimo != desde:
        settings.save_override(CLAVE_OFFSET, ultimo)
    return {"altas": altas, "ordenes": ordenes, "novedades": len(novedades)}


async def _procesar_cambio(cambio: Dict[str, Any]) -> bool:
    """Da de alta a quien acaba de entrar; marca de baja a quien se fue."""
    nuevo = (cambio.get("new_chat_member") or {}).get("status")
    persona = (cambio.get("new_chat_member") or {}).get("user") or {}
    user_id = persona.get("id")
    if not user_id or persona.get("is_bot"):
        return False
    if user_id == admin_id():
        # El dueño del grupo no es un socio. Si se le diera plazo, al vencer
        # el bot intentaría expulsarlo, Telegram lo impediría —a un creador no
        # se le puede echar— y quedaría marcado como expulsado sin estarlo.
        return False

    if nuevo in ("member", "administrator", "creator"):
        existente = socio(user_id)
        if existente and existente["estado"] == "activo":
            return False  # ya lo teníamos: no reinicia el plazo
        nombre = " ".join(
            p for p in (persona.get("first_name"), persona.get("last_name")) if p
        )
        alta = dar_alta(
            user_id, nombre=nombre or None, usuario=persona.get("username"),
            nota="entró al grupo",
        )
        log("info", "membresia",
            f"Alta: {nombre or user_id} — vence el {fecha_local(alta['vence_at'])}")
        await _privado(user_id, _bienvenida(alta["vence_at"]))

        # Y te lo cuenta a ti, con el identificador escrito para copiar. Es la
        # forma cómoda de tenerlo: buscar el id de alguien en Telegram obliga a
        # recurrir a otro bot o a rebuscar en la ficha.
        alias = f" (@{persona['username']})" if persona.get("username") else ""
        await _avisar_admin(
            f"👤 <b>Entró al grupo</b>\n"
            f"{html.escape(nombre or str(user_id))}{html.escape(alias)}\n"
            f"Vence el {fecha_local(alta['vence_at'])} "
            f"({_dias_por_defecto()} días)\n\n"
            f"Su identificador es <code>{user_id}</code>\n"
            f"<code>/renovar {user_id} 31</code>\n"
            f"<code>/baja {user_id}</code>"
        )
        return True

    if nuevo in ("left", "kicked"):
        if socio(user_id):
            marcar(user_id, "baja")
            log("info", "membresia", f"{user_id} salió del grupo")
            await _avisar_admin(
                f"🚪 <b>Salió del grupo</b>\n"
                f"{html.escape(persona.get('first_name') or str(user_id))} "
                f"(<code>{user_id}</code>)"
            )
    return False


async def _avisar_admin(texto: str) -> bool:
    destino = admin_id()
    return await _privado(destino, texto) if destino else False


def _bienvenida(vence_at: str) -> str:
    cfg = config()
    return (
        f"{cfg.get('bienvenida', '¡Bienvenido al grupo!')}\n\n"
        f"Tu acceso está activo hasta el <b>{fecha_local(vence_at)}</b>.\n"
        f"Te aviso unos días antes de que venza."
    )


# ---------------------------------------------------------------------------
# Órdenes
# ---------------------------------------------------------------------------
async def _obedecer(texto: str) -> None:
    partes = texto.split()
    orden = partes[0].lower().split("@")[0]
    destino = admin_id()

    async def responder(mensaje: str) -> None:
        if destino:
            await _privado(destino, mensaje)

    if orden == "/ayuda" or orden == "/start":
        await responder(AYUDA)

    elif orden == "/socios":
        activos = socios("activo")
        if not activos:
            await responder("No hay socios activos.")
            return
        ahora = _ahora()
        lineas = ["<b>Socios activos</b>", ""]
        for s in activos:
            vence = _leer_fecha(s["vence_at"])
            quedan = (vence - ahora).days if vence else "?"
            quien = s["nombre"] or (f"@{s['usuario']}" if s["usuario"] else s["user_id"])
            lineas.append(f"{quien} — {fecha_local(s['vence_at'])} ({quedan} días)")
            lineas.append(f"<code>/renovar {s['user_id']}</code>")
            lineas.append("")
        await responder("\n".join(lineas))

    elif orden in ("/alta", "/renovar"):
        if len(partes) < 2 or not partes[1].isdigit():
            await responder(
                f"Uso: <code>{orden} &lt;id&gt; [días o fecha]</code>\n"
                f"Ejemplos:\n"
                f"<code>{orden} 123456789</code> — {_dias_por_defecto()} días\n"
                f"<code>{orden} 123456789 31</code> — 31 días más\n"
                f"<code>{orden} 123456789 30/09/2026</code> — hasta esa fecha"
            )
            return

        user_id = int(partes[1])
        hasta = dias = None
        if len(partes) > 2:
            plazo = parsear_plazo(partes[2])
            if plazo is None:
                await responder(
                    f"No entiendo «{partes[2]}». Pon un número de días (31) "
                    f"o una fecha (30/09/2026)."
                )
                return
            if partes[2].isdigit():
                dias = int(partes[2])
            else:
                hasta = plazo

        alta = dar_alta(user_id, dias, hasta=hasta, nota="alta manual")
        detalle = (
            f"hasta el {fecha_local(alta['vence_at'])}"
            if hasta is not None
            else f"+{alta['dias']} días, hasta el {fecha_local(alta['vence_at'])}"
        )
        await responder(
            f"{'Renovado' if alta['renovacion'] else 'Dado de alta'} "
            f"{user_id}: {detalle}."
        )
        await _privado(user_id, _bienvenida(alta["vence_at"]))

    elif orden == "/baja":
        if len(partes) < 2 or not partes[1].isdigit():
            await responder("Uso: <code>/baja &lt;id&gt;</code>")
            return
        user_id = int(partes[1])
        fuera = await expulsar(user_id)
        marcar(user_id, "expulsado")
        await responder(f"{user_id} {'expulsado' if fuera else 'marcado de baja (no se pudo expulsar)'}.")

    else:
        await responder(f"No conozco «{orden}».\n\n{AYUDA}")


# ---------------------------------------------------------------------------
# Vencimientos
# ---------------------------------------------------------------------------
async def revisar_vencimientos() -> Dict[str, Any]:
    """Avisa a quien está por vencer y expulsa a quien ya venció."""
    cfg = config()
    avisos_en = sorted((int(d) for d in (cfg.get("avisos_dias") or [3, 1])), reverse=True)
    ahora = _ahora()
    avisados = expulsados = 0

    for s in socios("activo"):
        if s["user_id"] == admin_id():
            continue  # al dueño del grupo no se le echa
        vence = _leer_fecha(s["vence_at"])
        if vence is None:
            continue

        if vence <= ahora:
            if await expulsar(s["user_id"]):
                expulsados += 1
            marcar(s["user_id"], "expulsado")
            await _privado(s["user_id"], _despedida(s))
            log("info", "membresia",
                f"Vencido: {s['nombre'] or s['user_id']} (venció el {fecha_local(s['vence_at'])})")
            continue

        quedan = _dias_hasta(vence, ahora)
        # El escalón MÁS PEQUEÑO que todavía cubre lo que queda. Con avisos a
        # 3 y 1 día, a quien le quedan cero días le toca el de 1, no el de 3:
        # si se anotara el de 3 nunca recibiría el segundo recordatorio.
        candidatos = [d for d in avisos_en if quedan <= d]
        toca = min(candidatos) if candidatos else None
        if toca is None or (s["ultimo_aviso"] is not None and s["ultimo_aviso"] <= toca):
            continue
        if await _privado(s["user_id"], _recordatorio(s, quedan)):
            avisados += 1
        with transaction() as conn:
            conn.execute(
                "UPDATE miembros SET ultimo_aviso = ?, updated_at = datetime('now') "
                "WHERE user_id = ?",
                (toca, s["user_id"]),
            )

    return {"avisados": avisados, "expulsados": expulsados}


def _recordatorio(s: Dict[str, Any], quedan: int) -> str:
    cfg = config()
    cuando = "hoy" if quedan <= 0 else ("mañana" if quedan == 1 else f"en {quedan} días")
    return (
        f"Tu acceso al grupo vence <b>{cuando}</b> "
        f"({fecha_local(s['vence_at'])}).\n\n"
        f"{cfg.get('renovacion', 'Escríbeme para renovar.')}"
    )


def _despedida(s: Dict[str, Any]) -> str:
    cfg = config()
    return (
        "Tu acceso al grupo ha vencido y he tenido que sacarte.\n\n"
        f"{cfg.get('renovacion', 'Escríbeme para renovar.')}\n"
        "Al renovar vuelves a entrar sin problema."
    )


# ---------------------------------------------------------------------------
# Punto de entrada del cron
# ---------------------------------------------------------------------------
async def ejecutar() -> Dict[str, Any]:
    cfg = config()
    if not cfg.get("enabled"):
        return {"activo": False}
    if not token() or not chat_id():
        log("warn", "membresia", "Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID")
        return {"activo": False}
    if not admin_id():
        log("warn", "membresia",
            "Falta TELEGRAM_ADMIN_ID: no se obedecerán órdenes, solo se "
            "controlarán los vencimientos")

    novedades = await recoger_novedades()
    vencimientos = await revisar_vencimientos()
    log("info", "membresia",
        f"{len(socios('activo'))} socios activos · {novedades['altas']} altas · "
        f"{novedades['ordenes']} órdenes · {vencimientos['avisados']} avisados · "
        f"{vencimientos['expulsados']} expulsados")
    return {"activo": True, **novedades, **vencimientos}
