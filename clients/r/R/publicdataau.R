#' @details
#' Every function takes a dataset's slug, the part of its page's URL after
#' `/d/`, so a dataset added to the site works with no new release. Start with
#' [pd_datasets()] to find one, [pd_fields()] to see what it holds, then
#' [pd_rows()], [pd_aggregate()], [pd_read()] or [pd_tbl()] to use it.
#'
#' @section Options:
#' \describe{
#'   \item{`publicdataau.cache`}{`TRUE` keeps every downloaded file in
#'     [pd_cache_dir()] for reuse. Off by default.}
#'   \item{`publicdataau.cache_dir`}{Where the cache is kept.}
#'   \item{`publicdataau.quiet`}{`TRUE` turns off the licence notice and the
#'     download progress bar.}
#'   \item{`publicdataau.site`}{Another copy of the site to read, such as a
#'     preview. The `PUBLICDATA_SITE` environment variable does the same.}
#' }
#'
#' @section Errors:
#' Every error the package raises has the class `publicdataau_error`. One that
#' comes from failing to reach the site also has `publicdataau_unreachable`,
#' and asking [pd_sf()] for a dataset without a map layer has
#' `publicdataau_no_layer`, so either can be caught with `tryCatch()`. An error
#' answer from the site keeps the classes 'httr2' gives it, such as
#' `httr2_http_404`.
#'
#' @keywords internal
"_PACKAGE"

pd_site <- function() {
  site <- getOption("publicdataau.site", Sys.getenv("PUBLICDATA_SITE", "https://publicdata.au"))
  sub("/+$", "", site)
}

formats <- c("parquet", "csv", "csv.gz", "json", "ndjson", "sqlite", "duckdb", "xlsx", "arrow", "geojson", "gpkg", "geo.parquet")

check_table <- function(table) {
  if (is.null(table)) return(NULL)
  if (!is.character(table) || length(table) != 1 || !grepl("^[A-Za-z0-9_]+$", table)) {
    pd_abort("not a table name: ", format(table))
  }
  table
}

check_slug <- function(slug) {
  if (!is.character(slug) || length(slug) != 1 || !grepl("^[A-Za-z0-9-]+$", slug)) {
    pd_abort("not a dataset slug: ", format(slug))
  }
  slug
}

check_version <- function(version) {
  if (is.null(version)) return(NULL)
  if (!is.character(version) || length(version) != 1 || !grepl("^[0-9]{4}-[0-9]{2}-[0-9]{2}$", version)) {
    pd_abort("a version is a date such as \"2026-08-07\"")
  }
  version
}

pd_request <- function(url) {
  req <- httr2::request(url)
  req <- httr2::req_user_agent(req, paste0("publicdataau-r/", utils::packageVersion("publicdataau")))
  # A stalled connection fails; a slow download of a large file does not.
  req <- httr2::req_options(req, connecttimeout = 30, low_speed_time = 60, low_speed_limit = 1)
  # The API allows a burst per address, then answers 429 with how long to wait; a passing server
  # error is tried again after a pause.
  req <- httr2::req_retry(req, max_tries = 4, is_transient = function(resp) {
    httr2::resp_status(resp) %in% c(429, 500, 502, 503, 504)
  })
  httr2::req_error(req, body = function(resp) {
    body <- tryCatch(httr2::resp_body_json(resp), error = function(e) NULL)
    if (is.list(body) && !is.null(body$error)) body$error else NULL
  })
}

pd_perform <- function(req, ...) {
  tryCatch(
    httr2::req_perform(req, ...),
    httr2_failure = function(e) {
      host <- regmatches(req$url, regexpr("^[a-z]+://[^/]+", req$url))
      pd_abort("could not reach ", host, ": ", conditionMessage(e),
               "\nCheck the connection, or try again later.", class = "publicdataau_unreachable")
    }
  )
}

