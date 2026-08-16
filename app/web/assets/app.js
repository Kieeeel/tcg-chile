/* =====================================================================
   TCG Comparative — interfaz web (JavaScript sin dependencias externas)
   ===================================================================== */

// ------------------------------------------------------------------ API
const api = {
  async get(path, params) {
    const url = new URL(path, location.origin);
    Object.entries(params || {}).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '' && v !== false) url.searchParams.set(k, v);
    });
    const res = await fetch(url);
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    return res.json();
  },
  async send(method, path, body) {
    const res = await fetch(path, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    return res.status === 204 ? null : res.json();
  },
  post: (p, b) => api.send('POST', p, b),
  put: (p, b) => api.send('PUT', p, b),
  del: (p, b) => api.send('DELETE', p, b),
};

// -------------------------------------------------------------- formato
const state = {
  currency: { symbol: '$', thousands: '.', decimal: ',', decimals: 0 },
  facets: null,
  // 'compare' existió antes: si quedó guardado, vuelve a tarjetas.
  viewMode: ['cards', 'list'].includes(localStorage.getItem('viewMode'))
    ? localStorage.getItem('viewMode') : 'cards',
  filters: {},
};

function money(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const c = state.currency;
  const fixed = Number(value).toFixed(c.decimals);
  const [intPart, decPart] = fixed.split('.');
  const grouped = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, c.thousands);
  return c.symbol + grouped + (decPart ? c.decimal + decPart : '');
}

function pct(value, digits = 1) {
  if (value === null || value === undefined) return '—';
  return `${Number(value).toFixed(digits).replace('.', ',')} %`;
}

function num(value) {
  return (value ?? 0).toLocaleString('es-CL');
}

