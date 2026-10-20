import gzip
import secrets
from http import HTTPStatus

from flask import Flask, Response, abort, jsonify, render_template, request, session
from werkzeug.exceptions import HTTPException

from yiju.core.dictionary import DictionaryUnavailableError
from yiju.storage.database import ConflictError

MIN_COMPRESSION_SIZE = 1024
SECTIONS = {
    "pages.home": "home",
    "study.page": "study",
    "pages.library": "library",
    "pages.sentences": "library",
    "pages.word_detail": "library",
    "pages.sentence_detail": "library",
    "pages.import_page": "library",
    "pages.pending_page": "library",
    "pages.about": "settings",
    "pages.preferences": "settings",
}


def wants_json() -> bool:
    return (
        request.path.startswith("/api/")
        or bool(request.headers.get("X-Fragment"))
        or request.is_json
        or request.accept_mimetypes.best == "application/json"
    )


def protect_forms() -> None:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    if request.method == "POST":
        token = request.headers.get("X-CSRF-Token") or request.form.get("csrf", "")
        if not secrets.compare_digest(str(session["csrf"]).encode(), token.encode()):
            abort(400, "This page has expired. Reload it and try again.")


def template_context() -> dict[str, str]:
    return {
        "csrf": session.get("csrf", ""),
        "section": SECTIONS.get(request.endpoint or "", ""),
    }


def response_headers(response: Response) -> Response:
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; media-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store"
    response.vary.add("Accept-Encoding")
    if (
        request.accept_encodings["gzip"] > 0
        and response.status_code == HTTPStatus.OK
        and not response.direct_passthrough
        and response.mimetype in {"text/html", "application/json"}
    ):
        data = response.get_data()
        if len(data) >= MIN_COMPRESSION_SIZE:
            response.set_data(gzip.compress(data, compresslevel=1, mtime=0))
            response.headers["Content-Encoding"] = "gzip"
    return response


def http_error(error: HTTPException) -> tuple[str | Response, int]:
    if wants_json():
        return jsonify(error=error.description), error.code or 500
    return render_template(
        "error.html", message=error.description, title=error.name
    ), error.code or 500


def conflict(error: ConflictError) -> tuple[str | Response, int]:
    if wants_json():
        return jsonify(error=str(error)), 409
    return render_template(
        "error.html", message=str(error), title="Something changed"
    ), 409


def unavailable(error: DictionaryUnavailableError) -> tuple[str | Response, int]:
    if wants_json():
        return jsonify(error=str(error)), 503
    return render_template(
        "error.html", message=str(error), title="Dictionary unavailable"
    ), 503


def register_handlers(app: Flask) -> None:
    app.before_request(protect_forms)
    app.context_processor(template_context)
    app.after_request(response_headers)
    app.register_error_handler(HTTPException, http_error)
    app.register_error_handler(ConflictError, conflict)
    app.register_error_handler(DictionaryUnavailableError, unavailable)
