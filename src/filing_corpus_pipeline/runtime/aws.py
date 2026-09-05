"""Late-bound access to the AWS SDK supplied by the Lambda runtime."""

from importlib import import_module
from typing import Protocol, cast


class _AwsClientFactory(Protocol):
    def client(self, service_name: str) -> object:
        """Create one low-level AWS service client."""


def aws_client(service_name: str) -> object:
    """Create a boto3 client without making boto3 a deployment dependency."""
    factory = cast(_AwsClientFactory, import_module("boto3"))
    return factory.client(service_name)
