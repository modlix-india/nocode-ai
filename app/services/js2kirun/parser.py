"""
JavaScript Parser wrapper using esprima.

Parses JavaScript code to ESTree-compatible AST.
"""
from typing import Dict, Any, Optional, List
import esprima
from esprima import nodes


class ParseError(Exception):
    """Exception raised when JavaScript parsing fails"""
    def __init__(self, message: str, line: int = 0, column: int = 0):
        self.message = message
        self.line = line
        self.column = column
        super().__init__(f"Parse error at line {line}, column {column}: {message}")


class JSParser:
    """
    JavaScript parser that converts JavaScript code to AST.
    
    Uses esprima to parse JavaScript and returns an ESTree-compatible AST.
    """
    
    @staticmethod
    def parse(code: str, tolerant: bool = True) -> Dict[str, Any]:
        """
        Parse JavaScript code to AST.
        
        Args:
            code: JavaScript code to parse
            tolerant: If True, continue parsing after errors
            
        Returns:
            AST as a dictionary
            
        Raises:
            ParseError: If parsing fails
        """
        options = {
            'tolerant': tolerant,
            'range': True,
            'loc': True,
            'comment': True,
        }
        
        try:
            # Try parseScript first
            ast = esprima.parseScript(code, options=options)
            return JSParser._node_to_dict(ast)
        except esprima.Error:
            try:
                # Fall back to parseModule for top-level await
                ast = esprima.parseModule(code, options=options)
                return JSParser._node_to_dict(ast)
            except esprima.Error as e:
                raise ParseError(str(e), getattr(e, 'lineNumber', 0), getattr(e, 'column', 0))
    
    @staticmethod
    def parse_expression(code: str) -> Dict[str, Any]:
        """
        Parse a single JavaScript expression.
        
        Args:
            code: JavaScript expression to parse
            
        Returns:
            Expression AST as a dictionary
        """
        # Wrap in parentheses to ensure it's parsed as expression
        wrapped = f"({code})"
        try:
            ast = esprima.parseScript(wrapped, options={'tolerant': True})
            # Get the expression from ExpressionStatement
            body = ast.body
            if body and len(body) > 0:
                stmt = body[0]
                if hasattr(stmt, 'expression'):
                    return JSParser._node_to_dict(stmt.expression)
            return {}
        except esprima.Error as e:
            raise ParseError(str(e), getattr(e, 'lineNumber', 0), getattr(e, 'column', 0))
    
    @staticmethod
    def _node_to_dict(node: Any) -> Dict[str, Any]:
        """
        Convert esprima node to dictionary.
        
        Args:
            node: Esprima AST node
            
        Returns:
            Dictionary representation of the node
        """
        if node is None:
            return None
        
        if isinstance(node, list):
            return [JSParser._node_to_dict(item) for item in node]
        
        if not hasattr(node, '__dict__'):
            return node
        
        result = {}
        for key, value in node.__dict__.items():
            if key.startswith('_'):
                continue
            if isinstance(value, list):
                result[key] = [JSParser._node_to_dict(item) for item in value]
            elif hasattr(value, '__dict__'):
                result[key] = JSParser._node_to_dict(value)
            else:
                result[key] = value
        
        return result
    
    @staticmethod
    def get_statements(ast: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Get all statements from the AST body.
        
        Args:
            ast: Parsed AST
            
        Returns:
            List of statement nodes
        """
        return ast.get('body', [])
    
    @staticmethod
    def get_node_type(node: Dict[str, Any]) -> str:
        """
        Get the type of an AST node.
        
        Args:
            node: AST node
            
        Returns:
            Node type string
        """
        return node.get('type', '') if node else ''
    
    @staticmethod
    def is_store_path(node: Dict[str, Any]) -> bool:
        """
        Check if a node represents a store path (Page.*, Store.*, Url.*, Parent.*).
        
        Args:
            node: AST node
            
        Returns:
            True if the node is a store path
        """
        if node.get('type') != 'MemberExpression':
            return False
        
        obj = node.get('object', {})
        if obj.get('type') == 'Identifier':
            name = obj.get('name', '')
            return name in ('Page', 'Store', 'Url', 'Parent', 'Steps', 'Arguments', 'Context')
        
        # Nested member expression (e.g., Page.formData.email)
        return JSParser.is_store_path(obj)
    
    @staticmethod
    def get_store_root(node: Dict[str, Any]) -> Optional[str]:
        """
        Get the root store prefix from a member expression.
        
        Args:
            node: MemberExpression AST node
            
        Returns:
            Store prefix (Page, Store, etc.) or None
        """
        if node.get('type') != 'MemberExpression':
            return None
        
        obj = node.get('object', {})
        if obj.get('type') == 'Identifier':
            name = obj.get('name', '')
            if name in ('Page', 'Store', 'Url', 'Parent', 'Steps', 'Arguments', 'Context'):
                return name
            return None
        
        return JSParser.get_store_root(obj)

