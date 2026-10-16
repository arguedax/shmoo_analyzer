import tempfile
import unittest
from pathlib import Path

import sys

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import shmoo_nav


class CoordinateLegendTests(unittest.TestCase):
    def count_legends(self):
        return {
            f"[{x}^{y}^{n}]": shmoo_nav.parse_legend("pat|1:plist:domain(3,4):pin")
            for y, counts in enumerate([[10] * 5, [10, 10, 5, 0, 0], [0] * 5])
            for x, count in enumerate(counts)
            for n in range(1, count + 1)
        }

    def test_decodes_variable_width_decimal_counts_without_shifting_points(self):
        params = shmoo_nav.parse_params("X^0^4^1^Y^0^2^1_ShmooHub")

        x, y, rows, _, _ = shmoo_nav.build_grid(
            params,
            "1010101010_10105**_*****",
            coordinate_legends=self.count_legends(),
        )

        self.assertEqual(x, [0, 1, 2, 3, 4])
        self.assertEqual(y, [0, 1, 2])
        self.assertEqual(rows[0], ["10"] * 5)
        self.assertEqual(rows[1], ["10", "10", "5", "*", "*"])
        self.assertEqual(rows[2], ["*"] * 5)
        summary = shmoo_nav.summarize(y, rows)
        self.assertEqual(summary["fails"], 8)
        self.assertEqual(summary["total"], 15)

    def test_rejects_results_that_disagree_with_legend_counts(self):
        params = shmoo_nav.parse_params("X^0^4^1^Y^0^2^1_ShmooHub")
        with self.assertRaisesRegex(ValueError, "does not match coordinate legend counts"):
            shmoo_nav.build_grid(params, "1010101010_10106**_*****", self.count_legends())

    def test_matches_coordinate_legends_and_keeps_suffix_separate_from_pins(self):
        legend = {
            "[0.0^0.0^2]": shmoo_nav.parse_legend("pat|1:plist:domain(3,4,-1):pin_a,pin_b|InvalidDecoder"),
            "[4.0^2.0^1]": shmoo_nav.parse_legend("pat2|2:plist:domain(5,6,-1):pin_c|1010"),
        }

        cells = shmoo_nav.map_coordinate_legends(legend, [0, 1, 2, 3, 4], [0, 1, 2])

        self.assertEqual([item["index"] for item in cells["0,0"]], [2])
        self.assertEqual(cells["0,0"][0]["pins"], "pin_a,pin_b")
        self.assertEqual(cells["0,0"][0]["suffix"], "InvalidDecoder")
        self.assertEqual(cells["2,4"][0]["suffix"], "1010")

    def test_rejects_coordinate_legend_outside_axes(self):
        legend = {"[9.0^0.0^1]": shmoo_nav.parse_legend("pat|1:plist:domain(3,4):pin")}

        with self.assertRaisesRegex(ValueError, "do not match the shmoo axes"):
            shmoo_nav.map_coordinate_legends(legend, [0, 1], [0, 1])

    def test_parses_coordinate_legend_records_from_ituff(self):
        contents = "\n".join(
            [
                "2_lbeg",
                "0_tssid_U1",
                "0_tname_TEST_ShmooParams",
                "0_strgval_X^0^4^1^Y^0^2^1_ShmooHub",
                "0_tname_TEST_ShmooResults",
                "0_strgval_1****_*****_*****",
                "0_tname_TEST^LEGEND^[0^0^1]",
                "0_strgval_pat|1:plist:domain(3,4,-1):pin|InvalidDecoder",
                "2_lend",
                "",
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.itf"
            path.write_text(contents, encoding="utf-8")
            parsed = shmoo_nav.parse_ituff(path, None)

        unit = parsed["units"][0]
        rec = next(iter(unit["shmoos"].values()))
        self.assertIn("[0^0^1]", rec["legend"])
        x, y, rows, _, _ = shmoo_nav.build_grid(rec["params"], rec["results"], rec["legend"])
        cells = shmoo_nav.map_coordinate_legends(rec["legend"], x, y)
        self.assertEqual(rows[0], ["1", "*", "*", "*", "*"])
        self.assertEqual(cells["0,0"][0]["suffix"], "InvalidDecoder")

    def test_keeps_character_legends_and_single_symbol_grid_behavior(self):
        params = shmoo_nav.parse_params("X^0^1^1^Y^0^1^1_ShmooHub")

        _, _, rows, _, _ = shmoo_nav.build_grid(params, "A*_*B")

        self.assertEqual(rows, [["A", "*"], ["*", "B"]])


if __name__ == "__main__":
    unittest.main()
