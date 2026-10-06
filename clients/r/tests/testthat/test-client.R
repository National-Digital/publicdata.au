meta <- list(version = "2026-08-07", attribution = "Publisher, CC BY 4.0.", licence = list(id = "CC-BY-4.0"))

json <- function(x, status = 200L, url = "https://publicdata.test/", headers = list()) {
  httr2::response(
    status_code = status,
    url = url,
    headers = c(list(`Content-Type` = "application/json"), headers),
    body = charToRaw(jsonlite::toJSON(x, auto_unbox = TRUE, null = "null"))
  )
}

seen <- new.env()

# A driver that keeps duckdb's own files out of the home directory, as CRAN requires.
drv <- function(dbdir) {
  if ("shared_home" %in% names(formals(duckdb::duckdb))) duckdb::duckdb(dbdir, shared_home = FALSE) else duckdb::duckdb(dbdir)
}

fake <- function(req) {
  seen$urls <- c(seen$urls, req$url)
  u <- httr2::url_parse(req$url)
  if (u$path == "/api/v1/datasets") return(json(list(results = data.frame(slug = c("a", "b"), title = c("A", "B")))))
  if (u$path %in% c("/api/v1/datasets/a/rows", "/api/v1/datasets/a/versions/2026-08-07/rows")) {
    off <- as.integer(if (is.null(u$query$offset)) 0 else u$query$offset)
    lim <- as.integer(if (is.null(u$query$limit)) 100 else u$query$limit)
    n <- seq(off, min(off + lim, 5) - 1)
    nxt <- if (off + lim < 5) paste0("https://publicdata.test/api/v1/datasets/a/rows?limit=", lim, "&offset=", off + lim) else NULL
    return(json(list(publicdata = meta, rows = data.frame(n = n), `next` = nxt)))
  }
  if (u$path == "/api/v1/datasets/a/aggregate") {
    return(json(list(publicdata = meta, rows = data.frame(g = "x", count = 2L), `next` = NULL)))
  }
  if (u$path == "/api/v1/datasets/nope/rows") return(json(list(error = "No such dataset in the query API"), 404L))
  if (u$path == "/d/a/versions.json") return(json(list(latest = "2026-08-07", versions = data.frame(version = "2026-08-07", rows = 5L))))
  if (u$path == "/d/db/latest/schema.json") {
    return(json(list(kind = "database", tables = list(
      list(name = "thing", description = "Things.", rows = 3L, fields = list(list(name = "id")), primaryKey = list("id")),
      list(name = "kind_aut", description = "", rows = 2L, fields = list(list(name = "code"), list(name = "name")), primaryKey = list("code"))
    ), views = list())))
  }
  if (u$path == "/d/a/latest/schema.json") {
    return(json(list(fields = list(list(name = "n", type = "integer")), primaryKey = list("n"))))
  }
  if (u$path == "/catalog.json") {
    return(json(list(dataset = list(
      list(identifier = "a", publisher = list(name = "Bureau of Things"), spatial = "Queensland",
           `publicdata:jurisdiction` = "Qld", `publicdata:topics` = list("roads")),
      list(identifier = "b", publisher = list(name = "Other"), spatial = "Commonwealth",
           `publicdata:jurisdiction` = "Cth", `publicdata:topics` = list("crime"))
    ))))
  }
  if (u$path == "/d/a/changes.json") {
    return(json(list(dataset = "a", changes = list(list(
      from = "2026-08-01", to = "2026-08-07", rows_from = 4L, rows_to = 5L, added = 1L, removed = 0L,
      changed = 0L, unchanged = 4L, schema = list(fields_added = list("m"), fields_removed = list()),
      truncated = FALSE, url = "https://publicdata.test/d/a/diff/2026-08-01..2026-08-07.json"
    )))))
  }
  if (u$path == "/d/a/diff/2026-08-01..2026-08-07.json") {
    return(json(list(dataset = "a", from = "2026-08-01", to = "2026-08-07", rows_from = 4L, rows_to = 5L,
                     added = 1L, removed = 0L, changed = 0L, unchanged = 4L,
                     schema = list(fields_added = list("m")), added_keys = list(9L))))
  }
  if (u$path == "/d/a/datapackage.json") {
    return(json(list(title = "A things", version = "2026-08-07",
                     licenses = list(list(name = "CC-BY-4.0", title = "CC BY 4.0")),
                     contributors = list(list(title = "Bureau of Things", role = "publisher")),
                     `publicdata:attribution` = "Bureau of Things, CC BY 4.0.")))
  }
  if (u$path == "/d/a/v/2026-08-01/manifest.json") return(json(list(fetched_at = "2026-08-01T03:00:00+00:00")))
  if (u$path == "/d/c/datapackage.json") {
    return(json(list(licenses = list(list(name = "X", `publicdata:condition` = "No mail lists.")))))
  }
  if (u$path == "/api/v1/datasets/c/aggregate") {
    m <- meta
    m$licence <- list(id = "X", condition = "No mail lists.")
    return(json(list(publicdata = m, rows = data.frame(g = "x", count = 1L), `next` = NULL)))
  }
  if (u$path == "/d/a/fields.json") {
    return(json(list(slug = "a", fields = list(
      list(name = "n", type = "integer", description = "A count.", min = 0L, max = 4L),
      list(name = "day", type = "date", min = "2026-01-01", max = "2026-08-07"),
      list(name = "flag", type = "boolean"),
      list(name = "g", type = "string", values = list("x", "y")),
      list(name = "lga_2025_code", type = "string", description = "Code of the Local Government Area (2025).")
    ))))
  }
  if (u$path == "/d/a/v/2026-08-07/manifest.json" || u$path == "/d/a/latest/manifest.json") {
    return(json(list(dataset = "a", version = "2026-08-07", filename = "a.csv", bytes = 1200L, sha256 = "abc",
                     fetched_at = "2026-08-07T01:00:00+00:00", source = list(url = "https://gov.example/a.csv"),
                     licence = list(title = "CC BY 4.0", read_at = "2026-08-07T00:59:00+00:00"), rows = 5L,
                     url = "https://publicdata.test/d/a/v/2026-08-07/")))
  }
  if (u$path == "/api/v1/datasets/t/rows") {
    return(json(list(publicdata = meta, rows = data.frame(n = c(1, 2), day = c("2026-01-02", NA), flag = c(1L, 0L),
                                                           g = c("x", "y"), extra = c("e", "f")), `next` = NULL)))
  }
  if (u$path == "/api/v1/datasets/t/versions/2026-01-01/rows") {
    return(json(list(publicdata = meta, rows = data.frame(n = 1, day = "2026-01-02"), `next` = NULL)))
  }
  if (u$path == "/d/t/v/2026-01-01/schema.json") {
    return(json(list(fields = list(list(name = "n", type = "number", description = "An old measure."),
                                   list(name = "day", type = "string")))))
  }
  if (u$path == "/d/t/fields.json") {
    return(json(list(fields = list(list(name = "n", type = "integer", description = "A count."),
                                   list(name = "day", type = "date"), list(name = "flag", type = "boolean"),
                                   list(name = "g", type = "string")))))
  }
  if (u$path == "/api/v1/catalogue") {
    return(json(list(total = 2L, next_offset = NULL, rows = list(
      list(id = "qld-1", title = "Water", jur = "qld", state = "votable", url = "https://data.qld.gov.au/x"),
      list(id = "qld-2", title = "Rain", jur = "qld", state = "served", page = "https://publicdata.au/d/rain/")
    ))))
  }
  if (u$path == "/places.json") {
    return(json(list(layers = list(
      list(key = "postcode", slug = "abs-postal-areas-2021", title = "Postal Area (2021)", code = "poa_2021_code",
           name = "poa_2021_name", noun = "postcode", version = "2021-06-24"),
      list(key = "lga", slug = "abs-lga-2025", title = "Local Government Area (2025)", code = "lga_2025_code",
           name = "lga_2025_name", noun = "council area", version = "2026-05-14")
    ))))
  }
  json(list(error = "not here"), 404L)
}

