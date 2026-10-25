"""git_util.py - Git subprocess utilities for git-remote-seafile."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import time
import tempfile
from pathlib import Path
from typing import Any


class GitError(Exception):
    pass


#: Default ceiling, in seconds, for one git subprocess.  Generous enough for any
#: legitimate local operation -- a `pack-objects` over a large repository is
#: minutes, not hours -- while still bounding the two ways a git child can hang
#: forever: a wedged `index-pack`/`repack`, which in `gc` holds the remote lease
#: open until it expires and another client lawfully takes over, and a
#: credential-helper prompt on a machine with nobody at the keyboard.
#:
#: Raisable, but only from a **trusted** scope: ``SEAFILE_GIT_TIMEOUT`` in the
#: environment, or `seafile.gittimeout` in the system or global git config --
#: both read by :func:`get_git_timeout`.  Deliberately not a repository-local
#: key: a timeout the repository being operated on could raise is a timeout a
#: hostile repository could remove, and this one exists to protect the user from
#: a hang, not to be tuned per checkout.  The default is simply too short for the
#: repositories the README advertises: a first push of a few GB, an `index-pack`
#: of one large downloaded pack, or a `repack -a -d` of a big remote all
#: routinely exceed five minutes.  The machine owner gets to say so; the
#: repository does not.
GIT_TIMEOUT = 300

#: Blobs at or above this size are streamed into a pack as they are, instead of
#: being put through git's delta search.  The default (``core.bigFileThreshold``
#: = 512 MiB) sends every 50-500 MB raw-data file through a window of ten
#: neighbours that it will never deltify against: measured on 1 GiB of such
#: files plus 300 similar text files, 50.0 s with the default and 5.1 s at
#: 50m, with a byte-identical pack size -- the small text files are still
#: deltified.  Applied per invocation (``-c``), not written into any config, so
#: staging behaviour and a later full ``gc`` are untouched.
PACK_BIG_FILE_THRESHOLD = "50m"

#: Longest "<id> <path>" line (bytes) that git for Windows' pack-objects reads whole.
_STDIN_LINE_MAX = 300


class _TimeoutUnset:
    """Type of :data:`_UNSET`, the sentinel for :func:`run_git`'s ``timeout``.

    A sentinel rather than ``None`` because ``None`` already means "no timeout
    at all".  The two must stay distinguishable so the configured ceiling is
    resolved when a command runs rather than when this module is imported.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "_UNSET"


_UNSET = _TimeoutUnset()

#: The resolved ceiling, filled in once per process by :func:`get_git_timeout`.
_GIT_TIMEOUT: int | None = None


