import contextlib
import io
import importlib.util
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import sapper  # noqa: E402

SETTINGS_PATH = ROOT / "lib" / "_settings_patch.py"
_spec = importlib.util.spec_from_file_location("settings_patch_for_tests", SETTINGS_PATH)
settings_patch = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(settings_patch)


class GraphIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = sapper.Store(Path(self.tmp.name) / ".sapper")
        self.store.init("test", "ship it")

    def tearDown(self):
        self.tmp.cleanup()

    def test_corrupt_node_is_skipped_and_reported_by_validate(self):
        (self.store.facts_dir / "f001.json").write_text('{"id":"f001"}\n', encoding="utf-8")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rendered = sapper.render_graph_text(self.store)
        self.assertIn("goal: ship it", rendered)
        self.assertIn("missing string 'description'", stderr.getvalue())
        errors = sapper.validate_graph(self.store)
        self.assertTrue(any("f001.json" in error for error in errors))

    def test_init_refuses_non_empty_directory(self):
        root = Path(self.tmp.name) / "new-store"
        root.mkdir()
        (root / "unrelated.txt").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(sapper.FGError, "non-empty directory"):
            sapper.Store(root).init("", "goal")
        self.assertTrue((root / "unrelated.txt").exists())

    def test_validate_detects_goal_metadata_drift(self):
        goal = self.store.facts_dir / "goal.json"
        data = json.loads(goal.read_text(encoding="utf-8"))
        data["description"] = "different goal"
        goal.write_text(json.dumps(data), encoding="utf-8")
        errors = sapper.validate_graph(self.store)
        self.assertIn("goal fact description does not match project goal", errors)

    def test_validate_detects_intent_state_mismatch(self):
        seed = self.store.add_fact("seed", "test", None)
        intent = self.store.add_intent([seed["id"]], "work", "test")
        path = self.store.intents_dir / f"{intent['id']}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = "done"
        path.write_text(json.dumps(data), encoding="utf-8")
        errors = sapper.validate_graph(self.store)
        self.assertTrue(any("completed status requires a target" in error for error in errors))

    def test_confirmation_is_enforced_at_write_boundary(self):
        fact = self.store.add_fact("starting point", "test", None)
        intent = self.store.add_intent(
            [fact["id"]], "delete data", "test", requires_confirmation=True
        )
        with self.assertRaisesRegex(sapper.FGError, "requires confirmation"):
            self.store.conclude_with_new_fact(intent["id"], "must not run", "test")
        self.store.confirm_intent(intent["id"])
        self.store.claim_intent(intent["id"], "owner")
        with self.assertRaisesRegex(sapper.FGError, "claimed by owner"):
            self.store.conclude_with_new_fact(intent["id"], "wrong worker", "other")
        result, concluded = self.store.conclude_with_new_fact(
            intent["id"], "approval was recorded", "owner"
        )
        self.assertEqual(concluded["to"], result["id"])

    def test_lookup_ids_cannot_escape_store(self):
        with self.assertRaisesRegex(sapper.FGError, "invalid fact id"):
            self.store.get_fact("../../outside")

    def test_stale_counter_never_overwrites_existing_node(self):
        first = self.store.add_fact("first", "test", None)
        project = self.store.load_project()
        project["counters"]["fact"] = 0
        self.store.save_project(project)
        second = self.store.add_fact("second", "test", None)
        self.assertEqual(first["id"], "f001")
        self.assertEqual(second["id"], "f002")
        self.assertEqual(self.store.get_fact("f001")["description"], "first")

    def test_fact_from_intent_closes_the_intent_atomically(self):
        seed = self.store.add_fact("seed", "test", None)
        intent = self.store.add_intent([seed["id"]], "observe", "test")
        fact = self.store.add_fact("observed", "test", intent["id"])
        self.assertEqual(self.store.get_intent(intent["id"])["to"], fact["id"])
        self.assertEqual(sapper.validate_graph(self.store), [])

    def test_linking_existing_fact_sets_its_source_edge(self):
        seed = self.store.add_fact("seed", "test", None)
        result = self.store.add_fact("already observed", "test", None)
        intent = self.store.add_intent([seed["id"]], "link result", "test")
        self.store.conclude_intent(intent["id"], result["id"], "test")
        self.assertEqual(self.store.get_fact(result["id"])["source_intent"], intent["id"])
        self.assertEqual(sapper.validate_graph(self.store), [])

    def test_pick_claim_skips_another_workers_lease(self):
        seed = self.store.add_fact("seed", "test", None)
        first = self.store.add_intent([seed["id"]], "first", "test")
        second = self.store.add_intent([seed["id"]], "second", "test")
        self.store.claim_intent(first["id"], "other-worker")
        args = type("Args", (), {
            "store": str(self.store.root), "claim": True,
            "worker": "my-worker", "any": False,
        })()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            sapper.cmd_pick(args)
        self.assertEqual(output.getvalue().strip(), second["id"])

    def test_static_view_escapes_script_terminators(self):
        self.store.add_fact("</script><script>alert(1)</script>", "test", None)
        html = sapper._render_static_html(self.store)
        self.assertNotIn("</script><script>", html)
        self.assertIn(r"\u003c/script\u003e", html)

    def test_prompt_substitution_does_not_rewrite_inserted_braces(self):
        self.store.add_fact("literal {goal} marker", "test", None)
        prompt = sapper.render_prompt(self.store, "{graph_yaml}\nGOAL={goal}")
        self.assertIn("literal {goal} marker", prompt)
        self.assertTrue(prompt.endswith("GOAL=ship it"))

    def test_brainstorm_template_token_does_not_rewrite_graph_text(self):
        self.store.add_fact("literal {max_options} marker", "test", None)
        args = type("Args", (), {
            "max_options": 2, "dry_run": True,
        })()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            sapper._dispatch_brainstorm(
                self.store, ROOT / "templates", "sonnet", 10, False, ROOT, args
            )
        self.assertIn("literal {max_options} marker", output.getvalue())

    def test_verify_payload_requires_actionable_issues_on_failure(self):
        with self.assertRaisesRegex(sapper.FGError, "explain"):
            sapper._parse_verify_payload({"verified": False, "issues": ""})
        with self.assertRaisesRegex(sapper.FGError, "leave 'issues' empty"):
            sapper._parse_verify_payload({"verified": True, "issues": "warning"})

    def test_dispatch_verify_returns_failure_status(self):
        seed = self.store.add_fact("seed", "test", None)
        intent = self.store.add_intent([seed["id"]], "check", "test")
        result, _ = self.store.conclude_with_new_fact(intent["id"], "result", "test")
        args = type("Args", (), {
            "intent": intent["id"], "dry_run": False,
        })()
        with mock.patch.object(
            sapper,
            "run_claude",
            return_value={"result": '{"accepted": true, "data": {"verified": false, "issues": "mismatch"}}'},
        ):
            rc = sapper._dispatch_verify(
                self.store, ROOT / "templates", "sonnet", 10, False, ROOT, args
            )
        self.assertEqual(rc, 1)

    def test_completed_graph_validates_and_duplicate_completion_is_rejected(self):
        fact = self.store.add_fact("evidence", "test", None)
        self.store.complete_project([fact["id"]], "finish", "test")
        self.assertEqual(self.store.load_project()["status"], "completed")
        self.assertEqual(sapper.validate_graph(self.store), [])
        with self.assertRaisesRegex(sapper.FGError, "already completed"):
            self.store.complete_project([fact["id"]], "finish again", "test")

    def test_brainstorm_batches_are_unique(self):
        seed = self.store.add_fact("seed", "test", None)
        payload = {
            "accepted": True,
            "data": {
                "options": [
                    {"from": [seed["id"]], "title": "one", "description": "first"}
                ]
            },
        }
        sapper._apply_brainstorm(self.store, payload)
        sapper._apply_brainstorm(self.store, payload)
        docs = [i["doc"] for i in self.store.list_intents()]
        self.assertEqual(len(docs), 2)
        tags = [line for doc in docs for line in doc.splitlines() if line.startswith("[brainstorm batch:")]
        self.assertEqual(len(set(tags)), 2)

    def test_brainstorm_pick_preserves_abandoned_edges(self):
        seed = self.store.add_fact("seed", "test", None)
        payload = {
            "accepted": True,
            "data": {"options": [
                {"from": [seed["id"]], "title": "one", "description": "first"},
                {"from": [seed["id"]], "title": "two", "description": "second"},
            ]},
        }
        sapper._apply_brainstorm(self.store, payload)
        picked = self.store.list_intents()[0]
        args = type("Args", (), {"store": str(self.store.root), "id": picked["id"], "purge": False})()
        sapper.cmd_brainstorm_pick(args)
        intents = self.store.list_intents()
        self.assertEqual(sum(i["to"] is None for i in intents), 1)
        self.assertEqual(sum(i["to"] is not None for i in intents), 1)
        self.assertEqual(sapper.validate_graph(self.store), [])


