"""
KIRun to JavaScript Converter

Converts KIRun function definitions back to JavaScript code using a
template-based approach. This is the reverse of JS2KIRunConverter.

The converter uses a template registry to map KIRun functions to their
JavaScript equivalents. For unknown functions, a generic format is used.
"""
from typing import Dict, Any, List, Tuple, Optional
from collections import deque


# Template Registry: Maps (namespace, name) to JavaScript patterns
# 
# Each template has:
#   - template: JavaScript code with {param} placeholders
#   - extract: List of parameter names to extract from parameterMap
#   - inline: If True, this is an inline expression (no semicolon)
#   - control_flow: If True, this handles control flow (if/loops)
#
FUNCTION_TEMPLATES: Dict[Tuple[str, str], Dict[str, Any]] = {
    # ============ UIEngine Functions ============
    
    ("UIEngine", "SetStore"): {
        "template": "{path} = {value};",
        "extract": ["path", "value"]
    },
    
    ("UIEngine", "Navigate"): {
        "template": "navigate({linkPath});",
        "extract": ["linkPath"]
    },
    
    ("UIEngine", "SendData"): {
        "template": 'fetch({url}, {{ method: {method}, body: {payload} }});',
        "extract": ["url", "method", "payload"]
    },
    
    ("UIEngine", "FetchData"): {
        "template": "fetch({url});",
        "extract": ["url"]
    },
    
    ("UIEngine", "Message"): {
        "template": "showMessage({msg}, {type});",
        "extract": ["msg", "type"]
    },
    
    ("UIEngine", "GenerateEvent"): {
        "template": "generateEvent({eventName}, {results});",
        "extract": ["eventName", "results"]
    },
    
    ("UIEngine", "Output"): {
        "template": "return {value};",
        "extract": ["value"]
    },
    
    # ============ System Functions ============
    
    ("System", "Wait"): {
        "template": "wait({millis});",
        "extract": ["millis"]
    },
    
    ("System", "If"): {
        "template": "if ({condition}) {{ ... }}",
        "extract": ["condition"],
        "control_flow": True
    },
    
    ("System", "Print"): {
        "template": "console.log({values});",
        "extract": ["values"]
    },
    
    ("System", "GenerateEvent"): {
        "template": "generateEvent({eventName}, {results});",
        "extract": ["eventName", "results"]
    },
    
    # ============ System.Loop Functions ============
    
    ("System.Loop", "ForEachLoop"): {
        "template": "for (let {iteratorKey} of {source}) {{ ... }}",
        "extract": ["source", "iteratorKey"],
        "identifiers": ["iteratorKey"],  # These params should not be quoted
        "control_flow": True
    },
    
    ("System.Loop", "CountLoop"): {
        "template": "for (let {counterKey} = 0; {counterKey} < {count}; {counterKey}++) {{ ... }}",
        "extract": ["count", "counterKey"],
        "identifiers": ["counterKey"],
        "control_flow": True
    },
    
    ("System.Loop", "RangeLoop"): {
        "template": "for (let {counterKey} = {from}; {counterKey} < {to}; {counterKey}++) {{ ... }}",
        "extract": ["from", "to", "counterKey"],
        "identifiers": ["counterKey"],
        "control_flow": True
    },
    
    ("System.Loop", "Break"): {
        "template": "break;",
        "extract": []
    },
    
    # ============ System.Math Functions ============
    
    ("System.Math", "Add"): {
        "template": "({value1} + {value2})",
        "extract": ["value1", "value2"],
        "inline": True
    },
    
    ("System.Math", "Subtract"): {
        "template": "({value1} - {value2})",
        "extract": ["value1", "value2"],
        "inline": True
    },
    
    ("System.Math", "Multiply"): {
        "template": "({value1} * {value2})",
        "extract": ["value1", "value2"],
        "inline": True
    },
    
    ("System.Math", "Divide"): {
        "template": "({value1} / {value2})",
        "extract": ["value1", "value2"],
        "inline": True
    },
    
    ("System.Math", "Modulus"): {
        "template": "({value1} % {value2})",
        "extract": ["value1", "value2"],
        "inline": True
    },
    
    ("System.Math", "Power"): {
        "template": "Math.pow({value1}, {value2})",
        "extract": ["value1", "value2"],
        "inline": True
    },
    
    ("System.Math", "Random"): {
        "template": "Math.random()",
        "extract": [],
        "inline": True
    },
    
    ("System.Math", "Minimum"): {
        "template": "Math.min({values})",
        "extract": ["values"],
        "inline": True
    },
    
    ("System.Math", "Maximum"): {
        "template": "Math.max({values})",
        "extract": ["values"],
        "inline": True
    },
    
    ("System.Math", "AbsoluteValue"): {
        "template": "Math.abs({value})",
        "extract": ["value"],
        "inline": True
    },
    
    ("System.Math", "Round"): {
        "template": "Math.round({value})",
        "extract": ["value"],
        "inline": True
    },
    
    ("System.Math", "Floor"): {
        "template": "Math.floor({value})",
        "extract": ["value"],
        "inline": True
    },
    
    ("System.Math", "Ceiling"): {
        "template": "Math.ceil({value})",
        "extract": ["value"],
        "inline": True
    },
    
    # ============ System.String Functions ============
    
    ("System.String", "Concatenate"): {
        "template": "({values}.join(''))",
        "extract": ["values"],
        "inline": True
    },
    
    ("System.String", "Split"): {
        "template": "{source}.split({searchString})",
        "extract": ["source", "searchString"],
        "inline": True
    },
    
    ("System.String", "Length"): {
        "template": "{source}.length",
        "extract": ["source"],
        "inline": True
    },
    
    ("System.String", "Substring"): {
        "template": "{source}.substring({start}, {end})",
        "extract": ["source", "start", "end"],
        "inline": True
    },
    
    ("System.String", "ToUpperCase"): {
        "template": "{source}.toUpperCase()",
        "extract": ["source"],
        "inline": True
    },
    
    ("System.String", "ToLowerCase"): {
        "template": "{source}.toLowerCase()",
        "extract": ["source"],
        "inline": True
    },
    
    ("System.String", "Trim"): {
        "template": "{source}.trim()",
        "extract": ["source"],
        "inline": True
    },
    
    ("System.String", "Replace"): {
        "template": "{source}.replace({searchString}, {replacement})",
        "extract": ["source", "searchString", "replacement"],
        "inline": True
    },
    
    ("System.String", "IndexOf"): {
        "template": "{source}.indexOf({searchString})",
        "extract": ["source", "searchString"],
        "inline": True
    },
    
    # ============ System.Array Functions ============
    
    ("System.Array", "Size"): {
        "template": "{source}.length",
        "extract": ["source"],
        "inline": True
    },
    
    ("System.Array", "AddFirst"): {
        "template": "{source}.unshift({element});",
        "extract": ["source", "element"]
    },
    
    ("System.Array", "InsertLast"): {
        "template": "{source}.push({element});",
        "extract": ["source", "element"]
    },
    
    ("System.Array", "DeleteFirst"): {
        "template": "{source}.shift();",
        "extract": ["source"]
    },
    
    ("System.Array", "DeleteLast"): {
        "template": "{source}.pop();",
        "extract": ["source"]
    },
    
    ("System.Array", "IndexOf"): {
        "template": "{source}.indexOf({element})",
        "extract": ["source", "element"],
        "inline": True
    },
    
    ("System.Array", "Join"): {
        "template": "{source}.join({delimiter})",
        "extract": ["source", "delimiter"],
        "inline": True
    },
    
    ("System.Array", "Reverse"): {
        "template": "{source}.reverse()",
        "extract": ["source"],
        "inline": True
    },
    
    ("System.Array", "Sort"): {
        "template": "{source}.sort()",
        "extract": ["source"],
        "inline": True
    },
    
    ("System.Array", "Concatenate"): {
        "template": "{source}.concat({secondSource})",
        "extract": ["source", "secondSource"],
        "inline": True
    },
    
    ("System.Array", "SubArray"): {
        "template": "{source}.slice({start}, {end})",
        "extract": ["source", "start", "end"],
        "inline": True
    },
    
    # ============ System.Object Functions ============
    
    ("System.Object", "Keys"): {
        "template": "Object.keys({source})",
        "extract": ["source"],
        "inline": True
    },
    
    ("System.Object", "Values"): {
        "template": "Object.values({source})",
        "extract": ["source"],
        "inline": True
    },
    
    ("System.Object", "Entries"): {
        "template": "Object.entries({source})",
        "extract": ["source"],
        "inline": True
    },
    
    # ============ System.Context Functions ============
    
    ("System.Context", "Create"): {
        "template": "// Context.create({name})",
        "extract": ["name"]
    },
    
    ("System.Context", "Get"): {
        "template": "Context.{name}",
        "extract": ["name"],
        "inline": True
    },
    
    ("System.Context", "Set"): {
        "template": "Context.{name} = {value};",
        "extract": ["name", "value"]
    },
    
    # ============ System.JSON Functions ============
    
    ("System", "JSONStringify"): {
        "template": "JSON.stringify({source})",
        "extract": ["source"],
        "inline": True
    },
    
    ("System", "JSONParse"): {
        "template": "JSON.parse({source})",
        "extract": ["source"],
        "inline": True
    },
}


