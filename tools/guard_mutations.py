"""Remove each guard in the table below, one at a time, and run the test that is meant to catch it: it must go RED.

    python tools/guard_mutations.py            # every entry (a few minutes)
    python tools/guard_mutations.py 3 14       # only those numbered

Two reviewers independently deleted guards of the blob store (round 6: 8 of 12 and 5 of 24) and the suite stayed green; this
table is how that is kept from coming back.  Add a row for every guard a test is written for (``tools/mutate_check.py`` does one
hand-picked mutation; this one keeps the list).  A row whose pattern no longer matches exactly once is reported and counts as
unchecked: fix the row when the code moves.  ``SKIPPED`` means the test did not run on this platform (the POSIX ones on
Windows, and the reverse): run it where it can.  Exit status 1 when anything survived or is unchecked.  The working tree
is restored after every row, also on Ctrl-C; do not edit files while it runs.
"""
import subprocess
import sys

T = "tests.test_blob_round6b."
B = "git_remote_seafile/blob/"
M = [
    (B + "cli.py", 'plain_http = str(client.server_url or "").lower().startswith("http://")', 'plain_http = bool(server) and str(server).lower().startswith("http://")', T + "ReadBackProofTests.test_a_short_url_over_http_turns_the_proof_on"),
    (B + "store.py", "self._drop_object(directory, ref)\n                raise", "raise", T + "ReadBackProofTests.test_a_failed_read_back_raises_and_removes_the_object"),
    (B + "store.py", "if self.verify_by_download:\n            try:", "if False:\n            try:", T + "ReadBackProofTests.test_a_failed_read_back_raises_and_removes_the_object"),
    (B + "tree.py", "if not clashing or not _folds_case(dest, dry_run):", "if True:", T + "TreeGuardTests.test_paths_that_differ_only_in_case_are_refused_on_a_volume_that_folds_case"),
    (B + "tree.py", "or bool(getattr(st, \"st_file_attributes\", 0) & 0x400)", "or False", T + "TreeGuardTests.test_a_junction_counts_as_a_link"),
    (B + "tree.py", "os.O_WRONLY | os.O_CREAT | os.O_EXCL", "os.O_WRONLY | os.O_CREAT", T + "TreeGuardTests.test_without_hard_links_a_name_taken_in_the_window_is_not_replaced"),
    (B + "tree.py", 'temp = target.with_name(f".sfblob-part-{os.urandom(8).hex()}")', 'temp = target.with_name(f"{target.name}.sfblob-part-{os.urandom(8).hex()}")', T + "TreeGuardTests.test_the_staging_name_does_not_depend_on_the_file_name"),
    (B + "tree.py", "os.walk(directory, followlinks=False, onerror=unreadable)", "os.walk(directory, followlinks=False)", T + "TreeGuardTests.test_a_folder_that_cannot_be_read_is_a_problem_not_a_silent_gap"),
    (B + "tree.py", "    if pin:\n        from . import pins\n        pins.check_name(pin)\n", "", T + "TreeGuardTests.test_a_bad_pin_name_is_refused_before_the_marker_is_written"),
    (B + "store.py", "            self._refuse_vault_neighbours()\n", "", T + "RootRefusalTests"),
    (B + "store.py", "while True:          # up to and including the library root, which is the empty path", "while parent:", T + "RootRefusalTests.test_a_store_beside_a_vault_at_the_library_root_is_refused"),
    (B + "pins.py", "            if not found:", "            if False:", T + "PinTests.test_an_empty_pin_file_is_an_error_not_a_pin_of_nothing"),
    (B + "pins.py", '    if name == "ls":', "    if False:", T + "PinTests.test_ls_is_not_a_pin_name"),
    (B + "cache.py", "            elif not _names_the_open_file(fd, path):\n                return 0", "            elif False:\n                return 0", T + "CacheTests.test_a_lock_file_that_is_no_longer_the_one_opened_is_not_unlinked"),
    (B + "cache.py", 'if os.name == "nt" and stat.S_ISREG(_lstat_mode(final)):', "if final.exists():", T + "CacheTests.test_admit_never_chmods_what_a_link_at_the_name_points_to"),
    (B + "cache.py", "if now - atime < grace:", "if now - atime < GRACE_SECONDS:", T + "CacheTests.test_an_explicit_size_evicts_what_was_just_handed_out"),
    (B + "store.py", "            if not keep:\n                part.unlink(missing_ok=True)\n            # A kept partial", "            if True:\n                part.unlink(missing_ok=True)\n            # A kept partial", T + "FetchTests.test_a_kept_partial_survives_three_dropped_transfers"),
    (B + "store.py", 'part = Path(tempfile.gettempdir()) / f"sfblob-cat-', 'part = self.cache.partial_dir() / f"sfblob-cat-', T + "FetchTests.test_cat_stages_outside_the_cache_and_cleans_up_on_ctrl_c"),
    (B + "store.py", "            if created:\n                try:", "            if False:\n                try:", T + "FetchTests.test_a_failed_get_to_leaves_no_half_file_and_the_rerun_works"),
    (B + "store.py", "        if back != body:\n            # A marker", "        if False:\n            # A marker", T + "PutGuardTests.test_the_marker_must_read_back_as_written"),
    (B + "store.py", "        if MARKER not in names and names & _GIT_ENTRIES:", "        if False:", T + "PutGuardTests.test_a_dry_run_into_a_vault_is_refused"),
    (B + "store.py", "            pins.check_name(pin)          # a bad name is refused", "            pass          # a bad name is refused", T + "PutGuardTests.test_a_pin_name_is_checked_before_the_marker_is_written"),
    (B + "store.py", "have = self._listed_size(ref, fresh=bool(pin))", "have = self._listed_size(ref)", T + "PutGuardTests.test_a_pinned_put_asks_the_server_not_a_listing_it_remembers"),
    (B + "store.py", "if self._listed_size(ref, fresh=True) != size:", "if False:", T + "PutGuardTests.test_the_listing_after_the_upload_must_show_the_size"),
    (B + "store.py", 'if e.get("type") != "dir"]      # a folder named like an object is not one', "]", T + "PutGuardTests.test_a_short_reference_does_not_match_a_folder_named_like_an_object"),
    (B + "pins.py", "    if back != body:", "    if False:", T + "PinTests.test_a_pin_must_read_back_as_written"),
    (B + "pins.py", "            if data is None:", "            if False:", T + "PinTests.test_a_pin_file_that_cannot_be_read_stops_the_read"),
    (B + "tree.py", "        return _sha256_file(target) == entry.ref.hex", "        return True", T + "TreeGuardTests.test_a_path_holding_other_bytes_of_the_same_size_is_not_in_place"),
    (B + "gcreport.py", " and name.startswith(fan_name)", "", T + "GcReportTests.test_the_report_names_what_is_not_the_stores"),
    (B + "gcreport.py", "len({r.hex for r in wanted})", "len(wanted)", T + "GcReportTests.test_a_reference_named_twice_is_one_object_of_the_pin"),
    (B + "cli.py", "if not wanted and not args.record:", "if not wanted:", T + "CommandGuardTests.test_an_empty_record_verifies_nothing_and_not_the_whole_store"),
    (B + "cli.py", "    if args.clear and args.size:", "    if False:", T + "CommandGuardTests.test_clear_and_size_together_are_a_usage_error"),
    (B + "cli.py", "        if args.refs:\n            if args.refs[0] not in held:", "        if False:\n            if args.refs[0] not in held:", T + "CommandGuardTests.test_pin_ls_names_one_pin_or_all_and_refuses_extra_words"),
    (B + "cli.py", "    if args.command in NOT_YET:", "    if False:", T + "CommandGuardTests.test_a_command_not_built_yet_says_so_even_after_a_global_flag"),
    (B + "cli.py", 'risk = base.client.hidden_history_risk(base.repo_id, noun="pins")', "risk = None", T + "CommandGuardTests.test_the_pro_warning_reaches_standard_error_in_gc"),
    ("git_remote_seafile/gitcred.py", "if not token or _BAD.search(token):", "if not token:", T + "SmallFixTests.test_a_token_with_a_line_feed_is_never_handed_to_git"),
    ("git_remote_seafile/snapshot/restore_only.py", ' or re.match(r"^[A-Za-z]:", path)', "", T + "SmallFixTests.test_a_drive_letter_behind_dot_slash_is_refused"),
    ("git_remote_seafile/umbrella.py", "sys.argv = saved ", "pass ", T + "SmallFixTests.test_the_umbrella_keeps_the_callers_argv_and_answers_version"),
    ("git_remote_seafile/client.py", "if not asked_git and self._credentials_from_git", "if self._credentials_from_git", T + "SmallFixTests.test_git_is_asked_for_a_credential_once_when_the_config_asks_for_it"),
    # round 7 (Opus)
    (B + "store.py", "            except ReadBackMismatch:\n                # Wrong bytes", "            except StoreError:\n                # Wrong bytes", T + "RoundSevenTests.test_a_read_that_failed_proves_nothing_and_deletes_nothing"),
    (B + "store.py", 'if unknown.get("incompat"):', "if False:", T + "RoundSevenTests.test_an_unknown_incompat_feature_refuses_everything"),
    (B + "store.py", "        self.check_root(create=True)\n        self.require_writable()", "        self.check_root(create=True)", T + "RoundSevenTests.test_an_unknown_ro_compat_feature_lets_a_store_be_read_but_not_written"),
    (B + "store.py", "        self.check_root(create=False)          # a full reference skips", "        pass          # a full reference skips", T + "RoundSevenTests.test_an_unknown_incompat_feature_refuses_everything"),
    (B + "pins.py", "if isinstance(listed, int) and not isinstance(listed, bool) and listed != len(data):", "if False:", T + "RoundSevenTests.test_a_pin_body_shorter_than_the_listing_says_is_refused"),
    (B + "tree.py", '        if not any(unicodedata.normalize("NFC", n)', '        if False and not any(unicodedata.normalize("NFC", n)', T + "CaseSpellingTests.test_a_file_found_under_another_spelling_is_not_in_place"),
    (B + "record.py", 'if text.startswith("\\ufeff"):', "if False:", T + "RecordRoundSevenTests.test_a_path_cannot_start_with_a_byte_order_mark"),
    (B + "record.py", "if len(fields) > 3:", "if False:", T + "RecordRoundSevenTests.test_a_line_with_more_fields_says_a_newer_version_may_have_written_it"),
    (B + "gcreport.py", "if name in _NEWER_ROOT:", "if False:", T + "RoundSevenTests.test_the_report_keeps_a_newer_versions_entries_apart_from_foreign_ones"),
    # round 7, from Gemini, DeepSeek and Qwen
    (B + "store.py", "        # one byte long is a CR, which is JSON whitespace and reads fine; anything that does not parse fails here.\n        self._read_marker()", "        # one byte long is a CR, which is JSON whitespace and reads fine; anything that does not parse fails here.\n        pass", T + "PutGuardTests.test_a_marker_that_is_not_a_marker_when_read_back_is_refused"),
    (B + "store.py", "        # one byte long is a CR, which is JSON whitespace and reads fine; anything that does not parse fails here.\n        self._read_marker()", "        if self.client.get_file_bytes(self.repo_id, f\"{self.root}/{MARKER}\") != body:\n            raise StoreError(\"did not read back\")", T + "PutGuardTests.test_two_first_puts_both_succeed_and_the_winners_marker_stays"),
    (B + "store.py", "            self._refuse_unfit_root(entries, names)          # what the real put would refuse", "            pass          # what the real put would refuse", T + "DryRunRefusalTests"),
    (B + "tree.py", "    if dry_run or not dest.is_dir():", "    if not dest.is_dir():", T + "TreeRoundSevenTests.test_a_dry_run_writes_no_probe_file"),
    (B + "tree.py", "        return True          # cannot tell", "        return False          # cannot tell", T + "TreeRoundSevenTests.test_a_probe_that_cannot_be_made_counts_the_names_as_colliding"),
    (B + "tree.py", "        if entry.path in folders:", "        if False:", T + "TreeRoundSevenTests.test_a_file_and_a_folder_of_the_same_name_in_one_record_are_refused_everywhere"),
    (B + "tree.py", '            if any("/".join(parts[:n]) in folder_clashes for n in range(1, len(parts))):', "            if False:", T + "TreeRoundSevenTests.test_a_folder_and_a_file_that_differ_only_in_case_are_refused_where_case_folds"),
    (B + "store.py", "            if not place_new(stage, destination):\n                raise StoreError(f\"{destination} already exists; get does not overwrite\")", "            os.replace(stage, destination)", T + "TreeRoundSevenTests.test_get_to_that_loses_a_race_for_the_name_keeps_the_other_file"),
    (B + "gcreport.py", "or not _FANOUT.fullmatch(fan_name):", "or len(fan_name) != 2:", T + "ReportAndPinRoundSevenTests.test_the_markers_copy_a_pin_comment_and_a_wrong_fanout_name"),
    (B + "gcreport.py", "elif _TEMPORARY.fullmatch(name) and name.startswith(fan_name):", "elif _TEMPORARY.fullmatch(name):", T + "ReportAndPinRoundSevenTests.test_the_markers_copy_a_pin_comment_and_a_wrong_fanout_name"),
    (B + "gcreport.py", '_MARKER_COPY = re.compile(r" \\((\\d+|SFConflict [^)]*)\\)" + re.escape(MARKER) + "|" + ', '_MARKER_COPY = re.compile(r"(?!)" + "|" + ', T + "ReportAndPinRoundSevenTests.test_the_markers_copy_a_pin_comment_and_a_wrong_fanout_name"),
    (B + "pins.py", 'enumerate(text.split("\\n"), 1)', "enumerate(text.splitlines(), 1)", T + "ReportAndPinRoundSevenTests.test_a_pin_file_may_carry_comment_lines_and_u2028_is_not_a_line_break"),
    (B + "pins.py", ' or line.lstrip().startswith("#")', "", T + "ReportAndPinRoundSevenTests.test_a_pin_file_may_carry_comment_lines_and_u2028_is_not_a_line_break"),
    ("git_remote_seafile/client.py", "timeout=self.timeout, allow_redirects=False", "timeout=self.timeout", T + "RenameRedirectTests.test_a_301_names_the_landing_in_its_location_and_is_not_followed"),
    ("git_remote_seafile/client.py", "        if resp.status_code in (301, 302):", "        if False:", T + "RenameRedirectTests.test_a_301_names_the_landing_in_its_location_and_is_not_followed"),
    (B + "cli.py", "        exit_code = 1          # the store is not what its pins say", "        exit_code = 0          # the store is not what its pins say", "tests.test_blob_cli.CommandTests.test_gc_warns_about_a_pinned_object_that_is_not_in_the_store"),
    ("tools/gates.py", '                modules.update(_blob_tests(root))\n                modules.update({"test_entry_points", "test_docs_consistency"})', '                modules.update({"test_entry_points", "test_docs_consistency"})', T + "GatesMappingTests"),
    ("git_remote_seafile/client.py", "                if not unchanged:\n                    file_resp.close()", "                if False:\n                    file_resp.close()", "tests.test_client_resume.ResumeTests.test_a_partial_that_changes_while_the_request_runs_is_not_appended_to"),
]

