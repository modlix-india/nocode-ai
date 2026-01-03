"""
AST Analyzer for JavaScript to KIRun conversion.

Traverses the JavaScript AST and identifies patterns for conversion.
"""
from typing import Dict, Any, List, Optional, Tuple
from .parser import JSParser
from .patterns import PatternMatcher, PatternMatchResult
from .builder import StatementNameGenerator
from .types import KIRunStatement


class AnalysisResult:
    """Result of analyzing JavaScript AST."""
    
    def __init__(self):
        self.statements: Dict[str, KIRunStatement] = {}
        self.warnings: List[str] = []
        self.errors: List[str] = []
        self.statement_order: List[str] = []
    
    def add_statement(self, statement: KIRunStatement):
        """Add a statement to the result."""
        name = statement['statementName']
        self.statements[name] = statement
        self.statement_order.append(name)
    
    def add_statements(self, statements: List[KIRunStatement]):
        """Add multiple statements to the result."""
        for stmt in statements:
            self.add_statement(stmt)
    
    def add_warning(self, warning: str):
        """Add a warning message."""
        self.warnings.append(warning)
    
    def add_error(self, error: str):
        """Add an error message."""
        self.errors.append(error)


class DependencyTracker:
    """
    Tracks dependencies between statements.
    
    Analyzes step references in expressions and maintains
    the dependency graph for execution order.
    """
    
    def __init__(self):
        self.step_outputs: Dict[str, str] = {}  # Maps step name to output event
        self.pending_dependencies: List[Tuple[str, str]] = []
    
    def register_step(self, step_name: str, output_event: str = "output"):
        """Register a step and its output event."""
        self.step_outputs[step_name] = output_event
    
    def find_step_references(self, expression: str) -> List[str]:
        """
        Find all Step references in an expression.
        
        Args:
            expression: KIRun expression string
            
        Returns:
            List of step paths (e.g., ["Steps.fetchData1.output"])
        """
        import re
        pattern = r'Steps\.([a-zA-Z0-9]+)\.([a-zA-Z0-9]+)'
        matches = re.findall(pattern, expression)
        
        return [f"Steps.{m[0]}.{m[1]}" for m in matches]
    
    def analyze_dependencies(
        self,
        statements: Dict[str, KIRunStatement]
    ) -> Dict[str, KIRunStatement]:
        """
        Analyze and update dependencies for all statements.
        
        Ensures that statements referencing step outputs have
        proper dependentStatements set.
        
        Args:
            statements: Dictionary of statements
            
        Returns:
            Updated statements with proper dependencies
        """
        # First pass: register all steps
        for name in statements:
            self.register_step(name)
        
        # Second pass: analyze and update dependencies
        for name, stmt in statements.items():
            param_map = stmt.get('parameterMap', {})
            
            for param_name, refs in param_map.items():
                for ref_key, ref in refs.items():
                    if ref.get('type') == 'EXPRESSION':
                        expr = ref.get('expression', '')
                        step_refs = self.find_step_references(expr)
                        
                        if step_refs:
                            if 'dependentStatements' not in stmt:
                                stmt['dependentStatements'] = {}
                            
                            for ref_path in step_refs:
                                stmt['dependentStatements'][ref_path] = True
        
        return statements


class JSAnalyzer:
    """
    Analyzes JavaScript AST and converts to KIRun statements.
    
    Traverses the AST, matches patterns, and builds the
    collection of KIRun statements with proper dependencies.
    """
    
    def __init__(self):
        self.name_generator = StatementNameGenerator()
        self.pattern_matcher = PatternMatcher(self.name_generator)
        self.dependency_tracker = DependencyTracker()
    
    def reset(self):
        """Reset the analyzer state."""
        self.name_generator.reset()
        self.pattern_matcher.reset()
        self.dependency_tracker = DependencyTracker()
    
    def analyze(self, ast: Dict[str, Any]) -> AnalysisResult:
        """
        Analyze a JavaScript AST and convert to KIRun statements.
        
        Args:
            ast: Parsed JavaScript AST
            
        Returns:
            AnalysisResult with statements, warnings, and errors
        """
        result = AnalysisResult()
        
        # Get all top-level statements
        body = ast.get('body', [])
        
        # Track the previous statement for sequential dependencies
        prev_statement_name: Optional[str] = None
        
        for node in body:
            match_result = self.pattern_matcher.match_statement(node)
            
            if match_result.matched:
                # Add sequential dependency if needed
                for stmt in match_result.statements:
                    # Only add sequential dependency if no other dependencies exist
                    if prev_statement_name and not stmt.get('dependentStatements'):
                        # Check if this statement references the previous one
                        # If not, we may want to add implicit ordering
                        pass
                    
                    result.add_statement(stmt)
                    prev_statement_name = stmt['statementName']
            
            # Collect warnings and errors
            result.warnings.extend(match_result.warnings)
            result.errors.extend(match_result.errors)
        
        # Analyze and update dependencies
        result.statements = self.dependency_tracker.analyze_dependencies(
            result.statements
        )
        
        return result
    
    def analyze_code(self, js_code: str) -> AnalysisResult:
        """
        Analyze JavaScript code string.
        
        Args:
            js_code: JavaScript code to analyze
            
        Returns:
            AnalysisResult with statements, warnings, and errors
        """
        from .parser import JSParser, ParseError
        
        result = AnalysisResult()
        
        try:
            ast = JSParser.parse(js_code)
            return self.analyze(ast)
        except ParseError as e:
            result.add_error(f"Parse error: {e.message} at line {e.line}, column {e.column}")
            return result
    
    def get_execution_order(
        self,
        statements: Dict[str, KIRunStatement]
    ) -> List[str]:
        """
        Determine the execution order of statements based on dependencies.
        
        Args:
            statements: Dictionary of statements
            
        Returns:
            List of statement names in execution order
        """
        # Build dependency graph
        graph: Dict[str, List[str]] = {name: [] for name in statements}
        in_degree: Dict[str, int] = {name: 0 for name in statements}
        
        for name, stmt in statements.items():
            deps = stmt.get('dependentStatements', {})
            for dep_path in deps:
                # Extract step name from path like "Steps.fetchData1.output"
                parts = dep_path.split('.')
                if len(parts) >= 2 and parts[0] == 'Steps':
                    dep_name = parts[1]
                    if dep_name in graph:
                        graph[dep_name].append(name)
                        in_degree[name] += 1
        
        # Topological sort using Kahn's algorithm
        order = []
        queue = [name for name, degree in in_degree.items() if degree == 0]
        
        while queue:
            current = queue.pop(0)
            order.append(current)
            
            for dependent in graph[current]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)
        
        # Check for cycles
        if len(order) != len(statements):
            # There's a cycle - return original order
            return list(statements.keys())
        
        return order


def analyze_javascript(js_code: str) -> AnalysisResult:
    """
    Convenience function to analyze JavaScript code.
    
    Args:
        js_code: JavaScript code to analyze
        
    Returns:
        AnalysisResult with statements, warnings, and errors
    """
    analyzer = JSAnalyzer()
    return analyzer.analyze_code(js_code)

