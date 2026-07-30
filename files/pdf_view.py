"""
Visor de PDF basado en QGraphicsView + PyMuPDF.
Scroll continuo (todas las páginas apiladas verticalmente en una sola escena),
pero con CARGA PEREZOSA: al abrir el documento, solo se calculan los tamaños
de página (sin renderizar) y se colocan placeholders livianos. Solo se
renderizan de verdad las páginas cercanas a lo que estás viendo; las que
quedan lejos del scroll se liberan de memoria y vuelven a ser placeholder.

Esto es importante para libros largos (cientos de páginas): renderizar TODO
el documento de una vez puede consumir varios GB de RAM y hacer que el
sistema operativo empiece a intercambiar memoria (swap) hasta trabarse.

Superpone rectángulos de recorte (clickeables, cargados desde la DB) y
permite dibujar un rectángulo nuevo con el mouse para crear un recorte.

Las coordenadas de los recortes se guardan en espacio "de página" en
puntos PDF (72 dpi), relativas a cada página individual, para que
sobrevivan a cambios de zoom.
"""
from PySide6.QtWidgets import (
    QGraphicsView, QGraphicsScene, QGraphicsRectItem, QGraphicsPixmapItem,
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QSpinBox,
    QMenu, QMessageBox, QListWidget, QListWidgetItem, QSplitter, QInputDialog
)
from PySide6.QtGui import QPixmap, QImage, QPen, QBrush, QColor, QCursor, QPainter, QIcon
from PySide6.QtCore import Qt, QRectF, Signal, QPointF, QSize, QPoint, QTimer

import fitz  # PyMuPDF
import db

RENDER_ZOOM = 2.0  # factor de render interno para nitidez (2x = ~144dpi)
GAP_ENTRE_PAGINAS = 16  # separación visual entre páginas, en px de escena

ZOOM_MIN = 0.3
ZOOM_MAX = 4.0
ZOOM_PASO = 1.15

# Ventana de carga perezosa: cuántas páginas de margen se renderizan de más
# alrededor de lo visible, y cuántas de más se toleran antes de liberarlas.
# El segundo número es más grande que el primero a propósito, para evitar
# que una página se cargue y descargue todo el tiempo al scrollear justo
# en el borde (efecto "parpadeo").
BUFFER_CARGA = 2
BUFFER_DESCARGA = 5


TAMANO_MANIJA = 10  # px de escena (la vista de PDF no tiene zoom, así que 1:1 con pantalla)
COLOR_MANIJA = QColor(0, 120, 255)
ANCHO_MINIMO_RECORTE = 15
ALTO_MINIMO_RECORTE = 15


class ManijaRedimension(QGraphicsRectItem):
    """Cuadradito arrastrable en una esquina de un recorte seleccionado, para cambiar su tamaño."""

    CURSORES = {
        "nw": Qt.SizeFDiagCursor, "se": Qt.SizeFDiagCursor,
        "ne": Qt.SizeBDiagCursor, "sw": Qt.SizeBDiagCursor,
    }

    def __init__(self, overlay, esquina):
        m = TAMANO_MANIJA
        super().__init__(-m / 2, -m / 2, m, m)
        self.overlay = overlay
        self.esquina = esquina
        self.setBrush(QBrush(COLOR_MANIJA))
        self.setPen(QPen(QColor(255, 255, 255), 1))
        self.setZValue(30)
        self.setCursor(QCursor(self.CURSORES[esquina]))
        self._arrastrando = False

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._arrastrando = True
            event.accept()
        else:
            event.ignore()

    def mouseMoveEvent(self, event):
        if self._arrastrando:
            self.overlay.redimensionar_desde_esquina(self.esquina, event.scenePos())
            event.accept()
        else:
            event.ignore()

    def mouseReleaseEvent(self, event):
        if self._arrastrando:
            self._arrastrando = False
            self.overlay.confirmar_redimension()
            event.accept()
        else:
            event.ignore()


