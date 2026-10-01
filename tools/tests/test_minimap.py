"""`minimap.py`: crossed axes, a park slot judged on x and z, and a check
that fails for a map without constants or a projection that does not fit."""
from __future__ import annotations

import json

import pyarrow as pa
import pyarrow.parquet as pq

from support import TempDirTestCase
import minimap

MAP = "/Game/Maps/Test/Test"
CONSTANTS = {"xMultiplier": 2.0, "yMultiplier": 3.0, "xScalarToAdd": 10.0, "yScalarToAdd": 20.0}


class MinimapTests(TempDirTestCase):
    def setUp(self):
        self.root = self.tmp()

    def export(self, positions, constants):
        export = self.root / "export"
        export.mkdir(exist_ok=True)
        (export / "manifest.json").write_text(json.dumps(
            {"level_names_and_times": [{"name": MAP, "time_ms": 0}]}), encoding="utf-8")
        pq.write_table(pa.table({axis: pa.array([p[i] for p in positions], pa.float32())
                                 for i, axis in enumerate(("pos_x", "pos_y", "pos_z"))}),
                       export / "movement.parquet")
        maps = self.root / "maps.json"
        maps.write_text(json.dumps({"data": [{"mapUrl": MAP, **constants}]}), encoding="utf-8")
        return minimap.main(["--export", str(export), "--maps", str(maps)])

    def test_pos_y_drives_u_and_pos_x_drives_v(self):
        self.assertEqual(minimap.project(1.0, 100.0, CONSTANTS), (210.0, 23.0))

    def test_the_park_slot_needs_both_x_and_z(self):
        self.assertTrue(minimap.parked(-50000.0, -49900.0))
        self.assertFalse(minimap.parked(500.0, -49900.0))  # a fall through the slot's height
        self.assertFalse(minimap.parked(-50000.0, 100.0))

    def test_a_map_without_constants_is_refused(self):
        maps = self.root / "maps.json"
        maps.write_text(json.dumps([{"mapUrl": MAP, **dict.fromkeys(CONSTANTS, 0)}]), encoding="utf-8")
        with self.assertRaises(KeyError):
            minimap.load_constants(maps, MAP)
        with self.assertRaises(KeyError):
            minimap.load_constants(maps, "/Game/Maps/Other/Other")
        self.assertEqual(self.export([(0, 0, 0)], dict.fromkeys(CONSTANTS, 0)), 1)

    def test_the_check_passes_a_fitting_projection_and_ignores_parked_rows(self):
        # Asymmetric: |pos_y| <= 500 and |pos_x| <= 5000 fit, so only the crossed order passes.
        fitting = {"xMultiplier": 0.001, "yMultiplier": -0.0001, "xScalarToAdd": 0.5, "yScalarToAdd": 0.5}
        positions = [(4000, 400, 0), (-300, -450, 50), (-50000, 0, -49900)]
        self.assertEqual(self.export(positions, fitting), 0)
        self.assertEqual(self.export(positions + [(100, 5000, 0)], fitting), 1)