def _env_git_timeout() -> int | None:
    """``SEAFILE_GIT_TIMEOUT`` as an integer, or ``None`` when it is unset.

    The environment is trusted for the same reason the system and global git
    scopes are: the repository being operated on cannot set it.  A value that is
    not an integer is reported and ignored, so a typo cannot silently become the
    default.
    """
    raw = os.environ.get("SEAFILE_GIT_TIMEOUT", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        sys.stderr.write(
            f"Warning: SEAFILE_GIT_TIMEOUT='{raw}' is not an integer; ignoring it.\n"
        )
        sys.stderr.flush()
        return None


def get_git_timeout() -> int:
    """The effective ceiling for one git subprocess, in seconds.

    ``SEAFILE_GIT_TIMEOUT`` if it is set, otherwise ``seafile.gittimeout`` from
    the system or global git config, otherwise :data:`GIT_TIMEOUT`.  Both
    sources are *trusted* scopes -- the repository being operated on cannot raise
    its own ceiling, which is the whole point of the constant.

    Remembered after the first call.  Every ``run_git`` consults this, so
    re-reading git config each time would spawn a ``git config`` for every git
    command and roughly double the process count of every operation.  A value
    that is not a positive number is refused out loud, because a silent fallback
    to a zero-second ceiling would look exactly like a hang.
    """
    global _GIT_TIMEOUT
    if _GIT_TIMEOUT is None:
        # Seeded before the read so the `git config` this spawns cannot recurse
        # back into here, and so a failure leaves the default in place.
        _GIT_TIMEOUT = GIT_TIMEOUT
        value = _env_git_timeout()
        if value is None:
            value = get_trusted_git_config_int("seafile.gittimeout", GIT_TIMEOUT)
        if value > 0:
            _GIT_TIMEOUT = value
        else:
            sys.stderr.write(
                f"Warning: a git timeout of {value}s is not positive; "
                f"keeping {GIT_TIMEOUT}.\n"
            )
            sys.stderr.flush()
    return _GIT_TIMEOUT


def _communicate(
    cmd: list[str], input_bytes: bytes | None, timeout: float | None
) -> tuple[bytes, bytes, int]:
    """Run *cmd* with piped output; kill it and raise TimeoutExpired on overrun.

    The one place a bounded git child is started, waited on and reaped, so the
    timeout is applied -- and a hung child killed -- the same way for every
    caller.  Callers decide what an overrun *means*: ``run_git`` reports a
    failure, ``filter_existing_objects`` degrades to "cannot tell".
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if input_bytes is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        out, err = proc.communicate(input=input_bytes, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        raise
    return out, err, proc.returncode


def run_git(
    args: list[str],
    input_bytes: bytes | None = None,
    timeout: float | None | _TimeoutUnset = _UNSET,
) -> tuple[bytes, bytes, int]:
    """Execute a git command and return (stdout, stderr, returncode).

    ``timeout`` bounds the wait.  Left unset it resolves to
    :func:`get_git_timeout`, the configured ceiling; ``None`` disables the bound
    entirely.  A child that overruns is killed and the call raises ``GitError``:
    "the command hung" is a failure to report, not a state to wait out.
    """
    if isinstance(timeout, _TimeoutUnset):
        timeout = get_git_timeout()
    try:
        return _communicate(["git"] + args, input_bytes, timeout)
    except subprocess.TimeoutExpired:
        raise GitError(
            f"git {' '.join(args)} did not finish within {timeout:g}s and was killed. "
            f"If this repository legitimately needs longer, raise seafile.gittimeout "
            f"in the system or global git config."
        ) from None


def get_git_dir() -> Path:
    """Return the absolute path to the .git directory of the current repository."""
    out, err, code = run_git(["rev-parse", "--git-dir"])
    if code != 0:
        raise GitError(f"Not inside a git repository: {err.decode('utf-8', errors='replace')}")
    p = Path(out.decode("utf-8").strip())
    if not p.is_absolute():
        p = Path.cwd() / p
    return p.resolve()


#: Variables git uses to decide which repository it is operating on.  A remote
#: helper is launched *by* git, which exports these pointing at the caller's
#: repository; they take precedence over -C, so a command meant for a
#: *different* repository (the scratch bare repo used by compaction) must not
#: inherit them.
_GIT_REPO_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_CEILING_DIRECTORIES",
)


def clean_git_env() -> dict[str, str]:
    """Return a copy of the environment with git's repo-locating vars removed.

    Use this for git commands that must operate on a repository other than the
    one this process was launched in.
    """
    env = dict(os.environ)
    for var in _GIT_REPO_ENV_VARS:
        env.pop(var, None)
    return env


def run_git_checked(
    args: list[str], env: dict[str, str], **kwargs: Any
) -> subprocess.CompletedProcess[Any]:
    """Run git against a scratch repository, bounded by :func:`get_git_timeout`.

    Compaction holds the remote lease for the whole operation, so a git child
    that never returns is not merely a stuck command -- it is a stuck *lease*,
    and the queue will lawfully hand the lock to another client while this
    process still believes it owns it.  A timeout is therefore converted into a
    RuntimeError so the caller's ordinary failure path (which releases the
    lease) runs, rather than the process blocking until someone kills it.

    ``repack -a -d`` over a large remote is the step most likely to outlast the
    default ceiling, which is why that ceiling is raisable from the trusted
    scope -- see :func:`get_git_timeout`.

    Unlike :func:`run_git` this takes an explicit *env* (use
    :func:`clean_git_env`) and the usual ``subprocess.run`` keywords, and with
    ``check=True`` turns a failure into a RuntimeError carrying git's own words.
    """
    limit = get_git_timeout()
    check = kwargs.pop("check", False)
    try:
        return subprocess.run(["git", *args], env=env, timeout=limit, check=check, **kwargs)
    except subprocess.CalledProcessError as exc:
        # CalledProcessError's repr is the command line and the exit status and
        # *not* git's own words, which are the one line that says what was wrong
        # ("pack is corrupted (SHA1 mismatch)").  Name them.
        detail = exc.stderr
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        detail = (detail or "").strip() or "(no output)"
        raise RuntimeError(
            f"git {' '.join(args[:4])} failed (exit {exc.returncode}): {detail}"
        ) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"git {' '.join(args)} did not finish within {limit:g}s; aborting. "
            f"If this repository legitimately needs longer, raise seafile.gittimeout "
            f"in the system or global git config."
        ) from None


#: Prefixes of the scratch directories this package creates under ``.git``.
_SCRATCH_PREFIXES = ("grs-staging-", "grs-push-", "grs-fetch-")


def remove_tree(path: Path | str) -> bool:
    """Delete a directory tree, including files git marked read-only; True if it is gone.

    Git for Windows writes pack ``.idx`` and ``.pack`` files ``0444``, and
    Windows will not delete a read-only file, so ``rmtree(ignore_errors=True)``
    silently left every scratch directory behind -- a staged pack can be
    gigabytes -- while the sweeper counted it as removed.  The handler clears
    the attribute and retries once; what still cannot be removed is reported by
    the return value rather than assumed.
    """

    def _retry(func: Any, failed: str, _exc: Any) -> None:
        try:
            os.chmod(failed, stat.S_IWRITE | stat.S_IREAD)
            func(failed)
        except OSError:
            pass

    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_retry)
        else:
            shutil.rmtree(path, onerror=_retry)
    except OSError:
        pass
    return not Path(path).exists()


def sweep_stale_scratch(max_age_seconds: float = 24 * 3600, now: float | None = None) -> int:
    """Remove scratch directories a crashed run left under ``.git``.

    Each is created and removed inside one operation, so anything older than a
    day belongs to a process that did not get to clean up (a kill, a power
    cut) -- and a staged pack can be gigabytes.  Best effort: never raises, and
    returns how many directories were removed.
    """
    removed = 0
    try:
        git_dir = get_git_dir()
        cutoff = (time.time() if now is None else now) - max_age_seconds
        for entry in git_dir.iterdir():
            if not entry.name.startswith(_SCRATCH_PREFIXES) or not entry.is_dir():
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    if remove_tree(entry):
                        removed += 1
            except OSError:
                continue
    except Exception:
        return removed
    return removed


def rev_parse(ref: str) -> str | None:
    """Resolve a reference to its SHA-1 hash."""
    if ref.startswith("-"):
        # git would read it as an option.  Git sends full ref names and object
        # ids, so this only guards a hand-driven or hostile spec.
        return None
    out, _, code = run_git(["rev-parse", "--verify", ref])
    if code != 0:
        return None
    return out.decode("utf-8").strip()


def is_ancestor(ancestor_sha: str, descendant_sha: str) -> bool:
    """Check if ancestor_sha is an ancestor of descendant_sha (fast-forward check)."""
    _, err, code = run_git(["merge-base", "--is-ancestor", ancestor_sha, descendant_sha])
    if code == 0:
        return True
    if code == 1:
        return False
    raise GitError(
        f"git merge-base failed (exit {code}): {err.decode('utf-8', errors='replace').strip()}"
    )


HEX_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")


def filter_existing_objects(shas: list[str] | str | None) -> list[str]:
    """Return those SHAs that exist in the local object store, in order.

    Checked in a single git process, because one call per ref would be
    needlessly slow on a repository with many branches.
    """
    if not shas:
        return []
    if isinstance(shas, str):
        shas = [shas]
    # `fullmatch`, not `match`: `$` also matches just before a trailing
    # newline, so `HEX_SHA_RE.match("a"*40 + "\n")` is True while the same
    # string is not a SHA.  A caller that hands this a line read out of a file
    # would otherwise get a candidate that `git cat-file` rejects, and the
    # rejection would surface as a missing object rather than as bad input.
    candidates = [s for s in dict.fromkeys(shas) if s and HEX_SHA_RE.fullmatch(s)]
    if not candidates:
        return []

    try:
        git_dir = get_git_dir()
    except Exception:
        git_dir = None

    cmd = ["git"]
    if git_dir is not None:
        cmd.extend(["--git-dir", str(git_dir)])
    cmd.extend(["cat-file", "--batch-check"])

    # Resolved outside the try: a failure to read the config is not the child's
    # fault and must not be swallowed by the broad handler below.
    limit = get_git_timeout()

    try:
        out, _, returncode = _communicate(
            cmd, ("\n".join(candidates) + "\n").encode("utf-8"), limit
        )
        if returncode != 0:
            return []
    except subprocess.TimeoutExpired:
        # "Cannot tell" must not be answered as "none of them exist".  Both
        # callers read an empty list as "nothing here to skip", so the result is
        # more work (a redundant download, a fuller rev-list) rather than a
        # wrong skip -- the safe direction.  But a wait cut short is worth
        # saying out loud, because the alternative is a silent slowdown that
        # nothing in the output explains.
        sys.stderr.write(
            f"Warning: git cat-file --batch-check did not finish within "
            f"{limit:g}s; assuming no objects are present.\n"
        )
        sys.stderr.flush()
        return []
    except Exception:
        return []

    existing = []
    for line in out.decode("utf-8", errors="replace").splitlines():
        # "<sha> <type> <size>" when present, "<sha> missing" when absent.
        parts = line.split()
        if len(parts) >= 2 and parts[1] != "missing":
            existing.append(parts[0])
    return existing


def _repo_git(*args: str) -> tuple[bytes, bytes, int]:
    """Run a git command against the *caller's* repository.

    A remote helper is launched by git with ``GIT_DIR`` set, and
    ``filter_existing_objects`` already names the repository explicitly rather
    than trusting the working directory.  These probes answer questions about
    the local clone, so they must resolve the same repository the fetch is
    populating -- not whichever one the current directory happens to be in.

    ``run_git`` prepends the executable, so the arguments built here must not
    repeat it.
    """
    argv: list[str] = []
    try:
        argv.extend(["--git-dir", str(get_git_dir())])
    except Exception:
        pass
    argv.extend(args)
    return run_git(argv)


def is_shallow_repository() -> bool:
    """True if the local clone is shallow (``--depth``), so history is grafted.

    A non-zero exit -- "not a repository", or a git too old for
    ``--is-shallow-repository`` -- is reported as False rather than raised: the
    callers that matter are asking a question with a safe default, and
    ``objects_may_be_incomplete()`` re-adds the fail-safe on top.
    """
    out, _, code = _repo_git("rev-parse", "--is-shallow-repository")
    if code != 0:
        return False
    return out.decode("utf-8", errors="replace").strip().lower() == "true"


def has_promisor_remote() -> bool:
    """True if any remote is a promisor -- i.e. a partial or blobless clone.

    ``git clone --filter=blob:none`` records the remote it may lazily fetch
    from as ``remote.<name>.promisor = true``.  That is the durable marker of a
    clone whose objects can be missing on purpose; a nonzero exit from the
    config lookup just means no remote carries the key.
    """
    out, _, code = _repo_git("config", "--get-regexp", r"^remote\..*\.promisor$")
    if code != 0:
        return False
    for line in out.decode("utf-8", errors="replace").splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].strip().lower() == "true":
            return True
    return False


def objects_may_be_incomplete() -> bool:
    """True when holding a commit does not imply holding its trees and blobs.

    The fetch-skip heuristic concludes from *tip-commit presence* that the whole
    remote pack can be skipped.  That inference holds only in a full clone: a
    shallow clone has the tip but a truncated history, and a partial (promisor)
    clone has the tip while its trees and blobs still live on the server.
    Skipping the download in either case leaves ``git checkout`` reporting
    missing objects.

    Fails safe.  If the question cannot be answered at all -- no git on PATH, a
    repository git cannot read -- the answer is True, because the cost of a
    redundant download is bandwidth while the cost of a wrong skip is a broken
    checkout.
    """
    try:
        if is_shallow_repository():
            return True
        return has_promisor_remote()
    except Exception:
        return True


def get_objects_to_push(local_sha: list[str] | str, exclude_shas: list[str] | str | None = None) -> list[str]:
    """Find all git object lines reachable from local_sha but not in exclude_shas.

    local_sha may be a single SHA string or a list of SHAs (for multi-spec pushes).
    Preserves full '<sha> <path>' lines emitted by 'rev-list --objects' so that
    'pack-objects' receives file path hints for optimal delta compression windows.

    Exclusions that are not in the local object store are ignored rather than
    handed to git.  Over diverged history the remote tip can be a commit this
    clone has never fetched, and "rev-list --not <unknown>" fails with
    "bad object" instead of simply excluding nothing -- which made a legitimate
    force-push impossible.
    """
    if isinstance(local_sha, str):
        shas = [local_sha]
    else:
        shas = list(local_sha)
    unique_shas = list(dict.fromkeys(s for s in shas if s))
    if not unique_shas:
        return []

    args = ["rev-list", "--objects"] + unique_shas
    for ex in filter_existing_objects(exclude_shas):
        args.append(f"^{ex}")

    out, err, code = run_git(args)
    if code != 0:
        raise GitError(f"Failed to list objects to push: {err.decode('utf-8', errors='replace')}")

    # Split on "\n" only: str.splitlines() also breaks on \x0b, \x0c, \x1c-\x1e,
    # NEL, the Unicode line and paragraph separators, which are legal in a file name, and the tail of
    # such a path then reached 'pack-objects' as a line that is not an object
    # id ("expected object ID, got garbage").  A path containing a real newline
    # leaves the same kind of tail; a line that does not start with an object
    # id is only the rest of a path hint, so it is dropped.
    objects = []
    for line in out.decode("utf-8", errors="surrogateescape").split("\n"):
        line = line.strip()
        if not line:
            continue
        head = line.split(None, 1)[0]
        if len(head) in (40, 64) and all(c in "0123456789abcdef" for c in head):
            objects.append(line)
    return objects


def _generate_pack_in_dir(
    tmp_dir: Path, object_shas: list[str], max_pack_bytes: int | None = None
) -> tuple[str, Path, Path]:
    """Helper to generate packfile and index in a target directory.

    With *max_pack_bytes* git may write several packs (each self-contained, no
    deltas across them); the first one's name is returned and the others sit
    beside it in *tmp_dir* for the caller to find.
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    pack_prefix = tmp_dir / "pack"

    # 'pack-objects --stdin' reads a line into a "40 + PATH_MAX + 2" buffer
    # (302 bytes where PATH_MAX is 260, i.e. git for Windows) and takes the rest
    # of an over-long line for the next one: "expected object ID, got garbage".
    # Thai paths are 3 bytes per character, so a 100-character name overflows.
    # The path is only a delta-window hint; such a line goes in as a bare id.
    encoded = [s.encode("utf-8", errors="surrogateescape") for s in object_shas]
    input_data = b"\n".join(
        b[: b.index(b" ")] if len(b) > _STDIN_LINE_MAX and b" " in b else b for b in encoded
    ) + b"\n"
    cmd = ["-c", f"core.bigFileThreshold={PACK_BIG_FILE_THRESHOLD}", "pack-objects"]
    if max_pack_bytes:
        cmd.append(f"--max-pack-size={max_pack_bytes}")
    cmd.append(str(pack_prefix))
    out, err, code = run_git(cmd, input_bytes=input_data)
    if code != 0:
        err_msg = err.decode("utf-8", errors="replace").strip()
        raise GitError(f"git pack-objects failed: {err_msg}")

    # One hash per pack written, in order; the first names the one returned.
    pack_sha = (out.decode("utf-8").split() or [""])[0]
    pack_file = tmp_dir / f"pack-{pack_sha}.pack"
    idx_file = tmp_dir / f"pack-{pack_sha}.idx"

    if not pack_file.is_file():
        raise GitError(f"Expected packfile not created: {pack_file}")

    # If .idx was not automatically generated, create it
    if not idx_file.is_file():
        _, err, code = run_git(["index-pack", "-o", str(idx_file), str(pack_file)])
        if code != 0:
            err_msg = err.decode("utf-8", errors="replace").strip()
            raise GitError(f"git index-pack failed: {err_msg}")

    return pack_sha, pack_file, idx_file