class RecorteOverlayItem(QGraphicsRectItem):
    """Rectángulo semi-transparente que representa un recorte guardado.

    - Click izquierdo: abre el recorte (modo normal) o lo selecciona para
      editar tamaño/posición (modo edición de recortes).
    - Click derecho: siempre disponible, menú contextual para eliminarlo.
    - Cuando está seleccionado en modo edición, muestra 4 manijas en las
      esquinas para redimensionar arrastrando.
    """

    def __init__(self, recorte_id, pagina, rect, parent_view):
        super().__init__(rect)
        self.recorte_id = recorte_id
        self.pagina = pagina
        self.parent_view = parent_view
        self.seleccionado = False
        self.manijas = []
        self.setBrush(QBrush(QColor(255, 210, 0, 60)))
        self.setPen(QPen(QColor(255, 170, 0, 220), 2))
        self.setCursor(QCursor(Qt.PointingHandCursor))
        self.setAcceptHoverEvents(True)
        self.setZValue(10)

    def hoverEnterEvent(self, event):
        if not self.seleccionado:
            self.setBrush(QBrush(QColor(255, 210, 0, 110)))
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        if not self.seleccionado:
            self.setBrush(QBrush(QColor(255, 210, 0, 60)))
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self._mostrar_menu_contextual(event)
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            if self.parent_view.modo_edicion_recortes:
                self.parent_view.seleccionar_overlay(self)
            else:
                self.parent_view.recorte_clickeado.emit(self.recorte_id)
            event.accept()
            return
        event.ignore()

    def _mostrar_menu_contextual(self, event):
        menu = QMenu()
        accion_eliminar = menu.addAction("🗑 Delete crop")
        elegido = menu.exec(event.screenPos())
        if elegido == accion_eliminar:
            respuesta = QMessageBox.question(
                self.parent_view, "Delete crop",
                "Delete this crop and all its annotations?\nThis action cannot be undone.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if respuesta == QMessageBox.Yes:
                self.parent_view.recorte_eliminar.emit(self.recorte_id)

    # ---------- Selección y manijas de redimensión ----------

    def set_seleccionado(self, valor):
        self.seleccionado = valor
        if valor:
            self.setPen(QPen(COLOR_MANIJA, 3))
            self.setBrush(QBrush(QColor(255, 210, 0, 90)))
            self._crear_manijas()
        else:
            self.setPen(QPen(QColor(255, 170, 0, 220), 2))
            self.setBrush(QBrush(QColor(255, 210, 0, 60)))
            self._quitar_manijas()

    def _crear_manijas(self):
        self._quitar_manijas()
        for esquina in ("nw", "ne", "sw", "se"):
            m = ManijaRedimension(self, esquina)
            self._posicionar_manija(m, esquina)
            self.scene().addItem(m)
            self.manijas.append(m)

    def _posicionar_manija(self, manija, esquina):
        r = self.rect()
        posiciones = {
            "nw": r.topLeft(), "ne": r.topRight(),
            "sw": r.bottomLeft(), "se": r.bottomRight(),
        }
        manija.setPos(posiciones[esquina])

    def _quitar_manijas(self):
        for m in self.manijas:
            if m.scene() is not None:
                self.scene().removeItem(m)
        self.manijas = []

    def redimensionar_desde_esquina(self, esquina, punto_escena):
        r = self.rect()
        if esquina == "se":
            nuevo = QRectF(r.x(), r.y(),
                            max(ANCHO_MINIMO_RECORTE, punto_escena.x() - r.x()),
                            max(ALTO_MINIMO_RECORTE, punto_escena.y() - r.y()))
        elif esquina == "nw":
            nuevo_ancho = max(ANCHO_MINIMO_RECORTE, r.x() + r.width() - punto_escena.x())
            nuevo_alto = max(ALTO_MINIMO_RECORTE, r.y() + r.height() - punto_escena.y())
            nuevo = QRectF(r.x() + r.width() - nuevo_ancho, r.y() + r.height() - nuevo_alto,
                            nuevo_ancho, nuevo_alto)
        elif esquina == "ne":
            nuevo_ancho = max(ANCHO_MINIMO_RECORTE, punto_escena.x() - r.x())
            nuevo_alto = max(ALTO_MINIMO_RECORTE, r.y() + r.height() - punto_escena.y())
            nuevo = QRectF(r.x(), r.y() + r.height() - nuevo_alto, nuevo_ancho, nuevo_alto)
        else:  # "sw"
            nuevo_ancho = max(ANCHO_MINIMO_RECORTE, r.x() + r.width() - punto_escena.x())
            nuevo_alto = max(ALTO_MINIMO_RECORTE, punto_escena.y() - r.y())
            nuevo = QRectF(r.x() + r.width() - nuevo_ancho, r.y(), nuevo_ancho, nuevo_alto)

        # Un recorte no puede extenderse a los márgenes ni a otra página.
        # Además de evitar referencias vacías, esto mantiene las coordenadas
        # PDF guardadas siempre válidas al reabrir el documento.
        info = self.parent_view.paginas_info[self.pagina]
        limites = QRectF(info["x_offset"], info["y_offset"],
                         info["w_scene"], info["h_scene"])
        nuevo = nuevo.intersected(limites)
        self.setRect(nuevo)
        for m in self.manijas:
            self._posicionar_manija(m, m.esquina)

    def confirmar_redimension(self):
        """Se llama al soltar una manija: convierte el rect (en coordenadas de
        escena) de vuelta a puntos PDF relativos a la página, y persiste."""
        info = self.parent_view.paginas_info[self.pagina]
        r = self.rect()
        x_pdf = (r.x() - info["x_offset"]) / RENDER_ZOOM
        y_pdf = (r.y() - info["y_offset"]) / RENDER_ZOOM
        ancho_pdf = r.width() / RENDER_ZOOM
        alto_pdf = r.height() / RENDER_ZOOM
        self.parent_view.recorte_redimensionado.emit(self.recorte_id, x_pdf, y_pdf, ancho_pdf, alto_pdf)


class PDFGraphicsView(QGraphicsView):
    """
    Vista principal del PDF, con scroll continuo (todas las páginas apiladas)
    y carga perezosa de las páginas (ver docstring del módulo).

    Emite señales cuando:
    - se hace click en un recorte existente (recorte_clickeado)
    - el usuario termina de dibujar un rectángulo nuevo (recorte_nuevo)
    - cambia la página visible mientras se hace scroll (pagina_visible_cambio)
    - se elimina un recorte (recorte_eliminar)
    - se termina de redimensionar un recorte (recorte_redimensionado)
    """
    recorte_clickeado = Signal(int)
    # x, y, ancho, alto en puntos PDF (espacio de la página), y número de página
    recorte_nuevo = Signal(float, float, float, float, int)
    pagina_visible_cambio = Signal(int)
    recorte_eliminar = Signal(int)
    recorte_redimensionado = Signal(int, float, float, float, float)
    zoom_cambio = Signal(float)  # emite el nuevo factor de zoom (1.0 = 100%)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene_ = QGraphicsScene(self)
        self.setScene(self.scene_)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.setRenderHint(QPainter.Antialiasing)
        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)

        self.doc = None
        # por cada página: dict con x_offset, y_offset, w_scene, h_scene,
        # w_pdf, h_pdf, "item" (el QGraphicsItem actual, placeholder o pixmap
        # real) y "cargada" (bool)
        self.paginas_info = []
        self._pagina_visible = 0
        self._zoom_actual = 1.0

        self.modo_seleccion = False
        self._rect_temp = None
        self._origen_drag = None

        # modo edición de recortes: al estar activo, click izquierdo sobre un
        # recorte lo selecciona (para redimensionar) en vez de abrirlo
        self.modo_edicion_recortes = False
        self.overlay_seleccionado = None

        self.overlays = []  # lista de RecorteOverlayItem actuales

        self.verticalScrollBar().valueChanged.connect(self._detectar_pagina_visible)
        self.verticalScrollBar().valueChanged.connect(self._actualizar_paginas_cargadas)

    # ---------- Zoom (Ctrl + rueda del mouse) ----------

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            factor = ZOOM_PASO if event.angleDelta().y() > 0 else (1 / ZOOM_PASO)
            nuevo_zoom = self._zoom_actual * factor
            if ZOOM_MIN <= nuevo_zoom <= ZOOM_MAX:
                self.scale(factor, factor)
                self._zoom_actual = nuevo_zoom
                self.zoom_cambio.emit(self._zoom_actual)
                # el zoom cambia cuántas páginas entran en pantalla
                self._actualizar_paginas_cargadas()
            event.accept()
        else:
            super().wheelEvent(event)

    def restablecer_zoom(self):
        self.resetTransform()
        self._zoom_actual = 1.0
        self.zoom_cambio.emit(1.0)
        self._actualizar_paginas_cargadas()

    def zoom_actual(self):
        return self._zoom_actual

    # ---------- Carga de documento (placeholders livianos, sin renderizar todavía) ----------

    def abrir_pdf(self, ruta):
        # liberar el documento anterior si había uno, para no acumular memoria
        if self.doc is not None:
            self.doc.close()

        self.doc = fitz.open(ruta)
        self.scene_.clear()
        self.overlays = []
        self.paginas_info = []

        # primera pasada: SOLO calcular tamaños (page.rect no renderiza nada,
        # es prácticamente gratis), para poder armar el layout sin gastar RAM
        max_w_scene = 0
        tamanos = []
        for page in self.doc:
            w_pdf, h_pdf = page.rect.width, page.rect.height
            w_scene = round(w_pdf * RENDER_ZOOM)
            h_scene = round(h_pdf * RENDER_ZOOM)
            tamanos.append((w_pdf, h_pdf, w_scene, h_scene))
            max_w_scene = max(max_w_scene, w_scene)

        # segunda pasada: colocar un placeholder liviano por página, apiladas
        # y centradas horizontalmente. Todavía no se renderiza ningún PDF real.
        y_actual = 0.0
        for w_pdf, h_pdf, w_scene, h_scene in tamanos:
            x_offset = (max_w_scene - w_scene) / 2.0
            placeholder = self._crear_placeholder(w_scene, h_scene)
            placeholder.setPos(x_offset, y_actual)
            self.scene_.addItem(placeholder)

            self.paginas_info.append({
                "x_offset": x_offset,
                "y_offset": y_actual,
                "w_scene": w_scene,
                "h_scene": h_scene,
                "w_pdf": w_pdf,
                "h_pdf": h_pdf,
                "item": placeholder,
                "cargada": False,
            })
            y_actual += h_scene + GAP_ENTRE_PAGINAS

        alto_total = y_actual - GAP_ENTRE_PAGINAS if tamanos else 0
        self.scene_.setSceneRect(QRectF(0, 0, max_w_scene, alto_total))
        self.resetTransform()
        self.verticalScrollBar().setValue(0)
        self._pagina_visible = 0
        self._zoom_actual = 1.0
        self.zoom_cambio.emit(1.0)

        # recién ahora se renderizan de verdad las páginas cercanas al inicio
        self._actualizar_paginas_cargadas()

    def _crear_placeholder(self, w_scene, h_scene):
        item = QGraphicsRectItem(0, 0, w_scene, h_scene)
        item.setBrush(QBrush(QColor(238, 238, 238)))
        item.setPen(QPen(QColor(215, 215, 215), 1))
        return item

    def num_paginas(self):
        return self.doc.page_count if self.doc else 0

    def pagina_visible(self):
        return self._pagina_visible

    # ---------- Carga / descarga perezosa según scroll ----------

    def _rango_indices_visibles(self):
        """Devuelve (primera, ultima) página que se ve, aunque sea parcialmente, en el viewport."""
        if not self.paginas_info:
            return 0, 0
        rect_visible = self.mapToScene(self.viewport().rect()).boundingRect()
        top, bottom = rect_visible.top(), rect_visible.bottom()
        primera, ultima = None, None
        for i, info in enumerate(self.paginas_info):
            y0, y1 = info["y_offset"], info["y_offset"] + info["h_scene"]
            if y1 >= top and y0 <= bottom:
                if primera is None:
                    primera = i
                ultima = i
        if primera is None:
            primera = ultima = min(self._pagina_visible, len(self.paginas_info) - 1)
        return primera, ultima

    def pagina_es_visible(self, num_pagina):
        """True si esa página ya se ve (aunque sea parcialmente) en el viewport actual."""
        primera, ultima = self._rango_indices_visibles()
        return primera <= num_pagina <= ultima

    def _actualizar_paginas_cargadas(self, _valor=None):
        if not self.paginas_info or self.doc is None:
            return
        primera, ultima = self._rango_indices_visibles()
        n = len(self.paginas_info)

        rango_carga = range(max(0, primera - BUFFER_CARGA), min(n, ultima + BUFFER_CARGA + 1))
        rango_mantener = set(range(max(0, primera - BUFFER_DESCARGA), min(n, ultima + BUFFER_DESCARGA + 1)))

        for i in rango_carga:
            if not self.paginas_info[i]["cargada"]:
                self._cargar_pagina(i)

        for i, info in enumerate(self.paginas_info):
            if info["cargada"] and i not in rango_mantener:
                self._descargar_pagina(i)

    def _cargar_pagina(self, i):
        info = self.paginas_info[i]
        page = self.doc[i]
        mat = fitz.Matrix(RENDER_ZOOM, RENDER_ZOOM)
        pix = page.get_pixmap(matrix=mat)
        # .copy() es importante: el buffer de "pix" se libera cuando la
        # variable local desaparece, y no queremos que la imagen quede corrupta
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
        qpixmap = QPixmap.fromImage(img)

        nuevo_item = QGraphicsPixmapItem(qpixmap)
        nuevo_item.setPos(info["x_offset"], info["y_offset"])
        self.scene_.addItem(nuevo_item)
        self.scene_.removeItem(info["item"])
        info["item"] = nuevo_item
        info["cargada"] = True

    def _descargar_pagina(self, i):
        info = self.paginas_info[i]
        placeholder = self._crear_placeholder(info["w_scene"], info["h_scene"])
        placeholder.setPos(info["x_offset"], info["y_offset"])
        self.scene_.addItem(placeholder)
        self.scene_.removeItem(info["item"])
        info["item"] = placeholder
        info["cargada"] = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._actualizar_paginas_cargadas()

    def _detectar_pagina_visible(self, _valor=None):
        """Determina qué página ocupa la parte superior del viewport actual."""
        if not self.paginas_info:
            return
        punto_superior = self.mapToScene(0, 0).y()
        nueva_pagina = 0
        for i, info in enumerate(self.paginas_info):
            if info["y_offset"] <= punto_superior:
                nueva_pagina = i
            else:
                break
        if nueva_pagina != self._pagina_visible:
            self._pagina_visible = nueva_pagina
            self.pagina_visible_cambio.emit(nueva_pagina)

    def ir_a_pagina(self, num_pagina):
        if not self.paginas_info or num_pagina < 0 or num_pagina >= len(self.paginas_info):
            return
        info = self.paginas_info[num_pagina]
        # centerOn funciona correctamente sin importar el zoom actual (a
        # diferencia de tocar el valor de la scrollbar directamente, que
        # asume zoom=1.0 y se desalinea si hay zoom aplicado). Centramos
        # el punto que deja el borde superior de la página cerca del tope
        # visible, usando el alto real del viewport en unidades de escena.
        alto_viewport_escena = self.viewport().height() / max(0.01, self._zoom_actual)
        ancho_viewport_escena = self.viewport().width() / max(0.01, self._zoom_actual)
        x_centro = info["x_offset"] + min(info["w_scene"], ancho_viewport_escena) / 2
        y_centro = info["y_offset"] + alto_viewport_escena / 2
        self.centerOn(x_centro, y_centro)
        self._pagina_visible = num_pagina
        self._actualizar_paginas_cargadas()

    # ---------- Conversión de coordenadas página <-> escena ----------

    def pdf_a_escena(self, pagina, x, y, w, h):
        """Convierte un rect en puntos PDF de una página dada a coordenadas absolutas de la escena."""
        info = self.paginas_info[pagina]
        return QRectF(
            info["x_offset"] + x * RENDER_ZOOM,
            info["y_offset"] + y * RENDER_ZOOM,
            w * RENDER_ZOOM,
            h * RENDER_ZOOM,
        )

    def escena_a_pdf(self, rect):
        """
        Convierte un QRectF en coordenadas absolutas de la escena a
        (pagina, x, y, w, h) en puntos PDF relativos a esa página.
        Usa el centro del rect para decidir a qué página pertenece.
        """
        centro_y = rect.y() + rect.height() / 2.0
        pagina = 0
        for i, info in enumerate(self.paginas_info):
            if info["y_offset"] <= centro_y <= info["y_offset"] + info["h_scene"]:
                pagina = i
                break
        info = self.paginas_info[pagina]
        x = (rect.x() - info["x_offset"]) / RENDER_ZOOM
        y = (rect.y() - info["y_offset"]) / RENDER_ZOOM
        w = rect.width() / RENDER_ZOOM
        h = rect.height() / RENDER_ZOOM
        return pagina, x, y, w, h

    def cargar_overlays(self, recortes):
        """recortes: lista de dicts con id, pagina, x, y, ancho, alto (en puntos PDF), de TODO el libro."""
        for ov in self.overlays:
            ov._quitar_manijas()
            self.scene_.removeItem(ov)
        self.overlays = []
        self.overlay_seleccionado = None
        for r in recortes:
            if r["pagina"] >= len(self.paginas_info):
                continue
            rect = self.pdf_a_escena(r["pagina"], r["x"], r["y"], r["ancho"], r["alto"])
            item = RecorteOverlayItem(r["id"], r["pagina"], rect, self)
            self.scene_.addItem(item)
            self.overlays.append(item)

    # ---------- Modo edición de recortes (seleccionar/redimensionar/eliminar) ----------

    def set_modo_edicion_recortes(self, activo):
        self.modo_edicion_recortes = activo
        if not activo:
            self.deseleccionar_overlay_actual()

    def seleccionar_overlay(self, overlay):
        if self.overlay_seleccionado is overlay:
            return
        self.deseleccionar_overlay_actual()
        self.overlay_seleccionado = overlay
        overlay.set_seleccionado(True)

    def deseleccionar_overlay_actual(self):
        if self.overlay_seleccionado is not None:
            self.overlay_seleccionado.set_seleccionado(False)
            self.overlay_seleccionado = None

    # ---------- Modo selección (crear recorte nuevo) ----------

    def set_modo_seleccion(self, activo):
        self.modo_seleccion = activo
        if activo:
            self.setCursor(QCursor(Qt.CrossCursor))
        else:
            self.setCursor(QCursor(Qt.ArrowCursor))

    def mousePressEvent(self, event):
        if self.modo_seleccion and event.button() == Qt.LeftButton:
            self._origen_drag = self.mapToScene(event.pos())
            if self._pagina_en_punto(self._origen_drag) is None:
                # Ignorar el espacio entre páginas y los márgenes laterales.
                self._origen_drag = None
                event.accept()
                return
            self._rect_temp = QGraphicsRectItem(QRectF(self._origen_drag, self._origen_drag))
            self._rect_temp.setBrush(QBrush(QColor(0, 150, 255, 60)))
            self._rect_temp.setPen(QPen(QColor(0, 120, 255, 220), 2, Qt.DashLine))
            self._rect_temp.setZValue(20)
            self.scene_.addItem(self._rect_temp)
            return

        # en modo edición, clickear en un área vacía (ni un recorte ni una
        # manija) deselecciona el recorte actualmente seleccionado
        if self.modo_edicion_recortes and event.button() == Qt.LeftButton:
            item_bajo_cursor = self.itemAt(event.pos())
            if not isinstance(item_bajo_cursor, (RecorteOverlayItem, ManijaRedimension)):
                self.deseleccionar_overlay_actual()

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.modo_seleccion and self._rect_temp is not None:
            actual = self.mapToScene(event.pos())
            pagina = self._pagina_en_punto(self._origen_drag)
            info = self.paginas_info[pagina]
            actual.setX(min(max(actual.x(), info["x_offset"]),
                            info["x_offset"] + info["w_scene"]))
            actual.setY(min(max(actual.y(), info["y_offset"]),
                            info["y_offset"] + info["h_scene"]))
            rect = QRectF(self._origen_drag, actual).normalized()
            self._rect_temp.setRect(rect)
            return
        super().mouseMoveEvent(event)

    def _pagina_en_punto(self, punto):
        """Índice de la página que contiene el punto, o ``None`` en un margen."""
        for i, info in enumerate(self.paginas_info):
            limites = QRectF(info["x_offset"], info["y_offset"],
                             info["w_scene"], info["h_scene"])
            if limites.contains(punto):
                return i
        return None

    def mouseReleaseEvent(self, event):
        if self.modo_seleccion and self._rect_temp is not None and event.button() == Qt.LeftButton:
            rect = self._rect_temp.rect()
            self.scene_.removeItem(self._rect_temp)
            self._rect_temp = None
            self._origen_drag = None
            self.set_modo_seleccion(False)

            if rect.width() > 5 and rect.height() > 5:
                pagina, x, y, w, h = self.escena_a_pdf(rect)
                self.recorte_nuevo.emit(x, y, w, h, pagina)
            return
        super().mouseReleaseEvent(event)


