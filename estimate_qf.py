#!/usr/bin/env python3
"""
Estimate a project's quadratic-funding matching for an active Giveth QF round.

Usage:
    python3 estimate_qf.py <project-slug> <qf-round-id>

Example:
    python3 estimate_qf.py echidna:-a-fast-smart-contract-fuzzer 16

Method:
    Matching formula:           matching_p = pool * S_p^2 / sum_q(S_q^2)
    Giveth's per-project S_p:   sum over each donation row in the round of
                                sqrt(donation.valueUsd).
    (Note: this is NOT canonical QF, which would group by donor first. The
    impact-graph backend reproduces this script's number — verified against
    the live `projectDonationsSqrtSum` field.)

    Round-specific weighting:
    The Ethereum Security round (id 16) applies a 4× multiplier to donations
    from "verified badge holders". Anyone who donated TIK or FINN tokens
    counts as a badge holder. With --badge-boost the script does a first
    pass across the round to identify badge wallets, then a second pass
    that applies sqrt(4·v) for those donations.

    No COCM / passport / sybil clustering is applied here; that adjustment
    is run off-chain at distribution time by Giveth's COCM_QF_Algorithm.
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
    donations { valueUsd currency fromWalletAddress }
    total
  }
}
"""

BADGE_TOKENS = {"TIK", "FINN"}
BADGE_MULTIPLIER = 4.0
PAIRWISE_M_DEFAULT = 300.0


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


def project_donations(project_id, qf_round_id):
    rows_out = []
    skip, take = 0, 200
    while True:
        d = gql(
            DONATIONS_Q,
            {"projectId": project_id, "skip": skip, "take": take, "qfRoundId": qf_round_id},
        )["donationsByProject"]
        for row in d["donations"]:
            v = row.get("valueUsd") or 0
            if v <= 0:
                continue
            rows_out.append({
                "valueUsd": v,
                "currency": (row.get("currency") or "").upper(),
                "addr": (row.get("fromWalletAddress") or "").lower(),
            })
        if skip + take >= d["total"]:
            break
        skip += take
    return rows_out


def pairwise_phi(all_donations, target_pid, badge_wallets, M=PAIRWISE_M_DEFAULT):
    """Pairwise CLR (Vitalik/Weyl) approximation of COCM clustering.

    Treats each wallet as one donor (badge-boosted where applicable).
    Anonymous rows (no wallet address) become unique synthetic donors that
    cannot cluster with anyone else (k=1 vs everyone). Returns (phi_target,
    phi_total).
    """
    donor_by_project = defaultdict(dict)
    anon_id = 0
    for pid, rows in all_donations.items():
        for r in rows:
            v = r["valueUsd"]
            if v <= 0:
                continue
            is_badge = (r["currency"] in BADGE_TOKENS) or (
                r["addr"] and r["addr"] in badge_wallets
            )
            weighted = v * (BADGE_MULTIPLIER if is_badge else 1.0)
            if r["addr"]:
                donor_id = r["addr"]
            else:
                donor_id = f"__anon_{pid}_{anon_id}"
                anon_id += 1
            donor_by_project[pid][donor_id] = (
                donor_by_project[pid].get(donor_id, 0.0) + weighted
            )

    pair_score = defaultdict(float)
    for pid, donors in donor_by_project.items():
        items = list(donors.items())
        for i in range(len(items)):
            id_i, c_i = items[i]
            sqrt_i = math.sqrt(c_i)
            for j in range(i + 1, len(items)):
                id_j, c_j = items[j]
                key = (id_i, id_j) if id_i < id_j else (id_j, id_i)
                pair_score[key] += sqrt_i * math.sqrt(c_j)

    phi_total = 0.0
    phi_target = 0.0
    for pid, donors in donor_by_project.items():
        items = list(donors.items())
        phi = sum(c for _, c in items)
        for i in range(len(items)):
            id_i, c_i = items[i]
            sqrt_i = math.sqrt(c_i)
            for j in range(i + 1, len(items)):
                id_j, c_j = items[j]
                key = (id_i, id_j) if id_i < id_j else (id_j, id_i)
                k = M / (M + pair_score[key])
                phi += 2 * sqrt_i * math.sqrt(c_j) * k
        phi_total += phi
        if pid == target_pid:
            phi_target = phi
    return phi_target, phi_total


