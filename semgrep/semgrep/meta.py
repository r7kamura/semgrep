import json
import os
import subprocess
import urllib.parse
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from textwrap import dedent
from typing import Any
from typing import Dict
from typing import Optional
from typing import Sequence

from boltons.cacheutils import cachedproperty
from glom import glom
from glom import T
from glom.core import TType

from semgrep import __VERSION__
from semgrep.error import SemgrepError
from semgrep.git import GIT_SH_TIMEOUT
from semgrep.verbose_logging import getLogger

logger = getLogger(__name__)


def git_check_output(command: Sequence[str]) -> str:
    try:
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use.dangerous-subprocess-use
        return subprocess.check_output(
            command, stderr=subprocess.PIPE, encoding="utf-8", timeout=GIT_SH_TIMEOUT
        ).strip()
    except subprocess.CalledProcessError:
        command_str = " ".join(command)
        raise SemgrepError(
            dedent(
                f"""
                Failed to run '{command_str}'. Possible reasons:

                - the git binary is not available
                - the current working directory is not a git repository
                - the current working directory is not marked as safe
                    (fix with `git config --global --add safe.directory $(pwd)`)

                Try running the command yourself to debug the issue.
                """
            ).strip()
        )


@dataclass
class GitMeta:
    """Gather metadata only from local filesystem."""

    cli_baseline_ref: Optional[str] = None
    environment: str = field(default="git", init=False)

    @property
    def event_name(self) -> str:
        if self.pr_id:
            return "pull_request"
        return "unknown"

    @property
    def repo_name(self) -> str:
        repo_name = os.getenv("SEMGREP_REPO_NAME")
        if repo_name:
            return repo_name

        rev_parse = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            encoding="utf-8",
            timeout=GIT_SH_TIMEOUT,
        )
        if rev_parse.returncode != 0:
            raise Exception(
                "Unable to infer repo_name. Set SEMGREP_REPO_NAME environment variable or run in a valid git project"
            )

        repo_root_str = rev_parse.stdout.strip()
        return str(os.path.basename(repo_root_str))

    @property
    def repo_url(self) -> Optional[str]:
        return os.getenv("SEMGREP_REPO_URL")

    @property
    def commit_sha(self) -> Optional[str]:
        """
        Read commit hash of head from env var or run `git rev-parse HEAD`
        """
        commit = os.getenv("SEMGREP_COMMIT")
        if commit:
            return commit

        return git_check_output(["git", "rev-parse", "HEAD"])

    @property
    def head_ref(self) -> Optional[str]:
        return None

    @cachedproperty
    def base_commit_ref(self) -> Optional[str]:
        return self.cli_baseline_ref

    @property
    def ci_job_url(self) -> Optional[str]:
        return os.getenv("SEMGREP_JOB_URL")

    @property
    def pr_id(self) -> Optional[str]:
        return os.getenv("SEMGREP_PR_ID")

    @property
    def pr_title(self) -> Optional[str]:
        return os.getenv("SEMGREP_PR_TITLE")

    @property
    def branch(self) -> Optional[str]:
        branch = os.getenv("SEMGREP_BRANCH")
        if branch:
            return branch

        try:
            return git_check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        except Exception as e:
            logger.debug(f"Could not get branch name using git: {e}")
            return None

    def initialize_repo(self) -> None:
        return

    @property
    def commit_datetime(self) -> str:
        """
        Returns epoch time as str of head commit
        """
        return git_check_output(["git", "show", "-s", "--format=%ct"])

    def to_dict(self) -> Dict[str, Any]:
        commit_title = git_check_output(["git", "show", "-s", "--format=%B"])
        commit_author_email = git_check_output(["git", "show", "-s", "--format=%ae"])
        commit_author_name = git_check_output(["git", "show", "-s", "--format=%an"])

        return {
            "semgrep_version": __VERSION__,
            # REQUIRED for semgrep-app backend
            "repository": self.repo_name,
            #  OPTIONAL for semgrep-app backend
            "repo_url": self.repo_url,
            "branch": self.branch,
            "ci_job_url": self.ci_job_url,
            "commit": self.commit_sha,
            "commit_author_email": commit_author_email,
            "commit_author_name": commit_author_name,
            "commit_author_username": None,
            "commit_author_image_url": None,
            "commit_title": commit_title,
            "on": self.event_name,
            "pull_request_author_username": None,
            "pull_request_author_image_url": None,
            "pull_request_id": self.pr_id,
            "pull_request_title": self.pr_title,
            "scan_environment": self.environment,
            "is_full_scan": self.base_commit_ref == None,
        }


