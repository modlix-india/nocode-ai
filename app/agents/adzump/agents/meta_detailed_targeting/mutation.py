from typing import Tuple

def _normalize(s: str) -> str:
    return str(s).strip().lower()

def apply_targeting_edit(action: str, segment: dict | str, context: dict) -> Tuple[bool, str]:
    """Shared mutation logic with exact-match-first resolution."""
    dt = context.setdefault("detailed_targeting", {"entities": [], "excluded_ids": []})
    entities = dt.setdefault("entities", [])
    excluded = dt.setdefault("excluded_ids", [])
    
    if action == "add":
        if not isinstance(segment, dict) or "id" not in segment:
            return False, "Add action requires a full segment dictionary with an 'id'."
            
        seg_id = segment["id"]
        if seg_id in excluded:
            excluded.remove(seg_id)
            
        if len(entities) >= 60:
            return False, "Maximum of 60 segments allowed."
            
        if not any(e.get("id") == seg_id for e in entities):
            entities.append(segment)
        return True, "Added successfully."
        
    elif action == "delete":
        search_term = segment if isinstance(segment, str) else segment.get("id", segment.get("name", ""))
        search_term = _normalize(search_term)
        target = None
        
        # 1. Exact ID match
        matches = [e for e in entities if _normalize(e.get("id", "")) == search_term]
        if len(matches) == 1:
            target = matches[0]
            
        # 2. Exact Normalized Name match
        if not target:
            matches = [e for e in entities if _normalize(e.get("name", "")) == search_term]
            if len(matches) == 1:
                target = matches[0]
            elif len(matches) > 1:
                return False, f"Ambiguous deletion: '{search_term}' exactly matches multiple segments by name. Please specify an exact ID."
                
        # 3. Unique Substring Fallback (Bidirectional)
        if not target:
            matches = []
            for e in entities:
                e_name = _normalize(e.get("name", ""))
                if e_name and (search_term in e_name or e_name in search_term):
                    matches.append(e)
                    
            if len(matches) == 1:
                target = matches[0]
            elif len(matches) > 1:
                return False, f"Ambiguous deletion: '{search_term}' matches {len(matches)} segments. Be more specific."

        # 4. Token Overlap Fallback
        if not target:
            search_tokens = set(search_term.split())
            token_matches = []
            for e in entities:
                e_name = _normalize(e.get("name", ""))
                if e_name:
                    entity_tokens = set(e_name.split())
                    if entity_tokens.issubset(search_tokens) or search_tokens.issubset(entity_tokens):
                        token_matches.append(e)
                        
            if len(token_matches) == 1:
                target = token_matches[0]
            elif len(token_matches) > 1:
                return False, f"Ambiguous deletion: '{search_term}' matches multiple segments. Be more specific."
                
        if not target:
            return False, f"Segment '{search_term}' not found."
            
        seg_id = target.get("id")
        dt["entities"] = [e for e in entities if e.get("id") != seg_id]
        if seg_id and seg_id not in excluded:
            excluded.append(seg_id)
            
        return True, f"Deleted '{target.get('name')}'."
    
    elif action == "clear_all":
        # Clear all does not wipe excluded_ids
        dt["entities"] = []
        return True, "Cleared all segments."
        
    elif action == "start_over":
        dt["entities"] = []
        dt["excluded_ids"] = []
        return True, "Reset completely."
        
    return False, f"Unknown action: {action}"

def reconcile_ai_suggestions(fresh_db_ctx: dict, ai_entities: list) -> dict:
    """
    Merge newly discovered AI targeting segments with the live database state,
    respecting any segments the user may have concurrently excluded/deleted.
    """
    dt_data = fresh_db_ctx.get("detailed_targeting") or {}
    live_entities = dt_data.get("entities") or []
    excluded_ids = set(str(eid) for eid in (dt_data.get("excluded_ids") or []))
    user_added_ids = set(str(aid) for aid in (dt_data.get("user_added_ids") or []))

    reconciled = [
        item for item in live_entities
        if str(item.get("id") if isinstance(item, dict) else getattr(item, "id", None)) not in excluded_ids
    ]
    existing_ids = {
        str(item.get("id") if isinstance(item, dict) else getattr(item, "id", None))
        for item in reconciled
    }

    for ai_ent in ai_entities:
        ai_id = str(ai_ent.id) if hasattr(ai_ent, "id") else str(ai_ent.get("id"))
        if ai_id not in excluded_ids and ai_id not in existing_ids:
            reconciled.append(ai_ent.model_dump() if hasattr(ai_ent, "model_dump") else ai_ent)
            existing_ids.add(ai_id)

    reconciled = reconciled[:60]
    
    return {
        "entities": reconciled,
        "excluded_ids": list(excluded_ids),
        "user_added_ids": list(user_added_ids)
    }