class SettingsPatchTests(unittest.TestCase):
    def test_settings_patch_is_atomic_and_preserves_unrelated_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(
                json.dumps({"hooks": {"SessionStart": [{"matcher": "x", "hooks": [
                    {"type": "command", "command": "echo keep"
                    }
                ]}]}}),
                encoding="utf-8",
            )
            changed, _ = settings_patch.add(path, "/tmp/sapper-hook.py")
            self.assertTrue(changed)
            data = json.loads(path.read_text(encoding="utf-8"))
            commands = [
                h["command"]
                for groups in data["hooks"].values()
                for group in groups
                for h in group.get("hooks", [])
            ]
            self.assertIn("echo keep", commands)
            self.assertEqual(sum("sapper-hook.py" in command for command in commands), 2)
            settings_patch.remove(path, "/tmp/sapper-hook.py")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["hooks"]["SessionStart"][0]["hooks"][0]["command"], "echo keep")

    def test_settings_patch_rejects_non_object_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "JSON object"):
                settings_patch.add(path, "/tmp/sapper-hook.py")
            with self.assertRaisesRegex(ValueError, "JSON object"):
                settings_patch.remove(path, "/tmp/sapper-hook.py")

    def test_settings_validation_catches_malformed_event_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"hooks": {"SessionStart": {}}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hooks.SessionStart"):
                settings_patch.validate(path)


