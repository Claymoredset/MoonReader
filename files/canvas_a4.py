"""
Canvas de edición INFINITO (no más hoja A4 de tamaño fijo), con herramientas
inspiradas en Excalidraw:
- Lápiz libre, Rectángulo, Elipse, Flecha, Texto
- Seleccionar (mover elementos existentes) y Goma (borrar elementos enteros)
- Estilo visual "boceto a mano alzada": las formas geométricas se dibujan con
  un pequeño "temblor" fijo (no aleatorio en cada repintado) que imita el
  efecto rough.js de Excalidraw, en vez de líneas perfectamente rectas.
- Deshacer/Rehacer real, basado en un historial de comandos (agregar,
  eliminar, mover), no solo "sacar el último trazo".
- Zoom con Ctrl + rueda del mouse (centrado en el cursor), y paneo
  arrastrando con el botón central del mouse.

Implementación: el canvas es un QGraphicsView con una escena muy grande
(no literalmente infinita, pero con margen de sobra para cualquier uso real),
fondo blanco en TODO el viewport (no solo dentro de una "hoja"), y el
contenido se dibuja directamente en drawForeground() en coordenadas de
escena — el mismo patrón que ya usamos para el visor de PDF.

Modelo de datos: cada elemento es un dict con al menos:
    {"id": int, "tipo": "lapiz"|"rectangulo"|"elipse"|"flecha"|"texto",
     "color": "#rrggbb", "grosor": float, "seed": int, ...geometría...}
Geometría según tipo (todo en coordenadas de escena, sin límite de página):
    lapiz:       "puntos": [[x,y], ...]
    rectangulo:  "x", "y", "w", "h"
    elipse:      "x", "y", "w", "h"
    flecha:      "x1", "y1", "x2", "y2"
    texto:       "x", "y", "texto"
"""
import math
import random

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QColorDialog,
    QSlider, QButtonGroup, QInputDialog, QGraphicsView, QGraphicsScene
)
from PySide6.QtGui import (
    QPainter, QPen, QColor, QShortcut, QKeySequence, QFont, QBrush,
    QPainterPath, QFontDatabase
)
from PySide6.QtCore import Qt, QPointF, QRectF, Signal

# Tamaño de referencia por defecto para la imagen del recorte (ya no hay
# "hoja A4"; esto solo define qué tan grande se ve la imagen de fondo).
ANCHO_REFERENCIA_DEFAULT = 700
ALTO_REFERENCIA_DEFAULT = int(ANCHO_REFERENCIA_DEFAULT * 1.4142)

MARGEN_SELECCION = 8  # tolerancia en px (de escena) para hacer click sobre un elemento

# La escena es muy grande pero finita (esto ya cubre cualquier uso real de
# una hoja de anotaciones; nadie va a panear 25000 unidades en la práctica).
LIMITE_ESCENA = 25000

ZOOM_MIN = 0.1
ZOOM_MAX = 8.0
ZOOM_PASO = 1.15

_FUENTE_BOCETO_CACHE = None


def _fuente_boceto():
    """Elige la mejor fuente 'a mano alzada' disponible en el sistema.
    Comic Sans MS se ve genérico/barato; probamos alternativas más prolijas
    primero. Si ninguna está instalada, cae a una sans-serif normal (todavía
    se ve bien, solo pierde el toque "escrito a mano").
    En Ubuntu, `sudo apt install fonts-comic-neue` instala la primera opción
    de esta lista y mejora bastante el resultado."""
    global _FUENTE_BOCETO_CACHE
    if _FUENTE_BOCETO_CACHE is not None:
        return _FUENTE_BOCETO_CACHE
    candidatas = ["Comic Neue", "Segoe Print", "Bradley Hand", "Short Stack",
                  "Patrick Hand", "Kalam", "Comic Sans MS"]
    disponibles = set(QFontDatabase.families())
    _FUENTE_BOCETO_CACHE = next((f for f in candidatas if f in disponibles), "sans-serif")
    return _FUENTE_BOCETO_CACHE


# ---------- Utilidades geométricas (sin cambios respecto a la versión anterior) ----------

def _distancia_punto_segmento(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    largo2 = dx * dx + dy * dy
    if largo2 == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / largo2))
    proy_x, proy_y = x1 + t * dx, y1 + t * dy
    return math.hypot(px - proy_x, py - proy_y)


