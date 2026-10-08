"""helper.py - Git remote helper protocol implementation for Seafile."""

from __future__ import annotations

import os
import sys
from urllib.parse import urlparse

from .client import SeafileClient, SeafileAPIError
from .lock import RemoteLock
from .refs import REF_NAMESPACES, iter_refs
from .safety import check_preflight_safety, SafetyError
from .git_util import (
    create_packfile,
    get_objects_to_push,
    install_packfile,
    is_ancestor,
    rev_parse,
    get_git_dir,
    get_git_config_bool,
    get_git_config_int,
)


def _looks_like_host(segment: str) -> bool:
    """Heuristic: does this URL segment name a server rather than a library?

    A port ("host:8443") or a dot ("seafile.example.com") is a strong hint.
    This is only consulted for the bare form, and only when a library and a
    path follow, so a library whose name merely contains a dot is never caught
    by it.
    """
    if ":" in segment:
        return True
    return "." in segment.split("@")[-1]


class RemoteHelper:
    """Implements Git remote-helper protocol over Seafile Web API."""

    def __init__(self, remote_name: str, url: str):
        self.remote_name = remote_name
        self.raw_url = url
        self.server_url, self.library_name, self.repo_path = self._parse_url(url)
        self.client = SeafileClient(server_url=self.server_url)
        try:
            self.repo_id = self.client.get_repo_id(self.library_name)
        except Exception as ex:
            try:
                check_preflight_safety(self.client, self.library_name, self.repo_path, push_mode=False)
            except SafetyError:
                raise
            raise ex
        self._refs_cache: dict[str, str] = {}  # refname -> sha1

    def _parse_url(self, url: str) -> tuple[str | None, str, str]:
        """Parse a seafile:// URL into (server_url, library_name, repo_path).

        Accepted forms:

          seafile://<library>/<path>                  server from credentials
          seafile://<library>/<sub>/<path>            ditto, nested path
          seafile://<host>/<library>/<path>           server from the first segment
          seafile://https://<host>/<library>/<path>   server stated explicitly

        The third form is the only ambiguous one, so it is kept deliberately
        narrow: the first segment is read as a host only when a library *and* a
        path follow it, and it actually looks like a hostname.  A two-segment
        URL is therefore always <library>/<path> -- previously a library whose
        name merely contained a dot ("my.library/repo") was read as a host, and
        the library silently became "repo".  When a name really is ambiguous,
        the explicit-scheme form above settles it.
        """
        stripped = url.removeprefix("seafile://")
        server_url = None

        if stripped.startswith("http://") or stripped.startswith("https://"):
            parsed = urlparse(stripped)
            server_url = f"{parsed.scheme}://{parsed.netloc}"
            path_parts = [p for p in parsed.path.strip("/").split("/") if p]
        else:
            parts = [p for p in stripped.strip("/").split("/") if p]
            if len(parts) >= 3 and _looks_like_host(parts[0]):
                server_url = f"https://{parts[0]}"
                path_parts = parts[1:]
            else:
                path_parts = parts

        if not path_parts:
            raise ValueError(f"Invalid Seafile URL format: {url}")

        # A lone hostname-shaped segment is a server with no library named.
        # Saying that here is far clearer than treating "seafile.example.com" as
        # a library and reporting "library not found" much later, after a
        # round-trip to the server.
        if server_url is None and len(path_parts) == 1 and _looks_like_host(path_parts[0]):
            raise ValueError(
                f"'{url}' names a server but no library. Write "
                f"seafile://{path_parts[0]}/<library>/<path>, or "
                f"seafile://https://{path_parts[0]}/<library>/<path> to be explicit."
            )

        library_name = path_parts[0]
        repo_path = "/" + "/".join(path_parts[1:]) if len(path_parts) > 1 else "/git-repo"
        return server_url, library_name, repo_path

    def _full_path(self, rel_path: str) -> str:
        """Combine repo_path with relative subpath."""
        clean_rel = rel_path.strip("/")
        return f"{self.repo_path}/{clean_rel}".rstrip("/")

    def _repository_has_objects(self) -> bool:
        """True if the remote already holds at least one packfile.

        Used to tell a genuinely empty repository apart from one whose ref
        listing failed.  Returns False when the question cannot be answered, so
        that a brand-new repository -- whose objects/pack does not exist yet --
        is never mistaken for a broken one.
        """
        try:
            entries = self.client.list_dir(self.repo_id, self._full_path("objects/pack"))
        except Exception:
            return False
        return any(e.get("name", "").endswith(".pack") for e in entries)

    def cmd_capabilities(self) -> None:
        """Report supported capabilities to Git."""
        sys.stdout.write("fetch\n")
        sys.stdout.write("push\n")
        sys.stdout.write("\n")
        sys.stdout.flush()

    def cmd_list(self, for_push: bool = False) -> None:
        """List references on the remote Seafile repository."""
        self._refs_cache.clear()

        # 1. Discover branches and tags.  iter_refs walks recursively, so refs
        # whose names contain a slash ("refs/heads/feature/auth") are included.
        # A single-level listing would hide them -- and a hidden ref is not just
        # missing from the advert, it is also treated as garbage by compaction.
        listed_any = False
        for namespace in REF_NAMESPACES:
            for ref_name, sha in iter_refs(self.client, self.repo_id, self.repo_path, namespace):
                self._refs_cache[ref_name] = sha
                sys.stdout.write(f"{sha} {ref_name}\n")
                listed_any = True

        # An empty ref listing is the correct answer for a brand-new repository.
        # But Seafile reports a missing directory as 404, which is also what a
        # transient failure looks like, and from the listing alone the two are
        # indistinguishable.  So cross-check the object store: a repository that
        # already holds packfiles is not empty, and advertising it as empty makes
        # clone/fetch/ls-remote quietly do nothing while exiting 0.
        if not listed_any and self._repository_has_objects():
            raise SeafileAPIError(
                f"{self.library_name}:{self.repo_path} contains packfiles but no refs could be "
                "listed. Refusing to report an empty repository -- this is usually a transient "
                "server error. Retry, or run 'git-remote-seafile test <url>' to diagnose."
            )

        # 2. Read HEAD symbolic ref
        head_path = self._full_path("HEAD")
        head_content = self.client.get_file_text(self.repo_id, head_path)
        if head_content and head_content.startswith("ref:"):
            target_ref = head_content.split(":", 1)[1].strip()
            sys.stdout.write(f"@{target_ref} HEAD\n")

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
                getattr(self, "library_name", ""),
                getattr(self, "repo_path", ""),
                push_mode=push_mode,
            )
        except SafetyError as safe_err:
            self._report_safety_block(safe_err)
            raise
        self._report_safety_warnings(warnings)
        return warnings

    def cmd_push(self, push_specs: list[str]) -> None:
        """Process push instructions."""
        # Pre-flight safety checks (Trap 1, Trap 2, root pollution, typos)
        try:
            self._preflight_safety(push_mode=True)
        except SafetyError as safe_err:
            first_line = str(safe_err).splitlines()[0]
            for spec in push_specs:
                dst = spec.lstrip("+").split(":", 1)[1] if ":" in spec else spec
                sys.stdout.write(f"error {dst} safety check failed: {first_line}\n")
            sys.stdout.write("\n")
            sys.stdout.flush()
            return

        # Set while the push lock is held, acted on after it is released.
        auto_gc_min_packs = 0
        auto_gc_pack_count = 0

        try:
            with RemoteLock(self.client, self.repo_id, self.repo_path):
                for spec in push_specs:
                    force = spec.startswith("+")
                    clean_spec = spec.lstrip("+")
                    src, dst = clean_spec.split(":", 1)

                    # Case 1: Branch deletion (push :refs/heads/branch)
                    if not src:
                        ref_file = self._full_path(dst)
                        try:
                            if not self.client.delete_entry(self.repo_id, ref_file):
                                sys.stdout.write(f"error {dst} failed to delete ref on remote\n")
                            else:
                                # Forget it here as well.  The cache is read back
                                # as a push exclusion list, and it is only
                                # truthful while the ref still exists on the
                                # remote -- a stale SHA would have a later push
                                # exclude objects on the strength of a ref that
                                # is gone.
                                self._refs_cache.pop(dst, None)
                                sys.stdout.write(f"ok {dst}\n")
                        except Exception as ex:
                            sys.stdout.write(f"error {dst} {ex}\n")
                        continue

                    # Case 2: Normal / Fast-forward push
                    local_sha = rev_parse(src)
                    if not local_sha:
                        sys.stdout.write(f"error {dst} local ref does not exist\n")
                        continue

                    remote_sha = self._refs_cache.get(dst)
                    if not remote_sha:
                        # Refresh ref from remote in case it exists
                        remote_sha = self.client.get_file_text(self.repo_id, self._full_path(dst))

                    if remote_sha and not force:
                        if not is_ancestor(remote_sha, local_sha):
                            sys.stdout.write(f"error {dst} non-fast-forward\n")
                            continue

                    # Compute and pack missing objects
                    try:
                        exclude = [remote_sha] if remote_sha else list(self._refs_cache.values())
                        objects_to_push = get_objects_to_push(local_sha, exclude)
                        if objects_to_push:
                            pack_sha, pack_bytes, idx_bytes = create_packfile(objects_to_push)
                            if pack_bytes:
                                size_kb = max(1, len(pack_bytes) // 1024)
                                sys.stderr.write(f"Uploading packfile pack-{pack_sha[:8]} ({size_kb} KB)...\n")
                                sys.stderr.flush()
                                pack_dir = self._full_path("objects/pack")
                                self.client.upload_file(
                                    self.repo_id, pack_dir, f"pack-{pack_sha}.pack", pack_bytes, replace=True
                                )
                                self.client.upload_file(
                                    self.repo_id, pack_dir, f"pack-{pack_sha}.idx", idx_bytes, replace=True
                                )

                        # Update remote ref file
                        dst_parent = self._full_path(os.path.dirname(dst))
                        dst_filename = os.path.basename(dst)
                        self.client.upload_file(
                            self.repo_id,
                            dst_parent,
                            dst_filename,
                            f"{local_sha}\n".encode("utf-8"),
                            replace=True,
                        )

                        # If HEAD does not exist, set default branch
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
                    except Exception as ex:
                        err_line = str(ex).replace("\r", " ").replace("\n", " ").strip()
                        sys.stdout.write(f"error {dst} {err_line}\n")
                        sys.stderr.write(f"\nPush error for {dst}: {ex}\n")
                        sys.stderr.flush()

                # Decide whether compaction is due -- but only *decide* here.
                # Running it inside this block self-deadlocks: compact_repository
                # acquires the same lock, RemoteLock is not reentrant, so the
                # inner acquire would read back our own unexpired lease, wait out
                # the timeout and fail.
                try:
                    pack_entries = self.client.list_dir(self.repo_id, self._full_path("objects/pack"))
                    remote_packs = [e["name"] for e in pack_entries if e["name"].endswith(".pack")]
                    threshold = get_git_config_int("seafile.gcthreshold", 20)
                    auto_gc = get_git_config_bool("seafile.autogc", False)

                    if len(remote_packs) >= threshold:
                        if auto_gc:
                            auto_gc_min_packs = threshold
                            auto_gc_pack_count = len(remote_packs)
                        else:
                            sys.stderr.write(
                                f"\nNotice: Remote repository has {len(remote_packs)} packfiles (threshold: {threshold}).\n"
                                f"Tip: Run 'git-remote-seafile gc {self.raw_url}' to optimize remote storage,\n"
                                f"     or run 'git config seafile.autogc true' to enable automatic compaction.\n\n"
                            )
                            sys.stderr.flush()
                except Exception:
                    pass
        except Exception as lock_err:
            err_line = str(lock_err).replace("\r", " ").replace("\n", " ").strip()
            for spec in push_specs:
                dst = spec.lstrip("+").split(":", 1)[1] if ":" in spec else spec
                sys.stdout.write(f"error {dst} {err_line}\n")
            sys.stderr.write(f"\nPush failed: {lock_err}\n")
            sys.stderr.flush()

        # Compaction runs here, after the push lock is released, so that it can
        # take the lock for itself.  It must never disturb the per-ref ok/error
        # lines written above: the push already succeeded, and a compaction
        # problem is a separate, recoverable event (gc can always be run by
        # hand).  Under genuine contention with another machine this may still
        # time out, which is correct -- it just must not self-inflict it.
        if auto_gc_min_packs:
            try:
                sys.stderr.write(
                    f"Auto-compacting remote repository ({auto_gc_pack_count} packfiles detected)...\n"
                )
                sys.stderr.flush()
                from .gc import compact_repository

                compact_repository(
                    self.client,
                    self.repo_id,
                    self.repo_path,
                    min_packs=auto_gc_min_packs,
                    verbose=True,
                )
            except Exception as ex:
                err_line = str(ex).replace("\r", " ").replace("\n", " ").strip()
                sys.stderr.write(f"Auto-compaction skipped: {err_line}\n")
                sys.stderr.flush()

        sys.stdout.write("\n")
        sys.stdout.flush()

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
            git_dir = get_git_dir()
            local_pack_dir = git_dir / "objects" / "pack"
            local_packs = {p.name for p in local_pack_dir.glob("pack-*.pack")} if local_pack_dir.is_dir() else set()

            remote_pack_dir = self._full_path("objects/pack")
            remote_entries = self.client.list_dir(self.repo_id, remote_pack_dir)
            pack_files = [e["name"] for e in remote_entries if e["name"].endswith(".pack")]

            for pack_name in pack_files:
                if pack_name in local_packs:
                    continue  # already downloaded
                
                idx_name = pack_name.removesuffix(".pack") + ".idx"
                sys.stderr.write(f"Downloading {pack_name} from Seafile...\n")
                sys.stderr.flush()
                pack_bytes = self.client.get_file_bytes(self.repo_id, f"{remote_pack_dir}/{pack_name}")
                idx_bytes = self.client.get_file_bytes(self.repo_id, f"{remote_pack_dir}/{idx_name}")

                if not pack_bytes:
                    # The listing advertised this pack a moment ago, so failing
                    # to download it would leave the repository without objects
                    # its refs point at.  A compaction landing between the
                    # listing and the download does exactly this; retrying picks
                    # up the consolidated pack.  Either way, do not carry on as
                    # though the fetch had succeeded.
                    raise SeafileAPIError(
                        f"Packfile {pack_name} is listed at {remote_pack_dir} but could not be "
                        "downloaded -- the local repository would be missing objects. Retry the "
                        "fetch, or run 'git-remote-seafile test <url>' to diagnose."
                    )
                install_packfile(pack_name, pack_bytes, idx_bytes)

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
                # Unsupported command
                sys.stdout.write("\n")
                sys.stdout.flush()
