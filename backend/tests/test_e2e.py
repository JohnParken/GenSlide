"""HTTP handoff, cross-instance restoration and replay fences for assistant turns."""
from uuid import uuid4
import httpx
import pytest
from genslide_agentscope.bff import BFFClient
from genslide_agentscope.config import Settings
from genslide_agentscope.domain import ExecuteRequest, ServiceError
from genslide_agentscope.engine import Engine
from genslide_agentscope.execution import ExecutionRuntime
from genslide_agentscope.mock_bff import create_mock_bff
from genslide_agentscope.skills import SkillRegistry

TOKEN = "local-test-token-with-more-than-32-characters"

class Model:
    def __init__(self, kind="writing"):
        self.calls = []
        self.kind = kind

    async def complete(self, system, payload):
        self.calls.append(payload)
        return {
            "thought": "Generate requested deliverable via unified ReAct kernel.",
            "action": "final_reply",
            "action_input": {
                "effect": "deliverable",
                "target_kind": self.kind,
                "skill_id": self.kind,
                "reply": "完成",
                "deliverable": {
                    "title": "测试写作",
                    "sections": [
                        {"title": "主题介绍", "body": "这是完整正文，用于验证内容生成与交接。", "notes": "讲解主题"}
                    ],
                },
            },
        }

def app_for(monkeypatch, tmp_path):
    monkeypatch.setenv("GENSLIDE_ALLOW_MOCK", "1")
    monkeypatch.setenv("GENSLIDE_ENV", "test")
    monkeypatch.setenv("GENSLIDE_SERVICE_TOKEN", TOKEN)
    return create_mock_bff()

def create_test_skills(tmp_path):
    root = tmp_path / "skills"
    for kind in ("writing", "document", "presentation"):
        path = root / kind / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {kind}\ndescription: Test {kind} skill\nmetadata:\n  target_kind: {kind}\n---\nTest instructions", encoding="utf-8")
    return SkillRegistry(root)

def body(kind="writing", **extra):
    return dict(engine="agentscope", tenant_id="t", user_id="u", session_id="s", runtime_epoch="e",
                action_id=uuid4().hex, authorization="placeholder", expected_session_version=0,
                expected_lifecycle_version=1, mode="assistant",
                requested_output="text" if kind == "writing" else kind, message="直接出稿") | extra

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["writing", "document", "presentation"])
async def test_real_engine_bff_http_and_artifact_handoff(monkeypatch, tmp_path, kind):
    app = app_for(monkeypatch, tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bff/internal/genslide/v1",
                                headers={"Authorization": "Bearer " + TOKEN}) as http:
        settings = Settings(environment="test", service_token=TOKEN, bff_url=str(http.base_url),
                            workspace_root=tmp_path / "workspaces")
        model = Model(kind)
        engine = Engine(model)
        engine.skills = create_test_skills(tmp_path)
        runtime = ExecutionRuntime(BFFClient(settings, http), engine, settings)
        data = body(kind)
        response = await http.post("http://bff/dev/begin", json=data)
        assert response.status_code == 200, response.text
        request = ExecuteRequest.model_validate(response.json()["request"])
        try:
            result = await runtime.perform(await runtime.prepare(request))
            assert result["session_version"] == 1
            assert result["effect"] == "deliverable"
            assert len(model.calls) == 1
            assert len(result["files"]) == (0 if kind == "writing" else 1)
            assert not list((tmp_path / "workspaces").glob("ws-*"))
            for file in result["files"]:
                artifact = await http.get("http://bff/dev/artifacts/" + file["file_id"])
                assert artifact.status_code == 200 and artifact.content
            assert app.state.sessions[request.session_key()]["snapshot"]["content"] == result["content"]
            repeat = await http.post("http://bff/dev/begin", json=data)
            assert repeat.status_code == 200 and repeat.json()["status"] == "committed"
            with pytest.raises(ServiceError):
                await runtime.prepare(request)
            assert len(model.calls) == 1
        finally:
            await runtime.aclose()

