# 0020: A dataset is published only under a licence a person has checked, and the gate enforces it

- Status: **Accepted** (the initial public release; publishers' answers to a request for permission
  recorded on their entries: #136)
- Date: 2026-10-04 (recorded 2026-10-10)
- Deciders: National Digital
- Relates to: [0002](0002-versions-are-kept.md), [0022](0022-copies-on-the-hubs.md)

## Context

The site republishes data it does not own, so every file it serves rests on the publisher's
licence. A portal's licence field is often blank, generic ("Licence specified by agency") or at
odds with the publisher's own statement, and a publisher can change its licence between releases.
A file served under a licence that does not allow it is a liability for the site and a reason for
publishers to distrust it.

## Decision

- Licence is data. Every register entry carries a licence id, an evidence URL from the publisher's
  own statement, the attribution the licence asks for and the date a person reviewed it.
- Every fetch reads the licence again and records where and when in the version's manifest. The
  gate refuses a dataset whose licence is not open or has changed since a person reviewed it.
- Creative Commons attribution licences, share-alike included, and CC0 are published. A grant that
  is not Creative Commons is admitted only through a file in `register/licences/` that quotes the
  publisher on reproduction, adaptation, commercial use and attribution. An open licence with a
  condition of use, such as G-NAF's, is published with the condition on the dataset and version
  pages and in every file's provenance.
- No-derivatives, non-commercial and restricted licences are never published. Such an entry stays
  in the register as `blocked`, with the reason shown on the backlog.
- Where we ask a publisher for permission directly, the entry records when we asked, when the
  publisher answered and what it said (#136). A permission granted becomes a grant in
  `register/licences/`.
- A version stays up under the licence it was published under.

## Alternatives considered

- **Trust the portal's licence field.** It is too often blank, generic or at odds with the
  publisher's own statement. A blank field holds an entry as `blocked` and a generic one as
  `assessing` until a person records the publisher's own words.

## Consequences

- A licence change stops a dataset until a person has read the new licence and updated the entry.
- Datasets people ask for can stay blocked for good, and the backlog says why.
- A dataset with a condition of use is never copied to the hubs, since no hub can state the
  condition ([0022](0022-copies-on-the-hubs.md)).
- The data's licences are separate from the code's licence and from the CC BY 4.0 of the register
  text, and stay that way.

See `docs/ARCHITECTURE.md` ("Rules that decide the code", "Licence gate") and `CONTRIBUTING.md`
("Licences that are not Creative Commons").