pd_get <- function(path, query = list(), simplify = TRUE) {
  url <- if (grepl("://", path, fixed = TRUE)) path else paste0(pd_site(), path)
  req <- pd_request(url)
  query <- query[!vapply(query, is.null, logical(1))]
  if (length(query)) req <- httr2::req_url_query(req, !!!query)
  httr2::resp_body_json(pd_perform(req), simplifyVector = simplify)
}

latest_version <- function(slug) {
  pd_get(paste0("/d/", check_slug(slug), "/versions.json"))$latest
}

#' Is publicdata.au reachable?
#'
#' Asks the site for its catalogue's headers, with a short timeout. Use it to
#' skip work that needs the site when there is no connection.
#'
#' @return `TRUE` when the site answers, else `FALSE`. It never fails.
#' @family find
#' @examples
#' pd_available()
#' @export
pd_available <- function() {
  req <- httr2::req_method(pd_request(paste0(pd_site(), "/catalog.json")), "HEAD")
  req <- httr2::req_timeout(httr2::req_retry(req, max_tries = 1), 10)
  ok <- tryCatch(httr2::resp_status(httr2::req_perform(req)) < 400, error = function(e) FALSE)
  isTRUE(ok)
}

#' Filters for pd_rows() and pd_aggregate()
#'
#' Each helper builds one condition in the query API's `operator.value` form.
#' A plain value passed to [pd_rows()] must match exactly, a vector of several
#' values matches any of them, and `NA` matches a blank or suppressed cell.
#'
#' @param value A value to compare with. Numbers and dates compare as numbers
#'   and dates, text as text.
#' @param pattern A pattern in which `*` stands for any run of characters.
#' @param ... Values, any of which may match. None may contain a comma.
#' @param filter A filter to negate.
#' @return A `pd_filter` object.
#' @family query
#' @examples
#' pd_gte(2020)
#' pd_in("QLD", "NSW")
#' pd_not(pd_is_null())
#' @name filters
NULL

new_filter <- function(expr) structure(list(expr = expr), class = "pd_filter")

fv <- function(value) {
  if (length(value) != 1) pd_abort("a filter takes one value; pd_in() takes several")
  if (is.na(value)) pd_abort("a filter cannot compare with NA; use pd_is_null()")
  if (is.logical(value)) return(if (value) "true" else "false")
  format(value, scientific = FALSE, trim = TRUE)
}

#' @rdname filters
#' @export
pd_eq <- function(value) new_filter(paste0("eq.", fv(value)))
#' @rdname filters
#' @export
pd_neq <- function(value) new_filter(paste0("neq.", fv(value)))
#' @rdname filters
#' @export
pd_gt <- function(value) new_filter(paste0("gt.", fv(value)))
#' @rdname filters
#' @export
pd_gte <- function(value) new_filter(paste0("gte.", fv(value)))
#' @rdname filters
#' @export
pd_lt <- function(value) new_filter(paste0("lt.", fv(value)))
#' @rdname filters
#' @export
pd_lte <- function(value) new_filter(paste0("lte.", fv(value)))
#' @rdname filters
#' @export
pd_like <- function(pattern) new_filter(paste0("like.", pattern))
#' @rdname filters
#' @export
pd_ilike <- function(pattern) new_filter(paste0("ilike.", pattern))
#' @rdname filters
#' @export
pd_in <- function(...) {
  values <- vapply(as.list(unlist(list(...))), fv, character(1))
  if (any(grepl(",", values, fixed = TRUE))) pd_abort("a value in pd_in() cannot contain a comma")
  new_filter(paste0("in.(", paste(values, collapse = ","), ")"))
}
#' @rdname filters
#' @export
pd_is_null <- function() new_filter("is.null")
#' @rdname filters
#' @export
pd_not <- function(filter) new_filter(paste0("not.", filter$expr))

#' @export
print.pd_filter <- function(x, ...) {
  cat("<pd_filter>", x$expr, "\n")
  invisible(x)
}

