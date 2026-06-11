from enum import Enum
from pydantic import BaseModel, Field
from typing import Any, List, Optional


class StorageRequest(BaseModel):
    storageName: str = Field(..., description="Name of the storage to read from")
    appCode: str = Field(default="marketingai", description="App code in NCLC")
    clientCode: str = Field(..., description="Client code for tenant identification")
    dataObjectId: Optional[str] = None
    eager: bool = False
    eagerFields: List[str] = []


class StorageRequestWithPayload(BaseModel):
    storageName: str = Field(..., description="Name of the storage to create in")
    appCode: str = Field(default="marketingai", description="App code in NCLC")
    clientCode: Optional[str] = Field(None, description="Client code for tenant identification")
    dataObject: Any
    eager: bool = False
    eagerFields: List[str] = []


class StorageUpdateWithPayload(BaseModel):
    storageName: str = Field(..., description="Name of the storage to create in")
    appCode: str = Field(default="marketingai", description="App code in NCLC")
    clientCode: Optional[str] = Field(None, description="Client code for tenant identification")
    dataObject: Any
    dataObjectId: str
    isPartial: bool = Field(
        default=True, description="Whether to partially update the record"
    )
    eager: bool = False
    eagerFields: List[str] = []


class StorageFilter(BaseModel):
    field: str = Field(..., description="Field name to filter records by")
    value: Any = Field(..., description="Value to match for the given field")




class FilterOperator(str, Enum):
    EQUALS = "EQUALS"
    LESS_THAN = "LESS_THAN"
    GREATER_THAN = "GREATER_THAN"
    LESS_THAN_EQUAL = "LESS_THAN_EQUAL"
    GREATER_THAN_EQUAL = "GREATER_THAN_EQUAL"
    IS_TRUE = "IS_TRUE"
    IS_FALSE = "IS_FALSE"
    IS_NULL = "IS_NULL"
    BETWEEN = "BETWEEN"
    IN = "IN"
    LIKE = "LIKE"
    STRING_LOOSE_EQUAL = "STRING_LOOSE_EQUAL"
    MATCH = "MATCH"
    MATCH_ALL = "MATCH_ALL"
    TEXT_SEARCH = "TEXT_SEARCH"


class LogicalOperator(str, Enum):
    AND = "AND"
    OR = "OR"


class FilterCondition(BaseModel):
    field: str
    operator: FilterOperator = FilterOperator.EQUALS
    value: Any = None
    negate: bool = False


class ComplexCondition(BaseModel):
    operator: LogicalOperator = LogicalOperator.AND
    conditions: List[FilterCondition] = []
    negate: bool = False


class SortDirection(str, Enum):
    ASC = "ASC"
    DESC = "DESC"


class SortOrder(BaseModel):
    property: str
    direction: SortDirection = SortDirection.ASC


class StorageReadRequest(BaseModel):
    storageName: str = Field(..., description="Name of the storage to read from")
    appCode: str = Field(default="marketingai", description="App code in NCLC")
    clientCode: str = Field(..., description="Client code for tenant identification")
    eager: bool = Field(default=False, description="Whether to eagerly load related data")
    eagerFields: List[str] = Field(
        default_factory=list, description="List of fields to eagerly load"
    )
    filter: Optional[StorageFilter | ComplexCondition | FilterCondition] = Field(
        None,
        description="Filter condition - simple StorageFilter, ComplexCondition, or FilterCondition",
    )
    size: Optional[int] = Field(None, description="Number of records per page")
    sort: Optional[List[SortOrder]] = Field(None, description="Sort order for results")


class StorageResponse(BaseModel):
    success: bool
    result: Optional[Any] = None
    error: Optional[str] = None

    @property
    def content(self) -> list:
        """
        Extracts content from the response result.
        Handles both list-based results (paginated) and single-object results.
        """
        if not self.result:
            return []

        data = self.result
        if isinstance(data, list) and len(data) > 0:
            data = data[0]

        # Fixed 2-level unwrap matching the known API wrapper (result.result)
        for _ in range(2):
            if isinstance(data, dict) and "result" in data:
                data = data["result"]
            else:
                break

        if data is None:
            return []

        # Handle paginated wrapper (ReadPage)
        if isinstance(data, dict) and "content" in data:
            content_list = data["content"]
            return content_list if isinstance(content_list, list) else [content_list]

        # Handle single record or list of records (Read)
        return data if isinstance(data, list) else [data]
