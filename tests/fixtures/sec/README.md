# SEC acceptance fixtures

These are bounded excerpts of real SEC Company Facts responses retrieved on 2026-10-23 using an identifying contact. They retain source accession numbers, filing dates, units, and original values for supported concepts with period ends from 2024 onward. They are not invented examples.

Sources:

- AMZN: https://data.sec.gov/api/xbrl/companyfacts/CIK0001018724.json
- MSFT: https://data.sec.gov/api/xbrl/companyfacts/CIK0000789019.json
- NVDA: https://data.sec.gov/api/xbrl/companyfacts/CIK0001045810.json
- JPM: https://data.sec.gov/api/xbrl/companyfacts/CIK0000019617.json
- WMT: https://data.sec.gov/api/xbrl/companyfacts/CIK0000104169.json

Fiscal-2025 acceptance values, in USD base units, are explicit in test_sec_fixtures.py. They cover different fiscal calendars and the bank revenue concept. Tests do not contact SEC. A real Amazon annual report was additionally retrieved during implementation to exercise all three breakdown dimensions. That full document stays in ignored local storage rather than this repository.