as_filter <- function(value) {
  if (inherits(value, "pd_filter")) return(value$expr)
  if (is.null(value) || (length(value) == 1 && is.na(value))) return("is.null")
  if (length(value) > 1) return(pd_in(value)$expr)
  pd_eq(value)$expr
}

conditions <- function(where) {
  if (!length(where)) return(list())
  if (is.null(names(where)) || any(names(where) == "")) {
    pd_abort("every condition needs a field name, as in pd_rows(slug, state = \"QLD\")")
  }
  lapply(where, as_filter)
}

joined <- function(x) if (is.null(x)) NULL else paste(x, collapse = ",")

raw_rows <- function(body) {
  rows <- body$rows
  if (is.null(rows) || length(rows) == 0) return(data.frame())
  as.data.frame(rows, stringsAsFactors = FALSE)
}

# An answer's rows as a tibble, typed and labelled from the dataset's fields, with its provenance.
answer <- function(rows, slug, meta, nxt = NULL, version = NULL) {
  f <- field_meta(slug, version = version)
  out <- as_tbl(labelled(typed(rows, f), f))
  attr(out, "publicdata") <- meta
  attr(out, "next") <- nxt
  out
}

#' Datasets served by publicdata.au
#'
#' @param q Words to search titles, summaries, publishers, keywords and field
#'   names for. Every served dataset when `NULL`.
#' @param publisher Part of a publisher's name, such as `"Bureau of
#'   Meteorology"` or `"Transport"`, ignoring case.
#' @param topic A topic the site files datasets under, such as `"roads"`,
#'   `"crime"` or `"housing"`. An unknown topic fails with the list of topics.
#' @param jurisdiction The government a publisher belongs to, as a code such
#'   as `"Qld"` or `"Cth"` or a name such as `"Queensland"`, ignoring case.
#' @return A tibble with one row per dataset: its slug, title, publisher,
#'   licence, page and newest Parquet file. Every condition given must match.
#' @family find
#' @examplesIf pd_available()
#' pd_datasets("road crashes")
#' pd_datasets(publisher = "Transport", jurisdiction = "Queensland")
#' @export
pd_datasets <- function(q = NULL, publisher = NULL, topic = NULL, jurisdiction = NULL) {
  out <- as_tbl(pd_get("/api/v1/datasets", list(q = q))$results)
  if (is.null(publisher) && is.null(topic) && is.null(jurisdiction)) return(out)
  keep <- catalogue_match(publisher, topic, jurisdiction)
  if (!nrow(out)) return(out)
  out[out$slug %in% keep, , drop = FALSE]
}

one_text <- function(x, what) {
  if (!is.character(x) || length(x) != 1 || is.na(x) || !nzchar(x)) {
    pd_abort(what, " must be one piece of text")
  }
  tolower(x)
}

catalogue_match <- function(publisher, topic, jurisdiction) {
  entries <- pd_get("/catalog.json", simplify = FALSE)$dataset
  text <- function(x) if (is.null(x)) "" else tolower(as.character(x))
  keep <- rep(TRUE, length(entries))
  if (!is.null(publisher)) {
    p <- one_text(publisher, "publisher")
    keep <- keep & vapply(entries, function(e) grepl(p, text(e$publisher$name), fixed = TRUE), logical(1))
  }
  if (!is.null(topic)) {
    t <- one_text(topic, "topic")
    topics <- lapply(entries, function(e) tolower(as.character(unlist(e[["publicdata:topics"]]))))
    known <- sort(unique(unlist(topics)))
    if (!length(known)) pd_abort("the site's catalogue does not list topics yet")
    if (!t %in% known) {
      pd_abort("no datasets are filed under topic \"", topic, "\"; the topics are ",
           paste(known, collapse = ", "))
    }
    keep <- keep & vapply(topics, function(x) t %in% x, logical(1))
  }
  if (!is.null(jurisdiction)) {
    j <- one_text(jurisdiction, "jurisdiction")
    if (j %in% c("commonwealth", "australian government", "federal")) j <- "cth"
    keep <- keep & vapply(entries, function(e) {
      j %in% c(text(e[["publicdata:jurisdiction"]]), text(e$spatial))
    }, logical(1))
  }
  vapply(entries[keep], function(e) e$identifier, character(1))
}

