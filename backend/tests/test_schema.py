import json
from pathlib import Path
from genslide_agentscope.domain import ExecuteRequest

def test_request_schema_matches_packaged_contract():
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "contracts" / "execute.schema.json").read_text())
    assert ExecuteRequest.model_json_schema() == schema