def _strip_outer_parens(expr: str) -> str:
    """
    Strip ALL unnecessary outer parentheses from an expression.
    
    Examples:
        "(Page.value)" -> "Page.value"
        "((a + b))" -> "a + b"
        "(((x)))" -> "x"
        "(a + b) * c" -> "(a + b) * c"  (keep - parens not purely outer)
        "func(arg)" -> "func(arg)"  (keep - function call)
    """
    if not expr:
        return expr
    
    expr = expr.strip()
    
    # Keep stripping while we have matching outer parens
    changed = True
    while changed and len(expr) >= 2 and expr.startswith('(') and expr.endswith(')'):
        changed = False
        
        # Check if these parens actually match (the opening paren matches the closing one)
        # We need to verify the first '(' corresponds to the last ')'
        depth = 0
        matches_outer = True
        
        for i, char in enumerate(expr):
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                # If depth reaches 0 before the last character, 
                # the first paren doesn't match the last
                if depth == 0 and i < len(expr) - 1:
                    matches_outer = False
                    break
        
        if matches_outer and depth == 0:
            expr = expr[1:-1].strip()
            changed = True
    
    return expr


def extract_param_value(param_map: Dict[str, Any], param_name: str, as_identifier: bool = False) -> str:
    """
    Extract value from parameterMap, handling VALUE vs EXPRESSION types.
    
    Args:
        param_map: The step's parameterMap
        param_name: Name of the parameter to extract
        as_identifier: If True, treat string values as identifiers (no quotes)
        
    Returns:
        JavaScript representation of the value
    """
    param_entries = param_map.get(param_name, {})
    
    if not param_entries:
        return "undefined"
    
    # Handle variadic parameters (multiple values)
    if len(param_entries) > 1:
        values = []
        for ref in sorted(param_entries.values(), key=lambda x: x.get("order", 0)):
            values.append(_extract_single_value(ref, as_identifier))
        return f"[{', '.join(values)}]"
    
    # Single value
    for ref in param_entries.values():
        return _extract_single_value(ref, as_identifier)
    
    return "undefined"


