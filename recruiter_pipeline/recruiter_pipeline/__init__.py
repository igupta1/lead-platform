"""Recruiter hiring-volume pipeline.

Surfaces companies hiring heavily (3+ unique roles in a rolling 30-day window)
for staffing / recruiting agencies. Company-centric by design: it reads ATS job
boards (Greenhouse / Lever / Ashby), where one call returns a company's entire
open-role list, so counting unique roles is exact rather than inferred.

Independent sibling of msp/cfo/insurance pipelines — no shared imports.
"""
