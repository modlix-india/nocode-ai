#!/usr/bin/env python3
"""
Interactive HTML comparison tool for website import.

Compares source website elements with generated Nocode components.
Generates an interactive HTML report with:
- Left pane: DOM tree view (expandable/collapsible)
- Right pane: JSON output for the selected node

Usage:
    python scripts/compare_tags.py <source_url> [--output report.html]

Example:
    python scripts/compare_tags.py https://ceo.pronexus.in/ -o comparison.html
"""

import asyncio
import json
import sys
import argparse
from pathlib import Path
from typing import Dict, Any, List, Optional
from html import escape

# Add app directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.website_extractor import get_website_extractor, VisualElement, VisualData
from app.agents.page_agent import PageAgent


def flatten_elements(elements: List[VisualElement]) -> List[VisualElement]:
    """Flatten element tree to list."""
    result = []
    for elem in elements:
        result.append(elem)
        result.extend(flatten_elements(elem.children))
    return result


def get_component_type(tag: str, has_children: bool) -> str:
    """Get the Nocode component type for a tag."""
    tag_lower = tag.lower()

    if tag_lower == "a" and has_children:
        return "Grid"

    tag_map = {
        "h1": "Text", "h2": "Text", "h3": "Text", "h4": "Text", "h5": "Text", "h6": "Text",
        "p": "Text", "span": "Text", "label": "Text", "li": "Text",
        "strong": "Text", "b": "Text", "em": "Text", "i": "Text",
        "button": "Button",
        "a": "Link",
        "img": "Image",
        "svg": "Image",
        "input": "TextBox", "textarea": "TextBox",
        "div": "Grid", "section": "Grid", "article": "Grid", "main": "Grid",
        "header": "Grid", "footer": "Grid", "nav": "Grid", "aside": "Grid",
        "form": "Grid", "ul": "Grid", "ol": "Grid",
    }
    return tag_map.get(tag_lower, "Grid")


