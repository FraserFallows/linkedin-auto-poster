#!/usr/bin/env python3
"""
Core logic for the LinkedIn auto-poster.

Usage:
    python post.py --mode generic
    python post.py --mode vargr
    python post.py --mode vargr --intro-post   # one-time, manually-triggered whole-history intro

DRY_RUN=true in the environment prints the composed post instead of publishing it,
and skips every write to posted.json and POSTS.md (content selection still runs
for real).
"""

import argparse
import json
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# Windows consoles default to a codepage that can't encode this content's
# emoji, crashing print() without this.
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

# --- Paths -------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTENT_DIR = REPO_ROOT / "content"
SNIPPETS_PATH = CONTENT_DIR / "snippets.json"
INTROS_GENERIC_PATH = CONTENT_DIR / "intros_generic.json"
INTROS_VARGR_PATH = CONTENT_DIR / "intros_vargr.json"
POSTED_PATH = CONTENT_DIR / "posted.json"
POSTS_LOG_PATH = REPO_ROOT / "POSTS.md"

# --- Config --------------------------------------------------------------
# Kept as data rather than scattered literals so a second source repo later
# (see plan: Future - Multiple Source Repos) is a data addition, not a rewrite.

VARGR = {
    "repo": "FraserFallows/vargr-viking",
    "site": "vargrviking.co.uk",
    "github_url": "github.com/FraserFallows/vargr-viking",
}

REPO_LINK_LINE = "🔗 See how this post was made: github.com/FraserFallows/linkedin-auto-poster"
VARGR_LINKS_LINE = f"Visit: {VARGR['site']} — Source: {VARGR['github_url']}"

SUBSTANTIAL_MIN_COMMITS = 3
SUBSTANTIAL_MIN_LINES = 50

TIER = {"target_chars": 250, "max_chars": 450, "sentences": "1-4"}

# Prints Claude's should_post reasoning when True - flip on when a verdict
# looks wrong and you need to see why.
DEBUG_EVALUATION = False

# Bump roughly yearly - LinkedIn rejects deprecated versions outright with
# 426 Upgrade Required (hit this for real with the old "202401").
LINKEDIN_MAX_CHARS = 3000
LINKEDIN_API_VERSION = "202607"

GITHUB_API = "https://api.github.com"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-haiku-4-5"  # cheapest tier - plenty for summarising a handful of short commit messages


def raise_for_status(resp):
    """Like resp.raise_for_status(), but prints the response body first - the
    body carries the actual reason (e.g. Anthropic's error.message), which a
    bare HTTPError discards."""
    if not resp.ok:
        print(f"::error::{resp.request.method} {resp.url} -> HTTP {resp.status_code}\n{resp.text}",
              file=sys.stderr)
    resp.raise_for_status()


# --- posted.json -----------------------------------------------------------

def load_posted():
    if POSTED_PATH.exists():
        return json.loads(POSTED_PATH.read_text(encoding="utf-8"))
    return {}


def save_posted(state):
    POSTED_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


# --- POSTS.md changelog ------------------------------------------------------

POSTS_LOG_TITLE = "# Post History\n\nEvery post this bot has published, newest first.\n\n"


def log_post(track_label, post_text):
    """Prepend a dated entry to POSTS.md - a human-readable post history,
    newest first. Only called after a real (non-dry-run) publish succeeds,
    same rule as posted.json - dry runs never touch this file."""
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry = f"## {date} — {track_label}\n\n{post_text}\n"

    if POSTS_LOG_PATH.exists():
        existing = POSTS_LOG_PATH.read_text(encoding="utf-8")
        marker = "\n## "
        idx = existing.find(marker)
        if idx == -1:
            title, entries = existing.rstrip("\n") + "\n\n", ""
        else:
            title, entries = existing[:idx + 1], existing[idx + 1:]
    else:
        title, entries = POSTS_LOG_TITLE, ""

    POSTS_LOG_PATH.write_text(title + entry + "\n---\n\n" + entries, encoding="utf-8")


