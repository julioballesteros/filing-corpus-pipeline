"""SEC-specific filing discovery behavior."""

from filing_corpus_pipeline.discovery.models import DiscoveryRequest
from filing_corpus_pipeline.domain import FilingForm, FilingReference
from filing_corpus_pipeline.sources.sec import (
    SecEdgarClient,
    SecSubmission,
    sec_filing_directory_url,
)


class SecFilingDiscoverySource:
    """List and map SEC filings for a bounded set of registrants."""

    provider = "sec"

    def __init__(self, client: SecEdgarClient) -> None:
        self._client = client

    def discover(self, request: DiscoveryRequest) -> list[FilingReference]:
        """Retrieve matching recent and relevant historical SEC filings."""
        filings: list[FilingReference] = []
        for issuer in request.issuers:
            company = self._client.get_company_submissions(issuer.provider_issuer_id)
            submissions = list(company.recent)
            for historical_file in company.files:
                if (
                    historical_file.filing_to < request.filed_from
                    or historical_file.filing_from > request.filed_to
                ):
                    continue
                submissions.extend(
                    self._client.get_historical_submissions(
                        historical_file.name,
                        cik=company.cik,
                        issuer_name=company.issuer_name,
                    )
                )

            filings.extend(
                filing
                for submission in submissions
                if (filing := _to_filing_reference(submission, request)) is not None
            )
        return filings


def _to_filing_reference(
    submission: SecSubmission,
    request: DiscoveryRequest,
) -> FilingReference | None:
    try:
        form = FilingForm(submission.form)
    except ValueError:
        return None
    if form not in request.forms or not (
        request.filed_from <= submission.filed_on <= request.filed_to
    ):
        return None

    directory_url = sec_filing_directory_url(
        submission.cik,
        submission.accession_number,
    )
    return FilingReference(
        provider="sec",
        provider_filing_id=submission.accession_number,
        provider_issuer_id=submission.cik,
        issuer_name=submission.issuer_name,
        form=form,
        filed_on=submission.filed_on,
        report_date=submission.report_date,
        accepted_at=submission.accepted_at,
        primary_document=submission.primary_document,
        filing_detail_url=(f"{directory_url}/{submission.accession_number}-index.html"),
        primary_document_url=f"{directory_url}/{submission.primary_document}",
    )
