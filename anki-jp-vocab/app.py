#!/usr/bin/env python3
"""JP vocab -> Anki pipeline: review server, AnkiConnect pusher, and the
image/audio lookup CLI used by the AI orchestrator while building cards.
Everything lives in this one file — external API clients (irasutoya/Pixabay
image search, ElevenLabs TTS), the review HTTP server, and AnkiConnect push
— because each piece is small and this is a single-purpose personal tool,
not a library meant to be reused elsewhere.

Run modes:
    python3 app.py                    start the review UI at http://localhost:8877
    python3 app.py push               push all "approved" cards into Anki via
                                       AnkiConnect and exit, no server
    python3 app.py images-search --query 犬 --source irasutoya --limit 6
    python3 app.py images-download --url <image_url> --dest /tmp/out.jpg
    python3 app.py audio-voices
    python3 app.py audio-speak --text 本業 --voice-id <id> --dest /tmp/out.mp3

State lives in a flat YAML file so a human or an AI can both read/edit it
directly between steps: anki-jp-vocab/state.yaml
"""
import argparse
import html
import json
import mimetypes
import os
import re
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests
import yaml

PORT = 8877
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = SCRIPT_DIR
STATE_FILE = os.path.join(STATE_DIR, "state.yaml")
IMAGES_DIR = os.path.join(STATE_DIR, "images")
AUDIO_DIR = os.path.join(STATE_DIR, "audio")
STATIC_DIR = SCRIPT_DIR

ANKICONNECT_URL = "http://127.0.0.1:8765"
# Separate note type + deck from the original "Japanese Personal Vocab" /
# "1 Japanese::Japanese Personal Vocab" so the existing collection (already
# pushed cards) stays untouched. This one adds AudioWord/AudioSentence
# fields with a fallback to Anki's built-in system TTS when no generated
# clip is attached.
DECK_NAME = "1 Japanese::Japanese Vocab AI Gen"
MODEL_NAME = "Japanese Vocab AI Gen"
MODEL_FIELDS = ["Expression", "Meaning", "ExampleJpn", "Example", "Image", "AudioWord", "AudioSentence"]
MODEL_CSS = """.card {
 font-family: "Hiragino Mincho ProN";
 font-size: 20px;
 text-align: center;
 color: black;
 background-color: white;
}"""
_ANSWER_BODY = (
    "{{FrontSide}}<hr id=answer>"
    "<span style=\"font-size: 30px;\">{{furigana:Expression}}</span><br>"
    "<span style=\"font-size: 30px;\">{{Meaning}}</span><br>"
    "{{#ExampleJpn}}<br>{{furigana:ExampleJpn}}<br>{{furigana:Example}}{{/ExampleJpn}}"
    "{{#Image}}<br>{{Image}}{{/Image}}"
    "<br>{{#AudioWord}}{{AudioWord}}{{/AudioWord}}{{^AudioWord}}{{tts ja_JP:kanji:Expression}}{{/AudioWord}}"
    "<br>{{#AudioSentence}}{{AudioSentence}}{{/AudioSentence}}{{^AudioSentence}}{{tts ja_JP:kanji:ExampleJpn}}{{/AudioSentence}}"
)
MODEL_TEMPLATES = [
    {
        "Name": "Recognition",
        "Front": "<span style=\"font-size: 30px;\">{{kanji:Expression}}</span>",
        "Back": _ANSWER_BODY,
    },
    {
        "Name": "Recall",
        "Front": "<span style=\"font-size: 30px;\">{{Meaning}}</span>",
        "Back": _ANSWER_BODY,
    },
]


# ---------- state ----------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"cards": []}
    with open(STATE_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f) or {"cards": []}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(state, f, allow_unicode=True, sort_keys=False)


def find_card(state, card_id):
    for c in state["cards"]:
        if str(c["id"]) == str(card_id):
            return c
    return None


ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

_IMAGE_MAGIC = [
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
]


