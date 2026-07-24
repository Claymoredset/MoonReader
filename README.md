# 📖 MoonReader

**A split-view PDF reader built for studying.**

Select any region of a page — a problem, a diagram, a paragraph you need to
work through — and annotate it on an infinite canvas right next to it. No
more switching between a PDF viewer and a separate notes app.

Built for autodidacts who want to study independently and keep track of
their own progress.
<img width="1919" height="984" alt="Screenshot From 2026-07-24 18-29-16" src="https://github.com/user-attachments/assets/15e70f5d-a8c5-464f-9810-49b876c9fa35" />


---

### 📚 Library
- Grid view with real page-1 covers for every imported PDF, lazy-loaded so
  it stays smooth even with a big collection.
- Remove a book with one click (your original PDF file is never touched).
- Picks up right where you left off — resumes at the last page you were on.

<img width="1429" height="846" alt="Screenshot From 2026-07-24 18-10-01" src="https://github.com/user-attachments/assets/8161e576-828d-4f66-9d2a-ee2607ed76f0" />


### 📄 Reader
- Continuous scroll, not page-by-page — reads like any modern PDF viewer.
- Lazy page rendering: opening a 500-page textbook won't eat your RAM.
- Zoom with `Ctrl` + mouse wheel.
- A collapsible page-thumbnail sidebar for quick navigation.
- **Bookmarks with peek-and-return**: jump to a page far away (like an
  answer key at the back of the book), then jump straight back to exactly
  where you were.

<!-- ![Reader with thumbnails](docs/screenshots/reader.png) -->

### Crops
- Drag a rectangle over any part of a page to create a crop.
- Resize or reposition it anytime by dragging its corner handles.
- Delete it with a right-click.

<!-- ![Creating a crop](docs/screenshots/crop.png) -->

### Canvas
Opening a crop reveals an infinite, pannable, zoomable canvas right next to
the PDF — this is where the actual studying happens.

- Tools: pen, rectangle, ellipse, arrow, text, eraser, and a select tool to
  move or delete anything you've drawn.
- Hand-drawn sketch style for shapes (a bit of intentional wobble, like a
  real sketch instead of a vector-perfect rectangle).
- Full undo/redo history.
- Custom background per crop — solid color or grid, your choice — so one
  crop can stay plain white while another goes dark-mode-grid, independently.
- Optional semi-transparent overlay of the original crop image, for
  reference while you write.

<!-- ![Canvas with drawings](docs/screenshots/canvas.png) -->

### Everything else
- Dark mode (View → Dark mode).
- Everything autosaves to a local SQLite database — nothing to remember
  to hit "save" on.

---

## Getting started

```bash
git clone https://github.com/Claymoredset/MoonReader.git
cd MoonReader
pip install -r requirements.txt
python3 main.py
```

That's it — no account, no server, no internet connection required. Your
library lives in a local `biblioteca.db` file next to the app.

### Requirements
- Python 3.9+
- [PySide6](https://pypi.org/project/PySide6/) — Qt for Python
- [PyMuPDF](https://pypi.org/project/PyMuPDF/) — PDF rendering

---

## 🗂️ Project structure

```
MoonReader/
├── main.py         # App entry point, library grid, main window, theming
├── pdf_view.py      # PDF viewer: continuous scroll, zoom, crops, bookmarks
├── canvas_a4.py     # The annotation canvas: drawing tools, undo/redo
├── db.py            # SQLite persistence layer
└── requirements.txt
```

---

## 🧭 Roadmap

Things that would be nice to add next:

- [ ] Export a crop + its annotations to an image or PDF
- [ ] Rename crops
- [ ] Zoom in/out on the PDF thumbnails panel
- [ ] Bring-to-front / send-to-back for overlapping canvas elements

Contributions and ideas welcome.

---

## 📄 License

MIT © Evian Morelli — see [LICENSE](LICENSE) for details.
