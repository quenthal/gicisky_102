# Bundled fonts and icons

Only assets that may be redistributed are kept here:

- `NotoSansKR-*.ttf` (Thin … Black): Noto Sans KR, SIL Open Font License 1.1.
  `NotoSansKR-Regular.ttf` is the renderer default and is required.
- `materialdesignicons-webfont.ttf` and `materialdesignicons-webfont_meta.json`:
  Material Design Icons (Pictogrammers; icons Apache 2.0, font SIL OFL 1.1). Required
  for `icon` elements.

Same files as upstream `eigger/hass-gicisky`
(https://github.com/eigger/hass-gicisky/tree/master/custom_components/gicisky/fonts).

Fonts with unclear redistribution terms (CookieRun, GmarketSans, OwnglyphParkDaHyun)
are deliberately left out. Put them in `/config/www/fonts/` and reference them by bare
file name (`font: CookieRunBold.ttf`); this folder is replaced on a HACS update.
