#' A table to query with dplyr
#'
#' Attaches a version's DuckDB file with [pd_connect()] and returns one of its
#' tables or views as a lazy 'dplyr' table. Verbs such as `filter()`,
#' `group_by()` and `summarise()` are translated to SQL and run by 'DuckDB'
#' over HTTPS, reading only the blocks they need; `collect()` brings the
#' result into R. Calls for the same dataset and version share one
#' connection.
#'
#' @inheritParams pd_connect
#' @param table The table or view to query. A dataset that is one table has
#'   one, `records`, which is the default; for a database such as G-NAF, name
#'   one of [pd_tables()] or a view such as `"address_view"`.
#' @return A lazy table from 'dbplyr'. Its connection is
#'   `dbplyr::remote_con(x)`, which carries the same `"publicdata"` attribute
#'   as [pd_connect()].
#' @family query
#' @examplesIf interactive() && pd_available() && rlang::is_installed(c("dbplyr", "duckdb"))
#' library(dplyr)
#' pd_tbl("au-road-deaths") |>
#'   filter(year >= 2020) |>
#'   count(state) |>
#'   collect()
#' @export
pd_tbl <- function(slug, table = NULL, version = NULL, cache = NULL) {
  need("dplyr", "pd_tbl()")
  need("dbplyr", "pd_tbl()")
  if (is.null(check_version(version))) version <- latest_version(slug)
  key <- paste(check_slug(slug), version, use_cache(cache))
  con <- state$cons[[key]]
  if (is.null(con) || !DBI::dbIsValid(con)) {
    con <- pd_connect(slug, version, cache = cache)
    state$cons[[key]] <- con
  }
  tables <- DBI::dbGetQuery(con, paste(
    "SELECT table_name AS name FROM information_schema.tables",
    "WHERE table_catalog = current_database()"
  ))$name
  if (!is.null(table) && is.character(table) && grepl("^[0-9]{4}-[0-9]{2}-[0-9]{2}$", table)) {
    pd_abort("\"", table, "\" is a version; pass it as version = \"", table, "\"")
  }
  if (is.null(table)) {
    if (!"records" %in% tables) {
      pd_abort("'", slug, "' has several tables; name one of ", toString(sort(tables)))
    }
    table <- "records"
  }
  if (!check_table(table) %in% tables) {
    pd_abort(
      "'", slug, "' has no table or view '", table, "'; its tables are ",
      toString(sort(tables))
    )
  }
  dplyr::tbl(con, table)
}

.onUnload <- function(libpath) {
  for (con in state$cons) try(DBI::dbDisconnect(con), silent = TRUE)
}
