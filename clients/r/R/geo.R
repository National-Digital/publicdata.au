#' Read a dataset's map layer as an sf object
#'
#' Downloads one version's GeoPackage and reads it with 'sf'. Datasets with a
#' location, such as crash points, or a shape, such as ABS boundaries, have
#' one; their coordinates are in the reference system the publisher used,
#' which the GeoPackage names, usually GDA2020 (EPSG:7844).
#'
#' @inheritParams pd_download
#' @return An 'sf' tibble with a row per feature. `attr(x, "publicdata")`
#'   is the provenance the file carries: version, licence, attribution,
#'   citation and source.
#' @family maps
#' @examplesIf pd_available() && requireNamespace("sf", quietly = TRUE)
#' storages <- pd_sf("au-water-storages")
#' sf::st_crs(storages)$epsg
#' @export
pd_sf <- function(slug, version = NULL, cache = NULL) {
  need("sf", "pd_sf()")
  got <- tryCatch(
    fetch_file(slug, "gpkg", version, cache = cache),
    httr2_http_404 = function(e) {
      pd_abort("'", slug, "' has no map layer: only datasets with a location or a shape have one. ",
        "pd_datasets() lists what is served; a map layer shows as a GeoPackage file on the dataset's page.",
        class = "publicdataau_no_layer"
      )
    }
  )
  if (got$temp) on.exit(unlink(got$path), add = TRUE)
  x <- sf::st_read(got$path, layer = "records", quiet = TRUE, as_tibble = TRUE)
  kv <- tryCatch(
    suppressWarnings(sf::st_read(got$path, layer = "publicdata", quiet = TRUE)),
    error = function(e) NULL
  )
  attr(x, "publicdata") <- if (is.data.frame(kv)) parse_header(kv) else list()
  licence_notice(slug, attr(x, "publicdata")$licence)
  x
}
