# Copyright (C) 2008 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import collections
import contextlib
import functools
import http.cookiejar as cookielib
import io
import json
import multiprocessing
import netrc
import optparse
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import List, NamedTuple, Optional, Set, Tuple, Union
import urllib.error
import urllib.parse
import urllib.request
import xml.parsers.expat
import xmlrpc.client


try:
    import threading as _threading
except ImportError:
    import dummy_threading as _threading

try:
    import resource

    def _rlimit_nofile():
        return resource.getrlimit(resource.RLIMIT_NOFILE)

except ImportError:

    def _rlimit_nofile():
        return (256, 256)


from command import Command
from command import DEFAULT_LOCAL_JOBS
from command import MirrorSafeCommand
from command import WORKER_BATCH_SIZE
from error import GitError
from error import RepoChangedException
from error import RepoError
from error import RepoExitError
from error import RepoUnhandledExceptionError
from error import SyncError
from error import UpdateManifestError
import event_log
from git_command import git_require
from git_config import GetUrlCookieFile
from git_refs import HEAD
from git_refs import R_HEADS
import git_superproject
import platform_utils
from progress import elapsed_str
from progress import jobs_str
from progress import Progress
from project import DeleteWorktreeError
from project import Project
from project import RemoteSpec
from project import SyncBuffer
from repo_logging import RepoLogger
from repo_trace import Trace
import ssh
from wrapper import Wrapper


_ONE_DAY_S = 24 * 60 * 60

_REPO_ALLOW_SHALLOW = os.environ.get("REPO_ALLOW_SHALLOW")

logger = RepoLogger(__file__)


def _SafeCheckoutOrder(checkouts: List[Project]) -> List[List[Project]]:
    """Generate a sequence of checkouts that is safe to perform. The client
    should checkout everything from n-th index before moving to n+1.

    This is only useful if manifest contains nested projects.

    E.g. if foo, foo/bar and foo/bar/baz are project paths, then foo needs to
    finish before foo/bar can proceed, and foo/bar needs to finish before
    foo/bar/baz."""
    res = [[]]
    current = res[0]

    # depth_stack contains a current stack of parent paths.
    depth_stack = []
    # Checkouts are iterated in the hierarchical order. That way, it can easily
    # be determined if the previous checkout is parent of the current checkout.
    # We are splitting by the path separator so the final result is
    # hierarchical, and not just lexicographical. For example, if the projects
    # are: foo, foo/bar, foo-bar, lexicographical order produces foo, foo-bar
    # and foo/bar, which doesn't work.
    for checkout in sorted(checkouts, key=lambda x: x.relpath.split("/")):
        checkout_path = Path(checkout.relpath)
        while depth_stack:
            try:
                checkout_path.relative_to(depth_stack[-1])
            except ValueError:
                # Path.relative_to returns ValueError if paths are not relative.
                # TODO(sokcevic): Switch to is_relative_to once min supported
                # version is py3.9.
                depth_stack.pop()
            else:
                if len(depth_stack) >= len(res):
                    # Another depth created.
                    res.append([])
                break

        current = res[len(depth_stack)]
        current.append(checkout)
        depth_stack.append(checkout_path)

    return res


