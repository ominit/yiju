import struct

from flask.testing import FlaskClient


def test_manifest_provides_standalone_study_app_and_valid_icons(
    client: FlaskClient,
) -> None:
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.mimetype == "application/manifest+json"
    manifest = response.get_json()
    assert manifest["id"] == "/"
    assert manifest["start_url"] == "/study"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert f'name="theme-color" content="{manifest["theme_color"]}"' in client.get(
        "/"
    ).get_data(as_text=True)
    assert {icon["purpose"] for icon in manifest["icons"]} == {"any", "maskable"}
    assert {icon["sizes"] for icon in manifest["icons"]} == {"192x192", "512x512"}
    for icon in manifest["icons"]:
        image = client.get(icon["src"])
        assert image.status_code == 200
        assert image.mimetype == "image/png"
        assert image.data[:8] == b"\x89PNG\r\n\x1a\n"
        size = tuple(int(value) for value in icon["sizes"].split("x"))
        assert struct.unpack(">II", image.data[16:24]) == size


def test_pages_include_install_metadata_and_settings_controls(
    client: FlaskClient,
) -> None:
    page = client.get("/settings").get_data(as_text=True)
    assert 'rel="manifest" href="/manifest.webmanifest"' in page
    assert 'rel="apple-touch-icon"' in page
    assert 'name="apple-mobile-web-app-capable" content="yes"' in page
    assert "data-install" in page
    icon = client.get("/static/icons/apple-touch-icon.png")
    assert icon.status_code == 200
    assert struct.unpack(">II", icon.data[16:24]) == (180, 180)