function parseDate(text) {
  if (!text) return null;
  // SQLite entrega "YYYY-MM-DD HH:MM:SS" en UTC.
  const iso = text.includes('T') ? text : text.replace(' ', 'T') + 'Z';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

function ago(text) {
  const date = parseDate(text);
  if (!date) return 'nunca';
  const seconds = Math.max(0, (Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return 'hace instantes';
  const mins = Math.floor(seconds / 60);
  if (mins < 60) return `hace ${mins} min`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `hace ${hours} h ${mins % 60} min`;
  const days = Math.floor(hours / 24);
  return days === 1 ? 'hace 1 día' : `hace ${days} días`;
}

function countdown(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `en ${h} h ${m} min`;
  return `en ${m} min`;
}

const esc = (text) =>
  String(text ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function toast(message, isError = false) {
  const el = document.getElementById('toast');
  el.textContent = message;
  el.className = 'toast' + (isError ? ' toast--error' : '');
  el.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { el.hidden = true; }, 3800);
}

const STOCK_TAG = {
  in_stock: '<span class="tag tag--ok">En stock</span>',
  out_of_stock: '<span class="tag tag--off">Agotado</span>',
  preorder: '<span class="tag tag--warn">Preventa</span>',
  coming_soon: '<span class="tag tag--warn">Próximamente</span>',
  unknown: '<span class="tag">Desconocido</span>',
};

const RANK_ICON = ['🥇', '🥈', '🥉'];

// El idioma forma parte de la identidad del producto: un ETB en español y uno
// en inglés son productos distintos y con precios distintos. Se marca siempre.
// Banderas dibujadas (símbolos de index.html), no emoji: Windows no incluye
// los glifos de bandera y 🇪🇸 se veía como las letras "ES" sueltas.
const LANG_FLAG = { es: 'f-es', en: 'f-en', jp: 'f-jp', ko: 'f-ko', zh: 'f-zh',
                    fr: 'f-fr', de: 'f-de', it: 'f-it', pt: 'f-pt' };

function bandera(code) {
  const simbolo = LANG_FLAG[code];
  if (!simbolo) return '';
  return `<svg class="flag" aria-hidden="true"><use href="#${simbolo}"/></svg>`;
}

function langTag(code, name) {
  if (!code) return '<span class="tag tag--lang tag--lang-none">Idioma sin declarar</span>';
  return `<span class="tag tag--lang">${bandera(code)}${esc(name || code.toUpperCase())}</span>`;
}

// =====================================================================
// Selector de TCG (barra bajo la cabecera)
//
// Vive fuera de las vistas porque acompaña a toda la aplicación. Los juegos
// salen de config/games.yaml: los que aún no tienen catálogo se muestran
// apagados, para dejar ver hacia dónde va la aplicación sin prometer nada.
// =====================================================================
function juegoActual() {
  if (state.game) return state.game;
  const guardado = localStorage.getItem('game');
  const juegos = (state.facets && state.facets.games) || [];
  // Vale cualquier juego configurado, tenga catálogo o no: si eliges uno
  // vacío se muestra por qué está vacío, en vez de ignorar el clic.
  if (guardado && juegos.some((g) => g.code === guardado)) return guardado;
  const base = juegos.find((g) => g.available);
  return base ? base.code : '';
}

function juegoInfo(code) {
  return ((state.facets && state.facets.games) || []).find((g) => g.code === code);
}

function renderGamesNav() {
  const nav = document.getElementById('games-nav');
  const juegos = (state.facets && state.facets.games) || [];
  if (!nav || !juegos.length) return;

  // «Inicio» abre esta misma fila, a la izquierda del primer juego: es el
  // paso previo a elegir TCG, no una sección más del menú de iconos.
  nav.innerHTML = `
    <a class="game game--home" href="#/inicio" data-route="inicio" title="Portada">
      <svg aria-hidden="true"><use href="#i-home"/></svg>
      <span class="game__name">Inicio</span>
    </a>
    <span class="games__sep" aria-hidden="true"></span>
    ${juegos.map((g) => `
    <button class="game ${g.available ? '' : 'is-soon'}"
            style="--game:${esc(g.color)}" data-game="${esc(g.code)}"
            title="${g.available ? esc(g.name) : esc(g.name) + ' — todavía sin catálogo'}">
      <span class="game__name">${esc(g.name)}</span>
      ${g.available ? `<span class="game__count">${num(g.count)}</span>` : ''}
    </button>`).join('')}`;

  marcarBarraJuegos();

  // Los botones de la barra son un atajo a la búsqueda de ese TCG. Elegir
  // juego «para mirar» se hace en la portada; aquí se elige «para buscar».
  nav.querySelectorAll('.game[data-game]').forEach((btn) => {
    btn.onclick = () => elegirJuego(btn.dataset.game, 'buscar');
  });
}

/**
 * Marca UNA sola pestaña de la barra.
 *
 * «Inicio» y los TCG llevan a sitios distintos —la portada y la búsqueda—,
 * así que no pueden estar encendidos a la vez: el resaltado dice dónde
 * estás, no qué juego tienes elegido. En el resto de pantallas (favoritos,
 * alertas, administración) no se enciende ninguno.
 */
function marcarBarraJuegos() {
  const ruta = rutaActual();
  const inicio = document.querySelector('.game--home');
  if (inicio) inicio.classList.toggle('is-active', ruta === 'inicio');

  const juego = ruta === 'buscar' ? juegoActual() : null;
  document.querySelectorAll('.games .game[data-game]').forEach((btn) =>
    btn.classList.toggle('is-active', btn.dataset.game === juego));
}

/**
 * Fija el TCG y lleva a donde toque.
 *
 * @param {string} codigo  juego elegido
 * @param {string} destino 'buscar' (listado filtrado) o 'inicio' (portada)
 */
function elegirJuego(codigo, destino) {
  state.game = codigo;
  localStorage.setItem('game', codigo);
  // Set y tipo se reinician: son catálogos distintos en cada juego.
  state.filters = { ...state.filters, game: codigo, set_code: '', product_type: '', page: 1 };
  location.hash = `#/${destino}?game=${encodeURIComponent(codigo)}`;
}

/**
 * Hace aparecer un bloque con una transición de opacidad.
 *
 * El contenido entra entero y de una vez, ya cargado. Se probó a escalonar
 * las tarjetas y a desplazarlas unos píxeles al entrar, pero ese
 * desplazamiento desbordaba la tabla y sacaba una barra de scroll a la
 * derecha mientras duraba la animación. Solo opacidad.
 *
 * Quitar la clase y forzar un reflujo es lo que reinicia la animación: sin
 * eso, un elemento que ya la tiene puesta no la repite.
 */
function aparecer(elemento) {
  if (!elemento) return;
  elemento.classList.remove('fade-in');
  void elemento.offsetWidth;
  elemento.classList.add('fade-in');
}

// ------------------------------------------------------------------ router
const routes = {};
function route(name, handler) { routes[name] = handler; }

// =====================================================================
// Administración
//
// Tiendas, revisión manual y configuración son tareas de mantenimiento, no
// de consulta: viven juntas bajo #/admin con subpestañas. Cada sección
// reutiliza tal cual la vista que ya existía; aquí solo se decide cuál se
// pinta y dónde.
// =====================================================================
const SECCIONES_ADMIN = [
  { id: 'tiendas', nombre: 'Tiendas', vista: () => vistaTiendas },
  { id: 'revision', nombre: 'Revisión', vista: () => vistaRevision, badge: 'pending_reviews' },
  { id: 'productos', nombre: 'Productos', vista: () => vistaProductos },
  { id: 'configuracion', nombre: 'Configuración', vista: () => vistaConfiguracion },
];

route('admin', async (view, [seccion]) => {
  const actual = SECCIONES_ADMIN.some((s) => s.id === seccion) ? seccion : 'tiendas';
  // Los contadores de las subpestañas salen de aquí: si se entra directo por
  // la URL todavía no se han pedido, y la pestaña salía sin su número.
  if (!state.totals) await refreshBadges().catch(() => {});

  view.innerHTML = `
    <nav class="subtabs">
      ${SECCIONES_ADMIN.map((s) => `
        <a href="#/admin/${s.id}" class="${s.id === actual ? 'is-active' : ''}">
          ${esc(s.nombre)}
          ${s.badge && state.totals && state.totals[s.badge]
            ? `<span class="badge">${num(state.totals[s.badge])}</span>` : ''}
        </a>`).join('')}
    </nav>
    <div id="admin-body"><div class="loading"><span class="spinner"></span> Cargando…</div></div>`;

  const cuerpo = document.getElementById('admin-body');
  const vista = SECCIONES_ADMIN.find((s) => s.id === actual).vista();
  await vista(cuerpo);
  // Al cambiar de subpestaña solo entra la sección, no la barra de arriba.
  aparecer(cuerpo);
});

// Las rutas antiguas siguen funcionando: llevan a su sección de administración.
['tiendas', 'revision', 'configuracion'].forEach((id) =>
  route(id, () => { location.replace(`#/admin/${id}`); }));


// Ruta actual, sin la query. La usan el selector de TCG y la portada para
// saber a dónde volver.
function rutaActual() {
  const hash = location.hash.slice(2) || 'inicio';
  return (hash.split('?')[0] || 'inicio').split('/')[0];
}

async function render() {
  // "#/buscar?q=151" -> ruta "buscar"; la query la lee cada vista aparte.
  const hash = location.hash.slice(2) || 'inicio';
  const [path] = hash.split('?');
  const [name, ...rest] = (path || 'inicio').split('/');
  const handler = routes[name] || routes.inicio;

  // Las rutas antiguas marcan el icono al que fueron a parar.
  const equivalencias = {
    tiendas: 'admin', revision: 'admin', configuracion: 'admin',
  };
  const rutaActiva = equivalencias[name] || name;
  document.querySelectorAll('#tabs a').forEach((a) =>
    a.classList.toggle('is-active', a.dataset.route === rutaActiva));
  marcarBarraJuegos();

  const view = document.getElementById('view');
  view.innerHTML = '<div class="loading"><span class="spinner"></span> Cargando…</div>';
  try {
    await handler(view, rest);
  } catch (err) {
    view.innerHTML = `<div class="empty"><h3>Ocurrió un error</h3><p>${esc(err.message)}</p></div>`;
  }
  // Ya con la página montada: entra entera de una vez.
  aparecer(view);
}

window.addEventListener('hashchange', render);

// =====================================================================
// Buscar / comparar
// =====================================================================
const vistaBuscar = async (view) => {
  const params = new URLSearchParams(location.hash.split('?')[1] || '');
  state.filters = {
    q: params.get('q') || '',
    game: params.get('game') || '',
    set_code: params.get('set_code') || '',
    product_type: params.get('product_type') || '',
    language: params.get('language') || '',
    store_id: params.get('store_id') || '',
    only_in_stock: params.get('only_in_stock') === '1',
    min_price: params.get('min_price') || '',
    max_price: params.get('max_price') || '',
    sort: params.get('sort') || 'relevance',
    page: Number(params.get('page') || 1),
  };
  if (!state.facets) state.facets = await api.get('/api/facets');

  // Sin juego en la URL manda el TCG elegido en la barra superior.
  if (!state.filters.game) state.filters.game = juegoActual();
  state.game = state.filters.game;
  renderGamesNav();

  // El texto vive solo en el buscador de la cabecera: lo dejamos sincronizado
  // para que al llegar desde un enlace se vea qué se está buscando.
  const buscador = document.getElementById('global-search');
  if (buscador && buscador.value !== state.filters.q) buscador.value = state.filters.q;

  // En el teléfono los filtros arrancan plegados y se despliegan desde su
  // propia cabecera; en el escritorio no hay nada que plegar y el panel se ve
  // entero, que para eso tiene su columna.
  //
  // El plegado es una clase, no un <details>: quien decide si esa clase
  // esconde algo es la hoja de estilos, y solo dentro de la consulta de
  // móvil. Así, al agrandar la ventana el panel reaparece sin que haya que
  // escuchar nada ni recordar en qué estado se quedó.
  view.innerHTML = `
    <div class="layout">
      <aside class="filters">
        <div class="filterbox is-collapsed">
          <button type="button" class="filterbox__toggle" aria-expanded="false"
                  aria-controls="filter-panel">
            <span>Filtros</span>
            <span class="filterbox__count" id="filter-count"></span>
          </button>
          <div class="card" id="filter-panel"><div class="card__body">${filterPanel()}</div></div>
        </div>
      </aside>
      <section><div id="results"><div class="loading"><span class="spinner"></span> Buscando…</div></div></section>
    </div>`;

  // La vista se ha reconstruido: lo que hubiera en caché es de otra visita.
  olvidarBusqueda();
  bindFilters();
  pintarContadorFiltros();

  const plegable = view.querySelector('.filterbox');
  plegable.querySelector('.filterbox__toggle').onclick = () => {
    const plegado = plegable.classList.toggle('is-collapsed');
    plegable.querySelector('.filterbox__toggle')
      .setAttribute('aria-expanded', String(!plegado));
  };

  await loadResults();
};

route('buscar', vistaBuscar);

// =====================================================================
// Portada
//
// Escaparate por secciones, todas filtradas por el TCG que esté elegido en
// la barra de arriba: al cambiar de juego cambia la portada entera. Cada
// sección enlaza a la búsqueda con los filtros ya puestos, que es donde se
// afina de verdad.
// =====================================================================
const vistaPortada = async (view) => {
  const params = new URLSearchParams(location.hash.split('?')[1] || '');
  if (params.get('game')) state.game = params.get('game');
  const juego = juegoActual();
  state.game = juego;
  if (!state.facets) state.facets = await api.get('/api/facets');
  renderGamesNav();

  // El buscador de la cabecera se vacía: en la portada no hay búsqueda activa.
  const buscador = document.getElementById('global-search');
  if (buscador) buscador.value = '';

  const datos = await api.get('/api/home', { game: juego, per_section: 12 });
  const info = juegoInfo(juego);

  if (info && !info.available) {
    // El selector se mantiene: es lo que permite volver a un juego que sí
    // tiene catálogo sin tener que ir a buscarlo a otro sitio.
    view.innerHTML = `
      ${juegosHtml(juego)}
      <div class="empty">
        <h3>Todavía no hay catálogo de ${esc(info.name)}</h3>
        <p>Ninguna de las tiendas configuradas publica productos de este juego.</p>
        <p class="hint">Para añadirlo, crea un archivo en <span class="mono">config/stores/</span>
           con <span class="mono">default_game: ${esc(info.code)}</span> y pulsa
           <strong>Actualizar todas</strong> en Administración → Tiendas.</p>
      </div>`;
    view.querySelectorAll('.tcg[data-game]').forEach((tarjeta) => {
      tarjeta.onclick = () => elegirJuego(tarjeta.dataset.game, 'inicio');
    });
    return;
  }

  const q = (extra) => `#/buscar?game=${encodeURIComponent(juego)}${extra || ''}`;

  view.innerHTML = `
    ${juegosHtml(juego)}

    ${carrusel('visto', 'Lo más visto', datos.viewed, q('&sort=stores'),
      'Las fichas que más has abierto.')}

    ${carrusel('ofertas', 'Ofertas del día', datos.deals,
      q('&sort=discount&only_in_stock=1'), {
        drops: 'Lo que ha bajado de precio en la última semana.',
        mixed: 'Bajadas de esta semana y, tras ellas, las mayores diferencias entre tiendas.',
        spread: 'Todavía no hay bajadas registradas: estas son las mayores diferencias entre tiendas.',
      }[datos.deals_source] || '')}

    ${categoriasHtml(datos.categories, juego)}

    ${carrusel('nuevo', 'Recién agregados', datos.recent, q('&sort=updated'),
      'Lo último que apareció en las tiendas.')}`;

  view.querySelectorAll('.shelf').forEach(bindCarrusel);

  // En la portada, elegir juego cambia lo que se está mirando; no saca de
  // aquí. El salto a la búsqueda es cosa de la barra de arriba.
  view.querySelectorAll('.tcg[data-game]').forEach((tarjeta) => {
    tarjeta.onclick = () => elegirJuego(tarjeta.dataset.game, 'inicio');
  });
};

route('inicio', vistaPortada);

// Selector de TCG de la portada: aquí se decide qué catálogo se mira, y las
// secciones de abajo se rehacen con ese juego.
function juegosHtml(actual) {
  const juegos = (state.facets && state.facets.games) || [];
  if (!juegos.length) return '';
  return `
    <section class="shelf">
      <div class="shelf__head"><div>
        <h2 class="shelf__title">Elige tu juego</h2>
        <p class="shelf__sub">La portada entera cambia con el TCG que elijas.</p>
      </div></div>
      <div class="tcgs">
        ${juegos.map((g) => `
          <button class="tcg ${g.code === actual ? 'is-active' : ''} ${g.available ? '' : 'is-soon'}"
                  style="--game:${esc(g.color)}" data-game="${esc(g.code)}">
            <span class="tcg__art" aria-hidden="true">
              <svg viewBox="0 0 60 60">
                <rect x="6" y="12" width="24" height="34" rx="3" transform="rotate(-14 18 29)"/>
                <rect x="20" y="9" width="25" height="36" rx="3"/>
                <rect x="35" y="12" width="24" height="34" rx="3" transform="rotate(14 47 29)"/>
              </svg>
            </span>
            <span class="tcg__name">${esc(g.name)}</span>
            <span class="tcg__count">
              ${g.available ? `${num(g.count)} productos` : 'Sin catálogo'}
            </span>
            ${g.code === actual ? '<span class="tcg__flag">Viendo</span>' : ''}
          </button>`).join('')}
      </div>
    </section>`;
}

function carrusel(id, titulo, items, verMas, bajada) {
  if (!items || !items.length) return '';
  return `
    <section class="shelf" data-shelf="${id}">
      <div class="shelf__head">
        <div>
          <h2 class="shelf__title">${esc(titulo)}</h2>
          ${bajada ? `<p class="shelf__sub">${esc(bajada)}</p>` : ''}
        </div>
        <a class="shelf__more" href="${verMas}">Ver más</a>
      </div>
      <div class="shelf__viewport">
        <button class="shelf__arrow shelf__arrow--prev" aria-label="Anterior">‹</button>
        <div class="shelf__track">${items.map(productCard).join('')}</div>
        <button class="shelf__arrow shelf__arrow--next" aria-label="Siguiente">›</button>
      </div>
    </section>`;
}

function categoriasHtml(categorias, juego) {
  if (!categorias || !categorias.length) return '';
  return `
    <section class="shelf">
      <div class="shelf__head"><div><h2 class="shelf__title">Categorías</h2></div></div>
      <div class="cats">
        ${categorias.map((c) => `
          <a class="cat" href="#/buscar?game=${encodeURIComponent(juego)}&product_type=${encodeURIComponent(c.code)}">
            <span class="cat__name">${esc(c.name)}</span>
            <span class="cat__count">${num(c.count)} productos</span>
          </a>`).join('')}
      </div>
    </section>`;
}

// Flechas del carrusel: mueven una pantalla de ancho y se apagan al llegar
// al borde, para que se vea si queda más a los lados.
function bindCarrusel(seccion) {
  const via = seccion.querySelector('.shelf__viewport');
  const carril = seccion.querySelector('.shelf__track');
  const anterior = seccion.querySelector('.shelf__arrow--prev');
  const siguiente = seccion.querySelector('.shelf__arrow--next');
  if (!carril) return;

  const actualizar = () => {
    const max = carril.scrollWidth - carril.clientWidth - 2;
    anterior.disabled = carril.scrollLeft <= 2;
    siguiente.disabled = carril.scrollLeft >= max;
    via.classList.toggle('is-static', max <= 0);
  };
  const mover = (signo) =>
    carril.scrollBy({ left: signo * (carril.clientWidth * 0.9), behavior: 'smooth' });

  anterior.onclick = () => mover(-1);
  siguiente.onclick = () => mover(1);
  carril.addEventListener('scroll', actualizar, { passive: true });
  actualizar();
}

function filterPanel() {
  const f = state.filters;
  const facets = state.facets;
  const opt = (list, value, labelKey = 'name', valueKey = 'code') =>
    list.map((i) => `<option value="${esc(i[valueKey])}" ${String(i[valueKey]) === String(value) ? 'selected' : ''}>
        ${esc(i[labelKey] || i[valueKey])}${i.count !== undefined ? ` (${i.count})` : ''}</option>`).join('');

  // Ni el texto ni el juego están aquí: el primero se escribe en el buscador
  // de arriba y el segundo se elige con los banners.
  return `
    <div class="filter-group"><label>Set / expansión</label>
      <select id="f-set"><option value="">Todos</option>${
        // Solo las expansiones del juego elegido: los sets no se comparten.
        opt(facets.sets.filter((s) => !f.game || s.game === f.game), f.set_code)
      }</select></div>
    <div class="filter-group"><label>Tipo de producto</label>
      <select id="f-type"><option value="">Todos</option>${opt(facets.types, f.product_type)}</select></div>
    <div class="filter-group"><label>Idioma</label>
      <select id="f-lang"><option value="">Todos</option>${opt(facets.languages || [], f.language)}</select></div>
    <div class="filter-group"><label>Tienda</label>
      <select id="f-store"><option value="">Todas</option>
        ${facets.stores.map((s) => `<option value="${s.id}" ${String(s.id) === String(f.store_id) ? 'selected' : ''}>
            ${esc(s.name)} (${s.products})</option>`).join('')}</select></div>
    <div class="filter-group"><label>Precio</label>
      <div class="range">
        <input type="number" id="f-min" placeholder="mín" value="${esc(f.min_price)}">
        <input type="number" id="f-max" placeholder="máx" value="${esc(f.max_price)}">
      </div></div>
    <div class="filter-group">
      <label class="check check--box ${f.only_in_stock ? 'is-on' : ''}">
        <input type="checkbox" id="f-stock" ${f.only_in_stock ? 'checked' : ''}>
        Ocultar los agotados</label></div>
    <div class="filter-group">
      <button class="btn btn--ghost btn--sm" id="f-clear">Limpiar filtros</button></div>`;
}

// El teléfono no es solo una pantalla estrecha: hay cosas que ahí sobran.
// El mismo objeto se reutiliza para reaccionar a los cambios de tamaño.
const CONSULTA_MOVIL = window.matchMedia('(max-width: 620px)');
function esMovil() {
  return CONSULTA_MOVIL.matches;
}

// Cuántos filtros hay puestos. Se enseña junto al botón para que, con el
// panel plegado, se sepa que la lista está filtrada y por cuántas cosas.
function pintarContadorFiltros() {
  const hueco = document.getElementById('filter-count');
  if (!hueco) return;
  const f = state.filters;
  const puestos = [f.set_code, f.product_type, f.language, f.store_id,
                   f.min_price, f.max_price, f.only_in_stock].filter(Boolean).length;
  hueco.textContent = puestos ? String(puestos) : '';
  hueco.hidden = !puestos;
}

function bindFilters() {
  const apply = () => {
    state.filters = {
      // `q` lo fija el buscador de la cabecera y `game` los banners: ambos
      // se conservan tal cual al tocar cualquier filtro de esta barra.
      ...state.filters,
      set_code: document.getElementById('f-set').value,
      product_type: document.getElementById('f-type').value,
      language: document.getElementById('f-lang').value,
      store_id: document.getElementById('f-store').value,
      min_price: document.getElementById('f-min').value,
      max_price: document.getElementById('f-max').value,
      only_in_stock: document.getElementById('f-stock').checked,
      page: 1,
    };
    syncHash();
    pintarContadorFiltros();
    loadResults();
  };

  ['f-set', 'f-type', 'f-lang', 'f-store', 'f-stock'].forEach((id) =>
    document.getElementById(id).addEventListener('change', apply));

  const casilla = document.getElementById('f-stock');
  casilla.addEventListener('change', () =>
    casilla.closest('.check--box').classList.toggle('is-on', casilla.checked));
  ['f-min', 'f-max'].forEach((id) =>
    document.getElementById(id).addEventListener('change', apply));

  document.getElementById('f-clear').onclick = () => {
    // Un solo repintado: cambiar el hash ya dispara `render` por hashchange.
    // Hacer las dos cosas dejaba dos renders compitiendo y la lista vacía.
    const destino = '#/buscar';
    if (location.hash === destino || location.hash === '') render();
    else location.hash = destino;
  };
}

function syncHash() {
  const p = new URLSearchParams();
  Object.entries(state.filters).forEach(([k, v]) => {
    if (v === '' || v === false || v === undefined || v === null) return;
    p.set(k, v === true ? '1' : v);
  });
  history.replaceState(null, '', `#/buscar?${p.toString()}`);
}

// Último resultado servido y contador de peticiones.
//
// El contador evita que una respuesta lenta pise a otra más nueva: al cambiar
// varios filtros seguidos, la primera consulta puede llegar después de la
// segunda y dejar en pantalla una lista que ya no corresponde a los filtros.
let ultimaBusqueda = null;
let peticionEnCurso = 0;

function olvidarBusqueda() {
  ultimaBusqueda = null;
  peticionEnCurso += 1;
}

/**
 * Carga y pinta los resultados.
 *
 * Antes se vaciaba `#results` y se ponía un «Buscando…» en su lugar: la lista
 * desaparecía de golpe, la página daba un salto y volvía a aparecer. Ahora la
 * lista se queda donde está y solo se atenúa mientras llega la respuesta.
 *
 * @param {boolean} refetch  false = repintar con los datos que ya tenemos
 *                           (cambiar de tarjetas a lista no necesita red).
 */
async function loadResults({ refetch = true } = {}) {
  const box = document.getElementById('results');
  if (!box) return;

  if (!refetch && ultimaBusqueda) {
    pintarResultados(box, ultimaBusqueda);
    return;
  }

  const token = ++peticionEnCurso;
  const yaHayLista = !!box.querySelector('.toolbar');

  // La atenuación no se enseña de inmediato. Contra la base local la
  // respuesta suele tardar 30 ms, y un parpadeo de 30 ms se ve peor que no
  // enseñar nada: solo avisamos si la espera se nota de verdad.
  let aviso = null;
  if (yaHayLista) aviso = setTimeout(() => box.classList.add('is-loading'), 140);
  else box.innerHTML = '<div class="loading"><span class="spinner"></span> Buscando…</div>';

  let data;
  try {
    data = await api.get('/api/products', { ...state.filters, page_size: 24 });
  } catch (err) {
    clearTimeout(aviso);
    if (token !== peticionEnCurso) return;   // ya hay una consulta más nueva
    box.classList.remove('is-loading');
    box.innerHTML = `<div class="empty"><h3>No se pudo buscar</h3>
      <p>${esc(err.message)}</p></div>`;
    return;
  }
  clearTimeout(aviso);
  if (token !== peticionEnCurso) return;

  ultimaBusqueda = data;
  pintarResultados(box, data);
}

function pintarResultados(box, data) {
  // Si el foco está en un control de la barra (llegar aquí con el teclado es
  // lo normal al cambiar el orden), se devuelve tras repintar.
  const enfocado = document.activeElement;
  const idEnfocado = box.contains(enfocado) ? enfocado.id : null;

  box.innerHTML = `
    <div class="toolbar">
      <strong>${num(data.total)}</strong><span class="muted">productos encontrados</span>
      <div class="toolbar__spacer"></div>
      <select id="sort" style="width:auto">
        ${[['relevance', 'Relevancia'], ['price_asc', 'Precio ↑'], ['price_desc', 'Precio ↓'],
           ['discount', 'Mayor ahorro'], ['stores', 'Más tiendas'], ['name', 'Nombre'],
           ['updated', 'Actualizado']]
          .map(([v, l]) => `<option value="${v}" ${state.filters.sort === v ? 'selected' : ''}>${l}</option>`).join('')}
      </select>
      ${esMovil() ? '' : `<div class="viewmodes">
        ${[['cards', 'Tarjetas'], ['list', 'Lista']]
          .map(([v, l]) => `<button data-mode="${v}" class="${state.viewMode === v ? 'is-active' : ''}">${l}</button>`).join('')}
      </div>`}
    </div>
    <div class="results-body">
      ${data.items.length ? renderResults(data.items) : emptyState()}
      ${data.pages > 1 ? pagination(data) : ''}
    </div>`;

  // Se quita ya, sin esperar a `requestAnimationFrame`: en una pestaña en
  // segundo plano ese callback no se ejecuta y la lista se quedaba atenuada
  // para siempre.
  box.classList.remove('is-loading');
  // Al filtrar o cambiar de vista entran los resultados, no la barra de
  // herramientas: el número de productos y los botones se quedan quietos.
  aparecer(box.querySelector('.results-body'));

  if (idEnfocado) {
    const control = document.getElementById(idEnfocado);
    if (control) control.focus({ preventScroll: true });
  }

  document.getElementById('sort').onchange = (e) => {
    state.filters.sort = e.target.value;
    state.filters.page = 1;
    syncHash();
    loadResults();
  };
  box.querySelectorAll('.viewmodes button').forEach((btn) => {
    btn.onclick = () => {
      if (state.viewMode === btn.dataset.mode) return;
      state.viewMode = btn.dataset.mode;
      localStorage.setItem('viewMode', state.viewMode);
      // Cambiar de tarjetas a lista es la misma búsqueda pintada de otra
      // forma: no hay que volver a preguntarle al servidor.
      loadResults({ refetch: false });
    };
  });
  box.querySelectorAll('[data-page]').forEach((btn) => {
    btn.onclick = () => {
      state.filters.page = Number(btn.dataset.page);
      syncHash();
      loadResults();
      window.scrollTo({ top: 0, behavior: 'smooth' });
    };
  });
}

function emptyState() {
  // Si el juego elegido aún no tiene catálogo, el vacío no es un "no encontré
  // nada": es que todavía no hay ninguna tienda configurada para ese TCG.
  const juego = juegoInfo(state.filters.game);
  if (juego && !juego.available) {
    return `<div class="empty">
      <h3>Todavía no hay catálogo de ${esc(juego.name)}</h3>
      <p>Ninguna de las tiendas configuradas publica productos de este juego.</p>
      <p class="hint">Para añadirlo, crea un archivo en <span class="mono">config/stores/</span>
         con <span class="mono">default_game: ${esc(juego.code)}</span> y pulsa
         <strong>Actualizar todas</strong> en la pantalla Tiendas.</p>
    </div>`;
  }
  return `<div class="empty"><h3>Sin resultados</h3>
    <p>Prueba con menos filtros o ejecuta una actualización para descubrir productos.</p></div>`;
}

function pagination(data) {
  return `<div class="pagination">
    <button class="btn btn--sm" data-page="${data.page - 1}" ${data.page <= 1 ? 'disabled' : ''}>‹ Anterior</button>
    <span>Página ${data.page} de ${data.pages}</span>
    <button class="btn btn--sm" data-page="${data.page + 1}" ${data.page >= data.pages ? 'disabled' : ''}>Siguiente ›</button>
  </div>`;
}

function renderResults(items) {
  // La tabla no cabe en un teléfono: siete columnas obligan a desplazarse a
  // lo ancho y se pierde de vista justo lo que se compara. Ahí, tarjetas
  // siempre, sin importar lo que estuviera elegido en el escritorio.
  if (state.viewMode === 'list' && !esMovil()) return listView(items);
  return `<div class="grid-cards">${items.map(productCard).join('')}</div>`;
}

function productCard(p) {
  return `<a class="pcard" href="#/producto/${p.id}">
    <div class="pcard__img">
      ${p.badge ? `<span class="pcard__badge">${esc(p.badge)}</span>` : ''}
      ${p.image_url ? `<img src="${esc(p.image_url)}" alt="" loading="lazy">` : ''}</div>
    <div class="pcard__body">
      <div class="pcard__name">${esc(p.name)}</div>
      <div class="pcard__tags">
        ${langTag(p.language, p.language_name)}
        ${p.in_stock_count ? '<span class="tag tag--ok">Disponible</span>'
                           : '<span class="tag tag--off">Agotado</span>'}
      </div>
      <div class="pcard__foot">
        <div>
          <div class="pcard__price">${money(p.best_price)}</div>
          <div class="pcard__store">${esc(p.best_store || '—')} · ${num(p.stores_count)} tiendas</div>
        </div>
        ${p.max_savings > 1 ? `<span class="tag tag--save">-${pct(p.max_savings, 0)}</span>` : ''}
      </div>
    </div></a>`;
}

function listView(items) {
  return `<div class="card table-wrap"><table class="table">
    <thead><tr><th>Producto</th><th>Idioma</th><th class="num">Mejor precio</th>
      <th>Stock</th><th class="num">Actualizado</th></tr></thead>
    <tbody>${items.map((p) => `<tr onclick="location.hash='#/producto/${p.id}'" style="cursor:pointer">
      <td><div class="rowline">${p.image_url ? `<img src="${esc(p.image_url)}" alt="" loading="lazy">` : ''}
        <strong>${esc(p.name)}</strong></div></td>
      <td class="nowrap">${langTag(p.language, p.language_name)}</td>
      <td class="num"><span class="price-cell price-cell--best">${money(p.best_price)}</span></td>
      <td>${p.in_stock_count ? STOCK_TAG.in_stock : STOCK_TAG.out_of_stock}</td>
      <td class="num nowrap muted">${ago(p.last_scraped_at)}</td>
    </tr>`).join('')}</tbody></table></div>`;
}

// =====================================================================
// Ficha de producto
// =====================================================================
route('producto', async (view, [id]) => {
  const [p, history] = await Promise.all([
    api.get(`/api/products/${id}`),
    api.get(`/api/products/${id}/history`, { days: 90 }),
  ]);

  const best = p.offers.find((o) => o.is_best) || p.offers[0];
  const priced = p.offers.filter((o) => o.price !== null);

  view.innerHTML = `
    <div class="page-head">
      <div><a class="btn btn--ghost btn--sm" href="#/buscar">‹ Volver a la búsqueda</a></div>
      <div>
        <button class="btn" id="btn-fav">${p.is_favorite ? '❤️ En favoritos' : '🤍 Añadir a favoritos'}</button>
        <button class="btn" id="btn-unir" title="Unir este producto con otro que sea el mismo artículo">🔗 Unir con otro</button>
        <a class="btn" href="/api/export?format=xlsx&product_id=${p.id}">Exportar</a>
      </div>
    </div>

    <div class="product-hero section">
      <div class="hero__img">${p.image_url ? `<img src="${esc(p.image_url)}" alt="">` : '<span class="muted">Sin imagen</span>'}</div>
      <div>
        <h1 class="hero__title">${esc(p.name)}</h1>
        <div class="hero__meta">
          ${langTag(p.language, p.language_name)}
          ${p.in_stock_count
            ? `<span class="tag tag--ok">${num(p.in_stock_count)} tiendas con stock</span>`
            : '<span class="tag tag--off">Sin stock en ninguna tienda</span>'}
        </div>
        ${(p.other_languages || []).length ? `<div class="otherlang">
          ${p.other_languages.map((o) => `
            <a class="btn btn--sm otherlang__btn" href="#/producto/${o.id}">
              ${bandera(o.language)} Ver en ${esc(o.language_name)}
              ${o.best_price ? `<span class="muted">· ${money(o.best_price)}</span>` : ''}
            </a>`).join('')}
        </div>` : ''}
        <div class="pricestats">
          <div><span>Mínimo histórico</span><strong>${money(p.history_stats.min_price)}</strong></div>
          <div><span>Máximo histórico</span><strong>${money(p.history_stats.max_price)}</strong></div>
          <div><span>Actualizado</span><strong>${ago(p.last_scraped_at)}</strong></div>
        </div>

        <div class="alertbox">
          <span class="alertbox__label">🔔 Avísame si baja de</span>
          <input type="number" id="alert-price" class="alertbox__input"
                 placeholder="${p.best_price ? Math.round(p.best_price * 0.9) : ''}">
          <button class="btn btn--primary btn--sm" id="btn-alert">Crear alerta</button>
        </div>
      </div>
      <div>
        <div class="bestbox">
          <div class="bestbox__label">Mejor precio</div>
          <div class="bestbox__price">${money(p.best_price)}</div>
          <div class="bestbox__store">${esc(best ? best.store : '—')}</div>
          ${best && best.unit_price ? `<div class="muted" style="font-size:13px">${money(best.unit_price)} por ${esc(p.unit_name || 'unidad')}</div>` : ''}
          <div class="bestbox__row">
            <span>Ahorro máximo</span>
            <strong>${money(p.max_savings_amount)} · ${pct(p.max_savings_pct)}</strong>
          </div>
          ${best ? `<a class="btn btn--primary" style="width:100%;margin-top:12px" href="${esc(best.url)}" target="_blank" rel="noopener">Ir a la tienda ↗</a>` : ''}
        </div>
      </div>
    </div>

    <div class="section">
      <div class="card">
        <div class="card__head"><h2>Comparación entre tiendas</h2>
          <span class="muted" style="font-size:13px">${num(priced.length)} ofertas con precio</span></div>
        <div class="table-wrap"><table class="table">
          <thead><tr><th></th><th>Tienda</th><th>Producto en la tienda</th>
            <th class="num">Precio</th><th class="num">Diferencia</th><th>Stock</th>
            <th class="num">Actualizado</th><th></th></tr></thead>
          <tbody id="offer-rows">${p.offers.slice(0, OFERTAS_VISIBLES).map(offerRow).join('')}</tbody>
        </table></div>
        ${p.offers.length > OFERTAS_VISIBLES ? `<div class="more-offers">
          <button class="btn btn--sm" id="btn-more-offers">Cargar más tiendas</button>
          <span class="muted" id="more-offers-count"></span>
        </div>` : ''}
      </div>
    </div>

    <div class="section">
      <div class="card">
        <div class="card__head">
          <h2>Historial de precios${best ? ` · ${esc(best.store)}` : ''}</h2>
          <select id="hist-days" style="width:auto">
            <option value="30">30 días</option><option value="90" selected>90 días</option>
            <option value="365">1 año</option></select></div>
        <div class="card__body"><div id="chart-box">${chartHtml(history, best && best.store)}</div></div>
      </div>
    </div>

    <div class="section">
      <div class="card">
        <div class="card__head"><h2>Comentarios</h2>
          <span class="muted" style="font-size:13px" id="comment-count"></span></div>
        <div class="card__body">
          <form class="commentbox" id="comment-form">
            <input id="comment-author" class="commentbox__author" maxlength="60"
                   placeholder="Tu nombre (opcional)" value="${esc(localStorage.getItem('autor') || '')}">
            <textarea id="comment-body" class="commentbox__body" rows="3" maxlength="4000"
                      placeholder="Anota lo que quieras recordar de este producto: dónde lo viste más barato, si la preventa se retrasó, qué tienda respondió bien…"></textarea>
            <div class="commentbox__actions">
              <button class="btn btn--primary btn--sm" type="submit">Publicar</button>
            </div>
          </form>
          <div id="comment-list"></div>
        </div>
      </div>
    </div>`;

  document.getElementById('btn-fav').onclick = async (e) => {
    const now = !p.is_favorite;
    await (now ? api.post(`/api/products/${p.id}/favorite`) : api.del(`/api/products/${p.id}/favorite`));
    p.is_favorite = now;
    e.target.textContent = now ? '❤️ En favoritos' : '🤍 Añadir a favoritos';
    toast(now ? 'Añadido a favoritos' : 'Quitado de favoritos');
  };

  document.getElementById('btn-unir').onclick = () => dialogoUnir(p);

  document.getElementById('btn-alert').onclick = async () => {
    const value = Number(document.getElementById('alert-price').value);
    if (!value) return toast('Escribe un precio objetivo', true);
    await api.post('/api/alerts', { product_id: p.id, target_price: value, only_in_stock: true });
    toast(`Alerta creada: avisaremos bajo ${money(value)}`);
    refreshBadges();
  };

  document.getElementById('hist-days').onchange = async (e) => {
    const fresh = await api.get(`/api/products/${p.id}/history`, { days: e.target.value });
    document.getElementById('chart-box').innerHTML = chartHtml(fresh, best && best.store);
  };

  bindOfferRows(view);
  await bindComments(p.id);

  // Las ofertas vienen ordenadas de más barata a más cara, así que las diez
  // primeras son las que interesan. El resto se cargan a petición para que
  // un producto en quince tiendas no tape el historial de precios.
  const masOfertas = document.getElementById('btn-more-offers');
  if (masOfertas) {
    const cuerpo = document.getElementById('offer-rows');
    const contador = document.getElementById('more-offers-count');
    const actualizar = () => {
      const faltan = p.offers.length - cuerpo.rows.length;
      contador.textContent = `Quedan ${faltan} de ${p.offers.length} tiendas`;
    };
    actualizar();

    masOfertas.onclick = () => {
      const desde = cuerpo.rows.length;
      const lote = p.offers.slice(desde, desde + OFERTAS_VISIBLES);
      const provisional = document.createElement('tbody');
      provisional.innerHTML = lote.map(offerRow).join('');
      const nuevas = [...provisional.rows];
      nuevas.forEach((fila) => cuerpo.appendChild(fila));
      nuevas.forEach(aparecer);
      bindOfferRows(cuerpo);

      if (cuerpo.rows.length >= p.offers.length) masOfertas.closest('.more-offers').remove();
      else actualizar();
    };
  }
});

// Cuántas ofertas se enseñan de entrada, y cuántas añade cada «Cargar más».
const OFERTAS_VISIBLES = 10;

// ---------------------------------------------------------------- comentarios
//
// Guardados en la propia base de la aplicación. No hay servicio externo: la
// ficha no pide nada por internet ni hay cuenta que crear, que es lo mismo
// que vale para el resto del proyecto.
async function bindComments(productId) {
  const lista = document.getElementById('comment-list');
  const contador = document.getElementById('comment-count');
  const formulario = document.getElementById('comment-form');
  if (!lista) return;

  const pintar = (comentarios) => {
    contador.textContent = comentarios.length
      ? `${num(comentarios.length)} ${comentarios.length === 1 ? 'comentario' : 'comentarios'}`
      : '';
    lista.innerHTML = comentarios.length
      ? comentarios.map(commentCard).join('')
      : `<p class="muted" style="font-size:13px">Todavía no hay comentarios en este producto.</p>`;
    lista.querySelectorAll('[data-del-comment]').forEach((btn) => {
      btn.onclick = async () => {
        if (!confirm('¿Borrar este comentario?')) return;
        await api.del(`/api/comments/${btn.dataset.delComment}`);
        pintar(await api.get(`/api/products/${productId}/comments`));
        toast('Comentario borrado');
      };
    });
  };

  pintar(await api.get(`/api/products/${productId}/comments`).catch(() => []));

  formulario.onsubmit = async (e) => {
    e.preventDefault();
    const cuerpo = document.getElementById('comment-body');
    const autor = document.getElementById('comment-author');
    if (!cuerpo.value.trim()) return toast('Escribe algo antes de publicar', true);
    try {
      await api.post(`/api/products/${productId}/comments`,
        { body: cuerpo.value, author: autor.value });
      localStorage.setItem('autor', autor.value.trim());
      cuerpo.value = '';
      pintar(await api.get(`/api/products/${productId}/comments`));
      toast('Comentario publicado');
    } catch (err) {
      toast(err.message, true);
    }
  };
}

function commentCard(c) {
  return `<div class="comment">
    <div class="comment__head">
      <strong>${esc(c.author || 'Anónimo')}</strong>
      <span class="muted">${ago(c.created_at)}</span>
      <button class="btn btn--ghost btn--sm" data-del-comment="${c.id}" title="Borrar">✕</button>
    </div>
    <div class="comment__body">${esc(c.body)}</div>
  </div>`;
}

function bindOfferRows(ambito) {
  ambito.querySelectorAll('[data-split]').forEach((btn) => {
    btn.onclick = async () => {
      if (!confirm('¿Separar esta oferta del producto agrupado? La decisión se recordará.')) return;
      await api.post(`/api/offers/${btn.dataset.split}/split`);
      toast('Oferta separada. Se recordará para próximas actualizaciones.');
      render();
    };
  });
  bindVariantWarnings(ambito);
}

// ------------------------------------------- aviso antes de ir a la tienda
//
// Hay tiendas (las Bsale) que venden varias versiones en la misma página y no
// admiten un enlace por variante: al llegar sale marcada la que ellas
// eligen, no la que se estaba mirando aquí. Antes de abrir la ficha se avisa,
// porque el precio que verá ahí puede no ser el que le hizo pulsar.
//
// Se puede silenciar por tienda: la decisión se guarda en el navegador.
function silenciada(codigoTienda) {
  return localStorage.getItem(`aviso-variante:${codigoTienda}`) === 'no';
}

function avisoVariante(datos) {
  const dialogo = document.createElement('dialog');
  dialogo.className = 'modal';
  dialogo.innerHTML = `
    <h3 class="modal__title">Al llegar a ${esc(datos.store)}, elige la versión</h3>
    <p class="modal__text">
      Esta tienda vende varias versiones en la misma página y no permite
      enlazar a una en concreto, así que abrirá con la que ella tenga marcada.
    </p>
    <div class="modal__pick">
      <span class="modal__pick-label">Elige la opción</span>
      <strong>${esc(datos.pick)}</strong>
      <span class="muted">— es la de ${esc(datos.price)}</span>
    </div>
    <label class="check modal__mute">
      <input type="checkbox" id="modal-mute"> No volver a avisarme de ${esc(datos.store)}
    </label>
    <div class="modal__actions">
      <button class="btn" value="cancel" id="modal-cancel">Cancelar</button>
      <button class="btn btn--primary" value="go" id="modal-go">Ir a la tienda ↗</button>
    </div>`;
  document.body.appendChild(dialogo);
  dialogo.showModal();

  const cerrar = (ir) => {
    if (document.getElementById('modal-mute').checked) {
      localStorage.setItem(`aviso-variante:${datos.storeCode}`, 'no');
    }
    dialogo.close();
    dialogo.remove();
    // `window.open` va dentro del clic del botón: así el navegador lo trata
    // como acción del usuario y no lo bloquea como ventana emergente.
    if (ir) window.open(datos.url, '_blank', 'noopener');
  };
  dialogo.querySelector('#modal-go').onclick = () => cerrar(true);
  dialogo.querySelector('#modal-cancel').onclick = () => cerrar(false);
  dialogo.addEventListener('cancel', () => { dialogo.remove(); });
}

// ---------------------------------------------------------------------
// Unir dos productos que en realidad son el mismo
//
// Pasa cuando dos tiendas escriben tan distinto el mismo artículo que el
// agrupador automático no se atreve a juntarlos. La decisión se guarda contra
// la clave estable de cada oferta, así que sobrevive a los scrapings.
// ---------------------------------------------------------------------
function dialogoUnir(producto) {
  const dialogo = document.createElement('dialog');
  dialogo.className = 'modal modal--wide';
  dialogo.innerHTML = `
    <h3 class="modal__title">Unir con otro producto</h3>
    <p class="modal__text">
      Los dos pasarán a ser uno solo: se juntan sus tiendas y sus precios.
      Se recuerda para siempre, también después de cada actualización.
    </p>
    <div class="merge__base">
      <span class="muted">Este producto</span>
      <strong>${esc(producto.name)}</strong>
    </div>
    <input type="search" class="merge__search" id="merge-q"
           placeholder="Buscar por nombre, o deja vacío para ver los parecidos">
    <div class="merge__list" id="merge-list"><p class="muted">Buscando…</p></div>
    <p class="merge__msg" id="merge-msg" hidden></p>
    <div class="modal__actions">
      <button class="btn" id="merge-cancel">Cancelar</button>
    </div>`;
  document.body.appendChild(dialogo);
  dialogo.showModal();

  const lista = dialogo.querySelector('#merge-list');
  const buscador = dialogo.querySelector('#merge-q');
  const mensaje = dialogo.querySelector('#merge-msg');

  const cerrar = () => { dialogo.close(); dialogo.remove(); };
  dialogo.querySelector('#merge-cancel').onclick = cerrar;
  dialogo.addEventListener('cancel', () => dialogo.remove());

  const pintar = (candidatos) => {
    if (!candidatos.length) {
      lista.innerHTML = '<p class="muted">Ningún producto coincide.</p>';
      return;
    }
    lista.innerHTML = candidatos.map((c) => `
      <button class="merge__item" data-id="${c.id}">
        <span class="merge__thumb">${c.image_url ? `<img src="${esc(c.image_url)}" alt="">` : ''}</span>
        <span class="merge__info">
          <strong>${esc(c.name)}</strong>
          <span class="muted">${esc(c.set_name || '')}${c.stores_count ? ` · ${num(c.stores_count)} tienda(s)` : ''}</span>
        </span>
        <span class="merge__price">${c.best_price ? money(c.best_price) : ''}</span>
      </button>`).join('');

    lista.querySelectorAll('.merge__item').forEach((btn) => {
      btn.onclick = async () => {
        const otro = candidatos.find((c) => String(c.id) === btn.dataset.id);
        if (!confirm(`¿Unir «${producto.name}» con «${otro.name}»?\n\nSe convertirán en un solo producto.`)) return;

        mensaje.hidden = true;
        lista.querySelectorAll('.merge__item').forEach((b) => { b.disabled = true; });
        try {
          const r = await api.post(`/api/products/${producto.id}/merge`, { other_id: otro.id });
          if (!r.merged) throw new Error(
            'No se pudieron unir. Alguna separación manual entre sus ofertas lo impide.');

          cerrar();
          toast(r.forgotten_separations
            ? `Productos unidos (se olvidaron ${r.forgotten_separations} separación(es) que habías hecho antes)`
            : 'Productos unidos');
          // El maestro resultante puede ser cualquiera de los dos.
          if (String(r.product_id) === String(producto.id)) location.reload();
          else location.hash = `#/producto/${r.product_id}`;
        } catch (err) {
          // En el diálogo y no en un aviso flotante: el motivo del rechazo es
          // largo y explica qué hacer, así que hay que poder leerlo con calma.
          mensaje.textContent = err.message;
          mensaje.hidden = false;
          lista.querySelectorAll('.merge__item').forEach((b) => { b.disabled = false; });
        }
      };
    });
  };

  const cargar = async () => {
    lista.innerHTML = '<p class="muted">Buscando…</p>';
    try {
      pintar(await api.get(`/api/products/${producto.id}/merge-candidates`,
                           { q: buscador.value.trim() }));
    } catch (err) {
      lista.innerHTML = `<p class="muted">${esc(err.message)}</p>`;
    }
  };

  let temporizador = null;
  buscador.oninput = () => {
    clearTimeout(temporizador);
    temporizador = setTimeout(cargar, 250);
  };
  cargar();
}

function bindVariantWarnings(raiz) {
  raiz.querySelectorAll('a[data-pick]').forEach((enlace) => {
    enlace.addEventListener('click', (e) => {
      // Con Ctrl/⌘ o botón central el usuario ya sabe lo que hace: se respeta.
      if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
      if (silenciada(enlace.dataset.storeCode)) return;
      e.preventDefault();
      avisoVariante({
        store: enlace.dataset.store,
        storeCode: enlace.dataset.storeCode,
        pick: enlace.dataset.pick,
        price: enlace.dataset.price,
        url: enlace.href,
      });
    });
  });
}

function offerRow(o) {
  const icon = o.rank && o.rank <= 3 ? RANK_ICON[o.rank - 1] : (o.rank ? o.rank + '.' : '');
  return `<tr>
    <td><span class="rank ${o.rank <= 3 ? 'rank--' + o.rank : 'rank--n'}">${icon}</span></td>
    <td><strong>${esc(o.store)}</strong></td>
    <td><a href="${esc(o.url)}" target="_blank" rel="noopener"
        ${o.pick_variant ? `data-pick="${esc(o.pick_variant)}" data-store="${esc(o.store)}"
           data-store-code="${esc(o.store_code)}" data-price="${esc(money(o.price))}"` : ''}
        >${esc(o.name)} ↗</a>
      ${o.pick_variant ? `<div class="hint-variant" title="La tienda vende varias versiones en la misma página y no admite un enlace por variante">
          ⚠ En la tienda elige la opción «${esc(o.pick_variant)}»</div>` : ''}
      ${o.ean ? `<div class="muted mono" style="font-size:11px">EAN ${esc(o.ean)}</div>` : ''}</td>
    <td class="num"><span class="price-cell ${o.is_best ? 'price-cell--best' : ''}">${money(o.price)}</span></td>
    <td class="num">${o.difference ? `<span class="delta">+${money(o.difference)}</span>
        <div class="muted" style="font-size:11px">${pct(o.difference_pct)}</div>` : '<span class="muted">—</span>'}</td>
    <td>${STOCK_TAG[o.stock_status] || STOCK_TAG.unknown}</td>
    <td class="num nowrap muted">${ago(o.last_seen_at)}</td>
    <td><button class="btn btn--ghost btn--sm" data-split="${o.id}" title="Separar del grupo">✂</button></td>
  </tr>`;
}

// ------------------------------------------------------------- gráfico SVG
const CHART_COLORS = ['#2f6df6', '#12855a', '#d99b18', '#cf3535', '#8e44ad', '#0aa2c0'];

// `soloTienda` limita el gráfico a una sola tienda: normalmente la que tiene
// el mejor precio, que es la que interesa seguir. Con una línea por tienda el
// gráfico se volvía ilegible en cuanto había cinco o seis ofertas.
function chartHtml(history, soloTienda) {
  let series = (history.series || []).filter((s) => s.points.length);
  if (soloTienda) {
    const filtrada = series.filter((s) => s.store === soloTienda);
    if (filtrada.length) series = filtrada;
  }
  if (!series.length) {
    return '<div class="empty"><h3>Sin historial todavía</h3><p>El gráfico se llena con cada actualización.</p></div>';
  }

  const all = series.flatMap((s) => s.points);
  const times = all.map((p) => parseDate(p.t)?.getTime()).filter(Boolean);
  const prices = all.map((p) => p.price);
  const minT = Math.min(...times), maxT = Math.max(...times);
  let minP = Math.min(...prices), maxP = Math.max(...prices);
  const pad = (maxP - minP) * 0.12 || maxP * 0.05 || 1;
  minP -= pad; maxP += pad;

  const W = 860, H = 260, L = 62, R = 14, T = 14, B = 28;
  const x = (t) => L + ((t - minT) / (maxT - minT || 1)) * (W - L - R);
  const y = (p) => T + (1 - (p - minP) / (maxP - minP || 1)) * (H - T - B);

  const gridLines = [0, 0.25, 0.5, 0.75, 1].map((f) => {
    const price = minP + f * (maxP - minP);
    const py = y(price);
    return `<line class="grid-line" x1="${L}" y1="${py}" x2="${W - R}" y2="${py}"></line>
            <text x="${L - 8}" y="${py + 4}" text-anchor="end">${money(Math.round(price))}</text>`;
  }).join('');

  const paths = series.map((s, i) => {
    const color = CHART_COLORS[i % CHART_COLORS.length];
    const pts = s.points
      .map((p) => ({ t: parseDate(p.t)?.getTime(), price: p.price }))
      .filter((p) => p.t)
      .sort((a, b) => a.t - b.t);
    if (!pts.length) return '';
    // Escalonada: el precio se mantiene hasta que cambia.
    let d = `M ${x(pts[0].t)} ${y(pts[0].price)}`;
    for (let k = 1; k < pts.length; k++) {
      d += ` L ${x(pts[k].t)} ${y(pts[k - 1].price)} L ${x(pts[k].t)} ${y(pts[k].price)}`;
    }
    d += ` L ${x(maxT)} ${y(pts[pts.length - 1].price)}`;
    const dots = pts.map((p) => `<circle cx="${x(p.t)}" cy="${y(p.price)}" r="2.5" fill="${color}"></circle>`).join('');
    return `<path class="line" d="${d}" stroke="${color}"></path>${dots}`;
  }).join('');

  const ticks = [0, 0.5, 1].map((f) => {
    const t = minT + f * (maxT - minT);
    const label = new Date(t).toLocaleDateString('es-CL', { day: '2-digit', month: '2-digit' });
    return `<text x="${x(t)}" y="${H - 8}" text-anchor="middle">${label}</text>`;
  }).join('');

  const legend = series.map((s, i) =>
    `<span><i style="background:${CHART_COLORS[i % CHART_COLORS.length]}"></i>${esc(s.store)}</span>`).join('');

  return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
      ${gridLines}
      <line class="axis" x1="${L}" y1="${T}" x2="${L}" y2="${H - B}"></line>
      <line class="axis" x1="${L}" y1="${H - B}" x2="${W - R}" y2="${H - B}"></line>
      ${paths}${ticks}
    </svg><div class="chart-legend">${legend}</div>`;
}

// =====================================================================
// Favoritos
// =====================================================================
route('favoritos', async (view) => {
  const data = await api.get('/api/favorites');
  view.innerHTML = `
    <div class="page-head"><div><h1>Favoritos</h1>
      <p>Seguimiento de los productos que te interesan</p></div></div>
    ${data.items.length ? `<div class="grid-cards">${data.items.map(productCard).join('')}</div>`
      : `<div class="empty"><h3>Todavía no tienes favoritos</h3>
         <p>Marca productos con ❤️ desde su ficha para verlos aquí.</p></div>`}`;
});

// =====================================================================
// Revisión manual de matches
// =====================================================================
// Cuántos pares se piden. Es el mismo número al cargar y al refrescar: si no,
// la lista «encoge» sola según vas decidiendo.
const REVIEWS_SIZES = [20, 60, 100, 200];
let REVIEWS_LIMIT = Number(localStorage.getItem('revLimit')) || 60;
const DECISIONS_LIMIT = 50;

const vistaRevision = async (view) => {
  if (!REVIEWS_SIZES.includes(REVIEWS_LIMIT)) REVIEWS_LIMIT = 60;

  // El total de pendientes sale de aquí; si aún no se ha pedido, se pide,
  // para que el «de N» salga siempre y no según quién llegue antes.
  const [reviews, decisions, langs] = await Promise.all([
    api.get('/api/reviews', { limit: REVIEWS_LIMIT }),
    api.get('/api/manual-decisions', { limit: DECISIONS_LIMIT }),
    api.get('/api/languages'),
    state.totals ? Promise.resolve() : refreshBadges().catch(() => {}),
  ]);
  state.languages = langs;

  view.innerHTML = `
    <div class="page-head">
      <div><h1>Productos pendientes de confirmar</h1>
        <p>Pares con puntaje entre el umbral de revisión y el de agrupación automática.
           Tu decisión se recuerda para siempre, y puedes deshacerla abajo.</p></div>
      <div class="status">
        <label class="revsize">Mostrar
          <select id="rev-limit">
            ${REVIEWS_SIZES.map((n) => `<option value="${n}" ${n === REVIEWS_LIMIT ? 'selected' : ''}>${n} pares</option>`).join('')}
          </select>
        </label>
        <div><strong id="rev-count">${num(reviews.length)}</strong> en esta página
          ${state.totals && state.totals.pending_reviews
            ? `<span class="muted">de ${num(state.totals.pending_reviews)}</span>` : ''}</div>
        <div id="regroup-hint" class="muted"></div>
      </div>
    </div>

    <div id="review-list">
      ${reviews.length ? reviews.map(reviewCard).join('') : emptyReviews()}
    </div>

    <div class="section" style="margin-top:32px">
      <div class="card">
        <div class="card__head"><h2>Últimos marcados</h2>
          <span class="muted" style="font-size:13px">
            Pulsa «Deshacer» si te equivocaste: el par vuelve a la lista de arriba.</span></div>
        <div class="table-wrap"><table class="table">
          <thead><tr><th>Producto A</th><th>Producto B</th><th>Decisión</th>
            <th class="num">Cuándo</th><th></th></tr></thead>
          <tbody id="decision-list">${decisionRows(decisions)}</tbody>
        </table></div>
      </div>
    </div>`;

  document.getElementById('rev-limit').onchange = async (e) => {
    REVIEWS_LIMIT = Number(e.target.value);
    localStorage.setItem('revLimit', String(REVIEWS_LIMIT));
    // Solo se repinta la lista: la barra de arriba y los últimos marcados se
    // quedan donde están, así el desplegable no pierde el foco.
    const lista = document.getElementById('review-list');
    lista.classList.add('is-loading');
    const frescos = await api.get('/api/reviews', { limit: REVIEWS_LIMIT });
    lista.classList.remove('is-loading');
    lista.innerHTML = frescos.length ? frescos.map(reviewCard).join('') : emptyReviews();
    document.getElementById('rev-count').textContent = num(frescos.length);
    bindReviewActions(lista);
    aparecer(lista);
  };

  bindReviewActions(view);
  bindUndoActions(view);
};

function emptyReviews() {
  return `<div class="empty"><h3>No hay nada pendiente</h3>
    <p>Todos los productos se agruparon con confianza suficiente.</p></div>`;
}

function decisionRows(decisions) {
  if (!decisions.length) {
    return '<tr><td colspan="5" class="muted">Todavía no has marcado ningún par.</td></tr>';
  }
  const lado = (s) => s.missing
    ? `<span class="muted">${esc(s.store)} · producto retirado</span>`
    : `<strong>${esc(s.name)}</strong>
       <div class="muted" style="font-size:12px">${esc(s.store)}
         ${s.language_name ? '· ' + esc(s.language_name) : ''}
         ${s.price ? '· ' + money(s.price) : ''}</div>`;

  return decisions.map((d) => `<tr data-decision-row="${d.id}">
      <td>${lado(d.a)}</td>
      <td>${lado(d.b)}</td>
      <td>${d.decision === 'same' ? '<span class="tag tag--ok">Mismo producto</span>'
                                  : '<span class="tag tag--off">Productos distintos</span>'}</td>
      <td class="num nowrap muted">${ago(d.created_at)}</td>
      <td><button class="btn btn--sm" data-undo="${d.id}">↶ Deshacer</button></td>
    </tr>`).join('');
}

// Refresca solo la tabla de últimos marcados, sin tocar el resto de la página.
// Mientras se refresca, los botones de deshacer quedan bloqueados para que no
// se pueda pulsar uno que está a punto de ser reemplazado por otra fila.
async function refreshDecisions() {
  const cuerpo = document.getElementById('decision-list');
  if (!cuerpo) return;
  cuerpo.querySelectorAll('[data-undo]').forEach((b) => { b.disabled = true; });
  const decisions = await api.get('/api/manual-decisions', { limit: DECISIONS_LIMIT });
  cuerpo.innerHTML = decisionRows(decisions);
  bindUndoActions(cuerpo);
}

function regroupHint(respuesta) {
  const el = document.getElementById('regroup-hint');
  if (!el) return;
  el.innerHTML = respuesta && respuesta.regroup && respuesta.regroup.scheduled
    ? '<span class="spinner" style="width:11px;height:11px"></span> reagrupando…'
    : '';
  clearTimeout(regroupHint._t);
  regroupHint._t = setTimeout(() => { el.innerHTML = ''; refreshBadges(); }, 3500);
}

// La misma oferta puede estar en varias tarjetas: el idioma se refleja en
// todas sus apariciones, no solo en la que el usuario tocó.
function propagarIdioma(ofertaId, idioma) {
  document.querySelectorAll(`[data-set-lang="${ofertaId}"]`).forEach((s) => {
    s.value = idioma || '';
    s.closest('.review__lang')?.classList.toggle('review__lang--missing', !idioma);
  });
}

// Retira todas las tarjetas cuyos dos lados tengan idiomas conocidos y
// distintos: son productos distintos por definición y no hay nada que decidir.
function purgarIdiomasIncompatibles() {
  let retiradas = 0;
  document.querySelectorAll('[data-review-card]').forEach((tarjeta) => {
    const [uno, otro] = [...tarjeta.querySelectorAll('[data-set-lang]')].map((s) => s.value);
    if (uno && otro && uno !== otro) {
      retiradas += 1;
      quitarTarjetaRevision(tarjeta);
    }
  });
  return retiradas;
}

function quitarTarjetaRevision(tarjeta) {
  tarjeta.classList.add('is-leaving');
  setTimeout(() => {
    tarjeta.remove();
    const quedan = document.querySelectorAll('[data-review-card]').length;
    const contador = document.getElementById('rev-count');
    if (contador) contador.textContent = num(quedan);
    if (!quedan) {
      const lista = document.getElementById('review-list');
      if (lista) lista.innerHTML = emptyReviews();
    }
  }, 220);
}

function bindReviewActions(scope) {
  scope.querySelectorAll('[data-set-lang]').forEach((sel) => {
    sel.onchange = async () => {
      const valor = sel.value || null;
      sel.disabled = true;
      try {
        const r = await api.post(`/api/offers/${sel.dataset.setLang}/language`,
                                 { language: valor });
        regroupHint(r);
        sel.disabled = false;

        // Una misma oferta puede aparecer en VARIAS tarjetas (emparejada con
        // distintas tiendas). Hay que reflejar el cambio en todas, no solo en
        // la que se tocó, o las demás se quedan mostrando el valor viejo.
        propagarIdioma(sel.dataset.setLang, r.language);
        const retiradas = purgarIdiomasIncompatibles();

        toast(retiradas
          ? `Idiomas distintos → productos distintos. ${retiradas === 1
              ? 'Se retiró ese par' : `Se retiraron ${retiradas} pares`}, no hace falta decidir nada.`
          : (r.language ? 'Idioma fijado' : 'Idioma devuelto a automático'));
      } catch (err) {
        toast(err.message, true);
        sel.disabled = false;
      }
    };
  });

  scope.querySelectorAll('[data-decide]').forEach((btn) => {
    btn.onclick = async () => {
      const { a, b, decide } = btn.dataset;
      const tarjeta = btn.closest('[data-review-card]');
      // Quitamos la tarjeta en cuanto el servidor confirma que guardó la
      // decisión; la reagrupación va por detrás y no se espera.
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span>';
      try {
        const r = await api.post(
          decide === 'same' ? '/api/reviews/confirm' : '/api/reviews/reject',
          { a_id: Number(a), b_id: Number(b) });

        if (tarjeta) quitarTarjetaRevision(tarjeta);

        // Transitividad: si ya existía A=B y acabas de marcar B=C, el par A–C
        // queda respondido solo. El servidor devuelve esos pares implicados y
        // aquí se retiran sus tarjetas, sin que tengas que decidirlos otra vez.
        (r.implied || []).forEach((par) => {
          const otra = document.querySelector(`[data-review-card="${par.a_id}-${par.b_id}"]`)
                    || document.querySelector(`[data-review-card="${par.b_id}-${par.a_id}"]`);
          if (otra) quitarTarjetaRevision(otra);
        });

        const implicados = (r.implied || []).length;
        toast(decide !== 'same'
          ? 'Marcados como distintos ✓'
          : implicados
            ? `Agrupados ✓ · ${implicados} par${implicados > 1 ? 'es' : ''} más resuelto${implicados > 1 ? 's' : ''} por transitividad`
            : 'Agrupados ✓');
        regroupHint(r);
        // Se espera a que la tabla de "últimos marcados" quede al día: si se
        // dejara en segundo plano, un clic en "Deshacer" durante ese instante
        // actuaría sobre una fila que ya no es la que se ve.
        await refreshDecisions();
      } catch (err) {
        toast(err.message, true);
        btn.disabled = false;
        btn.textContent = decide === 'same' ? '✓ Mismo' : '✕ Distinto';
      }
    };
  });
}

function bindUndoActions(scope) {
  scope.querySelectorAll('[data-undo]').forEach((btn) => {
    btn.onclick = async () => {
      const fila = btn.closest('[data-decision-row]');
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span>';
      try {
        const r = await api.del(`/api/manual-decisions/${btn.dataset.undo}`);
        if (fila) fila.remove();
        toast('Decisión deshecha — el par vuelve a la lista de pendientes');
        regroupHint(r);
        // El par reaparece arriba, así que refrescamos solo esa lista.
        const reviews = await api.get('/api/reviews', { limit: REVIEWS_LIMIT });
        const lista = document.getElementById('review-list');
        if (lista) {
          lista.innerHTML = reviews.length ? reviews.map(reviewCard).join('') : emptyReviews();
          bindReviewActions(lista);
          const contador = document.getElementById('rev-count');
          if (contador) contador.textContent = num(reviews.length);
        }
      } catch (err) {
        toast(err.message, true);
        btn.disabled = false;
        btn.textContent = '↶ Deshacer';
      }
    };
  });
}

function reviewCard(r) {
  const side = (prefix, store) => {
    const id = r[prefix + '_id'];
    const lang = r[prefix + '_lang'];
    return `
    <div class="review__side">
      <div class="review__store">${esc(store)}</div>
      <div class="review__name">${esc(r[prefix + '_name'])}</div>
      <div class="review__facts">
        ${r[prefix + '_set'] ? `<span class="tag tag--set">${esc(r[prefix + '_set'])}</span>` : '<span class="tag">set ?</span>'}
        ${r[prefix + '_type'] ? `<span class="tag">${esc(r[prefix + '_type'])}</span>` : '<span class="tag">tipo ?</span>'}
        ${r[prefix + '_units'] ? `<span class="tag">${r[prefix + '_units']} u.</span>` : ''}
        <span class="tag">${money(r[prefix + '_price'])}</span>
        ${STOCK_TAG[r[prefix + '_stock']] || ''}
      </div>
      <label class="review__lang ${lang ? '' : 'review__lang--missing'}">
        <span>Idioma</span>
        <select data-set-lang="${id}">
          <option value="">${lang ? 'Automático' : '— sin declarar —'}</option>
          ${(state.languages || []).map((l) =>
            `<option value="${esc(l.code)}" ${l.code === lang ? 'selected' : ''}>
               ${LANG_FLAG[l.code] || ''} ${esc(l.name)}</option>`).join('')}
        </select>
      </label>
      <a class="muted" style="font-size:12px" href="${esc(r[prefix + '_url'])}" target="_blank" rel="noopener">Ver en la tienda ↗</a>
    </div>`;
  };

  return `<div class="card" style="margin-bottom:14px"
               data-review-card="${r.a_id}-${r.b_id}"><div class="card__body">
    <div class="review">
      ${side('a', r.a_store)}
      <div class="review__center">
        <div class="review__score">${Number(r.score).toFixed(0)}%</div>
        <div class="muted" style="font-size:12px">${esc(r.method)}</div>
        <div class="review__actions">
          <button class="btn btn--success btn--sm" data-decide="same" data-a="${r.a_id}" data-b="${r.b_id}">✓ Mismo</button>
          <button class="btn btn--danger btn--sm" data-decide="diff" data-a="${r.a_id}" data-b="${r.b_id}">✕ Distinto</button>
        </div>
      </div>
      ${side('b', r.b_store)}
    </div>
    ${breakdownHtml(r.breakdown)}
  </div></div>`;
}

function breakdownHtml(breakdown) {
  if (!breakdown || !Object.keys(breakdown).length) return '';
  const rows = Object.entries(breakdown).map(([key, value]) => {
    if (value && typeof value === 'object') {
      return `<tr><td>${esc(key)}</td><td>${esc(JSON.stringify(value.value))}</td>
              <td>${value.points > 0 ? '+' : ''}${esc(value.points ?? '')}</td></tr>`;
    }
    return `<tr><td>${esc(key)}</td><td colspan="2">${esc(value)}</td></tr>`;
  }).join('');
  return `<details class="breakdown"><summary>Ver por qué el sistema los considera parecidos</summary>
    <table>${rows}</table></details>`;
}

// =====================================================================
// Tiendas
// =====================================================================
const vistaTiendas = async (view) => {
  const [stores, adapters] = await Promise.all([api.get('/api/stores'), api.get('/api/stores/adapters')]);

  view.innerHTML = `
    <div class="page-head">
      <div><h1>Tiendas</h1>
        <p>Cada tienda es un archivo YAML en <span class="mono">config/stores/</span>.
           Adaptadores disponibles: ${Object.keys(adapters).map((a) => `<span class="tag">${esc(a)}</span>`).join(' ')}</p></div>
      <div class="status" id="update-status"></div>
      <div class="nowrap">
        <button class="btn" id="btn-reload">Recargar configuración</button>
        <button class="btn btn--primary" id="btn-update">Actualizar todas</button>
      </div>
    </div>

    <div class="card table-wrap"><table class="table">
      <thead><tr><th>Tienda</th><th>Adaptador</th><th class="num">Productos</th>
        <th class="num">Sin precio</th><th class="num">Errores 7d</th><th class="num">Duración media</th>
        <th>Último scraping</th><th>Estado</th><th></th></tr></thead>
      <tbody>${stores.map(storeRow).join('')}</tbody>
    </table></div>

    <div id="store-errors" class="section" style="margin-top:24px"></div>`;

  document.getElementById('btn-reload').onclick = async () => {
    await api.post('/api/stores/reload');
    toast('Configuración recargada');
    state.facets = null;          // pueden haber cambiado los juegos
    render();
  };

  // La actualización manual vive aquí, junto a las tiendas que actualiza.
  document.getElementById('btn-update').onclick = async (e) => {
    try {
      await api.post('/api/update', {});
      toast('Actualización iniciada — puedes seguir navegando');
      e.target.disabled = true;
      pollStatus();
    } catch (err) { toast(err.message, true); }
  };
  pollStatus();

  view.querySelectorAll('[data-toggle]').forEach((btn) => {
    btn.onclick = async () => {
      await api.post(`/api/stores/${btn.dataset.toggle}/toggle?enabled=${btn.dataset.enable}`);
      render();
    };
  });
  view.querySelectorAll('[data-scrape]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        await api.post(`/api/stores/${btn.dataset.scrape}/scrape`);
        toast('Actualización iniciada');
        pollStatus();
      } catch (err) { toast(err.message, true); }
    };
  });
  view.querySelectorAll('[data-delete]').forEach((btn) => {
    btn.onclick = async () => {
      const name = btn.dataset.name;
      if (!confirm(`¿Eliminar «${name}»?\n\nSe borrarán sus productos y todo su historial de precios. Esta acción no se puede deshacer.\n\nRecuerda borrar también su archivo de config/stores/ para que no vuelva a aparecer.`)) return;
      const r = await api.del(`/api/stores/${btn.dataset.delete}`);
      toast(`«${name}» eliminada (${r.offers_removed} ofertas). Quedan ${r.matching.products} productos.`);
      render();
    };
  });

  view.querySelectorAll('[data-errors]').forEach((btn) => {
    btn.onclick = async () => {
      const errors = await api.get(`/api/stores/${btn.dataset.errors}/errors`, { limit: 40 });
      document.getElementById('store-errors').innerHTML = `
        <div class="card"><div class="card__head"><h2>Errores de ${esc(btn.dataset.name)}</h2></div>
        <div class="table-wrap"><table class="table">
          <thead><tr><th>Etapa</th><th>URL</th><th>Mensaje</th><th>Fecha</th></tr></thead>
          <tbody>${errors.map((e) => `<tr><td><span class="tag">${esc(e.stage)}</span></td>
            <td class="mono" style="max-width:280px;overflow:hidden;text-overflow:ellipsis">${esc(e.url || '')}</td>
            <td>${esc(e.message)}</td><td class="muted nowrap">${ago(e.created_at)}</td></tr>`).join('') ||
            '<tr><td colspan="4" class="muted">Sin errores registrados 🎉</td></tr>'}</tbody>
        </table></div></div>`;
      document.getElementById('store-errors').scrollIntoView({ behavior: 'smooth' });
    };
  });
};

function storeRow(s) {
  const statusTag = { ok: 'tag--ok', partial: 'tag--warn', error: 'tag--off' }[s.last_status] || 'tag';
  return `<tr>
    <td><strong>${esc(s.name)}</strong>
      <div class="muted mono" style="font-size:11px">${esc(s.base_url)}</div></td>
    <td><span class="tag">${esc(s.adapter)}</span></td>
    <td class="num">${num(s.products)}</td>
    <td class="num ${s.products_without_price ? 'muted' : ''}">${num(s.products_without_price)}</td>
    <td class="num">${s.recent_errors ? `<span class="tag tag--off">${num(s.recent_errors)}</span>` : '0'}</td>
    <td class="num">${s.avg_duration_ms ? (s.avg_duration_ms / 1000).toFixed(1) + ' s' : '—'}</td>
    <td class="nowrap muted">${ago(s.last_run_at)}</td>
    <td>${s.enabled ? `<span class="tag ${statusTag}">${esc(s.last_status || 'activa')}</span>`
                    : '<span class="tag">desactivada</span>'}</td>
    <td class="nowrap">
      <button class="btn btn--sm" data-scrape="${s.id}">Actualizar</button>
      <button class="btn btn--sm" data-toggle="${s.id}" data-enable="${s.enabled ? 'false' : 'true'}">
        ${s.enabled ? 'Desactivar' : 'Activar'}</button>
      <button class="btn btn--ghost btn--sm" data-errors="${s.id}" data-name="${esc(s.name)}">Errores</button>
      <button class="btn btn--ghost btn--sm" data-delete="${s.id}" data-name="${esc(s.name)}"
              title="Eliminar la tienda y su historial">🗑</button>
    </td></tr>`;
}

// =====================================================================
// Alertas
// =====================================================================
route('alertas', async (view) => {
  const [alerts, hits] = await Promise.all([api.get('/api/alerts'), api.get('/api/alerts/hits')]);

  view.innerHTML = `
    <div class="page-head"><div><h1>Alertas de precio</h1>
      <p>Se evalúan localmente después de cada actualización.</p></div>
      ${hits.length ? '<button class="btn" id="btn-seen">Marcar avisos como vistos</button>' : ''}</div>

    ${hits.length ? `<div class="section"><div class="card">
      <div class="card__head"><h2>Avisos sin leer (${hits.length})</h2></div>
      <div class="table-wrap"><table class="table"><tbody>
        ${hits.map((h) => `<tr><td><a href="#/producto/${h.product_id}"><strong>${esc(h.display_name)}</strong></a>
          <div class="muted" style="font-size:12px">${esc(h.store_name || '')} · ${ago(h.created_at)}</div></td>
          <td class="num"><span class="price-cell price-cell--best">${money(h.price)}</span></td></tr>`).join('')}
      </tbody></table></div></div></div>` : ''}

    <div class="card table-wrap"><table class="table">
      <thead><tr><th>Producto</th><th class="num">Objetivo</th><th class="num">Precio actual</th>
        <th>Estado</th><th></th></tr></thead>
      <tbody>${alerts.map(alertRow).join('') ||
        '<tr><td colspan="5" class="muted">Sin alertas. Créalas desde la ficha de un producto.</td></tr>'}</tbody>
    </table></div>`;

  const seenBtn = document.getElementById('btn-seen');
  if (seenBtn) seenBtn.onclick = async () => {
    await api.post('/api/alerts/hits/seen', {});
    toast('Avisos marcados como vistos');
    render(); refreshBadges();
  };

  view.querySelectorAll('[data-del-alert]').forEach((btn) => {
    btn.onclick = async () => {
      await api.del(`/api/alerts/${btn.dataset.delAlert}`);
      toast('Alerta eliminada');
      render();
    };
  });
  view.querySelectorAll('[data-toggle-alert]').forEach((btn) => {
    btn.onclick = async () => {
      await api.post(`/api/alerts/${btn.dataset.toggleAlert}/active`, { active: btn.dataset.active === 'true' });
      render();
    };
  });
});

function alertRow(a) {
  const current = a.best_available_price ?? a.best_price;
  const reached = current !== null && current <= a.target_price;
  return `<tr>
    <td><a href="#/producto/${a.product_id}"><strong>${esc(a.display_name)}</strong></a>
      <div class="muted" style="font-size:12px">${esc(a.set_name || '')} · ${esc(a.product_type_name || '')}</div></td>
    <td class="num">${money(a.target_price)}</td>
    <td class="num"><span class="price-cell ${reached ? 'price-cell--best' : ''}">${money(current)}</span></td>
    <td>${!a.active ? '<span class="tag">pausada</span>'
        : reached ? '<span class="tag tag--ok">¡Objetivo alcanzado!</span>' : '<span class="tag">vigilando</span>'}</td>
    <td class="nowrap">
      <button class="btn btn--sm" data-toggle-alert="${a.id}" data-active="${a.active ? 'false' : 'true'}">
        ${a.active ? 'Pausar' : 'Reanudar'}</button>
      <button class="btn btn--ghost btn--sm" data-del-alert="${a.id}">Eliminar</button>
    </td></tr>`;
}

// =====================================================================
// Productos (edición manual de ofertas)
//
// Se editan OFERTAS, no productos maestros: el idioma o el enlace son
// siempre de la ficha de una tienda concreta. Todo lo que se toca aquí se
// guarda contra la clave estable de la oferta, así que sobrevive a los
// siguientes scrapings aunque los atributos se recalculen del nombre.
// =====================================================================
const filtrosOfertas = { q: '', store_id: '', language: '', edited_only: false, page: 1 };

const vistaProductos = async (view) => {
  const [langs, facets] = await Promise.all([
    state.languages ? Promise.resolve(state.languages) : api.get('/api/languages'),
    state.facets ? Promise.resolve(state.facets) : api.get('/api/facets'),
  ]);
  state.languages = langs;
  state.facets = facets;

  view.innerHTML = `
    <div class="page-head">
      <div><h1>Productos</h1>
        <p>Corrige a mano lo que el sistema no acertó: el idioma, el enlace o
           la agrupación. Los cambios se recuerdan y sobreviven a las
           actualizaciones.</p></div>
    </div>

    <div class="card" style="margin-bottom:18px"><div class="card__body">
      <div class="offer-filters">
        <div class="filter-group" style="flex:2 1 260px">
          <label>Buscar por nombre, enlace o SKU</label>
          <input type="search" id="o-q" placeholder="«mini tin», «shrouded», «PER-ORD04»…"
                 value="${esc(filtrosOfertas.q)}"></div>
        <div class="filter-group" style="flex:1 1 180px"><label>Tienda</label>
          <select id="o-store"><option value="">Todas</option>
            ${facets.stores.map((s) => `<option value="${s.id}" ${String(s.id) === String(filtrosOfertas.store_id) ? 'selected' : ''}>${esc(s.name)}</option>`).join('')}
          </select></div>
        <div class="filter-group" style="flex:1 1 150px"><label>Idioma</label>
          <select id="o-lang"><option value="">Todos</option>
            <option value="unknown" ${filtrosOfertas.language === 'unknown' ? 'selected' : ''}>Sin idioma</option>
            ${langs.map((l) => `<option value="${l.code}" ${l.code === filtrosOfertas.language ? 'selected' : ''}>${esc(l.name)}</option>`).join('')}
          </select></div>
        <div class="filter-group" style="flex:0 0 auto">
          <label class="check check--box ${filtrosOfertas.edited_only ? 'is-on' : ''}">
            <input type="checkbox" id="o-edited" ${filtrosOfertas.edited_only ? 'checked' : ''}>
            Solo los ya corregidos</label></div>
      </div>
    </div></div>

    <div id="offer-list"><div class="loading"><span class="spinner"></span> Buscando…</div></div>`;

  const recargar = () => {
    filtrosOfertas.q = document.getElementById('o-q').value.trim();
    filtrosOfertas.store_id = document.getElementById('o-store').value;
    filtrosOfertas.language = document.getElementById('o-lang').value;
    filtrosOfertas.edited_only = document.getElementById('o-edited').checked;
    filtrosOfertas.page = 1;
    cargarOfertas();
  };

  let temporizador;
  document.getElementById('o-q').addEventListener('input', () => {
    clearTimeout(temporizador);
    temporizador = setTimeout(recargar, 350);
  });
  ['o-store', 'o-lang'].forEach((id) =>
    document.getElementById(id).addEventListener('change', recargar));
  const casilla = document.getElementById('o-edited');
  casilla.addEventListener('change', () => {
    casilla.closest('.check--box').classList.toggle('is-on', casilla.checked);
    recargar();
  });

  await cargarOfertas();
};

async function cargarOfertas() {
  const caja = document.getElementById('offer-list');
  if (!caja) return;
  caja.classList.add('is-loading');
  const data = await api.get('/api/offers', { ...filtrosOfertas, page_size: 25 });
  caja.classList.remove('is-loading');

  caja.innerHTML = `
    <div class="toolbar"><strong>${num(data.total)}</strong>
      <span class="muted">ofertas</span></div>
    <div class="results-body">
      ${data.items.length ? data.items.map(offerEditor).join('')
        : '<div class="empty"><h3>Sin resultados</h3><p>Prueba con otro texto o quita algún filtro.</p></div>'}
      ${data.pages > 1 ? `<div class="pagination">
        <button class="btn btn--sm" data-opage="${data.page - 1}" ${data.page <= 1 ? 'disabled' : ''}>‹ Anterior</button>
        <span>Página ${data.page} de ${data.pages}</span>
        <button class="btn btn--sm" data-opage="${data.page + 1}" ${data.page >= data.pages ? 'disabled' : ''}>Siguiente ›</button>
      </div>` : ''}
    </div>`;
  aparecer(caja.querySelector('.results-body'));
  bindOfferEditors(caja);
}

function offerEditor(o) {
  const editado = Object.keys(o.manual || {}).length > 0;
  const idiomas = (state.languages || []).map(
    (l) => `<option value="${l.code}" ${l.code === o.language ? 'selected' : ''}>${esc(l.name)}</option>`
  ).join('');
  return `<div class="offer-card${editado ? ' is-edited' : ''}" data-offer="${o.id}">
    <div class="offer-card__head">
      <div>
        <div class="offer-card__name">${esc(o.name)}</div>
        <div class="offer-card__meta">
          <strong>${esc(o.store)}</strong> · ${money(o.price)} · ${esc(o.stock_label)}
          ${o.product_type_name ? ` · ${esc(o.product_type_name)}` : ''}
          ${o.sku ? ` · <span class="mono">${esc(o.sku)}</span>` : ''}
        </div>
        <div class="offer-card__meta">
          ${o.product_id
            ? `Agrupado en <a href="#/producto/${o.product_id}">${esc(o.product_name || 'producto')}</a>
               ${o.stores_count > 1 ? `<span class="muted">(${o.stores_count} tiendas)</span>` : '<span class="muted">(solo esta tienda)</span>'}`
            : '<span class="muted">Sin agrupar</span>'}
        </div>
      </div>
      ${editado ? '<span class="tag tag--warn">Corregido a mano</span>' : ''}
    </div>

    <div class="offer-card__grid">
      <div class="filter-group">
        <label>Idioma${o.manual && o.manual.language ? ' <span class="muted">(fijado)</span>' : ''}</label>
        <select data-attr="language"><option value="">Automático</option>${idiomas}</select>
      </div>
      <div class="filter-group" style="flex:1 1 100%">
        <label>Enlace a la tienda${o.manual && o.manual.url ? ' <span class="muted">(corregido)</span>' : ''}</label>
        <div class="offer-card__link">
          <input type="url" data-attr="url" value="${esc(o.url)}" spellcheck="false">
          <button class="btn btn--sm" data-save-url>Guardar</button>
          ${o.manual && o.manual.url
            ? '<button class="btn btn--ghost btn--sm" data-reset-url>Restaurar</button>' : ''}
          <a class="btn btn--ghost btn--sm" href="${esc(o.url)}" target="_blank" rel="noopener">Abrir ↗</a>
        </div>
        ${o.manual && o.manual.url
          ? `<div class="muted" style="font-size:11px;margin-top:4px">Original: ${esc(o.original_url)}</div>` : ''}
      </div>
    </div>

    <div class="offer-card__actions">
      ${o.product_id && o.stores_count > 1
        ? '<button class="btn btn--sm" data-split>✂ Separar de este grupo</button>' : ''}
      <button class="btn btn--ghost btn--sm" data-rejoin>Olvidar separaciones</button>
      <span class="offer-card__msg"></span>
    </div>
  </div>`;
}

function bindOfferEditors(caja) {
  caja.querySelectorAll('[data-opage]').forEach((btn) => {
    btn.onclick = () => { filtrosOfertas.page = Number(btn.dataset.opage); cargarOfertas(); };
  });

  caja.querySelectorAll('.offer-card').forEach((tarjeta) => {
    const id = tarjeta.dataset.offer;
    const msg = tarjeta.querySelector('.offer-card__msg');
    const avisar = (texto, error) => {
      msg.textContent = texto;
      msg.className = `offer-card__msg${error ? ' is-error' : ''}`;
    };

    const guardar = async (attribute, value) => {
      try {
        await api.post(`/api/offers/${id}/attribute`, { attribute, value });
        avisar('Guardado');
        tarjeta.classList.add('is-edited');
      } catch (err) {
        avisar(err.message, true);
      }
    };

    tarjeta.querySelector('[data-attr="language"]').onchange = (e) =>
      guardar('language', e.target.value);

    tarjeta.querySelector('[data-save-url]').onclick = () =>
      guardar('url', tarjeta.querySelector('[data-attr="url"]').value.trim());

    const restaurar = tarjeta.querySelector('[data-reset-url]');
    if (restaurar) restaurar.onclick = async () => { await guardar('url', ''); cargarOfertas(); };

    const separar = tarjeta.querySelector('[data-split]');
    if (separar) separar.onclick = async () => {
      separar.disabled = true;
      try {
        const r = await api.post(`/api/offers/${id}/split`);
        avisar(`Separado de ${num(r.separated_from)} oferta(s)`);
        toast('Oferta separada de su grupo');
        cargarOfertas();
      } catch (err) { avisar(err.message, true); separar.disabled = false; }
    };

    tarjeta.querySelector('[data-rejoin]').onclick = async () => {
      try {
        const r = await api.post(`/api/offers/${id}/rejoin`);
        avisar(r.forgotten ? `${num(r.forgotten)} separación(es) olvidadas` : 'No había separaciones');
        if (r.forgotten) { toast('Se olvidaron las separaciones de esta oferta'); cargarOfertas(); }
      } catch (err) { avisar(err.message, true); }
    };
  });
}

// =====================================================================
// Configuración
// =====================================================================
const vistaConfiguracion = async (view) => {
  const config = await api.get('/api/config');
  const s = config.settings;
  const m = s.matching || {};
  const w = m.weights || {};
  const p = m.penalties || {};

  view.innerHTML = `
    <div class="page-head"><div><h1>Configuración</h1>
      <p>Los cambios se guardan en la base de datos local y tienen prioridad sobre
         <span class="mono">config/settings.yaml</span>.</p></div></div>

    <div class="section"><div class="card">
      <div class="card__head"><h2>Actualización automática</h2></div>
      <div class="card__body">
        <div class="form-row">
          <div><label>Intervalo</label>
            <select id="c-interval">${[1, 3, 6, 12, 24].map((h) =>
              `<option value="${h}" ${s.scheduler.interval_hours === h ? 'selected' : ''}>
                 cada ${h} ${h === 1 ? 'hora' : 'horas'}${h === 24 ? ' (diariamente)' : ''}</option>`).join('')}
            </select></div>
          <div><label class="check"><input type="checkbox" id="c-sched-on" ${s.scheduler.enabled ? 'checked' : ''}>
            Actualización automática activada</label></div>
          <div><label class="check"><input type="checkbox" id="c-startup" ${s.scheduler.run_on_startup ? 'checked' : ''}>
            Actualizar al iniciar la aplicación</label></div>
        </div>
        <div class="form-row">
          <div><label>Espera mínima entre peticiones (s)</label>
            <input type="number" step="0.1" id="c-delay" value="${s.scraping.min_delay_seconds}"></div>
          <div><label>Peticiones simultáneas por tienda</label>
            <input type="number" id="c-conc" value="${s.scraping.concurrency_per_store}"></div>
          <div><label>Tiendas en paralelo</label>
            <input type="number" id="c-gconc" value="${s.scraping.global_concurrency}"></div>
        </div>
        <p class="hint">Subir la concurrencia o bajar la espera acelera el scraping pero
           carga más los servidores de las tiendas.</p>
      </div></div></div>

    <div class="section"><div class="card">
      <div class="card__head"><h2>Umbrales de agrupación</h2></div>
      <div class="card__body">
        <div class="form-row">
          <div><label>Match automático (≥)</label>
            <input type="number" id="c-auto" value="${m.auto_threshold}"></div>
          <div><label>Revisión manual (≥)</label>
            <input type="number" id="c-review" value="${m.review_threshold}"></div>
          <div><label>Confianza mínima de cantidad</label>
            <input type="number" step="0.05" id="c-qconf" value="${s.unit_price.min_confidence}"></div>
        </div>
        <h3 style="font-size:14px;margin:16px 0 8px">Pesos del puntaje</h3>
        <div class="form-row">
          <div><label>Identificador EAN/UPC</label><input type="number" id="w-ident" value="${w.identifier}"></div>
          <div><label>Misma expansión</label><input type="number" id="w-set" value="${w.same_set}"></div>
          <div><label>Mismo tipo</label><input type="number" id="w-type" value="${w.same_product_type}"></div>
          <div><label>Misma cantidad</label><input type="number" id="w-qty" value="${w.same_quantity}"></div>
          <div><label>Tokens coincidentes</label><input type="number" id="w-tok" value="${w.token_overlap}"></div>
          <div><label>Similitud de nombre</label><input type="number" id="w-name" value="${w.name_similarity}"></div>
        </div>
        <div class="form-row">
          <div><label>Penalización: distinta expansión</label><input type="number" id="p-set" value="${p.different_set}"></div>
          <div><label>Penalización: distinto tipo</label><input type="number" id="p-type" value="${p.different_product_type}"></div>
          <div><label>Penalización: distinta cantidad</label><input type="number" id="p-qty" value="${p.different_quantity}"></div>
        </div>
        <p class="hint">Tras cambiar los umbrales conviene reagrupar sin volver a descargar.</p>
      </div></div></div>

    <div class="section"><div class="card">
      <div class="card__head"><h2>Probar la normalización</h2></div>
      <div class="card__body">
        <div class="form-row">
          <div style="flex:3"><label>Nombre de producto</label>
            <input type="text" id="norm-input" value="Pokémon TCG: Scarlet &amp; Violet—151 Booster Bundle"></div>
          <button class="btn" id="btn-norm">Analizar</button>
        </div>
        <div id="norm-out"></div>
      </div></div></div>

    <div class="toolbar">
      <button class="btn btn--primary" id="btn-save">Guardar configuración</button>
      <button class="btn" id="btn-rematch">Reagrupar ahora (sin scrapear)</button>
      <div class="toolbar__spacer"></div>
      <span class="muted">Moneda: ${esc(s.app.currency)} · Zona horaria: ${esc(s.app.timezone)}</span>
    </div>`;

  document.getElementById('btn-norm').onclick = async () => {
    const name = document.getElementById('norm-input').value;
    const r = await api.post('/api/normalization/preview', { name });
    document.getElementById('norm-out').innerHTML = `
      <dl class="kv" style="margin-top:12px">
        <dt>Texto básico</dt><dd class="mono">${esc(r.basic)}</dd>
        <dt>Canónico</dt><dd class="mono">${esc(r.canonical)}</dd>
        <dt>Tokens relevantes</dt><dd>${r.core_tokens.map((t) => `<span class="tag">${esc(t)}</span>`).join(' ')}</dd>
        <dt>Clave de nombre</dt><dd class="mono">${esc(r.name_key)}</dd>
        <dt>Juego</dt><dd>${esc(r.attributes.game_name || '—')}</dd>
        <dt>Expansión</dt><dd>${esc(r.attributes.set_name || '—')} <span class="muted">(${esc(r.attributes.set_code || 'sin detectar')})</span></dd>
        <dt>Tipo</dt><dd>${esc(r.attributes.product_type_name || '—')}</dd>
        <dt>Unidades</dt><dd>${r.attributes.units_total ?? '—'} ${esc(r.attributes.unit_name || '')}
          <span class="muted">(confianza ${(r.attributes.quantity_confidence * 100).toFixed(0)} %)</span></dd>
      </dl>`;
  };

  document.getElementById('btn-save').onclick = async () => {
    const val = (id) => Number(document.getElementById(id).value);
    await api.put('/api/config', {
      'scheduler.interval_hours': val('c-interval'),
      'scheduler.enabled': document.getElementById('c-sched-on').checked,
      'scheduler.run_on_startup': document.getElementById('c-startup').checked,
      'scraping.min_delay_seconds': val('c-delay'),
      'scraping.concurrency_per_store': val('c-conc'),
      'scraping.global_concurrency': val('c-gconc'),
      'matching.auto_threshold': val('c-auto'),
      'matching.review_threshold': val('c-review'),
      'unit_price.min_confidence': val('c-qconf'),
      'matching.weights.identifier': val('w-ident'),
      'matching.weights.same_set': val('w-set'),
      'matching.weights.same_product_type': val('w-type'),
      'matching.weights.same_quantity': val('w-qty'),
      'matching.weights.token_overlap': val('w-tok'),
      'matching.weights.name_similarity': val('w-name'),
      'matching.penalties.different_set': val('p-set'),
      'matching.penalties.different_product_type': val('p-type'),
      'matching.penalties.different_quantity': val('p-qty'),
    });
    toast('Configuración guardada');
  };

  document.getElementById('btn-rematch').onclick = async (e) => {
    e.target.disabled = true;
    e.target.textContent = 'Reagrupando…';
    const r = await api.post('/api/rematch');
    toast(`Listo: ${r.products} productos maestros, ${r.reviews} pendientes de revisión`);
    render();
  };
};

// =====================================================================
// Barra superior: búsqueda global, actualización y estado
// =====================================================================
function initTopbar() {
  const input = document.getElementById('global-search');
  const box = document.getElementById('suggestions');
  let timer;

  input.addEventListener('input', () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { box.hidden = true; return; }
    timer = setTimeout(async () => {
      const items = await api.get('/api/suggest', { q, limit: 8 });
      if (!items.length) { box.hidden = true; return; }
      box.innerHTML = items.map((i) => `<div class="suggestion" data-id="${i.id}">
          <div><div>${esc(i.name)}</div>
            <div class="suggestion__meta">${esc(i.set_name || '')} · ${esc(i.product_type_name || '')}</div></div>
          <div class="right"><strong>${money(i.best_price)}</strong>
            <div class="suggestion__meta">${num(i.stores_count)} tiendas</div></div>
        </div>`).join('');
      box.hidden = false;
      box.querySelectorAll('.suggestion').forEach((el) => {
        el.onclick = () => { box.hidden = true; input.value = ''; location.hash = `#/producto/${el.dataset.id}`; };
      });
    }, 220);
  });

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      box.hidden = true;
      location.hash = `#/buscar?q=${encodeURIComponent(input.value.trim())}`;
    }
    if (e.key === 'Escape') box.hidden = true;
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('.search')) box.hidden = true;
  });

  document.getElementById('btn-top').onclick = () =>
    window.scrollTo({ top: 0, behavior: 'smooth' });

  // El tema ya lo aplicó el script en línea de index.html, antes de pintar.
  // Aquí solo se atiende el interruptor y se mantiene su estado accesible.
  const interruptor = document.getElementById('btn-theme');
  const sincronizar = () => {
    const oscuro = document.documentElement.dataset.theme !== 'light';
    interruptor.setAttribute('aria-checked', String(oscuro));
    interruptor.setAttribute('aria-label', oscuro ? 'Tema oscuro' : 'Tema claro');
  };
  sincronizar();
  interruptor.onclick = () => {
    const siguiente = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = siguiente;
    localStorage.setItem('theme', siguiente);
    sincronizar();
  };
}

