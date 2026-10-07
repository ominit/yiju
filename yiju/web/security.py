import secrets

from flask import Flask, Response, abort, jsonify, render_template, request, session
from werkzeug.exceptions import HTTPException

from yiju.storage.learning import ConflictError


def protect_forms() -> None:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    if request.method == "POST" and not secrets.compare_digest(
        str(session["csrf"]).encode(),
        request.form.get("csrf", "").encode(),
    ):
        abort(400, "This form has expired. Reload the page and try again.")


def template_context() -> dict[str, str]:
    return {"csrf": session.get("csrf", "")}


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
    return response


def http_error(error: HTTPException) -> tuple[str | Response, int]:
    if request.headers.get("X-Yiju-Partial") == "study":
        return jsonify(error=error.description), error.code or 500
    return render_template(
        "error.html", message=error.description, title=error.name
    ), error.code or 500


def conflict(error: ConflictError) -> tuple[str | Response, int]:
    if request.headers.get("X-Yiju-Partial") == "study":
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
