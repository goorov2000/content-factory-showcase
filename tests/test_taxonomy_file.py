import json
from pathlib import Path


def test_taxonomy_json_exists_and_valid():
    data = json.loads(Path("prompts/agents/niche-taxonomy.json").read_text(encoding="utf-8"))
    assert "мужские-образы" in data["niches"]
    assert len(data["niches"]) >= 10
    assert len(data["niches"]) == len(set(data["niches"]))