# --- Shuffle-bag intro cycling ----------------------------------------------

def load_intro_pool(path):
    return json.loads(path.read_text(encoding="utf-8"))


def pick_intro(track, pool_size):
    """
    Shuffle-bag with a boundary guard, not plain random: first cycle is file
    order, later cycles are fresh permutations whose first 3 slots exclude
    the previous cycle's last 3 - guarantees a minimum 4-post gap between
    repeats. Returns (intro_index, updated_track) without mutating input;
    skipped on a dry run so testing doesn't consume the cycle.
    """
    order = track.get("intro_order")
    position = track.get("intro_position", 0)
    if order is None:
        order = list(range(pool_size))
        position = 0

    intro_index = order[position]
    next_position = position + 1

    if next_position >= pool_size:
        last_three = set(order[-3:])
        safe_first = [i for i in range(pool_size) if i not in last_three]
        random.shuffle(safe_first)
        first_three = safe_first[:3]
        remaining = [i for i in range(pool_size) if i not in first_three]
        random.shuffle(remaining)
        updated = {"intro_order": first_three + remaining, "intro_position": 0}
    else:
        updated = {"intro_order": order, "intro_position": next_position}

    return intro_index, updated


# --- Monday / generic mode ---------------------------------------------------

def pick_snippet(posted):
    snippets = json.loads(SNIPPETS_PATH.read_text(encoding="utf-8"))
    used = set(posted.get("generic", {}).get("used_snippet_ids", []))
    eligible = [s for s in snippets if s["id"] not in used]
    if not eligible:
        return None
    return random.choice(eligible)


# --- Friday / vargr mode ------------------------------------------------------

def github_headers():
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_commits(since_iso=None):
    """Commits to the Vargr Viking repo, optionally since a given ISO date.
    Returns a list of dicts newest-first: {sha, message, is_merge}."""
    commits = []
    url = f"{GITHUB_API}/repos/{VARGR['repo']}/commits"
    params = {"per_page": 100}
    if since_iso:
        params["since"] = since_iso
    page = 1
    while True:
        params["page"] = page
        resp = requests.get(url, headers=github_headers(), params=params, timeout=30)
        raise_for_status(resp)
        batch = resp.json()
        if not batch:
            break
        for c in batch:
            commits.append({
                "sha": c["sha"],
                "message": c["commit"]["message"].splitlines()[0],
                "is_merge": len(c.get("parents", [])) > 1,
            })
        if len(batch) < params["per_page"]:
            break
        page += 1
    return commits


def fetch_commit_line_count(sha):
    url = f"{GITHUB_API}/repos/{VARGR['repo']}/commits/{sha}"
    resp = requests.get(url, headers=github_headers(), timeout=30)
    raise_for_status(resp)
    return resp.json().get("stats", {}).get("total", 0)


def is_substantial(non_merge_commits):
    if len(non_merge_commits) >= SUBSTANTIAL_MIN_COMMITS:
        return True
    # OR-clause safety net for a single huge commit. Only fetch per-commit
    # stats when the cheap count check alone didn't already pass.
    for commit in non_merge_commits:
        if fetch_commit_line_count(commit["sha"]) >= SUBSTANTIAL_MIN_LINES:
            return True
    return False