def _extract_single_value(ref: Dict[str, Any], as_identifier: bool = False) -> str:
    """Extract a single value from a parameter reference.
    
    Args:
        ref: Parameter reference dict
        as_identifier: If True, treat string values as identifiers (no quotes)
    """
    param_type = ref.get("type", "VALUE")
    
    if param_type == "EXPRESSION":
        expr = ref.get("expression", "")
        # Remove {{ }} wrapper if present
        if expr.startswith("{{") and expr.endswith("}}"):
            expr = expr[2:-2].strip()
        # Remove unnecessary outer parentheses
        # e.g., "(Page.value)" -> "Page.value"
        # But keep needed ones like "(a + b)" in complex expressions
        expr = _strip_outer_parens(expr)
        return expr
    else:
        value = ref.get("value")
        if value is None:
            return "null"
        elif isinstance(value, str):
            # If treating as identifier, don't quote
            if as_identifier:
                return value
            # Check if it's a store path
            if any(value.startswith(prefix) for prefix in ["Page.", "Store.", "Url.", "Parent.", "Steps."]):
                return value
            return f'"{value}"'
        elif isinstance(value, bool):
            return "true" if value else "false"
        else:
            return str(value)


def _extract_step_references_from_params(step: Dict[str, Any]) -> set:
    """
    Extract all Steps.xxx references from a step's parameterMap.
    
    This finds implicit dependencies where a step uses another step's output
    in an expression, even without explicit dependentStatements.
    """
    import re
    references = set()
    param_map = step.get("parameterMap", {})
    
    for param_entries in param_map.values():
        if not isinstance(param_entries, dict):
            continue
        for entry in param_entries.values():
            if not isinstance(entry, dict):
                continue
            # Check expression field
            expr = entry.get("expression", "")
            if expr and "Steps." in expr:
                # Find all Steps.stepName references
                matches = re.findall(r'Steps\.(\w+)', expr)
                references.update(matches)
            # Check value field
            value = entry.get("value", "")
            if isinstance(value, str) and "Steps." in value:
                matches = re.findall(r'Steps\.(\w+)', value)
                references.update(matches)
    
    return references


