"""
Expression converter for JavaScript to KIRun expressions.

Converts JavaScript expressions to KIRun-compatible expression strings.
"""
from typing import Dict, Any, Optional, List, Tuple
from .parser import JSParser


# Valid KIRun path prefixes
KIRUN_PREFIXES = ('Page', 'Store', 'Url', 'Parent', 'Steps', 'Arguments', 'Context')

# JavaScript operators mapped to KIRun operators
BINARY_OPERATORS = {
    '+': '+',
    '-': '-',
    '*': '*',
    '/': '/',
    '%': '%',
    '==': '=',      # KIRun uses single = for equality
    '===': '=',     # KIRun uses single = for equality
    '!=': '!=',
    '!==': '!=',
    '<': '<',
    '>': '>',
    '<=': '<=',
    '>=': '>=',
    '&&': '&&',
    '||': '||',
    '&': '&',
    '|': '|',
    '^': '^',
}

UNARY_OPERATORS = {
    '!': '!',
    '-': '-',
    '+': '+',
    '~': '~',
}


class ExpressionConverter:
    """
    Converts JavaScript expressions to KIRun expression format.
    
    Key assumptions:
    - All data access is through Page/Store/Url/Parent paths
    - No local variables - identifiers not matching KIRun paths are errors/warnings
    - Binary operations, member access, and function calls are preserved
    """
    
    def __init__(self):
        self.warnings: List[str] = []
        self.errors: List[str] = []
    
    def reset(self):
        """Reset warnings and errors."""
        self.warnings = []
        self.errors = []
    
    def convert(self, node: Dict[str, Any]) -> str:
        """
        Convert an AST node to a KIRun expression string.
        
        Args:
            node: AST node representing an expression
            
        Returns:
            KIRun expression string
        """
        if node is None:
            return ""
        
        node_type = node.get('type', '')
        
        handler = getattr(self, f'_convert_{node_type}', None)
        if handler:
            return handler(node)
        
        self.warnings.append(f"Unknown expression type: {node_type}")
        return ""
    
    def _convert_Literal(self, node: Dict[str, Any]) -> str:
        """Convert a literal value."""
        value = node.get('value')
        raw = node.get('raw', '')
        
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            # Return with quotes for string literals
            return f'"{value}"'
        if isinstance(value, (int, float)):
            return str(value)
        
        # Fallback to raw if available
        return raw if raw else str(value)
    
    def _convert_Identifier(self, node: Dict[str, Any]) -> str:
        """Convert an identifier."""
        name = node.get('name', '')
        
        # Check if it's a KIRun path prefix
        if name in KIRUN_PREFIXES:
            return name
        
        # Check for special JS identifiers
        if name in ('true', 'false', 'null', 'undefined'):
            if name == 'undefined':
                return 'null'
            return name
        
        # Warn about unknown identifier (likely unsupported local variable)
        self.warnings.append(
            f"Unknown identifier '{name}' - should be a KIRun path (Page.*, Store.*, etc.)"
        )
        return name
    
    def _convert_MemberExpression(self, node: Dict[str, Any]) -> str:
        """Convert a member expression (e.g., Page.counter, Store.items[0])."""
        obj = self.convert(node.get('object', {}))
        prop = node.get('property', {})
        computed = node.get('computed', False)
        
        if computed:
            # Array access: obj[prop]
            prop_expr = self.convert(prop)
            return f"{obj}[{prop_expr}]"
        else:
            # Dot access: obj.prop
            prop_name = prop.get('name', '')
            return f"{obj}.{prop_name}"
    
    def _convert_BinaryExpression(self, node: Dict[str, Any]) -> str:
        """Convert a binary expression (e.g., a + b, x === y)."""
        left = self.convert(node.get('left', {}))
        right = self.convert(node.get('right', {}))
        operator = node.get('operator', '')
        
        kirun_op = BINARY_OPERATORS.get(operator, operator)
        
        # Add parentheses for complex expressions
        return f"({left} {kirun_op} {right})"
    
    def _convert_LogicalExpression(self, node: Dict[str, Any]) -> str:
        """Convert a logical expression (e.g., a && b, x || y)."""
        return self._convert_BinaryExpression(node)
    
    def _convert_UnaryExpression(self, node: Dict[str, Any]) -> str:
        """Convert a unary expression (e.g., !x, -y)."""
        argument = self.convert(node.get('argument', {}))
        operator = node.get('operator', '')
        prefix = node.get('prefix', True)
        
        kirun_op = UNARY_OPERATORS.get(operator, operator)
        
        if prefix:
            return f"{kirun_op}{argument}"
        else:
            return f"{argument}{kirun_op}"
    
    def _convert_ConditionalExpression(self, node: Dict[str, Any]) -> str:
        """Convert a ternary expression (e.g., a ? b : c)."""
        test = self.convert(node.get('test', {}))
        consequent = self.convert(node.get('consequent', {}))
        alternate = self.convert(node.get('alternate', {}))
        
        return f"({test} ? {consequent} : {alternate})"
    
    def _convert_CallExpression(self, node: Dict[str, Any]) -> str:
        """Convert a function call expression."""
        callee = self.convert(node.get('callee', {}))
        args = node.get('arguments', [])
        
        arg_strs = [self.convert(arg) for arg in args]
        
        return f"{callee}({', '.join(arg_strs)})"
    
    def _convert_ArrayExpression(self, node: Dict[str, Any]) -> str:
        """Convert an array literal."""
        elements = node.get('elements', [])
        element_strs = [self.convert(el) for el in elements if el]
        
        return f"[{', '.join(element_strs)}]"
    
    def _convert_ObjectExpression(self, node: Dict[str, Any]) -> str:
        """Convert an object literal."""
        properties = node.get('properties', [])
        prop_strs = []
        
        for prop in properties:
            key = prop.get('key', {})
            value = prop.get('value', {})
            
            if key.get('type') == 'Identifier':
                key_str = key.get('name', '')
            else:
                key_str = self.convert(key)
            
            value_str = self.convert(value)
            prop_strs.append(f'"{key_str}": {value_str}')
        
        return "{" + ", ".join(prop_strs) + "}"
    
    def _convert_TemplateLiteral(self, node: Dict[str, Any]) -> str:
        """Convert a template literal (string interpolation)."""
        quasis = node.get('quasis', [])
        expressions = node.get('expressions', [])
        
        parts = []
        for i, quasi in enumerate(quasis):
            cooked = quasi.get('value', {}).get('cooked', '')
            if cooked:
                parts.append(f'"{cooked}"')
            
            if i < len(expressions):
                expr = self.convert(expressions[i])
                parts.append(expr)
        
        if len(parts) == 1:
            return parts[0]
        
        # Join with string concatenation
        return " + ".join(parts)
    
    def _convert_UpdateExpression(self, node: Dict[str, Any]) -> str:
        """Convert an update expression (e.g., i++, --j)."""
        argument = self.convert(node.get('argument', {}))
        operator = node.get('operator', '')
        prefix = node.get('prefix', False)
        
        # Convert to expression form
        if operator == '++':
            return f"({argument} + 1)"
        elif operator == '--':
            return f"({argument} - 1)"
        
        return argument
    
    def _convert_AssignmentExpression(self, node: Dict[str, Any]) -> str:
        """Convert an assignment expression value (just the right side)."""
        # For expressions, we typically just want the right side
        right = self.convert(node.get('right', {}))
        return right
    
    def _convert_SequenceExpression(self, node: Dict[str, Any]) -> str:
        """Convert a sequence expression (comma-separated expressions)."""
        expressions = node.get('expressions', [])
        # Return the last expression
        if expressions:
            return self.convert(expressions[-1])
        return ""
    
    def _convert_SpreadElement(self, node: Dict[str, Any]) -> str:
        """Convert a spread element (...arg)."""
        argument = self.convert(node.get('argument', {}))
        self.warnings.append(f"Spread element may not be fully supported: ...{argument}")
        return f"...{argument}"
    
    def _convert_ArrowFunctionExpression(self, node: Dict[str, Any]) -> str:
        """Convert an arrow function - not supported."""
        self.errors.append("Arrow functions are not supported in KIRun expressions")
        return ""
    
    def _convert_FunctionExpression(self, node: Dict[str, Any]) -> str:
        """Convert a function expression - not supported."""
        self.errors.append("Function expressions are not supported in KIRun expressions")
        return ""


def expression_to_kirun(node: Dict[str, Any]) -> Tuple[str, List[str], List[str]]:
    """
    Convert an AST expression node to a KIRun expression string.
    
    Args:
        node: AST node representing an expression
        
    Returns:
        Tuple of (expression_string, warnings, errors)
    """
    converter = ExpressionConverter()
    expr = converter.convert(node)
    return expr, converter.warnings, converter.errors


def is_store_path_expression(expr: str) -> bool:
    """
    Check if an expression string is a valid KIRun store path.
    
    Args:
        expr: Expression string
        
    Returns:
        True if the expression starts with a valid KIRun prefix
    """
    return any(expr.startswith(prefix + '.') or expr == prefix 
               for prefix in KIRUN_PREFIXES)


def extract_path_from_expression(expr: str) -> Optional[str]:
    """
    Extract the store path from an expression if it's a simple path reference.
    
    Args:
        expr: Expression string
        
    Returns:
        Store path or None if not a simple path
    """
    # Simple check - if it's just a path without operators
    if is_store_path_expression(expr):
        # Check if it's a simple path (no operators)
        if not any(op in expr for op in ['+', '-', '*', '/', '(', ')', '?', ':', '&&', '||']):
            return expr
    return None

