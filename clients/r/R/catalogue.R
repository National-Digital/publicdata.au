JUR_CODES <- c(
  cth = "cth", commonwealth = "cth", "australian government" = "cth", federal = "cth",
  nsw = "nsw", "new south wales" = "nsw", vic = "vic", victoria = "vic",
  qld = "qld", queensland = "qld", wa = "wa", "western australia" = "wa",
  sa = "sa", "south australia" = "sa", tas = "tas", tasmania = "tas",
  act = "act", "australian capital territory" = "act", nt = "nt", "northern territory" = "nt"
)

#' Search every dataset on the government portals
#'
#' publicdata.au reads the catalogues of the Australian government open data
#' portals, well over a hundred thousand datasets, of which it serves a small
#' part. This searches all of them, so a dataset that is not served yet can be
#' found, and says which are served. Vote for one to be built on its page.
#'
#' @param q Words to search titles, summaries and publishers for. The newest
#'   records when `NULL`.
#' @param jurisdiction A government, as a code such as `"qld"` or a name such
#'   as `"Queensland"`.
#' @param status Any of `"served"` (on publicdata.au), `"votable"` (can be
#'   voted for), `"chosen"` (being built) and `"closed"` (cannot be built, with
#'   the reason).
#' @param limit Records to return, 1 to 50.
#' @param offset Records to skip, for the next page.
#' @return A tibble with one row per record: its `id`, `title`, `summary`,
#'   `publisher`, `jurisdiction`, portal `url`, `licence`, `formats`,
#'   `modified`, `status`, the publicdata.au `page` when served and the
#'   `reason` when closed. `attr(x, "total")` is the number of matches.
#' @family find
#' @examplesIf pd_available()
#' pd_catalogue("water quality", jurisdiction = "Queensland", limit = 5)
#' @export
pd_catalogue <- function(q = NULL, jurisdiction = NULL, status = NULL, limit = 20, offset = 0) {
  jur <- NULL
  if (!is.null(jurisdiction)) {
    jur <- unname(JUR_CODES[one_text(jurisdiction, "jurisdiction")])
    if (is.na(jur)) pd_abort("jurisdiction is one of ", paste(unique(JUR_CODES), collapse = ", "))
  }
  body <- pd_get("/api/v1/catalogue", list(q = q, jur = jur, state = joined(status), limit = limit, offset = offset),
    simplify = FALSE
  )
  rows <- body$rows
  text <- function(r, k) if (is.null(r[[k]])) NA_character_ else as.character(r[[k]])
  cols <- c(
    id = "id", title = "title", summary = "summary", publisher = "publisher", jurisdiction = "jur",
    url = "url", licence = "licence", formats = "formats", modified = "modified", status = "state",
    page = "page", reason = "reason"
  )
  out <- tibble::as_tibble(lapply(cols, function(k) vapply(rows, text, character(1), k = k)))
  attr(out, "total") <- body$total
  attr(out, "next_offset") <- body$next_offset
  out
}