def _chunksize(projects: int, jobs: int) -> int:
    """Calculate chunk size for the given number of projects and jobs."""
    return min(max(1, projects // jobs), WORKER_BATCH_SIZE)


class _FetchOneResult(NamedTuple):
    """_FetchOne return value.

    Attributes:
      success (bool): True if successful.
      project_idx (int): The fetched project index.
      start (float): The starting time.time().
      finish (float): The ending time.time().
      remote_fetched (bool): True if the remote was actually queried.
    """

    success: bool
    errors: List[Exception]
    project_idx: int
    start: float
    finish: float
    remote_fetched: bool


class _FetchResult(NamedTuple):
    """_Fetch return value.

    Attributes:
      success (bool): True if successful.
      projects (Set[str]): The names of the git directories of fetched projects.
    """

    success: bool
    projects: Set[str]


class _FetchMainResult(NamedTuple):
    """_FetchMain return value.

    Attributes:
      all_projects (List[Project]): The fetched projects.
    """

    all_projects: List[Project]


class _CheckoutOneResult(NamedTuple):
    """_CheckoutOne return value.

    Attributes:
      success (bool): True if successful.
      project_idx (int): The project index.
      start (float): The starting time.time().
      finish (float): The ending time.time().
    """

    success: bool
    errors: List[Exception]
    project_idx: int
    start: float
    finish: float


class _SyncResult(NamedTuple):
    """Individual project sync result for interleaved mode.

    Attributes:
      project_index (int): The index of the project in the shared list.
      relpath (str): The project's relative path from the repo client top.
      remote_fetched (bool): True if the remote was actually queried.
      fetch_success (bool): True if the fetch operation was successful.
      fetch_error (Optional[Exception]): The Exception from a failed fetch,
          or None.
      fetch_start (Optional[float]): The time.time() when fetch started.
      fetch_finish (Optional[float]): The time.time() when fetch finished.
      checkout_success (bool): True if the checkout operation was
          successful.
      checkout_error (Optional[Exception]): The Exception from a failed
          checkout, or None.
      checkout_start (Optional[float]): The time.time() when checkout
          started.
      checkout_finish (Optional[float]): The time.time() when checkout
          finished.
      stderr_text (str): The combined output from both fetch and checkout.
    """

    project_index: int
    relpath: str

    remote_fetched: bool
    fetch_success: bool
    fetch_error: Optional[Exception]
    fetch_start: Optional[float]
    fetch_finish: Optional[float]

    checkout_success: bool
    checkout_error: Optional[Exception]
    checkout_start: Optional[float]
    checkout_finish: Optional[float]

    stderr_text: str


class _InterleavedSyncResult(NamedTuple):
    """Result of an interleaved sync.

    Attributes:
      results (List[_SyncResult]): A list of results, one for each project
          processed. Empty if the worker failed before creating results.
    """

    results: List[_SyncResult]


class SuperprojectError(SyncError):
    """Superproject sync repo."""


class SyncFailFastError(SyncError):
    """Sync exit error when --fail-fast set."""


class SmartSyncError(SyncError):
    """Smart sync exit error."""


class ManifestInterruptError(RepoError):
    """Aggregate Error to be logged when a user interrupts a manifest update."""

    def __init__(self, output, **kwargs):
        super().__init__(output, **kwargs)
        self.output = output

    def __str__(self):
        error_type = type(self).__name__
        return f"{error_type}:{self.output}"


class TeeStringIO(io.StringIO):
    """StringIO class that can write to an additional destination."""

    def __init__(
        self, io: Union[io.TextIOWrapper, None], *args, **kwargs
    ) -> None:
        super().__init__(*args, **kwargs)
        self.io = io

    def write(self, s: str) -> int:
        """Write to additional destination."""
        ret = super().write(s)
        if self.io is not None:
            self.io.write(s)
        return ret


class Sync(Command, MirrorSafeCommand):
    COMMON = True
    MULTI_MANIFEST_SUPPORT = True
    helpSummary = "Update working tree to the latest revision"
    helpUsage = """
%prog [<project>...]
"""
    helpDescription = """
The '%prog' command synchronizes local project directories
with the remote repositories specified in the manifest.  If a local
project does not yet exist, it will clone a new local directory from
the remote repository and set up tracking branches as specified in
the manifest.  If the local project already exists, '%prog'
will update the remote branches and rebase any new local changes
on top of the new remote changes.

'%prog' will synchronize all projects listed at the command
line.  Projects can be specified either by name, or by a relative
or absolute path to the project's local directory. If no projects
are specified, '%prog' will synchronize all projects listed in
the manifest.

The -d/--detach option can be used to switch specified projects
back to the manifest revision.  This option is especially helpful
if the project is currently on a topic branch, but the manifest
revision is temporarily needed.

The -s/--smart-sync option can be used to sync to a known good
build as specified by the manifest-server element in the current
manifest. The -t/--smart-tag option is similar and allows you to
specify a custom tag/label.

The -u/--manifest-server-username and -p/--manifest-server-password
options can be used to specify a username and password to authenticate
with the manifest server when using the -s or -t option.

If -u and -p are not specified when using the -s or -t option, '%prog'
will attempt to read authentication credentials for the manifest server
from the user's .netrc file.

'%prog' will not use authentication credentials from -u/-p or .netrc
if the manifest server specified in the manifest file already includes
credentials.

By default, all projects will be synced. The --fail-fast option can be used
to halt syncing as soon as possible when the first project fails to sync.

The --force-sync option can be used to overwrite existing git
directories if they have previously been linked to a different
object directory. WARNING: This may cause data to be lost since
refs may be removed when overwriting.

The --force-checkout option can be used to force git to switch revs even if the
index or the working tree differs from HEAD, and if there are untracked files.
WARNING: This may cause data to be lost since uncommitted changes may be
removed.

The --force-remove-dirty option can be used to remove previously used
projects with uncommitted changes. WARNING: This may cause data to be
lost since uncommitted changes may be removed with projects that no longer
exist in the manifest.

The --no-clone-bundle option disables any attempt to use
$URL/clone.bundle to bootstrap a new Git repository from a
resumeable bundle file on a content delivery network. This
may be necessary if there are problems with the local Python
HTTP client or proxy configuration, but the Git binary works.

The --fetch-submodules option enables fetching Git submodules
of a project from server.

The -c/--current-branch option can be used to only fetch objects that
are on the branch specified by a project's revision.

The --optimized-fetch option can be used to only fetch projects that
are fixed to a sha1 revision if the sha1 revision does not already
exist locally.

The --prune option can be used to remove any refs that no longer
exist on the remote.

The --auto-gc option can be used to trigger garbage collection on all
projects. By default, repo does not run garbage collection.

# SSH Connections

If at least one project remote URL uses an SSH connection (ssh://,
git+ssh://, or user@host:path syntax) repo will automatically
enable the SSH ControlMaster option when connecting to that host.
This feature permits other projects in the same '%prog' session to
reuse the same SSH tunnel, saving connection setup overheads.

To disable this behavior on UNIX platforms, set the GIT_SSH
environment variable to 'ssh'.  For example:

  export GIT_SSH=ssh
  %prog

# Compatibility

This feature is automatically disabled on Windows, due to the lack
of UNIX domain socket support.

This feature is not compatible with url.insteadof rewrites in the
user's ~/.gitconfig.  '%prog' is currently not able to perform the
rewrite early enough to establish the ControlMaster tunnel.

If the remote SSH daemon is Gerrit Code Review, version 2.0.10 or
later is required to fix a server side protocol bug.

"""
    # A value of 0 means we want parallel jobs, but we'll determine the default
    # value later on.
    PARALLEL_JOBS = 0

    _JOBS_WARN_THRESHOLD = 100

    def _Options(self, p, show_smart=True):
        p.add_option(
            "--jobs-network",
            default=None,
            type=int,
            metavar="JOBS",
            help="number of network jobs to run in parallel (defaults to "
            "--jobs or 1). Ignored when --interleaved is set",
        )
        p.add_option(
            "--jobs-checkout",
            default=None,
            type=int,
            metavar="JOBS",
            help="number of local checkout jobs to run in parallel (defaults "
            f"to --jobs or {DEFAULT_LOCAL_JOBS}). Ignored when --interleaved "
            "is set",
        )

        p.add_option(
            "-f",
            "--force-broken",
            action="store_true",
            help="obsolete option (to be deleted in the future)",
        )
        p.add_option(
            "--fail-fast",
            action="store_true",
            help="stop syncing after first error is hit",
        )
        p.add_option(
            "--force-sync",
            action="store_true",
            help="overwrite an existing git directory if it needs to "
            "point to a different object directory. WARNING: this "
            "may cause loss of data",
        )
        p.add_option(
            "--force-checkout",
            action="store_true",
            help="force checkout even if it results in throwing away "
            "uncommitted modifications. "
            "WARNING: this may cause loss of data",
        )
        p.add_option(
            "--force-remove-dirty",
            action="store_true",
            help="force remove projects with uncommitted modifications if "
            "projects no longer exist in the manifest. "
            "WARNING: this may cause loss of data",
        )
        p.add_option(
            "--rebase",
            action="store_true",
            help="rebase local commits regardless of whether they are "
            "published",
        )
        p.add_option(
            "-l",
            "--local-only",
            action="store_true",
            help="only update working tree, don't fetch",
        )
        p.add_option(
            "--no-manifest-update",
            "--nmu",
            dest="mp_update",
            action="store_false",
            default="true",
            help="use the existing manifest checkout as-is. "
            "(do not update to the latest revision)",
        )
        p.add_option(
            "--interleaved",
            action="store_true",
            help="fetch and checkout projects in parallel (experimental)",
        )
        p.add_option(
            "-n",
            "--network-only",
            action="store_true",
            help="fetch only, don't update working tree",
        )
        p.add_option(
            "-d",
            "--detach",
            dest="detach_head",
            action="store_true",
            help="detach projects back to manifest revision",
        )
        p.add_option(
            "-c",
            "--current-branch",
            dest="current_branch_only",
            action="store_true",
            help="fetch only current branch from server",
        )
        p.add_option(
            "--no-current-branch",
            dest="current_branch_only",
            action="store_false",
            help="fetch all branches from server",
        )
        p.add_option(
            "-m",
            "--manifest-name",
            help="temporary manifest to use for this sync",
            metavar="NAME.xml",
        )
        p.add_option(
            "--clone-bundle",
            action="store_true",
            help="enable use of /clone.bundle on HTTP/HTTPS",
        )
        p.add_option(
            "--no-clone-bundle",
            dest="clone_bundle",
            action="store_false",
            help="disable use of /clone.bundle on HTTP/HTTPS",
        )
        p.add_option(
            "-u",
            "--manifest-server-username",
            action="store",
            help="username to authenticate with the manifest server",
        )
        p.add_option(
            "-p",
            "--manifest-server-password",
            action="store",
            help="password to authenticate with the manifest server",
        )
        p.add_option(
            "--fetch-submodules",
            action="store_true",
            help="fetch submodules from server",
        )
        p.add_option(
            "--use-superproject",
            action="store_true",
            help="use the manifest superproject to sync projects; implies -c",
        )
        p.add_option(
            "--no-use-superproject",
            action="store_false",
            dest="use_superproject",
            help="disable use of manifest superprojects",
        )
        p.add_option("--tags", action="store_true", help="fetch tags")
        p.add_option(
            "--no-tags",
            dest="tags",
            action="store_false",
            help="don't fetch tags (default)",
        )
        p.add_option(
            "--optimized-fetch",
            action="store_true",
            help="only fetch projects fixed to sha1 if revision does not exist "
            "locally",
        )
        p.add_option(
            "--retry-fetches",
            default=0,
            action="store",
            type="int",
            help="number of times to retry fetches on transient errors",
        )
        p.add_option(
            "--prune",
            action="store_true",
            help="delete refs that no longer exist on the remote (default)",
        )
        p.add_option(
            "--no-prune",
            dest="prune",
            action="store_false",
            help="do not delete refs that no longer exist on the remote",
        )
        p.add_option(
            "--auto-gc",
            action="store_true",
            default=None,
            help="run garbage collection on all synced projects",
        )
        p.add_option(
            "--no-auto-gc",
            dest="auto_gc",
            action="store_false",
            help="do not run garbage collection on any projects (default)",
        )
        if show_smart:
            p.add_option(
                "-s",
                "--smart-sync",
                action="store_true",
                help="smart sync using manifest from the latest known good "
                "build",
            )
            p.add_option(
                "-t",
                "--smart-tag",
                action="store",
                help="smart sync using manifest from a known tag",
            )

        g = p.add_option_group("repo Version options")
        g.add_option(
            "--no-repo-verify",
            dest="repo_verify",
            default=True,
            action="store_false",
            help="do not verify repo source code",
        )
        g.add_option(
            "--repo-upgraded",
            action="store_true",
            help=optparse.SUPPRESS_HELP,
        )

    def _GetBranch(self, manifest_project):
        """Returns the branch name for getting the approved smartsync manifest.

        Args:
            manifest_project: The manifestProject to query.
        """
        b = manifest_project.GetBranch(manifest_project.CurrentBranch)
        branch = b.merge
        if branch.startswith(R_HEADS):
            branch = branch[len(R_HEADS) :]
        return branch

    @classmethod
    def _GetCurrentBranchOnly(cls, opt, manifest):
        """Returns whether current-branch or use-superproject options are
        enabled.

        Args:
            opt: Program options returned from optparse.  See _Options().
            manifest: The manifest to use.

        Returns:
            True if a superproject is requested, otherwise the value of the
            current_branch option (True, False or None).
        """
        return (
            git_superproject.UseSuperproject(opt.use_superproject, manifest)
            or opt.current_branch_only
        )

    def _UpdateProjectsRevisionId(
        self, opt, args, superproject_logging_data, manifest
    ):
        """Update revisionId of projects with the commit from the superproject.

        This function updates each project's revisionId with the commit hash
        from the superproject.  It writes the updated manifest into a file and
        reloads the manifest from it.  When appropriate, sub manifests are also
        processed.

        Args:
            opt: Program options returned from optparse.  See _Options().
            args: Arguments to pass to GetProjects. See the GetProjects
                docstring for details.
            superproject_logging_data: A dictionary of superproject data to log.
            manifest: The manifest to use.
        """
        have_superproject = manifest.superproject or any(
            m.superproject for m in manifest.all_children
        )
        if not have_superproject:
            return

        if opt.local_only and manifest.superproject:
            manifest_path = manifest.superproject.manifest_path
            if manifest_path:
                self._ReloadManifest(manifest_path, manifest)
            return

        all_projects = self.GetProjects(
            args,
            missing_ok=True,
            submodules_ok=opt.fetch_submodules,
            manifest=manifest,
            all_manifests=not opt.this_manifest_only,
        )

        per_manifest = collections.defaultdict(list)
        if opt.this_manifest_only:
            per_manifest[manifest.path_prefix] = all_projects
        else:
            for p in all_projects:
                per_manifest[p.manifest.path_prefix].append(p)

        superproject_logging_data = {}
        need_unload = False
        for m in self.ManifestList(opt):
            if m.path_prefix not in per_manifest:
                continue
            use_super = git_superproject.UseSuperproject(
                opt.use_superproject, m
            )
            if superproject_logging_data:
                superproject_logging_data["multimanifest"] = True
            superproject_logging_data.update(
                superproject=use_super,
                haslocalmanifests=bool(m.HasLocalManifests),
                hassuperprojecttag=bool(m.superproject),
            )
            if use_super and (m.IsMirror or m.IsArchive):
                # Don't use superproject, because we have no working tree.
                use_super = False
                superproject_logging_data["superproject"] = False
                superproject_logging_data["noworktree"] = True
                if opt.use_superproject is not False:
                    logger.warning(
                        "%s: not using superproject because there is no "
                        "working tree.",
                        m.path_prefix,
                    )

            if not use_super:
                continue
            m.superproject.SetQuiet(not opt.verbose)
            print_messages = git_superproject.PrintMessages(
                opt.use_superproject, m
            )
            m.superproject.SetPrintMessages(print_messages)
            update_result = m.superproject.UpdateProjectsRevisionId(
                per_manifest[m.path_prefix], git_event_log=self.git_event_log
            )
            manifest_path = update_result.manifest_path
            superproject_logging_data["updatedrevisionid"] = bool(manifest_path)
            if manifest_path:
                m.SetManifestOverride(manifest_path)
                need_unload = True
            else:
                if print_messages:
                    logger.warning(
                        "%s: warning: Update of revisionId from superproject "
                        "has failed, repo sync will not use superproject to "
                        "fetch the source. Please resync with the "
                        "--no-use-superproject option to avoid this repo "
                        "warning.",
                        m.path_prefix,
                    )
                if update_result.fatal and opt.use_superproject is not None:
                    raise SuperprojectError()
        if need_unload:
            m.outer_client.manifest.Unload()

    @classmethod
    def _FetchProjectList(cls, opt, projects):
        """Main function of the fetch worker.

        The projects we're given share the same underlying git object store, so
        we have to fetch them in serial.

        Delegates most of the work to _FetchOne.

        Args:
            opt: Program options returned from optparse.  See _Options().
            projects: Projects to fetch.
        """
        return [cls._FetchOne(opt, x) for x in projects]

    @classmethod
    def _FetchOne(cls, opt, project_idx):
        """Fetch git objects for a single project.

        Args:
            opt: Program options returned from optparse.  See _Options().
            project_idx: Project index for the project to fetch.

        Returns:
            Whether the fetch was successful.
        """
        project = cls.get_parallel_context()["projects"][project_idx]
        start = time.time()
        k = f"{project.name} @ {project.relpath}"
        cls.get_parallel_context()["sync_dict"][k] = start
        success = False
        remote_fetched = False
        errors = []
        buf = TeeStringIO(sys.stdout if opt.verbose else None)
        try:
            sync_result = project.Sync_NetworkHalf(
                quiet=opt.quiet,
                verbose=opt.verbose,
                output_redir=buf,
                current_branch_only=cls._GetCurrentBranchOnly(
                    opt, project.manifest
                ),
                force_sync=opt.force_sync,
                clone_bundle=opt.clone_bundle,
                tags=opt.tags,
                archive=project.manifest.IsArchive,
                optimized_fetch=opt.optimized_fetch,
                retry_fetches=opt.retry_fetches,
                prune=opt.prune,
                ssh_proxy=cls.get_parallel_context()["ssh_proxy"],
                clone_filter=project.manifest.CloneFilter,
                partial_clone_exclude=project.manifest.PartialCloneExclude,
                clone_filter_for_depth=project.manifest.CloneFilterForDepth,
            )
            success = sync_result.success
            remote_fetched = sync_result.remote_fetched
            if sync_result.error:
                errors.append(sync_result.error)

            output = buf.getvalue()
            if output and buf.io is None and not success:
                print("\n" + output.rstrip())

            if not success:
                logger.error(
                    "error: Cannot fetch %s from %s",
                    project.name,
                    project.remote.url,
                )
        except KeyboardInterrupt:
            logger.error("Keyboard interrupt while processing %s", project.name)
        except GitError as e:
            logger.error("error.GitError: Cannot fetch %s", e)
            errors.append(e)
        except Exception as e:
            logger.error(
                "error: Cannot fetch %s (%s: %s)",
                project.name,
                type(e).__name__,
                e,
            )
            errors.append(e)
            raise
        finally:
            del cls.get_parallel_context()["sync_dict"][k]

        finish = time.time()
        return _FetchOneResult(
            success, errors, project_idx, start, finish, remote_fetched
        )

    def _GetSyncProgressMessage(self):
        earliest_time = float("inf")
        earliest_proj = None
        items = self.get_parallel_context()["sync_dict"].items()
        for project, t in items:
            if t < earliest_time:
                earliest_time = t
                earliest_proj = project

        if not earliest_proj:
            # This function is called when sync is still running but in some
            # cases (by chance), sync_dict can contain no entries. Return some
            # text to indicate that sync is still working.
            return "..working.."

        elapsed = time.time() - earliest_time
        jobs = jobs_str(len(items))
        return f"{jobs} | {elapsed_str(elapsed)} {earliest_proj}"

    @classmethod
    def InitWorker(cls):
        # Force connect to the manager server now.
        # This is good because workers are initialized one by one. Without this,
        # multiple workers may connect to the manager when handling the first
        # job at the same time. Then the connection may fail if too many
        # connections are pending and execeeded the socket listening backlog,
        # especially on MacOS.
        len(cls.get_parallel_context()["sync_dict"])

    def _Fetch(self, projects, opt, err_event, ssh_proxy, errors):
        ret = True

        fetched = set()
        remote_fetched = set()
        pm = Progress(
            "Fetching",
            len(projects),
            delay=False,
            quiet=opt.quiet,
            show_elapsed=True,
            elide=True,
        )

        sync_event = _threading.Event()
        sync_progress_thread = self._CreateSyncProgressThread(pm, sync_event)

        def _ProcessResults(pool, pm, results_sets):
            ret = True
            for results in results_sets:
                for result in results:
                    success = result.success
                    project = projects[result.project_idx]
                    start = result.start
                    finish = result.finish
                    self._fetch_times.Set(project, finish - start)
                    self._local_sync_state.SetFetchTime(project)
                    self.event_log.AddSync(
                        project,
                        event_log.TASK_SYNC_NETWORK,
                        start,
                        finish,
                        success,
                    )
                    if result.errors:
                        errors.extend(result.errors)
                    if result.remote_fetched:
                        remote_fetched.add(project)
                    # Check for any errors before running any more tasks.
                    # ...we'll let existing jobs finish, though.
                    if not success:
                        ret = False
                    else:
                        fetched.add(project.gitdir)
                    pm.update()
                if not ret and opt.fail_fast:
                    if pool:
                        pool.close()
                    break
            return ret

        with self.ParallelContext():
            self.get_parallel_context()["projects"] = projects
            self.get_parallel_context()[
                "sync_dict"
            ] = multiprocessing.Manager().dict()

            objdir_project_map = dict()
            for index, project in enumerate(projects):
                objdir_project_map.setdefault(project.objdir, []).append(index)
            projects_list = list(objdir_project_map.values())

            jobs = max(1, min(opt.jobs_network, len(projects_list)))

            # We pass the ssh proxy settings via the class.  This allows
            # multiprocessing to pickle it up when spawning children.  We can't
            # pass it as an argument to _FetchProjectList below as
            # multiprocessing is unable to pickle those.
            self.get_parallel_context()["ssh_proxy"] = ssh_proxy

            sync_progress_thread.start()
            if not opt.quiet:
                pm.update(inc=0, msg="warming up")
            try:
                ret = self.ExecuteInParallel(
                    jobs,
                    functools.partial(self._FetchProjectList, opt),
                    projects_list,
                    callback=_ProcessResults,
                    output=pm,
                    # Use chunksize=1 to avoid the chance that some workers are
                    # idle while other workers still have more than one job in
                    # their chunk queue.
                    chunksize=1,
                    initializer=self.InitWorker,
                )
            finally:
                sync_event.set()
                sync_progress_thread.join()

        self._fetch_times.Save()
        self._local_sync_state.Save()

        if not self.outer_client.manifest.IsArchive:
            self._GCProjects(projects, opt, err_event)

        return _FetchResult(ret, fetched)

    def _FetchMain(
        self, opt, args, all_projects, err_event, ssh_proxy, manifest, errors
    ):
        """The main network fetch loop.

        Args:
            opt: Program options returned from optparse.  See _Options().
            args: Command line args used to filter out projects.
            all_projects: List of all projects that should be fetched.
            err_event: Whether an error was hit while processing.
            ssh_proxy: SSH manager for clients & masters.
            manifest: The manifest to use.

        Returns:
            List of all projects that should be checked out.
        """
        to_fetch = []
        to_fetch.extend(all_projects)
        to_fetch.sort(key=self._fetch_times.Get, reverse=True)

        result = self._Fetch(to_fetch, opt, err_event, ssh_proxy, errors)
        success = result.success
        fetched = result.projects
        if not success:
            err_event.set()

        if opt.network_only:
            # Bail out now; the rest touches the working tree.
            if err_event.is_set():
                e = SyncError(
                    "error: Exited sync due to fetch errors.",
                    aggregate_errors=errors,
                )

                logger.error(e)
                raise e
            return _FetchMainResult([])

        # Iteratively fetch missing and/or nested unregistered submodules.
        previously_missing_set = set()
        while True:
            self._ReloadManifest(None, manifest)
            all_projects = self.GetProjects(
                args,
                missing_ok=True,
                submodules_ok=opt.fetch_submodules,
                manifest=manifest,
                all_manifests=not opt.this_manifest_only,
            )
            missing = []
            for project in all_projects:
                if project.gitdir not in fetched:
                    missing.append(project)
            if not missing:
                break
            # Stop us from non-stopped fetching actually-missing repos: If set
            # of missing repos has not been changed from last fetch, we break.
            missing_set = {p.name for p in missing}
            if previously_missing_set == missing_set:
                break
            previously_missing_set = missing_set
            result = self._Fetch(missing, opt, err_event, ssh_proxy, errors)
            success = result.success
            new_fetched = result.projects
            if not success:
                err_event.set()
            fetched.update(new_fetched)

        return _FetchMainResult(all_projects)

    @classmethod
    def _CheckoutOne(
        cls,
        detach_head,
        force_sync,
        force_checkout,
        force_rebase,
        verbose,
        project_idx,
    ):
        """Checkout work tree for one project

        Args:
            detach_head: Whether to leave a detached HEAD.
            force_sync: Force checking out of .git directory (e.g. overwrite
            existing git directory that was previously linked to a different
            object directory).
            force_checkout: Force checking out of the repo content.
            force_rebase: Force rebase.
            verbose: Whether to show verbose messages.
            project_idx: Project index for the project to checkout.

        Returns:
            Whether the fetch was successful.
        """
        project = cls.get_parallel_context()["projects"][project_idx]
        start = time.time()
        syncbuf = SyncBuffer(
            project.manifest.manifestProject.config, detach_head=detach_head
        )
        success = False
        errors = []
        try:
            project.Sync_LocalHalf(
                syncbuf,
                force_sync=force_sync,
                force_checkout=force_checkout,
                force_rebase=force_rebase,
                errors=errors,
                verbose=verbose,
            )
            success = syncbuf.Finish()
        except KeyboardInterrupt:
            logger.error("Keyboard interrupt while processing %s", project.name)
        except GitError as e:
            logger.error(
                "error.GitError: Cannot checkout %s: %s", project.name, e
            )
            errors.append(e)
        except Exception as e:
            logger.error(
                "error: Cannot checkout %s: %s: %s",
                project.name,
                type(e).__name__,
                e,
            )
            raise

        if not success:
            logger.error("error: Cannot checkout %s", project.name)
        finish = time.time()
        return _CheckoutOneResult(success, errors, project_idx, start, finish)

    def _Checkout(self, all_projects, opt, err_results, checkout_errors):
        """Checkout projects listed in all_projects

        Args:
            all_projects: List of all projects that should be checked out.
            opt: Program options returned from optparse.  See _Options().
            err_results: A list of strings, paths to git repos where checkout
                failed.
        """
        # Only checkout projects with worktrees.
        all_projects = [x for x in all_projects if x.worktree]

        def _ProcessResults(pool, pm, results):
            ret = True
            for result in results:
                success = result.success
                project = self.get_parallel_context()["projects"][
                    result.project_idx
                ]
                start = result.start
                finish = result.finish
                self.event_log.AddSync(
                    project, event_log.TASK_SYNC_LOCAL, start, finish, success
                )

                if result.errors:
                    checkout_errors.extend(result.errors)

                # Check for any errors before running any more tasks.
                # ...we'll let existing jobs finish, though.
                if success:
                    self._local_sync_state.SetCheckoutTime(project)
                else:
                    ret = False
                    err_results.append(
                        project.RelPath(local=opt.this_manifest_only)
                    )
                    if opt.fail_fast:
                        if pool:
                            pool.close()
                        return ret
                pm.update(msg=project.name)
            return ret

        for projects in _SafeCheckoutOrder(all_projects):
            with self.ParallelContext():
                self.get_parallel_context()["projects"] = projects
                proc_res = self.ExecuteInParallel(
                    opt.jobs_checkout,
                    functools.partial(
                        self._CheckoutOne,
                        opt.detach_head,
                        opt.force_sync,
                        opt.force_checkout,
                        opt.rebase,
                        opt.verbose,
                    ),
                    range(len(projects)),
                    callback=_ProcessResults,
                    output=Progress(
                        "Checking out", len(all_projects), quiet=opt.quiet
                    ),
                    # Use chunksize=1 to avoid the chance that some workers are
                    # idle while other workers still have more than one job in
                    # their chunk queue.
                    chunksize=1,
                )

        self._local_sync_state.Save()
        return proc_res and not err_results

    def _PrintManifestNotices(self, opt):
        """Print all manifest notices, but only once."""
        printed_notices = set()
        # Print all manifest notices, but only once.
        # Sort by path_prefix to ensure consistent ordering.
        for m in sorted(self.ManifestList(opt), key=lambda x: x.path_prefix):
            if m.notice and m.notice not in printed_notices:
                print(m.notice)
                printed_notices.add(m.notice)

    @staticmethod
    def _GetPreciousObjectsState(project: Project, opt):
        """Get the preciousObjects state for the project.

        Args:
            project (Project): the project to examine, and possibly correct.
            opt (optparse.Values): options given to sync.

        Returns:
            Expected state of extensions.preciousObjects:
                False: Should be disabled. (not present)
                True: Should be enabled.
        """
        if project.use_git_worktrees:
            return False
        projects = project.manifest.GetProjectsWithName(
            project.name, all_manifests=True
        )
        if len(projects) == 1:
            return False
        if len(projects) > 1:
            # Objects are potentially shared with another project.
            # See the logic in Project.Sync_NetworkHalf regarding UseAlternates.
            # - When False, shared projects share (via symlink)
            #   .repo/project-objects/{PROJECT_NAME}.git as the one-and-only
            #   objects directory.  All objects are precious, since there is no
            #   project with a complete set of refs.
            # - When True, shared projects share (via info/alternates)
            #   .repo/project-objects/{PROJECT_NAME}.git as an alternate object
            #   store, which is written only on the first clone of the project,
            #   and is not written subsequently. (When Sync_NetworkHalf sees
            #   that it exists, it makes sure that the alternates file points
            #   there, and uses a project-local .git/objects directory for all
            #   syncs going forward.
            # We do not support switching between the options.  The environment
            # variable is present for testing and migration only.
            return not project.UseAlternates

        return False

    def _SetPreciousObjectsState(self, project: Project, opt):
        """Correct the preciousObjects state for the project.

        Args:
            project: the project to examine, and possibly correct.
            opt: options given to sync.
        """
        expected = self._GetPreciousObjectsState(project, opt)
        actual = (
            project.config.GetBoolean("extensions.preciousObjects") or False
        )
        relpath = project.RelPath(local=opt.this_manifest_only)

        if expected != actual:
            # If this is unexpected, log it and repair.
            Trace(
                f"{relpath} expected preciousObjects={expected}, got {actual}"
            )
            if expected:
                if not opt.quiet:
                    print(
                        "\r%s: Shared project %s found, disabling pruning."
                        % (relpath, project.name)
                    )

                if git_require((2, 7, 0)):
                    project.EnableRepositoryExtension("preciousObjects")
                else:
                    # This isn't perfect, but it's the best we can do with old
                    # git.
                    logger.warning(
                        "%s: WARNING: shared projects are unreliable when "
                        "using old versions of git; please upgrade to "
                        "git-2.7.0+.",
                        relpath,
                    )
                    project.config.SetString("gc.pruneExpire", "never")
            else:
                project.config.SetString("extensions.preciousObjects", None)
                project.config.SetString("gc.pruneExpire", None)

    def _GCProjects(self, projects, opt, err_event):
        """Perform garbage collection.

        If We are skipping garbage collection (opt.auto_gc not set), we still
        want to potentially mark objects precious, so that `git gc` does not
        discard shared objects.
        """
        if not opt.auto_gc:
            # Just repair preciousObjects state, and return.
            for project in projects:
                self._SetPreciousObjectsState(project, opt)
            return

        pm = Progress(
            "Garbage collecting", len(projects), delay=False, quiet=opt.quiet
        )
        pm.update(inc=0, msg="prescan")

        tidy_dirs = {}
        for project in projects:
            self._SetPreciousObjectsState(project, opt)

            project.config.SetString("gc.autoDetach", "false")
            # Only call git gc once per objdir, but call pack-refs for the
            # remainder.
            if project.objdir not in tidy_dirs:
                tidy_dirs[project.objdir] = (
                    True,  # Run a full gc.
                    project.bare_git,
                )
            elif project.gitdir not in tidy_dirs:
                tidy_dirs[project.gitdir] = (
                    False,  # Do not run a full gc; just run pack-refs.
                    project.bare_git,
                )

        jobs = opt.jobs

        if jobs < 2:
            for run_gc, bare_git in tidy_dirs.values():
                pm.update(msg=bare_git._project.name)

                if run_gc:
                    bare_git.gc("--auto")
                else:
                    bare_git.pack_refs()
            pm.end()
            return

        cpu_count = os.cpu_count()
        config = {"pack.threads": cpu_count // jobs if cpu_count > jobs else 1}

        threads = set()
        sem = _threading.Semaphore(jobs)

        def tidy_up(run_gc, bare_git):
            pm.start(bare_git._project.name)
            try:
                try:
                    if run_gc:
                        bare_git.gc("--auto", config=config)
                    else:
                        bare_git.pack_refs(config=config)
                except GitError:
                    err_event.set()
                except Exception:
                    err_event.set()
                    raise
            finally:
                pm.finish(bare_git._project.name)
                sem.release()

        for run_gc, bare_git in tidy_dirs.values():
            if err_event.is_set() and opt.fail_fast:
                break
            sem.acquire()
            t = _threading.Thread(
                target=tidy_up,
                args=(
                    run_gc,
                    bare_git,
                ),
            )
            t.daemon = True
            threads.add(t)
            t.start()

        for t in threads:
            t.join()
        pm.end()

    def _UpdateRepoProject(self, opt, manifest, errors):
        """Fetch the repo project and check for updates."""
        if opt.local_only:
            return

        rp = manifest.repoProject
        now = time.time()
        # If we've fetched in the last day, don't bother fetching again.
        if (now - rp.LastFetch) < _ONE_DAY_S:
            return

        with multiprocessing.Manager() as manager:
            with ssh.ProxyManager(manager) as ssh_proxy:
                ssh_proxy.sock()
                start = time.time()
                buf = TeeStringIO(sys.stdout if opt.verbose else None)
                sync_result = rp.Sync_NetworkHalf(
                    quiet=opt.quiet,
                    verbose=opt.verbose,
                    output_redir=buf,
                    current_branch_only=self._GetCurrentBranchOnly(
                        opt, manifest
                    ),
                    force_sync=opt.force_sync,
                    clone_bundle=opt.clone_bundle,
                    tags=opt.tags,
                    archive=manifest.IsArchive,
                    optimized_fetch=opt.optimized_fetch,
                    retry_fetches=opt.retry_fetches,
                    prune=opt.prune,
                    ssh_proxy=ssh_proxy,
                    clone_filter=manifest.CloneFilter,
                    partial_clone_exclude=manifest.PartialCloneExclude,
                    clone_filter_for_depth=manifest.CloneFilterForDepth,
                )
                if sync_result.error:
                    errors.append(sync_result.error)

                finish = time.time()
                self.event_log.AddSync(
                    rp,
                    event_log.TASK_SYNC_NETWORK,
                    start,
                    finish,
                    sync_result.success,
                )
                if not sync_result.success:
                    logger.error("error: Cannot fetch repo tool %s", rp.name)
                    return

        # After fetching, check if a new version of repo is available and
        # restart. This is only done if the user hasn't explicitly disabled it.
        if os.environ.get("REPO_SKIP_SELF_UPDATE", "0") == "0":
            _PostRepoFetch(rp, opt.repo_verify)

    def _ReloadManifest(self, manifest_name, manifest):
        """Reload the manfiest from the file specified by the |manifest_name|.

        It unloads the manifest if |manifest_name| is None.

        Args:
            manifest_name: Manifest file to be reloaded.
            manifest: The manifest to use.
        """
        if manifest_name:
            # Override calls Unload already.
            manifest.Override(manifest_name)
        else:
            manifest.Unload()

    def UpdateProjectList(self, opt, manifest):
        """Update the cached projects list for |manifest|

        In a multi-manifest checkout, each manifest has its own project.list.

        Args:
            opt: Program options returned from optparse.  See _Options().
            manifest: The manifest to use.

        Returns:
            0: success
            1: failure
        """
        new_project_paths = []
        for project in self.GetProjects(
            None, missing_ok=True, manifest=manifest, all_manifests=False
        ):
            if project.relpath:
                new_project_paths.append(project.relpath)
        file_name = "project.list"
        file_path = os.path.join(manifest.subdir, file_name)
        old_project_paths = []

        if os.path.exists(file_path):
            with open(file_path) as fd:
                old_project_paths = fd.read().split("\n")
            # In reversed order, so subfolders are deleted before parent folder.
            for path in sorted(old_project_paths, reverse=True):
                if not path:
                    continue
                if path not in new_project_paths:
                    # If the path has already been deleted, we don't need to do
                    # it.
                    gitdir = os.path.join(manifest.topdir, path, ".git")
                    if os.path.exists(gitdir):
                        project = Project(
                            manifest=manifest,
                            name=path,
                            remote=RemoteSpec("origin"),
                            gitdir=gitdir,
                            objdir=gitdir,
                            use_git_worktrees=os.path.isfile(gitdir),
                            worktree=os.path.join(manifest.topdir, path),
                            relpath=path,
                            revisionExpr="HEAD",
                            revisionId=None,
                            groups=None,
                        )
                        project.DeleteWorktree(
                            verbose=opt.verbose, force=opt.force_remove_dirty
                        )

        new_project_paths.sort()
        with open(file_path, "w") as fd:
            fd.write("\n".join(new_project_paths))
            fd.write("\n")
        return 0

    def UpdateCopyLinkfileList(self, manifest):
        """Save all dests of copyfile and linkfile, and update them if needed.

        Returns:
            Whether update was successful.
        """
        new_paths = {}
        new_linkfile_paths = []
        new_copyfile_paths = []
        for project in self.GetProjects(
            None, missing_ok=True, manifest=manifest, all_manifests=False
        ):
            new_linkfile_paths.extend(x.dest for x in project.linkfiles)
            new_copyfile_paths.extend(x.dest for x in project.copyfiles)

        new_paths = {
            "linkfile": new_linkfile_paths,
            "copyfile": new_copyfile_paths,
        }

        copylinkfile_name = "copy-link-files.json"
        copylinkfile_path = os.path.join(manifest.subdir, copylinkfile_name)
        old_copylinkfile_paths = {}

        if os.path.exists(copylinkfile_path):
            with open(copylinkfile_path, "rb") as fp:
                try:
                    old_copylinkfile_paths = json.load(fp)
                except Exception:
                    logger.error(
                        "error: %s is not a json formatted file.",
                        copylinkfile_path,
                    )
                    platform_utils.remove(copylinkfile_path)
                    raise

            need_remove_files = []
            need_remove_files.extend(
                set(old_copylinkfile_paths.get("linkfile", []))
                - set(new_linkfile_paths)
            )
            need_remove_files.extend(
                set(old_copylinkfile_paths.get("copyfile", []))
                - set(new_copyfile_paths)
            )

            for need_remove_file in need_remove_files:
                # Try to remove the updated copyfile or linkfile.
                # So, if the file is not exist, nothing need to do.
                platform_utils.remove(
                    os.path.join(self.client.topdir, need_remove_file),
                    missing_ok=True,
                )

        # Create copy-link-files.json, save dest path of "copyfile" and
        # "linkfile".
        with open(copylinkfile_path, "w", encoding="utf-8") as fp:
            json.dump(new_paths, fp)
        return True

    def _SmartSyncSetup(self, opt, smart_sync_manifest_path, manifest):
        if not manifest.manifest_server:
            raise SmartSyncError(
                "error: cannot smart sync: no manifest server defined in "
                "manifest"
            )

        manifest_server = manifest.manifest_server
        if not opt.quiet:
            print("Using manifest server %s" % manifest_server)

        if "@" not in manifest_server:
            username = None
            password = None
            if opt.manifest_server_username and opt.manifest_server_password:
                username = opt.manifest_server_username
                password = opt.manifest_server_password
            else:
                try:
                    info = netrc.netrc()
                except OSError:
                    # .netrc file does not exist or could not be opened.
                    pass
                else:
                    try:
                        parse_result = urllib.parse.urlparse(manifest_server)
                        if parse_result.hostname:
                            auth = info.authenticators(parse_result.hostname)
                            if auth:
                                username, _account, password = auth
                            else:
                                logger.error(
                                    "No credentials found for %s in .netrc",
                                    parse_result.hostname,
                                )
                    except netrc.NetrcParseError as e:
                        logger.error("Error parsing .netrc file: %s", e)

            if username and password:
                manifest_server = manifest_server.replace(
                    "://", f"://{username}:{password}@", 1
                )

        transport = PersistentTransport(manifest_server)
        if manifest_server.startswith("persistent-"):
            manifest_server = manifest_server[len("persistent-") :]

        # Changes in behavior should update docs/smart-sync.md accordingly.
        try:
            server = xmlrpc.client.Server(manifest_server, transport=transport)
            if opt.smart_sync:
                branch = self._GetBranch(manifest.manifestProject)

                if "SYNC_TARGET" in os.environ:
                    target = os.environ["SYNC_TARGET"]
                    [success, manifest_str] = server.GetApprovedManifest(
                        branch, target
                    )
                elif (
                    "TARGET_PRODUCT" in os.environ
                    and "TARGET_BUILD_VARIANT" in os.environ
                    and "TARGET_RELEASE" in os.environ
                ):
                    target = "%s-%s-%s" % (
                        os.environ["TARGET_PRODUCT"],
                        os.environ["TARGET_RELEASE"],
                        os.environ["TARGET_BUILD_VARIANT"],
                    )
                    [success, manifest_str] = server.GetApprovedManifest(
                        branch, target
                    )
                elif (
                    "TARGET_PRODUCT" in os.environ
                    and "TARGET_BUILD_VARIANT" in os.environ
                ):
                    target = "%s-%s" % (
                        os.environ["TARGET_PRODUCT"],
                        os.environ["TARGET_BUILD_VARIANT"],
                    )
                    [success, manifest_str] = server.GetApprovedManifest(
                        branch, target
                    )
                else:
                    [success, manifest_str] = server.GetApprovedManifest(branch)
            else:
                assert opt.smart_tag
                [success, manifest_str] = server.GetManifest(opt.smart_tag)

            if success:
                manifest_name = os.path.basename(smart_sync_manifest_path)
                try:
                    with open(smart_sync_manifest_path, "w") as f:
                        f.write(manifest_str)
                except OSError as e:
                    raise SmartSyncError(
                        "error: cannot write manifest to %s:\n%s"
                        % (smart_sync_manifest_path, e),
                        aggregate_errors=[e],
                    )
                self._ReloadManifest(manifest_name, manifest)
            else:
                raise SmartSyncError(
                    "error: manifest server RPC call failed: %s" % manifest_str
                )
        except (OSError, xmlrpc.client.Fault) as e:
            raise SmartSyncError(
                "error: cannot connect to manifest server %s:\n%s"
                % (manifest.manifest_server, e),
                aggregate_errors=[e],
            )
        except xmlrpc.client.ProtocolError as e:
            raise SmartSyncError(
                "error: cannot connect to manifest server %s:\n%d %s"
                % (manifest.manifest_server, e.errcode, e.errmsg),
                aggregate_errors=[e],
            )

        return manifest_name

    def _UpdateAllManifestProjects(self, opt, mp, manifest_name, errors):
        """Fetch & update the local manifest project.

        After syncing the manifest project, if the manifest has any sub
        manifests, those are recursively processed.

        Args:
            opt: Program options returned from optparse.  See _Options().
            mp: the manifestProject to query.
            manifest_name: Manifest file to be reloaded.
        """
        if not mp.standalone_manifest_url:
            self._UpdateManifestProject(opt, mp, manifest_name, errors)

        if mp.manifest.submanifests:
            for submanifest in mp.manifest.submanifests.values():
                child = submanifest.repo_client.manifest
                child.manifestProject.SyncWithPossibleInit(
                    submanifest,
                    current_branch_only=self._GetCurrentBranchOnly(opt, child),
                    verbose=opt.verbose,
                    tags=opt.tags,
                    git_event_log=self.git_event_log,
                )
                self._UpdateAllManifestProjects(
                    opt, child.manifestProject, None, errors
                )

    def _UpdateManifestProject(self, opt, mp, manifest_name, errors):
        """Fetch & update the local manifest project.

        Args:
            opt: Program options returned from optparse.  See _Options().
            mp: the manifestProject to query.
            manifest_name: Manifest file to be reloaded.
        """
        if not opt.local_only:
            start = time.time()
            buf = TeeStringIO(sys.stdout)
            try:
                result = mp.Sync_NetworkHalf(
                    quiet=not opt.verbose,
                    output_redir=buf,
                    verbose=opt.verbose,
                    current_branch_only=self._GetCurrentBranchOnly(
                        opt, mp.manifest
                    ),
                    force_sync=opt.force_sync,
                    tags=opt.tags,
                    optimized_fetch=opt.optimized_fetch,
                    retry_fetches=opt.retry_fetches,
                    submodules=mp.manifest.HasSubmodules,
                    clone_filter=mp.manifest.CloneFilter,
                    partial_clone_exclude=mp.manifest.PartialCloneExclude,
                    clone_filter_for_depth=mp.manifest.CloneFilterForDepth,
                )
                if result.error:
                    errors.append(result.error)
            except KeyboardInterrupt:
                errors.append(
                    ManifestInterruptError(buf.getvalue(), project=mp.name)
                )
                raise

            finish = time.time()
            self.event_log.AddSync(
                mp, event_log.TASK_SYNC_NETWORK, start, finish, result.success
            )

        if mp.HasChanges:
            errors = []
            syncbuf = SyncBuffer(mp.config)
            start = time.time()
            mp.Sync_LocalHalf(
                syncbuf,
                submodules=mp.manifest.HasSubmodules,
                errors=errors,
                verbose=opt.verbose,
            )
            clean = syncbuf.Finish()
            self.event_log.AddSync(
                mp, event_log.TASK_SYNC_LOCAL, start, time.time(), clean
            )
            if not clean:
                raise UpdateManifestError(aggregate_errors=errors)
            self._ReloadManifest(manifest_name, mp.manifest)

    def ValidateOptions(self, opt, args):
        if opt.force_broken:
            logger.warning(
                "warning: -f/--force-broken is now the default behavior, and "
                "the options are deprecated"
            )
        if opt.network_only and opt.detach_head:
            self.OptionParser.error("cannot combine -n and -d")
        if opt.network_only and opt.local_only:
            self.OptionParser.error("cannot combine -n and -l")
        if opt.manifest_name and opt.smart_sync:
            self.OptionParser.error("cannot combine -m and -s")
        if opt.manifest_name and opt.smart_tag:
            self.OptionParser.error("cannot combine -m and -t")
        if opt.manifest_server_username or opt.manifest_server_password:
            if not (opt.smart_sync or opt.smart_tag):
                self.OptionParser.error(
                    "-u and -p may only be combined with -s or -t"
                )
            if None in [
                opt.manifest_server_username,
                opt.manifest_server_password,
            ]:
                self.OptionParser.error("both -u and -p must be given")

        if opt.prune is None:
            opt.prune = True

    def _ValidateOptionsWithManifest(self, opt, mp):
        """Like ValidateOptions, but after we've updated the manifest.

        Needed to handle sync-xxx option defaults in the manifest.

        Args:
            opt: The options to process.
            mp: The manifest project to pull defaults from.
        """
        if not opt.jobs:
            # If the user hasn't made a choice, use the manifest value.
            opt.jobs = mp.manifest.default.sync_j
        if opt.jobs:
            # If --jobs has a non-default value, propagate it as the default for
            # --jobs-xxx flags too.
            if not opt.jobs_network:
                opt.jobs_network = opt.jobs
            if not opt.jobs_checkout:
                opt.jobs_checkout = opt.jobs
        else:
            # Neither user nor manifest have made a choice, so setup defaults.
            if not opt.jobs_network:
                opt.jobs_network = 1
            if not opt.jobs_checkout:
                opt.jobs_checkout = DEFAULT_LOCAL_JOBS
            opt.jobs = os.cpu_count()

        # Try to stay under user rlimit settings.
        #
        # Since each worker requires at 3 file descriptors to run `git fetch`,
        # use that to scale down the number of jobs.  Unfortunately there isn't
        # an easy way to determine this reliably as systems change, but it was
        # last measured by hand in 2011.
        soft_limit, _ = _rlimit_nofile()
        jobs_soft_limit = max(1, (soft_limit - 5) // 3)
        opt.jobs = min(opt.jobs, jobs_soft_limit)
        opt.jobs_network = min(opt.jobs_network, jobs_soft_limit)
        opt.jobs_checkout = min(opt.jobs_checkout, jobs_soft_limit)

        # Warn once if effective job counts seem excessively high.
        # Prioritize --jobs, then --jobs-network, then --jobs-checkout.
        job_options_to_check = (
            ("--jobs", opt.jobs),
            ("--jobs-network", opt.jobs_network),
            ("--jobs-checkout", opt.jobs_checkout),
        )
        for name, value in job_options_to_check:
            if value > self._JOBS_WARN_THRESHOLD:
                logger.warning(
                    "High job count (%d > %d) specified for %s; this may "
                    "lead to excessive resource usage or diminishing returns.",
                    value,
                    self._JOBS_WARN_THRESHOLD,
                    name,
                )
                break

    def Execute(self, opt, args):
        errors = []
        try:
            self._ExecuteHelper(opt, args, errors)
        except (RepoExitError, RepoChangedException):
            raise
        except (KeyboardInterrupt, Exception) as e:
            raise RepoUnhandledExceptionError(e, aggregate_errors=errors)

    def _ExecuteHelper(self, opt, args, errors):
        manifest = self.outer_manifest
        if not opt.outer_manifest:
            manifest = self.manifest

        if opt.manifest_name:
            manifest.Override(opt.manifest_name)

        manifest_name = opt.manifest_name
        smart_sync_manifest_path = os.path.join(
            manifest.manifestProject.worktree, "smart_sync_override.xml"
        )

        if opt.clone_bundle is None:
            opt.clone_bundle = manifest.CloneBundle

        if opt.smart_sync or opt.smart_tag:
            manifest_name = self._SmartSyncSetup(
                opt, smart_sync_manifest_path, manifest
            )
        else:
            if os.path.isfile(smart_sync_manifest_path):
                try:
                    platform_utils.remove(smart_sync_manifest_path)
                except OSError as e:
                    logger.error(
                        "error: failed to remove existing smart sync override "
                        "manifest: %s",
                        e,
                    )

        rp = manifest.repoProject
        rp.PreSync()
        cb = rp.CurrentBranch
        if cb:
            base = rp.GetBranch(cb).merge
            if not base or not base.startswith("refs/heads/"):
                logger.warning(
                    "warning: repo is not tracking a remote branch, so it will "
                    "not receive updates; run `repo init --repo-rev=stable` to "
                    "fix."
                )

        for m in self.ManifestList(opt):
            if not m.manifestProject.standalone_manifest_url:
                m.manifestProject.PreSync()

        if opt.repo_upgraded:
            _PostRepoUpgrade(manifest, quiet=opt.quiet)

        mp = manifest.manifestProject

        if _REPO_ALLOW_SHALLOW is not None:
            if _REPO_ALLOW_SHALLOW == "1":
                mp.ConfigureCloneFilterForDepth(None)
            elif (
                _REPO_ALLOW_SHALLOW == "0" and mp.clone_filter_for_depth is None
            ):
                mp.ConfigureCloneFilterForDepth("blob:none")

        if opt.mp_update:
            self._UpdateAllManifestProjects(opt, mp, manifest_name, errors)
        else:
            print("Skipping update of local manifest project.")

        # Now that the manifests are up-to-date, setup options whose defaults
        # might be in the manifest.
        self._ValidateOptionsWithManifest(opt, mp)

        # Update the repo project and check for new versions of repo.
        self._UpdateRepoProject(opt, manifest, errors)

        superproject_logging_data = {}
        self._UpdateProjectsRevisionId(
            opt, args, superproject_logging_data, manifest
        )

        all_projects = self.GetProjects(
            args,
            missing_ok=True,
            submodules_ok=opt.fetch_submodules,
            manifest=manifest,
            all_manifests=not opt.this_manifest_only,
        )

        # Log the repo projects by existing and new.
        existing = [x for x in all_projects if x.Exists]
        mp.config.SetString("repo.existingprojectcount", str(len(existing)))
        mp.config.SetString(
            "repo.newprojectcount", str(len(all_projects) - len(existing))
        )

        self._fetch_times = _FetchTimes(manifest)
        self._local_sync_state = LocalSyncState(manifest)

        if opt.interleaved:
            sync_method = self._SyncInterleaved
        else:
            sync_method = self._SyncPhased

        sync_method(
            opt,
            args,
            errors,
            manifest,
            mp,
            all_projects,
            superproject_logging_data,
        )

        # Log the previous sync analysis state from the config.
        self.git_event_log.LogDataConfigEvents(
            mp.config.GetSyncAnalysisStateData(), "previous_sync_state"
        )

        # Update and log with the new sync analysis state.
        mp.config.UpdateSyncAnalysisState(opt, superproject_logging_data)
        self.git_event_log.LogDataConfigEvents(
            mp.config.GetSyncAnalysisStateData(), "current_sync_state"
        )

        self._local_sync_state.PruneRemovedProjects()
        if self._local_sync_state.IsPartiallySynced():
            logger.warning(
                "warning: Partial syncs are not supported. For the best "
                "experience, sync the entire tree."
            )

        if not opt.quiet:
            print("repo sync has finished successfully.")

    def _CreateSyncProgressThread(
        self, pm: Progress, stop_event: _threading.Event
    ) -> _threading.Thread:
        """Creates and returns a daemon thread to update a Progress object.

        The returned thread is not yet started. The thread will periodically
        update the progress bar with information from _GetSyncProgressMessage
        until the stop_event is set.

        Args:
            pm: The Progress object to update.
            stop_event: The threading.Event to signal the monitor to stop.

        Returns:
            The configured _threading.Thread object.
        """

        def _monitor_loop():
            """The target function for the monitor thread."""
            while True:
                # Update the progress bar with the current status message.
                pm.update(inc=0, msg=self._GetSyncProgressMessage())
                # Wait for 1 second or until the stop_event is set.
                if stop_event.wait(timeout=1):
                    return

        return _threading.Thread(target=_monitor_loop, daemon=True)

    def _UpdateManifestLists(
        self,
        opt: optparse.Values,
        err_event: multiprocessing.Event,
        errors: List[Exception],
    ) -> Tuple[bool, bool]:
        """Updates project lists and copy/link files for all manifests.

        Args:
            opt: Program options from optparse.
            err_event: An event to set if any error occurs.
            errors: A list to append any encountered exceptions to.

        Returns:
            A tuple (err_update_projects, err_update_linkfiles) indicating
                an error for each task.
        """
        err_update_projects = False
        err_update_linkfiles = False
        for m in self.ManifestList(opt):
            if m.IsMirror or m.IsArchive:
                continue

            try:
                self.UpdateProjectList(opt, m)
            except Exception as e:
                err_event.set()
                err_update_projects = True
                errors.append(e)
                if isinstance(e, DeleteWorktreeError):
                    errors.extend(e.aggregate_errors)
                if opt.fail_fast:
                    logger.error("error: Local checkouts *not* updated.")
                    raise SyncFailFastError(aggregate_errors=errors)

            try:
                self.UpdateCopyLinkfileList(m)
            except Exception as e:
                err_event.set()
                err_update_linkfiles = True
                errors.append(e)
                if opt.fail_fast:
                    logger.error(
                        "error: Local update copyfile or linkfile failed."
                    )
                    raise SyncFailFastError(aggregate_errors=errors)
        return err_update_projects, err_update_linkfiles

    def _ReportErrors(
        self,
        errors,
        err_network_sync=False,
        failing_network_repos=None,
        err_checkout=False,
        failing_checkout_repos=None,
        err_update_projects=False,
        err_update_linkfiles=False,
    ):
        """Logs detailed error messages and raises a SyncError."""

        def print_and_log(err_msg):
            self.git_event_log.ErrorEvent(err_msg)
            logger.error("%s", err_msg)

        print_and_log("error: Unable to fully sync the tree")
        if err_network_sync:
            print_and_log("error: Downloading network changes failed.")
            if failing_network_repos:
                logger.error(
                    "Failing repos (network):\n%s",
                    "\n".join(sorted(failing_network_repos)),
                )
        if err_update_projects:
            print_and_log("error: Updating local project lists failed.")
        if err_update_linkfiles:
            print_and_log("error: Updating copyfiles or linkfiles failed.")
        if err_checkout:
            print_and_log("error: Checking out local projects failed.")
            if failing_checkout_repos:
                logger.error(
                    "Failing repos (checkout):\n%s",
                    "\n".join(sorted(failing_checkout_repos)),
                )
        logger.error(
            'Try re-running with "-j1 --fail-fast" to exit at the first error.'
        )
        raise SyncError(aggregate_errors=errors)

    def _SyncPhased(
        self,
        opt,
        args,
        errors,
        manifest,
        mp,
        all_projects,
        superproject_logging_data,
    ):
        """Sync projects by separating network and local operations.

        This method performs sync in two distinct, sequential phases:
        1.  Network Phase: Fetches updates for all projects from their remotes.
        2.  Local Phase: Checks out the updated revisions into the local
            worktrees for all projects.

        This approach ensures that the local work-tree is not modified until
        all network operations are complete, providing a transactional-like
        safety net for the checkout state.
        """
        err_event = multiprocessing.Event()
        err_network_sync = False
        err_update_projects = False
        err_update_linkfiles = False

        if not opt.local_only:
            with multiprocessing.Manager() as manager:
                with ssh.ProxyManager(manager) as ssh_proxy:
                    # Initialize the socket dir once in the parent.
                    ssh_proxy.sock()
                    result = self._FetchMain(
                        opt,
                        args,
                        all_projects,
                        err_event,
                        ssh_proxy,
                        manifest,
                        errors,
                    )
                    all_projects = result.all_projects

            if opt.network_only:
                return

            # If we saw an error, exit with code 1 so that other scripts can
            # check.
            if err_event.is_set():
                err_network_sync = True
                if opt.fail_fast:
                    logger.error(
                        "error: Exited sync due to fetch errors.\n"
                        "Local checkouts *not* updated. Resolve network issues "
                        "& retry.\n"
                        "`repo sync -l` will update some local checkouts."
                    )
                    raise SyncFailFastError(aggregate_errors=errors)

        err_update_projects, err_update_linkfiles = self._UpdateManifestLists(
            opt,
            err_event,
            errors,
        )

        err_results = []
        # NB: We don't exit here because this is the last step.
        err_checkout = not self._Checkout(
            all_projects, opt, err_results, errors
        )
        if err_checkout:
            err_event.set()

        self._PrintManifestNotices(opt)

        # If we saw an error, exit with code 1 so that other scripts can check.
        if err_event.is_set():
            self._ReportErrors(
                errors,
                err_network_sync=err_network_sync,
                err_checkout=err_checkout,
                failing_checkout_repos=err_results,
                err_update_projects=err_update_projects,
                err_update_linkfiles=err_update_linkfiles,
            )

    @classmethod
    def _SyncOneProject(cls, opt, project_index, project) -> _SyncResult:
        """Syncs a single project for interleaved sync."""
        fetch_success = False
        remote_fetched = False
        fetch_error = None
        fetch_start = None
        fetch_finish = None
        network_output = ""

        if opt.local_only:
            fetch_success = True
        else:
            fetch_start = time.time()
            network_output_capture = io.StringIO()
            try:
                ssh_proxy = cls.get_parallel_context().get("ssh_proxy")
                sync_result = project.Sync_NetworkHalf(
                    quiet=opt.quiet,
                    verbose=opt.verbose,
                    output_redir=network_output_capture,
                    current_branch_only=cls._GetCurrentBranchOnly(
                        opt, project.manifest
                    ),
                    force_sync=opt.force_sync,
                    clone_bundle=opt.clone_bundle,
                    tags=opt.tags,
                    archive=project.manifest.IsArchive,
                    optimized_fetch=opt.optimized_fetch,
                    retry_fetches=opt.retry_fetches,
                    prune=opt.prune,
                    ssh_proxy=ssh_proxy,
                    clone_filter=project.manifest.CloneFilter,
                    partial_clone_exclude=project.manifest.PartialCloneExclude,
                    clone_filter_for_depth=project.manifest.CloneFilterForDepth,
                )
                fetch_success = sync_result.success
                remote_fetched = sync_result.remote_fetched
                fetch_error = sync_result.error
            except KeyboardInterrupt:
                logger.error(
                    "Keyboard interrupt while processing %s", project.name
                )
            except GitError as e:
                fetch_error = e
                logger.error("error.GitError: Cannot fetch %s", e)
            except Exception as e:
                fetch_error = e
                logger.error(
                    "error: Cannot fetch %s (%s: %s)",
                    project.name,
                    type(e).__name__,
                    e,
                )
            finally:
                fetch_finish = time.time()
                network_output = network_output_capture.getvalue()

        checkout_success = False
        checkout_error = None
        checkout_start = None
        checkout_finish = None
        checkout_stderr = ""

        if fetch_success and not opt.network_only:
            checkout_start = time.time()
            stderr_capture = io.StringIO()
            try:
                with contextlib.redirect_stderr(stderr_capture):
                    syncbuf = SyncBuffer(
                        project.manifest.manifestProject.config,
                        detach_head=opt.detach_head,
                    )
                    local_half_errors = []
                    project.Sync_LocalHalf(
                        syncbuf,
                        force_sync=opt.force_sync,
                        force_checkout=opt.force_checkout,
                        force_rebase=opt.rebase,
                        errors=local_half_errors,
                        verbose=opt.verbose,
                    )
                    checkout_success = syncbuf.Finish()
                    if local_half_errors:
                        checkout_error = SyncError(
                            aggregate_errors=local_half_errors
                        )
            except KeyboardInterrupt:
                logger.error(
                    "Keyboard interrupt while processing %s", project.name
                )
            except GitError as e:
                checkout_error = e
                logger.error(
                    "error.GitError: Cannot checkout %s: %s", project.name, e
                )
            except Exception as e:
                checkout_error = e
                logger.error(
                    "error: Cannot checkout %s: %s: %s",
                    project.name,
                    type(e).__name__,
                    e,
                )
            finally:
                checkout_finish = time.time()
                checkout_stderr = stderr_capture.getvalue()
        elif fetch_success:
            checkout_success = True

        # Consolidate all captured output.
        captured_parts = []
        if network_output:
            captured_parts.append(network_output)
        if checkout_stderr:
            captured_parts.append(checkout_stderr)
        stderr_text = "\n".join(captured_parts)

        return _SyncResult(
            project_index=project_index,
            relpath=project.relpath,
            fetch_success=fetch_success,
            remote_fetched=remote_fetched,
            checkout_success=checkout_success,
            fetch_error=fetch_error,
            checkout_error=checkout_error,
            stderr_text=stderr_text.strip(),
            fetch_start=fetch_start,
            fetch_finish=fetch_finish,
            checkout_start=checkout_start,
            checkout_finish=checkout_finish,
        )

    @classmethod
    def _SyncProjectList(cls, opt, project_indices) -> _InterleavedSyncResult:
        """Worker for interleaved sync.

        This function is responsible for syncing a group of projects that share
        a git object directory.

        Args:
            opt: Program options returned from optparse.  See _Options().
            project_indices: A list of indices into the projects list stored in
                the parallel context.

        Returns:
            An `_InterleavedSyncResult` containing the results for each project.
        """
        results = []
        context = cls.get_parallel_context()
        projects = context["projects"]
        sync_dict = context["sync_dict"]

        assert project_indices, "_SyncProjectList called with no indices."

        # Use the first project as the representative for the progress bar.
        first_project = projects[project_indices[0]]
        key = f"{first_project.name} @ {first_project.relpath}"
        sync_dict[key] = time.time()

        try:
            for idx in project_indices:
                project = projects[idx]
                results.append(cls._SyncOneProject(opt, idx, project))
        finally:
            del sync_dict[key]

        return _InterleavedSyncResult(results=results)

    def _ProcessSyncInterleavedResults(
        self,
        synced_relpaths: Set[str],
        err_event: _threading.Event,
        errors: List[Exception],
        opt: optparse.Values,
        pool: Optional[multiprocessing.Pool],
        pm: Progress,
        results_sets: List[_InterleavedSyncResult],
    ):
        """Callback to process results from interleaved sync workers."""
        ret = True
        projects = self.get_parallel_context()["projects"]
        for result_group in results_sets:
            for result in result_group.results:
                pm.update()
                project = projects[result.project_index]

                if opt.verbose and result.stderr_text:
                    pm.display_message(result.stderr_text)

                if result.fetch_start:
                    self._fetch_times.Set(
                        project,
                        result.fetch_finish - result.fetch_start,
                    )
                    self._local_sync_state.SetFetchTime(project)
                    self.event_log.AddSync(
                        project,
                        event_log.TASK_SYNC_NETWORK,
                        result.fetch_start,
                        result.fetch_finish,
                        result.fetch_success,
                    )
                if result.checkout_start:
                    if result.checkout_success:
                        self._local_sync_state.SetCheckoutTime(project)
                    self.event_log.AddSync(
                        project,
                        event_log.TASK_SYNC_LOCAL,
                        result.checkout_start,
                        result.checkout_finish,
                        result.checkout_success,
                    )

                if result.fetch_success and result.checkout_success:
                    synced_relpaths.add(result.relpath)
                else:
                    ret = False
                    err_event.set()
                    if result.fetch_error:
                        errors.append(result.fetch_error)
                        self._interleaved_err_network = True
                        self._interleaved_err_network_results.append(
                            result.relpath
                        )
                    if result.checkout_error:
                        errors.append(result.checkout_error)
                        self._interleaved_err_checkout = True
                        self._interleaved_err_checkout_results.append(
                            result.relpath
                        )

            if not ret and opt.fail_fast:
                if pool:
                    pool.close()
                break
        return ret

    def _SyncInterleaved(
        self,
        opt,
        args,
        errors,
        manifest,
        mp,
        all_projects,
        superproject_logging_data,
    ):
        """Sync projects by performing network and local operations in parallel.

        This method processes each project (or groups of projects that share git
        objects) independently. For each project, it performs the fetch and
        checkout operations back-to-back. These independent tasks are run in
        parallel.

        It respects two constraints for correctness:
        1.  Projects in nested directories (e.g. 'foo' and 'foo/bar') are
            processed in hierarchical order.
        2.  Projects that share git objects are processed serially to prevent
            race conditions.
        """
        # Temporary state for tracking errors in interleaved mode.
        self._interleaved_err_network = False
        self._interleaved_err_network_results = []
        self._interleaved_err_checkout = False
        self._interleaved_err_checkout_results = []

        err_event = multiprocessing.Event()
        synced_relpaths = set()
        project_list = list(all_projects)
        pm = Progress(
            "Syncing",
            len(project_list),
            delay=False,
            quiet=opt.quiet,
            show_elapsed=True,
            elide=True,
        )
        previously_pending_relpaths = set()

        sync_event = _threading.Event()
        sync_progress_thread = self._CreateSyncProgressThread(pm, sync_event)

        with multiprocessing.Manager() as manager, ssh.ProxyManager(
            manager
        ) as ssh_proxy:
            ssh_proxy.sock()
            with self.ParallelContext():
                self.get_parallel_context()["ssh_proxy"] = ssh_proxy
                # TODO(gavinmak): Use multprocessing.Queue instead of dict.
                self.get_parallel_context()[
                    "sync_dict"
                ] = multiprocessing.Manager().dict()
                sync_progress_thread.start()

                try:
                    # Outer loop for dynamic project discovery. This continues
                    # until no unsynced projects remain.
                    while True:
                        projects_to_sync = [
                            p
                            for p in project_list
                            if p.relpath not in synced_relpaths
                        ]
                        if not projects_to_sync:
                            break

                        pending_relpaths = {p.relpath for p in projects_to_sync}
                        if previously_pending_relpaths == pending_relpaths:
                            logger.error(
                                "Stall detected in interleaved sync, not all "
                                "projects could be synced."
                            )
                            err_event.set()
                            break
                        previously_pending_relpaths = pending_relpaths

                        self.get_parallel_context()[
                            "projects"
                        ] = projects_to_sync
                        project_index_map = {
                            p: i for i, p in enumerate(projects_to_sync)
                        }

                        # Inner loop to process projects in a hierarchical
                        # order. This iterates through levels of project
                        # dependencies (e.g. 'foo' then 'foo/bar'). All projects
                        # in one level can be processed in parallel, but we must
                        # wait for a level to complete before starting the next.
                        for level_projects in _SafeCheckoutOrder(
                            projects_to_sync
                        ):
                            if not level_projects:
                                continue

                            objdir_project_map = collections.defaultdict(list)
                            for p in level_projects:
                                objdir_project_map[p.objdir].append(
                                    project_index_map[p]
                                )

                            work_items = list(objdir_project_map.values())
                            if not work_items:
                                continue

                            jobs = max(1, min(opt.jobs, len(work_items)))
                            callback = functools.partial(
                                self._ProcessSyncInterleavedResults,
                                synced_relpaths,
                                err_event,
                                errors,
                                opt,
                            )
                            if not self.ExecuteInParallel(
                                jobs,
                                functools.partial(self._SyncProjectList, opt),
                                work_items,
                                callback=callback,
                                output=pm,
                                chunksize=1,
                            ):
                                err_event.set()

                            if err_event.is_set() and opt.fail_fast:
                                raise SyncFailFastError(aggregate_errors=errors)

                        self._ReloadManifest(None, manifest)
                        project_list = self.GetProjects(
                            args,
                            missing_ok=True,
                            submodules_ok=opt.fetch_submodules,
                            manifest=manifest,
                            all_manifests=not opt.this_manifest_only,
                        )
                finally:
                    sync_event.set()
                    sync_progress_thread.join()

        pm.end()

        err_update_projects, err_update_linkfiles = self._UpdateManifestLists(
            opt, err_event, errors
        )
        if not self.outer_client.manifest.IsArchive:
            self._GCProjects(project_list, opt, err_event)

        self._PrintManifestNotices(opt)
        if err_event.is_set():
            self._ReportErrors(
                errors,
                err_network_sync=self._interleaved_err_network,
                failing_network_repos=self._interleaved_err_network_results,
                err_checkout=self._interleaved_err_checkout,
                failing_checkout_repos=self._interleaved_err_checkout_results,
                err_update_projects=err_update_projects,
                err_update_linkfiles=err_update_linkfiles,
            )


def _PostRepoUpgrade(manifest, quiet=False):
    # Link the docs for the internal .repo/ layout for people.
    link = os.path.join(manifest.repodir, "internal-fs-layout.md")
    if not platform_utils.islink(link):
        target = os.path.join("repo", "docs", "internal-fs-layout.md")
        try:
            platform_utils.symlink(target, link)
        except Exception:
            pass

    wrapper = Wrapper()
    if wrapper.NeedSetupGnuPG():
        wrapper.SetupGnuPG(quiet)
    for project in manifest.projects:
        if project.Exists:
            project.PostRepoUpgrade()


def _PostRepoFetch(rp, repo_verify=True, verbose=False):
    if rp.HasChanges:
        logger.warning("info: A new version of repo is available")
        wrapper = Wrapper()
        try:
            rev = rp.bare_git.describe(rp.GetRevisionId())
        except GitError:
            rev = None
        _, new_rev = wrapper.check_repo_rev(
            rp.gitdir, rev, repo_verify=repo_verify
        )
        # See if we're held back due to missing signed tag.
        current_revid = rp.bare_git.rev_parse("HEAD")
        new_revid = rp.bare_git.rev_parse("--verify", new_rev)
        if current_revid != new_revid:
            # We want to switch to the new rev, but also not trash any
            # uncommitted changes.  This helps with local testing/hacking.
            # If a local change has been made, we will throw that away.
            # We also have to make sure this will switch to an older commit if
            # that's the latest tag in order to support release rollback.
            try:
                # Refresh index since reset --keep won't do it.
                rp.work_git.update_index("-q", "--refresh")
                rp.work_git.reset("--keep", new_rev)
            except GitError as e:
                raise RepoUnhandledExceptionError(e)
            print("info: Restarting repo with latest version")
            raise RepoChangedException(["--repo-upgraded"])
        else:
            logger.warning("warning: Skipped upgrade to unverified version")
    else:
        if verbose:
            print("repo version %s is current" % rp.work_git.describe(HEAD))


class _FetchTimes:
    _ALPHA = 0.5

    def __init__(self, manifest):
        self._path = os.path.join(manifest.repodir, ".repo_fetchtimes.json")
        self._saved = None
        self._seen = {}

    def Get(self, project):
        self._Load()
        return self._saved.get(project.name, _ONE_DAY_S)

    def Set(self, project, t):
        name = project.name

        # For shared projects, save the longest time.
        self._seen[name] = max(self._seen.get(name, 0), t)

    def _Load(self):
        if self._saved is None:
            try:
                with open(self._path) as f:
                    self._saved = json.load(f)
            except (OSError, ValueError):
                platform_utils.remove(self._path, missing_ok=True)
                self._saved = {}

    def Save(self):
        if self._saved is None:
            return

        for name, t in self._seen.items():
            # Keep a moving average across the previous/current sync runs.
            old = self._saved.get(name, t)
            self._seen[name] = (self._ALPHA * t) + ((1 - self._ALPHA) * old)

        try:
            with open(self._path, "w") as f:
                json.dump(self._seen, f, indent=2)
        except (OSError, TypeError):
            platform_utils.remove(self._path, missing_ok=True)


class LocalSyncState:
    _LAST_FETCH = "last_fetch"
    _LAST_CHECKOUT = "last_checkout"

    def __init__(self, manifest):
        self._manifest = manifest
        self._path = os.path.join(
            self._manifest.repodir, ".repo_localsyncstate.json"
        )
        self._time = time.time()
        self._state = None
        self._Load()

    def SetFetchTime(self, project):
        self._Set(project, self._LAST_FETCH)

    def SetCheckoutTime(self, project):
        self._Set(project, self._LAST_CHECKOUT)

    def GetFetchTime(self, project):
        return self._Get(project, self._LAST_FETCH)

    def GetCheckoutTime(self, project):
        return self._Get(project, self._LAST_CHECKOUT)

    def _Get(self, project, key):
        self._Load()
        p = project.relpath
        if p not in self._state:
            return
        return self._state[p].get(key)

    def _Set(self, project, key):
        p = project.relpath
        if p not in self._state:
            self._state[p] = {}
        self._state[p][key] = self._time

    def _Load(self):
        if self._state is None:
            try:
                with open(self._path) as f:
                    self._state = json.load(f)
            except (OSError, ValueError):
                platform_utils.remove(self._path, missing_ok=True)
                self._state = {}

    def Save(self):
        if not self._state:
            return
        try:
            with open(self._path, "w") as f:
                json.dump(self._state, f, indent=2)
        except (OSError, TypeError):
            platform_utils.remove(self._path, missing_ok=True)

    def PruneRemovedProjects(self):
        """Remove entries don't exist on disk and save."""
        if not self._state:
            return
        delete = set()
        for path in self._state:
            gitdir = os.path.join(self._manifest.topdir, path, ".git")
            if not os.path.exists(gitdir) or os.path.islink(gitdir):
                delete.add(path)
        if not delete:
            return
        for path in delete:
            del self._state[path]
        self.Save()

    def IsPartiallySynced(self):
        """Return whether a partial sync state is detected."""
        self._Load()
        prev_checkout_t = None
        for path, data in self._state.items():
            if path == self._manifest.repoProject.relpath:
                # The repo project isn't included in most syncs so we should
                # ignore it here.
                continue
            checkout_t = data.get(self._LAST_CHECKOUT)
            if not checkout_t:
                return True
            prev_checkout_t = prev_checkout_t or checkout_t
            if prev_checkout_t != checkout_t:
                return True
        return False


# This is a replacement for xmlrpc.client.Transport using urllib2
# and supporting persistent-http[s]. It cannot change hosts from
# request to request like the normal transport, the real url
# is passed during initialization.
class PersistentTransport(xmlrpc.client.Transport):
    def __init__(self, orig_host):
        super().__init__()
        self.orig_host = orig_host

    def request(self, host, handler, request_body, verbose=False):
        with GetUrlCookieFile(self.orig_host, not verbose) as (
            cookiefile,
            proxy,
        ):
            # Python doesn't understand cookies with the #HttpOnly_ prefix
            # Since we're only using them for HTTP, copy the file temporarily,
            # stripping those prefixes away.
            if cookiefile:
                tmpcookiefile = tempfile.NamedTemporaryFile(mode="w")
                tmpcookiefile.write("# HTTP Cookie File")
                try:
                    with open(cookiefile) as f:
                        for line in f:
                            if line.startswith("#HttpOnly_"):
                                line = line[len("#HttpOnly_") :]
                            tmpcookiefile.write(line)
                    tmpcookiefile.flush()

                    cookiejar = cookielib.MozillaCookieJar(tmpcookiefile.name)
                    try:
                        cookiejar.load()
                    except cookielib.LoadError:
                        cookiejar = cookielib.CookieJar()
                finally:
                    tmpcookiefile.close()
            else:
                cookiejar = cookielib.CookieJar()

            proxyhandler = urllib.request.ProxyHandler
            if proxy:
                proxyhandler = urllib.request.ProxyHandler(
                    {"http": proxy, "https": proxy}
                )

            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(cookiejar), proxyhandler
            )

            url = urllib.parse.urljoin(self.orig_host, handler)
            parse_results = urllib.parse.urlparse(url)

            scheme = parse_results.scheme
            if scheme == "persistent-http":
                scheme = "http"
            if scheme == "persistent-https":
                # If we're proxying through persistent-https, use http. The
                # proxy itself will do the https.
                if proxy:
                    scheme = "http"
                else:
                    scheme = "https"

            # Parse out any authentication information using the base class.
            host, extra_headers, _ = self.get_host_info(parse_results.netloc)

            url = urllib.parse.urlunparse(
                (
                    scheme,
                    host,
                    parse_results.path,
                    parse_results.params,
                    parse_results.query,
                    parse_results.fragment,
                )
            )

            request = urllib.request.Request(url, request_body)
            if extra_headers is not None:
                for name, header in extra_headers:
                    request.add_header(name, header)
            request.add_header("Content-Type", "text/xml")
            try:
                response = opener.open(request)
            except urllib.error.HTTPError as e:
                if e.code == 501:
                    # We may have been redirected through a login process
                    # but our POST turned into a GET. Retry.
                    response = opener.open(request)
                else:
                    raise

            p, u = xmlrpc.client.getparser()
            # Response should be fairly small, so read it all at once.
            # This way we can show it to the user in case of error (e.g. HTML).
            data = response.read()
            try:
                p.feed(data)
            except xml.parsers.expat.ExpatError as e:
                raise OSError(
                    f"Parsing the manifest failed: {e}\n"
                    f"Please report this to your manifest server admin.\n"
                    f'Here is the full response:\n{data.decode("utf-8")}'
                )
            p.close()
            return u.close()

    def close(self):
        pass