def _bbox_elemento(el):
    """Devuelve (x0, y0, x1, y1) del cuadro delimitador de un elemento."""
    tipo = el["tipo"]
    if tipo == "lapiz":
        xs = [p[0] for p in el["puntos"]]
        ys = [p[1] for p in el["puntos"]]
        return min(xs), min(ys), max(xs), max(ys)
    if tipo in ("rectangulo", "elipse"):
        return el["x"], el["y"], el["x"] + el["w"], el["y"] + el["h"]
    if tipo == "flecha":
        return (min(el["x1"], el["x2"]), min(el["y1"], el["y2"]),
                max(el["x1"], el["x2"]), max(el["y1"], el["y2"]))
    if tipo == "texto":
        ancho_aprox = max(20, len(el["texto"]) * el.get("grosor", 3) * 3.2)
        alto_aprox = el.get("grosor", 3) * 6 + 10
        return el["x"], el["y"] - alto_aprox, el["x"] + ancho_aprox, el["y"]
    return 0, 0, 0, 0


def _elemento_contiene_punto(el, px, py, margen=MARGEN_SELECCION):
    tipo = el["tipo"]
    if tipo == "lapiz":
        pts = el["puntos"]
        for i in range(len(pts) - 1):
            if _distancia_punto_segmento(px, py, pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]) <= margen:
                return True
        return False
    if tipo == "flecha":
        return _distancia_punto_segmento(px, py, el["x1"], el["y1"], el["x2"], el["y2"]) <= margen
    # rectángulo, elipse, texto: usamos el bounding box expandido (simple y predecible)
    x0, y0, x1, y1 = _bbox_elemento(el)
    return (x0 - margen) <= px <= (x1 + margen) and (y0 - margen) <= py <= (y1 + margen)


def _mover_elemento(el, dx, dy):
    tipo = el["tipo"]
    if tipo == "lapiz":
        el["puntos"] = [(p[0] + dx, p[1] + dy) for p in el["puntos"]]
    elif tipo in ("rectangulo", "elipse"):
        el["x"] += dx
        el["y"] += dy
    elif tipo == "flecha":
        el["x1"] += dx
        el["y1"] += dy
        el["x2"] += dx
        el["y2"] += dy
    elif tipo == "texto":
        el["x"] += dx
        el["y"] += dy


