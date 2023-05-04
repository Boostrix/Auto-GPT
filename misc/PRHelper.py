import requests
import json
import hashlib
import os

from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Tuple

import concurrent.futures
import argparse

#####
# NOTE: For the time being this is just a rough prototype, it's not even a proper class.
# So, if this is to be used as a plugin inside Auto-GPT one day, there are some things that need to happen first.
# For now, the focus was on tinkering with the original idea.
#
# If we're going to play with this, we should probably split up the whole thing and come up with a helper Class to manage PRs,
# as was suggested in the original discussion.
#
# Also, JSON caching should be supported, too. 
# The plugin would then register a few command (categories really) to allow the agent to fetch PR/issue data in JSON data from github
# the project/URL can be easily made configurable already, so people could tinker with different repos.
# For now, the whole thing will remain strictly read-only, to  help with identifying interesting PRs/issues.
# At some point, this could be changed to also provide a feature to automatically comment on PRs/issues, for instance in order to inform people about overlapping PRs
#
# Obviously, as some folks mentioned in the original issue, the holy grail would be getting Auto-GPT to use this plugin to tackle feature requests and some up with PRs on its own
# Initially, these could be focused on non-code "contributions", i.e. README files, docs or comments (docstrings)
# The plugin could also be used to help review/summarize and label/classify new issues
#
# if you'd like to get involved, feel free to reach out


cache_time_seconds = 3600  # TODO: Cache time in seconds, set to one hour by default

# Set up the API request headers and parameters
headers = {
        "Accept": "application/vnd.github.v3+json"
}


def compute_heuristics(prs, session, headers, max_file_conflicts):
    """
    Computes heuristics for the given list of PRs that have not modified the same files/paths.
    Returns a sorted list of tuples, where each tuple contains the PR number, title,
    the number of modified files, and the complexity score.
    """
    pr_files = {}
    pr_files_data = {}
    result = []

    # Get the list of filenames for each PR
    for i, pr in enumerate(prs):
        pr_number = pr["number"]
        pr_title = pr["title"]
        pr_files_url = pr["url"] + "/files"
        pr_files_response = session.get(pr_files_url, headers=headers)
        pr_files[pr_number] = set(f["filename"] for f in pr_files_response.json())
        pr_files_data[pr_number] = pr_files_response.json()

        # Show progress indicator
        progress = (i + 1) / len(prs)
        print(f"Fetching PR files... {progress:.0%} for {len(prs)} PRs", end="\r")

    # Compute the complexity score for each PR
    for i, (pr1_number, pr1_files) in enumerate(pr_files.items()):
        # Determine whether the PR is unique based on max_file_conflicts
        num_file_conflicts = 0
        for pr2_number, pr2_files in pr_files.items():
            if pr1_number != pr2_number:
                if pr1_files.intersection(pr2_files):
                    num_file_conflicts += 1
                    if num_file_conflicts > max_file_conflicts:
                        break
        is_unique = num_file_conflicts <= max_file_conflicts

        if is_unique:
            pr1_title = next(pr["title"] for pr in prs if pr["number"] == pr1_number)
            pr1_num_modified_files = len(pr1_files)
            pr1_num_modified_lines = sum(f["changes"] for f in pr_files_data[pr1_number] if f["filename"] in pr1_files)
            pr1_complexity = pr1_num_modified_files * pr1_num_modified_lines
            result.append((pr1_number, pr1_title, pr1_num_modified_files, pr1_complexity))

        # Show progress indicator
        progress = (i + 1) / len(pr_files)
        print(f"Computing complexity score... {progress:.0%}", end="\r")

    print(" " * 50, end="\r")  # Clear progress indicator
    return sorted(result, key=lambda x: (x[2], x[3]))


