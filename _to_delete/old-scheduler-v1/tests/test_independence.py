"""
The scheduler must not import the application.

This is the test that keeps the folder deployable on its own. Everything else
here checks that the scheduler works; this checks that it works ALONE, which
is a different property and the one most easily lost by a single convenient
import added in a hurry.

Two angles, because either alone can be fooled:

  * Static - read every source file and look at what it imports. Catches an
    import added inside a function, which an import-time check would miss
    until that function ran at 2am.

  * Dynamic - import every scheduler module in a fresh interpreter with the
    application's packages blocked, and confirm they all load.
"""

import ast
import pathlib
import subprocess
import sys

SCHEDULER_DIR = pathlib.Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCHEDULER_DIR.parent

# Top-level packages that belong to the application. Importing any of them
# from this folder is what this test exists to prevent.
APP_PACKAGES = {"app", "core", "models", "schemas", "services", "helpers", "routers"}

SCHEDULER_MODULES = [
    "scheduler.config", "scheduler.db", "scheduler.models", "scheduler.money",
    "scheduler.audit", "scheduler.settings", "scheduler.errors",
    "scheduler.logging_setup", "scheduler.service", "scheduler.master",
    "scheduler.task_runner", "scheduler.billing.rent", "scheduler.billing.penalty",
    "scheduler.tasks.rent_generation", "scheduler.tasks.due_date_penalty",
    "scheduler.tasks.future_task_checker",
]


def _source_files():
    for path in sorted(SCHEDULER_DIR.rglob("*.py")):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        yield path


def test_no_source_file_imports_the_application():
    """Static: nothing under scheduler/ names an application package."""
    offenders = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # level > 0 is a relative import, which cannot reach the app.
                names = [] if node.level else [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                if name in APP_PACKAGES:
                    offenders.append(
                        f"{path.relative_to(PROJECT_ROOT)}:{node.lineno} imports {name!r}"
                    )
    assert not offenders, (
        "the scheduler must not import the application:\n  " + "\n  ".join(offenders)
    )


def test_every_module_imports_with_the_application_unavailable():
    """
    Dynamic: load every scheduler module in a subprocess where importing any
    application package raises. Proves the folder runs on a box that has the
    scheduler and nothing else.
    """
    program = f"""
import sys

BLOCKED = {sorted(APP_PACKAGES)!r}

class Blocker:
    def find_module(self, name, path=None):
        return self if name.split(".")[0] in BLOCKED else None
    def load_module(self, name):
        raise ImportError(f"{{name}} is the application and must not be imported here")

sys.meta_path.insert(0, Blocker())

for module in {SCHEDULER_MODULES!r}:
    __import__(module)

print("ok")
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        "a scheduler module could not be imported without the application:\n"
        + result.stdout + result.stderr
    )
    assert "ok" in result.stdout


def test_scheduler_requirements_exclude_the_web_stack():
    """
    The dependency list is part of independence: the scheduler is not a web
    service and should not drag one in. A `pip install -r
    scheduler/requirements.txt` on a bare box must not pull FastAPI.
    """
    text = (SCHEDULER_DIR / "requirements.txt").read_text(encoding="utf-8").lower()
    declared = [
        line.split("#")[0].strip().split("==")[0]
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    for unwanted in ("fastapi", "uvicorn", "razorpay", "python-jose", "passlib",
                     "python-multipart", "httpx", "pydantic", "bcrypt"):
        assert unwanted not in declared, f"{unwanted} is not needed to run the scheduler"