def create_packfile(
    object_shas: list[str],
    staged_dir: Path | str | None = None,
    max_pack_bytes: int | None = None,
) -> tuple[str, bytes | Path, bytes | Path]:
    """Generate a .pack file and corresponding .idx file for the given object SHAs.

    If *staged_dir* is provided, the pack and idx are created directly in that
    directory and their Path objects are returned, avoiding buffering the packfile
    into memory. Otherwise, returns (pack_sha, pack_bytes, idx_bytes).

    *max_pack_bytes* only takes effect with *staged_dir*: the in-memory form can
    only return one pack.  When the objects do not fit, further packs are left
    in *staged_dir* and the caller uploads every ``pack-*.pack`` found there.
    """
    if not object_shas:
        return "", b"", b""

    # Use .git directory as parent for temporary pack directory to guarantee
    # it resides on the same filesystem/drive, preventing cross-device 'Improper link' (EXDEV)
    # errors during git pack-objects atomic rename on Windows and multi-volume systems.
    try:
        tmp_parent = get_git_dir()
    except Exception:
        tmp_parent = None

    if staged_dir is not None:
        return _generate_pack_in_dir(Path(staged_dir), object_shas, max_pack_bytes)

    with tempfile.TemporaryDirectory(prefix="git-seaf-pack-", dir=str(tmp_parent) if tmp_parent else None) as td:
        pack_sha, pack_file, idx_file = _generate_pack_in_dir(Path(td), object_shas)
        pack_bytes = pack_file.read_bytes()
        idx_bytes = idx_file.read_bytes()
        return pack_sha, pack_bytes, idx_bytes


