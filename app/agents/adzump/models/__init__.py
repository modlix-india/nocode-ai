"""Typed schemas for adzump's shared session state.

Split so location data can be shared without a cycle:

  * ``place``   - the ``Place`` location primitive (a pure pydantic leaf).
  * ``product`` - the ``product_data`` schema, which composes ``Place`` AND
    the location models.

This package root re-exports ONLY the leaf (``Place``). It deliberately does
NOT import ``product`` here: a downstream module (e.g. the geo-targeting
payload builder, reached through the location package) imports ``Place`` from
this package, and if importing the package pulled in ``product`` - which
imports the location models - that back-edge would be a circular import.
Import product-side models from ``app.agents.adzump.models.product`` directly.
"""

from app.agents.adzump.models.place import Place

__all__ = ["Place"]