#' A dataset's description and files
#'
#' @param slug The dataset's slug, as in its page URL `https://publicdata.au/d/<slug>/`.
#' @return The dataset's Frictionless data package as a list: title, licence,
#'   attribution, fields and every file of the newest version.
#' @family find
#' @examplesIf pd_available()
#' pd_dataset("au-road-deaths")$title
#' @export
pd_dataset <- function(slug) {
  pd_get(paste0("/d/", check_slug(slug), "/datapackage.json"))
}

#' Every version of a dataset
#'
#' @inheritParams pd_dataset
#' @return A tibble with one row per version, newest first: its date,
#'   as-at date, rows, fields and the SHA-256 of the publisher's file.
#' @family find
#' @examplesIf pd_available()
#' pd_versions("au-road-deaths")
#' @export
pd_versions <- function(slug) {
  as_tbl(pd_get(paste0("/d/", check_slug(slug), "/versions.json"))$versions)
}

query_path <- function(slug, kind, version) {
  base <- paste0("/api/v1/datasets/", check_slug(slug), "/")
  if (!is.null(check_version(version))) base <- paste0(base, "versions/", version, "/")
  paste0(base, kind)
}

#' Rows of a dataset
#'
#' Reads rows from the query API. For a whole table, [pd_read()] or
#' [pd_download()] is faster and has no rate limit.
#'
#' @inheritParams pd_dataset
#' @param ... Conditions, each a field name and a value or a filter such as
#'   `year = pd_gte(2020)`. A vector of several values matches any of them and
#'   `NA` matches a blank or suppressed cell. Every condition must match.
#' @param .select Fields to return. Every field when `NULL`.
#' @param .order Fields to sort by, each `"field.asc"` or `"field.desc"`.
#' @param .limit Rows per page, 1 to 10000.
#' @param .offset Rows to skip.
#' @param .version A version date from [pd_versions()]. Without it the answer
#'   comes from the newest version and changes when the publisher releases
#'   again; with it the answer never changes.
#' @param .all Follow every page and return all matching rows.
#' @return A tibble, with each column typed as its field says (dates as `Date`,
#'   booleans as logical) and labelled with the field's description.
#'   `attr(x, "publicdata")` holds the version, licence and attribution the
#'   answer came with; see [pd_attribution()].
#' @family query
#' @examplesIf pd_available()
#' pd_rows("au-road-deaths", state = "QLD", year = pd_gte(2024), .limit = 5)
#' @export
pd_rows <- function(slug, ..., .select = NULL, .order = NULL, .limit = NULL,
                    .offset = NULL, .version = NULL, .all = FALSE) {
  if (isTRUE(.all) && is.null(.limit)) .limit <- 10000
  query <- c(
    conditions(list(...)),
    list(select = joined(.select), order = joined(.order), limit = .limit, offset = .offset)
  )
  body <- pd_get(query_path(slug, "rows", .version), query)
  meta <- body$publicdata
  nxt <- body[["next"]]
  pages <- list(raw_rows(body))
  while (isTRUE(.all) && !is.null(nxt)) {
    body <- pd_get(nxt)
    pages[[length(pages) + 1]] <- raw_rows(body)
    nxt <- body[["next"]]
  }
  rows <- if (length(pages) > 1) do.call(rbind, pages) else pages[[1]]
  licence_notice(slug, meta$licence)
  answer(rows, slug, meta, nxt, .version)
}

