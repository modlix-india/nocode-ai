"""
Statement and Function Definition builders for KIRun.

Creates KIRun statements, parameter references, and function definitions.
"""
from typing import Dict, Any, Optional, List
import random
import string

from .types import (
    KIRunFunctionDefinition,
    KIRunStatement,
    KIRunParameter,
    KIRunEvent,
    ParameterReference,
    Position,
    SchemaDefinition,
)


class StatementNameGenerator:
    """Generates unique statement names in camelCase without underscores."""
    
    def __init__(self):
        self._counters: Dict[str, int] = {}
    
    def generate(self, prefix: str = "step") -> str:
        """
        Generate a unique statement name.
        
        Args:
            prefix: Base name prefix (e.g., "setStore", "fetchData")
            
        Returns:
            Unique statement name in camelCase (e.g., "setStore1", "fetchData2")
        """
        # Ensure prefix is camelCase
        prefix = self._to_camel_case(prefix)
        
        # Increment counter for this prefix
        if prefix not in self._counters:
            self._counters[prefix] = 0
        self._counters[prefix] += 1
        
        return f"{prefix}{self._counters[prefix]}"
    
    def reset(self):
        """Reset all counters."""
        self._counters = {}
    
    @staticmethod
    def _to_camel_case(name: str) -> str:
        """
        Convert a name to camelCase.
        
        Args:
            name: Input name (may contain underscores, hyphens, etc.)
            
        Returns:
            camelCase version of the name
        """
        # Remove underscores and hyphens, capitalize following letters
        result = []
        capitalize_next = False
        for i, char in enumerate(name):
            if char in ('_', '-', ' '):
                capitalize_next = True
            elif capitalize_next:
                result.append(char.upper())
                capitalize_next = False
            else:
                # First character should be lowercase
                if i == 0:
                    result.append(char.lower())
                else:
                    result.append(char)
        return ''.join(result)


def generate_id(length: int = 20) -> str:
    """
    Generate a unique ID for parameter references and keys.
    
    Args:
        length: Length of the ID
        
    Returns:
        Random alphanumeric ID
    """
    chars = string.ascii_letters + string.digits
    return ''.join(random.choices(chars, k=length))


def generate_key() -> str:
    """
    Generate a short unique key for parameter references.
    
    Returns:
        Short unique key
    """
    return generate_id(10)


def create_parameter_reference(
    value: Any = None,
    expression: str = None,
    order: int = 1,
    key: str = None
) -> ParameterReference:
    """
    Create a parameter reference.
    
    Args:
        value: Static value (for VALUE type)
        expression: Expression string (for EXPRESSION type)
        order: Order for variadic parameters
        key: Optional custom key (generated if not provided)
        
    Returns:
        ParameterReference dictionary
    """
    param_ref: ParameterReference = {
        "key": key or generate_key(),
        "order": order
    }
    
    if expression is not None:
        param_ref["type"] = "EXPRESSION"
        param_ref["expression"] = expression
    else:
        param_ref["type"] = "VALUE"
        param_ref["value"] = value
    
    return param_ref


def create_parameter_map(
    params: Dict[str, Any]
) -> Dict[str, Dict[str, ParameterReference]]:
    """
    Create a parameter map from a dictionary of parameter values.
    
    Each parameter can be:
    - A static value (creates VALUE type)
    - A dict with 'expression' key (creates EXPRESSION type)
    - A dict with 'value' key (creates VALUE type)
    
    Args:
        params: Dictionary of parameter name to value/expression
        
    Returns:
        Parameter map in KIRun format
    """
    param_map: Dict[str, Dict[str, ParameterReference]] = {}
    
    for name, value in params.items():
        if isinstance(value, dict):
            if 'expression' in value:
                ref = create_parameter_reference(expression=value['expression'])
            else:
                ref = create_parameter_reference(value=value.get('value', value))
        else:
            ref = create_parameter_reference(value=value)
        
        param_map[name] = {ref['key']: ref}
    
    return param_map


def create_statement(
    statement_name: str,
    name: str,
    namespace: str,
    parameter_map: Dict[str, Dict[str, ParameterReference]] = None,
    dependent_statements: Dict[str, bool] = None,
    execute_if_true: Dict[str, bool] = None,
    comment: str = None,
    description: str = None,
    position: Position = None
) -> KIRunStatement:
    """
    Create a KIRun statement.
    
    Args:
        statement_name: Unique identifier for the statement (camelCase, no underscores)
        name: Function name to call (e.g., "SetStore", "FetchData")
        namespace: Function namespace (e.g., "UIEngine", "System")
        parameter_map: Map of parameter names to parameter references
        dependent_statements: Dependencies on other statements
        execute_if_true: Conditional execution dependencies
        comment: Optional developer comment
        description: Optional description
        position: Optional visual position
        
    Returns:
        KIRunStatement dictionary
    """
    statement: KIRunStatement = {
        "statementName": statement_name,
        "name": name,
        "namespace": namespace,
        "parameterMap": parameter_map or {}
    }
    
    if dependent_statements:
        statement["dependentStatements"] = dependent_statements
    if execute_if_true:
        statement["executeIftrue"] = execute_if_true
    if comment:
        statement["comment"] = comment
    if description:
        statement["description"] = description
    if position:
        statement["position"] = position
    
    return statement