def topological_sort(steps: Dict[str, Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any]]]:
    """
    Sort steps by dependencies using topological sort.
    
    Considers both:
    1. Explicit dependencies (dependentStatements)
    2. Implicit dependencies (Steps.xxx references in expressions)
    
    Args:
        steps: Dictionary of step definitions
        
    Returns:
        List of (step_name, step) tuples in execution order
    """
    if not steps:
        return []
    
    # Build dependency graph
    in_degree = {name: 0 for name in steps}
    graph = {name: [] for name in steps}
    processed_edges = set()  # Avoid duplicate edges
    
    for name, step in steps.items():
        all_deps = set()
        
        # 1. Explicit dependencies from dependentStatements
        deps = step.get("dependentStatements", {})
        for dep_path in deps.keys():
            # Extract step name from path like "Steps.stepName.output"
            parts = dep_path.split(".")
            if len(parts) >= 2 and parts[0] == "Steps":
                dep_name = parts[1]
                all_deps.add(dep_name)
        
        # 2. Implicit dependencies from expression references
        implicit_deps = _extract_step_references_from_params(step)
        all_deps.update(implicit_deps)
        
        # Add edges for all dependencies
        for dep_name in all_deps:
            if dep_name in steps and dep_name != name:
                edge = (dep_name, name)
                if edge not in processed_edges:
                    processed_edges.add(edge)
                    graph[dep_name].append(name)
                    in_degree[name] += 1
    
    # Kahn's algorithm
    queue = deque([name for name, deg in in_degree.items() if deg == 0])
    result = []
    
    while queue:
        name = queue.popleft()
        result.append((name, steps[name]))
        
        for neighbor in graph[name]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)
    
    # If not all steps are in result, there's a cycle - just return original order
    if len(result) != len(steps):
        return [(name, step) for name, step in steps.items()]
    
    return result


