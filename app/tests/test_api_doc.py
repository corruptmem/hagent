"""``docs/api.md`` names every route that the app serves."""

from pathlib import Path

from hagent_app.app import default_components
from hagent_app.http import HttpServer

DOC = Path(__file__).resolve().parents[2] / "docs" / "api.md"


def test_api_doc_names_every_route() -> None:
    components = default_components()
    http = components[-1]
    assert isinstance(http, HttpServer)
    text = DOC.read_text(encoding="utf-8")
    paths = {r.canonical for r in http.make_app().router.resources()}
    missing = []
    for path in paths:
        # The doc writes paths relative to the Ingress base, with {id} and
        # {version} in place of the route patterns.
        doc_path = (
            path.lstrip("/")
            .replace("{agent_id}", "{id}")
            .replace("{request_id}", "{id}")
        )
        if doc_path not in text:
            missing.append(doc_path)
    assert not missing
