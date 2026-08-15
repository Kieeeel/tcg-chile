"""Acceso a la base de datos: SQLite en local, PostgreSQL en el despliegue.

Los dos motores conviven a propósito. En tu equipo sigue siendo un archivo
`data/tcg.db` que puedes copiar y abrir con cualquier visor; en el servidor,
con `DATABASE_URL` apuntando a Supabase, es Postgres. El resto del proyecto no
se entera: sigue escribiendo el mismo SQL con marcadores `?`.

Lo que traduce `_traducir()` es un conjunto pequeño y cerrado de diferencias
—las que de verdad usa este código—, no un dialecto completo.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

SCHEMA_SQLITE = Path(__file__).resolve().parent / "schema.sql"
SCHEMA_POSTGRES = Path(__file__).resolve().parent / "schema_postgres.sql"

_local = threading.local()


def database_url() -> Optional[str]:
    return (os.environ.get("DATABASE_URL") or "").strip() or None


def es_postgres() -> bool:
    url = database_url()
    return bool(url and url.startswith(("postgres://", "postgresql://")))


# ---------------------------------------------------------------------------
# Traducción de SQL
#
# Todas las consultas del proyecto están escritas en el dialecto de SQLite,
# que es el que se usa en local. Estas reglas las adaptan a Postgres.
# ---------------------------------------------------------------------------
_INTERVALO = re.compile(
    r"datetime\(\s*'now'\s*,\s*'([+-]?)\s*(\d+)\s+(second|minute|hour|day|month|year)s?'\s*\)",
    re.IGNORECASE,
)

# Las fechas se guardan como TEXT en los dos motores, con el formato de SQLite.
# Así una misma consulta compara texto con texto en ambos sitios, y lo que sale
# de la base es siempre una cadena, nunca a veces un datetime.
_AHORA = "to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')"


def _traducir(sql: str) -> str:
    if not es_postgres():
        return sql

    # datetime('now', '-7 days')  ->  to_char(now() - interval '7 days', …)
    def intervalo(m: "re.Match[str]") -> str:
        signo = "-" if m.group(1) == "-" else "+"
        desplazado = f"(now() AT TIME ZONE 'utc') {signo} interval '{m.group(2)} {m.group(3)}'"
        return f"to_char({desplazado}, 'YYYY-MM-DD HH24:MI:SS')"

    sql = _INTERVALO.sub(intervalo, sql)
    sql = re.sub(r"datetime\(\s*'now'\s*\)", _AHORA, sql, flags=re.IGNORECASE)

    # `IS NOT ?` compara con NULL en SQLite; en Postgres es IS DISTINCT FROM.
    sql = re.sub(r"\bIS\s+NOT\s+\?", "IS DISTINCT FROM ?", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bIS\s+\?", "IS NOT DISTINCT FROM ?", sql, flags=re.IGNORECASE)

    # LIKE en SQLite ignora mayúsculas; en Postgres no. ILIKE restaura el
    # comportamiento que espera el buscador.
    sql = re.sub(r"\bLIKE\b", "ILIKE", sql, flags=re.IGNORECASE)

    # Marcadores: lo último, para no tocar los `?` recién escritos.
    return sql.replace("?", "%s")


_TABLA_INSERT = re.compile(r"INSERT\s+INTO\s+([a-z_]+)", re.IGNORECASE)

# Qué tablas tienen columna `id`. Se pregunta a la base en lugar de mantener
# una lista a mano: media docena de tablas usan otra clave primaria (games,
# favorites, settings_kv…) y una lista escrita a mano se queda vieja en cuanto
# se añade una tabla nueva.
_con_id: Optional[set] = None


def _tablas_con_id(conn: Any) -> set:
    global _con_id
    if _con_id is None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_name = 'id'"
            )
            _con_id = {
                (f["table_name"] if isinstance(f, dict) else f[0]) for f in cur.fetchall()
            }
    return _con_id


class _Cursor:
    """Cursor con la misma forma que el de sqlite3.

    El proyecto usa `.lastrowid`, `.rowcount`, `.fetchone()` y `.fetchall()`;
    esto los ofrece igual sobre psycopg para no reescribir las llamadas.
    """

    def __init__(self, cur: Any, lastrowid: Optional[int] = None) -> None:
        self._cur = cur
        self.lastrowid = lastrowid

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    def fetchone(self) -> Any:
        return self._cur.fetchone()

    def fetchall(self) -> List[Any]:
        return self._cur.fetchall()

    def __iter__(self) -> Iterator[Any]:
        return iter(self._cur)


class _PgConnection:
    """Envoltorio que imita la API de sqlite3.Connection sobre psycopg."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Iterable[Any] = ()) -> _Cursor:
        # El punto y coma final estorba: detrás hay que poder pegar RETURNING.
        consulta = _traducir(sql).strip().rstrip(";")
        cur = self._conn.cursor()

        # `lastrowid` no existe en Postgres: se consigue con RETURNING id.
        ultimo = None
        tabla = _TABLA_INSERT.search(consulta)
        pedir_id = (
            tabla is not None
            and tabla.group(1).lower() in _tablas_con_id(self._conn)
            and "returning" not in consulta.lower()
        )
        try:
            if pedir_id:
                cur.execute(consulta + " RETURNING id", tuple(params))
                # Con ON CONFLICT DO NOTHING puede no devolver nada: no es error.
                fila = cur.fetchone() if cur.rowcount else None
                ultimo = (fila["id"] if isinstance(fila, dict) else fila[0]) if fila else None
            else:
                cur.execute(consulta, tuple(params))
        except Exception:
            # En Postgres una sentencia fallida aborta la transacción entera y
            # deja la conexión rechazando todo lo que venga detrás. Como las
            # conexiones se reutilizan por hilo, sin este rollback un único
            # error dejaría inservible la conexión para el resto del proceso.
            # SQLite no se comporta así; esto iguala los dos motores.
            self._conn.rollback()
            raise
        return _Cursor(cur, ultimo)

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> _Cursor:
        cur = self._conn.cursor()
        try:
            cur.executemany(_traducir(sql), [tuple(p) for p in seq])
        except Exception:
            self._conn.rollback()  # ver el comentario en execute()
            raise
        return _Cursor(cur)

    def executescript(self, sql: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(sql)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


def _db_path() -> Path:
    from app.settings import DB_PATH

    return DB_PATH


def _connect() -> Any:
    if es_postgres():
        import psycopg
        from psycopg.rows import dict_row

        # `autocommit=False`: el proyecto controla la transacción con
        # `transaction()`, igual que en SQLite.
        conn = psycopg.connect(database_url(), row_factory=dict_row, autocommit=False)
        return _PgConnection(conn)

    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def get_connection() -> Iterator[Any]:
    """Conexión por hilo. No hace commit automático: hazlo tú."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    yield conn


@contextmanager
def transaction() -> Iterator[Any]:
    """Conexión con commit/rollback automático."""
    with get_connection() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def close_thread_connection() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


def init_db() -> None:
    """Crea el esquema si no existe (idempotente en los dos motores)."""
    ruta = SCHEMA_POSTGRES if es_postgres() else SCHEMA_SQLITE
    sql = ruta.read_text(encoding="utf-8")
    with get_connection() as conn:
        conn.executescript(sql)
        conn.commit()


# ---------------------------------------------------------------------------
# Helpers de consulta
# ---------------------------------------------------------------------------
def query(sql: str, params: Iterable[Any] = ()) -> List[Any]:
    with get_connection() as conn:
        return conn.execute(sql, tuple(params)).fetchall()


def query_one(sql: str, params: Iterable[Any] = ()) -> Optional[Any]:
    with get_connection() as conn:
        return conn.execute(sql, tuple(params)).fetchone()


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with transaction() as conn:
        cur = conn.execute(sql, tuple(params))
        return cur.lastrowid


def execute_many(sql: str, seq: Iterable[Iterable[Any]]) -> None:
    with transaction() as conn:
        conn.executemany(sql, [tuple(p) for p in seq])


class Lote:
    """Acumula escrituras repetidas para mandarlas de una vez.

    Con SQLite daba igual: escribir decenas de miles de filas sueltas en un
    archivo local es instantáneo. Contra una base remota cada una es un viaje
    de ida y vuelta por la red, y a ~60 ms el viaje una actualización completa
    no cabía ni en media hora.

    Aquí se juntan por sentencia y se mandan con `executemany`, que viaja una
    sola vez. Solo sirve para escrituras cuyo resultado no haga falta en el
    momento: dar de alta una fila devuelve su id y por eso sigue siendo directa.
    """

    def __init__(self, conn: Any, tamano: int = 500) -> None:
        self._conn = conn
        self._tamano = tamano
        self._pendiente: "OrderedDict[str, List[tuple]]" = OrderedDict()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        cola = self._pendiente.setdefault(sql, [])
        cola.append(tuple(params))
        if len(cola) >= self._tamano:
            self._conn.executemany(sql, cola)
            cola.clear()

    def flush(self) -> None:
        for sql, cola in self._pendiente.items():
            if cola:
                self._conn.executemany(sql, cola)
                cola.clear()


def rows_to_dicts(rows: Iterable[Any]) -> List[Dict[str, Any]]:
    return [dict(row) for row in rows]


def log(level: str, scope: str, message: str) -> None:
    """Escribe en app_log (alimenta la consola de la interfaz) y por pantalla.

    Lo de imprimir importa cuando esto corre desatendido en GitHub Actions: si
    los mensajes solo van a la base, el registro del flujo se queda mudo
    durante toda la actualización y no hay forma de saber en qué punto está ni
    dónde se atasca.
    """
    marca = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{marca}] {level:5} {scope:14} {message}", flush=True)
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO app_log (level, scope, message) VALUES (?, ?, ?)",
                (level, scope, message),
            )
    except Exception:
        pass  # el logging nunca debe romper el pipeline
