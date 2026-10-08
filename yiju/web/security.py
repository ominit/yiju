import gzip
import secrets
from http import HTTPStatus

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.exceptions import HTTPException

from yiju.storage.learning import ConflictError

MIN_COMPRESSION_SIZE = 1024


def protect_forms() -> None:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    if request.method == "POST" and not secrets.compare_digest(
        str(session["csrf"]).encode(),
        request.form.get("csrf", "").encode(),
    ):
        abort(400, "This form has expired. Reload the page and try again.")


def template_context() -> dict[str, str]:
    endpoint = request.endpoint or ""
    if endpoint in {"pages.sentences", "pages.sentence_detail", "pages.library"}:
        endpoint = "pages.library"
    navigation = (
        url_for(endpoint)
        if endpoint
        in {"pages.home", "pages.study", "pages.library", "pages.preferences"}
        else ""
    )
    return {"csrf": session.get("csrf", ""), "navigation": navigation}


def response_headers(response: Response) -> Response:
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store"
        response.vary.add("X-Yiju-Partial")
        response.vary.add("X-Yiju-Buffer")
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
    if request.headers.get("X-Yiju-Partial") in {"study", "page"}:
        return jsonify(error=error.description), error.code or 500
    return render_template(
        "error.html", message=error.description, title=error.name
    ), error.code or 500


def conflict(error: ConflictError) -> tuple[str | Response, int]:
    if request.headers.get("X-Yiju-Partial") in {"study", "page"}:
        return jsonify(error=str(error)), 409
    return render_template(
        "error.html", message=str(error), title="Something changed"
    ), 409


def register_handlers(app: Flask) -> None:
    app.before_request(protect_forms)
    app.context_processor(template_context)
    app.after_request(response_headers)
    app.register_error_handler(HTTPException, http_error)
    app.register_error_handler(ConflictError, conflict)
