"""
Pattern matchers for JavaScript to KIRun conversion.

Matches JavaScript patterns and converts them to KIRun function calls.
"""
from typing import Dict, Any, Optional, List, Tuple
from .expression import ExpressionConverter, is_store_path_expression
from .builder import (
    StatementNameGenerator,
    create_set_store_statement,
    create_fetch_data_statement,
    create_send_data_statement,
    create_navigate_statement,
    create_message_statement,
    create_wait_statement,
    create_if_statement,
    create_statement,
    create_parameter_reference,
    generate_key,
)
from .types import KIRunStatement


# Store path prefixes
STORE_PREFIXES = ('Page', 'Store', 'Url', 'Parent')


class PatternMatchResult:
    """Result of a pattern match."""
    
    def __init__(
        self,
        matched: bool = False,
        statements: List[KIRunStatement] = None,
        warnings: List[str] = None,
        errors: List[str] = None
    ):
        self.matched = matched
        self.statements = statements or []
        self.warnings = warnings or []
        self.errors = errors or []


class PatternMatcher:
    """
    Matches JavaScript AST patterns to KIRun function calls.
    
    Provides pattern matching for:
    - Store operations (Page.x = value, Store.x = value)
    - API calls (fetch)
    - Navigation
    - Conditionals (if/else)
    - Loops
    - Function calls
    """
    
    def __init__(self, name_generator: StatementNameGenerator):
        self.name_generator = name_generator
        self.expression_converter = ExpressionConverter()
        self.warnings: List[str] = []
        self.errors: List[str] = []
    
    def reset(self):
        """Reset state."""
        self.expression_converter.reset()
        self.warnings = []
        self.errors = []
    
    def match_statement(self, node: Dict[str, Any]) -> PatternMatchResult:
        """
        Match a statement node and convert to KIRun.
        
        Args:
            node: AST statement node
            
        Returns:
            PatternMatchResult with matched statements
        """
        node_type = node.get('type', '')
        
        if node_type == 'ExpressionStatement':
            return self._match_expression_statement(node)
        elif node_type == 'IfStatement':
            return self._match_if_statement(node)
        elif node_type == 'ForStatement':
            return self._match_for_statement(node)
        elif node_type == 'ForOfStatement':
            return self._match_for_of_statement(node)
        elif node_type == 'ForInStatement':
            return self._match_for_in_statement(node)
        elif node_type == 'WhileStatement':
            return self._match_while_statement(node)
        elif node_type == 'VariableDeclaration':
            return self._match_variable_declaration(node)
        elif node_type == 'ReturnStatement':
            return self._match_return_statement(node)
        elif node_type == 'BlockStatement':
            return self._match_block_statement(node)
        
        return PatternMatchResult(
            matched=False,
            warnings=[f"Unhandled statement type: {node_type}"]
        )
    
    def _match_expression_statement(self, node: Dict[str, Any]) -> PatternMatchResult:
        """Match an expression statement."""
        expression = node.get('expression', {})
        return self._match_expression(expression)
    
    def _match_expression(self, node: Dict[str, Any]) -> PatternMatchResult:
        """Match an expression and convert to KIRun."""
        expr_type = node.get('type', '')
        
        if expr_type == 'AssignmentExpression':
            return self._match_assignment(node)
        elif expr_type == 'CallExpression':
            return self._match_call_expression(node)
        elif expr_type == 'AwaitExpression':
            return self._match_await_expression(node)
        elif expr_type == 'UpdateExpression':
            return self._match_update_expression(node)
        
        return PatternMatchResult(matched=False)
    
    def _match_assignment(self, node: Dict[str, Any]) -> PatternMatchResult:
        """
        Match assignment expressions like Page.x = value.
        
        Converts to SetStore statement.
        """
        left = node.get('left', {})
        right = node.get('right', {})
        operator = node.get('operator', '=')
        
        # Check if left side is a store path
        if not self._is_store_path(left):
            return PatternMatchResult(
                matched=False,
                warnings=["Assignment to non-store path - local variables not supported"]
            )
        
        # Get the store path
        path = self.expression_converter.convert(left)
        
        # Handle compound assignment operators
        if operator != '=':
            # e.g., Page.x += 1 becomes Page.x = Page.x + 1
            op = operator[:-1]  # Remove the '=' from +=, -=, etc.
            value_expr = self.expression_converter.convert(right)
            expression = f"({path} {op} {value_expr})"
            statement = create_set_store_statement(
                self.name_generator,
                path=path,
                expression=expression
            )
        elif right.get('type') == 'Literal':
            # Simple literal value - use VALUE type
            value = right.get('value')
            statement = create_set_store_statement(
                self.name_generator,
                path=path,
                value=value
            )
        else:
            # Expression
            expression = self.expression_converter.convert(right)
            statement = create_set_store_statement(
                self.name_generator,
                path=path,
                expression=expression
            )
        
        return PatternMatchResult(
            matched=True,
            statements=[statement],
            warnings=self.expression_converter.warnings,
            errors=self.expression_converter.errors
        )
    
    def _match_update_expression(self, node: Dict[str, Any]) -> PatternMatchResult:
        """
        Match update expressions like Page.counter++.
        
        Converts to SetStore statement.
        """
        argument = node.get('argument', {})
        operator = node.get('operator', '')
        
        if not self._is_store_path(argument):
            return PatternMatchResult(
                matched=False,
                warnings=["Update expression on non-store path"]
            )
        
        path = self.expression_converter.convert(argument)
        
        if operator == '++':
            expression = f"({path} + 1)"
        elif operator == '--':
            expression = f"({path} - 1)"
        else:
            return PatternMatchResult(matched=False)
        
        statement = create_set_store_statement(
            self.name_generator,
            path=path,
            expression=expression
        )
        
        return PatternMatchResult(
            matched=True,
            statements=[statement]
        )
    
    def _match_call_expression(self, node: Dict[str, Any]) -> PatternMatchResult:
        """Match function call expressions."""
        callee = node.get('callee', {})
        args = node.get('arguments', [])
        
        # Check for specific function patterns
        callee_type = callee.get('type', '')
        
        if callee_type == 'Identifier':
            func_name = callee.get('name', '')
            return self._match_function_call(func_name, args)
        
        elif callee_type == 'MemberExpression':
            return self._match_member_call(callee, args)
        
        return PatternMatchResult(matched=False)
    
    def _match_function_call(
        self,
        func_name: str,
        args: List[Dict[str, Any]]
    ) -> PatternMatchResult:
        """Match direct function calls like navigate(), fetch(), wait(), etc."""
        
        if func_name == 'fetch':
            return self._match_fetch(args)
        elif func_name == 'navigate':
            return self._match_navigate(args)
        elif func_name == 'wait' or func_name == 'delay' or func_name == 'sleep':
            return self._match_wait(args)
        elif func_name == 'alert' or func_name == 'showMessage':
            return self._match_message(args)
        elif func_name == 'setStore':
            return self._match_set_store_call(args)
        
        # Generic function call - might be a custom function
        return self._match_generic_function(func_name, "", args)
    
    def _match_member_call(
        self,
        callee: Dict[str, Any],
        args: List[Dict[str, Any]]
    ) -> PatternMatchResult:
        """Match member function calls like router.push(), console.log(), etc."""
        obj = callee.get('object', {})
        prop = callee.get('property', {})
        method = prop.get('name', '') if prop.get('type') == 'Identifier' else ''
        
        obj_name = obj.get('name', '') if obj.get('type') == 'Identifier' else ''
        
        # Router navigation patterns
        if obj_name == 'router' and method in ('push', 'replace'):
            return self._match_navigate(args, replace=method == 'replace')
        
        # Window location pattern
        if obj_name == 'window' or (obj.get('type') == 'MemberExpression'):
            obj_str = self.expression_converter.convert(obj)
            if 'location' in obj_str:
                if method == 'assign' or method == 'replace':
                    return self._match_navigate(args)
        
        # Console.log - convert to Print or ignore
        if obj_name == 'console' and method == 'log':
            return self._match_print(args)
        
        # Array methods on store paths
        if self._is_store_path(obj):
            if method == 'push':
                return self._match_array_push(obj, args)
            elif method == 'forEach':
                return self._match_for_each(obj, args)
            elif method == 'map':
                return self._match_array_map(obj, args)
            elif method == 'filter':
                return self._match_array_filter(obj, args)
        
        return PatternMatchResult(matched=False)
    
    def _match_await_expression(self, node: Dict[str, Any]) -> PatternMatchResult:
        """Match await expressions like await fetch(...)."""
        argument = node.get('argument', {})
        
        if argument.get('type') == 'CallExpression':
            return self._match_call_expression(argument)
        
        return PatternMatchResult(matched=False)
    
    def _match_fetch(self, args: List[Dict[str, Any]]) -> PatternMatchResult:
        """Match fetch() calls and convert to FetchData or SendData."""
        if not args:
            return PatternMatchResult(
                matched=False,
                errors=["fetch() requires at least one argument (url)"]
            )
        
        # Get URL
        url_node = args[0]
        url_expr = self.expression_converter.convert(url_node)
        url_is_expression = not (url_node.get('type') == 'Literal')
        
        # Check for options (second argument)
        method = 'GET'
        payload = None
        payload_is_expression = False
        headers = None
        
        if len(args) > 1:
            options = args[1]
            if options.get('type') == 'ObjectExpression':
                for prop in options.get('properties', []):
                    key = prop.get('key', {})
                    value = prop.get('value', {})
                    key_name = key.get('name', '') if key.get('type') == 'Identifier' else ''
                    
                    if key_name == 'method':
                        if value.get('type') == 'Literal':
                            method = value.get('value', 'GET').upper()
                    elif key_name == 'body':
                        payload = self.expression_converter.convert(value)
                        payload_is_expression = True
                    elif key_name == 'headers':
                        headers = self._extract_object_literal(value)
        
        # Clean up URL if it's a string literal
        if url_is_expression == False and url_expr.startswith('"') and url_expr.endswith('"'):
            url = url_expr[1:-1]
            url_is_expression = False
        else:
            url = url_expr
            url_is_expression = True
        
        # Create appropriate statement based on method
        if method == 'GET':
            statement = create_fetch_data_statement(
                self.name_generator,
                url=url,
                url_is_expression=url_is_expression,
                headers=headers
            )
        else:
            statement = create_send_data_statement(
                self.name_generator,
                url=url,
                method=method,
                url_is_expression=url_is_expression,
                payload=payload,
                payload_is_expression=payload_is_expression,
                headers=headers
            )
        
        return PatternMatchResult(
            matched=True,
            statements=[statement]
        )
    
    def _match_navigate(
        self,
        args: List[Dict[str, Any]],
        replace: bool = False
    ) -> PatternMatchResult:
        """Match navigate() calls."""
        if not args:
            return PatternMatchResult(
                matched=False,
                errors=["navigate() requires a path argument"]
            )
        
        path_node = args[0]
        path_expr = self.expression_converter.convert(path_node)
        
        # Check if it's a literal string
        if path_node.get('type') == 'Literal':
            path = path_node.get('value', '')
            path_is_expression = False
        else:
            path = path_expr
            path_is_expression = True
        
        statement = create_navigate_statement(
            self.name_generator,
            link_path=path,
            path_is_expression=path_is_expression
        )
        
        return PatternMatchResult(
            matched=True,
            statements=[statement]
        )
    
    def _match_wait(
        self,
        args: List[Dict[str, Any]]
    ) -> PatternMatchResult:
        """Match wait(milliseconds) calls for delays."""
        if not args:
            return PatternMatchResult(
                matched=False,
                errors=["wait() requires a milliseconds argument"]
            )
        
        millis_node = args[0]
        
        # Check if it's a literal number
        if millis_node.get('type') == 'Literal':
            millis = millis_node.get('value', 0)
            millis_is_expression = False
        else:
            millis = self.expression_converter.convert(millis_node)
            millis_is_expression = True
        
        statement = create_wait_statement(
            self.name_generator,
            millis=millis,
            millis_is_expression=millis_is_expression
        )
        
        return PatternMatchResult(
            matched=True,
            statements=[statement]
        )
    
    def _match_message(self, args: List[Dict[str, Any]]) -> PatternMatchResult:
        """Match alert() or showMessage() calls."""
        if not args:
            return PatternMatchResult(
                matched=False,
                errors=["Message function requires a message argument"]
            )
        
        msg_node = args[0]
        msg_expr = self.expression_converter.convert(msg_node)
        
        if msg_node.get('type') == 'Literal':
            msg = msg_node.get('value', '')
            msg_is_expression = False
        else:
            msg = msg_expr
            msg_is_expression = True
        
        # Determine message type from second argument if provided
        msg_type = "INFO"
        if len(args) > 1:
            type_node = args[1]
            if type_node.get('type') == 'Literal':
                msg_type = type_node.get('value', 'INFO').upper()
        
        statement = create_message_statement(
            self.name_generator,
            msg=msg,
            msg_is_expression=msg_is_expression,
            msg_type=msg_type
        )
        
        return PatternMatchResult(
            matched=True,
            statements=[statement]
        )
    
    def _match_set_store_call(self, args: List[Dict[str, Any]]) -> PatternMatchResult:
        """Match setStore(path, value) calls."""
        if len(args) < 2:
            return PatternMatchResult(
                matched=False,
                errors=["setStore() requires path and value arguments"]
            )
        
        path_node = args[0]
        value_node = args[1]
        
        if path_node.get('type') == 'Literal':
            path = path_node.get('value', '')
        else:
            path = self.expression_converter.convert(path_node)
        
        value_expr = self.expression_converter.convert(value_node)
        
        statement = create_set_store_statement(
            self.name_generator,
            path=path,
            expression=value_expr
        )
        
        return PatternMatchResult(
            matched=True,
            statements=[statement]
        )
    
    def _match_print(self, args: List[Dict[str, Any]]) -> PatternMatchResult:
        """Match console.log() and convert to Print statement."""
        param_map = {}
        
        for i, arg in enumerate(args):
            value_expr = self.expression_converter.convert(arg)
            ref = create_parameter_reference(expression=value_expr)
            param_map[f"values"] = param_map.get("values", {})
            param_map["values"][ref["key"]] = ref
        
        statement = create_statement(
            statement_name=self.name_generator.generate("print"),
            name="Print",
            namespace="System",
            parameter_map=param_map
        )
        
        return PatternMatchResult(
            matched=True,
            statements=[statement]
        )
    
    def _match_if_statement(self, node: Dict[str, Any]) -> PatternMatchResult:
        """
        Match if statements and convert to KIRun.
        
        Special handling:
        - if (Steps.xxx.output) { ... } -> adds dependentStatements to children
        - if (Steps.xxx.error) { ... } -> adds dependentStatements to children
        - if (condition) { ... } -> creates If statement with branches
        """
        test = node.get('test', {})
        consequent = node.get('consequent', {})
        alternate = node.get('alternate')
        
        # Convert condition
        condition = self.expression_converter.convert(test)
        
        # Check if this is a Steps.xxx.output or Steps.xxx.error pattern
        # These are NOT real If statements - they represent branch dependencies
        import re
        branch_dep_match = re.match(r'^Steps\.(\w+)\.(output|error)$', condition)
        
        if branch_dep_match and not alternate:
            # This is a branch dependency wrapper, not a real If statement
            step_name = branch_dep_match.group(1)
            branch = branch_dep_match.group(2)
            dep_key = f"Steps.{step_name}.{branch}"
            
            # Process consequent statements and add dependency
            true_result = self.match_statement(consequent)
            if true_result.matched:
                for stmt in true_result.statements:
                    # Add dependency on the branch
                    stmt['dependentStatements'] = stmt.get('dependentStatements', {})
                    stmt['dependentStatements'][dep_key] = True
                
                return PatternMatchResult(
                    matched=True,
                    statements=true_result.statements,
                    warnings=true_result.warnings,
                    errors=true_result.errors
                )
            
            return PatternMatchResult(matched=False)
        
        # Regular If statement - create KIRun If
        if_statement = create_if_statement(
            self.name_generator,
            condition=condition
        )
        if_name = if_statement['statementName']
        
        statements = [if_statement]
        
        # Process true branch (consequent)
        true_result = self.match_statement(consequent)
        if true_result.matched:
            for stmt in true_result.statements:
                # Add dependency on if.true
                stmt['dependentStatements'] = stmt.get('dependentStatements', {})
                stmt['dependentStatements'][f'Steps.{if_name}.true'] = True
            statements.extend(true_result.statements)
        
        # Process false branch (alternate) if exists
        if alternate:
            false_result = self.match_statement(alternate)
            if false_result.matched:
                for stmt in false_result.statements:
                    # Add dependency on if.false
                    stmt['dependentStatements'] = stmt.get('dependentStatements', {})
                    stmt['dependentStatements'][f'Steps.{if_name}.false'] = True
                statements.extend(false_result.statements)
        
        return PatternMatchResult(
            matched=True,
            statements=statements
        )
    
    def _match_block_statement(self, node: Dict[str, Any]) -> PatternMatchResult:
        """Match a block statement (curly braces with statements)."""
        body = node.get('body', [])
        all_statements = []
        all_warnings = []
        all_errors = []
        
        for stmt in body:
            result = self.match_statement(stmt)
            if result.matched:
                all_statements.extend(result.statements)
            all_warnings.extend(result.warnings)
            all_errors.extend(result.errors)
        
        return PatternMatchResult(
            matched=len(all_statements) > 0,
            statements=all_statements,
            warnings=all_warnings,
            errors=all_errors
        )
    
    def _match_for_statement(self, node: Dict[str, Any]) -> PatternMatchResult:
        """Match for loop and convert to RangeLoop or CountLoop."""
        init = node.get('init', {})
        test = node.get('test', {})
        update = node.get('update', {})
        body = node.get('body', {})
        
        # Try to detect loop bounds
        # Pattern: for (let i = 0; i < n; i++)
        from_val = "0"
        to_expr = None
        
        # Check test for upper bound
        if test.get('type') == 'BinaryExpression':
            if test.get('operator') in ('<', '<='):
                right = test.get('right', {})
                to_expr = self.expression_converter.convert(right)
        
        if not to_expr:
            return PatternMatchResult(
                matched=False,
                warnings=["Could not determine loop bounds"]
            )
        
        # Create RangeLoop statement
        param_map = {
            "from": {generate_key(): create_parameter_reference(value=0)},
            "to": {generate_key(): create_parameter_reference(expression=to_expr)}
        }
        
        loop_statement = create_statement(
            statement_name=self.name_generator.generate("rangeLoop"),
            name="RangeLoop",
            namespace="System.Loop",
            parameter_map=param_map
        )
        loop_name = loop_statement['statementName']
        
        statements = [loop_statement]
        
        # Process body
        body_result = self.match_statement(body)
        if body_result.matched:
            for stmt in body_result.statements:
                # Add dependency on loop iteration
                stmt['dependentStatements'] = stmt.get('dependentStatements', {})
                stmt['dependentStatements'][f'Steps.{loop_name}.iteration'] = True
            statements.extend(body_result.statements)
        
        return PatternMatchResult(
            matched=True,
            statements=statements
        )
    
    def _match_for_of_statement(self, node: Dict[str, Any]) -> PatternMatchResult:
        """Match for...of loop and convert to ForEachLoop."""
        right = node.get('right', {})
        body = node.get('body', {})
        
        # Get the array expression
        array_expr = self.expression_converter.convert(right)
        
        # Create ForEachLoop statement
        param_map = {
            "source": {generate_key(): create_parameter_reference(expression=array_expr)}
        }
        
        loop_statement = create_statement(
            statement_name=self.name_generator.generate("forEachLoop"),
            name="ForEachLoop",
            namespace="System.Loop",
            parameter_map=param_map
        )
        loop_name = loop_statement['statementName']
        
        statements = [loop_statement]
        
        # Process body
        body_result = self.match_statement(body)
        if body_result.matched:
            for stmt in body_result.statements:
                stmt['dependentStatements'] = stmt.get('dependentStatements', {})
                stmt['dependentStatements'][f'Steps.{loop_name}.iteration'] = True
            statements.extend(body_result.statements)
        
        return PatternMatchResult(
            matched=True,
            statements=statements
        )
    
    def _match_for_in_statement(self, node: Dict[str, Any]) -> PatternMatchResult:
        """Match for...in loop - similar to for...of."""
        return self._match_for_of_statement(node)
    
    def _match_while_statement(self, node: Dict[str, Any]) -> PatternMatchResult:
        """Match while loop - convert to CountLoop with high count."""
        return PatternMatchResult(
            matched=False,
            warnings=["while loops are not fully supported - consider using for loops"]
        )
    
    def _match_variable_declaration(self, node: Dict[str, Any]) -> PatternMatchResult:
        """
        Match variable declarations.
        
        Variable declarations are only supported if they're immediately assigned
        to a store path or if the right side is a supported function call.
        """
        declarations = node.get('declarations', [])
        all_statements = []
        all_warnings = []
        
        for decl in declarations:
            init = decl.get('init')
            
            if not init:
                continue
            
            # Check if the init is a function call we can handle
            if init.get('type') == 'CallExpression' or init.get('type') == 'AwaitExpression':
                result = self._match_expression(init)
                if result.matched:
                    all_statements.extend(result.statements)
                    all_warnings.extend(result.warnings)
                continue
            
            # Warn about local variable
            var_name = decl.get('id', {}).get('name', 'unknown')
            all_warnings.append(
                f"Variable declaration '{var_name}' - local variables not supported. "
                f"Use Page.{var_name} or Store.{var_name} instead."
            )
        
        return PatternMatchResult(
            matched=len(all_statements) > 0,
            statements=all_statements,
            warnings=all_warnings
        )
    
    def _match_return_statement(self, node: Dict[str, Any]) -> PatternMatchResult:
        """Match return statement - convert to GenerateEvent output."""
        argument = node.get('argument')
        
        if not argument:
            return PatternMatchResult(matched=False)
        
        value_expr = self.expression_converter.convert(argument)
        
        # Create GenerateEvent statement
        param_map = {
            "eventName": {generate_key(): create_parameter_reference(value="output")},
            "results": {generate_key(): create_parameter_reference(value={
                "name": "returnValue",
                "value": {
                    "isExpression": True,
                    "value": value_expr
                }
            })}
        }
        
        statement = create_statement(
            statement_name=self.name_generator.generate("generateEvent"),
            name="GenerateEvent",
            namespace="System",
            parameter_map=param_map
        )
        
        return PatternMatchResult(
            matched=True,
            statements=[statement]
        )
    
    def _match_array_push(
        self,
        array: Dict[str, Any],
        args: List[Dict[str, Any]]
    ) -> PatternMatchResult:
        """Match array.push() and convert to Array.InsertLast."""
        array_expr = self.expression_converter.convert(array)
        
        param_map = {
            "source": {generate_key(): create_parameter_reference(expression=array_expr)}
        }
        
        for i, arg in enumerate(args):
            value_expr = self.expression_converter.convert(arg)
            ref = create_parameter_reference(expression=value_expr, order=i+1)
            if "element" not in param_map:
                param_map["element"] = {}
            param_map["element"][ref["key"]] = ref
        
        statement = create_statement(
            statement_name=self.name_generator.generate("insertLast"),
            name="InsertLast",
            namespace="System.Array",
            parameter_map=param_map
        )
        
        return PatternMatchResult(
            matched=True,
            statements=[statement]
        )
    
    def _match_for_each(
        self,
        array: Dict[str, Any],
        args: List[Dict[str, Any]]
    ) -> PatternMatchResult:
        """Match array.forEach() and convert to ForEachLoop."""
        array_expr = self.expression_converter.convert(array)
        
        param_map = {
            "source": {generate_key(): create_parameter_reference(expression=array_expr)}
        }
        
        loop_statement = create_statement(
            statement_name=self.name_generator.generate("forEachLoop"),
            name="ForEachLoop",
            namespace="System.Loop",
            parameter_map=param_map
        )
        
        # Note: The callback function body would need to be processed
        # This is a simplified version
        return PatternMatchResult(
            matched=True,
            statements=[loop_statement],
            warnings=["forEach callback body may not be fully converted"]
        )
    
    def _match_array_map(
        self,
        array: Dict[str, Any],
        args: List[Dict[str, Any]]
    ) -> PatternMatchResult:
        """Match array.map() - warn that it's not fully supported."""
        return PatternMatchResult(
            matched=False,
            warnings=["array.map() is not directly supported - use ForEachLoop instead"]
        )
    
    def _match_array_filter(
        self,
        array: Dict[str, Any],
        args: List[Dict[str, Any]]
    ) -> PatternMatchResult:
        """Match array.filter() - use Array filter function."""
        array_expr = self.expression_converter.convert(array)
        
        if args and args[0].get('type') == 'ArrowFunctionExpression':
            callback = args[0]
            body = callback.get('body', {})
            condition = self.expression_converter.convert(body)
            
            param_map = {
                "source": {generate_key(): create_parameter_reference(expression=array_expr)},
                "condition": {generate_key(): create_parameter_reference(expression=condition)}
            }
            
            statement = create_statement(
                statement_name=self.name_generator.generate("filter"),
                name="Filter",
                namespace="System.Array",
                parameter_map=param_map
            )
            
            return PatternMatchResult(
                matched=True,
                statements=[statement],
                warnings=["Filter condition may need adjustment for KIRun syntax"]
            )
        
        return PatternMatchResult(
            matched=False,
            warnings=["array.filter() requires an arrow function callback"]
        )
    
    def _match_generic_function(
        self,
        name: str,
        namespace: str,
        args: List[Dict[str, Any]]
    ) -> PatternMatchResult:
        """Match a generic function call."""
        param_map = {}
        
        for i, arg in enumerate(args):
            arg_expr = self.expression_converter.convert(arg)
            ref = create_parameter_reference(expression=arg_expr, order=i+1)
            param_map[f"arg{i}"] = {ref["key"]: ref}
        
        # Use the function name as namespace.name if no namespace specified
        if not namespace:
            # Check if it's a UIEngine function
            uiengine_functions = [
                'SetStore', 'GetStoreData', 'Navigate', 'NavigateBack',
                'FetchData', 'SendData', 'DeleteData', 'Message',
                'Login', 'Logout', 'Refresh', 'ScrollTo', 'ScrollToGrid'
            ]
            if name in uiengine_functions:
                namespace = "UIEngine"
            else:
                namespace = ""
        
        statement = create_statement(
            statement_name=self.name_generator.generate(name.lower()),
            name=name,
            namespace=namespace,
            parameter_map=param_map
        )
        
        return PatternMatchResult(
            matched=True,
            statements=[statement]
        )
    
    def _is_store_path(self, node: Dict[str, Any]) -> bool:
        """Check if a node represents a store path."""
        if node.get('type') != 'MemberExpression':
            return False
        
        obj = node.get('object', {})
        if obj.get('type') == 'Identifier':
            return obj.get('name', '') in STORE_PREFIXES
        
        return self._is_store_path(obj)
    
    def _extract_object_literal(self, node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Extract a simple object literal to a Python dict."""
        if node.get('type') != 'ObjectExpression':
            return None
        
        result = {}
        for prop in node.get('properties', []):
            key = prop.get('key', {})
            value = prop.get('value', {})
            
            key_name = key.get('name', '') if key.get('type') == 'Identifier' else None
            if not key_name and key.get('type') == 'Literal':
                key_name = str(key.get('value', ''))
            
            if not key_name:
                continue
            
            if value.get('type') == 'Literal':
                result[key_name] = {"value": value.get('value')}
            else:
                value_expr = self.expression_converter.convert(value)
                result[key_name] = {"location": {"type": "EXPRESSION", "expression": value_expr}}
        
        return result

