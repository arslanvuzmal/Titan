from typing import Dict, Type, Any, Optional
from pydantic import BaseModel
from abc import ABC, abstractmethod

# Type checking for forward references
try:
    from app.tools.security import ToolContext
except ImportError:
    ToolContext = Any


class ToolResult(BaseModel):
    """
    Standardized return object for all tool executions.
    """

    status: str  # "SUCCESS" or "FAILED"
    message: str
    data: Optional[Dict[str, Any]] = None


class BaseTool(ABC):
    """
    Abstract base class for all tools. Enforces strict typing and execution interfaces.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """The unique string identifier for the tool."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Description provided to the LLM to understand when to use this tool."""
        pass

    @property
    @abstractmethod
    def parameters_schema(self) -> Type[BaseModel]:
        """Pydantic model defining the expected inputs."""
        pass

    @abstractmethod
    async def execute(self, params: BaseModel, context: "ToolContext") -> ToolResult:
        """The core execution logic using the strictly validated params."""
        pass


class ToolRegistry:
    """
    Singleton registry holding all instantiated tools.
    """

    _instance = None
    _tools: Dict[str, BaseTool] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ToolRegistry, cls).__new__(cls)
        return cls._instance

    @classmethod
    def register(cls, tool: BaseTool):
        cls._tools[tool.name] = tool

    @classmethod
    def get_tool(cls, name: str) -> Optional[BaseTool]:
        return cls._tools.get(name)
