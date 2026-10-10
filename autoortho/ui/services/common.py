"""Common structured service results."""

from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar


class ServiceErrorCode(str, Enum):
    CANCELLED = "cancelled"
    NETWORK = "network"
    FILESYSTEM = "filesystem"
    VALIDATION = "validation"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal"


@dataclass(frozen=True)
class ServiceError:
    code: ServiceErrorCode
    message: str
    detail: str = ""


T = TypeVar("T")


@dataclass(frozen=True)
class ServiceResult(Generic[T]):
    value: T | None = None
    error: ServiceError | None = None

    @property
    def success(self) -> bool:
        return self.error is None