let statusTimer = null;
async function pollStatus() {
  clearTimeout(statusTimer);
  const info = await api.get('/api/status').catch(() => null);
  if (!info) return;

  // Estos elementos solo existen en la página de Tiendas: en el resto se
  // sigue consultando el estado (para el aviso al terminar) sin pintar nada.
  const el = document.getElementById('update-status');
  const btn = document.getElementById('btn-update');
  const running = info.pipeline.running;

  if (btn) {
    btn.disabled = running;
    btn.textContent = running ? 'Actualizando…' : 'Actualizar todas';
  }
  if (el) {
    el.innerHTML = running
      ? `<div><span class="spinner"></span> ${esc(info.pipeline.current_store || 'Procesando')}…</div>`
      : `<div${info.scheduler.overdue ? ' class="is-overdue" title="Ha pasado más tiempo del intervalo configurado"' : ''}>Última: <strong>${ago(info.pipeline.finished_at || info.last_update)}</strong></div>
         <div>Próxima: <strong>${info.scheduler.enabled ? countdown(info.scheduler.seconds_until_next_run) : 'desactivada'}</strong></div>`;
  }

  if (running) {
    statusTimer = setTimeout(pollStatus, 2000);
  } else {
    if (pollStatus._wasRunning) { toast('Actualización completada'); render(); refreshBadges(); }
    statusTimer = setTimeout(pollStatus, 30000);
  }
  pollStatus._wasRunning = running;
}

