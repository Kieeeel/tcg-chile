-- Esquema para PostgreSQL (Supabase). Generado a partir de schema.sql.
--
-- Dos decisiones deliberadas, las dos por el mismo motivo: que una consulta
-- escrita una vez dé el mismo resultado en los dos motores.
--
--   · Los booleanos siguen siendo enteros 0/1, así las 166 comparaciones
--     `= 1` que hay en el proyecto valen igual aquí.
--   · Las fechas siguen siendo TEXT con el formato de SQLite
--     ('YYYY-MM-DD HH:MM:SS' en UTC), no TIMESTAMPTZ. Con TIMESTAMPTZ,
--     Postgres devolvería objetos datetime donde SQLite devuelve cadenas, y
--     además rechazaría escribir now() en una columna de texto. Como el
--     formato es de ancho fijo, ordenar y comparar por texto sigue siendo
--     ordenar y comparar cronológicamente.

-- =====================================================================
-- Esquema de TCG Comparative
-- =====================================================================
-- (En SQLite aquí van dos PRAGMA —journal_mode y foreign_keys— que en
-- Postgres no existen ni hacen falta: WAL y las claves foráneas ya están
-- activos de serie.)

-- ---------------------------------------------------------------------
-- Configuración persistida (sobrescribe config/settings.yaml en runtime)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings_kv (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'))
);

