import json

import pytest

from alx.prod.config import load_config


def test_config_reads_only_the_supplied_json_file(tmp_path, monkeypatch):
    config_path = tmp_path / "settings.json"
    expected = {"limit": 3.3, "enabled": True, "name": "sample", "options": {"ports": [1, 2]}}
    config_path.write_text(json.dumps(expected), encoding="utf-8")
    (tmp_path / "settings.local.json").write_text('{"limit": 99}', encoding="utf-8")
    monkeypatch.setenv("LIMIT", "99")
    monkeypatch.setenv("ALX_PROD_CONFIG", str(tmp_path / "settings.local.json"))
    assert load_config(config_path) == expected
    assert load_config(str(config_path)) == expected


def test_config_accepts_an_empty_object(tmp_path):
    config_path = tmp_path / "settings.json"
    config_path.write_text("{}", encoding="utf-8")
    assert load_config(config_path) == {}


@pytest.mark.parametrize("document", ["[]", "null", "1", "true", '"settings"'])
def test_config_rejects_nonobject_documents(tmp_path, document):
    config_path = tmp_path / "settings.json"
    config_path.write_text(document, encoding="utf-8")
    with pytest.raises(ValueError, match="object"):
        load_config(config_path)


@pytest.mark.parametrize(
    "document",
    [
        '{"limit":',
        '{"limit": 1,}',
        '{"limit": 1} {"limit": 2}',
        '{"limit": NaN}',
        '{"limit": Infinity}',
        '{"limit": -Infinity}',
        '{"limit": 1, "limit": 2}',
        '{"nested": {"limit": 1, "limit": 2}}',
    ],
)
def test_config_rejects_invalid_or_ambiguous_json(tmp_path, document):
    config_path = tmp_path / "settings.json"
    config_path.write_text(document, encoding="utf-8")
    with pytest.raises(
        ValueError,
        match=r"Expecting|Extra data|[Dd]uplicate|[Nn]on.?finite|[Ii]nvalid|NaN|Infinity",
    ):
        load_config(config_path)


def test_config_does_not_replace_a_missing_file_with_defaults(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "missing.json")
