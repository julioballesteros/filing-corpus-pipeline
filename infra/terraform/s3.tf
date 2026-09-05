data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "target_config" {
  bucket        = local.target_config_bucket_name
  force_destroy = var.target_config_bucket_force_destroy

  tags = {
    Component = "discovery-target-config"
    Service   = "filing-ingestion"
  }
}

resource "aws_s3_bucket_ownership_controls" "target_config" {
  bucket = aws_s3_bucket.target_config.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "target_config" {
  bucket = aws_s3_bucket.target_config.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "target_config" {
  bucket = aws_s3_bucket.target_config.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "target_config" {
  bucket = aws_s3_bucket.target_config.id

  versioning_configuration {
    status = "Enabled"
  }
}

data "aws_iam_policy_document" "target_config" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.target_config.arn,
      "${aws_s3_bucket.target_config.arn}/*",
    ]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "target_config" {
  bucket = aws_s3_bucket.target_config.id
  policy = data.aws_iam_policy_document.target_config.json

  depends_on = [aws_s3_bucket_public_access_block.target_config]
}

resource "aws_s3_object" "discovery_targets" {
  bucket                 = aws_s3_bucket.target_config.id
  key                    = "discovery-targets/${var.environment}.json"
  source                 = local.discovery_target_manifest_path
  etag                   = filemd5(local.discovery_target_manifest_path)
  content_type           = "application/json"
  server_side_encryption = "AES256"

  metadata = {
    schema-version = "1"
    sha256         = local.discovery_target_manifest_sha256
  }

  depends_on = [
    aws_s3_bucket_policy.target_config,
    aws_s3_bucket_server_side_encryption_configuration.target_config,
    aws_s3_bucket_versioning.target_config,
  ]
}

resource "aws_s3_bucket" "raw_documents" {
  bucket        = local.raw_bucket_name
  force_destroy = var.raw_bucket_force_destroy

  tags = {
    Component = "raw-document-store"
    Service   = "filing-ingestion"
  }
}

resource "aws_s3_bucket_ownership_controls" "raw_documents" {
  bucket = aws_s3_bucket.raw_documents.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "raw_documents" {
  bucket = aws_s3_bucket.raw_documents.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "raw_documents" {
  bucket = aws_s3_bucket.raw_documents.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "raw_documents" {
  bucket = aws_s3_bucket.raw_documents.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "raw_documents" {
  bucket = aws_s3_bucket.raw_documents.id

  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  rule {
    id     = "remove-expired-delete-markers"
    status = "Enabled"

    filter {}

    expiration {
      expired_object_delete_marker = true
    }
  }

  depends_on = [aws_s3_bucket_versioning.raw_documents]
}

data "aws_iam_policy_document" "raw_documents" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.raw_documents.arn,
      "${aws_s3_bucket.raw_documents.arn}/*",
    ]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "raw_documents" {
  bucket = aws_s3_bucket.raw_documents.id
  policy = data.aws_iam_policy_document.raw_documents.json

  depends_on = [aws_s3_bucket_public_access_block.raw_documents]
}

resource "aws_s3_bucket" "normalized_corpus" {
  bucket        = local.normalized_bucket_name
  force_destroy = var.normalized_bucket_force_destroy

  tags = {
    Component = "normalized-corpus-store"
    Service   = "filing-ingestion"
  }
}

resource "aws_s3_bucket_ownership_controls" "normalized_corpus" {
  bucket = aws_s3_bucket.normalized_corpus.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "normalized_corpus" {
  bucket = aws_s3_bucket.normalized_corpus.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "normalized_corpus" {
  bucket = aws_s3_bucket.normalized_corpus.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "normalized_corpus" {
  bucket = aws_s3_bucket.normalized_corpus.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "normalized_corpus" {
  bucket = aws_s3_bucket.normalized_corpus.id

  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  rule {
    id     = "remove-expired-delete-markers"
    status = "Enabled"

    filter {}

    expiration {
      expired_object_delete_marker = true
    }
  }

  depends_on = [aws_s3_bucket_versioning.normalized_corpus]
}

data "aws_iam_policy_document" "normalized_corpus" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.normalized_corpus.arn,
      "${aws_s3_bucket.normalized_corpus.arn}/*",
    ]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "normalized_corpus" {
  bucket = aws_s3_bucket.normalized_corpus.id
  policy = data.aws_iam_policy_document.normalized_corpus.json

  depends_on = [aws_s3_bucket_public_access_block.normalized_corpus]
}