def step_to_js(step: Dict[str, Any], step_name: str) -> str:
    """
    Convert a single KIRun step to JavaScript.
    
    Args:
        step: Step definition
        step_name: Name of the step
        
    Returns:
        JavaScript code string
    """
    namespace = step.get("namespace", "")
    name = step.get("name", "")
    params = step.get("parameterMap", {})
    
    # Look up template
    key = (namespace, name)
    template_info = FUNCTION_TEMPLATES.get(key)
    
    if template_info:
        # Extract parameters
        extract_params = template_info.get("extract", [])
        identifier_params = set(template_info.get("identifiers", []))
        values = {}
        
        for param_name in extract_params:
            is_identifier = param_name in identifier_params
            values[param_name] = extract_param_value(params, param_name, as_identifier=is_identifier)
        
        # Apply template
        try:
            js_code = template_info["template"].format(**values)
        except KeyError as e:
            # Missing parameter - use placeholder
            js_code = f"// {namespace}.{name}(...) - missing param: {e}"
        
        return js_code
    else:
        # Generic fallback - output as comment with all params
        param_strs = []
        for param_name in params.keys():
            value = extract_param_value(params, param_name)
            param_strs.append(f"{param_name}={value}")
        
        return f"// {namespace}.{name}({', '.join(param_strs)})"