def summarise_commits(messages, intro_post=False):
    if intro_post:
        task = (
            "Summarise this commit history as an introduction to the project, "
            "written for people seeing it for the first time. Somewhere in the "
            "post, make clear that what's being introduced is a website - the "
            "reader doesn't yet know that going in. This is the site's original "
            "build, described from its very first commit - do not call it a "
            "\"rebuild\", \"redesign\", \"relaunch\", or imply an earlier version "
            "existed and was redone; it's being built for the first time, not "
            "remade. There's a lot of history to draw from, but the length target "
            "below still applies - pick only the handful of most important "
            "changes across the whole history and leave the rest out entirely, "
            "rather than compressing everything in to fit. Order what you do "
            "include by importance, not by when it happened - lead with the most "
            "important thing for a first-time reader to know, then the next most "
            "important, rather than retelling the commit history chronologically. "
            "Capability changes outrank the tools or pipeline used to build them, "
            "same as elsewhere in this brief - lead with what the site can now "
            "do, not which framework or CI setup got it there."
        )
    else:
        task = "Summarise this week's progress in a short, casual, LinkedIn-friendly update."

    prompt = (
        f"{task}\n\n"
        f"Target length if you do post: aim for about {TIER['target_chars']} characters, "
        f"{TIER['sentences']} sentences - a soft target, not a hard rule. Flex up to "
        f"{TIER['max_chars']} characters when there's genuinely enough substantial material "
        "to justify it - don't cut a substantial item just to land under the target. Just as "
        "importantly, stop well short of the target, even at a single short sentence, when "
        "the genuinely important material runs out sooner - padding to reach it is exactly "
        "how minor details sneak back in after being told to leave them out.\n"
        "Casual, punchy tone - this is a LinkedIn post, not a changelog. Write it as one "
        "flowing narrative, not a list of separate items each parked in its own sentence - "
        "connect related changes together so it reads as a continuous story of the build, "
        "not a recap of individual commits.\n"
        "Do not name-drop \"Vargr Viking\" - the post's intro line already establishes "
        "that context, so repeating the name reads as redundant.\n"
        "When attributing the work to whoever built this, say \"the human\" - never \"I\" "
        "or \"we\"/\"our\". The narrator is the automated posting account reporting on what "
        "the human did, not the human speaking directly.\n"
        "Not every commit deserves a mention, whether you're summarising a single week or "
        "the entire project history - being comprehensive is not the goal, being selective "
        "is. A change is substantial if it adds a genuinely new feature, system, or flow "
        "the software didn't have before - a backend, an admin panel, authentication, "
        "persistent sessions, a new user-facing flow. It is NOT substantial just because "
        "it's technically new: metadata/presentation changes (social preview tags, SEO "
        "markup, security headers, styling, icons) change how existing functionality is "
        "perceived, shared, or looks without the software doing anything new, so they're "
        "polish, not a capability, even though in a literal sense \"it couldn't do that "
        "before.\" Bug fixes are the same: one only clears the bar if it was broadly broken "
        "for ordinary, everyday use of a feature (e.g. a contact form silently failing for "
        "every visitor) - not if it's a narrow edge case triggered by one specific, unusual "
        "input (e.g. a button that only breaks when a title contains an apostrophe). "
        "Several routine items landing together don't add up to substantial either - judge "
        "each on its own, never by the total count. This also means never tacking on a "
        "trailing sentence that rounds up minor extras just to add texture or make the post "
        "feel complete - if a detail doesn't clear the bar on its own, leave it out entirely "
        "rather than mentioning it as an aside. Whatever does clear the bar, state the "
        "specific concrete fact rather than vague praise like \"sweating the details\" or "
        "\"attention to detail\", and never inflate it by claiming it's common or typical "
        "(e.g. \"trips up most sites\") - only what's actually true from the commit history, "
        "nothing broader or invented. If nothing clears the bar, set should_post to false "
        "rather than manufacturing a post out of routine work just to have something to "
        "say.\n"
        "Treat near-identical consecutive commit messages (e.g. a fixup commit reusing "
        "the same message as the one before it) as a single logical change, not two.\n\n"
        "Commit messages:\n" + "\n".join(f"- {m}" for m in messages)
    )

    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 1500,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "evaluation": {
                            "type": "string",
                            "description": "Internal reasoning only, never shown "
                            "publicly - fill this in before deciding should_post, don't "
                            "skip straight to a verdict. Keep it terse: for each commit, "
                            "a few words tagging it as either a capability change / "
                            "meaningful user-facing fix, or routine upkeep (polish, "
                            "hygiene, tooling, docs, edge-case fix) - not full sentences "
                            "of explanation. Base should_post on this classification, not "
                            "a separate overall impression.",
                        },
                        "should_post": {
                            "type": "boolean",
                            "description": "True only if the evaluation above found at "
                            "least one item that classifies as a capability change or "
                            "meaningful user-facing fix. False if every item classified "
                            "as routine upkeep.",
                        },
                        "content": {
                            "type": "string",
                            "description": "The post's content block (just the summary "
                            "text - no intro, no links). Empty string if should_post is "
                            "false.",
                        },
                        "skip_reason": {
                            "type": "string",
                            "description": "Brief internal note on why should_post is "
                            "false, for logging only - never shown publicly. Empty "
                            "string if should_post is true.",
                        },
                    },
                    "required": ["evaluation", "should_post", "content", "skip_reason"],
                    "additionalProperties": False,
                },
            }
        },
    }
    resp = requests.post(ANTHROPIC_API, headers=headers, json=body, timeout=60)
    raise_for_status(resp)
    # output_config.format guarantees the first content block is text containing
    # valid JSON matching the schema above.
    return json.loads(resp.json()["content"][0]["text"])


