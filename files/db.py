"""
Capa de persistencia con SQLite.
Maneja libros, recortes (crops) y trazos (anotaciones vectoriales).
"""
import sqlite3
import json
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "biblioteca.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS libros (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titulo TEXT NOT NULL,
            autor TEXT,
            ruta_archivo TEXT NOT NULL UNIQUE,
            fecha_agregado TEXT NOT NULL,
            tags TEXT DEFAULT ''
        )
    """)

    # migración: si la DB es de una versión anterior sin "ultima_pagina",
    # se agrega ahora sin borrar nada de lo que ya tenías guardado
    cur.execute("PRAGMA table_info(libros)")
    columnas_libros = {fila[1] for fila in cur.fetchall()}
    if "ultima_pagina" not in columnas_libros:
        cur.execute("ALTER TABLE libros ADD COLUMN ultima_pagina INTEGER DEFAULT 0")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS recortes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            libro_id INTEGER NOT NULL,
            pagina INTEGER NOT NULL,
            x REAL NOT NULL,
            y REAL NOT NULL,
            ancho REAL NOT NULL,
            alto REAL NOT NULL,
            titulo_opcional TEXT DEFAULT '',
            fecha_creado TEXT NOT NULL,
            FOREIGN KEY (libro_id) REFERENCES libros(id) ON DELETE CASCADE
        )
    """)

    # migración: el fondo (color + cuadriculado) ahora es POR RECORTE, no
    # global. Los recortes que ya existían en la DB (de antes de esta
    # función) quedan con el valor por defecto de estas columnas —
    # blanco/liso, que es justamente el fondo que ya tenían — así que no
    # cambian de aspecto. Solo los recortes NUEVOS usan el fondo que
    # tengas configurado como preferido en ese momento (ver crear_recorte).
    cur.execute("PRAGMA table_info(recortes)")
    columnas_recortes = {fila[1] for fila in cur.fetchall()}
    if "color_fondo" not in columnas_recortes:
        cur.execute("ALTER TABLE recortes ADD COLUMN color_fondo TEXT DEFAULT '#ffffff'")
    if "estilo_fondo" not in columnas_recortes:
        cur.execute("ALTER TABLE recortes ADD COLUMN estilo_fondo TEXT DEFAULT 'liso'")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS trazos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorte_id INTEGER NOT NULL,
            datos_json TEXT NOT NULL,
            color TEXT DEFAULT '#000000',
            grosor REAL DEFAULT 2.0,
            FOREIGN KEY (recorte_id) REFERENCES recortes(id) ON DELETE CASCADE
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS config (
            clave TEXT PRIMARY KEY,
            valor TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS marcadores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            libro_id INTEGER NOT NULL,
            pagina INTEGER NOT NULL,
            titulo TEXT NOT NULL,
            fecha_creado TEXT NOT NULL,
            FOREIGN KEY (libro_id) REFERENCES libros(id) ON DELETE CASCADE
        )
    """)

    conn.commit()
    conn.close()


# ---------- Libros ----------

def agregar_libro(titulo, autor, ruta_archivo, tags=""):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO libros (titulo, autor, ruta_archivo, fecha_agregado, tags) VALUES (?, ?, ?, ?, ?)",
        (titulo, autor, ruta_archivo, datetime.now().isoformat(), tags),
    )
    conn.commit()
    libro_id = cur.lastrowid
    conn.close()
    return libro_id


def listar_libros():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM libros ORDER BY fecha_agregado DESC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def obtener_libro(libro_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM libros WHERE id = ?", (libro_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def actualizar_ultima_pagina(libro_id, pagina):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE libros SET ultima_pagina = ? WHERE id = ?", (pagina, libro_id))
    conn.commit()
    conn.close()


def eliminar_libro(libro_id):
    """Elimina el libro de la biblioteca. Sus recortes y marcadores se
    borran solos por el ON DELETE CASCADE de esas tablas. El archivo PDF
    original en disco NO se toca, solo se quita la referencia."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM libros WHERE id = ?", (libro_id,))
    conn.commit()
    conn.close()


# ---------- Recortes ----------

