"""Storage layer public API.

All code should import from app.storage.factory, not from here.
This file re-exports the factory functions for convenience only.
"""
from app.storage.factory import get_vector_store
from app.storage.base import BaseKVStore, BaseVectorStore

__all__ = ["get_vector_store", "BaseKVStore", "BaseVectorStore"]
