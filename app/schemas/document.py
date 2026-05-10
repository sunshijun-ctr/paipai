from typing import Optional
from pydantic import BaseModel


class Paper(BaseModel):
    title: str
    year: Optional[int] = None
    source: Optional[str] = None
    pdf_url: Optional[str] = None
    local_path: Optional[str] = None
    authors: list[str] = []
    abstract: Optional[str] = None
    arxiv_id: Optional[str] = None
    tags: list[str] = []


class DownloadedPDF(BaseModel):
    title: str
    local_path: str
    arxiv_id: Optional[str] = None
    source_url: Optional[str] = None