@dataclass
class GithubMeta(GitMeta):
    """Gather metadata from GitHub Actions."""

    environment: str = field(default="github-actions", init=False)
    # the last attempt will be 4**10 == 1048576 commits
    MAX_FETCH_ATTEMPT_COUNT: int = field(default=10, init=False)

    def glom_event(self, spec: TType) -> Any:
        return glom(self.event, spec, default=None)

    @property
    def event(self) -> Dict[str, Any]:
        value = os.getenv("GITHUB_EVENT_PATH")
        if value:
            return json.loads(Path(value).read_text())  # type: ignore
        return {}

    @property
    def is_pull_request_event(self) -> bool:
        """Return if running on a PR, even for variant types such as `pull_request_target`."""
        return self.event_name in {"pull_request", "pull_request_target"}

    @property
    def repo_name(self) -> str:
        repo_name = os.getenv("GITHUB_REPOSITORY")
        if repo_name:
            return repo_name
        else:
            raise Exception("Could not get repo_name when running in GithubAction")

    @property
    def repo_url(self) -> Optional[str]:
        server_url = os.getenv("GITHUB_SERVER_URL", "https://github.com")

        if self.repo_name:
            return f"{server_url}/{self.repo_name}"
        return None

    @property
    def commit_sha(self) -> Optional[str]:
        if self.is_pull_request_event:
            # https://github.community/t/github-sha-not-the-same-as-the-triggering-commit/18286/2
            return self.glom_event(T["pull_request"]["head"]["sha"])  # type: ignore
        if self.event_name == "push":
            return os.getenv("GITHUB_SHA")
        return super().commit_sha

    @property
    def head_ref(self) -> Optional[str]:
        if self.is_pull_request_event:
            return self.commit_sha
        else:
            return None

    @property
    def base_branch_tip(self) -> Optional[str]:
        return self.glom_event(T["pull_request"]["base"]["sha"])  # type: ignore

    def _find_branchoff_point(self, attempt_count: int = 0) -> str:
        """
        GithubActions is a shallow clone and the "base" that github sends
        is not the merge base. We must fetch and get the merge-base ourselves
        """
        # Should only be called if head_ref is defined
        assert self.head_ref is not None
        assert self.base_branch_tip is not None

        # fetch 0, 4, 16, 64, 256, 1024, ...
        fetch_depth = 4**attempt_count if attempt_count else 0
        if attempt_count > self.MAX_FETCH_ATTEMPT_COUNT:  # get all commits on last try
            fetch_depth = 2**31 - 1  # git expects a signed 32-bit integer

        logger.debug(
            f"Attempting to find merge base, attempt_count={attempt_count}, fetch_depth={fetch_depth}"
        )

        if attempt_count:
            process = subprocess.run(
                [
                    "git",
                    "fetch",
                    "origin",
                    "--depth",
                    str(fetch_depth),
                    self.base_branch_tip,
                ],
                check=True,
                capture_output=True,
                encoding="utf-8",
                timeout=GIT_SH_TIMEOUT,
            )
            logger.debug(
                f"Base branch fetch: args={process.args}, stdout={process.stdout}, stderr={process.stderr}"
            )
            process = subprocess.run(
                ["git", "fetch", "origin", "--depth", str(fetch_depth), self.head_ref],
                check=True,
                encoding="utf-8",
                capture_output=True,
                timeout=GIT_SH_TIMEOUT,
            )
            logger.debug(
                f"Head branch fetch: args={process.args}, stdout={process.stdout}, stderr={process.stderr}"
            )

        try:  # check if both branches connect to the yet-unknown branch-off point now
            process = subprocess.run(
                ["git", "merge-base", self.base_branch_tip, self.head_ref],
                encoding="utf-8",
                capture_output=True,
                check=True,
                timeout=GIT_SH_TIMEOUT,
            )
        except subprocess.CalledProcessError as e:
            output = e.stderr.strip()
            if (
                output  # output is empty when unable to find branch-off point
                and "Not a valid " not in output  # the error when a ref is missing
            ):
                raise Exception(f"Unexpected git merge-base error message: ({output})")

            if attempt_count >= self.MAX_FETCH_ATTEMPT_COUNT:
                raise Exception(
                    f"Could not find branch-off point between the baseline tip {self.base_branch_tip} and current head '{self.head_ref}' "
                )

            return self._find_branchoff_point(attempt_count + 1)
        else:
            logger.debug(
                f"Found merge base: args={process.args}, stdout={process.stdout}, stderr={process.stderr}"
            )
            return process.stdout.strip()

    @cachedproperty
    def base_commit_ref(self) -> Optional[str]:
        if self.cli_baseline_ref:
            return self.cli_baseline_ref
        if self.is_pull_request_event and self.head_ref is not None:
            return self._find_branchoff_point()
        return None

    @property
    def ci_job_url(self) -> Optional[str]:
        value = os.getenv("GITHUB_RUN_ID")
        if self.repo_url and value:
            return f"{self.repo_url}/actions/runs/{value}"
        return None

    @property
    def event_name(self) -> str:
        return os.getenv("GITHUB_EVENT_NAME", "unknown")

    @property
    def pr_id(self) -> Optional[str]:
        pr_id = self.glom_event(T["pull_request"]["number"])
        return str(pr_id) if pr_id else None

    @property
    def pr_title(self) -> Optional[str]:
        pr_title = self.glom_event(T["pull_request"]["title"])
        return str(pr_title) if pr_title else None

    @property
    def branch(self) -> Optional[str]:
        """This branch name gets used for tracking issue state over time on the backend.

        The head ref is in GITHUB_HEAD_REF and the base ref is in GITHUB_REF.

        Event name            GITHUB_HEAD_REF -> GITHUB_REF
        ---------------------------------------------------
        pull_request        - johnny-patch-1  -> refs/pulls/123/merge
        pull_request_target - johnny-patch-1  -> refs/heads/main
        push/schedule/etc.  - <unset>         -> refs/heads/main

        This code originally always sent GITHUB_REF.
        This caused obvious breakage for pull_request_target,
        so we just fixed the ref we report for that event.
        But it's more subtly wrong for pull_request events:
        what we're scanning there is still the head ref;
        we force-switch to the head ref in `fix_head_if_github_action`.
        But fixing this slight data inaccuracy would be incompatible with all existing data.
        So as of May 2022 we have not corrected it.
        """
        if self.event_name == "pull_request_target":
            return os.getenv("GITHUB_HEAD_REF")
        return os.getenv("GITHUB_REF")

    def initialize_repo(self) -> None:
        if self.is_pull_request_event and self.head_ref is not None:
            self._find_branchoff_point()
        return

    def to_dict(self) -> Dict[str, Any]:
        return {
            **super().to_dict(),
            "commit_author_username": self.glom_event(T["sender"]["login"]),
            "commit_author_image_url": self.glom_event(T["sender"]["avatar_url"]),
            "pull_request_author_username": self.glom_event(
                T["pull_request"]["user"]["login"]
            ),
            "pull_request_author_image_url": self.glom_event(
                T["pull_request"]["user"]["avatar_url"]
            ),
        }