-- ---------------------------------------------------------------------
-- Catálogos
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS games (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sets (
    id          BIGSERIAL PRIMARY KEY,
    game        TEXT NOT NULL,
    code        TEXT NOT NULL,
    name        TEXT NOT NULL,
    released    TEXT,
    UNIQUE (game, code)
);
CREATE INDEX IF NOT EXISTS idx_sets_game ON sets(game);

-- ---------------------------------------------------------------------
-- Tiendas
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stores (
    id              BIGSERIAL PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    base_url        TEXT NOT NULL,
    adapter         TEXT NOT NULL,
    config_file     TEXT,
    currency        TEXT NOT NULL DEFAULT 'CLP',
    enabled         INTEGER NOT NULL DEFAULT 1,
    country         TEXT,
    logo_url        TEXT,
    created_at      TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')),
    updated_at      TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'))
);

CREATE TABLE IF NOT EXISTS categories (
    id          BIGSERIAL PRIMARY KEY,
    store_id    INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    url         TEXT NOT NULL,
    game        TEXT,
    last_seen_at TEXT,
    UNIQUE (store_id, url)
);

-- ---------------------------------------------------------------------
-- Producto maestro (agrupación entre tiendas)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS products (
    id                      BIGSERIAL PRIMARY KEY,
    canonical_name          TEXT NOT NULL,
    display_name            TEXT NOT NULL,
    normalized_name         TEXT NOT NULL,
    game                    TEXT,
    set_code                TEXT,
    set_name                TEXT,
    product_type            TEXT,
    product_type_name       TEXT,
    quantity                INTEGER,
    units_total             INTEGER,
    unit_name               TEXT,
    language                TEXT,
    image_url               TEXT,
    -- Campos denormalizados para el dashboard y los listados.
    offers_count            INTEGER NOT NULL DEFAULT 0,
    stores_count            INTEGER NOT NULL DEFAULT 0,
    in_stock_count          INTEGER NOT NULL DEFAULT 0,
    best_price              DOUBLE PRECISION,
    best_store_id           INTEGER,
    best_available_price    DOUBLE PRECISION,
    best_available_store_id INTEGER,
    worst_price             DOUBLE PRECISION,
    avg_price               DOUBLE PRECISION,
    last_scraped_at         TEXT,
    created_at              TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')),
    updated_at              TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'))
);
CREATE INDEX IF NOT EXISTS idx_products_game ON products(game);
CREATE INDEX IF NOT EXISTS idx_products_set ON products(set_code);
CREATE INDEX IF NOT EXISTS idx_products_type ON products(product_type);
CREATE INDEX IF NOT EXISTS idx_products_best_price ON products(best_available_price);
CREATE INDEX IF NOT EXISTS idx_products_norm ON products(normalized_name);

-- ---------------------------------------------------------------------
-- Producto tal como lo publica cada tienda (una fila por oferta)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS store_products (
    id                  BIGSERIAL PRIMARY KEY,
    store_id            INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    external_id         TEXT,
    url                 TEXT NOT NULL,
    name                TEXT NOT NULL,
    normalized_name     TEXT NOT NULL DEFAULT '',
    name_key            TEXT NOT NULL DEFAULT '',
    tokens              TEXT NOT NULL DEFAULT '[]',   -- JSON
    description         TEXT,
    image_url           TEXT,
    category            TEXT,
    price               DOUBLE PRECISION,
    price_raw           TEXT,
    currency            TEXT NOT NULL DEFAULT 'CLP',
    stock_status        TEXT NOT NULL DEFAULT 'unknown',  -- in_stock|out_of_stock|preorder|coming_soon|unknown
    stock_raw           TEXT,
    sku                 TEXT,
    mpn                 TEXT,
    upc                 TEXT,
    ean                 TEXT,
    gtin                TEXT,
    brand               TEXT,
    -- Atributos extraídos por el normalizador
    game                TEXT,
    set_code            TEXT,
    product_type        TEXT,
    quantity            INTEGER,
    quantity_confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
    units_total         INTEGER,
    language            TEXT,
    -- Agrupación
    product_id          INTEGER REFERENCES products(id) ON DELETE SET NULL,
    match_score         DOUBLE PRECISION,
    match_method        TEXT,
    -- Control de cambios
    content_hash        TEXT,
    first_seen_at       TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')),
    last_seen_at        TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')),
    last_changed_at     TEXT,
    last_price_change_at TEXT,
    is_active           INTEGER NOT NULL DEFAULT 1,
    UNIQUE (store_id, url)
);
CREATE INDEX IF NOT EXISTS idx_sp_store ON store_products(store_id);
CREATE INDEX IF NOT EXISTS idx_sp_product ON store_products(product_id);
CREATE INDEX IF NOT EXISTS idx_sp_active ON store_products(is_active);
CREATE INDEX IF NOT EXISTS idx_sp_namekey ON store_products(name_key);
CREATE INDEX IF NOT EXISTS idx_sp_set_type ON store_products(game, set_code, product_type);
CREATE INDEX IF NOT EXISTS idx_sp_extid ON store_products(store_id, external_id);

-- ---------------------------------------------------------------------
-- Identificadores objetivos (UPC/EAN/GTIN/SKU/MPN)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product_identifiers (
    id                  BIGSERIAL PRIMARY KEY,
    store_product_id    INTEGER NOT NULL REFERENCES store_products(id) ON DELETE CASCADE,
    kind                TEXT NOT NULL,   -- upc|ean|gtin|mpn|sku|external_id
    value               TEXT NOT NULL,
    normalized_value    TEXT NOT NULL,
    priority            INTEGER NOT NULL DEFAULT 100,
    UNIQUE (store_product_id, kind, normalized_value)
);
CREATE INDEX IF NOT EXISTS idx_ident_value ON product_identifiers(normalized_value);
CREATE INDEX IF NOT EXISTS idx_ident_kind ON product_identifiers(kind, normalized_value);

-- ---------------------------------------------------------------------
-- Atributos extraídos (trazabilidad de por qué se clasificó así)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product_attributes (
    id              BIGSERIAL PRIMARY KEY,
    entity_type     TEXT NOT NULL,   -- store_product | product
    entity_id       INTEGER NOT NULL,
    key             TEXT NOT NULL,
    value           TEXT,
    confidence      DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    source          TEXT,            -- name | description | catalog | default
    UNIQUE (entity_type, entity_id, key)
);
CREATE INDEX IF NOT EXISTS idx_attr_entity ON product_attributes(entity_type, entity_id);

-- ---------------------------------------------------------------------
-- Decisión de agrupación por oferta (por qué quedó en ese maestro)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product_matches (
    id                  BIGSERIAL PRIMARY KEY,
    product_id          INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    store_product_id    INTEGER NOT NULL REFERENCES store_products(id) ON DELETE CASCADE,
    score               DOUBLE PRECISION NOT NULL DEFAULT 0,
    method              TEXT NOT NULL,   -- EAN_MATCH|SKU_MATCH|NAME_MATCH|ATTRIBUTE_MATCH|FUZZY_MATCH|MANUAL_MATCH|SINGLETON
    breakdown           TEXT,            -- JSON con el detalle del puntaje
    anchor_id           INTEGER,         -- oferta con la que hizo match
    created_at          TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')),
    UNIQUE (store_product_id)
);
CREATE INDEX IF NOT EXISTS idx_pm_product ON product_matches(product_id);

-- ---------------------------------------------------------------------
-- Cola de revisión manual (pares dudosos: 75..89)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS match_reviews (
    id              BIGSERIAL PRIMARY KEY,
    a_id            INTEGER NOT NULL REFERENCES store_products(id) ON DELETE CASCADE,
    b_id            INTEGER NOT NULL REFERENCES store_products(id) ON DELETE CASCADE,
    score           DOUBLE PRECISION NOT NULL,
    method          TEXT NOT NULL,
    breakdown       TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending|confirmed|rejected
    created_at      TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')),
    decided_at      TEXT,
    UNIQUE (a_id, b_id)
);
CREATE INDEX IF NOT EXISTS idx_reviews_status ON match_reviews(status, score DESC);

-- ---------------------------------------------------------------------
-- Decisiones humanas permanentes (se respetan en cada re-agrupación)
-- La pareja se guarda con a_key < b_key para evitar duplicados.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS manual_matches (
    id          BIGSERIAL PRIMARY KEY,
    a_key       TEXT NOT NULL,   -- store_code::external_key estable
    b_key       TEXT NOT NULL,
    decision    TEXT NOT NULL,   -- same | different
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')),
    UNIQUE (a_key, b_key)
);
CREATE INDEX IF NOT EXISTS idx_manual_decision ON manual_matches(decision);

-- ---------------------------------------------------------------------
-- Atributos corregidos a mano.
--
-- Los atributos se vuelven a extraer del nombre en cada actualización, así
-- que una corrección manual se perdería. Aquí se guarda contra la misma
-- clave estable que usan las decisiones de agrupación y se vuelve a aplicar
-- después de cada scraping.
--
-- Caso típico: la tienda no escribe el idioma en el título y el usuario
-- sabe, mirando la ficha, que ese producto es el español.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS manual_attributes (
    id          BIGSERIAL PRIMARY KEY,
    entity_key  TEXT NOT NULL,   -- store_code::external_id
    attribute   TEXT NOT NULL,   -- language | set_code | product_type
    value       TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')),
    UNIQUE (entity_key, attribute)
);
CREATE INDEX IF NOT EXISTS idx_manual_attr ON manual_attributes(attribute);

-- ---------------------------------------------------------------------
-- Historial
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS price_history (
    id                  BIGSERIAL PRIMARY KEY,
    store_product_id    INTEGER NOT NULL REFERENCES store_products(id) ON DELETE CASCADE,
    price               DOUBLE PRECISION NOT NULL,
    currency            TEXT NOT NULL DEFAULT 'CLP',
    recorded_at         TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'))
);
CREATE INDEX IF NOT EXISTS idx_ph_sp_time ON price_history(store_product_id, recorded_at DESC);

CREATE TABLE IF NOT EXISTS stock_history (
    id                  BIGSERIAL PRIMARY KEY,
    store_product_id    INTEGER NOT NULL REFERENCES store_products(id) ON DELETE CASCADE,
    stock_status        TEXT NOT NULL,
    recorded_at         TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'))
);
CREATE INDEX IF NOT EXISTS idx_sh_sp_time ON stock_history(store_product_id, recorded_at DESC);

-- ---------------------------------------------------------------------
-- Detección de cambios / feed de eventos
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id                  BIGSERIAL PRIMARY KEY,
    type                TEXT NOT NULL,  -- price_drop|price_rise|new_product|removed_product|
                                        -- back_in_stock|out_of_stock|name_change|new_match
    store_id            INTEGER,
    store_product_id    INTEGER,
    product_id          INTEGER,
    old_value           TEXT,
    new_value           TEXT,
    pct_change          DOUBLE PRECISION,
    message             TEXT,
    created_at          TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'))
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type, created_at DESC);

