"""Browse what matched, at http://localhost:8000.

ponytail: stdlib http.server. It's a local read-only list page; FastAPI plus a
template engine would be three dependencies to render one table.
"""

from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from src import db

PAGE_SIZE = 50

CSS = """
:root { color-scheme: light dark; }
body { font: 15px/1.5 -apple-system, system-ui, sans-serif; margin: 0; padding: 2rem;
       max-width: 60rem; margin-inline: auto; }
h1 { font-size: 1.2rem; margin: 0 0 .25rem; }
.stats { opacity: .6; font-size: .85rem; margin-bottom: 1.5rem; }
form { margin-bottom: 1.5rem; display: flex; gap: .5rem; }
input[type=search] { flex: 1; padding: .5rem .75rem; font-size: 1rem;
                     border: 1px solid color-mix(in srgb, currentColor 25%, transparent);
                     border-radius: .375rem; background: transparent; color: inherit; }
button { padding: .5rem 1rem; font-size: 1rem; border-radius: .375rem; cursor: pointer;
         border: 1px solid color-mix(in srgb, currentColor 25%, transparent);
         background: transparent; color: inherit; }
article { border-top: 1px solid color-mix(in srgb, currentColor 15%, transparent);
          padding: 1rem 0; }
.reason { font-weight: 600; }
.when { float: right; opacity: .5; font-size: .8rem; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 0 .75rem; margin: .5rem 0; }
dt { opacity: .55; font-size: .85rem; }
dd { margin: 0; font-size: .85rem; }
details { margin-top: .5rem; }
summary { cursor: pointer; opacity: .55; font-size: .85rem; }
pre { white-space: pre-wrap; word-break: break-word; font-size: .85rem;
      opacity: .8; margin: .5rem 0 0; }
.imgs { display: flex; gap: .5rem; overflow-x: auto; margin-top: .5rem; }
.imgs img { height: 7rem; border-radius: .375rem; }
nav { margin-top: 2rem; display: flex; gap: 1rem; }
.empty { opacity: .6; padding: 3rem 0; text-align: center; }
"""


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _render(rows: list[dict[str, Any]], total: int, page: int, q: str, stats: dict) -> str:
    out = [
        "<!doctype html><meta charset=utf-8>",
        "<meta name=viewport content='width=device-width,initial-scale=1'>",
        "<title>Facebook Group Market Watch</title>",
        f"<style>{CSS}</style>",
        f"<h1>{total} matched</h1>",
        "<p class=stats>" + ", ".join(f"{_esc(k)}: {_esc(v)}" for k, v in sorted(stats.items())) + "</p>",
        f"<form><input type=search name=q value='{_esc(q)}' "
        "placeholder='search text and parsed fields'><button>search</button></form>",
    ]

    if not rows:
        out.append("<p class=empty>Nothing here yet. Run <code>watcher run --once</code>, "
                   "or check <code>watcher test</code> to see why posts aren't matching.</p>")

    for row in rows:
        parsed = row.get("parsed") or {}
        when = (row.get("posted_at") or row.get("scraped_at") or "")[:16].replace("T", " ")
        out.append("<article>")
        out.append(f"<span class=when>{_esc(when)}</span>")
        out.append(f"<div class=reason>{_esc(row.get('reason') or '')}</div>")

        shown = {k: v for k, v in parsed.items() if v is not None and v != ""}
        if shown:
            out.append("<dl>")
            for key, value in shown.items():
                out.append(f"<dt>{_esc(key)}</dt><dd>{_esc(value)}</dd>")
            out.append("</dl>")

        images = row.get("image_urls") or []
        if images:
            out.append("<div class=imgs>" + "".join(
                f"<img src='{_esc(u)}' loading=lazy alt=''>" for u in images[:6]
            ) + "</div>")

        out.append(f"<p><a href='{_esc(row['post_url'])}' target=_blank rel=noreferrer>"
                   "open on facebook</a></p>")
        out.append("<details><summary>original post</summary>"
                   f"<pre>{_esc(row.get('raw_text') or '')}</pre></details>")
        out.append("</article>")

    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    out.append("<nav>")
    if page > 1:
        out.append(f"<a href='?q={_esc(q)}&page={page - 1}'>previous</a>")
    out.append(f"<span>page {page} of {pages}</span>")
    if page < pages:
        out.append(f"<a href='?q={_esc(q)}&page={page + 1}'>next</a>")
    out.append("</nav>")
    return "".join(out)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        url = urlparse(self.path)
        params = parse_qs(url.query)

        if url.path == "/stats":
            body = json.dumps(db.stats(), indent=2).encode()
            self._respond(200, "application/json", body)
            return
        if url.path not in ("/", "/index.html"):
            self._respond(404, "text/plain; charset=utf-8", b"not found")
            return

        q = (params.get("q", [""])[0]).strip()
        try:
            page = max(1, int(params.get("page", ["1"])[0]))
        except ValueError:
            page = 1

        rows, total = db.matched_posts(limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE, q=q)
        body = _render(rows, total, page, q, db.stats()).encode("utf-8")
        self._respond(200, "text/html; charset=utf-8", body)

    def _respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass  # don't spam the terminal you're also running the watcher in


def serve(port: int = 8000) -> None:
    # Localhost only. There is no auth here and the data is scraped from private
    # groups — do not bind this to a public interface.
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"http://localhost:{port}  (ctrl-c to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
