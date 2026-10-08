#' What changed between versions
#'
#' Each time a publisher releases again, the site compares the new version with
#' the one before it, row by row on the dataset's key. `pd_changes()` lists
#' those comparisons; `pd_diff()` reads one of them in full.
#'
#' @inheritParams pd_dataset
#' @param from,to Version dates bounding the comparisons listed. Every
#'   comparison kept when both are `NULL`.
#' @return `pd_changes()` returns a tibble with one row per comparison,
#'   oldest first: the two versions, their rows, how many rows were added,
#'   removed, changed and unchanged, the fields added and removed, and the
#'   comparison's URL. A database is compared by each table's row count, so its
#'   added, removed and changed are `NA` and its tables added and removed are
#'   listed as fields.
#' @family metadata
#' @examplesIf pd_available()
#' pd_changes("rba-money-market-daily")
#' @export
pd_changes <- function(slug, from = NULL, to = NULL) {
  check_version(from)
  check_version(to)
  steps <- pd_get(paste0("/d/", check_slug(slug), "/changes.json"), simplify = FALSE)$changes
  num <- function(x) if (is.null(x)) NA_real_ else as.numeric(x)
  names_of <- function(s, a, b) toString(unlist(c(s[[a]], s[[b]])))
  rows <- lapply(steps, function(s) {
    data.frame(
      from = s$from,
      to = s$to,
      rows_from = num(s$rows_from),
      rows_to = num(s$rows_to),
      added = num(s$added),
      removed = num(s$removed),
      changed = num(s$changed),
      unchanged = num(s$unchanged),
      fields_added = names_of(s$schema, "fields_added", "tables_added"),
      fields_removed = names_of(s$schema, "fields_removed", "tables_removed"),
      truncated = isTRUE(s$truncated),
      url = s$url,
      stringsAsFactors = FALSE
    )
  })
  out <- if (length(rows)) {
    do.call(rbind, rows)
  } else {
    data.frame(
      from = character(), to = character(), rows_from = numeric(), rows_to = numeric(),
      added = numeric(), removed = numeric(), changed = numeric(), unchanged = numeric(),
      fields_added = character(), fields_removed = character(), truncated = logical(),
      url = character(), stringsAsFactors = FALSE
    )
  }
  if (!is.null(from)) out <- out[out$from >= from, , drop = FALSE]
  if (!is.null(to)) out <- out[out$to <= to, , drop = FALSE]
  as_tbl(out)
}

#' @rdname pd_changes
#' @param version The newer version of the pair: the comparison with the
#'   version before it. The newest when `NULL`.
#' @return `pd_diff()` returns the comparison as a list of class `pd_diff`:
#'   the counts above, the keys of the rows added, removed and changed (up to
#'   50,000 of each, with `truncated` saying when there were more) and up to
#'   ten changed rows with each changed field's old and new value.
#' @examplesIf pd_available()
#' steps <- pd_changes("rba-money-market-daily")
#' if (nrow(steps)) pd_diff("rba-money-market-daily", steps$to[nrow(steps)])
#' @export
pd_diff <- function(slug, version = NULL) {
  if (is.null(check_version(version))) version <- latest_version(slug)
  steps <- pd_changes(slug)
  hit <- steps[steps$to == version, , drop = FALSE]
  if (!nrow(hit)) {
    pd_abort(
      "no comparison ends at ", version, " for '", slug,
      "': it is the first version kept, or not a version. pd_changes() lists them."
    )
  }
  structure(pd_get(hit$url[1L], simplify = FALSE), class = "pd_diff")
}

#' @export
print.pd_diff <- function(x, ...) {
  cat("<pd_diff> ", x$dataset, ": ", x$from, " to ", x$to, "\n", sep = "")
  big <- function(n) format(n, big.mark = ",")
  if (!is.null(x$added)) {
    cat(big(x$added), " added, ", big(x$removed), " removed, ", big(x$changed), " changed, ",
      big(x$unchanged), " unchanged\n",
      sep = ""
    )
  } else {
    cat("rows: ", big(x$rows_from), " to ", big(x$rows_to), "\n", sep = "")
  }
  for (k in c("fields_added", "fields_removed", "tables_added", "tables_removed")) {
    v <- unlist(x$schema[[k]])
    if (length(v)) cat(sub("_", " ", k, fixed = TRUE), ": ", toString(v), "\n", sep = "")
  }
  if (!is.null(x$note)) cat(x$note, "\n")
  invisible(x)
}