@pytest.mark.asyncio
async def test_two_pods_unique_claim_and_trusted_snapshot_restore(monkeypatch, tmp_path):
    app = app_for(monkeypatch, tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bff/internal/genslide/v1",
                                headers={"Authorization": "Bearer " + TOKEN}) as http:
        settings = Settings(environment="test", service_token=TOKEN, bff_url=str(http.base_url),
                            workspace_root=tmp_path / "workspaces")
        one, two = Model(), Model()
        first_engine, second_engine = Engine(one), Engine(two)
        first_engine.skills = second_engine.skills = create_test_skills(tmp_path)
        first = ExecutionRuntime(BFFClient(settings, http), first_engine, settings)
        second = ExecutionRuntime(BFFClient(settings, http), second_engine, settings)
        async def begin(data):
            response = await http.post("http://bff/dev/begin", json=data)
            assert response.status_code == 200, response.text
            return ExecuteRequest.model_validate(response.json()["request"])
        try:
            request = await begin(body())
            prepared = await first.prepare(request)
            with pytest.raises(ServiceError):
                await second.prepare(request)
            assert app.state.actions[request.action_id]["status"] == "active"
            result = await first.perform(prepared)
            next_request = await begin(body(expected_session_version=1, message="修改当前稿"))
            restored = await second.prepare(next_request)
            assert restored.memory.content.model_dump() == result["content"]
            next_result = await second.perform(restored)
            assert next_result["session_version"] == 2
            assert two.calls[0]["current_content"] == result["content"]
        finally:
            await first.aclose()
            await second.aclose()

@pytest.mark.asyncio
@pytest.mark.parametrize("commit_first", [False, True])
async def test_deletion_blocks_late_commit_and_old_receipt(monkeypatch, tmp_path, commit_first):
    app = app_for(monkeypatch, tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bff/internal/genslide/v1",
                                headers={"Authorization": "Bearer " + TOKEN}) as http:
        settings = Settings(environment="test", service_token=TOKEN, bff_url=str(http.base_url),
                            workspace_root=tmp_path / "workspaces")
        engine = Engine(Model())
        engine.skills = create_test_skills(tmp_path)
        runtime = ExecutionRuntime(BFFClient(settings, http), engine, settings)
        data = body()
        authorized = (await http.post("http://bff/dev/begin", json=data)).json()["request"]
        request = ExecuteRequest.model_validate(authorized)
        prepared = await runtime.prepare(request)
        if commit_first:
            await runtime.perform(prepared)
        deleted = await http.post("http://bff/dev/sessions/s/delete", json={"tenant_id": "t", "user_id": "u"})
        assert deleted.status_code == 200
        if not commit_first:
            with pytest.raises(ServiceError):
                await runtime.perform(prepared)
        assert app.state.sessions[request.session_key()]["version"] == int(commit_first)
        repeat = await http.post("http://bff/dev/begin", json=data)
        assert repeat.status_code == 410
        assert not list((tmp_path / "workspaces").glob("ws-*"))
        assert runtime.metrics()["generation_active"] == 0
        await runtime.aclose()

@pytest.mark.asyncio
async def test_workspace_creation_failure_settles_and_releases_action(monkeypatch, tmp_path):
    app = app_for(monkeypatch, tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bff/internal/genslide/v1",
                                headers={"Authorization": "Bearer " + TOKEN}) as http:
        settings = Settings(environment="test", service_token=TOKEN, bff_url=str(http.base_url),
                            workspace_root=tmp_path / "workspaces", workspace_max_bytes=1)
        model = Model()
        engine = Engine(model)
        engine.skills = create_test_skills(tmp_path)
        runtime = ExecutionRuntime(BFFClient(settings, http), engine, settings)
        request = ExecuteRequest.model_validate((await http.post("http://bff/dev/begin", json=body())).json()["request"])
        prepared = await runtime.prepare(request)
        assert prepared.context.current_file_ids == ()
        assert prepared.context.lifecycle_version == 1
        with pytest.raises(ServiceError, match="WORKSPACE_CAPACITY"):
            await runtime.perform(prepared)
        assert not model.calls
        assert app.state.actions[request.action_id]["status"] == "closed"
        assert runtime.metrics()["generation_active"] == 0
        await runtime.aclose()