def create_set_store_statement(
    name_generator: StatementNameGenerator,
    path: str,
    value: Any = None,
    expression: str = None,
    dependent_statements: Dict[str, bool] = None
) -> KIRunStatement:
    """
    Create a SetStore statement.
    
    Args:
        name_generator: Statement name generator
        path: Store path (e.g., "Page.counter")
        value: Static value to set
        expression: Expression to evaluate for value
        dependent_statements: Dependencies on other statements
        
    Returns:
        SetStore statement
    """
    param_map: Dict[str, Dict[str, ParameterReference]] = {
        "path": {generate_key(): create_parameter_reference(value=path)}
    }
    
    if expression is not None:
        value_ref = create_parameter_reference(expression=expression)
    else:
        value_ref = create_parameter_reference(value=value)
    
    param_map["value"] = {value_ref["key"]: value_ref}
    
    return create_statement(
        statement_name=name_generator.generate("setStore"),
        name="SetStore",
        namespace="UIEngine",
        parameter_map=param_map,
        dependent_statements=dependent_statements
    )


def create_fetch_data_statement(
    name_generator: StatementNameGenerator,
    url: str,
    url_is_expression: bool = False,
    query_params: Dict[str, Any] = None,
    path_params: Dict[str, Any] = None,
    headers: Dict[str, Any] = None,
    dependent_statements: Dict[str, bool] = None
) -> KIRunStatement:
    """
    Create a FetchData statement.
    
    Args:
        name_generator: Statement name generator
        url: URL to fetch
        url_is_expression: If True, url is treated as an expression
        query_params: Query parameters
        path_params: Path parameters
        headers: HTTP headers
        dependent_statements: Dependencies on other statements
        
    Returns:
        FetchData statement
    """
    param_map: Dict[str, Dict[str, ParameterReference]] = {}
    
    if url_is_expression:
        url_ref = create_parameter_reference(expression=url)
    else:
        url_ref = create_parameter_reference(value=url)
    
    param_map["url"] = {url_ref["key"]: url_ref}
    
    if query_params:
        qp_ref = create_parameter_reference(value=query_params)
        param_map["queryParams"] = {qp_ref["key"]: qp_ref}
    
    if path_params:
        pp_ref = create_parameter_reference(value=path_params)
        param_map["pathParams"] = {pp_ref["key"]: pp_ref}
    
    if headers:
        h_ref = create_parameter_reference(value=headers)
        param_map["headers"] = {h_ref["key"]: h_ref}
    
    return create_statement(
        statement_name=name_generator.generate("fetchData"),
        name="FetchData",
        namespace="UIEngine",
        parameter_map=param_map,
        dependent_statements=dependent_statements
    )


def create_send_data_statement(
    name_generator: StatementNameGenerator,
    url: str,
    method: str = "POST",
    url_is_expression: bool = False,
    payload: Any = None,
    payload_is_expression: bool = False,
    query_params: Dict[str, Any] = None,
    path_params: Dict[str, Any] = None,
    headers: Dict[str, Any] = None,
    dependent_statements: Dict[str, bool] = None
) -> KIRunStatement:
    """
    Create a SendData statement.
    
    Args:
        name_generator: Statement name generator
        url: URL to send data to
        method: HTTP method (POST, PUT, PATCH)
        url_is_expression: If True, url is treated as an expression
        payload: Request body
        payload_is_expression: If True, payload is treated as an expression
        query_params: Query parameters
        path_params: Path parameters
        headers: HTTP headers
        dependent_statements: Dependencies on other statements
        
    Returns:
        SendData statement
    """
    param_map: Dict[str, Dict[str, ParameterReference]] = {}
    
    if url_is_expression:
        url_ref = create_parameter_reference(expression=url)
    else:
        url_ref = create_parameter_reference(value=url)
    param_map["url"] = {url_ref["key"]: url_ref}
    
    method_ref = create_parameter_reference(value=method.upper())
    param_map["method"] = {method_ref["key"]: method_ref}
    
    if payload is not None:
        if payload_is_expression:
            payload_ref = create_parameter_reference(expression=payload)
        else:
            payload_ref = create_parameter_reference(value=payload)
        param_map["payload"] = {payload_ref["key"]: payload_ref}
    
    if query_params:
        qp_ref = create_parameter_reference(value=query_params)
        param_map["queryParams"] = {qp_ref["key"]: qp_ref}
    
    if path_params:
        pp_ref = create_parameter_reference(value=path_params)
        param_map["pathParams"] = {pp_ref["key"]: pp_ref}
    
    if headers:
        h_ref = create_parameter_reference(value=headers)
        param_map["headers"] = {h_ref["key"]: h_ref}
    
    return create_statement(
        statement_name=name_generator.generate("sendData"),
        name="SendData",
        namespace="UIEngine",
        parameter_map=param_map,
        dependent_statements=dependent_statements
    )