local_fake <- function(env = parent.frame()) {
  seen$urls <- character()
  withr::local_options(publicdataau.site = "https://publicdata.test", .local_envir = env)
  httr2::local_mocked_responses(fake, env = env)
}

query_of <- function(url) httr2::url_parse(url)$query

test_that("filters render in the API's operator form", {
  expect_equal(pd_gte(2020)$expr, "gte.2020")
  expect_equal(pd_in("QLD", "NSW")$expr, "in.(QLD,NSW)")
  expect_equal(pd_in(c("a", "b"))$expr, "in.(a,b)")
  expect_equal(pd_not(pd_eq("x"))$expr, "not.eq.x")
  expect_equal(pd_is_null()$expr, "is.null")
  expect_equal(pd_ilike("*rider*")$expr, "ilike.*rider*")
  expect_equal(pd_eq(TRUE)$expr, "eq.true")
  expect_equal(pd_eq(100000)$expr, "eq.100000")
  expect_error(pd_in("a,b"), "comma")
  expect_error(pd_eq(NA), "pd_is_null")
  expect_error(pd_gt(NA_real_), "pd_is_null")
  expect_error(pd_in("a", NA), "pd_is_null")
  expect_error(pd_eq(c(1, 2)), "pd_in")
  expect_equal(pd_eq(FALSE)$expr, "eq.false")
})

