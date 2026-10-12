"""helper.py - Git remote helper protocol implementation for Seafile."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

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
from .refs import REF_NAMESPACES, iter_refs, is_valid_ref_name
from .safety import check_preflight_safety, SafetyError
from .url import parse_seafile_url
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


class RemoteHelper:
    """Implements Git remote-helper protocol over Seafile Web API."""
    remote_name: str = ""
    raw_url: str = ""
    server_url: str | None = None
    library_name: str = ""
    repo_path: str = ""
    repo_id: str = ""
    #: Set from ``option dry-run true``, which git sends for ``git push
    #: --dry-run``.  Declared on the class, not only in __init__, so the
    #: ``__new__``-built instances the tests use read as False instead of
    #: raising AttributeError.
    dry_run: bool = False

    def __init__(self, remote_name: str, url: str, client: SeafileClient | None = None):
        self.remote_name = remote_name
        self.raw_url = url
        parsed_url = parse_seafile_url(url)
        self.server_url = parsed_url.server_url
        self.library_name = parsed_url.library_name
        self.repo_path = parsed_url.repo_path
        self.client = client if client is not None else SeafileClient(server_url=self.server_url)
        self.repo_id = self.client.get_repo_id(self.library_name)
        self._refs_cache: dict[str, str] = {}  # refname -> sha1

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

    def cmd_list(self, for_push: bool = False) -> None:
        """List references on the remote Seafile repository."""
        if not hasattr(self, "_refs_cache"):
            self._refs_cache = {}
        self._refs_cache.clear()

        # 1. Discover branches and tags.  iter_refs walks recursively, so refs
        # whose names contain a slash ("refs/heads/feature/auth") are included.
        # A single-level listing would hide them -- and a hidden ref is not just
        # missing from the advert, it is also treated as garbage by compaction.
        discovered_refs: list[tuple[str, str]] = []
        for namespace in REF_NAMESPACES:
            for ref_name, sha in iter_refs(self.client, self.repo_id, self.repo_path, namespace):
                if not is_valid_ref_name(ref_name):
                    sys.stderr.write(f"Warning: ignoring malformed ref name '{ref_name}'\n")
                    sys.stderr.flush()
                    continue
                if not HEX_SHA_RE.fullmatch(sha):
                    sys.stderr.write(f"Warning: ignoring invalid SHA '{sha}' for ref '{ref_name}'\n")
                    sys.stderr.flush()
                    continue
                self._refs_cache[ref_name] = sha
                discovered_refs.append((ref_name, sha))

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
        head_path = self._full_path("HEAD")
        head_content = self.client.get_file_text(self.repo_id, head_path)
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

    def _push_dry_run(self, push_specs: list[str]) -> None:
        """Report what cmd_push() *would* do, and write nothing.

        Git asks for this by sending ``option dry-run true`` ahead of the push
        commands -- that is `git push --dry-run` -- and then reads exactly the
        same ``ok``/``error`` lines a real push emits.  So the accuracy of those
        lines *is* the feature, and they come from the same decisions the real
        path makes: the namespace check, whether the ref exists locally, whether
        it exists remotely, and whether the update is a fast-forward.

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
            force = spec.startswith("+")
            clean_spec = spec.lstrip("+")
            if ":" not in clean_spec:
                # git itself never sends a spec without the src:dst colon; a
                # hand-driven protocol can.  A ValueError here would kill the
                # helper mid-protocol with no error line for git to read.
                _proto_error(clean_spec, "invalid refspec (missing ':')")
                continue
            src, dst = clean_spec.split(":", 1)

            problem = _destination_error(dst)
            if problem:
                _proto_error(dst, problem)
                continue

            try:
                # Branch deletion: report it, delete nothing.
                if not src:
                    sys.stdout.write(f"ok {dst}\n")
                    continue

                local_sha = rev_parse(src)
                if not local_sha:
                    _proto_error(dst, "local ref does not exist")
                    continue

                remote_sha = self.client.get_file_text(self.repo_id, self._full_path(dst))
                if remote_sha and not force:
                    try:
                        if not is_ancestor(remote_sha, local_sha):
                            _proto_error(dst, "non-fast-forward")
                            continue
                    except GitError:
                        _proto_error(dst, "fetch first")
                        continue

                sys.stdout.write(f"ok {dst}\n")
            except Exception as ex:
                _proto_error(dst, ex)

        sys.stdout.write("\n")
        sys.stdout.flush()

    def cmd_push(self, push_specs: list[str]) -> None:
        """Process push instructions."""
        # Pre-flight safety checks (Trap 1, Trap 2, root pollution, typos)
        try:
            self._preflight_safety(push_mode=True)
        except SafetyError as safe_err:
            first_line = str(safe_err).splitlines()[0]
            for spec in push_specs:
                dst = spec.lstrip("+").split(":", 1)[1] if ":" in spec else spec
                _proto_error(dst, f"safety check failed: {first_line}")
            sys.stdout.write("\n")
            sys.stdout.flush()
            return

        # A dry run answers the same question a push does -- what would change --
        # and must change nothing to answer it.  Handled before the lock is
        # taken, because taking the lock is itself a side effect.
        if self.dry_run:
            self._push_dry_run(push_specs)
            return

        # Set while the push lock is held, acted on after it is released.
        auto_gc_movable_count = 0
        reported_specs: set[str] = set()

        # Load session config once and reuse it for the push lock below and the
        # post-lock auto-GC so these values resolve through one documented
        # dataclass.
        try:
            session_config = RemoteConfig.load()
            with RemoteLock(self.client, self.repo_id, self.repo_path, config=session_config) as lock:
                def on_upload_progress(transferred: int, total: int) -> None:
                    lock.maybe_renew(20.0)

                pending_updates: list[tuple[str, str, str | None]] = []

                for spec in push_specs:
                    force = spec.startswith("+")
                    clean_spec = spec.lstrip("+")
                    if ":" not in clean_spec:
                        # See _push_dry_run: a malformed spec must produce a
                        # protocol error line, not a dead helper.
                        _proto_error(clean_spec, "invalid refspec (missing ':')")
                        reported_specs.add(clean_spec)
                        continue
                    src, dst = clean_spec.split(":", 1)

                    problem = _destination_error(dst)
                    if problem:
                        _proto_error(dst, problem)
                        reported_specs.add(dst)
                        continue

                    # Case 1: Branch deletion (push :refs/heads/branch)
                    if not src:
                        ref_file = self._full_path(dst)
                        try:
                            # Read the ref before removing it, for three
                            # reasons.  It proves the target is a *file*: a ref
                            # is a file under refs/, while `delete_entry`
                            # removes files and directories alike, so deleting
                            # blind is how `:refs/heads/` took every branch with
                            # it.  It turns deleting a ref that is not there
                            # into git's own "remote ref does not exist" instead
                            # of an `ok` for a no-op.  And it gives the lease
                            # fence something to be checked *before* -- a
                            # deletion is as destructive as a ref write, and
                            # until now only writes were fenced.
                            existing = self.client.get_file_text(self.repo_id, ref_file)
                            if not existing:
                                _proto_error(dst, "remote ref does not exist")
                            elif not lock.verify_ownership():
                                state = lock.ownership_state() if hasattr(lock, "ownership_state") else ""
                                _proto_error(
                                    dst,
                                    "could not confirm lock ownership before deleting a ref "
                                    f"({describe_ownership_failure(state)})",
                                )
                            elif not self.client.delete_entry(self.repo_id, ref_file):
                                _proto_error(dst, "failed to delete ref on remote")
                            else:
                                # Forget it here as well.  The cache is read back
                                # as a push exclusion list, and it is only
                                # truthful while the ref still exists on the
                                # remote -- a stale SHA would have a later push
                                # exclude objects on the strength of a ref that
                                # is gone.
                                self._refs_cache.pop(dst, None)
                                self._prune_empty_ref_parents(dst)
                                sys.stdout.write(f"ok {dst}\n")
                        except Exception as ex:
                            _proto_error(dst, ex)
                        reported_specs.add(dst)
                        continue

                    # Case 2: Normal / Fast-forward push - validate and stage for batched packing
                    try:
                        local_sha = rev_parse(src)
                        if not local_sha:
                            _proto_error(dst, "local ref does not exist")
                            reported_specs.add(dst)
                            continue

                        # Always re-read ref under lock to catch concurrent pushes
                        remote_sha = self.client.get_file_text(self.repo_id, self._full_path(dst))
                        if remote_sha:
                            self._refs_cache[dst] = remote_sha
                        else:
                            self._refs_cache.pop(dst, None)

                        if remote_sha and not force:
                            try:
                                if not is_ancestor(remote_sha, local_sha):
                                    _proto_error(dst, "non-fast-forward")
                                    reported_specs.add(dst)
                                    continue
                            except GitError:
                                _proto_error(dst, "fetch first")
                                reported_specs.add(dst)
                                continue

                        pending_updates.append((dst, local_sha, remote_sha))
                    except Exception as ex:
                        _proto_error(dst, ex)
                        sys.stderr.write(f"\nPush error for {dst}: {ex}\n")
                        sys.stderr.flush()
                        reported_specs.add(dst)

                # Batch packfile generation and upload for all valid update targets
                if pending_updates:
                    known_remote_shas = {s for s in self._refs_cache.values() if s}
                    for _, _, r_sha in pending_updates:
                        if r_sha:
                            known_remote_shas.add(r_sha)
                    exclude = list(known_remote_shas)
                    target_shas = [u[1] for u in pending_updates]

                    try:
                        lock.maybe_renew(20.0)
                        objects_to_push = get_objects_to_push(target_shas, exclude)
                        if objects_to_push:
                            try:
                                git_dir = get_git_dir()
                            except Exception:
                                git_dir = None
                            staging_dir = Path(tempfile.mkdtemp(prefix="grs-push-", dir=str(git_dir) if git_dir else None))
                            try:
                                pack_sha, pack_data, idx_data = create_packfile(objects_to_push, staged_dir=staging_dir)
                                lock.maybe_renew(20.0)
                                if isinstance(pack_data, (bytes, bytearray)):
                                    pack_size = len(pack_data)
                                    has_pack = bool(pack_size)
                                else:
                                    pack_path = Path(pack_data)
                                    has_pack = pack_path.is_file()
                                    pack_size = pack_path.stat().st_size if has_pack else 0

                                if has_pack:
                                    size_kb = max(1, pack_size // 1024)
                                    sys.stderr.write(f"Uploading packfile pack-{pack_sha[:8]} ({size_kb} KB)...\n")
                                    sys.stderr.flush()
                                    pack_dir = self._full_path("objects/pack")
                                    self.client.upload_file(
                                        self.repo_id,
                                        pack_dir,
                                        f"pack-{pack_sha}.pack",
                                        pack_data,
                                        replace=True,
                                        progress_callback=on_upload_progress,
                                    )
                                    self.client.upload_file(
                                        self.repo_id, pack_dir, f"pack-{pack_sha}.idx", idx_data, replace=True
                                    )
                            finally:
                                shutil.rmtree(staging_dir, ignore_errors=True)
                    except Exception as ex:
                        for dst, _, _ in pending_updates:
                            _proto_error(dst, ex)
                            reported_specs.add(dst)
                        sys.stderr.write(f"\nPush pack upload error: {ex}\n")
                        sys.stderr.flush()
                        pending_updates.clear()

                    # Update remote ref files and report ok for each updated target
                    if pending_updates and not lock.verify_ownership():
                        state = lock.ownership_state() if hasattr(lock, "ownership_state") else ""
                        reason = (
                            "could not confirm lock ownership before updating refs "
                            f"({describe_ownership_failure(state)})"
                        )
                        for dst, _, _ in pending_updates:
                            _proto_error(dst, reason)
                            reported_specs.add(dst)
                        sys.stderr.write(f"\nPush error: {reason}\n")
                        sys.stderr.flush()
                        pending_updates.clear()

                    for dst, local_sha, expected_remote_sha in pending_updates:
                        try:
                            # CAS check: verify remote ref hasn't changed since fast-forward check
                            current_remote = self.client.get_file_text(self.repo_id, self._full_path(dst))
                            if current_remote != expected_remote_sha and not force:
                                _proto_error(dst, "fetch first")
                                reported_specs.add(dst)
                                continue

                            dst_parent = self._full_path(os.path.dirname(dst))
                            dst_filename = os.path.basename(dst)
                            self.client.upload_file(
                                self.repo_id,
                                dst_parent,
                                dst_filename,
                                f"{local_sha}\n".encode("utf-8"),
                                replace=True,
                            )

                            # Read the ref back.  Seafile exposes no atomic
                            # compare-and-swap, so the CAS check above and this
                            # write are separate round trips, and a peer that
                            # ignores the advisory lock can land between them:
                            # the ref would then hold *its* commit while we
                            # report ok, and the push that lost would be silent.
                            # Re-reading turns that into a reported failure.
                            #
                            # Only a *string* that differs proves a lost update.
                            # A read that cannot settle the question -- None (the
                            # ref is not there), an exception, or a client that
                            # returns something that is not a string at all -- is
                            # a warning, never an error: the upload already
                            # returned success, and telling the user a good push
                            # failed is its own bug.  The same tri-state
                            # reasoning as the lock's LEASE_HELD / LOST /
                            # UNKNOWN, and for the same reason.
                            try:
                                landed = self.client.get_file_text(
                                    self.repo_id, self._full_path(dst)
                                )
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
                                    reported_specs.add(dst)
                                    continue
                                if landed is None:
                                    sys.stderr.write(
                                        f"Warning: {dst} was not visible immediately after "
                                        "writing it; the write is unverified.\n"
                                    )
                                    sys.stderr.flush()

                            # If HEAD does not exist, set default branch
                            # Only point HEAD to a branch in refs/heads/, never to a tag
                            if dst.startswith("refs/heads/"):
                                head_path = self._full_path("HEAD")
                                if not self.client.get_file_text(self.repo_id, head_path):
                                    self.client.upload_file(
                                        self.repo_id,
                                        self.repo_path,
                                        "HEAD",
                                        f"ref: {dst}\n".encode("utf-8"),
                                        replace=True,
                                    )

                            self._refs_cache[dst] = local_sha
                            sys.stdout.write(f"ok {dst}\n")
                            reported_specs.add(dst)
                        except Exception as ex:
                            _proto_error(dst, ex)
                            sys.stderr.write(f"\nPush error for {dst}: {ex}\n")
                            sys.stderr.flush()
                            reported_specs.add(dst)

                # Decide whether tail merge is due -- but only *decide* here.
                # Running it inside this block self-deadlocks: tail_merge acquires
                # the same lock, RemoteLock is not reentrant, so the inner acquire
                # would read back our own unexpired lease, wait out the timeout and
                # fail.
                try:
                    pack_entries = self.client.list_dir(self.repo_id, self._full_path("objects/pack"))
                    freeze_at = session_config.gc_max_pack_mib * 1024 * 1024
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

                    if movable_count >= threshold:
                        # Tail merge downloads only the small packs and re-uploads
                        # their union, so above seafile.gcmaxmib it defers to a
                        # manual off-hours run no matter how many pile up.
                        if auto_gc and movable_bytes > session_config.gc_max_mib * 1024 * 1024:
                            sys.stderr.write(
                                f"\nNotice: Movable packs total {movable_bytes / (1024**3):.1f} GiB across "
                                f"{movable_count} packfile(s), above the automatic download budget "
                                f"(seafile.gcmaxmib = {session_config.gc_max_mib} MiB).\n"
                                f"Auto tail-merge was deferred: it downloads and re-uploads those packs.\n"
                                f"Run 'git-remote-seafile gc {self.raw_url}' manually when the time cost suits.\n\n"
                            )
                            sys.stderr.flush()
                        elif auto_gc:
                            auto_gc_movable_count = movable_count
                        else:
                            sys.stderr.write(
                                f"\nNotice: Remote repository has {movable_count} small packfiles (threshold: {threshold}), "
                                f"totaling {movable_bytes / (1024**3):.1f} GiB.\n"
                                f"Tip: Run 'git-remote-seafile gc {self.raw_url}' to optimize remote storage,\n"
                                f"     or run 'git config seafile.autogc true' to enable automatic tail merge\n"
                                f"     (automatic runs defer while the movable packs exceed seafile.gcmaxmib).\n\n"
                            )
                            sys.stderr.flush()
                except Exception:
                    pass
        except Exception as lock_err:
            for spec in push_specs:
                dst = spec.lstrip("+").split(":", 1)[1] if ":" in spec else spec
                if dst not in reported_specs:
                    _proto_error(dst, lock_err)
            sys.stderr.write(f"\nPush failed: {lock_err}\n")
            sys.stderr.flush()

        # Tail merge runs here, after the push lock is released, so that it can
        # take the lock for itself.  It must never disturb the per-ref ok/error
        # lines written above: the push already succeeded, and a tail-merge
        # problem is a separate, recoverable event (gc can always be run by
        # hand).  Under genuine contention with another machine this may still
        # time out, which is correct -- it just must not self-inflict it.
        if auto_gc_movable_count:
            try:
                sys.stderr.write(
                    f"Auto-merging remote repository tail ({auto_gc_movable_count} small packfiles detected)...\n"
                )
                sys.stderr.flush()
                from .gc import tail_merge

                # The threshold and budget are re-checked inside, under the lock,
                # so growth between this decision and the merge is covered.
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

        sys.stdout.write("\n")
        sys.stdout.flush()

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

            if line == "capabilities":
                self.cmd_capabilities()
            elif line.startswith("option"):
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
                elif opt in ("progress", "verbosity"):
                    sys.stdout.write("ok\n")
                else:
                    sys.stdout.write("unsupported\n")
                sys.stdout.flush()
            elif line.startswith("list"):
                for_push = "for-push" in line
                self.cmd_list(for_push=for_push)
            elif line.startswith("push"):
                push_specs = [line.removeprefix("push ").strip()]
                # Collect all remaining push lines until empty line
                while True:
                    next_line = sys.stdin.readline()
                    if not next_line or not next_line.strip():
                        break
                    if next_line.startswith("push "):
                        push_specs.append(next_line.removeprefix("push ").strip())
                self.cmd_push(push_specs)
            elif line.startswith("fetch"):
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
