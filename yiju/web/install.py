from flask import Blueprint, Response, jsonify, url_for

install = Blueprint("install", __name__)


@install.get("/manifest.webmanifest")
def manifest() -> Response:
    response = jsonify(
        id=url_for("pages.home"),
        name="Yiju · 一句",
        short_name="Yiju",
        description="Chinese vocabulary and sentence study",
        lang="en",
        start_url=url_for("pages.study"),
        scope=url_for("pages.home"),
        display="standalone",
        background_color="#101417",
        theme_color="#101417",
        icons=[
            {
                "src": url_for("static", filename=f"icons/{filename}.png"),
                "sizes": size,
                "type": "image/png",
                "purpose": purpose,
            }
            for filename, size, purpose in (
                ("icon-192", "192x192", "any"),
                ("icon-512", "512x512", "any"),
                ("icon-maskable-512", "512x512", "maskable"),
            )
        ],
    )
    response.mimetype = "application/manifest+json"
    return response