#' Totals of a dataset by group
#'
#' @inheritParams pd_rows
#' @param group Fields to group by.
#' @param metric `"count"`, `"sum.<field>"`, `"avg.<field>"`, `"min.<field>"`
#'   or `"max.<field>"`, or several of them.
#' @param ... Conditions, as for [pd_rows()].
#' @return A tibble with one row per group, typed and carrying the same
#'   `"publicdata"` attribute as [pd_rows()].
#' @family query
#' @examplesIf pd_available()
#' pd_aggregate("au-road-deaths", group = "state", year = 2025)
#' @export
pd_aggregate <- function(slug, group = NULL, metric = "count", ..., .version = NULL) {
  query <- c(conditions(list(...)), list(group = joined(group), metric = joined(metric)))
  body <- pd_get(query_path(slug, "aggregate", .version), query)
  licence_notice(slug, body$publicdata$licence)
  answer(raw_rows(body), slug, body$publicdata, version = .version)
}

#' The attribution an answer carries
#'
#' @param x A data frame from [pd_rows()], [pd_aggregate()], [pd_read()] or
#'   [pd_sf()], or a connection from [pd_connect()].
#' @return The attribution string the publisher's licence requires, or `NULL`.
#' @family metadata
#' @examplesIf pd_available()
#' pd_attribution(pd_rows("au-road-deaths", .limit = 1))
#' @export
pd_attribution <- function(x) attr(x, "publicdata")$attribution

file_url <- function(slug, format, version, table = NULL) {
  format <- match.arg(format, formats)
  at <- if (is.null(check_version(version))) "latest" else paste0("v/", version)
  if (!is.null(check_table(table))) {
    if (format != "parquet") pd_abort("a table of a database is served as parquet")
    return(paste0(pd_site(), "/d/", check_slug(slug), "/", at, "/tables/", table, ".parquet"))
  }
  paste0(pd_site(), "/d/", check_slug(slug), "/", at, "/data.", format)
}

save_file <- function(slug, format, version, path, table = NULL) {
  req <- pd_request(file_url(slug, format, version, table))
  if (interactive() && !isTRUE(getOption("publicdataau.quiet"))) req <- httr2::req_progress(req)
  resp <- pd_perform(req, path = path)
  got <- regmatches(resp$url, regexpr("(?<=/v/)[0-9]{4}-[0-9]{2}-[0-9]{2}", resp$url, perl = TRUE))
  list(path = path, version = if (length(got)) got else NA_character_)
}

#' Download one version's file
#'
#' @inheritParams pd_dataset
#' @param format One of `"parquet"`, `"csv"`, `"csv.gz"`, `"json"`,
#'   `"ndjson"`, `"sqlite"`, `"duckdb"`, `"xlsx"`, `"arrow"`, `"geojson"`,
#'   `"gpkg"` or `"geo.parquet"`. The last three are served only for
#'   datasets with a location or a shape. Parquet, CSV, CSV (gzip), NDJSON and
#'   DuckDB are on every version. Excel, JSON, GeoJSON and SQLite are left out
#'   of a version whose table is over their size limits, and Arrow is only on
#'   versions made before October 2026.
#' @param version A version date from [pd_versions()]. The newest when `NULL`.
#' @param path Where to save the file. When `NULL`, a file in the session's
#'   temporary directory named for the slug and version, or the file in the
#'   cache when `cache` is on.
#' @param table For a database, one of its tables, which is served as Parquet.
#'   See [pd_tables()].
#' @param cache Keep the file in [pd_cache_dir()] and reuse it next time.
#'   Versions never change, so a kept file never goes stale. Off unless
#'   `options(publicdataau.cache = TRUE)` is set.
#' @return The path of the saved file, invisibly, with the version it resolved
#'   to in `attr(path, "version")`.
#' @family files
#' @examplesIf pd_available()
#' f <- pd_download("qld-road-crash-factors", "csv")
#' @export
pd_download <- function(slug, format = "parquet", version = NULL, path = NULL, table = NULL,
                        cache = NULL) {
  format <- match.arg(format, formats)
  if (use_cache(cache)) {
    got <- fetch_file(slug, format, version, table, cache = TRUE)
    if (!is.null(path)) {
      if (!file.copy(got$path, path, overwrite = TRUE)) pd_abort("could not write ", path)
      got$path <- path
    }
  } else {
    if (is.null(path)) {
      stem <- paste0(check_slug(slug), "-", if (is.null(version)) "latest" else version)
      if (!is.null(check_table(table))) stem <- paste0(stem, "-", table)
      path <- file.path(tempdir(), paste0(stem, ".", format))
    }
    got <- save_file(slug, format, version, path, table)
  }
  licence_notice(slug)
  out <- got$path
  attr(out, "version") <- got$version
  invisible(out)
}

