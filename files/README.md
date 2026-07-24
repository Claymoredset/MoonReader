# MoonReader — Prototipo (v8)

Gestor de PDF con recortes anotables (split view PDF + hoja de anotaciones infinita).

## Instalar y correr

```bash
pip install PySide6 PyMuPDF
python3 main.py
```

## Novedades de esta versión

**1. Estilo visual.** Toda la app tiene ahora una hoja de estilos consistente:
botones redondeados con estados de hover/presionado, color de acento en las
herramientas activas, listas y menús con mejor contraste, scrollbars más
finas. Nada funcional cambió, solo se ve más prolijo.

**2. Antialiasing reforzado.** Tanto el visor de PDF como el lienzo de
dibujo ahora fuerzan `Antialiasing` + `SmoothPixmapTransform` — los bordes
de los recortes, las manijas de redimensión, y las líneas dibujadas (sobre
todo las curvas y diagonales del estilo boceto) se ven más suaves.

**3. El fondo del lienzo ahora es POR RECORTE, no global.** Este era un bug
de diseño: antes, cambiar el fondo se aplicaba a TODOS los recortes
(incluidos los viejos que ya eran blancos). Ahora cada recorte guarda su
propio color/estilo de fondo:
- Los recortes que ya tenías (blancos) **se quedan blancos** aunque cambies
  la configuración después.
- Cuando cambiás el fondo mientras tenés un recorte abierto, se aplica a
  ESE recorte, y además queda guardado como el fondo con el que van a
  nacer los recortes **nuevos** de ahí en adelante.
- Ejemplo: abrís un recorte, le ponés fondo negro cuadriculado → ese
  recorte queda negro cuadriculado para siempre, y el próximo recorte que
  crees también arranca así (hasta que lo cambies de nuevo).

## Cómo probarlo

1. Abrí un recorte existente (blanco) — sigue blanco.
2. Cambiale el fondo a negro + cuadriculado.
3. Volvé a un recorte viejo distinto: sigue blanco, no se contagió.
4. Creá un recorte nuevo: nace directamente en negro cuadriculado.

## Archivos

- `db.py` — SQLite: libros, recortes (ahora con fondo propio por recorte),
  elementos de dibujo, config (default de fondo para recortes nuevos).
- `pdf_view.py` — Visor de PDF con scroll continuo, carga perezosa,
  recortes editables/eliminables, antialiasing.
- `canvas_a4.py` — Lienzo infinito con herramientas tipo Excalidraw,
  fondo por recorte, antialiasing reforzado.
- `main.py` — Ventana principal + hoja de estilos (QSS) de toda la app.

## Pendiente para siguiente iteración

- Vista de biblioteca en grid con miniaturas de portada.
- Exportar recorte + anotaciones a imagen o PDF.
- Renombrar recortes.
- Zoom in/out del propio visor de PDF.
- Traer al frente / enviar atrás entre elementos superpuestos del lienzo.

## v9 — Por qué se veía "temblorosa" y cómo se arregló

El problema no era el antialiasing (eso ya estaba bien) sino la técnica del
efecto boceto en sí:

- **Antes**: cada esquina/punto se hacía temblar por separado y se unían con
  líneas rectas → daba un zigzag nervioso.
- **Ahora**: cada arista se dibuja como UNA sola curva continua con una
  panza suave en el medio, y los extremos se pasan un poquito de largo (como
  una mano real que no levanta el lápiz justo en la esquina). Es la misma
  técnica base de Excalidraw/rough.js.
- El **lápiz libre** ahora suaviza el trazo con curvas en vez de conectar
  cada punto del mouse con una línea recta — se nota sobre todo en curvas
  y diagonales.
- El **texto** ya no usa Comic Sans fijo (que se ve genérico): busca en tu
  sistema fuentes más prolijas tipo boceto (Comic Neue, Segoe Print, Bradley
  Hand, Patrick Hand...) y usa la primera que encuentre; si no hay ninguna,
  cae a una sans-serif normal sin romper nada.
  - Para el mejor resultado en Ubuntu: `sudo apt install fonts-comic-neue`
    (es una alternativa a Comic Sans hecha específicamente para verse más
    limpia).

## v10 — Lápiz con grosor variable (estilo GoodNotes)

El trazo del lápiz libre ahora se dibuja como una **cinta rellena de ancho
variable**, no una línea de grosor fijo:

- **Más lento el movimiento del mouse → más grueso** (como si apretaras más
  fuerte con la lapicera).
- **Más rápido → más fino.**
- La curva se suaviza con **Catmull-Rom** (una interpolación más prolija
  que las curvas cuadráticas simples de antes), notable sobre todo en
  trazos rápidos con pocos puntos capturados.
- Puntas redondeadas en el inicio y el final del trazo, como remate de
  pincel.

Esto es aproximado (no hay un lápiz con sensor de presión real, se estima
a partir de qué tan separados quedaron los puntos capturados del mouse
entre un evento y el siguiente), pero da un resultado bastante cercano al
de GoodNotes/Notability. Las formas geométricas (rectángulo, elipse,
flecha) siguen con el estilo boceto de Excalidraw de la versión anterior,
que es un efecto distinto y no necesita grosor variable.

## v11 — Arreglo: el trazo se veía negro/vacío al cruzarse consigo mismo

