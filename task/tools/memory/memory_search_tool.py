import json
from typing import Any

from task.tools.base import BaseTool
from task.tools.memory._models import MemoryData
from task.tools.memory.memory_store import LongTermMemoryStore
from task.tools.models import ToolCallParams


class SearchMemoryTool(BaseTool):
    """
    Tool for searching long-term memories about the user.

    Performs semantic search over stored memories to find relevant information.
    """

    def __init__(self, memory_store: LongTermMemoryStore):
        self.memory_store = memory_store


    @property
    def name(self) -> str:
        return "search_memory"

    @property
    def description(self) -> str:
        return (
            "Search long-term memories about the user. Use this when you need to recall information about the user "
            "that might have been mentioned in previous conversations. The search is semantic, so you can use natural "
            "language queries. For example, if you need to know where the user lives, search for 'location' or 'where do you live'. "
            "If you need to know preferences, search for 'preferences' or specific topics. Always search before making "
            "assumptions about the user. Returns the most relevant memories based on semantic similarity."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query. Can be a question or keywords to find relevant memories"
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of most relevant memories to return.",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 5
                }
            },
            "required": ["query"]
        }


    async def _execute(self, tool_call_params: ToolCallParams) -> str:
        arguments = json.loads(tool_call_params.tool_call.function.arguments)
        query = arguments["query"]
        top_k = arguments.get("top_k", 5)
        
        results = await self.memory_store.search_memories(
            api_key=tool_call_params.api_key,
            query=query,
            top_k=top_k
        )
        
        stage = tool_call_params.stage
        stage.append_content(f"## Search query: {query}\n\n")
        
        if not results:
            final_result = "No memories found."
            stage.append_content("**Result:** No memories found.\n")
        else:
            markdown_lines = []
            for i, memory in enumerate(results, 1):
                markdown_lines.append(f"### Memory {i}")
                markdown_lines.append(f"- **Content:** {memory.content}")
                markdown_lines.append(f"- **Category:** {memory.category}")
                if memory.topics:
                    markdown_lines.append(f"- **Topics:** {', '.join(memory.topics)}")
                markdown_lines.append("")
            
            final_result = "\n".join(markdown_lines)
            stage.append_content(final_result)
        
        return final_result