-- ---------------------------------------------------------------------
-- Ejecuciones de scraping y sus errores
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scrape_runs (
    id                  BIGSERIAL PRIMARY KEY,
    store_id            INTEGER REFERENCES stores(id) ON DELETE CASCADE,
    trigger             TEXT NOT NULL DEFAULT 'manual',  -- manual|scheduled|startup
    status              TEXT NOT NULL DEFAULT 'running', -- running|ok|partial|error
    started_at          TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')),
    finished_at         TEXT,
    duration_ms         INTEGER,
    products_found      INTEGER NOT NULL DEFAULT 0,
    products_new        INTEGER NOT NULL DEFAULT 0,
    products_updated    INTEGER NOT NULL DEFAULT 0,
    products_removed    INTEGER NOT NULL DEFAULT 0,
    requests_made       INTEGER NOT NULL DEFAULT 0,
    requests_cached     INTEGER NOT NULL DEFAULT 0,
    error_count         INTEGER NOT NULL DEFAULT 0,
    message             TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_store ON scrape_runs(store_id, started_at DESC);

CREATE TABLE IF NOT EXISTS scrape_errors (
    id          BIGSERIAL PRIMARY KEY,
    run_id      INTEGER REFERENCES scrape_runs(id) ON DELETE CASCADE,
    store_id    INTEGER REFERENCES stores(id) ON DELETE CASCADE,
    stage       TEXT NOT NULL,   -- categories|listing|product|parse|network
    url         TEXT,
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'))
);
CREATE INDEX IF NOT EXISTS idx_errors_store ON scrape_errors(store_id, created_at DESC);

-- Log unificado que alimenta la consola de la interfaz.
CREATE TABLE IF NOT EXISTS app_log (
    id          BIGSERIAL PRIMARY KEY,
    level       TEXT NOT NULL DEFAULT 'info',  -- debug|info|warn|error
    scope       TEXT,
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'))
);
CREATE INDEX IF NOT EXISTS idx_log_time ON app_log(created_at DESC);

-- ---------------------------------------------------------------------
-- Caché HTTP condicional (ETag / Last-Modified)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS http_cache (
    url             TEXT PRIMARY KEY,
    etag            TEXT,
    last_modified   TEXT,
    content_hash    TEXT,
    status_code     INTEGER,
    fetched_at      TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'))
);

-- ---------------------------------------------------------------------
-- Favoritos y alertas
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS favorites (
    product_id  INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'))
);

CREATE TABLE IF NOT EXISTS alerts (
    id                  BIGSERIAL PRIMARY KEY,
    product_id          INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    target_price        DOUBLE PRECISION NOT NULL,
    only_in_stock       INTEGER NOT NULL DEFAULT 1,
    active              INTEGER NOT NULL DEFAULT 1,
    last_triggered_at   TEXT,
    last_triggered_price DOUBLE PRECISION,
    created_at          TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'))
);
CREATE INDEX IF NOT EXISTS idx_alerts_product ON alerts(product_id);

CREATE TABLE IF NOT EXISTS alert_hits (
    id          BIGSERIAL PRIMARY KEY,
    alert_id    INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    product_id  INTEGER NOT NULL,
    price       DOUBLE PRECISION NOT NULL,
    store_id    INTEGER,
    seen        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'))
);
CREATE INDEX IF NOT EXISTS idx_hits_seen ON alert_hits(seen, created_at DESC);

-- ---------------------------------------------------------------------------
-- Comentarios de producto
--
-- Guardados aquí, en la misma base que todo lo demás. No hay servicio externo
-- ni cuenta que crear: la aplicación es local y los comentarios también.
-- `author` es texto libre porque no hay usuarios; se recuerda el último
-- nombre usado en el navegador.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS comments (
    id          BIGSERIAL PRIMARY KEY,
    product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    author      TEXT,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'))
);
CREATE INDEX IF NOT EXISTS idx_comments_product ON comments(product_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Productos vistos
--
-- Cuenta cuántas veces se ha abierto cada ficha. Es una aplicación local: «lo
-- más visto» es lo que más has mirado tú, que para decidir una compra es
-- justo lo que interesa tener a mano.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product_views (
    product_id      INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    views           INTEGER NOT NULL DEFAULT 0,
    last_viewed_at  TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'))
);
CREATE INDEX IF NOT EXISTS idx_views_top ON product_views(views DESC, last_viewed_at DESC);

-- ---------------------------------------------------------------------------
-- Publicaciones en Telegram
--
-- Un evento se publica una sola vez. Sin esto, un run repetido o a medias
-- volvería a anunciar las mismas ofertas.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS telegram_sent (
    event_id  INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    sent_at   TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'))
);

-- ---------------------------------------------------------------------------
-- Ofertas descartadas a mano
--
-- Borrar un producto no basta: el siguiente scraping vuelve a encontrarlo en
-- la tienda y lo da de alta otra vez. Lo que hace falta es recordar la
-- decisión contra la misma clave estable que usan el resto de correcciones
-- manuales, y saltarse esas fichas al recoger.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS excluded_offers (
    entity_key  TEXT PRIMARY KEY,
    store_code  TEXT,
    name        TEXT,
    url         TEXT,
    reason      TEXT,
    created_at  TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'))
);

-- ---------------------------------------------------------------------------
-- Productos ya destacados
--
-- Cuando no hay ninguna bajada que contar, el bot publica igualmente una buena
-- oportunidad para que el grupo no se quede mudo días enteros. Aquí se apunta
-- cuál, para no repetir el mismo producto una y otra vez.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS telegram_destacados (
    product_id  INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    sent_at     TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'))
);

-- ---------------------------------------------------------------------------
-- Socios del grupo de pago
--
-- El cobro ocurre fuera del sistema: alguien transfiere, el administrador
-- aprueba su entrada en Telegram, y aquí solo se lleva la cuenta de hasta
-- cuándo tiene acceso. `user_id` es BIGINT porque los identificadores de
-- Telegram ya no caben en 32 bits.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS miembros (
    user_id     BIGINT PRIMARY KEY,
    nombre      TEXT,
    usuario     TEXT,
    alta_at     TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')),
    vence_at    TEXT NOT NULL,
    estado      TEXT NOT NULL DEFAULT 'activo',  -- activo | expulsado | baja
    ultimo_aviso INTEGER,
    nota        TEXT,
    updated_at  TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'))
);
CREATE INDEX IF NOT EXISTS idx_miembros_vence ON miembros(estado, vence_at);

-- ---------------------------------------------------------------------------
-- Vigilancia de enlaces sueltos
--
-- Fuera del catálogo normal: direcciones concretas que se miran una a una, sin
-- recorrer la tienda entera. Se guarda el último estado conocido para poder
-- contar solo lo que cambia; sin eso, mirar cada hora sería anunciar cada hora
-- lo mismo.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vigilancia (
    url          TEXT PRIMARY KEY,
    etiqueta     TEXT,
    nombre       TEXT,
    price        DOUBLE PRECISION,
    stock_status TEXT,
    http_status  INTEGER,
    image_url    TEXT,
    primera_vez  TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')),
    visto_at     TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')),
    avisado_at   TEXT
);
