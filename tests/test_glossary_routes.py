import json
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import glossary as glossary_routes


@pytest.fixture
def glossary_client():
    with tempfile.TemporaryDirectory() as temp_dir:
        glossary_dir = Path(temp_dir)
        original_glossary_dir = glossary_routes.service.glossary_dir
        glossary_routes.service.glossary_dir = glossary_dir
        glossary_routes.service.glossary_dir.mkdir(parents=True, exist_ok=True)

        app = FastAPI()
        app.include_router(glossary_routes.router)
        try:
            yield TestClient(app), glossary_dir
        finally:
            glossary_routes.service.glossary_dir = original_glossary_dir


def test_create_glossary_accepts_json_body(glossary_client):
    client, glossary_dir = glossary_client
    response = client.post(
        "/api/glossary/",
        json={"name": "My Terms", "terms": {"Hyperstition": "超虚构"}},
    )

    assert response.status_code == 200
    assert response.json()["id"] == "my_terms"
    saved = json.loads((glossary_dir / "my_terms.json").read_text(encoding="utf-8"))
    assert saved["Hyperstition"] == "超虚构"


def test_update_glossary_accepts_wrapped_terms_body(glossary_client):
    client, glossary_dir = glossary_client
    (glossary_dir / "review_case.json").write_text(
        json.dumps({"A": "B"}, ensure_ascii=False),
        encoding="utf-8",
    )

    response = client.put(
        "/api/glossary/review_case",
        json={"terms": {"A": "C"}},
    )

    assert response.status_code == 200
    saved = json.loads((glossary_dir / "review_case.json").read_text(encoding="utf-8"))
    assert saved == {"A": "C"}


def test_import_glossary_uses_form_name(glossary_client):
    client, glossary_dir = glossary_client
    response = client.post(
        "/api/glossary/import",
        files={"file": ("demo.json", b'{"A":"B"}', "application/json")},
        data={"name": "custom_name"},
    )

    assert response.status_code == 200
    assert response.json()["id"] == "custom_name"
    assert (glossary_dir / "custom_name.json").exists()


def test_glossary_names_with_spaces_punctuation_and_chinese_are_accepted(tmp_path):
    import asyncio

    from src.infrastructure.persistence.glossary_repository import GlossaryService

    service = GlossaryService()
    service.glossary_dir = tmp_path
    created = asyncio.run(service.create_glossary("Cyclonopedia & Ccru 术语表", {"Hyperstition": "超虚构"}))
    assert created["id"] == "cyclonopedia_ccru_术语表"
    assert asyncio.run(service.get_glossary(created["id"]))["terms"] == {"Hyperstition": "超虚构"}