def build_component_lookup(comp_def: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Build a lookup table for components by various keys."""
    lookup = {}
    for key, comp in comp_def.items():
        lookup[key] = comp
        # Also index by element ID if present in key
        parts = key.split('_')
        if len(parts) > 1 and parts[0]:
            lookup[parts[0]] = comp
    return lookup


def generate_tree_node_html(elem: VisualElement, depth: int, node_index: List[int], comp_lookup: Dict) -> str:
    """Generate a tree node for the DOM tree view."""
    tag = elem.tag.lower()
    has_children = len(elem.children) > 0
    comp_type = get_component_type(tag, has_children)

    # Increment node index
    node_id = node_index[0]
    node_index[0] += 1

    # Find matching component
    matched_comp = None
    for key, comp in comp_lookup.items():
        if elem.id and elem.id in key:
            matched_comp = comp
            break

    # Create node data for JSON
    node_data = {
        "sourceTag": tag,
        "sourceId": elem.id or "",
        "sourceText": (elem.text or "")[:100],
        "sourceAttrs": elem.attributes,
        "extractedStyles": elem.styles.get('desktop', {}),
        "componentType": comp_type,
        "component": matched_comp
    }

    # Escape for HTML attribute
    node_data_json = escape(json.dumps(node_data))

    # Text preview for tree
    text_preview = ""
    if elem.text and elem.text.strip():
        text_preview = f': "{elem.text.strip()[:30]}..."' if len(elem.text.strip()) > 30 else f': "{elem.text.strip()}"'

    # Component badge color
    comp_colors = {
        "Text": "#3498db",
        "Grid": "#9b59b6",
        "Image": "#e67e22",
        "Link": "#1abc9c",
        "Button": "#e74c3c",
        "TextBox": "#f39c12"
    }
    badge_color = comp_colors.get(comp_type.split()[0], "#95a5a6")

    html = f'''
        <div class="tree-node" data-node-id="{node_id}" data-node-data="{node_data_json}">
            <div class="tree-label" onclick="selectNode({node_id}, this)">
                {'<span class="tree-toggle" onclick="toggleNode(event, this)">' + ('▶' if has_children else '&nbsp;&nbsp;') + '</span>' if has_children else '<span class="tree-spacer"></span>'}
                <span class="tag-name">&lt;{tag}&gt;</span>
                <span class="comp-badge" style="background: {badge_color};">{comp_type}</span>
                <span class="text-preview">{escape(text_preview)}</span>
            </div>
'''

    if has_children:
        html += '<div class="tree-children">'
        for child in elem.children:
            html += generate_tree_node_html(child, depth + 1, node_index, comp_lookup)
        html += '</div>'

    html += '</div>'
    return html


def generate_html_report(
    source_url: str,
    elements: List[VisualElement],
    root_styles: Dict[str, Dict[str, str]],
    page_def: Dict[str, Any] = None
) -> str:
    """Generate an interactive HTML comparison report with tree view and JSON panel."""

    flat_elements = flatten_elements(elements)
    comp_def = page_def.get("componentDefinition", {}) if page_def else {}
    comp_lookup = build_component_lookup(comp_def)

    # Build root component data
    root_comp = comp_def.get("pageRoot", {})
    root_data = {
        "sourceTag": "body",
        "sourceId": "body",
        "sourceText": "",
        "sourceAttrs": {},
        "extractedStyles": root_styles.get('desktop', {}),
        "componentType": "Grid (Root)",
        "component": root_comp
    }
    root_data_json = escape(json.dumps(root_data))

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Tag Comparison - {escape(source_url)}</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            height: 100vh;
            overflow: hidden;
        }}
        .header {{
            background: #252540;
            padding: 10px 20px;
            border-bottom: 1px solid #333;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .header h1 {{
            font-size: 1.2rem;
            color: #fff;
        }}
        .source-url {{
            color: #888;
            font-size: 0.8rem;
        }}
        .stats {{
            display: flex;
            gap: 20px;
            font-size: 0.85rem;
        }}
        .stat {{
            color: #888;
        }}
        .stat span {{
            color: #4a90d9;
            font-weight: bold;
        }}
        .main-container {{
            display: flex;
            height: calc(100vh - 60px);
        }}
        .tree-panel {{
            width: 40%;
            border-right: 1px solid #333;
            overflow-y: auto;
            padding: 10px;
            background: #1e1e36;
        }}
        .json-panel {{
            width: 60%;
            overflow-y: auto;
            padding: 20px;
            background: #1a1a2e;
        }}
        .tree-node {{
            margin-left: 15px;
        }}
        .tree-node:first-child {{
            margin-left: 0;
        }}
        .tree-label {{
            display: flex;
            align-items: center;
            gap: 5px;
            padding: 4px 8px;
            cursor: pointer;
            border-radius: 4px;
            font-size: 0.85rem;
        }}
        .tree-label:hover {{
            background: #2a2a4a;
        }}
        .tree-label.selected {{
            background: #3a4a6a;
        }}
        .tree-toggle {{
            width: 16px;
            font-size: 0.7rem;
            color: #888;
            cursor: pointer;
        }}
        .tree-spacer {{
            width: 16px;
        }}
        .tag-name {{
            color: #4a90d9;
            font-family: monospace;
        }}
        .comp-badge {{
            font-size: 0.7rem;
            padding: 1px 6px;
            border-radius: 10px;
            color: #fff;
        }}
        .text-preview {{
            color: #666;
            font-size: 0.75rem;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 200px;
        }}
        .tree-children {{
            display: none;
        }}
        .tree-children.expanded {{
            display: block;
        }}
        .json-section {{
            margin-bottom: 20px;
        }}
        .json-section h3 {{
            color: #888;
            font-size: 0.8rem;
            text-transform: uppercase;
            margin-bottom: 10px;
            padding-bottom: 5px;
            border-bottom: 1px solid #333;
        }}
        .json-content {{
            background: #252540;
            padding: 15px;
            border-radius: 8px;
            overflow-x: auto;
        }}
        pre {{
            font-family: 'Monaco', 'Menlo', monospace;
            font-size: 0.8rem;
            line-height: 1.5;
            white-space: pre-wrap;
            word-break: break-word;
        }}
        .json-key {{ color: #9cdcfe; }}
        .json-string {{ color: #ce9178; }}
        .json-number {{ color: #b5cea8; }}
        .json-boolean {{ color: #569cd6; }}
        .json-null {{ color: #569cd6; }}
        .placeholder {{
            color: #666;
            text-align: center;
            padding: 40px;
            font-size: 0.9rem;
        }}
        .info-row {{
            display: flex;
            gap: 10px;
            margin-bottom: 10px;
        }}
        .info-badge {{
            background: #3a3a5a;
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 0.8rem;
        }}
        .info-badge.tag {{
            background: #2a4a6a;
            color: #4a90d9;
        }}
        .info-badge.type {{
            background: #4a3a6a;
            color: #9b59b6;
        }}
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1>DOM Tree &rarr; Nocode Components</h1>
            <div class="source-url">{escape(source_url)}</div>
        </div>
        <div class="stats">
            <div class="stat">Elements: <span>{len(flat_elements)}</span></div>
            <div class="stat">Components: <span>{len(comp_def)}</span></div>
        </div>
    </div>

    <div class="main-container">
        <div class="tree-panel">
            <!-- Root/Body node -->
            <div class="tree-node" data-node-id="0" data-node-data="{root_data_json}">
                <div class="tree-label selected" onclick="selectNode(0, this)">
                    <span class="tree-toggle" onclick="toggleNode(event, this)">▶</span>
                    <span class="tag-name">&lt;body&gt;</span>
                    <span class="comp-badge" style="background: #e74c3c;">ROOT</span>
                </div>
                <div class="tree-children expanded">
'''

    # Generate tree for all elements
    node_index = [1]  # Start from 1 (0 is root)
    for elem in elements:
        html += generate_tree_node_html(elem, 0, node_index, comp_lookup)

    html += '''
                </div>
            </div>
        </div>

        <div class="json-panel" id="jsonPanel">
            <div class="placeholder">
                Click on a node in the tree to see its details
            </div>
        </div>
    </div>

    <script>
        let selectedLabel = document.querySelector('.tree-label.selected');

        function toggleNode(event, toggle) {
            event.stopPropagation();
            const node = toggle.closest('.tree-node');
            const children = node.querySelector('.tree-children');
            if (children) {
                children.classList.toggle('expanded');
                toggle.textContent = children.classList.contains('expanded') ? '▼' : '▶';
            }
        }

        function selectNode(nodeId, label) {
            // Deselect previous
            if (selectedLabel) {
                selectedLabel.classList.remove('selected');
            }

            // Select new
            label.classList.add('selected');
            selectedLabel = label;

            // Get node data
            const node = label.closest('.tree-node');
            const nodeData = JSON.parse(node.dataset.nodeData);

            // Update JSON panel
            updateJsonPanel(nodeData);
        }

        function updateJsonPanel(data) {
            const panel = document.getElementById('jsonPanel');

            let html = `
                <div class="info-row">
                    <span class="info-badge tag">&lt;${data.sourceTag}&gt;</span>
                    <span class="info-badge type">${data.componentType}</span>
                    ${data.sourceId ? `<span class="info-badge">id: ${data.sourceId}</span>` : ''}
                </div>
            `;

            // Source text if any
            if (data.sourceText) {
                html += `
                    <div class="json-section">
                        <h3>Source Text</h3>
                        <div class="json-content">
                            <pre>${escapeHtml(data.sourceText)}</pre>
                        </div>
                    </div>
                `;
            }

            // Extracted styles
            if (Object.keys(data.extractedStyles).length > 0) {
                html += `
                    <div class="json-section">
                        <h3>Extracted Styles (${Object.keys(data.extractedStyles).length} properties)</h3>
                        <div class="json-content">
                            <pre>${formatJson(data.extractedStyles)}</pre>
                        </div>
                    </div>
                `;
            }

            // Final Nocode Component
            if (data.component) {
                html += `
                    <div class="json-section">
                        <h3>Final Nocode Component JSON</h3>
                        <div class="json-content">
                            <pre>${formatJson(data.component)}</pre>
                        </div>
                    </div>
                `;
            } else {
                html += `
                    <div class="json-section">
                        <h3>Final Nocode Component JSON</h3>
                        <div class="json-content">
                            <pre style="color: #888;">Component not found in page definition</pre>
                        </div>
                    </div>
                `;
            }

            panel.innerHTML = html;
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        function formatJson(obj) {
            const json = JSON.stringify(obj, null, 2);
            return json
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"([^"]+)":/g, '<span class="json-key">"$1"</span>:')
                .replace(/: "([^"]*)"/g, ': <span class="json-string">"$1"</span>')
                .replace(/: (\\d+\\.?\\d*)/g, ': <span class="json-number">$1</span>')
                .replace(/: (true|false)/g, ': <span class="json-boolean">$1</span>')
                .replace(/: (null)/g, ': <span class="json-null">$1</span>');
        }

        // Select root node by default
        document.addEventListener('DOMContentLoaded', () => {
            const rootNode = document.querySelector('.tree-node');
            if (rootNode) {
                const nodeData = JSON.parse(rootNode.dataset.nodeData);
                updateJsonPanel(nodeData);
            }
        });
    </script>
</body>
</html>
'''
    return html


async def main():
    parser = argparse.ArgumentParser(description='Compare source website tags with Nocode components')
    parser.add_argument('url', help='Source website URL')
    parser.add_argument('--output', '-o', default='tag_comparison.html', help='Output HTML file')
    args = parser.parse_args()

    print(f"Extracting from: {args.url}")

    extractor = get_website_extractor()
    try:
        visual_data = await extractor.extract(args.url)

        flat_elements = flatten_elements(visual_data.elements)
        print(f"Extracted {len(flat_elements)} total elements")

        # Convert to Nocode page definition using PageAgent
        print("Converting to Nocode page definition...")
        agent = PageAgent()
        page_def = agent._convert_visual_to_nocode(visual_data, {})
        print(f"Generated {len(page_def.get('componentDefinition', {}))} components")

        # Generate HTML report with tree view
        html = generate_html_report(
            args.url,
            visual_data.elements,
            visual_data.root_styles,
            page_def
        )

        output_path = Path(args.output)
        output_path.write_text(html)
        print(f"\nReport generated: {output_path.absolute()}")
        print(f"Open in browser: file://{output_path.absolute()}")

        # Print summary
        print(f"\nTag Summary:")
        tag_counts = {}
        for elem in flat_elements:
            tag = elem.tag.lower()
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

        for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1]):
            has_children = any(e.tag.lower() == tag and len(e.children) > 0 for e in flat_elements)
            comp_type = get_component_type(tag, has_children)
            print(f"  <{tag}> ({count}x) -> {comp_type}")

    finally:
        await extractor.close()


if __name__ == '__main__':
    asyncio.run(main())
