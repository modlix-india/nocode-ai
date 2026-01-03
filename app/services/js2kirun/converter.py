"""
Main JavaScript to KIRun Converter.

Converts JavaScript code to KIRun function definitions.
"""
from typing import Dict, Any, Optional, List
import json

from .parser import JSParser, ParseError
from .analyzer import JSAnalyzer, AnalysisResult
from .builder import create_function_definition, StatementNameGenerator
from .types import (
    KIRunFunctionDefinition,
    KIRunStatement,
    ConversionOptions,
    ConversionContext,
    ConvertedResult,
)


class JS2KIRunConverter:
    """
    Converts JavaScript code to KIRun function definitions.
    
    This converter parses JavaScript code, analyzes the AST,
    matches patterns to KIRun functions, and builds a complete
    function definition.
    
    Key assumptions:
    - All data access is through Page/Store/Url/Parent paths
    - No local variables - identifiers not matching KIRun paths generate warnings
    - Statement names use camelCase without underscores
    
    Example:
        converter = JS2KIRunConverter()
        result = converter.convert('''
            Page.counter = Page.counter + 1;
        ''')
        print(json.dumps(result['functionDefinition'], indent=2))
    """
    
    def __init__(self):
        self.analyzer = JSAnalyzer()
    
    def convert(
        self,
        js_code: str,
        options: ConversionOptions = None
    ) -> ConvertedResult:
        """
        Convert JavaScript code to a KIRun function definition.
        
        Args:
            js_code: JavaScript code to convert
            options: Conversion options (namespace, function name, etc.)
            
        Returns:
            ConvertedResult with function definition, errors, and warnings
        """
        options = options or {}
        
        # Reset analyzer state
        self.analyzer.reset()
        
        # Analyze the JavaScript code
        analysis = self.analyzer.analyze_code(js_code)
        
        # Build the function definition
        func_name = options.get('functionName', 'eventHandler')
        namespace = options.get('namespace', '')
        
        func_def = create_function_definition(
            name=func_name,
            namespace=namespace,
            steps=analysis.statements
        )
        
        return {
            'functionDefinition': func_def,
            'errors': analysis.errors,
            'warnings': analysis.warnings
        }
    
    def convert_to_json(
        self,
        js_code: str,
        options: ConversionOptions = None,
        indent: int = 2
    ) -> str:
        """
        Convert JavaScript code to JSON string.
        
        Args:
            js_code: JavaScript code to convert
            options: Conversion options
            indent: JSON indentation
            
        Returns:
            JSON string of the function definition
        """
        result = self.convert(js_code, options)
        return json.dumps(result['functionDefinition'], indent=indent)
    
    def convert_event_handler(
        self,
        js_code: str,
        event_name: str,
        namespace: str = ""
    ) -> ConvertedResult:
        """
        Convert JavaScript code for an event handler.
        
        Args:
            js_code: JavaScript code for the event
            event_name: Name of the event (e.g., "onClick", "onSubmit")
            namespace: Optional namespace
            
        Returns:
            ConvertedResult with function definition
        """
        return self.convert(js_code, {
            'functionName': event_name,
            'namespace': namespace
        })
    
    def validate_code(self, js_code: str) -> Dict[str, Any]:
        """
        Validate JavaScript code without full conversion.
        
        Checks for:
        - Parse errors
        - Unsupported patterns
        - Local variable usage
        
        Args:
            js_code: JavaScript code to validate
            
        Returns:
            Dictionary with 'valid' boolean, 'errors', and 'warnings'
        """
        try:
            ast = JSParser.parse(js_code)
        except ParseError as e:
            return {
                'valid': False,
                'errors': [f"Parse error: {e.message}"],
                'warnings': []
            }
        
        # Quick analysis for validation
        analysis = self.analyzer.analyze(ast)
        
        return {
            'valid': len(analysis.errors) == 0,
            'errors': analysis.errors,
            'warnings': analysis.warnings
        }


class EventFunctionConverter:
    """
    Specialized converter for page event functions.
    
    Converts JavaScript code to the event function format
    used in page definitions.
    """
    
    def __init__(self):
        self.converter = JS2KIRunConverter()
    
    def convert(
        self,
        js_code: str,
        event_name: str = "eventHandler"
    ) -> Dict[str, Any]:
        """
        Convert JavaScript to page event function format.
        
        Args:
            js_code: JavaScript code
            event_name: Name for the event function
            
        Returns:
            Event function definition ready for page eventFunctions
        """
        result = self.converter.convert(js_code, {
            'functionName': event_name
        })
        
        func_def = result['functionDefinition']
        
        return {
            'name': event_name,
            'namespace': func_def.get('namespace', ''),
            'steps': func_def.get('steps', {})
        }
    
    def convert_multiple(
        self,
        event_handlers: Dict[str, str]
    ) -> Dict[str, Dict[str, Any]]:
        """
        Convert multiple event handlers.
        
        Args:
            event_handlers: Dictionary mapping event names to JavaScript code
            
        Returns:
            Dictionary of event function definitions
        """
        result = {}
        
        for event_name, js_code in event_handlers.items():
            event_func = self.convert(js_code, event_name)
            # Generate a unique key for the event function
            from .builder import generate_id
            key = generate_id(20)
            result[key] = event_func
        
        return result


def convert_js_to_kirun(
    js_code: str,
    function_name: str = "eventHandler",
    namespace: str = ""
) -> ConvertedResult:
    """
    Convenience function to convert JavaScript to KIRun.
    
    Args:
        js_code: JavaScript code to convert
        function_name: Name for the function
        namespace: Optional namespace
        
    Returns:
        ConvertedResult with function definition
    """
    converter = JS2KIRunConverter()
    return converter.convert(js_code, {
        'functionName': function_name,
        'namespace': namespace
    })


def convert_js_to_json(
    js_code: str,
    function_name: str = "eventHandler",
    namespace: str = "",
    indent: int = 2
) -> str:
    """
    Convenience function to convert JavaScript to JSON.
    
    Args:
        js_code: JavaScript code to convert
        function_name: Name for the function
        namespace: Optional namespace
        indent: JSON indentation
        
    Returns:
        JSON string of the function definition
    """
    converter = JS2KIRunConverter()
    return converter.convert_to_json(js_code, {
        'functionName': function_name,
        'namespace': namespace
    }, indent)