@dataclass
class GitlabMeta(GitMeta):
    """Gather metadata from GitLab 10.0+"""

    environment: str = field(default="gitlab-ci", init=False)

    @staticmethod
    def _fetch_branch_get_merge_base(branch_name: str, head_sha: str) -> str:
        """
        Return merge base of current head and head commit in branch_name

        Use Gitlab env vars to fetch target branch
        By default gitlab pipelines do a shallow clone

        Moved out to method so tests can mock this

        Because this is mocked it is not well tested. Use caution when modifying
        """
        parts = urllib.parse.urlsplit(os.environ["CI_MERGE_REQUEST_PROJECT_URL"])
        parts = parts._replace(
            netloc=f"gitlab-ci-token:{os.environ['CI_JOB_TOKEN']}@{parts.netloc}"
        )
        url = urllib.parse.urlunsplit(parts)
        subprocess.run(
            ["git", "fetch", url, branch_name],
            check=True,
            timeout=GIT_SH_TIMEOUT,
            capture_output=True,
        )

        base_sha = subprocess.check_output(
            ["git", "merge-base", "--all", head_sha, "FETCH_HEAD"],
            encoding="utf-8",
            timeout=GIT_SH_TIMEOUT,
        ).strip()
        return base_sha

    @property
    def repo_name(self) -> str:
        return os.getenv("CI_PROJECT_PATH", "[unknown]")

    @property
    def repo_url(self) -> Optional[str]:
        return os.getenv("CI_PROJECT_URL")

    @property
    def commit_sha(self) -> Optional[str]:
        return os.getenv("CI_COMMIT_SHA")

    @property
    def commit_ref(self) -> Optional[str]:
        return os.getenv("CI_COMMIT_REF_NAME")

    @cachedproperty
    def base_commit_ref(self) -> Optional[str]:
        if self.cli_baseline_ref:
            return self.cli_baseline_ref
        target_branch = os.getenv("CI_MERGE_REQUEST_TARGET_BRANCH_NAME")
        if not target_branch:
            return None

        head_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            encoding="utf-8",
            timeout=GIT_SH_TIMEOUT,
        ).strip()
        return self._fetch_branch_get_merge_base(target_branch, head_sha)

    @property
    def ci_job_url(self) -> Optional[str]:
        return os.getenv("CI_JOB_URL")

    @property
    def event_name(self) -> str:
        gitlab_event_name = os.getenv("CI_PIPELINE_SOURCE", "unknown")
        if gitlab_event_name in ["merge_request_event", "external_pull_request_event"]:
            return "pull_request"
        return gitlab_event_name

    @property
    def pr_id(self) -> Optional[str]:
        return os.getenv("CI_MERGE_REQUEST_IID")

    @property
    def start_sha(self) -> Optional[str]:
        return os.getenv("CI_MERGE_REQUEST_DIFF_BASE_SHA")

    @property
    def pr_title(self) -> Optional[str]:
        return os.getenv("CI_MERGE_REQUEST_TITLE")

    def to_dict(self) -> Dict[str, Any]:
        return {
            **super().to_dict(),
            "branch": self.commit_ref,
            "base_sha": self.base_commit_ref,
            "start_sha": self.start_sha,
        }


