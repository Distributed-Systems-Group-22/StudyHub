
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class SummaryRequest(BaseModel):
    document_ids: list[str] = Field(
        min_length=1,
        max_length=20
    )
    instruction: str = Field(
        default="Generate a combined summary",
        min_length=1,
        max_length=2000
    )

    @field_validator("document_ids")
    @classmethod
    def validate_document_ids(cls, values):
        normalized = []
        for value in values:
            value = value.strip()
            try:
                value = str(UUID(value))
            except ValueError:
                pass
            normalized.append(value)
        values = normalized
        if any(not value.strip() for value in values):
            raise ValueError("Document IDs cannot be empty")

        if len(values) != len(set(values)):
            raise ValueError("Duplicate document IDs are not allowed")

        return values

    @field_validator("instruction")
    @classmethod
    def validate_instruction(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Instruction cannot be blank")
        return value


class SummaryJobResponse(BaseModel):
    job_id: UUID
    status: str
    cache_hit: bool


class JobStatusResponse(BaseModel):
    job_id: UUID
    status: str
    attempts: int
    error_message: str | None = None
    summary_text: str | None = None
    summary_status: str | None = None
    citations: list["Citation"] | None = None


class Citation(BaseModel):
    source_id: str
    document_id: str
    document_version: int
    chunk_id: str
    page: int | None = None
    source_location: str | None = None
