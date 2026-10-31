Golden prompt fixtures (D44) — deliberate tripwires.

Each pair pins the EXACT (system, user) text that `prompts/compose.py`
assembles for one prompt variant:

  single_broker_invoice            profile schema path (no pin)
  single_broker_invoice_pinned     pinned pages 1-2, signals omitted
  multi_broker_invoice_air_waybill    multi-document wrapper
  generic_test                     fieldless generic minimal schema
                                   (uses synthetic_generic_profile.json)
  catalog                          discovery sweep (static)
  recheck_broker_invoice           narrow Check-4 subset (invoice_number, boe_date)

A failing comparison means prompt text or assembly changed. That must be
deliberate: update the fixture to the new expected text, bump
`prompts/text.PROMPT_VERSION`, and run the paid eval matrix (`make eval`)
before accepting the change. Never regenerate fixtures just to make the
tests pass.
