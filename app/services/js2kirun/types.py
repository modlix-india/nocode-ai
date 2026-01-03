"""
Type definitions for JavaScript to KIRun converter
"""
from typing import Dict, Optional, List, Any, TypedDict, Literal


class ParameterReference(TypedDict, total=False):
    """
    Parameter reference in a KIRun statement.
    
    Attributes:
        key: Unique identifier for this parameter reference
        type: Either "VALUE" for static values or "EXPRESSION" for dynamic expressions
        value: Static value (when type is VALUE)
        expression: Expression string (when type is EXPRESSION)
        order: Execution order for variadic parameters
    """
    key: str
    type: Literal["VALUE", "EXPRESSION"]
    value: Any
    expression: str
    order: int


class Position(TypedDict, total=False):
    """Visual position in the editor"""
    left: float
    top: float


class KIRunStatement(TypedDict, total=False):
    """
    A single step/statement in a KIRun function.
    
    Attributes:
        statementName: Unique identifier for the statement (camelCase, no underscores)
        name: Name of the function to call (e.g., "SetStore", "FetchData")
        namespace: Namespace of the function (e.g., "UIEngine", "System")
        comment: Developer comment
        description: Description of the statement
        position: Visual position in editor
        parameterMap: Map of parameter names to parameter references
        dependentStatements: Map of step paths to boolean - specifies which steps must have
                             completed before this step can run
        executeIftrue: Map of conditions that must be true for execution
    """
    statementName: str
    name: str
    namespace: str
    comment: str
    description: str
    position: Position
    parameterMap: Dict[str, Dict[str, ParameterReference]]
    dependentStatements: Dict[str, bool]
    executeIftrue: Dict[str, bool]


class SchemaDefinition(TypedDict, total=False):
    """Schema definition for parameters and events"""
    version: int
    type: List[str]
    items: Dict[str, Any]
    properties: Dict[str, Any]


class KIRunParameter(TypedDict, total=False):
    """
    Parameter definition for a KIRun function.
    
    Attributes:
        parameterName: Name of the parameter
        schema: Type schema for the parameter
        variableArgument: Whether this is a variadic parameter
    """
    parameterName: str
    schema: SchemaDefinition
    variableArgument: bool


class KIRunEvent(TypedDict, total=False):
    """
    Event definition for a KIRun function.
    
    Attributes:
        name: Name of the event (e.g., "output", "error")
        parameters: Map of parameter names to their schemas
    """
    name: str
    parameters: Dict[str, SchemaDefinition]


class KIRunFunctionDefinition(TypedDict, total=False):
    """
    Complete KIRun function definition.
    
    Attributes:
        name: Function name
        namespace: Function namespace
        version: Version number (default: 1)
        parameters: Input parameters
        events: Output events
        steps: Execution steps
        stepGroups: Optional step groups
        parts: Sub-function parts
    """
    name: str
    namespace: str
    version: int
    parameters: Dict[str, KIRunParameter]
    events: Dict[str, KIRunEvent]
    steps: Dict[str, KIRunStatement]
    stepGroups: Dict[str, Any]
    parts: List["KIRunFunctionDefinition"]


class ConversionContext(TypedDict, total=False):
    """
    Context for conversion.
    
    Attributes:
        storePrefix: Default store prefix for variables (Page, Store, Url, Parent)
        availableFunctions: Map of available functions by namespace
    """
    storePrefix: Literal["Page", "Store", "Url", "Parent"]
    availableFunctions: Dict[str, Dict[str, Any]]


class ConversionOptions(TypedDict, total=False):
    """
    Options for conversion.
    
    Attributes:
        namespace: Namespace for the generated function
        functionName: Name for the generated function
        generateIds: Whether to generate unique IDs
        context: Conversion context
    """
    namespace: str
    functionName: str
    generateIds: bool
    context: ConversionContext


class ConvertedResult(TypedDict):
    """
    Result of conversion.
    
    Attributes:
        functionDefinition: The generated KIRun function definition
        errors: List of errors encountered during conversion
        warnings: List of warnings encountered during conversion
    """
    functionDefinition: KIRunFunctionDefinition
    errors: List[str]
    warnings: List[str]