test_that("rows sends conditions, select, order and limit", {
  local_fake()
  r <- pd_rows("a", state = "QLD", year = pd_gte(2020), lga = NA, sex = c("F", "M"),
               .select = c("n", "m"), .order = "n.desc", .limit = 2)
  q <- query_of(tail(grep("/rows", seen$urls, value = TRUE), 1))
  expect_equal(q[c("state", "year", "lga", "sex", "select", "order", "limit")],
               list(state = "eq.QLD", year = "gte.2020", lga = "is.null", sex = "in.(F,M)",
                    select = "n,m", order = "n.desc", limit = "2"))
  expect_equal(r$n, c(0L, 1L), ignore_attr = TRUE)
  expect_false(is.null(attr(r, "next")))
})

test_that("an answer carries its version and attribution", {
  local_fake()
  r <- pd_rows("a")
  expect_equal(attr(r, "publicdata")$version, "2026-08-07")
  expect_equal(pd_attribution(r), "Publisher, CC BY 4.0.")
})

test_that(".all follows every page and keeps the provenance", {
  local_fake()
  r <- pd_rows("a", .all = TRUE, .limit = 2)
  expect_equal(r$n, 0:4, ignore_attr = TRUE)
  expect_null(attr(r, "next"))
  expect_equal(pd_attribution(r), "Publisher, CC BY 4.0.")
  expect_length(grep("/rows", seen$urls), 3)
})

test_that("a dated version goes on the path and a bad one is refused", {
  local_fake()
  pd_rows("a", .version = "2026-08-07")
  expect_match(tail(grep("/rows", seen$urls, value = TRUE), 1), "/api/v1/datasets/a/versions/2026-08-07/rows")
  expect_error(pd_rows("a", .version = "latest"), "date")
})

test_that("aggregate sends group, metric and conditions", {
  local_fake()
  a <- pd_aggregate("a", group = c("g", "h"), metric = c("count", "sum.n"), x = 1)
  expect_equal(query_of(tail(seen$urls, 1))[c("x", "group", "metric")],
               list(x = "eq.1", group = "g,h", metric = "count,sum.n"))
  expect_equal(a$count, 2L)
})

test_that("an error names the status and the API's message", {
  local_fake()
  expect_error(pd_rows("nope"), "No such dataset in the query API")
})

test_that("a bad slug or an unnamed condition never reaches the network", {
  local_fake()
  expect_error(pd_rows("../etc"), "slug")
  expect_error(pd_rows("a", "QLD"), "field name")
  expect_length(seen$urls, 0)
})

test_that("datasets and versions", {
  local_fake()
  expect_equal(pd_datasets("crash")$slug, c("a", "b"))
  expect_equal(query_of(tail(seen$urls, 1))$q, "crash")
  expect_equal(pd_versions("a")$version, "2026-08-07")
})

test_that("a 429 or a passing server error is retried and nothing else is", {
  req <- publicdataau:::pd_request("https://publicdata.test/")
  expect_equal(req$policies$retry_max_tries, 4)
  transient <- req$policies$retry_is_transient
  expect_true(transient(json(list(), 429L)))
  for (s in c(500L, 502L, 503L, 504L)) expect_true(transient(json(list(), s)))
  expect_false(transient(json(list(), 404L)))
  expect_false(transient(json(list(), 400L)))
  expect_equal(req$options[c("connecttimeout", "low_speed_time", "low_speed_limit")],
               list(connecttimeout = 30, low_speed_time = 60, low_speed_limit = 1))
})

