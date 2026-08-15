# TCG Chile

Comparador de precios de producto sellado de juegos de cartas coleccionables
en tiendas chilenas.

Recorre las tiendas cada pocas horas, agrupa el mismo producto entre distintas
tiendas y muestra dónde está más barato, con su historial de precios.

## Cómo funciona

La agrupación es la parte difícil: cada tienda escribe el nombre a su manera.
Se resuelve con normalización de texto, identificadores del producto cuando la
tienda los publica (UPC/EAN/SKU) y medidas de similitud entre nombres. Todo con
código y reglas locales: **no interviene ningún servicio de inteligencia
artificial** en ninguna parte del sistema.

Los casos dudosos no se adivinan: quedan en una cola de revisión manual, y la
decisión que se toma ahí se recuerda para siempre.

## Hecho con

Python · FastAPI · PostgreSQL · JavaScript sin frameworks

---

Proyecto personal. El código está publicado, pero no es un producto instalable
ni acepta contribuciones por ahora.
