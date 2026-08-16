-- =====================================================================
-- Esquema SQLite de TCG Comparative
-- =====================================================================
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
-- Configuración persistida (sobrescribe config/settings.yaml en runtime)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings_kv (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------
-- Catálogos
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS games (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
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
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    base_url        TEXT NOT NULL,
    adapter         TEXT NOT NULL,
    config_file     TEXT,
    currency        TEXT NOT NULL DEFAULT 'CLP',
    enabled         INTEGER NOT NULL DEFAULT 1,
    country         TEXT,
    logo_url        TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
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
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
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
    best_price              REAL,
    best_store_id           INTEGER,
    best_available_price    REAL,
    best_available_store_id INTEGER,
    worst_price             REAL,
    avg_price               REAL,
    last_scraped_at         TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
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
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
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
    price               REAL,
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
    quantity_confidence REAL NOT NULL DEFAULT 0,
    units_total         INTEGER,
    language            TEXT,
    -- Agrupación
    product_id          INTEGER REFERENCES products(id) ON DELETE SET NULL,
    match_score         REAL,
    match_method        TEXT,
    -- Control de cambios
    content_hash        TEXT,
    first_seen_at       TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at        TEXT NOT NULL DEFAULT (datetime('now')),
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
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
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
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type     TEXT NOT NULL,   -- store_product | product
    entity_id       INTEGER NOT NULL,
    key             TEXT NOT NULL,
    value           TEXT,
    confidence      REAL NOT NULL DEFAULT 1.0,
    source          TEXT,            -- name | description | catalog | default
    UNIQUE (entity_type, entity_id, key)
);
CREATE INDEX IF NOT EXISTS idx_attr_entity ON product_attributes(entity_type, entity_id);

-- ---------------------------------------------------------------------
-- Decisión de agrupación por oferta (por qué quedó en ese maestro)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product_matches (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id          INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    store_product_id    INTEGER NOT NULL REFERENCES store_products(id) ON DELETE CASCADE,
    score               REAL NOT NULL DEFAULT 0,
    method              TEXT NOT NULL,   -- EAN_MATCH|SKU_MATCH|NAME_MATCH|ATTRIBUTE_MATCH|FUZZY_MATCH|MANUAL_MATCH|SINGLETON
    breakdown           TEXT,            -- JSON con el detalle del puntaje
    anchor_id           INTEGER,         -- oferta con la que hizo match
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (store_product_id)
);
CREATE INDEX IF NOT EXISTS idx_pm_product ON product_matches(product_id);

-- ---------------------------------------------------------------------
-- Cola de revisión manual (pares dudosos: 75..89)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS match_reviews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    a_id            INTEGER NOT NULL REFERENCES store_products(id) ON DELETE CASCADE,
    b_id            INTEGER NOT NULL REFERENCES store_products(id) ON DELETE CASCADE,
    score           REAL NOT NULL,
    method          TEXT NOT NULL,
    breakdown       TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending|confirmed|rejected
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at      TEXT,
    UNIQUE (a_id, b_id)
);
CREATE INDEX IF NOT EXISTS idx_reviews_status ON match_reviews(status, score DESC);

-- ---------------------------------------------------------------------
-- Decisiones humanas permanentes (se respetan en cada re-agrupación)
-- La pareja se guarda con a_key < b_key para evitar duplicados.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS manual_matches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    a_key       TEXT NOT NULL,   -- store_code::external_key estable
    b_key       TEXT NOT NULL,
    decision    TEXT NOT NULL,   -- same | different
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
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
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_key  TEXT NOT NULL,   -- store_code::external_id
    attribute   TEXT NOT NULL,   -- language | set_code | product_type
    value       TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (entity_key, attribute)
);
CREATE INDEX IF NOT EXISTS idx_manual_attr ON manual_attributes(attribute);