class PeerConfigTests(unittest.TestCase):
    def test_malformed_peer_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = sapper.Store(Path(tmp) / ".sapper")
            store.init("", "peers")
            (store.root / sapper.PEERS_FILE).write_text(
                json.dumps({"version": 1, "channel": "x", "peers": {"bad name": {"target": "x"}}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(sapper.FGError, "invalid peer name"):
                sapper._load_peers(store)

    def test_setup_preflights_invalid_settings_before_creating_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".claude").mkdir()
            (project / ".claude" / "settings.json").write_text("[]", encoding="utf-8")
            old_cwd = Path.cwd()
            try:
                import os
                os.chdir(project)
                args = type("Args", (), {
                    "store": ".sapper", "goal": "should not initialize",
                    "origin": "", "agents": False, "force": False,
                })()
                with self.assertRaisesRegex(sapper.FGError, "JSON object"):
                    sapper.cmd_setup(args)
            finally:
                os.chdir(old_cwd)
            self.assertFalse((project / ".sapper" / "project.json").exists())


if __name__ == "__main__":
    unittest.main()


class TypedFactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = sapper.Store(Path(self.tmp.name) / ".sapper")
        self.store.init("test", "glitch the boot rom")

    def tearDown(self):
        self.tmp.cleanup()

    def test_typed_fact_roundtrip_and_render(self):
        fact = self.store.add_fact(
            "SWD interface on target chip, pins PA13/PA14", "user",
            kind="interface", data={"chip": "stm32f103", "pins": "PA13,PA14", "baud": "0"},
        )
        self.assertEqual(fact["kind"], "interface")
        self.assertEqual(fact["data"]["pins"], "PA13,PA14")
        text = sapper.render_graph_text(self.store)
        self.assertIn("<interface>", text)
        self.assertIn("pins=PA13,PA14", text)
        yaml = sapper._to_yaml_like(sapper.build_graph_view(self.store))
        self.assertIn("<interface>", yaml)
        # prompt rendering scrubs braces so flag{...} cannot eat placeholders
        self.assertIn("pins=PA13,PA14", yaml)

    def test_unknown_kind_and_bad_data_rejected(self):
        with self.assertRaises(sapper.FGError):
            self.store.add_fact("x", "user", kind="exploit")
        with self.assertRaises(sapper.FGError):
            self.store.add_fact("x", "user", kind="cred")
        with self.assertRaises(sapper.FGError):
            self.store.add_fact("x", "user", kind="host")
        with self.assertRaises(sapper.FGError):
            self.store.add_fact(
                "x", "user", kind="interface", data={"bad key!": "1"})

    def test_validate_rejects_invalid_kind_on_disk(self):
        fact = self.store.add_fact("x", "user", kind="interface")
        p = self.store.facts_dir / f"{fact['id']}.json"
        node = json.loads(p.read_text(encoding="utf-8"))
        node["kind"] = "exploit"
        p.write_text(json.dumps(node), encoding="utf-8")
        errors = sapper.validate_graph(self.store)
        self.assertTrue(any("kind" in e for e in errors))

    def test_explore_payload_carries_kind_and_data(self):
        payload = {"accepted": True, "data": {
            "description": "glitch hit confirmed",
            "kind": "glitch_result",
            "data": {"outcome": "success", "attempts": "12"},
        }}
        title, desc, doc, kind, data = sapper._parse_explore_payload(payload)
        self.assertEqual(kind, "glitch_result")
        self.assertEqual(data, {"outcome": "success", "attempts": "12"})


class EvidenceLevelSoftWarningTests(unittest.TestCase):
    """C6: evidence_level/confidence are advisory — warn, never reject."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = sapper.Store(Path(self.tmp.name) / ".sapper")
        self.store.init("test", "glitch the boot rom")

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_metadata_produces_no_warnings(self):
        self.store.add_fact(
            "single success at ext_offset=47", "user",
            kind="glitch_result",
            data={"evidence_level": "observation", "confidence": "high"},
        )
        self.assertEqual(sapper.fact_soft_warnings(self.store), [])

    def test_missing_metadata_warns_but_fact_stays_valid(self):
        self.store.add_fact("suspicious timing", "user", kind="glitch_result")
        warns = sapper.fact_soft_warnings(self.store)
        self.assertTrue(any("evidence_level" in w for w in warns))
        self.assertTrue(any("confidence" in w for w in warns))
        # hard errors stay empty: the fact is valid
        self.assertEqual(sapper.validate_graph(self.store), [])

    def test_out_of_vocabulary_value_warns(self):
        self.store.add_fact(
            "guess", "user",
            kind="vulnerability",
            data={"evidence_level": "certainty", "confidence": "certain"},
        )
        warns = sapper.fact_soft_warnings(self.store)
        self.assertTrue(any("'evidence_level'" in w and "certainty" in w for w in warns))
        self.assertTrue(any("'confidence'" in w and "certain" in w for w in warns))
        self.assertEqual(sapper.validate_graph(self.store), [])

    def test_unknown_literal_accepted_for_both_fields(self):
        self.store.add_fact(
            " unsure note", "user",
            kind="analysis_note",
            data={"evidence_level": "unknown", "confidence": "unknown"},
        )
        self.assertEqual(sapper.fact_soft_warnings(self.store), [])

    def test_untyped_fact_skipped(self):
        self.store.add_fact("plain observation", "user")
        self.assertEqual(sapper.fact_soft_warnings(self.store), [])

    def test_cmd_validate_prints_warnings_without_failing(self):
        import io
        import contextlib
        self.store.add_fact("no metadata yet", "user", kind="glitch_result")
        stdout = io.StringIO()
        args = type("A", (), {"store": str(self.store.root)})()
        with contextlib.redirect_stdout(stdout):
            rc = sapper.cmd_validate(args)
        text = stdout.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("graph valid", text)
        self.assertIn("soft warnings", text)
        self.assertIn("evidence_level", text)


class FindingLedgerTests(unittest.TestCase):
    """A3: the finding ledger keeps the flag-ledger mechanics (dedupe +
    record-first + lifecycle) with hardware semantics: kind=glitch_result,
    dedupe key (challenge/campaign, data.value), states observed/confirmed/rejected."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = sapper.Store(Path(self.tmp.name) / ".sapper")
        self.store.init("test", "glitch the boot rom")

    def tearDown(self):
        self.tmp.cleanup()

    def test_finding_record_and_dedupe(self):
        fact = self.store.add_fact(
            "finding recorded: ext_offset=47 width=32 skip", "agent",
            kind="glitch_result",
            data={"value": "ext_offset=47 width=32 skip", "challenge": "c01", "status": "observed"},
        )
        dup = sapper._find_finding_fact(self.store, "ext_offset=47 width=32 skip")
        self.assertEqual(dup["id"], fact["id"])
        self.assertIsNone(
            sapper._find_finding_fact(self.store, "a different result"))

    def test_finding_dedupe_campaign_scoped(self):
        self.store.add_fact(
            "finding recorded: r1", "agent",
            kind="glitch_result",
            data={"value": "r1", "challenge": "c01", "status": "observed"},
        )
        self.assertIsNotNone(
            sapper._find_finding_fact(self.store, "r1", campaign="c01"))
        self.assertIsNone(
            sapper._find_finding_fact(self.store, "r1", campaign="c02"))

    def test_finding_status_lifecycle_and_type_guard(self):
        fact = self.store.add_fact(
            "finding recorded: r1", "agent",
            kind="glitch_result", data={"value": "r1", "status": "observed"},
        )
        updated = self.store.update_finding_fact(fact["id"], "confirmed", '{"rate":0.4}')
        self.assertEqual(updated["data"]["status"], "confirmed")
        self.assertEqual(updated["data"]["result"], '{"rate":0.4}')
        plain = self.store.add_fact("just text", "agent")
        with self.assertRaises(sapper.FGError):
            self.store.update_finding_fact(plain["id"], "confirmed", None)
        # a dispatch-produced glitch_result without ledger keys is protected too
        bare = self.store.add_fact(
            "batch result", "agent", kind="glitch_result",
            data={"outcome": "crash", "attempts": "20"},
        )
        with self.assertRaises(sapper.FGError):
            self.store.update_finding_fact(bare["id"], "confirmed", None)
        with self.assertRaises(sapper.FGError):
            self.store.update_finding_fact(fact["id"], "hacked-status", None)
        with self.assertRaises(sapper.FGError):
            self.store.update_finding_fact(fact["id"], "submitted", None)

    def test_cmd_finding_records_glitch_result_fact(self):
        args = type("A", (), {
            "store": str(self.store.root),
            "value": "ext_offset=47 width=32 skip",
            "challenge": "c01", "title": None, "creator": "agent",
        })()
        import io, contextlib
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = sapper.cmd_finding(args)
        self.assertEqual(rc, 0)
        fact = sapper._find_finding_fact(self.store, "ext_offset=47 width=32 skip")
        self.assertEqual(fact["kind"], "glitch_result")
        self.assertEqual(fact["data"]["status"], "observed")
        # second identical record is rejected (dedupe)
        with self.assertRaises(sapper.FGError):
            sapper.cmd_finding(args)

    def test_status_shows_finding_ledger(self):
        self.store.add_fact(
            "finding recorded: r9", "agent",
            kind="glitch_result",
            data={"value": "r9", "challenge": "c09", "status": "confirmed"},
        )
        text = sapper.render_status(self.store)
        self.assertIn("finding:", text)
        self.assertIn("status=confirmed", text)
        self.assertIn("challenge=c09", text)


class DeadlineTests(unittest.TestCase):
    def test_minutes_and_iso_and_conflict(self):
        import time as _t
        a = type("A", (), {"deadline": None, "minutes": 5})()
        d = sapper.deadline_from_args(a)
        self.assertGreater(d, _t.time())
        b = type("B", (), {"deadline": "2026-10-17T13:00:00", "minutes": None})()
        self.assertIsNotNone(sapper.deadline_from_args(b))
        c = type("C", (), {"deadline": "2026-10-17T13:00:00+08:00", "minutes": 5})()
        with self.assertRaises(sapper.FGError):
            sapper.deadline_from_args(c)
        bad = type("D", (), {"deadline": "not-a-date", "minutes": None})()
        with self.assertRaises(sapper.FGError):
            sapper.deadline_from_args(bad)
        self.assertEqual(sapper.deadline_from_args(type("E", (), {"deadline": None, "minutes": None})()), None)

    def test_format_time_budget(self):
        self.assertEqual(sapper.format_time_budget(None), "")
        self.assertIn("TIME IS UP",
                      sapper.format_time_budget(0))  # 0 -> epoch 1970, always past
        self.assertIn("remaining", sapper.format_time_budget(_t.time() + 600))


import time as _t  # noqa: E402  (DeadlineTests)


class CommanderTests(unittest.TestCase):
    def test_parse_command_payload(self):
        ok = {"accepted": True, "data": {"action": "work", "target": "c03", "minutes": 25, "note": "close"}}
        self.assertEqual(sapper._parse_command_payload(ok), ("work", "c03", 25, "close"))
        stop = {"accepted": True, "data": {"action": "stop", "reason": "all done"}}
        self.assertEqual(sapper._parse_command_payload(stop), ("stop", "", 0, "all done"))
        rej = {"accepted": False, "reason": "bad board"}
        self.assertEqual(sapper._parse_command_payload(rej)[0], "reject")
        with self.assertRaises(sapper.FGError):
            sapper._parse_command_payload({"accepted": True, "data": {"action": "hack"}})
        with self.assertRaises(sapper.FGError):
            sapper._parse_command_payload({"accepted": True, "data": {"action": "work", "minutes": 10}})
        with self.assertRaises(sapper.FGError):
            sapper._parse_command_payload({"accepted": True, "data": {"action": "work", "target": "c1", "minutes": 0}})

    def test_board_summary_and_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("c01", "c02"):
                store = sapper.Store(root / name / ".sapper")
                store.init("t", f"experiment {name}")
            sapper.Store(root / "c01" / ".sapper").add_fact(
                "finding recorded: r1", "agent",
                kind="glitch_result",
                data={"value": "r1", "challenge": "c01", "status": "confirmed"})
            sapper.Store(root / "c02" / ".sapper").add_fact(
                "batch result", "agent", kind="glitch_result",
                data={"outcome": "crash"})  # no ledger keys: not a finding
            rows = sapper._board_summary(root)
            self.assertEqual([r["name"] for r in rows], ["c01", "c02"])
            by_name = {r["name"]: r for r in rows}
            self.assertEqual(by_name["c01"]["findings_confirmed"], 1)
            self.assertEqual(by_name["c02"]["findings_confirmed"], 0)
            self.assertEqual(by_name["c02"]["findings_observed"], 0)
            board = sapper.render_board(rows)
            self.assertIn("c01", board)
            self.assertIn("findings(conf/obs/rej)", board)


class FleetTests(unittest.TestCase):
    def test_fleet_pane_commands(self):
        cfg = {"session": "s", "root": "runs", "deadline": "2026-10-17T13:00:00",
               "panes": []}
        engine = sapper._sapper_engine_prefix()
        self.assertEqual(
            sapper._fleet_pane_command(cfg, {"type": "command"}),
            f"{engine} command --root runs --deadline 2026-10-17T13:00:00")
        self.assertEqual(
            sapper._fleet_pane_command(cfg, {"type": "auto:c02", "extra_args": "--verify"}),
            f"{engine} --store runs/c02/.sapper auto --deadline 2026-10-17T13:00:00 --verify")
        self.assertEqual(
            sapper._fleet_pane_command(cfg, {"type": "auto:c02"}, base_dir=Path("/w")),
            f"cd /w && {engine} --store runs/c02/.sapper auto --deadline 2026-10-17T13:00:00")
        self.assertEqual(
            sapper._fleet_pane_command({"root": "runs", "panes": []}, {"type": "shell", "command": "top"}),
            "top")
        with self.assertRaises(sapper.FGError):
            sapper._fleet_pane_command(cfg, {"type": "warp"})
        with self.assertRaises(sapper.FGError):
            sapper._fleet_pane_command(cfg, {"type": "shell"})

    def test_fleet_init_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path.cwd()
            try:
                import os
                os.chdir(tmp)
                args = type("A", (), {"action": "init", "file": "fleet.json"})()
                self.assertEqual(sapper.cmd_fleet(args), 0)
                cfg = sapper._fleet_load(Path("fleet.json"))
                self.assertTrue(cfg["panes"])
                args2 = type("A", (), {"action": "init", "file": "fleet.json"})()
                with self.assertRaises(sapper.FGError):
                    sapper.cmd_fleet(args2)
            finally:
                os.chdir(old)


class HireFireTests(unittest.TestCase):
    def test_parse_command_payload_hire_fire(self):
        hire = {"accepted": True, "data": {"action": "hire", "target": "c07", "minutes": 30}}
        self.assertEqual(sapper._parse_command_payload(hire), ("hire", "c07", 30, ""))
        fire = {"accepted": True, "data": {"action": "fire", "target": "c07", "note": "stuck"}}
        self.assertEqual(sapper._parse_command_payload(fire), ("fire", "c07", 0, "stuck"))
        with self.assertRaises(sapper.FGError):
            sapper._parse_command_payload({"accepted": True, "data": {"action": "warp", "target": "c"}})
        with self.assertRaises(sapper.FGError):
            sapper._parse_command_payload({"accepted": True, "data": {"action": "hire", "minutes": 10}})
        with self.assertRaises(sapper.FGError):
            sapper._parse_command_payload({"accepted": True, "data": {"action": "hire", "target": "c", "minutes": -1}})

    def test_active_workers_parsing(self):
        with mock.patch.object(sapper, "_tmux") as tm:
            tm.return_value = mock.Mock(returncode=1)
            self.assertEqual(sapper._active_workers("nope"), set())
            tm.return_value = mock.Mock(returncode=0, stdout="commander\nw-c01\nw-c07\nboard\n")
            self.assertEqual(sapper._active_workers("s"), {"c01", "c07"})

    def test_board_render_worker_column(self):
        rows = [
            {"name": "c01", "status": "active",
             "findings_confirmed": 1, "findings_observed": 0, "findings_rejected": 0,
             "kinds": "1 glitch_result", "open_intents": 2, "last_activity": "t",
             "goal": "g", "worker": "yes"},
            {"name": "c02", "status": "active",
             "findings_confirmed": 0, "findings_observed": 0, "findings_rejected": 0,
             "kinds": "-", "open_intents": 0, "last_activity": "t", "goal": "g", "worker": "-"},
        ]
        board = sapper.render_board(rows)
        self.assertIn("worker", board)
        self.assertIn("worker=yes means", board)


import os  # noqa: E402  (KeyRotationTests)


class KeyRotationTests(unittest.TestCase):
    def test_quota_matcher(self):
        self.assertTrue(sapper._is_quota_error("insufficient balance (quota exceeded)"))
        self.assertTrue(sapper._is_quota_error("Error 401: Unauthorized"))
        self.assertTrue(sapper._is_quota_error("账户欠费，请充值"))
        self.assertFalse(sapper._is_quota_error("connection timed out"))
        self.assertFalse(sapper._is_quota_error("claude exited 2: bad args"))

    def test_pool_load_and_rotate(self):
        pool_json = json.dumps([
            {"base_url": "http://a", "token": "t1", "model": "m1"},
            {"base_url": "http://b", "token": "t2", "model": "m2"},
        ])
        with mock.patch.dict("os.environ", {
            "SAPPER_KEY_POOL": pool_json, "ANTHROPIC_AUTH_TOKEN": "t1",
            "ANTHROPIC_BASE_URL": "http://a", "SAPPER_MODEL": "m1"}):
            self.assertTrue(sapper._rotate_key())
            self.assertEqual(os.environ["ANTHROPIC_AUTH_TOKEN"], "t2")
            self.assertEqual(os.environ["ANTHROPIC_BASE_URL"], "http://b")
            self.assertEqual(os.environ["SAPPER_MODEL"], "m2")
        with mock.patch.dict("os.environ", {"SAPPER_KEY_POOL": "not json"}):
            self.assertEqual(sapper._load_key_pool(), [])
        with mock.patch.dict("os.environ", {"SAPPER_KEY_POOL": ""}):
            self.assertFalse(sapper._rotate_key())
        # single-entry pool: rotate returns True but keeps the key
        with mock.patch.dict("os.environ", {
            "SAPPER_KEY_POOL": json.dumps([{"base_url": "http://a", "token": "t1"}]),
            "ANTHROPIC_AUTH_TOKEN": "t1"}):
            self.assertTrue(sapper._rotate_key())
            self.assertEqual(os.environ["ANTHROPIC_AUTH_TOKEN"], "t1")


class HookExecutionTests(unittest.TestCase):
    """Regression for #2: the hook script itself must run and emit valid JSON.

    SettingsPatchTests only asserted the command string was written; the hook
    binary was never executed (its fg->sapper rename import bug shipped silently
    because Claude Code swallows hook stderr).
    """

    def _run_hook(self, event, cwd, stdin=None):
        import subprocess
        proc = subprocess.run(
            [sys.executable, str(ROOT / "lib" / "sapper-hook.py"), event],
            capture_output=True, text=True, timeout=30,
            cwd=cwd, input=stdin,
        )
        return proc

    def test_sessionstart_and_userpromptsubmit_execute_and_emit_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = sapper.Store(Path(tmp) / ".sapper")
            store.init("hooktest", "verify hook executes")
            for event, stdin in (("sessionstart", None),
                                 ("userpromptsubmit", '{"prompt": "hi"}')):
                proc = self._run_hook(event, tmp, stdin)
                self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                payload = json.loads(proc.stdout)
                outer = payload["hookSpecificOutput"]
                self.assertEqual(outer["hookEventName"].lower(), event)
                ctx = outer.get("additionalContext") or outer.get("contextForClaude")
                self.assertIn("fact-graph", ctx)