Bug de la cinta de ancho variable (v10): cuando un trazo se cruzaba a sí
mismo (garabatos, curvas cerradas, firmas, letras cursivas), la regla de
relleno por defecto de Qt ("par-impar") dejaba huecos vacíos donde el
trazo se solapaba consigo mismo, y en otras zonas se acumulaban bordes
antialiaseados dando un efecto sucio/oscuro. Cambiado a regla de relleno
"winding" (`QPainterPath.setFillRule(Qt.WindingFill)`), que rellena sólido
sin huecos sin importar cuántas veces se cruce el trazo.

## v12 — Revertido: lápiz de ancho variable

Por pedido, se revirtió el experimento de la "cinta de ancho variable"
(v10/v11 — grosor según velocidad + Catmull-Rom), que terminó dando
problemas al cruzar el trazo consigo mismo. El lápiz volvió a la versión
estable anterior: grosor fijo, curva suavizada con la técnica simple de
punto medio (sin el efecto de presión). Las formas geométricas
(rectángulo, elipse, flecha) con estilo boceto Excalidraw no se tocaron,
siguen igual.

## v13 — Miniaturas de página y marcadores con "vistazo"

**Panel de miniaturas** (angosto, al costado, no ocupa toda la app):
- Aparece a la izquierda del PDF, con una miniatura por página.
- Click en una miniatura salta directo a esa página.
- Botón **🖼** en la barra para mostrar/ocultar el panel.
- Carga perezosa: solo se renderizan las miniaturas cerca de lo que estás
  viendo en ese panel (igual que las páginas del visor principal), así que
  no importa si el libro tiene cientos de páginas.

**Marcadores con "vistazo"** — pensado justo para el caso que describiste
(la hoja de respuestas lejos de donde estás trabajando):
- **📌 Marcar esta página**: crea un marcador con nombre en la página actual.
- Aparece como una píldora en la barra. Tocala: te lleva a esa página,
  recordando de dónde veniás.
- Tocala **de nuevo**: te devuelve exactamente a donde estabas.
- Los marcadores quedan guardados por libro (persisten al cerrar la app).
- Click derecho sobre un marcador: eliminarlo.

## Archivos

- `db.py` — SQLite: libros, recortes, elementos de dibujo, config, y ahora
  **marcadores** (tabla nueva).
- `pdf_view.py` — Visor de PDF con scroll continuo, carga perezosa, recortes
  editables/eliminables, y ahora **panel de miniaturas** + **marcadores**.
- `canvas_a4.py` — Lienzo infinito con herramientas tipo Excalidraw.
- `main.py` — Ventana principal + hoja de estilos.

## Pendiente para siguiente iteración

- Vista de biblioteca en grid con miniaturas de portada (la de libros, no
  la de páginas — esa ya está).
- Exportar recorte + anotaciones a imagen o PDF.
- Renombrar recortes.
- Zoom in/out del propio visor de PDF.
- Traer al frente / enviar atrás entre elementos superpuestos del lienzo.

## v14 — Biblioteca en grid con portadas

La biblioteca ya no es una lista de texto: ahora es un grid con la portada
de cada libro (miniatura de su primera página), estilo cualquier gestor
de PDFs/ebooks.

- Doble click en una portada para abrir el libro (igual que antes).
- Carga perezosa: si tenés muchos libros importados, solo se renderizan
  las portadas de los que se ven en pantalla (probado con 80 libros en
  una ventana chica: cargó ~28 al inicio, no las 80 de una).
- Si un PDF está roto, movido o no se puede abrir, muestra una portada
  gris rosada en vez de romper toda la biblioteca.

## v15 — PDF zoom + English UI

**PDF viewer zoom**: Ctrl + mouse wheel to zoom in/out (centered on the
cursor), a "🔍 100%" button + live percentage label in the toolbar to reset.
Page navigation (jump to page, bookmarks) now works correctly at any zoom
level — this needed a fix since it originally assumed zoom was always 1:1.

**UI text switched to English**: all visible buttons, labels, tooltips,
and dialogs across the app are now in English (window title, library
screen, PDF toolbar, drawing tools, background controls, bookmarks, crop
dialogs). Internal code — variable names, comments, function names, and
the data stored in the database — stays in Spanish, since that's just for
the code/data layer and doesn't affect what you see in the app.

## v16 — Dark mode

New **🌙 Dark mode** button, available both in the library screen and in
the reader's top bar — toggling either one keeps both in sync. The
preference is saved and restored automatically the next time you open
the app.

Covers the whole UI: buttons, lists, menus, scrollbars, sliders, dialogs.
The PDF page content and your drawings keep their own colors either way
(a page is still white unless you set a custom canvas background, same as
before) — dark mode only re-themes the app's chrome, not your content.

## v17 — Professional polish

- **App icon**: generated in code (no external file needed), shows in the
  window/taskbar.
- **Menu bar**: File (Import PDF — Ctrl+O, Exit — Ctrl+Q), View (Dark mode,
  synced with the existing buttons), Help (About MoonReader with version info).
- **Status bar**: brief feedback after actions — opening a book, importing,
  creating/deleting a crop, removing a book.
- **Remove a book from the library**: right-click a cover → confirm →
  deletes the book plus its crops/bookmarks (cascade). The original PDF
  file on disk is never touched.
- **Empty state**: a friendly message instead of a blank grid when you
  haven't imported anything yet.
- **Broken PDF handling**: if a book's file was moved, renamed, or deleted,
  opening it now shows a clear error and returns you to the library
  instead of leaving the app in a broken state.
- **requirements.txt** added for a one-line install (`pip install -r requirements.txt`).

## Install

```bash
pip install -r requirements.txt
python3 main.py
```