test_that("an unknown format is refused", {
  expect_error(pd_download("a", "docx"))
})

test_that("the live site answers", {
  skip_on_cran()
  skip_if_offline("publicdata.au")
  skip_if(Sys.getenv("PUBLICDATA_LIVE") == "", "set PUBLICDATA_LIVE=1 to query the live site")
  expect_true("au-road-deaths" %in% pd_datasets()$slug)
  r <- pd_rows("au-road-deaths", state = "QLD", .limit = 1)
  expect_equal(nrow(r), 1)
  expect_match(pd_attribution(r), "CC BY")
  f <- pd_download("qld-road-crash-factors", "csv")
  expect_match(attr(f, "version"), "^[0-9]{4}-")
  expect_gt(file.size(f), 0)
})

test_that("pd_read takes provenance from the file it read", {
  skip_if_not_installed("arrow")
  header <- list(version = "2026-08-07", attribution = "Publisher, CC BY 4.0.", licence = list(id = "CC-BY-4.0"),
                 not_endorsed = "The publisher has not endorsed this site.")
  tbl <- arrow::arrow_table(n = 1:3)
  tbl$metadata$publicdata <- as.character(jsonlite::toJSON(header, auto_unbox = TRUE))
  src <- tempfile(fileext = ".parquet")
  arrow::write_parquet(tbl, src)
  local_mocked_bindings(has_zstd = function() TRUE)
  local_mocked_bindings(save_file = function(slug, format, version, path, table = NULL) {
    file.copy(src, path, overwrite = TRUE)
    list(path = path, version = "2026-08-07")
  })
  df <- pd_read("a", "2026-08-07")
  expect_equal(df$n, 1:3, ignore_attr = TRUE)
  expect_equal(pd_attribution(df), "Publisher, CC BY 4.0.")
  expect_equal(attr(df, "publicdata")$version, "2026-08-07")
})

test_that("the tables of a database and of a table", {
  local_fake()
  t <- pd_tables("db")
  expect_equal(t$name, c("thing", "kind_aut"))
  expect_equal(t$rows, c(3, 2))
  expect_equal(t$fields, c(1L, 2L))
  expect_equal(t$key, c("id", "code"))
  expect_equal(pd_tables("a")$name, "records")
})

test_that("a table of a database is served as parquet under tables/", {
  expect_equal(publicdataau:::file_url("db", "parquet", "2026-08-07", "thing"),
               paste0(pd_site(), "/d/db/v/2026-08-07/tables/thing.parquet"))
  expect_error(publicdataau:::file_url("db", "csv", NULL, "thing"), "parquet")
  expect_error(publicdataau:::file_url("db", "parquet", NULL, "../x"), "table")
})

test_that("pd_connect attaches the newest version read-only and names it", {
  skip_if_not_installed("DBI")
  skip_if_not_installed("duckdb")
  local_fake()
  src <- tempfile(fileext = ".duckdb")
  w <- DBI::dbConnect(drv(src))
  DBI::dbExecute(w, "CREATE TABLE records AS SELECT 1 AS n UNION ALL SELECT 2")
  DBI::dbExecute(w, "CREATE TABLE publicdata (key VARCHAR, value VARCHAR)")
  DBI::dbExecute(w, "INSERT INTO publicdata VALUES ('licence', '{\"id\": \"CC-BY-4.0\"}')")
  DBI::dbDisconnect(w)
  local_mocked_bindings(file_url = function(slug, format, version, table = NULL) src)
  con <- pd_connect("a")
  on.exit(DBI::dbDisconnect(con), add = TRUE)
  expect_equal(DBI::dbGetQuery(con, "SELECT count(*) AS n FROM records")$n, 2)
  p <- attr(con, "publicdata")
  expect_equal(p$version, "2026-08-07")
  expect_equal(p$name, "a")
  expect_equal(p$licence$id, "CC-BY-4.0")
  expect_match(tail(seen$urls, 1), "/d/a/versions.json")
  expect_error(DBI::dbExecute(con, "INSERT INTO records VALUES (3)"))
})

