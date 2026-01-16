import os
os.environ['OMP_NUM_THREADS'] = '1'

import json
from datetime import datetime, UTC, timedelta
import numpy as np
import faiss
from aidial_client import AsyncDial
from sentence_transformers import SentenceTransformer

from task.tools.memory._models import Memory, MemoryData, MemoryCollection


class LongTermMemoryStore:
    """
    Manages long-term memory storage for users.

    Storage format: Single JSON file per user in DIAL bucket
    - File: {user_id}/long-memories.json
    - Caching: In-memory cache with conversation_id as key
    - Deduplication: O(n log n) using FAISS batch search
    """

    DEDUP_INTERVAL_HOURS = 24

    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self.model = SentenceTransformer('all-MiniLM-L6-v2')
        self.cache: dict[str, MemoryCollection] = {}
        faiss.omp_set_num_threads(1)

    async def _get_memory_file_path(self, dial_client: AsyncDial) -> str:
        """Get the path to the memory file in DIAL bucket."""
        files_home = await dial_client.my_appdata_home()
        return f"files/{(files_home / '__long-memories' / 'data.json').as_posix()}"

    async def _load_memories(self, api_key: str) -> MemoryCollection:
        dial_client = AsyncDial(
            base_url=self.endpoint,
            api_key=api_key,
            api_version='2025-01-01-preview'
        )
        memory_file_path = await self._get_memory_file_path(dial_client)
        
        if memory_file_path in self.cache:
            return self.cache[memory_file_path]
        
        try:
            file_response = await dial_client.files.download(memory_file_path)
            content = file_response.get_content().decode('utf-8')
            data = json.loads(content)
            collection = MemoryCollection.model_validate(data)
        except Exception:
            collection = MemoryCollection(
                memories=[],
                updated_at=datetime.now(UTC),
                last_deduplicated_at=None
            )
        
        self.cache[memory_file_path] = collection
        return collection

    async def _save_memories(self, api_key: str, memories: MemoryCollection):
        """Save memories to DIAL bucket and update cache."""
        dial_client = AsyncDial(
            base_url=self.endpoint,
            api_key=api_key,
            api_version='2025-01-01-preview'
        )
        memory_file_path = await self._get_memory_file_path(dial_client)
        
        memories.updated_at = datetime.now(UTC)
        json_content = memories.model_dump_json()
        file_data = json_content.encode('utf-8')
        
        await dial_client.files.upload(url=memory_file_path, file=file_data)
        self.cache[memory_file_path] = memories

    async def add_memory(self, api_key: str, content: str, importance: float, category: str, topics: list[str]) -> str:
        """Add a new memory to storage."""
        collection = await self._load_memories(api_key)
        
        embedding = self.model.encode([content])[0].tolist()
        
        memory_data = MemoryData(
            id=int(datetime.now(UTC).timestamp()),
            content=content,
            importance=importance,
            category=category,
            topics=topics
        )
        memory = Memory(data=memory_data, embedding=embedding)
        
        collection.memories.append(memory)
        await self._save_memories(api_key, collection)
        
        return f"Memory successfully stored: {content}"

    async def search_memories(self, api_key: str, query: str, top_k: int = 5) -> list[MemoryData]:
        """
        Search memories using semantic similarity.

        Returns:
            List of MemoryData objects (without embeddings)
        """
        collection = await self._load_memories(api_key)
        
        if not collection.memories:
            return []
        
        if self._needs_deduplication(collection):
            collection = await self._deduplicate_and_save(api_key, collection)
        
        # Encode query
        query_embedding = self.model.encode([query])[0]
        query_embedding = np.array([query_embedding], dtype=np.float32)
        faiss.normalize_L2(query_embedding)
        
        # Build FAISS index from memories
        embeddings = np.array([memory.embedding for memory in collection.memories], dtype=np.float32)
        faiss.normalize_L2(embeddings)
        
        dimension = embeddings.shape[1]
        index = faiss.IndexFlatIP(dimension)
        index.add(embeddings)
        
        # Search
        k = min(top_k, len(collection.memories))
        similarities, indices = index.search(query_embedding, k)
        
        # Return MemoryData objects sorted by similarity
        results = []
        for idx, similarity in zip(indices[0], similarities[0]):
            if similarity > 0:  # Only include positive similarities
                results.append(collection.memories[idx].data)
        
        return results

    def _needs_deduplication(self, collection: MemoryCollection) -> bool:
        """Check if deduplication is needed (>24 hours since last deduplication)."""
        if len(collection.memories) <= 10:
            return False
        
        if collection.last_deduplicated_at is None:
            return True
        
        time_since_dedup = datetime.now(UTC) - collection.last_deduplicated_at
        return time_since_dedup > timedelta(hours=self.DEDUP_INTERVAL_HOURS)

    async def _deduplicate_and_save(self, api_key: str, collection: MemoryCollection) -> MemoryCollection:
        """
        Deduplicate memories synchronously and save the result.
        Returns the updated collection.
        """
        collection.memories = self._deduplicate_fast(collection.memories)
        collection.last_deduplicated_at = datetime.now(UTC)
        await self._save_memories(api_key, collection)
        return collection

    def _deduplicate_fast(self, memories: list[Memory]) -> list[Memory]:
        """
        Fast deduplication using FAISS batch search with cosine similarity.

        Strategy:
        - Find k nearest neighbors for each memory using cosine similarity
        - Mark duplicates based on similarity threshold (cosine similarity > 0.75)
        - Keep memory with higher importance
        """
        if len(memories) <= 1:
            return memories
        
        # Build FAISS index
        embeddings = np.array([memory.embedding for memory in memories], dtype=np.float32)
        dimension = embeddings.shape[1]
        
        # Normalize embeddings for cosine similarity
        faiss.normalize_L2(embeddings)
        
        # Create index
        index = faiss.IndexFlatIP(dimension)  # Inner product for cosine similarity (after normalization)
        index.add(embeddings)
        
        # Find duplicates
        to_remove = set()
        threshold = 0.75
        
        # For each memory, find similar ones
        for i in range(len(memories)):
            if i in to_remove:
                continue
            
            # Search for similar memories (including itself)
            similarities, indices = index.search(embeddings[i:i+1], min(len(memories), 20))
            
            for j, similarity in zip(indices[0], similarities[0]):
                if j == i or j in to_remove:
                    continue
                
                if similarity > threshold:
                    # Keep the one with higher importance, remove the other
                    if memories[i].data.importance >= memories[j].data.importance:
                        to_remove.add(j)
                    else:
                        to_remove.add(i)
                        break
        
        # Return deduplicated list
        return [mem for idx, mem in enumerate(memories) if idx not in to_remove]

    async def delete_all_memories(self, api_key: str, ) -> str:
        """
        Delete all memories for the user.

        Removes the memory file from DIAL bucket and clears the cache
        for the current conversation.
        """
        dial_client = AsyncDial(
            base_url=self.endpoint,
            api_key=api_key,
            api_version='2025-01-01-preview'
        )
        memory_file_path = await self._get_memory_file_path(dial_client)
        
        try:
            await dial_client.files.delete(memory_file_path)
        except Exception:
            pass  # File might not exist
        
        # Clear cache
        if memory_file_path in self.cache:
            del self.cache[memory_file_path]
        
        return "All long-term memories have been successfully deleted."
