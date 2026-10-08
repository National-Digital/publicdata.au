#' Boundary layers
#'
#' The ABS boundaries publicdata.au serves, each keyed by a code that rows of
#' other datasets carry: council areas (LGA), SA2s, suburbs, postal areas and
#' state and federal electorates.
#'
#' @return A tibble with one row per layer: its `key` (such as `"lga"`), `slug`,
#'   `title`, the `code` and `name` fields that identify an area, a `noun` for
#'   one area, and the `version` served.
#' @family maps
#' @examplesIf pd_available()
#' pd_boundary_layers()
#' @export
pd_boundary_layers <- function() {
  memo("places", {
    p <- pd_get("/places.json", simplify = FALSE)$layers
    text <- function(k) vapply(p, function(l) as.character(l[[k]]), character(1))
    tibble::tibble(
      key = text("key"), slug = text("slug"), title = text("title"), code = text("code"),
      name = text("name"), noun = text("noun"), version = text("version")
    )
  })
}

find_layer <- function(layer) {
  layers <- pd_boundary_layers()
  l <- one_text(layer, "layer")
  hit <- which(tolower(layers$key) == l | tolower(layers$slug) == l | tolower(layers$code) == l)
  if (!length(hit)) pd_abort("no boundary layer \"", layer, "\"; the layers are ", paste(layers$key, collapse = ", "))
  layers[hit[1], ]
}

#' A boundary layer as an sf object
#'
#' @param layer A layer's key, such as `"lga"`, `"sa2"`, `"suburb"`,
#'   `"postcode"`, `"state_electorate"` or `"federal_electorate"`, or its slug or
#'   code field. [pd_boundary_layers()] lists them.
#' @inheritParams pd_download
#' @return An 'sf' tibble with a row per area: its code, name and the other
#'   fields of the layer, in GDA2020 (EPSG:7844).
#' @family maps
#' @examplesIf interactive() && pd_available() && requireNamespace("sf", quietly = TRUE)
#' # A boundary file is tens of megabytes, so this runs only when asked.
#' electorates <- pd_boundaries("federal_electorate")
#' @export
pd_boundaries <- function(layer, cache = NULL) {
  pd_sf(find_layer(layer)$slug, cache = cache)
}

#' Join rows to their boundaries
#'
#' Adds the boundary of the area each row names, by its ABS code, and returns
#' an 'sf' object ready to map. Rows from [pd_rows()] or [pd_aggregate()] carry
#' the codes of the areas their point falls in, such as `lga_2025_code`, and
#' the layer is found from the column's name; any data frame with a column of
#' codes works with `layer` and `by`. Aggregate first: a map of 500 council
#' areas is quicker to draw than 80,000 rows each carrying its area's shape.
#'
#' @param x A data frame.
#' @param layer The layer to join to, as for [pd_boundaries()]. Found from the
#'   column names of `x` when `NULL`.
#' @param by The column of `x` that holds the codes. The layer's code field,
#'   such as `"lga_2025_code"`, when `NULL`. Numeric codes are compared as text,
#'   with leading zeros restored, so postcode 800 matches `"0800"`.
#' @inheritParams pd_download
#' @return An 'sf' tibble with the rows of `x` in their order, the area's name
#'   when `x` lacks it, and the area's geometry. A row whose code matches no
#'   area has an empty geometry, and a message says how many.
#' @family maps
#' @examplesIf interactive() && pd_available() && requireNamespace("sf", quietly = TRUE)
#' # Downloads the SA2 boundaries, tens of megabytes, so this runs only when asked.
#' by_sa2 <- pd_aggregate("act-road-crashes", group = "sa2_2021_code")
#' map <- pd_join_boundaries(by_sa2)
#' plot(map["count"])
#' @export
pd_join_boundaries <- function(x, layer = NULL, by = NULL, cache = NULL) {
  need("sf", "pd_join_boundaries()")
  if (!is.data.frame(x)) pd_abort("x must be a data frame")
  if (inherits(x, "sf")) pd_abort("x already has a geometry; drop it with sf::st_drop_geometry() first")
  if (is.null(layer)) {
    layers <- pd_boundary_layers()
    hits <- layers[layers$code %in% names(x), ]
    if (!nrow(hits)) {
      pd_abort(
        "x has no column named for a boundary code (", paste(layers$code, collapse = ", "),
        "); name the layer and the column, as in layer = \"lga\", by = \"council_code\""
      )
    }
    if (nrow(hits) > 1) {
      pd_abort("x has codes for several layers (", paste(hits$key, collapse = ", "), "); choose one with layer =")
    }
    l <- hits[1, ]
  } else {
    l <- find_layer(layer)
  }
  col <- if (is.null(by)) l$code else one_text(by, "by")
  col <- names(x)[tolower(names(x)) == col][1]
  if (is.na(col)) pd_abort("x has no column \"", if (is.null(by)) l$code else by, "\"")
  b <- pd_boundaries(l$key, cache = cache)
  ref <- as.character(b[[l$code]])
  codes <- norm_codes(x[[col]], ref)
  idx <- match(codes, ref)
  add_name <- l$name %in% names(b) && !l$name %in% names(x)
  geom <- sf::st_geometry(b)
  empty <- sf::st_sfc(lapply(seq_len(sum(is.na(idx))), function(i) sf::st_multipolygon()), crs = sf::st_crs(b))
  g <- geom[ifelse(is.na(idx), 1L, idx)]
  if (any(is.na(idx))) g[is.na(idx)] <- empty
  out <- tibble::as_tibble(x)
  out[[col]] <- codes
  if (add_name) out[[l$name]] <- b[[l$name]][idx]
  out <- sf::st_sf(out, geometry = g)
  attr(out, "publicdata") <- attr(x, "publicdata")
  attr(out, "boundaries") <- attr(b, "publicdata")
  missed <- sum(is.na(idx) & !is.na(codes))
  if (missed) message(format(missed, big.mark = ","), " row(s) matched no ", l$noun, " boundary.")
  out
}

# Codes as text, with the leading zeros a number lost restored when every code has one width.
norm_codes <- function(v, ref) {
  if (is.factor(v)) v <- as.character(v)
  if (is.numeric(v)) {
    widths <- unique(nchar(ref[!is.na(ref)]))
    whole <- all(is.na(v) | v == round(v))
    fmt <- if (length(widths) == 1 && whole) paste0("%0", widths, ".0f") else if (whole) "%.0f" else "%.15g"
    v <- ifelse(is.na(v), NA_character_, sprintf(fmt, v))
  }
  trimws(as.character(v))
}