test_that("pd_connect keeps extensions out of the home directory on an older duckdb", {
  skip_if_not_installed("DBI")
  skip_if_not_installed("duckdb")
  local_fake()
  src <- tempfile(fileext = ".duckdb")
  w <- DBI::dbConnect(drv(src))
  DBI::dbExecute(w, "CREATE TABLE records AS SELECT 1 AS n")
  DBI::dbDisconnect(w)
  local_mocked_bindings(file_url = function(slug, format, version, table = NULL) src, has_shared_home = function() FALSE)
  con <- pd_connect("a")
  on.exit(DBI::dbDisconnect(con), add = TRUE)
  dir <- DBI::dbGetQuery(con, "SELECT current_setting('extension_directory') AS d")$d
  expect_equal(normalizePath(dir, "/", FALSE), normalizePath(file.path(tempdir(), "duckdb"), "/", FALSE))
})

test_that("an unreachable site fails with a message that names it, and pd_available() says no", {
  withr::local_options(publicdataau.site = "https://publicdata.invalid")
  expect_error(pd_versions("a"), "could not reach https://publicdata.invalid")
  expect_false(pd_available())
})

test_that("datasets filter by publisher, topic and jurisdiction", {
  local_fake()
  expect_equal(pd_datasets(publisher = "bureau")$slug, "a")
  expect_equal(pd_datasets(topic = "crime")$slug, "b")
  expect_equal(pd_datasets(jurisdiction = "queensland")$slug, "a")
  expect_equal(pd_datasets(jurisdiction = "Commonwealth")$slug, "b")
  expect_equal(nrow(pd_datasets(topic = "roads", jurisdiction = "cth")), 0)
  expect_error(pd_datasets(topic = "nope"), "crime, roads")
  expect_error(pd_datasets(publisher = c("a", "b")), "one piece of text")
  httr2::local_mocked_responses(function(req) {
    if (grepl("catalog.json", req$url, fixed = TRUE)) return(json(list(dataset = list(list(identifier = "a")))))
    fake(req)
  })
  expect_error(pd_datasets(topic = "roads"), "does not list topics yet")
})

test_that("changes are listed and one comparison is read in full", {
  local_fake()
  ch <- pd_changes("a")
  expect_equal(ch$added, 1)
  expect_equal(ch$fields_added, "m")
  expect_equal(nrow(pd_changes("a", from = "2026-08-02")), 0)
  d <- pd_diff("a")
  expect_s3_class(d, "pd_diff")
  expect_equal(unlist(d$added_keys), 9L)
  expect_output(print(d), "1 added, 0 removed")
  expect_error(pd_diff("a", "2026-08-01"), "first version")
})

test_that("a citation names the publisher, version and URL, as text or BibTeX", {
  local_fake()
  cit <- pd_cite("a")
  expect_s3_class(cit, "bibentry")
  bib <- paste(utils::toBibtex(cit), collapse = "\n")
  expect_match(bib, "@Misc{a-2026-08-07,", fixed = TRUE)
  expect_match(bib, "author = {{Bureau of Things}}", fixed = TRUE)
  expect_match(bib, "https://publicdata.test/d/a/v/2026-08-07/", fixed = TRUE)
  flat <- function(x) gsub("\\s+", " ", format(x, style = "text"))
  expect_match(flat(cit), "Bureau of Things, CC BY 4.0", fixed = TRUE)
  expect_match(flat(pd_cite("a", "2026-08-01")), "read from the publisher on 2026-08-01")
})

test_that("the cache keeps a version, reuses it and clears it", {
  local_fake()
  withr::local_options(publicdataau.cache_dir = file.path(withr::local_tempdir(), "cache"))
  calls <- 0
  local_mocked_bindings(save_file = function(slug, format, version, path, table = NULL) {
    calls <<- calls + 1
    writeLines("x", path)
    list(path = path, version = version)
  })
  a <- pd_download("a", "csv", cache = TRUE)
  b <- pd_download("a", "csv", cache = TRUE)
  expect_equal(calls, 1)
  expect_equal(as.character(a), as.character(b))
  expect_equal(attr(a, "version"), "2026-08-07")
  kept <- pd_cache_list()
  expect_equal(kept$dataset, "a")
  expect_equal(kept$version, "2026-08-07")
  expect_equal(kept$file, "data.csv")
  out <- tempfile(fileext = ".csv")
  pd_download("a", "csv", path = out, cache = TRUE)
  expect_equal(readLines(out), "x")
  expect_equal(calls, 1)
  expect_gt(pd_cache_clear("a"), 0)
  expect_equal(nrow(pd_cache_list()), 0)
  expect_error(pd_cache_clear(version = "2026-08-07"), "slug")
})