def media_url(route: str, path: str) -> str | None:
    """Build a /images/... or /audio/... URL stamped with the file's mtime.

    Re-uploading a picture (or regenerating audio) overwrites the file under
    the card's existing name, so the URL alone never changes and the browser
    happily keeps showing the copy it already has in memory. The mtime stamp
    makes each new version a distinct URL.
    """
    if not path:
        return None
    abs_path = path if os.path.isabs(path) else os.path.join(STATE_DIR, path)
    url = f"/{route}/{os.path.basename(abs_path)}"
    try:
        return f"{url}?v={int(os.path.getmtime(abs_path))}"
    except OSError:
        return url


def with_media_urls(state: dict) -> dict:
    """Card list for the UI, each card carrying versioned media URLs.

    Computed per request rather than stored, so state.yaml stays the plain
    hand-editable file the pipeline expects.
    """
    cards = []
    for card in state.get("cards", []):
        card = dict(card)
        card["image_url"] = media_url("images", card.get("image_path"))
        card["audio_word_url"] = media_url("audio", card.get("audio_word_path"))
        card["audio_sentence_url"] = media_url("audio", card.get("audio_sentence_path"))
        cards.append(card)
    return {**state, "cards": cards}


def _sniff_image_ext(data: bytes) -> str:
    """Fall back to magic bytes when the upload has no usable filename."""
    for magic, ext in _IMAGE_MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ""


_FURIGANA_RE = re.compile(r"\[[^\]]*\]")


def strip_furigana(text):
    return _FURIGANA_RE.sub("", text or "")


def tts_text(text):
    """Field text as sent to the voice model: no furigana, no spaces.

    The spaces exist only to anchor Anki furigana; TTS reads them as pauses.
    """
    return re.sub(r"\s+", "", strip_furigana(text))


# ---------- image search (irasutoya scrape + Pixabay API) ----------

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY")

_SIZE_RE = re.compile(r"/s\d+(?:-c)?/")
_JP_CHAR_RE = re.compile(r"[぀-ヿ㐀-鿿]")


def _resize_blogger_url(url: str, size: str) -> str:
    """Blogger/googleusercontent image URLs encode size as a path segment
    like /s72-c/ or /s320/. Swap it for the size we want. size="s0" gives
    the original, full-resolution image."""
    if _SIZE_RE.search(url):
        return _SIZE_RE.sub(f"/{size}/", url, count=1)
    return url


def search_irasutoya(query: str, limit: int = 6) -> list[dict]:
    resp = requests.get(
        "https://www.irasutoya.com/search",
        params={"q": query},
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.text

    results = []
    for block in body.split("class='post-outer'")[1:]:
        link_m = re.search(r"<a href='(https://www\.irasutoya\.com/[^']+\.html)'>", block)
        thumb_m = re.search(r'bp_thumbnail_resize\("([^"]*)","([^"]*)"\)', block)
        if not link_m or not thumb_m:
            continue
        raw_thumb_url, raw_title = thumb_m.groups()
        if not raw_thumb_url:
            continue
        title = html.unescape(raw_title)
        results.append({
            "source": "irasutoya",
            "title": title,
            "page_url": link_m.group(1),
            "thumb_url": _resize_blogger_url(raw_thumb_url, "s400"),
            "image_url": _resize_blogger_url(raw_thumb_url, "s0"),
        })
        if len(results) >= limit:
            break
    return results


def search_pixabay(query: str, limit: int = 6, image_type: str = "illustration") -> list[dict]:
    if not PIXABAY_API_KEY:
        raise RuntimeError(
            "PIXABAY_API_KEY is not set. Sign up at https://pixabay.com/ (free) and "
            "grab your key from https://pixabay.com/api/docs/, then "
            "export PIXABAY_API_KEY=... before using the pixabay image source."
        )
    params = {
        "key": PIXABAY_API_KEY,
        "q": query,
        "lang": "ja" if _JP_CHAR_RE.search(query) else "en",
        "image_type": image_type,
        "safesearch": "true",
        "per_page": max(limit, 3),  # pixabay requires per_page >= 3
    }
    resp = requests.get("https://pixabay.com/api/", params=params, timeout=15)
    resp.raise_for_status()
    hits = resp.json().get("hits", [])

    if not hits and image_type != "all":
        # illustrations are the closest style match to irasutoya, but if
        # there are none for this query, broaden to photos too
        params["image_type"] = "all"
        resp = requests.get("https://pixabay.com/api/", params=params, timeout=15)
        resp.raise_for_status()
        hits = resp.json().get("hits", [])

    results = []
    for item in hits[:limit]:
        results.append({
            "source": "pixabay",
            "title": item.get("tags", ""),
            "page_url": item.get("pageURL", ""),
            "thumb_url": item.get("previewURL", ""),
            "image_url": item.get("largeImageURL") or item.get("webformatURL", ""),
        })
    return results


def get_image_candidates(query: str, source: str = "irasutoya", limit: int = 6) -> list[dict]:
    if source == "irasutoya":
        return search_irasutoya(query, limit)
    if source == "pixabay":
        return search_pixabay(query, limit)
    raise ValueError(f"unknown source: {source!r}")


def download_image(url: str, dest_path: str) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30, stream=True)
    resp.raise_for_status()
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(8192):
            f.write(chunk)
    return dest_path


