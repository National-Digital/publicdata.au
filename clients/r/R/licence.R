state <- new.env(parent = emptyenv())
state$shown <- character()
state$conditions <- list()
state$cons <- list()
state$memo <- list()

# The condition a dataset's licence sets beyond attribution, such as G-NAF's rule on mail
# compilation, or "" when it sets none. Read once per session from the data package when the
# answer in hand does not carry the licence.
licence_condition <- function(slug, licence = NULL) {
  if (is.list(licence)) {
    return(licence$condition %||% "")
  }
  if (!is.null(state$conditions[[slug]])) {
    return(state$conditions[[slug]])
  }
  dp <- tryCatch(
    pd_get(paste0("/d/", slug, "/datapackage.json"), simplify = FALSE),
    error = function(e) NULL
  )
  if (is.null(dp)) {
    return("")
  }
  cond <- unlist(lapply(dp$licenses, function(l) l[["publicdata:condition"]]))
  state$conditions[[slug]] <- if (length(cond)) paste(cond, collapse = " ") else ""
  state$conditions[[slug]]
}

# Says once per session, per dataset, what its licence's condition is, as the dataset's page does.
licence_notice <- function(slug, licence = NULL) {
  if (isTRUE(getOption("publicdataau.quiet")) || slug %in% state$shown) {
    return(invisible())
  }
  cond <- licence_condition(slug, licence)
  if (!nzchar(cond)) {
    return(invisible())
  }
  state$shown <- c(state$shown, slug)
  msg <- paste0(
    "The licence of '", slug, "' sets a condition on its use: ", cond,
    "\nThis is shown once per session. options(publicdataau.quiet = TRUE) turns it off."
  )
  message(structure(
    class = c("pd_licence_condition", "message", "condition"),
    list(message = paste0(msg, "\n"), call = NULL)
  ))
  invisible()
}
