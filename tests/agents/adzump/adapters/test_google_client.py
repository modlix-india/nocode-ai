"""Unit tests for the Google Ads client adapter
(app/agents/adzump/adapters/google/client.py).
"""

# regression: a failed mutate arrives in TWO different envelopes - GoogleAdsFailure.errors[]
# and google.rpc.BadRequest.fieldViolations[] - so a handler reading one shows nothing at all
# for the other, and the user is told the request failed with no reason.
from __future__ import annotations

import unittest

from app.agents.adzump.adapters.google.client import parse_mutate_errors


class ErrorEnvelopeTests(unittest.TestCase):
    """Google answers in two shapes; reading one shows nothing for the other."""

    def test_google_ads_failure_envelope(self):
        payload = {
            "error": {
                "details": [
                    {
                        "errors": [
                            {
                                "errorCode": {"audienceError": "DIMENSION_INVALID"},
                                "message": "A dimension is not valid.",
                            }
                        ]
                    }
                ]
            }
        }
        self.assertEqual(
            parse_mutate_errors(payload),
            ["DIMENSION_INVALID: A dimension is not valid."],
        )

    def test_field_violation_envelope(self):
        payload = {
            "error": {
                "details": [
                    {
                        "fieldViolations": [
                            {
                                "field": "operations[0].create.type",
                                "description": 'Unknown name "maps"',
                            }
                        ]
                    }
                ]
            }
        }
        self.assertEqual(
            parse_mutate_errors(payload),
            ['operations[0].create.type: Unknown name "maps"'],
        )

    def test_the_first_error_is_kept_first(self):
        # In a failed atomic build the later RESOURCE_NOT_FOUNDs are symptoms of the
        # operation that actually failed.
        payload = {
            "error": {
                "details": [
                    {
                        "errors": [
                            {
                                "errorCode": {
                                    "contextError": "OPERATION_NOT_PERMITTED_FOR_CONTEXT"
                                },
                                "message": "cause",
                            },
                            {
                                "errorCode": {"mutateError": "RESOURCE_NOT_FOUND"},
                                "message": "symptom",
                            },
                        ]
                    }
                ]
            }
        }
        self.assertEqual(
            parse_mutate_errors(payload)[0],
            "OPERATION_NOT_PERMITTED_FOR_CONTEXT: cause",
        )

    def test_a_bare_message_still_surfaces(self):
        self.assertEqual(
            parse_mutate_errors({"error": {"message": "UNAUTHENTICATED"}}),
            ["UNAUTHENTICATED"],
        )


if __name__ == "__main__":
    unittest.main()