@dataclass
class CircleCIMeta(GitMeta):
    """Gather metadata from Circle CI."""

    environment: str = field(default="circleci", init=False)

    @property
    def repo_name(self) -> str:
        project_name = os.getenv("CIRCLE_PROJECT_USERNAME", "")
        repo_name = os.getenv("CIRCLE_PROJECT_REPONAME", "")
        if repo_name == "" and project_name == "":
            return super().repo_name
        return f"{project_name}/{repo_name}"

    @property
    def repo_url(self) -> Optional[str]:
        return os.getenv("CIRCLE_REPOSITORY_URL")

    @property
    def branch(self) -> Optional[str]:
        return os.getenv("CIRCLE_BRANCH")

    @property
    def ci_job_url(self) -> Optional[str]:
        return os.getenv("CIRCLE_BUILD_URL")

    @property
    def commit_sha(self) -> Optional[str]:
        return os.getenv("CIRCLE_SHA1")

    @property
    def pr_id(self) -> Optional[str]:
        return os.getenv("CIRCLE_PR_NUMBER")


def get_repo_name_from_github_repo_url(url: str) -> str:
    """Pulls repository name from the url, assuming it is a GitHub repo url.
    If url can't be parsed, just returns the full url as the repo name.
    """
    # url in format https://github.com/org/reponame.git
    # and we want org/reponame
    second_to_last_slash = url.rfind("/", 0, url.rfind("/"))
    if second_to_last_slash == -1:
        return url
    # slice of beginning of string to last slash and ".git" at the end
    return url[second_to_last_slash + 1 : -4]