def install_packfile(
    pack_name: str,
    pack_bytes: bytes | Path | str,
    idx_bytes: bytes | Path | str | None = None,
    move: bool = False,
) -> None:
    """Install a packfile into the local repository's .git/objects/pack/.

    Accepts pack content either in-memory as bytes, or as a Path/str pointing
    to a staged file on disk, avoiding in-memory buffering for large packs.
    When ``move=True`` and disk paths are supplied, staged files are moved rather
    than copied, eliminating redundant disk I/O.

    The pack and its index are staged in a scratch directory and only moved to
    their final names once both are complete, so ``os.replace`` publishes them
    atomically.  This matters because cmd_fetch decides whether to download a
    pack by checking whether a file of that *name* already exists: a truncated
    .pack written straight to the final path would never be retried and would
    poison the object store permanently.  Nothing may appear at the final path
    until it is known-good.
    """
    git_dir = get_git_dir()
    pack_dir = git_dir / "objects" / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)

    # Name the repository explicitly, as filter_existing_objects does.  git
    # only exports GIT_DIR when the helper is launched *by* git, and both of
    # these commands need a repository as their context to know the object
    # format: index-pack would otherwise write a SHA-1 index for a SHA-256
    # pack, which is exactly the failure gc hit (D18/D24).  These are arguments
    # to git, not a command line -- run_git() supplies the leading "git".
    git_dir_args = ["--git-dir", str(git_dir)]

    base_name = pack_name.removesuffix(".pack").removesuffix(".idx")
    target_pack = pack_dir / f"{base_name}.pack"
    target_idx = pack_dir / f"{base_name}.idx"

    # Stage beside objects/ rather than inside objects/pack: git scans that
    # directory for pack-*.pack / pack-*.idx, and "git index-pack -o" insists
    # that the index it writes is named *.idx -- so a staged file cannot be
    # renamed out of the way there.  A sibling of objects/ is still on the same
    # filesystem, which is what keeps the final os.replace atomic.
    staging_dir = Path(tempfile.mkdtemp(prefix="grs-staging-", dir=str(git_dir)))
    try:
        staged_pack = staging_dir / f"{base_name}.pack"
        staged_idx = staging_dir / f"{base_name}.idx"

        if isinstance(pack_bytes, (str, Path, os.PathLike)):
            if move:
                shutil.move(str(pack_bytes), str(staged_pack))
            else:
                shutil.copyfile(pack_bytes, staged_pack)
        else:
            staged_pack.write_bytes(pack_bytes)

        if idx_bytes:
            if isinstance(idx_bytes, (str, Path, os.PathLike)):
                if move:
                    shutil.move(str(idx_bytes), str(staged_idx))
                else:
                    shutil.copyfile(idx_bytes, staged_idx)
            else:
                staged_idx.write_bytes(idx_bytes)
            # Verify the index matches the packfile.
            _, err, code = run_git([*git_dir_args, "verify-pack", str(staged_idx)])
            if code != 0:
                try:
                    staged_idx.unlink(missing_ok=True)
                except Exception:
                    pass
                _, err, code = run_git([*git_dir_args, "index-pack", "-o", str(staged_idx), str(staged_pack)])
                if code != 0:
                    raise GitError(f"git index-pack failed on installed pack: {err.decode('utf-8', errors='replace')}")
        else:
            # Generate index locally
            _, err, code = run_git([*git_dir_args, "index-pack", "-o", str(staged_idx), str(staged_pack)])
            if code != 0:
                raise GitError(f"git index-pack failed on installed pack: {err.decode('utf-8', errors='replace')}")

        # Both artefacts are complete; publish them.
        #
        # Order matters.  Git discovers packfiles by scanning for *.idx and
        # assumes the sibling .pack exists, so publishing the index first
        # exposes a window in which a concurrent process (git status, an IDE
        # fetch, git log) can list an idx whose pack is not there yet and die
        # with "cannot open packfile".  A pack without its idx, by contrast,
        # is simply ignored by git until the index lands -- so pack first,
        # idx second is the order with no observable broken state in between.
        #
        # git writes pack and index files read-only (0444) to protect them.  On
        # Windows `os.replace` onto a read-only destination fails with
        # "PermissionError: [WinError 5] Access is denied", so re-installing a
        # name that is already there -- a retry, or a forced re-fetch -- died
        # with a permission error naming neither git nor the cause.  POSIX
        # rename only needs write permission on the directory, so this is
        # Windows-only; clearing the bit is harmless everywhere else.
        for target in (target_idx, target_pack):
            try:
                target.chmod(0o644)
            except OSError:
                pass  # absent, or on a filesystem that cannot express this
        os.replace(staged_pack, target_pack)
        os.replace(staged_idx, target_idx)
    finally:
        # After a successful publish the staged files have been renamed away;
        # after a failure this is what keeps the final path clean for a retry.
        remove_tree(staging_dir)