# ---------- ElevenLabs text-to-speech ----------

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1"
ELEVENLABS_DEFAULT_MODEL_ID = "eleven_v3"


def _require_elevenlabs_key():
    if not ELEVENLABS_API_KEY:
        raise RuntimeError(
            "ELEVENLABS_API_KEY is not set. Grab a key from https://elevenlabs.io/app/settings/api-keys, "
            "then export ELEVENLABS_API_KEY=... before generating audio."
        )


def list_voices() -> list[dict]:
    _require_elevenlabs_key()
    resp = requests.get(
        f"{ELEVENLABS_BASE_URL}/voices",
        headers={"xi-api-key": ELEVENLABS_API_KEY},
        timeout=15,
    )
    resp.raise_for_status()
    voices = resp.json().get("voices", [])
    return [
        {
            "voice_id": v["voice_id"],
            "name": v["name"],
            "category": v.get("category", ""),
            "preview_url": v.get("preview_url", ""),
        }
        for v in voices
    ]


def synthesize(
    text: str,
    voice_id: str,
    dest_path: str,
    model_id: str = ELEVENLABS_DEFAULT_MODEL_ID,
    language_code: str | None = None,
) -> str:
    _require_elevenlabs_key()
    body = {"text": text, "model_id": model_id}
    if language_code:
        body["language_code"] = language_code
    resp = requests.post(
        f"{ELEVENLABS_BASE_URL}/text-to-speech/{voice_id}",
        headers={
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return dest_path


# ---------- AnkiConnect ----------

def anki_invoke(action, **params):
    resp = requests.post(
        ANKICONNECT_URL,
        json={"action": action, "version": 6, "params": params},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(data["error"])
    return data.get("result")


def ensure_model_and_deck():
    models = anki_invoke("modelNames")
    if MODEL_NAME not in models:
        anki_invoke(
            "createModel",
            modelName=MODEL_NAME,
            inOrderFields=MODEL_FIELDS,
            css=MODEL_CSS,
            cardTemplates=MODEL_TEMPLATES,
        )
    anki_invoke("createDeck", deck=DECK_NAME)


def push_approved():
    state = load_state()
    try:
        ensure_model_and_deck()
    except requests.exceptions.ConnectionError:
        return {"error": "Could not reach AnkiConnect at %s — make sure Anki is open." % ANKICONNECT_URL}
    except RuntimeError as e:
        return {"error": str(e)}

    pushed, errors = [], []
    for card in state["cards"]:
        if card.get("status") != "approved":
            continue
        fields = {
            "Expression": card.get("expression", ""),
            "Meaning": card.get("meaning", ""),
            "ExampleJpn": card.get("example_jpn", ""),
            "Example": card.get("example_en", ""),
            "Image": "",
            "AudioWord": "",
            "AudioSentence": "",
        }
        picture = []
        if card.get("image_path") and os.path.exists(card["image_path"]):
            filename = f"jpvocab_{card['id']}_{os.path.basename(card['image_path'])}"
            picture.append({"path": card["image_path"], "filename": filename, "fields": ["Image"]})
        audio = []
        if card.get("audio_word_path") and os.path.exists(card["audio_word_path"]):
            filename = f"jpvocab_{card['id']}_word_{os.path.basename(card['audio_word_path'])}"
            audio.append({"path": card["audio_word_path"], "filename": filename, "fields": ["AudioWord"]})
        if card.get("audio_sentence_path") and os.path.exists(card["audio_sentence_path"]):
            filename = f"jpvocab_{card['id']}_sentence_{os.path.basename(card['audio_sentence_path'])}"
            audio.append({"path": card["audio_sentence_path"], "filename": filename, "fields": ["AudioSentence"]})
        note = {
            "deckName": DECK_NAME,
            "modelName": MODEL_NAME,
            "fields": fields,
            "options": {"allowDuplicate": False},
            "tags": ["ai-generated"],
        }
        if picture:
            note["picture"] = picture
        if audio:
            note["audio"] = audio
        try:
            note_id = anki_invoke("addNote", note=note)
            card["status"] = "pushed"
            card["anki_note_id"] = note_id
            pushed.append(card["id"])
        except RuntimeError as e:
            errors.append({"id": card["id"], "error": str(e)})

    save_state(state)
    return {"pushed": pushed, "errors": errors}


# ---------- HTTP handler ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            # Never let a malformed/binary body escape as an exception: an
            # unhandled error here kills the connection with no response at
            # all, which the browser only sees as "Failed to fetch".
            return {}

    def _parse_multipart_file(self, field_name="file"):
        """Pull one file field out of a multipart/form-data body.

        Returns (filename, data), or (None, None) if the field is absent.
        Hand-rolled on purpose: the stdlib `cgi` module is gone as of Python
        3.13, and this only ever needs a single small file field.
        """
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            raise ValueError("multipart/form-data required")

        m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', ctype)
        if not m:
            raise ValueError("multipart body has no boundary")
        boundary = (m.group(1) or m.group(2)).strip().encode()

        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            raise ValueError("empty request body")
        body = self.rfile.read(length)

        # A multipart body is: --BOUNDARY CRLF headers CRLF CRLF data CRLF
        # repeated, terminated by --BOUNDARY--. The client guarantees the
        # boundary never occurs inside the payload, so a plain split is safe.
        for segment in body.split(b"--" + boundary)[1:-1]:
            segment = segment[2:] if segment.startswith(b"\r\n") else segment
            split_at = segment.find(b"\r\n\r\n")
            if split_at == -1:
                continue
            raw_headers = segment[:split_at].decode("utf-8", "replace")
            data = segment[split_at + 4:]
            if data.endswith(b"\r\n"):
                data = data[:-2]

            disposition = ""
            for line in raw_headers.split("\r\n"):
                if line.lower().startswith("content-disposition:"):
                    disposition = line
                    break
            name_m = re.search(r'name=(?:"([^"]*)"|([^;]+))', disposition)
            name = (name_m.group(1) or name_m.group(2)).strip() if name_m else ""
            if name != field_name:
                continue
            fn_m = re.search(r'filename=(?:"([^"]*)"|([^;]+))', disposition)
            filename = (fn_m.group(1) or fn_m.group(2)).strip() if fn_m else ""
            return filename, data

        return None, None

    def _serve_file(self, path, content_type, no_cache=False):
        if not os.path.exists(path):
            self._json({"error": "not found"}, 404)
            return
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if no_cache:
            # Media is overwritten in place (a re-upload reuses the card's
            # filename), so a cached copy would keep showing the old picture
            # even after the new one lands on disk.
            self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _handle_image_upload(self, card_id):
        # Parse before anything else can bail out: leaving an unread body in
        # the socket desyncs the next request on a keep-alive connection.
        try:
            filename, data = self._parse_multipart_file("file")
        except ValueError as e:
            self._json({"error": str(e)}, 400)
            return
        except Exception as e:
            self._json({"error": f"could not parse upload: {e}"}, 400)
            return

        if not data:
            self._json({"error": "no file uploaded"}, 400)
            return

        state = load_state()
        card = find_card(state, card_id)
        if not card:
            self._json({"error": "no such card"}, 404)
            return

        # Honour the real extension: saving a PNG as .jpg makes the browser
        # (and Anki) guess the wrong type, and it silently reuses the old
        # URL so the card keeps showing the previous picture.
        ext = os.path.splitext(filename or "")[1].lower()
        if ext not in ALLOWED_IMAGE_EXTS:
            ext = _sniff_image_ext(data)
        if not ext:
            self._json({"error": f"unsupported image type: {filename!r}"}, 400)
            return

        os.makedirs(IMAGES_DIR, exist_ok=True)
        dest = os.path.join(IMAGES_DIR, f"{card['id']}{ext}")
        with open(dest, "wb") as f:
            f.write(data)

        # Drop the previous file when the new one lands under a different
        # extension, otherwise it lingers in images/ forever.
        old = card.get("image_path")
        if old:
            old_abs = old if os.path.isabs(old) else os.path.join(STATE_DIR, old)
            if os.path.abspath(old_abs) != os.path.abspath(dest) and os.path.exists(old_abs):
                try:
                    os.remove(old_abs)
                except OSError:
                    pass

        card["image_path"] = dest
        card["image_source"] = "uploaded"
        save_state(state)
        self._json({"image_url": media_url("images", dest)})

    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)

        if path == "/":
            # Served uncached so edits to the UI show up on a plain reload
            # rather than needing a hard refresh.
            self._serve_file(
                os.path.join(STATIC_DIR, "index.html"),
                "text/html; charset=utf-8",
                no_cache=True,
            )
            return

        if path == "/api/cards":
            self._json(with_media_urls(load_state()))
            return

        m = re.match(r"^/api/cards/([^/]+)/candidates$", path)
        if m:
            query = qs.get("query", [""])[0]
            source = qs.get("source", ["irasutoya"])[0]
            limit = int(qs.get("limit", ["6"])[0])
            try:
                results = get_image_candidates(query, source, limit)
                self._json({"results": results})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return

        m = re.match(r"^/images/(.+)$", path)
        if m:
            filename = m.group(1)
            ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            self._serve_file(os.path.join(IMAGES_DIR, filename), ctype, no_cache=True)
            return

        m = re.match(r"^/audio/(.+)$", path)
        if m:
            filename = m.group(1)
            ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            self._serve_file(os.path.join(AUDIO_DIR, filename), ctype, no_cache=True)
            return

        if path == "/api/voices":
            try:
                self._json({"voices": list_voices()})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return

        self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # The image upload is the one route with a binary (multipart) body, so
        # it has to be dispatched before the JSON read below — that read would
        # otherwise swallow the bytes and choke on them.
        m = re.match(r"^/api/cards/([^/]+)/image/upload$", path)
        if m:
            self._handle_image_upload(m.group(1))
            return

        body = self._read_json_body()

        m = re.match(r"^/api/cards/([^/]+)$", path)
        if m:
            state = load_state()
            card = find_card(state, m.group(1))
            if not card:
                self._json({"error": "no such card"}, 404)
                return
            for key in ("expression", "meaning", "example_jpn", "example_en", "status", "notes"):
                if key in body:
                    card[key] = body[key]
            save_state(state)
            self._json(card)
            return

        m = re.match(r"^/api/cards/([^/]+)/image$", path)
        if m:
            state = load_state()
            card = find_card(state, m.group(1))
            if not card:
                self._json({"error": "no such card"}, 404)
                return
            image_url = body.get("image_url")
            ext = os.path.splitext(urlparse(image_url).path)[1] or ".jpg"
            if len(ext) > 5:
                ext = ".jpg"
            dest = os.path.join(IMAGES_DIR, f"{card['id']}{ext}")
            try:
                download_image(image_url, dest)
            except Exception as e:
                self._json({"error": str(e)}, 400)
                return
            card["image_path"] = dest
            card["image_source"] = body.get("source", "")
            save_state(state)
            self._json({"image_url": media_url("images", dest)})
            return

        m = re.match(r"^/api/cards/([^/]+)/image/remove$", path)
        if m:
            state = load_state()
            card = find_card(state, m.group(1))
            if not card:
                self._json({"error": "no such card"}, 404)
                return
            if card.get("image_path") and os.path.exists(card["image_path"]):
                os.remove(card["image_path"])
            card["image_path"] = None
            card["image_source"] = None
            save_state(state)
            self._json({})
            return

        m = re.match(r"^/api/cards/([^/]+)/audio$", path)
        if m:
            state = load_state()
            card = find_card(state, m.group(1))
            if not card:
                self._json({"error": "no such card"}, 404)
                return
            kind = body.get("kind")
            if kind not in ("word", "sentence"):
                self._json({"error": "kind must be 'word' or 'sentence'"}, 400)
                return
            voice_id = body.get("voice_id")
            voice_name = body.get("voice_name", "")
            source_field = "expression" if kind == "word" else "example_jpn"
            text = tts_text(card.get(source_field, ""))
            if not text:
                self._json({"error": f"card has no {source_field} text to speak"}, 400)
                return
            dest = os.path.join(AUDIO_DIR, f"{card['id']}_{kind}.mp3")
            try:
                synthesize(text, voice_id, dest, language_code="ja")
            except Exception as e:
                self._json({"error": str(e)}, 400)
                return
            card[f"audio_{kind}_path"] = dest
            card[f"audio_{kind}_source"] = "elevenlabs"
            card[f"audio_{kind}_attribution"] = voice_name
            save_state(state)
            self._json({"audio_url": media_url("audio", dest)})
            return

        m = re.match(r"^/api/cards/([^/]+)/audio/remove$", path)
        if m:
            state = load_state()
            card = find_card(state, m.group(1))
            if not card:
                self._json({"error": "no such card"}, 404)
                return
            kind = body.get("kind")
            if kind not in ("word", "sentence"):
                self._json({"error": "kind must be 'word' or 'sentence'"}, 400)
                return
            key = f"audio_{kind}_path"
            if card.get(key) and os.path.exists(card[key]):
                os.remove(card[key])
            card[key] = None
            card[f"audio_{kind}_source"] = None
            card[f"audio_{kind}_attribution"] = None
            save_state(state)
            self._json({})
            return

        if path == "/api/push":
            self._json(push_approved())
            return

        self._json({"error": "not found"}, 404)


def serve(no_browser=False):
    os.makedirs(IMAGES_DIR, exist_ok=True)
    os.makedirs(AUDIO_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("localhost", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Review UI running at {url}")
    if not no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# ---------- CLI ----------

def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="start the review UI (default if no subcommand given)")
    p_serve.add_argument("--no-browser", action="store_true")

    sub.add_parser("push", help='push all "approved" cards into Anki and exit')

    p_isearch = sub.add_parser("images-search")
    p_isearch.add_argument("--query", required=True)
    p_isearch.add_argument("--source", choices=["irasutoya", "pixabay"], default="irasutoya")
    p_isearch.add_argument("--limit", type=int, default=6)

    p_idl = sub.add_parser("images-download")
    p_idl.add_argument("--url", required=True)
    p_idl.add_argument("--dest", required=True)

    sub.add_parser("audio-voices")

    p_speak = sub.add_parser("audio-speak")
    p_speak.add_argument("--text", required=True)
    p_speak.add_argument("--voice-id", required=True)
    p_speak.add_argument("--dest", required=True)
    p_speak.add_argument("--model-id", default=ELEVENLABS_DEFAULT_MODEL_ID)
    p_speak.add_argument("--language-code", default=None)

    args = parser.parse_args()

    try:
        if args.cmd in (None, "serve"):
            serve(no_browser=getattr(args, "no_browser", False))
        elif args.cmd == "push":
            _print(push_approved())
        elif args.cmd == "images-search":
            _print(get_image_candidates(args.query, args.source, args.limit))
        elif args.cmd == "images-download":
            _print({"saved": download_image(args.url, args.dest)})
        elif args.cmd == "audio-voices":
            _print(list_voices())
        elif args.cmd == "audio-speak":
            _print({"saved": synthesize(args.text, args.voice_id, args.dest, args.model_id, args.language_code)})
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
