"""Output publishers must keep the previous complete file on write failure."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock


from support import TempDirTestCase, run_cli
import atomic_io
import bench_export
import check_metrics_baseline
import compare_with_csharp
import extract_active_effects
import extract_spike_carrier


class AtomicOutputTests(TempDirTestCase):
    OLD = "previous complete output\n"

    def assert_preserved_when_replace_fails(
        self, output: Path, operation, *, previous: str | None = None
    ) -> None:
        previous = self.OLD if previous is None else previous
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(previous, encoding="utf-8")
        with mock.patch.object(
            atomic_io.os, "replace", side_effect=OSError("simulated replace failure")
        ):
            with self.assertRaisesRegex(OSError, "simulated replace failure"):
                operation()
        self.assertEqual(output.read_text(encoding="utf-8"), previous)

    def test_json_cli_refuses_inputs_keeps_hard_links_and_the_old_file(self):
        root = self.tmp()
        export = root / "export"
        export.mkdir()
        table, source, out = export / "fields.parquet", root / "tool.py", root / "out.json"
        for path in (table, export / "manifest.json", source):
            path.write_text("input", encoding="utf-8")

        def run(target):
            return run_cli(lambda argv: atomic_io.run_json_cli("", lambda e: {"n": 1}, lambda d, o: ["ok"],
                                                               argv, sources=[source]),
                           "--export", export, "--out", target)[0]

        for target in (table, export / "manifest.json", source):
            with self.subTest(target=target.name):
                self.assertEqual(run(target), 1)
                self.assertEqual(target.read_text(encoding="utf-8"), "input")
        # A hard link to an input is replaced by name; the input keeps its bytes.
        os.link(table, out)
        self.assertEqual(run(out), 0)
        self.assertEqual((table.read_text(encoding="utf-8"), out.read_text(encoding="utf-8")),
                         ("input", '{"n": 1}\n'))
        out.write_text(self.OLD, encoding="utf-8")
        with mock.patch.object(atomic_io.os, "replace", side_effect=OSError("simulated")):
            self.assertEqual(run(out), 1)
        self.assertEqual(out.read_text(encoding="utf-8"), self.OLD)

    def test_benchmark_baseline_update_preserves_previous_file(self):
        root = self.tmp()
        executable = root / "vrfkit"
        replay = root / "match.vrf"
        executable.write_bytes(b"exe")
        replay.write_bytes(b"replay")
        baseline = root / "bench.json"

        def update():
            with mock.patch.object(bench_export, "time_export", return_value=[1.0]):
                run_cli(bench_export.main, "--exe", executable, "--replay", replay, "--baseline", baseline,
                        "--repeats", "1", "--update", prog="bench_export.py")

        self.assert_preserved_when_replace_fails(
            baseline, update, previous='{"sentinel": "previous"}\n'
        )

    def test_metrics_baseline_update_preserves_previous_file(self):
        root = self.tmp()
        executable = root / "vrfkit"
        compute_metrics = root / "compute_metrics.py"
        replay = root / "match.vrf"
        for path in (executable, compute_metrics, replay):
            path.write_bytes(b"fixture")
        baseline = root / "metrics.json"
        healthy = {
            "rounds_rpc": 1,
            "rounds_objective": 1,
            "team_score": {"Blue": 1},
            "players": 1,
            "kills": 0,
            "damage_dealt": 0,
        }

        def update():
            with mock.patch.multiple(check_metrics_baseline, COMPUTE_METRICS=compute_metrics,
                                     REPLAYS={"13.01": str(replay)},
                                     run_one=mock.Mock(return_value=("13.01", healthy, ""))):
                run_cli(check_metrics_baseline.main, "--exe", executable, "--baseline", baseline,
                        "--jobs", "1", "--update", prog="check_metrics_baseline.py")

        self.assert_preserved_when_replace_fails(
            baseline, update, previous='{"metrics": {"old": {}}}\n'
        )

    def test_comparison_report_preserves_previous_file(self):
        root = self.tmp()
        csharp = root / "csharp"
        vrfkit = root / "vrfkit"
        csharp.mkdir()
        vrfkit.mkdir()
        (csharp / "manifest.json").write_text("{}", encoding="utf-8")
        (vrfkit / "manifest.json").write_text("{}", encoding="utf-8")
        report = vrfkit / "comparison_report.txt"

        stubs = {"compare_totals": "totals", "compare_group_paths": "groups",
                 "compare_group_field_coverage": ("coverage", []), "compare_rpc_names": "rpcs",
                 "compare_movement": "movement", "compare_raw_blobs": "raw"}

        def generate():
            with mock.patch.multiple(compare_with_csharp,
                                     **{name: mock.Mock(return_value=v) for name, v in stubs.items()}):
                run_cli(compare_with_csharp.main, csharp, vrfkit, prog="compare_with_csharp.py")

        self.assert_preserved_when_replace_fails(report, generate)

    def run_parquet_cli(self, module, build_name, built, output):
        """Run one Parquet-writing CLI on a stubbed build() result."""
        with mock.patch.object(module, build_name, return_value=built):
            run_cli(module.main, "--export", output.parent, "--out", output, prog=f"{module.__name__}.py")

    def test_spike_carrier_parquet_preserves_previous_file(self):
        temp = self.tmp()
        output = temp / "spike_carrier.parquet"
        self.assert_preserved_when_replace_fails(
            output,
            lambda: self.run_parquet_cli(
                extract_spike_carrier, "build", ([], {}, 0, {}), output
            ),
        )

    def test_active_effects_parquet_preserves_previous_file(self):
        temp = self.tmp()
        output = temp / "active_effects.parquet"
        self.assert_preserved_when_replace_fails(
            output,
            lambda: self.run_parquet_cli(
                extract_active_effects, "build_with_tally",
                ([], {"went_dormant": 0}), output
            ),
        )