@dataclass
class JenkinsMeta(GitMeta):
    """Gather metadata from Jenkins CI."""

    environment: str = field(default="jenkins", init=False)

    @property
    def repo_name(self) -> str:
        """Constructs the repo name from the git url.
        This assumes that the url is in the github format.
        """
        return get_repo_name_from_github_repo_url(os.getenv("GIT_URL", ""))

    @property
    def repo_url(self) -> Optional[str]:
        return os.getenv("GIT_URL", os.getenv("GIT_URL_1"))

    @property
    def branch(self) -> Optional[str]:
        branch_or_tag = os.getenv("GIT_BRANCH", "")
        if "tags/" not in branch_or_tag:
            return branch_or_tag
        return None

    @property
    def ci_job_url(self) -> Optional[str]:
        return os.getenv("BUILD_URL")

    @property
    def commit_sha(self) -> Optional[str]:
        return os.getenv("GIT_COMMIT")


@dataclass
class BitbucketMeta(GitMeta):
    """Gather metadata from BitBucket."""

    environment: str = field(default="bitbucket", init=False)

    @property
    def repo_name(self) -> str:
        repo_name = os.getenv("BITBUCKET_REPO_FULL_NAME")
        return repo_name if repo_name else super().repo_name

    @property
    def repo_url(self) -> Optional[str]:
        return os.getenv("BITBUCKET_GIT_HTTP_ORIGIN")

    @property
    def branch(self) -> Optional[str]:
        return os.getenv("BITBUCKET_BRANCH")

    @property
    def ci_job_url(self) -> Optional[str]:
        url = "{}/addon/pipelines/home#!/results/{}".format(
            os.getenv("BITBUCKET_GIT_HTTP_ORIGIN"), os.getenv("BITBUCKET_PIPELINE_UUID")
        )
        return url

    @property
    def commit_sha(self) -> Optional[str]:
        return os.getenv("BITBUCKET_COMMIT")

    @property
    def pr_id(self) -> Optional[str]:
        return os.getenv("BITBUCKET_PR_ID")


@dataclass
class AzurePipelinesMeta(GitMeta):
    """Gather metadata from Azure pipelines.
    Pulled a lot from https://github.com/DataDog/dd-trace-py/blob/f583fec63c4392a0784b4199b0e20931f9aae9b5/ddtrace/ext/ci.py
    """

    environment: str = field(default="azure-pipelines", init=False)

    @property
    def repo_name(self) -> str:
        return get_repo_name_from_github_repo_url(self.repo_url or "")

    @property
    def repo_url(self) -> Optional[str]:
        return os.getenv("SYSTEM_PULLREQUEST_SOURCEREPOSITORYURI") or os.getenv(
            "BUILD_REPOSITORY_URI"
        )

    @property
    def branch(self) -> Optional[str]:
        branch_or_tag = (
            os.getenv("SYSTEM_PULLREQUEST_SOURCEBRANCH")
            or os.getenv("BUILD_SOURCEBRANCH")
            or os.getenv("BUILD_SOURCEBRANCHNAME")
            or ""
        )
        if "tags/" not in branch_or_tag:
            return branch_or_tag
        return None

    @property
    def ci_job_url(self) -> Optional[str]:
        if (
            os.getenv("SYSTEM_TEAMFOUNDATIONSERVERURI")
            and os.getenv("SYSTEM_TEAMPROJECTID")
            and os.getenv("BUILD_BUILDID")
        ):
            base_url = "{}{}/_build/results?buildId={}".format(
                os.getenv("SYSTEM_TEAMFOUNDATIONSERVERURI"),
                os.getenv("SYSTEM_TEAMPROJECTID"),
                os.getenv("BUILD_BUILDID"),
            )
            return base_url + "&view=logs&j={}&t={}".format(
                os.getenv("SYSTEM_JOBID"), os.getenv("SYSTEM_TASKINSTANCEID")
            )
        return None

    @property
    def commit_sha(self) -> Optional[str]:
        return os.getenv("SYSTEM_PULLREQUEST_SOURCECOMMITID") or os.getenv(
            "BUILD_SOURCEVERSION"
        )


