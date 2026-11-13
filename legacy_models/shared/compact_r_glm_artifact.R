#!/usr/bin/env Rscript
# Repackage an existing, SHA-verified R glm.fit artifact WITHOUT retraining.
# Saves only inference coefficients and preprocessing data. Preserves old
# artifact and manifest backups under ignored datasets/reports/.
#
# Usage: Rscript legacy_models/shared/compact_r_glm_artifact.R \
#          --package legacy_models/fraud_logistic_r

get_arg <- function(args, flag) {
  i <- match(flag, args)
  if (is.na(i) || i >= length(args) || !nzchar(args[i + 1L])) {
    stop("Missing required argument: ", flag)
  }
  args[i + 1L]
}

validate_model <- function(m) {
  # Historical RDS releases did not always include split_sha256 (or other
  # optional provenance fields). Scoring only requires these four components.
  required <- c("fit", "features", "center", "scale")
  if (!is.list(m)) stop("Invalid GLM artifact: expected an R list")
  missing <- setdiff(required, names(m))
  if (length(missing)) {
    stop("Invalid GLM artifact: missing required inference fields: ",
         paste(missing, collapse = ", "),
         "; available: ", paste(names(m), collapse = ", "))
  }
  if (!is.list(m$fit) || !is.numeric(m$fit$coefficients)) {
    stop("Invalid GLM artifact: fit$coefficients must be a numeric vector")
  }
  if (!is.character(m$features) || !length(m$features) ||
      anyNA(m$features) || anyDuplicated(m$features)) {
    stop("Invalid GLM artifact: features must be distinct character names")
  }
  k <- length(m$features)
  if (length(m$fit$coefficients) != k + 1L ||
      any(!is.finite(m$fit$coefficients))) {
    stop("Invalid GLM artifact: coefficient length/values (expected ",
         k + 1L, ", got ", length(m$fit$coefficients), ")")
  }
  if (!is.numeric(m$center) || !is.numeric(m$scale) ||
      length(m$center) != k || length(m$scale) != k ||
      any(!is.finite(m$center)) || any(!is.finite(m$scale)) ||
      any(m$scale == 0)) {
    stop("Invalid GLM artifact: center/scale must be finite nonzero numeric ",
         "vectors of length ", k)
  }
  invisible(TRUE)
}

compact_model <- function(m) {
  validate_model(m)
  # Explicit whitelist: no fitted.values, residuals, working arrays, weights,
  # QR decomposition, linear predictors or copied source/training rows.
  compact <- list(
    fit = list(coefficients = m$fit$coefficients),
    features = m$features,
    center = m$center,
    scale = m$scale
  )
  # Copy only provenance fields that genuinely existed in the old artifact.
  # Do NOT fabricate missing fit-time split attestation fields.
  optional <- intersect(c("domain", "checksum", "split_sha256", "version"),
                        names(m))
  compact[optional] <- m[optional]
  validate_model(compact)
  compact
}

predict_fixture <- function(m, inputs) {
  validate_model(m)
  if (anyDuplicated(names(inputs)) ||
      !all(c("row_id", m$features) %in% names(inputs)) ||
      !nrow(inputs)) {
    stop("Invalid development scoring fixture")
  }
  X <- sapply(inputs[, m$features, drop = FALSE],
              function(x) suppressWarnings(as.numeric(x)))
  if (!is.matrix(X)) X <- matrix(X, ncol = length(m$features))
  if (!all(is.finite(X))) stop("Non-finite development fixture features")
  z <- sweep(sweep(X, 2, m$center, "-"), 2, m$scale, "/")
  p <- as.numeric(plogis(cbind(1, z) %*% m$fit$coefficients))
  if (!all(is.finite(p)) || any(p < 0 | p > 1)) {
    stop("Invalid predicted probabilities")
  }
  p
}

