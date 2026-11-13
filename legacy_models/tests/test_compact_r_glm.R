#!/usr/bin/env Rscript
# R-only regression checks for compact representation (no public datasets needed).
# Run from repository root: Rscript legacy_models/tests/test_compact_r_glm.R

source("legacy_models/shared/compact_r_glm_artifact.R", local = TRUE)
set.seed(20261104)
n <- 6000L
x1 <- rnorm(n)
x2 <- rnorm(n)
X <- cbind("(Intercept)" = 1, x1 = x1, x2 = x2)
probability <- plogis(-0.7 + 0.6 * x1 - 0.25 * x2)
y <- rbinom(n, 1, probability)
fit <- glm.fit(x = X, y = y, family = binomial(),
               control = glm.control(maxit = 150, epsilon = 1e-9))
stopifnot(isTRUE(fit$converged), length(fit$fitted.values) == n)
model <- list(
  fit = fit,
  features = c("x1", "x2"),
  center = c(0, 0),
  scale = c(1, 1),
  domain = "test",
  checksum = paste(rep("0", 64), collapse = ""),
  split_sha256 = paste(rep("1", 64), collapse = ""),
  version = "1.0.0"
)
fixture <- data.frame(row_id = sprintf("row-%05d", seq_len(100L)),
                      x1 = x1[seq_len(100L)], x2 = x2[seq_len(100L)])
compact <- compact_model(model)
# A real legacy release may predate fit-time split SHA recording. Packaging
# must preserve this absence instead of inventing attestation metadata.
legacy_without_sha <- model
legacy_without_sha$split_sha256 <- NULL
compact_legacy <- compact_model(legacy_without_sha)
stopifnot(
  !("split_sha256" %in% names(compact_legacy)),
  identical(predict_fixture(compact_legacy, fixture),
            predict_fixture(legacy_without_sha, fixture))
)
# Errors must reveal the invalid field rather than returning a generic message.
malformed <- model
malformed$center <- numeric(0)
reason <- tryCatch({validate_model(malformed); ""}, error = conditionMessage)
stopifnot(grepl("center/scale", reason, fixed = TRUE))
stopifnot(
  identical(names(compact$fit), "coefficients"),
  identical(compact$fit$coefficients, fit$coefficients),
  is.null(compact$fit$fitted.values),
  is.null(compact$fit$residuals),
  is.null(compact$fit$qr),
  identical(predict_fixture(compact, fixture), predict_fixture(model, fixture))
)
original_path <- tempfile(fileext = ".rds")
compact_path <- tempfile(fileext = ".rds")
saveRDS(model, original_path)
saveRDS(compact, compact_path)
restored <- readRDS(compact_path)
stopifnot(file.info(compact_path)$size < file.info(original_path)$size,
          identical(names(restored$fit), "coefficients"),
          identical(predict_fixture(restored, fixture),
                    predict_fixture(model, fixture)))
unlink(c(original_path, compact_path))
cat("PASS: compact R GLM excludes training arrays, scores identically and serializes smaller\n")
