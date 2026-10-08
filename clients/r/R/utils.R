# Every error the package raises has the class publicdataau_error, plus a narrower class where a
# caller may want to handle it: publicdataau_unreachable, publicdataau_no_layer.
pd_abort <- function(..., class = NULL) {
  rlang::abort(paste0(...), class = c(class, "publicdataau_error"), call = NULL)
}

as_tbl <- function(x) {
  if (is.null(x) || (is.list(x) && !is.data.frame(x) && !length(x))) {
    return(tibble::tibble())
  }
  tibble::as_tibble(as.data.frame(x, stringsAsFactors = FALSE))
}

# Metadata that does not change within a session, such as a dataset's fields, read once.
memo <- function(key, value) {
  if (!is.null(state$memo[[key]])) {
    return(state$memo[[key]])
  }
  out <- value
  state$memo[[key]] <- out
  out
}