main <- function() {
  args <- commandArgs(trailingOnly = TRUE)
  if (length(args) != 2L || args[1L] != "--package") {
    stop("Usage: Rscript legacy_models/shared/compact_r_glm_artifact.R --package PACKAGE_DIR")
  }
  package <- normalizePath(get_arg(args, "--package"), mustWork = TRUE)
  artifact <- file.path(package, "model.rds")
  manifest_path <- file.path(package, "manifest.json")
  inputs_path <- file.path(package, "fixtures", "development_inputs.csv")
  refs_path <- file.path(package, "fixtures", "reference_predictions.csv")
  for (path in c(artifact, manifest_path, inputs_path, refs_path)) {
    if (!file.exists(path)) stop("Required file missing: ", path)
  }
  for (name in c("digest", "jsonlite")) {
    if (!requireNamespace(name, quietly = TRUE)) {
      stop("Missing R package: ", name)
    }
  }

  manifest <- jsonlite::fromJSON(manifest_path, simplifyVector = TRUE)
  if (!identical(manifest$language, "R") ||
      !identical(manifest$artifact_paths, "model.rds")) {
    stop("Only an existing R GLM model.rds is supported")
  }
  old_sha <- digest::digest(file = artifact, algo = "sha256")
  if (!identical(manifest$artifact_sha256, old_sha)) {
    stop("Existing artifact SHA-256 differs from manifest; refusing to repackage")
  }
  old <- readRDS(artifact) # Trusted, checksum-verified model only.
  cat("Historical RDS fields:", paste(names(old), collapse = ", "), "\n")
  if (is.list(old) && is.list(old$fit)) {
    cat("Historical fit fields:", paste(names(old$fit), collapse = ", "), "\n")
  }
  validate_model(old)
  if (identical(names(old$fit), "coefficients")) {
    cat("Already compact, preserving artifact and manifest:", package, "\n")
    return(invisible(NULL))
  }

  inputs <- read.csv(inputs_path, check.names = FALSE,
                     colClasses = "character", fileEncoding = "UTF-8")
  reference <- read.csv(refs_path, check.names = FALSE,
                        colClasses = "character", fileEncoding = "UTF-8")
  if (!identical(names(reference), c("row_id", "positive_probability")) ||
      !identical(inputs$row_id, reference$row_id) || nrow(inputs) > 100L) {
    stop("Development fixture/reference rows do not align")
  }
  old_prediction <- predict_fixture(old, inputs)
  expected <- suppressWarnings(as.numeric(reference$positive_probability))
  if (any(!is.finite(expected)) ||
      max(abs(old_prediction - expected)) > 1e-10) {
    stop("Original saved artifact does not reproduce the frozen reference predictions")
  }

  compact <- compact_model(old)
  compact_prediction <- predict_fixture(compact, inputs)
  max_difference <- max(abs(old_prediction - compact_prediction))
  if (!is.finite(max_difference) || max_difference > 1e-12) {
    stop("Compact artifact changes development predictions")
  }

  staged_artifact <- tempfile("compact_model_", tmpdir = package, fileext = ".rds")
  staged_manifest <- tempfile("compact_manifest_", tmpdir = package, fileext = ".json")
  on.exit(unlink(c(staged_artifact, staged_manifest)), add = TRUE)
  saveRDS(compact, staged_artifact)
  reloaded <- readRDS(staged_artifact)
  if (!identical(names(reloaded$fit), "coefficients") ||
      max(abs(predict_fixture(reloaded, inputs) - old_prediction)) > 1e-12) {
    stop("Serialised compact artifact failed scoring parity")
  }
  old_bytes <- unname(file.info(artifact)$size)
  new_bytes <- unname(file.info(staged_artifact)$size)
  if (!is.finite(new_bytes) || new_bytes >= old_bytes) {
    stop("Compaction did not reduce the artifact size")
  }
  new_sha <- digest::digest(file = staged_artifact, algo = "sha256")

  manifest$artifact_sha256 <- new_sha
  manifest$artifact_compaction <- list(
    format = "glm_coefficients_only_v1",
    original_artifact_sha256 = old_sha,
    evidence = "unchanged fitted coefficients and preprocessing; 100-row development prediction replay",
    development_rows_checked = nrow(inputs),
    max_absolute_prediction_difference = max_difference,
    retrained = FALSE
  )
  jsonlite::write_json(manifest, staged_manifest, auto_unbox = TRUE,
                       pretty = TRUE, null = "null", digits = 17)

  # Keep original evidence safely OUTSIDE the Git worktree (datasets/ is ignored).
  invocation <- grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)
  if (length(invocation) != 1L) stop("Cannot determine script path for backup root")
  script_path <- sub("^--file=", "", invocation)
  repo_root <- normalizePath(file.path(dirname(script_path), "..", ".."),
                             mustWork = TRUE)
  backup_dir <- file.path(repo_root, "datasets", "reports", "r_glm_compaction_backups")
  dir.create(backup_dir, recursive = TRUE, showWarnings = FALSE)
  name <- basename(package)
  backup_artifact <- file.path(backup_dir, paste0(name, "_", old_sha, ".rds"))
  backup_manifest <- file.path(backup_dir, paste0(name, "_", old_sha, ".manifest.json"))
  if (file.exists(backup_artifact) &&
      digest::digest(file = backup_artifact, algo = "sha256") != old_sha) {
    stop("Existing backup has an unexpected checksum; refusing overwrite")
  }
  if (!file.exists(backup_artifact) &&
      !file.copy(artifact, backup_artifact, overwrite = FALSE)) {
    stop("Failed to save original artifact backup")
  }
  if (!file.exists(backup_manifest) &&
      !file.copy(manifest_path, backup_manifest, overwrite = FALSE)) {
    stop("Failed to save original manifest backup")
  }

  completed <- FALSE
  on.exit({
    if (!completed) {
      file.copy(backup_artifact, artifact, overwrite = TRUE)
      file.copy(backup_manifest, manifest_path, overwrite = TRUE)
      warning("Compaction did not complete; restored original saved artifact and manifest")
    }
  }, add = TRUE)
  if (!file.copy(staged_artifact, artifact, overwrite = TRUE) ||
      !file.copy(staged_manifest, manifest_path, overwrite = TRUE) ||
      digest::digest(file = artifact, algo = "sha256") != new_sha ||
      !identical(jsonlite::fromJSON(manifest_path)$artifact_sha256, new_sha)) {
    stop("Failed to install the compact artifact and matching manifest")
  }
  completed <- TRUE
  cat(jsonlite::toJSON(list(
    package = name,
    original_bytes = old_bytes,
    compact_bytes = new_bytes,
    original_sha256 = old_sha,
    compact_sha256 = new_sha,
    development_rows_checked = nrow(inputs),
    max_absolute_prediction_difference = max_difference,
    backup_directory = backup_dir
  ), pretty = TRUE, auto_unbox = TRUE), "\n")
  cat("NOTE: prior hash-pinned reconciliation/verification reports must be regenerated or explicitly reconciled; do not claim historic fit-time evidence was rewritten.\n")
}

if (sys.nframe() == 0L) {
  tryCatch(main(), error = function(e) {
    message("R GLM COMPACTION FAILED: ", conditionMessage(e))
    quit(save = "no", status = 1L)
  })
}