class CanvasDibujo(QGraphicsView):
    """Lienzo infinito: pannable con el botón central del mouse, zoomable con
    Ctrl + rueda. El contenido (elementos + referencia) se dibuja en
    drawForeground(), en coordenadas de escena, para que todo lo demás
    (hit-test, mover, deshacer/rehacer) siga funcionando igual que antes."""

    HERRAMIENTAS_DIBUJO = ("lapiz", "rectangulo", "elipse", "flecha", "texto")

    zoom_cambio = Signal(float)  # emite el nuevo factor de zoom (1.0 = 100%)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene_ = QGraphicsScene(self)
        self.scene_.setSceneRect(QRectF(-LIMITE_ESCENA, -LIMITE_ESCENA,
                                         2 * LIMITE_ESCENA, 2 * LIMITE_ESCENA))
        self.setScene(self.scene_)

        # color y estilo de fondo (configurables desde HojaEdicionWidget,
        # persistidos en la config global); se dibujan en drawBackground(),
        # cubriendo TODO el viewport, no solo dentro de una hoja
        self.color_fondo = QColor(255, 255, 255)
        self.estilo_fondo = "liso"  # "liso" | "cuadriculado"
        self.paso_cuadricula = 40

        self.setDragMode(QGraphicsView.NoDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setRenderHint(QPainter.Antialiasing)
        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.setFocusPolicy(Qt.StrongFocus)

        self.elementos = []       # lista de dicts (ver encabezado del archivo)
        self._siguiente_id = 1
        self.historial = []       # comandos aplicados: {"tipo": "agregar"|"eliminar"|"mover", ...}
        self.rehacer_historial = []

        self.herramienta = "lapiz"
        self.color_actual = QColor(0, 0, 0)
        self.grosor_actual = 2.5

        # estado de dibujo en progreso
        self._elemento_en_progreso = None
        self._origen_forma = None

        # estado de selección / arrastre
        self.id_seleccionado = None
        self._arrastrando = False
        self._punto_arrastre_anterior = None
        self._acumulado_dx = 0.0
        self._acumulado_dy = 0.0

        # estado de paneo (botón central del mouse)
        self._paneando = False
        self._pan_ultimo_pos = None

        self.imagen_referencia = None
        self.mostrar_referencia = False

        self._zoom_actual = 1.0

        self._actualizar_cursor()
        # arrancamos centrados en el origen, donde se ancla el contenido nuevo
        self.centerOn(ANCHO_REFERENCIA_DEFAULT / 2, ALTO_REFERENCIA_DEFAULT / 2)

    # ---------- Referencia ----------

    def set_imagen_referencia(self, qpixmap):
        if qpixmap is not None:
            self.imagen_referencia = qpixmap.scaled(
                ANCHO_REFERENCIA_DEFAULT, ALTO_REFERENCIA_DEFAULT,
                Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        else:
            self.imagen_referencia = None
        self.viewport().update()

    def set_mostrar_referencia(self, mostrar):
        self.mostrar_referencia = mostrar
        self.viewport().update()

    # ---------- Herramienta / estilo activos ----------

    def set_herramienta(self, herramienta):
        self.herramienta = herramienta
        self.id_seleccionado = None
        self._actualizar_cursor()
        self.viewport().update()

    def _actualizar_cursor(self):
        if self._paneando:
            self.setCursor(Qt.ClosedHandCursor)
        elif self.herramienta == "seleccionar":
            self.setCursor(Qt.ArrowCursor)
        elif self.herramienta == "goma":
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.setCursor(Qt.CrossCursor)

    def set_color(self, qcolor):
        self.color_actual = qcolor

    def set_grosor(self, valor):
        self.grosor_actual = float(valor)

    # ---------- Zoom (Ctrl + rueda) ----------

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            factor = ZOOM_PASO if event.angleDelta().y() > 0 else (1 / ZOOM_PASO)
            nuevo_zoom = self._zoom_actual * factor
            if ZOOM_MIN <= nuevo_zoom <= ZOOM_MAX:
                self.scale(factor, factor)
                self._zoom_actual = nuevo_zoom
                self.zoom_cambio.emit(self._zoom_actual)
            event.accept()
        else:
            super().wheelEvent(event)

    def restablecer_zoom(self):
        self.resetTransform()
        self._zoom_actual = 1.0
        self.zoom_cambio.emit(1.0)

    def zoom_actual(self):
        return self._zoom_actual

    # ---------- Carga / guardado ----------

    def cargar_elementos(self, elementos):
        """Acepta tanto el formato nuevo (con 'tipo') como el viejo (solo 'puntos'),
        para no perder anotaciones ya guardadas con versiones anteriores."""
        cargados = []
        max_id = 0
        for e in elementos:
            el = dict(e)
            el.setdefault("tipo", "lapiz")
            el.setdefault("color", "#000000")
            el.setdefault("grosor", 2.5)
            el.setdefault("seed", random.randint(0, 9999))
            el.setdefault("id", self._siguiente_id)
            if el["tipo"] == "lapiz" and "puntos" in el:
                el["puntos"] = [tuple(p) for p in el["puntos"]]
            cargados.append(el)
            max_id = max(max_id, el["id"])
        self.elementos = cargados
        self._siguiente_id = max_id + 1
        self.historial = []
        self.rehacer_historial = []
        self.id_seleccionado = None

        # al abrir un recorte, centrar la vista sobre el contenido cargado
        # (o el origen si está vacío), en vez de dejar al usuario perdido
        # en donde haya quedado paneado/zoomeado la vez anterior
        self.restablecer_zoom()
        if self.elementos:
            x0s, y0s, x1s, y1s = zip(*(_bbox_elemento(e) for e in self.elementos))
            self.centerOn((min(x0s) + max(x1s)) / 2, (min(y0s) + max(y1s)) / 2)
        else:
            self.centerOn(ANCHO_REFERENCIA_DEFAULT / 2, ALTO_REFERENCIA_DEFAULT / 2)

        self.viewport().update()

    def obtener_elementos(self):
        return self.elementos

    def limpiar(self):
        self.elementos = []
        self.historial = []
        self.rehacer_historial = []
        self.id_seleccionado = None
        self.viewport().update()

    # ---------- Historial (undo/redo genérico) ----------

    def _registrar_comando(self, comando):
        self.historial.append(comando)
        self.rehacer_historial = []

    def deshacer(self):
        if not self.historial:
            return
        cmd = self.historial.pop()
        if cmd["tipo"] == "agregar":
            self.elementos = [e for e in self.elementos if e["id"] != cmd["elemento"]["id"]]
        elif cmd["tipo"] == "eliminar":
            idx = min(cmd["indice"], len(self.elementos))
            self.elementos.insert(idx, cmd["elemento"])
        elif cmd["tipo"] == "mover":
            el = self._buscar_por_id(cmd["id"])
            if el is not None:
                _mover_elemento(el, -cmd["dx"], -cmd["dy"])
        self.rehacer_historial.append(cmd)
        self.viewport().update()

    def rehacer(self):
        if not self.rehacer_historial:
            return
        cmd = self.rehacer_historial.pop()
        if cmd["tipo"] == "agregar":
            self.elementos.append(cmd["elemento"])
        elif cmd["tipo"] == "eliminar":
            self.elementos = [e for e in self.elementos if e["id"] != cmd["elemento"]["id"]]
        elif cmd["tipo"] == "mover":
            el = self._buscar_por_id(cmd["id"])
            if el is not None:
                _mover_elemento(el, cmd["dx"], cmd["dy"])
        self.historial.append(cmd)
        self.viewport().update()

    def _buscar_por_id(self, id_):
        for e in self.elementos:
            if e["id"] == id_:
                return e
        return None

    def _elemento_en_punto(self, px, py):
        """Devuelve el elemento más "de arriba" (último dibujado) bajo el punto, o None."""
        for el in reversed(self.elementos):
            if _elemento_contiene_punto(el, px, py):
                return el
        return None

    # ---------- Eventos de mouse ----------
    # Nota: todas las coordenadas se convierten con self.mapToScene(...) para
    # que la lógica de dibujo/selección funcione en coordenadas de escena
    # (independiente del zoom y del paneo actuales).

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._paneando = True
            self._pan_ultimo_pos = event.pos()
            self._actualizar_cursor()
            event.accept()
            return

        if event.button() != Qt.LeftButton:
            return
        self.setFocus()
        p = self.mapToScene(event.position().toPoint())

        if self.herramienta == "seleccionar":
            el = self._elemento_en_punto(p.x(), p.y())
            self.id_seleccionado = el["id"] if el else None
            self._arrastrando = el is not None
            self._punto_arrastre_anterior = (p.x(), p.y())
            self._acumulado_dx = 0.0
            self._acumulado_dy = 0.0
            self.viewport().update()
            return

        if self.herramienta == "goma":
            el = self._elemento_en_punto(p.x(), p.y())
            if el is not None:
                self._borrar_elemento(el)
            return

        if self.herramienta == "texto":
            texto, ok = QInputDialog.getText(self, "Add text", "Text:")
            if ok and texto.strip():
                nuevo = {
                    "id": self._siguiente_id, "tipo": "texto",
                    "x": p.x(), "y": p.y(), "texto": texto.strip(),
                    "color": self.color_actual.name(), "grosor": self.grosor_actual,
                    "seed": random.randint(0, 9999),
                }
                self._siguiente_id += 1
                self.elementos.append(nuevo)
                self._registrar_comando({"tipo": "agregar", "elemento": nuevo})
                self.viewport().update()
            return

        # herramientas de dibujo: lapiz, rectangulo, elipse, flecha
        base = {
            "id": self._siguiente_id, "tipo": self.herramienta,
            "color": self.color_actual.name(), "grosor": self.grosor_actual,
            "seed": random.randint(0, 9999),
        }
        self._siguiente_id += 1
        if self.herramienta == "lapiz":
            base["puntos"] = [(p.x(), p.y())]
        elif self.herramienta in ("rectangulo", "elipse"):
            base.update({"x": p.x(), "y": p.y(), "w": 0.0, "h": 0.0})
            self._origen_forma = (p.x(), p.y())
        elif self.herramienta == "flecha":
            base.update({"x1": p.x(), "y1": p.y(), "x2": p.x(), "y2": p.y()})
        self._elemento_en_progreso = base

    def mouseMoveEvent(self, event):
        if self._paneando:
            delta = event.pos() - self._pan_ultimo_pos
            self._pan_ultimo_pos = event.pos()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return

        p = self.mapToScene(event.position().toPoint())

        if self.herramienta == "seleccionar" and self._arrastrando and self.id_seleccionado is not None:
            el = self._buscar_por_id(self.id_seleccionado)
            if el is not None and (event.buttons() & Qt.LeftButton):
                dx = p.x() - self._punto_arrastre_anterior[0]
                dy = p.y() - self._punto_arrastre_anterior[1]
                _mover_elemento(el, dx, dy)
                self._acumulado_dx += dx
                self._acumulado_dy += dy
                self._punto_arrastre_anterior = (p.x(), p.y())
                self.viewport().update()
            return

        if self.herramienta == "goma" and (event.buttons() & Qt.LeftButton):
            el = self._elemento_en_punto(p.x(), p.y())
            if el is not None:
                self._borrar_elemento(el)
            return

        if self._elemento_en_progreso is None or not (event.buttons() & Qt.LeftButton):
            return

        if self.herramienta == "lapiz":
            self._elemento_en_progreso["puntos"].append((p.x(), p.y()))
        elif self.herramienta in ("rectangulo", "elipse"):
            ox, oy = self._origen_forma
            self._elemento_en_progreso["x"] = min(ox, p.x())
            self._elemento_en_progreso["y"] = min(oy, p.y())
            self._elemento_en_progreso["w"] = abs(p.x() - ox)
            self._elemento_en_progreso["h"] = abs(p.y() - oy)
        elif self.herramienta == "flecha":
            self._elemento_en_progreso["x2"] = p.x()
            self._elemento_en_progreso["y2"] = p.y()
        self.viewport().update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._paneando = False
            self._actualizar_cursor()
            event.accept()
            return

        if event.button() != Qt.LeftButton:
            return

        if self.herramienta == "seleccionar":
            if self._arrastrando and (self._acumulado_dx != 0 or self._acumulado_dy != 0):
                self._registrar_comando({
                    "tipo": "mover", "id": self.id_seleccionado,
                    "dx": self._acumulado_dx, "dy": self._acumulado_dy,
                })
            self._arrastrando = False
            return

        if self._elemento_en_progreso is None:
            return

        el = self._elemento_en_progreso
        self._elemento_en_progreso = None
        valido = True
        if el["tipo"] == "lapiz":
            valido = len(el["puntos"]) > 1
        elif el["tipo"] in ("rectangulo", "elipse"):
            valido = el["w"] > 3 and el["h"] > 3
        elif el["tipo"] == "flecha":
            valido = math.hypot(el["x2"] - el["x1"], el["y2"] - el["y1"]) > 3

        if valido:
            self.elementos.append(el)
            self._registrar_comando({"tipo": "agregar", "elemento": el})
        self.viewport().update()

    def _borrar_elemento(self, el):
        idx = self.elementos.index(el)
        self.elementos.pop(idx)
        self._registrar_comando({"tipo": "eliminar", "elemento": el, "indice": idx})
        if self.id_seleccionado == el["id"]:
            self.id_seleccionado = None
        self.viewport().update()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.id_seleccionado is not None:
            el = self._buscar_por_id(self.id_seleccionado)
            if el is not None:
                self._borrar_elemento(el)
            return
        super().keyPressEvent(event)

    # ---------- Color y estilo de fondo ----------

    def set_color_fondo(self, qcolor):
        self.color_fondo = qcolor
        self.viewport().update()

    def set_estilo_fondo(self, estilo):
        self.estilo_fondo = estilo
        self.viewport().update()

    def drawBackground(self, painter, rect):
        painter.fillRect(rect, self.color_fondo)
        if self.estilo_fondo != "cuadriculado":
            return

        # color de la cuadrícula según qué tan claro/oscuro es el fondo,
        # para que las líneas siempre se noten sin importar el color elegido
        luminancia = (0.299 * self.color_fondo.red()
                      + 0.587 * self.color_fondo.green()
                      + 0.114 * self.color_fondo.blue())
        color_linea = QColor(255, 255, 255, 45) if luminancia < 128 else QColor(0, 0, 0, 35)
        painter.setPen(QPen(color_linea, 1))

        paso = self.paso_cuadricula
        x = math.floor(rect.left() / paso) * paso
        while x < rect.right():
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            x += paso
        y = math.floor(rect.top() / paso) * paso
        while y < rect.bottom():
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            y += paso

    # ---------- Dibujo ----------
    # drawForeground recibe el painter ya en coordenadas de escena (no hace
    # falta transformar nada a mano); el fondo (color/cuadrícula) va en drawBackground.

    def drawForeground(self, painter, rect):
        painter.setRenderHint(QPainter.Antialiasing)

        if self.mostrar_referencia and self.imagen_referencia is not None:
            painter.setOpacity(0.25)
            painter.drawPixmap(0, 0, self.imagen_referencia)
            painter.setOpacity(1.0)

        for el in self.elementos:
            self._dibujar_elemento(painter, el)
            if el["id"] == self.id_seleccionado:
                self._dibujar_marco_seleccion(painter, el)

        if self._elemento_en_progreso is not None:
            self._dibujar_elemento(painter, self._elemento_en_progreso)

    def _dibujar_marco_seleccion(self, painter, el):
        x0, y0, x1, y1 = _bbox_elemento(el)
        pen = QPen(QColor(0, 120, 255, 200), 1, Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(int(x0) - 4, int(y0) - 4, int(x1 - x0) + 8, int(y1 - y0) + 8)

    def _dibujar_elemento(self, painter, el):
        tipo = el["tipo"]
        if tipo == "lapiz":
            self._dibujar_lapiz(painter, el)
        elif tipo == "rectangulo":
            self._dibujar_rectangulo(painter, el)
        elif tipo == "elipse":
            self._dibujar_elipse(painter, el)
        elif tipo == "flecha":
            self._dibujar_flecha(painter, el)
        elif tipo == "texto":
            self._dibujar_texto(painter, el)

    def _dibujar_lapiz(self, painter, el):
        """Traza suavizado con curvas (en vez de segmentos rectos entre cada
        punto del mouse): usa el punto medio entre puntos consecutivos como
        destino de cada curva, y el punto real como control — la técnica
        clásica de "suavizado rápido" que evita el aspecto poligonal."""
        pts = el["puntos"]
        if len(pts) < 2:
            return
        pen = QPen(QColor(el["color"]), el["grosor"], Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        path = QPainterPath()
        path.moveTo(QPointF(*pts[0]))
        if len(pts) == 2:
            path.lineTo(QPointF(*pts[1]))
        else:
            for i in range(1, len(pts) - 1):
                actual = QPointF(*pts[i])
                siguiente = QPointF(*pts[i + 1])
                medio = QPointF((actual.x() + siguiente.x()) / 2, (actual.y() + siguiente.y()) / 2)
                path.quadTo(actual, medio)
            path.lineTo(QPointF(*pts[-1]))
        painter.drawPath(path)

    def _jitter(self, seed, indice, magnitud):
        rnd = random.Random(seed * 97 + indice)
        return rnd.uniform(-magnitud, magnitud)

    def _linea_boceto(self, painter, x1, y1, x2, y2, seed, indice_base, pasadas=2):
        """Dibuja una línea con aspecto de boceto: una sola curva continua
        con una leve "panza" en el medio (no temblor en cada punto), y los
        extremos ligeramente pasados de largo o cortos — como cuando la
        mano no levanta el lápiz justo en la esquina. Esto es lo que da el
        aspecto prolijo de Excalidraw en vez de un zigzag nervioso."""
        painter.setBrush(Qt.NoBrush)  # por si el método anterior dejó un brush sólido activo
        largo = math.hypot(x2 - x1, y2 - y1) or 1.0
        ux, uy = (x2 - x1) / largo, (y2 - y1) / largo  # dirección unitaria
        perp_x, perp_y = -uy, ux  # perpendicular, para la "panza"

        for pasada in range(pasadas):
            base = indice_base + pasada * 10
            sobra_inicio = self._jitter(seed, base, 2.2)
            sobra_fin = self._jitter(seed, base + 1, 2.2)
            panza = self._jitter(seed, base + 2, 1.3 if pasada == 0 else 2.0)

            ax, ay = x1 - ux * sobra_inicio, y1 - uy * sobra_inicio
            bx, by = x2 + ux * sobra_fin, y2 + uy * sobra_fin
            mx = (ax + bx) / 2 + perp_x * panza
            my = (ay + by) / 2 + perp_y * panza

            path = QPainterPath()
            path.moveTo(QPointF(ax, ay))
            path.quadTo(QPointF(mx, my), QPointF(bx, by))
            painter.drawPath(path)

    def _dibujar_rectangulo(self, painter, el):
        x, y, w, h = el["x"], el["y"], el["w"], el["h"]
        seed = el.get("seed", 0)
        pen = QPen(QColor(el["color"]), el["grosor"], Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        aristas = [
            (x, y, x + w, y),          # arriba
            (x + w, y, x + w, y + h),  # derecha
            (x + w, y + h, x, y + h),  # abajo
            (x, y + h, x, y),          # izquierda
        ]
        for i, (ax, ay, bx, by) in enumerate(aristas):
            self._linea_boceto(painter, ax, ay, bx, by, seed, i * 100)

    def _dibujar_elipse(self, painter, el):
        x, y, w, h = el["x"], el["y"], el["w"], el["h"]
        seed = el.get("seed", 0)
        pen = QPen(QColor(el["color"]), el["grosor"], Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        for pasada in range(2):
            dx = self._jitter(seed, pasada, 1.6)
            dy = self._jitter(seed, pasada + 10, 1.6)
            dw = self._jitter(seed, pasada + 20, 1.6)
            dh = self._jitter(seed, pasada + 30, 1.6)
            painter.drawEllipse(QPointF(x + w / 2 + dx, y + h / 2 + dy), abs(w / 2 + dw), abs(h / 2 + dh))

    def _dibujar_flecha(self, painter, el):
        x1, y1, x2, y2 = el["x1"], el["y1"], el["x2"], el["y2"]
        seed = el.get("seed", 0)
        pen = QPen(QColor(el["color"]), el["grosor"], Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        painter.setPen(pen)

        self._linea_boceto(painter, x1, y1, x2, y2, seed, 0)

        # punta de flecha (líneas cortas, casi no se nota el temblor ahí)
        angulo = math.atan2(y2 - y1, x2 - x1)
        largo_punta = 12 + el["grosor"]
        for signo in (1, -1):
            ang = angulo + signo * math.radians(28)
            px = x2 - largo_punta * math.cos(ang)
            py = y2 - largo_punta * math.sin(ang)
            self._linea_boceto(painter, x2, y2, px, py, seed, 200 if signo == 1 else 300, pasadas=1)

    def _dibujar_texto(self, painter, el):
        pen = QPen(QColor(el["color"]))
        painter.setPen(pen)
        tam_fuente = 10 + int(el["grosor"] * 2)
        fuente = QFont(_fuente_boceto(), tam_fuente)
        painter.setFont(fuente)
        painter.drawText(QPointF(el["x"], el["y"]), el["texto"])


class HojaEdicionWidget(QWidget):
    """Panel derecho completo: barra de herramientas + lienzo infinito.

    El canvas ya no necesita ir dentro de un QScrollArea (como cuando era
    una hoja A4 de tamaño fijo): al ser un QGraphicsView, maneja su propio
    scroll/paneo internamente.
    """

    GROSOR_MIN = 1
    GROSOR_MAX = 20

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # --- barra superior: título + referencia + limpiar ---
        barra_superior = QHBoxLayout()
        self.label_recorte = QLabel("No crop open")
        self.btn_referencia = QPushButton("👁 Show reference")
        self.btn_referencia.setCheckable(True)
        self.btn_limpiar = QPushButton("🗑 Clear sheet")

        barra_superior.addWidget(self.label_recorte)
        barra_superior.addStretch()
        barra_superior.addWidget(self.btn_referencia)
        barra_superior.addWidget(self.btn_limpiar)
        layout.addLayout(barra_superior)

        # --- barra de herramientas: selección + formas ---
        barra_herramientas = QHBoxLayout()
        self.grupo_herramientas = QButtonGroup(self)
        self.grupo_herramientas.setExclusive(True)

        def _crear_btn_herramienta(texto, nombre):
            btn = QPushButton(texto)
            btn.setCheckable(True)
            btn.setProperty("herramienta", nombre)
            self.grupo_herramientas.addButton(btn)
            barra_herramientas.addWidget(btn)
            return btn

        self.btn_seleccionar = _crear_btn_herramienta("↖ Select", "seleccionar")
        self.btn_lapiz = _crear_btn_herramienta("✏ Pen", "lapiz")
        self.btn_rectangulo = _crear_btn_herramienta("▭ Rectangle", "rectangulo")
        self.btn_elipse = _crear_btn_herramienta("◯ Ellipse", "elipse")
        self.btn_flecha = _crear_btn_herramienta("↗ Arrow", "flecha")
        self.btn_texto = _crear_btn_herramienta("🔤 Text", "texto")
        self.btn_goma = _crear_btn_herramienta("🧹 Eraser", "goma")
        self.btn_lapiz.setChecked(True)

        barra_herramientas.addStretch()
        layout.addLayout(barra_herramientas)

        # --- barra secundaria: color, grosor, deshacer/rehacer, zoom ---
        barra_estilo = QHBoxLayout()
        self.btn_color = QPushButton("🎨 Color")
        self.btn_deshacer = QPushButton("↩ Undo")
        self.btn_rehacer = QPushButton("↪ Redo")

        self.label_grosor = QLabel("Thickness: 3")
        self.slider_grosor = QSlider(Qt.Horizontal)
        self.slider_grosor.setMinimum(self.GROSOR_MIN)
        self.slider_grosor.setMaximum(self.GROSOR_MAX)
        self.slider_grosor.setValue(3)
        self.slider_grosor.setFixedWidth(140)

        self.label_zoom = QLabel("Zoom: 100%")
        self.btn_reset_zoom = QPushButton("🔍 100%")
        self.btn_reset_zoom.setToolTip("Reset zoom (also: Ctrl + mouse wheel to zoom in/out)")

        barra_estilo.addWidget(self.btn_color)
        barra_estilo.addSpacing(12)
        barra_estilo.addWidget(self.label_grosor)
        barra_estilo.addWidget(self.slider_grosor)
        barra_estilo.addSpacing(12)
        barra_estilo.addWidget(self.btn_deshacer)
        barra_estilo.addWidget(self.btn_rehacer)
        barra_estilo.addSpacing(12)
        barra_estilo.addWidget(self.label_zoom)
        barra_estilo.addWidget(self.btn_reset_zoom)
        barra_estilo.addStretch()
        layout.addLayout(barra_estilo)

        # --- barra de fondo del lienzo: color + cuadriculado (se recuerda) ---
        barra_fondo = QHBoxLayout()
        self.btn_color_fondo = QPushButton("🖼 Background")
        self.btn_color_fondo.setToolTip("Canvas background color")
        self.btn_cuadriculado = QPushButton("▦ Grid")
        self.btn_cuadriculado.setCheckable(True)
        barra_fondo.addWidget(self.btn_color_fondo)
        barra_fondo.addWidget(self.btn_cuadriculado)
        barra_fondo.addStretch()
        layout.addLayout(barra_fondo)

        # --- lienzo infinito (ya no necesita QScrollArea) ---
        self.canvas = CanvasDibujo()
        layout.addWidget(self.canvas, stretch=1)

        ayuda = QLabel("Ctrl + mouse wheel: zoom  ·  middle mouse button: pan")
        ayuda.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(ayuda)

        # --- conexiones ---
        self.btn_referencia.toggled.connect(self.canvas.set_mostrar_referencia)
        self.btn_limpiar.clicked.connect(self.canvas.limpiar)
        self.btn_color.clicked.connect(self._elegir_color)
        self.grupo_herramientas.buttonClicked.connect(self._cambiar_herramienta)
        self.slider_grosor.valueChanged.connect(self._cambiar_grosor)
        self.btn_deshacer.clicked.connect(self.canvas.deshacer)
        self.btn_rehacer.clicked.connect(self.canvas.rehacer)
        self.btn_reset_zoom.clicked.connect(self.canvas.restablecer_zoom)
        self.canvas.zoom_cambio.connect(self._actualizar_label_zoom)
        self.btn_color_fondo.clicked.connect(self._elegir_color_fondo)
        self.btn_cuadriculado.toggled.connect(self._toggle_cuadriculado)

        # atajos de teclado: funcionan mientras el foco esté en esta hoja o el canvas
        QShortcut(QKeySequence.Undo, self, activated=self.canvas.deshacer,
                  context=Qt.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence.Redo, self, activated=self.canvas.rehacer,
                  context=Qt.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence("Ctrl+Shift+Z"), self, activated=self.canvas.rehacer,
                  context=Qt.WidgetWithChildrenShortcut)

        self._color_lapiz = QColor(0, 0, 0)
        self._cambiar_grosor(self.slider_grosor.value())
        self.canvas.set_herramienta("lapiz")

        # el fondo (color + cuadriculado) es propio de cada recorte, no
        # global — acá arrancamos en blanco/liso hasta que abrir_recorte()
        # aplique el fondo real de lo que se esté abriendo
        self.canvas.set_color_fondo(QColor("#ffffff"))
        self.canvas.set_estilo_fondo("liso")
        self.btn_cuadriculado.setChecked(False)

        self.recorte_id_actual = None

    # ---------- Fondo del lienzo (color + cuadriculado) ----------
    # Cada recorte guarda su propio fondo. Cuando el usuario lo cambia acá,
    # se actualizan DOS cosas: el fondo de ESTE recorte en particular, y el
    # "default" que usarán los recortes NUEVOS de ahora en más — así, un
    # recorte viejo que ya era blanco nunca cambia solo porque configuraste
    # otra cosa después.

    def _elegir_color_fondo(self):
        import db
        color = QColorDialog.getColor(self.canvas.color_fondo, self, "Canvas background color")
        if color.isValid():
            self.canvas.set_color_fondo(color)
            db.set_config("canvas_color_fondo", color.name())  # default para recortes nuevos
            if self.recorte_id_actual is not None:
                db.actualizar_fondo_recorte(self.recorte_id_actual, color.name(), self.canvas.estilo_fondo)

    def _toggle_cuadriculado(self, activo):
        import db
        estilo = "cuadriculado" if activo else "liso"
        self.canvas.set_estilo_fondo(estilo)
        db.set_config("canvas_estilo_fondo", estilo)  # default para recortes nuevos
        if self.recorte_id_actual is not None:
            db.actualizar_fondo_recorte(self.recorte_id_actual, self.canvas.color_fondo.name(), estilo)

    # ---------- Herramientas ----------

    def _elegir_color(self):
        color = QColorDialog.getColor(self._color_lapiz, self, "Choose color")
        if color.isValid():
            self._color_lapiz = color
            self.canvas.set_color(self._color_lapiz)

    def _cambiar_herramienta(self, boton):
        nombre = boton.property("herramienta")
        self.canvas.set_herramienta(nombre)

    def _cambiar_grosor(self, valor):
        self.canvas.set_grosor(valor)
        self.label_grosor.setText(f"Thickness: {valor}")

    def _actualizar_label_zoom(self, zoom):
        self.label_zoom.setText(f"Zoom: {int(round(zoom * 100))}%")

    # ---------- Apertura de recorte ----------

    def abrir_recorte(self, recorte_id, titulo, elementos, imagen_referencia=None,
                       mostrar_ref_default=False, color_fondo="#ffffff", estilo_fondo="liso"):
        self.recorte_id_actual = recorte_id
        self.label_recorte.setText(titulo)
        self.canvas.cargar_elementos(elementos)
        self.canvas.set_imagen_referencia(imagen_referencia)
        self.btn_referencia.setChecked(mostrar_ref_default)
        self.canvas.set_mostrar_referencia(mostrar_ref_default)
        self.btn_lapiz.setChecked(True)
        self.canvas.set_herramienta("lapiz")
        self._actualizar_label_zoom(self.canvas.zoom_actual())

        # aplicar el fondo propio de ESTE recorte (no un default global)
        self.canvas.set_color_fondo(QColor(color_fondo or "#ffffff"))
        self.canvas.set_estilo_fondo(estilo_fondo or "liso")
        self.btn_cuadriculado.setChecked((estilo_fondo or "liso") == "cuadriculado")