@dataclass
class BuildkiteMeta(GitMeta):
    """Gather metadata from Buildkite."""

    environment: str = field(default="buildkite", init=False)

    @property
    def repo_name(self) -> str:
        return get_repo_name_from_github_repo_url(
            os.getenv("BUILDKITE_PULL_REQUEST_REPO", "")
        )

    @property
    def repo_url(self) -> Optional[str]:
        return os.getenv("BUILDKITE_PULL_REQUEST_REPO")

    @property
    def branch(self) -> Optional[str]:
        return os.getenv("BUILDKITE_BRANCH")

    @property
    def ci_job_url(self) -> Optional[str]:
        return "{}#{}".format(
            os.getenv("BUILDKITE_BUILD_URL"), os.getenv("BUILDKITE_JOB_ID")
        )

    @property
    def commit_sha(self) -> Optional[str]:
        return os.getenv("BUILDKITE_COMMIT")

    @property
    def pr_id(self) -> Optional[str]:
        return os.getenv("BUILDKITE_PULL_REQUEST")

    def to_dict(self) -> Dict[str, Any]:
        return {
            **super().to_dict(),
            "commit_author_email": os.getenv("BUILDKITE_BUILD_AUTHOR"),
            "commit_author_name": os.getenv("BUILDKITE_BUILD_AUTHOR_EMAIL"),
            "commit_title": os.getenv("BUILDKITE_MESSAGE"),
        }


@dataclass
class TravisMeta(GitMeta):
    """Gather metadata from Travis CI."""

    environment: str = field(default="travis-ci", init=False)

    @property
    def repo_name(self) -> str:
        repo_name = os.getenv("TRAVIS_REPO_SLUG")
        return repo_name if repo_name else super().repo_name

    @property
    def repo_url(self) -> Optional[str]:
        return f"https://github.com/{self.repo_name}.git"

    @property
    def branch(self) -> Optional[str]:
        return os.getenv("TRAVIS_PULL_REQUEST_BRANCH") or os.getenv("TRAVIS_BRANCH")

    @property
    def ci_job_url(self) -> Optional[str]:
        return os.getenv("TRAVIS_JOB_WEB_URL")

    @property
    def commit_sha(self) -> Optional[str]:
        return os.getenv("TRAVIS_COMMIT")

    @property
    def pr_id(self) -> Optional[str]:
        return os.getenv("TRAVIS_PULL_REQUEST")

    def to_dict(self) -> Dict[str, Any]:
        return {**super().to_dict(), "commit_title": os.getenv("TRAVIS_COMMIT_MESSAGE")}


def generate_meta_from_environment(baseline_ref: Optional[str]) -> GitMeta:
    # https://help.github.com/en/actions/configuring-and-managing-workflows/using-environment-variables
    if os.getenv("GITHUB_ACTIONS") == "true":
        return GithubMeta(baseline_ref)

    # https://docs.gitlab.com/ee/ci/variables/predefined_variables.html
    elif os.getenv("GITLAB_CI") == "true":
        return GitlabMeta(baseline_ref)

    # https://circleci.com/docs/2.0/env-vars/#built-in-environment-variables
    elif os.getenv("CIRCLECI") == "true":
        return CircleCIMeta(baseline_ref)

    # https://e.printstacktrace.blog/jenkins-pipeline-environment-variables-the-definitive-guide/
    elif os.getenv("JENKINS_URL") is not None:
        return JenkinsMeta(baseline_ref)

    # https://support.atlassian.com/bitbucket-cloud/docs/variables-and-secrets/
    elif os.getenv("BITBUCKET_BUILD_NUMBER") is not None:
        return BitbucketMeta(baseline_ref)

    # https://github.com/DataDog/dd-trace-py/blob/f583fec63c4392a0784b4199b0e20931f9aae9b5/ddtrace/ext/ci.py#L90
    # picked an env var that is only defined by Azure Pipelines
    elif os.getenv("BUILD_BUILDID") is not None:
        return AzurePipelinesMeta(baseline_ref)

    # https://buildkite.com/docs/pipelines/environment-variables#bk-env-vars-buildkite-build-author-email
    elif os.getenv("BUILDKITE") == "true":
        return BuildkiteMeta(baseline_ref)

    # https://docs.travis-ci.com/user/environment-variables/
    elif os.getenv("TRAVIS") == "true":
        return TravisMeta(baseline_ref)

    else:
        return GitMeta(baseline_ref)