ANCHO_MINIATURA = 90
MARGEN_CARGA_MINIATURAS = 4  # páginas de más (arriba/abajo de lo visible) que se pre-renderizan


class PanelMiniaturas(QWidget):
    """Panel angosto (no ocupa toda la app) con una miniatura por página.
    Clickear una miniatura salta a esa página. Carga perezosa: solo se
    renderizan las miniaturas cerca de lo que se ve en este panel (mismo
    espíritu que la carga perezosa del visor principal, para no gastar
    memoria de más en libros largos)."""

    pagina_elegida = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        titulo = QLabel("Pages")
        titulo.setStyleSheet("font-weight: bold; padding: 2px;")
        layout.addWidget(titulo)

        self.lista = QListWidget()
        self.lista.setIconSize(QSize(ANCHO_MINIATURA, int(ANCHO_MINIATURA * 1.4142)))
        self.lista.setSpacing(6)
        self.lista.setResizeMode(QListWidget.Adjust)
        layout.addWidget(self.lista)

        self.doc = None
        self._cargadas = set()
        self._icono_placeholder = self._crear_icono_placeholder()

        self.lista.itemClicked.connect(self._on_item_clicked)
        self.lista.verticalScrollBar().valueChanged.connect(self._cargar_visibles)

    def _crear_icono_placeholder(self):
        """Ícono gris del mismo tamaño que las miniaturas reales. Importante:
        se lo asignamos a TODOS los items desde el arranque (no solo a los
        que todavía no cargaron), para que cada fila ya tenga su altura
        final desde el primer momento — si un item no tiene ícono todavía,
        Qt calcula su fila mucho más baja (solo el alto del texto), y eso
        hace que el cálculo de "qué está visible" se equivoque feo (cree
        que entran muchas más filas de las que en realidad entran)."""
        alto = int(ANCHO_MINIATURA * 1.4142)
        pix = QPixmap(ANCHO_MINIATURA, alto)
        pix.fill(QColor(230, 230, 230))
        return QIcon(pix)

    def cargar_documento(self, doc):
        self.doc = doc
        self.lista.clear()
        self._cargadas = set()
        for i in range(doc.page_count):
            item = QListWidgetItem(f"{i + 1}")
            item.setIcon(self._icono_placeholder)
            item.setData(Qt.UserRole, i)
            item.setTextAlignment(Qt.AlignHCenter)
            self.lista.addItem(item)
        self._cargar_visibles()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._cargar_visibles()

    def _cargar_visibles(self, *_args):
        if not self.doc or self.lista.count() == 0:
            return
        rect = self.lista.viewport().rect()
        # mientras el splitter todavía está acomodando el layout, a veces
        # pasa brevemente por anchos absurdamente angostos (más chicos que
        # la propia miniatura) — ahí Qt calcula mal las filas visibles.
        # Ese estado es transitorio: ignorarlo y esperar al próximo resize
        # (que ya trae el ancho real) es más simple y robusto que intentar
        # "arreglar" el cálculo para un estado que ni siquiera es válido.
        if rect.width() < ANCHO_MINIATURA * 0.8:
            return
        # ojo: no usar las esquinas exactas (0,0)/(0,alto) — con el espaciado
        # entre items, esos puntos caen en el margen entre uno y otro y
        # indexAt() devuelve -1 ahí. Con un margen interior seguro (15px,
        # mayor al spacing de 6px) siempre cae DENTRO de algún item.
        primero = self.lista.indexAt(QPoint(10, 15)).row()
        ultimo = self.lista.indexAt(QPoint(10, max(16, rect.height() - 15))).row()
        if primero == -1:
            primero = 0
        if ultimo == -1:
            ultimo = min(primero + 10, self.lista.count() - 1)

        inicio = max(0, primero - MARGEN_CARGA_MINIATURAS)
        fin = min(self.lista.count() - 1, ultimo + MARGEN_CARGA_MINIATURAS)
        for i in range(inicio, fin + 1):
            if i not in self._cargadas:
                self._renderizar_miniatura(i)
                self._cargadas.add(i)

    def _renderizar_miniatura(self, indice):
        page = self.doc[indice]
        zoom = ANCHO_MINIATURA / page.rect.width
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat)
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
        item = self.lista.item(indice)
        if item is not None:
            item.setIcon(QIcon(QPixmap.fromImage(img)))

    def _on_item_clicked(self, item):
        self.pagina_elegida.emit(item.data(Qt.UserRole))

    def resaltar_pagina(self, num_pagina):
        if 0 <= num_pagina < self.lista.count():
            self.lista.setCurrentRow(num_pagina)


