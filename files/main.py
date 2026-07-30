"""
Prototipo: gestor de PDF con recortes anotables.

Vista principal = split view:
  - Izquierda: lector de PDF con overlays de recortes clickeables
  - Derecha: hoja A4 de edición (en blanco por defecto, con toggle de referencia)

La biblioteca es un grid con portadas (miniatura de la primera página de
cada PDF), con carga perezosa para no gastar memoria de más si hay muchos
libros importados.
"""
import sys
import os
import fitz
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QListWidget, QListWidgetItem, QPushButton, QFileDialog,
    QLabel, QStackedWidget, QMessageBox, QCheckBox, QMenu
)
from PySide6.QtGui import QPixmap, QImage, QIcon, QColor, QAction, QPainter, QFont
from PySide6.QtCore import Qt, QSize, QPoint

import db
from pdf_view import LectorWidget, RENDER_ZOOM
from canvas_a4 import HojaEdicionWidget

VERSION_APP = "0.9 (prototype)"


def crear_icono_app():
    """Ícono simple generado en código (círculo con acento + 'M'), para no
    depender de un archivo .png externo que el usuario tendría que tener
    a mano."""
    pix = QPixmap(128, 128)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor("#4a72e8"))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(4, 4, 120, 120, 28, 28)
    painter.setPen(QColor("#ffffff"))
    fuente = QFont("Segoe UI", 60, QFont.Bold)
    painter.setFont(fuente)
    painter.drawText(pix.rect(), Qt.AlignCenter, "M")
    painter.end()
    return QIcon(pix)

ANCHO_PORTADA = 140
ALTO_PORTADA = int(ANCHO_PORTADA * 1.4142)
MARGEN_CARGA_PORTADAS = 3  # libros de más (arriba/abajo de lo visible) que se pre-renderizan


