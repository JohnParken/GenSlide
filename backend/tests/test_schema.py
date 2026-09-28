import json
from pathlib import Path
from genslide_agentscope.domain import ExecuteRequest

def test_request_schema_matches_packaged_contract():
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "contracts" / "execute.schema.json").read_text())
    assert ExecuteRequest.model_json_schema() == schema

def test_repository_contract_matches_backend_copy():
    root = Path(__file__).resolve().parents[2]
    public = json.loads((root / "contracts/genslide-v1/execute.schema.json").read_text())
    packaged = json.loads((root / "backend/contracts/execute.schema.json").read_text())
    assert public == packaged == ExecuteRequest.model_json_schema()
