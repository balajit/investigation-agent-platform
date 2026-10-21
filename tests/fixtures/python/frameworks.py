"""Synthetic Python fixture: dataclass and Pydantic declared defaults."""

from dataclasses import InitVar, dataclass, field
from typing import ClassVar

from pydantic import BaseModel, Field


@dataclass
class Job:
    name: str = "default-job"
    attempts: int = 3
    tags: list = field(default_factory=list)
    legacy: ClassVar[int] = 9
    tmp: InitVar[int] = 1


class JobModel(BaseModel):
    name: str = Field(default="default-job")
    attempts: int = 5