@pytest.mark.asyncio
async def test_multi_step_react_and_cross_pod_goal_ledger_snapshot_handoff(monkeypatch, tmp_path):
    """Verify default ExecutionRuntime -> Engine -> 8-Phase RuntimeEngine -> ReActAgent multi-step loop and cross-pod GoalLedger restore."""
    app = app_for(monkeypatch, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://bff/internal/genslide/v1",
        headers={"Authorization": "Bearer " + TOKEN},
    ) as http:
        settings = Settings(
            environment="test",
            service_token=TOKEN,
            bff_url=str(http.base_url),
            workspace_root=tmp_path / "workspaces",
        )

        class Pod1Model:
            def __init__(self):
                self.step = 0

            async def complete(self, system, payload):
                self.step += 1
                if self.step == 1:
                    return {
                        "thought": "Decompose mission into 2 milestones.",
                        "action": "plan_tasks",
                        "action_input": {
                            "tasks": [
                                {"title": "Analyze market data"},
                                {"title": "Draft final report"},
                            ]
                        },
                    }
                if self.step == 2:
                    obs = payload["history"][0]["observation"]
                    first_tid = obs.split("['")[1].split("'")[0]
                    return {
                        "thought": "Complete milestone 1.",
                        "action": "update_task",
                        "action_input": {
                            "task_id": first_tid,
                            "status": "completed",
                            "summary": "Market size is 50B",
                        },
                    }
                return {
                    "thought": "Pause after milestone 1 and ask user for confirmation.",
                    "action": "final_reply",
                    "action_input": {
                        "effect": "reply",
                        "target_kind": "writing",
                        "skill_id": "writing",
                        "reply": "已完成第一阶段数据分析（市场规模50B），是否继续撰写最终报告？",
                    },
                }

        class Pod2Model:
            def __init__(self):
                self.received_prompts = []

            async def complete(self, system, payload):
                self.received_prompts.append(system)
                return {
                    "thought": "Milestone 1 was restored from BFF snapshot; now deliver final report.",
                    "action": "final_reply",
                    "action_input": {
                        "effect": "deliverable",
                        "target_kind": "writing",
                        "skill_id": "writing",
                        "reply": "已为您完成最终报告。",
                        "deliverable": {
                            "title": "市场分析报告",
                            "sections": [{"title": "核心结论", "body": "市场规模达50B。", "notes": ""}],
                        },
                    },
                }

        pod1_engine = Engine(Pod1Model())
        pod2_model = Pod2Model()
        pod2_engine = Engine(pod2_model)
        pod1_engine.skills = pod2_engine.skills = create_test_skills(tmp_path)

        pod1_runtime = ExecutionRuntime(BFFClient(settings, http), pod1_engine, settings)
        pod2_runtime = ExecutionRuntime(BFFClient(settings, http), pod2_engine, settings)

        async def begin(data):
            response = await http.post("http://bff/dev/begin", json=data)
            assert response.status_code == 200, response.text
            return ExecuteRequest.model_validate(response.json()["request"])

        try:
            # Turn 1 on Pod 1
            req1 = await begin(body(message="帮我调研市场并撰写报告"))
            res1 = await pod1_runtime.perform(await pod1_runtime.prepare(req1))
            assert res1["effect"] == "reply"
            assert res1["result"]["react_iterations"] == 3
            assert "ledger" in res1["snapshot"]
            assert len(res1["snapshot"]["ledger"]["tasks"]) == 2
            assert res1["snapshot"]["ledger"]["tasks"][0]["status"] == "completed"
            assert res1["snapshot"]["ledger"]["tasks"][1]["status"] == "pending"

            # Turn 2 on Pod 2 (stateless cross-pod restoration from BFF snapshot)
            req2 = await begin(body(expected_session_version=1, message="继续撰写最终报告"))
            prepared2 = await pod2_runtime.prepare(req2)
            assert prepared2.memory.ledger is not None
            assert len(prepared2.memory.ledger.tasks) == 2
            assert prepared2.memory.ledger.tasks[0].result_summary == "Market size is 50B"

            res2 = await pod2_runtime.perform(prepared2)
            assert res2["session_version"] == 2
            assert res2["effect"] == "deliverable"
            # Verify P60 GoalLedgerContributor injected the restored milestones into Pod 2's system prompt
            assert "Analyze market data" in pod2_model.received_prompts[0]
            assert "Draft final report" in pod2_model.received_prompts[0]
        finally:
            await pod1_runtime.aclose()
            await pod2_runtime.aclose()