def create_navigate_statement(
    name_generator: StatementNameGenerator,
    link_path: str,
    path_is_expression: bool = False,
    target: str = None,
    force: bool = None,
    dependent_statements: Dict[str, bool] = None
) -> KIRunStatement:
    """
    Create a Navigate statement.
    
    Args:
        name_generator: Statement name generator
        link_path: Path to navigate to
        path_is_expression: If True, path is treated as an expression
        target: Window target (_self, _blank, etc.)
        force: Force full page navigation
        dependent_statements: Dependencies on other statements
        
    Returns:
        Navigate statement
    """
    param_map: Dict[str, Dict[str, ParameterReference]] = {}
    
    if path_is_expression:
        path_ref = create_parameter_reference(expression=link_path)
    else:
        path_ref = create_parameter_reference(value=link_path)
    param_map["linkPath"] = {path_ref["key"]: path_ref}
    
    if target:
        target_ref = create_parameter_reference(value=target)
        param_map["target"] = {target_ref["key"]: target_ref}
    
    if force is not None:
        force_ref = create_parameter_reference(value=force)
        param_map["force"] = {force_ref["key"]: force_ref}
    
    return create_statement(
        statement_name=name_generator.generate("navigate"),
        name="Navigate",
        namespace="UIEngine",
        parameter_map=param_map,
        dependent_statements=dependent_statements
    )


def create_wait_statement(
    name_generator: StatementNameGenerator,
    millis: int,
    millis_is_expression: bool = False,
    dependent_statements: Dict[str, bool] = None
) -> KIRunStatement:
    """
    Create a Wait statement.
    
    Args:
        name_generator: Statement name generator
        millis: Milliseconds to wait
        millis_is_expression: If True, millis is treated as an expression
        dependent_statements: Dependencies on other statements
        
    Returns:
        Wait statement
    """
    param_map: Dict[str, Dict[str, ParameterReference]] = {}
    
    if millis_is_expression:
        millis_ref = create_parameter_reference(expression=str(millis))
    else:
        millis_ref = create_parameter_reference(value=millis)
    param_map["millis"] = {millis_ref["key"]: millis_ref}
    
    return create_statement(
        statement_name=name_generator.generate("wait"),
        name="Wait",
        namespace="System",
        parameter_map=param_map,
        dependent_statements=dependent_statements
    )


def create_message_statement(
    name_generator: StatementNameGenerator,
    msg: str,
    msg_is_expression: bool = False,
    msg_type: str = "ERROR",
    dependent_statements: Dict[str, bool] = None
) -> KIRunStatement:
    """
    Create a Message statement.
    
    Args:
        name_generator: Statement name generator
        msg: Message content
        msg_is_expression: If True, msg is treated as an expression
        msg_type: Message type (ERROR, WARNING, INFO, SUCCESS)
        dependent_statements: Dependencies on other statements
        
    Returns:
        Message statement
    """
    param_map: Dict[str, Dict[str, ParameterReference]] = {}
    
    if msg_is_expression:
        msg_ref = create_parameter_reference(expression=msg)
    else:
        msg_ref = create_parameter_reference(value=msg)
    param_map["msg"] = {msg_ref["key"]: msg_ref}
    
    type_ref = create_parameter_reference(value=msg_type)
    param_map["type"] = {type_ref["key"]: type_ref}
    
    return create_statement(
        statement_name=name_generator.generate("message"),
        name="Message",
        namespace="UIEngine",
        parameter_map=param_map,
        dependent_statements=dependent_statements
    )


def create_if_statement(
    name_generator: StatementNameGenerator,
    condition: str,
    dependent_statements: Dict[str, bool] = None
) -> KIRunStatement:
    """
    Create an If statement.
    
    Args:
        name_generator: Statement name generator
        condition: Condition expression
        dependent_statements: Dependencies on other statements
        
    Returns:
        If statement
    """
    param_map: Dict[str, Dict[str, ParameterReference]] = {}
    
    cond_ref = create_parameter_reference(expression=condition)
    param_map["condition"] = {cond_ref["key"]: cond_ref}
    
    return create_statement(
        statement_name=name_generator.generate("if"),
        name="If",
        namespace="System",
        parameter_map=param_map,
        dependent_statements=dependent_statements
    )


def create_function_definition(
    name: str,
    namespace: str = "",
    steps: Dict[str, KIRunStatement] = None,
    parameters: Dict[str, KIRunParameter] = None,
    events: Dict[str, KIRunEvent] = None,
    version: int = 1
) -> KIRunFunctionDefinition:
    """
    Create a KIRun function definition.
    
    Args:
        name: Function name
        namespace: Function namespace
        steps: Execution steps
        parameters: Input parameters
        events: Output events
        version: Version number
        
    Returns:
        KIRunFunctionDefinition dictionary
    """
    func_def: KIRunFunctionDefinition = {
        "name": name,
        "namespace": namespace,
        "version": version,
        "steps": steps or {}
    }
    
    if parameters:
        func_def["parameters"] = parameters
    
    if events:
        func_def["events"] = events
    
    return func_def

