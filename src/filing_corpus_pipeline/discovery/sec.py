"""SEC-specific filing discovery behavior."""

from filing_corpus_pipeline.discovery.models import DiscoveryRequest, DiscoveryTarget
from filing_corpus_pipeline.discovery.service import UnsupportedFilingSelectionError
from filing_corpus_pipeline.domain import DocumentPolicy, FilingReference
from filing_corpus_pipeline.sources.sec import (
    SecEdgarClient,
    SecSubmission,
    sec_filing_detail_url,
    sec_filing_document_url,
)


class SecFilingDiscoverySource:
    """List and map SEC filings for a bounded set of registrants."""

    provider = "sec"
    _earnings_release_item = "2.02"
    _supported_routes = frozenset(
        {
            ("10-K", DocumentPolicy.PRIMARY),
            ("10-Q", DocumentPolicy.PRIMARY),
            ("8-K", DocumentPolicy.EARNINGS_RELEASE),
        }
    )

    def __init__(self, client: SecEdgarClient) -> None:
        self._client = client

    def validate_target(self, target: DiscoveryTarget) -> None:
        """Validate SEC identity and the discovery routes implemented here."""
        if target.issuer.provider != self.provider:
            raise ValueError(
                f"SEC discovery cannot process provider {target.issuer.provider!r}"
            )
        unsupported = [
            selection
            for selection in target.selections
            if (selection.filing_type, selection.document_policy)
            not in self._supported_routes
        ]
        if unsupported:
            selection = unsupported[0]
            raise UnsupportedFilingSelectionError(
                "unsupported SEC filing selection for issuer "
                f"{target.issuer.provider_issuer_id!r}: "
                f"{selection.filing_type!r} with document policy "
                f"{selection.document_policy.value!r}"
            )

    def discover(self, request: DiscoveryRequest) -> list[FilingReference]:
        """Retrieve matching recent and relevant historical SEC filings."""
        filings: list[FilingReference] = []
        for target in request.targets:
            company = self._client.get_company_submissions(
                target.issuer.provider_issuer_id
            )
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
                if (
                    filing := _to_filing_reference(
                        submission,
                        target=target,
                        request=request,
                    )
                )
                is not None
            )
        return filings


def _to_filing_reference(
    submission: SecSubmission,
    *,
    target: DiscoveryTarget,
    request: DiscoveryRequest,
) -> FilingReference | None:
    selections = {selection.filing_type: selection for selection in target.selections}
    selection = selections.get(submission.form)
    if selection is None or not (
        request.filed_from <= submission.filed_on <= request.filed_to
    ):
        return None
    if (
        selection.document_policy is DocumentPolicy.EARNINGS_RELEASE
        and SecFilingDiscoverySource._earnings_release_item not in submission.items
    ):
        return None

    return FilingReference(
        company_id=target.company_id,
        provider="sec",
        provider_filing_id=submission.accession_number,
        provider_issuer_id=submission.cik,
        issuer_name=submission.issuer_name,
        filing_type=submission.form,
        document_policy=selection.document_policy,
        filed_on=submission.filed_on,
        report_date=submission.report_date,
        accepted_at=submission.accepted_at,
        primary_document=submission.primary_document,
        filing_detail_url=sec_filing_detail_url(
            submission.cik,
            submission.accession_number,
        ),
        primary_document_url=sec_filing_document_url(
            submission.cik,
            submission.accession_number,
            submission.primary_document,
        ),
    )
