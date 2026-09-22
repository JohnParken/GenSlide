"""Tests for dynamic skill discovery and hot-reloading."""
import os
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
_AGENTSCOPE_SRC = _REPO_ROOT / "backend"
_FRONTEND_DIR = _REPO_ROOT / "frontend"

for p in [str(_AGENTSCOPE_SRC), str(_FRONTEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from genslide_agentscope.skills import SkillRegistry
from autonomous_agent import AutonomousAgent


class TestDynamicSkills(unittest.TestCase):
    def test_default_skill_discovery_includes_all_skills(self):
        """Verify that default SkillRegistry loads all skills including official-document-skill."""
        registry = SkillRegistry()
        skill_ids = [s["skill_id"] for s in registry.list_skills()]
        self.assertIn("business-report", skill_ids)
        self.assertIn("document", skill_ids)
        self.assertIn("presentation", skill_ids)
        self.assertIn("writing", skill_ids)
        self.assertIn("official-document-skill", skill_ids)

        # Verify official-document-skill metadata and large instruction size
        official = registry.get("document", "official-document-skill")
        self.assertEqual(official["skill_id"], "official-document-skill")
        self.assertIn("公文", official["description"])
        instruction_len = len(official["instructions"]["generate"])
        self.assertGreater(instruction_len, 4000, "Should support instructions > 4000 characters")

    def test_dynamic_hot_reload_lifecycle(self):
        """Verify adding and removing a skill on disk is immediately reflected via reload()."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            # Create an initial skill
            skill_a = tmp_path / "skill-a"
            skill_a.mkdir()
            (skill_a / "SKILL.md").write_text(
                "---\nname: skill-a\ndescription: Skill A Description\n---\nInstruction for A",
                encoding="utf-8",
            )

            registry = SkillRegistry(root=tmp_path)
            skills = registry.list_skills()
            self.assertEqual(len(skills), 1)
            self.assertEqual(skills[0]["skill_id"], "skill-a")

            # Add a second skill on disk while registry is running
            skill_b = tmp_path / "skill-b"
            skill_b.mkdir()
            (skill_b / "SKILL.md").write_text(
                "---\nname: skill-b\ndescription: Skill B Description\n---\nInstruction for B",
                encoding="utf-8",
            )

            # Without reload, cached skills remain 1
            self.assertEqual(len(registry.skills), 1)

            # Call reload()
            refreshed = registry.reload()
            self.assertEqual(len(refreshed), 2)
            refreshed_ids = [s["skill_id"] for s in refreshed]
            self.assertIn("skill-a", refreshed_ids)
            self.assertIn("skill-b", refreshed_ids)

            # Delete skill-a
            (skill_a / "SKILL.md").unlink()
            skill_a.rmdir()

            refreshed_after_delete = registry.reload()
            self.assertEqual(len(refreshed_after_delete), 1)
            self.assertEqual(refreshed_after_delete[0]["skill_id"], "skill-b")

    def test_autonomous_agent_skill_injection(self):
        """Verify AutonomousAgent discovers skills and injects skill instructions into prompt."""
        agent = AutonomousAgent()
        skills = agent.list_skills()
        skill_ids = [s["skill_id"] for s in skills]
        self.assertIn("official-document-skill", skill_ids)

        # Test prompt construction in astep
        with patch.object(agent, "_call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = '{"thought": "test", "skill": "reply", "reply_text": "ok"}'
            agent.step("请帮我草拟一份关于开展专项治理的通知", skill_id="official-document-skill")

            self.assertTrue(mock_llm.called)
            system_prompt, user_prompt = mock_llm.call_args[0]
            # Ensure official-document-skill guidelines are injected into the prompt
            self.assertIn("official-document-skill", user_prompt)
            self.assertIn("公文", user_prompt)


if __name__ == "__main__":
    unittest.main()
