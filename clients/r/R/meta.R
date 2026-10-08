#' A file's URL
#'
#' The address of one version's file, for reading with another tool such as
#' `read.csv()`, 'arrow' or 'DuckDB', or for a 'targets' pipeline with
#' `format = "url"`, which reruns when the address changes.
#'
#' @inheritParams pd_download
#' @return The URL as text. With a `version` it names a file that never
#'   changes; without one it names the newest version's file, which redirects to
#'   a dated URL and moves when the publisher releases again.
#' @family files
#' @examples
#' pd_url("au-road-deaths", "csv", "2026-08-07")
#' @export
pd_url <- function(slug, format = "parquet", version = NULL, table = NULL) {
  file_url(slug, format, version, table)
}

#' The newest version of a dataset
#'
#' @inheritParams pd_dataset
#' @return The newest version's date as text, such as `"2026-09-28"`.
#' @family metadata
#' @examplesIf pd_available()
#' pd_latest("au-road-deaths")
#' @export
pd_latest <- function(slug) latest_version(slug)

#' Where a version came from
#'
#' The record kept with every version: the publisher's file it was read from,
#' with its address, name, size and SHA-256, when it was fetched, the licence as
#' the publisher stated it and when that was read, and the rows and fields it
#' holds. It answers "where did this number come from" for a reviewer.
#'
#' @inheritParams pd_download
#' @return A list of class `pd_provenance`, the version's `manifest.json`.
#' @family metadata
#' @examplesIf pd_available()
#' pd_provenance("au-road-deaths")
#' @export
pd_provenance <- function(slug, version = NULL) {
  check_slug(slug)
  structure(pd_get(paste0("/d/", slug, "/", at_path(version), "/manifest.json"), simplify = FALSE),
    class = "pd_provenance"
  )
}

#' @export
print.pd_provenance <- function(x, ...) {
  line <- function(label, value) if (length(value) && !is.null(value) && nzchar(value)) cat(sprintf("%-12s %s\n", label, value))
  cat("<pd_provenance> ", x$dataset, " ", x$version, "\n", sep = "")
  line("Source", x$source$url)
  line("File", x$filename)
  line("Bytes", format(x$bytes, big.mark = ","))
  line("SHA-256", x$sha256)
  line("Fetched", x$fetched_at)
  line("As at", x$as_at)
  line("Licence", paste0(x$licence$title, if (!is.null(x$licence$read_at)) paste0(" (read ", x$licence$read_at, ")")))
  line("Rows", format(x$rows, big.mark = ","))
  line("Version", x$url)
  invisible(x)
}

#' Open a dataset's page
#'
#' Opens the dataset's page, or one version's, in the browser, in an
#' interactive session.
#'
#' @inheritParams pd_download
#' @return The page's URL, invisibly.
#' @family find
#' @examples
#' if (interactive()) pd_browse("au-road-deaths")
#' @export
pd_browse <- function(slug, version = NULL) {
  check_slug(slug)
  url <- paste0(pd_site(), "/d/", slug, "/", if (is.null(check_version(version))) "" else paste0("v/", version, "/"))
  if (interactive()) utils::browseURL(url)
  invisible(url)
}
