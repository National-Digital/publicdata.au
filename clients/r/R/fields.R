#' A dataset's fields
#'
#' What each field holds: its name, type and the publisher's description, with
#' the range of a number or date and the values of a field that holds few. Read
#' it before filtering with [pd_rows()], since a condition must name a field
#' exactly and match its values exactly.
#'
#' @inheritParams pd_download
#' @return A tibble with one row per field: `name`, `type` (`"string"`,
#'   `"integer"`, `"number"`, `"boolean"`, `"date"` or `"datetime"`),
#'   `description`, `min` and `max` as text, and `values`, a list of the values
#'   a field holds when it holds few. For a database such as G-NAF there is a
#'   row per field of every table, with a `table` column, and no ranges.
#'   Without `version` the fields are the newest version's; a pinned version is
#'   described by its schema, without ranges or values.
#' @family metadata
#' @examplesIf pd_available()
#' pd_fields("au-road-deaths")
#' @export
pd_fields <- function(slug, version = NULL) {
  check_slug(slug)
  if (is.null(check_version(version))) {
    f <- tryCatch(
      pd_get(paste0("/d/", slug, "/fields.json"), simplify = FALSE),
      httr2_http_404 = function(e) NULL
    )
    if (!is.null(f)) {
      return(fields_tibble(f$fields))
    }
  }
  s <- pd_get(paste0("/d/", slug, "/", at_path(version), "/schema.json"), simplify = FALSE)
  if (identical(s$kind, "database")) {
    every <- unlist(lapply(s$tables, function(t) t$fields), recursive = FALSE)
    tables <- unlist(lapply(s$tables, function(t) rep(t$name, length(t$fields))))
    return(tibble::add_column(fields_tibble(every), table = tables, .before = 1L))
  }
  fields_tibble(s$fields)
}

at_path <- function(version) {
  if (is.null(check_version(version))) "latest" else paste0("v/", version)
}

fields_tibble <- function(fields) {
  as_text <- function(x) if (is.null(x)) NA_character_ else as.character(x)
  tibble::tibble(
    name = vapply(fields, function(f) f$name, character(1L)),
    type = vapply(fields, function(f) as_text(f$type), character(1L)),
    description = vapply(fields, function(f) as_text(f$description), character(1L)),
    min = vapply(fields, function(f) as_text(f$min), character(1L)),
    max = vapply(fields, function(f) as_text(f$max), character(1L)),
    values = lapply(fields, function(f) unlist(f$values))
  )
}

# The fields of the version an answer came from, read once per session, for typing and
# labelling it. An answer is still returned, untyped, when they cannot be read.
field_meta <- function(slug, table = NULL, version = NULL) {
  key <- paste("fields", slug, version %||% "latest", table %||% "")
  if (!is.null(state$memo[[key]])) {
    return(state$memo[[key]])
  }
  f <- tryCatch(pd_fields(slug, version), error = function(e) NULL)
  if (is.null(f)) {
    return(NULL)
  }
  if ("table" %in% names(f)) f <- f[f$table == (table %||% "records"), , drop = FALSE]
  state$memo[[key]] <- f
  f
}

# Columns as the field types say: the query API sends dates as text and booleans as 0 and 1.
# A column that does not parse cleanly is left as it came.
typed <- function(df, fields) {
  if (is.null(fields) || !nrow(fields) || !ncol(df)) {
    return(df)
  }
  for (col in intersect(names(df), fields$name)) {
    x <- df[[col]]
    y <- as_type(x, fields$type[match(col, fields$name)])
    if (sum(is.na(y)) == sum(is.na(x))) df[[col]] <- y
  }
  df
}

as_type <- function(x, type) {
  switch(type,
    integer = {
      fits <- is.numeric(x) && all(is.na(x) | abs(x) < .Machine$integer.max)
      if (fits) as.integer(x) else x
    },
    number = suppressWarnings(as.numeric(x)),
    boolean = if (is.logical(x)) x else as.logical(suppressWarnings(as.numeric(x))),
    date = {
      if (inherits(x, "Date")) x else suppressWarnings(as.Date(as.character(x), optional = TRUE))
    },
    datetime = if (inherits(x, "POSIXct")) x else parse_datetime(x),
    x
  )
}

parse_datetime <- function(x) {
  x <- as.character(x)
  x <- sub("Z$", "+0000", x)
  x <- sub("([+-][0-9]{2}):([0-9]{2})$", "\\1\\2", x)
  x <- sub(" ", "T", x, fixed = TRUE)
  zoned <- grepl("[+-][0-9]{4}$", x)
  out <- as.POSIXct(rep(NA_real_, length(x)), tz = "UTC")
  if (any(zoned)) out[zoned] <- as.POSIXct(x[zoned], tz = "UTC", format = "%Y-%m-%dT%H:%M:%OS%z")
  if (!all(zoned)) {
    patterns <- c("%Y-%m-%dT%H:%M:%OS", "%Y-%m-%dT%H:%M", "%Y-%m-%d")
    out[!zoned] <- as.POSIXct(x[!zoned], tz = "UTC", tryFormats = patterns, optional = TRUE)
  }
  out
}

# Each column's description as its "label" attribute, which RStudio's viewer shows.
labelled <- function(df, fields) {
  if (is.null(fields) || !nrow(fields)) {
    return(df)
  }
  for (col in intersect(names(df), fields$name)) {
    d <- fields$description[match(col, fields$name)]
    if (!is.na(d) && nzchar(d)) attr(df[[col]], "label") <- d
  }
  df
}
