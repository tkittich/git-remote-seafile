"""helper.py - Git remote helper protocol implementation for Seafile."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, NamedTuple

from .client import SeafileClient, SeafileAPIError
from .config import RemoteConfig
from .lock import RemoteLock, describe_ownership_failure
from .packs import (
    check_remote_has_packs,
    fetch_and_install_pack,
    is_valid_pack_name,
    load_installed_packs,
    record_installed_packs,
)
from .refs import REF_NAMESPACES, conflict_copies_of, is_conflict_copy, iter_refs, is_valid_ref_name
from .safety import check_preflight_safety, SafetyError
from .url import mask_url, parse_seafile_url
from .git_util import (
    HEX_SHA_RE,
    create_packfile,
    filter_existing_objects,
    get_objects_to_push,
    install_packfile,
    is_ancestor,
    objects_may_be_incomplete,
    GitError,
    rev_parse,
    get_git_dir,
    get_git_config,
    remove_tree,
    sweep_stale_scratch,
)


#: The only ref namespaces this helper will write.  A module constant so the
#: real push path and its `--dry-run` preview cannot drift apart on what counts
#: as a legal destination.
_ALLOWED_REF_PREFIXES = tuple(f"{ns}/" for ns in REF_NAMESPACES)


def _flatten(text: object) -> str:
    """Collapse a message onto a single line.

    git reads the helper's stdout a line at a time.  An exception whose text
    carries a newline -- a git stderr dump, a path with a CRLF in it -- would
    otherwise be read as a second, arbitrary protocol line, which is how a
    failed push turns into an invented one.
    """
    return str(text).replace("\r", " ").replace("\n", " ").strip()


def _proto_error(dst: str, message: object) -> None:
    """Report one failure to git as ``error <dst> <message>``.

    Every error the helper emits goes through here, so the flattening above is
    a property of the protocol layer rather than a step each call site is
    trusted to remember.  It was not: six call sites open-coded
    ``str(ex).replace(...)`` while the delete path emitted the raw exception,
    and the documentation claimed a shared sanitizer existed.  Now one does.
    """
    sys.stdout.write(f"error {dst} {_flatten(message)}\n")


def _destination_error(dst: str) -> str | None:
    """Why ``dst`` is not a ref this helper may write, or ``None`` if it is.

    Two checks, because they catch different things.  The prefix test keeps the
    helper inside the namespaces it advertises.  ``is_valid_ref_name`` is git's
    own rule, and applying it to *destinations* is new: it was already applied
    to every ref the helper lists, so a name that could never be advertised
    could still be written or deleted.

    The middle check is the one with teeth.  ``refs/heads/`` and
    ``refs/heads/a/`` satisfy the prefix test, and ``is_valid_ref_name`` says
    nothing about a trailing slash -- like git-check-ref-format, it judges
    components, not the separator.  But ``_full_path`` strips the slash and the
    deletion path removes what remains, and a *directory* is a legal target for
    ``delete_entry``.  So ``git push origin :refs/heads/`` deleted every branch
    in the repository.  A destination must name a ref, not a namespace.
    """
    for prefix in _ALLOWED_REF_PREFIXES:
        if dst.startswith(prefix):
            name = dst[len(prefix):]
            break
    else:
        return "refusing to push outside refs/heads and refs/tags"
    if not name or name.endswith("/"):
        return "refusing to push a ref name with an empty or trailing component"
    if not is_valid_ref_name(dst):
        return "refusing to push a name git would reject (git-check-ref-format)"
    return None



class _PendingUpdate(NamedTuple):
    """A ref update that passed its checks and is waiting for the pack."""

    dst: str
    local_sha: str
    expected: str | None
    force: bool


_FOUR_GIB = 4 * 1024**3


def _warn_if_checkout_may_stall(largest_pack_bytes: int) -> None:
    """Warn before a fetch whose checkout git for Windows cannot do.

    Measured on git 2.55 for Windows: with ``core.autocrlf=true`` (its default)
    a blob of 4 GiB or more is silently *truncated* by ``git add`` (the size
    wraps at 32 bits -- 5 GiB came out as 1 GiB) and a ``git clone`` of one
    does not finish (240 s timeout, one core at 100 %), while with
    ``core.autocrlf=false`` the same 5 GiB blob adds in 110 s and clones in
    11 s, byte-identical.  The helper is not involved in either step, but it is
    the one place that sees the pack sizes before git starts checking out.
    """
    if os.name != "nt" or largest_pack_bytes < _FOUR_GIB:
        return
    value = (get_git_config("core.autocrlf") or "").strip().lower()
    if value in ("false", "input"):
        return
    sys.stderr.write(
        f"Warning: this repository holds a pack of {largest_pack_bytes / 1024**3:.1f} GiB. "
        "Git for Windows cannot check out a blob of 4 GiB or more while core.autocrlf is on "
        "(it is on by default): the checkout stalls, and adding such a file silently truncates it. "
        "Clone with 'git clone -c core.autocrlf=false <url>' (or set it before the checkout).\n"
    )
    sys.stderr.flush()


_NOTICE_INTERVAL_SECONDS = 24 * 60 * 60


def _notice_due(key: str, now: float | None = None) -> bool:
    """True at most once a day per *key*; records the time it said yes.

    A notice that appears on every push gets filtered out by the reader, so the
    advisory about unmerged packs is shown once a day per remote.  The state is
    a small JSON file in the user's home; any problem reading or writing it
    means "show it", never "stay silent about a real condition".
    """
    path = Path.home() / ".git-seafile-notices.json"
    stamp = time.time() if now is None else now
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except Exception:
        state = {}
    last = state.get(key)
    if isinstance(last, (int, float)) and 0 <= stamp - last < _NOTICE_INTERVAL_SECONDS:
        return False
    state[key] = stamp
    try:
        path.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass
    return True


class RemoteHelper:
    """Implements Git remote-helper protocol over Seafile Web API."""
    remote_name: str = ""
    raw_url: str = ""
    server_url: str | None = None
    library_name: str = ""
    repo_path: str = ""
    _repo_id: str = ""
    path_defaulted: bool = False
    #: Set from ``option dry-run true``, which git sends for ``git push
    #: --dry-run``.
    dry_run: bool
    #: ``{ref: expected remote sha}`` from ``option cas <ref>:<sha>``, which
    #: git sends for ``--force-with-lease``.  An empty expectation means "the
    #: ref must not exist".  Replaced, never mutated, so the class default is
    #: safe to share.
    cas_expect: dict[str, str] = {}

    def __init__(self, remote_name: str, url: str, client: SeafileClient | None = None):
        self.remote_name = remote_name
        self.raw_url = url
        parsed_url = parse_seafile_url(url)
        self.server_url = parsed_url.server_url
        self.library_name = parsed_url.library_name
        self.repo_path = parsed_url.repo_path
        self.path_defaulted = parsed_url.path_defaulted
        self.client = client if client is not None else SeafileClient(server_url=self.server_url)
        self.dry_run = False
        self._refs_cache: dict[str, str] = {}  # refname -> sha1

    @property
    def repo_id(self) -> str:
        """The library's repo id, resolved on first use.

        Not in ``__init__``: git asks ``capabilities`` before anything else, and
        that answer needs no server.  Resolving here keeps a bare handshake --
        which IDEs and prompt integrations trigger constantly -- off the
        network.  A library that does not exist is still reported, at the first
        operation that needs it.
        """
        if not self._repo_id:
            self._repo_id = self.client.get_repo_id(self.library_name)
        return self._repo_id

    @repo_id.setter
    def repo_id(self, value: str) -> None:
        self._repo_id = value

    def _full_path(self, rel_path: str) -> str:
        """Combine repo_path with relative subpath."""
        clean_rel = rel_path.strip("/")
        return f"{self.repo_path}/{clean_rel}".rstrip("/")

    def _prune_empty_ref_parents(self, ref_path: str) -> None:
        """Prune empty parent directories up to refs/heads or refs/tags after ref deletion."""
        parent = os.path.dirname(ref_path).replace("\\", "/")
        stop_points = {"refs/heads", "refs/tags", "refs", ""}
        while parent and parent not in stop_points:
            full_parent = self._full_path(parent)
            try:
                entries = self.client.list_dir(self.repo_id, full_parent)
                # list_dir answers [] for both a genuinely empty directory and
                # a missing/unreachable one; dir_exists() re-asks and only
                # confirms on an explicit 200 (raising on anything that is
                # neither 200 nor 404), so a transient failure reads as "stop"
                # rather than "delete this parent" -- the outer except below
                # catches the raise and breaks the loop.
                if (
                    isinstance(entries, list)
                    and len(entries) == 0
                    and self.client.dir_exists(self.repo_id, full_parent)
                    # The delete is recursive, and a peer may have created a
                    # sibling ref in the round trips since the listing above:
                    # look once more, as close to the delete as one request
                    # allows, and leave the directory if it is no longer empty.
                    and self.client.list_dir(self.repo_id, full_parent) == []
                ):
                    self.client.delete_entry(self.repo_id, full_parent)
                    # The directory cache must forget this path or it will
                    # never be recreated (a future D/F ref conflict).
                    self.client.evict_known_dir(self.repo_id, full_parent)
                    parent = os.path.dirname(parent).replace("\\", "/")
                else:
                    break
            except Exception:
                break

    def _repository_has_objects(self) -> bool:
        """True if the remote already holds at least one packfile."""
        return check_remote_has_packs(self.client, self.repo_id, self._full_path("objects/pack"))

    def cmd_capabilities(self) -> None:
        """Report supported capabilities to Git."""
        sys.stdout.write("fetch\n")
        sys.stdout.write("push\n")
        sys.stdout.write("option\n")
        sys.stdout.write("object-format\n")
        sys.stdout.write("\n")
        sys.stdout.flush()

    def _detect_remote_object_format(self, discovered_refs: list[tuple[str, str]]) -> str:
        """Return "sha1" or "sha256" from a non-empty ref listing.

        Every SHA that reaches here was already validated against HEX_SHA_RE, so
        it is exactly 40 or 64 hex characters and the first ref always decides.
        The pack-name and local-format fallbacks that used to follow were
        therefore unreachable from the only call site (cmd_list, guarded by
        ``if discovered_refs``) and have been removed rather than left to read as
        a live safety net.  ``cmd_list`` emits no ``:object-format`` at all for an
        empty remote, so this is never called with an empty list.
        """
        for _, sha in discovered_refs:
            if len(sha) == 64:
                return "sha256"
            if len(sha) == 40:
                return "sha1"
        return "sha1"

    def _discover_refs(self, out: list[tuple[str, str]]) -> None:
        """Append every valid remote ref to *out* and record it in the refs cache."""
        for namespace in REF_NAMESPACES:
            # SEAFILE_STRICT_REFS=1 is set by a caller that decides what to
            # *delete* from what it fetches (the snapshot tool's payload gc): a
            # ref this listing would only warn about and omit is, to it, a
            # branch whose objects look unreferenced.
            strict = os.environ.get("SEAFILE_STRICT_REFS") == "1"
            for ref_name, sha in iter_refs(
                self.client, self.repo_id, self.repo_path, namespace,
                strict=strict, strict_listing=True,
            ):
                if not is_valid_ref_name(ref_name):
                    if is_conflict_copy(ref_name):
                        sys.stderr.write(
                            f"Warning: '{ref_name}' is a Seafile conflict copy; ignoring it "
                            f"(RUNBOOK.md, \"Conflict Artifacts\").\n"
                        )
                    else:
                        sys.stderr.write(f"Warning: ignoring malformed ref name '{ref_name}'\n")
                    sys.stderr.flush()
                    continue
                if not HEX_SHA_RE.fullmatch(sha):
                    sys.stderr.write(f"Warning: ignoring invalid SHA '{sha}' for ref '{ref_name}'\n")
                    sys.stderr.flush()
                    continue
                self._refs_cache[ref_name] = sha
                out.append((ref_name, sha))

    def cmd_list(self, for_push: bool = False) -> None:
        """List references on the remote Seafile repository."""
        self._refs_cache.clear()

        # 1. Discover branches and tags.  iter_refs walks recursively, so refs
        # whose names contain a slash ("refs/heads/feature/auth") are included.
        # A single-level listing would hide them -- and a hidden ref is not just
        # missing from the advert, it is also treated as garbage by compaction.
        discovered_refs: list[tuple[str, str]] = []
        for attempt in (1, 2):
            self._refs_cache.clear()
            discovered_refs = []
            try:
                self._discover_refs(discovered_refs)
                break
            except SeafileAPIError as ex:
                # A refs directory that its parent listed and that then 404s is
                # a branch deleted between the two requests (the second listing
                # is consistent) or a server failing -- and advertising the
                # rest as if it were the whole repository is how a fetch quietly
                # loses branches.  One retry, then say so.
                if attempt == 2:
                    raise
                sys.stderr.write(
                    f"Warning: listing the remote refs failed ({_flatten(ex)}); retrying once.\n"
                )
                sys.stderr.flush()
                time.sleep(0.5)

        # HEAD is read *before* anything is written.  A transient failure here
        # used to surface after the ref lines were already on stdout, leaving
        # git with a partial advertisement and a helper that then died; read
        # first, the failure is raised while the listing is still empty and git
        # sees a helper that failed, not one that answered wrongly.
        head_content = self.client.get_file_text(self.repo_id, self._full_path("HEAD"))

        # Emit the object-format ref list keyword when refs exist.  An empty
        # remote emits nothing: it has no format yet to advertise, and git's
        # push to an empty remote is exercised by the e2e suite.
        if discovered_refs:
            obj_format = self._detect_remote_object_format(discovered_refs)
            sys.stdout.write(f":object-format {obj_format}\n")

        for ref_name, sha in discovered_refs:
            sys.stdout.write(f"{sha} {ref_name}\n")

        listed_any = bool(discovered_refs)

        # An empty ref listing is the correct answer for a brand-new repository.
        # But Seafile reports a missing directory as 404, which is also what a
        # transient failure looks like, and from the listing alone the two are
        # indistinguishable.  So cross-check the object store: a repository that
        # already holds packfiles is not empty, and advertising it as empty makes
        # clone/fetch/ls-remote quietly do nothing while exiting 0.
        # Skip this check when listing for push: pushing into an empty repository
        # or one where all refs were deleted must be allowed.
        if not listed_any and not for_push and self._repository_has_objects():
            raise SeafileAPIError(
                f"{self.library_name}:{self.repo_path} contains packfiles but no refs could be "
                "listed. Refusing to report an empty repository -- this is usually a transient "
                "server error. Retry, or run 'git-remote-seafile test <url>' to diagnose."
            )

        # 2. Read HEAD symbolic ref.  The target is validated before it is
        # echoed because this line goes straight to git's stdout: a HEAD file
        # carrying a newline (or any other control character) would append a
        # second, invented line to the ref listing.  That is the same injection
        # the ``_proto_error`` discipline exists to stop, on the one line that
        # discipline does not cover -- it guards ``error`` lines only.  A
        # malformed HEAD is skipped rather than fatal: git then treats the
        # remote as having no default branch, which is exactly what a HEAD we
        # cannot parse means.
        if head_content and head_content.startswith("ref:"):
            target_ref = head_content.split(":", 1)[1].strip()
            if is_valid_ref_name(target_ref):
                sys.stdout.write(f"@{target_ref} HEAD\n")
            else:
                # ``!r`` so a target carrying control characters is shown
                # escaped rather than replayed into the log.
                sys.stderr.write(f"Warning: ignoring malformed HEAD target {target_ref!r}\n")
                sys.stderr.flush()

        # Empty line ends the ref listing
        sys.stdout.write("\n")
        sys.stdout.flush()

    @staticmethod
    def _report_safety_warnings(warnings: list[str]) -> None:
        """Print the non-fatal guardrail notices (safety.py check 6)."""
        for w in warnings:
            sys.stderr.write(f"\n[git-remote-seafile NOTICE]\n{w}\n\n")
        sys.stderr.flush()

    @staticmethod
    def _report_safety_block(safe_err: SafetyError) -> None:
        """Print the guardrail banner for a condition that must block."""
        sys.stderr.write("\n====================================================================\n")
        sys.stderr.write("[git-remote-seafile PRE-FLIGHT SAFETY BLOCK]\n")
        sys.stderr.write(f"{safe_err}\n")
        sys.stderr.write("====================================================================\n\n")
        sys.stderr.flush()

    def _preflight_safety(self, push_mode: bool) -> list[str]:
        """Run the guardrails for this remote, printing notices as it goes.

        Raises SafetyError when the operation must be blocked; the caller
        decides how to signal that in its own half of the protocol.
        """
        try:
            warnings = check_preflight_safety(
                self.client,
                self.library_name,
                self.repo_path,
                push_mode=push_mode,
            )
        except SafetyError as safe_err:
            self._report_safety_block(safe_err)
            raise
        self._report_safety_warnings(warnings)
        return warnings

    # ------------------------------------------------------------------
    # Push
    #
    # cmd_push is a sequence of named steps.  The two decisions a dry run must
    # answer exactly as a real push does -- "is this spec acceptable" and "may
    # this ref move" -- are _split_spec and _decide_update, and both paths call
    # them, so the dry run cannot drift from the real thing.
    # ------------------------------------------------------------------

    @staticmethod
    def _spec_destination(spec: str) -> str:
        """Best-effort destination of *spec*, for reporting a failure that
        happened before it was parsed."""
        clean = spec.lstrip("+")
        return clean.split(":", 1)[1] if ":" in clean else clean

    def _split_spec(self, spec: str) -> tuple[bool, str, str] | None:
        """Parse one push spec into ``(force, src, dst)``.

        Writes the protocol error line and returns ``None`` when the spec
        cannot be acted on: a malformed spec (git itself never sends one without
        the colon, but a hand-driven protocol can, and a ValueError here would
        kill the helper mid-protocol with no error line for git to read) or a
        destination outside the allowed namespaces.
        """
        force = spec.startswith("+")
        clean_spec = spec.lstrip("+")
        if ":" not in clean_spec:
            _proto_error(clean_spec, "invalid refspec (missing ':')")
            return None
        src, dst = clean_spec.split(":", 1)
        problem = _destination_error(dst)
        if problem:
            _proto_error(dst, problem)
            return None
        return force, src, dst

    def _decide_update(self, src: str, dst: str, force: bool) -> tuple[str, str | None] | str:
        """Decide whether *dst* may move to what *src* points at.

        Returns ``(local_sha, remote_sha)`` when it may, or the reason string
        git will be shown when it may not.  Reads only; it is the whole of what
        a dry run needs.  The remote value is read fresh and noted in the refs
        cache, which is only ever a record of what was last seen.
        """
        local_sha = rev_parse(src)
        if not local_sha:
            return "local ref does not exist"
        remote_sha = self.client.get_file_text(self.repo_id, self._full_path(dst))
        if remote_sha:
            self._refs_cache[dst] = remote_sha
        else:
            self._refs_cache.pop(dst, None)
        if dst in self.cas_expect:
            # --force-with-lease: the push may rewrite history, but only over
            # exactly the value the user last saw.  Git sends no ``+`` for it,
            # so the lease is what permits the non-fast-forward.
            if (remote_sha or "") != self.cas_expect[dst]:
                return "stale info"
            return local_sha, remote_sha
        if remote_sha and not force:
            # A ref file's content is whatever the server returned.  It reaches
            # git's argv below, where `--stdin` or `--help` is an option, not a
            # revision.  Only that shape is refused here: any other non-object
            # name already fails inside git and gets the same answer.
            if isinstance(remote_sha, str) and remote_sha.startswith("-"):
                return "fetch first"
            try:
                if not is_ancestor(remote_sha, local_sha):
                    return "non-fast-forward"
            except GitError:
                return "fetch first"
        return local_sha, remote_sha

    def _accept_cas(self, value: str) -> bool:
        """Record one ``option cas <ref>:<sha>``; False if it is not that shape.

        Refused (answered ``unsupported``) rather than guessed at: a lease the
        helper cannot read must not become a push that ignores it.
        """
        ref, sep, sha = value.strip().partition(":")
        if not sep or not ref.startswith("refs/") or (sha and not HEX_SHA_RE.fullmatch(sha)):
            return False
        self.cas_expect = {**self.cas_expect, ref: sha}
        return True

    def _push_dry_run(self, push_specs: list[str]) -> None:
        """Report what cmd_push() *would* do, and write nothing.

        Git asks for this by sending ``option dry-run true`` ahead of the push
        commands -- that is `git push --dry-run` -- and then reads exactly the
        same ``ok``/``error`` lines a real push emits.  So the accuracy of those
        lines *is* the feature, and they come from the same decisions the real
        path makes (:meth:`_split_spec`, :meth:`_decide_update`).

        Deliberately takes no lock and performs no write.  A dry run is an
        advisory snapshot by definition, so the read-then-decide race that the
        real path closes with the lock plus a CAS re-check is not one this can
        close; taking the lock to answer a question nobody will act on would
        also park a genuine push behind the settlement window.  Skipping the
        pack build is what makes it fast, and that is safe because this helper
        builds packs from the *local* repository -- git never hands it objects
        that would then have to be consumed.
        """
        for spec in push_specs:
            parsed = self._split_spec(spec)
            if parsed is None:
                continue
            force, src, dst = parsed
            try:
                # Branch deletion: report it, delete nothing.
                if not src:
                    # The same question the real deletion asks first, so the
                    # preview does not say "ok" to what git would refuse.
                    current = self.client.get_file_text(self.repo_id, self._full_path(dst))
                    if not current:
                        _proto_error(dst, "remote ref does not exist")
                    elif dst in self.cas_expect and current != self.cas_expect[dst]:
                        _proto_error(dst, "stale info")
                    else:
                        sys.stdout.write(f"ok {dst}\n")
                    continue
                verdict = self._decide_update(src, dst, force)
                if isinstance(verdict, str):
                    _proto_error(dst, verdict)
                else:
                    sys.stdout.write(f"ok {dst}\n")
            except Exception as ex:
                _proto_error(dst, ex)

        sys.stdout.write("\n")
        sys.stdout.flush()

    def _note_defaulted_path(self) -> None:
        """First push into an empty location with no path in the URL: say where
        it is going, once, because "seafile://code" quietly meaning
        "code/git-repo" is the kind of default people only learn by looking in
        the library."""
        if self.path_defaulted and not self._refs_cache:
            sys.stderr.write(
                f"Note: {mask_url(self.raw_url)} names no path, so this repository lives at "
                f"'{self.repo_path}' in library '{self.library_name}'. Use "
                f"seafile://{self.library_name}{self.repo_path} to say so explicitly.\n"
            )
            sys.stderr.flush()

    def _delete_remote_ref(self, lock: RemoteLock, dst: str) -> None:
        """Handle ``push :<ref>``: write ``ok`` or a protocol error for *dst*."""
        ref_file = self._full_path(dst)
        try:
            # Read the ref before removing it, for three reasons.  It proves the
            # target is a *file*: a ref is a file under refs/, while
            # `delete_entry` removes files and directories alike, so deleting
            # blind is how `:refs/heads/` took every branch with it.  It turns
            # deleting a ref that is not there into git's own "remote ref does
            # not exist" instead of an `ok` for a no-op.  And it gives the lease
            # fence something to be checked *before* -- a deletion is as
            # destructive as a ref write.
            existing = self.client.get_file_text(self.repo_id, ref_file)
            if not existing:
                _proto_error(dst, "remote ref does not exist")
            elif dst in self.cas_expect and existing != self.cas_expect[dst]:
                _proto_error(dst, "stale info")
            elif not lock.verify_ownership():
                state = lock.ownership_state()
                _proto_error(
                    dst,
                    "could not confirm lock ownership before deleting a ref "
                    f"({describe_ownership_failure(state)})",
                )
            elif not self.client.delete_entry(self.repo_id, ref_file):
                _proto_error(dst, "failed to delete ref on remote")
            elif self._still_there(ref_file, existing):
                # Seafile answers 200 to a delete it did not perform (a missing
                # file measured so; a broken or hostile server could do it for
                # a present one).  The write path reads back; so does this.
                _proto_error(dst, "the server accepted the delete but the ref is still there")
            else:
                # Forget it here as well.  The cache is read back as a push
                # exclusion list, and it is only truthful while the ref still
                # exists on the remote -- a stale SHA would have a later push
                # exclude objects on the strength of a ref that is gone.
                self._refs_cache.pop(dst, None)
                self._prune_empty_ref_parents(dst)
                self._warn_if_head_dangles(dst)
                sys.stdout.write(f"ok {dst}\n")
        except Exception as ex:
            _proto_error(dst, ex)

    def _still_there(self, ref_file: str, before: str) -> bool:
        """True only when a read-back returns the very value that was deleted.

        A read that fails proves nothing either way and is not an error: the
        delete itself was acknowledged.
        """
        try:
            after = self.client.get_file_text(self.repo_id, ref_file)
        except Exception:
            return False
        return isinstance(after, str) and isinstance(before, str) and after == before

    def _warn_if_head_dangles(self, deleted_ref: str) -> None:
        """Say so when the branch just deleted is the one remote HEAD names.

        Git itself leaves a bare repository's HEAD pointing at a deleted branch
        and warns on the next clone; saying it here, with the command that
        fixes it, is the difference between a puzzling warning and a next step.
        Best effort: a failure to read HEAD must never fail the deletion.
        """
        if not deleted_ref.startswith("refs/heads/"):
            return
        try:
            head = self.client.get_file_text(self.repo_id, self._full_path("HEAD"))
        except Exception:
            return
        if head and head.startswith("ref:") and head.split(":", 1)[1].strip() == deleted_ref:
            sys.stderr.write(
                f"Warning: the remote's default branch (HEAD) was {deleted_ref}, which no longer "
                f"exists, so clones will have no default branch. Point it at another branch with "
                f"'git-remote-seafile set-head {mask_url(self.raw_url)} <branch>'.\n"
            )
            sys.stderr.flush()

    def _known_remote_tips(self, pending_updates: list[_PendingUpdate]) -> set[str]:
        """The commits the remote holds *now*, which the new pack may leave out.

        Whatever is reachable from a ref the remote still has is on the remote,
        so the pack need not repeat it.  Which refs it still has is only known
        under the lock: the listing ``list for-push`` produced was taken before
        it, and a branch deleted since then (with a ``gc`` run in the window)
        takes its unique commits with it.  A pack that left those out would be
        accepted, the ref would be written, and the repository would name an
        object it does not have -- for good, because a later push excludes it
        the same way.  So the refs are read again here.  When they cannot be
        read the exclusion shrinks to the tips verified under the lock (each
        ``expected``): a bigger pack, never a missing object.
        """
        known: set[str] = set()
        try:
            for namespace in REF_NAMESPACES:
                for _name, sha in iter_refs(
                    self.client, self.repo_id, self.repo_path, namespace, strict=True
                ):
                    if sha:
                        known.add(sha)
        except Exception as ex:
            sys.stderr.write(
                f"Warning: could not re-read the remote refs under the lock ({_flatten(ex)}); "
                "sending every object the new tips need.\n"
            )
            sys.stderr.flush()
            known = set()
        for upd in pending_updates:
            if upd.expected:
                known.add(upd.expected)
        return known

    def _upload_push_pack(
        self,
        lock: RemoteLock,
        pending_updates: list[_PendingUpdate],
        max_pack_bytes: int | None = None,
    ) -> None:
        """Build one pack of everything the pending updates need and upload it.

        Raises on any failure; the caller reports it against every pending ref.
        """
        exclude = list(self._known_remote_tips(pending_updates))
        target_shas = [u.local_sha for u in pending_updates]

        def on_upload_progress(transferred: int, total: int) -> None:
            lock.maybe_renew(20.0)

        lock.maybe_renew(20.0)
        objects_to_push = get_objects_to_push(target_shas, exclude)
        if not objects_to_push:
            return
        try:
            git_dir = get_git_dir()
        except Exception:
            git_dir = None
        staging_dir = Path(tempfile.mkdtemp(prefix="grs-push-", dir=str(git_dir) if git_dir else None))
        try:
            pack_sha, pack_data, idx_data = create_packfile(
                objects_to_push, staged_dir=staging_dir, max_pack_bytes=max_pack_bytes
            )
            lock.maybe_renew(20.0)
            if isinstance(pack_data, (bytes, bytearray)):
                pack_size = len(pack_data)
                has_pack = bool(pack_size)
            else:
                pack_path = Path(pack_data)
                has_pack = pack_path.is_file()
                pack_size = pack_path.stat().st_size if has_pack else 0
            if not has_pack:
                return
            # More than one pack when the objects exceed the size cap: each is
            # self-contained, so they upload independently.
            uploads: list[tuple[str, Any, Any]] = [(pack_sha, pack_data, idx_data)]
            if not isinstance(pack_data, (bytes, bytearray)):
                for extra in sorted(staging_dir.glob("pack-*.pack")):
                    extra_sha = extra.name[len("pack-"):-len(".pack")]
                    if extra_sha != pack_sha:
                        uploads.append((extra_sha, extra, extra.with_suffix(".idx")))
            pack_dir = self._full_path("objects/pack")
            landed: dict[str, int] = {}
            for up_sha, up_pack, up_idx in uploads:
                up_size = len(up_pack) if isinstance(up_pack, (bytes, bytearray)) else Path(up_pack).stat().st_size
                size_kb = max(1, up_size // 1024)
                sys.stderr.write(f"Uploading packfile pack-{up_sha[:8]} ({size_kb} KiB)...\n")
                sys.stderr.flush()
                self.client.upload_file(
                    self.repo_id,
                    pack_dir,
                    f"pack-{up_sha}.pack",
                    up_pack,
                    replace=True,
                    progress_callback=on_upload_progress,
                )
                self.client.upload_file(
                    self.repo_id, pack_dir, f"pack-{up_sha}.idx", up_idx, replace=True
                )
                landed[f"pack-{up_sha}.pack"] = up_size
                landed[f"pack-{up_sha}.idx"] = (
                    len(up_idx) if isinstance(up_idx, (bytes, bytearray)) else Path(up_idx).stat().st_size
                )
            self._verify_packs_landed(pack_dir, landed)
        finally:
            remove_tree(staging_dir)

    def _verify_packs_landed(self, pack_dir: str, expected: dict[str, int]) -> None:
        """Raise unless every uploaded file is listed on the remote at its local size.

        An upload that answered 2xx is not evidence the bytes are there: a
        truncated write, or a proxy that answered for a quota error, looks the
        same from here.  ``gc`` already proves its new pack this way before it
        deletes anything; the push does it before it publishes a ref, because
        a ref naming a pack that is not there is a repository nobody can clone.
        """
        listing = self.client.list_dir(self.repo_id, pack_dir)
        remote = {
            e.get("name"): e.get("size")
            for e in listing
            if isinstance(e, dict) and e.get("name")
        }
        problems: list[str] = []
        for name, size in sorted(expected.items()):
            got = remote.get(name)
            try:
                got_int = int(got) if got is not None else None
            except (TypeError, ValueError):
                got_int = None
            if got_int is None:
                problems.append(f"{name} is not listed on the remote with a readable size")
            elif got_int != size:
                problems.append(f"{name} is {got_int} bytes on the remote but {size} locally")
        if problems:
            raise RuntimeError(
                "the uploaded packfile could not be verified on the remote ("
                + "; ".join(problems)
                + "); no ref was updated"
            )

    def _publish_ref(self, dst: str, local_sha: str, expected_remote_sha: str | None, force: bool) -> bool:
        """Move one remote ref and report it; True when ``ok`` was written.

        CAS re-check, write, read-back, conflict-copy check, default HEAD.
        Writes the protocol error itself for the failures it can name; an
        unexpected exception propagates to the caller.
        """
        # CAS check: verify remote ref hasn't changed since fast-forward check
        current_remote = self.client.get_file_text(self.repo_id, self._full_path(dst))
        if current_remote != expected_remote_sha and not force:
            _proto_error(dst, "fetch first")
            return False

        dst_parent = self._full_path(os.path.dirname(dst))
        dst_filename = os.path.basename(dst)
        self.client.upload_file(
            self.repo_id,
            dst_parent,
            dst_filename,
            f"{local_sha}\n".encode("utf-8"),
            replace=True,
        )

        # Read the ref back.  Seafile exposes no atomic compare-and-swap, so the
        # CAS check above and this write are separate round trips, and a peer
        # that ignores the advisory lock can land between them: the ref would
        # then hold *its* commit while we report ok, and the push that lost
        # would be silent.  Re-reading turns that into a reported failure.
        #
        # Only a *string* that differs proves a lost update.  A read that cannot
        # settle the question -- None (the ref is not there), an exception, or a
        # client that returns something that is not a string at all -- is a
        # warning, never an error: the upload already returned success, and
        # telling the user a good push failed is its own bug.  The same
        # tri-state reasoning as the lock's LEASE_HELD / LOST / UNKNOWN.
        try:
            landed = self.client.get_file_text(self.repo_id, self._full_path(dst))
        except Exception as ex:
            sys.stderr.write(
                f"Warning: could not re-read {dst} after writing it "
                f"({ex}); the write is unverified.\n"
            )
            sys.stderr.flush()
        else:
            if isinstance(landed, str) and landed != local_sha:
                _proto_error(dst, "fetch first")
                sys.stderr.write(
                    f"\nPush error for {dst}: the remote ref holds "
                    f"{landed} after this push wrote {local_sha} -- "
                    "another client updated it concurrently. "
                    "Fetch and retry.\n"
                )
                sys.stderr.flush()
                return False
            if landed is None:
                sys.stderr.write(
                    f"Warning: {dst} was not visible immediately after "
                    "writing it; the write is unverified.\n"
                )
                sys.stderr.flush()

        # A concurrent writer leaves its copy *beside* the ref, and the
        # read-back above cannot see that: it reads our path, which holds one of
        # the two values.  Measured on a real server, simultaneous writes to one
        # path all report success and leave "<ref> (SFConflict ...)" files next
        # to it.
        try:
            siblings = self.client.list_dir(self.repo_id, dst_parent)
            copies = conflict_copies_of(siblings, dst_filename)
        except Exception:
            copies = []
        if copies:
            sys.stderr.write(
                f"Warning: another client wrote {dst} at the same time; Seafile "
                f"kept its version as {', '.join(copies)}. This push is recorded, "
                f"but decide which commit is right and delete the copy "
                f"(RUNBOOK.md, \"Conflict Artifacts\").\n"
            )
            sys.stderr.flush()

        # If HEAD does not exist, set default branch.  Only point HEAD to a
        # branch in refs/heads/, never to a tag.
        if dst.startswith("refs/heads/"):
            # Best effort: the ref is already written and verified, so a
            # failure here must not turn a landed push into "remote rejected".
            try:
                head_path = self._full_path("HEAD")
                if not self.client.get_file_text(self.repo_id, head_path):
                    self.client.upload_file(
                        self.repo_id,
                        self.repo_path,
                        "HEAD",
                        f"ref: {dst}\n".encode("utf-8"),
                        replace=True,
                    )
            except Exception as ex:
                sys.stderr.write(
                    f"Warning: {dst} was pushed, but the remote's default branch (HEAD) could "
                    f"not be set ({_flatten(ex)}). Set it with "
                    f"'git-remote-seafile set-head {mask_url(self.raw_url)} <branch>'.\n"
                )
                sys.stderr.flush()

        self._refs_cache[dst] = local_sha
        sys.stdout.write(f"ok {dst}\n")
        return True

    def _auto_gc_advice(self, session_config: RemoteConfig) -> int:
        """Decide whether a tail merge is due, and tell the user what applies.

        Returns the movable-pack count when one should run after the lock is
        released, else 0.  Only *decides*: running it under the push lock
        self-deadlocks, because tail_merge acquires the same lock and
        RemoteLock is not reentrant.
        """
        try:
            pack_entries = self.client.list_dir(self.repo_id, self._full_path("objects/pack"))
            freeze_at = session_config.freeze_bytes
            # Movable vs frozen is decided by size alone -- the same split
            # tail_merge makes under the lock, so this decision cannot drift
            # from what the merge itself will do.
            pack_info = [
                (e["name"], int(e.get("size") or 0))
                for e in pack_entries if e["name"].endswith(".pack")
            ]
            movable_count = sum(1 for _, size in pack_info if size < freeze_at)
            movable_bytes = sum(size for _, size in pack_info if size < freeze_at)
            threshold = session_config.gc_threshold
            auto_gc = session_config.auto_gc

            if movable_count < threshold:
                return 0
            # Tail merge downloads only the small packs and re-uploads their
            # union, so above the download budget it defers to a manual
            # off-hours run no matter how many pile up.
            if auto_gc and movable_bytes > session_config.gc_max_mib * 1024 * 1024:
                sys.stderr.write(
                    f"\nNotice: Movable packs total {movable_bytes / (1024**3):.1f} GiB across "
                    f"{movable_count} packfile(s), above the automatic download budget "
                    f"({session_config.gc_max_mib} MiB, 2 x seafile.gcmaxpackmib).\n"
                    f"Auto tail-merge was deferred: it downloads and re-uploads those packs.\n"
                    f"Run 'git-remote-seafile gc {mask_url(self.raw_url)}' manually when the time cost suits.\n\n"
                )
                sys.stderr.flush()
                return 0
            if auto_gc:
                return movable_count
            if _notice_due(self.raw_url):
                sys.stderr.write(
                    f"\nNotice: Remote repository has {movable_count} small packfiles (threshold: {threshold}), "
                    f"totaling {movable_bytes / (1024**3):.1f} GiB.\n"
                    f"Tip: Run 'git-remote-seafile gc {mask_url(self.raw_url)}' to optimize remote storage,\n"
                    f"     or run 'git config seafile.autogc true' to enable automatic tail merge\n"
                    f"     (automatic runs defer while the movable packs exceed 2 x seafile.gcmaxpackmib).\n\n"
                )
                sys.stderr.flush()
        except Exception:
            pass
        return 0

    def _run_auto_tail_merge(self, movable_count: int, session_config: RemoteConfig) -> None:
        """Merge the small packs, after the push lock is released.

        It must never disturb the per-ref ok/error lines already written: the
        push succeeded, and a tail-merge problem is a separate, recoverable
        event (gc can always be run by hand).  Under genuine contention with
        another machine this may still time out, which is correct -- it just
        must not self-inflict it.
        """
        try:
            sys.stderr.write(
                f"Auto-merging remote repository tail ({movable_count} small packfiles detected)...\n"
            )
            sys.stderr.flush()
            from .gc import tail_merge

            # The threshold and budget are re-checked inside, under the lock, so
            # growth between this decision and the merge is covered.
            tail_merge(
                self.client,
                self.repo_id,
                self.repo_path,
                config=session_config,
                verbose=True,
            )
        except Exception as ex:
            sys.stderr.write(f"Auto tail-merge skipped: {_flatten(ex)}\n")
            sys.stderr.flush()

    def cmd_push(self, push_specs: list[str]) -> None:
        """Process push instructions."""
        sweep_stale_scratch()
        # Pre-flight safety checks (Trap 1, Trap 2, root pollution, typos)
        try:
            self._preflight_safety(push_mode=True)
        except SafetyError as safe_err:
            first_line = str(safe_err).splitlines()[0]
            for spec in push_specs:
                _proto_error(self._spec_destination(spec), f"safety check failed: {first_line}")
            sys.stdout.write("\n")
            sys.stdout.flush()
            return

        self._note_defaulted_path()

        # A dry run answers the same question a push does -- what would change --
        # and must change nothing to answer it.  Handled before the lock is
        # taken, because taking the lock is itself a side effect.
        if self.dry_run:
            self._push_dry_run(push_specs)
            return

        auto_gc_movable_count = 0
        reported: set[str] = set()
        session_config: RemoteConfig | None = None

        try:
            # Loaded once and reused for the push lock and the post-lock tail
            # merge, so these values resolve through one documented dataclass.
            session_config = RemoteConfig.load()
            with RemoteLock(self.client, self.repo_id, self.repo_path, config=session_config) as lock:
                pending = self._stage_updates(lock, push_specs, reported)

                if pending:
                    try:
                        self._upload_push_pack(lock, pending, session_config.max_pack_bytes)
                    except Exception as ex:
                        for upd in pending:
                            _proto_error(upd.dst, ex)
                            reported.add(upd.dst)
                        sys.stderr.write(f"\nPush pack upload error: {ex}\n")
                        sys.stderr.flush()
                        pending.clear()

                    if pending and not lock.verify_ownership():
                        reason = (
                            "could not confirm lock ownership before updating refs "
                            f"({describe_ownership_failure(lock.ownership_state())})"
                        )
                        for upd in pending:
                            _proto_error(upd.dst, reason)
                            reported.add(upd.dst)
                        sys.stderr.write(f"\nPush error: {reason}\n")
                        sys.stderr.flush()
                        pending.clear()

                    for upd in pending:
                        # Each ref is several round trips, so a long multi-ref
                        # push can outlive the lease it took the fence under.
                        # Re-check before every ref, as a deletion does:
                        # DESIGN section 6.3 says fencing is at every
                        # destructive step, and a ref write is one.
                        if not lock.verify_ownership():
                            reason = (
                                "could not confirm lock ownership before updating this ref "
                                f"({describe_ownership_failure(lock.ownership_state())})"
                            )
                            _proto_error(upd.dst, reason)
                            sys.stderr.write(f"\nPush error for {upd.dst}: {reason}\n")
                            sys.stderr.flush()
                            reported.add(upd.dst)
                            continue
                        try:
                            self._publish_ref(upd.dst, upd.local_sha, upd.expected, upd.force)
                        except Exception as ex:
                            _proto_error(upd.dst, ex)
                            sys.stderr.write(f"\nPush error for {upd.dst}: {ex}\n")
                            sys.stderr.flush()
                        reported.add(upd.dst)

                auto_gc_movable_count = self._auto_gc_advice(session_config)
        except Exception as lock_err:
            for spec in push_specs:
                dst = self._spec_destination(spec)
                if dst not in reported:
                    _proto_error(dst, lock_err)
            sys.stderr.write(f"\nPush failed: {lock_err}\n")
            sys.stderr.flush()

        if auto_gc_movable_count and session_config is not None:
            self._run_auto_tail_merge(auto_gc_movable_count, session_config)

        sys.stdout.write("\n")
        sys.stdout.flush()

    def _stage_updates(
        self, lock: RemoteLock, push_specs: list[str], reported: set[str]
    ) -> list[_PendingUpdate]:
        """Handle every spec that needs no pack, and return those that do.

        Deletions are carried out here (under the lock, each fenced); updates
        are decided and returned as ``(dst, local_sha, remote_sha)`` so one
        pack can serve them all.  Anything refused is reported and added to
        *reported* so the caller's failure path does not report it twice.
        """
        pending: list[_PendingUpdate] = []
        for spec in push_specs:
            parsed = self._split_spec(spec)
            if parsed is None:
                reported.add(self._spec_destination(spec))
                continue
            force, src, dst = parsed

            if not src:
                self._delete_remote_ref(lock, dst)
                reported.add(dst)
                continue

            try:
                # Always re-read the ref under the lock to catch concurrent pushes.
                verdict = self._decide_update(src, dst, force)
            except Exception as ex:
                _proto_error(dst, ex)
                sys.stderr.write(f"\nPush error for {dst}: {ex}\n")
                sys.stderr.flush()
                reported.add(dst)
                continue
            if isinstance(verdict, str):
                _proto_error(dst, verdict)
                reported.add(dst)
                continue
            pending.append(_PendingUpdate(dst, verdict[0], verdict[1], force))
        return pending

    @staticmethod
    def _objects_present(shas: list[str]) -> bool:
        """True when every one of *shas* resolves in the local object store.

        "Cannot tell" answers False on purpose.  The only caller uses this to
        decide whether to trust the record of packs fetched on earlier runs, and
        a redundant download is a far better outcome than a skip that leaves git
        without the objects it asked for.
        """
        try:
            existing = set(filter_existing_objects(shas))
        except Exception:
            return False
        return len(existing) == len(set(shas))

    def cmd_fetch(self, fetch_specs: list[str]) -> None:
        """Download remote packfiles to local .git/objects/pack/.

        Failures must not be swallowed.  This used to catch every exception,
        write the protocol terminator and return, so a transport error looked
        like a *successful* fetch of nothing: ``git clone`` finished with an
        empty repository and exited 0, and the user found out much later.  The
        error is now re-raised, so the helper exits non-zero and git says so.

        The guardrails run here too.  They used to run only on push, so
        ``git clone seafile://Documents/code/myproject`` executed from inside
        the synced ``Documents/`` library was not blocked: the clone wrote a
        whole repository into a synced folder and started exactly the
        churn/reflection cycle they exist to prevent.  Fetch is read-only
        against the *server*, but it writes a repository locally, and that is
        what the collision check is about.
        """
        sweep_stale_scratch()
        # The fetch half of the protocol has no ``error <ref>`` line, so a block
        # here can only be signalled by exiting non-zero -- the same reasoning as
        # the failure path below.  The banner is already on stderr.
        self._preflight_safety(push_mode=False)

        try:
            # Smart Pack Fetch Filtering (M-9):
            # If all requested commit objects already exist in the local repository,
            # skip downloading redundant remote packfiles.
            # The skip reasons from *tip-commit presence*, which only implies a
            # complete object store in a full clone; the guard below withholds it
            # in shallow and partial clones, where the tip can be present while
            # its trees and blobs are not.
            requested_shas: list[str] = []
            for spec in fetch_specs:
                parts = spec.split()
                # `fullmatch`, matching the validation in `filter_existing_objects`
                # and in `cmd_list`.  `str.split()` already makes a trailing
                # newline unreachable here, but a regex that means "the whole
                # string" should be asked for the whole string (A12.2).
                if parts and HEX_SHA_RE.fullmatch(parts[0]):
                    requested_shas.append(parts[0])
                else:
                    requested_shas = []
                    break

            if requested_shas and len(requested_shas) == len(fetch_specs):
                if not objects_may_be_incomplete():
                    try:
                        existing = set(filter_existing_objects(requested_shas))
                        if len(existing) == len(set(requested_shas)):
                            sys.stdout.write("\n")
                            sys.stdout.flush()
                            return
                    except Exception:
                        pass

            git_dir = get_git_dir()
            local_pack_dir = git_dir / "objects" / "pack"
            local_packs = {p.name for p in local_pack_dir.glob("pack-*.pack")} if local_pack_dir.is_dir() else set()
            # What this clone has already fetched from this destination.  The
            # local-name check below is not enough on its own: a local `git gc`
            # repacks and renames, so the names stop matching while the objects
            # are still there, and every remote pack was downloaded again (A9).
            installed = load_installed_packs(git_dir, self.repo_id, self.repo_path)

            remote_pack_dir = self._full_path("objects/pack")
            remote_entries = self.client.list_dir(self.repo_id, remote_pack_dir)
            entries_by_name = {e["name"]: e for e in remote_entries if isinstance(e, dict) and "name" in e}
            pack_files = [name for name in entries_by_name if name.endswith(".pack")]
            _warn_if_checkout_may_stall(
                max((int(entries_by_name[n].get("size") or 0) for n in pack_files), default=0)
            )

            def download_missing(already_fetched: dict[str, int]) -> dict[str, int | None]:
                """Install every remote pack this clone does not already hold.

                ``already_fetched`` is the hint from earlier runs.  It is a
                parameter rather than a closed-over name so the retry below can
                pass an empty one.
                """
                fetched: dict[str, int | None] = {}
                for pack_name in pack_files:
                    if not is_valid_pack_name(pack_name):
                        sys.stderr.write(f"Warning: ignoring invalid remote packfile name: {pack_name}\n")
                        continue
                    size = entries_by_name[pack_name].get("size")
                    # Both are recorded, not just the downloads: a pack found
                    # under its own local name is proof this clone has it, and
                    # recording that is what lets the state file heal itself
                    # after the first fetch that follows a repack.
                    fetched[pack_name] = size
                    if pack_name in local_packs:
                        continue  # already downloaded, still under its own name
                    if pack_name in already_fetched:
                        continue  # already downloaded; a local repack renamed it

                    idx_name = pack_name.removesuffix(".pack") + ".idx"
                    expected_idx_size = entries_by_name.get(idx_name, {}).get("size")
                    fetch_and_install_pack(
                        self.client,
                        self.repo_id,
                        remote_pack_dir,
                        pack_name,
                        git_dir,
                        expected_size=size,
                        expected_idx_size=expected_idx_size,
                        installer=install_packfile,
                    )
                return fetched

            fetched = download_missing(installed)

            # The recorded list is a *hint*, and a hint must never outrank
            # evidence.  It says "this clone fetched that pack", which implies
            # the objects are still here only if something still reaches them:
            # the helper installs every remote pack, whatever git asked for, so
            # a single-branch clone holds objects no local ref reaches, and a
            # later `git gc --prune` drops them legitimately.  The hint would
            # then suppress exactly the download that is needed, and git would
            # die with "did not send all necessary objects" and nothing in the
            # output to explain it.  So ask the object store directly, and when
            # the tips git asked for are not all present, discard the hint and
            # go round once more.
            if requested_shas and installed and not self._objects_present(requested_shas):
                sys.stderr.write(
                    "Warning: packfiles recorded as already fetched do not provide "
                    "every requested object -- a local repack or prune drops objects "
                    "no ref reaches. Downloading them again.\n"
                )
                sys.stderr.flush()
                fetched.update(download_missing({}))

            record_installed_packs(git_dir, self.repo_id, self.repo_path, fetched)

            sys.stdout.write("\n")
            sys.stdout.flush()
        except Exception as ex:
            # Deliberately no protocol terminator and no normal return: writing
            # "\n" here made git treat the fetch as complete, so a clone of a
            # repository whose packs could not be downloaded "succeeded" as an
            # empty repository with exit code 0.  Re-raise instead.
            sys.stderr.write(f"Fetch failed: {ex}\n")
            sys.stderr.flush()
            raise

    def run(self) -> None:
        """Main protocol loop reading stdin from Git."""
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue

            # First token, not a prefix: "listing" or "pushed" is not "list" or
            # "push", and a hand-driven session should hear about it.
            command = line.split(None, 1)[0]

            if command == "capabilities":
                self.cmd_capabilities()
            elif command == "option":
                parts = line.split(None, 2)
                opt = parts[1] if len(parts) > 1 else ""
                val = parts[2] if len(parts) > 2 else ""
                if opt == "object-format" and val in ("true", "sha1", "sha256"):
                    # gitremote-helpers(7) documents {'true'|algorithm}: "true"
                    # asks the helper to report the format -- cmd_list does,
                    # from the refs themselves.  An explicit algorithm pins the
                    # helper to it, which this helper cannot enforce; accepting
                    # and then advertising the format the refs actually use is
                    # what lets git's own check catch a real mismatch, instead
                    # of failing operations that would have worked.
                    sys.stdout.write("ok\n")
                elif opt == "dry-run":
                    # `git push --dry-run` makes git send this, and git *dies*
                    # on "unsupported" -- "helper seafile does not support
                    # dry-run", exit 128 -- before it writes a single push
                    # command.  So accepting the option is what makes the flag
                    # usable at all; cmd_push() then reports without writing.
                    self.dry_run = val == "true"
                    sys.stdout.write("ok\n")
                elif opt == "cas" and self._accept_cas(val):
                    sys.stdout.write("ok\n")
                elif opt in ("progress", "verbosity"):
                    sys.stdout.write("ok\n")
                else:
                    sys.stdout.write("unsupported\n")
                sys.stdout.flush()
            elif command == "list":
                for_push = "for-push" in line
                self.cmd_list(for_push=for_push)
            elif command == "push":
                push_specs = [line.removeprefix("push ").strip()]
                # Collect all remaining push lines until empty line
                while True:
                    next_line = sys.stdin.readline()
                    if not next_line or not next_line.strip():
                        break
                    if next_line.startswith("push "):
                        push_specs.append(next_line.removeprefix("push ").strip())
                self.cmd_push(push_specs)
            elif command == "fetch":
                fetch_specs = [line.removeprefix("fetch ").strip()]
                while True:
                    next_line = sys.stdin.readline()
                    if not next_line or not next_line.strip():
                        break
                    if next_line.startswith("fetch "):
                        fetch_specs.append(next_line.removeprefix("fetch ").strip())
                self.cmd_fetch(fetch_specs)
            else:
                # Git sends no unknown commands today, but an empty line would be read as a
                # successful no-op; fail loudly instead.  The failure must be
                # on stderr and must stop the loop: "error unsupported" on stdout is not
                # a line git's helper reader recognizes, so writing it and continuing
                # leaves the protocol in an undefined state.  The
                # comment about empty lines was stale besides -- run() consumes empty
                # lines at the top of the loop and never reaches this branch with one.
                sys.stderr.write(f"git-remote-seafile: unsupported helper command '{line}'\n")
                sys.stderr.flush()
                raise GitError(f"unsupported helper command '{line}'")
