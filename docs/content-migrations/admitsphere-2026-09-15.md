# AdmitSphere migration audit

Source: `https://sites.google.com/view/admitsphere/`

Destination: `https://wikihub.md/@jacobcole/admitsphere`

The source exposes 44 routes. The WikiHub destination has the 38 substantive
Markdown pages. Five source-only routes are empty section landing pages, `home`
maps to the wiki index, and `contribute_1` duplicates `contribute`. Preserve all
old deep links with `.wikihub/redirects.json` rather than adding empty pages.

## Source aliases

| Google Sites route | WikiHub page |
|---|---|
| `about` | `About.md` |
| `app-tutorialarticles` | `index.md` |
| `caltech` | `Caltech.md` |
| `college-sample-essays-advice` | `index.md` |
| `common-app` | `Common-App.md` |
| `contests-and-activities` | `Contests-and-Activities.md` |
| `contribute` | `Contribute.md` |
| `contribute_1` | `Contribute.md` |
| `duke` | `Duke.md` |
| `essays` | `Essays.md` |
| `financial-aid` | `Financial-Aid.md` |
| `gates-millenium-scholarship` | `Gates-Millennium-Scholarship.md` |
| `general-principles` | `General-Principles.md` |
| `general-tips-and-tricks` | `General-Tips-and-Tricks.md` |
| `harvard` | `Harvard.md` |
| `high-school` | `index.md` |
| `home` | `index.md` |
| `how-to-memorize-vocabulary` | `How-to-Memorize-Vocabulary.md` |
| `if-i-went-to-i-would` | `If-I-Went-To-I-Would.md` |
| `if-you-got-deferred` | `If-You-Got-Deferred.md` |
| `interviews` | `Interviews.md` |
| `making-college-decisions` | `Making-College-Decisions.md` |
| `misc-tools-resources` | `Misc-Tools-and-Resources.md` |
| `mit` | `MIT.md` |
| `northwestern` | `Northwestern.md` |
| `oxford` | `Oxford.md` |
| `paying-for-college` | `index.md` |
| `princeton` | `Princeton.md` |
| `questbridge` | `QuestBridge.md` |
| `resources` | `index.md` |
| `satap-essay-examples` | `SAT-AP-Essay-Examples.md` |
| `scholarships` | `Scholarships.md` |
| `standardized-test-prep` | `Standardized-Test-Prep.md` |
| `stanford` | `Stanford.md` |
| `the-prime-directive` | `The-Prime-Directive.md` |
| `timed-stylistic-writing` | `Timed-and-Stylistic-Writing.md` |
| `uc-berkeley-regents` | `UC-Berkeley-Regents.md` |
| `university-of-california` | `University-of-California.md` |
| `university-of-chicago` | `University-of-Chicago.md` |
| `university-of-michigan` | `University-of-Michigan.md` |
| `university-of-pennsylvania` | `University-of-Pennsylvania.md` |
| `usaco` | `USACO.md` |
| `what-to-do` | `What-To-Do.md` |
| `yale` | `Yale.md` |

The source also contains nine image assets on Caltech, Duke, University of
Chicago, and Yale pages. Store copies under `assets/` in the WikiHub repository
before changing DNS so the migrated site does not depend on Google Sites.

## Cutover order

1. Claim `admitsphere.wikihub.md` for the existing wiki.
2. Commit the alias map and the nine source image assets.
3. Verify the built-in hostname and representative deep links.
4. Verify ownership of `admitsphere.org` and `www.admitsphere.org`.
5. Point both hostnames at WikiHub, verify HTTPS, then activate the apex domain.
6. Keep the old DNS values in the deployment record for rollback.