def main():
    parser = argparse.ArgumentParser(description="Fetch PR data and compute heuristics for simple PRs")
    parser.add_argument("--project-id", type=str, help="GitHub project ID", default="Auto-GPT")

    parser.add_argument("--url", help="GitHub API URL", default="https://api.github.com/repos/Significant-Gravitas/Auto-GPT/pulls?q=is%3Apr+is%3Aopen+-is%3Aconflict")
    parser.add_argument("--per-page", help="params per page", default=100)
    parser.add_argument("--cache-time-sec", help="cache time in seconds", default=3600)
    parser.add_argument("--max-mutual-pr-conflicts", help="max number of files touched by other PRs (default:0)", default=0)

    args = parser.parse_args()

    params = {"per_page": args.per_page}

    id = args.project_id
    filename = f"{id}.json"
    version = 1  # Replace with the current version number of the JSON file format
    token_env_var = "GITHUB_ACCESS_TOKEN"
    token_file = "github.token"
   
    # Set up the proxy settings
    http_proxy = os.environ.get("HTTP_PROXY")
    https_proxy = os.environ.get("HTTPS_PROXY")
    proxies = {
        "http": http_proxy,
        "https": https_proxy
    }

    # Add the proxy settings to the requests library
    session = requests.Session()
    session.proxies.update(proxies)

    # Read the github access token from an environment variable or file
    if token_env_var in os.environ:
        access_token = os.environ[token_env_var]
    elif os.path.isfile(token_file):
        with open(token_file, "r") as f:
            access_token = f.read().strip()
    else:
        access_token = None
        print(f"Warning: GitHub access token not found in environment variable {token_env_var} or file: {token_file}")
        print("Without GitHub Token, you WILL be subject to API restrictions")
        print("Set up an access token at: https://github.com/settings/tokens")

    # Add the access token to the API request headers, if available
    if access_token is not None:
        headers["Authorization"] = f"token {access_token}"
        print("Using Token based GitHub access")

    # Calculate the timestamp of the last PR data download, if available
    try:
        with open(filename, "r") as f:
            pr_data = json.load(f)
            prev_timestamp = datetime.fromisoformat(pr_data["meta"]["timestamp"])
            prev_version = pr_data["meta"]["version"]
    except (FileNotFoundError, KeyError):
        prev_timestamp = datetime.min.replace(tzinfo=timezone.utc)
        prev_version = 0

    # Check if the cached data is still valid
    cache_age = datetime.now(timezone.utc) - prev_timestamp
    use_cached_data = cache_age <= timedelta(seconds=cache_time_seconds) and prev_version == version

    if use_cached_data:
        # Output a message indicating that the previously downloaded data is being used
        print("Using previously downloaded PR data")
        # Load the previously downloaded PR data
        with open(filename, "r") as f:
            pr_data = json.load(f)
            prs = pr_data["prs"]
    else:
		# print("Downloading new PR data");
        # Send the API request to retrieve the PR data
        response = session.get(args.url, headers=headers, params=params)

        if response.status_code != 200:
            raise Exception(f"Failed to retrieve PRs (status code {response.status_code}): {response.text}")

        # Extract the PR data from the API response
        prs = response.json()
        print(f"Total number of PRs: {len(prs)}")
        
        # Write the PR data and metadata to a JSON file
        with open(filename, "w") as f:
        	json.dump({
            	"meta": {"version": version, "size": len(prs),"timestamp": datetime.now(timezone.utc).isoformat()},
            	"prs": prs
        	}, f, indent=4)

        # Output a confirmation message
        print(f"New PR data saved to {filename}")
     
    print(f"Max mututal file name based conflicts in between PRs: {args.max_mutual_pr_conflicts}") 
    # Compute heuristics for unique PRs
    unique_prs = compute_heuristics(prs, session, headers, args.max_mutual_pr_conflicts )

    print(f"\nNow showing least complex PRs first:")

    # Output the results
    for pr_number, pr_title, num_modified_files, complexity_score in unique_prs:
      print(f"PR #{pr_number}: {pr_title} ({num_modified_files} modified files, complexity score: {complexity_score})")

if __name__ == "__main__":
    main()
