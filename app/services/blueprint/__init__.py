"""Blueprints: what an application is MEANT to be, as opposed to what it is.

An app's definitions record what it is. Nothing records what it was meant to be,
so an agent asked to change a page infers intent from nine hundred components, a
customer asked to restructure a site has nothing to restructure but the output,
and every regeneration starts from zero.

The blueprint is that missing layer: a machine-readable plan carried on each
object as a `blueprint` field on `AbstractOverridableDTO`, plus a decision ledger
explaining why. The data model is written up in `nocode-saas/docs/blueprint.md`.

This package is the service side of it:

  validate.py  the write gate — no arrays at any depth, addressable keys, a size
               budget that refuses rather than truncates
  objects.py   reading and writing the field on a platform object, override-aware
  service.py   generate / describe / suggest_features — three model calls, no
               agent loop
  prompts.py   what the model is told about the schema
  router.py    /api/ai/blueprint
  tools.py     three agent tools
  context.py   the per-request brief pushed into the system prompt
"""
