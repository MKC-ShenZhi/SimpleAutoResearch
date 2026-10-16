"""Small keyword-project data shared by code, application and plan tests."""

from pathlib import Path

from simple_ar.core.artifacts import write_text


TOY_PREDICT_SOURCE = (
    "def predict(text):\n"
    "    return 'spam' if 'win' in text.lower() else 'ham'\n"
)
TOY_PREDICT_WITH_PRIZE = (
    "def predict(text):\n"
    "    lowered = text.lower()\n"
    "    return 'spam' if any(keyword in lowered for keyword in ('win', 'prize')) else 'ham'\n"
)


def write_toy_project(code_root: Path) -> None:
    write_text(
        code_root / "spam_model.py",
        (
            "import math\n\n\n"
            "class SpamModel:\n"
            "    def score(self, text):\n"
            "        return math.log(len(text) + 1)\n\n\n"
            "def predict(text):\n"
            "    return 'spam' if 'win' in text.lower() else 'ham'\n\n\n"
            "if __name__ == \"__main__\":\n"
            "    print(predict('win a prize'))\n"
        ),
    )
    write_keyword_validation_test(code_root)
    write_text(code_root / ".git" / "config", "[core]\nrepositoryformatversion = 0\n")
    write_text(code_root / ".env", "TOKEN=secret\n")
    write_text(
        code_root / "pyproject.toml",
        "[project]\nname = \"toy-project\"\nversion = \"0.1.0\"\n",
    )


def write_keyword_validation_test(code_root: Path) -> None:
    write_text(
        code_root / "tests" / "test_spam_model.py",
        (
            "import unittest\n\n"
            "from spam_model import predict\n\n\n"
            "class SpamModelTests(unittest.TestCase):\n"
            "    def test_predicts_spam_keyword(self):\n"
            "        self.assertEqual(predict('win now'), 'spam')\n"
        ),
    )


def keyword_code_responses():
    """Fresh explicit payloads; each test owns dispatch, failures and revisions."""
    return {
        "code-task-work-plan": {
            "summary": "Improve prize-message classification in one small batch.",
            "goal": "Classify prize messages as spam without changing tests.",
            "success_criteria": ["Benchmark passes after the patch."],
            "items": [{
                "id": "W1", "objective": "Implement a small keyword handling improvement.",
                "target_files": ["spam_model.py"],
                "read_only_evidence": ["tests/test_spam_model.py"], "depends_on": [],
                "validation": ["python -m unittest discover -s tests"],
                "done_criteria": ["Prediction handles prize messages."], "risk": "Low.",
                "parallelizable": False, "budget_profile": "normal",
                "requires_budget_override": False, "suggested_budget_override": "",
                "context_request": {"query": "predict prize", "files": ["spam_model.py"], "symbols": ["predict"]},
            }],
            "context_requests": [], "risks": [],
            "approval": {"required": True, "reason": "Review before editing."},
        },
        "code-task-plan": {
            "summary": "Patch the classifier keyword logic.",
            "goals": ["Handle prize messages as spam."],
            "files_to_modify": [{"path": "spam_model.py", "reason": "Contains predict keyword logic.", "change_type": "modify"}],
            "new_files": [], "proposed_steps": ["Update predict keyword check."],
            "validation": ["python -m unittest discover -s tests"],
            "risks": ["Keep API stable."], "rollback": ["Discard workspace changes."],
            "open_questions": [], "requires_approval_before_patch": True,
        },
        "code-task-propose-edits": {
            "summary": "Add prize keyword support.",
            "edits": [{"path": "spam_model.py", "old": TOY_PREDICT_SOURCE,
                       "new": TOY_PREDICT_WITH_PRIZE, "reason": "Classify prize messages as spam."}],
            "validation": ["python -m unittest discover -s tests"], "risks": [],
        },
    }