class VistaBiblioteca(QWidget):
    """Grid con la portada de cada libro (miniatura de su primera página).
    Carga perezosa: solo se renderizan las portadas cerca de lo que se ve,
    igual que hicimos con las miniaturas de página dentro del lector."""

    def __init__(self, on_abrir_libro, mostrar_estado=None):
        super().__init__()
        self.on_abrir_libro = on_abrir_libro
        self.mostrar_estado = mostrar_estado or (lambda *a, **k: None)
        layout = QVBoxLayout(self)

        titulo = QLabel("📚 My library")
        titulo.setObjectName("libraryTitle")
        titulo.setStyleSheet("font-size: 20px; font-weight: bold; padding: 8px;")
        layout.addWidget(titulo)

        barra = QHBoxLayout()
        self.btn_importar = QPushButton("+ Import PDF")
        self.btn_importar.clicked.connect(self._importar)
        barra.addWidget(self.btn_importar)
        barra.addStretch()
        layout.addLayout(barra)

        self.label_vacio = QLabel(
            "No books yet.\nClick \"+ Import PDF\" above to add your first one."
        )
        self.label_vacio.setAlignment(Qt.AlignCenter)
        self.label_vacio.setStyleSheet("color: gray; font-size: 15px; padding: 60px;")
        self.label_vacio.setVisible(False)
        layout.addWidget(self.label_vacio)

        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.IconMode)
        self.grid.setMovement(QListWidget.Static)
        self.grid.setResizeMode(QListWidget.Adjust)
        self.grid.setWrapping(True)
        self.grid.setSpacing(14)
        self.grid.setIconSize(QSize(ANCHO_PORTADA, ALTO_PORTADA))
        self.grid.setGridSize(QSize(ANCHO_PORTADA + 30, ALTO_PORTADA + 60))
        self.grid.itemDoubleClicked.connect(self._abrir_seleccionado)
        self.grid.verticalScrollBar().valueChanged.connect(self._cargar_visibles)
        layout.addWidget(self.grid)

        self._cargadas = set()
        self._icono_placeholder = self._crear_icono_placeholder()

        self.refrescar()

    def _crear_icono_placeholder(self):
        """Portada gris antes de renderizar la real — con el mismo tamaño
        final, para que Qt calcule bien qué filas están visibles desde el
        arranque (ver el mismo problema que resolvimos en las miniaturas
        de página del lector)."""
        pix = QPixmap(ANCHO_PORTADA, ALTO_PORTADA)
        pix.fill(QColor(225, 225, 230))
        return QIcon(pix)

    def refrescar(self):
        self.grid.clear()
        self._cargadas = set()
        libros = db.listar_libros()
        for libro in libros:
            item = QListWidgetItem(f"{libro['titulo']}\n{libro['autor'] or 'Unknown author'}")
            item.setIcon(self._icono_placeholder)
            item.setData(Qt.UserRole, libro["id"])
            item.setData(Qt.UserRole + 1, libro["ruta_archivo"])
            item.setTextAlignment(Qt.AlignHCenter)
            self.grid.addItem(item)
        self._cargar_visibles()
        self.label_vacio.setVisible(len(libros) == 0)
        self.grid.setVisible(len(libros) > 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._cargar_visibles()

    def contextMenuEvent(self, event):
        item = self.grid.itemAt(self.grid.viewport().mapFromGlobal(event.globalPos()))
        if item is None:
            return
        menu = QMenu(self)
        accion_eliminar = menu.addAction("🗑 Remove from library")
        elegido = menu.exec(event.globalPos())
        if elegido == accion_eliminar:
            self._eliminar_libro(item)

    def _eliminar_libro(self, item):
        titulo = item.data(Qt.UserRole + 1)
        libro_id = item.data(Qt.UserRole)
        respuesta = QMessageBox.question(
            self, "Remove book",
            "Remove this book from your library?\n"
            "Its crops and annotations will be deleted too.\n"
            "The original PDF file on disk is not touched.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if respuesta == QMessageBox.Yes:
            db.eliminar_libro(libro_id)
            self.refrescar()
            self.mostrar_estado("Book removed from library.")

    def _cargar_visibles(self, *_args):
        if self.grid.count() == 0:
            return
        rect = self.grid.viewport().rect()
        primero = self.grid.indexAt(rect.topLeft() + self._margen_interior()).row()
        if primero == -1:
            primero = 0

        # para el final del rango visible NO usamos indexAt: el punto de
        # prueba puede caer en el "hueco" de una fila a medio mostrar (sin
        # ítem ahí) y devolver -1 — en vez de eso, estimamos cuántas celdas
        # entran a partir del tamaño de celda del grid, que es confiable
        # sin importar en qué fila/columna exacta caiga el punto de prueba
        tam_celda = self.grid.gridSize()
        columnas = max(1, rect.width() // max(1, tam_celda.width()))
        filas_visibles = -(-rect.height() // max(1, tam_celda.height())) + 1  # ceil + 1 de margen
        estimado = columnas * filas_visibles
        ultimo = min(self.grid.count() - 1, primero + estimado)

        inicio = max(0, primero - MARGEN_CARGA_PORTADAS)
        fin = min(self.grid.count() - 1, ultimo + MARGEN_CARGA_PORTADAS)
        for i in range(inicio, fin + 1):
            if i not in self._cargadas:
                self._renderizar_portada(i)
                self._cargadas.add(i)

    def _margen_interior(self):
        return QPoint(15, 15)

    def _renderizar_portada(self, indice):
        item = self.grid.item(indice)
        if item is None:
            return
        ruta = item.data(Qt.UserRole + 1)
        try:
            doc = fitz.open(ruta)
            page = doc[0]
            zoom = ANCHO_PORTADA / page.rect.width
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat)
            img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
            item.setIcon(QIcon(QPixmap.fromImage(img)))
            doc.close()
        except Exception:
            # PDF corrupto, movido o inaccesible: portada gris con avisito,
            # no queremos que un libro roto tire abajo toda la biblioteca
            pix = QPixmap(ANCHO_PORTADA, ALTO_PORTADA)
            pix.fill(QColor(240, 220, 220))
            item.setIcon(QIcon(pix))

    def _importar(self):
        ruta, _ = QFileDialog.getOpenFileName(self, "Import PDF", "", "PDF (*.pdf)")
        if not ruta:
            return
        ruta = os.path.abspath(ruta)
        existente = db.obtener_libro_por_ruta(ruta)
        if existente:
            QMessageBox.information(
                self, "Already in library",
                f'"{existente["titulo"]}" is already in your library.',
            )
            return
        try:
            doc = fitz.open(ruta)
            titulo = doc.metadata.get("title") or os.path.splitext(os.path.basename(ruta))[0]
            autor = doc.metadata.get("author") or ""
            doc.close()
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Could not open the PDF: {e}")
            return

        try:
            db.agregar_libro(titulo, autor, ruta)
        except Exception as e:
            QMessageBox.warning(self, "Import error", f"Could not import the PDF: {e}")
            return
        self.refrescar()
        self.mostrar_estado(f'"{titulo}" imported successfully.')

    def _abrir_seleccionado(self, item):
        libro_id = item.data(Qt.UserRole)
        self.on_abrir_libro(libro_id)


class VistaLectorEdicion(QWidget):
    """Split view: lector de PDF a la izquierda, hoja de edición a la derecha."""

    def __init__(self, on_volver, mostrar_estado=None):
        super().__init__()
        self.libro_actual = None
        self.on_volver = on_volver
        self.mostrar_estado = mostrar_estado or (lambda *a, **k: None)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        barra_superior = QHBoxLayout()
        self.btn_volver = QPushButton("← Library")
        self.btn_volver.clicked.connect(self.on_volver)
        self.label_libro = QLabel("")
        self.label_libro.setObjectName("readerTitle")
        self.label_libro.setStyleSheet("font-weight: bold;")
        self.chk_ref_siempre = QCheckBox("Always show the reference when opening a crop")
        self.chk_ref_siempre.toggled.connect(self._toggle_ref_siempre)

        barra_superior.addWidget(self.btn_volver)
        barra_superior.addWidget(self.label_libro)
        barra_superior.addStretch()
        barra_superior.addWidget(self.chk_ref_siempre)
        layout.addLayout(barra_superior)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(8)
        self.lector = LectorWidget()
        self.hoja = HojaEdicionWidget()
        splitter.addWidget(self.lector)
        splitter.addWidget(self.hoja)
        # ambos paneles se estiran al mover el divisor (arrastrando la línea del medio)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([700, 700])
        layout.addWidget(splitter)

        self.lector.recorte_clickeado.connect(self._abrir_recorte)
        self.lector.recorte_nuevo.connect(self._crear_recorte)
        self.lector.recorte_eliminar.connect(self._eliminar_recorte)
        self.lector.recorte_redimensionado.connect(self._redimensionar_recorte)
        # cada vez que cambia la página visible (scroll), se guarda como
        # "última página" del libro, para poder retomar ahí la próxima vez
        self.lector.view.pagina_visible_cambio.connect(self._on_pagina_cambio)

        # cargar preferencia global de referencia
        pref = db.get_config("mostrar_ref_siempre", "0")
        self.chk_ref_siempre.setChecked(pref == "1")

    def _toggle_ref_siempre(self, activo):
        db.set_config("mostrar_ref_siempre", "1" if activo else "0")

    def abrir_libro(self, libro_id):
        # guardar trazos del recorte anterior antes de cambiar de libro
        self._guardar_hoja_actual()

        libro = db.obtener_libro(libro_id)
        if not libro:
            return False

        try:
            self.lector.abrir_pdf(libro["ruta_archivo"])
        except Exception as e:
            QMessageBox.warning(
                self, "Error opening book",
                f'Could not open "{libro["titulo"]}":\n{e}\n\n'
                "The file may have been moved, renamed, or deleted.",
            )
            return False

        self.libro_actual = libro
        self.label_libro.setText(libro["titulo"])

        # retomar en la página donde estabas la última vez que abriste este libro
        ultima_pagina = libro.get("ultima_pagina") or 0
        if ultima_pagina > 0:
            self.lector.ir_a_pagina(ultima_pagina)

        self._refrescar_overlays()
        self.lector.cargar_marcadores(libro_id)
        self.hoja.abrir_recorte(None, "No crop open", [])
        self.hoja.hide()  # el lienzo solo aparece al tocar un recorte
        return True

    def _on_pagina_cambio(self, num_pagina):
        if self.libro_actual:
            db.actualizar_ultima_pagina(self.libro_actual["id"], num_pagina)

    def _refrescar_overlays(self):
        if not self.libro_actual:
            return
        # con scroll continuo se ven varias páginas a la vez, así que se cargan
        # los overlays de TODO el libro de una sola vez (no por página)
        recortes = db.listar_recortes_por_libro(self.libro_actual["id"])
        self.lector.cargar_overlays(recortes)

    def _crear_recorte(self, x, y, w, h, pagina):
        if not self.libro_actual:
            return
        self._guardar_hoja_actual()
        recorte_id = db.crear_recorte(self.libro_actual["id"], pagina, x, y, w, h)
        self._refrescar_overlays()
        self._abrir_recorte(recorte_id)
        self.mostrar_estado("Crop created.")

    def _eliminar_recorte(self, recorte_id):
        db.eliminar_recorte(recorte_id)
        # si justo era el recorte abierto en la hoja, cerrarla (ya no existe)
        if self.hoja.recorte_id_actual == recorte_id:
            self.hoja.recorte_id_actual = None
            self.hoja.hide()
        self._refrescar_overlays()
        self.mostrar_estado("Crop deleted.")

    def _redimensionar_recorte(self, recorte_id, x, y, ancho, alto):
        db.actualizar_recorte(recorte_id, x, y, ancho, alto)
        # si estaba abierto en la hoja, la imagen de referencia quedó vieja
        # (correspondía al tamaño anterior); se vuelve a generar
        if self.hoja.recorte_id_actual == recorte_id and self.libro_actual:
            conn = db.get_connection()
            cur = conn.cursor()
            cur.execute("SELECT * FROM recortes WHERE id = ?", (recorte_id,))
            recorte = cur.fetchone()
            conn.close()
            if recorte:
                imagen_ref = self._renderizar_imagen_recorte(recorte)
                self.hoja.canvas.set_imagen_referencia(imagen_ref)

    def _abrir_recorte(self, recorte_id):
        self._guardar_hoja_actual()

        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM recortes WHERE id = ?", (recorte_id,))
        recorte = cur.fetchone()
        conn.close()
        if not recorte:
            return

        # solo hacer scroll si la página del recorte NO está ya visible
        # (si tocaste un recorte que ya se ve en pantalla, no tiene sentido
        # mover el PDF — antes esto causaba un salto molesto al tope de la página)
        if not self.lector.pagina_es_visible(recorte["pagina"]):
            self.lector.ir_a_pagina(recorte["pagina"])

        trazos = db.cargar_trazos(recorte_id)
        imagen_ref = self._renderizar_imagen_recorte(recorte)
        mostrar_default = db.get_config("mostrar_ref_siempre", "0") == "1"

        titulo = recorte["titulo_opcional"] or f"Crop #{recorte['id']} (page {recorte['pagina'] + 1})"
        self.hoja.abrir_recorte(
            recorte_id, titulo, trazos, imagen_ref, mostrar_default,
            color_fondo=recorte["color_fondo"], estilo_fondo=recorte["estilo_fondo"],
        )
        self.hoja.show()

    def _renderizar_imagen_recorte(self, recorte):
        """Genera un QPixmap de la región del recorte para usar como referencia."""
        doc = self.lector.view.doc
        if doc is None:
            return None
        page = doc[recorte["pagina"]]
        rect = fitz.Rect(
            recorte["x"], recorte["y"],
            recorte["x"] + recorte["ancho"], recorte["y"] + recorte["alto"]
        )
        mat = fitz.Matrix(RENDER_ZOOM, RENDER_ZOOM)
        pix = page.get_pixmap(matrix=mat, clip=rect)
        # Copiar el buffer: PyMuPDF libera ``pix`` al salir de esta función.
        # Sin la copia, la referencia puede aparecer corrupta o vacía.
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
        return QPixmap.fromImage(img)

    def _guardar_hoja_actual(self):
        if self.hoja.recorte_id_actual is not None:
            db.guardar_trazos(self.hoja.recorte_id_actual, self.hoja.canvas.obtener_elementos())


class VentanaPrincipal(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MoonReader — Annotated PDF Manager")
        self.setWindowIcon(crear_icono_app())
        self.resize(1400, 900)

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.vista_biblioteca = VistaBiblioteca(self._abrir_libro, self._mostrar_estado)
        self.vista_lector = VistaLectorEdicion(self._volver_biblioteca, self._mostrar_estado)

        self.stack.addWidget(self.vista_biblioteca)
        self.stack.addWidget(self.vista_lector)

        self._crear_menu()
        self.statusBar().showMessage("Ready", 3000)

        # modo oscuro: vive solo en el menú View, arranca reflejando lo
        # último guardado
        modo_oscuro_guardado = db.get_config("modo_oscuro", "0") == "1"
        self.accion_modo_oscuro.setChecked(modo_oscuro_guardado)
        self.accion_modo_oscuro.toggled.connect(self._on_toggle_modo_oscuro)

    def _crear_menu(self):
        menu_archivo = self.menuBar().addMenu("&File")
        accion_importar = QAction("Import PDF…", self)
        accion_importar.setShortcut("Ctrl+O")
        accion_importar.triggered.connect(self.vista_biblioteca._importar)
        menu_archivo.addAction(accion_importar)
        menu_archivo.addSeparator()
        accion_salir = QAction("Exit", self)
        accion_salir.setShortcut("Ctrl+Q")
        accion_salir.triggered.connect(self.close)
        menu_archivo.addAction(accion_salir)

        menu_ver = self.menuBar().addMenu("&View")
        self.accion_modo_oscuro = QAction("Dark mode", self)
        self.accion_modo_oscuro.setCheckable(True)
        menu_ver.addAction(self.accion_modo_oscuro)

        menu_ayuda = self.menuBar().addMenu("&Help")
        accion_acerca = QAction("About MoonReader", self)
        accion_acerca.triggered.connect(self._mostrar_acerca_de)
        menu_ayuda.addAction(accion_acerca)

    def _mostrar_estado(self, mensaje):
        self.statusBar().showMessage(mensaje, 4000)

    def _mostrar_acerca_de(self):
        QMessageBox.about(
            self, "About MoonReader",
            f"<h3>MoonReader</h3>"
            f"<p>Version {VERSION_APP}</p>"
            "<p>A study companion for reading PDFs and working through new "
            "material: mark a region on any page — a problem, a diagram, "
            "a passage — and work it out on a canvas right next to it.</p>"
            "<p>Built with PySide6 and PyMuPDF.</p>",
        )

    def _on_toggle_modo_oscuro(self, activo):
        aplicar_tema(QApplication.instance(), activo)

    def _abrir_libro(self, libro_id):
        # importante: mostrar el widget PRIMERO y recién después navegar a
        # la página guardada — si se navega antes de que el widget tenga su
        # tamaño real (todavía oculto en el stack), el cálculo de "qué
        # página se ve" se hace con una geometría provisoria y puede quedar
        # una página de menos
        self.stack.setCurrentWidget(self.vista_lector)
        exito = self.vista_lector.abrir_libro(libro_id)
        if not exito:
            # el PDF no se pudo abrir (movido/borrado/corrupto): volver a
            # la biblioteca en vez de dejar al usuario en un lector roto
            self.stack.setCurrentWidget(self.vista_biblioteca)
        else:
            self._mostrar_estado(f'Opened "{self.vista_lector.libro_actual["titulo"]}"')

    def _volver_biblioteca(self):
        self.vista_lector._guardar_hoja_actual()
        self.vista_biblioteca.refrescar()
        self.stack.setCurrentWidget(self.vista_biblioteca)

    def closeEvent(self, event):
        self.vista_lector._guardar_hoja_actual()
        super().closeEvent(event)


ESTILO_APP = """
QWidget {
    background-color: #f5f6f8;
    color: #2b2d31;
    font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
}

QMainWindow, QStackedWidget {
    background-color: #f5f6f8;
}

QPushButton {
    background-color: #ffffff;
    border: 1px solid #d5d8dd;
    border-radius: 7px;
    padding: 7px 12px;
}

QPushButton:hover {
    background-color: #eef2fb;
    border-color: #b9c6e6;
}

QPushButton:pressed {
    background-color: #e2e8f7;
}

QPushButton:checked {
    background-color: #4a72e8;
    color: #ffffff;
    border-color: #3a5fd0;
}

QPushButton:checked:hover {
    background-color: #3f63d6;
}

QPushButton[toolbar="true"] {
    min-width: 30px;
    padding: 6px 9px;
}

#libraryTitle {
    font-size: 23px;
    font-weight: 700;
    padding: 12px 8px 6px 8px;
}

#readerTitle, #cropTitle {
    font-size: 15px;
    font-weight: 650;
    padding: 4px 6px;
}

#readerMeta {
    color: #586174;
    font-weight: 600;
    padding: 4px 2px;
}

QGraphicsView#annotationCanvas, QGraphicsView#pdfCanvas {
    background-color: #ffffff;
    border: 1px solid #dce1e9;
    border-radius: 8px;
}

QLabel {
    background: transparent;
}

QListWidget {
    background-color: #ffffff;
    border: 1px solid #e0e2e7;
    border-radius: 6px;
    padding: 4px;
}

QListWidget::item {
    padding: 8px 6px;
    border-radius: 4px;
}

QListWidget::item:hover {
    background-color: #eef2fb;
}

QListWidget::item:selected {
    background-color: #4a72e8;
    color: #ffffff;
}

QSplitter::handle {
    background-color: #dde1e8;
}

QSplitter::handle:hover {
    background-color: #b9c6e6;
}

QSlider::groove:horizontal {
    height: 4px;
    background: #d5d8dd;
    border-radius: 2px;
}

QSlider::handle:horizontal {
    background: #4a72e8;
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
}

QScrollBar:vertical {
    background: #f0f1f4;
    width: 12px;
    border-radius: 6px;
}

QScrollBar::handle:vertical {
    background: #c6cad2;
    border-radius: 6px;
    min-height: 24px;
}

QScrollBar::handle:vertical:hover {
    background: #a8adb8;
}

QScrollBar:horizontal {
    background: #f0f1f4;
    height: 12px;
    border-radius: 6px;
}

QScrollBar::handle:horizontal {
    background: #c6cad2;
    border-radius: 6px;
    min-width: 24px;
}

QSpinBox, QColorDialog, QInputDialog {
    background-color: #ffffff;
    border: 1px solid #d5d8dd;
    border-radius: 4px;
    padding: 2px 4px;
}

QMenu {
    background-color: #ffffff;
    border: 1px solid #d5d8dd;
    border-radius: 6px;
    padding: 4px;
}

QMenu::item {
    padding: 6px 20px;
    border-radius: 4px;
}

QMenu::item:selected {
    background-color: #4a72e8;
    color: #ffffff;
}
"""

ESTILO_APP_OSCURO = """
QWidget {
    background-color: #1e1f22;
    color: #e6e6e6;
    font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
}

QMainWindow, QStackedWidget {
    background-color: #1e1f22;
}

QPushButton {
    background-color: #2b2d31;
    border: 1px solid #45484e;
    border-radius: 7px;
    padding: 7px 12px;
    color: #e6e6e6;
}

QPushButton:hover {
    background-color: #35383e;
    border-color: #5b82ff;
}

QPushButton:pressed {
    background-color: #26282c;
}

QPushButton:checked {
    background-color: #5b82ff;
    color: #ffffff;
    border-color: #7a9aff;
}

QPushButton:checked:hover {
    background-color: #6d8fff;
}

QPushButton[toolbar="true"] {
    min-width: 30px;
    padding: 6px 9px;
}

#libraryTitle {
    font-size: 23px;
    font-weight: 700;
    padding: 12px 8px 6px 8px;
}

#readerTitle, #cropTitle {
    font-size: 15px;
    font-weight: 650;
    padding: 4px 6px;
}

#readerMeta {
    color: #aeb7c7;
    font-weight: 600;
    padding: 4px 2px;
}

QGraphicsView#annotationCanvas, QGraphicsView#pdfCanvas {
    background-color: #202226;
    border: 1px solid #3a3d42;
    border-radius: 8px;
}

QLabel {
    background: transparent;
}

QListWidget {
    background-color: #26282c;
    border: 1px solid #3a3d42;
    border-radius: 6px;
    padding: 4px;
}

QListWidget::item {
    padding: 8px 6px;
    border-radius: 4px;
}

QListWidget::item:hover {
    background-color: #35383e;
}

QListWidget::item:selected {
    background-color: #5b82ff;
    color: #ffffff;
}

QSplitter::handle {
    background-color: #3a3d42;
}

QSplitter::handle:hover {
    background-color: #5b82ff;
}

QSlider::groove:horizontal {
    height: 4px;
    background: #45484e;
    border-radius: 2px;
}

QSlider::handle:horizontal {
    background: #5b82ff;
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
}

QScrollBar:vertical {
    background: #232427;
    width: 12px;
    border-radius: 6px;
}

QScrollBar::handle:vertical {
    background: #45484e;
    border-radius: 6px;
    min-height: 24px;
}

QScrollBar::handle:vertical:hover {
    background: #5a5d63;
}

QScrollBar:horizontal {
    background: #232427;
    height: 12px;
    border-radius: 6px;
}

QScrollBar::handle:horizontal {
    background: #45484e;
    border-radius: 6px;
    min-width: 24px;
}

QSpinBox, QColorDialog, QInputDialog {
    background-color: #2b2d31;
    border: 1px solid #45484e;
    border-radius: 4px;
    padding: 2px 4px;
    color: #e6e6e6;
}

QMenu {
    background-color: #2b2d31;
    border: 1px solid #45484e;
    border-radius: 6px;
    padding: 4px;
}

QMenu::item {
    padding: 6px 20px;
    border-radius: 4px;
}

QMenu::item:selected {
    background-color: #5b82ff;
    color: #ffffff;
}
"""


def aplicar_tema(app, oscuro):
    app.setStyleSheet(ESTILO_APP_OSCURO if oscuro else ESTILO_APP)
    db.set_config("modo_oscuro", "1" if oscuro else "0")


def main():
    db.init_db()
    app = QApplication(sys.argv)
    aplicar_tema(app, db.get_config("modo_oscuro", "0") == "1")
    ventana = VentanaPrincipal()
    ventana.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
