"""Checks on the WhatsApp variant service's own logic, not on the model's output.

The parts worth testing here are the guards: whether variants are genuinely different, and whether
they reference merge fields the sender can actually fill. Both are advisory warnings shown in the
editor, and both are wrong in a way nobody would notice at review time if they silently stopped
firing.
"""

from app.services.whatsapp_message_ai import (
    SUPPORTED_VARIABLES,
    _extract_json,
    _too_similar,
    _unknown_variables,
    _used_variables,
)


class TestSimilarityGuard:
    """The failure mode is a model that reorders a clause and calls it a new version."""

    def test_reordered_synonym_swap_is_caught(self):
        # Different strings, near-identical vocabulary. WhatsApp is not fooled by this and neither
        # should the editor be: at volume it is still one message.
        variants = [
            "Hi {{firstName}}, sharing the brochure. Shall I book a site visit this weekend?",
            "Hi {{firstName}}, sharing the brochure. Can I book a site visit this weekend?",
        ]
        assert _too_similar(variants)

    def test_genuinely_different_phrasings_pass(self):
        variants = [
            "Hi {{firstName}}, the brochure is attached. Would this weekend suit you for a visit?",
            "{{firstName}}, I have sent across the details. Let me know a time that works and I will "
            "arrange the site tour.",
            "Attaching everything about the project, {{firstName}}. Happy to walk you through it in "
            "person whenever you are free.",
        ]
        assert not _too_similar(variants)

    def test_a_single_variant_is_not_flagged_as_similar(self):
        # It is a problem, but a different one, reported by its own warning. Flagging it here too
        # would put two warnings on screen for one fault.
        assert not _too_similar(["Only one version here"])
        assert not _too_similar([])


class TestVariableExtraction:
    def test_finds_fields_in_first_seen_order(self):
        variants = ["Hi {{firstName}}, about {{productName}}", "{{firstName}} — {{ticketCode}}"]
        assert _used_variables(variants) == ["firstName", "productName", "ticketCode"]

    def test_tolerates_whitespace_inside_braces(self):
        assert _used_variables(["Hi {{ firstName }}"]) == ["firstName"]

    def test_unknown_fields_are_reported(self):
        # This is the one that sends a sentence with a hole in it, so it has to be caught before
        # somebody saves the message and fires it at two hundred leads.
        variants = ["Hi {{firstName}}, your {{loanAmount}} is approved"]
        assert _unknown_variables(variants) == ["loanAmount"]

    def test_known_fields_are_not_reported(self):
        variants = ["Hi {{firstName}} about {{productName}} — {{userName}}"]
        assert _unknown_variables(variants) == []

    def test_case_insensitive_against_the_supported_list(self):
        # The Java side lowercases before lookup, so {{FirstName}} does resolve. Warning about it
        # would be a false alarm the author cannot act on.
        assert _unknown_variables(["Hi {{FirstName}}"]) == []

    def test_supported_list_is_not_empty(self):
        # Guards against the list being emptied in a refactor, which would silently turn every
        # merge field into an "unknown field" warning.
        assert "firstName" in SUPPORTED_VARIABLES
        assert len(SUPPORTED_VARIABLES) >= 5


class TestJsonExtraction:
    def test_plain_json(self):
        assert _extract_json('{"variants": ["a"]}') == {"variants": ["a"]}

    def test_fenced_json(self):
        assert _extract_json('```json\n{"variants": ["a"]}\n```') == {"variants": ["a"]}

    def test_json_with_surrounding_prose(self):
        text = 'Sure, here you go:\n{"variants": ["a"]}\nHope that helps!'
        assert _extract_json(text) == {"variants": ["a"]}

    def test_non_json_returns_none_so_the_caller_can_salvage(self):
        assert _extract_json("Hi there, shall I book a visit?") is None
        assert _extract_json("") is None