test_that("nothing is kept unless asked", {
  local_fake()
  dir <- file.path(withr::local_tempdir(), "cache")
  withr::local_options(publicdataau.cache_dir = dir)
  local_mocked_bindings(save_file = function(slug, format, version, path, table = NULL) {
    writeLines("x", path)
    list(path = path, version = "2026-08-07")
  })
  pd_download("a", "csv")
  expect_false(dir.exists(dir))
})

test_that("a licence condition is shown once per session", {
  local_fake()
  state$shown <- character()
  expect_message(pd_aggregate("c"), "No mail lists", class = "pd_licence_condition")
  expect_no_message(pd_aggregate("c"))
  state$shown <- character()
  withr::local_options(publicdataau.quiet = TRUE)
  expect_no_message(pd_aggregate("c"))
})

test_that("pd_tbl queries a table lazily and checks its name", {
  skip_if_not_installed("dbplyr")
  skip_if_not_installed("duckdb")
  local_fake()
  src <- tempfile(fileext = ".duckdb")
  w <- DBI::dbConnect(drv(src))
  DBI::dbExecute(w, "CREATE TABLE records AS SELECT range AS n FROM range(10)")
  DBI::dbDisconnect(w)
  local_mocked_bindings(file_url = function(slug, format, version, table = NULL) src)
  x <- pd_tbl("a")
  expect_s3_class(x, "tbl_lazy")
  n <- dplyr::collect(dplyr::count(dplyr::filter(x, n >= 5)))$n
  expect_equal(as.numeric(n), 5)
  expect_identical(dbplyr::remote_con(pd_tbl("a")), dbplyr::remote_con(x))
  expect_error(pd_tbl("a", "nope"), "no table or view 'nope'")
  expect_error(pd_tbl("a", "2026-08-07"), "version = ")
  DBI::dbDisconnect(dbplyr::remote_con(x))
})

test_that("pd_sf reads the map layer and its provenance, and says when there is none", {
  skip_if_not_installed("sf")
  local_fake()
  src <- tempfile(fileext = ".gpkg")
  pts <- sf::st_sf(id = 1:2, geometry = sf::st_sfc(sf::st_point(c(153, -28)), sf::st_point(c(151, -33)), crs = 7844))
  sf::st_write(pts, src, layer = "records", quiet = TRUE)
  sf::st_write(data.frame(key = c("version", "licence"), value = c("2026-08-07", "{\"id\": \"CC-BY-4.0\"}")),
               src, layer = "publicdata", quiet = TRUE, append = TRUE)
  local_mocked_bindings(save_file = function(slug, format, version, path, table = NULL) {
    if (slug == "b") stop(structure(class = c("httr2_http_404", "httr2_http", "httr2_error", "error", "condition"),
                                    list(message = "404", call = NULL)))
    file.copy(src, path, overwrite = TRUE)
    list(path = path, version = "2026-08-07")
  })
  x <- pd_sf("a")
  expect_s3_class(x, "sf")
  expect_equal(sf::st_crs(x)$epsg, 7844L)
  expect_equal(attr(x, "publicdata")$licence$id, "CC-BY-4.0")
  expect_error(pd_sf("b"), "has no map layer")
})

test_that("pd_fields lists a dataset's fields with ranges and values", {
  local_fake()
  f <- pd_fields("a")
  expect_s3_class(f, "tbl_df")
  expect_equal(f$name, c("n", "day", "flag", "g", "lga_2025_code"))
  expect_equal(f$max[1], "4")
  expect_equal(f$values[[4]], c("x", "y"))
  db <- pd_fields("db")
  expect_equal(db$table, c("thing", "kind_aut", "kind_aut"))
})

test_that("rows come back typed and labelled from the dataset's fields", {
  local_fake()
  state$memo <- list()
  r <- pd_rows("t")
  expect_s3_class(r, "tbl_df")
  expect_type(r$n, "integer")
  expect_s3_class(r$day, "Date")
  expect_true(is.na(r$day[2]))
  expect_type(r$flag, "logical")
  expect_equal(r$flag, c(TRUE, FALSE))
  expect_equal(r$extra, c("e", "f"))
  expect_equal(attr(r$n, "label"), "A count.")
  expect_equal(pd_attribution(r), "Publisher, CC BY 4.0.")
})

