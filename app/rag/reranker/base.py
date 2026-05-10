from abc import ABC, abstractmethod
from typing import Any


class BaseReranker(ABC):
    """
    Reranker interface.

    Input chunks follow the standard RAG chunk format:
        {"id": str, "document": str, "metadata": dict, "distance": float}

    rerank() returns the same list, filtered to top_n and sorted by
    relevance score descending. Each returned chunk gains a "rerank_score" key.
    """

    @abstractmethod
    def rerank(self, query: str, chunks: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
        ...
