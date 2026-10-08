"""Shared finish-line detection; callers validate freshness and continuity first."""


class FinishLineTracker:
    """Keep the recorder's existing ACC counter/hotlap timer crossing rules."""

    def __init__(self):
        self.previous = None
        self.timer_reset = None
        self.last_boundary = None
        self.lap_ok = True

    def observe(self, row):
        previous = self.previous
        elapsed = row['elapsed_ns']
        boundary = None
        discontinuity = False
        if previous is None or row['segment_id'] != previous['segment_id']:
            self.timer_reset = None
            self.last_boundary = None
            self.lap_ok = True
        else:
            delta = row['completed_laps'] - previous['completed_laps']
            near_line = previous['progress'] > .8 or row['progress'] < .2
            crossed = delta == 1 and near_line
            if row['lap_time_ms'] + 2000 < previous['lap_time_ms'] and delta == 0 and near_line:
                self.timer_reset = elapsed
            if self.timer_reset is not None:
                if previous['progress'] > .8 and row['progress'] < .2 and elapsed - self.timer_reset < 1_000_000_000:
                    crossed = True
                    self.timer_reset = None
                elif delta != 0 or elapsed - self.timer_reset >= 1_000_000_000:
                    self.timer_reset = None
            if crossed and (self.last_boundary is None or elapsed - self.last_boundary >= 5_000_000_000):
                boundary = (row['segment_id'], elapsed, row['completed_laps'], self.lap_ok)
                self.lap_ok = True
                self.last_boundary = elapsed
            discontinuity = delta < 0 or delta > 1 or (delta == 1 and not near_line)
        self.lap_ok = self.lap_ok and row['lap_valid']
        self.previous = dict(row)
        return boundary, discontinuity