def project_sqrt_sum(rows, badge_wallets=None):
    sqrt_sum = 0.0
    badge_wallets = badge_wallets or set()
    boosted = 0
    for r in rows:
        v = r["valueUsd"]
        is_badge_currency = r["currency"] in BADGE_TOKENS
        is_badge_wallet = bool(r["addr"]) and r["addr"] in badge_wallets
        if is_badge_currency or is_badge_wallet:
            sqrt_sum += math.sqrt(v * BADGE_MULTIPLIER)
            boosted += 1
        else:
            sqrt_sum += math.sqrt(v)
    return sqrt_sum, boosted


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    M = PAIRWISE_M_DEFAULT
    pairwise = False
    apply_badge_boost = True
    for f in flags:
        if f == "--no-badge-boost":
            apply_badge_boost = False
        elif f == "--pairwise":
            pairwise = True
        elif f.startswith("--M="):
            pairwise = True
            M = float(f.split("=", 1)[1])
        else:
            print(f"unknown flag: {f}", file=sys.stderr)
            sys.exit(2)
    if len(args) != 2:
        print(
            "usage: estimate_qf.py [--no-badge-boost] [--pairwise] [--M=<n>] <project-slug> <qf-round-id>",
            file=sys.stderr,
        )
        sys.exit(2)
    slug, round_id = args[0], int(args[1])

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

    print("Fetching donations for every project...")
    all_donations = {}
    for i, p in enumerate(projects, 1):
        pid = int(p["id"])
        qf = next((r for r in p["projectQfRounds"] if r["qfRoundId"] == round_id), None)
        if not qf or (qf["sumDonationValueUsd"] or 0) <= 0:
            continue
        all_donations[pid] = project_donations(pid, round_id)
        if i % 20 == 0:
            print(f"  fetched {i}/{len(projects)}")

    badge_wallets = set()
    if apply_badge_boost:
        for rows in all_donations.values():
            for r in rows:
                if r["addr"] and r["currency"] in BADGE_TOKENS:
                    badge_wallets.add(r["addr"])
        print(f"Badge wallets identified (TIK/FINN donors): {len(badge_wallets)}")

    effective_badges = badge_wallets if apply_badge_boost else set()
    total_score = 0.0
    target_sqrt_sum = 0.0
    target_donations = 0
    target_donors = set()
    target_boosted = 0
    target_badge_donors = 0
    for pid, rows in all_donations.items():
        s, boosted = project_sqrt_sum(rows, effective_badges)
        total_score += s * s
        if pid == project_id:
            target_sqrt_sum = s
            target_donations = len(rows)
            target_donors = {r["addr"] for r in rows if r["addr"]}
            target_boosted = boosted
            target_badge_donors = len(target_donors & badge_wallets)

    score = target_sqrt_sum * target_sqrt_sum
    share = score / total_score if total_score else 0
    matching = share * pool_usd

    pairwise_match = None
    pairwise_share = None
    if pairwise:
        print(f"Computing pairwise CLR (M={M})...")
        phi_p, phi_total = pairwise_phi(all_donations, project_id, effective_badges, M=M)
        pairwise_share = phi_p / phi_total if phi_total else 0
        pairwise_match = pairwise_share * pool_usd

    print()
    label = "with 4× badge boost" if apply_badge_boost else "vanilla (no boost)"
    print(f"=== {project['title']} — round {round_id} ({round_info['name']}) — {label} ===")
    print(f"  Raised in round:        ${project_qf['sumDonationValueUsd']:,.2f}")
    print(f"  Donations counted:      {target_donations}")
    print(f"  Unique donor wallets:   {len(target_donors)}")
    if apply_badge_boost:
        print(f"  Badge donors (project): {target_badge_donors}")
        print(f"  Boosted donation rows:  {target_boosted}")
    print(f"  Project sqrt-sum (S_p): {target_sqrt_sum:.4f}")
    print(f"  Project QF score:       {score:,.2f}")
    print(f"  Round score total:      {total_score:,.2f}")
    print(f"  Project share:          {share * 100:.4f}%")
    print(f"  Matching pool:          ${pool_usd:,}")
    print(f"  Estimated matching:     ${matching:,.2f}")
    if pairwise:
        print()
        print(f"=== Pairwise CLR (M={M}) — clustering approximation ===")
        print(f"  Pairwise project Φ:     {phi_p:,.2f}")
        print(f"  Pairwise round Φ total: {phi_total:,.2f}")
        print(f"  Pairwise share:         {pairwise_share * 100:.4f}%")
        print(f"  Pairwise matching:      ${pairwise_match:,.2f}")


if __name__ == "__main__":
    main()
