#' How to cite a version
#'
#' A citation for one version of a dataset, naming the publisher, the version
#' and the URL it is kept at, with the attribution the publisher's licence
#' requires. Print it for text, or pass it to [utils::toBibtex()] for a
#' reference manager.
#'
#' @inheritParams pd_download
#' @return A [utils::bibentry()] object.
#' @family metadata
#' @examplesIf pd_available()
#' cit <- pd_cite("rba-cash-rate")
#' print(cit, style = "text")
#' utils::toBibtex(cit)
#' @export
pd_cite <- function(slug, version = NULL) {
  dp <- pd_get(paste0("/d/", check_slug(slug), "/datapackage.json"), simplify = FALSE)
  if (is.null(check_version(version))) version <- dp$version
  page_url <- paste0(pd_site(), "/d/", slug, "/v/", version, "/")
  pub <- Filter(function(c) identical(c$role, "publisher"), dp$contributors)
  publisher <- if (length(pub)) pub[[1L]]$title else "publicdata.au"
  licence_names <- toString(vapply(dp$licenses, function(l) l$title %||% l$name, character(1L)))
  note <- if (identical(version, dp$version) && !is.null(dp[["publicdata:attribution"]])) {
    dp[["publicdata:attribution"]]
  } else {
    m <- pd_get(paste0("/d/", slug, "/v/", version, "/manifest.json"), simplify = FALSE)
    fetched <- substr(m$fetched_at, 1L, 10L)
    paste0("Licensed under ", licence_names, ", read from the publisher on ", fetched, ".")
  }
  utils::bibentry(
    bibtype = "Misc",
    key = paste0(slug, "-", version),
    title = dp$title,
    author = utils::person(publisher),
    year = substr(version, 1L, 4L),
    howpublished = paste0(
      "Version ", version, ", serialised and versioned by National Digital at publicdata.au"
    ),
    url = page_url,
    note = sub("\\.$", "", note)
  )
}
