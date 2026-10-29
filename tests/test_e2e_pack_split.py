"""test_e2e_pack_split.py - real `git push` against a stub that answers 413 to a big body.

The unit tests in test_pack_split.py fake the pack build and the upload; this one
runs git, the helper, the client and the stub's ``max_upload`` fault (nginx's
``client_max_body_size``) end to end, so the claim "a push the proxy would have
refused goes through in smaller packs" is exercised, not assumed.
"""

from __future__ import annotations

import os
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from e2e_harness import REPO_ID, commit_file, init_repo, run_git  # noqa: E402
from test_e2e import E2ETestCase  # noqa: E402

MIB = 1024 * 1024
LIMIT = 10 * MIB


def add_random_files(repo: pathlib.Path, prefix: str, count: int, size: int, env: dict) -> dict[str, bytes]:
    """Incompressible files: the pack is as big as the data, so a 10 MiB limit really bites."""
    made = {}
    for n in range(count):
        name = f"{prefix}{n}.bin"
        made[name] = os.urandom(size)
        (repo / name).write_bytes(made[name])
        run_git(["add", "--", name], repo, env)
    run_git(["-c", "user.email=e2e@example.com", "-c", "user.name=E2E Tester",
             "commit", "-q", "-m", f"add {prefix}"], repo, env)
    return made


class TestPushThroughABodyLimit(E2ETestCase):
    def packs(self, name: str) -> dict[str, int]:
        return {
            path: len(data) for path, data in self.stub.files.get(REPO_ID, {}).items()
            if path.startswith(f"/{name}/objects/pack/") and path.endswith(".pack")
        }

    def test_a_push_the_proxy_would_refuse_goes_through_in_smaller_packs(self):
        src = init_repo(self.work / "src_split", self.env)
        commit_file(src, "README.txt", "base\n", "base", self.env)
        run_git(["remote", "add", "origin", self.url("split1")], src, self.env)
        made = add_random_files(src, "big", 6, 3 * MIB, self.env)   # ~18 MiB of random bytes
        self.stub.faults["max_upload"] = LIMIT

        proc = run_git(["push", "-u", "origin", "main"], src, self.env)

        self.assertIn("the server refused an upload of", proc.stderr)
        self.assertIn("Notice: this push went through in packs of at most", proc.stderr)
        remembered = run_git(["config", "--local", "--get", "seafile.gcmaxpackmib"], src, self.env).stdout.strip()
        self.assertTrue(1 <= int(remembered) < 10, remembered)
        packs = self.packs("split1")
        self.assertGreaterEqual(len(packs), 2, packs)
        self.assertTrue(all(size <= LIMIT for size in packs.values()), packs)

        # What arrived is the repository: a clone has every byte.
        dst = self.work / "clone_split"
        run_git(["clone", self.url("split1"), str(dst)], self.work, self.env)
        for name, data in made.items():
            self.assertEqual((dst / name).read_bytes(), data, name)

    def test_the_next_push_starts_from_the_remembered_cap_and_is_not_refused_again(self):
        src = init_repo(self.work / "src_split2", self.env)
        commit_file(src, "README.txt", "base\n", "base", self.env)
        run_git(["remote", "add", "origin", self.url("split2")], src, self.env)
        add_random_files(src, "one", 6, 3 * MIB, self.env)
        self.stub.faults["max_upload"] = LIMIT
        run_git(["push", "-u", "origin", "main"], src, self.env)

        add_random_files(src, "two", 6, 3 * MIB, self.env)
        proc = run_git(["push", "origin", "main"], src, self.env)

        self.assertNotIn("refused an upload", proc.stderr)
        self.assertNotIn("Notice: this push went through", proc.stderr)

    def test_a_server_that_takes_everything_is_not_given_a_cap_or_a_warning(self):
        src = init_repo(self.work / "src_split3", self.env)
        commit_file(src, "README.txt", "base\n", "base", self.env)
        run_git(["remote", "add", "origin", self.url("split3")], src, self.env)
        add_random_files(src, "big", 3, 3 * MIB, self.env)

        proc = run_git(["push", "-u", "origin", "main"], src, self.env)

        self.assertNotIn("refused an upload", proc.stderr)
        proc = run_git(["config", "--local", "--get", "seafile.gcmaxpackmib"], src, self.env, check=False)
        self.assertEqual(proc.returncode, 1)


if __name__ == "__main__":
    unittest.main()
