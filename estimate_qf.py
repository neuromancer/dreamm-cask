#!/usr/bin/env python3
"""
Estimate a project's quadratic-funding matching for an active Giveth QF round.

Usage:
    python3 estimate_qf.py <project-slug> <qf-round-id>

Example:
    python3 estimate_qf.py echidna:-a-fast-smart-contract-fuzzer 16

Method:
    QF formula used by Giveth:  matching_p = pool * S_p^2 / sum_q(S_q^2)
    where S_p = sum over donors d of sqrt(donor_d_total_to_project_p).
    We page every donation for every project in the round, group by donor wallet,
    take sqrt of each donor total, sum them, square the result.

Caveats:
    Giveth applies passport / sybil filters when computing the on-platform
    estimate (the API's `projectDonationsSqrtSum` reflects the filtered S_p,
    which is generally higher than what this script computes from raw donations
    because rejected donors are excluded entirely rather than partly counted).
    Treat this script's output as an upper-bound-shaped approximation that
    ignores anti-sybil weighting.
"""

import json
import math
import sys
import time
import urllib.request
from collections import defaultdict

GRAPHQL_URL = "https://core.v6.giveth.io/graphql"

PROJECT_Q = """
query ProjectBySlug($slug: String!) {
  projectBySlug(slug: $slug) {
    id title slug
    projectQfRounds {
      countUniqueDonors sumDonationValueUsd
      qfRound { id name slug isActive beginDate endDate }
    }
  }
}
"""

ROUND_Q = """
query R($slug: String!) {
  qfRoundBySlug(slug: $slug) {
    id name slug isActive beginDate endDate
    allocatedFund allocatedFundUSD allocatedTokenSymbol
  }
}
"""

PROJECTS_IN_ROUND_Q = """
query P($filters: ProjectFiltersInput) {
  projects(skip: 0, take: 500, filters: $filters) {
    total
    projects { id title projectQfRounds { qfRoundId sumDonationValueUsd } }
  }
}
"""

DONATIONS_Q = """
query D($projectId: Int!, $skip: Int, $take: Int, $qfRoundId: Int) {
  donationsByProject(
    projectId: $projectId, skip: $skip, take: $take,
    orderBy: CreatedAt, orderDirection: ASC, qfRoundId: $qfRoundId
  ) {
    donations { valueUsd fromWalletAddress }
    total
  }
}
"""


def gql(query, variables=None, retries=5):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        GRAPHQL_URL, data=body, headers={"Content-Type": "application/json"}
    )
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read())
                if "errors" in resp:
                    raise RuntimeError(resp["errors"])
                return resp["data"]
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GraphQL request failed: {last}")


def project_sqrt_sum(project_id, qf_round_id):
    donor_totals = defaultdict(float)
    skip, take = 0, 200
    while True:
        d = gql(
            DONATIONS_Q,
            {"projectId": project_id, "skip": skip, "take": take, "qfRoundId": qf_round_id},
        )["donationsByProject"]
        for row in d["donations"]:
            addr = (row.get("fromWalletAddress") or "").lower()
            v = row.get("valueUsd") or 0
            if addr and v > 0:
                donor_totals[addr] += v
        if skip + take >= d["total"]:
            break
        skip += take
    return sum(math.sqrt(v) for v in donor_totals.values()), len(donor_totals)


def main():
    if len(sys.argv) != 3:
        print("usage: estimate_qf.py <project-slug> <qf-round-id>", file=sys.stderr)
        sys.exit(2)
    slug, round_id = sys.argv[1], int(sys.argv[2])

    project = gql(PROJECT_Q, {"slug": slug})["projectBySlug"]
    project_id = int(project["id"])
    project_qf = next(
        (r for r in project["projectQfRounds"] if r["qfRound"]["id"] == str(round_id)),
        None,
    )
    if not project_qf:
        print(f"Project '{slug}' is not in round {round_id}", file=sys.stderr)
        sys.exit(1)

    round_info = gql(ROUND_Q, {"slug": project_qf["qfRound"]["slug"]})["qfRoundBySlug"]
    pool_usd = round_info["allocatedFundUSD"] or round_info["allocatedFund"]

    projects = gql(PROJECTS_IN_ROUND_Q, {"filters": {"qfRoundId": round_id}})["projects"]["projects"]
    print(f"Round {round_id} '{round_info['name']}': {len(projects)} projects, pool ${pool_usd:,}")
    print(f"Computing sqrt-sums for every project...")

    total_score = 0.0
    target_sqrt_sum = 0.0
    target_donors = 0
    for i, p in enumerate(projects, 1):
        pid = int(p["id"])
        qf = next((r for r in p["projectQfRounds"] if r["qfRoundId"] == round_id), None)
        if not qf or (qf["sumDonationValueUsd"] or 0) <= 0:
            continue
        s, donors = project_sqrt_sum(pid, round_id)
        total_score += s * s
        if pid == project_id:
            target_sqrt_sum = s
            target_donors = donors
        if i % 20 == 0:
            print(f"  {i}/{len(projects)} projects done")

    score = target_sqrt_sum * target_sqrt_sum
    share = score / total_score if total_score else 0
    matching = share * pool_usd

    print()
    print(f"=== {project['title']} — round {round_id} ({round_info['name']}) ===")
    print(f"  Raised in round:        ${project_qf['sumDonationValueUsd']:,.2f}")
    print(f"  Unique donors:          {target_donors}")
    print(f"  Project sqrt-sum (S_p): {target_sqrt_sum:.4f}")
    print(f"  Project QF score:       {score:,.2f}")
    print(f"  Round score total:      {total_score:,.2f}")
    print(f"  Project share:          {share * 100:.4f}%")
    print(f"  Matching pool:          ${pool_usd:,}")
    print(f"  Estimated matching:     ${matching:,.2f}")


if __name__ == "__main__":
    main()