-- ---------------------------------------------------------------------
-- Historial
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS price_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    store_product_id    INTEGER NOT NULL REFERENCES store_products(id) ON DELETE CASCADE,
    price               REAL NOT NULL,
    currency            TEXT NOT NULL DEFAULT 'CLP',
    recorded_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ph_sp_time ON price_history(store_product_id, recorded_at DESC);

CREATE TABLE IF NOT EXISTS stock_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    store_product_id    INTEGER NOT NULL REFERENCES store_products(id) ON DELETE CASCADE,
    stock_status        TEXT NOT NULL,
    recorded_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sh_sp_time ON stock_history(store_product_id, recorded_at DESC);

-- ---------------------------------------------------------------------
-- Detección de cambios / feed de eventos
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    type                TEXT NOT NULL,  -- price_drop|price_rise|new_product|removed_product|
                                        -- back_in_stock|out_of_stock|name_change|new_match
    store_id            INTEGER,
    store_product_id    INTEGER,
    product_id          INTEGER,
    old_value           TEXT,
    new_value           TEXT,
    pct_change          REAL,
    message             TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type, created_at DESC);

-- ---------------------------------------------------------------------
-- Ejecuciones de scraping y sus errores
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scrape_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id            INTEGER REFERENCES stores(id) ON DELETE CASCADE,
    trigger             TEXT NOT NULL DEFAULT 'manual',  -- manual|scheduled|startup
    status              TEXT NOT NULL DEFAULT 'running', -- running|ok|partial|error
    started_at          TEXT NOT NULL DEFAULT (datetime('now')),
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
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER REFERENCES scrape_runs(id) ON DELETE CASCADE,
    store_id    INTEGER REFERENCES stores(id) ON DELETE CASCADE,
    stage       TEXT NOT NULL,   -- categories|listing|product|parse|network
    url         TEXT,
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_errors_store ON scrape_errors(store_id, created_at DESC);

-- Log unificado que alimenta la consola de la interfaz.
CREATE TABLE IF NOT EXISTS app_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    level       TEXT NOT NULL DEFAULT 'info',  -- debug|info|warn|error
    scope       TEXT,
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
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
    fetched_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------
-- Favoritos y alertas
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS favorites (
    product_id  INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS alerts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id          INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    target_price        REAL NOT NULL,
    only_in_stock       INTEGER NOT NULL DEFAULT 1,
    active              INTEGER NOT NULL DEFAULT 1,
    last_triggered_at   TEXT,
    last_triggered_price REAL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_alerts_product ON alerts(product_id);

CREATE TABLE IF NOT EXISTS alert_hits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id    INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    product_id  INTEGER NOT NULL,
    price       REAL NOT NULL,
    store_id    INTEGER,
    seen        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
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
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    author      TEXT,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
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
    last_viewed_at  TEXT NOT NULL DEFAULT (datetime('now'))
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
    sent_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- Productos ya destacados
--
-- Cuando no hay ninguna bajada que contar, el bot publica igualmente una buena
-- oportunidad para que el grupo no se quede mudo días entéros. Aquí se apunta
-- cuál, para no repetir el mismo producto una y otra vez.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS telegram_destacados (
    product_id  INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- Socios del grupo de pago
--
-- El cobro ocurre fuera del sistema: alguien transfiere, el administrador
-- aprueba su entrada en Telegram, y aquí solo se lleva la cuenta de hasta
-- cuándo tiene acceso. La clave es el identificador de Telegram, que no
-- cambia aunque la persona se cambie el nombre o el alias.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS miembros (
    user_id     INTEGER PRIMARY KEY,
    nombre      TEXT,
    usuario     TEXT,                          -- @alias, solo para leerlo
    alta_at     TEXT NOT NULL DEFAULT (datetime('now')),
    vence_at    TEXT NOT NULL,
    estado      TEXT NOT NULL DEFAULT 'activo',  -- activo | expulsado | baja
    -- Último aviso enviado, en días de antelación: evita repetir el mismo
    -- recordatorio en cada pasada del cron.
    ultimo_aviso INTEGER,
    nota        TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_miembros_vence ON miembros(estado, vence_at);
