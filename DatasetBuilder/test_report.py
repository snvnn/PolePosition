"""Regression checks for displayed coordinates, not just SVG well-formedness."""
import math
import unittest
import xml.etree.ElementTree as ET

from .report import chart

NS = {'s': 'http://www.w3.org/2000/svg'}


class ChartCoordinateTests(unittest.TestCase):
    def check_projection(self, points, equal):
        rows = [dict(position_x=x, position_z=y) for x, y in points]
        svg = ET.fromstring(chart(rows, 'position_x', 'position_z', 'test', 10, equal)).find('s:svg', NS)
        ticks = {axis: svg.findall(f's:text[@class="{axis}-tick"]', NS) for axis in ('x', 'y')}
        x0, x1 = ticks['x'][0], ticks['x'][-1]
        y0, y1 = ticks['y'][0], ticks['y'][-1]
        sx = (float(x1.attrib['x'])-float(x0.attrib['x'])) / (float(x1.text)-float(x0.text))
        sy = (float(y1.attrib['y'])-float(y0.attrib['y'])) / (float(y0.text)-float(y1.text))
        if equal:
            # Label precision is 0.001 on X and 0.01 on Y. Allow only the
            # rounding uncertainty of the two displayed endpoint differences.
            xspan = float(x1.text)-float(x0.text)
            yspan = float(y0.text)-float(y1.text)
            xscale = (630/(xspan+.001), 630/(xspan-.001))
            yscale = (180/(yspan+.01), 180/(yspan-.01))
            self.assertLessEqual(max(xscale[0], yscale[0]), min(xscale[1], yscale[1]))
        plotted = [tuple(map(float, p.split(','))) for p in svg.find('s:polyline', NS).attrib['points'].split()]
        for (x, y), (px, py) in zip(points, plotted):
            # Recover source coordinates exclusively from the displayed tick labels.
            self.assertAlmostEqual(float(x0.text)+(px-float(x0.attrib['x']))/sx, x, delta=.02)
            self.assertAlmostEqual(float(y0.text)-(py-float(y0.attrib['y']))/sy, y, delta=.02)
        return plotted

    def test_tall_track_padded_x_ticks_match_data(self):
        self.check_projection([(10, 20), (20, 60), (15, 40)], True)

    def test_wide_track_padded_y_ticks_match_data(self):
        self.check_projection([(-200, -5), (200, 5), (0, 0)], True)

    def test_signal_uses_independent_scales(self):
        self.check_projection([(0, -1), (.5, 0), (1, 1)], False)

    def test_stationary_point_is_finite_and_centered(self):
        svg = ET.fromstring(chart([dict(x=0, y=0)], 'x', 'y', 'test', 10, True)).find('s:svg', NS)
        point = tuple(map(float, svg.find('s:polyline', NS).attrib['points'].split(',')))
        self.assertTrue(all(math.isfinite(v) for v in point))
        self.assertEqual(point, (405, 115))

    def test_progress_wrap_still_breaks_polyline(self):
        svg = ET.fromstring(chart([dict(progress=.99, speed=50), dict(progress=.01, speed=55)],
                                  'progress', 'speed', 'test', 10)).find('s:svg', NS)
        self.assertEqual(len(svg.findall('s:polyline', NS)), 2)


if __name__ == '__main__':
    unittest.main()
