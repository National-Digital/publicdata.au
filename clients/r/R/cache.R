use_cache <- function(cache) {
  isTRUE(if (is.null(cache)) getOption("publicdataau.cache", FALSE) else cache)
}

#' The download cache
#'
#' A version of a dataset never changes once published, so a file downloaded
#' once can be kept and read again without another download. Nothing is kept
#' unless you ask: pass `cache = TRUE` to [pd_read()], [pd_download()],
#' [pd_connect()], [pd_tbl()] or [pd_sf()], or set
#' `options(publicdataau.cache = TRUE)` for every call.
#'
#' `pd_cache_dir()` is where kept files go: `tools::R_user_dir("publicdataau",
#' "cache")` unless `options(publicdataau.cache_dir = )` names another folder.
#' `pd_cache_list()` lists what is kept and `pd_cache_clear()` deletes it.
#'
#' @param slug Only this dataset's files. Every dataset when `NULL`.
#' @param version Only this version's files. Every version when `NULL`.
#' @return `pd_cache_dir()` returns the folder's path. `pd_cache_list()`
#'   returns a tibble with one row per kept file: its dataset, version,
#'   file, size in bytes and when it was saved. `pd_cache_clear()` returns the
#'   number of bytes freed, invisibly.
#' @family files
#' @examples
#' pd_cache_dir()
#' pd_cache_list()
#' @name cache
NULL

#' @rdname cache
#' @export
pd_cache_dir <- function() {
  getOption("publicdataau.cache_dir", tools::R_user_dir("publicdataau", "cache"))
}

#' @rdname cache
#' @export
pd_cache_list <- function(slug = NULL, version = NULL) {
  root <- cache_root(slug, version)
  files <- if (dir.exists(root)) list.files(root, recursive = TRUE, full.names = TRUE) else character()
  files <- files[!grepl("\\.part$", files)]
  rel <- substring(normalizePath(files, "/", FALSE), nchar(normalizePath(pd_cache_dir(), "/", FALSE)) + 2L)
  parts <- strsplit(rel, "/", fixed = TRUE)
  info <- file.info(files)
  tibble::tibble(
    dataset = vapply(parts, `[`, character(1L), 1L),
    version = vapply(parts, `[`, character(1L), 2L),
    file = vapply(parts, function(p) paste(p[-(1L:2L)], collapse = "/"), character(1L)),
    bytes = as.numeric(info$size),
    saved = info$mtime
  )
}

#' @rdname cache
#' @export
pd_cache_clear <- function(slug = NULL, version = NULL) {
  root <- cache_root(slug, version)
  if (!dir.exists(root)) {
    return(invisible(0.0))
  }
  files <- list.files(root, recursive = TRUE, full.names = TRUE, all.files = TRUE)
  freed <- sum(file.info(files)$size, na.rm = TRUE)
  unlink(root, recursive = TRUE)
  invisible(freed)
}

cache_root <- function(slug, version) {
  if (!is.null(version) && is.null(slug)) pd_abort("a version needs its dataset's slug")
  root <- pd_cache_dir()
  if (!is.null(slug)) root <- file.path(root, check_slug(slug))
  if (!is.null(check_version(version))) root <- file.path(root, version)
  root
}

# One version's file: from the cache when it is on, else downloaded to a temporary file
# that the caller removes.
fetch_file <- function(slug, format, version = NULL, table = NULL, cache = NULL) {
  if (!use_cache(cache)) {
    tmp <- tempfile(fileext = paste0(".", format))
    got <- save_file(slug, format, version, tmp, table)
    return(list(path = tmp, version = got$version, temp = TRUE))
  }
  if (is.null(check_version(version))) version <- latest_version(slug)
  name <- if (is.null(check_table(table))) paste0("data.", format) else file.path("tables", paste0(table, ".parquet"))
  dest <- file.path(pd_cache_dir(), check_slug(slug), version, name)
  if (!file.exists(dest)) {
    dir.create(dirname(dest), recursive = TRUE, showWarnings = FALSE)
    part <- paste0(dest, ".part")
    on.exit(unlink(part), add = TRUE)
    save_file(slug, format, version, part, table)
    if (!file.rename(part, dest)) pd_abort("could not save ", dest)
  }
  list(path = dest, version = version, temp = FALSE)
}
