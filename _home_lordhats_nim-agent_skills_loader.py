"""
Dynamic Skill Loader — discovers and loads skills from built-in and custom directories.
"""

import importlib
import importlib.util
import inspect
import logging
from pathlib import Path
from skills.base import Skill

logger = logging.getLogger("nim_agent.skill_loader")


class SkillLoader:
    """Loads skill classes from the skills/ and custom_skills/ directories."""

    def __init__(self, builtin_dir: Path, custom_dir: Path):
        self.builtin_dir = builtin_dir
        self.custom_dir = custom_dir
        self.skills: list[Skill] = []
        self._skill_map: dict[str, Skill] = {}

    def load_all(self):
        """Scan both directories and load all skills."""
        self.skills.clear()
        self._skill_map.clear()

        # Load built-in skills
        self._load_from_directory(self.builtin_dir, package="skills")

        # Load custom skills
        if self.custom_dir.exists():
            self._load_from_directory(self.custom_dir, package=None)

        logger.info("Loaded %d skills total", len(self.skills))

    def _load_from_directory(self, directory: Path, package: str | None = None):
        """Load all Skill subclasses from .py files in a directory."""
        for py_file in directory.glob("*.py"):
            if py_file.name.startswith("_") or py_file.name in ("base.py", "loader.py"):
                continue

            try:
                if package:
                    # Import as part of the package (built-in skills)
                    module_name = f"{package}.{py_file.stem}"
                    module = importlib.import_module(module_name)
                else:
                    # Import standalone (custom skills)
                    spec = importlib.util.spec_from_file_location(
                        f"custom_skill_{py_file.stem}", str(py_file)
                    )
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                # Find all Skill subclasses in the module
                for _, obj in inspect.getmembers(module, inspect.isclass):
                    if issubclass(obj, Skill) and obj is not Skill:
                        instance = obj()
                        if instance.name not in self._skill_map:
                            self.skills.append(instance)
                            self._skill_map[instance.name] = instance
                            logger.info("Loaded skill: %s (%s)", instance.name, py_file.name)

            except Exception as e:
                logger.error("Failed to load skill from %s: %s", py_file, e)

    def reload_custom(self):
        """Hot-reload custom skills only (for /install command)."""
        # Remove existing custom skills
        builtin_names = set()
        for py_file in self.builtin_dir.glob("*.py"):
            if py_file.name.startswith("_") or py_file.name in ("base.py", "loader.py"):
                continue
            builtin_names.add(py_file.stem)

        self.skills = [s for s in self.skills if any(
            s.__class__.__module__.startswith("skills.") for _ in [None]
        )]
        # Simpler: just reload everything
        self.load_all()

    def get_tool_definitions(self) -> list[dict]:
        """Get OpenAI-compatible tool definitions for all loaded skills."""
        return [s.to_tool_definition() for s in self.skills]

    def get_skill_descriptions(self) -> str:
        """Get a human-readable list of all skills for the system prompt."""
        lines = []
        for s in self.skills:
            params = ", ".join(
                f"`{k}` ({v.get('type', '?')}): {v.get('description', '')}"
                for k, v in s.parameters.get("properties", {}).items()
            )
            lines.append(f"- **{s.name}**: {s.description}")
            if params:
                lines.append(f"  Parameters: {params}")
        return "\n".join(lines) if lines else "No skills loaded."

    async def execute(self, skill_name: str, **kwargs) -> str:
        """Execute a skill by name. _context may be passed in kwargs for sandboxing constraints."""
        skill = self._skill_map.get(skill_name)
        if not skill:
            return f"Error: Unknown skill '{skill_name}'. Available: {list(self._skill_map.keys())}"

        try:
            result = await skill.execute(**kwargs)
            return str(result)
        except Exception as e:
            error_msg = f"Error executing {skill_name}: {type(e).__name__}: {e}"
            logger.error(error_msg, exc_info=True)
            return error_msg
