# Yuren Model Topbar Themes

## Goal

Make the animated Yuren provider label use a model-specific colour band without
changing provider requests, model routing, or non-Yuren rendering.

## Behaviour

- `Yuren/gpt-5.6-terra` retains the existing green-cyan-blue animated band.
- `Yuren/gpt-5.6-sol` uses an orange-red-gold animated band.
- `Yuren/gpt-5.6-luna` uses a moonlight silver-blue-lilac animated band.
- Other providers and other Yuren models retain the standard static rendering.
- The animation timer runs only when one of the three supported Yuren model
  themes is active.

## Design

`firstcoder/app/tui.py` will hold an immutable mapping from supported model
names to colour palettes. A small helper resolves a palette only when the
provider display name is exactly `Yuren`; the existing provider/model markup
helpers and timer lifecycle use that helper. Existing ordinary-provider markup
is unchanged.

Tests in `tests/test_app_tui.py` will assert each palette's plain text,
animation movement, and distinguishing colour values; they will also assert
that unsupported Yuren models remain static and do not start the timer.