# --- Composition & length enforcement ----------------------------------------

def compose(intro, content, extra_line=None):
    parts = [intro, content]
    if extra_line:
        parts.append(extra_line)
    parts.append(REPO_LINK_LINE)
    return "\n\n".join(parts)


def enforce_hard_limit(intro, content, extra_line=None):
    """Defensive fallback only - the style target (~250/~450 chars) is the real
    length control. Hitting this means something upstream misbehaved."""
    full = compose(intro, content, extra_line)
    if len(full) <= LINKEDIN_MAX_CHARS:
        return full
    print(
        f"::warning::Composed post ({len(full)} chars) exceeded LinkedIn's "
        f"{LINKEDIN_MAX_CHARS}-char limit - this should not happen under normal "
        "operation. Truncating content."
    )
    fixed_overhead = len(compose(intro, "", extra_line)) + 1  # +1 for the ellipsis
    budget = max(LINKEDIN_MAX_CHARS - fixed_overhead, 0)
    truncated_content = content[:budget].rstrip() + "…"
    return compose(intro, truncated_content, extra_line)


# --- LinkedIn API --------------------------------------------------------------
# No refresh flow: LinkedIn only grants refresh tokens to approved Marketing
# Developer Platform partners, not a personal-tier app. LINKEDIN_ACCESS_TOKEN
# is a long-lived (~60 day) token renewed manually (see plan: Token Renewal);
# publish_post fails clearly on 401 when it expires.

# Reserved by LinkedIn's "little" commentary format - every occurrence needs
# escaping, not just when it'd look like markup. Real content hits this often
# (C# generics, attributes). See: learn.microsoft.com/en-us/linkedin/marketing/
# community-management/shares/little-text-format
LITTLE_RESERVED_CHARS = set("|{}@[]()<>#\\*_~")


def escape_little_text(text):
    return "".join(f"\\{c}" if c in LITTLE_RESERVED_CHARS else c for c in text)


