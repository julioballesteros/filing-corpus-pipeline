"""Map SEC submission metadata to the provider-neutral discovery contract."""

from filing_corpus_pipeline.adapters.sec.constants import SEC_ARCHIVE_BASE_URL
from filing_corpus_pipeline.adapters.sec.submissions import SecSubmissionsClient
from filing_corpus_pipeline.discovery import DiscoveryRequest
from filing_corpus_pipeline.domain import FilingForm, FilingReference, IssuerReference


class SecFilingDiscoverySource:
    """Discover supported filings for a bounded list of SEC issuers."""

    provider = "sec"

    def __init__(self, client: SecSubmissionsClient) -> None:
        self._client = client

    def discover(self, request: DiscoveryRequest) -> list[FilingReference]:
        """Retrieve, filter, and map SEC filing metadata."""
        filings: list[FilingReference] = []
        for requested_issuer in request.issuers:
            submissions = self._client.list_submissions(
                requested_issuer.provider_issuer_id,
                filed_from=request.filed_from,
                filed_to=request.filed_to,
            )
            for submission in submissions:
                try:
                    form = FilingForm(submission.form)
                except ValueError:
                    continue
                if form not in request.forms or not (
                    request.filed_from <= submission.filed_on <= request.filed_to
                ):
                    continue

                issuer = IssuerReference(
                    provider=self.provider,
                    provider_issuer_id=submission.cik,
                )
                accession_path = submission.accession_number.replace("-", "")
                archive_cik = str(int(submission.cik))
                directory_url = f"{SEC_ARCHIVE_BASE_URL}/{archive_cik}/{accession_path}"
                filings.append(
                    FilingReference(
                        provider=self.provider,
                        provider_filing_id=submission.accession_number,
                        issuer=issuer,
                        issuer_name=submission.issuer_name,
                        form=form,
                        filed_on=submission.filed_on,
                        report_date=submission.report_date,
                        accepted_at=submission.accepted_at,
                        primary_document=submission.primary_document,
                        filing_detail_url=(
                            f"{directory_url}/{submission.accession_number}-index.html"
                        ),
                        primary_document_url=(
                            f"{directory_url}/{submission.primary_document}"
                        ),
                    )
                )
        return filings