def get_git_config(key: str, default: str | None = None) -> str | None:
    """Read a git configuration value."""
    out, _, code = run_git(["config", "--get", key])
    if code != 0 or not out:
        return default
    return out.decode("utf-8").strip()


def _trusted_config_value(key: str) -> str | None:
    """*key* from the machine owner's scopes only, or None when unset.

    Global is consulted before system because that is git's own precedence: a
    user's ``~/.gitconfig`` overrides ``/etc/gitconfig``.  Repository-local
    config is never read.
    """
    for scope in ("--global", "--system"):
        out, _, code = run_git(["config", scope, "--get", key])
        if code == 0 and out:
            return out.decode("utf-8").strip()
    return None


def get_trusted_git_config_bool(key: str, default: bool = False) -> bool:
    """Read a boolean *security-relevant* git config value, ignoring repo-local config.

    `git config --get` reads every scope, including the `.git/config` of the
    repository being operated on.  That is the right behavior for tuning keys
    (lock timeouts, gc thresholds) but a trust-boundary inversion for the
    safety gates: a hostile or careless repository that ships
    `[seafile] skipsafetychecks = true` in its own config would otherwise
    disarm the guardrails for every helper operation launched from that
    checkout.  Keys read here answer only to the machine owner (system and
    global scopes) and the environment; the repository being cloned or pushed
    to never gets a vote.
    """
    raw_value = _trusted_config_value(key)
    if raw_value is None:
        return default
    return raw_value.lower() in ("true", "1", "yes", "on")


