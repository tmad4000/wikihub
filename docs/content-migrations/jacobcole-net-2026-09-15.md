# JacobCole.net migration audit

`jacobcole.net` remains unchanged for this migration. Its existing public index
contains a mixture of personal lists and substantial standalone projects. The
WikiHub information architecture should preserve that distinction:

- Standalone projects such as AdmitSphere remain standalone wikis.
- Small personal collections become pages or folders in the `jacobcole` wiki.
- Tabular collections become committed CSV files with a curated Markdown
  overview, rather than being flattened into prose.

## Current migration batch

| Source | Destination | Shape |
|---|---|---|
| Chocolate | `food/Chocolate.md` | Page |
| Cheese | `food/Cheese.md` | Page |
| Tea | `food/Tea.md` | Page |
| Foods list | `food/Foods.md` | Page |
| Body Masters sheet | `health/Body Masters.csv` | Git-backed data table |
| Body Masters context | `health/Body Masters.md` | Existing curated page, linked to table |

The Body Masters CSV remains an auditable snapshot in Git. Its public Google
Sheets URL is stored in `.wikihub/data-sources.json`, allowing explicit refresh
without making the wiki reader depend on Google's embed UI.

Before any future `jacobcole.net` domain cutover, repeat the page-by-page audit,
check redirects, and verify the external hostname independently. This batch does
not modify its DNS.
