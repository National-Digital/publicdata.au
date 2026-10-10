# 0020: The typeface's free styles are in the repository under their own licence

- Status: Accepted
- Date: 2026-10-10

## Context

The site is set in Random Grotesque (`RG-StandardRegular`, `RG-StandardMedium`, `RG-StandardBold`)
by RandomMaerks (Bao Nguyen). National Digital holds the free package, Package 1A (update 2.1),
under the RandomMaerks End-User License Agreement, License type "Commercial" (050424 RM-EULA
Type-C). That agreement allows the free styles to be used for websites (clause 1.3) and
redistributed with attribution (clause 4.3). It forbids redistributing the paid styles without
consent (clause 4.4) and changing a font file's extension (clause 3.2).

A contributor's build should look like the live site, social cards included.

## Decision

- The three free styles are committed in `pipeline/publicdata/static/fonts/` as the package
  supplies them, in WOFF2 and unconverted, which keeps clause 3.2 satisfied and makes their source
  plain.
- `RandomGrotesque-EULA.txt` sits beside them. It is the attribution clause 4.3 requires and it
  reproduces the agreement.
- The fonts are under the RM-EULA Type-C, which is neither the code's AGPL nor the CC BY 4.0 of
  the register text. `THIRD-PARTY-NOTICES.md` says so.
- Only styles from the free package may be added. A paid style never enters the repository, and
  a test fails if any font file other than the three free styles is tracked.

## Alternatives considered

- **Keep the fonts private and fetch them in the deploy.** A contributor's build would fall back to
  system fonts, so no one outside the deploy would see the site or its social cards as they appear
  live, and the deploy would need a credential to fetch them.

## Consequences

- The agreement can change, and licensees are bound by its latest version. A change that withdraws
  clause 4.3 would mean removing the files from the repository.
- Using a weight outside the free package means buying it and serving it from private storage,
  never committing it.

See `THIRD-PARTY-NOTICES.md` ("Typeface") and
`pipeline/publicdata/static/fonts/RandomGrotesque-EULA.txt`.
