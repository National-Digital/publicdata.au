# 0004: The typeface's free styles are in the repository under their own licence

- Status: **Accepted** (#104)
- Date: 2026-10-08
- Deciders: National Digital

## Context

The site is set in Random Grotesque (`RG-StandardRegular`, `RG-StandardMedium`, `RG-StandardBold`)
by RandomMaerks (Bao Nguyen). National Digital holds the free package, Package 1A (update 2.1),
under the RandomMaerks End-User License Agreement, License type "Commercial" (050424 RM-EULA
Type-C). That agreement allows the free styles to be used for websites (clause 1.3) and
redistributed with attribution (clause 4.3). It forbids redistributing the paid styles without
consent (clause 4.4) and changing a font file's extension (clause 3.2).

Until October 2026 the files were treated as licensed for our sites only. They were kept out of
git and the deploy fetched them from a private bucket, so a contributor's build fell back to
system fonts.

## Decision

- The three free styles are committed in `pipeline/publicdata/static/fonts/` as the package
  supplies them, in WOFF2 and unconverted.
- `RandomGrotesque-EULA.txt` sits beside them. It is the attribution clause 4.3 requires and it
  reproduces the agreement.
- The fonts are under the RM-EULA Type-C, which is neither the code's AGPL nor the CC BY 4.0 of
  the register text. `THIRD-PARTY-NOTICES.md` says so.
- Only styles from the free package may be added. A paid style never enters the repository, and
  a test fails if any font file other than the three free styles is tracked.

## Alternatives considered

- **Keep the fonts private and fetch them in the deploy**, as before. A contributor's build fell
  back to system fonts, so no one outside the deploy saw the site or its social cards as they
  appear live, and the deploy needed a credential to fetch them. Clause 4.3 permits the
  redistribution that a public repository is, provided the attribution travels with the files.

Committing the package's own files, unconverted, keeps clause 3.2 satisfied and makes their source
plain.

## Consequences

- The agreement can change, and licensees are bound by its latest version. A change that withdraws
  clause 4.3 would mean removing the files from the repository.
- Using a weight outside the free package means buying it and serving it from private storage,
  never committing it.
- The `_brand/fonts/` objects in `publicdata-raw` are no longer read.

See `THIRD-PARTY-NOTICES.md` ("Typeface") and
`pipeline/publicdata/static/fonts/RandomGrotesque-EULA.txt`.