#' Read a whole table
#'
#' Downloads one version's Parquet file, or one table of a database, and reads
#' it with 'arrow'. Without 'arrow' built with zstd, a table is read from the
#' version's gzipped CSV instead and typed from its fields.
#'
#' @inheritParams pd_download
#' @param columns Fields to read, which saves memory on a wide table. Every
#'   field when `NULL`.
#' @return A tibble, each column labelled with its field's description.
#'   `attr(x, "publicdata")` is the provenance header the file itself carries:
#'   version, licence, attribution, citation and source.
#' @family files
#' @examplesIf pd_available()
#' crashes <- pd_read("qld-road-crash-factors")
#' @export
pd_read <- function(slug, version = NULL, table = NULL, cache = NULL, columns = NULL) {
  if (!is.null(columns) && (!is.character(columns) || !length(columns))) {
    pd_abort("columns must be field names, as pd_fields() lists them")
  }
  if (!reads_parquet()) {
    if (!is.null(table)) {
      pd_abort(
        "a table of a database is served only as Parquet, which pd_read() reads with 'arrow' ",
        "built with zstd; reinstall it after Sys.setenv(ARROW_WITH_ZSTD = \"ON\"), or use pd_tbl()",
        class = "publicdataau_no_zstd"
      )
    }
    return(read_csv_gz(slug, version, cache, columns))
  }
  got <- fetch_file(slug, "parquet", version, table, cache)
  if (got$temp) on.exit(unlink(got$path), add = TRUE)
  tbl <- if (is.null(columns)) {
    arrow::read_parquet(got$path, as_data_frame = FALSE)
  } else {
    arrow::read_parquet(got$path, col_select = !!columns, as_data_frame = FALSE)
  }
  header <- tbl$metadata$publicdata
  df <- as_tbl(labelled(as.data.frame(tbl), field_meta(slug, table, version)))
  attr(df, "publicdata") <- if (is.null(header)) list() else jsonlite::fromJSON(header, simplifyVector = FALSE)
  licence_notice(slug, attr(df, "publicdata")$licence)
  df
}

has_zstd <- function() arrow::codec_is_available("zstd")

reads_parquet <- function() requireNamespace("arrow", quietly = TRUE) && has_zstd()

csv_classes <- c(string = "character", integer = "numeric", number = "numeric",
                 boolean = "logical", date = "character", datetime = "character")