def crear_recorte(libro_id, pagina, x, y, ancho, alto, titulo_opcional=""):
    # los recortes nuevos arrancan con el fondo "por defecto" que tengas
    # configurado en ese momento (ver get_config/set_config de
    # 'canvas_color_fondo' y 'canvas_estilo_fondo' en main.py) — esto NO
    # afecta a recortes ya existentes, cada uno guarda el suyo propio
    color_fondo_default = get_config("canvas_color_fondo", "#ffffff")
    estilo_fondo_default = get_config("canvas_estilo_fondo", "liso")

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO recortes
           (libro_id, pagina, x, y, ancho, alto, titulo_opcional, fecha_creado,
            color_fondo, estilo_fondo)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (libro_id, pagina, x, y, ancho, alto, titulo_opcional, datetime.now().isoformat(),
         color_fondo_default, estilo_fondo_default),
    )
    conn.commit()
    recorte_id = cur.lastrowid
    conn.close()
    return recorte_id


def listar_recortes_por_libro(libro_id, pagina=None):
    conn = get_connection()
    cur = conn.cursor()
    if pagina is not None:
        cur.execute(
            "SELECT * FROM recortes WHERE libro_id = ? AND pagina = ?",
            (libro_id, pagina),
        )
    else:
        cur.execute("SELECT * FROM recortes WHERE libro_id = ?", (libro_id,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def eliminar_recorte(recorte_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM recortes WHERE id = ?", (recorte_id,))
    conn.commit()
    conn.close()


def actualizar_recorte(recorte_id, x, y, ancho, alto):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE recortes SET x = ?, y = ?, ancho = ?, alto = ? WHERE id = ?",
        (x, y, ancho, alto, recorte_id),
    )
    conn.commit()
    conn.close()


def actualizar_fondo_recorte(recorte_id, color_fondo, estilo_fondo):
    """Guarda el fondo (color + cuadriculado o no) propio de ESTE recorte."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE recortes SET color_fondo = ?, estilo_fondo = ? WHERE id = ?",
        (color_fondo, estilo_fondo, recorte_id),
    )
    conn.commit()
    conn.close()


# ---------- Trazos / elementos de dibujo ----------
# Cada elemento (lápiz, rectángulo, elipse, flecha o texto) se guarda entero
# como JSON en datos_json, ya que cada tipo tiene campos de geometría distintos.
# color/grosor quedan también como columnas propias por si en el futuro se
# quiere filtrar/indexar sin parsear el JSON.

def guardar_trazos(recorte_id, elementos):
    """
    elementos: lista de dicts (ver formato en canvas_a4.py: cada uno con al
    menos 'tipo', 'color', 'grosor', y la geometría propia de su tipo).
    Reemplaza todos los elementos existentes del recorte (guardado simple y consistente).
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM trazos WHERE recorte_id = ?", (recorte_id,))
    for el in elementos:
        cur.execute(
            "INSERT INTO trazos (recorte_id, datos_json, color, grosor) VALUES (?, ?, ?, ?)",
            (recorte_id, json.dumps(el), el.get("color", "#000000"), el.get("grosor", 2.0)),
        )
    conn.commit()
    conn.close()


def cargar_trazos(recorte_id):
    """Devuelve la lista de elementos completos (dicts), en el orden en que se guardaron.

    Compatibilidad: si el proyecto tenía trazos guardados con una versión anterior
    de la app (donde datos_json era solo la lista de puntos, sin 'tipo'), se
    reconstruyen acá como elementos tipo 'lapiz' para no perder ese trabajo.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM trazos WHERE recorte_id = ? ORDER BY id", (recorte_id,))
    rows = cur.fetchall()
    conn.close()

    resultado = []
    for r in rows:
        datos = json.loads(r["datos_json"])
        if isinstance(datos, dict):
            resultado.append(datos)
        else:
            # formato viejo: datos_json era directamente la lista de puntos
            resultado.append({
                "tipo": "lapiz",
                "puntos": datos,
                "color": r["color"],
                "grosor": r["grosor"],
            })
    return resultado


# ---------- Config ----------

def get_config(clave, default=None):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT valor FROM config WHERE clave = ?", (clave,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return default
    return row["valor"]


def set_config(clave, valor):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO config (clave, valor) VALUES (?, ?) "
        "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
        (clave, str(valor)),
    )
    conn.commit()
    conn.close()


# ---------- Marcadores (para la función de "vistazo": ir y volver) ----------

def crear_marcador(libro_id, pagina, titulo):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO marcadores (libro_id, pagina, titulo, fecha_creado) VALUES (?, ?, ?, ?)",
        (libro_id, pagina, titulo, datetime.now().isoformat()),
    )
    conn.commit()
    marcador_id = cur.lastrowid
    conn.close()
    return marcador_id


def listar_marcadores_por_libro(libro_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM marcadores WHERE libro_id = ? ORDER BY id", (libro_id,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def eliminar_marcador(marcador_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM marcadores WHERE id = ?", (marcador_id,))
    conn.commit()
    conn.close()