test_that("a column that does not parse as its type is left as it came", {
  f <- tibble::tibble(name = c("d", "t"), type = c("date", "datetime"), description = NA_character_)
  df <- data.frame(d = c("2026-01-01", "soon"), t = c("2026-01-01T10:00:00Z", "2026-01-01 11:30:00"))
  out <- publicdataau:::typed(df, f)
  expect_equal(out$d, df$d)
  expect_s3_class(out$t, "POSIXct")
  expect_equal(format(out$t, "%H:%M", tz = "UTC"), c("10:00", "11:30"))
})

test_that("pd_url, pd_latest, pd_provenance and pd_browse", {
  local_fake()
  expect_equal(pd_url("a", "csv", "2026-08-07"), "https://publicdata.test/d/a/v/2026-08-07/data.csv")
  expect_equal(pd_url("a"), "https://publicdata.test/d/a/latest/data.parquet")
  expect_equal(pd_latest("a"), "2026-08-07")
  p <- pd_provenance("a", "2026-08-07")
  expect_s3_class(p, "pd_provenance")
  expect_equal(p$sha256, "abc")
  expect_output(print(p), "https://gov.example/a.csv")
  expect_equal(pd_browse("a"), "https://publicdata.test/d/a/")
  expect_equal(pd_browse("a", "2026-08-07"), "https://publicdata.test/d/a/v/2026-08-07/")
})

test_that("pd_catalogue searches the portals and maps the jurisdiction", {
  local_fake()
  out <- pd_catalogue("water", jurisdiction = "Queensland", status = c("votable", "served"), limit = 5)
  expect_equal(out$title, c("Water", "Rain"))
  expect_equal(out$jurisdiction, c("qld", "qld"))
  expect_equal(out$page, c(NA, "https://publicdata.au/d/rain/"))
  expect_equal(attr(out, "total"), 2L)
  q <- query_of(tail(seen$urls, 1))
  expect_equal(q$jur, "qld")
  expect_equal(q$state, "votable,served")
  expect_error(pd_catalogue(jurisdiction = "Narnia"), "jurisdiction is one of")
})

test_that("errors carry the package's classes", {
  withr::local_options(publicdataau.site = "https://publicdata.invalid")
  expect_error(pd_versions("a"), class = "publicdataau_unreachable")
  expect_error(pd_versions("a"), class = "publicdataau_error")
  expect_error(pd_rows("bad slug"), class = "publicdataau_error")
})

test_that("boundary codes are compared as text with leading zeros restored", {
  expect_equal(publicdataau:::norm_codes(c(800, 4220, NA), c("0800", "4220")), c("0800", "4220", NA))
  expect_equal(publicdataau:::norm_codes(factor("16490"), "16490"), "16490")
  expect_equal(publicdataau:::norm_codes(c(1, 22), c("1", "22")), c("1", "22"))
})

test_that("pd_join_boundaries finds the layer from the column and joins in order", {
  skip_if_not_installed("sf")
  local_fake()
  state$memo <- list()
  poly <- function(x) sf::st_multipolygon(list(list(rbind(c(x, 0), c(x + 1, 0), c(x + 1, 1), c(x, 0)))))
  b <- sf::st_sf(lga_2025_code = c("1", "2"), lga_2025_name = c("One", "Two"),
                 geometry = sf::st_sfc(poly(0), poly(5), crs = 7844))
  local_mocked_bindings(pd_sf = function(slug, version = NULL, cache = NULL) {
    expect_equal(slug, "abs-lga-2025")
    attr(b, "publicdata") <- list(attribution = "ABS")
    b
  })
  x <- tibble::tibble(lga_2025_code = c("2", "9", "1"), n = 1:3)
  expect_message(m <- pd_join_boundaries(x), "1 row\\(s\\) matched no council area boundary")
  expect_s3_class(m, "sf")
  expect_equal(m$n, 1:3)
  expect_equal(m$lga_2025_name, c("Two", NA, "One"))
  expect_equal(as.logical(sf::st_is_empty(m)), c(FALSE, TRUE, FALSE))
  expect_equal(sf::st_crs(m)$epsg, 7844L)
  expect_equal(attr(m, "boundaries")$attribution, "ABS")
  y <- data.frame(council = c(1, 2))
  expect_equal(pd_join_boundaries(y, layer = "lga", by = "council")$lga_2025_name, c("One", "Two"))
  expect_error(pd_join_boundaries(data.frame(z = 1)), "no column named for a boundary code")
  expect_error(pd_join_boundaries(x, layer = "nowhere"), "no boundary layer")
  expect_error(pd_join_boundaries(m), "already has a geometry")
})