async function refreshBadges() {
  const data = await api.get('/api/dashboard').catch(() => null);
  if (!data) return;
  const t = data.totals || {};
  state.totals = t;             // las subpestañas de administración lo usan
  const reviews = document.getElementById('badge-reviews');
  const alerts = document.getElementById('badge-alerts');

  reviews.hidden = !t.pending_reviews;
  reviews.textContent = t.pending_reviews;
  alerts.hidden = !t.alert_hits;
  alerts.textContent = t.alert_hits;

  document.getElementById('footer-stats').innerHTML = `
    <div><strong>${num(t.products)}</strong> productos</div>
    <div><strong>${num(t.offers)}</strong> ofertas</div>
    <div><strong>${num(t.stores_enabled)}</strong> tiendas activas</div>
    <div><strong>${num(t.compared)}</strong> comparables</div>`;
}

// ----------------------------------------------------------------- arranque
(async function boot() {
  try {
    const config = await api.get('/api/config');
    const app = config.settings.app || {};
    state.currency = {
      symbol: app.currency_symbol ?? '$',
      thousands: app.currency_thousands_sep ?? '.',
      decimal: app.currency_decimal_sep ?? ',',
      decimals: app.currency_decimals ?? 0,
    };
  } catch (err) {
    console.warn('No se pudo leer la configuración', err);
  }
  // Las facetas se cargan antes de pintar: la barra de juegos las necesita.
  try {
    state.facets = await api.get('/api/facets');
    state.game = juegoActual();
    renderGamesNav();
  } catch (err) {
    console.warn('No se pudieron cargar los juegos', err);
  }

  initTopbar();
  await render();
  pollStatus();
  refreshBadges();
})();
