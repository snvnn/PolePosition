use crate::{Config, Identity, cyclic_delta, model::Model, wrap};
use serde::{Deserialize, Serialize};

fn live_status() -> String {
    "live".into()
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Observation {
    pub sequence: u64,
    pub segment_id: u64,
    pub elapsed_ns: u64,
    pub physics_changed_ns: u64,
    pub graphics_changed_ns: u64,
    pub physics_packet_id: i32,
    pub graphics_packet_id: i32,
    pub position_x: f64,
    pub position_y: f64,
    pub position_z: f64,
    pub speed_kmh: f64,
    pub heading_rad: f64,
    pub lap_valid: bool,
    pub in_pit: bool,
    pub completed_laps: i32,
    pub session_type: i32,
    pub session_index: i32,
    #[serde(default = "live_status")]
    pub game_status: String,
    #[serde(skip)]
    pub identity: Option<Identity>,
}
#[derive(Debug, Clone, Serialize)]
pub struct State {
    pub sequence: Option<u64>,
    pub segment_id: Option<u64>,
    pub observation_ns: Option<u64>,
    pub now_ns: u64,
    pub tracking_epoch: u64,
    pub status: String,
    pub reason: String,
    pub observation_age_ns: Option<u64>,
    pub physics_age_ns: Option<u64>,
    pub graphics_age_ns: Option<u64>,
    pub position_x: Option<f64>,
    pub position_y: Option<f64>,
    pub position_z: Option<f64>,
    pub speed_kmh: Option<f64>,
    pub lap_valid: Option<bool>,
    pub in_pit: Option<bool>,
    pub segment_index: Option<usize>,
    pub fraction: Option<f64>,
    pub projected_x: Option<f64>,
    pub projected_y: Option<f64>,
    pub projected_z: Option<f64>,
    pub projection_distance_m: Option<f64>,
    pub s_m: Option<f64>,
    pub accumulated_m: Option<f64>,
    pub lateral_error_m: Option<f64>,
    pub vehicle_heading_rad: Option<f64>,
    pub path_heading_rad: Option<f64>,
    pub heading_error_rad: Option<f64>,
    pub curvature_per_m: Option<f64>,
    pub forward_curvature_json: String,
}
#[derive(Debug, Clone, Copy)]
struct Projection {
    i: usize,
    u: f64,
    x: f64,
    y: f64,
    z: f64,
    d: f64,
    s: f64,
    tx: f64,
    tz: f64,
}

pub struct Processor {
    pub model: Model,
    pub config: Config,
    identity: Identity,
    sign: f64,
    offset: f64,
    last: Option<Observation>,
    previous: Option<(Observation, Projection)>,
    epoch: u64,
    count: usize,
    accumulated: f64,
    clock: Option<u64>,
    expired: bool,
    reset_reason: String,
}
impl Processor {
    pub fn new(model: Model, config: Config, identity: Identity, sign: f64, offset: f64) -> Self {
        Self {
            model,
            config,
            identity,
            sign,
            offset,
            last: None,
            previous: None,
            epoch: 0,
            count: 0,
            accumulated: 0.0,
            clock: None,
            expired: false,
            reset_reason: "initial_acquisition".into(),
        }
    }
    pub fn reset(&mut self, reason: &str) {
        self.previous = None;
        self.last = None;
        self.count = 0;
        self.accumulated = 0.0;
        self.expired = false;
        self.reset_reason = reason.into();
    }
    fn empty(&self, o: Option<&Observation>, now: u64, status: &str, reason: &str) -> State {
        State {
            sequence: o.map(|x| x.sequence),
            segment_id: o.map(|x| x.segment_id),
            observation_ns: o.map(|x| x.elapsed_ns),
            now_ns: now,
            tracking_epoch: self.epoch,
            status: status.into(),
            reason: reason.into(),
            observation_age_ns: o.and_then(|x| now.checked_sub(x.elapsed_ns)),
            physics_age_ns: o.and_then(|x| now.checked_sub(x.physics_changed_ns)),
            graphics_age_ns: o.and_then(|x| now.checked_sub(x.graphics_changed_ns)),
            position_x: o.map(|x| x.position_x).filter(|x| x.is_finite()),
            position_y: o.map(|x| x.position_y).filter(|x| x.is_finite()),
            position_z: o.map(|x| x.position_z).filter(|x| x.is_finite()),
            speed_kmh: o.map(|x| x.speed_kmh).filter(|x| x.is_finite()),
            lap_valid: o.map(|x| x.lap_valid),
            in_pit: o.map(|x| x.in_pit),
            segment_index: None,
            fraction: None,
            projected_x: None,
            projected_y: None,
            projected_z: None,
            projection_distance_m: None,
            s_m: None,
            accumulated_m: None,
            lateral_error_m: None,
            vehicle_heading_rad: None,
            path_heading_rad: None,
            heading_error_rad: None,
            curvature_per_m: None,
            forward_curvature_json: "[]".into(),
        }
    }
    fn invalid(&mut self, o: &Observation, now: u64, reason: &str) -> State {
        self.previous = None;
        self.count = 0;
        self.accumulated = 0.0;
        self.reset_reason = reason.into();
        self.empty(Some(o), now, "invalid", reason)
    }
    pub fn reset_state(&mut self, reason: &str, now: u64) -> State {
        self.reset(reason);
        self.empty(None, now, "unavailable", reason)
    }
    pub fn tick(&mut self, now: u64) -> Option<State> {
        if self.clock.is_some_and(|t| now < t) {
            self.reset("time_reversal");
            self.clock = Some(now);
            return Some(self.empty(None, now, "invalid", "time_reversal"));
        }
        self.clock = Some(now);
        let o = self.last.clone()?;
        if !self.expired
            && [o.elapsed_ns, o.physics_changed_ns, o.graphics_changed_ns]
                .iter()
                .any(|t| now.saturating_sub(*t) as f64 >= self.config.max_age_ms * 1e6)
        {
            self.expired = true;
            return Some(self.invalid(&o, now, "stale"));
        }
        None
    }
    fn project(&self, o: &Observation, i: usize) -> Projection {
        let a = &self.model.points[i];
        let b = &self.model.points[(i + 1) % self.model.points.len()];
        let dx = b.position_x - a.position_x;
        let dz = b.position_z - a.position_z;
        let u = (((o.position_x - a.position_x) * dx + (o.position_z - a.position_z) * dz)
            / (dx * dx + dz * dz))
            .clamp(0.0, 1.0);
        let x = a.position_x + u * dx;
        let z = a.position_z + u * dz;
        let tx = a.tangent_x + (b.tangent_x - a.tangent_x) * u;
        let tz = a.tangent_z + (b.tangent_z - a.tangent_z) * u;
        let norm = tx.hypot(tz);
        Projection {
            i,
            u,
            x,
            y: a.position_y + u * (b.position_y - a.position_y),
            z,
            d: (o.position_x - x).hypot(o.position_z - z),
            s: (a.s_m + u * dx.hypot(dz)).rem_euclid(self.model.length),
            tx: tx / norm,
            tz: tz / norm,
        }
    }
    pub fn update(&mut self, o: Observation, now: u64) -> State {
        if self.clock.is_some_and(|t| now < t)
            || now < o.elapsed_ns
            || o.physics_changed_ns > o.elapsed_ns
            || o.graphics_changed_ns > o.elapsed_ns
        {
            self.clock = Some(now);
            self.last = None;
            return self.invalid(&o, now, "time_reversal");
        }
        self.clock = Some(now);
        if o.game_status != "live" {
            self.reset(&o.game_status);
            return self.empty(Some(&o), now, "unavailable", &o.game_status);
        }
        if o.identity.as_ref() != Some(&self.identity) {
            return self.invalid(&o, now, "identity_mismatch");
        }
        if let Some(last) = self.last.clone() {
            if o.elapsed_ns < last.elapsed_ns {
                return self.invalid(&o, now, "time_reversal");
            }
            if (o.physics_packet_id, o.graphics_packet_id)
                == (last.physics_packet_id, last.graphics_packet_id)
            {
                if let Some(s) = self.tick(now) {
                    return s;
                }
                return self.empty(Some(&last), now, "waiting", "duplicate_packets");
            }
            if o.elapsed_ns == last.elapsed_ns {
                return self.invalid(&o, now, "time_reversal");
            }
        }
        self.last = Some(o.clone());
        self.expired = false;
        if [
            o.position_x,
            o.position_y,
            o.position_z,
            o.speed_kmh,
            o.heading_rad,
        ]
        .iter()
        .any(|v| !v.is_finite())
            || o.speed_kmh < 0.0
        {
            return self.invalid(&o, now, "invalid_numeric");
        }
        if [o.elapsed_ns, o.physics_changed_ns, o.graphics_changed_ns]
            .iter()
            .any(|t| (now - *t) as f64 >= self.config.max_age_ms * 1e6)
        {
            self.expired = true;
            return self.invalid(&o, now, "stale");
        }
        if let Some((prev, _)) = &self.previous
            && (prev.segment_id != o.segment_id
                || prev.session_type != o.session_type
                || prev.session_index != o.session_index
                || o.completed_laps < prev.completed_laps)
        {
            self.previous = None;
            self.count = 0;
            self.reset_reason = "segment_or_session_change".into();
        }
        let allowance = self.previous.as_ref().map(|(prev, _)| {
            self.config.movement_multiplier * prev.speed_kmh.max(o.speed_kmh) / 3.6
                * (o.elapsed_ns - prev.elapsed_ns) as f64
                / 1e9
                + self.config.movement_margin_m
        });
        if let Some((prev, _)) = &self.previous {
            if (o.elapsed_ns - prev.elapsed_ns) as f64 >= self.config.max_age_ms * 1e6 {
                return self.invalid(&o, now, "observation_gap");
            }
            if (o.position_x - prev.position_x).hypot(o.position_z - prev.position_z)
                > allowance.unwrap()
            {
                return self.invalid(&o, now, "position_jump");
            }
        }
        let mut global = self.project(&o, 0);
        let mut local: Option<Projection> = None;
        for i in 0..self.model.points.len() {
            let p = self.project(&o, i);
            if p.d < global.d {
                global = p;
            }
            if let Some((_, previous)) = &self.previous
                && cyclic_delta(p.s, previous.s, self.model.length).abs() <= allowance.unwrap()
                && local.is_none_or(|q| p.d < q.d)
            {
                local = Some(p);
            }
        }
        let p = if self.previous.is_some() {
            let Some(local) = local else {
                return self.invalid(&o, now, "continuity_lost");
            };
            if global.d + self.config.ambiguity_m < local.d {
                return self.invalid(&o, now, "global_branch_conflict");
            }
            local
        } else {
            global
        };
        if p.d > self.config.max_projection_m {
            return self.invalid(&o, now, "outside_projection_range");
        }
        if !p.tx.is_finite() || !p.tz.is_finite() {
            return self.invalid(&o, now, "degenerate_tangent");
        }
        if let Some((_, previous)) = &self.previous {
            self.accumulated += cyclic_delta(p.s, previous.s, self.model.length);
        } else {
            for i in 0..self.model.points.len() {
                let other = self.project(&o, i);
                if cyclic_delta(other.s, p.s, self.model.length).abs()
                    >= self.config.branch_separation_m
                    && other.d <= p.d + self.config.ambiguity_m
                {
                    return self.invalid(&o, now, "ambiguous_projection");
                }
            }
            self.epoch += 1;
            self.accumulated = 0.0;
        }
        self.count += 1;
        let ready = self.count >= self.config.acquisition_samples;
        let mut s = self.empty(
            Some(&o),
            now,
            if ready { "ready" } else { "acquiring" },
            if ready { "" } else { &self.reset_reason },
        );
        s.segment_index = Some(p.i);
        s.fraction = Some(p.u);
        s.projected_x = Some(p.x);
        s.projected_y = Some(p.y);
        s.projected_z = Some(p.z);
        s.projection_distance_m = Some(p.d);
        s.s_m = Some(p.s);
        s.accumulated_m = Some(self.accumulated);
        s.lateral_error_m = Some(p.tx * (o.position_z - p.z) - p.tz * (o.position_x - p.x));
        let vehicle = wrap(self.sign * o.heading_rad + self.offset);
        let path = p.tz.atan2(p.tx);
        s.vehicle_heading_rad = Some(vehicle);
        s.path_heading_rad = Some(path);
        s.heading_error_rad = Some(wrap(vehicle - path));
        s.curvature_per_m = Some(self.model.curvature(p.s));
        s.forward_curvature_json = serde_json::to_string(
            &self
                .config
                .curvature_offsets_m
                .iter()
                .map(|d| self.model.curvature(p.s + d))
                .collect::<Vec<_>>(),
        )
        .unwrap();
        self.previous = Some((o, p));
        s
    }
}