only = sys.argv[1:]
survived = []
for n, (path, old, new, test) in enumerate(M):
    if only and str(n) not in only:
        continue
    src = open(path, encoding="utf-8", newline="").read()
    raw = src.replace("\r\n", "\n")
    if raw.count(old) != 1:
        print(n, "PATTERN NOT FOUND/UNIQUE", old[:50], raw.count(old))
        survived.append(n)
        continue
    try:
        open(path, "w", encoding="utf-8", newline="").write(raw.replace(old, new))
        r = subprocess.run([sys.executable, "-m", "unittest", test], capture_output=True, text=True, timeout=300)
        tail = [l for l in (r.stderr or "").splitlines() if l.startswith(("OK", "FAILED"))]
        red = r.returncode != 0
        skipped = "skipped" in (tail[0] if tail else "") and not red
        errored = red and bool(tail) and "errors=" in tail[0] and "failures=" not in tail[0]
        label = "RED (error: read it)" if errored else ("RED " if red else ("SKIPPED" if skipped else "GREEN <-- survived"))
        print(n, label, test.split(".")[-1][:70], tail[:1])
        if not red:
            survived.append(n)
    finally:
        open(path, "w", encoding="utf-8", newline="").write(src)
print("survivors/unchecked:", survived)
sys.exit(1 if survived else 0)
