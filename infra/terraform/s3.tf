data "aws_caller_identity" "current" {}

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
