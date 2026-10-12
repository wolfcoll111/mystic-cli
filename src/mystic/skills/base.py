"""
Base Skill class — all skills (built-in and custom) extend this.
"""

from abc import ABC, abstractmethod


class Skill(ABC):
    """
    Abstract base class for NIM Agent skills.

    To create a custom skill:
    1. Subclass Skill
    2. Set name, description, and parameters
    3. Implement the async execute() method
    """

    # Override these in subclasses
    name: str = "unnamed_skill"
    description: str = "No description provided"
    parameters: dict = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    @abstractmethod
    async def execute(self, **kwargs) -> str:
        """
        Execute the skill with the given arguments.

        Args:
            **kwargs: Arguments matching the parameters schema.

        Returns:
            A string result to feed back to the agent.
        """
        ...

    def to_tool_definition(self) -> dict:
        """Convert this skill to an OpenAI-compatible tool definition."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            }
        }
