resource "aws_dynamodb_table" "filing_registry" {
  name         = local.registry_table_name
  billing_mode = "PAY_PER_REQUEST"
  table_class  = "STANDARD"
  hash_key     = "filing_key"

  deletion_protection_enabled = var.registry_deletion_protection_enabled

  attribute {
    name = "filing_key"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = {
    Component = "filing-registry"
    Service   = "filing-ingestion"
  }
}