def publish_post(access_token, text):
    headers = {
        "Authorization": f"Bearer {access_token}",
        "LinkedIn-Version": LINKEDIN_API_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }
    body = {
        "author": os.environ["LINKEDIN_PERSON_URN"],
        "commentary": escape_little_text(text),
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    resp = requests.post(
        "https://api.linkedin.com/rest/posts", headers=headers, json=body, timeout=30
    )
    if resp.status_code == 401:
        print(
            "::error::LinkedIn returned 401 Unauthorized - LINKEDIN_ACCESS_TOKEN has "
            "almost certainly expired (LinkedIn access tokens last ~60 days, and this "
            "app isn't approved for refresh tokens, so renewal is manual). Redo the "
            "OAuth authorisation flow and update the LINKEDIN_ACCESS_TOKEN secret.",
            file=sys.stderr,
        )
    raise_for_status(resp)


# --- Main ----------------------------------------------------------------------

def is_dry_run():
    return os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["generic", "vargr"], required=True)
    parser.add_argument(
        "--intro-post",
        action="store_true",
        help="One-time manual whole-history intro post (--mode vargr only, see Rollout Plan)",
    )
    args = parser.parse_args()

    dry_run = is_dry_run()
    posted = load_posted()

    if args.mode == "generic":
        track = posted.setdefault("generic", {})

        snippet = pick_snippet(posted)
        if snippet is None:
            print(
                "Snippet pool exhausted - all 52 entries used. Refresh "
                "content/snippets.json to resume Monday posts."
            )
            return
        content = snippet["text"]

        intros = load_intro_pool(INTROS_GENERIC_PATH)
        intro_index, updated_intro_state = pick_intro(track, len(intros))
        intro = intros[intro_index]

        post_text = enforce_hard_limit(intro, content)

        if dry_run:
            print("DRY RUN - would publish:\n\n" + post_text)
            return

        access_token = os.environ["LINKEDIN_ACCESS_TOKEN"]
        publish_post(access_token, post_text)
        log_post("Generic", post_text)

        track.setdefault("used_snippet_ids", []).append(snippet["id"])
        track.update(updated_intro_state)
        save_posted(posted)
        print("Posted successfully.")

    else:  # vargr
        track = posted.setdefault("vargr", {})

        if args.intro_post:
            commits = fetch_commits(since_iso=None)
        else:
            last_checked = track.get("last_checked_date")
            if last_checked is None:
                print(
                    "No last_checked_date in posted.json for the vargr track. This "
                    "must be seeded once, manually, right after the one-time "
                    "--intro-post run - see the plan's Rollout Plan section. "
                    "Refusing to guess a start point.",
                    file=sys.stderr,
                )
                sys.exit(1)
            commits = fetch_commits(since_iso=last_checked)

        non_merge = [c for c in commits if not c["is_merge"]]
        latest_sha = commits[0]["sha"] if commits else track.get("last_checked_sha")
        now_iso = datetime.now(timezone.utc).isoformat()

        if not args.intro_post and not is_substantial(non_merge):
            print(f"Not substantial this week ({len(non_merge)} non-merge commits) - skipping.")
            if not dry_run:
                track["last_checked_date"] = now_iso
                track["last_checked_sha"] = latest_sha
                save_posted(posted)
            return

        messages = [c["message"] for c in non_merge]
        result = summarise_commits(messages, intro_post=args.intro_post)
        if DEBUG_EVALUATION:
            print(f"Claude's evaluation: {result['evaluation']}")

        if not result["should_post"]:
            print(f"Claude judged this period not post-worthy: {result['skip_reason']}")
            if not dry_run:
                track["last_checked_date"] = now_iso
                track["last_checked_sha"] = latest_sha
                save_posted(posted)
            return

        content = result["content"]
        if args.intro_post:
            content += " Expect updates semi-regularly."

        intros = load_intro_pool(INTROS_VARGR_PATH)
        intro_index, updated_intro_state = pick_intro(track, len(intros))
        intro = intros[intro_index]

        post_text = enforce_hard_limit(intro, content, extra_line=VARGR_LINKS_LINE)

        if dry_run:
            print("DRY RUN - would publish:\n\n" + post_text)
            return

        access_token = os.environ["LINKEDIN_ACCESS_TOKEN"]
        publish_post(access_token, post_text)
        log_post("Vargr Viking (intro post)" if args.intro_post else "Vargr Viking", post_text)

        # --intro-post seeds last_checked here too - the one auto-bootstrap
        # moment, safe only because this run is explicit and manual.
        track.update(updated_intro_state)
        track["last_checked_date"] = now_iso
        track["last_checked_sha"] = latest_sha
        save_posted(posted)
        print("Posted successfully.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"::error::post.py failed: {exc}", file=sys.stderr)
        raise