# A table from the version's gzipped CSV, for a session that cannot read the Parquet file.
read_csv_gz <- function(slug, version, cache, columns) {
  rlang::inform(
    "Reading the gzipped CSV, since 'arrow' with zstd is not installed.",
    .frequency = "once", .frequency_id = "publicdataau_csv_gz"
  )
  f <- field_meta(slug, NULL, version)
  if (!is.null(columns) && !is.null(f)) {
    unknown <- setdiff(columns, f$name)
    if (length(unknown)) pd_abort("unknown fields: ", paste(unknown, collapse = ", "))
  }
  got <- fetch_file(slug, "csv.gz", version, NULL, cache)
  if (got$temp) on.exit(unlink(got$path), add = TRUE)
  classes <- if (is.null(f)) NA else stats::setNames(unname(csv_classes[f$type]), f$name)
  df <- utils::read.csv(gzfile(got$path), colClasses = classes, na.strings = "",
                        check.names = FALSE, stringsAsFactors = FALSE, encoding = "UTF-8")
  if (!is.null(columns)) df <- df[, columns, drop = FALSE]
  df <- as_tbl(labelled(typed(df, f), f))
  at <- if (is.null(got$version) || is.na(got$version)) version else got$version
  attr(df, "publicdata") <- file_header(slug, at)
  licence_notice(slug, attr(df, "publicdata")$licence)
  df
}

# The provenance header every file of a version carries, from the first line of its NDJSON, read
# with a range request so the rest is not downloaded.
file_header <- function(slug, version) {
  req <- httr2::req_headers(pd_request(file_url(slug, "ndjson", version)), Range = "bytes=0-65535")
  tryCatch({
    body <- rawToChar(httr2::resp_body_raw(pd_perform(req)))
    Encoding(body) <- "UTF-8"
    h <- jsonlite::fromJSON(strsplit(body, "\n", fixed = TRUE)[[1]][1], simplifyVector = FALSE)$publicdata
    if (is.null(h)) list() else h
  }, error = function(e) list())
}

need <- function(pkg, fn) {
  if (!requireNamespace(pkg, quietly = TRUE)) {
    pd_abort(fn, " needs the '", pkg, "' package; install.packages(\"", pkg, "\")")
  }
}

#' A version's schema
#'
#' @inheritParams pd_download
#' @return The version's `schema.json` as a list: the fields of a table, or for
#'   a database every table with its fields, keys and references, and the views.
#'   [pd_fields()] gives the fields as a tibble.
#' @family metadata
#' @examplesIf pd_available()
#' names(pd_schema("qld-road-crash-factors"))
#' @export
pd_schema <- function(slug, version = NULL) {
  at <- if (is.null(check_version(version))) "latest" else paste0("v/", version)
  pd_get(paste0("/d/", check_slug(slug), "/", at, "/schema.json"))
}

#' The tables of a dataset
#'
#' A database such as G-NAF is served as one DuckDB file holding several
#' tables, with one Parquet file per table beside it. A dataset that is one
#' table has one entry, `records`.
#'
#' @inheritParams pd_download
#' @return A tibble with one row per table: its name, description, rows,
#'   number of fields and key.
#' @family metadata
#' @examplesIf pd_available()
#' pd_tables("qld-road-crash-factors")
#' @export
pd_tables <- function(slug, version = NULL) {
  at <- if (is.null(check_version(version))) "latest" else paste0("v/", version)
  req <- pd_request(paste0(pd_site(), "/d/", check_slug(slug), "/", at, "/schema.json"))
  s <- httr2::resp_body_json(pd_perform(req), simplifyVector = FALSE)
  one <- function(t) {
    data.frame(
      name = t$name,
      description = if (is.null(t$description)) "" else t$description,
      rows = if (is.null(t$rows)) NA_real_ else as.numeric(t$rows),
      fields = length(t$fields),
      key = paste(unlist(t$primaryKey), collapse = ", "),
      stringsAsFactors = FALSE
    )
  }
  if (identical(s$kind, "database")) return(as_tbl(do.call(rbind, lapply(s$tables, one))))
  as_tbl(one(list(name = "records", fields = s$fields, primaryKey = s$primaryKey)))
}

has_shared_home <- function() "shared_home" %in% names(formals(duckdb::duckdb))

