# Storing identical files once: measured and deferred

Issue #68 asks for each distinct file to be stored once in `publicdata-dist`, with every version
path resolving to it, and for the saving to be measured on the live bucket first. Decision record
0009 (#49) already proposes it. This note records the measurement, what the design must add to
0009, and why the work waits. It belongs in 0009 once that record is merged.

## Measurement, 8 October 2026

`publicdata r2 shared-report` lists the bucket and compares dated version files of equal size by
their stored bytes. Two single-part ETags are MD5s and settle it. In a size group that holds a
multipart ETag it reads the SHA-256 stored with each object, which took 54 HEAD requests. It
writes nothing. No object in the bucket was gzipped yet, since `restore-gzip` from #51 had not
run.

| | objects | bytes |
|---|---|---|
| whole bucket | 11,894 | 93,292,288,157 |
| dated version files, pages left out | 10,625 | 90,322,326,562 |
| copies of another file's bytes | 114 | 914,622 |

The saving is 914,622 bytes, or 0.001% of the dated files. By type it is 535,062 bytes of CSV in
5 copies, 190,679 bytes of JSON in 104 copies and 188,881 bytes of `data.csv.gz` in 5 copies.
Most of the JSON is `schema.json` repeated across versions of one dataset. The largest single
case is the CSV of `vic-heritage-register`, unchanged across three versions. Only 35,743 bytes
repeat across datasets.

The 24 datasets with more than one version hold 25.35 GB in their older versions. Apart from the
copies above, none of it repeats a newer version's bytes. A version is cut when the source
file's SHA-256 changes, and every writer except CSV and `schema.json` embeds the version's
provenance header, so a file whose rows did not move still differs by its header. Four datasets
cut versions whose CSV is byte for byte the same, and the other formats of those versions hold
about 141 MB that differs by the header alone. Content addressing cannot recover that.

Once #51's restore runs, the text copies shrink with everything else. The estimate from #51's
ratios is a factor of 7 to 12, which leaves the saving well under a megabyte.

## What period parts change

#53, still open, writes a finished period part once. When a part's rows and schema match the
snapshot before, the next manifest points at the earlier snapshot's file, which stays at its own
version path. The reuse is decided on rows, so the provenance header in each part does not stop
it, and no request needs a lookup to find the file.

What #53 leaves to content addressing is the files it rewrites on every snapshot: the current
period's part and the whole-table files kept beside small parts. Their `csv.gz` is the same bytes
when the rows did not move, and their Parquet differs by the header. A part revised and later
put back, or a file shared between datasets, also differs by the header, so content addressing
would not catch those.

## What the design must add to 0009

- Each object is stored at `o/<sha256>` keyed by its stored bytes, or by its decoded bytes with
  the encoding recorded beside the hash, since the same file is gzipped at one path and stored
  plain at another under `stored_gzipped`.
- A file is written at its version path as today, and a version refers to an `o/` object only
  when that hash is already held. Only shared files then cost the extra read on each request.
- An `o/` object is written with `ChecksumSHA256`, so R2 checks the bytes against the hash, and
  with `IfNoneMatch: "*"`, so a retried or concurrent push never overwrites one.
- The function must follow the reference wherever it reads R2 today: the `data.csv.gz` alias from
  #51, the second read for a range on a gzipped file, and the size test for the edge cache.
  `decode_stored` and `build --published` must follow it too. The download name, the content
  type and the 410 for a withheld dataset already come from the requested path.
- `dist-push --expect` must fail when a reference leads to no `o/` object.
- An `o/` object stays while any version refers to it. A legal takedown or a publisher's removal
  request removes the reference, and deletes the object only when no other version refers to it.
- An empty pointer object would list at size 0, which `shared-report`, `restore-gzip` and the
  storage projection in #50 read from the listing. 0009's manifest mapping avoids that.

## Recommendation

Leave #68 open and run `publicdata r2 shared-report` again once #53 has run for a quarter. The
dated files cost about $1.35 a month at R2's $0.015 per GB-month, so the 914,622 bytes measured
are worth less than a cent. Build the design when the report shows a saving worth more than the
extra R2 read on each request for a shared file, which at $0.36 per million reads means a saving
of several gigabytes.
