"""Unit tests for v2.12 Milestone 3: role-based review routing and desktop UI meta alignment."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from simple_ar.app.task_view import saved_model_recipients, saved_work_connection
from simple_ar.integrations.llm import LLMClient
from simple_ar.integrations.model_profiles import ModelCatalog


class M3RoleRoutingAndUITests(unittest.TestCase):
    def _catalog(self, *, with_review: bool = True, with_writing: bool = True) -> ModelCatalog:
        routes = {"default": "main", "code": "coder"}
        if with_writing:
            routes["writing"] = "writer"
        if with_review:
            routes["review"] = "reviewer"
        return ModelCatalog.model_validate(
            {
                "version": 1,
                "profiles": {
                    "main": {
                        "api": "openai_chat",
                        "base_url": "https://api.example.com/v1",
                        "model": "gpt-main",
                        "api_key_env": "TEST_KEY",
                        "capabilities": ["text", "code"],
                    },
                    "coder": {
                        "api": "openai_chat",
                        "base_url": "https://api.example.com/v1",
                        "model": "gpt-code",
                        "api_key_env": "TEST_KEY",
                        "capabilities": ["text", "code"],
                    },
                    "writer": {
                        "api": "openai_chat",
                        "base_url": "https://api.example.com/v1",
                        "model": "gpt-writer",
                        "api_key_env": "TEST_KEY",
                        "capabilities": ["text"],
                    },
                    "reviewer": {
                        "api": "openai_chat",
                        "base_url": "https://api.example.com/v1",
                        "model": "gpt-reviewer",
                        "api_key_env": "TEST_KEY",
                        "capabilities": ["text"],
                    },
                },
                "routes": routes,
            }
        )

    def test_review_route_selects_dedicated_profile_and_falls_back_cleanly(self) -> None:
        cat = self._catalog(with_review=True, with_writing=True)
        name, prof = cat.select(purpose="review")
        self.assertEqual((name, prof.model), ("reviewer", "gpt-reviewer"))

        with patch.dict(os.environ, {"TEST_KEY": "sk-test"}, clear=False):
            parent = LLMClient.from_settings_and_catalog if hasattr(LLMClient, "from_settings_and_catalog") else None
            _ = parent
            from simple_ar.integrations.llm import LLMSettings

            base_client = LLMClient(
                LLMSettings(**cat.profiles["main"].text_settings()),
                model_catalog=cat,
                profile_name="main",
                purpose="text",
            )
            binding = base_client.connection_binding()
            self.assertEqual(binding["review_route"]["profile"], "reviewer")
            self.assertEqual(saved_work_connection(binding, "review")["profile"], "reviewer")
            self.assertEqual(saved_model_recipients(binding)["review"]["model"], "gpt-reviewer")

            review_client = LLMClient.for_task(client=base_client, purpose="review")
            self.assertEqual(review_client.model, "gpt-reviewer")

            # Without explicit review route, falls back to writing route then main profile,
            # and leaves review_route out of connection_binding for snapshot compatibility.
            cat_no_review = self._catalog(with_review=False, with_writing=True)
            base_no_rev = LLMClient(
                LLMSettings(**cat_no_review.profiles["main"].text_settings()),
                model_catalog=cat_no_review,
                profile_name="main",
                purpose="text",
            )
            binding_no_rev = base_no_rev.connection_binding()
            self.assertNotIn("review_route", binding_no_rev)
            self.assertNotIn("review", saved_model_recipients(binding_no_rev))
            self.assertEqual(saved_work_connection(binding_no_rev, "review")["profile"], "writer")
            self.assertEqual(LLMClient.for_task(client=base_no_rev, purpose="review").model, "gpt-writer")

            cat_minimal = self._catalog(with_review=False, with_writing=False)
            base_min = LLMClient(
                LLMSettings(**cat_minimal.profiles["main"].text_settings()),
                model_catalog=cat_minimal,
                profile_name="main",
                purpose="text",
            )
            self.assertEqual(LLMClient.for_task(client=base_min, purpose="review").model, "gpt-main")

    def test_ui_creation_defaults_and_vision_route_detection(self) -> None:
        from simple_ar.ui import Workspace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "proj"
            tasks = root / "tasks"
            proj.mkdir()
            ws = Workspace(proj, [], tasks)
            defaults = ws.creation_defaults()
            self.assertIn("sandbox_gpu", defaults)
            self.assertFalse(defaults["sandbox_gpu"])

            vision_cat = ModelCatalog.model_validate(
                {
                    "version": 1,
                    "profiles": {
                        "v1": {
                            "api": "openai_chat",
                            "base_url": "https://api.example.com/v1",
                            "model": "gpt-vision",
                            "api_key_env": "TEST_VIS_KEY",
                            "capabilities": ["text", "vision"],
                        }
                    },
                    "routes": {"default": "v1", "vision": "v1"},
                }
            )
            with patch("simple_ar.ui.load_model_catalog", return_value=vision_cat), \
                    patch.dict(os.environ, {"TEST_VIS_KEY": "sk-vis"}, clear=False):
                status = ws.vision_route_status()
                self.assertEqual(status, {"available": True, "profile": "v1", "model": "gpt-vision"})


if __name__ == "__main__":
    unittest.main()