test_that("pd_read reads the gzipped CSV when arrow cannot read the Parquet file", {
  local_fake()
  state$memo <- list()
  src <- tempfile(fileext = ".csv.gz")
  con <- gzfile(src, "w")
  writeLines(c("n,day,flag,g,lga_2025_code,suppressed", "1,2026-01-02,true,x,01234,", "2,,false,,05678,g;day"), con)
  close(con)
  local_mocked_bindings(
    reads_parquet = function() FALSE,
    file_header = function(slug, version) list(version = version, attribution = "Publisher, CC BY 4.0."),
    save_file = function(slug, format, version, path, table = NULL) {
      expect_equal(format, "csv.gz")
      file.copy(src, path, overwrite = TRUE)
      list(path = path, version = "2026-08-07")
    }
  )
  df <- pd_read("a")
  expect_type(df$n, "integer")
  expect_s3_class(df$day, "Date")
  expect_true(is.na(df$day[2]))
  expect_equal(df$flag, c(TRUE, FALSE))
  expect_true(is.na(df$g[2]))
  expect_equal(df$lga_2025_code, c("01234", "05678"), ignore_attr = TRUE)
  expect_equal(attr(df$n, "label"), "A count.")
  expect_equal(pd_attribution(df), "Publisher, CC BY 4.0.")
  expect_equal(attr(df, "publicdata")$version, "2026-08-07")
  expect_equal(df$suppressed, list(character(), c("g", "day")))
  expect_equal(names(pd_read("a", columns = c("g", "n"))), c("g", "n"))
  expect_error(pd_read("a", columns = "nope"), "unknown fields")
  expect_error(pd_read("db", table = "thing"), class = "publicdataau_no_zstd")
})

test_that("whole numbers past 32 bits keep every digit in the CSV fallback", {
  skip_if_not_installed("bit64")
  x <- publicdataau:::as_int64(c("4611686018427387905", NA))
  expect_s3_class(x, "integer64")
  expect_equal(as.character(x[1]), "4611686018427387905")
  expect_type(publicdataau:::as_int64(c("1", NA)), "integer")
})

test_that("a format a version leaves out says why", {
  local_fake()
  expect_error(pd_download("a", "xlsx", "2026-08-07", path = tempfile()),
               "size limits", class = "publicdataau_not_offered")
  expect_error(pd_download("a", "geo.parquet", "2026-08-07", path = tempfile()),
               "location or a shape", class = "publicdataau_not_offered")
  expect_error(pd_download("a", "arrow", "2026-08-07", path = tempfile()),
               "no caps field", class = "publicdataau_not_offered")
  expect_error(pd_download("a", "csv.gz", "2026-08-07", path = tempfile()), class = "httr2_http_404")
})

test_that("pd_read reads only the columns asked for", {
  skip_if_not_installed("arrow")
  local_fake()
  src <- tempfile(fileext = ".parquet")
  arrow::write_parquet(arrow::arrow_table(n = 1:3, m = letters[1:3]), src)
  local_mocked_bindings(has_zstd = function() TRUE)
  local_mocked_bindings(save_file = function(slug, format, version, path, table = NULL) {
    file.copy(src, path, overwrite = TRUE)
    list(path = path, version = "2026-08-07")
  })
  df <- pd_read("a", columns = "n")
  expect_equal(names(df), "n")
  expect_equal(attr(df$n, "label"), "A count.")
  expect_error(pd_read("a", columns = 1), "field names")
})

test_that("a pinned version is typed and labelled from that version's own fields", {
  local_fake()
  state$memo <- list()
  r <- pd_rows("t", .version = "2026-01-01")
  expect_type(r$day, "character")
  expect_type(r$n, "double")
  expect_equal(attr(r$n, "label"), "An old measure.")
  expect_true(any(grepl("/d/t/v/2026-01-01/schema.json", seen$urls, fixed = TRUE)))
})
