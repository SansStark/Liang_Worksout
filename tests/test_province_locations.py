import datetime as dt
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import polyline

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "run_page"))

from generator.db import Activity  # noqa: E402
from province_locations import activity_province, in_ring, province_at  # noqa: E402


class ProvinceLocationTest(unittest.TestCase):
    def test_known_mainland_and_island_start_points(self):
        points = [
            (39.9, 116.4, "北京市"),
            (31.23, 121.47, "上海市"),
            (23.13, 113.26, "广东省"),
            (36.62, 101.78, "青海省"),
            (29.65, 91.13, "西藏自治区"),
            (25.03, 121.56, "台湾省"),
            (22.28, 114.16, "香港特别行政区"),
            (22.15, 113.56, "澳门特别行政区"),
        ]
        for lat, lon, expected in points:
            with self.subTest(expected=expected):
                self.assertEqual(province_at(lat, lon), expected)

    def test_missing_invalid_and_foreign_routes_are_not_assigned(self):
        for route in (None, "", "_", polyline.encode([(40.71, -74.0)])):
            self.assertIsNone(activity_province(None, route))

    def test_start_point_defines_the_activity_province(self):
        route = polyline.encode([(39.9, 116.4), (31.23, 121.47)])
        self.assertEqual(activity_province(None, route), "北京市")

    def test_existing_location_takes_precedence_without_rewriting_it(self):
        route = polyline.encode([(39.9, 116.4)])
        for location in (
            "西湖区, 杭州市, 浙江省, 中国",
            "{'country': '中国', 'province': '浙江省', 'city': '杭州市'}",
            '{"country":"China","province":"浙江省"}',
        ):
            self.assertEqual(activity_province(location, route), "浙江省")
        self.assertIsNone(
            activity_province('{"country":"USA","province":"浙江省"}', None)
        )

    def test_polygon_holes_and_outer_boundaries(self):
        outer = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
        hole = [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]]
        self.assertTrue(in_ring(0, 5, outer))
        self.assertFalse(in_ring(11, 5, outer))
        province_at.cache_clear()
        try:
            with patch(
                "province_locations.province_boundaries",
                return_value=[("Test", (0, 0, 10, 10), [outer, hole])],
            ):
                self.assertEqual(province_at(2, 2), "Test")
                self.assertIsNone(province_at(5, 5))
        finally:
            province_at.cache_clear()

    def test_every_export_preserves_location_and_generates_province(self):
        original = "西城区, 北京市, 中国"
        activity = Activity(
            run_id=1,
            name="Test",
            moving_time=dt.timedelta(minutes=30),
            location_country=original,
            summary_polyline=None,
        )
        exported = activity.to_dict()
        self.assertEqual(exported["location_country"], original)
        self.assertEqual(exported["province"], "北京市")
        self.assertEqual(json.loads(json.dumps(exported))["province"], "北京市")


if __name__ == "__main__":
    unittest.main()
