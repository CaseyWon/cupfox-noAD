from __future__ import annotations

import argparse
import gzip
import html
import json
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "index.html"
UPSTREAM = "https://www.nzwuye.com"
IMAGE_HOSTS = {"img.lzipic.com", "img.feifeiimg.vip", "tupian.ffeiimg.com"}
USER_AGENT = "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/125 Mobile Safari/537.36"
IMAGE_CACHE: OrderedDict[str, tuple[str, bytes]] = OrderedDict()
IMAGE_CACHE_LOCK = threading.Lock()
IMAGE_CACHE_LIMIT = 100
PAGE_CACHE: OrderedDict[str, tuple[float, str]] = OrderedDict()
PAGE_CACHE_LOCK = threading.Lock()
PAGE_TTL_SECONDS = 180
STATIC_SUFFIXES = {
    ".js", ".css", ".map", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff2",
}
STATIC_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".map": "application/json",
}


def fetch_page(path: str, fetcher) -> str:
    """Fetch an upstream page with a small TTL cache (shared by search/detail/play)."""
    now = time.time()
    with PAGE_CACHE_LOCK:
        cached = PAGE_CACHE.get(path)
        if cached and now - cached[0] < PAGE_TTL_SECONDS:
            PAGE_CACHE.move_to_end(path)
            return cached[1]
    _, raw = fetcher(path)
    page = raw.decode("utf-8", "replace")
    with PAGE_CACHE_LOCK:
        PAGE_CACHE[path] = (now, page)
        PAGE_CACHE.move_to_end(path)
        while len(PAGE_CACHE) > 64:
            PAGE_CACHE.popitem(last=False)
    return page


def clean(fragment: str) -> str:
    fragment = re.sub(r"<script\b[^>]*>.*?</script>", "", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<style\b[^>]*>.*?</style>", "", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    fragment = html.unescape(fragment).replace("\u3000", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", fragment).strip(" /\n\r\t")


def field(block: str, label: str) -> str:
    match = re.search(
        rf'<span class="video-info-itemtitle">{re.escape(label)}：</span>\s*<div class="[^"]*video-info-item[^"]*">(.*?)</div>',
        block,
        flags=re.I | re.S,
    )
    return clean(match.group(1)) if match else ""


def parse_results(page: str) -> list[dict[str, object]]:
    chunks = page.split('<div class="module-search-item">')[1:]
    results: list[dict[str, object]] = []
    seen: set[str] = set()
    for chunk in chunks:
        block = chunk.split('<div class="module-search-item">', 1)[0]
        title_match = re.search(
            r'<h3>\s*<a href="/movie/index(\d+)\.html"[^>]*title="([^"]+)"', block, flags=re.I | re.S
        )
        if not title_match:
            title_match = re.search(
                r'<a[^>]*href="/movie/index(\d+)\.html"[^>]*>\s*<img[^>]*alt="([^"]+)"', block, flags=re.I | re.S
            )
        if not title_match:
            continue
        vod_id, title = title_match.groups()
        if vod_id in seen:
            continue
        seen.add(vod_id)
        image_match = re.search(r'<img[^>]+(?:data-src|src)="(https?://[^"]+)"', block, flags=re.I)
        status_match = re.search(r'<a class="video-serial"[^>]*>(.*?)</a>', block, flags=re.I | re.S)
        aux_match = re.search(r'<div class="video-info-aux">(.*?)</div>\s*</div>', block, flags=re.I | re.S)
        aux = clean(aux_match.group(1)).split() if aux_match else []
        category = aux[0] if aux else "影视"
        year = next((part for part in aux if re.fullmatch(r"(?:19|20)\d{2}", part)), "")
        area = next((part for part in aux if part not in {category, year, "/"}), "")
        description = field(block, "剧情")
        results.append(
            {
                "id": vod_id,
                "title": html.unescape(title),
                "img": image_match.group(1).replace("http://", "https://", 1) if image_match else "",
                "status": clean(status_match.group(1)) if status_match else "",
                "cat": category,
                "year": int(year) if year else 0,
                "area": area or "未知",
                "director": field(block, "导演"),
                "actors": field(block, "主演"),
                "desc": description or "暂无剧情简介",
            }
        )
    return results