class BotonMarcador(QPushButton):
    """Botón tipo 'píldora' para un marcador. Click derecho: eliminarlo."""

    eliminar_solicitado = Signal(int)

    def __init__(self, marcador_id, titulo, parent=None):
        super().__init__(f"📌 {titulo}", parent)
        self.marcador_id = marcador_id
        self.setCheckable(True)

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            menu = QMenu(self)
            accion = menu.addAction("🗑 Delete bookmark")
            elegido = menu.exec(event.globalPosition().toPoint())
            if elegido == accion:
                self.eliminar_solicitado.emit(self.marcador_id)
            return
        super().mousePressEvent(event)


class LectorWidget(QWidget):
    """Widget completo del lector: panel de miniaturas (angosto, al costado)
    + barra superior + fila de marcadores + vista PDF con scroll continuo."""

    recorte_clickeado = Signal(int)
    recorte_nuevo = Signal(float, float, float, float, int)
    recorte_eliminar = Signal(int)
    recorte_redimensionado = Signal(int, float, float, float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout_externo = QHBoxLayout(self)
        layout_externo.setContentsMargins(0, 0, 0, 0)
        layout_externo.setSpacing(0)

        # --- panel de miniaturas: angosto, al costado (no ocupa la app) ---
        self.panel_miniaturas = PanelMiniaturas()
        self.panel_miniaturas.pagina_elegida.connect(self.ir_a_pagina)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self.panel_miniaturas)

        contenedor_principal = QWidget()
        layout = QVBoxLayout(contenedor_principal)
        layout.setContentsMargins(0, 0, 0, 0)

        barra = QHBoxLayout()
        self.btn_miniaturas = QPushButton("🖼")
        self.btn_miniaturas.setCheckable(True)
        self.btn_miniaturas.setChecked(True)
        self.btn_miniaturas.setToolTip("Show/hide thumbnail panel")
        self.btn_miniaturas.setFixedWidth(32)
        self.label_pagina = QLabel("Page 1 / 1")
        self.label_pagina.setObjectName("readerMeta")
        self.spin_ir_a = QSpinBox()
        self.spin_ir_a.setMinimum(1)
        self.spin_ir_a.setMaximum(1)
        self.btn_ir = QPushButton("Go")
        self.label_zoom = QLabel("Zoom: 100%")
        self.btn_reset_zoom = QPushButton("🔍 100%")
        self.btn_reset_zoom.setToolTip("Reset zoom (also: Ctrl + mouse wheel to zoom in/out)")
        self.btn_nuevo_recorte = QPushButton("✂ New crop")
        self.btn_nuevo_recorte.setCheckable(True)
        self.btn_editar_recortes = QPushButton("🔧 Edit crops")
        self.btn_editar_recortes.setCheckable(True)
        self.btn_editar_recortes.setToolTip(
            "Select a crop to drag its corners and resize it.\n"
            "Right-click a crop (anytime): delete it."
        )

        barra.addWidget(self.btn_miniaturas)
        barra.addWidget(self.label_pagina)
        barra.addWidget(QLabel("Go to page:"))
        barra.addWidget(self.spin_ir_a)
        barra.addWidget(self.btn_ir)
        barra.addSpacing(12)
        barra.addWidget(self.label_zoom)
        barra.addWidget(self.btn_reset_zoom)
        barra.addStretch()
        barra.addWidget(self.btn_editar_recortes)
        barra.addWidget(self.btn_nuevo_recorte)
        layout.addLayout(barra)

        # --- fila de marcadores: "vistazo" a una página lejana y volver ---
        fila_marcadores = QHBoxLayout()
        self.btn_marcar_pagina = QPushButton("📌 Bookmark this page")
        self.btn_marcar_pagina.setToolTip(
            "Creates a bookmark on the current page.\n"
            "Afterwards, tap it from anywhere to jump there, and tap it "
            "again to return to where you were."
        )
        fila_marcadores.addWidget(self.btn_marcar_pagina)
        self._layout_botones_marcadores = QHBoxLayout()
        fila_marcadores.addLayout(self._layout_botones_marcadores)
        fila_marcadores.addStretch()
        layout.addLayout(fila_marcadores)

        self.view = PDFGraphicsView()
        self.view.setObjectName("pdfCanvas")
        layout.addWidget(self.view)

        splitter.addWidget(contenedor_principal)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([150, 900])
        layout_externo.addWidget(splitter)

        self.btn_ir.clicked.connect(self._ir_a_pagina_spin)
        self.btn_nuevo_recorte.toggled.connect(self._on_toggle_nuevo_recorte)
        self.btn_editar_recortes.toggled.connect(self._on_toggle_editar_recortes)
        self.btn_miniaturas.toggled.connect(self.panel_miniaturas.setVisible)
        self.btn_marcar_pagina.clicked.connect(self._crear_marcador)
        self.btn_reset_zoom.clicked.connect(self.view.restablecer_zoom)
        self.view.zoom_cambio.connect(self._actualizar_label_zoom)
        self.view.recorte_clickeado.connect(self.recorte_clickeado.emit)
        self.view.recorte_nuevo.connect(self._on_recorte_nuevo)
        self.view.recorte_eliminar.connect(self.recorte_eliminar.emit)
        self.view.recorte_redimensionado.connect(self.recorte_redimensionado.emit)
        self.view.pagina_visible_cambio.connect(self._actualizar_label)

        # estado de "vistazo" (peek): marcador activo + página de origen
        self.libro_id_actual = None
        self._botones_marcadores = []
        self._marcador_activo_id = None
        self._pagina_antes_de_vistazo = None

    def _on_toggle_nuevo_recorte(self, activo):
        if activo:
            self.btn_editar_recortes.setChecked(False)
        self.view.set_modo_seleccion(activo)

    def _on_toggle_editar_recortes(self, activo):
        if activo:
            self.btn_nuevo_recorte.setChecked(False)
        self.view.set_modo_edicion_recortes(activo)

    def _on_recorte_nuevo(self, x, y, w, h, pagina):
        self.btn_nuevo_recorte.setChecked(False)
        self.recorte_nuevo.emit(x, y, w, h, pagina)

    def abrir_pdf(self, ruta):
        self.view.abrir_pdf(ruta)
        self.spin_ir_a.setMaximum(max(1, self.view.num_paginas()))
        self._actualizar_label(0)
        self.panel_miniaturas.cargar_documento(self.view.doc)

    def cargar_overlays(self, recortes):
        self.view.cargar_overlays(recortes)

    def pagina_actual(self):
        return self.view.pagina_visible()

    def pagina_es_visible(self, num_pagina):
        return self.view.pagina_es_visible(num_pagina)

    def ir_a_pagina(self, num_pagina):
        self.view.ir_a_pagina(num_pagina)
        self._actualizar_label(num_pagina)

    def _ir_a_pagina_spin(self):
        self.ir_a_pagina(self.spin_ir_a.value() - 1)

    def _actualizar_label(self, num_pagina):
        self.label_pagina.setText(f"Page {num_pagina + 1} / {self.view.num_paginas()}")
        self.panel_miniaturas.resaltar_pagina(num_pagina)

    def _actualizar_label_zoom(self, zoom):
        self.label_zoom.setText(f"Zoom: {int(round(zoom * 100))}%")

    # ---------- Marcadores ("vistazo": ir a una página lejana y volver) ----------

    def cargar_marcadores(self, libro_id):
        """Se llama al abrir un libro: guarda el libro_id y repuebla los botones."""
        self.libro_id_actual = libro_id
        self._marcador_activo_id = None
        self._pagina_antes_de_vistazo = None
        marcadores = db.listar_marcadores_por_libro(libro_id)
        self._reconstruir_botones_marcadores(marcadores)

    def _reconstruir_botones_marcadores(self, marcadores):
        for btn in self._botones_marcadores:
            self._layout_botones_marcadores.removeWidget(btn)
            btn.deleteLater()
        self._botones_marcadores = []

        for m in marcadores:
            btn = BotonMarcador(m["id"], m["titulo"])
            btn.setProperty("pagina_marcador", m["pagina"])
            btn.clicked.connect(lambda _checked=False, b=btn: self._on_click_marcador(b))
            btn.eliminar_solicitado.connect(self._eliminar_marcador)
            self._layout_botones_marcadores.addWidget(btn)
            self._botones_marcadores.append(btn)

    def _crear_marcador(self):
        if self.libro_id_actual is None:
            return
        pagina = self.pagina_actual()
        titulo, ok = QInputDialog.getText(
            self, "New bookmark", "Bookmark name:",
            text=f"Page {pagina + 1}",
        )
        if not ok or not titulo.strip():
            return
        db.crear_marcador(self.libro_id_actual, pagina, titulo.strip())
        self._reconstruir_botones_marcadores(db.listar_marcadores_por_libro(self.libro_id_actual))

    def _eliminar_marcador(self, marcador_id):
        db.eliminar_marcador(marcador_id)
        if self._marcador_activo_id == marcador_id:
            self._marcador_activo_id = None
            self._pagina_antes_de_vistazo = None
        self._reconstruir_botones_marcadores(db.listar_marcadores_por_libro(self.libro_id_actual))

    def _on_click_marcador(self, boton):
        marcador_id = boton.marcador_id
        pagina_marcador = boton.property("pagina_marcador")

        if self._marcador_activo_id == marcador_id:
            # ya estábamos viendo este marcador -> volver a donde estabas
            if self._pagina_antes_de_vistazo is not None:
                self.ir_a_pagina(self._pagina_antes_de_vistazo)
            self._marcador_activo_id = None
            self._pagina_antes_de_vistazo = None
        else:
            if self._marcador_activo_id is None:
                # recién arrancamos un vistazo: recordar de dónde veníamos
                self._pagina_antes_de_vistazo = self.pagina_actual()
            self._marcador_activo_id = marcador_id
            self.ir_a_pagina(pagina_marcador)

        self._actualizar_estado_botones_marcadores()

    def _actualizar_estado_botones_marcadores(self):
        for btn in self._botones_marcadores:
            btn.setChecked(btn.marcador_id == self._marcador_activo_id)