def get_trusted_git_config_int(key: str, default: int) -> int:
    """Read an integer *security-relevant* git config value, ignoring repo-local config.

    The integer counterpart of :func:`get_trusted_git_config_bool`, and trusted
    for the same reason: ``seafile.gittimeout`` bounds how long a wedged git
    child may hold a remote lease, so the repository being operated on must not
    be the scope that answers.  A value that is not an integer is reported and
    ignored rather than silently becoming the default.
    """
    raw_value = _trusted_config_value(key)
    if raw_value is None:
        return default
    raw = raw_value
    try:
        return int(raw)
    except ValueError:
        sys.stderr.write(f"Warning: {key}='{raw}' is not an integer; using {default}.\n")
        sys.stderr.flush()
        return default


def get_git_config_bool(key: str, default: bool = False) -> bool:
    """Read a boolean git configuration value."""
    val = get_git_config(key)
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes", "on")


def get_git_config_int(key: str, default: int) -> int:
    """Read an integer git configuration value."""
    val = get_git_config(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        # Out-of-range values are refused with a warning by RemoteConfig; a
        # value that is not a number at all deserves the same visibility --
        # a silent default reads exactly like the setting was honoured.
        sys.stderr.write(
            f"Warning: {key}='{val}' is not an integer; using {default}.\n"
        )
        sys.stderr.flush()
        return default