class KIRun2JSConverter:
    """
    Converts KIRun function definitions to JavaScript code.
    
    Uses a template-based approach where common functions are mapped
    to their JavaScript equivalents via templates. Unknown functions
    are output as comments.
    
    Handles control flow:
    - Steps depending on .true/.false are wrapped in if/else blocks
    - Steps depending on .output are wrapped in if(output) blocks
    - Steps depending on .error are wrapped in if(error) blocks
    
    Example:
        converter = KIRun2JSConverter()
        js_code = converter.convert(kirun_function_def)
        print(js_code)
    """
    
    def __init__(self):
        self.templates = FUNCTION_TEMPLATES
    
    def convert(self, func_def: Dict[str, Any]) -> str:
        """
        Convert a KIRun function definition to JavaScript.
        
        Handles control flow by grouping steps into branches.
        
        Args:
            func_def: KIRun function definition dictionary
            
        Returns:
            JavaScript code string
        """
        steps = func_def.get("steps", {})
        func_name = func_def.get("name", "unknown")
        
        if not steps:
            return f"// Function: {func_name}\n// (empty function)"
        
        # Generate header
        lines = [
            f"// Function: {func_name}",
            ""
        ]
        
        # Build dependency tree and group steps by branches
        branch_groups = self._group_steps_by_branches(steps)
        
        # Generate code with proper nesting
        self._generate_code(steps, branch_groups, lines, indent=0)
        
        return "\n".join(lines)
    
    def _group_steps_by_branches(self, steps: Dict[str, Any]) -> Dict[str, Dict[str, List[str]]]:
        """
        Group steps by their branch dependencies.
        
        Priority:
        1. Explicit dependencies (dependentStatements) - these take precedence
        2. Implicit dependencies from expressions - only if no explicit dependency
        
        A step with explicit dependency on Steps.if.true will ONLY be in the if.true branch,
        even if it also uses Steps.fetchData.output in its expression.
        
        Returns:
            Dict mapping step_name -> {
                'true': [steps depending on .true],
                'false': [steps depending on .false],
                'output': [steps depending on .output],
                'error': [steps depending on .error]
            }
        """
        import re
        branch_groups = {}
        steps_with_explicit_deps = set()  # Steps that have explicit dependentStatements
        
        # First pass: collect explicit dependencies (these take priority)
        for step_name, step in steps.items():
            deps = step.get("dependentStatements", {})
            
            for dep_path in deps.keys():
                parts = dep_path.split(".")
                if len(parts) >= 3 and parts[0] == "Steps":
                    parent_step = parts[1]
                    branch = parts[2]  # 'true', 'false', 'output', 'error'
                    
                    if parent_step not in branch_groups:
                        branch_groups[parent_step] = {'true': [], 'false': [], 'output': [], 'error': []}
                    
                    if branch in branch_groups[parent_step]:
                        if step_name not in branch_groups[parent_step][branch]:
                            branch_groups[parent_step][branch].append(step_name)
                            steps_with_explicit_deps.add(step_name)
        
        # Second pass: add implicit dependencies ONLY for steps without explicit deps
        for step_name, step in steps.items():
            # Skip if this step already has explicit branch dependencies
            if step_name in steps_with_explicit_deps:
                continue
            
            param_map = step.get("parameterMap", {})
            detected_branches = set()
            
            for param_entries in param_map.values():
                if not isinstance(param_entries, dict):
                    continue
                for entry in param_entries.values():
                    if not isinstance(entry, dict):
                        continue
                    expr = entry.get("expression", "") or ""
                    
                    # Find Steps.xxx.output or Steps.xxx.error patterns
                    matches = re.findall(r'Steps\.(\w+)\.(output|error)', expr)
                    for parent_step, branch in matches:
                        detected_branches.add((parent_step, branch))
            
            # Add implicit dependencies
            for parent_step, branch in detected_branches:
                if parent_step not in branch_groups:
                    branch_groups[parent_step] = {'true': [], 'false': [], 'output': [], 'error': []}
                
                if branch in branch_groups[parent_step]:
                    if step_name not in branch_groups[parent_step][branch]:
                        branch_groups[parent_step][branch].append(step_name)
        
        return branch_groups
    
    def _generate_code(
        self, 
        steps: Dict[str, Any], 
        branch_groups: Dict[str, Dict[str, List[str]]], 
        lines: List[str], 
        indent: int,
        processed: Optional[set] = None
    ):
        """
        Generate JavaScript code with proper control flow nesting.
        Recursively handles nested control flow structures.
        """
        if processed is None:
            processed = set()
        
        indent_str = "  " * indent
        
        # Get execution order
        ordered_steps = topological_sort(steps)
        
        for step_name, step in ordered_steps:
            if step_name in processed:
                continue
            
            processed.add(step_name)
            
            # Check if this step has branches (is a control flow step)
            branches = branch_groups.get(step_name, {})
            has_true_branch = bool(branches.get('true'))
            has_false_branch = bool(branches.get('false'))
            has_output_branch = bool(branches.get('output'))
            has_error_branch = bool(branches.get('error'))
            
            step_type = step.get("name", "")
            
            if step_type == "If" and (has_true_branch or has_false_branch):
                # Handle If statement with branches
                condition = self._get_condition(step)
                lines.append(f"{indent_str}if ({condition}) {{  // Step: {step_name}")
                
                # Recursively add steps in true branch
                self._generate_branch_steps(
                    steps, branch_groups, lines, 
                    branches.get('true', []), 
                    indent + 1, processed
                )
                
                if has_false_branch:
                    lines.append(f"{indent_str}}} else {{")
                    # Recursively add steps in false branch
                    self._generate_branch_steps(
                        steps, branch_groups, lines,
                        branches.get('false', []),
                        indent + 1, processed
                    )
                
                lines.append(f"{indent_str}}}")
                
            elif step_type in ("FetchData", "SendData") and (has_output_branch or has_error_branch):
                # Handle FetchData/SendData with output/error branches
                js_line = step_to_js(step, step_name)
                lines.append(f"{indent_str}{js_line}  // Step: {step_name}")
                
                # Wrap output-dependent steps in if(output) block
                if has_output_branch:
                    lines.append(f"{indent_str}if (Steps.{step_name}.output) {{")
                    # Recursively add steps - they might have their own control flow
                    self._generate_branch_steps(
                        steps, branch_groups, lines,
                        branches.get('output', []),
                        indent + 1, processed
                    )
                    lines.append(f"{indent_str}}}")
                
                # Wrap error-dependent steps in if(error) block
                if has_error_branch:
                    lines.append(f"{indent_str}if (Steps.{step_name}.error) {{")
                    self._generate_branch_steps(
                        steps, branch_groups, lines,
                        branches.get('error', []),
                        indent + 1, processed
                    )
                    lines.append(f"{indent_str}}}")
            else:
                # Regular step - just output it
                js_line = step_to_js(step, step_name)
                lines.append(f"{indent_str}{js_line}  // Step: {step_name}")
    
    def _generate_branch_steps(
        self,
        steps: Dict[str, Any],
        branch_groups: Dict[str, Dict[str, List[str]]],
        lines: List[str],
        step_names: List[str],
        indent: int,
        processed: set
    ):
        """
        Generate code for steps in a branch, handling nested control flow.
        """
        indent_str = "  " * indent
        
        for child_name in step_names:
            if child_name in processed or child_name not in steps:
                continue
            
            processed.add(child_name)
            child_step = steps[child_name]
            child_type = child_step.get("name", "")
            
            # Check if this child step also has branches
            child_branches = branch_groups.get(child_name, {})
            has_true = bool(child_branches.get('true'))
            has_false = bool(child_branches.get('false'))
            has_output = bool(child_branches.get('output'))
            has_error = bool(child_branches.get('error'))
            
            if child_type == "If" and (has_true or has_false):
                # Nested If statement
                condition = self._get_condition(child_step)
                lines.append(f"{indent_str}if ({condition}) {{  // Step: {child_name}")
                
                self._generate_branch_steps(
                    steps, branch_groups, lines,
                    child_branches.get('true', []),
                    indent + 1, processed
                )
                
                if has_false:
                    lines.append(f"{indent_str}}} else {{")
                    self._generate_branch_steps(
                        steps, branch_groups, lines,
                        child_branches.get('false', []),
                        indent + 1, processed
                    )
                
                lines.append(f"{indent_str}}}")
                
            elif child_type in ("FetchData", "SendData") and (has_output or has_error):
                # Nested fetch with branches
                js_line = step_to_js(child_step, child_name)
                lines.append(f"{indent_str}{js_line}  // Step: {child_name}")
                
                if has_output:
                    lines.append(f"{indent_str}if (Steps.{child_name}.output) {{")
                    self._generate_branch_steps(
                        steps, branch_groups, lines,
                        child_branches.get('output', []),
                        indent + 1, processed
                    )
                    lines.append(f"{indent_str}}}")
                
                if has_error:
                    lines.append(f"{indent_str}if (Steps.{child_name}.error) {{")
                    self._generate_branch_steps(
                        steps, branch_groups, lines,
                        child_branches.get('error', []),
                        indent + 1, processed
                    )
                    lines.append(f"{indent_str}}}")
            else:
                # Regular step
                js_line = step_to_js(child_step, child_name)
                lines.append(f"{indent_str}{js_line}  // Step: {child_name}")
    
    def _get_condition(self, step: Dict[str, Any]) -> str:
        """Extract condition expression from an If step."""
        params = step.get("parameterMap", {})
        condition_param = params.get("condition", {})
        for entry in condition_param.values():
            if entry.get("type") == "EXPRESSION":
                return entry.get("expression", "true")
            else:
                return str(entry.get("value", "true"))
        return "true"
    
    def convert_step(self, step: Dict[str, Any], step_name: str = "step") -> str:
        """
        Convert a single KIRun step to JavaScript.
        
        Args:
            step: Step definition
            step_name: Optional step name for comments
            
        Returns:
            JavaScript code string
        """
        return step_to_js(step, step_name)
    
    def add_template(
        self,
        namespace: str,
        name: str,
        template: str,
        extract: List[str],
        inline: bool = False,
        control_flow: bool = False
    ):
        """
        Add or update a function template.
        
        Args:
            namespace: Function namespace
            name: Function name
            template: JavaScript template with {param} placeholders
            extract: List of parameter names to extract
            inline: Whether this is an inline expression
            control_flow: Whether this is a control flow statement
        """
        self.templates[(namespace, name)] = {
            "template": template,
            "extract": extract,
            "inline": inline,
            "control_flow": control_flow
        }
    
    def get_supported_functions(self) -> List[Tuple[str, str]]:
        """
        Get list of supported function signatures.
        
        Returns:
            List of (namespace, name) tuples
        """
        return list(self.templates.keys())

