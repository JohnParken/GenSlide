"""CI-only parity check. Neither service imports this repository tool."""
from pathlib import Path
import tomllib
import json

ROOT = Path(__file__).resolve().parents[1] / "services"
ENGINES = ("langgraph", "agentscope")
COMMON = ("domain.py", "workflow.py", "skills.py", "content_io.py", "bff.py",
          "config.py", "execution.py", "api.py", "mock_bff.py", "tl_transport.py")


def main():
    for name in COMMON:
        sources = []
        for engine in ENGINES:
            path = ROOT / f"genslide-{engine}" / "src" / f"genslide_{engine}" / name
            text = path.read_text().replace(f"genslide_{engine}", "genslide_ENGINE")
            text = text.replace(f'"{engine}"', '"ENGINE"') if name != "domain.py" else text
            text = text.replace("LangGraph", "FRAMEWORK").replace("AgentScope", "FRAMEWORK")
            sources.append(text)
        if sources[0] != sources[1]:
            raise SystemExit(f"Shared contract/behavior drift: {name}")
    for kind in ("writing", "document", "presentation"):
        resources = [(ROOT / f"genslide-{e}" / "src" / f"genslide_{e}" / "skills" / f"{kind}.json").read_bytes() for e in ENGINES]
        if resources[0] != resources[1]:
            raise SystemExit(f"Skill drift: {kind}")
    manifests = [tomllib.loads((ROOT / f"genslide-{e}" / "pyproject.toml").read_text()) for e in ENGINES]
    common = [set(d for d in m["project"]["dependencies"] if not d.startswith(ENGINES)) for m in manifests]
    if common[0] != common[1]:
        raise SystemExit("Common dependency drift")
    schema = json.loads((ROOT.parent / "contracts/genslide-v1/execute.schema.json").read_text())
    for engine in ENGINES:
        copy = json.loads((ROOT / f"genslide-{engine}" / "contracts/execute.schema.json").read_text())
        if copy != schema:
            raise SystemExit(f"Request schema copy drift: {engine}")
    print("PASS: independent service contract, skill and dependency copies agree")


if __name__ == "__main__":
    main()