#' Connect to a version with DuckDB
#'
#' Attaches a version's DuckDB file read-only over HTTPS and makes it the
#' current database, so every table and view can be queried by name with
#' 'DBI' or 'dplyr'. Only the blocks a query touches are read, so a count over
#' millions of rows runs without a download. For a database such as G-NAF the
#' file holds every table, the keys between them and the publisher's views;
#' for a single table it holds `records`.
#'
#' @inheritParams pd_download
#' @param name The name the file is attached as. The slug with hyphens as
#'   underscores when `NULL`.
#' @param cache Download the whole file into [pd_cache_dir()] once and
#'   attach it from there, which makes repeated scans of a large database
#'   much faster. Off unless `options(publicdataau.cache = TRUE)` is set.
#' @return A 'DBI' connection. `attr(con, "publicdata")` names the dataset,
#'   the version, the file's URL and local path if cached, and carries the
#'   file's provenance: licence, attribution and citation. Disconnect with
#'   `DBI::dbDisconnect(con)`.
#' @param shared_home Where 'DuckDB' keeps the 'httpfs' extension it
#'   downloads to read over HTTPS. `FALSE`, the default, keeps it in the
#'   session's temporary directory, so it is downloaded again in a new
#'   session. `TRUE` keeps it under `~/.duckdb` for every session.
#' @param ... Passed to [duckdb::duckdb()].
#' @family query
#' @examplesIf interactive() && pd_available() && requireNamespace("duckdb", quietly = TRUE)
#' con <- pd_connect("au-road-deaths")
#' DBI::dbGetQuery(con, "SELECT state, count(*) AS n FROM records GROUP BY 1")
#' DBI::dbDisconnect(con)
#' @export
pd_connect <- function(slug, version = NULL, name = NULL, shared_home = FALSE, cache = NULL, ...) {
  need("DBI", "pd_connect()")
  need("duckdb", "pd_connect()")
  if (is.null(check_version(version))) version <- latest_version(slug)
  url <- file_url(slug, "duckdb", version)
  path <- NULL
  if (use_cache(cache)) path <- fetch_file(slug, "duckdb", version, cache = TRUE)$path
  if (is.null(name)) name <- gsub("-", "_", check_slug(slug), fixed = TRUE)
  if (!grepl("^[A-Za-z_][A-Za-z0-9_]*$", name)) pd_abort("not a database name: ", name)
  args <- list(...)
  has_home <- has_shared_home()
  if (has_home) args$shared_home <- shared_home
  con <- DBI::dbConnect(do.call(duckdb::duckdb, args))
  if (!has_home && !shared_home) {
    # 'duckdb' before 1.5.5 has no shared_home and would install under ~/.duckdb.
    dir <- gsub("\\", "/", file.path(tempdir(), "duckdb"), fixed = TRUE)
    DBI::dbExecute(con, sprintf("SET extension_directory = '%s'", gsub("'", "''", dir)))
  }
  src <- if (is.null(path)) url else path
  if (grepl("^https?://", src)) DBI::dbExecute(con, "INSTALL httpfs; LOAD httpfs")
  DBI::dbExecute(con, sprintf("ATTACH '%s' AS \"%s\" (READ_ONLY)", gsub("'", "''", src), name))
  DBI::dbExecute(con, sprintf("USE \"%s\"", name))
  header <- tryCatch(
    parse_header(DBI::dbGetQuery(con, "SELECT key, value FROM publicdata")),
    error = function(e) list()
  )
  attr(con, "publicdata") <- c(
    list(dataset = slug, version = version, url = url, path = path, name = name),
    header[setdiff(names(header), c("dataset", "version", "url", "name"))]
  )
  licence_notice(slug, header$licence)
  con
}

# A file's publicdata table: one row per key, with any value that is not text held as JSON.
parse_header <- function(kv) {
  out <- lapply(kv$value, function(v) {
    if (is.na(v) || !grepl("^[[{]", v)) return(v)
    tryCatch(jsonlite::fromJSON(v, simplifyVector = FALSE), error = function(e) v)
  })
  stats::setNames(out, kv$key)
}
