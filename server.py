#!/usr/bin/env python3
"""ローカル完結のPDF黒塗り(redaction)サーバ。

外部への通信は一切行わない。アップロードされたPDFは一時ディレクトリに置かれ、
プロセス終了時に削除される。
"""
import atexit
import http.server
import io
import json
import os
import re
import shutil
import socketserver
import tempfile
import threading
import uuid
import webbrowser
from urllib.parse import urlparse, parse_qs

import fitz  # PyMuPDF

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
WORK = tempfile.mkdtemp(prefix="pdf-redact-")
atexit.register(lambda: shutil.rmtree(WORK, ignore_errors=True))

RENDER_DPI = 144  # 画面表示用。72pt=1inch なので scale = DPI/72
SCALE = RENDER_DPI / 72.0

_lock = threading.Lock()
_docs = {}  # sid -> path


def _doc_path(sid):
    if not re.fullmatch(r"[0-9a-f]{32}", sid or ""):
        raise ValueError("bad session id")
    p = _docs.get(sid)
    if not p:
        raise KeyError(sid)
    return p


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "PDFRedact/1.0"

    # --- helpers -----------------------------------------------------
    def _send(self, code, body=b"", ctype="application/octet-stream", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 200 * 1024 * 1024:
            raise ValueError("too large")
        return self.rfile.read(n)

    def log_message(self, fmt, *args):  # 静かに
        pass

    # --- routing -----------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                return self._static("index.html", "text/html; charset=utf-8")
            if u.path == "/app.js":
                return self._static("app.js", "text/javascript; charset=utf-8")
            if u.path == "/app.css":
                return self._static("app.css", "text/css; charset=utf-8")
            m = re.fullmatch(r"/page/(\d+)", u.path)
            if m:
                return self.page_png(int(m.group(1)), parse_qs(u.query))
            self._send(404, b"not found", "text/plain")
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            if u.path == "/upload":
                return self.upload()
            if u.path == "/search":
                return self.search()
            if u.path == "/apply":
                return self.apply()
            self._send(404, b"not found", "text/plain")
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": str(e)})

    def _static(self, name, ctype):
        with open(os.path.join(STATIC, name), "rb") as f:
            self._send(200, f.read(), ctype)

    # --- endpoints ---------------------------------------------------
    def upload(self):
        data = self._body()
        if not data.startswith(b"%PDF"):
            return self._json(400, {"error": "PDFファイルではありません"})
        sid = uuid.uuid4().hex
        path = os.path.join(WORK, sid + ".pdf")
        with open(path, "wb") as f:
            f.write(data)
        doc = fitz.open(path)
        if doc.needs_pass:
            doc.close()
            os.remove(path)
            return self._json(400, {"error": "パスワード保護されたPDFは開けません"})
        pages = [{"w": p.rect.width, "h": p.rect.height} for p in doc]
        doc.close()
        with _lock:
            _docs[sid] = path
        self._json(200, {"sid": sid, "pages": pages, "scale": SCALE})

    def page_png(self, idx, q):
        sid = (q.get("sid") or [""])[0]
        doc = fitz.open(_doc_path(sid))
        try:
            pix = doc[idx].get_pixmap(matrix=fitz.Matrix(SCALE, SCALE))
            self._send(200, pix.tobytes("png"), "image/png")
        finally:
            doc.close()

    def search(self):
        req = json.loads(self._body() or b"{}")
        term = (req.get("term") or "").strip()
        if not term:
            return self._json(400, {"error": "検索語が空です"})
        doc = fitz.open(_doc_path(req.get("sid")))
        try:
            hits = []
            for i, page in enumerate(doc):
                for r in page.search_for(term, quads=False):
                    hits.append({"page": i, "x0": r.x0, "y0": r.y0,
                                 "x1": r.x1, "y1": r.y1})
            self._json(200, {"hits": hits})
        finally:
            doc.close()

    def apply(self):
        req = json.loads(self._body() or b"{}")
        sid = req.get("sid")
        boxes = req.get("boxes") or []
        rasterize = bool(req.get("rasterize"))
        doc = fitz.open(_doc_path(sid))
        try:
            by_page = {}
            for b in boxes:
                by_page.setdefault(int(b["page"]), []).append(
                    fitz.Rect(b["x0"], b["y0"], b["x1"], b["y1"]))

            for pno, rects in by_page.items():
                page = doc[pno]
                for r in rects:
                    # fill=黒。text/graphics/images すべて領域内は消される。
                    page.add_redact_annot(r, fill=(0, 0, 0))
                # images=REDACT_IMAGE_PIXELS: 画像も該当ピクセルだけ塗り潰す
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)

            if rasterize:
                out = fitz.open()
                for page in doc:
                    pix = page.get_pixmap(matrix=fitz.Matrix(200 / 72, 200 / 72))
                    np = out.new_page(width=page.rect.width, height=page.rect.height)
                    np.insert_image(np.rect, pixmap=pix)
                doc.close()
                doc = out

            # メタデータとXMPを除去してから保存(作成者名等の漏れ防止)
            doc.set_metadata({})
            doc.del_xml_metadata()
            buf = io.BytesIO()
            doc.save(buf, garbage=4, deflate=True, clean=True)
            self._send(200, buf.getvalue(), "application/pdf",
                       {"Content-Disposition": 'attachment; filename="redacted.pdf"'})
        finally:
            doc.close()


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    port = int(os.environ.get("PORT", "8756"))
    with Server(("127.0.0.1", port), Handler) as httpd:
        url = f"http://127.0.0.1:{port}/"
        print(f"PDF黒塗りツール起動: {url}")
        print("終了するには Ctrl+C")
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n終了しました。一時ファイルを削除します。")


if __name__ == "__main__":
    main()