def parse_detail(page: str, vod_id: str) -> dict[str, object]:
    def meta(prop: str) -> str:
        m = re.search(
            rf'<meta[^>]+(?:property|name)=["\']{prop}["\'][^>]*content=["\']([^"\']*)["\']',
            page, re.I,
        )
        if not m:
            m = re.search(
                rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{prop}["\']',
                page, re.I,
            )
        return html.unescape(m.group(1)).strip() if m else ""

    groups: dict[int, dict[int, str]] = {}
    for sid, nid, label in re.findall(
        rf'href="/play/{vod_id}-(\d+)-(\d+)\.html"[^>]*>\s*([^<]{{1,60}}?)\s*</a>', page, re.I
    ):
        source_id, episode = int(sid), int(nid)
        text = html.unescape(label).strip() or f"第{episode + 1}集"
        groups.setdefault(source_id, {})[episode] = text
    sources = [
        {"sid": sid, "eps": [{"n": n, "label": t} for n, t in sorted(eps.items())]}
        for sid, eps in sorted(groups.items())
    ]
    remarks = field(page, "备注") or field(page, "更新")
    year_text = field(page, "年份") or field(page, "上映")
    year_match = re.search(r"\d{4}", year_text)
    cover = meta("og:image").replace("http://", "https://", 1)
    return {
        "id": vod_id,
        "title": meta("og:title") or "",
        "cover": cover,
        "desc": re.sub(r"\s+", " ", meta("og:description")) or field(page, "剧情") or "暂无剧情简介",
        "director": re.sub(r"^[\s/]+", "", field(page, "导演") or "未知"),
        "actors": re.sub(r"^[\s/]+", "", field(page, "主演") or "未知"),
        "area": field(page, "地区") or "未知",
        "lang": field(page, "语言"),
        "cat": field(page, "类型"),
        "year": int(year_match.group(0)) if year_match else 0,
        "remarks": remarks,
        "status": remarks or "暂无更新信息",
        "sources": sources,
        "sourceCount": len(sources),
        "episodeCount": sum(len(s["eps"]) for s in sources),
    }


def parse_play(page: str) -> dict[str, object]:
    def var(name: str) -> str:
        m = re.search(rf'var\s+{name}\s*=\s*["\']([^"\']*)["\']', page, re.I)
        return m.group(1) if m else ""

    now = var("now")
    pn = var("pn")
    part = var("vpart") or var("vod_part")
    name = var("vod_name")
    if not now:
        return {"playable": False, "reason": "该集页面中没有可直接解析的视频直链", "pn": pn}
    url = f"https:{now}" if now.startswith("//") else now
    if not re.match(r"^https?://", url, re.I):
        return {"playable": False, "reason": "片源地址无效", "pn": pn}
    is_direct = bool(re.search(r"\.(?:m3u8|mp4)(?:\?|$)", url, re.I)) or pn == "ffm3u8"
    if not is_direct:
        return {
            "playable": False,
            "reason": "该线路依赖第三方解析播放器，纯净模式下已跳过",
            "pn": pn,
        }
    url = re.sub(r"^http://", "https://", url, flags=re.I)
    fmt = "mp4" if re.search(r"\.mp4(?:\?|$)", url, re.I) else "hls"
    return {"playable": True, "url": url, "format": fmt, "pn": pn, "part": part, "name": name}


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "CupfoxCleanBridge/1.0"

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    @property
    def proxy(self) -> str:
        return getattr(self.server, "upstream_proxy", "")

    def send_cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def json_response(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_cors()
        self.end_headers()
        self.wfile.write(body)

    def opener(self) -> urllib.request.OpenerDirector:
        handlers: list[object] = [urllib.request.HTTPSHandler(context=ssl._create_unverified_context())]
        if self.proxy:
            handlers.insert(0, urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
        return urllib.request.build_opener(*handlers)

    def fetch(self, url: str, data: bytes | None = None, accept: str = "*/*") -> tuple[dict[str, str], bytes]:
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": accept,
                "Accept-Encoding": "gzip",
                "Referer": UPSTREAM + "/",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with self.opener().open(request, timeout=25) as response:
            body = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return dict(response.headers.items()), body

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_cors()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            body = INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/health":
            self.json_response({"ok": True, "upstream": UPSTREAM})
            return
        if parsed.path == "/api/search":
            self.handle_search(parsed.query)
            return
        if parsed.path == "/api/detail":
            self.handle_detail(parsed.query)
            return
        if parsed.path == "/api/play":
            self.handle_play(parsed.query)
            return
        if parsed.path == "/api/image":
            self.handle_image(parsed.query)
            return
        if self.serve_static(parsed.path):
            return
        self.json_response({"error": "not_found"}, 404)

    def serve_static(self, path: str) -> bool:
        if not path.startswith("/") or ".." in path:
            return False
        candidate = (ROOT / path.lstrip("/")).resolve()
        try:
            candidate.relative_to(ROOT)
        except ValueError:
            return False
        if not candidate.is_file() or candidate.suffix.lower() not in STATIC_SUFFIXES:
            return False
        body = candidate.read_bytes()
        content_type = STATIC_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_cors()
        self.end_headers()
        self.wfile.write(body)
        return True

    def handle_detail(self, query_string: str) -> None:
        vod_id = urllib.parse.parse_qs(query_string).get("id", [""])[0].strip()
        if not re.fullmatch(r"\d{1,12}", vod_id):
            self.json_response({"error": "invalid_id"}, 400)
            return
        try:
            page = fetch_page(f"/movie/index{vod_id}.html", lambda p: self.fetch(UPSTREAM + p))
            self.json_response(parse_detail(page, vod_id))
        except Exception as error:
            self.json_response({"error": "upstream_unavailable", "message": str(error)}, 502)

    def handle_play(self, query_string: str) -> None:
        params = urllib.parse.parse_qs(query_string)
        vod_id = params.get("id", [""])[0].strip()
        episode = params.get("ep", [""])[0].strip()
        source = params.get("src", ["0"])[0].strip() or "0"
        if not (re.fullmatch(r"\d{1,12}", vod_id) and re.fullmatch(r"\d{1,6}", episode)
                and re.fullmatch(r"\d{1,4}", source)):
            self.json_response({"error": "invalid_params"}, 400)
            return
        try:
            path = f"/play/{vod_id}-{source}-{episode}.html"
            page = fetch_page(path, lambda p: self.fetch(UPSTREAM + p))
            payload = parse_play(page)
            if payload.get("playable"):
                payload.update({"ep": int(episode), "src": int(source)})
            self.json_response(payload)
        except Exception as error:
            self.json_response({"error": "upstream_unavailable", "message": str(error)}, 502)

    def handle_search(self, query_string: str) -> None:
        keyword = urllib.parse.parse_qs(query_string).get("q", [""])[0].strip()
        if not keyword or len(keyword) > 60:
            self.json_response({"error": "invalid_query", "results": []}, 400)
            return
        try:
            data = urllib.parse.urlencode({"searchword": keyword}).encode("utf-8")
            _, raw = self.fetch(UPSTREAM + "/search.php", data, "text/html,application/xhtml+xml")
            page = raw.decode("utf-8", "replace")
            results = parse_results(page)
            self.json_response({"query": keyword, "count": len(results), "results": results})
        except Exception as error:
            self.json_response({"error": "upstream_unavailable", "message": str(error), "results": []}, 502)

    def handle_image(self, query_string: str) -> None:
        url = urllib.parse.parse_qs(query_string).get("url", [""])[0]
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in IMAGE_HOSTS:
            self.json_response({"error": "invalid_image_host"}, 400)
            return
        with IMAGE_CACHE_LOCK:
            cached = IMAGE_CACHE.get(url)
            if cached:
                IMAGE_CACHE.move_to_end(url)
        if cached:
            content_type, body = cached
        else:
            try:
                headers, body = self.fetch(url, accept="image/avif,image/webp,image/apng,image/*,*/*;q=0.8")
                content_type = headers.get("Content-Type", "image/jpeg").split(";", 1)[0]
                if not content_type.startswith("image/") or len(body) > 8 * 1024 * 1024:
                    raise ValueError("invalid image response")
                with IMAGE_CACHE_LOCK:
                    IMAGE_CACHE[url] = (content_type, body)
                    IMAGE_CACHE.move_to_end(url)
                    while len(IMAGE_CACHE) > IMAGE_CACHE_LIMIT:
                        IMAGE_CACHE.popitem(last=False)
            except Exception as error:
                self.json_response({"error": "image_unavailable", "message": str(error)}, 502)
                return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_cors()
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ad-free live search bridge for the single-page clone")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--proxy", default="http://127.0.0.1:7890", help="Upstream HTTP proxy; pass an empty value for direct access")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), BridgeHandler)
    server.upstream_proxy = args.proxy
    print(f"Cupfox clean bridge: http://127.0.0.1:{args.port}/")
    print(f"Upstream: {UPSTREAM} via {args.proxy or 'direct connection'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
